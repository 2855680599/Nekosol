"""Restricted Unix-domain socket APIs for Governance and Life Supply owners."""
from __future__ import annotations

import json
import os
import socket
import socketserver
import struct
from pathlib import Path
from typing import Any

from lifesupply.artifact.store import ManagedArtifactStore
from lifesupply.governance.authority import GovernanceAuthority, UNKNOWN
from lifesupply.ports.governance_client import GovernanceSocketPort
from lifesupply.workspace.store import WorkspaceStore
from lifesupply.fencing import FenceHeld, StaleWriter, WriterFence
from lifesupply.trust.source import InquiryAdmissionPolicyV1, SourceEvidencePort, SourceScopePolicy, TrustedSourceRef
from lifesupply.fencing import require_schema_columns_from_path

MAX_REQUEST = 1_000_000


def _read_peer_uid(sock: socket.socket) -> int | None:
    if not hasattr(socket, "SO_PEERCRED"):
        return None
    try:
        _, uid, _ = struct.unpack("3i", sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")))
        return uid
    except OSError:
        return None


def _reservation_response(port: GovernanceSocketPort, *, grant_id: str,
                          subject: str, capability: str, scope: str,
                          target: str, action_key: str, payload: Any) -> dict[str, Any]:
    result = port.call({"op": "reserve_use", "grant_id": grant_id, "request": {
        "authority_scope": ("life-supply.workspace.v1" if capability in {"PROVISION_PERSONAL_WORKSPACE", "WRITE_OPEN_INQUIRY"} else "life-supply.artifact.v1"),
        "subject": subject, "capability": capability, "scope": scope,
        "target": target, "action_key": action_key, "payload": payload,
    }})
    return result.data


def _begin_reservation(port: GovernanceSocketPort, reservation_id: str) -> dict[str, Any]:
    return port.call({"op": "begin_execution", "reservation_id": reservation_id}).data


def _complete_reservation(port: GovernanceSocketPort, reservation_id: str,
                          *, consume: bool) -> dict[str, Any]:
    op = "consume_use" if consume else "release_reservation"
    return port.call({"op": op, "reservation_id": reservation_id}).data


def _finalize_no_effect(port: GovernanceSocketPort, reservation_id: str) -> dict[str, Any]:
    return port.call({"op": "finalize_no_effect", "reservation_id": reservation_id}).data


class Api:
    def __init__(self, root: str | Path, *, mode: str,
                 governance_socket: str = "/run/chiyo-governance/governance.sock",
                 governance_port: Any = None, control_uids: set[int] | None = None,
                 operator_uids: set[int] | None = None, supply_uids: set[int] | None = None,
                 owner_uid: int | None = None, owner_uids: set[int] | None = None,
                 writer_instance: str | None = None, read_only: bool = False,
                 service_principal: str = "service:chiyo-life-supply",
                 source_evidence_port: SourceEvidencePort | None = None,
                 inquiry_policy: InquiryAdmissionPolicyV1 | None = None,
                 require_non_root_identity: bool = False):
        root = Path(root)
        root.mkdir(parents=True, exist_ok=True)
        self.mode = mode
        self.operator_uids = {0} if operator_uids is None else set(operator_uids)
        self.supply_uids = set() if supply_uids is None else set(supply_uids)
        self.control_uids = {0} if control_uids is None else set(control_uids)
        self.owner_uids = {os.geteuid()} if owner_uids is None else set(owner_uids)
        if require_non_root_identity and mode == "governance" and (not self.operator_uids or 0 in self.operator_uids or not self.supply_uids or 0 in self.supply_uids):
            raise RuntimeError("dedicated non-root operator and service UID allowlists are required")
        if require_non_root_identity and mode == "supply" and (not self.control_uids or 0 in self.control_uids or not self.supply_uids or 0 in self.supply_uids):
            raise RuntimeError("dedicated non-root control and service UID allowlists are required")
        self.service_principal = service_principal
        self.writer_instance = writer_instance or f"{socket.gethostname()}:{os.getpid()}"
        self.read_only = read_only
        self.source_scope_policy = SourceScopePolicy(source_evidence_port, inquiry_policy)
        self.service_state = "STARTING"
        if read_only:
            if mode == "governance":
                require_schema_columns_from_path(root / "governance.sqlite", "Governance", {
                    "permission_grants": {"writer_owner", "writer_instance", "writer_epoch", "operation_id"},
                    "permission_reservations": {"execution_started_at"},
                    "authority_operations": {"operation_id", "request_digest", "result_json"},
                    "c6_writer_epoch": {"active_epoch", "instance_id", "writer_token"}})
            else:
                require_schema_columns_from_path(root / "workspace.sqlite", "Workspace", {
                    "workspaces": {"writer_owner", "writer_instance", "writer_epoch", "operation_id"},
                    "operation_keys": {"operation_key", "request_digest", "result_json"},
                    "c6_writer_epoch": {"active_epoch", "instance_id", "writer_token"}})
                require_schema_columns_from_path(root / "artifact.sqlite", "ManagedArtifact", {
                    "managed_artifacts": {"writer_owner", "writer_instance", "writer_epoch"},
                    "artifact_versions": {"version_id", "operation_key", "content_sha256"},
                    "artifact_operations": {"operation_key", "request_digest", "result_json"},
                    "artifact_projection_outbox": {"artifact_id", "expected_sha256", "state"},
                    "c6_writer_epoch": {"active_epoch", "instance_id", "writer_token"}})
        self.fences: dict[str, WriterFence] = {}
        if not read_only:
            owners = ["Governance"] if mode == "governance" else ["Workspace", "ManagedArtifact"]
            try:
                for name in owners:
                    self.fences[name] = WriterFence(name, root / "recovery" / "fences", instance_id=self.writer_instance)
            except Exception:
                for fence in self.fences.values(): fence.close()
                raise
        try:
            self.gov = GovernanceAuthority(root / "governance.sqlite", writer_fence=self.fences.get("Governance"), read_only=read_only) if mode == "governance" else None
            self.workspace = WorkspaceStore(root / "workspace.sqlite", writer_fence=self.fences.get("Workspace"), read_only=read_only) if mode == "supply" else None
            self.artifact = ManagedArtifactStore(root / "artifact.sqlite", projection_root=root / "projection",
                workspace_authority=self.workspace, writer_fence=self.fences.get("ManagedArtifact"),
                read_only=read_only) if mode == "supply" else None
        except Exception:
            for fence in self.fences.values():
                fence.close()
            raise
        actor_uid = os.geteuid() if owner_uid is None else owner_uid
        self.gov_port = (governance_port or GovernanceSocketPort(governance_socket, actor_uid=actor_uid)) if mode == "supply" else None
        self.operation_ledgers = {}
        try:
            if not read_only:
                from lifesupply.recovery.ledger import OperationLedger
                for owner, fence in self.fences.items():
                    self.operation_ledgers[owner] = OperationLedger(root / "recovery" / f"{owner.lower()}-operations.sqlite",
                        owner, writer_fence=fence)
        except Exception:
            for fence in self.fences.values(): fence.close()
            raise
        self.recovery_state = "RECOVERING"
        self.startup_recovery: dict[str, Any] = {"status": "OK", "projection": []}
        if mode == "supply" and not read_only:
            self.startup_recovery = self.artifact.recover_dirty_projections()
            if self.startup_recovery.get("status") != "OK":
                raise RuntimeError("startup projection recovery failed; refusing READY")
        self.recovery_state = "READY" if not read_only else "READ_ONLY"
        if not read_only and mode == "governance":
            self.startup_recovery = {"unresolved_reservations": self.gov.unresolved_reservations(),
                                     "status": "BLOCKED" if self.gov.unresolved_reservations() else "OK"}
        elif not read_only and mode == "supply":
            gov_health = self.gov_port.call({"op": "health"})
            if gov_health.status != "OK" or gov_health.data.get("service_state") != "READY":
                self.startup_recovery = {"status": "BLOCKED", "governance": gov_health.data}
            elif self.operation_ledgers and any(ledger.unresolved() for ledger in self.operation_ledgers.values()):
                self.startup_recovery = {"status": "BLOCKED", "detail": "unresolved owner operation ledger"}
        if self.startup_recovery.get("status") == "BLOCKED":
            self.recovery_state = "RECOVERY_REQUIRED"
        self.service_state = self.recovery_state

    def dispatch(self, req: dict[str, Any], *, peer_uid: int | None) -> dict[str, Any]:
        if self.mode == "governance":
            read_ops = {"health", "get_grant", "validate_grant", "list_active_grants", "operation_lookup"}
            if self.read_only and req.get("op") not in read_ops:
                return {"status": "BLOCKED", "detail": "READ_ONLY_MODE"}
            if self.recovery_state != "READY" and req.get("op") not in read_ops:
                return {"status": "BLOCKED", "detail": "GOVERNANCE_RECOVERY_REQUIRED"}
            if req.get("op") in {"issue_grant", "revoke_grant", "expire_grants", "reserve_use"}:
                unresolved = self.gov.unresolved_reservations()
                if unresolved:
                    self.recovery_state = "RECOVERY_REQUIRED"
                    self.service_state = self.recovery_state
                    return {"status": "BLOCKED", "detail": "GOVERNANCE_UNRESOLVED_OPERATIONS",
                            "count": len(unresolved)}
            return self._governance(req, peer_uid)
        return self._supply(req, peer_uid)

    def _governance(self, req: dict[str, Any], peer_uid: int | None) -> dict[str, Any]:
        gov = self.gov
        assert gov is not None
        op = req.get("op")
        operator_only = {"issue_grant", "get_grant", "validate_grant", "revoke_grant", "expire_grants", "list_active_grants", "operation_lookup"}
        supply_only = {"reserve_use", "begin_execution", "consume_use", "release_reservation", "finalize_no_effect"}
        if op in operator_only and peer_uid not in self.operator_uids:
            return {"status": "DENY", "detail": "operator peer credential required"}
        if op in supply_only and peer_uid not in self.supply_uids:
            return {"status": "DENY", "detail": "Life Supply peer credential required"}
        if op == "health":
            active = gov.list_active_grants()
            if active is None:
                return {"status": UNKNOWN}
            fence = self.fences.get("Governance")
            writer_status = fence.status() if fence else {"state": "READ_ONLY"}
            unresolved = gov.unresolved_reservations()
            unresolved_exec = gov.unresolved_executions()
            state = "READY" if (self.recovery_state == "READY" and writer_status.get("state") == "HELD" and not unresolved) else "READ_ONLY"
            return {"status": "OK", "owner": "Governance", "service_state": state,
                    "build_id": os.environ.get("LIFESUPPLY_BUILD_ID", "life-supply-c6-reconstructed-dev"),
                    "schema_version": "1", "meaning_version": "governance.c6.v1",
                    "service_principal": self.service_principal, "writer_instance": self.writer_instance,
                    "unresolved_reservations": len(unresolved), "unresolved_executions": unresolved_exec,
                    "recovery_state": self.recovery_state, "active_grant_count": len(active), "writer": writer_status}
        if op == "issue_grant":
            payload = dict(req.get("grant") or {})
            if not payload.get("operation_id"):
                return {"status": "DENY", "detail": "operation_id required"}
            payload.pop("bootstrap_authorized", None)
            payload.pop("grantor", None)
            payload["delegation_allowed"] = False
            authorized = peer_uid in self.operator_uids
            payload.pop("caller_principal", None)
            payload["caller_principal"] = "principal:host-operator"
            payload.pop("writer_instance", None)
            payload.pop("writer_epoch", None)
            payload["writer_instance"] = self.writer_instance
            if self.fences.get("Governance"):
                payload["writer_instance"] = self.fences["Governance"].instance_id
                payload["writer_epoch"] = int(self.fences["Governance"].epoch)
            return gov.issue_grant(grantor="HOST_OPERATOR" if authorized else "UNTRUSTED_CALLER",
                                   bootstrap_authorized=authorized, **payload).to_dict()
        if op == "operation_lookup":
            if peer_uid not in self.operator_uids:
                return {"status": "DENY", "detail": "operator peer credential required"}
            row = gov.lookup_operation(str(req.get("operation_id") or ""))
            return {"status": "KNOWN", "operation": row} if row else {"status": "NOT_FOUND"}
        if op == "get_grant":
            if peer_uid not in self.operator_uids:
                return {"status": "DENY", "detail": "operator peer credential required"}
            grant = gov.get_grant(str(req.get("grant_id") or ""))
            return {"status": "OK", "grant": grant} if grant else {"status": "NOT_FOUND"}
        if op == "validate_grant":
            if peer_uid not in self.operator_uids:
                return {"status": "DENY", "detail": "operator peer credential required"}
            return gov.validate_grant(str(req.get("grant_id") or ""), **dict(req.get("request") or {})).to_dict()
        if op == "reserve_use":
            if peer_uid not in self.supply_uids:
                return {"status": "DENY", "detail": "Life Supply peer credential required"}
            request = dict(req.get("request") or {})
            request["actor_uid"] = peer_uid  # overwrite, never trust a UID supplied in JSON
            supplied_scope = request.pop("authority_scope", None)
            expected_scope = "life-supply.workspace.v1" if request.get("capability") in {"PROVISION_PERSONAL_WORKSPACE", "WRITE_OPEN_INQUIRY"} else "life-supply.artifact.v1"
            if supplied_scope is not None and supplied_scope != expected_scope:
                return {"status": "DENY", "detail": "authority scope mismatch"}
            request["authority_scope"] = expected_scope
            return gov.reserve_use(str(req.get("grant_id") or ""), **request).to_dict()
        if op == "begin_execution":
            if peer_uid not in self.supply_uids or peer_uid not in self.owner_uids:
                return {"status": "DENY", "detail": "Life Supply service peer required"}
            return gov.begin_execution(str(req.get("reservation_id") or ""), actor_uid=peer_uid).to_dict()
        if op == "finalize_no_effect":
            if peer_uid not in self.supply_uids or peer_uid not in self.owner_uids:
                return {"status": "DENY", "detail": "Life Supply service peer required"}
            return gov.finalize_no_effect(str(req.get("reservation_id") or ""), actor_uid=peer_uid).to_dict()
        if op in {"consume_use", "release_reservation"}:
            if peer_uid not in self.supply_uids or peer_uid not in self.owner_uids:
                return {"status": "DENY", "detail": "Life Supply service peer required"}
            rid = str(req.get("reservation_id") or "")
            if op == "consume_use":
                return gov.consume_use(rid, actor_uid=peer_uid).to_dict()
            return gov.release_reservation(rid, actor_uid=peer_uid).to_dict()
        if op == "revoke_grant":
            if peer_uid not in self.operator_uids:
                return {"status": "DENY", "detail": "operator peer credential required"}
            try:
                revision = int(req.get("expected_revision", -1))
            except (TypeError, ValueError):
                return {"status": "DENY", "detail": "expected_revision must be integer"}
            operation_id = str(req.get("operation_id") or "")
            if not operation_id:
                return {"status": "DENY", "detail": "operation_id required"}
            if self.fences.get("Governance"):
                writer_epoch = int(self.fences["Governance"].epoch)
                writer_instance = self.fences["Governance"].instance_id
            else:
                writer_epoch, writer_instance = 0, self.writer_instance
            return gov.revoke_grant(str(req.get("grant_id") or ""), expected_revision=revision,
                operation_id=operation_id, writer_instance=writer_instance,
                writer_epoch=writer_epoch).to_dict()
        if op == "expire_grants":
            if peer_uid not in self.operator_uids:
                return {"status": "DENY"}
            n = gov.expire_grants()
            return {"status": "OK", "expired": n} if n is not None else {"status": UNKNOWN}
        if op == "list_active_grants":
            if peer_uid not in self.operator_uids:
                return {"status": "DENY", "detail": "operator peer credential required"}
            rows = gov.list_active_grants(subject=req.get("subject"))
            return {"status": "OK", "grants": rows} if rows is not None else {"status": UNKNOWN}
        return {"status": "NOT_FOUND", "detail": "unknown Governance operation"}

    def _supply(self, req: dict[str, Any], peer_uid: int | None) -> dict[str, Any]:
        read_ops = {"health", "get_workspace", "list_open_inquiries", "list_artifacts", "get_artifact", "get_artifact_head", "list_personal_opportunities", "get_effect_receipt", "operation_lookup"}
        if self.read_only and req.get("op") not in read_ops:
            return {"status": "BLOCKED", "detail": "READ_ONLY_MODE"}
        if self.recovery_state != "READY" and req.get("op") not in read_ops:
            return {"status": "BLOCKED", "detail": "OWNER_RECOVERY_NOT_READY"}
        ws, art, port = self.workspace, self.artifact, self.gov_port
        assert ws is not None and art is not None and port is not None
        op = req.get("op")
        if op not in read_ops:
            gov_health = port.call({"op": "health"})
            if gov_health.status != "OK" or gov_health.data.get("service_state") != "READY":
                self.recovery_state = "RECOVERY_REQUIRED"
                self.service_state = self.recovery_state
                return {"status": "BLOCKED", "detail": "GOVERNANCE_RECOVERY_NOT_READY"}
        read_ops = {"get_workspace", "list_open_inquiries", "list_artifacts", "get_artifact", "get_artifact_head", "list_personal_opportunities", "get_effect_receipt"}
        if peer_uid not in self.control_uids and op not in {"health"}:
            return {"status": "DENY", "detail": "Life Supply control peer required"}
        if op == "operation_lookup":
            if peer_uid not in self.control_uids:
                return {"status": "DENY"}
            owner = str(req.get("owner") or "")
            operation_id = str(req.get("operation_id") or "")
            if owner == "Governance":
                row = gov.lookup_operation(operation_id) if gov and self.mode == "governance" else None
                return {"status": "KNOWN", "receipt": row} if row else {"status": "NOT_FOUND"}
            if owner not in {"Workspace", "ManagedArtifact"} or not operation_id:
                return {"status": "DENY"}
            store = ws if owner == "Workspace" else art
            receipt = store.lookup_operation(operation_id)
            if receipt:
                return {"status": "KNOWN", "receipt": receipt}
            if owner in self.operation_ledgers:
                return self.operation_ledgers[owner].lookup(operation_id)
            return {"status": "NOT_FOUND", "operation_id": operation_id}
        if op == "health":
            wcount, acount = ws.workspace_count(), art.artifact_count()
            gov_health = port.call({"op": "health"})
            if wcount is None or acount is None or gov_health.status != "OK" or gov_health.data.get("service_state") != "READY":
                return {"status": UNKNOWN, "owner": "LifeSupply", "service_state": "DEGRADED",
                        "governance": gov_health.status, "governance_state": gov_health.data.get("service_state"),
                        "governance_unresolved": gov_health.data.get("unresolved_reservations")}
            if self.recovery_state != "READY":
                return {"status": UNKNOWN, "owner": "LifeSupply", "service_state": self.recovery_state,
                        "startup_recovery": self.startup_recovery}
            fence_health = {name: fence.status() for name, fence in self.fences.items()}
            unresolved = {owner: len(ledger.unresolved()) for owner, ledger in self.operation_ledgers.items()}
            governance_unresolved = int(gov_health.data.get("unresolved_reservations", 0))
            unresolved["GovernanceReservations"] = governance_unresolved
            gov_state = gov_health.data.get("service_state")
            has_unresolved = any(count > 0 for count in unresolved.values()) or gov_state != "READY"
            state = "READY" if (self.recovery_state == "READY" and not self.read_only and
                all(v["state"] == "HELD" for v in fence_health.values()) and not has_unresolved) else "READ_ONLY"
            return {"status": "OK", "owner": "LifeSupply", "service_state": state,
                    "build_id": os.environ.get("LIFESUPPLY_BUILD_ID", "life-supply-c6-reconstructed-dev"),
                    "schema_version": "1", "meaning_version": "life-supply.c6.v1",
                    "service_principal": self.service_principal, "writer_instance": self.writer_instance,
                    "writers": fence_health, "unresolved_operations": unresolved,
                    "workspace_count": wcount, "artifact_count": acount,
                    "gate_b": "PENDING", "write_gates": {"workspace": os.environ.get("REAL_WORKSPACE_PROVISION", "OFF"),
                                    "inquiry": "OFF", "artifact": os.environ.get("REAL_ARTIFACT_ACTION", "OFF")}}
        if op == "get_workspace":
            value = ws.get_workspace(str(req.get("subject") or ""))
            return {"status": "OK", "workspace": value} if value else {"status": "NOT_FOUND"}
        if op == "list_open_inquiries":
            value = ws.list_active_inquiries_view(str(req.get("subject") or ""))
            return {"status": "OK", "inquiries": value} if value is not None else {"status": UNKNOWN}
        if op == "list_artifacts":
            value = art.list_artifacts(str(req.get("subject") or ""))
            return {"status": "OK", "artifacts": value} if value is not None else {"status": UNKNOWN}
        if op == "get_artifact":
            artifact_id = str(req.get("artifact_id") or "")
            subject = str(req.get("subject") or "")
            row = art.get_artifact_for_subject(artifact_id, subject)
            value = art.read_artifact(artifact_id) if row and row["subject_id"] == subject else None
            return {"status": "OK", "artifact": value} if value else {"status": "NOT_FOUND"}
        if op == "get_artifact_head":
            artifact_id = str(req.get("artifact_id") or "")
            subject = str(req.get("subject") or "")
            row = art.get_artifact_for_subject(artifact_id, subject) if subject else None
            if not row and peer_uid in self.control_uids:
                value = art.read_head(artifact_id)
                return {"status": "OK", "head": value} if value else {"status": "NOT_FOUND"}
            value = art.read_head(artifact_id) if row else None
            return {"status": "OK", "head": value} if value else {"status": "NOT_FOUND"}
        if op == "list_personal_opportunities":
            inquiries = ws.personal_opportunities_view(str(req.get("subject") or ""))
            if inquiries is None:
                return {"status": UNKNOWN, "opportunities": None}
            return {"status": "OK", "opportunities": inquiries}
        if op == "get_effect_receipt":
            value = art.read_effect_receipt(str(req.get("operation_key") or ""))
            return {"status": "OK", "receipt": value} if value else {"status": "NOT_FOUND"}

        # Writes are denied while gates are OFF and are always checked against Governance.
        if op == "provision_workspace":
            if os.environ.get("REAL_WORKSPACE_PROVISION", "OFF") != "ON":
                return {"status": "BLOCKED", "detail": "REAL_WORKSPACE_PROVISION=OFF"}
            if self.recovery_state != "READY":
                return {"status": "BLOCKED", "detail": "OWNER_RECOVERY_NOT_READY"}
            if peer_uid not in self.control_uids:
                return {"status": "DENY", "detail": "Life Supply control peer required"}
            body = dict(req.get("workspace") or {})
            subject = str(body.get("subject_id") or "")
            grant_id, operation_key = str(req.get("grant_id") or ""), str(req.get("operation_key") or "")
            if not subject or not grant_id or not operation_key:
                return {"status": "DENY", "detail": "subject, grant, operation key required"}
            payload = {"subject_id": subject, "visibility": body.get("visibility", "PRIVATE"),
                       "artifact_scope": body.get("artifact_scope", "PERSONAL")}
            reservation = _reservation_response(port, grant_id=grant_id, subject=subject,
                capability="PROVISION_PERSONAL_WORKSPACE", scope="workspace:personal",
                target=f"workspace:{subject}", action_key=operation_key, payload=payload)
            if reservation.get("status") != "ALLOW" or not reservation.get("reservation_id"):
                return reservation
            begun = _begin_reservation(port, reservation["reservation_id"])
            if begun.get("status") != "ALLOW":
                return begun
            result = ws.provision_workspace(subject_id=subject, grant_id=grant_id,
                operation_key=operation_key, visibility=payload["visibility"],
                artifact_scope=payload["artifact_scope"])
            if result.get("status") in {"OK", "NO_CHANGE"} and "workspace" not in result:
                result["workspace"] = ws.get_workspace(subject)
            if result.get("status") in {"OK", "NO_CHANGE"}:
                completion = _complete_reservation(port, reservation["reservation_id"], consume=True)
                if completion.get("status") != "ALLOW":
                    return {"status": UNKNOWN, "detail": "workspace result persisted but grant finalization uncertain", "result": result}
            elif result.get("status") in {"DENY", "CONFLICT", "BLOCKED", "NOT_FOUND"}:
                _finalize_no_effect(port, reservation["reservation_id"])
            return result

        if op == "submit_inquiry_proposal":
            return {"status": "BLOCKED", "detail": "Gate B shadow not enabled; typed verified source required"}
        if op == "admit_inquiry":
            return {"status": "BLOCKED", "detail": "Gate B admission remains OFF pending production source/scope/quota owner ports"}
        if op in {"create_artifact", "commit_markdown", "archive_artifact", "tombstone_artifact"}:
            if self.recovery_state != "READY":
                return {"status": "BLOCKED", "detail": "OWNER_RECOVERY_NOT_READY"}
            if os.environ.get("REAL_ARTIFACT_ACTION", "OFF") != "ON":
                return {"status": "BLOCKED", "detail": "REAL_ARTIFACT_ACTION=OFF"}
            body = dict(req.get("artifact") or {})
            subject, grant_id = str(body.get("subject_id") or ""), str(req.get("grant_id") or "")
            key = str(req.get("operation_key") or "")
            if not subject or not grant_id or not key:
                return {"status": "DENY", "detail": "subject, grant, operation key required"}
            workspace = ws.get_workspace(subject)
            if workspace is None or workspace["lifecycle"] != "ACTIVE":
                return {"status": "BLOCKED", "detail": "active workspace required"}
            if op == "create_artifact":
                if str(body.get("workspace_id") or "") != workspace["workspace_id"]:
                    return {"status": "DENY", "detail": "artifact workspace/subject mismatch"}
            else:
                artifact_row = art.get_artifact_for_subject(str(body.get("artifact_id") or ""), subject)
                if artifact_row is None or artifact_row["workspace_id"] != workspace["workspace_id"]:
                    return {"status": "NOT_FOUND", "detail": "artifact/subject/workspace mismatch"}
            # Reconstruct a normalized operation payload. Unknown request fields and a caller's
            # body.operation can never change the routed operation or its authorization digest.
            if op == "create_artifact":
                authorized_payload = {"operation": op, "subject_id": subject,
                    "workspace_id": workspace["workspace_id"], "title": str(body.get("title") or "").strip()}
            elif op == "commit_markdown":
                authorized_payload = {"operation": op, "subject_id": subject,
                    "artifact_id": str(body.get("artifact_id") or ""),
                    "expected_head": body.get("expected_head"), "content": str(body.get("content") or "")}
            elif op in {"archive_artifact", "tombstone_artifact"}:
                authorized_payload = {"operation": op, "subject_id": subject,
                    "artifact_id": str(body.get("artifact_id") or ""),
                    "expected_revision": int(body.get("expected_revision", -1))}
            else:
                return {"status": "DENY", "detail": "unknown artifact operation"}
            reservation = _reservation_response(port, grant_id=grant_id, subject=subject,
                capability="COMMIT_MANAGED_ARTIFACT", scope="artifact:personal",
                target=f"workspace:{workspace['workspace_id']}", action_key=key,
                payload=authorized_payload)
            if reservation.get("status") != "ALLOW" or not reservation.get("reservation_id"):
                return reservation
            rid = reservation["reservation_id"]
            if op == "create_artifact":
                begun = _begin_reservation(port, rid)
                if begun.get("status") != "ALLOW":
                    return begun
                result = art.create_artifact(subject_id=subject,
                    workspace_id=workspace["workspace_id"],
                    title=str(body.get("title") or "").strip(), operation_key=key,
                    grant_reservation_id=rid, operation_payload=authorized_payload)
            elif op == "commit_markdown":
                begun = _begin_reservation(port, rid)
                if begun.get("status") != "ALLOW":
                    return begun
                result = art.commit_markdown(artifact_id=str(body.get("artifact_id") or ""),
                    authorized_subject_id=subject, expected_head=body.get("expected_head"),
                    content=str(body.get("content") or ""), operation_key=key,
                    grant_reservation_id=rid, operation_payload=authorized_payload)
            elif op == "archive_artifact":
                begun = _begin_reservation(port, rid)
                if begun.get("status") != "ALLOW":
                    return begun
                result = art.archive(str(body.get("artifact_id") or ""), authorized_subject_id=subject,
                    expected_revision=int(body.get("expected_revision", -1)),
                    operation_key=key, grant_reservation_id=rid, operation_payload=authorized_payload)
            else:
                begun = _begin_reservation(port, rid)
                if begun.get("status") != "ALLOW":
                    return begun
                result = art.tombstone(str(body.get("artifact_id") or ""), authorized_subject_id=subject,
                    expected_revision=int(body.get("expected_revision", -1)),
                    operation_key=key, grant_reservation_id=rid, operation_payload=authorized_payload)
            if result.get("status") in {"OK", "NO_CHANGE"}:
                completion = _complete_reservation(port, rid, consume=True)
                if completion.get("status") != "ALLOW":
                    return {"status": UNKNOWN, "detail": "artifact result persisted but grant consumption uncertain", "result": result}
            elif result.get("status") in {"DENY", "CONFLICT", "BLOCKED", "NOT_FOUND"}:
                _finalize_no_effect(port, rid)
            return result
        return {"status": "NOT_FOUND", "detail": "unknown Life Supply operation"}


class _Handler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        peer_uid = _read_peer_uid(self.request)
        limit = int(getattr(self.server, "max_payload_bytes", MAX_REQUEST))
        line = self.rfile.readline(limit + 1)
        if not line:
            return
        try:
            if len(line) > limit:
                # PHASE 13: a stable, typed refusal instead of an opaque parse failure.
                response = {"status": "DENY", "reason": "PAYLOAD_TOO_LARGE",
                            "detail": f"request exceeds {limit} bytes", "limit": limit}
            else:
                req = json.loads(line.decode("utf-8"))
                if not isinstance(req, dict):
                    raise ValueError("request must be an object")
                response = self.server.api.dispatch(req, peer_uid=peer_uid)
        except Exception as exc:
            response = {"status": "UNKNOWN", "detail": f"invalid request: {type(exc).__name__}"}
        self.wfile.write(json.dumps(response, ensure_ascii=False, separators=(",", ":")).encode() + b"\n")


class UnixApiServer(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, path: str, api: Api, *, max_payload_bytes: int = MAX_REQUEST):
        self.api = api
        self.max_payload_bytes = int(max_payload_bytes)
        super().__init__(path, _Handler)

#: C10 PHASE 06.  After the merge into one canonical database, this two-process /
#: three-database layout (governance.sqlite + workspace.sqlite + artifact.sqlite, plus a
#: per-owner ``*-operations.sqlite`` ledger) is a SECOND owner-and-recovery layout for the same
#: state.  Two competing recovery logics must not both be reachable, so it is now a
#: harness-only path: it refuses a root that carries the canonical single database, and it
#: refuses outright unless the operator asks for the legacy layout out loud.
LEGACY_THREE_DB_OPT_IN = "LIFE_SUPPLY_ALLOW_LEGACY_THREE_DB"


def _legacy_layout_refusal(data_root: str) -> str | None:
    if (os.environ.get(LEGACY_THREE_DB_OPT_IN) or "").strip().upper() == "ON":
        return None
    from lifesupply.db import CANONICAL_FILENAME
    if (Path(data_root) / CANONICAL_FILENAME).exists():
        return ("LEGACY_THREE_DB_REFUSED: this root holds the canonical single database; "
                "the three-database legacy layout must not run against it")
    return ("LEGACY_THREE_DB_REFUSED: the independent single-database service is the production "
            f"path; set {LEGACY_THREE_DB_OPT_IN}=ON only for a deliberate legacy harness run")


def run(mode: str, socket_path: str, data_root: str,
        governance_socket: str = "/run/chiyo-governance/governance.sock", *,
        operator_uids: set[int] | None = None, supply_uids: set[int] | None = None,
        control_uids: set[int] | None = None, owner_uid: int | None = None,
        owner_uids: set[int] | None = None, writer_instance: str | None = None,
        read_only: bool = False, service_principal: str = "service:chiyo-life-supply",
        require_non_root_identity: bool = True) -> None:
    refusal = _legacy_layout_refusal(data_root)
    if refusal is not None:
        raise RuntimeError(refusal)
    path = Path(socket_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise RuntimeError("refusing to unlink a pre-existing service socket path")
    api = Api(data_root, mode=mode, governance_socket=governance_socket,
              operator_uids=operator_uids, supply_uids=supply_uids,
              control_uids=control_uids, owner_uid=owner_uid, owner_uids=owner_uids,
              writer_instance=writer_instance, read_only=read_only,
              service_principal=service_principal,
              require_non_root_identity=require_non_root_identity)
    server = UnixApiServer(str(path), api)
    os.chmod(path, 0o660)
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        server.server_close()
        path.unlink(missing_ok=True)
        for fence in api.fences.values():
            fence.close()


# ---------------------------------------------------------------------------------
# PHASE 11: the independent service entry point
# ---------------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    """Run the independent service: Governance + Workspace + ManagedArtifact, one socket.

    ``python3 -m lifesupply.service.server`` and the ``life-supply`` console script both land
    here.  Sandbox roots are supplied with ``--socket`` / ``--data-root`` or the
    ``LIFE_SUPPLY_SOCKET`` / ``LIFE_SUPPLY_DATA_ROOT`` environment variables.
    """
    import argparse
    import sys
    from dataclasses import replace

    from lifesupply.service.life_supply import (
        ServiceSettings, assert_only_hosted_owners, run_service,
    )

    parser = argparse.ArgumentParser(
        prog="life-supply",
        description="Independent Governance / Workspace / ManagedArtifact service (Unix socket)")
    parser.add_argument("--socket", default=None, help="Unix socket path (local only; no TCP)")
    parser.add_argument("--data-root", default=None, help="canonical data root")
    parser.add_argument("--read-only", action="store_true", help="serve reads, refuse writes")
    parser.add_argument("--operator-uid", type=int, action="append", default=[],
                        help="host-operator peer uid (repeatable)")
    parser.add_argument("--service-uid", type=int, action="append", default=[],
                        help="service-principal peer uid (repeatable)")
    parser.add_argument("--writer-instance", default=None)
    parser.add_argument("--allow-root", action="store_true",
                        help="accept root/default uids (sandbox only)")
    args = parser.parse_args(argv)

    assert_only_hosted_owners()
    settings = ServiceSettings.from_env()
    overrides: dict[str, object] = {}
    if args.socket:
        overrides["socket_path"] = args.socket
    if args.data_root:
        overrides["data_root"] = args.data_root
    if args.read_only:
        overrides["read_only"] = True
    if args.operator_uid:
        overrides["operator_uids"] = frozenset(args.operator_uid)
    if args.service_uid:
        overrides["service_uids"] = frozenset(args.service_uid)
    if args.writer_instance:
        overrides["writer_instance"] = args.writer_instance
    if args.allow_root:
        overrides["require_non_root_identity"] = False
    if overrides:
        settings = replace(settings, **overrides)
    return run_service(settings)


if __name__ == "__main__":
    import sys

    sys.exit(main())
