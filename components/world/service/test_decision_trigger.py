#!/usr/bin/env python3
"""Decision trigger tests (M13C §3/§6/§9/§11; M13C-FINAL §B)."""

from __future__ import annotations

import importlib.util
import json
import logging
import os
import pathlib
import sys
import unittest

import conftest  # noqa: F401

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "bridge"))
sys.path.insert(0, str(HERE))

import world_body_client as wbc  # noqa: E402

_SPEC = importlib.util.spec_from_file_location(
    "decision_trigger", HERE / "bridge" / "decision_trigger.py")
dt = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(dt)

_SPEC2 = importlib.util.spec_from_file_location(
    "action_intent", HERE / "action_intent.py")
ai = importlib.util.module_from_spec(_SPEC2)
_SPEC2.loader.exec_module(ai)

def _a_live_candidate():
    """Pick from the isolated fixture; never connect to a production socket."""
    space_now = ai.build_action_space(SNAPSHOT)
    cands = space_now["candidates"]
    assert cands, "no candidates offered"
    return cands[0]["candidate_id"]



SNAPSHOT = {
    "world_revision": 13,
    "location": {"place_id": "home", "area_id": "bedroom"},
    "pose": "standing",
    "hands": {"left_hand": None, "right_hand": None},
    "visible_objects": [{"label": "basic_bed", "kind": "room", "in_hand": False}],
}


class FakeClient:
    @property
    def enabled(self):
        return wbc.resolve_mode() != wbc.MODE_OFF
    last_status = wbc.OK

    def __init__(self, *, ready=True):
        self._ready = ready

    def get_status(self):
        return wbc.ClientResult(wbc.OK, data={"ready": self._ready,
                                              "readiness": "READY" if self._ready
                                              else "NOT_READY"})

    def get_snapshot(self):
        return wbc.ClientResult(wbc.OK, data=dict(SNAPSHOT))

    def get_body_signals(self):
        return wbc.ClientResult(wbc.OK, data={"signals": [], "recovery": {}})

    def submit_action(self, **kwargs):
        raise AssertionError("submit must not be reached in default tests")


