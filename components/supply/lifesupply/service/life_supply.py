"""The independent Life Supply service: one process, one socket, three owners.

PHASE 11-17.  This module hosts exactly these three owners --

* ``Governance``           (``GovernanceAuthority``)
* ``Workspace``            (``WorkspaceStore``)
* ``ManagedArtifact``      (``ManagedArtifactStore``)

-- on the single canonical database ``life_supply.sqlite`` (``lifesupply.db``).  It imports no
Memory / World / Body / Contact / LPC module, makes no network or model calls, and refuses to
start if a forbidden module is already imported (``assert_only_hosted_owners``).

Boundary rules enforced here:

* every write is a call to a *formal owner API* inside that owner's coordinated unit of work
  (``LifeSupplyUnitOfWork``), with the command name taken from the frozen allowlist
  (``lifesupply.db.CROSS_OWNER_COMMANDS``); no caller input reaches SQL, and no generic
  filesystem write is exposed;
* reads never open a unit of work, never acquire a write lease and never advance an epoch
  (``c6_writer_epoch``);
* every runtime switch (PHASE 17) ships OFF and gates only *new* writes -- switching one off
  never deletes or rewrites canonical data;
* the runtime state machine (PHASE 16) permits writes only in ``READY``.
"""
from __future__ import annotations

import json
import os
import signal
import socket
import socketserver
import sqlite3
import stat
import struct
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from lifesupply.artifact.store import ManagedArtifactStore
from lifesupply.db import (
    CANONICAL_FILENAME,
    OWNERS,
    SCHEMA_LAYOUT,
    LifeSupplyDB,
    UnitOfWorkDoomed,
)
from lifesupply.fencing import StaleWriter, WriterFence
from lifesupply.governance.authority import CAPABILITIES, GovernanceAuthority
from lifesupply.identity import (
    KIND_HOST_OPERATOR,
    KIND_SERVICE_PRINCIPAL,
    SERVICE_PRINCIPAL,
    Identity,
    IdentityError,
    IdentityNotInterchangeable,
    SelfAuthorityRefused,
    assert_matches_fence,
    assert_no_self_authority,
    assert_separation,
    chiyo_subject,
    host_operator,
    owner_writer_for_fence,
    require_authority_mint,
    service_principal,
)
from lifesupply.service.states import (
    FAILED,
    READ_ONLY,
    READY,
    RECOVERING,
    RuntimeStateMachine,
)
from lifesupply.service.switches import FeatureSwitches, SWITCH_NAMES
from lifesupply.workspace.store import WorkspaceStore

BUILD_ID = os.environ.get("LIFE_SUPPLY_BUILD_ID", "life-supply-v0.1-c10")

DEFAULT_SOCKET_PATH = "./data/supply/life-supply.sock"
DEFAULT_DATA_ROOT = "./data/supply"
DEFAULT_SOCKET_MODE = 0o600
DEFAULT_MAX_PAYLOAD_BYTES = 1_000_000

WORKSPACE_CAPABILITY = "PROVISION_PERSONAL_WORKSPACE"
INQUIRY_CAPABILITY = "WRITE_OPEN_INQUIRY"
ARTIFACT_CAPABILITY = "COMMIT_MANAGED_ARTIFACT"
WORKSPACE_SCOPE = "workspace:personal"
INQUIRY_SCOPE = "inquiry:personal"
ARTIFACT_SCOPE = "artifact:personal"
WORKSPACE_AUTHORITY_SCOPE = "life-supply.workspace.v1"
ARTIFACT_AUTHORITY_SCOPE = "life-supply.artifact.v1"

#: Modules this service must never load.  Checked against ``sys.modules`` at startup.
FORBIDDEN_MODULES: tuple[str, ...] = (
    "lifesupply.memory", "lifesupply.world", "lifesupply.body", "lifesupply.contact",
    "lifesupply.lpc", "hermes", "hermes_gateway", "chiyo_world_body", "chiyo_life_runtime",
    "chiyo_memory", "chiyo_contact", "chiyo_lpc", "life_runtime", "world_body",
    "memory_owner", "contact_owner",
)

#: The only owner modules this service is allowed to host.
HOSTED_OWNER_MODULES: tuple[str, ...] = (
    "lifesupply.governance.authority",
    "lifesupply.workspace.store",
    "lifesupply.artifact.store",
)


class ForbiddenModuleImported(RuntimeError):
    """A module outside the three hosted owners is loaded in this process."""


class ServiceConfigRefused(RuntimeError):
    """The service configuration itself is unsafe (e.g. a world-accessible socket)."""


def assert_only_hosted_owners(modules: Mapping[str, Any] | None = None) -> None:
    """Fail closed if any forbidden module is importable/imported in this process."""
    loaded = sys.modules if modules is None else modules
    offending = sorted(
        name for name in list(loaded)
        if any(name == prefix or name.startswith(prefix + ".") for prefix in FORBIDDEN_MODULES))
    if offending:
        raise ForbiddenModuleImported(f"FORBIDDEN_MODULE_IMPORTED:{','.join(offending)}")


def imported_owner_modules(modules: Mapping[str, Any] | None = None) -> list[str]:
    loaded = sys.modules if modules is None else modules
    return sorted(name for name in HOSTED_OWNER_MODULES if name in loaded)


# ---------------------------------------------------------------------------------
# configuration
# ---------------------------------------------------------------------------------


def _uids(raw: Any) -> frozenset[int]:
    if raw is None or raw == "":
        return frozenset()
    if isinstance(raw, (set, frozenset, list, tuple, dict)):
        return frozenset(int(value) for value in raw)
    return frozenset(int(part) for part in str(raw).split(",") if part.strip())


def _subjects(raw: Any) -> tuple[str, ...] | None:
    if raw is None or raw == "":
        return None
    if isinstance(raw, (set, frozenset, list, tuple)):
        return tuple(sorted(str(value) for value in raw))
    return tuple(sorted(part.strip() for part in str(raw).split(",") if part.strip()))


def _first_env(source: Mapping[str, str], *names: str) -> str | None:
    for name in names:
        if name in source:
            return source[name]
    return None


@dataclass(frozen=True)
class ServiceSettings:
    """Everything the service needs to start.  Sandbox roots come from here or the env."""

    socket_path: str = DEFAULT_SOCKET_PATH
    data_root: str = DEFAULT_DATA_ROOT
    socket_mode: int = DEFAULT_SOCKET_MODE
    max_payload_bytes: int = DEFAULT_MAX_PAYLOAD_BYTES
    operator_uids: frozenset[int] = frozenset()
    service_uids: frozenset[int] = frozenset()
    read_only: bool = False
    writer_instance: str | None = None
    allowed_subjects: tuple[str, ...] | None = None
    require_non_root_identity: bool = True

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "ServiceSettings":
        source = os.environ if env is None else env

        def get(*names: str) -> str | None:
            return _first_env(source, *names)

        raw_mode = get("LIFE_SUPPLY_SOCKET_MODE", "LIFESUPPLY_SOCKET_MODE")
        raw_limit = get("LIFE_SUPPLY_MAX_PAYLOAD_BYTES", "LIFESUPPLY_MAX_PAYLOAD_BYTES")
        read_only = (get("LIFE_SUPPLY_READ_ONLY", "LIFESUPPLY_READ_ONLY") or "OFF").strip().upper() == "ON"
        require_non_root = (get("LIFE_SUPPLY_REQUIRE_NON_ROOT", "LIFESUPPLY_REQUIRE_NON_ROOT_IDENTITY")
                            or "ON").strip().upper() == "ON"
        return cls(
            socket_path=get("LIFE_SUPPLY_SOCKET", "LIFESUPPLY_SOCKET") or DEFAULT_SOCKET_PATH,
            data_root=get("LIFE_SUPPLY_DATA_ROOT", "LIFESUPPLY_DATA_ROOT") or DEFAULT_DATA_ROOT,
            socket_mode=int(raw_mode, 8) if raw_mode else DEFAULT_SOCKET_MODE,
            max_payload_bytes=int(raw_limit) if raw_limit else DEFAULT_MAX_PAYLOAD_BYTES,
            operator_uids=_uids(get("LIFE_SUPPLY_OPERATOR_UIDS", "GOVERNANCE_OPERATOR_UIDS")),
            service_uids=_uids(get("LIFE_SUPPLY_SERVICE_UIDS", "LIFESUPPLY_SERVICE_UIDS",
                                   "LIFESUPPLY_CONTROL_UIDS")),
            read_only=read_only,
            writer_instance=get("LIFE_SUPPLY_WRITER_INSTANCE", "LIFESUPPLY_WRITER_INSTANCE"),
            allowed_subjects=_subjects(get("LIFE_SUPPLY_ALLOWED_SUBJECTS", "LIFESUPPLY_ALLOWED_SUBJECTS")),
            require_non_root_identity=require_non_root,
        )

    def validate(self) -> "ServiceSettings":
        if not self.socket_path.startswith("/"):
            raise ServiceConfigRefused(f"SOCKET_PATH_MUST_BE_ABSOLUTE:{self.socket_path}")
        if self.socket_mode & 0o077:
            raise ServiceConfigRefused(f"SOCKET_MODE_NOT_OWNER_ONLY:{oct(self.socket_mode)}")
        if self.max_payload_bytes < 1:
            raise ServiceConfigRefused("PAYLOAD_LIMIT_MUST_BE_POSITIVE")
        if bool(self.read_only):
            return self
        if self.require_non_root_identity:
            for label, uids in (("operator", self.operator_uids), ("service", self.service_uids)):
                if not uids or 0 in uids:
                    raise ServiceConfigRefused(f"DEDICATED_NON_ROOT_{label.upper()}_UID_REQUIRED")
        return self


