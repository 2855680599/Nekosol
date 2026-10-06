"""M0 writer worker: lifecycle, protocol limits, failure modes, security boundary.

Covers 搂9 of the P2-B closeout order. Real uid/gid semantics are POSIX-only, so
tests that need a genuinely downgraded process are gated on POSIX; the protocol
and rejection logic runs everywhere.

Every failure case asserts the same invariant from a different angle: a failure
must never be reported as a successful write.
"""
from __future__ import annotations

from pathlib import Path
import json
import os
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
for _candidate in (ROOT, ROOT / "src"):
    if str(_candidate) not in sys.path:
        sys.path.insert(0, str(_candidate))

import m0_writer_worker as mww  # noqa: E402

POSIX = hasattr(os, "getuid") and hasattr(socket, "AF_UNIX")
HEAD = Path(mww.__file__).resolve()
STAMP = "2026-10-01T00:00:00+00:00"


def event(index: int, *, ref: str | None = None, origin: str = "USER_VISIBLE_INPUT") -> dict:
    if origin == "USER_VISIBLE_INPUT":
        payload = {"delivery_status": "RECEIVED",
                   "epistemic_role": "OBSERVED_EXTERNAL_EXPRESSION", "speaker": "user"}
    else:
        payload = {"delivery_status": "DELIVERED",
                   "epistemic_role": "SELF_EXPRESSION", "speaker": "chiyo"}
    return {"occurred_at": STAMP, "created_at": STAMP, "source_origin": origin,
            "content": "worker probe %d" % index, "conversation_id": "tg-probe",
            "turn_id": "turn-%d" % index,
            "source_refs": [{"kind": "probe", "id": ref or ("probe:%d" % index)}],
            **payload}


def count_rows(m0_db: Path) -> int:
    import sqlite3
    connection = sqlite3.connect(str(m0_db))
    try:
        return int(connection.execute(
            "SELECT COUNT(*) FROM evidence_events").fetchone()[0])
    finally:
        connection.close()


def seed_store(m0_db: Path) -> None:
    native = ROOT
    if str(native) not in sys.path:
        sys.path.insert(0, str(native))
    from app.evidence import EvidenceStore
    EvidenceStore(m0_db)


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="m0worker-"))
        self.addCleanup(mww.close_all)
        self.m0_db = self.tmp / "memory" / "evidence.sqlite"
        self.m0_db.parent.mkdir(parents=True, exist_ok=True)
        seed_store(self.m0_db)

    def worker(self, **kwargs):
        worker = mww.M0WriterWorker(self.m0_db, **kwargs)
        self.addCleanup(worker.close)
        return worker


@unittest.skipUnless(POSIX, "requires POSIX uid/unix-socket semantics")
class LifecycleTest(_Base):
    def test_worker_starts_and_reports_its_identity(self):
        worker = self.worker()
        worker.start()
        self.assertEqual(worker.state, mww.READY)
        self.assertEqual(worker.starts, 1)
        identity = worker.ping()
        self.assertEqual(identity["uid"], os.getuid())
        self.assertEqual(identity["pid"], worker.status()["pid"])
        self.assertEqual(worker.status()["state"], mww.READY)

    def test_socket_is_private_and_owned_by_the_writer(self):
        worker = self.worker()
        worker.start()
        info = os.stat(str(worker.socket_path))
        self.assertEqual(info.st_mode & 0o777, 0o600)
        self.assertEqual(info.st_uid, os.getuid())

    def test_clean_shutdown_and_repeated_close(self):
        worker = self.worker()
        worker.start()
        pid = worker.status()["pid"]
        worker.close()
        worker.close()
        worker.close()
        # a closed worker must not look alive
        self.assertIsNone(worker.status()["pid"])
        self.assertFalse(worker.socket_path.exists())
        if pid is not None:
            time.sleep(0.2)
            with self.assertRaises(OSError):
                os.kill(pid, 0)

    def test_single_append_and_duplicate_is_not_a_second_row(self):
        worker = self.worker()
        worker.start()
        first = worker.append_events([event(0)])
        self.assertEqual([r["status"] for r in first], ["inserted"])
        self.assertEqual(count_rows(self.m0_db), 1)
        # same idempotency key -> duplicate, no second row
        again = worker.append_events([event(0)])
        self.assertEqual([r["status"] for r in again], ["duplicate"])
        self.assertEqual(count_rows(self.m0_db), 1)

    def test_hundred_sequential_appends(self):
        worker = self.worker()
        worker.start()
        for index in range(100):
            result = worker.append_events([event(index)])
            self.assertEqual(result[0]["status"], "inserted")
        self.assertEqual(count_rows(self.m0_db), 100)
        # the whole point: 100 appends on ONE interpreter
        self.assertEqual(worker.starts, 1)

    def test_concurrent_appends(self):
        worker = self.worker()
        worker.start()
        errors = []

        def run(index):
            try:
                worker.append_events([event(1000 + index)])
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=run, args=(i,)) for i in range(12)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        self.assertEqual(count_rows(self.m0_db), 12)
        self.assertEqual(worker.starts, 1)


