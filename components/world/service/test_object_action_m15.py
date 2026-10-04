#!/usr/bin/env python3
"""M15 battery — object manipulation (isolated).

§12 functional 16 · §13 sequence · §14 crash windows A/B/C.

Runs entirely in a temp world home.  Capability is declared explicitly (the
production path declares it in the economy catalog; the smoke/fixture path
declares it in the substrate registry), and nothing is assumed.
"""

from __future__ import annotations

import json
import os
import pathlib
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timezone

import conftest  # noqa: F401

import world_body_config as wbc
import world_body_service as wbsvc
import world_body_substrate as wbs
import world_action_resolver as war

RUNTIME_ROOT = pathlib.Path(__file__).resolve().parent.parent
NOW = datetime(2026, 9, 27, 12, 0, tzinfo=timezone.utc)

CUP = "m15-cup"
BED = "m15-bed"
TABLE_SURFACE = "table"


def seed_home(root: pathlib.Path) -> pathlib.Path:
    (root / "data" / "world").mkdir(parents=True, exist_ok=True)
    (root / "run").mkdir(parents=True, exist_ok=True)
    (root / "data" / "world" / "world_state.json").write_text(json.dumps({
        "facts": [], "location": {"area_id": "bedroom", "place_id": "home"},
        "revision": 5, "scene": {"revision": 5, "scene_id": "home.bedroom"},
        "schema_version": "world.foundation.v0", "timezone": "Asia/Tokyo",
        "world_id": "shiomi_city"}, sort_keys=True), encoding="utf-8")
    (root / "data" / "world" / "world_timeline.json").write_text(json.dumps(
        {"schema_version": "world.timeline.v1", "revision": 1,
         "world_id": "shiomi_city", "processes": []}, sort_keys=True), encoding="utf-8")
    (root / "data" / "life_execution.jsonl").write_text("", encoding="utf-8")
    return root


def declare_fixtures():
    wbs.declare_capability(CUP, {"takeable": True, "portable": True,
                                 "placeable": True, "fixed": False})
    wbs.declare_capability(BED, {"takeable": False, "portable": False,
                                 "placeable": True, "fixed": True})
    wbs.SURFACES.setdefault(TABLE_SURFACE, {"surface_id": TABLE_SURFACE,
                                            "owner_catalog_item_id": ""})


def clear_fixtures():
    for name in (CUP, BED):
        wbs.DECLARED_CAPABILITIES.pop(name, None)
    wbs.SURFACES.pop(TABLE_SURFACE, None)


class M15Base(unittest.TestCase):
    placements: dict = {}

    def setUp(self):
        self._saved_env = dict(os.environ)
        declare_fixtures()
        self.addCleanup(clear_fixtures)
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="m15-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        seed_home(self.tmp)
        self._write(self.placements)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._saved_env)

    def _write(self, placements):
        state = wbs.initial_substrate(world_id="shiomi_city", body_id="chiyo_body")
        state["pose"] = "standing"
        state["orientation"] = "south"
        for object_id, target in placements.items():
            state = wbs.place_object(state, object_id, target)
        wbs.SubstrateStore(wbs.substrate_path(self.tmp)).save(state)

    def start(self):
        service = wbsvc.WorldBodyService(self._config(), now=NOW, instance_id="m15")
        self.addCleanup(service.shutdown)
        service.startup()
        return service

    def _config(self):
        return wbc.load_config({"CHIYO_WORLD_LIVING_SKELETON_ENABLED": "1",
                                wbs.FEATURE_ENV: "1"},
                               runtime_root=RUNTIME_ROOT, world_home=self.tmp,
                               runtime_mode="canonical", authority="production",
                               world_id="shiomi_city")

    def deps(self, service):
        return service.resolver._dependencies()

    def propose(self, service, verb, params, *, deps=None, execution_id=None):
        return service.resolver.resolve({
            "action": verb,
            "execution_id": execution_id or ("world-exec:m15-%s-%d" % (verb.lower(), id(self) % 10 ** 6)),
            "actor_id": "chiyo", "body_id": "chiyo_body",
            "authority": war.AUTHORITY_VERIFIED_MAIN_SELF, "confirm_apply": True,
            "observed_dependencies": deps if deps is not None else self.deps(service),
            "params": params,
        })

    def placement(self, object_id):
        return wbs.placement_of(wbs.SubstrateStore(wbs.substrate_path(self.tmp)).load(), object_id)

    def substrate_bytes(self):
        return wbs.substrate_path(self.tmp).read_bytes()

    def ledger_entries(self):
        return [json.loads(l) for l in (self.tmp / "data" / "life_execution.jsonl").read_text().splitlines() if l.strip()]