# ---------------------------------------------------------------------------------
# typed request validation (PHASE 13)
# ---------------------------------------------------------------------------------

READ_OPS: frozenset[str] = frozenset({
    "health", "status", "get_workspace", "list_open_inquiries", "list_artifacts",
    "read_artifact", "read_head", "lookup_operation", "read_effect_receipt",
    "list_personal_opportunities", "list_active_grants", "get_grant",
    "list_unresolved_operations",
})

COMMAND_OPS: frozenset[str] = frozenset({
    "issue_grant", "revoke_grant", "provision_workspace", "admit_inquiry",
    "create_artifact", "commit_markdown", "archive_artifact", "tombstone",
})

OPS: frozenset[str] = READ_OPS | COMMAND_OPS

#: Which identity may call which operation.  A subject is never a caller.
OP_CALLERS: dict[str, frozenset[str]] = {
    # Reads: either configured peer may look.
    **{op: frozenset({KIND_HOST_OPERATOR, KIND_SERVICE_PRINCIPAL}) for op in READ_OPS},
    # Owner-facing commands belong to the service principal alone: the host operator mints
    # authority and never acts as the service, and the service never mints authority.
    **{op: frozenset({KIND_SERVICE_PRINCIPAL}) for op in COMMAND_OPS},
    "issue_grant": frozenset({KIND_HOST_OPERATOR}),
    "revoke_grant": frozenset({KIND_HOST_OPERATOR}),
    "get_grant": frozenset({KIND_HOST_OPERATOR}),
    "list_active_grants": frozenset({KIND_HOST_OPERATOR}),
}

_NESTED_GRANT = frozenset({
    "subject", "capability", "scope", "target", "authority_scope", "payload_binding",
    "valid_from", "expires_at", "use_mode", "max_uses", "meaning_version", "grant_id",
    "operation_id",
})
_NESTED_WORKSPACE = frozenset({"subject_id", "visibility", "artifact_scope"})
_NESTED_INQUIRY = frozenset({"subject_id", "workspace_id", "topic", "source_refs",
                             "review_after", "expires_at"})
_NESTED_ARTIFACT_CREATE = frozenset({"subject_id", "workspace_id", "title", "content"})
_NESTED_ARTIFACT_COMMIT = frozenset({"subject_id", "artifact_id", "expected_head", "content"})
_NESTED_ARTIFACT_LIFECYCLE = frozenset({"subject_id", "artifact_id", "expected_revision"})

FIELD_TYPES: dict[str, dict[str, Any]] = {
    "health": {},
    "status": {},
    "get_workspace": {"subject": str},
    "list_open_inquiries": {"subject": str},
    "list_artifacts": {"subject": str},
    "read_artifact": {"subject": str, "artifact_id": str},
    "read_head": {"artifact_id": str, "subject": (str, type(None))},
    "lookup_operation": {"owner": str, "operation_id": str},
    "read_effect_receipt": {"operation_key": str},
    "list_personal_opportunities": {"subject": str},
    "list_active_grants": {"subject": (str, type(None))},
    "get_grant": {"grant_id": str},
    "list_unresolved_operations": {},
    "issue_grant": {"grant": dict},
    "revoke_grant": {"grant_id": str, "expected_revision": int, "operation_id": str},
    "provision_workspace": {"grant_id": str, "operation_key": str, "workspace": dict},
    "admit_inquiry": {"grant_id": str, "operation_key": str, "inquiry": dict},
    "create_artifact": {"grant_id": str, "operation_key": str, "artifact": dict},
    "commit_markdown": {"grant_id": str, "operation_key": str, "artifact": dict},
    "archive_artifact": {"grant_id": str, "operation_key": str, "artifact": dict},
    "tombstone": {"grant_id": str, "operation_key": str, "artifact": dict},
}

REQUIRED_FIELDS: dict[str, frozenset[str]] = {
    "get_workspace": frozenset({"subject"}),
    "list_open_inquiries": frozenset({"subject"}),
    "list_artifacts": frozenset({"subject"}),
    "read_artifact": frozenset({"subject", "artifact_id"}),
    "read_head": frozenset({"artifact_id"}),
    "lookup_operation": frozenset({"owner", "operation_id"}),
    "read_effect_receipt": frozenset({"operation_key"}),
    "list_personal_opportunities": frozenset({"subject"}),
    "get_grant": frozenset({"grant_id"}),
    "issue_grant": frozenset({"grant"}),
    "revoke_grant": frozenset({"grant_id", "expected_revision", "operation_id"}),
    "provision_workspace": frozenset({"grant_id", "operation_key", "workspace"}),
    "admit_inquiry": frozenset({"grant_id", "operation_key", "inquiry"}),
    "create_artifact": frozenset({"grant_id", "operation_key", "artifact"}),
    "commit_markdown": frozenset({"grant_id", "operation_key", "artifact"}),
    "archive_artifact": frozenset({"grant_id", "operation_key", "artifact"}),
    "tombstone": frozenset({"grant_id", "operation_key", "artifact"}),
}

NESTED_FIELDS: dict[str, dict[str, frozenset[str]]] = {
    "issue_grant": {"grant": _NESTED_GRANT},
    "provision_workspace": {"workspace": _NESTED_WORKSPACE},
    "admit_inquiry": {"inquiry": _NESTED_INQUIRY},
    "create_artifact": {"artifact": _NESTED_ARTIFACT_CREATE},
    "commit_markdown": {"artifact": _NESTED_ARTIFACT_COMMIT},
    "archive_artifact": {"artifact": _NESTED_ARTIFACT_LIFECYCLE},
    "tombstone": {"artifact": _NESTED_ARTIFACT_LIFECYCLE},
}


class RequestRefused(Exception):
    """A refused request, carrying the exact payload the caller receives."""

    def __init__(self, payload: dict[str, Any]) -> None:
        super().__init__(str(payload.get("reason") or payload.get("detail")))
        self.payload = payload


def refusal(status: str, reason: str, detail: str = "", **extra: Any) -> dict[str, Any]:
    payload = {"status": status, "reason": reason, "detail": detail}
    payload.update(extra)
    return payload


def _type_ok(value: Any, expected: Any) -> bool:
    if isinstance(value, bool) and expected is int:
        return False
    return isinstance(value, expected)


def validate_request(request: Any, *, max_payload_bytes: int = DEFAULT_MAX_PAYLOAD_BYTES) -> dict[str, Any]:
    """Typed, fail-closed request validation.  Unknown fields are refused, not ignored."""
    if not isinstance(request, Mapping):
        raise RequestRefused(refusal("DENY", "REQUEST_NOT_AN_OBJECT",
                                     "request must be a JSON object"))
    op = request.get("op")
    if not isinstance(op, str) or not op:
        raise RequestRefused(refusal("DENY", "OP_REQUIRED", "op must be a non-empty string"))
    if op not in OPS:
        raise RequestRefused(refusal("DENY", "UNKNOWN_OPERATION", f"unknown op {op!r}", op=op))
    try:
        assert_no_self_authority(request)
    except SelfAuthorityRefused as exc:
        raise RequestRefused(refusal("DENY", "IDENTITY_FIELD_IN_REQUEST", str(exc))) from exc
    schema = FIELD_TYPES[op]
    unknown = sorted(set(request) - set(schema) - {"op"})
    if unknown:
        raise RequestRefused(refusal("DENY", "UNKNOWN_REQUEST_FIELD",
                                     f"unknown request field(s) {unknown}", fields=unknown))
    missing = sorted(name for name in REQUIRED_FIELDS.get(op, frozenset()) if name not in request)
    if missing:
        raise RequestRefused(refusal("DENY", f"FIELD_REQUIRED:{missing[0]}",
                                     f"required field(s) {missing}", fields=missing))
    for name, expected in schema.items():
        if name in request and not _type_ok(request[name], expected):
            raise RequestRefused(refusal("DENY", f"FIELD_TYPE:{name}",
                                         f"field {name} must be {expected}"))
    for name, allowed in NESTED_FIELDS.get(op, {}).items():
        body = request.get(name)
        if body is None:
            continue
        nested_unknown = sorted(set(body) - set(allowed))
        if nested_unknown:
            raise RequestRefused(refusal("DENY", "UNKNOWN_REQUEST_FIELD",
                                         f"unknown {name} field(s) {nested_unknown}",
                                         fields=nested_unknown))
    encoded = json.dumps(request, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    if len(encoded.encode("utf-8")) > int(max_payload_bytes):
        raise RequestRefused(refusal("DENY", "PAYLOAD_TOO_LARGE",
                                     f"request exceeds {int(max_payload_bytes)} bytes",
                                     limit=int(max_payload_bytes)))
    return dict(request)


def peer_uid_from_socket(sock: socket.socket) -> int | None:
    """SO_PEERCRED: the identity source.  Never a JSON field.  None means fail closed."""
    if not hasattr(socket, "SO_PEERCRED"):
        return None
    try:
        _, uid, _ = struct.unpack("3i", sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED,
                                                        struct.calcsize("3i")))
        return int(uid)
    except OSError:
        return None


