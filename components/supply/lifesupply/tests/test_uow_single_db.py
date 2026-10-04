"""C7 PHASE 01/02 + C9 PHASE 02 tests -- one file, three owners, one contract-bound UoW.

C7 proved the mechanism (one physical file, per-owner guards, one transaction, a command
journal).  C9 added the per-command step contract, so every test that wants to COMMIT now has
to satisfy that command's contract -- which is exactly the point: "no exception was raised" is
no longer enough to be reported as a successful coordinated command.

Requires POSIX (``fcntl``) because the writer fences are flock-based; run on Linux.
"""
from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from lifesupply.db import (
    CROSS_OWNER_COMMANDS,
    OWNERS,
    OWNER_TABLES,
    TABLE_OWNER,
    CommandContractViolation,
    CrossOwnerCommandRefused,
    JoinedConnectionExpired,
    LifeSupplyDB,
    NestedUnitOfWorkRefused,
    OwnerTableMismatch,
    UnitOfWorkDoomed,
    register_owner_writer_connection,
)
from lifesupply.fencing import WriterFence

GRANT_INSERT = (
    "INSERT INTO permission_grants(grant_id,grantor,subject,capability,scope,target,"
    "payload_binding,valid_from,expires_at,revision,revocation_state,revoked_at,use_mode,"
    "max_uses,uses,delegation_allowed,authority_scope,created_at,meaning_version) "
    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
)

WORKSPACE_INSERT = (
    "INSERT INTO workspaces(workspace_id,subject_id,lifecycle,visibility,artifact_scope,"
    "created_by_grant,created_at,updated_at,meaning_version,operation_id) "
    "VALUES(?,?,?,?,?,?,?,?,?,?)"
)

RESERVATION_INSERT = (
    "INSERT INTO permission_reservations(reservation_id,grant_id,action_key,action_digest,"
    "grant_revision,reservation_owner_uid,execution_started_at,state,created_at,updated_at) "
    "VALUES(?,?,?,?,?,?,?,?,?,?)"
)


def grant_values(grant_id: str = "grant:test") -> tuple:
    return (
        grant_id, "operator:host", "chiyo", "PROVISION_PERSONAL_WORKSPACE",
        "life-supply.workspace.v1", "workspace:chiyo", None,
        1000.0, None, 1, "ACTIVE", None, "ONE_SHOT", 1, 0, 0,
        "life-supply.workspace.v1", 1000.0, "lifesupply.grant.v1",
    )


def workspace_values(workspace_id: str = "ws:1", subject: str = "chiyo",
                     grant_id: str = "grant:test", operation_id: str = "") -> tuple:
    return (workspace_id, subject, "ACTIVE", "PRIVATE", "PERSONAL",
            grant_id, 1000.0, 1000.0, "lifesupply.workspace.v1", operation_id)


def satisfy_provision(uow, con, *, subject: str = "chiyo",
                      reservation_id: str = "res:contract:1",
                      grant_id: str = "grant:contract",
                      workspace_id: str = "ws:contract",
                      operation_key: str | None = None) -> None:
    """Register the rows + steps + parameters that PROVISION_WORKSPACE's contract requires.

    C10 PHASE 03: the raw rows carry this command's operation identity, because the
    postcondition binds to THIS operation -- an older ACTIVE workspace must not satisfy a new
    provision, and a workspace created by another grant must not satisfy this one.
    """
    key = operation_key or uow.operation_id
    con.execute(GRANT_INSERT, grant_values(grant_id))
    con.execute(WORKSPACE_INSERT, workspace_values(workspace_id, subject, grant_id, key))
    con.execute(RESERVATION_INSERT,
                (reservation_id, grant_id, "provision:contract", "digest", 1, None,
                 1000.0, "CONSUMED", 1000.0, 1000.0))
    for action in ("validate_grant", "reserve_use", "begin_execution", "consume_use"):
        uow.step({"status": "ALLOW"}, owner="Governance", action=action)
    uow.step({"status": "OK"}, owner="Workspace", action="provision_workspace")
    uow.param("subject_id", subject)
    uow.param("grant_id", grant_id)
    uow.param("reservation_id", reservation_id)
    uow.param("operation_key", key)


def satisfy_issue_grant(uow, con, *, grant_id: str = "grant:issue") -> None:
    """Register the rows + step + parameter that ISSUE_GRANT's contract requires."""
    con.execute(GRANT_INSERT, grant_values(grant_id))
    uow.step({"status": "ALLOW"}, owner="Governance", action="issue_grant")
    uow.param("grant_id", grant_id)


class SingleDatabaseLayoutTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.db_path = self.root / "life_supply.sqlite"
        self.fences = {owner: WriterFence(owner, self.root / "fences") for owner in OWNERS}
        self.db = LifeSupplyDB(self.db_path, fences=self.fences)

    def tearDown(self) -> None:
        for fence in self.fences.values():
            fence.release()
        self._tmp.cleanup()

    def _count(self, table: str) -> int:
        with self.db.connection() as con:
            return int(con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])

    # -- PHASE 01 --------------------------------------------------------------

    def test_one_file_holds_all_three_owners(self) -> None:
        """Exactly one canonical file; every owner's tables and epoch row live in it."""
        self.assertTrue(self.db_path.is_file())
        report = self.db.layout_report()

        self.assertEqual(report["layout"], "life-supply.single-db.v1")
        self.assertEqual(report["owners"], list(OWNERS))

        for owner, tables in OWNER_TABLES.items():
            self.assertEqual(report["owner_tables"][owner], list(tables))
            for table in tables:
                self.assertIn(table, report["tables"],
                              f"{table} missing from the single database ({owner})")

        self.assertEqual(sorted(report["writer_epoch_rows"]), sorted(OWNERS))
        for owner in OWNERS:
            self.assertGreaterEqual(report["writer_epoch_rows"][owner]["active_epoch"], 1)
            self.assertEqual(report["writer_epoch_rows"][owner]["instance_id"],
                             self.fences[owner].instance_id)
            self.assertEqual(report["writer_epoch_rows"][owner]["writer_token"],
                             self.fences[owner].writer_token)

        expected_guards = {f"c7_fence_{table}_{action.lower()}"
                           for table in TABLE_OWNER for action in ("INSERT", "UPDATE", "DELETE")}
        self.assertTrue(expected_guards.issubset(set(report["guard_triggers"])),
                        sorted(expected_guards - set(report["guard_triggers"])))

        for view in ("grants", "grant_operations", "inquiries", "inquiry_operations",
                     "artifacts", "artifact_receipts", "writer_epoch"):
            self.assertIn(view, report["logical_views"], view)

        self.assertEqual(sorted(p.name for p in self.root.glob("*.sqlite")),
                         ["life_supply.sqlite"])

    def test_no_separate_owner_databases_are_created(self) -> None:
        self.assertFalse((self.root / "governance.sqlite").exists())
        self.assertFalse((self.root / "workspace.sqlite").exists())
        self.assertFalse((self.root / "artifact.sqlite").exists())

    def test_per_owner_tables_are_disjoint(self) -> None:
        seen: dict[str, str] = {}
        for owner, tables in OWNER_TABLES.items():
            for table in tables:
                self.assertNotIn(table, seen, f"{table} claimed by {seen.get(table)} and {owner}")
                seen[table] = owner
        self.assertEqual(len(seen), len(TABLE_OWNER))

    # -- PHASE 02 --------------------------------------------------------------

    def test_coordinated_command_is_one_atomic_commit(self) -> None:
        """Owners' facts + journal row land in one transaction, and participants are named."""
        with self.db.unit_of_work(command="PROVISION_WORKSPACE",
                                  operation_id="op:provision:1",
                                  subject_id="chiyo") as uow:
            satisfy_provision(uow, uow.con)

        self.assertEqual(uow.participants, ["Governance", "Workspace"])
        self.assertEqual(self._count("permission_grants"), 1)
        self.assertEqual(self._count("workspaces"), 1)
        self.assertEqual(self._count("life_supply_uow_journal"), 1)

        with self.db.connection() as con:
            row = con.execute("SELECT command,operation_id,participants,outcome "
                              "FROM life_supply_uow_journal").fetchone()
        self.assertEqual(row["command"], "PROVISION_WORKSPACE")
        self.assertEqual(row["operation_id"], "op:provision:1")
        self.assertEqual(row["outcome"], "COMMITTED")
        self.assertIn("Governance", row["participants"])
        self.assertIn("Workspace", row["participants"])

    def test_cross_owner_failure_leaves_no_trace_in_any_owner(self) -> None:
        """A fault after the first owner's rows leaves NO half-committed effect."""
        with self.assertRaises(sqlite3.IntegrityError):
            with self.db.unit_of_work(command="PROVISION_WORKSPACE",
                                      operation_id="op:provision:2",
                                      subject_id="chiyo") as uow:
                uow.con.execute(GRANT_INSERT, grant_values("grant:half"))
                # NOT NULL violation, i.e. a fault after Governance already wrote
                uow.con.execute("INSERT INTO workspaces(workspace_id) VALUES('broken')")

        self.assertEqual(self._count("permission_grants"), 0)
        self.assertEqual(self._count("workspaces"), 0)
        self.assertEqual(self._count("life_supply_uow_journal"), 0)

    def test_missing_required_steps_block_the_commit(self) -> None:
        """C9 PHASE 02: a command missing its required steps must not be reported as success."""
        with self.assertRaises(CommandContractViolation):
            with self.db.unit_of_work(command="PROVISION_WORKSPACE",
                                      operation_id="op:incomplete",
                                      subject_id="chiyo") as uow:
                uow.con.execute(GRANT_INSERT, grant_values("grant:incomplete"))
        self.assertEqual(self._count("permission_grants"), 0)
        self.assertEqual(self._count("life_supply_uow_journal"), 0)

    def test_a_failing_commit_leaves_nothing_behind(self) -> None:
        """The journal row is written INSIDE the transaction, so a rejected command rolls back."""
        with self.db.unit_of_work(command="ISSUE_GRANT", operation_id="op:dup") as uow:
            satisfy_issue_grant(uow, uow.con, grant_id="grant:first")
        self.assertEqual(self._count("life_supply_uow_journal"), 1)
        self.assertEqual(self._count("permission_grants"), 1)

        with self.assertRaises(sqlite3.IntegrityError):
            with self.db.unit_of_work(command="ISSUE_GRANT", operation_id="op:dup") as uow:
                satisfy_issue_grant(uow, uow.con, grant_id="grant:two")
                # duplicate (command, operation_id) -> journal UNIQUE violation at commit

        # the rejected command's owner write is gone, and the first command survives
        self.assertEqual(self._count("permission_grants"), 1)
        self.assertEqual(self._count("life_supply_uow_journal"), 1)

    def test_unlisted_command_cannot_open_a_unit_of_work(self) -> None:
        self.assertNotIn("COMMIT_ANYTHING", CROSS_OWNER_COMMANDS)
        with self.assertRaises(CrossOwnerCommandRefused):
            self.db.unit_of_work(command="COMMIT_ANYTHING", operation_id="op:x")
        with self.assertRaises(CrossOwnerCommandRefused):
            self.db.unit_of_work(command="", operation_id="op:x")
        for allowed in CROSS_OWNER_COMMANDS:
            self.db.unit_of_work(command=allowed, operation_id="op:allowed")  # must not raise

    def test_operation_id_is_required(self) -> None:
        with self.assertRaises(ValueError):
            self.db.unit_of_work(command="ISSUE_GRANT", operation_id="")

    def test_nested_unit_of_work_is_refused(self) -> None:
        with self.db.unit_of_work(command="ISSUE_GRANT", operation_id="op:outer") as uow:
            with self.assertRaises(NestedUnitOfWorkRefused):
                with self.db.unit_of_work(command="ISSUE_GRANT", operation_id="op:inner"):
                    pass
            satisfy_issue_grant(uow, uow.con)

    def test_note_refuses_a_table_owned_by_another_owner(self) -> None:
        """One database must not become one universal owner."""
        with self.db.unit_of_work(command="PROVISION_WORKSPACE", operation_id="op:note") as uow:
            with self.assertRaises(OwnerTableMismatch):
                uow.note("Workspace", "managed_artifacts", "wrong owner")
            with self.assertRaises(OwnerTableMismatch):
                uow.note("Governance", "workspaces", "wrong owner")
            with self.assertRaises(OwnerTableMismatch):
                uow.note("Governance", "not_a_table", "unknown")
            uow.note("Governance", "permission_grants", "correct owner")
            satisfy_provision(uow, uow.con)

    # -- the boundary still holds ---------------------------------------------

    def test_guard_blocks_exactly_the_fenced_owner(self) -> None:
        """An active fence for one owner blocks its tables and only its tables."""
        with self.db.connection() as con:
            con.execute("BEGIN IMMEDIATE")
            con.execute("UPDATE c6_writer_epoch SET active_epoch=0,instance_id='unfenced-test',"
                        "writer_token='unfenced-test'")
            con.execute("COMMIT")

        con = sqlite3.connect(str(self.db_path), isolation_level=None)
        try:
            con.row_factory = sqlite3.Row
            con.execute("PRAGMA foreign_keys=ON")
            register_owner_writer_connection(con, OWNERS, {})
            con.execute(GRANT_INSERT, grant_values("grant:unfenced"))
            con.execute("BEGIN IMMEDIATE")
            con.execute("UPDATE c6_writer_epoch SET active_epoch=7,instance_id='other',"
                        "writer_token='other-token' WHERE owner='Workspace'")
            con.execute("COMMIT")
            with self.assertRaises(sqlite3.IntegrityError) as ctx:
                con.execute(WORKSPACE_INSERT, workspace_values("ws:x"))
            self.assertIn("STALE_WRITER", str(ctx.exception))
            con.execute(GRANT_INSERT, grant_values("grant:still-ok"))
        finally:
            con.close()

        self.assertEqual(self._count("permission_grants"), 2)
        self.assertEqual(self._count("workspaces"), 0)

    def test_read_only_verification_mode(self) -> None:
        ro = LifeSupplyDB(self.db_path, read_only=True)
        report = ro.layout_report()
        self.assertTrue(report["single_file"])
        self.assertEqual(report["owners"], list(OWNERS))

    # -- C10 PHASE 02: the joined connection dies with its unit of work --------------

    def test_joined_connection_is_dead_after_the_unit_of_work(self) -> None:
        """A store that kept its joined connection must fail loudly once the UoW finished."""
        uow = self.db.unit_of_work(command="ISSUE_GRANT", operation_id="op:life:1")
        with uow:
            self.assertTrue(uow.is_live)
            joined = uow.join()
            satisfy_issue_grant(uow, uow.con, grant_id="grant:life")
            self.assertEqual(joined.execute("SELECT COUNT(*) FROM permission_grants")
                             .fetchone()[0], 1)

        self.assertFalse(uow.is_live)
        with self.assertRaises(JoinedConnectionExpired):
            joined.execute("SELECT 1")
        with self.assertRaises(JoinedConnectionExpired):
            joined.executescript("SELECT 1")
        with self.assertRaises(JoinedConnectionExpired):
            joined.commit()
        with self.assertRaises(JoinedConnectionExpired):
            joined.rollback()
        joined.close()  # the UoW owns the lifetime: close() stays a no-op

    def test_one_unit_of_work_cannot_be_entered_twice(self) -> None:
        uow = self.db.unit_of_work(command="ISSUE_GRANT", operation_id="op:life:2")
        with uow:
            satisfy_issue_grant(uow, uow.con, grant_id="grant:twice")
        with self.assertRaises(UnitOfWorkDoomed):
            with uow:
                pass

    # -- C10 PHASE 03: a postcondition binds to THIS operation -----------------------

    def test_an_older_workspace_cannot_satisfy_a_new_provision(self) -> None:
        """`exactly one ACTIVE workspace exists` is not enough; it must be THIS operation's."""
        with self.db.unit_of_work(command="PROVISION_WORKSPACE",
                                  operation_id="op:first",
                                  subject_id="chiyo") as uow:
            satisfy_provision(uow, uow.con, reservation_id="res:first")
        self.assertEqual(self._count("workspaces"), 1)

        # A second provision that writes NO workspace row must not commit by pointing at the
        # first operation's workspace: the binding, not the row count, is what proves it.
        with self.assertRaises(CommandContractViolation) as ctx:
            with self.db.unit_of_work(command="PROVISION_WORKSPACE",
                                      operation_id="op:second",
                                      subject_id="chiyo") as uow:
                con = uow.con
                con.execute(GRANT_INSERT, grant_values("grant:second"))
                con.execute(RESERVATION_INSERT,
                            ("res:second", "grant:second", "provision:second", "digest", 1,
                             None, 1000.0, "CONSUMED", 1000.0, 1000.0))
                for action in ("validate_grant", "reserve_use", "begin_execution", "consume_use"):
                    uow.step({"status": "ALLOW"}, owner="Governance", action=action)
                uow.step({"status": "OK"}, owner="Workspace", action="provision_workspace")
                uow.param("subject_id", "chiyo")
                uow.param("grant_id", "grant:second")
                uow.param("reservation_id", "res:second")
                uow.param("operation_key", "op:second")
        self.assertIn("workspace_active", str(ctx.exception))
        self.assertEqual(self._count("workspaces"), 1)
        self.assertEqual(self._count("permission_grants"), 1)
        self.assertEqual(self._count("life_supply_uow_journal"), 1)

    def test_a_workspace_created_by_another_grant_is_not_this_operation(self) -> None:
        with self.assertRaises(CommandContractViolation) as ctx:
            with self.db.unit_of_work(command="PROVISION_WORKSPACE",
                                      operation_id="op:grant",
                                      subject_id="chiyo") as uow:
                satisfy_provision(uow, uow.con, grant_id="grant:wanted",
                                  workspace_id="ws:grant")
                uow.con.execute("UPDATE workspaces SET created_by_grant='grant:other'")
        self.assertIn("created by another grant", str(ctx.exception))
        self.assertEqual(self._count("workspaces"), 0)


if __name__ == "__main__":
    unittest.main()
