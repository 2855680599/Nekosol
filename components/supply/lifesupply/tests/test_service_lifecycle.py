"""C15 lifecycle tests (PHASE 10).

Covered: normal start, SIGTERM graceful stop, restart, restart after an abnormal exit,
stale-socket recovery, refusal to steal a live instance's socket, refusal to unlink a
non-socket, second-writer protection on one data root, and the bounded in-flight drain.

These run the real service in a child process over a real Unix socket in a temporary root.
They need POSIX (``fcntl``); on a platform without it the module is skipped.
"""
from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

try:  # the service itself needs fcntl; skip cleanly where it does not exist
    import fcntl  # noqa: F401
except ImportError:  # pragma: no cover - exercised only on non-POSIX hosts
    fcntl = None

REPO_ROOT = Path(__file__).resolve().parents[2]
PY = sys.executable
START_TIMEOUT = 25.0


def _env(root: Path, sock: Path) -> dict:
    env = dict(os.environ)
    env.update({
        "PYTHONPATH": str(REPO_ROOT),
        "LIFE_SUPPLY_DATA_ROOT": str(root / "var"),
        "LIFE_SUPPLY_SOCKET": str(sock),
        "LIFE_SUPPLY_REQUIRE_NON_ROOT": "OFF",
        "LIFE_SUPPLY_OPERATOR_UIDS": str(os.geteuid()),
        "LIFE_SUPPLY_SERVICE_UIDS": str(os.geteuid()),
        "PYTHONDONTWRITEBYTECODE": "1",
    })
    return env


def _request(path: Path, payload: dict, timeout: float = 5.0) -> dict:
    """One request over the socket.  Returns {} when the transport itself fails."""
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(timeout)
            client.connect(str(path))
            client.sendall(json.dumps(payload).encode() + b"\n")
            line = client.makefile("rb").readline(65536)
    except OSError:
        return {}
    try:
        answer = json.loads(line.decode("utf-8"))
    except (UnicodeError, ValueError):
        return {}
    return answer if isinstance(answer, dict) else {}


def _answers(path: Path) -> bool:
    return bool(_request(path, {"op": "status"}))


def _serves_and_is_ready(path: Path) -> bool:
    answer = _request(path, {"op": "status"})
    return answer.get("service_state") == "READY"


def _wait_until(path: Path, predicate, timeout: float = START_TIMEOUT) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if path.exists() and predicate(path):
            return True
        time.sleep(0.05)
    return False


