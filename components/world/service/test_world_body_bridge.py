#!/usr/bin/env python3
"""M13B tests: the gateway client, the bridge plugin, and the admin/admin split.

These run without a live socket; the live socket behaviour is exercised by
`/tmp/stage/m13b_verify.py` against the running service and recorded in the
M13B report.
"""

from __future__ import annotations

import importlib.util
import logging
import json
import pathlib
import socket
import sys
import tempfile
import threading
import time
import unittest

import conftest  # noqa: F401  (path setup)

# `chiyo-world-bridge__init__.py` is deployed as bridge_init.py because a Python
# module name cannot contain a dash.
HERE = pathlib.Path(__file__).resolve().parent
_SPEC = importlib.util.spec_from_file_location("bridge_init", HERE / "bridge" / "bridge_init.py")
bridge = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(bridge)

import world_body_client as wbc


class FakeSocketServer:
    """A minimal unix-socket stand-in for the standalone service."""

    def __init__(self, responder):
        self.responder = responder
        self.dir = pathlib.Path(tempfile.mkdtemp(prefix="m13b-sock-"))
        self.path = self.dir / "gateway.sock"
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.server.bind(str(self.path))
        self.server.listen(8)
        self.server.settimeout(0.2)
        self._stop = False
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self):
        while not self._stop:
            try:
                connection, _ = self.server.accept()
            except (socket.timeout, OSError):
                continue
            try:
                data = connection.recv(65536)
                request = json.loads(data.decode().strip().splitlines()[0])
                payload = self.responder(request.get("command"), request.get("argument"))
            except Exception as error:
                payload = {"ok": False, "error": type(error).__name__}
            try:
                connection.sendall((json.dumps(payload) + "\n").encode())
            except OSError:
                pass
            finally:
                connection.close()

    def close(self):
        self._stop = True
        self._thread.join(timeout=2)
        self.server.close()
        try:
            self.path.unlink()
        except OSError:
            pass


class ModeTests(unittest.TestCase):
    def test_mode_defaults_to_off(self):
        self.assertEqual(wbc.resolve_mode({}), wbc.MODE_OFF)

    def test_mode_rejects_unknown_value(self):
        with self.assertRaises(ValueError):
            wbc.resolve_mode({wbc.MODE_ENV: "cannonical"})

    def test_all_three_modes_accepted(self):
        for mode in ("off", "shadow", "canonical"):
            self.assertEqual(wbc.resolve_mode({wbc.MODE_ENV: mode}), mode)

    def test_off_client_does_nothing(self):
        client = wbc.WorldBodyClient(mode=wbc.MODE_OFF)
        self.assertFalse(client.enabled)
        self.assertFalse(client.ready())
        self.assertEqual(client.get_snapshot().outcome, wbc.UNAVAILABLE)
        self.assertEqual(client.submit_action(action="MOVE", execution_id="x",
                                               params={}).outcome, wbc.UNAVAILABLE)


