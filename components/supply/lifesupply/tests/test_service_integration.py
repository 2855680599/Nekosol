from __future__ import annotations

import json
import os
import socket
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from lifesupply.service.server import Api, UnixApiServer


class Services:
    def __init__(self, root: Path):
        self.root = root
        self.gov_path = root / "governance.sock"
        self.supply_path = root / "life-supply.sock"
        self.servers = []
        self.threads = []
        self.instance = 0

    def start(self):
        self.instance += 1
        supply_uid = os.geteuid()
        operator_uid = os.geteuid()
        self.gov_api = Api(self.root / "gov-data", mode="governance",
            operator_uids={operator_uid}, supply_uids={supply_uid}, control_uids={operator_uid}, owner_uids={supply_uid},
            writer_instance=f"test-{self.instance}")
        gov = UnixApiServer(str(self.gov_path), self.gov_api)
        tg = threading.Thread(target=gov.serve_forever, daemon=True); tg.start()
        # socket bind completes before serving thread starts.
        supply_api = Api(self.root / "supply-data", mode="supply", governance_socket=str(self.gov_path),
                         operator_uids={operator_uid}, supply_uids={supply_uid}, control_uids={operator_uid},
                         owner_uid=supply_uid, owner_uids={supply_uid}, writer_instance=f"test-{self.instance}")
        self.supply_api = supply_api
        supply = UnixApiServer(str(self.supply_path), supply_api)
        ts = threading.Thread(target=supply.serve_forever, daemon=True); ts.start()
        self.servers = [supply, gov]
        self.threads = [ts, tg]

    @staticmethod
    def call(path: Path, request: dict) -> dict:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(4)
            s.connect(str(path))
            s.sendall(json.dumps(request, ensure_ascii=False, separators=(",", ":")).encode() + b"\n")
            return json.loads(s.makefile("rb").readline().decode())

    def stop(self):
        for server in self.servers:
            server.shutdown(); server.server_close()
        for path in (self.gov_path, self.supply_path):
            path.unlink(missing_ok=True)
        self.servers = []
        self.threads = []
        for api in (getattr(self, "gov_api", None), getattr(self, "supply_api", None)):
            if api:
                for fence in api.fences.values(): fence.close()