@unittest.skipUnless(POSIX, "requires POSIX uid/unix-socket semantics")
class FailureTest(_Base):
    def test_privilege_mismatch_refuses_to_serve(self):
        """A worker that is not the store owner must exit, not serve."""
        head = HEAD
        proc = subprocess.run(
            [sys.executable, "-S", str(head), "--serve", str(self.m0_db),
             str(self.tmp / "refuse.sock"), str(os.getuid() + 1)],
            capture_output=True, text=True, timeout=30)
        self.assertEqual(proc.returncode, 2)
        handshake = json.loads(proc.stdout.strip().splitlines()[0])
        self.assertFalse(handshake["ready"])
        self.assertEqual(handshake["error"], "uid_mismatch")

    def test_crash_before_request_is_reported_as_failure(self):
        worker = self.worker(fault="before_request")
        with self.assertRaises(mww.WorkerError):
            worker.ping()
        self.assertEqual(worker.state, mww.ERROR)
        self.assertIn("worker_exited", worker.last_error or "")
        self.assertEqual(count_rows(self.m0_db), 0)

    def test_crash_during_request_is_reported_as_failure(self):
        worker = self.worker(fault="during_request")
        worker.start()
        with self.assertRaises(mww.WorkerError):
            worker.append_events([event(0)])
        self.assertEqual(worker.state, mww.ERROR)
        # nothing was written, and the caller was not told otherwise
        self.assertEqual(count_rows(self.m0_db), 0)

    def test_crash_after_commit_before_response_is_not_a_lost_event(self):
        worker = self.worker(fault="after_commit")
        worker.start()
        with self.assertRaises(mww.WorkerError):
            worker.append_events([event(0)])
        # the row IS committed even though the reply never arrived...
        self.assertEqual(count_rows(self.m0_db), 1)
        # ...and the caller must not have been told it succeeded
        worker.fault = ""
        worker.start()
        retry = worker.append_events([event(0)])
        self.assertEqual([r["status"] for r in retry], ["duplicate"])
        self.assertEqual(count_rows(self.m0_db), 1)

    def test_automatic_restart_then_retry_succeeds(self):
        worker = self.worker()
        worker.start()
        self.assertEqual(worker.append_events([event(0)])[0]["status"], "inserted")
        # kill it behind the worker handle's back
        os.kill(worker.status()["pid"], 9)
        time.sleep(0.2)
        with self.assertRaises(mww.WorkerError):
            worker.append_events([event(1)])
        results = worker.append_events_retrying([event(1)])
        self.assertEqual([r["status"] for r in results], ["inserted"])
        self.assertEqual(worker.restarts, 1)
        self.assertEqual(worker.state, mww.READY)
        self.assertEqual(count_rows(self.m0_db), 2)

    def test_request_timeout_is_a_failure_not_a_success(self):
        """A peer that accepts and never answers must not look like a success."""
        silent = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        path = self.tmp / "silent.sock"
        silent.bind(str(path))
        silent.listen(1)
        self.addCleanup(silent.close)

        worker = self.worker()
        worker.start()
        # point the live handle at the silent peer
        worker._connection.close()
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        connection.settimeout(0.3)
        connection.connect(str(path))
        worker._connection = connection
        original = mww.REQUEST_TIMEOUT_S
        mww.REQUEST_TIMEOUT_S = 0.3
        try:
            with self.assertRaises(mww.WorkerError):
                worker.append_events([event(0)])
        finally:
            mww.REQUEST_TIMEOUT_S = original
        self.assertEqual(count_rows(self.m0_db), 0)
        self.assertNotEqual(worker.state, mww.READY)


