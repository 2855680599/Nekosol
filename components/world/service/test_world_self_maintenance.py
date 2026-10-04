#!/usr/bin/env python3
"""Self-maintenance policy tests (M15C)."""

from __future__ import annotations

import pathlib
import sys
import tempfile
import unittest

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "bridge"))

import world_self_maintenance as SM  # noqa: E402

T0 = 1_800_000_000.0


def _policy(**kw):
    return SM.SelfMaintenancePolicy(
        pathlib.Path(tempfile.mkdtemp(prefix="sm-")) / "state.json", **kw)


def _signals(state="interrupted", trend="worsening", *, noticeable=False, high=False):
    return {"recovery": {"state": state, "mode": "rest", "trend": trend},
            "latch": {"fatigue_noticeable": noticeable, "fatigue_high": high}}


class PolicyTests(unittest.TestCase):
    def test_event_states_are_explicit(self):
        self.assertEqual(len(SM.EVENTS), 5)
        self.assertIn(SM.RECOVERY_WORSENING, SM.EVENTS)
        self.assertIn(SM.RECOVERY_STABILIZED, SM.EVENTS)

    def test_normal_state_produces_no_action(self):
        p = _policy()
        v = p.observe(_signals(state="active"), now=T0)
        self.assertEqual(v["event"], SM.RECOVERY_NORMAL)
        self.assertFalse(v["action_allowed"])

    def test_worsening_needs_minimum_dwell(self):
        p = _policy(min_dwell=300.0)
        p.observe(_signals(), now=T0)                     # episode starts
        v = p.observe(_signals(), now=T0 + 100)
        self.assertEqual(v["event"], SM.RECOVERY_WORSENING)
        self.assertFalse(v["action_allowed"])             # below dwell
        self.assertFalse(p.observe(_signals(), now=T0)["action_allowed"])

    def test_action_becomes_allowed_after_dwell(self):
        p = _policy(min_dwell=300.0)
        p.observe(_signals(), now=T0)
        v = p.observe(_signals(), now=T0 + 301)
        self.assertTrue(v["action_allowed"])
        self.assertEqual(v["severity"], SM.SEVERITY_DWELL)

    def test_fatigue_bands_raise_severity_immediately(self):
        p = _policy()
        p.observe(_signals(), now=T0)
        v = p.observe(_signals(noticeable=True), now=T0 + 1)
        self.assertEqual(v["severity"], SM.SEVERITY_NOTICEABLE)
        self.assertTrue(v["action_allowed"])
        v2 = p.observe(_signals(high=True), now=T0 + 2)
        self.assertEqual(v2["severity"], SM.SEVERITY_HIGH)

    def test_stabilized_state_is_not_worsening(self):
        p = _policy()
        p.observe(_signals(), now=T0)
        v = p.observe(_signals(trend="stalled"), now=T0 + 400)
        self.assertEqual(v["event"], SM.RECOVERY_STABILIZED)

    def test_action_cooldown_blocks_a_second_action(self):
        p = _policy(min_dwell=0.0, action_cooldown=600.0, decision_cooldown=0.0,
                    dedupe=0.0)
        first = p.observe(_signals(), now=T0)
        self.assertTrue(first["action_allowed"])
        p.note_action(now=T0)                     # the action committed
        second = p.observe(_signals(), now=T0 + 10)
        self.assertFalse(second["action_allowed"])
        self.assertIn("action cooldown", second["reason"])

    def test_decision_cooldown_is_separate_from_action_cooldown(self):
        p = _policy(min_dwell=0.0, action_cooldown=0.0, decision_cooldown=120.0,
                    dedupe=0.0)
        self.assertTrue(p.observe(_signals(), now=T0)["action_allowed"])
        second = p.observe(_signals(), now=T0 + 5)
        self.assertFalse(second["action_allowed"])
        self.assertIn("decision cooldown", second["reason"])

    def test_event_dedupe_is_separate_from_cooldowns(self):
        p = _policy(min_dwell=0.0, action_cooldown=0.0, decision_cooldown=0.0,
                    dedupe=300.0)
        self.assertTrue(p.observe(_signals(), now=T0)["action_allowed"])
        second = p.observe(_signals(), now=T0 + 2)
        self.assertFalse(second["action_allowed"])
        self.assertIn("event dedupe", second["reason"])

    def test_no_oscillation_across_many_turns(self):
        """The policy cannot produce standing/lying chatter."""

        p = _policy(min_dwell=300.0, action_cooldown=600.0)
        allowed = 0
        for i in range(60):                     # 60 turns, 10s apart
            v = p.observe(_signals(), now=T0 + i * 10)
            if v["action_allowed"]:
                allowed += 1
                p.note_action(now=T0 + i * 10)
        self.assertLessEqual(allowed, 1)

    def test_completed_recovery_is_terminal_for_the_episode(self):
        p = _policy()
        p.observe(_signals(), now=T0)
        v = p.observe(_signals(state="completed", trend="improving"), now=T0 + 500)
        self.assertEqual(v["event"], SM.RECOVERY_COMPLETE)
        self.assertFalse(v["action_allowed"])

    def test_state_file_is_written_atomically(self):
        p = _policy()
        p.observe(_signals(), now=T0)
        self.assertTrue(p.path.exists())
        self.assertFalse(p.path.with_suffix(".tmp").exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