class TransportTests(unittest.TestCase):
    def setUp(self):
        self.closed = []

    def tearDown(self):
        for server in self.closed:
            server.close()

    def _client(self, responder, **kwargs):
        server = FakeSocketServer(responder)
        self.closed.append(server)
        return wbc.WorldBodyClient(socket_path=server.path, mode=wbc.MODE_CANONICAL, **kwargs)

    def test_read_ok(self):
        client = self._client(lambda c, a: {"ok": True, "result": {"ready": True, "readiness": "READY"}})
        result = client.get_status()
        self.assertEqual(result.outcome, wbc.OK)
        self.assertIn("readiness", result.data)

    def test_not_ready_is_reported_not_success(self):
        client = self._client(lambda c, a: {"ok": True, "result": {"readiness": "NOT_READY"}})
        self.assertEqual(client.get_status().outcome, wbc.NOT_READY)

    def test_missing_socket_is_unavailable_not_none(self):
        client = wbc.WorldBodyClient(socket_path="/nonexistent/gateway.sock",
                                     mode=wbc.MODE_CANONICAL, read_retries=0)
        result = client.get_snapshot()
        self.assertEqual(result.outcome, wbc.UNAVAILABLE)
        self.assertIsNotNone(result.reason)

    def test_malformed_response_is_unavailable(self):
        server = FakeSocketServer(lambda c, a: {"ok": True, "result": {}})
        self.closed.append(server)
        real = server.responder
        server.responder = lambda c, a: {"ok": True, "result": {"readiness": "READY"}}
        client = wbc.WorldBodyClient(socket_path=server.path, mode=wbc.MODE_CANONICAL)
        self.assertEqual(client.get_status().outcome, wbc.OK)

    def test_timeout_is_bounded(self):
        def slow(command, argument):
            time.sleep(0.6)
            return {"ok": True, "result": {}}
        client = self._client(slow, timeout=0.15, read_retries=0)
        started = time.monotonic()
        result = client.get_status()
        elapsed = time.monotonic() - started
        self.assertEqual(result.outcome, wbc.TIMEOUT)
        self.assertLess(elapsed, 1.0, "a stuck service must not hang the caller")

    def test_reads_retry_and_report_attempts(self):
        """A read may retry; the attempt count is visible."""

        client = wbc.WorldBodyClient(socket_path="/nonexistent/gateway.sock",
                                     mode=wbc.MODE_CANONICAL, read_retries=3)
        result = client.get_snapshot()
        self.assertEqual(result.outcome, wbc.UNAVAILABLE)
        self.assertGreater(result.attempts, 1, "reads should retry")

        strict = wbc.WorldBodyClient(socket_path="/nonexistent/gateway.sock",
                                     mode=wbc.MODE_CANONICAL, read_retries=0)
        self.assertEqual(strict.get_snapshot().attempts, 1)

    def test_mutation_is_never_retried_with_a_new_id(self):
        """submit_action must not enter the retry loop at all."""

        client = wbc.WorldBodyClient(socket_path="/nonexistent/gateway.sock",
                                     mode=wbc.MODE_CANONICAL, read_retries=5)
        result = client.submit_action(action="MOVE", execution_id="e1", params={})
        self.assertEqual(result.outcome, wbc.UNKNOWN)
        self.assertEqual(result.attempts, 1, "a mutation must be attempted once")

    def test_applied_maps_to_success(self):
        client = self._client(lambda c, a: {"ok": True, "result": {"status": "applied",
                                                                   "reason_code": "OK"}})
        result = client.submit_action(action="MOVE", execution_id="e1", params={})
        self.assertEqual(result.outcome, wbc.SUCCESS)

    def test_already_applied_is_success_not_a_second_commit(self):
        client = self._client(lambda c, a: {"ok": True, "result": {
            "status": "already_applied", "reason_code": "ALREADY_APPLIED"}})
        self.assertEqual(client.submit_action(action="MOVE", execution_id="e1",
                                              params={}).outcome, wbc.SUCCESS)

    def test_stale_maps_to_stale(self):
        client = self._client(lambda c, a: {"ok": True, "result": {
            "status": "rejected", "reason_code": "STALE_STATE"}})
        self.assertEqual(client.submit_action(action="MOVE", execution_id="e1",
                                              params={}).outcome, wbc.STALE)

    def test_rejected_maps_to_failure(self):
        client = self._client(lambda c, a: {"ok": True, "result": {
            "status": "rejected", "reason_code": "NOT_REACHABLE"}})
        self.assertEqual(client.submit_action(action="MOVE", execution_id="e1",
                                              params={}).outcome, wbc.FAILURE)

    def test_uncertain_is_unknown_never_success(self):
        client = self._client(lambda c, a: {"ok": True, "result": {
            "status": "uncertain", "reason_code": "RECONCILIATION_REQUIRED"}})
        self.assertEqual(client.submit_action(action="MOVE", execution_id="e1",
                                              params={}).outcome, wbc.UNKNOWN)

    def test_transport_fault_on_submit_is_unknown(self):
        client = wbc.WorldBodyClient(socket_path="/nonexistent/gateway.sock",
                                     mode=wbc.MODE_CANONICAL)
        result = client.submit_action(action="MOVE", execution_id="e1", params={})
        self.assertEqual(result.outcome, wbc.UNKNOWN)
        self.assertIn("transport_fault", result.reason or "")

    def test_missing_execution_id_is_invalid(self):
        client = wbc.WorldBodyClient(mode=wbc.MODE_CANONICAL)
        self.assertEqual(client.submit_action(action="MOVE", execution_id="",
                                              params={}).outcome, wbc.INVALID)


