"""A CLI run must not leave its M0 writer worker behind (R6, UX-1).

The defect
----------
``chiyo_bundle.hermes_plugin`` registered ``close_services`` with ``atexit`` only.
``nyairo -z`` ends in ``hermes_cli.main._exit_after_oneshot``, which flushes and then
calls ``os._exit`` on purpose (#30387, #43055), skipping the whole atexit chain. The
M0 writer worker started by that run was therefore never released: the R5 cold-install
acceptance measured one orphan process and one stale socket per run (5 runs -> 5
workers, all reparented to init and still alive minutes later).

The fix
-------
``register_process_cleanup()`` joins Hermes' own one-shot hook -- the
``_ONESHOT_CLEANUPS`` table run by ``_cleanup_oneshot_runtime`` just before the hard
exit -- instead of building a second lifecycle, and the worker gained an owner-death
backstop for exits that run no cleanup code at all. Both routes end in the existing
``ConfiguredBridge.close()`` -> ``m0_writer_worker.close_for()``.

What these tests assert, and how
--------------------------------
Every assertion is made by the parent on the real OS state of real child processes: the
worker's pid (``/proc`` state plus ``os.kill(pid, 0)``) and its socket file. No log
field is trusted for "the resource is gone". Each child owns its own profile, so a
second process never contends for the same M0 store and the pool entry it reports is
unambiguously its own.

Test B is the negative control: it removes the new table entry inside the child, i.e.
restores the pre-fix world, and then asserts the opposite outcome (the worker must
survive). A green A therefore says something about the fix rather than the fixture.

Skipped where the Hermes host is not importable (native Windows), like
``tests/test_instance_teardown.py`` and ``tests/test_instance_writer_lifecycle.py``.
"""
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "vendor/hermes"),
                str(ROOT / "components/native"), str(ROOT / "components/native/src")]

CHILD = ROOT / "tests" / "cli_exit_child.py"
#: owner-death backstop interval plus room for a loaded machine
OWNER_DEATH_BUDGET_S = 12.0
#: A one-shot exit releases the worker *before* the process goes away (the cleanup
#: table runs first, and close_for waits for the worker to exit), so the release is
#: already visible when the child is reaped. The owner-death backstop cannot act
#: sooner than its own interval, so a budget below that separates the two
#: mechanisms: without the registration this test fails, with it the first check
#: already passes.
FAST_RELEASE_BUDGET_S = 1.5


def _hermes_host_available() -> bool:
    try:
        import run_agent  # noqa: F401
        return True
    except Exception:
        return False


HERMES_HOST = _hermes_host_available()


def _alive(pid: int) -> bool:
    """Whether the pid names a live (non-zombie) process."""
    if pid is None:
        return False
    try:
        fields = Path("/proc/%d/stat" % pid).read_text().rsplit(")", 1)[1].split()
        if fields[0] == "Z":
            return False
    except (OSError, IndexError):
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


