#!/usr/bin/env python3

"""Object portability tests (M15F)."""



from __future__ import annotations



import pathlib

import sys

import unittest



HERE = pathlib.Path(__file__).resolve().parent

sys.path.insert(0, str(HERE / "bridge"))



import world_object_capability as OC  # noqa: E402



BED = {"label": "basic_bed", "capability": {"fixed": True, "portable": False,

                                            "takeable": False, "placeable": False}}

CUP = {"label": "cup", "capability": {"portable": True, "takeable": True,

                                      "placeable": True, "weight_class": "light",

                                      "hand_requirement": "one"}}

LEGACY = {"label": "odd_thing"}





class CapabilityTests(unittest.TestCase):

    def test_missing_capability_is_unknown(self):

        cap = OC.capability_of(LEGACY)

        self.assertEqual(cap["takeable"], OC.UNKNOWN)

        self.assertEqual(cap["portable"], OC.UNKNOWN)



    def test_unknown_capability_denies_pick_up(self):

        v = OC.pick_up_verdict(obj=LEGACY, hand="left")

        self.assertEqual(v["verdict"], OC.DENIED)

        self.assertIn("takeable=UNKNOWN", v["reason"])



    def test_false_capability_denies_pick_up(self):

        v = OC.pick_up_verdict(obj=BED, hand="left")

        self.assertEqual(v["verdict"], OC.DENIED)



    def test_the_bed_is_refused_by_capability_not_by_name(self):

        source = (HERE / "bridge" / "world_object_capability.py").read_text()

        self.assertNotIn("basic_bed", source)

        renamed = {"label": "not_a_bed", "capability": BED["capability"]}

        self.assertEqual(OC.pick_up_verdict(obj=renamed, hand="left")["verdict"],

                         OC.DENIED)



    def test_takeable_object_with_a_free_hand_is_allowed(self):

        v = OC.pick_up_verdict(obj=CUP, hand="left")

        self.assertEqual(v["verdict"], OC.ALLOWED)



    def test_occupied_hand_denies(self):

        v = OC.pick_up_verdict(obj=CUP, hand="left", already_held=True)

        self.assertEqual(v["verdict"], OC.DENIED)

        self.assertIn("hand_occupied", v["reason"])



    def test_stale_dependency_or_bad_authorization_denies(self):

        self.assertEqual(OC.pick_up_verdict(obj=CUP, hand="left",

                                            dependencies_fresh=False)["verdict"],

                         OC.DENIED)

        self.assertEqual(OC.pick_up_verdict(obj=CUP, hand="left",

                                            authorization_valid=False)["verdict"],

                         OC.DENIED)



    def test_hand_state_reports_free_and_occupied(self):

        state = OC.hand_state({"left": None, "right": "cup"})

        self.assertEqual(state["slots"]["left"], "free")

        self.assertEqual(state["slots"]["right"], "occupied")

        self.assertEqual(state["free"], ["left"])



    def test_place_requires_the_object_to_be_held(self):

        v = OC.place_verdict(obj=CUP, held=False, target_kind="surface",

                             target_permits=True, slot_available=True)

        self.assertEqual(v["verdict"], OC.DENIED)

        self.assertIn("object_not_held", v["reason"])



    def test_production_verbs_blocked_without_a_canonical_object(self):

        out = OC.production_verbs_available([BED, LEGACY])

        self.assertEqual(out["verbs"], [])

        self.assertEqual(out["blocked_reason"], "BLOCKED_NO_CANONICAL_OBJECT")

        out2 = OC.production_verbs_available([BED, CUP])

        self.assertEqual(out2["verbs"], ["PICK_UP", "PLACE"])





if __name__ == "__main__":

    unittest.main(verbosity=2)