# ---------------------------------------------------------------- §12 PICK_UP
class PickUpBattery(M15Base):
    placements = {CUP: {"kind": "room", "location_id": "home", "area_id": "bedroom"}}

    def test_01_reachable_portable_object_succeeds(self):
        s = self.start()
        r = self.propose(s, "PICK_UP", {"object_id": CUP, "slot_id": "right_hand"})
        self.assertEqual(r["status"], "applied", r)
        self.assertEqual(self.placement(CUP), {"kind": "hand", "slot_id": "right_hand"})

    def test_02_unreachable_object_is_rejected(self):
        self._write({CUP: {"kind": "room", "location_id": "school", "area_id": "classroom"}})
        s = self.start()
        before = self.substrate_bytes()
        r = self.propose(s, "PICK_UP", {"object_id": CUP, "slot_id": "right_hand"})
        self.assertEqual(r["status"], "rejected")
        self.assertIn("NOT_REACHABLE", r["legacy_reason_code"])
        self.assertEqual(self.substrate_bytes(), before)

    def test_03_non_portable_object_is_rejected(self):
        self._write({BED: {"kind": "room", "location_id": "home", "area_id": "bedroom"}})
        s = self.start()
        before = self.substrate_bytes()
        r = self.propose(s, "PICK_UP", {"object_id": BED, "slot_id": "right_hand"})
        self.assertEqual(r["status"], "rejected")
        self.assertIn("NOT_PORTABLE", r["legacy_reason_code"])
        self.assertEqual(self.substrate_bytes(), before)

    def test_04_hand_occupied_is_rejected(self):
        self._write({CUP: {"kind": "room", "location_id": "home", "area_id": "bedroom"},
                     BED: {"kind": "hand", "slot_id": "right_hand"}})
        s = self.start()
        before = self.substrate_bytes()
        r = self.propose(s, "PICK_UP", {"object_id": CUP, "slot_id": "right_hand"})
        self.assertEqual(r["status"], "rejected")
        self.assertIn("OCCUPIED", r["legacy_reason_code"])
        self.assertEqual(self.substrate_bytes(), before)

    def test_05_stale_revision_is_rejected(self):
        s = self.start()
        stale = dict(self.deps(s)); stale["world.revision"] = 999
        before = self.substrate_bytes()
        r = self.propose(s, "PICK_UP", {"object_id": CUP, "slot_id": "right_hand"}, deps=stale)
        self.assertEqual(r["status"], "rejected")
        self.assertEqual(r["reason_code"], war.CODE_STALE_STATE)
        self.assertEqual(self.substrate_bytes(), before)

    def test_06_duplicate_execution_does_not_mutate_twice(self):
        s = self.start()
        first = self.propose(s, "PICK_UP", {"object_id": CUP, "slot_id": "right_hand"},
                             execution_id="world-exec:m15-dup")
        self.assertEqual(first["status"], "applied")
        after = self.substrate_bytes()
        second = self.propose(s, "PICK_UP", {"object_id": CUP, "slot_id": "right_hand"},
                              execution_id="world-exec:m15-dup")
        self.assertIn(second["status"], ("already_applied", "rejected"))
        self.assertEqual(self.substrate_bytes(), after)
        committed = [e for e in self.ledger_entries()
                     if e.get("execution_id") == "world-exec:m15-dup" and e.get("status") == "committed"]
        self.assertEqual(len(committed), 1)

    def test_07_object_already_held_is_rejected(self):
        self._write({CUP: {"kind": "hand", "slot_id": "left_hand"}})
        s = self.start()
        before = self.substrate_bytes()
        r = self.propose(s, "PICK_UP", {"object_id": CUP, "slot_id": "right_hand"})
        self.assertEqual(r["status"], "rejected")
        self.assertEqual(self.substrate_bytes(), before)

    def test_08_forged_capability_is_rejected_by_the_firewall(self):
        s = self.start()
        before = self.substrate_bytes()
        resp = s.gateway_surface.submit_action({
            "action": "PICK_UP", "execution_id": "world-exec:m15-forged",
            "params": {"object_id": CUP, "slot_id": "right_hand"},
            "authorization_id": "wauth:forged"})
        self.assertEqual(resp["status"], "rejected")
        self.assertIn(resp["reason_code"], ("AUTHORIZATION_NOT_FOUND", "AUTHORIZATION_REQUIRED"))
        self.assertEqual(self.substrate_bytes(), before)


