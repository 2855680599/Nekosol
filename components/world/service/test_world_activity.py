#!/usr/bin/env python3
"""Natural Activity V0 tests (M15D)."""

from __future__ import annotations

import pathlib
import sys
import tempfile
import unittest

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "bridge"))

import world_activity as WA  # noqa: E402

T0 = 1_800_000_000.0


def _store(**kw):
    return WA.ActivityStore(pathlib.Path(tempfile.mkdtemp(prefix="act-"))
                            / "state.json", **kw)


def _module_identifiers():
    """Identifiers actually used in code (docstrings/comments excluded)."""

    import ast as _ast
    tree = _ast.parse((HERE / "bridge" / "world_activity.py").read_text())
    names = set()
    for node in _ast.walk(tree):
        if isinstance(node, _ast.Name):
            names.add(node.id)
        elif isinstance(node, _ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, (_ast.Import, _ast.ImportFrom)):
            for alias in node.names:
                names.add(alias.name.split(".")[0])
                if alias.asname:
                    names.add(alias.asname)
    return names


class ActivityTests(unittest.TestCase):
    def test_allow_list_and_forbidden_types(self):
        self.assertEqual(len(WA.ALLOWED_TYPES), 6)
        for forbidden in WA.FORBIDDEN_TYPES:
            self.assertNotIn(forbidden, WA.ALLOWED_TYPES)
        s = _store()
        with self.assertRaises(WA.ActivityError):
            s.plan(activity_id="a1", activity_type="GO_OUTSIDE", now=T0)

    def test_no_random_in_the_module(self):
        names = _module_identifiers()
        self.assertNotIn("random", names)
        self.assertNotIn("choice", names)
        self.assertNotIn("randint", names)

    def test_lifecycle_transitions(self):
        s = _store()
        s.plan(activity_id="a1", activity_type=WA.REST, now=T0,
               expected_duration=600)
        self.assertEqual(s.load()["activity"]["status"], WA.PLANNED)
        s.act(now=T0 + 1)
        self.assertEqual(s.load()["activity"]["status"], WA.ACTIVE)
        s.interrupt(reason="user spoke", now=T0 + 2)
        self.assertEqual(s.load()["activity"]["status"], WA.INTERRUPTED)
        s.complete(now=T0 + 3)
        self.assertEqual(s.load()["activity"]["status"], WA.COMPLETED)
        with self.assertRaises(WA.ActivityError):
            s.complete(now=T0 + 4)

    def test_illegal_transition_is_refused(self):
        s = _store()
        s.plan(activity_id="a1", activity_type=WA.REST, now=T0)
        with self.assertRaises(WA.ActivityError):
            s.complete(now=T0 + 1)      # PLANNED -> COMPLETED is not allowed

    def test_second_activity_cannot_start_while_active(self):
        s = _store()
        s.plan(activity_id="a1", activity_type=WA.REST, now=T0)
        s.act(now=T0 + 1)
        with self.assertRaises(WA.ActivityError):
            s.plan(activity_id="a2", activity_type=WA.STAND, now=T0 + 2)

    def test_tick_reports_activity_expiry(self):
        s = _store()
        s.plan(activity_id="a1", activity_type=WA.REST, now=T0,
               expected_duration=600)
        s.act(now=T0)
        v = s.tick(now=T0 + 10)
        self.assertEqual(v["event"], WA.NO_EVENT)
        v = s.tick(now=T0 + 601)
        self.assertEqual(v["event"], WA.ACTIVITY_EXPIRED)
        self.assertEqual(v["candidate_hint"], WA.STAND)
        self.assertEqual(v["evidence"]["activity_id"], "a1")

    def test_tick_reports_idle_too_long(self):
        s = _store(idle_too_long=1000.0)
        s.tick(now=T0)                       # establishes the baseline
        v = s.tick(now=T0 + 1001)
        self.assertEqual(v["event"], WA.IDLE_TOO_LONG)
        self.assertIsNone(v["candidate_hint"])

    def test_tick_reports_recovery_check(self):
        s = _store(idle_too_long=10_000_000.0)
        s.tick(now=T0)
        v = s.tick(now=T0 + 1, recovery={"state": "interrupted"})
        self.assertEqual(v["event"], WA.RECOVERY_CHECK)

    def test_tick_never_mutates_anything_but_its_own_state(self):
        s = _store()
        before = s.load()
        s.tick(now=T0)
        after = s.load()
        self.assertEqual(set(after) - set(before), {"last_tick_at"})

    def test_every_event_carries_trigger_and_evidence(self):
        s = _store()
        s.plan(activity_id="a1", activity_type=WA.REST, now=T0,
               expected_duration=1)
        s.act(now=T0)
        v = s.tick(now=T0 + 5)
        for key in ("event", "trigger", "evidence"):
            self.assertIn(key, v)
        self.assertTrue(v["evidence"])

    def test_activity_is_not_a_pose(self):
        """No World/Body vocabulary may appear in the activity code."""

        names = _module_identifiers()
        for token in ("submit_action", "Resolver", "WorldResolver", "ledger",
                      "world_state", "pose_after"):
            self.assertNotIn(token, names)


if __name__ == "__main__":
    unittest.main(verbosity=2)