class ProtocolLimitsTest(_Base):
    """Pure protocol logic: runs on any platform."""

    @staticmethod
    def _reap(proc):
        """Stop the worker and close its pipes: no leaked process or handle."""
        try:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)
        except Exception:  # noqa: BLE001 - cleanup only
            pass
        for stream in (proc.stdout, proc.stderr):
            try:
                if stream is not None:
                    stream.close()
            except Exception:  # noqa: BLE001
                pass

    def _serve_and_talk(self, payloads, *, timeout=10.0):
        """Start a real worker, send raw frames, return the replies."""
        head = HEAD
        path = self.tmp / "raw.sock"
        proc = subprocess.Popen(
            [sys.executable, "-S", str(head), "--serve", str(self.m0_db),
             str(path), str(os.getuid() if POSIX else 0)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(self._reap, proc)
        ready = proc.stdout.readline()
        if not json.loads(ready or "{}").get("ready"):
            detail = proc.stderr.read() if proc.stderr else ""
            self.fail("worker could not serve: ready=%r stderr=%s" % (ready, detail[-800:]))
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        connection.settimeout(timeout)
        connection.connect(str(path))
        self.addCleanup(connection.close)
        replies = []
        for payload in payloads:
            body = json.dumps(payload).encode("utf-8")
            connection.sendall(struct.pack(">I", len(body)) + body)
            header = connection.recv(4)
            (length,) = struct.unpack(">I", header)
            data = b""
            while len(data) < length:
                data += connection.recv(length - len(data))
            replies.append(json.loads(data.decode("utf-8")))
        return replies

    def test_unknown_op_is_rejected(self):
        replies = self._serve_and_talk([
            {"version": 1, "op": "run_sql", "request_id": "r1",
             "sql": "DELETE FROM evidence_events"}])
        self.assertEqual(replies[0]["status"], "rejected")
        self.assertEqual(replies[0]["request_id"], "r1")
        self.assertEqual(count_rows(self.m0_db), 0)

    def test_arbitrary_path_and_sql_are_unrepresentable(self):
        # an op that would take a path or SQL does not exist; an event carrying
        # extra fields is rejected rather than silently narrowed
        smuggled = event(0)
        smuggled["m0_db"] = "/etc/passwd"
        replies = self._serve_and_talk([
            {"version": 1, "op": "append_events", "request_id": "r1",
             "events": [smuggled]}])
        self.assertEqual(replies[0]["status"], "rejected")
        self.assertEqual(count_rows(self.m0_db), 0)

    def test_missing_field_is_rejected(self):
        broken = event(0)
        broken.pop("content")
        replies = self._serve_and_talk([
            {"version": 1, "op": "append_events", "request_id": "r1",
             "events": [broken]}])
        self.assertEqual(replies[0]["status"], "rejected")
        self.assertEqual(count_rows(self.m0_db), 0)

    def test_too_many_events_rejected(self):
        replies = self._serve_and_talk([
            {"version": 1, "op": "append_events", "request_id": "r1",
             "events": [event(i) for i in range(mww.MAX_EVENTS_PER_REQUEST + 1)]}])
        self.assertEqual(replies[0]["status"], "rejected")

    def test_bad_protocol_version_rejected(self):
        replies = self._serve_and_talk([
            {"version": 999, "op": "ping", "request_id": "r1"}])
        self.assertEqual(replies[0]["status"], "rejected")

    def test_oversized_frame_is_refused_by_the_sender(self):
        with self.assertRaises(mww.WorkerError):
            mww.send_frame(None, {"blob": "x" * (mww.MAX_FRAME_BYTES + 1)})

    def test_validator_rejects_a_frame_that_is_not_an_object(self):
        with self.assertRaises(mww.WorkerError):
            mww.validate_request({"version": 1, "op": "ping"})  # missing request id


class PlatformTest(unittest.TestCase):
    def test_unsupported_platform_is_declared_not_faked(self):
        """On a platform without uid semantics the worker must say so."""
        if POSIX:
            self.assertTrue(mww.available())
        else:
            self.assertFalse(mww.available())
            self.assertEqual(mww.M0WriterWorker(Path("x")).status()["state"],
                             mww.UNSUPPORTED)

    def test_status_never_carries_secrets(self):
        blob = json.dumps(mww.M0WriterWorker(Path("x")).status())
        for forbidden in ("payload", "content", "prompt", "secret", "api_key"):
            self.assertNotIn(forbidden, blob.lower())


if __name__ == "__main__":
    unittest.main()
