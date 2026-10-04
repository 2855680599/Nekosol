#!/usr/bin/env python3
"""Intent executor tests: the gate, the fresh read, shadow, and transport."""

from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import socket
import sys
import tempfile
import threading
import unittest

import conftest  # noqa: F401

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "bridge"))
sys.path.insert(0, str(HERE))

import world_body_client as wbc  # noqa: E402

_SPEC = importlib.util.spec_from_file_location("intent_executor", HERE / "bridge" / "intent_executor.py")
executor = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(executor)

_SPEC2 = importlib.util.spec_from_file_location("action_intent", HERE / "action_intent.py")
ai = importlib.util.module_from_spec(_SPEC2)
_SPEC2.loader.exec_module(ai)


SNAPSHOT = {
    "captured_at": "2026-09-26T02:00:00+00:00",
    "world_revision": 13,
    "location": {"place_id": "home", "area_id": "bedroom"},
    "pose": "standing",
    "hands": {"left_hand": None, "right_hand": None},
    "visible_objects": [{"label": "basic_bed", "kind": "room", "in_hand": False}],
}


def space():
    return ai.build_action_space(SNAPSHOT)


def an_intent(candidate_id="pose:seated"):
    choice = {
        "status": "validated",
        "snapshot_fingerprint": ai.fingerprint_action_space(space()),
        "action": "SELECT",
        "candidate_id": candidate_id,
        "intent_summary": "测试意图",
        "validated_at": SNAPSHOT["captured_at"],
    }
    return ai.build_intent(choice, space(), created_at=SNAPSHOT["captured_at"])


class FakeSocketServer:
    def __init__(self, responder):
        self.responder = responder
        self.dir = pathlib.Path(tempfile.mkdtemp(prefix="ai-sock-"))
        self.path = self.dir / "gateway.sock"
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.server.bind(str(self.path))
        self.server.listen(8)
        self.server.settimeout(0.2)
        self.requests = []
        self._stop = False
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self):
        while not self._stop:
            try:
                conn, _ = self.server.accept()
            except (socket.timeout, OSError):
                continue
            try:
                data = conn.recv(65536)
                req = json.loads(data.decode().strip().splitlines()[0])
                self.requests.append(req)
                payload = self.responder(req.get("command"), req.get("argument"))
            except Exception as exc:
                payload = {"ok": False, "error": type(exc).__name__}
            try:
                conn.sendall((json.dumps(payload) + "\n").encode())
            except OSError:
                pass
            finally:
                conn.close()

    def close(self):
        self._stop = True
        self._thread.join(timeout=2)
        self.server.close()
        try:
            self.path.unlink()
        except OSError:
            pass


