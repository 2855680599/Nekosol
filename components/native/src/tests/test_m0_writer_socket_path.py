"""NYA-AUDIT-013: a store path too deep for sun_path must still work.

``struct sockaddr_un.sun_path`` holds 108 bytes on Linux *including* its
terminating NUL, so a socket path of 108 bytes cannot be bound at all (measured:
107 binds, 108 fails with ``OSError: AF_UNIX path too long``) and 104 on
macOS/BSD. The old rule put the socket at
``<store>/m0-writer-runtime/m0-writer-<tag16>-<pid>.sock``, which overflows that
buffer for a deep enough state directory: the worker child died inside
``bind()``, the parent only saw ``worker_exited_during_startup``, every append
degraded into the retry spool and ``confirm_visible`` raised
``M0WriterUnavailable`` -- unless the operator set ``CHIYO_M0_WRITER_SOCKET_DIR``
by hand.

The rule under test here is "the same file name, in the first candidate
directory that fits": the readable directory next to the store while it fits,
otherwise a short private per-uid directory. The name is SHA-256 derived, so it
is stable, collision-free across stores, and never depends on Python's randomised
``hash()``; nothing is truncated and nothing is random, so the parent and the
worker always agree on the path.

Classes A-E exercise the rule itself; the last class runs a *real* worker and a
*real* bridge append on a long store path, which is the case that used to fail.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]          # components/native/src
NATIVE = ROOT.parent                                # components/native
for _candidate in (str(NATIVE), str(ROOT)):
    if _candidate not in sys.path:
        sys.path.insert(0, _candidate)

import m0_writer_worker as mww  # noqa: E402

POSIX = hasattr(os, "getuid") and hasattr(socket, "AF_UNIX")
SEGMENT = "deep-state-segment-%02d-" + "x" * 20


def deep_store(root: Path, *, segments: int = 3) -> Path:
    """An M0 store path deep enough that the native socket path overflows sun_path."""
    node = root
    for index in range(segments):
        node = node / (SEGMENT % index)
    node = node / "memory"
    node.mkdir(parents=True, exist_ok=True)
    return node / "evidence.sqlite"


def native_socket_dir(m0_db: Path) -> Path:
    return Path(m0_db).resolve().parent / "m0-writer-runtime"


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="m0sockpath-"))
        self.addCleanup(mww.close_all)
        # A stray override in the ambient environment would defeat the point of
        # every default-path assertion below.
        saved = os.environ.pop(mww.SOCKET_DIR_ENV, None)
        if saved is not None:
            self.addCleanup(os.environ.__setitem__, mww.SOCKET_DIR_ENV, saved)
        self.bindings: list[Path] = []

    def tearDown(self):
        for path in self.bindings:
            try:
                path.unlink()
            except OSError:
                pass

    def bind(self, path: Path):
        """Bind and listen on ``path``, proving the kernel accepts the address."""
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(server.close)
        server.bind(str(path))
        server.listen(1)
        self.bindings.append(path)
        return server

    def short_store(self) -> Path:
        memory = self.tmp / "state" / "memory"
        memory.mkdir(parents=True, exist_ok=True)
        return memory / "evidence.sqlite"

    def long_store(self, *, name: str = "first", segments: int = 3) -> Path:
        return deep_store(self.tmp / name, segments=segments)


class PathRuleTest(_Base):
    """The length rule itself; no binding, so it runs on every platform."""

    def test_limit_is_declared_below_the_kernel_buffer(self):
        self.assertIsInstance(mww.SUN_PATH_SAFE_LIMIT, int)
        self.assertGreater(mww.SUN_PATH_SAFE_LIMIT, 0)
        # sun_path is 108 bytes including its NUL -> 107 usable on Linux, 103 on
        # macOS/BSD. One conservative limit covers both.
        self.assertLessEqual(mww.SUN_PATH_SAFE_LIMIT, 107)

    def test_name_is_sha256_derived_and_not_python_hash_based(self):
        name = mww.socket_name_for(self.short_store())
        self.assertRegex(name, r"^m0-writer-[0-9a-f]{16}-\d+\.sock$")
        # deterministic for the same store, and different stores differ
        self.assertEqual(name, mww.socket_name_for(self.short_store()))
        self.assertNotEqual(name, mww.socket_name_for(self.long_store()))

    def test_name_survives_a_different_hash_seed(self):
        """``hash()`` is salted per process; SHA-256 must not be."""
        db = self.long_store()
        script = ("import sys;sys.path[:0]=[r'%s',r'%s'];"
                  "import m0_writer_worker as m;"
                  "print(m.socket_path_for(__import__('pathlib').Path(r'%s'), pid=1234))"
                  % (NATIVE, ROOT, db))
        seen = set()
        for seed in ("0", "1", "random"):
            env = dict(os.environ, PYTHONHASHSEED=seed)
            proc = subprocess.run([sys.executable, "-S", "-c", script],
                                  capture_output=True, text=True, env=env, timeout=60)
            self.assertEqual(proc.returncode, 0, proc.stderr[-400:])
            seen.add(proc.stdout.strip())
        self.assertEqual(len(seen), 1, seen)
        self.assertIn(str(mww.socket_path_for(db, pid=1234)), seen)

    def test_candidates_are_side_effect_free(self):
        db = self.long_store()
        self.assertFalse(native_socket_dir(db).exists())
        first = mww.socket_path_candidates(db)
        second = mww.socket_path_candidates(db)
        self.assertEqual(first, second)
        self.assertEqual([path.name for path in first], [first[0].name] * len(first))
        self.assertFalse(native_socket_dir(db).exists())


@unittest.skipUnless(POSIX, "AF_UNIX semantics are POSIX-only")
class SocketPathLengthTest(_Base):
    def test_short_state_path_keeps_the_readable_location(self):
        """A: the ordinary case is unchanged, and the address really binds."""
        db = self.short_store()
        chosen = mww.socket_path_for(db)
        self.assertEqual(chosen.parent, native_socket_dir(db))
        self.assertEqual(chosen, mww.socket_path_candidates(db)[0])
        self.assertTrue(mww._fits_sun_path(chosen))
        self.assertEqual(os.stat(str(chosen.parent)).st_mode & 0o777, 0o700)
        self.bind(chosen)

    def test_long_state_path_falls_back_without_any_configuration(self):
        """B: nothing configured, and the worker's own address still binds."""
        db = self.long_store()
        native = mww.socket_path_candidates(db)[0]
        self.assertFalse(mww._fits_sun_path(native),
                         "fixture is not long enough to exercise the defect: %s" % native)
        chosen = mww.socket_path_for(db)
        self.assertNotEqual(chosen.parent, native_socket_dir(db))
        self.assertTrue(mww._fits_sun_path(chosen))
        # the file name is the same; only the directory moved
        self.assertEqual(chosen.name, native.name)
        self.assertEqual(os.stat(str(chosen.parent)).st_mode & 0o777, 0o700)
        self.bind(chosen)
        # and the fallback directory is short: a socket there is bindable even
        # with the longest name our rule can produce
        longest = chosen.parent / ("m0-writer-%s-%s.sock" % ("f" * 16, "9" * 7))
        self.assertTrue(mww._fits_sun_path(longest))

    def test_two_long_states_never_share_a_socket_path(self):
        """C: distinct stores stay distinct even in a shared fallback directory."""
        first, second = self.long_store(name="first"), self.long_store(name="second")
        path_a, path_b = mww.socket_path_for(first), mww.socket_path_for(second)
        self.assertNotEqual(path_a, path_b)
        self.assertEqual(path_a.parent, path_b.parent)   # one private dir per uid
        self.assertNotEqual(path_a.name, path_b.name)
        self.bind(path_a)
        self.bind(path_b)

    def test_same_state_recomputes_the_same_path(self):
        """D: repeated computation, explicit pid, and a fresh process all agree."""
        db = self.long_store()
        self.assertEqual(mww.socket_path_for(db), mww.socket_path_for(db))
        self.assertEqual(mww.socket_path_for(db, pid=4242),
                         mww.socket_path_for(db, pid=4242))
        self.assertEqual(mww.socket_path_for(db, pid=4242).name,
                         mww.socket_name_for(db, pid=4242))
        script = ("import sys;sys.path[:0]=[r'%s',r'%s'];"
                  "import m0_writer_worker as m;"
                  "print(m.socket_path_for(__import__('pathlib').Path(r'%s'), pid=4242))"
                  % (NATIVE, ROOT, db))
        proc = subprocess.run([sys.executable, "-S", "-c", script],
                              capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.stdout.strip(), str(mww.socket_path_for(db, pid=4242)))

    def test_explicit_override_still_wins(self):
        """E: a configured directory is used verbatim, in both directions."""
        chosen_dir = self.tmp / "chosen-sockets"
        with patch.dict(os.environ, {mww.SOCKET_DIR_ENV: str(chosen_dir)}):
            short = mww.socket_path_for(self.short_store())
            long = mww.socket_path_for(self.long_store())
        for path in (short, long):
            self.assertEqual(path.parent, chosen_dir)
            self.assertTrue(mww._fits_sun_path(path))
            self.assertEqual(os.stat(str(chosen_dir)).st_mode & 0o777, 0o700)
            self.bind(path)

    def test_an_override_that_cannot_bind_fails_loudly(self):
        """An explicit choice is never silently moved somewhere else."""
        absurd = self.tmp / ("deep-" + "y" * 40) / ("deeper-" + "z" * 40)
        db = self.long_store()
        with patch.dict(os.environ, {mww.SOCKET_DIR_ENV: str(absurd)}):
            # the configured directory is the only candidate, so there is no
            # second choice that could be used silently
            candidates = mww.socket_path_candidates(db)
            self.assertEqual(len(candidates), 1)
            self.assertEqual(candidates[0].parent, absurd)
            with self.assertRaises(mww.WorkerError) as caught:
                mww.socket_path_for(db)
            self.assertIn(str(mww.SUN_PATH_SAFE_LIMIT), str(caught.exception))
            self.assertIn(mww.SOCKET_DIR_ENV, str(caught.exception))