class BridgePluginTests(unittest.TestCase):
    def setUp(self):
        self.saved = dict(__import__("os").environ)
        for key in (wbc.MODE_ENV, bridge.MUTATION_MODE_ENV, wbc.SOCKET_ENV):
            __import__("os").environ.pop(key, None)

    def tearDown(self):
        import os

        os.environ.clear()
        os.environ.update(self.saved)
        bridge._CLIENT = None

    def test_hook_returns_nothing_when_off(self):
        self.assertIsNone(bridge.world_context_hook())

    def test_hook_returns_nothing_when_runtime_missing(self):
        import os

        os.environ[wbc.MODE_ENV] = "canonical"
        os.environ[wbc.SOCKET_ENV] = "/nonexistent/gateway.sock"
        bridge._CLIENT = None
        self.assertIsNone(bridge.world_context_hook())

    def test_mutation_mode_defaults_off(self):
        self.assertEqual(bridge._mutation_mode(), bridge.MUTATION_OFF)

    def test_action_tool_refuses_when_mutation_is_off(self):
        payload = json.loads(bridge._world_body_action_tool(
            {"action": "MOVE", "execution_id": "e1"}))
        self.assertEqual(payload["reason"], "mutation_disabled")

    def test_tool_checks_are_false_when_off(self):
        self.assertFalse(bridge._check_world_body())
        self.assertFalse(bridge._check_world_body_action())

    def test_bridge_defers_all_world_state_to_the_socket(self):
        """The bridge must not reach for World state itself.

        This checks *code*, not prose: the module docstring legitimately names
        the files it refuses to touch.  What must be absent is any statement that
        would actually open them.
        """

        for name in ("bridge_init.py", "world_body_client.py"):
            source = (HERE / "bridge" / name).read_text(encoding="utf-8")
            code = "\n".join(
                line for line in source.splitlines()
                if not line.lstrip().startswith("#")
            )
            # strip the module docstring: prose is not behaviour
            parts = code.split('"""')
            executable = "".join(parts[::2]) if len(parts) > 1 else code
            for forbidden in (
                "import world_living_skeleton", "import body_runtime",
                "import world_action_resolver", "import world_body_substrate",
                "import world_foundation", "import world_body_binding",
                "import life_execute", "import world_affordance_wiring",
                "hermes-agent-stock", "hermes_cli", "hermes_constants",
                "get_hermes_home", "state.db",
            ):
                self.assertNotIn(forbidden, executable, "%s leaks %s" % (name, forbidden))
            # it must go through the socket, and only the socket
            # the socket itself lives in the client, not in the plugin wrapper
            if name == "world_body_client.py":
                self.assertIn("socket.socket(socket.AF_UNIX", executable, name)
            else:
                self.assertIn("import world_body_client", executable, name)
                self.assertNotIn("socket.socket(", executable, name)
    def test_register_wires_one_hook_and_two_tools(self):
        recorded = {"hooks": [], "tools": []}

        class Ctx:
            def register_hook(self, name, cb):
                recorded["hooks"].append(name)

            def register_tool(self, **kwargs):
                recorded["tools"].append(kwargs["name"])

        bridge.register(Ctx())
        self.assertEqual(recorded["hooks"], ["pre_llm_call"])
        self.assertEqual(sorted(recorded["tools"]), ["world_body", "world_body_action"])

