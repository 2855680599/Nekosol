from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from lifesupply.governance.authority import GovernanceAuthority, ALLOW, STALE_REVISION, REVOKED, CONFLICT

class Clock:
    def __init__(self): self.now=100.0
    def __call__(self): return self.now

class ReservationFenceTests(unittest.TestCase):
    def test_reservation_replay_is_bound_to_revision_and_revocation(self):
        with tempfile.TemporaryDirectory() as td:
            clock=Clock(); g=GovernanceAuthority(Path(td)/"g.sqlite",clock=clock)
            grant=g.issue_grant(grantor="HOST_OPERATOR",bootstrap_authorized=True,subject="s",
                capability="COMMIT_MANAGED_ARTIFACT",scope="artifact:personal",target="subject:s",
                authority_scope="governance:test",use_mode="ONE_SHOT",operation_id="issue:test-one-shot")
            r=g.reserve_use(grant.grant_id,subject="s",capability="COMMIT_MANAGED_ARTIFACT",
                scope="artifact:personal",target="subject:s",action_key="a",expected_revision=1,payload={"x":1})
            self.assertEqual(r.status,ALLOW)
            self.assertEqual(g.revoke_grant(grant.grant_id,expected_revision=1).status,REVOKED)
            replay=g.reserve_use(grant.grant_id,subject="s",capability="COMMIT_MANAGED_ARTIFACT",
                scope="artifact:personal",target="subject:s",action_key="a",expected_revision=1,payload={"x":1})
            self.assertEqual(replay.status,STALE_REVISION)
            self.assertEqual(g.begin_execution(r.reservation_id,actor_uid=None).status,STALE_REVISION)
            self.assertEqual(g.consume_use(r.reservation_id,actor_uid=None).status,"DENY")
            self.assertEqual(g.validate_grant(grant.grant_id,subject="s",capability="COMMIT_MANAGED_ARTIFACT",
                scope="artifact:personal",target="subject:s",expected_revision=2,action_key="a",payload={"x":2}).status,REVOKED)

    def test_revoke_after_execution_linearization_does_not_misreport_effect(self):
        with tempfile.TemporaryDirectory() as td:
            g=GovernanceAuthority(Path(td)/"g.sqlite",clock=Clock())
            grant=g.issue_grant(grantor="HOST_OPERATOR",bootstrap_authorized=True,subject="s",
                capability="COMMIT_MANAGED_ARTIFACT",scope="artifact:personal",target="subject:s",
                authority_scope="governance:test",use_mode="ONE_SHOT",operation_id="issue:test-one-shot")
            r=g.reserve_use(grant.grant_id,subject="s",capability="COMMIT_MANAGED_ARTIFACT",
                scope="artifact:personal",target="subject:s",action_key="race",payload={})
            self.assertEqual(g.begin_execution(r.reservation_id,actor_uid=None).status,ALLOW)
            self.assertEqual(g.revoke_grant(grant.grant_id,expected_revision=1).status,REVOKED)
            # begin_execution is the linearization point. Revocation loses only to actions
            # already in EXECUTING; finalization records the effect authorized there.
            self.assertEqual(g.consume_use(r.reservation_id,actor_uid=None).status,ALLOW)

    def test_action_key_payload_conflict(self):
        with tempfile.TemporaryDirectory() as td:
            g=GovernanceAuthority(Path(td)/"g.sqlite",clock=Clock())
            grant=g.issue_grant(grantor="HOST_OPERATOR",bootstrap_authorized=True,subject="s",
                capability="COMMIT_MANAGED_ARTIFACT",scope="artifact:personal",target="subject:s",
                authority_scope="governance:test",use_mode="REUSABLE",operation_id="issue:test-reusable")
            a=g.reserve_use(grant.grant_id,subject="s",capability="COMMIT_MANAGED_ARTIFACT",
                scope="artifact:personal",target="subject:s",action_key="k",payload={"a":1})
            b=g.reserve_use(grant.grant_id,subject="s",capability="COMMIT_MANAGED_ARTIFACT",
                scope="artifact:personal",target="subject:s",action_key="k",payload={"a":2})
            self.assertEqual(a.status,ALLOW)
            self.assertEqual(b.status,CONFLICT)

if __name__=="__main__": unittest.main()
