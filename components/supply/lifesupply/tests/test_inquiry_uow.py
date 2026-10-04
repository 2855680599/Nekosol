"""C10 PHASE 05 -- the real ADMIT_INQUIRY unit of work.

C6 shipped ``submit_proposal`` / ``admit_proposal`` as deliberate fail-closed stubs: it refused
to trust a caller's ``source_exists`` / ``scope_allowed`` / ``within_quota`` booleans, because a
boolean in a request is not evidence.  This phase keeps that refusal and supplies what was
missing -- an injected trusted validator port -- so the workspace can actually admit an inquiry
in an isolated run while production stays OFF (no port wired means BLOCKED).

What these tests pin down:

* with no validator port the two methods are BLOCKED (the ``INQUIRY_ADMISSION = OFF`` default);
* with an explicitly marked sandbox port the whole path is real, end to end;
* the caller's own booleans are never consulted -- only the port is;
* the grant must be a CONSUMED Governance reservation read back out of the store;
* a topic that is already open MERGES instead of opening a second inquiry;
* a non-ACTIVE workspace cannot admit anything;
* the ADMIT_INQUIRY postcondition is bound to THIS operation, not to "some proposal is ACCEPT".

Requires POSIX (``fcntl``) because the writer fences are flock-based; run on Linux.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from lifesupply.db import OWNERS, CommandContractViolation, LifeSupplyDB
from lifesupply.fencing import WriterFence
from lifesupply.governance.authority import ALLOW, GovernanceAuthority
from lifesupply.workspace.store import WorkspaceStore

PROVISION_CAPABILITY = "PROVISION_PERSONAL_WORKSPACE"
INQUIRY_CAPABILITY = "WRITE_OPEN_INQUIRY"
SCOPE = "workspace:personal"
TARGET = "workspace:chiyo"


class Clock:
    def __init__(self, value: float = 1000.0) -> None:
        self.value = float(value)

    def __call__(self) -> float:
        return self.value

    def tick(self, seconds: float = 1.0) -> None:
        self.value += float(seconds)


class SandboxProposalValidators:
    """A clearly marked ISOLATED test source port -- never the production one.

    Every answer is decided by this object alone, so a test can prove that the store consults
    the port rather than the caller's booleans.
    """

    def __init__(self, *, sources=("note:chiyo:self",), scope=True, quota=True) -> None:
        self.sources = frozenset(sources)
        self._scope = bool(scope)
        self._quota = bool(quota)
        self.calls: list[tuple[str, str]] = []

    def source_exists(self, *, subject_id, topic, source_refs) -> bool:
        self.calls.append(("source_exists", topic))
        return bool(source_refs) and all(ref in self.sources for ref in source_refs)

    def scope_allowed(self, *, subject_id, workspace_id, topic) -> bool:
        self.calls.append(("scope_allowed", topic))
        return self._scope

    def within_quota(self, *, subject_id, workspace_id) -> bool:
        self.calls.append(("within_quota", subject_id))
        return self._quota


class InquiryUnitOfWorkTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.clock = Clock()
        self.db_path = self.root / "life_supply.sqlite"
        self.fences = {owner: WriterFence(owner, self.root / "fences") for owner in OWNERS}
        self.db = LifeSupplyDB(self.db_path, fences=self.fences, clock=self.clock)
        self.gov = GovernanceAuthority(db=self.db, clock=self.clock,
                                       writer_fence=self.fences["Governance"])
        self.validators = SandboxProposalValidators()
        self.workspace = WorkspaceStore(db=self.db, clock=self.clock,
                                        writer_fence=self.fences["Workspace"],
                                        validators=self.validators)
        # the production default: a store with no validator port wired
        self.production = WorkspaceStore(db=self.db, clock=self.clock,
                                         writer_fence=self.fences["Workspace"])

    def tearDown(self) -> None:
        for fence in self.fences.values():
            fence.release()
        self._tmp.cleanup()

    def _count(self, table: str) -> int:
        with self.db.connection() as con:
            return int(con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])

    def _grant(self, capability: str, action_key: str, operation_id: str) -> str:
        decision = self.gov.issue_grant(
            grantor="HOST_OPERATOR", bootstrap_authorized=True, subject="chiyo",
            capability=capability, scope=SCOPE, target=TARGET,
            authority_scope="governance:workspace", use_mode="ONE_SHOT",
            operation_id=operation_id)
        self.assertEqual(decision.status, ALLOW)
        reserved = self.gov.reserve_use(decision.grant_id, subject="chiyo",
                                        capability=capability, scope=SCOPE, target=TARGET,
                                        action_key=action_key)
        self.assertEqual(reserved.status, ALLOW)
        self.gov.begin_execution(reserved.reservation_id, actor_uid=None)
        consumed = self.gov.consume_use(reserved.reservation_id, actor_uid=None)
        self.assertEqual(consumed.status, ALLOW)
        return reserved.reservation_id

    def _workspace(self) -> str:
        provisioned = self.workspace.provision_workspace(
            subject_id="chiyo", grant_id="grant:provision", operation_key="op:provision:1")
        self.assertEqual(provisioned["status"], "OK")
        return provisioned["workspace"]["workspace_id"]

    # -- the production default -------------------------------------------------

    def test_without_a_validator_port_inquiry_admission_stays_off(self) -> None:
        self._workspace()
        staged = self.production.submit_proposal(
            subject_id="chiyo", topic="把屋子收拾一下", source_refs=["note:chiyo:self"],
            driver="SELF_INITIATED", review_after=None, expires_at=None,
            operation_key="op:proposal:off")
        self.assertEqual(staged["status"], "BLOCKED")
        self.assertIn("INQUIRY_ADMISSION=OFF", staged["detail"])

        admitted = self.production.admit_proposal(
            proposal_id="proposal:any", authorized_subject_id="chiyo",
            authorized_workspace_id="ws:any", governance_reservation_id="res:any",
            source_exists=True, scope_allowed=True, within_quota=True)
        self.assertEqual(admitted["status"], "BLOCKED")
        self.assertEqual(self._count("inquiry_proposals"), 0)
        self.assertEqual(self._count("open_inquiries"), 0)

    # -- the real path with an isolated source port ------------------------------

    def test_an_isolated_run_can_submit_and_admit_a_real_inquiry(self) -> None:
        workspace_id = self._workspace()
        reservation = self._grant(INQUIRY_CAPABILITY, "inquire:1", "issue:inquiry:1")

        staged = self.workspace.submit_proposal(
            subject_id="chiyo", topic="把屋子收拾一下", source_refs=["note:chiyo:self"],
            driver="SELF_INITIATED", review_after=None, expires_at=None,
            operation_key="op:proposal:1")
        self.assertEqual(staged["status"], "OK")
        self.assertEqual(staged["proposal"]["admission_status"], "PENDING")
        proposal_id = staged["proposal"]["proposal_id"]

        admitted = self.workspace.admit_proposal(
            proposal_id=proposal_id, authorized_subject_id="chiyo",
            authorized_workspace_id=workspace_id,
            governance_reservation_id=reservation, operation_key="op:admit:1")
        self.assertEqual(admitted["status"], "OK")
        self.assertEqual(admitted["outcome"], "ACCEPT")
        self.assertEqual(admitted["inquiry"]["status"], "OPEN")
        self.assertEqual(admitted["inquiry"]["subject_id"], "chiyo")

        self.assertEqual(self._count("open_inquiries"), 1)
        self.assertEqual(self.workspace.get_proposal(proposal_id)["admission_status"], "ACCEPT")
        open_now = self.workspace.list_open_inquiries("chiyo")
        self.assertEqual(len(open_now), 1)
        self.assertEqual(open_now[0]["topic"], "把屋子收拾一下")

    def test_the_callers_own_booleans_are_never_consulted(self) -> None:
        workspace_id = self._workspace()
        reservation = self._grant(INQUIRY_CAPABILITY, "inquire:2", "issue:inquiry:2")
        staged = self.workspace.submit_proposal(
            subject_id="chiyo", topic="学一点新东西", source_refs=["note:chiyo:self"],
            driver="SELF_INITIATED", review_after=None, expires_at=None,
            operation_key="op:proposal:2")
        proposal_id = staged["proposal"]["proposal_id"]

        # the caller shouts TRUE, and the port says False: the store must believe the port
        self.validators = SandboxProposalValidators(sources=("note:someone-else",))
        self.workspace.validators = self.validators
        blocked = self.workspace.admit_proposal(
            proposal_id=proposal_id, authorized_subject_id="chiyo",
            authorized_workspace_id=workspace_id, governance_reservation_id=reservation,
            operation_key="op:admit:2",
            source_exists=True, scope_allowed=True, within_quota=True)
        self.assertEqual(blocked["status"], "BLOCKED")
        self.assertIn("provenance", blocked["detail"])
        self.assertEqual(self._count("open_inquiries"), 0)
        self.assertEqual(self.workspace.get_proposal(proposal_id)["admission_status"], "PENDING")
        self.assertIn(("source_exists", "学一点新东西"), self.validators.calls)

    def test_admission_requires_a_consumed_governance_reservation(self) -> None:
        workspace_id = self._workspace()
        staged = self.workspace.submit_proposal(
            subject_id="chiyo", topic="修一个旧 bug", source_refs=["note:chiyo:self"],
            driver="SELF_INITIATED", review_after=None, expires_at=None,
            operation_key="op:proposal:3")
        proposal_id = staged["proposal"]["proposal_id"]

        missing = self.workspace.admit_proposal(
            proposal_id=proposal_id, authorized_subject_id="chiyo",
            authorized_workspace_id=workspace_id, governance_reservation_id=None,
            operation_key="op:admit:3")
        self.assertEqual(missing["status"], "DENY")

        # a well-formed reservation id that is NOT a consumed reservation is not a grant
        forged = self.workspace.admit_proposal(
            proposal_id=proposal_id, authorized_subject_id="chiyo",
            authorized_workspace_id=workspace_id, governance_reservation_id="res:invented",
            operation_key="op:admit:3b")
        self.assertEqual(forged["status"], "DENY")
        self.assertIn("consumed grant", forged["detail"])
        self.assertEqual(self._count("open_inquiries"), 0)

    def test_a_topic_that_is_already_open_merges_instead_of_duplicating(self) -> None:
        workspace_id = self._workspace()
        reservation = self._grant(INQUIRY_CAPABILITY, "inquire:4", "issue:inquiry:4")
        staged = self.workspace.submit_proposal(
            subject_id="chiyo", topic="重复的主题", source_refs=["note:chiyo:self"],
            driver="SELF_INITIATED", review_after=None, expires_at=None,
            operation_key="op:proposal:4")
        proposal_id = staged["proposal"]["proposal_id"]

        # someone opened the same topic between the proposal and the admission
        with self.db.connection() as con:
            con.execute("BEGIN IMMEDIATE")
            con.execute(
                "INSERT INTO open_inquiries(inquiry_id,workspace_id,subject_id,topic,source_refs,"
                "driver,status,review_after,expires_at,created_at,updated_at,revision,"
                "meaning_version) VALUES('inquiry:existing',?,?,?,'[]','OTHER','OPEN',"
                "NULL,NULL,?,?,1,'workspace.open_inquiry.v1')",
                (workspace_id, "chiyo", "重复的主题", 1000.0, 1000.0))
            con.execute("COMMIT")

        merged = self.workspace.admit_proposal(
            proposal_id=proposal_id, authorized_subject_id="chiyo",
            authorized_workspace_id=workspace_id,
            governance_reservation_id=reservation, operation_key="op:admit:4")
        self.assertEqual(merged["status"], "NO_CHANGE")
        self.assertEqual(merged["outcome"], "MERGE")
        self.assertEqual(merged["inquiry"]["inquiry_id"], "inquiry:existing")
        self.assertEqual(self._count("open_inquiries"), 1)
        self.assertEqual(self.workspace.get_proposal(proposal_id)["admission_status"], "MERGE")

    def test_a_non_active_workspace_cannot_admit_anything(self) -> None:
        workspace_id = self._workspace()
        staged = self.workspace.submit_proposal(
            subject_id="chiyo", topic="停用后不能做的事", source_refs=["note:chiyo:self"],
            driver="SELF_INITIATED", review_after=None, expires_at=None,
            operation_key="op:proposal:5")
        proposal_id = staged["proposal"]["proposal_id"]
        with self.db.connection() as con:
            con.execute("BEGIN IMMEDIATE")
            con.execute("UPDATE workspaces SET lifecycle='SUSPENDED' WHERE workspace_id=?",
                        (workspace_id,))
            con.execute("COMMIT")
        reservation = self._grant(INQUIRY_CAPABILITY, "inquire:5", "issue:inquiry:5")
        blocked = self.workspace.admit_proposal(
            proposal_id=proposal_id, authorized_subject_id="chiyo",
            authorized_workspace_id=workspace_id,
            governance_reservation_id=reservation, operation_key="op:admit:5")
        self.assertEqual(blocked["status"], "BLOCKED")
        self.assertEqual(self._count("open_inquiries"), 0)

    # -- the postcondition is bound to THIS operation ----------------------------

    def test_admit_inquiry_cannot_commit_on_another_operations_result(self) -> None:
        """A proposal that some EARLIER operation accepted must not satisfy a new admission."""
        workspace_id = self._workspace()
        with self.db.connection() as con:
            con.execute("BEGIN IMMEDIATE")
            con.execute(
                "INSERT INTO inquiry_proposals(proposal_id,workspace_id,subject_id,topic,"
                "source_refs,driver,review_after,expires_at,admission_status,admission_reason,"
                "created_at) VALUES('proposal:older',?,?,'旧主题','[\"note:chiyo:self\"]',"
                "'SELF_INITIATED',NULL,NULL,'ACCEPT','admitted',1000.0)",
                (workspace_id, "chiyo"))
            con.execute("COMMIT")

        with self.assertRaises(CommandContractViolation) as ctx:
            with self.db.unit_of_work(command="ADMIT_INQUIRY", operation_id="op:admit:older",
                                      subject_id="chiyo") as uow:
                # every required step is registered with a legal status ...
                uow.step({"status": "OK"}, owner="Workspace", action="admit_proposal")
                uow.param("proposal_id", "proposal:older")
                uow.param("operation_key", "op:admit:older")
                # ... but THIS operation admitted nothing, so it must not commit
        self.assertIn("inquiry_admitted", str(ctx.exception))
        self.assertEqual(self._count("life_supply_uow_journal"), 0)

    def test_the_admission_result_is_recorded_under_this_operations_key(self) -> None:
        workspace_id = self._workspace()
        reservation = self._grant(INQUIRY_CAPABILITY, "inquire:6", "issue:inquiry:6")
        staged = self.workspace.submit_proposal(
            subject_id="chiyo", topic="记账式主题", source_refs=["note:chiyo:self"],
            driver="SELF_INITIATED", review_after=None, expires_at=None,
            operation_key="op:proposal:6")
        proposal_id = staged["proposal"]["proposal_id"]

        with self.db.unit_of_work(command="ADMIT_INQUIRY", operation_id="op:admit:6",
                                  subject_id="chiyo") as uow:
            admitted = self.workspace.admit_proposal(
                proposal_id=proposal_id, authorized_subject_id="chiyo",
                authorized_workspace_id=workspace_id, governance_reservation_id=reservation,
                operation_key="op:admit:6")
            self.assertEqual(admitted["status"], "OK")
            uow.step(admitted, owner="Workspace", action="admit_proposal")
            uow.param("proposal_id", proposal_id)
            uow.param("operation_key", "op:admit:6")

        self.assertEqual(self._count("open_inquiries"), 1)
        self.assertEqual(self._count("life_supply_uow_journal"), 1)
        ledger = self.workspace.lookup_operation("op:admit:6")
        self.assertIsNotNone(ledger)
        recorded = json.loads(ledger["result_json"])
        self.assertEqual(recorded["proposal_id"], proposal_id)
        self.assertEqual(recorded["outcome"], "ACCEPT")


    def test_an_unrecorded_admission_cannot_commit(self) -> None:
        """PHASE 07: a command whose own result cannot be looked up must not commit.

        An admission that is not recorded under an operation key cannot be found after a lost
        response, so a client could only retry blindly with a fresh id.  That is exactly what
        the operation ledger exists to prevent.
        """
        workspace_id = self._workspace()
        with self.db.connection() as con:
            con.execute("BEGIN IMMEDIATE")
            con.execute(
                "INSERT INTO inquiry_proposals(proposal_id,workspace_id,subject_id,topic,"
                "source_refs,driver,review_after,expires_at,admission_status,admission_reason,"
                "created_at) VALUES('proposal:unrecorded',?,?,'无台账主题',"
                "'[\"note:chiyo:self\"]','SELF_INITIATED',NULL,NULL,'ACCEPT','admitted',1000.0)",
                (workspace_id, "chiyo"))
            con.execute("COMMIT")

        with self.assertRaises(CommandContractViolation) as ctx:
            with self.db.unit_of_work(command="ADMIT_INQUIRY", operation_id="op:admit:7",
                                      subject_id="chiyo") as uow:
                uow.step({"status": "OK"}, owner="Workspace", action="admit_proposal")
                uow.param("proposal_id", "proposal:unrecorded")
                # deliberately no operation_key parameter
        self.assertIn("operation key", str(ctx.exception))
        self.assertEqual(self._count("life_supply_uow_journal"), 0)


if __name__ == "__main__":
    unittest.main()