class SocketIntegrationTests(unittest.TestCase):
    def _issue(self, services: Services, *, capability, scope, target,
               subject="test:life-supply", use_mode="REUSABLE", max_uses=None):
        result = services.gov_api.dispatch({"op": "issue_grant", "grant": {
            "subject": subject, "capability": capability, "scope": scope, "target": target,
            "authority_scope": ("life-supply.workspace.v1" if capability == "PROVISION_PERSONAL_WORKSPACE" else "life-supply.artifact.v1"), "use_mode": use_mode, "max_uses": max_uses,
            "operation_id": f"issue:{capability}:{subject}:{scope}",
        }}, peer_uid=os.geteuid())
        return result

    def test_real_two_socket_integration_restart_and_revoke(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            sv = Services(root)
            sv.start()
            try:
                self.assertEqual(Services.call(sv.gov_path, {"op": "health"})["owner"], "Governance")
                self.assertEqual(Services.call(sv.supply_path, {"op": "health"})["status"], "OK")
                subject = "test:life-supply"
                wg = self._issue(sv, capability="PROVISION_PERSONAL_WORKSPACE",
                    scope="workspace:personal", target=f"workspace:{subject}", use_mode="ONE_SHOT")
                self.assertEqual(wg["status"], "ALLOW")
                with patch.dict(os.environ, {"REAL_WORKSPACE_PROVISION": "ON"}):
                    provision = Services.call(sv.supply_path, {"op": "provision_workspace",
                        "grant_id": wg["grant_id"], "operation_key": "fixture-workspace:1",
                        "workspace": {"subject_id": subject}})
                self.assertEqual(provision["status"], "OK")
                self.assertEqual(Services.call(sv.supply_path, {"op": "list_open_inquiries", "subject": subject})["inquiries"], [])
                self.assertEqual(Services.call(sv.supply_path, {"op": "list_artifacts", "subject": subject})["artifacts"], [])

                ag = self._issue(sv, capability="COMMIT_MANAGED_ARTIFACT",
                    scope="artifact:personal", target=f"workspace:{provision['workspace']['workspace_id']}")
                self.assertEqual(ag["status"], "ALLOW")
                with patch.dict(os.environ, {"REAL_ARTIFACT_ACTION": "ON"}):
                    created = Services.call(sv.supply_path, {"op": "create_artifact",
                        "grant_id": ag["grant_id"], "operation_key": "fixture-artifact:create",
                        "artifact": {"subject_id": subject, "workspace_id": provision["workspace"]["workspace_id"], "title": "TEST only"}})
                    v1 = Services.call(sv.supply_path, {"op": "commit_markdown",
                        "grant_id": ag["grant_id"], "operation_key": "fixture-artifact:v1",
                        "artifact": {"subject_id": subject, "artifact_id": created["artifact_id"],
                                     "expected_head": None, "content": "test only"}})
                    retry = Services.call(sv.supply_path, {"op": "commit_markdown",
                        "grant_id": ag["grant_id"], "operation_key": "fixture-artifact:v1",
                        "artifact": {"subject_id": subject, "artifact_id": created["artifact_id"],
                                     "expected_head": None, "content": "test only"}})
                self.assertEqual(created["status"], "OK")
                self.assertEqual(v1["status"], "OK")
                self.assertEqual(retry["version_id"], v1["version_id"])
                # A same-content NO_CHANGE is durable at both owners and exactly replayable.
                with patch.dict(os.environ, {"REAL_ARTIFACT_ACTION": "ON"}):
                    nochange = Services.call(sv.supply_path, {"op": "commit_markdown",
                        "grant_id": ag["grant_id"], "operation_key": "fixture-artifact:no-change",
                        "artifact": {"subject_id": subject, "artifact_id": created["artifact_id"],
                                     "expected_head": v1["version_id"], "content": "test only"}})
                    nochange_retry = Services.call(sv.supply_path, {"op": "commit_markdown",
                        "grant_id": ag["grant_id"], "operation_key": "fixture-artifact:no-change",
                        "artifact": {"subject_id": subject, "artifact_id": created["artifact_id"],
                                     "expected_head": v1["version_id"], "content": "test only"}})
                self.assertEqual(nochange["status"], "NO_CHANGE")
                self.assertEqual(nochange_retry["status"], "NO_CHANGE")
            finally:
                sv.stop()

            # Recreate both service API instances, retaining only the two canonical SQLite roots.
            sv.start()
            try:
                self.assertEqual(Services.call(sv.supply_path, {"op": "get_workspace", "subject": subject})["workspace"]["lifecycle"], "ACTIVE")
                self.assertEqual(Services.call(sv.supply_path, {"op": "list_open_inquiries", "subject": subject})["inquiries"], [])
                self.assertEqual(len(Services.call(sv.supply_path, {"op": "list_artifacts", "subject": subject})["artifacts"]), 1)
                self.assertEqual(Services.call(sv.supply_path, {"op": "get_artifact_head", "artifact_id": created["artifact_id"]})["head"]["version_id"], v1["version_id"])
                self.assertEqual(Services.call(sv.supply_path, {"op": "list_personal_opportunities", "subject": subject})["opportunities"], [])
                # Operator-only revocation is exercised directly with the modeled root peer UID.
                revoked = sv.gov_api.dispatch({"op": "revoke_grant", "grant_id": wg["grant_id"], "expected_revision": 1,
                    "operation_id": "revoke:workspace:test"}, peer_uid=os.geteuid())
                self.assertEqual(revoked["status"], "REVOKED")
                with patch.dict(os.environ, {"REAL_WORKSPACE_PROVISION": "ON"}):
                    denied = Services.call(sv.supply_path, {"op": "provision_workspace",
                        "grant_id": wg["grant_id"], "operation_key": "fixture-workspace:next",
                        "workspace": {"subject_id": "test:other"}})
                self.assertEqual(denied["status"], "REVOKED")
            finally:
                sv.stop()

    def test_peer_acl_and_route_payload_binding(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); sv = Services(root); sv.start()
            try:
                self.assertEqual(sv.gov_api.dispatch({"op": "list_active_grants"}, peer_uid=99999)["status"], "DENY")
                self.assertEqual(sv.supply_api.dispatch({"op": "list_artifacts", "subject": "other"}, peer_uid=99999)["status"], "DENY")
                subject = "test:acl"
                ws = sv.gov_api.gov
                grant = sv.gov_api.dispatch({"op": "issue_grant", "grant": {
                    "subject": subject, "capability": "PROVISION_PERSONAL_WORKSPACE",
                    "scope": "workspace:personal", "target": f"workspace:{subject}",
                    "authority_scope": "life-supply.workspace.v1", "use_mode": "ONE_SHOT",
                    "operation_id": "issue:workspace:acl"}}, peer_uid=os.geteuid())
                with patch.dict(os.environ, {"REAL_WORKSPACE_PROVISION": "ON"}):
                    provision = Services.call(sv.supply_path, {"op": "provision_workspace",
                        "grant_id": grant["grant_id"], "operation_key": "acl:provision",
                        "workspace": {"subject_id": subject}})
                artifact_grant = sv.gov_api.gov.issue_grant(grantor="HOST_OPERATOR", bootstrap_authorized=True,
                    subject=subject, capability="COMMIT_MANAGED_ARTIFACT", scope="artifact:personal",
                    target=f"workspace:{provision['workspace']['workspace_id']}",
                    payload_binding={"operation": "create_artifact", "subject_id": subject,
                        "workspace_id": provision["workspace"]["workspace_id"], "title": "fixture"},
                    authority_scope="life-supply.artifact.v1", use_mode="REUSABLE",
                    operation_id="issue:artifact:acl")
                with patch.dict(os.environ, {"REAL_ARTIFACT_ACTION": "ON"}):
                    result = Services.call(sv.supply_path, {"op": "create_artifact", "grant_id": artifact_grant.grant_id,
                        "operation_key": "acl:route-confusion", "artifact": {
                            "subject_id": subject, "workspace_id": provision["workspace"]["workspace_id"],
                            "title": "fixture", "operation": "tombstone_artifact", "artifact_id": "unknown"}})
                self.assertEqual(result["status"], "OK")
                with patch.dict(os.environ, {"REAL_ARTIFACT_ACTION": "ON"}):
                    forged = Services.call(sv.supply_path, {"op": "tombstone_artifact", "grant_id": artifact_grant.grant_id,
                        "operation_key": "acl:forged-tombstone", "artifact": {
                            "subject_id": subject, "artifact_id": result["artifact_id"], "expected_revision": 1,
                            "operation": "commit_markdown"}})
                self.assertEqual(forged["status"], "DENY")
                self.assertEqual(len(Services.call(sv.supply_path, {"op": "list_artifacts", "subject": subject})["artifacts"]), 1)
            finally:
                sv.stop()

    def test_write_gates_off_by_default(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); sv = Services(root); sv.start()
            try:
                result = Services.call(sv.supply_path, {"op": "provision_workspace", "grant_id": "x",
                    "operation_key": "x", "workspace": {"subject_id": "x"}})
                self.assertEqual(result["status"], "BLOCKED")
            finally:
                sv.stop()


if __name__ == "__main__":
    unittest.main()