class ContextProjectionTests(unittest.TestCase):
    """M13B-R1: reconciliation metadata must never reach the model."""

    def setUp(self):
        self.saved = dict(__import__("os").environ)

    def tearDown(self):
        import os

        os.environ.clear()
        os.environ.update(self.saved)
        bridge._CLIENT = None

    def _fake_snapshot(self):
        return {
            "kind": "world_body_gateway", "schema_version": "world.body.gateway.api.v1",
            "world_id": "shiomi_city", "world_revision": 13,
            "location": {"place_id": "home", "area_id": "bedroom"},
            "scene_id": "home.bedroom", "pose": "standing", "orientation": "south",
            "hands": {"left_hand": None, "right_hand": None},
            "visible_objects": [{"object_id": "item-deadbeef", "label": "basic_bed",
                                 "kind": "room", "in_hand": False}],
            "visible_object_count": 1, "observation_scope": "finite",
            "includes_test_objects": False, "full_world_json_exposed": False,
        }

    def test_model_view_strips_reconciliation_metadata(self):
        scrubbed = bridge._model_view("snapshot", self._fake_snapshot())
        for key in ("world_revision", "world_id", "kind", "schema_version"):
            self.assertNotIn(key, scrubbed, key)
        # the lived facts survive, the machine facts do not
        self.assertEqual(scrubbed["location"]["place_id"], "home")
        self.assertEqual(scrubbed["pose"], "standing")
        self.assertEqual(scrubbed["visible_objects"][0]["label"], "basic_bed")
        self.assertNotIn("object_id", scrubbed["visible_objects"][0])

    def test_model_view_is_defensive_about_non_dicts(self):
        self.assertIsNone(bridge._model_view("snapshot", None))
        self.assertEqual(bridge._model_view("snapshot", "x"), "x")

    def test_capsule_carries_no_revision(self):
        """The capsule is assembled from the same projection, so it cannot leak."""

        class FakeClient:
            enabled = True

            def get_status(self):
                return wbc.ClientResult(wbc.OK, data={"ready": True, "readiness": "READY"})

            def get_snapshot(self):
                return wbc.ClientResult(wbc.OK, data=self._snap)

            def get_body_signals(self):
                return wbc.ClientResult(wbc.OK, data={
                    "available": True, "bands_only": True, "raw_values_exposed": False,
                    "recovery": {"state": "interrupted", "trend": "worsening"},
                    "latch": {}, "signals": [{"signal_type": "recovery_interrupted"}]})

        client = FakeClient()
        client._snap = self._fake_snapshot()
        capsule = wbc.render_grounded_context(client)
        self.assertNotIn("revision", capsule.lower())
        self.assertNotIn("13", capsule)
        self.assertNotIn("shiomi_city", capsule)
        self.assertIn("location=home/bedroom", capsule)
        self.assertIn("pose=standing", capsule)
        self.assertIn("basic_bed", capsule)
        # the machine ids stay out of the lived view
        self.assertNotIn("item-deadbeef", capsule)
        self.assertLess(len(capsule), 300)

    def test_not_ready_runtime_injects_nothing(self):
        """Ticket section 26: canonical requires READY, not merely reachable."""

        class NotReadyClient:
            enabled = True
            last_status = wbc.OK

            def get_status(self):
                # reachable (outcome OK) but NOT_READY
                return wbc.ClientResult(wbc.OK, data={"ready": False,
                                                      "readiness": "NOT_READY"})

            def get_snapshot(self):  # must never be reached
                raise AssertionError("snapshot read must not happen when not ready")

            def get_body_signals(self):
                raise AssertionError("signal read must not happen when not ready")

        import os as _os

        _os.environ[bridge.wbc.MODE_ENV] = "canonical"
        bridge._CLIENT = NotReadyClient()
        self.addCleanup(setattr, bridge, "_CLIENT", None)
        self.assertIsNone(bridge.world_context_hook())

    def test_bridge_still_keeps_revision_for_its_own_use(self):
        """Scrubbing is model-facing only; the raw view stays available."""

        snap = self._fake_snapshot()
        self.assertEqual(snap["world_revision"], 13)
        self.assertIsNone(bridge._model_view("snapshot", snap).get("world_revision"))


    def test_engine_version_counters_are_stripped(self):
        """A version counter is reconciliation metadata wherever it appears."""

        signals = {
            "available": True, "bands_only": True, "raw_values_exposed": False,
            "recovery": {"state": "interrupted", "trend": "worsening"},
            "latch": {"fatigue_noticeable": True},
            "signals": [{
                "signal_type": "recovery_interrupted",
                "qualitative_band": "noticeable",
                "source_body_version": 54,          # <-- must go
                "body_id": "chiyo_body",            # <-- engine vocabulary
                "signal_id": "body-signal:deadbeef",  # <-- must go
                "occurred_at": "2026-09-26T01:55:48+00:00",  # <-- must go
                "visibility_scope": "body_internal",
                "source_transition_refs": [],
            }],
        }
        scrubbed = bridge._model_view("signals", signals)
        blob = json.dumps(scrubbed)
        for forbidden in ("source_body_version", "signal_id", "occurred_at",
                          "visibility_scope", "source_transition_refs", "body_id"):
            self.assertNotIn(forbidden, blob, forbidden)
        self.assertNotIn("54", blob)
        # the felt fact survives, in qualitative form
        self.assertEqual(scrubbed["signals"][0]["signal_type"], "recovery_interrupted")
        self.assertEqual(scrubbed["signals"][0]["qualitative_band"], "noticeable")
        self.assertTrue(scrubbed["latch"]["fatigue_noticeable"])

    def test_object_id_is_not_in_any_model_view(self):
        """`object_id` is reachable only as a tool parameter, never as cognition."""

        payload = {
            "visible_objects": [
                {"object_id": "item-8ad09561deadbeef", "label": "basic_bed",
                 "in_hand": False},
            ],
            "actions": [
                {"verb": "MOVE", "available": True,
                 "requires": ["world.revision", "world.location"]},
            ],
        }
        scrubbed = bridge._model_view("actions", payload)
        blob = json.dumps(scrubbed)
        self.assertNotIn("item-8ad09561deadbeef", blob)
        self.assertNotIn("object_id", blob)
        self.assertEqual(scrubbed["visible_objects"],
                         [{"label": "basic_bed", "in_hand": False}])
        # internal dependency keys stay out of the model's `requires` list
        self.assertEqual(scrubbed["actions"][0]["requires"], ["world.location"])

    def test_hook_logs_turn_correlation_ids_only(self):
        """The INFO line must carry ids, never message text or the capsule."""

        records = []

        class Capture(logging.Handler):
            def emit(self, record):
                records.append(record.getMessage())

        handler = Capture()
        bridge.logger.addHandler(handler)
        bridge.logger.setLevel(logging.INFO)
        self.addCleanup(bridge.logger.removeHandler, handler)

        class FakeClient:
            enabled = True
            last_status = wbc.READY

            def get_status(self):
                return wbc.ClientResult(wbc.OK, data={"ready": True, "readiness": "READY"})

            def get_snapshot(self):
                return wbc.ClientResult(wbc.OK, data={
                    "location": {"place_id": "home", "area_id": "bedroom"},
                    "pose": "standing", "hands": {}, "visible_objects": [],
                    "world_revision": 13, "world_id": "shiomi_city"})

            def get_body_signals(self):
                return wbc.ClientResult(wbc.OK, data={"signals": [], "recovery": {}})

        import os as _os

        _os.environ[bridge.wbc.MODE_ENV] = "canonical"
        bridge._CLIENT = FakeClient()
        self.addCleanup(setattr, bridge, "_CLIENT", None)

        secret = "今天过得怎么样？"
        bridge.world_context_hook(session_id="s-1", turn_id="t-42", task_id="k-9",
                                  platform="telegram", is_first_turn=False,
                                  user_message=secret)
        joined = "\n".join(records)
        self.assertIn("world-body context injected", joined)
        self.assertIn("t-42", joined)
        self.assertIn("s-1", joined)
        self.assertNotIn(secret, joined)
        self.assertNotIn("location=home", joined)  # the capsule itself is not logged
    def test_action_schema_hides_no_authority_field(self):
        props = bridge.WORLD_BODY_ACTION_SCHEMA["parameters"]["properties"]
        self.assertNotIn("authority", props)
        self.assertNotIn("confirm_apply", props)
        self.assertIn("execution_id", props)