# ---------------------------------------------------------------------------------
# the service
# ---------------------------------------------------------------------------------


class LifeSupplyService:
    """Governance + Workspace + ManagedArtifact on one canonical database, one socket."""

    def __init__(self, data_root: str | Path, *, operator_uids: Iterable[int] = (),
                 service_uids: Iterable[int] = (), read_only: bool = False,
                 writer_instance: str | None = None, switches: FeatureSwitches | None = None,
                 allowed_subjects: Iterable[str] | None = None,
                 max_payload_bytes: int = DEFAULT_MAX_PAYLOAD_BYTES,
                 require_non_root_identity: bool = False,
                 clock: Callable[[], float] = time.time) -> None:
        self.root = Path(data_root)
        self.clock = clock
        self.read_only = bool(read_only)
        self.operator_uids = frozenset(int(uid) for uid in operator_uids)
        self.service_uids = frozenset(int(uid) for uid in service_uids)
        self.allowed_subjects = None if allowed_subjects is None else frozenset(str(s) for s in allowed_subjects)
        self.max_payload_bytes = int(max_payload_bytes)
        self.switches = switches if switches is not None else FeatureSwitches.from_env()
        self.writer_instance = writer_instance or f"{socket.gethostname()}:{os.getpid()}"
        self.db_path = self.root / CANONICAL_FILENAME
        self.fences: dict[str, WriterFence] = {}
        self.db: LifeSupplyDB | None = None
        self.gov: GovernanceAuthority | None = None
        self.workspace: WorkspaceStore | None = None
        self.artifact: ManagedArtifactStore | None = None
        self.startup_error: str | None = None
        self.startup_recovery: dict[str, Any] = {"status": "OK"}
        self.state = RuntimeStateMachine(clock=clock)
        # A forbidden module or an aliased identity refuses startup outright (PHASE 11/12);
        # only configuration/schema/recovery failures degrade into FAILED.
        assert_only_hosted_owners()
        assert_separation()
        try:
            if require_non_root_identity:
                for label, uids in (("operator", self.operator_uids), ("service", self.service_uids)):
                    if not uids or 0 in uids:
                        raise ServiceConfigRefused(f"DEDICATED_NON_ROOT_{label.upper()}_UID_REQUIRED")
            self.root.mkdir(parents=True, exist_ok=True)
            if not self.read_only:
                for owner in OWNERS:
                    self.fences[owner] = WriterFence(owner, self.root / "recovery" / "fences",
                                                     instance_id=self.writer_instance)
            self.db = LifeSupplyDB(self.db_path, fences=self.fences, read_only=self.read_only,
                                   clock=self.clock)
            self.gov = GovernanceAuthority(db=self.db, writer_fence=self.fences.get("Governance"),
                                           read_only=self.read_only, clock=self.clock)
            self.workspace = WorkspaceStore(db=self.db, writer_fence=self.fences.get("Workspace"),
                                            read_only=self.read_only, clock=self.clock)
            self.artifact = ManagedArtifactStore(
                db=self.db, projection_root=self.root / "projection",
                workspace_authority=self.workspace,
                writer_fence=self.fences.get("ManagedArtifact"), read_only=self.read_only,
                clock=self.clock)
        except Exception as exc:  # unrecoverable startup failure -> FAILED, never READY
            self._release_fences()
            self.startup_error = f"{type(exc).__name__}: {exc}"
            self.state.transition(FAILED, "STARTUP_FAILED", self.startup_error)
            return
        if self.read_only:
            self.state.transition(READ_ONLY, "READ_ONLY_MODE")
            return
        self.state.transition(RECOVERING, "SCHEMA_COMPATIBLE_AND_FENCES_HELD")
        self.startup_recovery = self._recover()
        if self.startup_recovery.get("status") != "OK":
            self.state.transition("FENCED", str(self.startup_recovery.get("reason") or "RECOVERY_BLOCKED"),
                                  self.startup_recovery.get("detail"))
        else:
            self.state.transition(READY, "RECOVERY_COMPLETE")

    # -- lifecycle -------------------------------------------------------------

    def _recover(self) -> dict[str, Any]:
        assert self.gov is not None and self.artifact is not None
        unresolved = self._unresolved()
        if unresolved:
            return {"status": "BLOCKED", "reason": "UNRESOLVED_GOVERNANCE_OPERATIONS",
                    "detail": f"{len(unresolved)} unresolved Governance operation(s)",
                    "unresolved": unresolved}
        recovery = self.artifact.recover_dirty_projections()
        if recovery.get("status") != "OK":
            return {"status": "BLOCKED", "reason": "PROJECTION_RECOVERY_INCOMPLETE",
                    "detail": str(recovery.get("detail"))}
        return {"status": "OK", "recovered": recovery.get("recovered", []),
                "unresolved": 0}

    def _unresolved(self) -> list[dict[str, Any]]:
        if self.gov is None:
            return []
        entries: list[dict[str, Any]] = []
        for row in self.gov.unresolved_reservations():
            entries.append({"owner": "Governance", "kind": "RESERVATION", **row})
        for row in self.gov.unresolved_executions():
            entries.append({"owner": "Governance", "kind": "EXECUTION", **row})
        return entries

    def _release_fences(self) -> None:
        for fence in self.fences.values():
            try:
                fence.release()
            except Exception:  # pragma: no cover - release must never mask a failure
                pass
        self.fences = {}

    def close(self) -> None:
        self._release_fences()

    def __enter__(self) -> "LifeSupplyService":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    # -- state ----------------------------------------------------------------

    def fence_writer(self, reason: str = "OPERATOR_FENCED", detail: Any = None) -> str:
        """Move a READY service to FENCED: writes stop, reads continue."""
        if self.state.state == READY:
            return self.state.transition("FENCED", reason, detail)
        return self.state.state

    def _require_ready_for_write(self, owner: str) -> Any:
        if self.startup_error is not None or self.state.state == FAILED:
            raise RequestRefused(refusal("BLOCKED", "SERVICE_FAILED", self.startup_error or ""))
        if not self.state.may_write():
            raise RequestRefused(refusal("BLOCKED", self.state.write_refusal_reason(),
                                         f"service state is {self.state.state}"))
        fence = self.fences.get(owner)
        if fence is None:
            raise RequestRefused(refusal("BLOCKED", "SERVICE_READ_ONLY",
                                         f"no writer lease for {owner}"))
        try:
            identity = owner_writer_for_fence(owner, fence)
        except StaleWriter as exc:
            self.fence_writer("WRITER_SUPERSEDED", str(exc))
            raise RequestRefused(refusal("BLOCKED", "SERVICE_FENCED", str(exc))) from exc
        assert_matches_fence(identity, fence)
        return identity

    # -- identity --------------------------------------------------------------

    def caller_identities(self, peer_uid: int | None) -> tuple[Identity, ...]:
        """The identities this peer credential may act as.  Never a request field.

        A configured peer normally holds exactly one role (a dedicated operator uid, a
        dedicated service uid).  A sandbox may give one uid both roles; the operation's own
        ACL still decides which single identity performs it, so a service principal can never
        mint authority and an operator can never act as the service.
        """
        if peer_uid is None:
            raise RequestRefused(refusal("DENY", "PEER_CREDENTIAL_UNAVAILABLE",
                                         "SO_PEERCRED is unavailable; refusing to guess"))
        identities: list[Identity] = []
        if peer_uid in self.operator_uids:
            identities.append(host_operator(peer_uid))
        if peer_uid in self.service_uids:
            identities.append(service_principal(peer_uid))
        if not identities:
            raise RequestRefused(refusal("DENY", "PEER_CREDENTIAL_REJECTED",
                                         f"uid {peer_uid} is not an allowed peer"))
        return tuple(identities)

    def subject_identity(self, subject_id: Any) -> Identity:
        if not isinstance(subject_id, str) or not subject_id:
            raise RequestRefused(refusal("DENY", "SUBJECT_REQUIRED", "subject_id is required"))
        if self.allowed_subjects is not None and subject_id not in self.allowed_subjects:
            raise RequestRefused(refusal("DENY", "SUBJECT_NOT_OWNED",
                                         f"subject {subject_id!r} is not owned by this service"))
        return chiyo_subject(subject_id)

    def _authorize(self, op: str, identities: tuple[Identity, ...]) -> Identity:
        allowed = OP_CALLERS[op]
        for identity in identities:
            if identity.kind in allowed:
                return identity
        if allowed == frozenset({KIND_HOST_OPERATOR}):
            raise RequestRefused(refusal("DENY", "AUTHORITY_MINT_REQUIRES_HOST_OPERATOR",
                                         f"{op} is host-operator only"))
        raise RequestRefused(refusal("DENY", "SERVICE_PRINCIPAL_REQUIRED",
                                     f"{op} requires the Life Supply service principal"))

    # -- dispatch --------------------------------------------------------------

    def dispatch(self, request: Any, *, peer_uid: int | None) -> dict[str, Any]:
        try:
            req = validate_request(request, max_payload_bytes=self.max_payload_bytes)
        except RequestRefused as exc:
            return exc.payload
        op = req["op"]
        if op not in {"health", "status"}:
            if self.startup_error is not None or self.state.state == FAILED:
                return refusal("BLOCKED", "SERVICE_FAILED", self.startup_error or "")
            if op in COMMAND_OPS and not self.state.may_write():
                return refusal("BLOCKED", self.state.write_refusal_reason(),
                               f"service state is {self.state.state}",
                               state=self.state.state)
            if op in READ_OPS and not self.state.may_read():
                return refusal("BLOCKED", self.state.write_refusal_reason(),
                               f"service state is {self.state.state}",
                               state=self.state.state)
        try:
            caller = self._authorize(op, self.caller_identities(peer_uid))
            handler = getattr(self, f"_op_{op}")
            return handler(req, caller=caller, peer_uid=peer_uid)
        except RequestRefused as exc:
            return exc.payload
        except (IdentityError, IdentityNotInterchangeable) as exc:
            return refusal("DENY", "IDENTITY_REFUSED", str(exc))
        except StaleWriter as exc:
            self.fence_writer("WRITER_SUPERSEDED", str(exc))
            return refusal("BLOCKED", "SERVICE_FENCED", str(exc))
        except UnitOfWorkDoomed as exc:
            return refusal("BLOCKED", "UNIT_OF_WORK_ROLLED_BACK", str(exc))
        except sqlite3.IntegrityError as exc:
            if "life_supply_uow_journal" in str(exc):
                return refusal("BLOCKED", "OPERATION_ALREADY_COMMITTED",
                               "this coordinated command already committed with that operation id")
            return refusal("UNKNOWN", "SERVICE_ERROR", f"{type(exc).__name__}: {exc}")
        except Exception as exc:  # never leak a traceback to a socket caller
            return refusal("UNKNOWN", "SERVICE_ERROR", f"{type(exc).__name__}: {exc}")

    # -- shared helpers --------------------------------------------------------

    @property
    def gov_store(self) -> GovernanceAuthority:
        if self.gov is None:
            raise RequestRefused(refusal("BLOCKED", "SERVICE_FAILED", "Governance is unavailable"))
        return self.gov

    @property
    def workspace_store(self) -> WorkspaceStore:
        if self.workspace is None:
            raise RequestRefused(refusal("BLOCKED", "SERVICE_FAILED", "Workspace is unavailable"))
        return self.workspace

    @property
    def artifact_store(self) -> ManagedArtifactStore:
        if self.artifact is None:
            raise RequestRefused(refusal("BLOCKED", "SERVICE_FAILED", "ManagedArtifact is unavailable"))
        return self.artifact

    def _active_workspace(self, subject: str) -> dict[str, Any]:
        row = self.workspace_store.get_workspace(subject)
        if row is None:
            raise RequestRefused(refusal("NOT_FOUND", "WORKSPACE_NOT_FOUND",
                                         f"no workspace for subject {subject!r}"))
        if row["lifecycle"] != "ACTIVE":
            raise RequestRefused(refusal("BLOCKED", "WORKSPACE_NOT_ACTIVE",
                                         f"workspace lifecycle is {row['lifecycle']}"))
        return row

    def _require_subject_owned_artifact(self, artifact_id: str, subject: str) -> dict[str, Any]:
        row = self.artifact_store.get_artifact_for_subject(artifact_id, subject)
        if row is None:
            raise RequestRefused(refusal("NOT_FOUND", "ARTIFACT_NOT_FOUND",
                                         "artifact/subject mismatch or unknown artifact"))
        return row

    def _reserve(self, uow: Any, *, grant_id: str, subject: str, capability: str, scope: str,
                 target: str, operation_key: str, payload: Any, actor_uid: int | None,
                 authority_scope: str) -> Any:
        """Governance reservation + execution fence + consumption, inside the command."""
        gov = self.gov_store
        reserved = gov.reserve_use(grant_id, subject=subject, capability=capability, scope=scope,
                                   target=target, action_key=operation_key, payload=payload,
                                   actor_uid=actor_uid, authority_scope=authority_scope)
        uow.step(reserved, owner="Governance", action="reserve_use")
        if not reserved.allowed:
            raise RequestRefused(refusal(reserved.status or "DENY", "GOVERNANCE_REFUSED",
                                         reserved.detail or "reserve_use refused",
                                         stage="reserve_use", grant_id=grant_id))
        uow.param("reservation_id", reserved.reservation_id)
        begun = gov.begin_execution(reserved.reservation_id, actor_uid=actor_uid)
        uow.step(begun, owner="Governance", action="begin_execution")
        if not begun.allowed:
            raise RequestRefused(refusal(begun.status or "DENY", "GOVERNANCE_REFUSED",
                                         begun.detail or "begin_execution refused",
                                         stage="begin_execution"))
        consumed = gov.consume_use(reserved.reservation_id, actor_uid=actor_uid)
        uow.step(consumed, owner="Governance", action="consume_use")
        if not consumed.allowed:
            raise RequestRefused(refusal(consumed.status or "DENY", "GOVERNANCE_REFUSED",
                                         consumed.detail or "consume_use refused",
                                         stage="consume_use"))
        return reserved

    # -- PHASE 14: reads (no write lease, no epoch, no unit of work) ----------

    def _op_health(self, req, *, caller, peer_uid) -> dict[str, Any]:
        return self._status_payload()

    def _op_status(self, req, *, caller, peer_uid) -> dict[str, Any]:
        return self._status_payload()

    def _status_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "service": "chiyo-life-supply",
            "build_id": BUILD_ID,
            "schema_layout": SCHEMA_LAYOUT,
            "canonical_filename": CANONICAL_FILENAME,
            "owners": list(OWNERS),
            "read_only": self.read_only,
            "writer_instance": self.writer_instance,
            "switches": self.switches.as_dict(),
            "state": self.state.status(),
            "canonical_db": str(self.db_path),
        }
        if self.startup_error is not None:
            payload.update({"status": "FAILED", "service_state": FAILED,
                            "detail": self.startup_error})
            return payload
        unresolved = self._unresolved()
        payload.update({
            "status": "OK",
            "service_state": self.state.state,
            "unresolved_operations": len(unresolved),
            "writers": {owner: fence.status() for owner, fence in self.fences.items()},
            "workspace_count": self.workspace_store.workspace_count(),
            "artifact_count": self.artifact_store.artifact_count(),
            "startup_recovery": self.startup_recovery,
        })
        return payload

    def _op_get_workspace(self, req, *, caller, peer_uid) -> dict[str, Any]:
        subject = req["subject"]
        self.subject_identity(subject)
        row = self.workspace_store.get_workspace(subject)
        if row is None:
            return refusal("NOT_FOUND", "WORKSPACE_NOT_FOUND", f"no workspace for {subject!r}")
        return {"status": "OK", "workspace": row}

    def _op_list_open_inquiries(self, req, *, caller, peer_uid) -> dict[str, Any]:
        subject = req["subject"]
        self.subject_identity(subject)
        rows = self.workspace_store.list_active_inquiries_view(subject)
        if rows is None:
            return refusal("UNKNOWN", "OWNER_READ_UNAVAILABLE",
                           "Workspace owner read unavailable in this mode")
        return {"status": "OK", "inquiries": rows}

    def _op_list_artifacts(self, req, *, caller, peer_uid) -> dict[str, Any]:
        subject = req["subject"]
        self.subject_identity(subject)
        rows = self.artifact_store.list_artifacts(subject)
        if rows is None:
            return refusal("UNKNOWN", "OWNER_READ_UNAVAILABLE",
                           "ManagedArtifact owner read unavailable")
        return {"status": "OK", "artifacts": rows}

    def _op_read_artifact(self, req, *, caller, peer_uid) -> dict[str, Any]:
        subject, artifact_id = req["subject"], req["artifact_id"]
        self.subject_identity(subject)
        artifact = self.artifact_store.get_artifact_for_subject(artifact_id, subject)
        if artifact is None:
            return refusal("NOT_FOUND", "ARTIFACT_NOT_FOUND", "artifact/subject mismatch")
        if artifact["lifecycle"] == "TOMBSTONED":
            return refusal("NOT_FOUND", "ARTIFACT_TOMBSTONED",
                           "the artifact was tombstoned; its content is not served")
        head = self.artifact_store.read_head(artifact_id)
        return {"status": "OK", "artifact": artifact, "head": head,
                "content": None if head is None else head.get("content")}

    def _op_read_head(self, req, *, caller, peer_uid) -> dict[str, Any]:
        artifact_id = req["artifact_id"]
        subject = req.get("subject")
        if subject is not None:
            self.subject_identity(subject)
            if self.artifact_store.get_artifact_for_subject(artifact_id, subject) is None:
                return refusal("NOT_FOUND", "ARTIFACT_NOT_FOUND", "artifact/subject mismatch")
        head = self.artifact_store.read_head(artifact_id)
        if head is None:
            return refusal("NOT_FOUND", "ARTIFACT_HEAD_NOT_FOUND", "no committed head")
        return {"status": "OK", "head": head}

    def _op_lookup_operation(self, req, *, caller, peer_uid) -> dict[str, Any]:
        owner, operation_id = req["owner"], req["operation_id"]
        if owner not in OWNERS:
            return refusal("DENY", "UNKNOWN_OWNER", f"owner must be one of {list(OWNERS)}")
        if owner == "Governance":
            row = self.gov_store.lookup_operation(operation_id)
        elif owner == "Workspace":
            row = self.workspace_store.lookup_operation(operation_id)
        else:
            row = self.artifact_store.lookup_operation(operation_id)
        if row is None:
            return refusal("NOT_FOUND", "OPERATION_NOT_FOUND", f"{operation_id} is unknown to {owner}")
        return {"status": "KNOWN", "owner": owner, "operation": row}

    def _op_read_effect_receipt(self, req, *, caller, peer_uid) -> dict[str, Any]:
        row = self.artifact_store.read_effect_receipt(req["operation_key"])
        if row is None:
            return refusal("NOT_FOUND", "RECEIPT_NOT_FOUND", "no effect receipt for that key")
        return {"status": "OK", "receipt": row}

    def _op_list_personal_opportunities(self, req, *, caller, peer_uid) -> dict[str, Any]:
        subject = req["subject"]
        self.subject_identity(subject)
        rows = self.workspace_store.personal_opportunities_view(subject)
        if rows is None:
            return refusal("UNKNOWN", "OWNER_READ_UNAVAILABLE", "Workspace owner read unavailable")
        return {"status": "OK", "opportunities": rows}

    def _op_list_active_grants(self, req, *, caller, peer_uid) -> dict[str, Any]:
        rows = self.gov_store.list_active_grants(subject=req.get("subject"))
        if rows is None:
            return refusal("UNKNOWN", "OWNER_READ_UNAVAILABLE", "Governance owner read unavailable")
        return {"status": "OK", "grants": rows}

    def _op_get_grant(self, req, *, caller, peer_uid) -> dict[str, Any]:
        row = self.gov_store.get_grant(req["grant_id"])
        if row is None:
            return refusal("NOT_FOUND", "GRANT_NOT_FOUND", "unknown grant")
        return {"status": "OK", "grant": row}

    def _op_list_unresolved_operations(self, req, *, caller, peer_uid) -> dict[str, Any]:
        unresolved = self._unresolved()
        return {"status": "OK", "unresolved": unresolved, "count": len(unresolved)}

    # -- PHASE 15: commands ---------------------------------------------------

    def _op_issue_grant(self, req, *, caller, peer_uid) -> dict[str, Any]:
        require_authority_mint(caller)
        body = dict(req["grant"])
        subject = str(body["subject"])
        operation_id = str(body["operation_id"])
        capability = str(body["capability"])
        if capability not in CAPABILITIES:
            return refusal("DENY", "CAPABILITY_NOT_ALLOWED",
                           f"{capability!r} is not in the narrow allowlist")
        expected_scope = (WORKSPACE_AUTHORITY_SCOPE
                          if capability in {WORKSPACE_CAPABILITY, INQUIRY_CAPABILITY}
                          else ARTIFACT_AUTHORITY_SCOPE)
        supplied_scope = body.get("authority_scope")
        if supplied_scope is not None and supplied_scope != expected_scope:
            return refusal("DENY", "AUTHORITY_SCOPE_MISMATCH",
                           f"capability {capability} derives {expected_scope}")
        self.subject_identity(subject)
        writer = self._require_ready_for_write("Governance")
        try:
            with self.db_store.unit_of_work(command="ISSUE_GRANT", operation_id=operation_id,
                                            subject_id=subject,
                                            caller_principal=SERVICE_PRINCIPAL) as uow:
                decision = self.gov_store.issue_grant(
                    grantor="HOST_OPERATOR", bootstrap_authorized=True, subject=subject,
                    capability=capability, scope=str(body["scope"]), target=str(body["target"]),
                    authority_scope=expected_scope, payload_binding=body.get("payload_binding"),
                    valid_from=body.get("valid_from"), expires_at=body.get("expires_at"),
                    use_mode=str(body.get("use_mode") or "REUSABLE"),
                    max_uses=body.get("max_uses"), delegation_allowed=False,
                    meaning_version=str(body.get("meaning_version") or "governance.permission_grant.v1"),
                    grant_id=body.get("grant_id"), operation_id=operation_id,
                    caller_principal=SERVICE_PRINCIPAL, writer_instance=writer.instance_id,
                    writer_epoch=writer.epoch)
                uow.step(decision, owner="Governance", action="issue_grant")
                if decision.status not in {"ALLOW", "NO_CHANGE"}:
                    raise RequestRefused(refusal(decision.status or "DENY", "GOVERNANCE_REFUSED",
                                                 decision.detail or "issue_grant refused",
                                                 stage="issue_grant"))
                uow.param("grant_id", decision.grant_id)
                uow.note("Governance", "permission_grants", "grant issued", decision.grant_id)
                result = dict(decision.to_dict())
                result["writer"] = {"owner": "Governance", "instance_id": writer.instance_id,
                                    "epoch": writer.epoch}
        except RequestRefused as exc:
            return exc.payload
        result["operation_id"] = operation_id
        return result

    def _op_revoke_grant(self, req, *, caller, peer_uid) -> dict[str, Any]:
        require_authority_mint(caller)
        grant_id = req["grant_id"]
        expected_revision = int(req["expected_revision"])
        operation_id = req["operation_id"]
        writer = self._require_ready_for_write("Governance")
        try:
            with self.db_store.unit_of_work(command="REVOKE_GRANT", operation_id=operation_id,
                                            caller_principal=SERVICE_PRINCIPAL) as uow:
                decision = self.gov_store.revoke_grant(
                    grant_id, expected_revision=expected_revision, operation_id=operation_id,
                    writer_instance=writer.instance_id, writer_epoch=writer.epoch)
                # db.COMMAND_CONTRACTS["REVOKE_GRANT"] declares this step as ALLOW|NO_CHANGE while
                # the owner returns REVOKED on success.  The contract-conformant step status is
                # declared here, once, and the raw owner status is recorded in the journal facts
                # so nothing is hidden from a later reader.
                uow.step({"status": "ALLOW" if decision.status == "REVOKED" else decision.status},
                         owner="Governance", action="revoke_grant")
                if decision.status not in {"REVOKED", "NO_CHANGE"}:
                    raise RequestRefused(refusal(decision.status or "DENY", "GOVERNANCE_REFUSED",
                                                 decision.detail or "revoke_grant refused",
                                                 stage="revoke_grant"))
                uow.param("grant_id", grant_id)
                uow.note("Governance", "permission_grants", "grant revoked",
                         {"store_status": decision.status, "revision": decision.revision})
                result = dict(decision.to_dict())
        except RequestRefused as exc:
            return exc.payload
        result["operation_id"] = operation_id
        return result

    def _op_provision_workspace(self, req, *, caller, peer_uid) -> dict[str, Any]:
        gated = self.switches.require("WORKSPACE_PROVISION")
        if gated is not None:
            return gated
        body = dict(req["workspace"])
        subject = str(body["subject_id"])
        operation_key = req["operation_key"]
        grant_id = req["grant_id"]
        self.subject_identity(subject)
        visibility = str(body.get("visibility") or "PRIVATE")
        artifact_scope = str(body.get("artifact_scope") or "PERSONAL")
        payload = {"op": "PROVISION", "subject": subject, "grant": grant_id,
                   "visibility": visibility, "artifact_scope": artifact_scope}
        target = f"workspace:{subject}"
        self._require_ready_for_write("Workspace")
        try:
            with self.db_store.unit_of_work(command="PROVISION_WORKSPACE", operation_id=operation_key,
                                            subject_id=subject,
                                            caller_principal=SERVICE_PRINCIPAL) as uow:
                uow.param("subject_id", subject)
                uow.param("operation_key", operation_key)
                uow.param("grant_id", grant_id)
                validated = self.gov_store.validate_grant(
                    grant_id, subject=subject, capability=WORKSPACE_CAPABILITY,
                    scope=WORKSPACE_SCOPE, target=target, expected_revision=None, payload=payload)
                uow.step(validated, owner="Governance", action="validate_grant")
                if not validated.allowed:
                    raise RequestRefused(refusal(validated.status or "DENY", "GOVERNANCE_REFUSED",
                                                 validated.detail or "validate_grant refused",
                                                 stage="validate_grant"))
                reserved = self._reserve(
                    uow, grant_id=grant_id, subject=subject, capability=WORKSPACE_CAPABILITY,
                    scope=WORKSPACE_SCOPE, target=target, operation_key=operation_key,
                    payload=payload, actor_uid=peer_uid,
                    authority_scope=WORKSPACE_AUTHORITY_SCOPE)
                created = self.workspace_store.provision_workspace(
                    subject_id=subject, grant_id=grant_id, operation_key=operation_key,
                    visibility=visibility, artifact_scope=artifact_scope)
                uow.step(created, owner="Workspace", action="provision_workspace")
                if created.get("status") not in {"OK", "NO_CHANGE"}:
                    raise RequestRefused(refusal(created.get("status") or "UNKNOWN",
                                                 "WORKSPACE_REFUSED",
                                                 str(created.get("detail") or ""),
                                                 stage="provision_workspace"))
                workspace = created.get("workspace") or self.workspace_store.get_workspace(subject)
                uow.note("Workspace", "workspaces",
                         "provisioned" if created.get("status") == "OK" else "already provisioned",
                         None if not workspace else workspace.get("workspace_id"))
                result = {"status": created.get("status"), "workspace": workspace,
                          "reservation_id": reserved.reservation_id,
                          "operation_key": operation_key, "uow_id": uow.uow_id}
        except RequestRefused as exc:
            return exc.payload
        return result

    def _op_admit_inquiry(self, req, *, caller, peer_uid) -> dict[str, Any]:
        gated = self.switches.require("INQUIRY_ADMISSION")
        if gated is not None:
            return gated
        body = dict(req["inquiry"])
        subject = str(body["subject_id"])
        operation_key, grant_id = req["operation_key"], req["grant_id"]
        self.subject_identity(subject)
        workspace = self._active_workspace(subject)
        if body.get("workspace_id") and body["workspace_id"] != workspace["workspace_id"]:
            return refusal("DENY", "ARTIFACT_WORKSPACE_MISMATCH", "workspace id mismatch")
        topic = str(body.get("topic") or "").strip()
        if not topic:
            return refusal("DENY", "FIELD_REQUIRED:topic", "topic is required")
        source_refs = body.get("source_refs") or []
        if not isinstance(source_refs, list) or not all(isinstance(ref, str) for ref in source_refs):
            return refusal("DENY", "FIELD_TYPE:source_refs", "source_refs must be a list of strings")
        payload = {"operation": "admit_inquiry", "subject_id": subject,
                   "workspace_id": workspace["workspace_id"], "topic": topic,
                   "source_refs": source_refs}
        target = f"workspace:{workspace['workspace_id']}"
        self._require_ready_for_write("Workspace")
        try:
            with self.db_store.unit_of_work(command="ADMIT_INQUIRY", operation_id=operation_key,
                                            subject_id=subject,
                                            caller_principal=SERVICE_PRINCIPAL) as uow:
                uow.param("subject_id", subject)
                uow.param("operation_key", operation_key)
                uow.param("grant_id", grant_id)
                self._reserve(uow, grant_id=grant_id, subject=subject,
                              capability=INQUIRY_CAPABILITY, scope=INQUIRY_SCOPE, target=target,
                              operation_key=operation_key, payload=payload, actor_uid=peer_uid,
                              authority_scope=WORKSPACE_AUTHORITY_SCOPE)
                proposal = self.workspace_store.submit_proposal(
                    subject_id=subject, topic=topic, source_refs=source_refs,
                    driver=str(body.get("driver") or "OPERATOR"),
                    review_after=body.get("review_after"), expires_at=body.get("expires_at"),
                    operation_key=operation_key)
                uow.step(proposal, owner="Workspace", action="submit_proposal")
                raise RequestRefused(refusal("BLOCKED", "INQUIRY_ADMISSION_OWNER_STUB",
                                             str(proposal.get("detail") or "owner not staged"),
                                             owner_status=proposal.get("status")))
        except RequestRefused as exc:
            return exc.payload

    def _op_create_artifact(self, req, *, caller, peer_uid) -> dict[str, Any]:
        gated = self.switches.require("ARTIFACT_EXTERNAL_ACTION")
        if gated is not None:
            return gated
        body = dict(req["artifact"])
        subject = str(body["subject_id"])
        operation_key, grant_id = req["operation_key"], req["grant_id"]
        self.subject_identity(subject)
        title = str(body["title"]).strip()
        content = body.get("content") or ""
        if not title:
            return refusal("DENY", "FIELD_REQUIRED:title", "title is required")
        if not isinstance(content, str):
            return refusal("DENY", "FIELD_TYPE:content", "content must be a string")
        workspace = self._active_workspace(subject)
        workspace_id = workspace["workspace_id"]
        if body["workspace_id"] != workspace_id:
            return refusal("DENY", "ARTIFACT_WORKSPACE_MISMATCH",
                           "artifact workspace does not belong to this subject")
        target = f"workspace:{workspace_id}"
        first_version_key = f"{operation_key}#v1"
        # The reservation binds the caller's exact request: title plus the first version's bytes.
        reserve_payload = {"operation": "create_artifact", "subject_id": subject,
                           "workspace_id": workspace_id, "title": title,
                           "first_version_content": content}
        create_payload = {"operation": "create_artifact", "subject_id": subject,
                          "workspace_id": workspace_id, "title": title}
        self._require_ready_for_write("ManagedArtifact")
        try:
            with self.db_store.unit_of_work(command="COMMIT_MARKDOWN", operation_id=operation_key,
                                            subject_id=subject,
                                            caller_principal=SERVICE_PRINCIPAL) as uow:
                uow.param("subject_id", subject)
                uow.param("grant_id", grant_id)
                reserved = self._reserve(
                    uow, grant_id=grant_id, subject=subject, capability=ARTIFACT_CAPABILITY,
                    scope=ARTIFACT_SCOPE, target=target, operation_key=operation_key,
                    payload=reserve_payload, actor_uid=peer_uid,
                    authority_scope=ARTIFACT_AUTHORITY_SCOPE)
                created = self.artifact_store.create_artifact(
                    subject_id=subject, workspace_id=workspace_id, title=title,
                    operation_key=operation_key, grant_reservation_id=reserved.reservation_id,
                    operation_payload=create_payload)
                uow.step(created, owner="ManagedArtifact", action="create_artifact")
                if created.get("status") not in {"OK", "NO_CHANGE"}:
                    raise RequestRefused(refusal(created.get("status") or "UNKNOWN",
                                                 "ARTIFACT_REFUSED",
                                                 str(created.get("detail") or ""),
                                                 stage="create_artifact"))
                artifact_id = created.get("artifact_id")
                commit_payload = {"operation": "commit_markdown", "subject_id": subject,
                                  "artifact_id": artifact_id, "expected_head": None,
                                  "content": content}
                uow.param("artifact_id", artifact_id)
                uow.param("operation_key", first_version_key)
                committed = self.artifact_store.commit_markdown(
                    artifact_id=artifact_id, authorized_subject_id=subject, expected_head=None,
                    content=content, operation_key=first_version_key,
                    grant_reservation_id=reserved.reservation_id,
                    operation_payload=commit_payload)
                uow.step(committed, owner="ManagedArtifact", action="commit_markdown")
                if committed.get("status") not in {"OK", "NO_CHANGE"}:
                    raise RequestRefused(refusal(committed.get("status") or "UNKNOWN",
                                                 "ARTIFACT_REFUSED",
                                                 str(committed.get("detail") or ""),
                                                 stage="commit_markdown"))
                uow.note("ManagedArtifact", "managed_artifacts", "created", artifact_id)
                if committed.get("version_id"):
                    uow.note("ManagedArtifact", "artifact_versions", "first version committed",
                             committed["version_id"])
                result = {"status": "OK", "artifact_id": artifact_id,
                          "workspace_id": workspace_id, "title": title,
                          "version_id": committed.get("version_id"),
                          "version_number": committed.get("version_number"),
                          "operation_key": operation_key,
                          "first_version_operation_key": first_version_key,
                          "reservation_id": reserved.reservation_id, "uow_id": uow.uow_id}
        except RequestRefused as exc:
            return exc.payload
        return result

    def _op_commit_markdown(self, req, *, caller, peer_uid) -> dict[str, Any]:
        gated = self.switches.require("ARTIFACT_EXTERNAL_ACTION")
        if gated is not None:
            return gated
        body = dict(req["artifact"])
        subject = str(body["subject_id"])
        artifact_id = str(body["artifact_id"])
        operation_key, grant_id = req["operation_key"], req["grant_id"]
        expected_head = body.get("expected_head")
        content = body["content"]
        if not isinstance(content, str):
            return refusal("DENY", "FIELD_TYPE:content", "content must be a string")
        if expected_head is not None and not isinstance(expected_head, str):
            return refusal("DENY", "FIELD_TYPE:expected_head", "expected_head must be a string or null")
        self.subject_identity(subject)
        artifact = self._require_subject_owned_artifact(artifact_id, subject)
        workspace = self._active_workspace(subject)
        if artifact["workspace_id"] != workspace["workspace_id"]:
            return refusal("NOT_FOUND", "ARTIFACT_NOT_FOUND", "artifact/workspace mismatch")
        target = f"workspace:{workspace['workspace_id']}"
        head = self.artifact_store.read_head(artifact_id)
        if (head is not None and expected_head == head["version_id"] and head["content"] == content):
            # A NO_CHANGE commit would create no version, and
            # db.COMMAND_CONTRACTS["COMMIT_MARKDOWN"] rightly refuses to let a coordinated command
            # commit without the version its postcondition requires.  Nothing is written, so
            # nothing needs coordinating: report the owner's own NO_CHANGE outcome.
            return {"status": "NO_CHANGE", "artifact_id": artifact_id,
                    "head_version_id": head["version_id"], "version_created": False,
                    "operation_key": operation_key, "coordinated": False,
                    "detail": "identical content already at head; no canonical write"}
        authorized_payload = {"operation": "commit_markdown", "subject_id": subject,
                              "artifact_id": artifact_id, "expected_head": expected_head,
                              "content": content}
        self._require_ready_for_write("ManagedArtifact")
        try:
            with self.db_store.unit_of_work(command="COMMIT_MARKDOWN", operation_id=operation_key,
                                            subject_id=subject,
                                            caller_principal=SERVICE_PRINCIPAL) as uow:
                uow.param("subject_id", subject)
                uow.param("operation_key", operation_key)
                uow.param("artifact_id", artifact_id)
                uow.param("grant_id", grant_id)
                reserved = self._reserve(
                    uow, grant_id=grant_id, subject=subject, capability=ARTIFACT_CAPABILITY,
                    scope=ARTIFACT_SCOPE, target=target, operation_key=operation_key,
                    payload=authorized_payload, actor_uid=peer_uid,
                    authority_scope=ARTIFACT_AUTHORITY_SCOPE)
                committed = self.artifact_store.commit_markdown(
                    artifact_id=artifact_id, authorized_subject_id=subject,
                    expected_head=expected_head, content=content, operation_key=operation_key,
                    grant_reservation_id=reserved.reservation_id,
                    operation_payload=authorized_payload)
                uow.step(committed, owner="ManagedArtifact", action="commit_markdown")
                if committed.get("status") not in {"OK", "NO_CHANGE"}:
                    raise RequestRefused(refusal(committed.get("status") or "UNKNOWN",
                                                 "ARTIFACT_REFUSED",
                                                 str(committed.get("detail") or ""),
                                                 stage="commit_markdown",
                                                 current_head=committed.get("current_head")))
                if committed.get("version_id"):
                    uow.note("ManagedArtifact", "artifact_versions", "version committed",
                             committed["version_id"])
                result = {"status": committed.get("status"), "artifact_id": artifact_id,
                          "version_id": committed.get("version_id"),
                          "version_number": committed.get("version_number"),
                          "parent_version_id": committed.get("parent_version_id"),
                          "head_version_id": committed.get("head_version_id"),
                          "version_created": committed.get("version_created"),
                          "operation_key": operation_key,
                          "reservation_id": reserved.reservation_id, "uow_id": uow.uow_id,
                          "coordinated": True}
        except RequestRefused as exc:
            return exc.payload
        return result

    def _op_archive_artifact(self, req, *, caller, peer_uid) -> dict[str, Any]:
        return self._artifact_lifecycle(req, peer_uid=peer_uid, lifecycle="ARCHIVED")

    def _op_tombstone(self, req, *, caller, peer_uid) -> dict[str, Any]:
        return self._artifact_lifecycle(req, peer_uid=peer_uid, lifecycle="TOMBSTONED")

    def _artifact_lifecycle(self, req: Mapping[str, Any], *, peer_uid: int | None,
                            lifecycle: str) -> dict[str, Any]:
        gated = self.switches.require("ARTIFACT_EXTERNAL_ACTION")
        if gated is not None:
            return gated
        body = dict(req["artifact"])
        op = "archive_artifact" if lifecycle == "ARCHIVED" else "tombstone_artifact"
        subject = str(body["subject_id"])
        artifact_id = str(body["artifact_id"])
        operation_key, grant_id = req["operation_key"], req["grant_id"]
        expected_revision = int(body["expected_revision"])
        self.subject_identity(subject)
        artifact = self._require_subject_owned_artifact(artifact_id, subject)
        workspace = self._active_workspace(subject)
        if artifact["workspace_id"] != workspace["workspace_id"]:
            return refusal("NOT_FOUND", "ARTIFACT_NOT_FOUND", "artifact/workspace mismatch")
        target = f"workspace:{workspace['workspace_id']}"
        authorized_payload = {"operation": op, "subject_id": subject, "artifact_id": artifact_id,
                              "expected_revision": expected_revision}
        self._require_ready_for_write("ManagedArtifact")
        gov = self.gov_store
        reserved = gov.reserve_use(grant_id, subject=subject, capability=ARTIFACT_CAPABILITY,
                                   scope=ARTIFACT_SCOPE, target=target, action_key=operation_key,
                                   payload=authorized_payload, actor_uid=peer_uid,
                                   authority_scope=ARTIFACT_AUTHORITY_SCOPE)
        if not reserved.allowed:
            return refusal(reserved.status or "DENY", "GOVERNANCE_REFUSED",
                           reserved.detail or "reserve_use refused", stage="reserve_use")
        begun = gov.begin_execution(reserved.reservation_id, actor_uid=peer_uid)
        if not begun.allowed:
            gov.finalize_no_effect(reserved.reservation_id, actor_uid=peer_uid)
            return refusal(begun.status or "DENY", "GOVERNANCE_REFUSED",
                           begun.detail or "begin_execution refused", stage="begin_execution")
        if lifecycle == "ARCHIVED":
            result = self.artifact_store.archive(
                artifact_id, authorized_subject_id=subject, expected_revision=expected_revision,
                operation_key=operation_key, grant_reservation_id=reserved.reservation_id,
                operation_payload=authorized_payload)
        else:
            result = self.artifact_store.tombstone(
                artifact_id, authorized_subject_id=subject, expected_revision=expected_revision,
                operation_key=operation_key, grant_reservation_id=reserved.reservation_id,
                operation_payload=authorized_payload)
        if result.get("status") in {"OK", "NO_CHANGE"}:
            consumed = gov.consume_use(reserved.reservation_id, actor_uid=peer_uid)
            if not consumed.allowed:
                return refusal("UNKNOWN", "GOVERNANCE_FINALIZATION_UNCERTAIN",
                               "owner effect persisted but grant consumption is uncertain",
                               result=result)
        else:
            gov.finalize_no_effect(reserved.reservation_id, actor_uid=peer_uid)
        result = dict(result)
        result["operation_key"] = operation_key
        result["reservation_id"] = reserved.reservation_id
        # db.CROSS_OWNER_COMMANDS has no artifact-lifecycle command, so this path cannot use one
        # coordinated transaction; the caller is told exactly that instead of being misled.
        result["coordinated"] = False
        result["detail"] = (str(result.get("detail") or "")
                            + " | owner API only: CROSS_OWNER_COMMANDS carries no artifact-lifecycle "
                              "command, so the Governance reservation and the owner effect are not "
                              "one SQLite transaction").strip()
        return result

    # -- the single database ---------------------------------------------------

    @property
    def db_store(self) -> LifeSupplyDB:
        if self.db is None:
            raise RequestRefused(refusal("BLOCKED", "SERVICE_FAILED", self.startup_error or ""))
        return self.db

    def snapshot_epochs(self) -> dict[str, Any]:
        """Read-only epoch observation, used by status and by tests."""
        with self.db_store.connection() as con:
            rows = con.execute("SELECT owner,active_epoch,instance_id,writer_token "
                               "FROM c6_writer_epoch ORDER BY owner").fetchall()
            journal = con.execute("SELECT COUNT(*) FROM life_supply_uow_journal").fetchone()[0]
        return {"epochs": {row["owner"]: {"active_epoch": int(row["active_epoch"]),
                                          "instance_id": row["instance_id"],
                                          "writer_token": row["writer_token"]} for row in rows},
                "uow_journal_rows": int(journal)}