@unittest.skipUnless(POSIX, "the persistent writer needs POSIX uid semantics")
class RealWorkerOnALongPathTest(_Base):
    """F: the case that used to fail, with a real worker and a real bridge."""

    def test_worker_starts_pings_and_appends_on_a_long_path(self):
        db = self.long_store()
        worker = mww.M0WriterWorker(db, writer_user=os.getuid())
        self.addCleanup(worker.close)
        self.assertTrue(mww._fits_sun_path(worker.socket_path))
        self.assertNotEqual(worker.socket_path.parent, native_socket_dir(db))
        worker.start()
        self.assertEqual(worker.state, mww.READY)
        self.assertEqual(worker.ping()["uid"], os.getuid())
        self.assertEqual(worker.socket_path, mww.socket_path_for(db))
        result = worker.append_events([{
            "occurred_at": "2026-10-01T00:00:00+00:00", "created_at": "2026-10-01T00:00:00+00:00",
            "source_origin": "USER_VISIBLE_INPUT", "delivery_status": "RECEIVED",
            "epistemic_role": "OBSERVED_EXTERNAL_EXPRESSION", "speaker": "user",
            "content": "long path probe", "conversation_id": "tg-long-path",
            "turn_id": "turn-long-path",
            "source_refs": [{"kind": "probe", "id": "probe:long-path"}]}])
        self.assertEqual([item["status"] for item in result], ["inserted"])
        import sqlite3
        connection = sqlite3.connect(str(db))
        try:
            self.assertEqual(connection.execute(
                "SELECT COUNT(*) FROM evidence_events").fetchone()[0], 1)
        finally:
            connection.close()

    def test_bridge_append_never_reaches_the_retry_spool_on_a_long_path(self):
        """The regression: no queued record, no M0WriterUnavailable, evidence in M0."""
        import m37_m0_bridge as bridge
        self.addCleanup(bridge._SPOOLS.clear)
        db = self.long_store()
        spool = db.parent / "evidence-retry"
        self.assertFalse(spool.exists())
        receipt = bridge.write_user_event(
            conversation_id="tg-long-path", user_text="inbound on a deep state",
            source_ref="telegram:deep-owner:1", turn_id="turn-1",
            occurred_at="2026-10-01T00:00:00+00:00", m0_db=db, writer_user=os.getuid())
        self.assertEqual(receipt["status"], "inserted")
        # nothing was queued: the spool is empty, not merely drained later
        self.assertEqual(bridge.retry_spool(db).pending_count(), 0)
        connection = bridge.retry_spool(db)._connect()
        try:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM spool").fetchone()[0], 0)
        finally:
            connection.close()
        self.assertEqual(bridge.writer_health()["state"], mww.READY)
        import sqlite3
        connection = sqlite3.connect(str(db))
        try:
            rows = connection.execute(
                "SELECT source_origin, content FROM evidence_events").fetchall()
        finally:
            connection.close()
        self.assertEqual(rows, [("USER_VISIBLE_INPUT", "inbound on a deep state")])


if __name__ == "__main__":
    unittest.main()