@unittest.skipIf(fcntl is None, "the service requires fcntl (POSIX only)")
class ServiceLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="ls_lifecycle_")
        self.root = Path(self._tmp.name)
        (self.root / "var").mkdir(parents=True, exist_ok=True)
        (self.root / "run").mkdir(parents=True, exist_ok=True)
        self.sock = self.root / "run" / "life-supply.sock"
        self.env = _env(self.root, self.sock)
        subprocess.run(
            [PY, "-c",
             "import os, pathlib;"
             "from lifesupply.db import CANONICAL_FILENAME, LifeSupplyDB;"
             "r = pathlib.Path(os.environ['LIFE_SUPPLY_DATA_ROOT']);"
             "LifeSupplyDB(r / CANONICAL_FILENAME).layout_report()"],
            env=self.env, check=True, stdout=subprocess.DEVNULL)
        self._procs: list[subprocess.Popen] = []

    def tearDown(self) -> None:
        for proc in self._procs:
            if proc.poll() is None:
                proc.kill()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:  # pragma: no cover
                    pass
            for stream in (proc.stdout, proc.stderr):
                if stream is not None and not stream.closed:
                    stream.close()
        self._procs.clear()
        self._tmp.cleanup()

    # -- helpers -------------------------------------------------------------------------
    def _spawn(self, sock: Path) -> subprocess.Popen:
        proc = subprocess.Popen(
            [PY, "-m", "lifesupply.service.cli", "serve", "--socket", str(sock)],
            env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self._procs.append(proc)
        return proc

    def _start(self, sock: Path | None = None) -> subprocess.Popen:
        target = self.sock if sock is None else sock
        proc = self._spawn(target)
        self.assertTrue(_wait_until(target, _serves_and_is_ready),
                        "the service never reached READY")
        return proc

    def _stop(self, proc: subprocess.Popen) -> int:
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=START_TIMEOUT)
        return proc.returncode

    # -- tests ---------------------------------------------------------------------------
    def test_sigterm_exits_cleanly_and_removes_its_socket(self) -> None:
        proc = self._start()
        self.assertTrue(self.sock.exists())
        self.assertEqual(self._stop(proc), 0, "a requested stop must be a clean exit")
        self.assertFalse(self.sock.exists(), "the socket must not survive a graceful stop")

    def test_restart_after_graceful_stop(self) -> None:
        self.assertEqual(self._stop(self._start()), 0)
        second = self._start()
        self.assertTrue(_serves_and_is_ready(self.sock))
        self.assertEqual(self._stop(second), 0)
        self.assertFalse(self.sock.exists())

    def test_abnormal_exit_leaves_a_socket_and_the_next_start_recovers(self) -> None:
        proc = self._start()
        proc.kill()                      # SIGKILL: no handler can run, exactly like a crash
        proc.wait(timeout=START_TIMEOUT)
        self.assertNotEqual(proc.returncode, 0)
        self.assertTrue(self.sock.exists(), "a hard kill is expected to leave the socket behind")
        replacement = self._start()      # must clean up the provably stale socket itself
        self.assertTrue(_serves_and_is_ready(self.sock))
        self.assertEqual(self._stop(replacement), 0)
        self.assertFalse(self.sock.exists())

    def test_a_live_instance_socket_is_never_stolen(self) -> None:
        live = self._start()
        result = subprocess.run(
            [PY, "-m", "lifesupply.service.cli", "serve", "--socket", str(self.sock)],
            env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=START_TIMEOUT)
        self.assertNotEqual(result.returncode, 0, "a second instance must refuse to start")
        self.assertIn(b"REFUSING_TO_UNLINK_LIVE_SOCKET", result.stderr)
        self.assertIsNone(live.poll(), "the first instance must still be running")
        self.assertTrue(_serves_and_is_ready(self.sock), "the first instance must still serve")
        self.assertEqual(self._stop(live), 0)

    def test_a_non_socket_path_is_refused_and_left_alone(self) -> None:
        self.sock.write_bytes(b"not a socket\n")
        result = subprocess.run(
            [PY, "-m", "lifesupply.service.cli", "serve", "--socket", str(self.sock)],
            env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=START_TIMEOUT)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b"REFUSING_TO_UNLINK_NON_SOCKET", result.stderr)
        self.assertTrue(self.sock.exists(), "a regular file must not be deleted")
        self.assertEqual(self.sock.read_bytes(), b"not a socket\n")

    def test_second_writer_on_the_same_data_root_cannot_write(self) -> None:
        """A second process on one data root may run, but it must never become a writer.

        The service degrades to FAILED instead of raising, so the assertions are about the
        behaviour that matters: not READY, writes refused, and the live instance unaffected.
        """
        other = self.root / "run" / "second.sock"
        live = self._start()
        second = self._spawn(other)
        self.assertTrue(_wait_until(other, _answers), "the second instance never answered")

        status = _request(other, {"op": "status"})
        self.assertNotEqual(status.get("service_state"), "READY",
                            "a second writer must never reach READY")
        self.assertFalse(status.get("writes_permitted"), "a second writer must not permit writes")

        refused = _request(other, {"op": "provision_workspace", "grant_id": "grant:none",
                                   "operation_key": "c15:second-writer",
                                   "workspace": {"subject_id": "nobody"}})
        self.assertEqual(refused.get("status"), "BLOCKED")
        self.assertEqual(refused.get("reason"), "SERVICE_FAILED")

        self.assertIsNone(live.poll(), "the live instance must still be running")
        self.assertTrue(_serves_and_is_ready(self.sock), "the live instance must still serve")
        self.assertEqual(self._stop(live), 0)
        if second.poll() is None:
            second.kill()
            second.wait(timeout=START_TIMEOUT)


@unittest.skipIf(fcntl is None, "the service requires fcntl (POSIX only)")
class InflightDrainTests(unittest.TestCase):
    """The bounded drain is what lets a stop wait for an in-flight request."""

    class _StubService:
        def dispatch(self, request, *, peer_uid):  # pragma: no cover - never called here
            return {"status": "OK"}

        def close(self) -> None:
            return None

    def test_drain_reports_idle_and_waits_for_inflight(self) -> None:
        from lifesupply.service.life_supply import LifeSupplySocketServer

        with tempfile.TemporaryDirectory(prefix="ls_drain_") as tmp:
            path = Path(tmp) / "drain.sock"
            server = LifeSupplySocketServer(str(path), self._StubService(), socket_mode=0o600)
            try:
                self.assertEqual(server.inflight_requests, 0)
                self.assertTrue(server.drain(0.05), "an idle server drains immediately")
                server._enter_request()
                self.assertEqual(server.inflight_requests, 1)
                self.assertFalse(server.drain(0.05), "a busy server must not report drained")
                server._leave_request()
                self.assertEqual(server.inflight_requests, 0)
                self.assertTrue(server.drain(0.05), "the server drains once the request ends")
            finally:
                server.server_close()
                path.unlink(missing_ok=True)



class StopSignalTypeTests(unittest.TestCase):
    """A requested stop must not be swallowed by the service's own ``except Exception``."""

    def test_stop_signal_is_not_an_ordinary_exception(self) -> None:
        from lifesupply.service.life_supply import ServiceStoppedBySignal

        self.assertTrue(issubclass(ServiceStoppedBySignal, BaseException))
        self.assertFalse(issubclass(ServiceStoppedBySignal, Exception),
                         "the service's own except-Exception handlers must not catch a stop")

    def test_stop_handlers_are_installed_before_the_service_is_built(self) -> None:
        """The handler installation must precede any startup work, not follow it."""
        import inspect

        from lifesupply.service import life_supply as module

        source = inspect.getsource(module.run_service)
        self.assertLess(source.index("_install_stop_handlers()"),
                        source.index("LifeSupplyService("),
                        "handlers must be installed before the service is constructed")
        self.assertLess(source.index("_install_stop_handlers()"),
                        source.index("LifeSupplySocketServer("),
                        "handlers must be installed before the socket is bound")

if __name__ == "__main__":  # pragma: no cover
    unittest.main()