# ---------------------------------------------------------------------------------
# transport (PHASE 13)
# ---------------------------------------------------------------------------------


class LifeSupplyRequestHandler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        server = self.server
        server._enter_request()
        try:
            self._serve_one_request()
        finally:
            server._leave_request()

    def _serve_one_request(self) -> None:
        peer_uid = peer_uid_from_socket(self.request)
        limit = int(getattr(self.server, "max_payload_bytes", DEFAULT_MAX_PAYLOAD_BYTES))
        raw = self.rfile.readline(limit + 1)
        if not raw:
            return
        if len(raw) > limit:
            response = refusal("DENY", "PAYLOAD_TOO_LARGE",
                               f"request exceeds {limit} bytes", limit=limit)
        else:
            try:
                request = json.loads(raw.decode("utf-8"))
            except (UnicodeError, ValueError):
                response = refusal("DENY", "MALFORMED_REQUEST",
                                   "request is not valid UTF-8 JSON")
            else:
                service = self.server.service
                response = service.dispatch(request, peer_uid=peer_uid)
        self.wfile.write(json.dumps(response, ensure_ascii=False,
                                    separators=(",", ":")).encode() + b"\n")


class ServiceStoppedBySignal(BaseException):
    """Raised inside the accept loop when the service is asked to stop.

    A stop signal is turned into an exception instead of being left to the interpreter's
    default action, because the default action terminates the process *without* running the
    cleanup path -- which is exactly how a stale socket file used to survive a stop.

    It derives from ``BaseException`` deliberately: the service's own ``except Exception``
    handlers must never swallow a requested stop.
    """

    def __init__(self, signum: int) -> None:
        super().__init__(f"stop signal {int(signum)}")
        self.signum = int(signum)


