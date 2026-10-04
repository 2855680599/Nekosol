#!/usr/bin/env python3

"""Authorization hardening tests (M15G)."""



from __future__ import annotations



import pathlib

import sys

import tempfile

import unittest



HERE = pathlib.Path(__file__).resolve().parent

sys.path.insert(0, str(HERE / "bridge"))



import world_action_authorization as WAA  # noqa: E402





def _registry():

    return WAA.AuthorizationRegistry(pathlib.Path(tempfile.mkdtemp(prefix="wauth-")))





def _issue(reg, **over):

    params = {"pose": "lying"}

    kwargs = dict(decision_id="decision:d1", intent_id="world-intent:i1",

                  intent_fingerprint="fp1", execution_id="world-exec:e1",

                  actor="chiyo", verb="POSE", params=params,

                  authority_generation=207)

    kwargs.update(over)

    return reg.issue(**kwargs), kwargs





class AuthorizationTests(unittest.TestCase):

    def test_absolute_time_round_trip_and_ttl_do_not_depend_on_host_timezone(self):
        now = 1790964000.0
        self.assertEqual(WAA._parse_iso(WAA._iso(now)), now)
        reg = _registry()
        record, _ = _issue(reg, now=now, ttl_seconds=120)
        self.assertEqual(WAA._parse_iso(record['expires_at']), now + 120)
        consumed = reg.verify_and_consume(authorization_id=record['authorization_id'],
            execution_id='world-exec:e1', verb='POSE', params={'pose':'lying'}, now=now + 1)
        self.assertEqual(consumed['state'], WAA.STATE_CONSUMED)

    def test_expired_absolute_authorization_is_still_refused(self):
        now = 1790964000.0
        reg = _registry()
        record, _ = _issue(reg, now=now, ttl_seconds=120)
        with self.assertRaisesRegex(WAA.AuthorizationError, 'AUTHORIZATION_EXPIRED'):
            reg.verify_and_consume(authorization_id=record['authorization_id'],
                execution_id='world-exec:e1', verb='POSE', params={'pose':'lying'}, now=now + 121)

    def test_authorization_binds_every_critical_field(self):

        reg = _registry()

        rec, _ = _issue(reg)

        for field in ("authorization_id", "decision_id", "intent_id",

                      "execution_id", "actor", "verb", "args_digest",
                      "issued_at", "expires_at"):

            self.assertIn(field, rec)



    def test_first_consume_passes_second_is_denied(self):

        reg = _registry()

        rec, kwargs = _issue(reg)

        reg.verify_and_consume(authorization_id=rec["authorization_id"],

                               execution_id="world-exec:e1", verb="POSE",

                               params={"pose": "lying"}, actor="chiyo")

        with self.assertRaises(WAA.AuthorizationError) as ctx:

            reg.verify_and_consume(authorization_id=rec["authorization_id"],

                                   execution_id="world-exec:e1", verb="POSE",

                                   params={"pose": "lying"}, actor="chiyo")

        self.assertIn("ALREADY_CONSUMED", str(ctx.exception))



    def test_replay_with_a_different_execution_id_is_denied(self):

        reg = _registry()

        rec, _ = _issue(reg)

        with self.assertRaises(WAA.AuthorizationError):

            reg.verify_and_consume(authorization_id=rec["authorization_id"],

                                   execution_id="world-exec:other", verb="POSE",

                                   params={"pose": "lying"}, actor="chiyo")



    def test_tampered_verb_is_denied(self):

        reg = _registry()

        rec, _ = _issue(reg)

        with self.assertRaises(WAA.AuthorizationError) as ctx:

            reg.verify_and_consume(authorization_id=rec["authorization_id"],

                                   execution_id="world-exec:e1", verb="MOVE",

                                   params={"pose": "lying"}, actor="chiyo")

        self.assertIn("VERB_MISMATCH", str(ctx.exception))



    def test_tampered_target_is_denied(self):

        reg = _registry()

        rec, _ = _issue(reg)

        with self.assertRaises(WAA.AuthorizationError) as ctx:

            reg.verify_and_consume(authorization_id=rec["authorization_id"],

                                   execution_id="world-exec:e1", verb="POSE",

                                   params={"pose": "seated"}, actor="chiyo")

        self.assertIn("ARGS_MISMATCH", str(ctx.exception))



    def test_tampered_actor_is_denied(self):

        reg = _registry()

        rec, _ = _issue(reg)

        with self.assertRaises(WAA.AuthorizationError) as ctx:

            reg.verify_and_consume(authorization_id=rec["authorization_id"],

                                   execution_id="world-exec:e1", verb="POSE",

                                   params={"pose": "lying"}, actor="someone_else")

        self.assertIn("ACTOR_MISMATCH", str(ctx.exception))



    def test_expired_authorization_is_denied(self):

        reg = _registry()

        rec, _ = _issue(reg)

        with self.assertRaises(WAA.AuthorizationError) as ctx:

            reg.verify_and_consume(authorization_id=rec["authorization_id"],

                                   execution_id="world-exec:e1", verb="POSE",

                                   params={"pose": "lying"}, actor="chiyo",

                                   now=9_999_999_999.0)

        self.assertIn("EXPIRED", str(ctx.exception))



    def test_unknown_authorization_is_denied(self):

        reg = _registry()

        with self.assertRaises(WAA.AuthorizationError) as ctx:

            reg.verify_and_consume(authorization_id="wauth:does-not-exist",

                                   execution_id="world-exec:e1", verb="POSE",

                                   params={"pose": "lying"}, actor="chiyo")

        self.assertIn("NOT_FOUND", str(ctx.exception))



    def test_cross_decision_use_is_denied(self):

        """An authorization minted for decision A cannot serve decision B."""



        reg = _registry()

        rec, _ = _issue(reg, decision_id="decision:d1")

        with self.assertRaises(WAA.AuthorizationError) as ctx:

            reg.verify_and_consume(authorization_id=rec["authorization_id"],

                                   execution_id="world-exec:e1", verb="POSE",

                                   params={"pose": "lying"}, actor="chiyo",

                                   decision_id="decision:d2")

        self.assertIn("DECISION_MISMATCH", str(ctx.exception))



    def test_matching_decision_still_passes(self):

        reg = _registry()

        rec, _ = _issue(reg, decision_id="decision:d1")

        out = reg.verify_and_consume(authorization_id=rec["authorization_id"],

                                     execution_id="world-exec:e1", verb="POSE",

                                     params={"pose": "lying"}, actor="chiyo",

                                     decision_id="decision:d1")

        self.assertEqual(out["state"], "CONSUMED")





if __name__ == "__main__":

    unittest.main(verbosity=2)

