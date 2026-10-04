from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from lifesupply.governance.authority import (
    ALLOW, CAPABILITY_MISMATCH, CONFLICT, EXPIRED, REVOKED, STALE_REVISION,
    TARGET_MISMATCH, USE_EXHAUSTED, GovernanceAuthority,
)
from lifesupply.workspace.store import WorkspaceStore
from lifesupply.artifact.store import ManagedArtifactStore


class Clock:
    def __init__(self, now=1000.0): self.value = now
    def __call__(self): return self.value


class GovernanceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.clock = Clock()
        self.gov = GovernanceAuthority(Path(self.tmp.name) / "governance.sqlite", clock=self.clock)

    def tearDown(self): self.tmp.cleanup()

    def issue(self, **kw):
        args = dict(grantor="HOST_OPERATOR", bootstrap_authorized=True,
                    subject="chiyo", capability="PROVISION_PERSONAL_WORKSPACE",
                    scope="workspace:personal", target="workspace:chiyo",
                    authority_scope="governance:workspace", use_mode="ONE_SHOT",
                    operation_id=f"issue:chiyo:{kw.get('scope', 'workspace:personal')}:{kw.get('expires_at', 'none')}")
        args.update(kw)
        return self.gov.issue_grant(**args)

    def test_valid_grant_allows(self):
        g = self.issue()
        self.assertEqual(g.status, ALLOW)
        self.assertEqual(self.gov.validate_grant(g.grant_id, subject="chiyo",
            capability="PROVISION_PERSONAL_WORKSPACE", scope="workspace:personal",
            target="workspace:chiyo", expected_revision=1).status, ALLOW)

    def test_wrong_capability_denies(self):
        g = self.issue()
        self.assertEqual(self.gov.validate_grant(g.grant_id, subject="chiyo",
            capability="WRITE_OPEN_INQUIRY", scope="workspace:personal",
            target="workspace:chiyo").status, CAPABILITY_MISMATCH)

    def test_wrong_target_denies(self):
        g = self.issue()
        self.assertEqual(self.gov.validate_grant(g.grant_id, subject="chiyo",
            capability="PROVISION_PERSONAL_WORKSPACE", scope="workspace:personal",
            target="workspace:other").status, TARGET_MISMATCH)

    def test_expiry_denies(self):
        g = self.issue(expires_at=1001.0)
        self.clock.value = 1001.0
        self.assertEqual(self.gov.validate_grant(g.grant_id, subject="chiyo",
            capability="PROVISION_PERSONAL_WORKSPACE", scope="workspace:personal",
            target="workspace:chiyo").status, EXPIRED)

    def test_revocation_and_stale_revision(self):
        g = self.issue()
        self.assertEqual(self.gov.revoke_grant(g.grant_id, expected_revision=2).status, STALE_REVISION)
        self.assertEqual(self.gov.revoke_grant(g.grant_id, expected_revision=1).status, REVOKED)
        self.assertEqual(self.gov.validate_grant(g.grant_id, subject="chiyo",
            capability="PROVISION_PERSONAL_WORKSPACE", scope="workspace:personal",
            target="workspace:chiyo").status, REVOKED)

    def test_one_shot_retry_and_exhaustion(self):
        g = self.issue()
        a = self.gov.reserve_use(g.grant_id, subject="chiyo", capability="PROVISION_PERSONAL_WORKSPACE",
            scope="workspace:personal", target="workspace:chiyo", action_key="provision:chiyo")
        self.assertEqual(a.status, ALLOW)
        b = self.gov.reserve_use(g.grant_id, subject="chiyo", capability="PROVISION_PERSONAL_WORKSPACE",
            scope="workspace:personal", target="workspace:chiyo", action_key="provision:chiyo")
        self.assertEqual(b.reservation_id, a.reservation_id)
        self.assertEqual(self.gov.begin_execution(a.reservation_id, actor_uid=None).status, ALLOW)
        self.assertEqual(self.gov.consume_use(a.reservation_id, actor_uid=None).status, ALLOW)
        # Same logical action returns its original receipt even after use is exhausted.
        self.assertEqual(self.gov.reserve_use(g.grant_id, subject="chiyo", capability="PROVISION_PERSONAL_WORKSPACE",
            scope="workspace:personal", target="workspace:chiyo", action_key="provision:chiyo").reservation_id, a.reservation_id)
        self.assertEqual(self.gov.reserve_use(g.grant_id, subject="chiyo", capability="PROVISION_PERSONAL_WORKSPACE",
            scope="workspace:personal", target="workspace:chiyo", action_key="another-action").status, USE_EXHAUSTED)
        self.assertEqual(self.gov.reserve_use(g.grant_id, subject="chiyo", capability="PROVISION_PERSONAL_WORKSPACE",
            scope="workspace:personal", target="workspace:chiyo", action_key="provision:chiyo").status, ALLOW)
        no_effect = self.gov.reserve_use(g.grant_id, subject="chiyo", capability="PROVISION_PERSONAL_WORKSPACE",
            scope="workspace:personal", target="workspace:chiyo", action_key="no-effect")
        self.assertEqual(no_effect.status, USE_EXHAUSTED)

    def test_unknown_fails_closed_and_bootstrap_required(self):
        self.assertEqual(self.gov.validate_grant("missing", subject="chiyo", capability="x",
            scope="x", target="x").status, "UNKNOWN")
        d = self.issue(bootstrap_authorized=False)
        self.assertEqual(d.status, "DENY")