@unittest.skipUnless(HERMES_HOST, "Hermes host unavailable (native Windows: POSIX only)")
class CliProcessCleanupTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="cli-cleanup-"))
        self.children = []
        self.orphans = []
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        for child in self.children:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=30)
            self._close_pipes(child)
        for pid in self.orphans:
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass

    @staticmethod
    def _close_pipes(child):
        """Close the child's pipes ourselves: an abandoned pipe is a ResourceWarning."""
        for stream in (child.stdin, child.stdout, child.stderr):
            try:
                if stream is not None:
                    stream.close()
            except Exception:
                pass

    # ------------------------------------------------------------- helpers --- #
    def _spawn(self, mode: str, *, profile: str, expect_worker: bool = True,
               timeout: float = 180.0):
        """Start one child on its own profile; return (process, first report)."""
        home = self.tmp / profile
        environment = dict(os.environ)
        environment.update(HERMES_HOME=str(home), PYTHONDONTWRITEBYTECODE="1",
                           CLI_EXIT_STACK_AFTER_S="60")
        child = subprocess.Popen(
            [sys.executable, "-B", str(CHILD), mode], env=environment,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.children.append(child)
        report = self._line(child, timeout)
        if expect_worker:
            self.assertEqual(len(report["workers"]), 1, "the turn must have started a worker")
        return child, report

    def _line(self, child, timeout: float) -> dict:
        """Read one JSON report line without ever blocking the suite forever."""
        holder: dict = {}
        reader = threading.Thread(
            target=lambda: holder.update(line=child.stdout.readline()), daemon=True)
        reader.start()
        reader.join(timeout)
        if not holder.get("line"):
            child.kill()
            _, stderr = child.communicate(timeout=60)
            self._close_pipes(child)
            self.fail("child produced no report within %.0fs; stderr tail:\n%s"
                      % (timeout, stderr[-3000:]))
        return json.loads(holder["line"])

    def _finish(self, child, timeout: float = 120.0):
        """Wait for the child process itself, never for its pipes.

        ``communicate`` would also wait for EOF on the child's stdout/stderr, which a
        surviving grandchild can hold open -- that would hide the very thing under
        test and make the timing assertions meaningless.
        """
        code = child.wait(timeout=timeout)
        self._close_pipes(child)
        self.assertEqual(code, 0, "child exited with %s" % code)

    def _wait_gone(self, pid: int, timeout: float = 10.0):
        deadline = time.monotonic() + timeout
        while _alive(pid):
            if time.monotonic() > deadline:
                self.orphans.append(pid)
                self.fail("worker pid %d is still alive" % pid)
            time.sleep(0.05)

    # ------------------------------------------------------------------ A ---- #
    def test_one_shot_exit_releases_the_worker(self):
        """The one-shot exit itself must release the worker, not a later backstop."""
        child, report = self._spawn("oneshot", profile="oneshot")
        after = self._line(child, 60.0)          # what the cleanup table did, in-process
        self._finish(child)
        self.assertFalse(after["instance_open"],
                         "the one-shot cleanup left the instance open")
        self.assertEqual(after["workers_after"], [],
                         "the one-shot cleanup left the worker running")
        self.assertFalse(after["socket_exists"], "the socket survived the one-shot cleanup")
        self._wait_gone(report["workers"][0], timeout=FAST_RELEASE_BUDGET_S)
        self.assertFalse(Path(report["socket"]).exists(),
                         "the socket file outlived the one-shot exit")

    # ------------------------------------------------------------------ B ---- #
    def test_without_the_registration_the_one_shot_cleanup_does_nothing(self):
        """Negative control for the registration itself.

        The child removes the new table entry -- the pre-fix world -- and then runs the
        one-shot cleanup exactly as Hermes does. What is asserted is that the cleanup
        then leaves our instance and its worker open: that is the defect the entry
        fixes, observed deterministically instead of by timing. (The end-to-end
        symptom of the pre-fix world, one orphan process and one stale socket per run,
        was measured on the published rc7 in the R5 cold-install acceptance.)
        """
        child, report = self._spawn("oneshot_no_table", profile="no-table")
        after = self._line(child, 60.0)
        self._finish(child)
        self.assertTrue(after["instance_open"],
                        "without the table entry the one-shot cleanup must not close our instance")
        self.assertEqual(after["workers_after"], report["workers"],
                         "without the table entry the worker must still be running")
        self._wait_gone(report["workers"][0])   # this child exits normally: atexit closes it

    def test_the_registration_is_wired_into_the_hermes_one_shot_table(self):
        from chiyo_bundle import hermes_plugin
        from hermes_cli import main as hermes_main
        self.assertIn(("chiyo_bundle.hermes_plugin", "close_services", {}, Exception),
                      hermes_main._ONESHOT_CLEANUPS)
        self.assertEqual(getattr(hermes_main, "_cleanup_oneshot_runtime", None).__module__,
                         "hermes_cli.main")

    # ------------------------------------------------------------------ C ---- #
    def test_normal_exit_releases_the_worker_through_atexit(self):
        child, report = self._spawn("atexit", profile="atexit")
        self._finish(child)
        self._wait_gone(report["workers"][0])
        self.assertFalse(Path(report["socket"]).exists())

    # ------------------------------------------------------------------ D ---- #
    def test_close_services_is_idempotent(self):
        child, report = self._spawn("close_thrice", profile="thrice")
        final = self._line(child, 60.0)
        self._finish(child)
        self.assertEqual(final["workers_after"], [], "the pool still holds the worker")
        self.assertFalse(final["socket_exists"], "the socket file survived the close")
        self._wait_gone(report["workers"][0])

    # ------------------------------------------------------------------ E ---- #
    def test_a_running_owner_is_not_closed_by_another_process(self):
        holder, held = self._spawn("hold", profile="holder")
        try:
            self.assertTrue(_alive(held["workers"][0]))
            other, report = self._spawn("oneshot", profile="other")
            self.assertNotEqual(held["workers"][0], report["workers"][0])
            self._finish(other)
            self._wait_gone(report["workers"][0])
            self.assertFalse(Path(report["socket"]).exists())
            # the holder's worker belongs to the holder: untouched by the other process
            self.assertTrue(_alive(held["workers"][0]),
                            "a one-shot exit closed another process's worker")
        finally:
            if holder.poll() is None:
                holder.stdin.write("\n")
                holder.stdin.flush()
        released = self._line(holder, 60.0)
        self._finish(holder)
        self.assertEqual(released["workers_after"], [])
        self.assertFalse(released["socket_exists"],
                         "the holder's own clean exit must release its worker and socket")
        self._wait_gone(held["workers"][0])

    # ------------------------------------------------------------------ F ---- #
    def test_worker_stops_when_its_owner_is_killed(self):
        child, report = self._spawn("kill", profile="killed")
        pid = report["workers"][0]
        self.assertTrue(_alive(pid))
        child.kill()                        # no cleanup code runs in the owner at all
        child.wait(timeout=30)
        deadline = time.monotonic() + OWNER_DEATH_BUDGET_S
        while _alive(pid):
            if time.monotonic() > deadline:
                self.orphans.append(pid)
                self.fail("worker pid %d outlived its killed owner" % pid)
            time.sleep(0.1)
        self.assertFalse(Path(report["socket"]).exists(),
                         "the worker left its socket file behind")


if __name__ == "__main__":
    unittest.main()
