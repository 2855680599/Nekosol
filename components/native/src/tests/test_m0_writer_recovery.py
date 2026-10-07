"""NYA-AUDIT-005 closeout: a failed bridge append must reach the RetrySpool.

Layering under test: the downgraded worker only "tries to write M0"; durability
for a failed write lives in the parent (bridge + EvidenceWriter/RetrySpool). The
retry database is deliberately *not* inside the worker.

Testing note that cost a debugging round: ``EvidenceWriter.__init__`` ends with
``self.retry_pending()``, so constructing a writer *drains* the spool. A test that
queues first and only then constructs a writer has already had its evidence
retried for it, and its assertions on a later explicit ``retry_pending()`` will
see an empty spool. These tests therefore build the writer up front and reuse it.
``test_writer_construction_drains_pending_evidence`` pins that behaviour so it
cannot silently confuse the next reader.
"""
from __future__ import annotations

from pathlib import Path
import os
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]          # components/native/src
NATIVE = ROOT.parent                                # components/native
for _candidate in (str(NATIVE), str(ROOT)):
    if _candidate not in sys.path:
        sys.path.insert(0, _candidate)

import m0_writer_worker as mww  # noqa: E402
import m37_m0_bridge as bridge  # noqa: E402

from app.evidence import EvidenceStore, EvidenceWriter  # noqa: E402

POSIX = hasattr(os, "getuid")


def count_rows(m0_db: Path) -> int:
    import sqlite3
    connection = sqlite3.connect(str(m0_db))
    try:
        return int(connection.execute(
            "SELECT COUNT(*) FROM evidence_events").fetchone()[0])
    finally:
        connection.close()


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="m0recovery-"))
        self.data = self.tmp / "data"
        (self.data / "memory").mkdir(parents=True)
        self.m0 = self.data / "memory" / "evidence.sqlite"
        EvidenceStore(self.m0)
        self._saved_fault = os.environ.pop(mww.FAULT_ENV, None)
        bridge._SPOOLS.clear()
        # Built BEFORE any failure: its constructor would otherwise auto-retry
        # the very record the test wants to observe as pending.
        self.writer = EvidenceWriter(self.data)
        self.addCleanup(self._restore)

    def _restore(self):
        mww.close_all()
        if self._saved_fault is not None:
            os.environ[mww.FAULT_ENV] = self._saved_fault
        else:
            os.environ.pop(mww.FAULT_ENV, None)
        bridge._SPOOLS.clear()

    def _fail_writes(self, mode: str):
        os.environ[mww.FAULT_ENV] = mode
        mww.close_all()          # drop any cached healthy worker

    def _heal_writes(self):
        os.environ.pop(mww.FAULT_ENV, None)
        mww.close_all()

    def _user(self, index=0, ref=None):
        return dict(conversation_id="tg-recovery", user_text="turn %d" % index,
                    source_ref=ref or ("probe:%d" % index), turn_id="turn-%d" % index,
                    occurred_at="2026-10-01T00:00:00+00:00",
                    m0_db=self.m0, writer_user=os.getuid())

    def _assistant(self, index=0, ref=None):
        binding = {"chat_id": "4242", "namespace": "ns",
                   "conversation_id": "tg-recovery"}
        return dict(conversation_id="tg-recovery", content="reply %d" % index,
                    source_ref=ref or ("telegram:4242:%d" % index),
                    turn_id="turn-%d" % index,
                    occurred_at="2026-10-01T00:00:00+00:00",
                    binding=binding, m0_db=self.m0, writer_user=os.getuid())