def _install_stop_handlers() -> dict[int, Any]:
    """Install SIGTERM/SIGINT handlers that unwind the accept loop cleanly."""
    previous: dict[int, Any] = {}

    def _stop(signum: int, _frame: Any) -> None:
        raise ServiceStoppedBySignal(signum)

    for name in ("SIGTERM", "SIGINT"):
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        try:
            previous[int(sig)] = signal.signal(sig, _stop)
        except (ValueError, OSError):
            continue
    return previous


def _restore_stop_handlers(previous: Mapping[int, Any]) -> None:
    for sig, handler in previous.items():
        try:
            signal.signal(sig, handler)
        except (ValueError, OSError, TypeError):
            continue


def socket_is_live(path: Path) -> bool:
    """True only if something is really accepting connections on this socket path."""
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    probe.settimeout(1.0)
    try:
        probe.connect(str(path))
    except OSError:
        return False
    else:
        return True
    finally:
        probe.close()


def prepare_socket_path(path: Path) -> str:
    """Make the socket path bindable, and only ever remove a provably stale socket.

    Refuses -- rather than unlinking -- when the path is not a socket, when it is not owned by
    this uid, or when a live instance is still listening on it.  The writer fencing layer stays
    the final protection for writes; this function only decides whether the *path* is reusable.
    """
    if not path.exists() and not path.is_symlink():
        return "SOCKET_PATH_FREE"
    info = path.lstat()
    if not stat.S_ISSOCK(info.st_mode):
        raise RuntimeError(f"REFUSING_TO_UNLINK_NON_SOCKET:{path}")
    if info.st_uid != os.geteuid():
        raise RuntimeError(f"REFUSING_TO_UNLINK_SOCKET_NOT_OWNED_BY_THIS_UID:{path}")
    if socket_is_live(path):
        raise RuntimeError(f"REFUSING_TO_UNLINK_LIVE_SOCKET:{path}")
    path.unlink()
    return "STALE_SOCKET_REMOVED"


