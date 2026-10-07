"""Closing an Instance must leave no M0 writer worker, socket or pipe behind.

The defect
----------
``m37_m0_bridge.ConfiguredBridge`` owned the persistent writer worker (through
``m0_writer_worker.worker_for``) but exposed no ``close()``, so
``Instance.close()`` -- which already releases whatever its bridge offers -- had
nothing to call. The worker therefore outlived the instance, and the interpreter
reported it on the way out:

    ResourceWarning: subprocess 28 is still running
    ResourceWarning: unclosed <socket.socket ... m0-writer-...sock>
    ResourceWarning: unclosed file <_io.TextIOWrapper name=8 ...>

``Instance.close()`` now reaches ``ConfiguredBridge.close()`` → 
``m0_writer_worker.close_for()``, which stops one store's worker through the
protocol, lets it exit on its own, reaps it, closes the client socket and the
child's pipes, removes the socket file and drops the pool/cache entries.

What these tests assert, and how
--------------------------------
Every test body runs with ``ResourceWarning`` raised as an error *and* records
warnings, so a leaked handle fails the test instead of being printed. The worker
is observed through the module's own pool/status API -- deliberately, because the
claim under test is precisely "this resource is gone", which no wrapper can show.

Skipped where the Hermes host is not importable (native Windows), like
``tests/test_instance_teardown.py``: a real Instance cannot be assembled there.
"""
import contextlib
import gc
import os
import sys
import tempfile
import time
import unittest
import warnings
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "vendor/hermes"),
                str(ROOT / "components/native"), str(ROOT / "components/native/src")]


def _hermes_host_available() -> bool:
    try:
        import run_agent  # noqa: F401
        return True
    except Exception:
        return False


HERMES_HOST = _hermes_host_available()


@contextlib.contextmanager
def no_resource_warnings():
    """Fail on any ResourceWarning, and collect whatever warnings do appear."""
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        warnings.simplefilter("error", ResourceWarning)
        yield caught


