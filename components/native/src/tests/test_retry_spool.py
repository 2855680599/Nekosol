"""RetrySpool: bounded SQLite storage, legacy migration, and crash behaviour.

The previous spool was two append-only JSONL files with no bound, and it answered
``pending_count()`` by re-parsing both files in full: disk and time both grew with
the whole history. These tests pin the properties that must survive the move to
SQLite:

* pending counting and draining stay correct at 0, 1, 100 and 1000 records;
* the same (source_origin, source_ref_id) can never be queued twice;
* a restart sees the same pending set, and nothing is reported as committed
  unless it really is;
* a pending row that cannot be appended stays PENDING and keeps its error;
* a corrupted legacy record does not hide the rest of the file;
* the legacy migration is idempotent, never deletes the old bytes, and leaves the
  pending count unchanged;
* ``pending_count()`` no longer scales with history.
"""
from __future__ import annotations

from pathlib import Path
import json
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
NATIVE_SRC = ROOT / "components" / "native" / "src"
if str(NATIVE_SRC) not in sys.path:
    sys.path.insert(0, str(NATIVE_SRC))

from app import evidence  # noqa: E402

STAMP = "2026-10-01T00:00:00+00:00"


def make_event(index: int) -> "evidence.EvidenceEvent":
    return evidence.EvidenceEvent(
        event_id="0199a000-0000-7000-8000-%012d" % index,
        occurred_at=STAMP,
        memory_owner="chiyo",
        source_origin=evidence.USER_ORIGIN,
        delivery_status=evidence.RECEIVED,
        epistemic_role=evidence.USER_ROLE,
        speaker="user",
        content="spool probe %d" % index,
        conversation_id="tg-probe",
        turn_id="t%d" % index,
        created_at=STAMP,
        source_refs=[{"kind": "probe", "id": "probe:%d" % index}],
    )


def legacy_pending_line(index: int) -> str:
    return json.dumps({"event": make_event(index).to_dict(),
                       "queued_at": STAMP, "error": "OSError"},
                      ensure_ascii=False, separators=(",", ":"))