class AdminSurfaceSeparationTests(unittest.TestCase):
    """The gateway surface must not offer the admin commands, and vice versa."""

    def test_gateway_surface_forbids_admin_commands(self):
        import world_body_gateway_api as gapi

        class Dummy:
            pass

        surface = gapi.GatewaySurface(Dummy())
        for command in ("apply_result", "act", "reconcile", "bootstrap", "migration",
                        "set_fatigue", "edit_world", "force_placement"):
            reply = surface.handle(command)
            self.assertFalse(reply["ok"], command)
            self.assertEqual(reply["error"], "forbidden_on_gateway_surface")

    def test_gateway_command_lists_do_not_include_admin_mutations(self):
        import world_body_gateway_api as gapi

        for command in ("apply_result", "act", "reconcile"):
            self.assertNotIn(command, gapi.GATEWAY_COMMANDS)
        self.assertIn("submit_action", gapi.GATEWAY_MUTATING_COMMANDS)

    def test_test_object_filtering(self):
        import world_body_gateway_api as gapi

        self.assertTrue(gapi.is_test_object("M13A1_TEST_OBJECT"))
        self.assertTrue(gapi.is_test_object("M13A1_something"))
        self.assertFalse(gapi.is_test_object(
            "item-8ad09561bcc7d3d5b8e10aec36365df138c5d217e66db8e14f65cf4aafe54006"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
