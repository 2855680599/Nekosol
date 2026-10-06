"""Windows fallback and POSIX parity for the evidence/session sidecar locks.

``components/native/src/app/evidence.py`` and ``app/session.py`` used to
``import fcntl`` at module level, which made the whole memory subsystem
unimportable on native Windows (``ModuleNotFoundError: No module named
'fcntl'``). The import is now optional: POSIX keeps ``fcntl.flock``, Windows
falls back to a per-path in-process re-entrant lock.

Both branches are exercised on any platform: setting ``module.fcntl`` to None
puts the module in exactly the state a native Windows interpreter sees.

Scope of the Windows fallback: thread-safe **within one process only**. These
tests assert that, and deliberately do not claim cross-process safety.
"""
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NATIVE_SRC = ROOT / "components" / "native" / "src"
if str(NATIVE_SRC) not in sys.path:
    sys.path.insert(0, str(NATIVE_SRC))

from app import evidence, session  # noqa: E402

STAMP = "2026-10-01T00:00:00+00:00"


def make_event(index: int) -> "evidence.EvidenceEvent":
    return evidence.EvidenceEvent(
        event_id=evidence.new_event_id(),
        occurred_at=STAMP,
        memory_owner="chiyo",
        source_origin=evidence.USER_ORIGIN,
        delivery_status=evidence.RECEIVED,
        epistemic_role=evidence.USER_ROLE,
        speaker="user",
        content="probe %d" % index,
        conversation_id="tg-probe",
        turn_id="t%d" % index,
        created_at=STAMP,
        source_refs=[{"kind": "probe", "id": "probe:%d" % index}],
    )


def run_threads(worker, count):
    errors = []
    start = threading.Barrier(count)

    def wrapped(index):
        try:
            start.wait()
            worker(index)
        except BaseException as exc:  # noqa: BLE001 - reported below
            errors.append(exc)

    threads = [threading.Thread(target=wrapped, args=(i,)) for i in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return errors


class ModuleSurfaceTest(unittest.TestCase):
    def test_evidence_and_session_import_without_a_hard_fcntl_dependency(self):
        # The modules must import even where fcntl does not exist.
        self.assertTrue(hasattr(evidence, "fcntl"))
        self.assertTrue(hasattr(session, "fcntl"))

    def test_fsync_directory_tolerates_platforms_without_odirectory(self):
        with tempfile.TemporaryDirectory() as tmp:
            # POSIX: performs the barrier. Windows: returns early. Neither raises.
            session._fsync_directory(Path(tmp))


class MetricsStoreLockTest(unittest.TestCase):
    THREADS = 8
    PER_THREAD = 15

    def setUp(self):
        self.saved = evidence.fcntl

    def tearDown(self):
        evidence.fcntl = self.saved
        evidence._PROCESS_LOCKS.clear()

    def _hammer(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = evidence.MetricsStore(Path(tmp) / "metrics.json")

            def worker(_index):
                for _ in range(self.PER_THREAD):
                    store.increment("evidence_user_total")

            errors = run_threads(worker, self.THREADS)
            self.assertEqual(errors, [])
            return store.snapshot()

    def test_posix_branch_uses_fcntl_and_loses_no_increment(self):
        if evidence.fcntl is None:
            self.skipTest("platform has no fcntl; covered by the fallback test")
        snapshot = self._hammer()
        self.assertEqual(snapshot["evidence_user_total"], self.THREADS * self.PER_THREAD)

    def test_windows_fallback_is_thread_safe(self):
        evidence.fcntl = None  # exactly what a native Windows interpreter sees
        snapshot = self._hammer()
        self.assertEqual(snapshot["evidence_user_total"], self.THREADS * self.PER_THREAD)


class RetrySpoolLockTest(unittest.TestCase):
    def setUp(self):
        self.saved = evidence.fcntl

    def tearDown(self):
        evidence.fcntl = self.saved
        evidence._PROCESS_LOCKS.clear()

    def _run(self):
        with tempfile.TemporaryDirectory() as tmp:
            spool = evidence.RetrySpool(Path(tmp))
            events = [make_event(i) for i in range(10)]

            def worker(index):
                spool.queue(events[index], "OSError")
                spool.mark_committed(events[index], "inserted")

            errors = run_threads(worker, len(events))
            self.assertEqual(errors, [])
            pending = [line for line in spool.pending_path.read_text(encoding="utf8").splitlines() if line.strip()]
            committed = [line for line in spool.committed_path.read_text(encoding="utf8").splitlines() if line.strip()]
            # Every line must be intact JSON: interleaved writes would corrupt one.
            for line in pending + committed:
                json.loads(line)
            self.assertEqual(len(pending), len(events))
            self.assertEqual(len(committed), len(events))

    def test_queue_and_commit_under_fcntl(self):
        if evidence.fcntl is None:
            self.skipTest("platform has no fcntl; covered by the fallback test")
        self._run()

    def test_queue_and_commit_under_windows_fallback(self):
        evidence.fcntl = None
        self._run()


class ConversationStoreLockTest(unittest.TestCase):
    def setUp(self):
        self.saved = session.fcntl

    def tearDown(self):
        session.fcntl = self.saved
        session._PROCESS_LOCKS.clear()

    def test_persist_turn_under_windows_fallback(self):
        session.fcntl = None
        with tempfile.TemporaryDirectory() as tmp:
            store = session.ConversationStore(Path(tmp))
            store.persist_turn("tg-a", "t1", STAMP, "hi", "hello", "model-x")
            loaded = store.load("tg-a")
        self.assertEqual(len(loaded), 2)
        self.assertEqual(loaded[0]["speaker"], "user")
        self.assertEqual(loaded[0]["raw_content"], "hi")
        self.assertEqual(loaded[1]["raw_content"], "hello")

    def test_concurrent_persist_turns_keep_every_turn(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = session.ConversationStore(Path(tmp))

            def worker(index):
                store.persist_turn("tg-a", "t%d" % index, STAMP, "u%d" % index, "a%d" % index, "m")

            errors = run_threads(worker, 6)
            self.assertEqual(errors, [])
            loaded = store.load("tg-a")
            self.assertEqual(len(loaded), 12)
            self.assertEqual({row["turn_id"] for row in loaded}, {"t%d" % i for i in range(6)})


if __name__ == "__main__":
    unittest.main()