@unittest.skipUnless(HERMES_HOST, "Hermes host unavailable (native Windows: POSIX only)")
class InstanceWriterLifecycleTest(unittest.TestCase):
    def setUp(self):
        import m0_writer_worker as mww
        import m37_m0_bridge as bridge
        self.mww, self.bridge = mww, bridge
        self.tmp = Path(tempfile.mkdtemp(prefix="writer-lifecycle-"))
        self.state = self.tmp / "state"
        # Hygiene only: the assertions below read the pool *after* the code under
        # test has run, so a leftover worker from another file can never pass for
        # a released one.
        mww.close_all()
        bridge._SPOOLS.clear()
        self.addCleanup(mww.close_all)
        self.addCleanup(bridge._SPOOLS.clear)

    # ------------------------------------------------------------- helpers --- #
    def _environment(self) -> dict:
        env = dict(os.environ)
        env.update(CHIYO_MODEL_API_KEY="unused-lifecycle-key",
                   CHIYO_MODEL="unused-lifecycle-model",
                   CHIYO_MODEL_BASE_URL="https://api.example.invalid/v1")
        return env

    def _open(self):
        from chiyo_bundle.instance import Instance
        return Instance(self.state, owner="writer-lifecycle", memory=True,
                        environment=self._environment())

    def _turn(self, instance, index: int):
        """One offline chat + confirm_visible, i.e. two real M0 appends."""
        reply = SimpleNamespace(content="lifecycle reply %d" % index, usage={},
                                finish_reason="stop")
        with patch("chiyo_bundle.host.HermesCompletionProvider.complete",
                   side_effect=lambda messages: reply):
            instance.chat("lifecycle turn %d" % index, request_id="lifecycle-%d" % index)
        instance.confirm_visible("lifecycle-%d" % index)

    def _workers(self) -> list:
        with self.mww._POOL_LOCK:
            return list(self.mww._POOL.values())

    def _assert_process_gone(self, proc, timeout: float = 5.0):
        deadline = time.monotonic() + timeout
        while True:
            try:
                os.kill(proc.pid, 0)
            except OSError:
                return
            if time.monotonic() > deadline:
                self.fail("worker pid %d is still alive after close" % proc.pid)
            time.sleep(0.05)

    def _m0_rows(self):
        import sqlite3
        connection = sqlite3.connect(str(self.state / "memory" / "evidence.sqlite"))
        try:
            return sorted(origin for (origin,) in connection.execute(
                "SELECT source_origin FROM evidence_events"))
        finally:
            connection.close()

    def _spool_rows(self) -> dict:
        import sqlite3
        db = self.state / "memory" / "evidence-retry" / "spool.sqlite"
        if not db.exists():
            return {}
        connection = sqlite3.connect("file:%s?mode=ro" % db, uri=True)
        try:
            return {row: True for row in connection.execute(
                "SELECT source_origin FROM spool")}
        finally:
            connection.close()

    # ------------------------------------------------------------------ A ---- #
    def test_normal_write_then_close_releases_worker_socket_and_child(self):
        with no_resource_warnings() as caught:
            instance = self._open()
            self.addCleanup(instance.close)
            self._turn(instance, 1)

            workers = self._workers()
            self.assertEqual(len(workers), 1, "the turn must have started a worker")
            proc = workers[0]._proc
            socket_path = workers[0].socket_path
            self.assertIsNotNone(proc)
            self.assertTrue(socket_path.exists())
            self.assertEqual(self.mww.status_all()[0]["pid"], proc.pid)

            instance.close()

            # the child left on its own instead of being killed on the way out
            self.assertEqual(proc.returncode, 0, "worker was not stopped gracefully")
            self._assert_process_gone(proc)
            self.assertFalse(socket_path.exists(), "the socket file outlived the close")
            self.assertEqual(self._workers(), [], "the pool still holds the worker")
            self.assertEqual(self.mww.status_all(), [])
            gc.collect()
        self.assertEqual([w for w in caught if issubclass(w.category, ResourceWarning)], [])

    # ------------------------------------------------------------------ B ---- #
    def test_close_without_any_write_creates_no_worker(self):
        with no_resource_warnings() as caught:
            instance = self._open()
            self.assertEqual(self._workers(), [])
            self.assertFalse((self.state / "memory" / "m0-writer-runtime").exists(),
                             "a worker directory was created without any write")
            instance.close()
            self.assertTrue(instance._closed)
            self.assertEqual(self._workers(), [])
            self.assertEqual(self.mww.status_all(), [])
            gc.collect()
        self.assertEqual([w for w in caught if issubclass(w.category, ResourceWarning)], [])

    # ------------------------------------------------------------------ C ---- #
    def test_close_is_idempotent_after_use(self):
        with no_resource_warnings() as caught:
            instance = self._open()
            self._turn(instance, 1)
            self.assertEqual(len(self._workers()), 1)
            instance.close()
            empty = self._workers()
            for _ in range(3):
                instance.close()          # must stay silent and change nothing
            self.assertTrue(instance._closed)
            self.assertEqual(self._workers(), empty)
            self.assertEqual(self.mww.status_all(), [])
            gc.collect()
        self.assertEqual([w for w in caught if issubclass(w.category, ResourceWarning)], [])

    # ------------------------------------------------------------------ D ---- #
    def test_a_later_instance_restarts_the_writer(self):
        with no_resource_warnings() as caught:
            first = self._open()
            self._turn(first, 1)
            first_pid = self._workers()[0]._proc.pid
            first.close()
            self.assertEqual(self._workers(), [])
            self.assertEqual(self._m0_rows(),
                             ["CHIYO_VISIBLE_OUTPUT", "USER_VISIBLE_INPUT"])

            second = self._open()
            self.addCleanup(second.close)
            self._turn(second, 2)
            workers = self._workers()
            self.assertEqual(len(workers), 1, "the writer did not start again")
            self.assertNotEqual(workers[0]._proc.pid, first_pid)
            # identical M0 semantics: two turns, four events, none duplicated
            self.assertEqual(self._m0_rows(),
                             ["CHIYO_VISIBLE_OUTPUT", "CHIYO_VISIBLE_OUTPUT",
                              "USER_VISIBLE_INPUT", "USER_VISIBLE_INPUT"])
            self.assertEqual(self._spool_rows(), {})
            second.close()
            self.assertEqual(self._workers(), [])
            gc.collect()
        self.assertEqual([w for w in caught if issubclass(w.category, ResourceWarning)], [])

    # ------------------------------------------------------------------ E ---- #
    def test_deep_state_path_worker_is_released_too(self):
        deep = self.tmp
        for index in range(3):
            deep = deep / ("deep-state-segment-%02d-" % index + "x" * 20)
        self.state = deep / "state"
        db = self.state / "memory" / "evidence.sqlite"
        native = self.mww.socket_path_candidates(db, pid=os.getpid())[0]
        self.assertFalse(self.mww._fits_sun_path(native),
                         "fixture is not deep enough: %s" % native)

        with no_resource_warnings() as caught:
            instance = self._open()
            self.addCleanup(instance.close)
            self._turn(instance, 1)
            workers = self._workers()
            self.assertEqual(len(workers), 1)
            socket_path, proc = workers[0].socket_path, workers[0]._proc
            # P3-A still holds: the worker used the short fallback and it binds
            self.assertTrue(self.mww._fits_sun_path(socket_path))
            self.assertNotEqual(socket_path.parent, db.parent / "m0-writer-runtime")
            self.assertEqual(socket_path, self.mww.socket_path_for(db))

            instance.close()

            self.assertEqual(proc.returncode, 0)
            self._assert_process_gone(proc)
            self.assertFalse(socket_path.exists())
            self.assertEqual(self._workers(), [])
            self.assertEqual(self._spool_rows(), {})
            gc.collect()
        self.assertEqual([w for w in caught if issubclass(w.category, ResourceWarning)], [])


if __name__ == "__main__":
    unittest.main()