class SpoolBasicsTest(unittest.TestCase):
    def test_zero_records(self):
        with tempfile.TemporaryDirectory() as tmp:
            spool = evidence.RetrySpool(Path(tmp))
            self.assertEqual(spool.pending_count(), 0)
            self.assertEqual(spool.pending_records(), [])
            self.assertEqual(spool.compact(), 0)

    def test_one_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            spool = evidence.RetrySpool(Path(tmp))
            spool.queue(make_event(0), "OSError")
            self.assertEqual(spool.pending_count(), 1)
            records = spool.pending_records()
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["error"], "OSError")
            self.assertEqual(
                evidence.EvidenceEvent.from_dict(records[0]["event"]).event_id,
                make_event(0).event_id)

    def test_duplicate_key_is_never_queued_twice(self):
        with tempfile.TemporaryDirectory() as tmp:
            spool = evidence.RetrySpool(Path(tmp))
            event = make_event(0)
            spool.queue(event, "OSError")
            spool.queue(event, "OSError")
            spool.queue(event, "ValueError")
            self.assertEqual(spool.pending_count(), 1)
            # the re-queue is recorded as an extra attempt, not a second record
            connection = spool._connect()
            row = connection.execute(
                "SELECT attempts, last_error FROM spool").fetchone()
            connection.close()
            self.assertEqual(int(row["attempts"]), 2)
            self.assertEqual(row["last_error"], "ValueError")

    def test_commit_clears_pending_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            spool = evidence.RetrySpool(Path(tmp))
            event = make_event(0)
            spool.queue(event, "OSError")
            spool.mark_committed(event, "inserted")
            self.assertEqual(spool.pending_count(), 0)
            spool.mark_committed(event, "inserted")
            spool.mark_committed(event, "duplicate")
            self.assertEqual(spool.pending_count(), 0)

    def test_commit_of_never_queued_key_is_recorded_as_done(self):
        with tempfile.TemporaryDirectory() as tmp:
            spool = evidence.RetrySpool(Path(tmp))
            event = make_event(7)
            spool.mark_committed(event, "inserted")
            # queueing it afterwards must not resurrect it as pending work
            spool.queue(event, "OSError")
            self.assertEqual(spool.pending_count(), 0)

    def test_restart_preserves_pending(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = evidence.RetrySpool(Path(tmp))
            for i in range(5):
                first.queue(make_event(i), "OSError")
            first.mark_committed(make_event(0), "inserted")
            del first
            second = evidence.RetrySpool(Path(tmp))
            self.assertEqual(second.pending_count(), 4)
            self.assertEqual(
                {evidence.EvidenceEvent.from_dict(r["event"]).event_id
                 for r in second.pending_records()},
                {make_event(i).event_id for i in range(1, 5)})


class SpoolScaleTest(unittest.TestCase):
    def _build(self, tmp, count):
        spool = evidence.RetrySpool(Path(tmp))
        for i in range(count):
            spool.queue(make_event(i), "OSError")
        return spool

    def test_hundred_records(self):
        with tempfile.TemporaryDirectory() as tmp:
            spool = self._build(tmp, 100)
            self.assertEqual(spool.pending_count(), 100)
            self.assertEqual(len(spool.pending_records()), 100)
            for i in range(100):
                spool.mark_committed(make_event(i), "inserted")
            self.assertEqual(spool.pending_count(), 0)

    def test_thousand_records_and_batched_drain(self):
        with tempfile.TemporaryDirectory() as tmp:
            spool = self._build(tmp, 1000)
            self.assertEqual(spool.pending_count(), 1000)
            batch = spool.pending_records(spool.DEFAULT_RETRY_BATCH)
            self.assertEqual(len(batch), spool.DEFAULT_RETRY_BATCH)
            # 0=all, 1=exactly one: a whole-file read cannot answer these
            self.assertEqual(len(spool.pending_records(1)), 1)

    def test_pending_count_does_not_scale_with_history(self):
        timings = {}
        for count in (10, 5000):
            with tempfile.TemporaryDirectory() as tmp:
                spool = self._build(tmp, count)
                started = time.perf_counter()
                for _ in range(20):
                    spool.pending_count()
                timings[count] = (time.perf_counter() - started) / 20
                self.assertEqual(spool.pending_count(), count)
        ratio = timings[5000] / timings[10] if timings[10] else float("inf")
        # 500x more rows must not cost anything like 500x per call. The previous
        # implementation re-parsed both files, so this was inherently linear.
        self.assertLess(ratio, 20,
                        "pending_count scaled with history: %r" % (timings,))


class SpoolFailureTest(unittest.TestCase):
    def test_corrupted_legacy_record_does_not_hide_the_others(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "pending.jsonl").write_text(
                legacy_pending_line(0) + "\n" + "{not json at all\n" +
                legacy_pending_line(1) + "\n", encoding="utf8")
            spool = evidence.RetrySpool(root)
            # migration verified? the malformed line is skipped, not fatal
            self.assertEqual(spool.pending_count(), 2)

    def test_unusable_pending_row_stays_pending_with_its_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            writer = evidence.EvidenceWriter(Path(tmp) / "writer")
            event = make_event(0)
            writer.spool.queue(event, "OSError")
            self.assertEqual(writer.spool.pending_count(), 1)

            class Exploding:
                def append(self, _event):
                    raise RuntimeError("m0 unavailable")

            writer.store = Exploding()
            result = writer.retry_pending()
            self.assertEqual(result["failed"], 1)
            self.assertEqual(result["inserted"], 0)
            # still pending, still carrying its reason: nothing is reported done
            self.assertEqual(writer.spool.pending_count(), 1)
            connection = writer.spool._connect()
            row = connection.execute("SELECT status, last_error FROM spool").fetchone()
            connection.close()
            self.assertEqual(row["status"], evidence.RetrySpool.PENDING)
            self.assertEqual(row["last_error"], "OSError")

    def test_retry_commits_through_the_real_store(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            writer = evidence.EvidenceWriter(root / "data")
            event = make_event(0)
            writer.spool.queue(event, "OSError")
            self.assertEqual(writer.spool.pending_count(), 1)
            result = writer.retry_pending()
            self.assertEqual(result["inserted"], 1)
            self.assertEqual(writer.spool.pending_count(), 0)
            self.assertEqual(len(writer._get_store().list_events()), 1)


class SpoolMigrationTest(unittest.TestCase):
    def _write_legacy(self, root, pending, committed):
        if pending is not None:
            (root / "pending.jsonl").write_text(pending, encoding="utf8")
        if committed is not None:
            (root / "committed.jsonl").write_text(committed, encoding="utf8")

    def test_migration_preserves_pending_count_and_keeps_old_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pending = "\n".join(legacy_pending_line(i) for i in range(4)) + "\n"
            committed = json.dumps(
                {"source_origin": evidence.USER_ORIGIN, "source_ref_id": "probe:0",
                 "event_id": make_event(0).event_id, "status": "inserted",
                 "committed_at": STAMP}, ensure_ascii=False) + "\n"
            self._write_legacy(root, pending, committed)
            before_bytes = (root / "pending.jsonl").read_bytes()

            spool = evidence.RetrySpool(root)
            self.assertTrue(spool.migration["performed"])
            self.assertTrue(spool.migration["verified"])
            # probe:0 was committed, so 3 of the 4 remain pending
            self.assertEqual(spool.pending_count(), 3)
            # old data is renamed, never unlinked
            self.assertFalse((root / "pending.jsonl").exists())
            self.assertEqual(
                (root / "pending.jsonl.migrated").read_bytes(), before_bytes)
            self.assertTrue(spool.db_path.exists())

    def test_migration_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write_legacy(root, legacy_pending_line(0) + "\n", None)
            first = evidence.RetrySpool(root)
            self.assertEqual(first.pending_count(), 1)
            second = evidence.RetrySpool(root)
            self.assertEqual(second.pending_count(), 1)
            self.assertFalse(second.migration["performed"])

    def test_upgrade_never_hides_an_unmigrated_legacy_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            # A legacy file that appears after the store already exists (the
            # degraded case): it must still be visible, not silently ignored.
            spool = evidence.RetrySpool(root)
            self.assertEqual(spool.pending_count(), 0)
            self._write_legacy(root, legacy_pending_line(9) + "\n", None)
            reopened = evidence.RetrySpool(root, migrate=False)
            self.assertEqual(reopened.pending_count(), 1)
            self.assertEqual(
                evidence.EvidenceEvent.from_dict(
                    reopened.pending_records()[0]["event"]).event_id,
                make_event(9).event_id)


if __name__ == "__main__":
    unittest.main()
