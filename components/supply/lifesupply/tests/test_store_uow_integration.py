"""C8/C9 PHASE 02-07 -- the three real owners on ONE database and ONE transaction.

C7 proved the single-database and unit-of-work *mechanism* with raw SQL while the real stores
still opened their own files.  Here the real Governance / Workspace / ManagedArtifact stores
are bound to one ``LifeSupplyDB`` and take part in one coordinated command, and every command
must satisfy its own step contract and postconditions before it is allowed to COMMIT
(C9 PHASE 02).

Requires POSIX (``fcntl``) because the writer fences are flock-based; run on Linux.
"""
from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from lifesupply.artifact.store import ManagedArtifactStore
from lifesupply.db import (
    OWNERS,
    CommandContractViolation,
    LifeSupplyDB,
    UnitOfWorkDoomed,
)
from lifesupply.fencing import WriterFence
from lifesupply.governance.authority import ALLOW, GrantDecision, GovernanceAuthority
from lifesupply.workspace.store import WorkspaceStore

CAPABILITY = "PROVISION_PERSONAL_WORKSPACE"
ARTIFACT_CAPABILITY = "COMMIT_MANAGED_ARTIFACT"
SCOPE = "workspace:personal"
TARGET = "workspace:chiyo"


class Clock:
    def __init__(self, value: float = 1000.0) -> None:
        self.value = float(value)

    def __call__(self) -> float:
        return self.value

    def tick(self, seconds: float = 1.0) -> None:
        self.value += float(seconds)


class StoreUnitOfWorkIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.clock = Clock()
        self.db_path = self.root / "life_supply.sqlite"
        self.fences = {owner: WriterFence(owner, self.root / "fences") for owner in OWNERS}
        # ONE canonical database, three owners, one set of per-owner guards
        self.db = LifeSupplyDB(self.db_path, fences=self.fences, clock=self.clock)
        # the three REAL stores, bound to that one database
        self.gov = GovernanceAuthority(db=self.db, clock=self.clock,
                                       writer_fence=self.fences["Governance"])
        self.workspace = WorkspaceStore(db=self.db, clock=self.clock,
                                        writer_fence=self.fences["Workspace"])
        self.artifact = ManagedArtifactStore(
            db=self.db, projection_root=self.root / "projection",
            workspace_authority=self.workspace, clock=self.clock,
            writer_fence=self.fences["ManagedArtifact"])

    def tearDown(self) -> None:
        for fence in self.fences.values():
            fence.release()
        self._tmp.cleanup()

    def _count(self, table: str) -> int:
        with self.db.connection() as con:
            return int(con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])

    def _issue(self, **kw) -> GrantDecision:
        args = dict(grantor="HOST_OPERATOR", bootstrap_authorized=True, subject="chiyo",
                    capability=CAPABILITY, scope=SCOPE, target=TARGET,
                    authority_scope="governance:workspace", use_mode="ONE_SHOT",
                    operation_id=f"issue:chiyo:{kw.get('operation_id', 'default')}")
        args.update(kw)
        return self.gov.issue_grant(**args)

    # -- PHASE 02: one database, two usage modes -------------------------------

    def test_stores_share_one_file_and_still_commit_standalone(self) -> None:
        """A single-owner operation outside any UoW keeps working, on the shared file."""
        decision = self._issue()
        self.assertEqual(decision.status, ALLOW)
        self.assertEqual(self._count("permission_grants"), 1)
        self.assertTrue(self.db_path.is_file())
        self.assertEqual(sorted(p.name for p in self.root.glob("*.sqlite")),
                         ["life_supply.sqlite"])

    def test_store_sees_the_unit_of_work_connection(self) -> None:
        """Inside a UoW the stores share one connection, so they see each other's writes."""
        with self.db.unit_of_work(command="ISSUE_GRANT", operation_id="op:share",
                                  subject_id="chiyo") as uow:
            decision = self._issue(operation_id="issue:share")
            uow.step(decision, owner="Governance", action="issue_grant")
            uow.param("grant_id", decision.grant_id)
            # uncommitted, but already visible through the shared connection
            self.assertEqual(self._count("permission_grants"), 1)
            uow.note("Governance", "permission_grants", "grant issued", decision.grant_id)
        self.assertEqual(self._count("permission_grants"), 1)

    # -- PHASE 06: the real cross-owner provision transaction ------------------

    def test_coordinated_provision_is_one_transaction(self) -> None:
        decision = self._issue()
        self.assertEqual(decision.status, ALLOW)
        grant_id = decision.grant_id

        with self.db.unit_of_work(command="PROVISION_WORKSPACE",
                                  operation_id="op:provision:chiyo",
                                  subject_id="chiyo") as uow:
            uow.param("subject_id", "chiyo")
            uow.param("grant_id", grant_id)

            validated = self.gov.validate_grant(
                grant_id, subject="chiyo", capability=CAPABILITY, scope=SCOPE,
                target=TARGET, expected_revision=None)
            uow.step(validated, owner="Governance", action="validate_grant")
            self.assertEqual(validated.status, ALLOW)

            reserved = self.gov.reserve_use(
                grant_id, subject="chiyo", capability=CAPABILITY, scope=SCOPE,
                target=TARGET, action_key="provision:chiyo")
            uow.step(reserved, owner="Governance", action="reserve_use")
            self.assertEqual(reserved.status, ALLOW)
            uow.param("reservation_id", reserved.reservation_id)

            begun = self.gov.begin_execution(reserved.reservation_id, actor_uid=None)
            uow.step(begun, owner="Governance", action="begin_execution")
            self.assertEqual(begun.status, ALLOW)

            consumed = self.gov.consume_use(reserved.reservation_id, actor_uid=None)
            uow.step(consumed, owner="Governance", action="consume_use")
            self.assertEqual(consumed.status, ALLOW)

            created = self.workspace.provision_workspace(
                subject_id="chiyo", grant_id=grant_id, operation_key="op:provision:chiyo")
            uow.step(created, owner="Workspace", action="provision_workspace")
            self.assertEqual(created["status"], "OK", created)
            uow.note("Workspace", "workspaces", "provisioned",
                     created["workspace"]["workspace_id"])

        self.assertEqual(uow.participants, ["Governance", "Workspace"])
        self.assertEqual(self._count("permission_grants"), 1)
        self.assertEqual(self._count("permission_reservations"), 1)
        self.assertEqual(self._count("workspaces"), 1)

        with self.db.connection() as con:
            reservation = con.execute("SELECT state FROM permission_reservations").fetchone()
            journal = con.execute(
                "SELECT command,participants,outcome FROM life_supply_uow_journal").fetchone()
        self.assertEqual(reservation["state"], "CONSUMED")
        self.assertEqual(journal["command"], "PROVISION_WORKSPACE")
        self.assertEqual(journal["outcome"], "COMMITTED")
        self.assertIn("Governance", journal["participants"])
        self.assertIn("Workspace", journal["participants"])

    def test_coordinated_provision_denial_rolls_back_both_owners(self) -> None:
        """A refusal anywhere in the command leaves no half-authorised effect anywhere."""
        with self.assertRaises(UnitOfWorkDoomed):
            with self.db.unit_of_work(command="PROVISION_WORKSPACE",
                                      operation_id="op:provision:denied",
                                      subject_id="chiyo") as uow:
                uow.param("subject_id", "chiyo")
                decision = self._issue(operation_id="issue:denied")
                self.assertEqual(decision.status, ALLOW)
                self.assertEqual(self._count("permission_grants"), 1)
                # the contract allows only ALLOW for reserve_use, and this one is UNKNOWN
                refused = self.gov.reserve_use(
                    "grant:does-not-exist", subject="chiyo", capability=CAPABILITY,
                    scope=SCOPE, target=TARGET, action_key="provision:chiyo")
                uow.step(refused, owner="Governance", action="reserve_use")
                self.assertNotEqual(refused.status, ALLOW)
                self.assertIsNotNone(uow.doomed)
                created = self.workspace.provision_workspace(
                    subject_id="chiyo", grant_id=decision.grant_id,
                    operation_key="op:provision:denied")
                self.assertEqual(created["status"], "OK", created)

        # nothing survived: not the grant, not the reservation, not the workspace
        self.assertEqual(self._count("permission_grants"), 0)
        self.assertEqual(self._count("permission_reservations"), 0)
        self.assertEqual(self._count("workspaces"), 0)
        self.assertEqual(self._count("life_supply_uow_journal"), 0)

    # -- PHASE 07: the real artifact transaction -------------------------------

    def _reserve_for_artifact(self, uow, action_key: str) -> str:
        """Real Governance reservation for the artifact capability, inside the UoW."""
        decision = self._issue(capability=ARTIFACT_CAPABILITY, operation_id=f"issue:{action_key}")
        self.assertEqual(decision.status, ALLOW)
        reserved = self.gov.reserve_use(
            decision.grant_id, subject="chiyo", capability=ARTIFACT_CAPABILITY, scope=SCOPE,
            target=TARGET, action_key=action_key)
        uow.step(reserved, owner="Governance", action="reserve_use")
        self.assertEqual(reserved.status, ALLOW, reserved)
        begun = self.gov.begin_execution(reserved.reservation_id, actor_uid=None)
        uow.step(begun, owner="Governance", action="begin_execution")
        self.assertEqual(begun.status, ALLOW)
        consumed = self.gov.consume_use(reserved.reservation_id, actor_uid=None)
        uow.step(consumed, owner="Governance", action="consume_use")
        self.assertEqual(consumed.status, ALLOW)
        uow.param("reservation_id", reserved.reservation_id)
        return reserved.reservation_id

    def test_artifact_commit_markdown_in_one_transaction(self) -> None:
        decision = self._issue()
        self.workspace.provision_workspace(subject_id="chiyo", grant_id=decision.grant_id,
                                           operation_key="op:provision:art")
        workspace_id = self.workspace.get_workspace("chiyo")["workspace_id"]

        with self.db.unit_of_work(command="COMMIT_MARKDOWN", operation_id="op:commit:1",
                                  subject_id="chiyo") as uow:
            uow.param("subject_id", "chiyo")
            uow.param("operation_key", "op:commit:1")
            reservation_id = self._reserve_for_artifact(uow, "commit:art:1")

            created = self.artifact.create_artifact(
                subject_id="chiyo", workspace_id=workspace_id, title="第一个作品",
                operation_key="op:create:1", grant_reservation_id=reservation_id)
            uow.step(created, owner="ManagedArtifact", action="create_artifact")
            self.assertEqual(created["status"], "OK", created)
            artifact_id = created["artifact_id"]
            uow.param("artifact_id", artifact_id)
            uow.note("ManagedArtifact", "managed_artifacts", "created", artifact_id)

            committed = self.artifact.commit_markdown(
                artifact_id=artifact_id, authorized_subject_id="chiyo", expected_head=None,
                content="# 第一版\n\n她自己留下的东西。", operation_key="op:commit:1",
                grant_reservation_id=reservation_id)
            uow.step(committed, owner="ManagedArtifact", action="commit_markdown")
            self.assertEqual(committed["status"], "OK", committed)
            self.assertEqual(committed["version_number"], 1)
            self.assertEqual(committed["parent_version_id"], None)
            uow.note("ManagedArtifact", "artifact_versions", "v1 committed",
                     committed["version_id"])

        self.assertEqual(uow.participants, ["Governance", "ManagedArtifact"])
        self.assertEqual(self._count("managed_artifacts"), 1)
        self.assertEqual(self._count("artifact_versions"), 1)
        self.assertEqual(self._count("artifact_effect_receipts"), 2)  # create + commit

        head = self.artifact.read_head(
            self.artifact.list_artifacts("chiyo")[0]["artifact_id"])
        self.assertEqual(head["version_number"], 1)
        self.assertEqual(head["parent_version_id"], None)

    def test_artifact_denial_rolls_back_the_version(self) -> None:
        """A refused commit must not leave a version, a receipt or an artifact behind."""
        decision = self._issue()
        self.workspace.provision_workspace(subject_id="chiyo", grant_id=decision.grant_id,
                                           operation_key="op:provision:art2")
        workspace_id = self.workspace.get_workspace("chiyo")["workspace_id"]

        with self.assertRaises(UnitOfWorkDoomed):
            with self.db.unit_of_work(command="COMMIT_MARKDOWN", operation_id="op:commit:2",
                                      subject_id="chiyo") as uow:
                created = self.artifact.create_artifact(
                    subject_id="chiyo", workspace_id=workspace_id, title="x",
                    operation_key="op:create:2", grant_reservation_id="reserve:art:2")
                uow.step(created, owner="ManagedArtifact", action="create_artifact")
                self.assertEqual(created["status"], "OK", created)
                # a DENY from a participant: the step guard dooms the coordinated command
                denied = self.artifact.create_artifact(
                    subject_id="someone-else", workspace_id=workspace_id, title="y",
                    operation_key="op:create:3", grant_reservation_id="reserve:art:2")
                uow.step(denied, owner="ManagedArtifact", action="create_artifact")
                self.assertEqual(denied["status"], "DENY", denied)

        self.assertEqual(self._count("managed_artifacts"), 0)
        self.assertEqual(self._count("artifact_versions"), 0)
        self.assertEqual(self._count("life_supply_uow_journal"), 0)

    # -- C9 PHASE 02: the contract is what allows COMMIT ------------------------

    def test_command_without_its_required_steps_cannot_commit(self) -> None:
        """"No exception" is not success: a missing required step blocks the commit."""
        with self.assertRaises(CommandContractViolation):
            with self.db.unit_of_work(command="PROVISION_WORKSPACE",
                                      operation_id="op:incomplete",
                                      subject_id="chiyo") as uow:
                uow.param("subject_id", "chiyo")
                decision = self._issue(operation_id="issue:incomplete")
                uow.step(decision, owner="Governance", action="issue_grant")  # not a step here
                self.assertEqual(self._count("permission_grants"), 1)
        # the whole command rolled back, including the grant it did manage to write
        self.assertEqual(self._count("permission_grants"), 0)
        self.assertEqual(self._count("life_supply_uow_journal"), 0)

    def test_postcondition_failure_blocks_the_commit(self) -> None:
        """Even with every step registered, a false postcondition stops the command."""
        decision = self._issue()
        grant_id = decision.grant_id
        with self.assertRaises(CommandContractViolation):
            with self.db.unit_of_work(command="PROVISION_WORKSPACE",
                                      operation_id="op:no-workspace",
                                      subject_id="chiyo") as uow:
                # deliberately omit param("subject_id") so workspace_active cannot be proven,
                # while still registering every step the contract requires
                uow.param("grant_id", grant_id)
                validated = self.gov.validate_grant(
                    grant_id, subject="chiyo", capability=CAPABILITY, scope=SCOPE,
                    target=TARGET, expected_revision=None)
                uow.step(validated, owner="Governance", action="validate_grant")
                reserved = self.gov.reserve_use(
                    grant_id, subject="chiyo", capability=CAPABILITY, scope=SCOPE,
                    target=TARGET, action_key="provision:post")
                uow.step(reserved, owner="Governance", action="reserve_use")
                uow.param("reservation_id", reserved.reservation_id)
                begun = self.gov.begin_execution(reserved.reservation_id, actor_uid=None)
                uow.step(begun, owner="Governance", action="begin_execution")
                consumed = self.gov.consume_use(reserved.reservation_id, actor_uid=None)
                uow.step(consumed, owner="Governance", action="consume_use")
                created = self.workspace.provision_workspace(
                    subject_id="chiyo", grant_id=grant_id, operation_key="op:no-workspace")
                uow.step(created, owner="Workspace", action="provision_workspace")

        self.assertEqual(self._count("workspaces"), 0)
        self.assertEqual(self._count("life_supply_uow_journal"), 0)

    # -- the boundary still holds ---------------------------------------------

    def test_store_cannot_run_ddl_inside_a_coordinated_command(self) -> None:
        with self.db.unit_of_work(command="ISSUE_GRANT", operation_id="op:ddl") as uow:
            with self.assertRaises(RuntimeError):
                self.workspace._init_db()
            decision = self._issue(operation_id="issue:ddl")
            uow.step(decision, owner="Governance", action="issue_grant")
            uow.param("grant_id", decision.grant_id)

    def test_standalone_store_still_creates_its_own_schema_when_not_bound(self) -> None:
        """The C6 per-file path must keep working: not every caller is on the single DB."""
        legacy = WorkspaceStore(self.root / "legacy-workspace.sqlite", clock=self.clock)
        legacy.provision_workspace(subject_id="chiyo", grant_id="g", operation_key="op")
        self.assertEqual(legacy.workspace_count(), 1)
        self.assertTrue((self.root / "legacy-workspace.sqlite").is_file())


if __name__ == "__main__":
    unittest.main()
