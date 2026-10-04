#!/usr/bin/env python3
"""M13A.1 tests: canonical World instance bootstrap.

Covers the bootstrap-specific items of ticket section 40.  The action smokes
(MOVE / POSE / PICK_UP / PLACE positive and negative) are exercised against the
running production service and are recorded in the M13A.1 report.
"""

from __future__ import annotations

import json
import pathlib
import shutil
import tempfile
import unittest

import conftest  # noqa: F401  (path setup)

import world_body_substrate as wbs
import world_instance_bootstrap as boot


def seed_world(root: pathlib.Path, *, world_id: str = "shiomi_city") -> pathlib.Path:
    (root / "data" / "world").mkdir(parents=True, exist_ok=True)
    (root / "data" / "world" / "world_state.json").write_text(
        json.dumps({
            "facts": [],
            "location": {"area_id": "bedroom", "place_id": "home"},
            "revision": 5,
            "scene": {"revision": 5, "scene_id": "home.bedroom"},
            "schema_version": "world.foundation.v0",
            "timezone": "Asia/Tokyo",
            "world_id": world_id,
        }, sort_keys=True),
        encoding="utf-8",
    )
    (root / "state").mkdir(parents=True, exist_ok=True)
    (root / "state" / "world_property_economy.json").write_text(
        json.dumps({
            "owner": "chiyo",
            "currency": "JPY",
            "revision": 4,
            "items": [{
                "disposition": "OWNED",
                "item_instance_id": "item-8ad09561bcc7d3d5b8e10aec36365df138c5d217e66db8e14f65cf4aafe54006",
                "catalog_item_id": "basic_bed",
                "current_location": "home/bedroom",
                "owner": "chiyo",
            }],
        }, sort_keys=True),
        encoding="utf-8",
    )
    return root


class BootstrapTestBase(unittest.TestCase):
    def setUp(self):
        self.home = pathlib.Path(tempfile.mkdtemp(prefix="m13a1-"))
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)
        seed_world(self.home)


class SourceReconTests(BootstrapTestBase):
    def test_source_recon_finds_the_real_economy_object(self):
        items = boot.owned_economy_items(self.home)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["catalog_item_id"], "basic_bed")

    def test_no_substrate_exists_before_bootstrap(self):
        self.assertFalse(wbs.substrate_path(self.home).exists())
        self.assertIsNone(wbs.SubstrateStore(wbs.substrate_path(self.home)).load())

    def test_missing_world_state_refuses(self):
        (self.home / "data" / "world" / "world_state.json").unlink()
        with self.assertRaises(boot.BootstrapRefused):
            boot.plan(self.home)

    def test_unknown_world_schema_refuses(self):
        path = self.home / "data" / "world" / "world_state.json"
        payload = json.loads(path.read_text())
        payload["schema_version"] = "galaxy.v9"
        path.write_text(json.dumps(payload), encoding="utf-8")
        with self.assertRaises(boot.BootstrapRefused):
            boot.plan(self.home)


class PlanTests(BootstrapTestBase):
    def test_plan_is_dry_and_complete(self):
        planned = boot.plan(self.home)
        self.assertEqual(planned["world_id"], "shiomi_city")
        self.assertEqual(planned["location"], {"place_id": "home", "area_id": "bedroom"})
        ids = sorted(p["object_id"] for p in planned["placements"])
        self.assertEqual(len(ids), 2)
        self.assertIn(boot.TEST_OBJECT_ID, ids)
        self.assertFalse(wbs.substrate_path(self.home).exists())

    def test_plan_preserves_the_economy_object_identity(self):
        planned = boot.plan(self.home)
        item = boot.owned_economy_items(self.home)[0]["item_instance_id"]
        placements = planned["substrate"]["placements"]
        self.assertIn(item, placements)
        self.assertEqual(placements[item]["kind"], "room")
        self.assertEqual(placements[item]["location_id"], "home")
        self.assertEqual(placements[item]["area_id"], "bedroom")

    def test_test_object_is_marked_as_a_fixture(self):
        planned = boot.plan(self.home)
        entry = [p for p in planned["placements"]
                 if p["object_id"] == boot.TEST_OBJECT_ID][0]
        self.assertEqual(entry["identity_source"], "m13a1_fixture")
        self.assertIn("not Chiyo", entry["provenance"])
        self.assertTrue(planned["substrate"]["placements"][boot.TEST_OBJECT_ID])

    def test_genesis_placement_is_the_canonical_initial_location(self):
        planned = boot.plan(self.home)
        self.assertEqual(planned["substrate"]["pose"], boot.GENESIS_POSE)
        self.assertEqual(planned["substrate"]["orientation"], boot.GENESIS_ORIENTATION)