class DecisionTriggerTests(unittest.TestCase):
    def setUp(self):
        self._saved = dict(os.environ)
        os.environ[wbc.MODE_ENV] = "canonical"
        for key in (ai.GATE_ENV, "WORLD_BODY_DECISION_SELECTED_CANDIDATE",
                    "WORLD_BODY_DECISION_INTENT_SUMMARY",
                    dt.OVERRIDE_TEST_KEY_ENV):
            os.environ.pop(key, None)
        dt._LAST_SEEN.clear()
        self._tmp = pathlib.Path(__import__("tempfile").mkdtemp(prefix="m13c-"))
        import bridge_init as bi
        from unittest.mock import patch
        patcher = patch.object(bi, '_CLIENT', FakeClient())
        patcher.start()
        self.addCleanup(patcher.stop)
        import shutil
        self.addCleanup(shutil.rmtree, self._tmp)
        os.environ["HERMES_HOME"] = str(self._tmp)
        os.environ["WORLD_ACTION_LIFECYCLE_PATH"] = str(
            self._tmp / "action_lifecycle.jsonl")
        # M15C: keep the self-maintenance policy out of the production store,
        # and make the dwell requirement explicit for the test.
        os.environ["WORLD_SELF_MAINTENANCE_STATE"] = str(
            self._tmp / "self_maintenance_state.json")
        os.environ["WORLD_SELF_MAINTENANCE_MIN_DWELL"] = "0"

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._saved)
        dt._LAST_SEEN.clear()

    def test_natural_turn_yields_no_action_and_persists(self):
        os.environ["HERMES_HOME"] = str(self._tmp)
        os.environ["WORLD_ACTION_LIFECYCLE_PATH"] = str(
            self._tmp / "action_lifecycle.jsonl")
        # M15C: keep the self-maintenance policy out of the production store,
        # and make the dwell requirement explicit for the test.
        os.environ["WORLD_SELF_MAINTENANCE_STATE"] = str(
            self._tmp / "self_maintenance_state.json")
        os.environ["WORLD_SELF_MAINTENANCE_MIN_DWELL"] = "0"
        dt._LAST_SEEN.clear()
        outcome = dt.evaluate_turn_decision(session_id="s1", turn_id="t1",
                                            platform="telegram")
        self.assertEqual(outcome["outcome"], dt.NO_ACTION)
        self.assertIsNone(outcome["intent"])
        self.assertIsNotNone(outcome["decision_id"])
        log = self._tmp / "xinxi" / "world_decision_log.jsonl"
        self.assertTrue(log.exists())
        record = json.loads(log.read_text().splitlines()[-1])
        self.assertEqual(record["terminal_decision"], dt.NO_ACTION)
        self.assertEqual(record["selection_source"], "default_no_action")
        self.assertTrue(record["decision_id"].startswith("decision:"))

    def test_off_integration_yields_no_decision(self):
        os.environ[wbc.MODE_ENV] = "off"
        dt._LAST_SEEN.clear()
        outcome = dt.evaluate_turn_decision(session_id="s1", turn_id="t1")
        self.assertEqual(outcome["outcome"], "INTEGRATION_OFF")
        self.assertIsNone(outcome["decision_id"])

    def test_not_ready_yields_no_decision_and_no_intent(self):
        dt._LAST_SEEN.clear()
        import bridge_init as bi

        real = bi._CLIENT
        bi._CLIENT = FakeClient(ready=False)
        try:
            outcome = dt.evaluate_turn_decision(session_id="s1", turn_id="t1")
        finally:
            bi._CLIENT = real
        self.assertEqual(outcome["outcome"], "RUNTIME_NOT_READY")
        self.assertIsNone(outcome["intent"])

    def test_duplicate_turn_is_deduplicated(self):
        dt._LAST_SEEN.clear()
        os.environ["WORLD_BODY_DECISION_SELECTED_CANDIDATE"] = _a_live_candidate()
        os.environ[dt.OVERRIDE_TEST_KEY_ENV] = dt.OVERRIDE_TEST_VALUE
        first = dt.evaluate_turn_decision(session_id="s1", turn_id="t1")
        second = dt.evaluate_turn_decision(session_id="s1", turn_id="t1")
        self.assertEqual(second["outcome"], "DUPLICATE_TURN")
        self.assertIsNone(second["intent"])
        del first

    # --- M13C-FINAL §B: the override is a test-only path -------------------

    def test_override_without_test_key_is_refused_never_executed(self):
        """A production turn (no test key) cannot mutate through the override."""
        dt._LAST_SEEN.clear()
        os.environ["WORLD_BODY_DECISION_SELECTED_CANDIDATE"] = _a_live_candidate()
        outcome = dt.evaluate_turn_decision(session_id="s1", turn_id="t1",
                                            platform="telegram")
        self.assertEqual(outcome["outcome"], dt.OVERRIDE_UNAUTHORIZED)
        self.assertIsNone(outcome["intent"])
        self.assertIsNone(outcome["result"])
        record = outcome["decision"]
        self.assertEqual(record["terminal_decision"], dt.OVERRIDE_UNAUTHORIZED)
        self.assertEqual(record["selection_source"],
                         "environment_override:unauthorized")
        self.assertTrue(record["override_requested"])
        self.assertFalse(record["override_authorized"])
        # persisted for soak accounting
        log = self._tmp / "xinxi" / "world_decision_log.jsonl"
        persisted = json.loads(log.read_text().splitlines()[-1])
        self.assertEqual(persisted["terminal_decision"],
                         dt.OVERRIDE_UNAUTHORIZED)

    def test_override_with_wrong_key_is_refused(self):
        dt._LAST_SEEN.clear()
        os.environ["WORLD_BODY_DECISION_SELECTED_CANDIDATE"] = _a_live_candidate()
        os.environ[dt.OVERRIDE_TEST_KEY_ENV] = "SOMETHING_ELSE"
        outcome = dt.evaluate_turn_decision(session_id="s1", turn_id="t1")
        self.assertEqual(outcome["outcome"], dt.OVERRIDE_UNAUTHORIZED)
        self.assertIsNone(outcome["intent"])

    def test_authorized_override_carries_m13c_test_provenance(self):
        dt._LAST_SEEN.clear()
        os.environ["WORLD_BODY_DECISION_SELECTED_CANDIDATE"] = _a_live_candidate()
        os.environ[dt.OVERRIDE_TEST_KEY_ENV] = dt.OVERRIDE_TEST_VALUE
        outcome = dt.evaluate_turn_decision(session_id="s1", turn_id="t1",
                                            platform="telegram")
        record = outcome["decision"]
        self.assertEqual(record["terminal_decision"], dt.ACTION_SELECTED)
        self.assertEqual(record["selection_source"],
                         "environment_override:M13C_TEST")
        self.assertTrue(record["override_authorized"])
        log = self._tmp / "xinxi" / "world_decision_log.jsonl"
        blob = log.read_text()
        self.assertNotIn("environment_override\"", blob.replace(
            "environment_override:M13C_TEST", "").replace(
            "environment_override:unauthorized", ""))
        self.assertIn("environment_override:M13C_TEST", blob)

    def test_selected_candidate_produces_intent_and_provenance(self):
        dt._LAST_SEEN.clear()
        os.environ["WORLD_BODY_DECISION_SELECTED_CANDIDATE"] = _a_live_candidate()
        os.environ[dt.OVERRIDE_TEST_KEY_ENV] = dt.OVERRIDE_TEST_VALUE
        outcome = dt.evaluate_turn_decision(session_id="s1", turn_id="t1",
                                            platform="telegram")
        record = outcome["decision"]
        self.assertEqual(record["terminal_decision"], dt.ACTION_SELECTED)
        self.assertEqual(record["selected_candidate_id"],
                        os.environ["WORLD_BODY_DECISION_SELECTED_CANDIDATE"])
        self.assertIn("intent_id", record)
        self.assertIn("execution_id", record)
        # full provenance chain (section 11)
        self.assertTrue(record["decision_id"].startswith("decision:"))
        self.assertTrue(record["intent_id"].startswith("world-intent:"))
        self.assertTrue(record["execution_id"].startswith("world-exec:"))

    def test_gate_off_blocks_mutation_but_decision_is_persisted(self):
        dt._LAST_SEEN.clear()
        os.environ[ai.GATE_ENV] = ai.GATE_OFF
        os.environ["WORLD_BODY_DECISION_SELECTED_CANDIDATE"] = _a_live_candidate()
        os.environ[dt.OVERRIDE_TEST_KEY_ENV] = dt.OVERRIDE_TEST_VALUE
        outcome = dt.evaluate_turn_decision(session_id="s1", turn_id="t1")
        self.assertEqual((outcome["result"] or {}).get("outcome"), "GATE_OFF")
        self.assertEqual(outcome["decision"]["terminal_decision"], dt.ACTION_SELECTED)
        log = self._tmp / "xinxi" / "world_decision_log.jsonl"
        record = json.loads(log.read_text().splitlines()[-1])
        self.assertEqual(record["intent_status"], ai.STATUS_REJECTED)

    def test_invalid_selection_is_recorded_not_executed(self):
        dt._LAST_SEEN.clear()
        os.environ["WORLD_BODY_DECISION_SELECTED_CANDIDATE"] = "pose:teleport"
        os.environ[dt.OVERRIDE_TEST_KEY_ENV] = dt.OVERRIDE_TEST_VALUE
        outcome = dt.evaluate_turn_decision(session_id="s1", turn_id="t1")
        self.assertEqual(outcome["outcome"], "INVALID_SELECTION")
        self.assertIsNone(outcome["intent"])
        record = outcome["decision"]
        self.assertEqual(record["terminal_decision"], "INVALID_SELECTION")

    def test_engine_vocabulary_never_enters_the_decision_log(self):
        dt._LAST_SEEN.clear()
        os.environ["WORLD_BODY_DECISION_SELECTED_CANDIDATE"] = _a_live_candidate()
        os.environ[dt.OVERRIDE_TEST_KEY_ENV] = dt.OVERRIDE_TEST_VALUE
        dt.evaluate_turn_decision(session_id="s1", turn_id="t1")
        log = self._tmp / "xinxi" / "world_decision_log.jsonl"
        blob = log.read_text()
        self.assertNotIn("source_body_version", blob)
        self.assertNotIn("signal_id", blob)

    def test_hook_returns_capsule_even_after_decision_failure(self):
        """Fail-open: a Decision failure must not block the chat turn.

        Whatever happens inside the trigger, the hook itself returns None (no
        capsule) and never raises into the turn.
        """

        dt._LAST_SEEN.clear()
        original = dt.wbc.WorldBodyClient

        class Broken:
            def __init__(self, **kwargs):
                raise RuntimeError("boom")

        dt.wbc.WorldBodyClient = Broken
        try:
            result = dt.decision_hook(session_id="s1", turn_id="t1")
            self.assertIsNone(result)  # capsule hook also failed open -> None
        finally:
            dt.wbc.WorldBodyClient = original

    def test_decision_hook_registers_as_pre_llm_call(self):
        recorded = []

        class Ctx:
            def register_hook(self, name, cb):
                recorded.append(name)

        dt.register(Ctx())
        self.assertEqual(recorded, ["pre_llm_call"])


