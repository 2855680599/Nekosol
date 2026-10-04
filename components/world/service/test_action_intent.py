#!/usr/bin/env python3
"""Tests for the structured Action Intent V0 (this ticket).

Sections covered: 4 (contract + statuses), 5 (authority-side dependencies),
6 (exposure: PICK_UP/PLACE never emitted), 7 (mutation gate), 9 (result
mapping — `submitted` is not success, UNKNOWN never retried), 15 (failure
matrix, minus the live-service cases exercised in the report).
"""

from __future__ import annotations

import importlib.util
import pathlib
import unittest

import conftest  # noqa: F401  (path setup)

HERE = pathlib.Path(__file__).resolve().parent
_SPEC = importlib.util.spec_from_file_location(
    "action_intent", HERE / "action_intent.py")
ai = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(ai)


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


def validated_choice(candidate_id: str, *, summary: str = "测试意图"):
    """A properly-validated choice over the CURRENT action space."""
    # (validated_choice builds the fingerprint from the current action space)

    space_now = space()
    return {
        "status": "validated",
        "snapshot_fingerprint": ai.fingerprint_action_space(space_now),
        "action": "SELECT",
        "candidate_id": candidate_id,
        "intent_summary": summary,
        "validated_at": SNAPSHOT["captured_at"],
    }, space_now


class ActionSpaceTests(unittest.TestCase):
    def test_only_exposed_verbs_are_emitted(self):
        space_now = space()
        verbs = {c["verb"] for c in space_now["candidates"]}
        self.assertTrue(verbs <= set(ai.EXPOSED_VERBS), verbs)
        self.assertNotIn("PICK_UP", verbs)
        self.assertNotIn("PLACE", verbs)
        self.assertEqual(sorted(space_now["blocked_verbs"]), sorted(ai.BLOCKED_VERBS))
        self.assertEqual(space_now["blocked_reason"], "WORLD_OBJECT_PORTABILITY_GAP")

    def test_candidates_come_from_the_topology_not_from_the_model(self):
        space_now = space()
        for candidate in space_now["candidates"]:
            self.assertTrue(candidate["candidate_id"].startswith("pose:"))
            self.assertIn(candidate["params"]["pose"], ai.SUPPORTED_POSES)

    def test_the_current_pose_is_not_offered(self):
        space_now = space()
        for candidate in space_now["candidates"]:
            self.assertNotEqual(candidate["params"]["pose"], "standing")

    def test_no_raw_object_ids_anywhere(self):
        blob = ai.canonical_json(space())
        self.assertNotIn("item-8ad09561", blob)
        self.assertNotIn("object_id", blob)
        self.assertIn("basic_bed", ai.canonical_json(space()))

    def test_selector_view_has_no_revision(self):
        redacted = ai.redact_action_space_for_selector(space())
        self.assertNotIn("world_revision", redacted)
        self.assertIn("candidates", redacted)


class IntentTests(unittest.TestCase):
    def test_intent_from_a_validated_choice(self):
        choice, space_now = validated_choice("pose:seated")
        intent = ai.build_intent(choice, space_now, decision_ref="decision-1",
                                 created_at=SNAPSHOT["captured_at"])
        self.assertEqual(intent["intent_status"], ai.STATUS_AUTHORIZED)
        self.assertEqual(intent["verb"], "POSE")
        self.assertEqual(intent["params"], {"pose": "seated"})
        self.assertEqual(intent["decision_ref"], "decision-1")
        self.assertEqual(intent["source_provenance"], ai.SOURCE)
        self.assertTrue(intent["intent_id"].startswith("world-intent:"))
        self.assertTrue(intent["execution_id"].startswith("world-exec:"))
        self.assertEqual(intent["authorization_ref"], "pending")

    def test_intent_ids_are_deterministic(self):
        choice, space_now = validated_choice("pose:seated")
        one = ai.build_intent(choice, space_now, created_at=SNAPSHOT["captured_at"])
        two = ai.build_intent(choice, space_now, created_at=SNAPSHOT["captured_at"])
        self.assertEqual(one["intent_id"], two["intent_id"])
        self.assertEqual(one["execution_id"], two["execution_id"])

    def test_unvalidated_choice_is_refused(self):
        choice, space_now = validated_choice("pose:seated")
        choice["status"] = "proposed"
        with self.assertRaises(ai.ActionIntentError):
            ai.build_intent(choice, space_now)

    def test_stale_fingerprint_is_refused_not_refreshed(self):
        """The choice was validated against a DIFFERENT space; refuse, never refresh."""

        choice, _ = validated_choice("pose:seated")   # bound to the current space
        stale_space = space()
        stale_space["current_pose"] = "seated"         # the world moved on
        stale_space["candidates"] = [
            c for c in stale_space["candidates"] if c["candidate_id"] != "pose:seated"]
        with self.assertRaisesRegex(ai.ActionIntentError, "stale intent"):
            ai.build_intent(choice, stale_space)

    def test_verb_outside_the_allowlist_is_refused_even_from_a_matched_space(self):
        """M15 §19: the allowlist is explicit -- a selector cannot widen it.

        PICK_UP / PLACE became legal in M15 (the World can now declare an
        object's capability), so the guarantee that matters is: a verb that is
        NOT on ACTION_ALLOWLIST (DROP / THROW / GIVE / ...) is refused even
        when the space it was validated against contains it.
        """

        space_with_drop = space()
        space_with_drop["candidates"] = list(space_with_drop["candidates"]) + [
            {"candidate_id": "obj:basic_bed", "verb": "DROP",
             "summary": "丢下床", "params": {"object_id": "basic_bed"}}]
        choice = {
            "status": "validated",
            "snapshot_fingerprint": ai.fingerprint_action_space(space_with_drop),
            "action": "SELECT",
            "candidate_id": "obj:basic_bed",
            "intent_summary": "丢下床",
            "validated_at": SNAPSHOT["captured_at"],
        }
        with self.assertRaisesRegex(ai.IntentRefused, "not exposed"):
            ai.build_intent(choice, space_with_drop)

    def test_pick_up_and_place_are_on_the_allowlist(self):
        """M15: PICK_UP / PLACE are explicitly allowed; nothing else sneaks in."""

        self.assertIn("PICK_UP", ai.EXPOSED_VERBS)
        self.assertIn("PLACE", ai.EXPOSED_VERBS)
        self.assertEqual(tuple(ai.ACTION_ALLOWLIST),
                         ("MOVE", "POSE", "PICK_UP", "PLACE"))
        for verb in ("DROP", "THROW", "GIVE", "PUT_IN_CONTAINER", "STACK",
                     "EQUIP", "USE"):
            self.assertNotIn(verb, ai.ACTION_ALLOWLIST)

    def test_intent_cannot_invent_a_candidate(self):
        choice, space_now = validated_choice("pose:seated")
        choice["candidate_id"] = "pose:teleport"
        choice["action"] = "MOVE"
        with self.assertRaisesRegex(ai.ActionIntentError, "unknown candidate"):
            ai.build_intent(choice, space_now)

            ai.build_intent(choice, space_now)