class ExecutorGateTests(unittest.TestCase):

    def setUp(self):
        self._saved = dict(os.environ)
        # M15B: every test gets its own action lifecycle store, so a terminal
        # record from one test can never refuse the next test's attempt.
        import shutil as _shutil
        import tempfile as _tempfile
        self._lc_dir = _tempfile.mkdtemp(prefix="m15b-lc-")
        os.environ["WORLD_ACTION_LIFECYCLE_PATH"] = (
            self._lc_dir + "/action_lifecycle.jsonl")
        self.addCleanup(_shutil.rmtree, self._lc_dir, ignore_errors=True)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._saved)

    def test_gate_off_refuses_before_any_contact(self):
        result = executor.execute_intent(an_intent(), client=wbc.WorldBodyClient(mode="off"))
        self.assertEqual(result["intent_status"], ai.STATUS_REJECTED)
        self.assertEqual(result["outcome"], "GATE_OFF")
        self.assertFalse(result["authoritative"])

    def test_gate_shadow_builds_but_never_commits(self):
        calls = []

        def responder(command, argument):
            calls.append(command)
            if command == "get_status":
                return {"ok": True, "result": {"ready": True, "readiness": "READY"}}
            if command == "get_snapshot":
                return {"ok": True, "result": dict(SNAPSHOT)}
            raise AssertionError("unexpected command %s" % command)

        server = FakeSocketServer(responder)
        self.addCleanup(server.close)
        os.environ[ai.GATE_ENV] = ai.GATE_SHADOW
        client = wbc.WorldBodyClient(socket_path=server.path, mode=wbc.MODE_CANONICAL)
        result = executor.execute_intent(an_intent(), client=client)

        self.assertEqual(result["outcome"], "SHADOW_NO_COMMIT")
        self.assertEqual(result["intent_status"], ai.STATUS_PROPOSED)
        self.assertFalse(result["authoritative"])
        # a shadow run validates and builds the submission, but submits nothing
        self.assertEqual(calls, ["get_status", "get_snapshot"])
        self.assertEqual(result["submission_preview"]["action"], "POSE")
        self.assertEqual(result["submission_preview"]["observed_dependencies"],
                         {"world.revision": 13, "world.location": "home",
                          "body.pose": "standing"})

    def test_gate_canonical_submits_through_the_bridge(self):
        """M14A: the canonical branch mints an authorization and submits it.

        The submission must carry the Intent Executor's authorization_id (the
        socket consumes it) — no submission without a capability.
        """
        import json as _json
        import pathlib as _pathlib
        import tempfile

        calls = []
        payloads = []

        def responder(command, argument):
            calls.append(command)
            if command == "get_status":
                return {"ok": True, "result": {"ready": True, "readiness": "READY"}}
            if command == "get_snapshot":
                return {"ok": True, "result": dict(SNAPSHOT)}
            if command == "submit_action":
                payloads.append(dict(argument or {}))
                return {"ok": True, "result": {"status": "applied", "reason_code": "OK"}}
            raise AssertionError("unexpected command %s" % command)

        server = FakeSocketServer(responder)
        self.addCleanup(server.close)
        run_dir = _pathlib.Path(tempfile.mkdtemp(prefix="m14a-exec-"))
        self.addCleanup(__import__("shutil").rmtree, run_dir, ignore_errors=True)
        os.environ["WORLD_ACTION_AUTH_REGISTRY_DIR"] = str(run_dir)
        os.environ[ai.GATE_ENV] = ai.GATE_CANONICAL
        client = wbc.WorldBodyClient(socket_path=server.path, mode=wbc.MODE_CANONICAL)
        result = executor.execute_intent(an_intent(), client=client)

        self.assertEqual(result["intent_status"], ai.STATUS_APPLIED)
        self.assertEqual(calls, ["get_status", "get_snapshot", "submit_action"])
        # the capability travelled with the submission
        self.assertTrue(payloads and payloads[0].get("authorization_id"),
                        "canonical submission must carry an authorization_id")
        with (run_dir / "action_authorizations.json").open() as handle:
            registry = _json.load(handle)
        record = registry["records"][-1]
        self.assertEqual(record["state"], "ISSUED")
        self.assertEqual(record["execution_id"], payloads[0]["execution_id"])
        self.assertEqual(record["verb"], payloads[0]["action"])
        self.assertEqual(record["actor"], "chiyo")

    def test_canonical_without_registry_dir_does_not_submit(self):
        """M14A fail-closed: no capability source means no submission at all."""
        calls = []

        def responder(command, argument):
            calls.append(command)
            if command == "get_status":
                return {"ok": True, "result": {"ready": True, "readiness": "READY"}}
            if command == "get_snapshot":
                return {"ok": True, "result": dict(SNAPSHOT)}
            raise AssertionError("nothing may be submitted without a capability: %s" % command)

        server = FakeSocketServer(responder)
        self.addCleanup(server.close)
        for key in ("WORLD_ACTION_AUTH_REGISTRY_DIR", "CHIYO_WORLD_HOME", "HERMES_HOME"):
            os.environ.pop(key, None)
        os.environ[ai.GATE_ENV] = ai.GATE_CANONICAL
        client = wbc.WorldBodyClient(socket_path=server.path, mode=wbc.MODE_CANONICAL)
        result = executor.execute_intent(an_intent(), client=client)

        self.assertEqual(result["outcome"], "UNAVAILABLE")
        self.assertNotIn("submit_action", calls)

    def test_not_ready_runtime_injects_no_mutation(self):
        def responder(command, argument):
            if command == "get_status":
                return {"ok": True, "result": {"ready": False, "readiness": "NOT_READY"}}
            raise AssertionError("no further reads may happen: %s" % command)

        server = FakeSocketServer(responder)
        self.addCleanup(server.close)
        os.environ[ai.GATE_ENV] = ai.GATE_CANONICAL
        client = wbc.WorldBodyClient(socket_path=server.path, mode=wbc.MODE_CANONICAL)
        result = executor.execute_intent(an_intent(), client=client)
        self.assertEqual(result["intent_status"], ai.STATUS_UNKNOWN)
        self.assertIn("not READY", result["detail"])

    def test_down_runtime_yields_unavailable_without_fallback(self):
        import os as _os

        _os.environ[ai.GATE_ENV] = ai.GATE_CANONICAL
        client = wbc.WorldBodyClient(socket_path="/nonexistent/gateway.sock",
                                     mode=wbc.MODE_CANONICAL, read_retries=0)
        result = executor.execute_intent(an_intent(), client=client)
        self.assertEqual(result["outcome"], "UNAVAILABLE")
        self.assertIsNone(result.get("submission_preview"))
        self.assertEqual(result["outcome"], "UNAVAILABLE")
        self.assertIsNone(result.get("submission_preview"))


class ExecutorProvenanceTests(unittest.TestCase):
    def test_intent_to_submission_to_result_keeps_the_chain(self):
        intent = an_intent()
        submission = ai.build_submission(intent, fresh_snapshot=dict(SNAPSHOT))
        result = ai.map_result(intent, {"outcome": "SUCCESS"})
        self.assertEqual(result["intent_id"], intent["intent_id"])
        self.assertEqual(result["execution_id"], submission["execution_id"])
        self.assertEqual(result["decision_ref"], intent["decision_ref"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