class LifeSupplySocketServer(socketserver.ThreadingUnixStreamServer):
    """Local Unix socket only: no TCP, no management API on the network."""

    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 16

    def __init__(self, path: str, service: LifeSupplyService, *,
                 max_payload_bytes: int = DEFAULT_MAX_PAYLOAD_BYTES,
                 socket_mode: int = DEFAULT_SOCKET_MODE) -> None:
        self.service = service
        self.max_payload_bytes = int(max_payload_bytes)
        self.socket_mode = int(socket_mode)
        self.socket_path = str(path)
        self._inflight = 0
        self._inflight_lock = threading.Lock()
        self._inflight_idle = threading.Event()
        self._inflight_idle.set()
        try:
            super().__init__(str(path), LifeSupplyRequestHandler)
            os.chmod(str(path), self.socket_mode)
            applied = stat.S_IMODE(os.stat(str(path)).st_mode)
            if applied != self.socket_mode:
                raise ServiceConfigRefused(f"SOCKET_MODE_NOT_APPLIED:{oct(applied)}")
        except Exception:
            self.server_close()
            Path(str(path)).unlink(missing_ok=True)
            raise

    # -- in-flight accounting, so a stop can wait for a bounded time ----------------------
    def _enter_request(self) -> None:
        with self._inflight_lock:
            self._inflight += 1
            self._inflight_idle.clear()

    def _leave_request(self) -> None:
        with self._inflight_lock:
            self._inflight -= 1
            if self._inflight <= 0:
                self._inflight_idle.set()

    @property
    def inflight_requests(self) -> int:
        with self._inflight_lock:
            return int(self._inflight)

    def drain(self, timeout: float) -> bool:
        """Wait for in-flight requests to finish.  True if the service went idle in time."""
        if timeout <= 0:
            return bool(self._inflight_idle.is_set())
        return bool(self._inflight_idle.wait(timeout))