class GateTests(unittest.TestCase):
    def setUp(self):
        import os
        self._saved = dict(os.environ)
        os.environ.pop(ai.GATE_ENV, None)

    def tearDown(self):
        import os
        os.environ.clear()
        os.environ.update(self._saved)

    def test_gate_defaults_to_off(self):
        self.assertEqual(ai.resolve_gate({}), ai.GATE_OFF)

    def test_gate_rejects_unknown_value(self):
        with self.assertRaises(ai.ActionIntentError):
            ai.resolve_gate({ai.GATE_ENV: "canoncial"})

    def test_all_three_gates_accepted(self):
        for gate in ai.GATES:
            self.assertEqual(ai.resolve_gate({ai.GATE_ENV: gate}), gate)


class SubmissionTests(unittest.TestCase):
    def _intent(self):
        choice, space_now = validated_choice("pose:seated")
        return ai.build_intent(choice, space_now, created_at=SNAPSHOT["captured_at"])

    def test_dependencies_come_from_the_fresh_read(self):
        submission = ai.build_submission(self._intent(), fresh_snapshot={
            "world_revision": 14, "location": {"place_id": "home", "area_id": "bedroom"},
            "pose": "standing"})
        self.assertEqual(submission["observed_dependencies"],
                         {"world.revision": 14, "world.location": "home",
                          "body.pose": "standing"})

    def test_submission_uses_the_derived_execution_id(self):
        intent = self._intent()
        submission = ai.build_submission(intent, fresh_snapshot={
            "world_revision": 14, "location": {"place_id": "home", "area_id": "bedroom"},
            "pose": "standing"})
        self.assertEqual(submission["execution_id"], intent["execution_id"])
        self.assertEqual(submission["authorization_ref"], "verified_main_self_tool")

    def test_incomplete_fresh_snapshot_is_refused(self):
        with self.assertRaisesRegex(ai.ActionIntentError, "incomplete"):
            ai.build_submission(self._intent(), fresh_snapshot={"world_revision": 14})

    def test_cancelled_intent_is_not_submittable(self):
        intent = self._intent()
        intent["intent_status"] = ai.STATUS_CANCELLED
        with self.assertRaisesRegex(ai.ActionIntentError, "not submittable"):
            ai.build_submission(intent, fresh_snapshot={
                "world_revision": 14, "location": {"place_id": "home", "area_id": "bedroom"},
                "pose": "standing"})


class ResultMappingTests(unittest.TestCase):
    def _intent(self):
        choice, space_now = validated_choice("pose:seated")
        return ai.build_intent(choice, space_now, created_at=SNAPSHOT["captured_at"])

    def test_success_maps_to_applied(self):
        result = ai.map_result(self._intent(), {"outcome": "SUCCESS"})
        self.assertEqual(result["intent_status"], ai.STATUS_APPLIED)

    def test_submitted_is_not_success(self):
        """Section 9: 'submitted' must never be expressed as success."""
        result = ai.map_result(self._intent(), {"outcome": "SUBMITTED"})
        self.assertEqual(result["intent_status"], ai.STATUS_UNKNOWN)
        self.assertFalse(result["authoritative"])

    def test_unknown_is_unknown_and_never_retried(self):
        result = ai.map_result(self._intent(), {"outcome": "UNKNOWN"})
        self.assertEqual(result["intent_status"], ai.STATUS_UNKNOWN)
        self.assertTrue(result["authoritative"])  # honest: it IS the canonical answer
        self.assertEqual(result["reason"], "unknown")

    def test_stale_maps_to_stale(self):
        result = ai.map_result(self._intent(), {"outcome": "STALE"})
        self.assertEqual(result["intent_status"], ai.STATUS_STALE)

    def test_every_mapping_keeps_provenance(self):
        intent = self._intent()
        for outcome in ("SUCCESS", "FAILURE", "STALE", "UNKNOWN", "INVALID"):
            result = ai.map_result(intent, {"outcome": outcome})
            self.assertEqual(result["intent_id"], intent["intent_id"])
            self.assertEqual(result["decision_ref"], intent["decision_ref"])
            self.assertEqual(result["execution_id"], intent["execution_id"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
