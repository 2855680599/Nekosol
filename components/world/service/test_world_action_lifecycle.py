#!/usr/bin/env python3
"""Action Lifecycle tests (M15B)."""

from __future__ import annotations

import json
import pathlib
import sys
import tempfile
import unittest

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "bridge"))

import world_action_lifecycle as LC  # noqa: E402


def _lc():
    return LC.ActionLifecycle(pathlib.Path(tempfile.mkdtemp(prefix="lc-"))
                              / "action_lifecycle.jsonl")


def _begin(lc, eid="world-exec:test1"):
    return lc.begin(execution_id=eid, decision_id="decision:d1",
                    intent_id="world-intent:i1", verb="POSE")


class LifecycleContractTests(unittest.TestCase):
    def test_states_and_terminal_set_are_explicit(self):
        self.assertEqual(len(LC.STATES), 10)
        self.assertEqual(set(LC.TERMINAL),
                         {"APPLIED", "REJECTED", "STALE", "ABORTED", "CANCELLED"})
        self.assertNotIn(LC.UNKNOWN, LC.TERMINAL)   # UNKNOWN is never terminal

    def test_happy_path_reaches_applied(self):
        lc = _lc()
        _begin(lc)
        for state in (LC.AUTHORIZED, LC.SUBMITTING, LC.SUBMITTED, LC.APPLIED):
            lc.transition(execution_id="world-exec:test1", state=state)
        entry = lc.get("world-exec:test1")
        self.assertEqual(entry["state"], LC.APPLIED)
        self.assertTrue(lc.is_terminal("world-exec:test1"))
        self.assertEqual(entry["history"],
                         [LC.CREATED, LC.AUTHORIZED, LC.SUBMITTING,
                          LC.SUBMITTED, LC.APPLIED])

    def test_illegal_transition_is_refused(self):
        lc = _lc()
        _begin(lc)
        with self.assertRaises(LC.LifecycleError):
            lc.transition(execution_id="world-exec:test1", state=LC.APPLIED)

    def test_terminal_state_is_immutable(self):
        lc = _lc()
        _begin(lc)
        for state in (LC.AUTHORIZED, LC.SUBMITTING, LC.SUBMITTED, LC.REJECTED):
            lc.transition(execution_id="world-exec:test1", state=state)
        with self.assertRaises(LC.LifecycleError):
            lc.transition(execution_id="world-exec:test1", state=LC.APPLIED)

    def test_unknown_can_be_reconciled_into_a_terminal_state(self):
        lc = _lc()
        _begin(lc)
        for state in (LC.AUTHORIZED, LC.SUBMITTING, LC.SUBMITTED, LC.UNKNOWN):
            lc.transition(execution_id="world-exec:test1", state=state)
        self.assertFalse(lc.is_terminal("world-exec:test1"))
        lc.transition(execution_id="world-exec:test1", state=LC.APPLIED)
        self.assertTrue(lc.is_terminal("world-exec:test1"))

    def test_begin_is_idempotent_for_a_live_record(self):
        lc = _lc()
        first = _begin(lc)
        second = lc.begin(execution_id="world-exec:test1", decision_id="decision:d1",
                          intent_id="world-intent:i1", verb="POSE")
        self.assertEqual(first["execution_id"], second["execution_id"])
        self.assertEqual(len(lc.states()), 1)

    def test_begin_refuses_a_terminal_record(self):
        lc = _lc()
        _begin(lc)
        lc.transition(execution_id="world-exec:test1", state=LC.ABORTED)
        with self.assertRaises(LC.LifecycleError):
            _begin(lc)

    def test_open_records_lists_only_non_terminal(self):
        lc = _lc()
        _begin(lc, eid="world-exec:a")
        _begin(lc, eid="world-exec:b")
        lc.transition(execution_id="world-exec:b", state=LC.ABORTED)
        open_ids = {r["execution_id"] for r in lc.open_records()}
        self.assertEqual(open_ids, {"world-exec:a"})

    def test_record_is_append_only_jsonl(self):
        lc = _lc()
        _begin(lc)
        lc.transition(execution_id="world-exec:test1", state=LC.ABORTED)
        lines = [l for l in lc.path.read_text().splitlines() if l.strip()]
        self.assertEqual(len(lines), 2)
        self.assertEqual(json.loads(lines[0])["kind"], LC.KIND_TRANSITION)

    def test_reconcile_maps_committed_ledger_to_applied(self):
        lc = _lc()
        _begin(lc, eid="world-exec:committed")
        out = lc.reconcile(ledger_index={"world-exec:committed": "committed"})
        self.assertEqual(out["applied"], ["world-exec:committed"])
        self.assertEqual(lc.get("world-exec:committed")["state"], LC.APPLIED)

    def test_reconcile_aborts_a_record_with_no_ledger_evidence(self):
        lc = _lc()
        _begin(lc, eid="world-exec:never")
        out = lc.reconcile(ledger_index={})
        self.assertEqual(out["aborted"], ["world-exec:never"])
        entry = lc.get("world-exec:never")
        self.assertEqual(entry["state"], LC.ABORTED)
        self.assertEqual(entry["record"]["failure_class"],
                         "ABORTED_INTEGRATION_ERROR")

    def test_ledger_index_reads_the_canonical_ledger(self):
        p = pathlib.Path(tempfile.mkdtemp(prefix="led-")) / "life_execution.jsonl"
        p.write_text("\n".join([
            json.dumps({"kind": "world_execution_ledger_entry",
                        "execution_id": "world-exec:x", "status": "prepared"}),
            json.dumps({"kind": "world_execution_ledger_entry",
                        "execution_id": "world-exec:x", "status": "committed"}),
            json.dumps({"kind": "world_execution_ledger_entry",
                        "execution_id": "world-exec:y", "status": "prepared"}),
        ]) + "\n")
        index = LC.ledger_index(p)
        self.assertEqual(index["world-exec:x"], "committed")
        self.assertEqual(index["world-exec:y"], "prepared")


if __name__ == "__main__":
    unittest.main(verbosity=2)
