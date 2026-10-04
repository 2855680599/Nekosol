"""C10 PHASE 04 -- idempotency and the NO_CHANGE boundary.

The frozen rule is: ``same operation + same payload -> replay the original result`` and
``same operation + different payload -> conflict``.  Replay must return HISTORY, never a new
effect.  The specific case the work order calls out is:

    grant issued -> grant revoked -> the ORIGINAL issue operation is retried
    -> the caller must get the historical result, and the grant must stay REVOKED

and, for Workspace, a duplicate provision must neither create a second ACTIVE workspace nor
consume a one-shot grant a second time.  ``NO_CHANGE`` is not a blanket licence to commit
either: a NO_CHANGE result still has to satisfy the command's own contract, and the
PROVISION_WORKSPACE postcondition is bound to THIS operation, so a duplicate cannot commit by
pointing at a workspace some earlier operation created.

Requires POSIX (``fcntl``) because the writer fences are flock-based; run on Linux.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from lifesupply.db import OWNERS, CommandContractViolation, LifeSupplyDB
from lifesupply.fencing import WriterFence
from lifesupply.governance.authority import ALLOW, CONFLICT, GovernanceAuthority
from lifesupply.workspace.store import WorkspaceStore

CAPABILITY = "PROVISION_PERSONAL_WORKSPACE"
SCOPE = "workspace:personal"
TARGET = "workspace:chiyo"


class Clock:
    def __init__(self, value: float = 1000.0) -> None:
        self.value = float(value)

    def __call__(self) -> float:
        return self.value

    def tick(self, seconds: float = 1.0) -> None:
        self.value += float(seconds)


class IdempotencyTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.clock = Clock()
        self.db_path = self.root / "life_supply.sqlite"
        self.fences = {owner: WriterFence(owner, self.root / "fences") for owner in OWNERS}
        self.db = LifeSupplyDB(self.db_path, fences=self.fences, clock=self.clock)
        self.gov = GovernanceAuthority(db=self.db, clock=self.clock,
                                       writer_fence=self.fences["Governance"])
        self.workspace = WorkspaceStore(db=self.db, clock=self.clock,
                                        writer_fence=self.fences["Workspace"])

    def tearDown(self) -> None:
        for fence in self.fences.values():
            fence.release()
        self._tmp.cleanup()

    def _count(self, table: str) -> int:
        with self.db.connection() as con:
            return int(con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])

    def _issue(self, **kw):
        args = dict(grantor="HOST_OPERATOR", bootstrap_authorized=True, subject="chiyo",
                    capability=CAPABILITY, scope=SCOPE, target=TARGET,
                    authority_scope="governance:workspace", use_mode="ONE_SHOT",
                    operation_id="issue:chiyo:1")
        args.update(kw)
        return self.gov.issue_grant(**args)

    # -- the grant replay case the work order names -----------------------------

    def test_replayed_issue_returns_history_and_does_not_reactivate_the_grant(self) -> None:
        first = self._issue(grant_id="grant:idem")
        self.assertEqual(first.status, ALLOW)
        self.assertEqual(first.revision, 1)

        self.clock.tick()
        revoked = self.gov.revoke_grant("grant:idem", expected_revision=1,
                                        operation_id="revoke:grant:idem")
        self.assertEqual(revoked.status, "REVOKED")
        self.assertEqual(self.gov.get_grant("grant:idem")["revocation_state"], "REVOKED")

        # The client retries the ORIGINAL issue operation, payload unchanged.
        self.clock.tick()
        replay = self._issue(grant_id="grant:idem")
        self.assertEqual(replay.status, first.status)
        self.assertEqual(replay.grant_id, first.grant_id)
        self.assertEqual(replay.revision, first.revision)

        # ... and the retry must not have re-activated anything.
        row = self.gov.get_grant("grant:idem")
        self.assertEqual(row["revocation_state"], "REVOKED")
        self.assertEqual(row["revision"], 2, "the revocation must be the last word on revision")
        self.assertEqual(self._count("permission_grants"), 1)

    def test_same_operation_with_a_different_payload_is_a_conflict(self) -> None:
        first = self._issue(grant_id="grant:conflict")
        self.assertEqual(first.status, ALLOW)

        self.clock.tick()
        retried = self._issue(grant_id="grant:conflict", target="workspace:someone-else")
        self.assertEqual(retried.status, CONFLICT)
        # the conflicting retry created nothing
        self.assertEqual(self._count("permission_grants"), 1)
        self.assertEqual(self.gov.get_grant("grant:conflict")["target"], TARGET)

    # -- the duplicate provision case the work order names ----------------------

    def test_duplicate_provision_keeps_one_workspace_and_its_creation_provenance(self) -> None:
        first = self.workspace.provision_workspace(
            subject_id="chiyo", grant_id="grant:a", operation_key="op:provision:1")
        self.assertEqual(first["status"], "OK")
        workspace_id = first["workspace"]["workspace_id"]

        self.clock.tick()
        again = self.workspace.provision_workspace(
            subject_id="chiyo", grant_id="grant:b", operation_key="op:provision:2")
        self.assertEqual(again["status"], "NO_CHANGE")
        self.assertEqual(again["workspace"]["workspace_id"], workspace_id)

        # an exact replay of the FIRST operation still returns the historical OK
        self.clock.tick()
        replay = self.workspace.provision_workspace(
            subject_id="chiyo", grant_id="grant:a", operation_key="op:provision:1")
        self.assertEqual(replay["status"], "OK")
        self.assertEqual(replay["workspace"]["workspace_id"], workspace_id)

        self.assertEqual(self.workspace.workspace_count(), 1)
        row = self.workspace.get_workspace("chiyo")
        self.assertEqual(row["operation_id"], "op:provision:1",
                         "a later NO_CHANGE must not overwrite creation provenance")
        self.assertEqual(row["created_by_grant"], "grant:a")

    def test_duplicate_provision_cannot_commit_as_a_coordinated_command(self) -> None:
        """A second provision of the same subject must not be able to COMMIT at all."""
        decision = self._issue(grant_id="grant:first", operation_id="issue:first")
        self.assertEqual(decision.status, ALLOW)

        with self.db.unit_of_work(command="PROVISION_WORKSPACE",
                                  operation_id="op:provision:first",
                                  subject_id="chiyo") as uow:
            uow.param("subject_id", "chiyo")
            uow.param("grant_id", decision.grant_id)
            validated = self.gov.validate_grant(
                decision.grant_id, subject="chiyo", capability=CAPABILITY, scope=SCOPE,
                target=TARGET, expected_revision=None)
            uow.step(validated, owner="Governance", action="validate_grant")
            reserved = self.gov.reserve_use(
                decision.grant_id, subject="chiyo", capability=CAPABILITY, scope=SCOPE,
                target=TARGET, action_key="provision:first")
            uow.step(reserved, owner="Governance", action="reserve_use")
            uow.param("reservation_id", reserved.reservation_id)
            begun = self.gov.begin_execution(reserved.reservation_id, actor_uid=None)
            uow.step(begun, owner="Governance", action="begin_execution")
            consumed = self.gov.consume_use(reserved.reservation_id, actor_uid=None)
            uow.step(consumed, owner="Governance", action="consume_use")
            provisioned = self.workspace.provision_workspace(
                subject_id="chiyo", grant_id=decision.grant_id,
                operation_key="op:provision:first")
            uow.step(provisioned, owner="Workspace", action="provision_workspace")
            uow.param("operation_key", "op:provision:first")
        self.assertEqual(self.workspace.workspace_count(), 1)
        self.assertEqual(self._count("permission_grants"), 1)

        # A second, differently-keyed provision of the same subject: the store answers
        # NO_CHANGE, and the contract must refuse to let that become a successful command.
        second = self._issue(grant_id="grant:second", operation_id="issue:second")
        self.assertEqual(second.status, ALLOW)
        with self.assertRaises(CommandContractViolation) as ctx:
            with self.db.unit_of_work(command="PROVISION_WORKSPACE",
                                      operation_id="op:provision:second",
                                      subject_id="chiyo") as uow:
                uow.param("subject_id", "chiyo")
                uow.param("grant_id", second.grant_id)
                uow.step(self.gov.validate_grant(
                    second.grant_id, subject="chiyo", capability=CAPABILITY, scope=SCOPE,
                    target=TARGET, expected_revision=None),
                    owner="Governance", action="validate_grant")
                reserved2 = self.gov.reserve_use(
                    second.grant_id, subject="chiyo", capability=CAPABILITY, scope=SCOPE,
                    target=TARGET, action_key="provision:second")
                uow.step(reserved2, owner="Governance", action="reserve_use")
                uow.param("reservation_id", reserved2.reservation_id)
                uow.step(self.gov.begin_execution(reserved2.reservation_id, actor_uid=None),
                         owner="Governance", action="begin_execution")
                uow.step(self.gov.consume_use(reserved2.reservation_id, actor_uid=None),
                         owner="Governance", action="consume_use")
                duplicated = self.workspace.provision_workspace(
                    subject_id="chiyo", grant_id=second.grant_id,
                    operation_key="op:provision:second")
                self.assertEqual(duplicated["status"], "NO_CHANGE")
                uow.step(duplicated, owner="Workspace", action="provision_workspace")
                uow.param("operation_key", "op:provision:second")
        self.assertIn("workspace_active", str(ctx.exception))

        # Nothing from the refused command survived -- including the second reservation.
        self.assertEqual(self.workspace.workspace_count(), 1)
        self.assertEqual(self._count("life_supply_uow_journal"), 1)
        self.assertEqual(self._count("permission_reservations"), 1,
                         "the refused duplicate must not leave a consumed reservation")
        row = self.workspace.get_workspace("chiyo")
        self.assertEqual(row["operation_id"], "op:provision:first")


if __name__ == "__main__":
    unittest.main()