@unittest.skipUnless(POSIX, "the downgraded worker needs POSIX uid semantics")
class WriterRecoveryTest(_Base):
    def test_case_a_normal_write_lands_with_an_empty_spool(self):
        result = bridge.write_user_event(**self._user())
        self.assertEqual(result["status"], "inserted")
        self.assertEqual(count_rows(self.m0), 1)
        self.assertEqual(bridge.retry_spool(self.m0).pending_count(), 0)
        self.assertEqual(bridge.writer_health()["state"], mww.READY)

    def test_case_b_worker_down_queues_and_never_claims_success(self):
        self._fail_writes("before_request")
        with self.assertRaises(bridge.M0WriterUnavailable) as caught:
            bridge.write_user_event(**self._user())
        self.assertIn("queued_for_retry=True", str(caught.exception))
        self.assertEqual(count_rows(self.m0), 0)                      # no write happened
        self.assertEqual(bridge.retry_spool(self.m0).pending_count(), 1)
        # worker down but the evidence IS durable -> DEGRADED, not ERROR
        health = bridge.writer_health()
        self.assertEqual(health["state"], mww.DEGRADED)
        self.assertEqual(health["pending"], 1)
        self.assertIsNotNone(health["error"])

    def test_case_c_recovery_drains_the_spool_and_restores_ready(self):
        self._fail_writes("before_request")
        with self.assertRaises(bridge.M0WriterUnavailable):
            bridge.write_user_event(**self._user())
        self.assertEqual(bridge.retry_spool(self.m0).pending_count(), 1)
        self.assertEqual(bridge.writer_health()["state"], mww.DEGRADED)

        self._heal_writes()
        retried = self.writer.retry_pending()
        self.assertEqual(retried["inserted"], 1)
        self.assertEqual(count_rows(self.m0), 1)
        self.assertEqual(bridge.retry_spool(self.m0).pending_count(), 0)
        # the state follows the durable spool, so it heals without any notification
        self.assertEqual(bridge.writer_health()["state"], mww.READY)

    def test_case_c_recovery_appends_the_exact_queued_event(self):
        queued = self._user(index=7, ref="probe:7")
        self._fail_writes("before_request")
        with self.assertRaises(bridge.M0WriterUnavailable):
            bridge.write_user_event(**queued)
        self._heal_writes()
        self.writer.retry_pending()
        import sqlite3
        connection = sqlite3.connect(str(self.m0))
        try:
            rows = connection.execute(
                "SELECT source_origin, primary_source_ref_id, content, turn_id "
                "FROM evidence_events").fetchall()
        finally:
            connection.close()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][1], "probe:7")
        self.assertEqual(rows[0][2], "turn 7")
        self.assertEqual(rows[0][3], "turn-7")

    def test_case_d_crash_after_commit_stays_exactly_one_row(self):
        self._fail_writes("after_commit")
        with self.assertRaises(bridge.M0WriterUnavailable):
            bridge.write_user_event(**self._user())
        # the row was committed even though the reply never arrived
        self.assertEqual(count_rows(self.m0), 1)
        self.assertEqual(bridge.retry_spool(self.m0).pending_count(), 1)

        self._heal_writes()
        retried = self.writer.retry_pending()
        # the spool was drained by the M0 unique key, not by a second insert
        self.assertEqual(retried["duplicate"], 1)
        self.assertEqual(retried["inserted"], 0)
        self.assertEqual(count_rows(self.m0), 1)
        self.assertEqual(bridge.retry_spool(self.m0).pending_count(), 0)
        connection = bridge.retry_spool(self.m0)._connect()
        row = connection.execute("SELECT status, commit_status FROM spool").fetchone()
        connection.close()
        self.assertEqual(row["status"], "COMMITTED")
        self.assertEqual(row["commit_status"], "duplicate")
        self.assertEqual(bridge.writer_health()["state"], mww.READY)

    def test_case_b_and_c_hold_for_assistant_evidence_too(self):
        self._fail_writes("before_request")
        with self.assertRaises(bridge.M0WriterUnavailable):
            bridge.write_assistant_event(**self._assistant())
        self.assertEqual(count_rows(self.m0), 0)
        self.assertEqual(bridge.retry_spool(self.m0).pending_count(), 1)
        self.assertEqual(bridge.writer_health()["state"], mww.DEGRADED)

        self._heal_writes()
        retried = self.writer.retry_pending()
        self.assertEqual(retried["inserted"], 1)
        self.assertEqual(count_rows(self.m0), 1)
        self.assertEqual(bridge.retry_spool(self.m0).pending_count(), 0)
        self.assertEqual(bridge.writer_health()["state"], mww.READY)
        import sqlite3
        connection = sqlite3.connect(str(self.m0))
        try:
            speaker, origin = connection.execute(
                "SELECT speaker, source_origin FROM evidence_events").fetchone()
        finally:
            connection.close()
        self.assertEqual(speaker, "chiyo")
        self.assertEqual(origin, "CHIYO_VISIBLE_OUTPUT")

    def test_case_d_holds_for_assistant_evidence_too(self):
        self._fail_writes("after_commit")
        with self.assertRaises(bridge.M0WriterUnavailable):
            bridge.write_assistant_event(**self._assistant())
        self.assertEqual(count_rows(self.m0), 1)
        self._heal_writes()
        retried = self.writer.retry_pending()
        self.assertEqual(retried["duplicate"], 1)
        self.assertEqual(count_rows(self.m0), 1)
        self.assertEqual(bridge.retry_spool(self.m0).pending_count(), 0)


