"""PHASE 14/16: runtime states, read-only mode, fencing, and reads that stay reads.

The service state machine is ``STARTING -> RECOVERING -> (READ_ONLY | READY) -> (FENCED |
FAILED)``; writes are permitted only in ``READY``, and a read never opens a unit of work,
never takes a write lease and never advances a writer epoch.
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import tempfile
import threading
import unittest
from pathlib import Path

from lifesupply.db import CANONICAL_FILENAME, OWNERS, LifeSupplyDB
from lifesupply.fencing import WriterFence
from lifesupply.governance.authority import GovernanceAuthority
from lifesupply.service.life_supply import LifeSupplyService, LifeSupplySocketServer
from lifesupply.service.states import (
    ALLOWED_TRANSITIONS,
    FAILED,
    FENCED,
    READ_ONLY,
    READY,
    RECOVERING,
    STARTING,
    WRITE_PERMITTING_STATES,
    IllegalStateTransition,
    RuntimeStateMachine,
)
from lifesupply.service.switches import FeatureSwitches

TIMEOUT = 6.0


class Sandbox:
    def __init__(self, *, root: Path | None = None, read_only: bool = False,
                 data_root: Path | None = None, writer_instance: str = "states") -> None:
        self.uid = os.geteuid()
        self.root = Path(root) if root is not None else Path(tempfile.mkdtemp(prefix="c10svc_"))
        self.owns_root = root is None
        self.socket_path = self.root / "life-supply.sock"
        self.data_root = Path(data_root) if data_root is not None else self.root / "data"
        self.service = LifeSupplyService(self.data_root, operator_uids={self.uid},
                                         service_uids={self.uid},
                                         switches=FeatureSwitches.all_on(), read_only=read_only,
                                         writer_instance=f"{writer_instance}:{os.getpid()}")
        self.server = LifeSupplySocketServer(str(self.socket_path), self.service)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def call(self, request) -> dict:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(TIMEOUT)
            client.connect(str(self.socket_path))
            client.sendall(json.dumps(request, ensure_ascii=False).encode("utf-8") + b"\n")
            line = client.makefile("rb").readline(1_000_001)
        return json.loads(line.decode("utf-8"))

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.socket_path.unlink(missing_ok=True)
        self.service.close()

    def cleanup(self) -> None:
        self.stop()
        # A sandbox root only ever lives under /tmp/c10svc_*: always clean it up.
        shutil.rmtree(self.root, ignore_errors=True)


def seed(box: Sandbox, subject: str) -> dict:
    """A real workspace and a two-version artifact, created through the socket."""
    workspace_grant = box.call({"op": "issue_grant", "grant": {
        "subject": subject, "capability": "PROVISION_PERSONAL_WORKSPACE",
        "scope": "workspace:personal", "target": f"workspace:{subject}",
        "use_mode": "ONE_SHOT", "operation_id": f"seed:{subject}:ws"}})
    provision = box.call({"op": "provision_workspace", "grant_id": workspace_grant["grant_id"],
                          "operation_key": f"seed:{subject}:provision",
                          "workspace": {"subject_id": subject}})
    workspace_id = provision["workspace"]["workspace_id"]
    artifact_grant = box.call({"op": "issue_grant", "grant": {
        "subject": subject, "capability": "COMMIT_MANAGED_ARTIFACT", "scope": "artifact:personal",
        "target": f"workspace:{workspace_id}", "use_mode": "REUSABLE",
        "operation_id": f"seed:{subject}:art"}})
    created = box.call({"op": "create_artifact", "grant_id": artifact_grant["grant_id"],
                        "operation_key": f"seed:{subject}:create",
                        "artifact": {"subject_id": subject, "workspace_id": workspace_id,
                                     "title": "states", "content": "# one\n"}})
    v2 = box.call({"op": "commit_markdown", "grant_id": artifact_grant["grant_id"],
                   "operation_key": f"seed:{subject}:v2",
                   "artifact": {"subject_id": subject, "artifact_id": created["artifact_id"],
                                "expected_head": created["version_id"], "content": "# two\n"}})
    return {"subject": subject, "workspace_id": workspace_id, "artifact_id": created["artifact_id"],
            "v1": created["version_id"], "v2": v2["version_id"],
            "artifact_grant": artifact_grant["grant_id"], "workspace_grant": workspace_grant["grant_id"]}


class StateMachineTests(unittest.TestCase):
    def test_frozen_transition_graph(self) -> None:
        machine = RuntimeStateMachine()
        self.assertEqual(machine.state, STARTING)
        self.assertFalse(machine.may_write())
        self.assertEqual(machine.write_refusal_reason(), "SERVICE_STARTING")
        machine.transition(RECOVERING, "SCHEMA_AND_FENCES")
        self.assertFalse(machine.may_write())
        machine.transition(READY, "RECOVERY_COMPLETE")
        self.assertTrue(machine.may_write())
        self.assertTrue(machine.may_read())
        machine.transition(FENCED, "WRITER_SUPERSEDED")
        self.assertFalse(machine.may_write())
        self.assertTrue(machine.may_read())
        self.assertEqual(machine.write_refusal_reason(), "SERVICE_FENCED")
        with self.assertRaises(IllegalStateTransition):
            machine.transition(READY, "BACK")
        machine.transition(RECOVERING, "RETRY")
        machine.transition(READ_ONLY, "OPERATOR")
        self.assertFalse(machine.may_write())
        self.assertEqual(machine.write_refusal_reason(), "SERVICE_READ_ONLY")
        machine.transition(FAILED, "UNRECOVERABLE")
        with self.assertRaises(IllegalStateTransition):
            machine.transition(READY, "NO_WAY_BACK")
        self.assertEqual(sorted(ALLOWED_TRANSITIONS), sorted(
            [STARTING, RECOVERING, READ_ONLY, READY, FENCED, FAILED]))
        self.assertEqual(WRITE_PERMITTING_STATES, frozenset({READY}))

    def test_writes_are_refused_in_every_non_ready_state(self) -> None:
        self.assertTrue(RuntimeStateMachine(READY).may_write())
        for state in (STARTING, RECOVERING, READ_ONLY, FENCED, FAILED):
            machine = RuntimeStateMachine(state)
            self.assertFalse(machine.may_write(), state)
            self.assertTrue(machine.write_refusal_reason().startswith("SERVICE_"), state)
        self.assertFalse(RuntimeStateMachine(STARTING).may_read())
        self.assertFalse(RuntimeStateMachine(RECOVERING).may_read())
        for state in (READY, READ_ONLY, FENCED):
            self.assertTrue(RuntimeStateMachine(state).may_read(), state)


class ServiceStateTests(unittest.TestCase):
    def test_startup_history_and_status_expose_the_state(self) -> None:
        box = Sandbox()
        try:
            self.assertEqual(box.service.state.state, READY)
            history = [entry["to"] for entry in box.service.state.history]
            self.assertEqual(history[:3], [STARTING, RECOVERING, READY], history)
            self.assertEqual(box.service.state.history[1]["reason"],
                             "SCHEMA_COMPATIBLE_AND_FENCES_HELD")
            status = box.call({"op": "status"})
            self.assertEqual(status["status"], "OK")
            self.assertEqual(status["service_state"], READY)
            self.assertTrue(status["state"]["writes_permitted"])
            self.assertEqual(status["schema_layout"], "life-supply.single-db.v1")
            self.assertEqual(status["canonical_filename"], CANONICAL_FILENAME)
        finally:
            box.cleanup()

    def test_reads_do_not_advance_an_epoch_or_open_a_unit_of_work(self) -> None:
        box = Sandbox()
        try:
            ids = seed(box, "test:reads")
            before = box.service.snapshot_epochs()
            read_requests = [
                {"op": "health"}, {"op": "status"},
                {"op": "get_workspace", "subject": ids["subject"]},
                {"op": "list_open_inquiries", "subject": ids["subject"]},
                {"op": "list_artifacts", "subject": ids["subject"]},
                {"op": "read_artifact", "subject": ids["subject"], "artifact_id": ids["artifact_id"]},
                {"op": "read_head", "artifact_id": ids["artifact_id"]},
                {"op": "lookup_operation", "owner": "ManagedArtifact",
                 "operation_id": "seed:test:reads:create"},
                {"op": "lookup_operation", "owner": "Governance",
                 "operation_id": "seed:test:reads:ws"},
                {"op": "lookup_operation", "owner": "Workspace",
                 "operation_id": "seed:test:reads:provision"},
                {"op": "read_effect_receipt", "operation_key": "seed:test:reads:create"},
                {"op": "list_personal_opportunities", "subject": ids["subject"]},
                {"op": "list_active_grants", "subject": ids["subject"]},
                {"op": "list_unresolved_operations"},
            ]
            for request in read_requests:
                answer = box.call(request)
                self.assertIn(answer["status"], {"OK", "KNOWN", "NOT_FOUND"},
                              (request, answer))
            after = box.service.snapshot_epochs()
            self.assertEqual(before, after, "a read changed a writer epoch or wrote a journal row")
            for owner, row in after["epochs"].items():
                self.assertEqual(row["instance_id"], box.service.fences[owner].instance_id)
                self.assertEqual(row["active_epoch"], int(box.service.fences[owner].epoch))
        finally:
            box.cleanup()

    def test_read_only_service_serves_reads_without_any_write_lease(self) -> None:
        root = Path(tempfile.mkdtemp(prefix="c10svc_"))
        writer = Sandbox(root=root)
        try:
            ids = seed(writer, "test:readonly")
        finally:
            writer.stop()
        reader = Sandbox(root=root, read_only=True, writer_instance="reader")
        try:
            self.assertEqual(reader.service.state.state, READ_ONLY)
            self.assertEqual(reader.service.fences, {})
            status = reader.call({"op": "status"})
            self.assertEqual(status["service_state"], READ_ONLY)
            self.assertTrue(status["read_only"])
            self.assertFalse(status["state"]["writes_permitted"])
            before = reader.service.snapshot_epochs()
            workspace = reader.call({"op": "get_workspace", "subject": ids["subject"]})
            self.assertEqual(workspace["workspace"]["workspace_id"], ids["workspace_id"])
            head = reader.call({"op": "read_head", "artifact_id": ids["artifact_id"]})
            self.assertEqual(head["head"]["version_id"], ids["v2"])
            self.assertEqual(reader.call({"op": "read_artifact", "subject": ids["subject"],
                                          "artifact_id": ids["artifact_id"]})["content"], "# two\n")
            self.assertEqual(reader.service.snapshot_epochs(), before)
            refused = reader.call({"op": "provision_workspace", "grant_id": "g",
                                   "operation_key": "k", "workspace": {"subject_id": "s"}})
            self.assertEqual(refused["status"], "BLOCKED", refused)
            self.assertEqual(refused["reason"], "SERVICE_READ_ONLY")
            # Owner read filters expiry without sweeping or requiring a writer lease.
            self.assertEqual(reader.call({"op": "list_open_inquiries",
                                  "subject": ids["subject"]})["status"], "OK")
        finally:
            reader.cleanup()
            shutil.rmtree(root, ignore_errors=True)

    def test_fenced_when_recovery_is_incomplete(self) -> None:
        root = Path(tempfile.mkdtemp(prefix="c10svc_"))
        data_root = root / "data"
        subject = "test:fenced"
        fences = {owner: WriterFence(owner, data_root / "recovery" / "fences") for owner in OWNERS}
        try:
            db = LifeSupplyDB(data_root / CANONICAL_FILENAME, fences=fences)
            gov = GovernanceAuthority(db=db, writer_fence=fences["Governance"])
            grant = gov.issue_grant(grantor="HOST_OPERATOR", bootstrap_authorized=True,
                                    subject=subject, capability="PROVISION_PERSONAL_WORKSPACE",
                                    scope="workspace:personal", target=f"workspace:{subject}",
                                    authority_scope="life-supply.workspace.v1",
                                    use_mode="REUSABLE", operation_id="test:fenced:issue")
            self.assertEqual(grant.status, "ALLOW", grant)
            reserved = gov.reserve_use(grant.grant_id, subject=subject,
                                       capability="PROVISION_PERSONAL_WORKSPACE",
                                       scope="workspace:personal", target=f"workspace:{subject}",
                                       action_key="test:fenced:reserve")
            self.assertEqual(reserved.status, "ALLOW", reserved)
        finally:
            for fence in fences.values():
                fence.release()

        box = Sandbox(root=root)
        try:
            self.assertEqual(box.service.state.state, FENCED)
            reasons = [entry["reason"] for entry in box.service.state.history]
            self.assertEqual(reasons[-1], "UNRESOLVED_GOVERNANCE_OPERATIONS", reasons)
            health = box.call({"op": "health"})
            self.assertEqual(health["service_state"], FENCED)
            self.assertGreaterEqual(health["unresolved_operations"], 1)
            writes = box.call({"op": "provision_workspace", "grant_id": grant.grant_id,
                               "operation_key": "test:fenced:provision",
                               "workspace": {"subject_id": subject}})
            self.assertEqual(writes["status"], "BLOCKED", writes)
            self.assertEqual(writes["reason"], "SERVICE_FENCED")
            self.assertEqual(box.call({"op": "list_artifacts", "subject": subject})["artifacts"], [])
        finally:
            box.cleanup()
            shutil.rmtree(root, ignore_errors=True)

    def test_failed_state_is_terminal_and_reported(self) -> None:
        root = Path(tempfile.mkdtemp(prefix="c10svc_"))
        try:
            blocker = root / "blocker"
            blocker.write_text("not a directory\n", encoding="utf-8")
            uid = os.geteuid()
            service = LifeSupplyService(blocker / "data", operator_uids={uid}, service_uids={uid},
                                        switches=FeatureSwitches.all_on())
            self.assertEqual(service.state.state, FAILED)
            self.assertIsNotNone(service.startup_error)
            health = service.dispatch({"op": "health"}, peer_uid=uid)
            self.assertEqual(health["status"], "FAILED")
            self.assertEqual(health["service_state"], FAILED)
            self.assertIn("detail", health)
            refused = service.dispatch({"op": "get_workspace", "subject": "x"}, peer_uid=uid)
            self.assertEqual(refused["status"], "BLOCKED")
            self.assertEqual(refused["reason"], "SERVICE_FAILED")
            refused_write = service.dispatch(
                {"op": "provision_workspace", "grant_id": "g", "operation_key": "k",
                 "workspace": {"subject_id": "s"}}, peer_uid=uid)
            self.assertEqual(refused_write["reason"], "SERVICE_FAILED")
            with self.assertRaises(IllegalStateTransition):
                service.state.transition(READY, "WISHFUL")
            service.close()
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def test_writer_can_be_fenced_at_runtime_and_writes_stop(self) -> None:
        box = Sandbox()
        try:
            ids = seed(box, "test:fence")
            self.assertEqual(box.service.state.state, READY)
            box.service.fence_writer("TEST_FENCE", "operator fenced the writer")
            self.assertEqual(box.service.state.state, FENCED)
            refused = box.call({"op": "commit_markdown", "grant_id": ids["artifact_grant"],
                                "operation_key": "test:fence:v3",
                                "artifact": {"subject_id": ids["subject"],
                                             "artifact_id": ids["artifact_id"],
                                             "expected_head": ids["v2"], "content": "# three\n"}})
            self.assertEqual(refused["status"], "BLOCKED", refused)
            self.assertEqual(refused["reason"], "SERVICE_FENCED")
            head = box.call({"op": "read_head", "artifact_id": ids["artifact_id"]})["head"]
            self.assertEqual(head["version_id"], ids["v2"], "the fenced write moved the head")
        finally:
            box.cleanup()

    def test_recovering_state_refuses_writes(self) -> None:
        box = Sandbox()
        try:
            ids = seed(box, "test:recovering")
            box.service.state.transition(RECOVERING, "TEST_RECOVERY")
            refused = box.call({"op": "commit_markdown", "grant_id": ids["artifact_grant"],
                                "operation_key": "test:recovering:v3",
                                "artifact": {"subject_id": ids["subject"],
                                             "artifact_id": ids["artifact_id"],
                                             "expected_head": ids["v2"], "content": "x"}})
            self.assertEqual(refused["reason"], "SERVICE_RECOVERING", refused)
            self.assertEqual(box.call({"op": "health"})["status"], "OK")
        finally:
            box.cleanup()


if __name__ == "__main__":
    unittest.main()
