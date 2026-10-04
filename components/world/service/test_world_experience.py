#!/usr/bin/env python3

"""Experience pipeline tests (M15E)."""



from __future__ import annotations



import json

import pathlib

import sys

import tempfile

import unittest



HERE = pathlib.Path(__file__).resolve().parent

sys.path.insert(0, str(HERE / "bridge"))



import world_experience as XP  # noqa: E402





def _spool():

    return XP.ExperienceSpool(pathlib.Path(tempfile.mkdtemp(prefix="xp-"))

                              / "experience.jsonl")





def _record(state="APPLIED", *, execution="world-exec:e1", decision="decision:d1",

            verb="POSE", pose_target="lying"):

    return {"state": state, "execution_id": execution, "decision_id": decision,

            "intent_id": "world-intent:i1", "verb": verb,

            "target": {"pose": pose_target}}





OBS = {"pose": "lying", "location": {"place_id": "home", "area_id": "bedroom"}}





class ExperienceTests(unittest.TestCase):

    def test_applied_may_be_described_as_done(self):

        c = XP.build_candidate(lifecycle_record=_record("APPLIED"), observation=OBS)

        self.assertEqual(c["assertion"], "completed")

        self.assertIn("躺下", c["summary"])



    def test_rejected_is_only_an_attempt(self):

        c = XP.build_candidate(lifecycle_record=_record("REJECTED"),

                               observation={"pose": "standing"})

        self.assertTrue(c["assertion"].startswith("attempted"))

        self.assertNotIn("躺下", c["summary"])



    def test_stale_is_only_an_attempt(self):

        c = XP.build_candidate(lifecycle_record=_record("STALE"),

                               observation={"pose": "standing"})

        self.assertIn(c["assertion"], ("attempted_failed_stale",))



    def test_unknown_stays_uncertain(self):

        c = XP.build_candidate(lifecycle_record=_record("UNKNOWN"),

                               observation={"pose": "standing"})

        self.assertEqual(c["assertion"], "attempted_uncertain")



    def test_no_identifiers_in_the_summary(self):

        c = XP.build_candidate(lifecycle_record=_record("APPLIED"), observation=OBS)

        for token in ("world-exec", "decision:", "world-intent", "wauth"):

            self.assertNotIn(token, c["summary"])



    def test_dedupe_by_canonical_provenance(self):

        s = _spool()

        first = s.record(lifecycle_record=_record("APPLIED"), observation=OBS)

        second = s.record(lifecycle_record=_record("APPLIED"), observation=OBS)

        self.assertIsNotNone(first)

        self.assertIsNone(second)

        lines = [l for l in s.path.read_text().splitlines() if l.strip()]

        self.assertEqual(len(lines), 1)



    def test_correction_instead_of_a_second_experience(self):

        s = _spool()

        s.record(lifecycle_record=_record("UNKNOWN"), observation={"pose": "standing"})

        correction = s.correct(lifecycle_record=_record("UNKNOWN"),

                               reconciled_state="APPLIED", observation=OBS)

        self.assertIsNotNone(correction)

        self.assertEqual(correction["kind"], XP.KIND_CORRECTION)

        self.assertEqual(correction["assertion"], "completed")

        kinds = [json.loads(l)["kind"] for l in s.path.read_text().splitlines() if l.strip()]

        self.assertEqual(kinds.count(XP.KIND_CANDIDATE), 1)

        self.assertEqual(kinds.count(XP.KIND_CORRECTION), 1)



    def test_experience_is_built_from_observation_not_the_ledger(self):

        source = (HERE / "bridge" / "world_experience.py").read_text()

        self.assertNotIn("life_execution", source)   # never reads the ledger

        c = XP.build_candidate(lifecycle_record=_record("APPLIED"), observation=OBS)

        self.assertEqual(c["observation"]["pose"], "lying")



    def test_spool_is_append_only(self):

        s = _spool()

        s.record(lifecycle_record=_record("APPLIED", execution="world-exec:a"),

                 observation=OBS)

        s.record(lifecycle_record=_record("APPLIED", execution="world-exec:b"),

                 observation=OBS)

        self.assertEqual(len(s.path.read_text().strip().splitlines()), 2)





if __name__ == "__main__":

    unittest.main(verbosity=2)