# ---------------------------------------------------------------- §12 PLACE
class PlaceBattery(M15Base):
    placements = {CUP: {"kind": "hand", "slot_id": "right_hand"}}

    def test_09_held_object_to_valid_surface_succeeds(self):
        s = self.start()
        r = self.propose(s, "PLACE", {"object_id": CUP, "target": {
            "kind": "surface", "location_id": "home", "surface_id": TABLE_SURFACE}})
        self.assertEqual(r["status"], "applied", r)
        self.assertEqual(self.placement(CUP)["kind"], "surface")

    def test_10_object_not_held_is_rejected(self):
        self._write({CUP: {"kind": "room", "location_id": "home", "area_id": "bedroom"}})
        s = self.start()
        before = self.substrate_bytes()
        r = self.propose(s, "PLACE", {"object_id": CUP, "target": {
            "kind": "surface", "location_id": "home", "surface_id": TABLE_SURFACE}})
        self.assertEqual(r["status"], "rejected")
        self.assertEqual(self.substrate_bytes(), before)

    def test_11_undeclared_destination_is_rejected(self):
        s = self.start()
        before = self.substrate_bytes()
        r = self.propose(s, "PLACE", {"object_id": CUP, "target": {
            "kind": "surface", "location_id": "home", "surface_id": "void_surface"}})
        self.assertEqual(r["status"], "rejected")
        self.assertIn("UNKNOWN_DESTINATION", r["legacy_reason_code"])
        self.assertEqual(self.substrate_bytes(), before)

    def test_12_unreachable_destination_is_rejected(self):
        s = self.start()
        before = self.substrate_bytes()
        r = self.propose(s, "PLACE", {"object_id": CUP, "target": {
            "kind": "room", "location_id": "school", "area_id": "school"}})
        self.assertEqual(r["status"], "rejected")
        self.assertIn("NOT_REACHABLE", r["legacy_reason_code"])
        self.assertEqual(self.substrate_bytes(), before)

    def test_13_occupied_slot_target_is_rejected(self):
        self._write({CUP: {"kind": "hand", "slot_id": "right_hand"},
                     BED: {"kind": "hand", "slot_id": "left_hand"}})
        s = self.start()
        before = self.substrate_bytes()
        r = self.propose(s, "PLACE", {"object_id": CUP, "target": {
            "kind": "hand", "slot_id": "left_hand"}})
        self.assertEqual(r["status"], "rejected")
        self.assertEqual(self.substrate_bytes(), before)

    def test_14_container_destination_is_not_enabled_in_v1(self):
        s = self.start()
        before = self.substrate_bytes()
        r = self.propose(s, "PLACE", {"object_id": CUP, "target": {
            "kind": "container", "location_id": "home", "container_id": "box"}})
        self.assertEqual(r["status"], "rejected")
        self.assertIn("DESTINATION_NOT_ALLOWED", r["legacy_reason_code"])
        self.assertEqual(self.substrate_bytes(), before)

    def test_15_replay_creates_no_duplicate_placement(self):
        s = self.start()
        first = self.propose(s, "PLACE", {"object_id": CUP, "target": {
            "kind": "surface", "location_id": "home", "surface_id": TABLE_SURFACE}},
            execution_id="world-exec:m15-replay-place")
        self.assertEqual(first["status"], "applied")
        after = self.substrate_bytes()
        self.propose(s, "PLACE", {"object_id": CUP, "target": {
            "kind": "surface", "location_id": "home", "surface_id": TABLE_SURFACE}},
            execution_id="world-exec:m15-replay-place")
        self.assertEqual(self.substrate_bytes(), after)

    def test_16_forged_capability_is_rejected_by_the_firewall(self):
        s = self.start()
        before = self.substrate_bytes()
        resp = s.gateway_surface.submit_action({
            "action": "PLACE", "execution_id": "world-exec:m15-forged-place",
            "params": {"object_id": CUP, "target": {
                "kind": "room", "location_id": "home", "area_id": "bedroom"}},
            "authorization_id": "wauth:forged"})
        self.assertEqual(resp["status"], "rejected")
        self.assertEqual(self.substrate_bytes(), before)