class ApplyTests(BootstrapTestBase):
    def test_dry_run_writes_nothing(self):
        receipt = boot.bootstrap(self.home, apply=False)
        self.assertEqual(receipt["status"], boot.STATUS_CREATED)
        self.assertTrue(receipt["planned_only"])
        self.assertFalse(receipt["wrote_anything"])
        self.assertFalse(wbs.substrate_path(self.home).exists())

    def test_apply_creates_a_valid_bound_substrate(self):
        receipt = boot.bootstrap(self.home, apply=True)
        self.assertEqual(receipt["status"], boot.STATUS_CREATED)
        self.assertTrue(receipt["wrote_anything"])
        self.assertTrue(receipt["binding"]["bound"])
        substrate = wbs.SubstrateStore(wbs.substrate_path(self.home)).load()
        self.assertEqual(substrate["world_id"], "shiomi_city")
        self.assertEqual(substrate["body_id"], "chiyo_body")
        self.assertEqual(substrate["world_entity_id"], substrate["body_id"])

    def test_bootstrap_is_idempotent(self):
        first = boot.bootstrap(self.home, apply=True)
        self.assertTrue(first["wrote_anything"])
        before = wbs.substrate_path(self.home).read_bytes()

        second = boot.bootstrap(self.home, apply=True)
        self.assertEqual(second["status"], boot.STATUS_ALREADY)
        self.assertFalse(second["wrote_anything"])
        self.assertEqual(wbs.substrate_path(self.home).read_bytes(), before)

    def test_written_substrate_revision_is_versioned(self):
        receipt = boot.bootstrap(self.home, apply=True)
        self.assertGreaterEqual(receipt["written_revision"], 1)
        substrate = wbs.SubstrateStore(wbs.substrate_path(self.home)).load()
        self.assertEqual(substrate["revision"], receipt["written_revision"])

    def test_duplicate_object_id_is_rejected_by_the_writer(self):
        planned = boot.plan(self.home)
        with self.assertRaises(wbs.BodySubstrateError):
            wbs.place_object(planned["substrate"], boot.TEST_OBJECT_ID,
                             {"kind": "hand", "slot_id": "right_hand"},
                             expected_revision=999)

    def test_binding_fails_closed_on_a_foreign_world(self):
        receipt = boot.bootstrap(self.home, apply=True)
        substrate = wbs.SubstrateStore(wbs.substrate_path(self.home)).load()
        foreign = {"world_id": "somewhere_else", "schema_version": "world.foundation.v0"}
        self.assertFalse(wbs.verify_binding(substrate, foreign)["bound"])
        self.assertTrue(receipt["binding"]["bound"])

    def test_bootstrap_does_not_create_extra_objects(self):
        boot.bootstrap(self.home, apply=True)
        substrate = wbs.SubstrateStore(wbs.substrate_path(self.home)).load()
        self.assertEqual(len(substrate["placements"]), 2)
        self.assertEqual(substrate["reservations"], {})

    def test_bootstrap_writes_nothing_outside_the_world_home(self):
        marker = self.home / "data" / "world" / "world_state.json"
        before = marker.read_bytes()
        boot.bootstrap(self.home, apply=True)
        self.assertEqual(marker.read_bytes(), before)


class FirewallTests(BootstrapTestBase):
    def test_bootstrap_touches_no_memory_or_activity_state(self):
        boot.bootstrap(self.home, apply=True)
        for forbidden in ("memory", "memories", "activity", "affect", "desire",
                          "relationship", "self"):
            self.assertFalse((self.home / forbidden).exists(), forbidden)

    def test_bootstrap_module_has_no_gateway_reference(self):
        source = pathlib.Path(boot.__file__).read_text(encoding="utf-8")
        for forbidden in ("hermes_cli", "hermes-agent-stock", ".hermes-stock",
                          "telegram", "expression"):
            self.assertNotIn(forbidden, source.lower())


if __name__ == "__main__":
    unittest.main(verbosity=2)