class WriterConstructionTest(_Base):
    """Pins the behaviour that caused a mistaken bug report."""

    def test_writer_construction_drains_pending_evidence(self):
        self._fail_writes("before_request")
        with self.assertRaises(bridge.M0WriterUnavailable):
            bridge.write_user_event(**self._user())
        self.assertEqual(bridge.retry_spool(self.m0).pending_count(), 1)

        self._heal_writes()
        # A *new* writer drains the spool in its constructor ...
        fresh = EvidenceWriter(self.data)
        self.assertEqual(count_rows(self.m0), 1)
        self.assertEqual(fresh.spool.pending_count(), 0)
        # ... which is why an explicit retry afterwards legitimately finds nothing.
        self.assertEqual(fresh.retry_pending(),
                         {"inserted": 0, "duplicate": 0, "failed": 0})

    def test_bridge_and_writer_share_one_spool(self):
        """There is exactly one spool: no "queued to A, drained from B" split."""
        self.assertEqual(bridge.retry_spool(self.m0).db_path,
                         self.writer.spool.db_path)
        self.assertEqual(bridge.retry_spool(self.m0).db_path,
                         bridge.retry_spool(self.m0).db_path)


class SideEffectTest(_Base):
    def test_writer_health_is_read_only(self):
        self._fail_writes("before_request")
        with self.assertRaises(bridge.M0WriterUnavailable):
            bridge.write_user_event(**self._user())
        spool = bridge.retry_spool(self.m0)
        before = (spool.pending_count(), count_rows(self.m0),
                  sum(1 for _ in bridge.retry_spool(self.m0).pending_records()))
        for _ in range(10):
            bridge.writer_health()
        after = (spool.pending_count(), count_rows(self.m0),
                 sum(1 for _ in bridge.retry_spool(self.m0).pending_records()))
        self.assertEqual(before, after)
        self.assertEqual(before[0], 1)

    def test_repeated_retry_spool_calls_do_not_lose_pending(self):
        self._fail_writes("before_request")
        with self.assertRaises(bridge.M0WriterUnavailable):
            bridge.write_user_event(**self._user())
        for _ in range(3):
            self.assertEqual(bridge.retry_spool(self.m0).pending_count(), 1)
        self.assertEqual(count_rows(self.m0), 0)


@unittest.skipUnless(POSIX, "the downgraded worker needs POSIX uid semantics")
class OwnershipTest(_Base):
    """§3: writing must not change who owns the store."""

    def test_store_and_socket_ownership_are_unchanged_by_an_append(self):
        before = os.stat(str(self.m0))
        result = bridge.write_user_event(**self._user())
        self.assertEqual(result["status"], "inserted")
        after = os.stat(str(self.m0))
        self.assertEqual((before.st_uid, before.st_gid), (after.st_uid, after.st_gid))
        self.assertEqual(after.st_uid, os.getuid())

        worker = mww.worker_for(self.m0, writer_user=os.getuid())
        socket_info = os.stat(str(worker.socket_path))
        self.assertEqual(socket_info.st_uid, after.st_uid)          # socket owner == store owner
        self.assertEqual(socket_info.st_mode & 0o777, 0o600)
        identity = worker.ping()
        self.assertEqual(identity["uid"], after.st_uid)             # worker uid == store owner

    def test_directory_permissions_are_private(self):
        bridge.write_user_event(**self._user())
        worker = mww.worker_for(self.m0, writer_user=os.getuid())
        mode = os.stat(str(worker.socket_path.parent)).st_mode & 0o777
        self.assertEqual(mode, 0o700)


class WriterHealthPrivacyTest(unittest.TestCase):
    def test_health_exposes_no_paths_payloads_or_uids(self):
        import json
        blob = json.dumps(bridge.writer_health())
        lowered = blob.lower()
        for forbidden in ("payload", "content", "prompt", "api_key", "bearer",
                          "/home/", "c:\\", "sql", "socket"):
            self.assertNotIn(forbidden, lowered, forbidden)


if __name__ == "__main__":
    unittest.main()