# ---------------------------------------------------------------- §13 sequence
class SequenceBattery(M15Base):
    placements = {CUP: {"kind": "room", "location_id": "home", "area_id": "bedroom"}}

    def test_sequence_pickup_restart_place_restart(self):
        s1 = self.start()
        self.assertEqual(self.propose(s1, "PICK_UP", {"object_id": CUP, "slot_id": "right_hand"})["status"], "applied")
        self.assertEqual(self.placement(CUP), {"kind": "hand", "slot_id": "right_hand"})
        s1.shutdown()

        s2 = self.start()   # restart: the held state must survive
        self.assertEqual(self.placement(CUP), {"kind": "hand", "slot_id": "right_hand"})
        sub = wbs.SubstrateStore(wbs.substrate_path(self.tmp)).load()
        self.assertEqual(wbs.slot_occupant(sub, "right_hand"), CUP)
        self.assertEqual(wbs.free_slots(sub), ["left_hand"])
        r = self.propose(s2, "PLACE", {"object_id": CUP, "target": {
            "kind": "surface", "location_id": "home", "surface_id": TABLE_SURFACE}})
        self.assertEqual(r["status"], "applied")
        sub = wbs.SubstrateStore(wbs.substrate_path(self.tmp)).load()
        self.assertEqual(wbs.free_slots(sub), ["left_hand", "right_hand"], "hand empty after PLACE")
        s2.shutdown()

        self.start()        # restart again: the surface placement must survive
        self.assertEqual(self.placement(CUP)["kind"], "surface")


# ---------------------------------------------------------------- §14 crash windows
class CrashWindows(M15Base):
    placements = {CUP: {"kind": "room", "location_id": "home", "area_id": "bedroom"}}

    def test_window_a_crash_before_resolver_leaves_world_unchanged(self):
        s = self.start()
        before = self.substrate_bytes()
        s.gateway_surface.authorizations.issue(
            decision_id="decision:m15-a", intent_id="world-intent:m15-a",
            intent_fingerprint="fp", execution_id="world-exec:m15-a",
            actor="chiyo", verb="PICK_UP",
            params={"object_id": CUP, "slot_id": "right_hand"}, authority_generation=5)
        self.assertEqual(self.substrate_bytes(), before, "an issued capability alone must not mutate")

    def test_window_b_crash_after_apply_reconciles_without_second_pickup(self):
        s = self.start()
        with self.assertRaises(war.ResolverCrash):
            s.resolver.resolve({
                "action": "PICK_UP", "execution_id": "world-exec:m15-b2",
                "actor_id": "chiyo", "body_id": "chiyo_body",
                "authority": war.AUTHORITY_VERIFIED_MAIN_SELF, "confirm_apply": True,
                "observed_dependencies": self.deps(s),
                "params": {"object_id": CUP, "slot_id": "right_hand"}},
                crash_after=war.CRASH_AFTER_MUTATION)
        held = self.placement(CUP)
        self.assertEqual(held["kind"], "hand", "crash hook must reach the mutation")
        s.shutdown()   # release the single-writer lease before restarting
        s2 = wbsvc.WorldBodyService(self._config(), now=NOW, instance_id="m15-b2")
        self.addCleanup(s2.shutdown)
        report = s2.startup()
        self.assertEqual(self.placement(CUP), {"kind": "hand", "slot_id": "right_hand"},
                         "no second PICK_UP")
        recon = report.get("reconciliation") or s2.startup_reconciliation or {}
        unresolved = recon.get("unresolved", [])
        self.assertEqual(len(unresolved), 1)
        self.assertEqual(unresolved[0]["resolver_verdict"], "UNKNOWN")
        self.assertTrue(unresolved[0]["reconciliation_required"])
        before = self.substrate_bytes()
        replay = self.propose(s2, "PICK_UP", {"object_id": CUP, "slot_id": "right_hand"},
                              execution_id="world-exec:m15-b2")
        self.assertEqual(replay["status"], "uncertain")
        self.assertEqual(self.substrate_bytes(), before, "uncertain execution must not apply twice")

    def test_window_c_replay_after_commit_is_already_committed(self):
        s = self.start()
        first = self.propose(s, "PICK_UP", {"object_id": CUP, "slot_id": "right_hand"},
                             execution_id="world-exec:m15-c")
        self.assertEqual(first["status"], "applied")
        after = self.substrate_bytes()
        replay = self.propose(s, "PICK_UP", {"object_id": CUP, "slot_id": "right_hand"},
                              execution_id="world-exec:m15-c")
        self.assertIn(replay["status"], ("already_applied", "applied"))
        self.assertEqual(self.substrate_bytes(), after)


if __name__ == "__main__":
    unittest.main()
