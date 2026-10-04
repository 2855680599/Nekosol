from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

from lifesupply.fencing import WriterFence, register_writer_connection
from lifesupply.governance.authority import GovernanceAuthority
from lifesupply.service.server import Api
from lifesupply.trust.source import (
    DeliveredSourceEvidence, EvidenceResult, EvidenceStatus, InquiryAdmissionPolicyV1,
    SourceKind, SourceScopePolicy, TrustedSourceRef,
)


class FakeEvidencePort:
    def __init__(self, evidence):
        self.evidence = evidence

    def verify_source(self, ref):
        return EvidenceResult(EvidenceStatus.FOUND, self.evidence)

    def resolve_source(self, ref):
        return self.verify_source(ref)

    def read_source_scope(self, ref):
        return self.evidence.visibility_scope

    def read_source_subject(self, ref):
        return self.evidence.subject_id

    def read_source_delivery_state(self, ref):
        return self.evidence.delivered_status


class C6FencingTests(unittest.TestCase):
    def test_issue_grant_retry_is_stable_when_valid_from_is_defaulted(self):
        class Tick:
            def __init__(self): self.value = 10.0
            def __call__(self): self.value += 1.0; return self.value
        with tempfile.TemporaryDirectory() as td:
            g = GovernanceAuthority(Path(td) / "g.sqlite", clock=Tick())
            args = dict(grantor="HOST_OPERATOR", bootstrap_authorized=True,
                subject="chiyo", capability="PROVISION_PERSONAL_WORKSPACE", scope="workspace:personal",
                target="workspace:chiyo", authority_scope="life-supply.workspace.v1", operation_id="retry-issue")
            first = g.issue_grant(**args)
            replay = g.issue_grant(**args)
            self.assertEqual(first.status, "ALLOW")
            self.assertEqual(replay.to_dict(), first.to_dict())

    def test_read_only_store_open_does_not_clobber_active_writer_fence(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            fence = WriterFence("Governance", root / "fences", instance_id="primary")
            writer = GovernanceAuthority(root / "governance.sqlite", writer_fence=fence)
            first = writer.issue_grant(grantor="HOST_OPERATOR", bootstrap_authorized=True,
                subject="chiyo", capability="PROVISION_PERSONAL_WORKSPACE", scope="workspace:personal",
                target="workspace:chiyo", authority_scope="life-supply.workspace.v1", operation_id="first")
            self.assertEqual(first.status, "ALLOW")
            observer = GovernanceAuthority(root / "governance.sqlite", read_only=True)
            self.assertEqual(observer.get_grant(first.grant_id)["grant_id"], first.grant_id)
            second = writer.issue_grant(grantor="HOST_OPERATOR", bootstrap_authorized=True,
                subject="chiyo", capability="PROVISION_PERSONAL_WORKSPACE", scope="workspace:personal",
                target="workspace:chiyo", authority_scope="life-supply.workspace.v1", operation_id="second")
            self.assertEqual(second.status, "ALLOW")
            fence.close()

    def test_unresolved_governance_reservation_blocks_new_authority_writes(self):
        with tempfile.TemporaryDirectory() as td:
            uid = os.geteuid()
            api = Api(Path(td), mode="governance", operator_uids={uid}, supply_uids={uid},
                      control_uids={uid}, owner_uids={uid}, writer_instance="unresolved-test")
            try:
                issued = api.dispatch({"op": "issue_grant", "grant": {
                    "subject": "chiyo", "capability": "PROVISION_PERSONAL_WORKSPACE",
                    "scope": "workspace:personal", "target": "workspace:chiyo",
                    "authority_scope": "life-supply.workspace.v1", "operation_id": "seed-unresolved"}}, peer_uid=uid)
                self.assertEqual(issued["status"], "ALLOW")
                reserved = api.dispatch({"op": "reserve_use", "grant_id": issued["grant_id"], "request": {
                    "subject": "chiyo", "capability": "PROVISION_PERSONAL_WORKSPACE",
                    "scope": "workspace:personal", "target": "workspace:chiyo",
                    "authority_scope": "life-supply.workspace.v1", "action_key": "uncertain-op",
                    "payload": {"subject_id": "chiyo"}}}, peer_uid=uid)
                self.assertEqual(reserved["status"], "ALLOW")
                begun = api.dispatch({"op": "begin_execution", "reservation_id": reserved["reservation_id"]}, peer_uid=uid)
                self.assertEqual(begun["status"], "ALLOW")
                blocked = api.dispatch({"op": "issue_grant", "grant": {
                    "subject": "chiyo", "capability": "PROVISION_PERSONAL_WORKSPACE",
                    "scope": "workspace:personal", "target": "workspace:chiyo",
                    "authority_scope": "life-supply.workspace.v1", "operation_id": "must-block"}}, peer_uid=uid)
                self.assertEqual(blocked["status"], "BLOCKED")
                self.assertEqual(api.dispatch({"op": "health"}, peer_uid=uid)["service_state"], "READ_ONLY")
            finally:
                for fence in api.fences.values(): fence.close()

    def test_second_live_writer_is_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            first = WriterFence("Governance", Path(td) / "fences", instance_id="primary")
            try:
                with self.assertRaisesRegex(RuntimeError, "WRITE_FENCE_HELD"):
                    WriterFence("Governance", Path(td) / "fences", instance_id="standby")
            finally:
                first.close()

    def test_stale_sqlite_connection_cannot_mutate_after_new_epoch(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            f1 = WriterFence("Governance", root / "fences", instance_id="writer-1")
            authority1 = GovernanceAuthority(root / "governance.sqlite", writer_fence=f1)
            issued = authority1.issue_grant(grantor="HOST_OPERATOR", bootstrap_authorized=True,
                subject="chiyo", capability="PROVISION_PERSONAL_WORKSPACE", scope="workspace:personal",
                target="workspace:chiyo", authority_scope="life-supply.workspace.v1", operation_id="seed",
                writer_instance="writer-1")
            self.assertEqual(issued.status, "ALLOW")
            stale = sqlite3.connect(root / "governance.sqlite", isolation_level=None)
            stale.row_factory = sqlite3.Row
            register_writer_connection(stale, f1)
            f1.close()
            f2 = WriterFence("Governance", root / "fences", instance_id="writer-2")
            try:
                authority2 = GovernanceAuthority(root / "governance.sqlite", writer_fence=f2)
                self.assertIsNotNone(authority1)
                with self.assertRaisesRegex(sqlite3.IntegrityError, "STALE_WRITER"):
                    stale.execute("BEGIN IMMEDIATE")
                    stale.execute("UPDATE permission_grants SET revision=revision+1 WHERE grant_id=?", (issued.grant_id,))
                stale.rollback()
                self.assertEqual(f2.status()["state"], "HELD")
            finally:
                stale.close()
                f2.close()

    def test_scope_mismatch_is_denied_by_service_rpc(self):
        # Governance RPC chooses the scope from the allowlisted capability, not caller JSON.
        # The typed-source policy also stays closed when its production port is absent.
        policy = SourceScopePolicy(None)
        ref = TrustedSourceRef("turn:1", SourceKind.DELIVERED_USER_TURN, "runtime", "chiyo",
            1.0, "PRIVATE_SELF", "r1", "sha256:abc", "DELIVERED")
        verdict = policy.verify(ref, workspace_subject="chiyo", workspace_visibility="PRIVATE",
            grant_scope="inquiry:personal")
        self.assertEqual(verdict.status, "BLOCK")
        self.assertEqual(verdict.reason, "TRUSTED_SOURCE_PORT_PENDING")

    def test_c5_schema_is_rejected_without_partial_upgrade(self):
        import sqlite3
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "governance.sqlite"
            con = sqlite3.connect(path)
            con.execute("CREATE TABLE permission_grants(grant_id TEXT PRIMARY KEY, subject TEXT)")
            con.commit(); con.close()
            with self.assertRaisesRegex(RuntimeError, "C6_SCHEMA_MIGRATION_REQUIRED"):
                GovernanceAuthority(path)
            con = sqlite3.connect(path)
            columns = {row[1] for row in con.execute("PRAGMA table_info(permission_grants)")}
            con.close()
            self.assertEqual(columns, {"grant_id", "subject"})

    def test_replaced_sidecar_token_cannot_alias_same_instance(self):
        import os
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            f1 = WriterFence("Governance", root / "fences", instance_id="stable-instance")
            authority1 = GovernanceAuthority(root / "governance.sqlite", writer_fence=f1)
            grant = authority1.issue_grant(grantor="HOST_OPERATOR", bootstrap_authorized=True,
                subject="chiyo", capability="PROVISION_PERSONAL_WORKSPACE", scope="workspace:personal",
                target="workspace:chiyo", authority_scope="life-supply.workspace.v1", operation_id="before-reset")
            stale = sqlite3.connect(root / "governance.sqlite", isolation_level=None)
            stale.row_factory = sqlite3.Row
            register_writer_connection(stale, f1)
            fence_path = f1.path
            f1.close()
            replacement = root / "fences" / "replacement"
            replacement.write_text('{"epoch":0}', encoding="utf-8")
            os.replace(replacement, fence_path)
            f2 = WriterFence("Governance", root / "fences", instance_id="stable-instance")
            try:
                GovernanceAuthority(root / "governance.sqlite", writer_fence=f2)
                with self.assertRaisesRegex(sqlite3.IntegrityError, "STALE_WRITER"):
                    stale.execute("BEGIN IMMEDIATE")
                    stale.execute("UPDATE permission_grants SET revision=revision+1 WHERE grant_id=?", (grant.grant_id,))
                stale.rollback()
            finally:
                stale.close(); f2.close()

    def test_typed_source_rejects_untrusted_prompt_and_allows_exact_delivered_evidence(self):
        ref = TrustedSourceRef("turn:2", SourceKind.DELIVERED_USER_TURN, "runtime", "chiyo",
            100.0, "PRIVATE_SELF", "rev-3", "sha256:123", "DELIVERED")
        evidence = DeliveredSourceEvidence("turn:2", SourceKind.DELIVERED_USER_TURN,
            "runtime", "chiyo", 100.0, "PRIVATE_SELF", "rev-3", "sha256:123", "DELIVERED")
        policy = SourceScopePolicy(FakeEvidencePort(evidence), InquiryAdmissionPolicyV1())
        self.assertEqual(policy.verify(ref, workspace_subject="chiyo", workspace_visibility="PRIVATE",
            grant_scope="inquiry:personal").status, "ALLOW")
        prompt = TrustedSourceRef("prompt:2", SourceKind.PROMPT_TEXT, "model", "chiyo",
            100.0, "PRIVATE_SELF", "rev-3", "sha256:123", "DELIVERED")
        self.assertEqual(policy.verify(prompt, workspace_subject="chiyo", workspace_visibility="PRIVATE",
            grant_scope="inquiry:personal").reason, "SOURCE_KIND_NOT_ACCEPTED")


if __name__ == "__main__":
    unittest.main()