if __name__ == "__main__":
    unittest.main(verbosity=2)


class ProductionEventTests(unittest.TestCase):
    """PROD-CUTOVER §4/§5/§6/§13: production selection is event-gated.

    Both the production mutation gate (canonical) and the explicit
    production-events switch (on) are required; anything else is NO_ACTION.
    """

    def setUp(self):
        self._saved = dict(os.environ)
        os.environ[wbc.MODE_ENV] = "canonical"
        for key in ("WORLD_BODY_DECISION_SELECTED_CANDIDATE",
                    dt.OVERRIDE_TEST_KEY_ENV, dt.PRODUCTION_EVENTS_ENV):
            os.environ.pop(key, None)
        dt._LAST_SEEN.clear()
        import tempfile
        self._tmp = pathlib.Path(tempfile.mkdtemp(prefix="pdc-"))
        os.environ["HERMES_HOME"] = str(self._tmp)
        os.environ["WORLD_ACTION_LIFECYCLE_PATH"] = str(
            self._tmp / "action_lifecycle.jsonl")
        # M15C: keep the self-maintenance policy out of the production store,
        # and make the dwell requirement explicit for the test.
        os.environ["WORLD_SELF_MAINTENANCE_STATE"] = str(
            self._tmp / "self_maintenance_state.json")
        os.environ["WORLD_SELF_MAINTENANCE_MIN_DWELL"] = "0"
        os.environ["WORLD_ACTION_AUTH_REGISTRY_DIR"] = str(self._tmp)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._saved)
        dt._LAST_SEEN.clear()
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _client(self, *, recovery_state="interrupted", fatigue_noticeable=True):
        class EventClient(FakeClient):
            def get_snapshot(self):
                snap = dict(SNAPSHOT)
                snap["pose"] = "standing"
                return wbc.ClientResult(wbc.OK, data=snap)

            def get_body_signals(self):
                return wbc.ClientResult(wbc.OK, data={
                    "signals": [{"signal_type": "fatigue"}],
                    "recovery": {"state": recovery_state, "mode": "rest",
                                 "trend": "worsening"},
                    "latch": {"fatigue_noticeable": fatigue_noticeable,
                              "fatigue_high": False},
                })

            def submit_action(self, **kwargs):
                # the real client maps status=applied -> SUCCESS; the executor
                # maps SUCCESS -> STATUS_APPLIED.  Mirror that here.
                return wbc.ClientResult(wbc.SUCCESS, data={"status": "applied",
                                                           "reason_code": "OK"})

        return EventClient()

    def _run(self, fake, turn="t1"):
        import bridge_init as bi
        real = bi._CLIENT
        bi._CLIENT = fake
        dt._LAST_SEEN.clear()
        try:
            return dt.evaluate_turn_decision(session_id="pdc", turn_id=turn,
                                             platform="telegram")
        finally:
            bi._CLIENT = real

    def test_events_off_by_default_means_no_action(self):
        os.environ[ai.GATE_ENV] = ai.GATE_CANONICAL
        outcome = self._run(self._client())
        self.assertEqual(outcome["outcome"], dt.NO_ACTION)
        self.assertFalse(outcome["decision"]["production_event"])

    def test_events_on_but_gate_off_means_no_action(self):
        os.environ[ai.GATE_ENV] = ai.GATE_OFF
        os.environ[dt.PRODUCTION_EVENTS_ENV] = dt.PRODUCTION_EVENTS_ON
        outcome = self._run(self._client())
        self.assertEqual(outcome["outcome"], dt.NO_ACTION)

    def test_event_with_gate_canonical_selects_rest_pose(self):
        os.environ[ai.GATE_ENV] = ai.GATE_CANONICAL
        os.environ[dt.PRODUCTION_EVENTS_ENV] = dt.PRODUCTION_EVENTS_ON
        outcome = self._run(self._client())
        record = outcome["decision"]
        self.assertEqual(record["terminal_decision"], dt.ACTION_SELECTED)
        self.assertTrue(str(record["selection_source"]).startswith(
            "runtime_event:RECOVERY_"))
        self.assertEqual(record["maintenance_event"], "RECOVERY_WORSENING")
        self.assertTrue(record["production_event"])
        self.assertFalse(record["override_requested"])
        self.assertFalse(record["override_authorized"])
        self.assertTrue(str(record.get("intent_id")).startswith("world-intent:"))
        # a production action really submitted (M14A capability carried it)
        self.assertEqual((outcome["result"] or {}).get("intent_status"),
                         ai.STATUS_APPLIED)

    def test_no_fatigue_latch_and_no_dwell_means_no_action(self):
        """Without a band AND without dwell the policy stays silent."""

        os.environ[ai.GATE_ENV] = ai.GATE_CANONICAL
        os.environ[dt.PRODUCTION_EVENTS_ENV] = dt.PRODUCTION_EVENTS_ON
        os.environ["WORLD_SELF_MAINTENANCE_MIN_DWELL"] = "3600"
        outcome = self._run(self._client(fatigue_noticeable=False))
        self.assertEqual(outcome["outcome"], dt.NO_ACTION)

    def test_recovery_not_interrupted_means_no_action(self):
        os.environ[ai.GATE_ENV] = ai.GATE_CANONICAL
        os.environ[dt.PRODUCTION_EVENTS_ENV] = dt.PRODUCTION_EVENTS_ON
        outcome = self._run(self._client(recovery_state="active"))
        self.assertEqual(outcome["outcome"], dt.NO_ACTION)

    def test_switch_value_is_literal_on_only(self):
        os.environ[ai.GATE_ENV] = ai.GATE_CANONICAL
        os.environ[dt.PRODUCTION_EVENTS_ENV] = "yes"
        outcome = self._run(self._client())
        self.assertEqual(outcome["outcome"], dt.NO_ACTION)
        self.assertFalse(outcome["decision"]["production_events_enabled"])

    def test_override_still_requires_the_test_key(self):
        os.environ[ai.GATE_ENV] = ai.GATE_CANONICAL
        os.environ[dt.PRODUCTION_EVENTS_ENV] = dt.PRODUCTION_EVENTS_ON
        os.environ["WORLD_BODY_DECISION_SELECTED_CANDIDATE"] = "pose:seated"
        outcome = self._run(self._client())
        self.assertEqual(outcome["outcome"], dt.OVERRIDE_UNAUTHORIZED)

    def test_no_rest_candidate_offered_means_no_action(self):
        os.environ[ai.GATE_ENV] = ai.GATE_CANONICAL
        os.environ[dt.PRODUCTION_EVENTS_ENV] = dt.PRODUCTION_EVENTS_ON

        class NoRest(FakeClient):
            def get_snapshot(self):
                snap = dict(SNAPSHOT)
                snap["pose"] = "standing"
                return wbc.ClientResult(wbc.OK, data=snap)

            def get_body_signals(self):
                return wbc.ClientResult(wbc.OK, data={
                    "signals": [],
                    "recovery": {"state": "interrupted", "mode": "rest"},
                    "latch": {"fatigue_noticeable": True},
                })

        import action_intent as wired_ai  # the module the trigger imports
        original = wired_ai.build_action_space
        wired_ai.build_action_space = lambda snap, **kw: {
            "kind": "world_action_space", "candidates": [
                {"candidate_id": "pose:seated", "verb": "POSE", "params": {}}],
            "location": {}, "current_pose": "standing",
            "world_revision": 13}
        try:
            outcome = self._run(NoRest())
        finally:
            wired_ai.build_action_space = original
        self.assertEqual(outcome["outcome"], dt.NO_ACTION)

    def test_interrupted_worsening_rest_is_an_event(self):
        os.environ["WORLD_SELF_MAINTENANCE_MIN_DWELL"] = "0"
        """The runtime's own interrupted+worsening record is a body event."""

        class InterruptedRest(FakeClient):
            def get_snapshot(self):
                snap = dict(SNAPSHOT)
                snap["pose"] = "standing"
                return wbc.ClientResult(wbc.OK, data=snap)

            def get_body_signals(self):
                return wbc.ClientResult(wbc.OK, data={
                    "signals": [],
                    "recovery": {"state": "interrupted", "mode": "rest",
                                 "progress": 0.11, "trend": "worsening"},
                    "latch": {"fatigue_noticeable": False, "fatigue_high": False},
                })

            def submit_action(self, **kwargs):
                return wbc.ClientResult(wbc.SUCCESS, data={"status": "applied",
                                                           "reason_code": "OK"})

        os.environ[ai.GATE_ENV] = ai.GATE_CANONICAL
        os.environ[dt.PRODUCTION_EVENTS_ENV] = dt.PRODUCTION_EVENTS_ON
        outcome = self._run(InterruptedRest())
        record = outcome["decision"]
        self.assertEqual(record["terminal_decision"], dt.ACTION_SELECTED)
        self.assertTrue(
            str(record["selection_source"]).startswith("runtime_event:RECOVERY_"))

    def test_interrupted_but_not_worsening_is_not_an_event(self):
        os.environ[ai.GATE_ENV] = ai.GATE_CANONICAL
        os.environ[dt.PRODUCTION_EVENTS_ENV] = dt.PRODUCTION_EVENTS_ON
        # M15C: a stabilized (non-worsening) rest needs the fatigue band too
        os.environ["WORLD_SELF_MAINTENANCE_MIN_DWELL"] = "0"

        class FreshInterrupt(FakeClient):
            def get_snapshot(self):
                snap = dict(SNAPSHOT)
                snap["pose"] = "standing"
                return wbc.ClientResult(wbc.OK, data=snap)

            def get_body_signals(self):
                return wbc.ClientResult(wbc.OK, data={
                    "signals": [],
                    "recovery": {"state": "interrupted", "mode": "rest",
                                 "trend": "stalled"},
                    "latch": {"fatigue_noticeable": False},
                })

        outcome = self._run(FreshInterrupt())
        self.assertEqual(outcome["outcome"], dt.NO_ACTION)
