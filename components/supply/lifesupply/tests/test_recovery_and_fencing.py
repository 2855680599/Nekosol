"""C10 PHASE 08 (four crash windows) and PHASE 09 (writer fencing).

PHASE 08 demands REAL process death.  A ``raise`` inside the test process would let
``LifeSupplyUnitOfWork.__exit__`` roll back -- which is precisely the thing under test -- so
every case runs in its own subprocess that dies with ``os._exit(9)`` at the chosen instant.
The child never runs an interpreter shutdown handler, so nothing is unwound on its behalf.

The four windows and what the parent proves afterwards:

  C1  died before any transaction started  -> no residue at all
  C2  first owner wrote, died before COMMIT -> no residue: not a row, not a journal entry
  C3  COMMIT succeeded, died before the reply -> the result is explicitly there and findable,
      and re-requesting the same operation does not create a second effect
  C4  COMMIT succeeded, died before the projection finished -> the projection is rebuildable
      and NO duplicate ArtifactVersion appears

PHASE 09 is checked at the level where it is decidable: a participating owner whose writer has
been superseded must not be able to have its refusal reported as success, and the whole
coordinated command must roll back.  A read-only open must not advance any writer epoch.

Requires POSIX (``fcntl``) because the writer fences are flock-based; run on Linux.
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from lifesupply.db import OWNERS, LifeSupplyDB, UnitOfWorkDoomed
from lifesupply.fencing import WriterFence
from unittest.mock import patch

REPO_ROOT = str(Path(__file__).resolve().parents[2])

#: The child program.  It takes the case name in ``LS_CASE`` and the root in ``LS_ROOT``.
CHILD = r'''
import os, pathlib, sys

from lifesupply.artifact.store import ManagedArtifactStore
from lifesupply.db import OWNERS, LifeSupplyDB
from lifesupply.fencing import WriterFence
from lifesupply.governance.authority import GovernanceAuthority
from lifesupply.workspace.store import WorkspaceStore

root = pathlib.Path(os.environ["LS_ROOT"])
case = os.environ["LS_CASE"]
fences = {owner: WriterFence(owner, root / "fences") for owner in OWNERS}
db = LifeSupplyDB(root / "life_supply.sqlite", fences=fences)
gov = GovernanceAuthority(db=db, writer_fence=fences["Governance"])
ws = WorkspaceStore(db=db, writer_fence=fences["Workspace"])
art = ManagedArtifactStore(db=db, projection_root=root / "projection",
                           workspace_authority=ws, writer_fence=fences["ManagedArtifact"])

GRANT = dict(grantor="HOST_OPERATOR", bootstrap_authorized=True, subject="chiyo",
             capability="WRITE_OPEN_INQUIRY", scope="inquiry:personal",
             target="workspace:chiyo", authority_scope="life-supply.workspace.v1",
             use_mode="REUSABLE")


def issue(operation_id):
    decision = gov.issue_grant(operation_id=operation_id, **GRANT)
    assert decision.status == "ALLOW", decision
    return decision


if case == "C1":
    # died before any transaction started
    os._exit(9)

if case == "C2":
    with db.unit_of_work(command="ISSUE_GRANT", operation_id="op:crash:2",
                         subject_id="chiyo") as uow:
        decision = issue("issue:crash:2")
        uow.step(decision, owner="Governance", action="issue_grant")
        uow.param("grant_id", decision.grant_id)
        # every required step is registered; the unit of work simply never commits
        os._exit(9)

if case == "C3":
    with db.unit_of_work(command="ISSUE_GRANT", operation_id="op:crash:3",
                         subject_id="chiyo") as uow:
        decision = issue("issue:crash:3")
        uow.step(decision, owner="Governance", action="issue_grant")
        uow.param("grant_id", decision.grant_id)
    # committed; the reply never reached anyone
    os._exit(9)

if case == "C4":
    provisioned = ws.provision_workspace(subject_id="chiyo", grant_id="grant:crash:4",
                                         operation_key="op:ws:4")
    assert provisioned["status"] == "OK", provisioned
    workspace_id = provisioned["workspace"]["workspace_id"]
    created = art.create_artifact(subject_id="chiyo", workspace_id=workspace_id,
                                  title="crash window 4", operation_key="op:create:4",
                                  grant_reservation_id="res:crash:4")
    assert created["status"] == "OK", created
    # the projection is the step AFTER the commit: replacing it with a no-op is exactly a
    # process dying once the row is durable and before the markdown is written
    art._update_projection = lambda *a, **k: None
    committed = art.commit_markdown(artifact_id=created["artifact_id"],
                                    authorized_subject_id="chiyo", expected_head=None,
                                    content="# crash window 4\n",
                                    operation_key="op:commit:4",
                                    grant_reservation_id="res:crash:4")
    assert committed["status"] == "OK", committed
    os._exit(9)

raise SystemExit(f"unknown case {case}")
'''


class CrashWindowTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        # create the canonical database once; the child processes inherit it
        fences = {owner: WriterFence(owner, self.root / "fences") for owner in OWNERS}
        LifeSupplyDB(self.root / "life_supply.sqlite", fences=fences).layout_report()
        for fence in fences.values():
            fence.release()
        child = self.root / "crash_child.py"
        child.write_text(CHILD, encoding="utf-8")
        self.child = child

    def tearDown(self) -> None:
        self._tmp.cleanup()

    # -- helpers ----------------------------------------------------------------

    def _crash(self, case: str) -> None:
        env = dict(os.environ, LS_CASE=case, LS_ROOT=str(self.root),
                   PYTHONPATH=REPO_ROOT)
        completed = subprocess.run([sys.executable, str(self.child)], env=env,
                                   capture_output=True, text=True, timeout=120)
        self.assertEqual(completed.returncode, 9,
                         f"the child did not die as intended: {completed.returncode} "
                         f"stdout={completed.stdout} stderr={completed.stderr}")

    def _count(self, table: str) -> int:
        con = sqlite3.connect(f"file:{self.root / 'life_supply.sqlite'}?mode=ro", uri=True)
        try:
            return int(con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        finally:
            con.close()

    def _scalar(self, sql: str, args: tuple = ()) -> object:
        con = sqlite3.connect(f"file:{self.root / 'life_supply.sqlite'}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        try:
            row = con.execute(sql, args).fetchone()
            return None if row is None else row[0]
        finally:
            con.close()

    def _stores(self):
        """A fresh writer instance, as a restarted service would build."""
        fences = {owner: WriterFence(owner, self.root / "fences") for owner in OWNERS}
        db = LifeSupplyDB(self.root / "life_supply.sqlite", fences=fences)
        from lifesupply.artifact.store import ManagedArtifactStore
        from lifesupply.governance.authority import GovernanceAuthority
        from lifesupply.workspace.store import WorkspaceStore
        gov = GovernanceAuthority(db=db, writer_fence=fences["Governance"])
        ws = WorkspaceStore(db=db, writer_fence=fences["Workspace"])
        art = ManagedArtifactStore(db=db, projection_root=self.root / "projection",
                                   workspace_authority=ws, writer_fence=fences["ManagedArtifact"])
        return db, fences, gov, ws, art

    # -- C1 ---------------------------------------------------------------------

    def test_c1_a_death_before_the_transaction_leaves_no_residue(self) -> None:
        self._crash("C1")
        for table in ("permission_grants", "permission_reservations", "workspaces",
                      "managed_artifacts", "artifact_versions", "life_supply_uow_journal"):
            self.assertEqual(self._count(table), 0, table)
        self.assertEqual(self._count("c6_writer_epoch"), len(OWNERS),
                         "the owner epoch rows are the only thing a startup must leave")

    # -- C2 ---------------------------------------------------------------------

    def test_c2_a_death_before_commit_leaves_no_half_written_owner(self) -> None:
        self._crash("C2")
        # the Governance row was written inside the transaction, so it must be gone
        self.assertEqual(self._count("permission_grants"), 0)
        self.assertEqual(self._count("authority_operations"), 0)
        self.assertEqual(self._count("life_supply_uow_journal"), 0)

    # -- C3 ---------------------------------------------------------------------

    def test_c3_a_committed_command_is_findable_and_replays_without_a_second_effect(self) -> None:
        self._crash("C3")
        # the result is explicit, not inferred
        self.assertEqual(self._count("permission_grants"), 1)
        self.assertEqual(self._count("life_supply_uow_journal"), 1)
        journal = self._scalar("SELECT outcome FROM life_supply_uow_journal")
        self.assertEqual(journal, "COMMITTED")

        db, fences, gov, _ws, _art = self._stores()
        try:
            ledger = gov.lookup_operation("issue:crash:3")
            self.assertIsNotNone(ledger, "the committed operation must be look-up-able")
            recorded = json.loads(ledger["result_json"])
            self.assertEqual(recorded["status"], "ALLOW")
            grant_id = recorded["grant_id"]

            # re-requesting the SAME operation must replay history, not create a second grant
            again = gov.issue_grant(operation_id="issue:crash:3",
                                    grantor="HOST_OPERATOR", bootstrap_authorized=True,
                                    subject="chiyo", capability="WRITE_OPEN_INQUIRY",
                                    scope="inquiry:personal", target="workspace:chiyo",
                                    authority_scope="life-supply.workspace.v1",
                                    use_mode="REUSABLE")
            self.assertEqual(again.status, "ALLOW")
            self.assertEqual(again.grant_id, grant_id)
            self.assertEqual(self._count("permission_grants"), 1)
        finally:
            for fence in fences.values():
                fence.release()

    # -- C4 ---------------------------------------------------------------------

    def test_c4_a_projection_interrupted_after_commit_is_rebuildable_without_a_duplicate(self) -> None:
        self._crash("C4")
        # the canonical rows are durable ...
        self.assertEqual(self._count("managed_artifacts"), 1)
        self.assertEqual(self._count("artifact_versions"), 1)
        artifact_id = self._scalar("SELECT artifact_id FROM managed_artifacts")
        head = self._scalar("SELECT head_version_id FROM managed_artifacts")
        self.assertIsNotNone(head, "the committed head must survive the crash")
        # ... and the projection is NOT finished: the outbox row is still DIRTY
        self.assertEqual(self._scalar("SELECT state FROM artifact_projection_outbox"), "DIRTY")

        db, fences, _gov, _ws, art = self._stores()
        try:
            before = art.check_projection(artifact_id)
            self.assertNotEqual(before["status"], "OK",
                                "a projection that never finished must not read as consistent")

            recovered = art.recover_dirty_projections()
            self.assertEqual(recovered["status"], "OK")
            self.assertEqual(recovered["recovered"], [artifact_id])

            after = art.check_projection(artifact_id)
            self.assertEqual(after["status"], "OK", after)
            self.assertEqual(after["expected_sha256"], after["actual_sha256"])
            self.assertEqual(self._scalar("SELECT state FROM artifact_projection_outbox"), "CLEAN")

            # recovery must REPAIR, never re-commit: no second version, no moved head
            self.assertEqual(self._count("artifact_versions"), 1)
            self.assertEqual(self._scalar("SELECT head_version_id FROM managed_artifacts"), head)
        finally:
            for fence in fences.values():
                fence.release()

    # -- PHASE 09 ---------------------------------------------------------------

    def test_a_superseded_participant_cannot_be_reported_as_success(self) -> None:
        """A store whose writer has been superseded must doom the whole coordinated command."""
        fences = {owner: WriterFence(owner, self.root / "fences") for owner in OWNERS}
        db = LifeSupplyDB(self.root / "life_supply.sqlite", fences=fences)
        from lifesupply.governance.authority import GovernanceAuthority
        from lifesupply.workspace.store import WorkspaceStore
        gov = GovernanceAuthority(db=db, writer_fence=fences["Governance"])
        ws = WorkspaceStore(db=db, writer_fence=fences["Workspace"])
        try:
            with self.assertRaises(UnitOfWorkDoomed) as ctx:
                with db.unit_of_work(command="PROVISION_WORKSPACE",
                                     operation_id="op:fence:1", subject_id="chiyo") as uow:
                    decision = gov.issue_grant(
                        grantor="HOST_OPERATOR", bootstrap_authorized=True, subject="chiyo",
                        capability="PROVISION_PERSONAL_WORKSPACE", scope="workspace:personal",
                        target="workspace:chiyo", authority_scope="life-supply.workspace.v1",
                        use_mode="ONE_SHOT", operation_id="issue:fence:1")
                    self.assertEqual(decision.status, "ALLOW")
                    uow.step(decision, owner="Governance", action="issue_grant")

                    # another writer instance takes the Workspace owner over mid-command
                    uow.con.execute(
                        "UPDATE c6_writer_epoch SET active_epoch=active_epoch+100,"
                        "instance_id='other-writer',writer_token='other-token'"
                        " WHERE owner='Workspace'")
                    # every required step is registered, so a refusal here is the fence talking
                    for action in ("validate_grant", "reserve_use", "begin_execution",
                                   "consume_use"):
                        uow.step({"status": "ALLOW"}, owner="Governance", action=action)
                    uow.param("subject_id", "chiyo")
                    uow.param("grant_id", decision.grant_id)
                    uow.param("reservation_id", "res:fence:1")
                    uow.param("operation_key", "op:fence:1")
                    refused = ws.provision_workspace(subject_id="chiyo",
                                                     grant_id=decision.grant_id,
                                                     operation_key="op:fence:1")
                    self.assertNotEqual(refused["status"], "OK",
                                        "a superseded writer must not succeed")
                    uow.step(refused, owner="Workspace", action="provision_workspace")
            self.assertIn("provision_workspace", str(ctx.exception))

            # nothing from the doomed command survived -- not the grant, not the journal row
            self.assertEqual(self._count("permission_grants"), 0)
            self.assertEqual(self._count("workspaces"), 0)
            self.assertEqual(self._count("life_supply_uow_journal"), 0)
        finally:
            for fence in fences.values():
                fence.release()

    def test_a_read_only_open_does_not_advance_any_writer_epoch(self) -> None:
        fences = {owner: WriterFence(owner, self.root / "fences") for owner in OWNERS}
        LifeSupplyDB(self.root / "life_supply.sqlite", fences=fences)
        for fence in fences.values():
            fence.release()

        before = {}
        con = sqlite3.connect(f"file:{self.root / 'life_supply.sqlite'}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        try:
            before = {row["owner"]: (row["active_epoch"], row["instance_id"], row["writer_token"])
                      for row in con.execute("SELECT * FROM c6_writer_epoch")}
        finally:
            con.close()

        ro = LifeSupplyDB(self.root / "life_supply.sqlite", read_only=True)
        report = ro.layout_report()
        self.assertTrue(report["single_file"])
        from lifesupply.artifact.store import ManagedArtifactStore
        artifact = ManagedArtifactStore(db=ro, projection_root=self.root / "projection",
                                        read_only=True)
        self.assertEqual(artifact.list_artifacts("chiyo"), [])
        self.assertEqual(artifact.artifact_count(), 0)

        con = sqlite3.connect(f"file:{self.root / 'life_supply.sqlite'}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        try:
            after = {row["owner"]: (row["active_epoch"], row["instance_id"], row["writer_token"])
                     for row in con.execute("SELECT * FROM c6_writer_epoch")}
        finally:
            con.close()
        self.assertEqual(after, before, "a read-only open must not take a write lease")


class LegacyThreeDatabaseGateTests(unittest.TestCase):
    """PHASE 06: after the merge, the pre-single-database layout must not be reachable by accident.

    It would otherwise be a second owner-and-recovery layout for the same state, and PHASE 06
    forbids two competing recovery logics.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        fences = {owner: WriterFence(owner, self.root / "fences") for owner in OWNERS}
        LifeSupplyDB(self.root / "life_supply.sqlite", fences=fences).layout_report()
        for fence in fences.values():
            fence.release()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_the_legacy_layout_refuses_a_single_database_root(self) -> None:
        from lifesupply.service.server import run
        with self.assertRaises(RuntimeError) as ctx:
            run("governance", str(self.root / "legacy.sock"), str(self.root))
        self.assertIn("LEGACY_THREE_DB_REFUSED", str(ctx.exception))
        # and it must have refused BEFORE creating a socket
        self.assertFalse((self.root / "legacy.sock").exists())

    def test_the_legacy_layout_is_reachable_only_through_an_explicit_opt_in(self) -> None:
        from lifesupply.service.server import LEGACY_THREE_DB_OPT_IN, _legacy_layout_refusal
        self.assertIsNotNone(_legacy_layout_refusal(str(self.root)))
        with patch.dict(os.environ, {LEGACY_THREE_DB_OPT_IN: "ON"}):
            self.assertIsNone(_legacy_layout_refusal(str(self.root)))
        # a value that merely looks like consent is not consent
        with patch.dict(os.environ, {LEGACY_THREE_DB_OPT_IN: "1"}):
            self.assertIsNotNone(_legacy_layout_refusal(str(self.root)))

    def test_the_cli_legacy_entry_point_declares_its_own_opt_in(self) -> None:
        from lifesupply.service import cli
        saved = os.environ.pop("LIFE_SUPPLY_ALLOW_LEGACY_THREE_DB", None)
        try:
            # the launcher must not depend on the ambient environment for its own opt-in
            with self.assertRaises(SystemExit):
                cli._legacy_split_service(["--mode", "governance",
                                           "--socket", str(self.root / "legacy.sock"),
                                           "--data-root", str(self.root)])
            self.assertEqual(os.environ.get("LIFE_SUPPLY_ALLOW_LEGACY_THREE_DB"), "ON")
        finally:
            if saved is None:
                os.environ.pop("LIFE_SUPPLY_ALLOW_LEGACY_THREE_DB", None)
            else:
                os.environ["LIFE_SUPPLY_ALLOW_LEGACY_THREE_DB"] = saved


if __name__ == "__main__":
    unittest.main()