class WorkspaceTests(unittest.TestCase):
    def test_provision_is_empty_idempotent_and_persistent(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "workspace.sqlite"
            store = WorkspaceStore(p, clock=Clock())
            created = store.provision_workspace(subject_id="chiyo", grant_id="g1", operation_key="op1")
            self.assertEqual(created["status"], "OK")
            self.assertEqual(store.workspace_count(), 1)
            self.assertEqual(store.list_open_inquiries("chiyo"), [])
            retry = store.provision_workspace(subject_id="chiyo", grant_id="g1", operation_key="op1")
            self.assertEqual(retry["workspace"]["workspace_id"], created["workspace"]["workspace_id"])
            restarted = WorkspaceStore(p, clock=Clock())
            self.assertEqual(restarted.get_workspace("chiyo")["lifecycle"], "ACTIVE")
            self.assertEqual(restarted.list_open_inquiries("chiyo"), [])

    def test_duplicate_workspace_does_not_create_second(self):
        with tempfile.TemporaryDirectory() as td:
            store = WorkspaceStore(Path(td) / "w.sqlite", clock=Clock())
            a = store.provision_workspace(subject_id="chiyo", grant_id="g1", operation_key="op1")
            b = store.provision_workspace(subject_id="chiyo", grant_id="g2", operation_key="op2")
            self.assertEqual(a["workspace"]["workspace_id"], b["workspace"]["workspace_id"])
            self.assertEqual(store.workspace_count(), 1)

    def test_admission_needs_grant_and_deterministic_gate(self):
        with tempfile.TemporaryDirectory() as td:
            store = WorkspaceStore(Path(td) / "w.sqlite", clock=Clock())
            store.provision_workspace(subject_id="chiyo", grant_id="g", operation_key="provision")
            p = store.submit_proposal(subject_id="chiyo", topic="volcanoes", source_refs=["cognition:event:1"],
                driver="SELF_INITIATED", review_after=None, expires_at=2000, operation_key="proposal1")
            self.assertEqual(p["status"], "BLOCKED")
            denied = store.admit_proposal(proposal_id="proposal:forged", authorized_subject_id="chiyo",
                authorized_workspace_id="workspace:chiyo", governance_reservation_id="r1",
                source_exists=True, scope_allowed=True, within_quota=True)
            self.assertEqual(denied["status"], "BLOCKED")
            self.assertEqual(len(store.list_open_inquiries("chiyo")), 0)


class ArtifactTests(unittest.TestCase):
    def test_eight_core_contract_cases(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            workspace = WorkspaceStore(root / "workspace.sqlite", clock=Clock())
            workspace.provision_workspace(subject_id="test:life-supply", grant_id="wg", operation_key="wp")
            store = ManagedArtifactStore(root / "artifact.sqlite", projection_root=root / "projection",
                                         workspace_authority=workspace, clock=Clock())
            workspace_id = workspace.get_workspace("test:life-supply")["workspace_id"]
            self.assertEqual(store.create_artifact(subject_id="test:life-supply", workspace_id="workspace:other",
                title="wrong workspace", operation_key="wrong-workspace", grant_reservation_id="reserve:bad",
                operation_payload={"operation": "create_artifact", "subject_id": "test:life-supply",
                    "workspace_id": "workspace:other", "title": "wrong workspace"})["status"], "DENY")
            self.assertEqual(store.create_artifact(subject_id="test:life-supply", workspace_id=workspace_id,
                title="test", operation_key="create", grant_reservation_id="reserve:create",
                operation_payload={"operation": "create_artifact", "subject_id": "test:life-supply",
                    "workspace_id": workspace_id, "title": "test"})["status"], "OK")
            aid = store.list_artifacts("test:life-supply")[0]["artifact_id"]
            denied_owner = store.commit_markdown(artifact_id=aid, authorized_subject_id="test:other",
                expected_head=None, content="cross-subject", operation_key="bad-owner",
                grant_reservation_id="r-cross")
            self.assertEqual(denied_owner["status"], "NOT_FOUND")
            v1 = store.commit_markdown(artifact_id=aid, authorized_subject_id="test:life-supply", expected_head=None, content="# One",
                operation_key="v1", grant_reservation_id="r1", operation_payload={"operation": "commit_markdown",
                    "subject_id": "test:life-supply", "artifact_id": aid, "expected_head": None, "content": "# One"})
            self.assertEqual(v1["status"], "OK")
            v2 = store.commit_markdown(artifact_id=aid, authorized_subject_id="test:life-supply", expected_head=v1["version_id"], content="# Two",
                operation_key="v2", grant_reservation_id="r2", operation_payload={"operation": "commit_markdown",
                    "subject_id": "test:life-supply", "artifact_id": aid, "expected_head": v1["version_id"], "content": "# Two"})
            self.assertEqual(v2["status"], "OK")
            # same-key retry after head advanced is returned before CAS
            self.assertEqual(store.commit_markdown(artifact_id=aid, authorized_subject_id="test:life-supply", expected_head=v1["version_id"], content="# Two",
                operation_key="v2", grant_reservation_id="r2", operation_payload={"operation": "commit_markdown",
                    "subject_id": "test:life-supply", "artifact_id": aid, "expected_head": v1["version_id"], "content": "# Two"})["version_id"], v2["version_id"])
            self.assertEqual(store.commit_markdown(artifact_id=aid, authorized_subject_id="test:life-supply", expected_head=v1["version_id"], content="# Different",
                operation_key="v2", grant_reservation_id="r2", operation_payload={"operation": "commit_markdown",
                    "subject_id": "test:life-supply", "artifact_id": aid, "expected_head": v1["version_id"], "content": "# Different"})["status"], "CONFLICT")
            self.assertEqual(store.commit_markdown(artifact_id=aid, authorized_subject_id="test:life-supply", expected_head=v1["version_id"], content="# stale",
                operation_key="stale", grant_reservation_id="r3", operation_payload={"operation": "commit_markdown",
                    "subject_id": "test:life-supply", "artifact_id": aid, "expected_head": v1["version_id"], "content": "# stale"})["status"], "CONFLICT")
            nochange = store.commit_markdown(artifact_id=aid, authorized_subject_id="test:life-supply", expected_head=v2["version_id"], content="# Two",
                operation_key="same-content", grant_reservation_id="r4", operation_payload={"operation": "commit_markdown",
                    "subject_id": "test:life-supply", "artifact_id": aid, "expected_head": v2["version_id"], "content": "# Two"})
            nochange_replay = store.commit_markdown(artifact_id=aid, authorized_subject_id="test:life-supply", expected_head=v2["version_id"], content="# Two",
                operation_key="same-content", grant_reservation_id="r4", operation_payload={"operation": "commit_markdown",
                    "subject_id": "test:life-supply", "artifact_id": aid, "expected_head": v2["version_id"], "content": "# Two"})
            self.assertEqual(nochange_replay, nochange)
            self.assertEqual(nochange["status"], "NO_CHANGE")
            self.assertFalse(nochange["version_created"])
            self.assertEqual(store.check_projection(aid)["status"], "OK")
            # External divergence detected through the public checker after test-only tampering.
            import sqlite3
            con = sqlite3.connect(root / "artifact.sqlite")
            rel = con.execute("SELECT relative_path FROM artifact_projection_bindings WHERE artifact_id=?", (aid,)).fetchone()[0]
            con.close()
            (root / "projection" / rel).write_text("diverged", encoding="utf-8")
            self.assertEqual(store.check_projection(aid)["status"], "CONFLICT")
            store.rebuild_projection(aid)
            revision = store.read_artifact(aid)["revision"]
            self.assertEqual(store.tombstone(aid, authorized_subject_id="test:life-supply", expected_revision=revision, operation_key="tombstone",
                grant_reservation_id="r5", operation_payload={"operation": "tombstone_artifact",
                    "subject_id": "test:life-supply", "artifact_id": aid, "expected_revision": revision})["status"], "OK")
            self.assertEqual(store.commit_markdown(artifact_id=aid, authorized_subject_id="test:life-supply", expected_head=v2["version_id"], content="x",
                operation_key="after-tombstone", grant_reservation_id="r6", operation_payload={"operation": "commit_markdown",
                    "subject_id": "test:life-supply", "artifact_id": aid, "expected_head": v2["version_id"], "content": "x"})["status"], "BLOCKED")
            self.assertIsNone(store.read_artifact("missing"))


if __name__ == "__main__":
    unittest.main()
