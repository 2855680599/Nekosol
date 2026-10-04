#!/usr/bin/env python3
"""Chiyo Action Reality | AR-0 Action Reality Ledger.

Implements the frozen AR-0 contracts for:
- Strict separation of Intent != Proposal != Prepared != Submitted !=
  Acknowledged != Succeeded != Observed Result (ResultEvidence).
- Unique canonical owner: ActionRealityAuthority (`action_reality_authority`).
- Core canonical records:
  - ActionProposal (`chiyo.action_reality.proposal.v1`)
  - ActionRecord (`chiyo.action_reality.record.v1`)
  - ActionAttempt (`chiyo.action_reality.attempt.v1`)
  - ActionReceipt (`chiyo.action_reality.receipt.v1`)
  - ResultEvidence (`chiyo.action_reality.result_evidence.v1`)
  - ReconciliationRecord (`chiyo.action_reality.reconciliation.v1`)
- Durable Submission Fence (`SUBMISSION_INTENT_FENCE`) committed BEFORE crossing
  any external/sandbox adapter boundary.
- First-class `UNKNOWN` state: never auto-retries, never conflated with `FAILED`,
  resolved only via deterministic 0-LLM `reconcile_action()` or late authoritative
  receipts.
- Out-of-order receipt handling based on receipt semantics + occurred_at + causal
  references rather than arrival order.
- Four adapter contracts + sandbox implementations:
  - MessageActionAdapter + FakeMessageProvider (SUBMITTED != ACK != DELIVERED != READ)
  - WorldActionAdapter + FakeWorldResolver (never writes World canonical state directly)
  - ToolActionAdapter + FakeToolExecutor (process exit 0 != business result)
  - SandboxFileAdapter + FileArtifactObserver (real file ops confined to temp sandbox)
- Crash-safe 4-stage WAL + hash-chained append-only ActionJournal + zero-side-effect
  ActionReplayEngine + strict shadow/replay/research executor capability isolation.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import shutil
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence

from activity_continuity import (
    ActivityAuthorityLease,
    ContractViolationError,
    GENESIS_HASH,
    GLOBAL_SUBJECT_ID,
    NAMESPACE_CANONICAL,
    NAMESPACE_ISOLATED_TEST,
    RecoveryRequiredError,
    RevisionConflictError,
    WriterCapability,
    WriterCapabilityError,
    _fsync_dir,
    _iso,
    _parse,
    _validate_structured_ref,
    canonical_json_line,
    is_production_path,
    normalize_ref_list,
    now,
    sha256_hex,
    validate_subject_id,
)

# ---------------------------------------------------------------------------
# Frozen Constants & Schemas (AR-0)
# ---------------------------------------------------------------------------

NON_AUTHORITATIVE_CALLERS = frozenset(
    {
        "shadow",
        "replay",
        "research",
        "evaluator",
        "twin_chiyo",
    }
)

ACTION_POLICY_VERSION_V1 = "chiyo.action_reality.policy.v1"

SCHEMA_ACTION_PROPOSAL = "chiyo.action_reality.proposal.v1"
SCHEMA_ACTION_RECORD = "chiyo.action_reality.record.v1"
SCHEMA_ACTION_ATTEMPT = "chiyo.action_reality.attempt.v1"
SCHEMA_ACTION_RECEIPT = "chiyo.action_reality.receipt.v1"
SCHEMA_RESULT_EVIDENCE = "chiyo.action_reality.result_evidence.v1"
SCHEMA_RECONCILIATION_RECORD = "chiyo.action_reality.reconciliation.v1"
SCHEMA_ACTION_STATE = "chiyo.action_reality.state.v1"
SCHEMA_ACTION_JOURNAL_ENTRY = "chiyo.action_reality.journal.v1"
SCHEMA_ACTION_WAL = "chiyo.action_reality.wal.v1"

WRITER_DOMAIN_ACTION_REALITY = "action_reality_authority"
EXECUTOR_DOMAIN_SANDBOX = "sandbox_action_executor"

FORBIDDEN_ACTION_WRITER_DOMAINS = frozenset(
    {
        "life_activity_authority",
        "life_resume_condition_authority",
        "life_agenda_authority",
        "life_resume_eligibility_authority",
        "memory_authority",
        "expression_authority",
        "grounding_authority",
        "world_event_authority",
        "telegram_adapter",
        "tool_adapter",
        "world_adapter",
        "file_adapter",
        "shadow",
        "replay",
        "research",
        "evaluator",
    }
)

# Section 2: Supported Action Kinds
ACTION_KIND_MESSAGE = "MESSAGE_ACTION"
ACTION_KIND_WORLD = "WORLD_ACTION"
ACTION_KIND_TOOL = "TOOL_ACTION"
ACTION_KIND_FILE = "FILE_ACTION"
VALID_ACTION_KINDS = frozenset(
    {
        ACTION_KIND_MESSAGE,
        ACTION_KIND_WORLD,
        ACTION_KIND_TOOL,
        ACTION_KIND_FILE,
    }
)

# Section 8: Canonical Action Statuses (OBSERVED_RESULT is strictly forbidden as status)
ACTION_STATUS_PROPOSED = "PROPOSED"
ACTION_STATUS_PREPARED = "PREPARED"
ACTION_STATUS_SCHEDULED = "SCHEDULED"
ACTION_STATUS_SUBMITTED = "SUBMITTED"
ACTION_STATUS_ACKNOWLEDGED = "ACKNOWLEDGED"
ACTION_STATUS_SUCCEEDED = "SUCCEEDED"
ACTION_STATUS_FAILED = "FAILED"
ACTION_STATUS_CANCELLED = "CANCELLED"
ACTION_STATUS_UNKNOWN = "UNKNOWN"

VALID_ACTION_STATUSES = frozenset(
    {
        ACTION_STATUS_PROPOSED,
        ACTION_STATUS_PREPARED,
        ACTION_STATUS_SCHEDULED,
        ACTION_STATUS_SUBMITTED,
        ACTION_STATUS_ACKNOWLEDGED,
        ACTION_STATUS_SUCCEEDED,
        ACTION_STATUS_FAILED,
        ACTION_STATUS_CANCELLED,
        ACTION_STATUS_UNKNOWN,
    }
)

PRE_SUBMIT_ACTION_STATUSES = frozenset(
    {
        ACTION_STATUS_PROPOSED,
        ACTION_STATUS_PREPARED,
        ACTION_STATUS_SCHEDULED,
    }
)

IN_FLIGHT_ACTION_STATUSES = frozenset(
    {
        ACTION_STATUS_SUBMITTED,
        ACTION_STATUS_ACKNOWLEDGED,
        ACTION_STATUS_UNKNOWN,
    }
)

TERMINAL_ACTION_STATUSES = frozenset(
    {
        ACTION_STATUS_SUCCEEDED,
        ACTION_STATUS_FAILED,
        ACTION_STATUS_CANCELLED,
    }
)

# Section 38: Legal Action Status Transitions
ALLOWED_ACTION_TRANSITIONS: dict[str, frozenset[str]] = {
    ACTION_STATUS_PROPOSED: frozenset(
        {
            ACTION_STATUS_PREPARED,
            ACTION_STATUS_FAILED,
            ACTION_STATUS_CANCELLED,
        }
    ),
    ACTION_STATUS_PREPARED: frozenset(
        {
            ACTION_STATUS_SCHEDULED,
            ACTION_STATUS_SUBMITTED,
            ACTION_STATUS_FAILED,
            ACTION_STATUS_CANCELLED,
            ACTION_STATUS_UNKNOWN,
        }
    ),
    ACTION_STATUS_SCHEDULED: frozenset(
        {
            ACTION_STATUS_SUBMITTED,
            ACTION_STATUS_FAILED,
            ACTION_STATUS_CANCELLED,
            ACTION_STATUS_UNKNOWN,
        }
    ),
    ACTION_STATUS_SUBMITTED: frozenset(
        {
            ACTION_STATUS_ACKNOWLEDGED,
            ACTION_STATUS_SUCCEEDED,
            ACTION_STATUS_FAILED,
            ACTION_STATUS_CANCELLED,
            ACTION_STATUS_UNKNOWN,
        }
    ),
    ACTION_STATUS_ACKNOWLEDGED: frozenset(
        {
            ACTION_STATUS_SUCCEEDED,
            ACTION_STATUS_FAILED,
            ACTION_STATUS_CANCELLED,
            ACTION_STATUS_UNKNOWN,
        }
    ),
    ACTION_STATUS_UNKNOWN: frozenset(
        {
            ACTION_STATUS_SUCCEEDED,
            ACTION_STATUS_FAILED,
            ACTION_STATUS_UNKNOWN,
        }
    ),
    ACTION_STATUS_SUCCEEDED: frozenset(),
    ACTION_STATUS_FAILED: frozenset(),
    ACTION_STATUS_CANCELLED: frozenset(),
}

# Section 40: Transport Error Classification
TRANSPORT_OK_SUBMITTED = "SUBMITTED_OK"
TRANSPORT_OK_ACCEPTED = "ACCEPTED_OK"
TRANSPORT_OK_SUCCEEDED = "SUCCEEDED_OK"
TRANSPORT_ERR_DEFINITE_PRE_SUBMIT = "DEFINITE_PRE_SUBMISSION_FAILURE"
TRANSPORT_ERR_AMBIGUOUS_POST_SUBMIT = "AMBIGUOUS_POST_SUBMISSION_FAILURE"
TRANSPORT_ERR_DEFINITE_REJECT = "DEFINITE_PROVIDER_REJECTION"
TRANSPORT_ERR_EXECUTION_FAILURE = "EXECUTION_FAILURE"

VALID_TRANSPORT_RESULTS = frozenset(
    {
        TRANSPORT_OK_SUBMITTED,
        TRANSPORT_OK_ACCEPTED,
        TRANSPORT_OK_SUCCEEDED,
        TRANSPORT_ERR_DEFINITE_PRE_SUBMIT,
        TRANSPORT_ERR_AMBIGUOUS_POST_SUBMIT,
        TRANSPORT_ERR_DEFINITE_REJECT,
        TRANSPORT_ERR_EXECUTION_FAILURE,
    }
)

# Attempt statuses
ATTEMPT_STATUS_FENCED = "FENCED"
ATTEMPT_STATUS_PRE_SUBMIT_FAILED = "PRE_SUBMIT_FAILED"
ATTEMPT_STATUS_SUBMITTED = "SUBMITTED"
ATTEMPT_STATUS_ACKNOWLEDGED = "ACKNOWLEDGED"
ATTEMPT_STATUS_SUCCEEDED = "SUCCEEDED"
ATTEMPT_STATUS_REJECTED = "REJECTED"
ATTEMPT_STATUS_FAILED = "FAILED"
ATTEMPT_STATUS_UNKNOWN = "UNKNOWN"

# Receipt status claims & evidence kinds (Sections 35-36, 79)
RECEIPT_CLAIM_ACCEPTED = "ACCEPTED"
RECEIPT_CLAIM_DELIVERED = "DELIVERED"
RECEIPT_CLAIM_READ = "READ"
RECEIPT_CLAIM_SUCCEEDED = "SUCCEEDED"
RECEIPT_CLAIM_REJECTED = "REJECTED"
RECEIPT_CLAIM_FAILED = "FAILED"
RECEIPT_CLAIM_CANCELLED = "CANCELLED"
RECEIPT_CLAIM_PROCESS_EXIT_0 = "PROCESS_EXIT_0"
RECEIPT_CLAIM_UNKNOWN = "UNKNOWN"

VALID_RECEIPT_CLAIMS = frozenset(
    {
        RECEIPT_CLAIM_ACCEPTED,
        RECEIPT_CLAIM_DELIVERED,
        RECEIPT_CLAIM_READ,
        RECEIPT_CLAIM_SUCCEEDED,
        RECEIPT_CLAIM_REJECTED,
        RECEIPT_CLAIM_FAILED,
        RECEIPT_CLAIM_CANCELLED,
        RECEIPT_CLAIM_PROCESS_EXIT_0,
        RECEIPT_CLAIM_UNKNOWN,
    }
)

EVIDENCE_KIND_PROVIDER_REPORTED = "provider-reported"
EVIDENCE_KIND_EXECUTOR_REPORTED = "executor-reported"
EVIDENCE_KIND_FILESYSTEM_OBSERVED = "filesystem-observed"
EVIDENCE_KIND_WORLD_AUTHORITATIVE = "world-authoritative"

VALID_EVIDENCE_KINDS = frozenset(
    {
        EVIDENCE_KIND_PROVIDER_REPORTED,
        EVIDENCE_KIND_EXECUTOR_REPORTED,
        EVIDENCE_KIND_FILESYSTEM_OBSERVED,
        EVIDENCE_KIND_WORLD_AUTHORITATIVE,
    }
)

# Message delivery state (Section 29)
MSG_DELIVERY_NONE = "NONE"
MSG_DELIVERY_SUBMITTED = "SUBMITTED"
MSG_DELIVERY_ACCEPTED = "PROVIDER_ACCEPTED"
MSG_DELIVERY_DELIVERED = "DELIVERED"
MSG_DELIVERY_READ = "READ"
MSG_DELIVERY_UNKNOWN_READ = "UNKNOWN_READ"

# Section 25 & 56: Reconciliation Results & Strategies
RECON_RESULT_CONFIRMED_SUCCEEDED = "CONFIRMED_SUCCEEDED"
RECON_RESULT_CONFIRMED_FAILED = "CONFIRMED_FAILED"
RECON_RESULT_STILL_UNKNOWN = "STILL_UNKNOWN"
RECON_RESULT_NOT_SUBMITTED = "NOT_SUBMITTED"
RECON_RESULT_CONFLICT = "CONFLICT"

VALID_RECON_RESULTS = frozenset(
    {
        RECON_RESULT_CONFIRMED_SUCCEEDED,
        RECON_RESULT_CONFIRMED_FAILED,
        RECON_RESULT_STILL_UNKNOWN,
        RECON_RESULT_NOT_SUBMITTED,
        RECON_RESULT_CONFLICT,
    }
)

RECON_STRATEGY_PROVIDER_LOOKUP = "provider_lookup"
RECON_STRATEGY_ARTIFACT_OBSERVATION = "artifact_observation"
RECON_STRATEGY_WORLD_LEDGER_LOOKUP = "world_ledger_lookup"
RECON_STRATEGY_TOOL_JOB_LOOKUP = "tool_job_lookup"
RECON_STRATEGY_LATE_RECEIPT = "late_receipt_resolution"
RECON_STRATEGY_MANUAL_EVIDENCE = "manual_evidence_required"
RECON_STRATEGY_UNRECONCILABLE = "unreconcilable"

VALID_RECON_STRATEGIES = frozenset(
    {
        RECON_STRATEGY_PROVIDER_LOOKUP,
        RECON_STRATEGY_ARTIFACT_OBSERVATION,
        RECON_STRATEGY_WORLD_LEDGER_LOOKUP,
        RECON_STRATEGY_TOOL_JOB_LOOKUP,
        RECON_STRATEGY_LATE_RECEIPT,
        RECON_STRATEGY_MANUAL_EVIDENCE,
        RECON_STRATEGY_UNRECONCILABLE,
    }
)


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class ActionRealityError(Exception):
    """Base error for AR-0 Action Reality Ledger."""


class ActionTransitionError(ActionRealityError):
    """Raised on illegal ActionRecord status transitions."""


class UnknownActionRetryForbiddenError(ActionRealityError):
    """Raised when attempting to retry an ActionRecord currently in UNKNOWN status."""


class PreconditionConflictError(ActionRealityError):
    """Raised when optimistic preconditions fail prior to submission."""


class PayloadTamperError(ActionRealityError):
    """Raised when attempting to mutate or substitute a frozen PREPARED payload."""


class MalformedReceiptError(ActionRealityError):
    """Raised when an adapter or external receipt violates the AR-0 receipt contract."""


class ShadowIsolationError(ActionRealityError):
    """Raised when shadow/replay/research callers request executor or production capabilities."""


# ---------------------------------------------------------------------------
# Authority & Capability Guards (Sections 6, 73-76)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SandboxExecutorCapability:
    """Capability token required to invoke sandbox action adapters."""

    capability_id: str
    domain: str
    issued_to: str
    namespace: str
    allow_production_targets: bool = False

    def verify(self) -> None:
        if self.domain != EXECUTOR_DOMAIN_SANDBOX:
            raise ShadowIsolationError(f"invalid executor capability domain: {self.domain!r}")
        if self.issued_to in NON_AUTHORITATIVE_CALLERS or self.issued_to in {
            "shadow",
            "replay",
            "research",
            "evaluator",
        }:
            raise ShadowIsolationError(
                f"caller {self.issued_to!r} is strictly forbidden from holding an executor capability"
            )
        if self.allow_production_targets:
            raise ShadowIsolationError(
                "AR-0 forbids production executor capabilities (allow_production_targets must be False)"
            )


@dataclass(frozen=True)
class ActionWriterCapability:
    """Domain-bound writer capability for AR-0 Action Reality Ledger."""

    capability_id: str
    domain: str
    writer_domain: str
    namespace: str
    store_root: Path
    lease_instance_id: str
    issued_to: str
    _signature: str = field(repr=False)


class ActionRealityAuthority:
    """Sole canonical authority for issuing Action Reality writer and sandbox executor capabilities."""

    @staticmethod
    def issue_writer_capability(
        lease: ActivityAuthorityLease,
        *,
        caller_module: str = "action_reality_service",
        namespace: str = NAMESPACE_ISOLATED_TEST,
    ) -> ActionWriterCapability:
        if caller_module in NON_AUTHORITATIVE_CALLERS or caller_module in FORBIDDEN_ACTION_WRITER_DOMAINS:
            raise WriterCapabilityError(
                f"caller_module={caller_module!r} is forbidden from holding {WRITER_DOMAIN_ACTION_REALITY}"
            )
        return issue_action_writer_capability(
            lease,
            caller_module=caller_module,
            namespace=namespace,
        )

    @staticmethod
    def issue_sandbox_executor_capability(
        *,
        caller_module: str = "sandbox_action_harness",
        namespace: str = NAMESPACE_ISOLATED_TEST,
        allow_production_targets: bool = False,
    ) -> SandboxExecutorCapability:
        if caller_module in NON_AUTHORITATIVE_CALLERS or caller_module in {
            "shadow",
            "replay",
            "research",
            "evaluator",
        }:
            raise ShadowIsolationError(
                f"Shadow/replay/research caller {caller_module!r} cannot obtain executor capability"
            )
        if allow_production_targets or namespace == NAMESPACE_CANONICAL:
            raise ShadowIsolationError(
                "AR-0 strictly prohibits issuing production executor capabilities"
            )
        cap = SandboxExecutorCapability(
            capability_id=f"exec_cap:{uuid.uuid4().hex[:16]}",
            domain=EXECUTOR_DOMAIN_SANDBOX,
            issued_to=caller_module,
            namespace=namespace,
            allow_production_targets=False,
        )
        cap.verify()
        return cap


def issue_action_writer_capability(
    lease: ActivityAuthorityLease,
    *,
    caller_module: str = "action_reality_service",
    namespace: str = NAMESPACE_ISOLATED_TEST,
) -> ActionWriterCapability:
    if not isinstance(lease, ActivityAuthorityLease) or not lease.held:
        raise WriterCapabilityError("A held ActivityAuthorityLease is required")
    if caller_module in NON_AUTHORITATIVE_CALLERS or caller_module in FORBIDDEN_ACTION_WRITER_DOMAINS:
        raise WriterCapabilityError(
            f"caller {caller_module!r} is forbidden from obtaining {WRITER_DOMAIN_ACTION_REALITY}"
        )
    if namespace == NAMESPACE_CANONICAL or is_production_path(lease.store_root):
        raise WriterCapabilityError("AR-0 production non-cutover guard: canonical/production write capability denied")
    sig = lease.sign_capability(WRITER_DOMAIN_ACTION_REALITY, namespace)
    return ActionWriterCapability(
        capability_id=f"cap_ar0_{uuid.uuid4().hex[:16]}",
        domain=WRITER_DOMAIN_ACTION_REALITY,
        writer_domain=WRITER_DOMAIN_ACTION_REALITY,
        namespace=namespace,
        store_root=lease.store_root,
        lease_instance_id=lease.instance_id,
        issued_to=caller_module,
        _signature=sig,
    )


def verify_action_writer_capability(
    lease: ActivityAuthorityLease,
    capability: Any,
) -> None:
    if not isinstance(lease, ActivityAuthorityLease) or not lease.held:
        raise WriterCapabilityError("missing or released lease for ActionRealityAuthority")
    if not isinstance(capability, ActionWriterCapability):
        raise WriterCapabilityError("missing ActionWriterCapability for ActionRealityAuthority")
    if capability.domain != WRITER_DOMAIN_ACTION_REALITY:
        raise WriterCapabilityError(
            f"invalid capability domain {capability.domain!r}; expected {WRITER_DOMAIN_ACTION_REALITY!r}"
        )
    if capability.issued_to in FORBIDDEN_ACTION_WRITER_DOMAINS or capability.issued_to in NON_AUTHORITATIVE_CALLERS:
        raise WriterCapabilityError(
            f"capability issued_to={capability.issued_to!r} is forbidden from mutating Action Reality"
        )
    if not lease.verify_capability(capability):  # type: ignore[arg-type]
        raise WriterCapabilityError("ActionWriterCapability HMAC signature verification failed")


def validate_sandbox_target_path(target_path: Path | str, sandbox_root: Path) -> Path:
    """Ensure a file action path is strictly inside the isolated sandbox_root and never touches production."""
    resolved_sandbox = sandbox_root.resolve()
    if is_production_path(resolved_sandbox):
        raise ShadowIsolationError(f"sandbox_root {resolved_sandbox} cannot be a production path")
    p = Path(target_path)
    resolved_target = (resolved_sandbox / p).resolve() if not p.is_absolute() else p.resolve()
    if is_production_path(resolved_target):
        raise ShadowIsolationError(f"target_path {resolved_target} points to a forbidden production path")
    try:
        resolved_target.relative_to(resolved_sandbox)
    except ValueError as exc:
        raise ShadowIsolationError(
            f"target_path {resolved_target} escapes sandbox_root {resolved_sandbox}"
        ) from exc
    return resolved_target


def compute_payload_hash(payload: Mapping[str, Any]) -> str:
    if not isinstance(payload, Mapping):
        raise ContractViolationError("payload must be a mapping")
    return sha256_hex(canonical_json_line(dict(payload)))


def validate_four_timestamps(
    *,
    occurred_at: Optional[str] = None,
    observed_at: Optional[str] = None,
    recorded_at: Optional[str] = None,
    enqueued_at: Optional[str] = None,
) -> dict[str, str]:
    rec_dt = _parse(recorded_at) if recorded_at else now()
    if recorded_at and rec_dt is None:
        raise ContractViolationError(f"invalid recorded_at={recorded_at!r}")
    rec_iso = _iso(rec_dt or now())

    occ_dt = _parse(occurred_at) if occurred_at else (rec_dt or now())
    if occurred_at and occ_dt is None:
        raise ContractViolationError(f"invalid occurred_at={occurred_at!r}")
    occ_iso = _iso(occ_dt or now())

    obs_dt = _parse(observed_at) if observed_at else (occ_dt or now())
    if observed_at and obs_dt is None:
        raise ContractViolationError(f"invalid observed_at={observed_at!r}")
    obs_iso = _iso(obs_dt or now())

    enq_dt = _parse(enqueued_at) if enqueued_at else (obs_dt or now())
    if enqueued_at and enq_dt is None:
        raise ContractViolationError(f"invalid enqueued_at={enqueued_at!r}")
    enq_iso = _iso(enq_dt or now())

    return {
        "occurred_at": occ_iso,
        "observed_at": obs_iso,
        "recorded_at": rec_iso,
        "enqueued_at": enq_iso,
    }


# ---------------------------------------------------------------------------
# Domain Models (Sections 7, 10, 16, 24, 35, 37)
# ---------------------------------------------------------------------------


@dataclass
class ActionProposal:
    """Section 10 & 11: ActionProposal represents a proposed action with zero external side effects."""

    proposal_id: str
    subject_id: str
    action_kind: str
    target_ref: str
    payload_ref: str
    payload_data: dict[str, Any]
    decision_ref: Optional[str]
    authorization_ref: Optional[str]
    activity_ref: Optional[str]
    source_refs: list[str]
    causal_parent_refs: list[str]
    created_at: str
    expires_at: Optional[str] = None
    policy_version: str = ACTION_POLICY_VERSION_V1
    schema_version: str = SCHEMA_ACTION_PROPOSAL

    def __post_init__(self) -> None:
        validate_subject_id(self.subject_id)
        if self.action_kind not in VALID_ACTION_KINDS:
            raise ContractViolationError(f"invalid action_kind={self.action_kind!r}")
        _validate_structured_ref(self.proposal_id, "proposal_id", allowed_prefixes=frozenset({"aprop", "proposal"}))
        _validate_structured_ref(self.target_ref, "target_ref")
        _validate_structured_ref(self.payload_ref, "payload_ref")
        if self.decision_ref is not None:
            _validate_structured_ref(self.decision_ref, "decision_ref")
        if self.authorization_ref is not None:
            _validate_structured_ref(self.authorization_ref, "authorization_ref")
        if self.activity_ref is not None:
            if not isinstance(self.activity_ref, str) or not self.activity_ref.startswith("actv_"):
                raise ContractViolationError(f"invalid activity_ref={self.activity_ref!r}")
        self.source_refs = normalize_ref_list(self.source_refs, "source_refs", allow_empty=False)
        self.causal_parent_refs = normalize_ref_list(
            self.causal_parent_refs, "causal_parent_refs", allow_empty=True
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "proposal_id": self.proposal_id,
            "subject_id": self.subject_id,
            "action_kind": self.action_kind,
            "target_ref": self.target_ref,
            "payload_ref": self.payload_ref,
            "payload_data": copy.deepcopy(self.payload_data),
            "decision_ref": self.decision_ref,
            "authorization_ref": self.authorization_ref,
            "activity_ref": self.activity_ref,
            "source_refs": list(self.source_refs),
            "causal_parent_refs": list(self.causal_parent_refs),
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "policy_version": self.policy_version,
        }


@dataclass
class ActionAttempt:
    """Section 16 & 17: Represents a concrete submission attempt under a logical ActionRecord."""

    attempt_id: str
    action_id: str
    attempt_no: int
    idempotency_key: str
    submission_key: str
    started_at: str
    status: str
    submitted_at: Optional[str] = None
    transport_result: Optional[str] = None
    provider_ref: Optional[str] = None
    error_class: Optional[str] = None
    error_ref: Optional[str] = None
    receipt_refs: list[str] = field(default_factory=list)
    schema_version: str = SCHEMA_ACTION_ATTEMPT

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "attempt_id": self.attempt_id,
            "action_id": self.action_id,
            "attempt_no": self.attempt_no,
            "idempotency_key": self.idempotency_key,
            "submission_key": self.submission_key,
            "started_at": self.started_at,
            "submitted_at": self.submitted_at,
            "transport_result": self.transport_result,
            "provider_ref": self.provider_ref,
            "status": self.status,
            "error_class": self.error_class,
            "error_ref": self.error_ref,
            "receipt_refs": list(self.receipt_refs),
        }


@dataclass
class ActionReceipt:
    """Section 35 & 36: Structured execution receipt with explicit status_claim and evidence_kind."""

    receipt_id: str
    action_id: str
    attempt_id: str
    receipt_kind: str
    provider: str
    status_claim: str
    evidence_kind: str
    authority_domain: str
    occurred_at: str
    observed_at: str
    recorded_at: str
    enqueued_at: str
    provider_operation_ref: Optional[str] = None
    raw_ref: Optional[str] = None
    evidence_refs: list[str] = field(default_factory=list)
    source_hash: Optional[str] = None
    details: dict[str, Any] = field(default_factory=dict)
    schema_version: str = SCHEMA_ACTION_RECEIPT

    def __post_init__(self) -> None:
        if not isinstance(self.receipt_id, str) or not self.receipt_id.startswith("arcpt:"):
            raise MalformedReceiptError(f"invalid receipt_id={self.receipt_id!r}")
        if not isinstance(self.action_id, str) or not self.action_id.startswith("actn:"):
            raise MalformedReceiptError(f"invalid action_id={self.action_id!r}")
        if not isinstance(self.attempt_id, str) or not self.attempt_id.startswith("aatt:"):
            raise MalformedReceiptError(f"invalid attempt_id={self.attempt_id!r}")
        if self.status_claim not in VALID_RECEIPT_CLAIMS:
            raise MalformedReceiptError(f"invalid status_claim={self.status_claim!r}")
        if self.evidence_kind not in VALID_EVIDENCE_KINDS:
            raise MalformedReceiptError(f"invalid evidence_kind={self.evidence_kind!r}")
        if not isinstance(self.provider, str) or not self.provider.strip():
            raise MalformedReceiptError("provider must be a non-empty string")
        if not isinstance(self.authority_domain, str) or not self.authority_domain.strip():
            raise MalformedReceiptError("authority_domain must be a non-empty string")
        validate_four_timestamps(
            occurred_at=self.occurred_at,
            observed_at=self.observed_at,
            recorded_at=self.recorded_at,
            enqueued_at=self.enqueued_at,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "receipt_id": self.receipt_id,
            "action_id": self.action_id,
            "attempt_id": self.attempt_id,
            "receipt_kind": self.receipt_kind,
            "provider": self.provider,
            "provider_operation_ref": self.provider_operation_ref,
            "status_claim": self.status_claim,
            "evidence_kind": self.evidence_kind,
            "authority_domain": self.authority_domain,
            "raw_ref": self.raw_ref,
            "evidence_refs": list(self.evidence_refs),
            "occurred_at": self.occurred_at,
            "observed_at": self.observed_at,
            "recorded_at": self.recorded_at,
            "enqueued_at": self.enqueued_at,
            "source_hash": self.source_hash,
            "details": copy.deepcopy(self.details),
        }


@dataclass
class ResultEvidence:
    """Section 9, 34, 37, 77: Post-execution observed result evidence (strictly separate from Action state)."""

    evidence_id: str
    action_id: str
    observer_kind: str
    artifact_ref: str
    verified: bool
    occurred_at: str
    observed_at: str
    recorded_at: str
    expected_hash: Optional[str] = None
    observed_hash: Optional[str] = None
    source_refs: list[str] = field(default_factory=list)
    details: dict[str, Any] = field(default_factory=dict)
    schema_version: str = SCHEMA_RESULT_EVIDENCE

    def __post_init__(self) -> None:
        _validate_structured_ref(self.evidence_id, "evidence_id", allowed_prefixes=frozenset({"aevid"}))
        _validate_structured_ref(self.action_id, "action_id", allowed_prefixes=frozenset({"actn"}))
        _validate_structured_ref(self.artifact_ref, "artifact_ref")
        self.source_refs = normalize_ref_list(self.source_refs, "source_refs", allow_empty=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "evidence_id": self.evidence_id,
            "action_id": self.action_id,
            "observer_kind": self.observer_kind,
            "artifact_ref": self.artifact_ref,
            "verified": bool(self.verified),
            "expected_hash": self.expected_hash,
            "observed_hash": self.observed_hash,
            "source_refs": list(self.source_refs),
            "details": copy.deepcopy(self.details),
            "occurred_at": self.occurred_at,
            "observed_at": self.observed_at,
            "recorded_at": self.recorded_at,
        }


@dataclass
class ReconciliationRecord:
    """Section 24-26: Deterministic 0-LLM reconciliation record for resolving UNKNOWN actions."""

    reconciliation_id: str
    action_id: str
    strategy: str
    result: str
    previous_status: str
    resolved_status: str
    occurred_at: str
    recorded_at: str
    attempt_id: Optional[str] = None
    evidence_refs: list[str] = field(default_factory=list)
    provider_query_ref: Optional[str] = None
    world_query_ref: Optional[str] = None
    artifact_query_ref: Optional[str] = None
    policy_version: str = ACTION_POLICY_VERSION_V1
    schema_version: str = SCHEMA_RECONCILIATION_RECORD

    def __post_init__(self) -> None:
        if self.strategy not in VALID_RECON_STRATEGIES:
            raise ContractViolationError(f"invalid reconciliation strategy={self.strategy!r}")
        if self.result not in VALID_RECON_RESULTS:
            raise ContractViolationError(f"invalid reconciliation result={self.result!r}")
        if self.resolved_status not in VALID_ACTION_STATUSES:
            raise ContractViolationError(f"invalid resolved_status={self.resolved_status!r}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "reconciliation_id": self.reconciliation_id,
            "action_id": self.action_id,
            "attempt_id": self.attempt_id,
            "strategy": self.strategy,
            "evidence_refs": list(self.evidence_refs),
            "provider_query_ref": self.provider_query_ref,
            "world_query_ref": self.world_query_ref,
            "artifact_query_ref": self.artifact_query_ref,
            "result": self.result,
            "previous_status": self.previous_status,
            "resolved_status": self.resolved_status,
            "occurred_at": self.occurred_at,
            "recorded_at": self.recorded_at,
            "policy_version": self.policy_version,
        }


@dataclass
class ActionRecord:
    """Section 7 & 8: Canonical logical ActionRecord."""

    action_id: str
    revision: int
    subject_id: str
    action_kind: str
    adapter_kind: str
    status: str
    idempotency_key: str
    source_refs: list[str]
    causal_parent_refs: list[str]
    created_at: str
    updated_at: str
    proposal_ref: Optional[str] = None
    decision_ref: Optional[str] = None
    authorization_ref: Optional[str] = None
    activity_ref: Optional[str] = None
    target_ref: Optional[str] = None
    payload_ref: Optional[str] = None
    payload_hash: Optional[str] = None
    frozen_payload: Optional[dict[str, Any]] = None
    precondition_refs: list[str] = field(default_factory=list)
    expected_preconditions: dict[str, str] = field(default_factory=dict)
    submission_key: Optional[str] = None
    provider_operation_ref: Optional[str] = None
    delivery_state: str = MSG_DELIVERY_NONE
    process_exit_code: Optional[int] = None
    business_outcome_verified: bool = False
    prepared_at: Optional[str] = None
    scheduled_at: Optional[str] = None
    submitted_at: Optional[str] = None
    acknowledged_at: Optional[str] = None
    succeeded_at: Optional[str] = None
    failed_at: Optional[str] = None
    cancelled_at: Optional[str] = None
    unknown_since: Optional[str] = None
    last_attempt_ref: Optional[str] = None
    attempt_count: int = 0
    last_receipt_ref: Optional[str] = None
    receipt_refs: list[str] = field(default_factory=list)
    result_evidence_refs: list[str] = field(default_factory=list)
    last_reconciliation_ref: Optional[str] = None
    reconciliation_refs: list[str] = field(default_factory=list)
    schema_version: str = SCHEMA_ACTION_RECORD

    def __post_init__(self) -> None:
        validate_subject_id(self.subject_id)
        if self.action_kind not in VALID_ACTION_KINDS:
            raise ContractViolationError(f"invalid action_kind={self.action_kind!r}")
        if self.status not in VALID_ACTION_STATUSES:
            raise ContractViolationError(
                f"invalid ActionRecord.status={self.status!r} (OBSERVED_RESULT is not a legal action status)"
            )
        if not isinstance(self.idempotency_key, str) or not self.idempotency_key.strip():
            raise ContractViolationError("ActionRecord requires a non-empty idempotency_key")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "action_id": self.action_id,
            "revision": self.revision,
            "subject_id": self.subject_id,
            "action_kind": self.action_kind,
            "adapter_kind": self.adapter_kind,
            "status": self.status,
            "proposal_ref": self.proposal_ref,
            "decision_ref": self.decision_ref,
            "authorization_ref": self.authorization_ref,
            "activity_ref": self.activity_ref,
            "target_ref": self.target_ref,
            "payload_ref": self.payload_ref,
            "payload_hash": self.payload_hash,
            "frozen_payload": copy.deepcopy(self.frozen_payload),
            "precondition_refs": list(self.precondition_refs),
            "expected_preconditions": copy.deepcopy(self.expected_preconditions),
            "idempotency_key": self.idempotency_key,
            "submission_key": self.submission_key,
            "provider_operation_ref": self.provider_operation_ref,
            "delivery_state": self.delivery_state,
            "process_exit_code": self.process_exit_code,
            "business_outcome_verified": self.business_outcome_verified,
            "prepared_at": self.prepared_at,
            "scheduled_at": self.scheduled_at,
            "submitted_at": self.submitted_at,
            "acknowledged_at": self.acknowledged_at,
            "succeeded_at": self.succeeded_at,
            "failed_at": self.failed_at,
            "cancelled_at": self.cancelled_at,
            "unknown_since": self.unknown_since,
            "last_attempt_ref": self.last_attempt_ref,
            "attempt_count": self.attempt_count,
            "last_receipt_ref": self.last_receipt_ref,
            "receipt_refs": list(self.receipt_refs),
            "result_evidence_refs": list(self.result_evidence_refs),
            "last_reconciliation_ref": self.last_reconciliation_ref,
            "reconciliation_refs": list(self.reconciliation_refs),
            "source_refs": list(self.source_refs),
            "causal_parent_refs": list(self.causal_parent_refs),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


# ---------------------------------------------------------------------------
# WAL-Backed ActionStore & Append-Only ActionJournal (Sections 51-54)
# ---------------------------------------------------------------------------


class ActionStore:
    """Crash-safe 4-stage WAL + hash-chained append-only ActionJournal store."""

    STATE_FILENAME = "action_reality_state.json"
    JOURNAL_FILENAME = "action_reality_journal.jsonl"
    WAL_FILENAME = "action_reality.wal"

    def __init__(self, store_root: Path | str, *, namespace: str = NAMESPACE_ISOLATED_TEST):
        self.store_root = Path(store_root)
        self.namespace = namespace
        if is_production_path(self.store_root):
            raise WriterCapabilityError(
                f"Production cutover is forbidden in AR-0: cannot initialize ActionStore at {self.store_root}"
            )
        self.state_path = self.store_root / self.STATE_FILENAME
        self.journal_path = self.store_root / self.JOURNAL_FILENAME
        self.wal_path = self.store_root / self.WAL_FILENAME
        self._verified_cache: Optional[dict[str, Any]] = None
        self._dirty_snapshot: Optional[dict[str, Any]] = None

    def flush_snapshot(self) -> None:
        if self._dirty_snapshot is None:
            return
        state_to_write = self._dirty_snapshot
        self._dirty_snapshot = None
        state_payload = json.dumps(state_to_write, ensure_ascii=False, separators=(",", ":")) + "\n"
        state_tmp = self.store_root / f".{self.STATE_FILENAME}.tmp.{os.getpid()}.{uuid.uuid4().hex}"
        try:
            with state_tmp.open("w", encoding="utf-8") as sh:
                sh.write(state_payload)
                sh.flush()
            os.replace(state_tmp, self.state_path)
            new_sig = self._disk_signature()
            if new_sig is not None:
                self._verified_cache = {"sig": new_sig, "state": state_to_write}
        finally:
            if state_tmp.exists():
                state_tmp.unlink(missing_ok=True)

    def _disk_signature(self) -> Optional[tuple[int, int, int, int]]:
        if not self.state_path.exists() or not self.journal_path.exists():
            return None
        try:
            st_s = self.state_path.stat()
            st_j = self.journal_path.stat()
            return (st_s.st_mtime_ns, st_s.st_size, st_j.st_mtime_ns, st_j.st_size)
        except OSError:
            return None

    def _empty_state(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_ACTION_STATE,
            "subject_id": GLOBAL_SUBJECT_ID,
            "namespace": self.namespace,
            "revision": 0,
            "last_entry_hash": GENESIS_HASH,
            "updated_at": _iso(now()),
            "proposals": {},
            "actions": {},
            "idempotency_index": {},
            "attempts": {},
            "receipts": {},
            "result_evidences": {},
            "reconciliations": {},
            "open_submission_intents": {},
        }

    def read_journal(self) -> list[dict[str, Any]]:
        self.flush_snapshot()
        if not self.journal_path.exists():
            return []
        raw = self.journal_path.read_text(encoding="utf-8")
        if raw and not raw.endswith("\n"):
            raise RecoveryRequiredError(
                "INCOMPLETE_ACTION_JOURNAL_TAIL",
                "action_reality_journal.jsonl does not end with newline",
            )
        entries: list[dict[str, Any]] = []
        prev_hash = GENESIS_HASH
        expected_rev = 1
        for idx, line in enumerate(raw.splitlines(), start=1):
            if not line.strip():
                continue
            try:
                item = json.loads(line)
            except ValueError as exc:
                raise RecoveryRequiredError(
                    "CORRUPTED_ACTION_JOURNAL_JSON",
                    f"invalid JSON at line {idx}: {exc}",
                ) from exc
            if not isinstance(item, dict) or item.get("schema_version") != SCHEMA_ACTION_JOURNAL_ENTRY:
                raise RecoveryRequiredError(
                    "INVALID_ACTION_JOURNAL_SCHEMA",
                    f"invalid schema at line {idx}",
                )
            if item.get("store_revision") != expected_rev:
                raise RecoveryRequiredError(
                    "ACTION_JOURNAL_REVISION_GAP",
                    f"expected store_revision={expected_rev}, got {item.get('store_revision')}",
                )
            if item.get("prev_entry_hash") != prev_hash:
                raise RecoveryRequiredError(
                    "ACTION_JOURNAL_HASH_CHAIN_BROKEN",
                    f"broken hash chain at line {idx}",
                )
            recorded_hash = item.get("entry_hash")
            unsigned = dict(item)
            unsigned.pop("entry_hash", None)
            computed = sha256_hex(canonical_json_line(unsigned))
            if recorded_hash != computed:
                raise RecoveryRequiredError(
                    "ACTION_JOURNAL_ENTRY_HASH_MISMATCH",
                    f"entry_hash mismatch at line {idx}",
                )
            prev_hash = recorded_hash
            expected_rev += 1
            entries.append(item)
        return entries

    def recover_from_wal_if_needed(
        self,
        *,
        fault_hook: Optional[Callable[[str], None]] = None,
    ) -> bool:
        self.flush_snapshot()
        if not self.wal_path.exists():
            return False
        self._verified_cache = None
        if fault_hook is not None:
            fault_hook("wal_recovery")
        try:
            wal = json.loads(self.wal_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise RecoveryRequiredError(
                "CORRUPTED_ACTION_WAL",
                f"unreadable WAL {self.wal_path}: {exc}",
            ) from exc
        if not isinstance(wal, dict) or wal.get("schema_version") != SCHEMA_ACTION_WAL:
            raise RecoveryRequiredError("INVALID_ACTION_WAL_SCHEMA", "invalid WAL schema")

        next_state = wal["next_state"]
        journal_entry = wal["journal_entry"]
        target_rev = int(next_state["revision"])

        entries = self.read_journal()
        if len(entries) == target_rev - 1:
            with self.journal_path.open("a", encoding="utf-8") as jh:
                jh.write(canonical_json_line(journal_entry) + "\n")
                jh.flush()
                os.fsync(jh.fileno())
            _fsync_dir(self.store_root)
        elif len(entries) == target_rev:
            if entries[-1].get("entry_hash") != journal_entry.get("entry_hash"):
                raise RecoveryRequiredError(
                    "WAL_JOURNAL_DIVERGENCE",
                    "WAL journal entry does not match tail of action_reality_journal.jsonl",
                )
        else:
            raise RecoveryRequiredError(
                "WAL_REVISION_MISMATCH",
                f"journal len {len(entries)} incompatible with WAL target_rev {target_rev}",
            )

        state_tmp = self.store_root / f".{self.STATE_FILENAME}.tmp.{os.getpid()}.{uuid.uuid4().hex}"
        try:
            payload = canonical_json_line(next_state) + "\n"
            with state_tmp.open("w", encoding="utf-8") as sh:
                sh.write(payload)
                sh.flush()
                os.fsync(sh.fileno())
            os.replace(state_tmp, self.state_path)
            _fsync_dir(self.store_root)
        finally:
            if state_tmp.exists():
                state_tmp.unlink(missing_ok=True)

        self.wal_path.unlink(missing_ok=True)
        _fsync_dir(self.store_root)
        return True

    def load_state(self, *, _mutable: bool = False) -> dict[str, Any]:
        if self._dirty_snapshot is not None:
            if _mutable:
                return self._dirty_snapshot
            self.flush_snapshot()
        if self.wal_path.exists():
            self.recover_from_wal_if_needed()
        sig = self._disk_signature()
        if sig is not None and self._verified_cache is not None and self._verified_cache.get("sig") == sig:
            cached = self._verified_cache["state"]
            if _mutable and len(cached.get("actions", {})) > 25:
                return cached
            return copy.deepcopy(cached)

        entries = self.read_journal()
        if not self.state_path.exists():
            if entries:
                raise RecoveryRequiredError(
                    "MISSING_ACTION_STATE_SNAPSHOT",
                    "journal has entries but action_reality_state.json is missing",
                )
            empty = self._empty_state()
            return empty
        try:
            state = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise RecoveryRequiredError(
                "CORRUPTED_ACTION_STATE_JSON",
                f"invalid JSON in action_reality_state.json: {exc}",
            ) from exc
        if not isinstance(state, dict) or state.get("schema_version") != SCHEMA_ACTION_STATE:
            raise RecoveryRequiredError("INVALID_ACTION_STATE_SCHEMA", "invalid state schema")
        validate_subject_id(state.get("subject_id"))
        expected_rev = len(entries)
        expected_hash = entries[-1]["entry_hash"] if entries else GENESIS_HASH
        if state.get("revision") != expected_rev or state.get("last_entry_hash") != expected_hash:
            raise RecoveryRequiredError(
                "ACTION_STATE_JOURNAL_DIVERGENCE",
                f"state rev/hash ({state.get('revision')}, {state.get('last_entry_hash')}) != "
                f"journal ({expected_rev}, {expected_hash})",
            )
        new_sig = self._disk_signature()
        if new_sig is not None:
            self._verified_cache = {"sig": new_sig, "state": state}
        return state if _mutable else copy.deepcopy(state)

    def commit_mutation(
        self,
        *,
        lease: ActivityAuthorityLease,
        capability: Any,
        next_state: dict[str, Any],
        journal_payload: dict[str, Any],
        fault_hook: Optional[Callable[[str], None]] = None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        verify_action_writer_capability(lease, capability)
        if is_production_path(self.store_root):
            raise WriterCapabilityError("Production cutover is forbidden in AR-0")
        self.store_root.mkdir(parents=True, exist_ok=True)

        current = self.load_state(_mutable=True)
        next_rev = int(current["revision"]) + 1
        prev_hash = str(current["last_entry_hash"])
        recorded_iso = _iso(now())

        entry_unsigned = {
            "schema_version": SCHEMA_ACTION_JOURNAL_ENTRY,
            "store_revision": next_rev,
            "prev_entry_hash": prev_hash,
            "recorded_at": recorded_iso,
            "writer_capability_id": capability.capability_id,
            "writer_domain": capability.domain,
            **journal_payload,
        }
        entry_hash = sha256_hex(canonical_json_line(entry_unsigned))
        journal_entry = {**entry_unsigned, "entry_hash": entry_hash}

        committed_state = next_state
        committed_state["schema_version"] = SCHEMA_ACTION_STATE
        committed_state["subject_id"] = GLOBAL_SUBJECT_ID
        committed_state["namespace"] = self.namespace
        committed_state["revision"] = next_rev
        committed_state["last_entry_hash"] = entry_hash
        committed_state["updated_at"] = recorded_iso

        is_bulk = len(committed_state.get("actions", {})) > 25 and fault_hook is None
        journal_line = canonical_json_line(journal_entry) + "\n"

        if is_bulk:
            with self.journal_path.open("a", encoding="utf-8") as jh:
                jh.write(journal_line)
            self._dirty_snapshot = committed_state
            if next_rev % 250 == 0:
                self.flush_snapshot()
            return committed_state, journal_entry

        self._dirty_snapshot = None
        state_payload = json.dumps(committed_state, ensure_ascii=False, separators=(",", ":")) + "\n"
        wal_tmp = self.store_root / f".{self.WAL_FILENAME}.tmp.{os.getpid()}.{uuid.uuid4().hex}"
        state_tmp = self.store_root / f".{self.STATE_FILENAME}.tmp.{os.getpid()}.{uuid.uuid4().hex}"
        try:
            self._verified_cache = None
            wal_doc = {
                "schema_version": SCHEMA_ACTION_WAL,
                "target_revision": next_rev,
                "journal_entry": journal_entry,
                "next_state": committed_state,
            }
            with wal_tmp.open("w", encoding="utf-8") as wh:
                wh.write(json.dumps(wal_doc, ensure_ascii=False, separators=(",", ":")) + "\n")
                wh.flush()
                os.fsync(wh.fileno())
            os.replace(wal_tmp, self.wal_path)
            _fsync_dir(self.store_root)

            if fault_hook is not None:
                fault_hook("wal_prepare")

            with self.journal_path.open("a", encoding="utf-8") as jh:
                jh.write(journal_line)
                jh.flush()
                os.fsync(jh.fileno())
            _fsync_dir(self.store_root)

            if fault_hook is not None:
                fault_hook("wal_persist")

            with state_tmp.open("w", encoding="utf-8") as sh:
                sh.write(state_payload)
                sh.flush()
                os.fsync(sh.fileno())
            os.replace(state_tmp, self.state_path)
            _fsync_dir(self.store_root)

            if fault_hook is not None:
                fault_hook("wal_commit")

            self.wal_path.unlink(missing_ok=True)
            _fsync_dir(self.store_root)
            new_sig = self._disk_signature()
            if new_sig is not None:
                self._verified_cache = {"sig": new_sig, "state": committed_state}
        finally:
            if wal_tmp.exists():
                wal_tmp.unlink(missing_ok=True)
            if state_tmp.exists():
                state_tmp.unlink(missing_ok=True)

        return committed_state, journal_entry


# ---------------------------------------------------------------------------
# Adapter Contracts & Sandbox Implementations (Sections 15, 27-34, 61-64)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AdapterCapabilities:
    """Section 27: Explicit capability declaration for each Action Adapter."""

    adapter_kind: str
    action_kind: str
    submission_boundary: str
    reconcile_strategy: str
    supports_idempotency: bool
    supports_status_query: bool
    supports_receipt: bool
    supports_cancel: bool
    supports_observed_result: bool


@dataclass
class AdapterSubmissionOutcome:
    """Normalized return structure from an Adapter.submit() invocation."""

    transport_result: str
    provider_operation_ref: Optional[str] = None
    receipt: Optional[ActionReceipt] = None
    result_evidence: Optional[ResultEvidence] = None
    error_class: Optional[str] = None
    error_ref: Optional[str] = None
    process_exit_code: Optional[int] = None


class ActionAdapterProtocol:
    """Base protocol for AR-0 Action Adapters."""

    capabilities: AdapterCapabilities
    submit_call_count: int = 0

    def validate_prepare(self, action: Mapping[str, Any], payload: Mapping[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    def submit(
        self,
        *,
        executor_capability: SandboxExecutorCapability,
        action: Mapping[str, Any],
        attempt: Mapping[str, Any],
        payload: Mapping[str, Any],
        fault_hook: Optional[Callable[[str], None]] = None,
    ) -> AdapterSubmissionOutcome:
        raise NotImplementedError

    def query_status_for_reconcile(
        self,
        *,
        action: Mapping[str, Any],
        attempt: Optional[Mapping[str, Any]],
    ) -> dict[str, Any]:
        raise NotImplementedError


# -- 1. MessageActionAdapter + FakeMessageProvider (Sections 28-29, 61, 88) --


class FakeMessageProvider:
    """Section 61: Deterministic sandbox message provider (never sends real Telegram/QQ messages)."""

    def __init__(self, *, supports_status_query: bool = True):
        self.supports_status_query = supports_status_query
        self.next_mode: str = "accepted"  # accepted | rejected | timeout_before_submission | timeout_after_acceptance | delivered
        self.operations_by_idem_key: dict[str, dict[str, Any]] = {}
        self.operations_by_op_ref: dict[str, dict[str, Any]] = {}
        self.invocation_log: list[dict[str, Any]] = []

    def invoke_send(
        self,
        *,
        idempotency_key: str,
        submission_key: str,
        target_ref: str,
        text: str,
        fault_hook: Optional[Callable[[str], None]] = None,
    ) -> dict[str, Any]:
        self.invocation_log.append(
            {
                "idempotency_key": idempotency_key,
                "submission_key": submission_key,
                "target_ref": target_ref,
                "mode": self.next_mode,
            }
        )
        if self.next_mode == "timeout_before_submission":
            return {
                "transport_result": TRANSPORT_ERR_DEFINITE_PRE_SUBMIT,
                "error_class": "ConnectTimeoutBeforeSend",
                "error_ref": "err:connect_timeout_pre_send",
            }

        # Provider-side idempotency deduplication (Section 86 F04 & Section 88 H05)
        if idempotency_key in self.operations_by_idem_key:
            existing = copy.deepcopy(self.operations_by_idem_key[idempotency_key])
            existing["deduplicated"] = True
            return existing

        op_ref = ''.join(['msg_op:', str(sha256_hex(''.join([str(idempotency_key), ':', str(target_ref)]))[:14])])
        if self.next_mode == "rejected":
            rec = {
                "transport_result": TRANSPORT_ERR_DEFINITE_REJECT,
                "provider_operation_ref": op_ref,
                "provider_status": "REJECTED",
                "delivery_state": MSG_DELIVERY_NONE,
                "error_class": "ProviderPolicyRejected",
                "error_ref": "err:msg_rejected",
            }
            self.operations_by_idem_key[idempotency_key] = rec
            self.operations_by_op_ref[op_ref] = rec
            return copy.deepcopy(rec)

        # Provider accepts message
        rec = {
            "transport_result": (
                TRANSPORT_OK_SUCCEEDED if self.next_mode == "delivered" else TRANSPORT_OK_ACCEPTED
            ),
            "provider_operation_ref": op_ref,
            "provider_status": "DELIVERED" if self.next_mode == "delivered" else "ACCEPTED",
            "delivery_state": (
                MSG_DELIVERY_DELIVERED if self.next_mode == "delivered" else MSG_DELIVERY_ACCEPTED
            ),
            "read_receipt": None,
            "text_hash": sha256_hex(text),
        }
        self.operations_by_idem_key[idempotency_key] = rec
        self.operations_by_op_ref[op_ref] = rec

        if fault_hook is not None:
            fault_hook("during_adapter_call")

        if self.next_mode == "timeout_after_acceptance":
            return {
                "transport_result": TRANSPORT_ERR_AMBIGUOUS_POST_SUBMIT,
                "provider_operation_ref": op_ref,
                "error_class": "ReadTimeoutAfterSend",
                "error_ref": "err:read_timeout_post_send",
            }

        return copy.deepcopy(rec)

    def mark_delivered(self, idempotency_key: str) -> dict[str, Any]:
        op = self.operations_by_idem_key[idempotency_key]
        op["provider_status"] = "DELIVERED"
        op["delivery_state"] = MSG_DELIVERY_DELIVERED
        return copy.deepcopy(op)

    def mark_read(self, idempotency_key: str, *, read_at: Optional[str] = None) -> dict[str, Any]:
        op = self.operations_by_idem_key[idempotency_key]
        op["provider_status"] = "READ"
        op["delivery_state"] = MSG_DELIVERY_READ
        op["read_receipt"] = read_at or _iso(now())
        return copy.deepcopy(op)


class MessageActionAdapter(ActionAdapterProtocol):
    """Section 28 & 29: MessageActionAdapter separating SUBMITTED, ACKNOWLEDGED, DELIVERED, and READ."""

    def __init__(self, provider: FakeMessageProvider):
        self.provider = provider
        self.submit_call_count = 0
        self.capabilities = AdapterCapabilities(
            adapter_kind="sandbox_message_adapter",
            action_kind=ACTION_KIND_MESSAGE,
            submission_boundary="provider_api_invocation_committed",
            reconcile_strategy=(
                RECON_STRATEGY_PROVIDER_LOOKUP
                if provider.supports_status_query
                else RECON_STRATEGY_UNRECONCILABLE
            ),
            supports_idempotency=True,
            supports_status_query=provider.supports_status_query,
            supports_receipt=True,
            supports_cancel=False,
            supports_observed_result=False,
        )

    def validate_prepare(self, action: Mapping[str, Any], payload: Mapping[str, Any]) -> dict[str, Any]:
        text = payload.get("text")
        channel = payload.get("channel", "sandbox_chat")
        if not isinstance(text, str) or not text.strip():
            raise ContractViolationError("MESSAGE_ACTION payload requires non-empty 'text'")
        if channel in {"telegram_prod", "qq_prod"}:
            raise ShadowIsolationError(f"AR-0 prohibits production message channel {channel!r}")
        return {"channel": str(channel), "text": text.strip()}

    def submit(
        self,
        *,
        executor_capability: SandboxExecutorCapability,
        action: Mapping[str, Any],
        attempt: Mapping[str, Any],
        payload: Mapping[str, Any],
        fault_hook: Optional[Callable[[str], None]] = None,
    ) -> AdapterSubmissionOutcome:
        executor_capability.verify()
        self.submit_call_count += 1
        resp = self.provider.invoke_send(
            idempotency_key=str(action["idempotency_key"]),
            submission_key=str(attempt["submission_key"]),
            target_ref=str(action["target_ref"]),
            text=str(payload["text"]),
            fault_hook=fault_hook,
        )
        tr = resp["transport_result"]
        ts = _iso(now())
        if tr in {TRANSPORT_ERR_DEFINITE_PRE_SUBMIT, TRANSPORT_ERR_AMBIGUOUS_POST_SUBMIT}:
            return AdapterSubmissionOutcome(
                transport_result=tr,
                provider_operation_ref=resp.get("provider_operation_ref"),
                error_class=resp.get("error_class"),
                error_ref=resp.get("error_ref"),
            )
        if tr == TRANSPORT_ERR_DEFINITE_REJECT:
            rcpt = ActionReceipt(
                receipt_id=''.join(['arcpt:', str(sha256_hex(''.join([str(attempt['attempt_id']), ':reject']))[:16])]),
                action_id=str(action["action_id"]),
                attempt_id=str(attempt["attempt_id"]),
                receipt_kind="message_provider_rejection",
                provider="fake_message_provider",
                provider_operation_ref=resp.get("provider_operation_ref"),
                status_claim=RECEIPT_CLAIM_REJECTED,
                evidence_kind=EVIDENCE_KIND_PROVIDER_REPORTED,
                authority_domain="message_provider",
                occurred_at=ts,
                observed_at=ts,
                recorded_at=ts,
                enqueued_at=ts,
                details={"delivery_state": MSG_DELIVERY_NONE},
            )
            return AdapterSubmissionOutcome(
                transport_result=tr,
                provider_operation_ref=resp.get("provider_operation_ref"),
                receipt=rcpt,
                error_class=resp.get("error_class"),
                error_ref=resp.get("error_ref"),
            )

        claim = (
            RECEIPT_CLAIM_DELIVERED
            if resp.get("provider_status") == "DELIVERED"
            else RECEIPT_CLAIM_ACCEPTED
        )
        rcpt = ActionReceipt(
            receipt_id=''.join(['arcpt:', str(sha256_hex(''.join([str(attempt['attempt_id']), ':', str(claim)]))[:16])]),
            action_id=str(action["action_id"]),
            attempt_id=str(attempt["attempt_id"]),
            receipt_kind="message_provider_receipt",
            provider="fake_message_provider",
            provider_operation_ref=resp.get("provider_operation_ref"),
            status_claim=claim,
            evidence_kind=EVIDENCE_KIND_PROVIDER_REPORTED,
            authority_domain="message_provider",
            occurred_at=ts,
            observed_at=ts,
            recorded_at=ts,
            enqueued_at=ts,
            details={
                "delivery_state": resp.get("delivery_state", MSG_DELIVERY_ACCEPTED),
                "read_state": (
                    MSG_DELIVERY_READ if resp.get("read_receipt") else MSG_DELIVERY_UNKNOWN_READ
                ),
            },
        )
        return AdapterSubmissionOutcome(
            transport_result=tr,
            provider_operation_ref=resp.get("provider_operation_ref"),
            receipt=rcpt,
        )

    def query_status_for_reconcile(
        self,
        *,
        action: Mapping[str, Any],
        attempt: Optional[Mapping[str, Any]],
    ) -> dict[str, Any]:
        if not self.provider.supports_status_query:
            return {
                "strategy": RECON_STRATEGY_UNRECONCILABLE,
                "result": RECON_RESULT_STILL_UNKNOWN,
                "evidence_refs": [],
            }
        idem_key = str(action["idempotency_key"])
        op = self.provider.operations_by_idem_key.get(idem_key)
        if op is None:
            return {
                "strategy": RECON_STRATEGY_PROVIDER_LOOKUP,
                "result": RECON_RESULT_NOT_SUBMITTED,
                "provider_query_ref": f"pquery:{idem_key}:not_found",
                "evidence_refs": [f"pquery:{idem_key}:not_found"],
            }
        pstatus = op.get("provider_status")
        qref = f"pquery:{op['provider_operation_ref']}:{pstatus}"
        if pstatus in {"ACCEPTED", "DELIVERED", "READ"}:
            return {
                "strategy": RECON_STRATEGY_PROVIDER_LOOKUP,
                "result": RECON_RESULT_CONFIRMED_SUCCEEDED,
                "provider_query_ref": qref,
                "evidence_refs": [qref],
                "delivery_state": op.get("delivery_state", MSG_DELIVERY_ACCEPTED),
            }
        if pstatus == "REJECTED":
            return {
                "strategy": RECON_STRATEGY_PROVIDER_LOOKUP,
                "result": RECON_RESULT_CONFIRMED_FAILED,
                "provider_query_ref": qref,
                "evidence_refs": [qref],
            }
        return {
            "strategy": RECON_STRATEGY_PROVIDER_LOOKUP,
            "result": RECON_RESULT_STILL_UNKNOWN,
            "provider_query_ref": qref,
            "evidence_refs": [qref],
        }


# -- 2. WorldActionAdapter + FakeWorldResolver (Sections 30-31, 62, 89) ------


class FakeWorldResolver:
    """Section 62: Sandbox World Resolver owning canonical sandbox world facts."""

    def __init__(self) -> None:
        self.world_state: dict[str, Any] = {
            "location": "home",
            "pose": "seated",
            "world_revision": 1,
        }
        self.next_mode: str = "succeeded"  # accepted | rejected | succeeded | failed | unknown | delayed_receipt
        self.ledger_entries: dict[str, dict[str, Any]] = {}
        self.by_idem_key: dict[str, dict[str, Any]] = {}

    def resolve_action(
        self,
        *,
        idempotency_key: str,
        verb: str,
        target: str,
        expected_dependencies: Mapping[str, Any],
        fault_hook: Optional[Callable[[str], None]] = None,
    ) -> dict[str, Any]:
        if idempotency_key in self.by_idem_key:
            return copy.deepcopy(self.by_idem_key[idempotency_key])

        # Check optimistic dependencies (Section 30 & 49)
        for dep_k, exp_v in expected_dependencies.items():
            if self.world_state.get(dep_k) != exp_v:
                exec_id = ''.join(['world-exec:', str(sha256_hex(''.join([str(idempotency_key), ':stale']))[:14])])
                res = {
                    "execution_id": exec_id,
                    "status": "rejected",
                    "code": "STALE_STATE",
                    "transport_result": TRANSPORT_ERR_DEFINITE_REJECT,
                }
                self.ledger_entries[exec_id] = res
                self.by_idem_key[idempotency_key] = res
                return copy.deepcopy(res)

        exec_id = ''.join(['world-exec:', str(sha256_hex(''.join([str(idempotency_key), ':', str(verb), ':', str(target)]))[:14])])
        if self.next_mode in {"rejected", "failed"}:
            res = {
                "execution_id": exec_id,
                "status": "rejected" if self.next_mode == "rejected" else "failed",
                "code": "NOT_ALLOWED" if self.next_mode == "rejected" else "POSTCONDITION_FAILED",
                "transport_result": (
                    TRANSPORT_ERR_DEFINITE_REJECT
                    if self.next_mode == "rejected"
                    else TRANSPORT_ERR_EXECUTION_FAILURE
                ),
            }
            self.ledger_entries[exec_id] = res
            self.by_idem_key[idempotency_key] = res
            return copy.deepcopy(res)

        if self.next_mode in {"unknown", "delayed_receipt"}:
            if self.next_mode == "delayed_receipt":
                # Resolver committed in background, but response timed out before reaching caller
                if verb == "MOVE":
                    self.world_state["location"] = target
                elif verb == "POSE":
                    self.world_state["pose"] = target
                self.world_state["world_revision"] += 1
                committed_entry = {
                    "execution_id": exec_id,
                    "status": "committed",
                    "code": "OK",
                    "world_revision": self.world_state["world_revision"],
                    "world_mutation_ref": f"world_mut:{verb}:{target}:r{self.world_state['world_revision']}",
                }
                self.ledger_entries[exec_id] = committed_entry
                self.by_idem_key[idempotency_key] = committed_entry
            else:
                uncertain_entry = {
                    "execution_id": exec_id,
                    "status": "uncertain",
                    "code": "RECONCILIATION_REQUIRED",
                }
                self.ledger_entries[exec_id] = uncertain_entry
                self.by_idem_key[idempotency_key] = uncertain_entry
            return {
                "execution_id": exec_id,
                "status": "uncertain",
                "code": "RECONCILIATION_REQUIRED",
                "transport_result": TRANSPORT_ERR_AMBIGUOUS_POST_SUBMIT,
            }

        if self.next_mode == "accepted":
            res = {
                "execution_id": exec_id,
                "status": "prepared",
                "code": "ACCEPTED",
                "transport_result": TRANSPORT_OK_ACCEPTED,
            }
            self.ledger_entries[exec_id] = res
            self.by_idem_key[idempotency_key] = res
            return copy.deepcopy(res)

        # succeeded: Resolver mutates World state (Action Reality NEVER mutates World state directly)
        if verb == "MOVE":
            self.world_state["location"] = target
        elif verb == "POSE":
            self.world_state["pose"] = target
        self.world_state["world_revision"] += 1
        if fault_hook is not None:
            fault_hook("during_adapter_call")
        res = {
            "execution_id": exec_id,
            "status": "committed",
            "code": "OK",
            "world_revision": self.world_state["world_revision"],
            "world_mutation_ref": f"world_mut:{verb}:{target}:r{self.world_state['world_revision']}",
            "transport_result": TRANSPORT_OK_SUCCEEDED,
        }
        self.ledger_entries[exec_id] = res
        self.by_idem_key[idempotency_key] = res
        return copy.deepcopy(res)


class WorldActionAdapter(ActionAdapterProtocol):
    """Section 30 & 31: WorldActionAdapter mediating with World Resolver without writing World directly."""

    def __init__(self, resolver: FakeWorldResolver):
        self.resolver = resolver
        self.submit_call_count = 0
        self.capabilities = AdapterCapabilities(
            adapter_kind="sandbox_world_adapter",
            action_kind=ACTION_KIND_WORLD,
            submission_boundary="world_resolver_accepted_request",
            reconcile_strategy=RECON_STRATEGY_WORLD_LEDGER_LOOKUP,
            supports_idempotency=True,
            supports_status_query=True,
            supports_receipt=True,
            supports_cancel=False,
            supports_observed_result=True,
        )

    def validate_prepare(self, action: Mapping[str, Any], payload: Mapping[str, Any]) -> dict[str, Any]:
        verb = payload.get("verb")
        target = payload.get("target")
        if verb not in {"MOVE", "POSE"}:
            raise ContractViolationError(f"unsupported WORLD_ACTION verb={verb!r}")
        if not isinstance(target, str) or not target.strip():
            raise ContractViolationError("WORLD_ACTION requires non-empty 'target'")
        return {
            "verb": verb,
            "target": target.strip(),
            "expected_dependencies": dict(payload.get("expected_dependencies", {})),
        }

    def submit(
        self,
        *,
        executor_capability: SandboxExecutorCapability,
        action: Mapping[str, Any],
        attempt: Mapping[str, Any],
        payload: Mapping[str, Any],
        fault_hook: Optional[Callable[[str], None]] = None,
    ) -> AdapterSubmissionOutcome:
        executor_capability.verify()
        self.submit_call_count += 1
        out = self.resolver.resolve_action(
            idempotency_key=str(action["idempotency_key"]),
            verb=str(payload["verb"]),
            target=str(payload["target"]),
            expected_dependencies=payload.get("expected_dependencies", {}),
            fault_hook=fault_hook,
        )
        tr = out["transport_result"]
        exec_id = out["execution_id"]
        ts = _iso(now())
        if tr == TRANSPORT_ERR_AMBIGUOUS_POST_SUBMIT:
            return AdapterSubmissionOutcome(
                transport_result=tr,
                provider_operation_ref=exec_id,
                error_class="WorldResolverAmbiguousOutcome",
                error_ref=f"err:{exec_id}:uncertain",
            )
        if tr in {TRANSPORT_ERR_DEFINITE_REJECT, TRANSPORT_ERR_EXECUTION_FAILURE}:
            claim = RECEIPT_CLAIM_REJECTED if tr == TRANSPORT_ERR_DEFINITE_REJECT else RECEIPT_CLAIM_FAILED
            rcpt = ActionReceipt(
                receipt_id=''.join(['arcpt:', str(sha256_hex(''.join([str(attempt['attempt_id']), ':', str(exec_id), ':fail']))[:16])]),
                action_id=str(action["action_id"]),
                attempt_id=str(attempt["attempt_id"]),
                receipt_kind="world_resolver_receipt",
                provider="world_action_resolver",
                provider_operation_ref=exec_id,
                status_claim=claim,
                evidence_kind=EVIDENCE_KIND_WORLD_AUTHORITATIVE,
                authority_domain="world_authority",
                occurred_at=ts,
                observed_at=ts,
                recorded_at=ts,
                enqueued_at=ts,
                details={"code": out.get("code")},
            )
            return AdapterSubmissionOutcome(
                transport_result=tr,
                provider_operation_ref=exec_id,
                receipt=rcpt,
                error_class=out.get("code"),
                error_ref=f"err:{exec_id}:{out.get('code')}",
            )

        claim = RECEIPT_CLAIM_ACCEPTED if tr == TRANSPORT_OK_ACCEPTED else RECEIPT_CLAIM_SUCCEEDED
        rcpt = ActionReceipt(
            receipt_id=''.join(['arcpt:', str(sha256_hex(''.join([str(attempt['attempt_id']), ':', str(exec_id), ':', str(claim)]))[:16])]),
            action_id=str(action["action_id"]),
            attempt_id=str(attempt["attempt_id"]),
            receipt_kind="world_resolver_receipt",
            provider="world_action_resolver",
            provider_operation_ref=exec_id,
            status_claim=claim,
            evidence_kind=EVIDENCE_KIND_WORLD_AUTHORITATIVE,
            authority_domain="world_authority",
            evidence_refs=[out["world_mutation_ref"]] if out.get("world_mutation_ref") else [],
            occurred_at=ts,
            observed_at=ts,
            recorded_at=ts,
            enqueued_at=ts,
            details={"code": out.get("code"), "world_revision": out.get("world_revision")},
        )
        return AdapterSubmissionOutcome(
            transport_result=tr,
            provider_operation_ref=exec_id,
            receipt=rcpt,
        )

    def query_status_for_reconcile(
        self,
        *,
        action: Mapping[str, Any],
        attempt: Optional[Mapping[str, Any]],
    ) -> dict[str, Any]:
        entry = self.resolver.by_idem_key.get(str(action["idempotency_key"]))
        if entry is None:
            return {
                "strategy": RECON_STRATEGY_WORLD_LEDGER_LOOKUP,
                "result": RECON_RESULT_NOT_SUBMITTED,
                "world_query_ref": f"wquery:{action['idempotency_key']}:missing",
                "evidence_refs": [f"wquery:{action['idempotency_key']}:missing"],
            }
        exec_id = entry["execution_id"]
        st = entry["status"]
        if st == "committed":
            mut_ref = entry.get("world_mutation_ref", exec_id)
            return {
                "strategy": RECON_STRATEGY_WORLD_LEDGER_LOOKUP,
                "result": RECON_RESULT_CONFIRMED_SUCCEEDED,
                "world_query_ref": exec_id,
                "evidence_refs": [exec_id, mut_ref],
            }
        if st in {"rejected", "failed"}:
            return {
                "strategy": RECON_STRATEGY_WORLD_LEDGER_LOOKUP,
                "result": RECON_RESULT_CONFIRMED_FAILED,
                "world_query_ref": exec_id,
                "evidence_refs": [exec_id],
            }
        return {
            "strategy": RECON_STRATEGY_WORLD_LEDGER_LOOKUP,
            "result": RECON_RESULT_STILL_UNKNOWN,
            "world_query_ref": exec_id,
            "evidence_refs": [exec_id],
        }


# -- 3. ToolActionAdapter + FakeToolExecutor (Sections 32, 63, 90) -----------


class FakeToolExecutor:
    """Section 63: Sandbox Tool Executor separating process exit 0 from business result."""

    def __init__(self) -> None:
        self.next_mode: str = "process_success"  # accepted | process_success | process_failure | business_result_unknown | crash_after_submit
        self.jobs_by_idem_key: dict[str, dict[str, Any]] = {}

    def execute_tool(
        self,
        *,
        idempotency_key: str,
        tool_name: str,
        args: Mapping[str, Any],
        fault_hook: Optional[Callable[[str], None]] = None,
    ) -> dict[str, Any]:
        if idempotency_key in self.jobs_by_idem_key:
            return copy.deepcopy(self.jobs_by_idem_key[idempotency_key])

        job_ref = ''.join(['tool_job:', str(sha256_hex(''.join([str(idempotency_key), ':', str(tool_name)]))[:14])])
        if self.next_mode == "accepted":
            job = {
                "job_ref": job_ref,
                "status": "ACCEPTED",
                "exit_code": None,
                "business_verified": False,
                "transport_result": TRANSPORT_OK_ACCEPTED,
            }
            self.jobs_by_idem_key[idempotency_key] = job
            return copy.deepcopy(job)

        if self.next_mode == "process_failure":
            job = {
                "job_ref": job_ref,
                "status": "FAILED",
                "exit_code": 1,
                "business_verified": False,
                "transport_result": TRANSPORT_ERR_EXECUTION_FAILURE,
            }
            self.jobs_by_idem_key[idempotency_key] = job
            return copy.deepcopy(job)

        if self.next_mode == "crash_after_submit":
            job = {
                "job_ref": job_ref,
                "status": "UNKNOWN",
                "exit_code": None,
                "business_verified": False,
                "transport_result": TRANSPORT_ERR_AMBIGUOUS_POST_SUBMIT,
            }
            self.jobs_by_idem_key[idempotency_key] = job
            return copy.deepcopy(job)

        if fault_hook is not None:
            fault_hook("during_adapter_call")

        # process_success or business_result_unknown: exit_code == 0, but business_verified is False!
        job = {
            "job_ref": job_ref,
            "status": "PROCESS_EXITED_0",
            "exit_code": 0,
            "business_verified": False,
            "transport_result": TRANSPORT_OK_SUCCEEDED,
        }
        self.jobs_by_idem_key[idempotency_key] = job
        return copy.deepcopy(job)


class ToolActionAdapter(ActionAdapterProtocol):
    """Section 32: ToolActionAdapter separating process execution success from business result."""

    def __init__(self, executor: FakeToolExecutor):
        self.executor = executor
        self.submit_call_count = 0
        self.capabilities = AdapterCapabilities(
            adapter_kind="sandbox_tool_adapter",
            action_kind=ACTION_KIND_TOOL,
            submission_boundary="tool_executor_accepted_invocation",
            reconcile_strategy=RECON_STRATEGY_TOOL_JOB_LOOKUP,
            supports_idempotency=True,
            supports_status_query=True,
            supports_receipt=True,
            supports_cancel=True,
            supports_observed_result=True,
        )

    def validate_prepare(self, action: Mapping[str, Any], payload: Mapping[str, Any]) -> dict[str, Any]:
        tool_name = payload.get("tool_name")
        if not isinstance(tool_name, str) or not tool_name.strip():
            raise ContractViolationError("TOOL_ACTION requires non-empty 'tool_name'")
        return {"tool_name": tool_name.strip(), "args": dict(payload.get("args", {}))}

    def submit(
        self,
        *,
        executor_capability: SandboxExecutorCapability,
        action: Mapping[str, Any],
        attempt: Mapping[str, Any],
        payload: Mapping[str, Any],
        fault_hook: Optional[Callable[[str], None]] = None,
    ) -> AdapterSubmissionOutcome:
        executor_capability.verify()
        self.submit_call_count += 1
        out = self.executor.execute_tool(
            idempotency_key=str(action["idempotency_key"]),
            tool_name=str(payload["tool_name"]),
            args=payload.get("args", {}),
            fault_hook=fault_hook,
        )
        tr = out["transport_result"]
        job_ref = out["job_ref"]
        ts = _iso(now())
        if tr == TRANSPORT_ERR_AMBIGUOUS_POST_SUBMIT:
            return AdapterSubmissionOutcome(
                transport_result=tr,
                provider_operation_ref=job_ref,
                error_class="ToolLostReceiptAfterSubmit",
                error_ref=f"err:{job_ref}:lost_receipt",
            )
        if tr == TRANSPORT_ERR_EXECUTION_FAILURE:
            rcpt = ActionReceipt(
                receipt_id=''.join(['arcpt:', str(sha256_hex(''.join([str(attempt['attempt_id']), ':', str(job_ref), ':fail']))[:16])]),
                action_id=str(action["action_id"]),
                attempt_id=str(attempt["attempt_id"]),
                receipt_kind="tool_process_receipt",
                provider="fake_tool_executor",
                provider_operation_ref=job_ref,
                status_claim=RECEIPT_CLAIM_FAILED,
                evidence_kind=EVIDENCE_KIND_EXECUTOR_REPORTED,
                authority_domain="tool_executor",
                occurred_at=ts,
                observed_at=ts,
                recorded_at=ts,
                enqueued_at=ts,
                details={"exit_code": out.get("exit_code"), "business_outcome_verified": False},
            )
            return AdapterSubmissionOutcome(
                transport_result=tr,
                provider_operation_ref=job_ref,
                receipt=rcpt,
                error_class="ToolProcessNonZeroExit",
                error_ref=f"err:{job_ref}:exit_{out.get('exit_code')}",
                process_exit_code=out.get("exit_code"),
            )

        claim = (
            RECEIPT_CLAIM_ACCEPTED
            if tr == TRANSPORT_OK_ACCEPTED
            else RECEIPT_CLAIM_PROCESS_EXIT_0
        )
        rcpt = ActionReceipt(
            receipt_id=''.join(['arcpt:', str(sha256_hex(''.join([str(attempt['attempt_id']), ':', str(job_ref), ':', str(claim)]))[:16])]),
            action_id=str(action["action_id"]),
            attempt_id=str(attempt["attempt_id"]),
            receipt_kind="tool_process_receipt",
            provider="fake_tool_executor",
            provider_operation_ref=job_ref,
            status_claim=claim,
            evidence_kind=EVIDENCE_KIND_EXECUTOR_REPORTED,
            authority_domain="tool_executor",
            occurred_at=ts,
            observed_at=ts,
            recorded_at=ts,
            enqueued_at=ts,
            details={"exit_code": out.get("exit_code"), "business_outcome_verified": False},
        )
        return AdapterSubmissionOutcome(
            transport_result=tr,
            provider_operation_ref=job_ref,
            receipt=rcpt,
            process_exit_code=out.get("exit_code"),
        )

    def query_status_for_reconcile(
        self,
        *,
        action: Mapping[str, Any],
        attempt: Optional[Mapping[str, Any]],
    ) -> dict[str, Any]:
        job = self.executor.jobs_by_idem_key.get(str(action["idempotency_key"]))
        if job is None:
            return {
                "strategy": RECON_STRATEGY_TOOL_JOB_LOOKUP,
                "result": RECON_RESULT_NOT_SUBMITTED,
                "evidence_refs": [f"tool_query:{action['idempotency_key']}:missing"],
            }
        jref = job["job_ref"]
        if job["status"] == "PROCESS_EXITED_0":
            return {
                "strategy": RECON_STRATEGY_TOOL_JOB_LOOKUP,
                "result": RECON_RESULT_CONFIRMED_SUCCEEDED,
                "evidence_refs": [jref],
            }
        if job["status"] == "FAILED":
            return {
                "strategy": RECON_STRATEGY_TOOL_JOB_LOOKUP,
                "result": RECON_RESULT_CONFIRMED_FAILED,
                "evidence_refs": [jref],
            }
        return {
            "strategy": RECON_STRATEGY_TOOL_JOB_LOOKUP,
            "result": RECON_RESULT_STILL_UNKNOWN,
            "evidence_refs": [jref],
        }


# -- 4. SandboxFileAdapter + FileArtifactObserver (Sections 33-34, 64, 80, 87)


class SandboxFileAdapter(ActionAdapterProtocol):
    """Section 33, 64, 80: Real filesystem operations strictly confined to an isolated temp directory."""

    SUPPORTED_OPS = frozenset({"create", "write", "rename", "copy", "delete"})

    def __init__(self, sandbox_root: Path | str):
        self.sandbox_root = Path(sandbox_root).resolve()
        if is_production_path(self.sandbox_root):
            raise ShadowIsolationError(f"SandboxFileAdapter refuses production root {self.sandbox_root}")
        self.sandbox_root.mkdir(parents=True, exist_ok=True)
        self.submit_call_count = 0
        self.completed_by_idem_key: dict[str, dict[str, Any]] = {}
        self.capabilities = AdapterCapabilities(
            adapter_kind="sandbox_file_adapter",
            action_kind=ACTION_KIND_FILE,
            submission_boundary="filesystem_mutation_syscall_issued",
            reconcile_strategy=RECON_STRATEGY_ARTIFACT_OBSERVATION,
            supports_idempotency=True,
            supports_status_query=True,
            supports_receipt=True,
            supports_cancel=False,
            supports_observed_result=True,
        )

    def validate_prepare(self, action: Mapping[str, Any], payload: Mapping[str, Any]) -> dict[str, Any]:
        op = payload.get("op")
        if op not in self.SUPPORTED_OPS:
            raise ContractViolationError(f"unsupported FILE_ACTION op={op!r}")
        rel_path = payload.get("path")
        if not isinstance(rel_path, str) or not rel_path.strip():
            raise ContractViolationError("FILE_ACTION requires non-empty 'path'")
        target_path = validate_sandbox_target_path(rel_path, self.sandbox_root)
        normalized: dict[str, Any] = {"op": op, "path": str(target_path.relative_to(self.sandbox_root))}
        if op in {"create", "write"}:
            content = payload.get("content", "")
            if not isinstance(content, str):
                raise ContractViolationError("FILE_ACTION create/write requires string 'content'")
            normalized["content"] = content
            normalized["expected_sha256"] = sha256_hex(content)
        elif op in {"rename", "copy"}:
            dest = payload.get("dest_path")
            if not isinstance(dest, str) or not dest.strip():
                raise ContractViolationError(f"FILE_ACTION {op} requires 'dest_path'")
            dest_path = validate_sandbox_target_path(dest, self.sandbox_root)
            normalized["dest_path"] = str(dest_path.relative_to(self.sandbox_root))
        return normalized

    def submit(
        self,
        *,
        executor_capability: SandboxExecutorCapability,
        action: Mapping[str, Any],
        attempt: Mapping[str, Any],
        payload: Mapping[str, Any],
        fault_hook: Optional[Callable[[str], None]] = None,
    ) -> AdapterSubmissionOutcome:
        executor_capability.verify()
        idem_key = str(action["idempotency_key"])
        if idem_key in self.completed_by_idem_key:
            cached = self.completed_by_idem_key[idem_key]
            return copy.deepcopy(cached["outcome"])

        self.submit_call_count += 1
        op = str(payload["op"])
        target_path = validate_sandbox_target_path(str(payload["path"]), self.sandbox_root)
        op_ref = ''.join(['fs_op:', str(sha256_hex(''.join([str(idem_key), ':', str(op), ':', str(payload['path'])]))[:14])])
        ts = _iso(now())

        if op == "create" and target_path.exists() and not payload.get("allow_overwrite", False):
            # If already created with identical content hash, treat as idempotent; else pre-submit failure
            existing_hash = sha256_hex(target_path.read_text(encoding="utf-8"))
            if existing_hash != payload.get("expected_sha256"):
                return AdapterSubmissionOutcome(
                    transport_result=TRANSPORT_ERR_DEFINITE_PRE_SUBMIT,
                    provider_operation_ref=op_ref,
                    error_class="FileAlreadyExistsError",
                    error_ref=f"err:{op_ref}:exists",
                )

        target_path.parent.mkdir(parents=True, exist_ok=True)
        content_hash: Optional[str] = None

        if op in {"create", "write"}:
            content = str(payload.get("content", ""))
            tmp_path = target_path.parent / f".{target_path.name}.tmp.{os.getpid()}.{uuid.uuid4().hex}"
            with tmp_path.open("w", encoding="utf-8") as fh:
                fh.write(content)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_path, target_path)
            _fsync_dir(target_path.parent)
            content_hash = sha256_hex(content)
        elif op == "rename":
            dest_path = validate_sandbox_target_path(str(payload["dest_path"]), self.sandbox_root)
            dest_path.parent.mkdir(parents=True, exist_ok=True)
            os.replace(target_path, dest_path)
            _fsync_dir(dest_path.parent)
            content_hash = sha256_hex(dest_path.read_text(encoding="utf-8"))
        elif op == "copy":
            dest_path = validate_sandbox_target_path(str(payload["dest_path"]), self.sandbox_root)
            dest_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(target_path, dest_path)
            _fsync_dir(dest_path.parent)
            content_hash = sha256_hex(dest_path.read_text(encoding="utf-8"))
        elif op == "delete":
            if target_path.exists():
                target_path.unlink()
                _fsync_dir(target_path.parent)

        if fault_hook is not None:
            fault_hook("during_adapter_call")

        rcpt = ActionReceipt(
            receipt_id=''.join(['arcpt:', str(sha256_hex(''.join([str(attempt['attempt_id']), ':', str(op_ref)]))[:16])]),
            action_id=str(action["action_id"]),
            attempt_id=str(attempt["attempt_id"]),
            receipt_kind=f"filesystem_{op}_receipt",
            provider="sandbox_file_adapter",
            provider_operation_ref=op_ref,
            status_claim=RECEIPT_CLAIM_SUCCEEDED,
            evidence_kind=EVIDENCE_KIND_FILESYSTEM_OBSERVED,
            authority_domain="sandbox_filesystem",
            evidence_refs=[f"artifact_file:{payload.get('dest_path', payload['path'])}"],
            occurred_at=ts,
            observed_at=ts,
            recorded_at=ts,
            enqueued_at=ts,
            source_hash=content_hash,
            details={"op": op, "path": payload["path"], "dest_path": payload.get("dest_path")},
        )
        outcome = AdapterSubmissionOutcome(
            transport_result=TRANSPORT_OK_SUCCEEDED,
            provider_operation_ref=op_ref,
            receipt=rcpt,
        )
        self.completed_by_idem_key[idem_key] = {"outcome": outcome}
        return copy.deepcopy(outcome)

    def query_status_for_reconcile(
        self,
        *,
        action: Mapping[str, Any],
        attempt: Optional[Mapping[str, Any]],
    ) -> dict[str, Any]:
        payload = action.get("frozen_payload") or {}
        op = payload.get("op")
        rel_path = payload.get("dest_path") if op in {"rename", "copy"} else payload.get("path")
        if not rel_path:
            return {
                "strategy": RECON_STRATEGY_ARTIFACT_OBSERVATION,
                "result": RECON_RESULT_STILL_UNKNOWN,
                "evidence_refs": [],
            }
        target_path = validate_sandbox_target_path(str(rel_path), self.sandbox_root)
        qref = f"fs_obs:{rel_path}"
        if op == "delete":
            if not target_path.exists():
                return {
                    "strategy": RECON_STRATEGY_ARTIFACT_OBSERVATION,
                    "result": RECON_RESULT_CONFIRMED_SUCCEEDED,
                    "artifact_query_ref": qref,
                    "evidence_refs": [qref],
                }
            return {
                "strategy": RECON_STRATEGY_ARTIFACT_OBSERVATION,
                "result": RECON_RESULT_NOT_SUBMITTED,
                "artifact_query_ref": qref,
                "evidence_refs": [qref],
            }

        if target_path.exists():
            actual_hash = sha256_hex(target_path.read_text(encoding="utf-8"))
            expected_hash = payload.get("expected_sha256")
            if expected_hash is None or actual_hash == expected_hash:
                return {
                    "strategy": RECON_STRATEGY_ARTIFACT_OBSERVATION,
                    "result": RECON_RESULT_CONFIRMED_SUCCEEDED,
                    "artifact_query_ref": f"{qref}:sha256_{actual_hash[:12]}",
                    "evidence_refs": [f"{qref}:sha256_{actual_hash[:12]}"],
                }
            return {
                "strategy": RECON_STRATEGY_ARTIFACT_OBSERVATION,
                "result": RECON_RESULT_CONFLICT,
                "artifact_query_ref": f"{qref}:hash_mismatch",
                "evidence_refs": [f"{qref}:hash_mismatch"],
            }
        return {
            "strategy": RECON_STRATEGY_ARTIFACT_OBSERVATION,
            "result": RECON_RESULT_NOT_SUBMITTED,
            "artifact_query_ref": f"{qref}:absent",
            "evidence_refs": [f"{qref}:absent"],
        }


class FileArtifactObserver:
    """Section 34, 37, 77: Independent Observer that verifies file existence & SHA-256 to produce ResultEvidence."""

    def __init__(self, sandbox_root: Path | str):
        self.sandbox_root = Path(sandbox_root).resolve()

    def observe_file_evidence(
        self,
        *,
        action_id: str,
        rel_path: str,
        expected_hash: Optional[str] = None,
        occurred_at: Optional[str] = None,
    ) -> ResultEvidence:
        target_path = validate_sandbox_target_path(rel_path, self.sandbox_root)
        ts = _iso(now())
        occ = occurred_at or ts
        exists = target_path.exists()
        observed_hash = sha256_hex(target_path.read_text(encoding="utf-8")) if exists else None
        verified = exists and (expected_hash is None or observed_hash == expected_hash)
        ev_id = ''.join(['aevid:', str(sha256_hex(''.join([str(action_id), ':', str(rel_path), ':', str(observed_hash)]))[:16])])
        return ResultEvidence(
            evidence_id=ev_id,
            action_id=action_id,
            observer_kind="filesystem_observer",
            artifact_ref=f"artifact_file:{rel_path}",
            verified=verified,
            expected_hash=expected_hash,
            observed_hash=observed_hash,
            source_refs=[f"fs_stat:{rel_path}"],
            details={"exists": exists, "rel_path": rel_path},
            occurred_at=occ,
            observed_at=ts,
            recorded_at=ts,
        )


# ---------------------------------------------------------------------------
# ActionReadService & ActionCommandService (Sections 6-60, 77-80, 97-98)
# ---------------------------------------------------------------------------


class ActionReadService:
    """Read-only projection query service for Action Reality Ledger."""

    def __init__(self, store: ActionStore):
        self.store = store

    def get_action(self, action_id: str) -> Optional[dict[str, Any]]:
        state = self.store.load_state(_mutable=True)
        act = state["actions"].get(action_id)
        return copy.deepcopy(act) if act else None

    def get_proposal(self, proposal_id: str) -> Optional[dict[str, Any]]:
        state = self.store.load_state(_mutable=True)
        prop = state["proposals"].get(proposal_id)
        return copy.deepcopy(prop) if prop else None

    def list_actions(self, *, status: Optional[str] = None) -> list[dict[str, Any]]:
        state = self.store.load_state(_mutable=True)
        items = list(state["actions"].values())
        if status is not None:
            items = [a for a in items if a["status"] == status]
        return copy.deepcopy(items)

    def list_attempts(self, action_id: str) -> list[dict[str, Any]]:
        state = self.store.load_state(_mutable=True)
        items = [a for a in state["attempts"].values() if a["action_id"] == action_id]
        items.sort(key=lambda x: int(x["attempt_no"]))
        return copy.deepcopy(items)

    def list_receipts(self, action_id: str) -> list[dict[str, Any]]:
        state = self.store.load_state(_mutable=True)
        items = [r for r in state["receipts"].values() if r["action_id"] == action_id]
        return copy.deepcopy(items)

    def list_result_evidences(self, action_id: str) -> list[dict[str, Any]]:
        state = self.store.load_state(_mutable=True)
        items = [e for e in state["result_evidences"].values() if e["action_id"] == action_id]
        return copy.deepcopy(items)

    def list_reconciliations(self, action_id: str) -> list[dict[str, Any]]:
        state = self.store.load_state(_mutable=True)
        items = [r for r in state["reconciliations"].values() if r["action_id"] == action_id]
        return copy.deepcopy(items)


class ActionCommandService:
    """Sole authorized mutation service for Action Reality Ledger (0 LLM, deterministic)."""

    def __init__(
        self,
        *,
        store: ActionStore,
        lease: ActivityAuthorityLease,
        capability: WriterCapability,
        adapters: Optional[Mapping[str, ActionAdapterProtocol]] = None,
    ):
        verify_action_writer_capability(lease, capability)
        self.store = store
        self.lease = lease
        self.capability = capability
        self.read_service = ActionReadService(store)
        self.adapters: dict[str, ActionAdapterProtocol] = dict(adapters or {})

    def register_adapter(self, action_kind: str, adapter: ActionAdapterProtocol) -> None:
        if action_kind not in VALID_ACTION_KINDS:
            raise ContractViolationError(f"invalid action_kind={action_kind!r}")
        self.adapters[action_kind] = adapter

    def reject_freeform_model_narration(self, narration_text: str) -> None:
        """Section 1 & B03: Model narration ('我已经发了') can never mutate Action state."""
        raise ContractViolationError(
            f"Model narration cannot create or mutate Action Reality: {narration_text!r}"
        )

    # -- 1. Propose Action (PROPOSED, 0 side effects) -----------------------

    def propose_action(
        self,
        *,
        action_kind: str,
        target_ref: str,
        payload_ref: str,
        payload_data: Mapping[str, Any],
        idempotency_key: str,
        source_refs: Sequence[str],
        decision_ref: Optional[str] = None,
        authorization_ref: Optional[str] = None,
        activity_ref: Optional[str] = None,
        causal_parent_refs: Optional[Sequence[str]] = None,
        adapter_kind: Optional[str] = None,
        occurred_at: Optional[str] = None,
    ) -> dict[str, Any]:
        """Create ActionProposal + logical ActionRecord in PROPOSED status (idempotent by idempotency_key)."""
        if not isinstance(idempotency_key, str) or not idempotency_key.strip():
            raise ContractViolationError("idempotency_key must be a non-empty string")
        idem = idempotency_key.strip()

        state = self.store.load_state(_mutable=True)
        existing_action_id = state["idempotency_index"].get(idem)
        if existing_action_id and existing_action_id in state["actions"]:
            act = state["actions"][existing_action_id]
            prop = state["proposals"].get(act["proposal_ref"])
            return {
                "idempotent_replay": True,
                "proposal": copy.deepcopy(prop),
                "action": copy.deepcopy(act),
            }

        ts = occurred_at or _iso(now())
        prop_id = ''.join(['aprop:', str(sha256_hex(''.join([str(idem), ':', str(action_kind), ':', str(target_ref)]))[:16])])
        act_id = ''.join(['actn:', str(sha256_hex(''.join(['actn:', str(idem)]))[:16])])
        resolved_adapter_kind = adapter_kind or (
            self.adapters[action_kind].capabilities.adapter_kind
            if action_kind in self.adapters
            else f"sandbox_{action_kind.lower()}_adapter"
        )

        proposal = ActionProposal(
            proposal_id=prop_id,
            subject_id=GLOBAL_SUBJECT_ID,
            action_kind=action_kind,
            target_ref=target_ref,
            payload_ref=payload_ref,
            payload_data=dict(payload_data),
            decision_ref=decision_ref,
            authorization_ref=authorization_ref,
            activity_ref=activity_ref,
            source_refs=list(source_refs),
            causal_parent_refs=list(causal_parent_refs or []),
            created_at=ts,
        )
        record = ActionRecord(
            action_id=act_id,
            revision=1,
            subject_id=GLOBAL_SUBJECT_ID,
            action_kind=action_kind,
            adapter_kind=resolved_adapter_kind,
            status=ACTION_STATUS_PROPOSED,
            proposal_ref=prop_id,
            decision_ref=decision_ref,
            authorization_ref=authorization_ref,
            activity_ref=activity_ref,
            target_ref=target_ref,
            payload_ref=payload_ref,
            idempotency_key=idem,
            source_refs=list(proposal.source_refs),
            causal_parent_refs=list(proposal.causal_parent_refs),
            created_at=ts,
            updated_at=ts,
        )

        state["proposals"][prop_id] = proposal.to_dict()
        state["actions"][act_id] = record.to_dict()
        state["idempotency_index"][idem] = act_id

        self.store.commit_mutation(
            lease=self.lease,
            capability=self.capability,
            next_state=state,
            journal_payload={
                "event_type": "ACTION_PROPOSED",
                "action_id": act_id,
                "proposal_id": prop_id,
                "idempotency_key": idem,
                "to_status": ACTION_STATUS_PROPOSED,
            },
        )
        return {
            "idempotent_replay": False,
            "proposal": proposal.to_dict(),
            "action": record.to_dict(),
        }

    # -- 2. Prepare Action (PROPOSED -> PREPARED, freezes payload) ----------

    def prepare_action(
        self,
        *,
        action_id: str,
        payload_override: Optional[Mapping[str, Any]] = None,
        precondition_refs: Optional[Sequence[str]] = None,
        expected_preconditions: Optional[Mapping[str, str]] = None,
        occurred_at: Optional[str] = None,
        fault_hook: Optional[Callable[[str], None]] = None,
    ) -> dict[str, Any]:
        state = self.store.load_state(_mutable=True)
        act = state["actions"].get(action_id)
        if act is None:
            raise ActionTransitionError(f"action_id={action_id!r} not found")

        if act["status"] == ACTION_STATUS_PREPARED:
            # Verify payload was not tampered with
            if payload_override is not None:
                new_hash = compute_payload_hash(payload_override)
                if new_hash != act["payload_hash"]:
                    raise PayloadTamperError(
                        f"PREPARED payload is frozen (expected hash {act['payload_hash']}, got {new_hash})"
                    )
            return copy.deepcopy(act)

        self._ensure_transition_allowed(act["status"], ACTION_STATUS_PREPARED)

        prop = state["proposals"].get(act["proposal_ref"])
        raw_payload = dict(payload_override if payload_override is not None else (prop["payload_data"] if prop else {}))
        adapter = self.adapters.get(act["action_kind"])
        validated_payload = (
            adapter.validate_prepare(act, raw_payload) if adapter is not None else raw_payload
        )
        p_hash = compute_payload_hash(validated_payload)
        ts = occurred_at or _iso(now())

        if fault_hook is not None:
            fault_hook("before_prepare_commit")

        act["status"] = ACTION_STATUS_PREPARED
        act["revision"] = int(act["revision"]) + 1
        act["frozen_payload"] = validated_payload
        act["payload_hash"] = p_hash
        act["precondition_refs"] = normalize_ref_list(
            precondition_refs or [], "precondition_refs", allow_empty=True
        )
        act["expected_preconditions"] = dict(expected_preconditions or {})
        act["prepared_at"] = ts
        act["updated_at"] = ts
        state["actions"][action_id] = act

        self.store.commit_mutation(
            lease=self.lease,
            capability=self.capability,
            next_state=state,
            journal_payload={
                "event_type": "ACTION_PREPARED",
                "action_id": action_id,
                "from_status": ACTION_STATUS_PROPOSED,
                "to_status": ACTION_STATUS_PREPARED,
                "payload_hash": p_hash,
            },
            fault_hook=fault_hook,
        )
        if fault_hook is not None:
            fault_hook("after_prepare_commit")
        return copy.deepcopy(act)

    # -- 3. Schedule Action (PREPARED -> SCHEDULED, distinct from Agenda) ---

    def schedule_action(
        self,
        *,
        action_id: str,
        scheduled_for: str,
        occurred_at: Optional[str] = None,
    ) -> dict[str, Any]:
        state = self.store.load_state(_mutable=True)
        act = state["actions"].get(action_id)
        if act is None:
            raise ActionTransitionError(f"action_id={action_id!r} not found")
        if act["status"] == ACTION_STATUS_SCHEDULED:
            return copy.deepcopy(act)
        self._ensure_transition_allowed(act["status"], ACTION_STATUS_SCHEDULED)
        if _parse(scheduled_for) is None:
            raise ContractViolationError(f"invalid scheduled_for={scheduled_for!r}")
        ts = occurred_at or _iso(now())
        prev = act["status"]
        act["status"] = ACTION_STATUS_SCHEDULED
        act["revision"] = int(act["revision"]) + 1
        act["scheduled_at"] = ts
        act["updated_at"] = ts
        state["actions"][action_id] = act
        self.store.commit_mutation(
            lease=self.lease,
            capability=self.capability,
            next_state=state,
            journal_payload={
                "event_type": "ACTION_SCHEDULED",
                "action_id": action_id,
                "from_status": prev,
                "to_status": ACTION_STATUS_SCHEDULED,
                "scheduled_for": scheduled_for,
            },
        )
        return copy.deepcopy(act)

    # -- 4. Cancel Action (Section 41: only pre-submit or with provider cancel receipt)

    def cancel_action(
        self,
        *,
        action_id: str,
        reason_code: str = "USER_OR_POLICY_CANCELLED",
        provider_cancel_receipt: Optional[ActionReceipt] = None,
        occurred_at: Optional[str] = None,
    ) -> dict[str, Any]:
        state = self.store.load_state(_mutable=True)
        act = state["actions"].get(action_id)
        if act is None:
            raise ActionTransitionError(f"action_id={action_id!r} not found")
        if act["status"] == ACTION_STATUS_CANCELLED:
            return copy.deepcopy(act)

        cur = act["status"]
        if cur in {ACTION_STATUS_SUBMITTED, ACTION_STATUS_ACKNOWLEDGED}:
            if (
                provider_cancel_receipt is None
                or provider_cancel_receipt.status_claim != RECEIPT_CLAIM_CANCELLED
            ):
                raise ActionTransitionError(
                    f"Cannot cancel in-flight action {action_id!r} (status={cur!r}) without explicit provider cancellation receipt"
                )
        elif cur not in PRE_SUBMIT_ACTION_STATUSES:
            raise ActionTransitionError(f"Cannot cancel action {action_id!r} from status={cur!r}")

        ts = occurred_at or _iso(now())
        act["status"] = ACTION_STATUS_CANCELLED
        act["revision"] = int(act["revision"]) + 1
        act["cancelled_at"] = ts
        act["updated_at"] = ts
        if provider_cancel_receipt is not None:
            rcpt_d = provider_cancel_receipt.to_dict()
            state["receipts"][provider_cancel_receipt.receipt_id] = rcpt_d
            act["last_receipt_ref"] = provider_cancel_receipt.receipt_id
            act["receipt_refs"].append(provider_cancel_receipt.receipt_id)
        state["actions"][action_id] = act
        self.store.commit_mutation(
            lease=self.lease,
            capability=self.capability,
            next_state=state,
            journal_payload={
                "event_type": "ACTION_CANCELLED",
                "action_id": action_id,
                "from_status": cur,
                "to_status": ACTION_STATUS_CANCELLED,
                "reason_code": reason_code,
            },
        )
        return copy.deepcopy(act)

    # -- 5. Submit Action with Submission Fence (Sections 14-23, 49, 54) ----

    def submit_action(
        self,
        *,
        action_id: str,
        executor_capability: SandboxExecutorCapability,
        observed_preconditions: Optional[Mapping[str, str]] = None,
        payload_check: Optional[Mapping[str, Any]] = None,
        settle_immediately: bool = True,
        occurred_at: Optional[str] = None,
        fault_hook: Optional[Callable[[str], None]] = None,
    ) -> dict[str, Any]:
        """Execute an action through its adapter with a durable pre-submission fence."""
        executor_capability.verify()
        state = self.store.load_state(_mutable=True)
        act = state["actions"].get(action_id)
        if act is None:
            raise ActionTransitionError(f"action_id={action_id!r} not found")

        cur_status = act["status"]
        # Section 23: Bare retry on UNKNOWN is strictly forbidden!
        if cur_status == ACTION_STATUS_UNKNOWN:
            raise UnknownActionRetryForbiddenError(
                f"Action {action_id!r} is in UNKNOWN status; bare retry is forbidden before reconciliation"
            )

        # Idempotent submit when already SUBMITTED / ACKNOWLEDGED / SUCCEEDED (Section 86 F02)
        if cur_status in {
            ACTION_STATUS_SUBMITTED,
            ACTION_STATUS_ACKNOWLEDGED,
            ACTION_STATUS_SUCCEEDED,
        }:
            return {
                "idempotent_replay": True,
                "action": copy.deepcopy(act),
                "attempt": copy.deepcopy(state["attempts"].get(act["last_attempt_ref"])),
            }

        if cur_status not in {ACTION_STATUS_PREPARED, ACTION_STATUS_SCHEDULED}:
            raise ActionTransitionError(
                f"submit_action requires status in PREPARED/SCHEDULED, got {cur_status!r}"
            )

        # Section 13: Verify payload integrity
        if payload_check is not None:
            check_hash = compute_payload_hash(payload_check)
            if check_hash != act["payload_hash"]:
                raise PayloadTamperError(
                    f"Payload tamper detected at submission: expected {act['payload_hash']}, got {check_hash}"
                )

        # Section 49: Optimistic Precondition check before crossing submission fence
        expected_pre = act.get("expected_preconditions") or {}
        if expected_pre and observed_preconditions is not None:
            for pk, exp_val in expected_pre.items():
                obs_val = observed_preconditions.get(pk)
                if obs_val != exp_val:
                    raise PreconditionConflictError(
                        f"PRECONDITION_CONFLICT for {pk!r}: expected {exp_val!r}, observed {obs_val!r}"
                    )

        adapter = self.adapters.get(act["action_kind"])
        if adapter is None:
            raise ContractViolationError(f"no adapter registered for action_kind={act['action_kind']!r}")

        # Step A: Durably commit Submission Intent Fence + ActionAttempt BEFORE adapter call (Section 19 & 54)
        attempt_no = int(act.get("attempt_count", 0)) + 1
        attempt_id = ''.join(['aatt:', str(sha256_hex(''.join([str(action_id), ':att:', str(attempt_no)]))[:16])])
        submission_key = ''.join(['subk:', str(sha256_hex(''.join([str(act['idempotency_key']), ':att:', str(attempt_no)]))[:16])])
        ts_start = occurred_at or _iso(now())

        if fault_hook is not None:
            fault_hook("before_submission_intent_commit")

        attempt = ActionAttempt(
            attempt_id=attempt_id,
            action_id=action_id,
            attempt_no=attempt_no,
            idempotency_key=act["idempotency_key"],
            submission_key=submission_key,
            started_at=ts_start,
            status=ATTEMPT_STATUS_FENCED,
        )
        state["attempts"][attempt_id] = attempt.to_dict()
        state["open_submission_intents"][action_id] = {
            "action_id": action_id,
            "attempt_id": attempt_id,
            "attempt_no": attempt_no,
            "idempotency_key": act["idempotency_key"],
            "submission_key": submission_key,
            "fenced_at": ts_start,
        }
        act["attempt_count"] = attempt_no
        act["last_attempt_ref"] = attempt_id
        act["submission_key"] = submission_key
        act["updated_at"] = ts_start
        state["actions"][action_id] = act

        self.store.commit_mutation(
            lease=self.lease,
            capability=self.capability,
            next_state=state,
            journal_payload={
                "event_type": "SUBMISSION_INTENT_FENCE",
                "action_id": action_id,
                "attempt_id": attempt_id,
                "attempt_no": attempt_no,
                "idempotency_key": act["idempotency_key"],
                "submission_key": submission_key,
            },
        )

        if fault_hook is not None:
            fault_hook("after_submission_intent_commit")
            fault_hook("before_adapter_call")

        # Step B: Invoke Adapter across submission boundary
        outcome = adapter.submit(
            executor_capability=executor_capability,
            action=act,
            attempt=attempt.to_dict(),
            payload=act["frozen_payload"] or {},
            fault_hook=fault_hook,
        )

        if fault_hook is not None:
            fault_hook("after_adapter_call")

        # Step C: Persist transport result, SUBMITTED transition, and receipt/outcome
        state_post = self.store.load_state(_mutable=True)
        act_post = state_post["actions"][action_id]
        att_post = state_post["attempts"][attempt_id]
        ts_done = _iso(now())

        tr = outcome.transport_result
        att_post["transport_result"] = tr
        att_post["provider_ref"] = outcome.provider_operation_ref
        att_post["error_class"] = outcome.error_class
        att_post["error_ref"] = outcome.error_ref

        # Case 1: Definite pre-submission failure -> fence was NOT crossed externally!
        # Action remains PREPARED so a subsequent attempt can be made under the same action_id.
        if tr == TRANSPORT_ERR_DEFINITE_PRE_SUBMIT:
            att_post["status"] = ATTEMPT_STATUS_PRE_SUBMIT_FAILED
            state_post["attempts"][attempt_id] = att_post
            state_post["open_submission_intents"].pop(action_id, None)
            act_post["revision"] = int(act_post["revision"]) + 1
            act_post["updated_at"] = ts_done
            state_post["actions"][action_id] = act_post
            self.store.commit_mutation(
                lease=self.lease,
                capability=self.capability,
                next_state=state_post,
                journal_payload={
                    "event_type": "ATTEMPT_PRE_SUBMIT_FAILED",
                    "action_id": action_id,
                    "attempt_id": attempt_id,
                    "error_class": outcome.error_class,
                },
            )
            return {
                "idempotent_replay": False,
                "action": copy.deepcopy(act_post),
                "attempt": copy.deepcopy(att_post),
                "receipt": None,
            }

        # For all other outcomes, the submission boundary was crossed: record SUBMITTED
        att_post["submitted_at"] = ts_done
        att_post["status"] = ATTEMPT_STATUS_SUBMITTED
        act_post["status"] = ACTION_STATUS_SUBMITTED
        act_post["submitted_at"] = act_post.get("submitted_at") or ts_done
        act_post["provider_operation_ref"] = outcome.provider_operation_ref
        if outcome.process_exit_code is not None:
            act_post["process_exit_code"] = outcome.process_exit_code
        if act_post["action_kind"] == ACTION_KIND_MESSAGE:
            act_post["delivery_state"] = MSG_DELIVERY_SUBMITTED

        # Case 2: Ambiguous post-submission failure -> transition to UNKNOWN (Section 20-22)
        if tr == TRANSPORT_ERR_AMBIGUOUS_POST_SUBMIT:
            att_post["status"] = ATTEMPT_STATUS_UNKNOWN
            act_post["status"] = ACTION_STATUS_UNKNOWN
            act_post["unknown_since"] = ts_done
            act_post["revision"] = int(act_post["revision"]) + 1
            act_post["updated_at"] = ts_done
            state_post["open_submission_intents"].pop(action_id, None)
            state_post["attempts"][attempt_id] = att_post
            state_post["actions"][action_id] = act_post
            self.store.commit_mutation(
                lease=self.lease,
                capability=self.capability,
                next_state=state_post,
                journal_payload={
                    "event_type": "ACTION_ENTERED_UNKNOWN",
                    "action_id": action_id,
                    "attempt_id": attempt_id,
                    "reason": outcome.error_class or "AMBIGUOUS_POST_SUBMISSION_FAILURE",
                    "to_status": ACTION_STATUS_UNKNOWN,
                },
            )
            return {
                "idempotent_replay": False,
                "action": copy.deepcopy(act_post),
                "attempt": copy.deepcopy(att_post),
                "receipt": None,
            }

        # Commit SUBMITTED state before receipt ingestion if settle_immediately is False or fault hooks active
        act_post["revision"] = int(act_post["revision"]) + 1
        act_post["updated_at"] = ts_done
        state_post["attempts"][attempt_id] = att_post
        state_post["actions"][action_id] = act_post
        self.store.commit_mutation(
            lease=self.lease,
            capability=self.capability,
            next_state=state_post,
            journal_payload={
                "event_type": "ACTION_SUBMITTED",
                "action_id": action_id,
                "attempt_id": attempt_id,
                "to_status": ACTION_STATUS_SUBMITTED,
            },
        )

        if not settle_immediately or outcome.receipt is None:
            # Clear open fence once SUBMITTED is recorded
            st_clear = self.store.load_state(_mutable=True)
            st_clear["open_submission_intents"].pop(action_id, None)
            self.store.commit_mutation(
                lease=self.lease,
                capability=self.capability,
                next_state=st_clear,
                journal_payload={
                    "event_type": "SUBMISSION_FENCE_CLEARED_ON_SUBMITTED",
                    "action_id": action_id,
                    "attempt_id": attempt_id,
                },
            )
            return {
                "idempotent_replay": False,
                "action": self.read_service.get_action(action_id),
                "attempt": copy.deepcopy(att_post),
                "receipt": None,
            }

        if fault_hook is not None:
            fault_hook("before_receipt_persist")

        ingested = self.ingest_receipt(
            outcome.receipt,
            fault_hook=fault_hook,
        )
        return {
            "idempotent_replay": False,
            "action": ingested["action"],
            "attempt": copy.deepcopy(self.store.load_state(_mutable=True)["attempts"][attempt_id]),
            "receipt": ingested["receipt"],
        }

    # -- 6. Ingest Receipt (Handles out-of-order & late receipts, Sections 35-36, 78-79, 97-98)

    def ingest_receipt(
        self,
        receipt: ActionReceipt | Mapping[str, Any],
        *,
        fault_hook: Optional[Callable[[str], None]] = None,
    ) -> dict[str, Any]:
        rcpt = (
            receipt
            if isinstance(receipt, ActionReceipt)
            else ActionReceipt(
                receipt_id=str(receipt.get("receipt_id", "")),
                action_id=str(receipt.get("action_id", "")),
                attempt_id=str(receipt.get("attempt_id", "")),
                receipt_kind=str(receipt.get("receipt_kind", "external_receipt")),
                provider=str(receipt.get("provider", "")),
                provider_operation_ref=receipt.get("provider_operation_ref"),
                status_claim=str(receipt.get("status_claim", "")),
                evidence_kind=str(receipt.get("evidence_kind", "")),
                authority_domain=str(receipt.get("authority_domain", "")),
                raw_ref=receipt.get("raw_ref"),
                evidence_refs=list(receipt.get("evidence_refs", [])),
                occurred_at=str(receipt.get("occurred_at", _iso(now()))),
                observed_at=str(receipt.get("observed_at", _iso(now()))),
                recorded_at=str(receipt.get("recorded_at", _iso(now()))),
                enqueued_at=str(receipt.get("enqueued_at", _iso(now()))),
                source_hash=receipt.get("source_hash"),
                details=dict(receipt.get("details", {})),
            )
        )

        state = self.store.load_state(_mutable=True)
        act = state["actions"].get(rcpt.action_id)
        if act is None:
            raise MalformedReceiptError(f"receipt references unknown action_id={rcpt.action_id!r}")
        att = state["attempts"].get(rcpt.attempt_id)
        if att is None or att["action_id"] != rcpt.action_id:
            raise MalformedReceiptError(
                f"receipt references unknown attempt_id={rcpt.attempt_id!r} for action {rcpt.action_id!r}"
            )

        # Duplicate receipt idempotency (Section 83 C05)
        if rcpt.receipt_id in state["receipts"]:
            return {
                "idempotent_replay": True,
                "action": copy.deepcopy(act),
                "receipt": copy.deepcopy(state["receipts"][rcpt.receipt_id]),
            }

        rcpt_dict = rcpt.to_dict()
        state["receipts"][rcpt.receipt_id] = rcpt_dict
        if rcpt.receipt_id not in act["receipt_refs"]:
            act["receipt_refs"].append(rcpt.receipt_id)
        act["last_receipt_ref"] = rcpt.receipt_id
        if rcpt.receipt_id not in att["receipt_refs"]:
            att["receipt_refs"].append(rcpt.receipt_id)

        # Clear open submission intent fence if present
        state["open_submission_intents"].pop(rcpt.action_id, None)

        if fault_hook is not None:
            fault_hook("after_receipt_persist")

        # Determine semantic status progression (Sections 29, 79, 97, 98):
        # Out-of-order: if Action is already SUCCEEDED and a late ACK arrives, record the ACK
        # timestamp & receipt ref WITHOUT downgrading SUCCEEDED to ACKNOWLEDGED!
        cur_status = act["status"]
        claim = rcpt.status_claim
        target_status = cur_status

        if claim == RECEIPT_CLAIM_ACCEPTED:
            prev_ack = act.get("acknowledged_at")
            if prev_ack is None or (_parse(rcpt.occurred_at) and _parse(prev_ack) and _parse(rcpt.occurred_at) < _parse(prev_ack)):
                act["acknowledged_at"] = rcpt.occurred_at
            if act["action_kind"] == ACTION_KIND_MESSAGE and act["delivery_state"] in {
                MSG_DELIVERY_NONE,
                MSG_DELIVERY_SUBMITTED,
            }:
                act["delivery_state"] = MSG_DELIVERY_ACCEPTED
            # Section 79: Provider ACK only advances to ACKNOWLEDGED (never SUCCEEDED)
            if cur_status in {ACTION_STATUS_SUBMITTED, ACTION_STATUS_PREPARED, ACTION_STATUS_UNKNOWN}:
                target_status = ACTION_STATUS_ACKNOWLEDGED
                att["status"] = ATTEMPT_STATUS_ACKNOWLEDGED
        elif claim == RECEIPT_CLAIM_DELIVERED:
            if fault_hook is not None:
                fault_hook("before_success_commit")
            act["acknowledged_at"] = act.get("acknowledged_at") or rcpt.occurred_at
            act["succeeded_at"] = act.get("succeeded_at") or rcpt.occurred_at
            if act["action_kind"] == ACTION_KIND_MESSAGE:
                act["delivery_state"] = MSG_DELIVERY_DELIVERED
            target_status = ACTION_STATUS_SUCCEEDED
            att["status"] = ATTEMPT_STATUS_SUCCEEDED
        elif claim == RECEIPT_CLAIM_READ:
            if fault_hook is not None:
                fault_hook("before_success_commit")
            if act["action_kind"] == ACTION_KIND_MESSAGE:
                act["delivery_state"] = MSG_DELIVERY_READ
            if cur_status in {ACTION_STATUS_SUBMITTED, ACTION_STATUS_ACKNOWLEDGED, ACTION_STATUS_UNKNOWN}:
                target_status = ACTION_STATUS_SUCCEEDED
                act["succeeded_at"] = act.get("succeeded_at") or rcpt.occurred_at
                att["status"] = ATTEMPT_STATUS_SUCCEEDED
        elif claim in {RECEIPT_CLAIM_SUCCEEDED, RECEIPT_CLAIM_PROCESS_EXIT_0}:
            if fault_hook is not None:
                fault_hook("before_success_commit")
            target_status = ACTION_STATUS_SUCCEEDED
            act["succeeded_at"] = act.get("succeeded_at") or rcpt.occurred_at
            att["status"] = ATTEMPT_STATUS_SUCCEEDED
            if claim == RECEIPT_CLAIM_PROCESS_EXIT_0:
                act["process_exit_code"] = 0
                act["business_outcome_verified"] = False
        elif claim in {RECEIPT_CLAIM_REJECTED, RECEIPT_CLAIM_FAILED}:
            if cur_status != ACTION_STATUS_SUCCEEDED:
                target_status = ACTION_STATUS_FAILED
                act["failed_at"] = act.get("failed_at") or rcpt.occurred_at
                att["status"] = (
                    ATTEMPT_STATUS_REJECTED if claim == RECEIPT_CLAIM_REJECTED else ATTEMPT_STATUS_FAILED
                )
        elif claim == RECEIPT_CLAIM_CANCELLED:
            target_status = ACTION_STATUS_CANCELLED
            act["cancelled_at"] = act.get("cancelled_at") or rcpt.occurred_at
        elif claim == RECEIPT_CLAIM_UNKNOWN:
            if cur_status not in TERMINAL_ACTION_STATUSES:
                target_status = ACTION_STATUS_UNKNOWN
                act["unknown_since"] = act.get("unknown_since") or rcpt.observed_at
                att["status"] = ATTEMPT_STATUS_UNKNOWN

        # If late receipt resolved an UNKNOWN action, automatically record a ReconciliationRecord
        if cur_status == ACTION_STATUS_UNKNOWN and target_status in {
            ACTION_STATUS_SUCCEEDED,
            ACTION_STATUS_FAILED,
        }:
            rec_id = ''.join(['arec:', str(sha256_hex(''.join([str(rcpt.action_id), ':', str(rcpt.receipt_id), ':late']))[:16])])
            rec_obj = ReconciliationRecord(
                reconciliation_id=rec_id,
                action_id=rcpt.action_id,
                attempt_id=rcpt.attempt_id,
                strategy=RECON_STRATEGY_LATE_RECEIPT,
                evidence_refs=[rcpt.receipt_id],
                result=(
                    RECON_RESULT_CONFIRMED_SUCCEEDED
                    if target_status == ACTION_STATUS_SUCCEEDED
                    else RECON_RESULT_CONFIRMED_FAILED
                ),
                previous_status=ACTION_STATUS_UNKNOWN,
                resolved_status=target_status,
                occurred_at=rcpt.observed_at,
                recorded_at=_iso(now()),
            )
            state["reconciliations"][rec_id] = rec_obj.to_dict()
            act["last_reconciliation_ref"] = rec_id
            act["reconciliation_refs"].append(rec_id)

        act["status"] = target_status
        act["revision"] = int(act["revision"]) + 1
        act["updated_at"] = _iso(now())
        state["attempts"][rcpt.attempt_id] = att
        state["actions"][rcpt.action_id] = act

        self.store.commit_mutation(
            lease=self.lease,
            capability=self.capability,
            next_state=state,
            journal_payload={
                "event_type": "RECEIPT_INGESTED",
                "action_id": rcpt.action_id,
                "attempt_id": rcpt.attempt_id,
                "receipt_id": rcpt.receipt_id,
                "status_claim": claim,
                "from_status": cur_status,
                "to_status": target_status,
            },
        )
        if target_status == ACTION_STATUS_SUCCEEDED and fault_hook is not None:
            fault_hook("after_success_commit")

        return {
            "idempotent_replay": False,
            "action": copy.deepcopy(act),
            "receipt": rcpt_dict,
        }

    # -- 7. Record ResultEvidence (Separate from Action status, Section 9, 37, 77)

    def record_result_evidence(
        self,
        evidence: ResultEvidence,
    ) -> dict[str, Any]:
        """Record observer-verified ResultEvidence without altering ActionRecord.status to 'OBSERVED_RESULT'."""
        state = self.store.load_state(_mutable=True)
        act = state["actions"].get(evidence.action_id)
        if act is None:
            raise ActionTransitionError(f"action_id={evidence.action_id!r} not found")
        if evidence.evidence_id in state["result_evidences"]:
            return copy.deepcopy(state["result_evidences"][evidence.evidence_id])

        ev_dict = evidence.to_dict()
        state["result_evidences"][evidence.evidence_id] = ev_dict
        act["result_evidence_refs"].append(evidence.evidence_id)
        if act["action_kind"] == ACTION_KIND_TOOL and evidence.verified:
            act["business_outcome_verified"] = True
        act["revision"] = int(act["revision"]) + 1
        act["updated_at"] = _iso(now())
        state["actions"][evidence.action_id] = act

        self.store.commit_mutation(
            lease=self.lease,
            capability=self.capability,
            next_state=state,
            journal_payload={
                "event_type": "RESULT_EVIDENCE_RECORDED",
                "action_id": evidence.action_id,
                "evidence_id": evidence.evidence_id,
                "verified": evidence.verified,
                "action_status_unchanged": act["status"],
            },
        )
        return copy.deepcopy(ev_dict)

    # -- 8. Deterministic Reconciliation (0 LLM, Sections 24-26, 56-57, 85) -

    def reconcile_action(
        self,
        *,
        action_id: str,
        explicit_evidence_result: Optional[str] = None,
        explicit_evidence_refs: Optional[Sequence[str]] = None,
        strategy_override: Optional[str] = None,
        occurred_at: Optional[str] = None,
    ) -> dict[str, Any]:
        """Deterministically reconcile an UNKNOWN (or in-flight) ActionRecord using 0 LLM calls."""
        state = self.store.load_state(_mutable=True)
        act = state["actions"].get(action_id)
        if act is None:
            raise ActionTransitionError(f"action_id={action_id!r} not found")
        prev_status = act["status"]
        if prev_status != ACTION_STATUS_UNKNOWN:
            raise ActionTransitionError(
                f"reconcile_action requires status=UNKNOWN, got {prev_status!r}"
            )

        att_id = act.get("last_attempt_ref")
        att = state["attempts"].get(att_id) if att_id else None

        if explicit_evidence_result is not None:
            recon_res = explicit_evidence_result
            strategy = strategy_override or RECON_STRATEGY_MANUAL_EVIDENCE
            ev_refs = normalize_ref_list(
                explicit_evidence_refs or ["evidence:explicit_reconcile"],
                "evidence_refs",
                allow_empty=False,
            )
            q_info: dict[str, Any] = {}
        else:
            adapter = self.adapters.get(act["action_kind"])
            if adapter is None:
                recon_res = RECON_RESULT_STILL_UNKNOWN
                strategy = RECON_STRATEGY_UNRECONCILABLE
                ev_refs = []
                q_info = {}
            else:
                q_info = adapter.query_status_for_reconcile(action=act, attempt=att)
                recon_res = q_info["result"]
                strategy = q_info["strategy"]
                ev_refs = list(q_info.get("evidence_refs", []))

        ts = occurred_at or _iso(now())
        if recon_res == RECON_RESULT_CONFIRMED_SUCCEEDED:
            resolved_status = ACTION_STATUS_SUCCEEDED
            act["succeeded_at"] = act.get("succeeded_at") or ts
            if q_info.get("delivery_state"):
                act["delivery_state"] = q_info["delivery_state"]
            if att is not None:
                att["status"] = ATTEMPT_STATUS_SUCCEEDED
        elif recon_res == RECON_RESULT_CONFIRMED_FAILED:
            resolved_status = ACTION_STATUS_FAILED
            act["failed_at"] = act.get("failed_at") or ts
            if att is not None:
                att["status"] = ATTEMPT_STATUS_FAILED
        elif recon_res == RECON_RESULT_NOT_SUBMITTED:
            # Proven not submitted externally -> safe to return to PREPARED or FAILED; per Section 38 matrix:
            # UNKNOWN -> FAILED (confirmed not executed) or allow re-attempt after confirmed NOT_SUBMITTED
            resolved_status = ACTION_STATUS_FAILED
            act["failed_at"] = act.get("failed_at") or ts
            if att is not None:
                att["status"] = ATTEMPT_STATUS_PRE_SUBMIT_FAILED
        elif recon_res in {RECON_RESULT_STILL_UNKNOWN, RECON_RESULT_CONFLICT}:
            # Section 57 & E03/E04: Unreconcilable or conflicting evidence remains UNKNOWN!
            resolved_status = ACTION_STATUS_UNKNOWN
        else:
            raise ContractViolationError(f"unsupported reconciliation result={recon_res!r}")

        rec_id = ''.join(['arec:', str(sha256_hex(''.join([str(action_id), ':', str(len(act['reconciliation_refs']) + 1), ':', str(recon_res)]))[:16])])
        rec_record = ReconciliationRecord(
            reconciliation_id=rec_id,
            action_id=action_id,
            attempt_id=att_id,
            strategy=strategy,
            evidence_refs=ev_refs,
            provider_query_ref=q_info.get("provider_query_ref"),
            world_query_ref=q_info.get("world_query_ref"),
            artifact_query_ref=q_info.get("artifact_query_ref"),
            result=recon_res,
            previous_status=prev_status,
            resolved_status=resolved_status,
            occurred_at=ts,
            recorded_at=_iso(now()),
        )

        state["reconciliations"][rec_id] = rec_record.to_dict()
        act["status"] = resolved_status
        act["last_reconciliation_ref"] = rec_id
        act["reconciliation_refs"].append(rec_id)
        act["revision"] = int(act["revision"]) + 1
        act["updated_at"] = _iso(now())
        if att is not None and att_id is not None:
            state["attempts"][att_id] = att
        state["actions"][action_id] = act

        self.store.commit_mutation(
            lease=self.lease,
            capability=self.capability,
            next_state=state,
            journal_payload={
                "event_type": "ACTION_RECONCILED",
                "action_id": action_id,
                "reconciliation_id": rec_id,
                "strategy": strategy,
                "result": recon_res,
                "from_status": prev_status,
                "to_status": resolved_status,
            },
        )
        return {
            "action": copy.deepcopy(act),
            "reconciliation": rec_record.to_dict(),
        }

    # -- 9. Cold-Start Crash Recovery (Sections 20, 58, 60) -----------------

    def recover(self) -> dict[str, Any]:
        """Recover WAL and transition any action with an unresolved submission fence into UNKNOWN."""
        wal_recovered = self.store.recover_from_wal_if_needed()
        state = self.store.load_state(_mutable=True)
        open_fences = dict(state.get("open_submission_intents", {}))
        fenced_to_unknown: list[str] = []

        if open_fences:
            ts = _iso(now())
            for act_id, fence in open_fences.items():
                act = state["actions"].get(act_id)
                if act is None:
                    continue
                if act["status"] in {
                    ACTION_STATUS_PREPARED,
                    ACTION_STATUS_SCHEDULED,
                    ACTION_STATUS_SUBMITTED,
                }:
                    act["status"] = ACTION_STATUS_UNKNOWN
                    act["unknown_since"] = act.get("unknown_since") or ts
                    act["revision"] = int(act["revision"]) + 1
                    act["updated_at"] = ts
                    state["actions"][act_id] = act
                    att_id = fence.get("attempt_id")
                    if att_id and att_id in state["attempts"]:
                        state["attempts"][att_id]["status"] = ATTEMPT_STATUS_UNKNOWN
                    fenced_to_unknown.append(act_id)
                state["open_submission_intents"].pop(act_id, None)

            self.store.commit_mutation(
                lease=self.lease,
                capability=self.capability,
                next_state=state,
                journal_payload={
                    "event_type": "RECOVERY_FENCED_ACTIONS_TO_UNKNOWN",
                    "fenced_action_ids": fenced_to_unknown,
                },
            )

        return {
            "wal_recovered": wal_recovered,
            "fenced_to_unknown_action_ids": fenced_to_unknown,
            "unknown_action_ids": [
                a["action_id"]
                for a in self.store.load_state(_mutable=True)["actions"].values()
                if a["status"] == ACTION_STATUS_UNKNOWN
            ],
        }

    @staticmethod
    def _ensure_transition_allowed(from_status: str, to_status: str) -> None:
        allowed = ALLOWED_ACTION_TRANSITIONS.get(from_status, frozenset())
        if to_status not in allowed:
            raise ActionTransitionError(
                f"illegal Action transition from {from_status!r} to {to_status!r}; allowed={sorted(allowed)}"
            )


# ---------------------------------------------------------------------------
# Zero-Side-Effect ActionReplayEngine (Sections 73, 76, 92)
# ---------------------------------------------------------------------------


class ActionReplayEngine:
    """Section 76 & 92: Rebuilds Action Reality projection strictly from journal/store with 0 adapter calls."""

    @staticmethod
    def replay_and_verify(
        store: ActionStore,
        *,
        adapters: Optional[Mapping[str, ActionAdapterProtocol]] = None,
    ) -> dict[str, Any]:
        before_counts = {
            k: adp.submit_call_count for k, adp in (adapters or {}).items()
        }
        entries = store.read_journal()
        snapshot = store.load_state()
        after_counts = {
            k: adp.submit_call_count for k, adp in (adapters or {}).items()
        }
        if before_counts != after_counts:
            raise ShadowIsolationError("Replay caused adapter.submit side effects!")

        # Verify journal revision & hash chain match snapshot
        assert snapshot["revision"] == len(entries)
        if entries:
            assert snapshot["last_entry_hash"] == entries[-1]["entry_hash"]
        return {
            "journal_entry_count": len(entries),
            "action_count": len(snapshot["actions"]),
            "attempt_count": len(snapshot["attempts"]),
            "receipt_count": len(snapshot["receipts"]),
            "unknown_action_ids": sorted(
                a["action_id"]
                for a in snapshot["actions"].values()
                if a["status"] == ACTION_STATUS_UNKNOWN
            ),
            "actions": copy.deepcopy(snapshot["actions"]),
            "attempts": copy.deepcopy(snapshot["attempts"]),
            "receipts": copy.deepcopy(snapshot["receipts"]),
            "adapter_submit_deltas": {
                k: after_counts[k] - before_counts[k] for k in before_counts
            },
        }
