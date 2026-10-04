"""PHASE 12: the four identities are distinct, and the separation holds mechanically.

``HOST_OPERATOR`` (the peer credential that may mint authority), ``SERVICE_PRINCIPAL`` (the
service that performs owner writes), ``CHIYO_SUBJECT`` (whose canonical facts are written) and
``OWNER_WRITER`` (what the fencing layer sees).  None of them can be substituted for another.
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path

from lifesupply.db import CANONICAL_FILENAME
from lifesupply.fencing import WriterFence
from lifesupply.identity import (
    AUTHORITY_MINTING_REQUEST_FIELDS,
    CHIYO_SUBJECT,
    HOST_OPERATOR,
    IDENTITY_KINDS,
    OWNER_WRITER,
    SERVICE_PRINCIPAL,
    Identity,
    IdentityError,
    IdentityNotInterchangeable,
    OwnerWriterIdentity,
    SelfAuthorityRefused,
    WriteContext,
    assert_matches_fence,
    assert_no_self_authority,
    assert_separation,
    canonical_identities,
    chiyo_subject,
    default_context,
    host_operator,
    owner_writer_for_fence,
    require_authority_mint,
    require_owner_writer,
    service_principal,
)
from lifesupply.service.life_supply import LifeSupplyService, LifeSupplySocketServer
from lifesupply.service.switches import FeatureSwitches

TIMEOUT = 6.0
FOREIGN_UID = 65534


class Sandbox:
    def __init__(self, *, root: Path | None = None, operator_uids: set[int] | None = None,
                 service_uids: set[int] | None = None) -> None:
        self.uid = os.geteuid()
        self.root = Path(root) if root is not None else Path(tempfile.mkdtemp(prefix="c10svc_"))
        self.owns_root = root is None
        self.socket_path = self.root / "life-supply.sock"
        self.data_root = self.root / "data"
        self.service = LifeSupplyService(
            self.data_root,
            operator_uids={self.uid} if operator_uids is None else operator_uids,
            service_uids={self.uid} if service_uids is None else service_uids,
            switches=FeatureSwitches.all_on(), writer_instance=f"identity:{os.getpid()}")
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


def grant_request(*, subject: str, capability: str, scope: str, target: str,
                  operation_id: str, use_mode: str = "REUSABLE") -> dict:
    return {"op": "issue_grant", "grant": {
        "subject": subject, "capability": capability, "scope": scope, "target": target,
        "use_mode": use_mode, "operation_id": operation_id}}


class IdentityUnitTests(unittest.TestCase):
    def test_four_names_are_distinct_and_separable(self) -> None:
        canonical = canonical_identities()
        self.assertEqual(sorted(canonical), sorted(IDENTITY_KINDS))
        self.assertEqual(len(set(canonical.values())), len(IDENTITY_KINDS))
        assert_separation()
        self.assertNotEqual(HOST_OPERATOR, SERVICE_PRINCIPAL)
        self.assertNotEqual(SERVICE_PRINCIPAL, CHIYO_SUBJECT)
        self.assertNotEqual(CHIYO_SUBJECT, OWNER_WRITER)
        self.assertTrue(host_operator(0).mints_authority)
        self.assertFalse(service_principal(1).mints_authority)
        self.assertFalse(chiyo_subject("chiyo").mints_authority)
        self.assertEqual(chiyo_subject("chiyo").name, f"subject:{'chiyo'}")
        self.assertIn("bootstrap_authorized", AUTHORITY_MINTING_REQUEST_FIELDS)

    def test_identities_cannot_be_substituted(self) -> None:
        require_authority_mint(host_operator(1000))
        with self.assertRaises(IdentityNotInterchangeable):
            require_authority_mint(service_principal(1000))
        with self.assertRaises(IdentityNotInterchangeable):
            require_authority_mint(chiyo_subject("chiyo"))
        with self.assertRaises(IdentityNotInterchangeable):
            require_owner_writer(host_operator(1000))
        with self.assertRaises(IdentityError):
            Identity("NOT_A_KIND", "x")
        with self.assertRaises(IdentityError):
            Identity("OWNER_WRITER", "x")
        with self.assertRaises(IdentityError):
            OwnerWriterIdentity(owner="Governance", instance_id="i", epoch=0, writer_token="t")
        with self.assertRaises(IdentityError):
            chiyo_subject("")

    def test_owner_writer_identity_is_bound_to_the_live_fence(self) -> None:
        root = Path(tempfile.mkdtemp(prefix="c10svc_"))
        try:
            fence = WriterFence("Governance", root / "fences")
            identity = owner_writer_for_fence("Governance", fence)
            self.assertEqual(identity.instance_id, fence.instance_id)
            self.assertEqual(identity.epoch, int(fence.epoch))
            self.assertEqual(identity.writer_token, fence.writer_token)
            assert_matches_fence(identity, fence)
            stale = OwnerWriterIdentity(owner="Governance", instance_id=fence.instance_id,
                                        epoch=identity.epoch + 1, writer_token=fence.writer_token)
            with self.assertRaises(IdentityNotInterchangeable):
                assert_matches_fence(stale, fence)
            cross_owner = OwnerWriterIdentity(owner="Workspace",
                                              instance_id=fence.instance_id,
                                              epoch=identity.epoch,
                                              writer_token=fence.writer_token)
            with self.assertRaises(IdentityNotInterchangeable):
                assert_matches_fence(cross_owner, fence)
            with self.assertRaises(IdentityError):
                owner_writer_for_fence("Governance", None)
            fence.release()
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def test_request_may_not_mint_its_own_authority(self) -> None:
        assert_no_self_authority(grant_request(subject="chiyo",
                                               capability="PROVISION_PERSONAL_WORKSPACE",
                                               scope="workspace:personal",
                                               target="workspace:chiyo",
                                               operation_id="op"))
        for field in sorted(AUTHORITY_MINTING_REQUEST_FIELDS):
            request = grant_request(subject="chiyo", capability="PROVISION_PERSONAL_WORKSPACE",
                                    scope="workspace:personal", target="workspace:chiyo",
                                    operation_id="op")
            request["grant"][field] = "1"
            with self.assertRaises(SelfAuthorityRefused, msg=field):
                assert_no_self_authority(request)
        with self.assertRaises(SelfAuthorityRefused):
            assert_no_self_authority({"op": "health", "context": [{"caller_principal": "x"}]})

    def test_write_context_envelope_still_works(self) -> None:
        context = default_context(subject_id="chiyo", owner="Governance", operation_id="op",
                                  writer_instance="instance", writer_epoch=3, created_at=1.0)
        context.validate("Governance")
        self.assertEqual(context.caller_principal, SERVICE_PRINCIPAL)
        self.assertEqual(context.to_dict()["writer_epoch"], 3)
        with self.assertRaises(ValueError):
            WriteContext("chiyo", SERVICE_PRINCIPAL, "Workspace", "instance", 3, "op", 1.0
                         ).validate("Governance")


class ServiceIdentityTests(unittest.TestCase):
    def test_operator_only_peer_cannot_act_as_the_service(self) -> None:
        box = Sandbox(operator_uids={os.geteuid()}, service_uids=set())
        try:
            granted = box.call(grant_request(subject="test:identity",
                                             capability="PROVISION_PERSONAL_WORKSPACE",
                                             scope="workspace:personal",
                                             target="workspace:test:identity",
                                             operation_id="identity:operator:issue"))
            self.assertEqual(granted["status"], "ALLOW", granted)
            provision = box.call({"op": "provision_workspace", "grant_id": granted["grant_id"],
                                  "operation_key": "identity:operator:provision",
                                  "workspace": {"subject_id": "test:identity"}})
            self.assertEqual(provision["status"], "DENY", provision)
            self.assertEqual(provision["reason"], "SERVICE_PRINCIPAL_REQUIRED")
            created = box.call({"op": "create_artifact", "grant_id": granted["grant_id"],
                                "operation_key": "identity:operator:artifact",
                                "artifact": {"subject_id": "test:identity",
                                             "workspace_id": "workspace:none", "title": "x"}})
            self.assertEqual(created["reason"], "SERVICE_PRINCIPAL_REQUIRED")
            grants = box.call({"op": "list_active_grants"})
            self.assertEqual(grants["status"], "OK")
            self.assertEqual(box.service.snapshot_epochs()["uow_journal_rows"], 1)
        finally:
            box.cleanup()

    def test_service_principal_cannot_mint_authority(self) -> None:
        box = Sandbox(operator_uids={FOREIGN_UID}, service_uids={os.geteuid()})
        try:
            refused = box.call(grant_request(subject="test:identity",
                                             capability="PROVISION_PERSONAL_WORKSPACE",
                                             scope="workspace:personal",
                                             target="workspace:test:identity",
                                             operation_id="identity:service:issue"))
            self.assertEqual(refused["status"], "DENY", refused)
            self.assertEqual(refused["reason"], "AUTHORITY_MINT_REQUIRES_HOST_OPERATOR")
            revoked = box.call({"op": "revoke_grant", "grant_id": "grant:none",
                                "expected_revision": 1, "operation_id": "identity:service:revoke"})
            self.assertEqual(revoked["reason"], "AUTHORITY_MINT_REQUIRES_HOST_OPERATOR")
            self.assertEqual(box.call({"op": "get_grant", "grant_id": "grant:none"})["reason"],
                             "AUTHORITY_MINT_REQUIRES_HOST_OPERATOR")
            self.assertEqual(box.service.snapshot_epochs()["uow_journal_rows"], 0)

            # Minted by the operator peer (modelled here, because only one uid can connect to
            # a local socket in a single-process sandbox), then used by the service principal.
            issued = box.service.dispatch(
                grant_request(subject="test:identity", capability="PROVISION_PERSONAL_WORKSPACE",
                              scope="workspace:personal", target="workspace:test:identity",
                              operation_id="identity:operator:issue", use_mode="ONE_SHOT"),
                peer_uid=FOREIGN_UID)
            self.assertEqual(issued["status"], "ALLOW", issued)
            self.assertEqual(box.service.snapshot_epochs()["uow_journal_rows"], 1)
            provision = box.call({"op": "provision_workspace", "grant_id": issued["grant_id"],
                                  "operation_key": "identity:service:provision",
                                  "workspace": {"subject_id": "test:identity"}})
            self.assertEqual(provision["status"], "OK", provision)
            self.assertEqual(provision["workspace"]["lifecycle"], "ACTIVE")
            self.assertEqual(box.call({"op": "health"})["status"], "OK")
        finally:
            box.cleanup()

    def test_request_json_cannot_supply_authority_fields(self) -> None:
        box = Sandbox()
        try:
            attempts = [
                {"op": "issue_grant", "grant": {"subject": "test:identity",
                                                "capability": "PROVISION_PERSONAL_WORKSPACE",
                                                "scope": "workspace:personal",
                                                "target": "workspace:test:identity",
                                                "operation_id": "identity:forged",
                                                "grantor": "HOST_OPERATOR",
                                                "bootstrap_authorized": True}},
                {"op": "issue_grant", "grant": {"subject": "test:identity",
                                                "capability": "PROVISION_PERSONAL_WORKSPACE",
                                                "scope": "workspace:personal",
                                                "target": "workspace:test:identity",
                                                "operation_id": "identity:forged",
                                                "caller_principal": "service:chiyo-life-supply"}},
                {"op": "issue_grant", "grant": {"subject": "test:identity",
                                                "capability": "PROVISION_PERSONAL_WORKSPACE",
                                                "scope": "workspace:personal",
                                                "target": "workspace:test:identity",
                                                "operation_id": "identity:forged",
                                                "writer_epoch": 99, "writer_instance": "forged"}},
                {"op": "get_grant", "grant_id": "grant:none", "peer_uid": 0},
                {"op": "provision_workspace", "grant_id": "g", "operation_key": "k",
                 "workspace": {"subject_id": "s", "actor_uid": 0}},
            ]
            for attempt in attempts:
                answer = box.call(attempt)
                self.assertEqual(answer["status"], "DENY", (attempt, answer))
                self.assertEqual(answer["reason"], "IDENTITY_FIELD_IN_REQUEST", attempt)
            self.assertEqual(box.service.snapshot_epochs()["uow_journal_rows"], 0)
        finally:
            box.cleanup()

    def test_writer_identity_is_the_one_the_fencing_layer_sees(self) -> None:
        box = Sandbox()
        try:
            grant = box.call(grant_request(subject="test:writer",
                                           capability="PROVISION_PERSONAL_WORKSPACE",
                                           scope="workspace:personal",
                                           target="workspace:test:writer",
                                           operation_id="identity:writer:issue",
                                           use_mode="ONE_SHOT"))
            self.assertEqual(grant["status"], "ALLOW", grant)
            fence = box.service.fences["Governance"]
            identity = owner_writer_for_fence("Governance", fence)
            uri = f"file:{box.data_root / CANONICAL_FILENAME}?mode=ro"
            with sqlite3.connect(uri, uri=True) as con:
                con.row_factory = sqlite3.Row
                row = con.execute("SELECT writer_instance,writer_epoch,writer_owner,caller_principal"
                                  " FROM permission_grants WHERE grant_id=?",
                                  (grant["grant_id"],)).fetchone()
                epochs = {entry["owner"]: dict(entry) for entry in con.execute(
                    "SELECT owner,active_epoch,instance_id,writer_token FROM c6_writer_epoch").fetchall()}
            self.assertEqual(row["writer_owner"], "Governance")
            self.assertEqual(row["writer_instance"], identity.instance_id)
            self.assertEqual(row["writer_instance"], fence.instance_id)
            self.assertEqual(int(row["writer_epoch"]), int(fence.epoch))
            self.assertEqual(row["caller_principal"], SERVICE_PRINCIPAL)
            for owner, epoch in epochs.items():
                self.assertEqual(epoch["instance_id"], box.service.fences[owner].instance_id)
                self.assertEqual(epoch["active_epoch"], int(box.service.fences[owner].epoch))
                self.assertEqual(epoch["writer_token"], box.service.fences[owner].writer_token)
            self.assertEqual(epochs["Governance"]["writer_token"], fence.writer_token)
            with self.assertRaises(IdentityNotInterchangeable):
                assert_matches_fence(identity, box.service.fences["Workspace"])
        finally:
            box.cleanup()


if __name__ == "__main__":
    unittest.main()