def run_service(settings: ServiceSettings, *, service: LifeSupplyService | None = None,
                poll_interval: float = 0.5, drain_timeout: float = 10.0) -> int:
    """Start the independent service on its Unix socket and serve until asked to stop.

    Stop contract (SIGTERM / SIGINT):

    1. stop accepting new connections (the accept loop ends);
    2. give in-flight requests a bounded time to finish, then close the listening socket;
    3. remove *our own* socket file, and only when the path is safe to reuse;
    4. release the writer fences.

    The stop handlers are installed before any startup work, so a stop signal that arrives
    during startup is honoured too, instead of killing the process without cleanup.

    Committed transactions keep their real result; an uncommitted transaction rolls back; a
    committed operation whose response never reached the caller is recovered by looking the
    operation up, never by replaying it.
    """
    assert_only_hosted_owners()
    settings = settings.validate()
    socket_path = Path(settings.socket_path)
    previous = _install_stop_handlers()
    stop_signal: int | None = None
    socket_handling = "NOT_REACHED"
    active: LifeSupplyService | None = None
    server: LifeSupplySocketServer | None = None
    drained = True
    try:
        socket_path.parent.mkdir(parents=True, exist_ok=True)
        socket_handling = prepare_socket_path(socket_path)
        active = service or LifeSupplyService(
            settings.data_root, operator_uids=settings.operator_uids,
            service_uids=settings.service_uids, read_only=settings.read_only,
            writer_instance=settings.writer_instance, switches=FeatureSwitches.from_env(),
            allowed_subjects=settings.allowed_subjects,
            max_payload_bytes=settings.max_payload_bytes,
            require_non_root_identity=settings.require_non_root_identity)
        server = LifeSupplySocketServer(str(socket_path), active,
                                        max_payload_bytes=settings.max_payload_bytes,
                                        socket_mode=settings.socket_mode)
        server.serve_forever(poll_interval=poll_interval)
    except ServiceStoppedBySignal as exc:
        stop_signal = exc.signum
    finally:
        _restore_stop_handlers(previous)
        if server is not None:
            server.server_close()        # stop accepting new connections
            drained = server.drain(drain_timeout)
            socket_path.unlink(missing_ok=True)   # only ever our own bound socket
        if active is not None:
            active.close()
        if stop_signal is not None:
            sys.stderr.write(
                "life-supply: graceful stop on signal %d "
                "(socket_handling=%s, drained=%s, inflight=%d)\n"
                % (stop_signal, socket_handling, drained,
                   server.inflight_requests if server is not None else 0))
            sys.stderr.flush()
    return 0
