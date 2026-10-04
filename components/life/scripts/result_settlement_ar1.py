#!/usr/bin/env python3
"""Chiyo Action Reality | AR-1 Result Settlement.

Implements the frozen AR-1 contracts for:
- Top-level invariant:
  Action Status != Receipt Claim != Observed Result != Domain Fact !=
  Settled Result != Downstream Acceptance.
- Unique canonical owner: ResultSettlementAuthority (`result_settlement_authority`).
- Core canonical aggregates & records:
  - ResultSettlementRecord (`chiyo.result_settlement.record.v1`)
  - EffectClaim (`chiyo.result_settlement.effect_claim.v1`)
  - SettlementConflict (`chiyo.result_settlement.conflict.v1`)
  - SettledResultEvent (`chiyo.result_settlement.event.v1`)
  - SettledResultCorrectionEvent (`chiyo.result_settlement.correction_event.v1`)
  - SettlementDeliveryRecord (`chiyo.result_settlement.delivery.v1`)
- Domain-specific SettlementPolicyRegistry (0 LLM, deterministic) for:
  - MESSAGE_ACTION (ACCEPTED != DELIVERED != READ; late read receipt supersedes)
  - WORLD_ACTION (requires World canonical authority fact; never writes World state)
  - FILE_ACTION (requires FileArtifactObserver hash verification; hash mismatch -> CONFLICT)
  - TOOL_ACTION (exit_code 0 = process success != business outcome verified)
- Atomic Settlement + Outbox persistence via 4-stage WAL + hash-chained append-only
  SettlementJournal (`result_settlement_journal.jsonl`).
- At-least-once SettlementOutboxDispatcher with per-consumer isolation and idempotency
  for FakeLifeConsumer, FakeMemoryConsumer, FakeGoalConsumer, FakeWorldConsumer.
- Zero-side-effect ResultSettlementReplay and cold-start crash recovery across all
  settlement, outbox, dispatch, ack, and supersession stages.
"""

from __future__ import annotations

import copy
import json
import os
import uuid
from dataclasses import dataclass, field
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
from action_reality_ledger import (
    ACTION_KIND_FILE,
    ACTION_KIND_MESSAGE,
    ACTION_KIND_TOOL,
    ACTION_KIND_WORLD,
    ACTION_STATUS_ACKNOWLEDGED,
    ACTION_STATUS_CANCELLED,
    ACTION_STATUS_FAILED,
    ACTION_STATUS_PREPARED,
    ACTION_STATUS_PROPOSED,
    ACTION_STATUS_SCHEDULED,
    ACTION_STATUS_SUBMITTED,
    ACTION_STATUS_SUCCEEDED,
    ACTION_STATUS_UNKNOWN,
    ATTEMPT_STATUS_PRE_SUBMIT_FAILED,
    ActionAdapterProtocol,
    ActionReadService,
    ActionReceipt,
    ActionStore,
    EVIDENCE_KIND_EXECUTOR_REPORTED,
    EVIDENCE_KIND_FILESYSTEM_OBSERVED,
    EVIDENCE_KIND_PROVIDER_REPORTED,
    EVIDENCE_KIND_WORLD_AUTHORITATIVE,
    NON_AUTHORITATIVE_CALLERS,
    RECEIPT_CLAIM_ACCEPTED,
    RECEIPT_CLAIM_CANCELLED,
    RECEIPT_CLAIM_DELIVERED,
    RECEIPT_CLAIM_FAILED,
    RECEIPT_CLAIM_PROCESS_EXIT_0,
    RECEIPT_CLAIM_READ,
    RECEIPT_CLAIM_REJECTED,
    RECEIPT_CLAIM_SUCCEEDED,
    RECEIPT_CLAIM_UNKNOWN,
    ResultEvidence,
    ShadowIsolationError,
    VALID_ACTION_KINDS,
    validate_four_timestamps,
)

# ---------------------------------------------------------------------------
# Frozen Constants & Schemas (AR-1)
# ---------------------------------------------------------------------------

SETTLEMENT_POLICY_VERSION_V1 = "chiyo.result_settlement.policy.v1"

SCHEMA_SETTLEMENT_RECORD = "chiyo.result_settlement.record.v1"
SCHEMA_EFFECT_CLAIM = "chiyo.result_settlement.effect_claim.v1"
SCHEMA_SETTLEMENT_CONFLICT = "chiyo.result_settlement.conflict.v1"
SCHEMA_SETTLED_RESULT_EVENT = "chiyo.result_settlement.event.v1"
SCHEMA_SETTLED_CORRECTION_EVENT = "chiyo.result_settlement.correction_event.v1"
SCHEMA_SETTLEMENT_DELIVERY = "chiyo.result_settlement.delivery.v1"
SCHEMA_SETTLEMENT_STATE = "chiyo.result_settlement.state.v1"
SCHEMA_SETTLEMENT_JOURNAL_ENTRY = "chiyo.result_settlement.journal.v1"
SCHEMA_SETTLEMENT_WAL = "chiyo.result_settlement.wal.v1"

WRITER_DOMAIN_RESULT_SETTLEMENT = "result_settlement_authority"

FORBIDDEN_SETTLEMENT_WRITER_DOMAINS = frozenset(
    {
        "action_reality_authority",
        "life_activity_authority",
        "life_resume_condition_authority",
        "life_agenda_authority",
        "life_resume_eligibility_authority",
        "memory_authority",
        "goal_authority",
        "world_authority",
        "world_event_authority",
        "expression_authority",
        "grounding_authority",
        "telegram_adapter",
        "tool_adapter",
        "world_adapter",
        "file_adapter",
        "shadow",
        "replay",
        "research",
        "evaluator",
        "twin_chiyo",
    }
)

# Section 8: Evidence Authority Grades
AUTH_GRADE_DOMAIN_FACT = "AUTHORITATIVE_DOMAIN_FACT"
AUTH_GRADE_OBSERVER_VERIFIED = "OBSERVER_VERIFIED"
AUTH_GRADE_EXECUTOR_RECEIPT = "AUTHORITATIVE_EXECUTOR_RECEIPT"
AUTH_GRADE_PROVIDER_REPORTED = "PROVIDER_REPORTED"
AUTH_GRADE_TRANSPORT_RECEIPT = "TRANSPORT_RECEIPT"
AUTH_GRADE_INFERRED = "INFERRED"
AUTH_GRADE_UNTRUSTED = "UNTRUSTED"

VALID_AUTHORITY_GRADES = frozenset(
    {
        AUTH_GRADE_DOMAIN_FACT,
        AUTH_GRADE_OBSERVER_VERIFIED,
        AUTH_GRADE_EXECUTOR_RECEIPT,
        AUTH_GRADE_PROVIDER_REPORTED,
        AUTH_GRADE_TRANSPORT_RECEIPT,
        AUTH_GRADE_INFERRED,
        AUTH_GRADE_UNTRUSTED,
    }
)

AUTHORITY_GRADE_RANK: dict[str, int] = {
    AUTH_GRADE_DOMAIN_FACT: 100,
    AUTH_GRADE_OBSERVER_VERIFIED: 95,
    AUTH_GRADE_PROVIDER_REPORTED: 80,
    AUTH_GRADE_EXECUTOR_RECEIPT: 70,
    AUTH_GRADE_TRANSPORT_RECEIPT: 40,
    AUTH_GRADE_INFERRED: 10,
    AUTH_GRADE_UNTRUSTED: 0,
}

# Section 11: Settlement Statuses
SETTLEMENT_STATUS_OPEN = "OPEN"
SETTLEMENT_STATUS_SETTLED = "SETTLED"
SETTLEMENT_STATUS_PARTIAL = "PARTIAL"
SETTLEMENT_STATUS_CONFLICT = "CONFLICT"
SETTLEMENT_STATUS_UNRESOLVED = "UNRESOLVED"
SETTLEMENT_STATUS_SUPERSEDED = "SUPERSEDED"

VALID_SETTLEMENT_STATUSES = frozenset(
    {
        SETTLEMENT_STATUS_OPEN,
        SETTLEMENT_STATUS_SETTLED,
        SETTLEMENT_STATUS_PARTIAL,
        SETTLEMENT_STATUS_CONFLICT,
        SETTLEMENT_STATUS_UNRESOLVED,
        SETTLEMENT_STATUS_SUPERSEDED,
    }
)

# Section 12: Outcome Kinds
OUTCOME_SUCCESS_EFFECT = "SUCCESS_EFFECT"
OUTCOME_FAILURE_NO_EFFECT = "FAILURE_NO_EFFECT"
OUTCOME_PARTIAL_EFFECT = "PARTIAL_EFFECT"
OUTCOME_CANCELLED_NO_EFFECT = "CANCELLED_NO_EFFECT"
OUTCOME_UNKNOWN_EFFECT = "UNKNOWN_EFFECT"

VALID_OUTCOME_KINDS = frozenset(
    {
        OUTCOME_SUCCESS_EFFECT,
        OUTCOME_FAILURE_NO_EFFECT,
        OUTCOME_PARTIAL_EFFECT,
        OUTCOME_CANCELLED_NO_EFFECT,
        OUTCOME_UNKNOWN_EFFECT,
    }
)

# Epistemic statuses for EffectClaim (Section 14)
EPISTEMIC_CONFIRMED = "CONFIRMED"
EPISTEMIC_PARTIAL = "PARTIAL"
EPISTEMIC_CONFLICTING = "CONFLICTING"
EPISTEMIC_UNVERIFIED = "UNVERIFIED"

VALID_EPISTEMIC_STATUSES = frozenset(
    {
        EPISTEMIC_CONFIRMED,
        EPISTEMIC_PARTIAL,
        EPISTEMIC_CONFLICTING,
        EPISTEMIC_UNVERIFIED,
    }
)

# Consumer domains & dispositions (Sections 21, 49-57)
CONSUMER_DOMAIN_LIFE = "life"
CONSUMER_DOMAIN_MEMORY = "memory"
CONSUMER_DOMAIN_GOAL = "goal"
CONSUMER_DOMAIN_WORLD = "world"
CONSUMER_DOMAIN_ARTIFACT = "artifact"

VALID_CONSUMER_DOMAINS = frozenset(
    {
        CONSUMER_DOMAIN_LIFE,
        CONSUMER_DOMAIN_MEMORY,
        CONSUMER_DOMAIN_GOAL,
        CONSUMER_DOMAIN_WORLD,
        CONSUMER_DOMAIN_ARTIFACT,
    }
)

DISPOSITION_ACCEPT = "ACCEPT"
DISPOSITION_IGNORE = "IGNORE"
DISPOSITION_DEFER = "DEFER"
DISPOSITION_REJECT = "REJECT"
DISPOSITION_CONFLICT = "CONFLICT"

VALID_CONSUMER_DISPOSITIONS = frozenset(
    {
        DISPOSITION_ACCEPT,
        DISPOSITION_IGNORE,
        DISPOSITION_DEFER,
        DISPOSITION_REJECT,
        DISPOSITION_CONFLICT,
    }
)

DELIVERY_STATUS_PENDING = "PENDING"
DELIVERY_STATUS_DELIVERED = "DELIVERED_TO_CONSUMER"
DELIVERY_STATUS_FAILED = "DELIVERY_FAILED"

VALID_DELIVERY_STATUSES = frozenset(
    {
        DELIVERY_STATUS_PENDING,
        DELIVERY_STATUS_DELIVERED,
        DELIVERY_STATUS_FAILED,
    }
)

EVENT_KIND_SETTLED = "SETTLED_RESULT"
EVENT_KIND_CORRECTION = "SETTLED_RESULT_CORRECTION"


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class ResultSettlementError(Exception):
    """Base error for AR-1 Result Settlement."""


class UntrustedEvidenceError(ResultSettlementError):
    """Raised when untrusted or narrative text is submitted as settlement evidence."""


class AuthorityUsurpationError(ResultSettlementError):
    """Raised when an EffectClaim attempts to usurp World or Artifact canonical authority."""


class ConsumerIsolationError(ResultSettlementError):
    """Raised when a consumer or settlement attempts to obtain cross-domain writer capabilities."""


# ---------------------------------------------------------------------------
# Authority & Capability Guards (Sections 6, 22, 76-78)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SettlementWriterCapability:
    """Domain-bound writer capability for AR-1 ResultSettlementAuthority."""

    capability_id: str
    domain: str
    writer_domain: str
    namespace: str
    store_root: Path
    lease_instance_id: str
    issued_to: str
    _signature: str = field(repr=False)


class ResultSettlementAuthority:
    """Sole canonical owner of ResultSettlementRecord, SettledResultEvent, Journal, and Outbox."""

    @staticmethod
    def issue_writer_capability(
        lease: ActivityAuthorityLease,
        *,
        caller_module: str = "result_settlement_service",
        namespace: str = NAMESPACE_ISOLATED_TEST,
    ) -> SettlementWriterCapability:
        if not isinstance(lease, ActivityAuthorityLease) or not lease.held:
            raise WriterCapabilityError("A held ActivityAuthorityLease is required")
        if (
            caller_module in NON_AUTHORITATIVE_CALLERS
            or caller_module in FORBIDDEN_SETTLEMENT_WRITER_DOMAINS
        ):
            raise WriterCapabilityError(
                f"caller_module={caller_module!r} is forbidden from holding {WRITER_DOMAIN_RESULT_SETTLEMENT}"
            )
        if namespace == NAMESPACE_CANONICAL or is_production_path(lease.store_root):
            raise WriterCapabilityError(
                "AR-1 production non-cutover guard: canonical/production settlement capability denied"
            )
        sig = lease.sign_capability(WRITER_DOMAIN_RESULT_SETTLEMENT, namespace)
        return SettlementWriterCapability(
            capability_id=f"cap_ar1_{uuid.uuid4().hex[:16]}",
            domain=WRITER_DOMAIN_RESULT_SETTLEMENT,
            writer_domain=WRITER_DOMAIN_RESULT_SETTLEMENT,
            namespace=namespace,
            store_root=lease.store_root,
            lease_instance_id=lease.instance_id,
            issued_to=caller_module,
            _signature=sig,
        )

    @staticmethod
    def request_domain_writer_capability(target_domain: str) -> None:
        """Section 22, 78, E05: Settlement and consumers can never obtain domain writer capabilities."""
        raise ConsumerIsolationError(
            f"AR-1 ResultSettlementAuthority and its consumers are strictly forbidden from obtaining "
            f"writer capability for domain {target_domain!r}"
        )


def verify_settlement_writer_capability(
    lease: ActivityAuthorityLease,
    capability: Any,
) -> None:
    if not isinstance(lease, ActivityAuthorityLease) or not lease.held:
        raise WriterCapabilityError("missing or released lease for ResultSettlementAuthority")
    if not isinstance(capability, SettlementWriterCapability):
        raise WriterCapabilityError("missing SettlementWriterCapability for ResultSettlementAuthority")
    if capability.domain != WRITER_DOMAIN_RESULT_SETTLEMENT:
        raise WriterCapabilityError(
            f"invalid capability domain {capability.domain!r}; expected {WRITER_DOMAIN_RESULT_SETTLEMENT!r}"
        )
    if (
        capability.issued_to in FORBIDDEN_SETTLEMENT_WRITER_DOMAINS
        or capability.issued_to in NON_AUTHORITATIVE_CALLERS
    ):
        raise WriterCapabilityError(
            f"capability issued_to={capability.issued_to!r} is forbidden from mutating Result Settlement"
        )
    if not lease.verify_capability(capability):  # type: ignore[arg-type]
        raise WriterCapabilityError("SettlementWriterCapability HMAC signature verification failed")


# ---------------------------------------------------------------------------
# Evidence Input & Authority Grading (Sections 7-9, 32-36, 73)
# ---------------------------------------------------------------------------


@dataclass
class SettlementEvidenceInput:
    """Normalized, authority-graded evidence input for Result Settlement."""

    evidence_id: str
    root_evidence_id: str
    action_id: str
    source_ref: str
    authority_domain: str
    evidence_kind: str
    authority_grade: str
    claim_kind: str
    verified: bool
    occurred_at: str
    observed_at: str
    recorded_at: str
    target_ref: Optional[str] = None
    domain_fact_ref: Optional[str] = None
    expected_hash: Optional[str] = None
    observed_hash: Optional[str] = None
    details: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.evidence_id, str) or ":" not in self.evidence_id:
            raise ContractViolationError(f"structured evidence_id required, got {self.evidence_id!r}")
        if not isinstance(self.root_evidence_id, str) or ":" not in self.root_evidence_id:
            raise ContractViolationError(f"structured root_evidence_id required, got {self.root_evidence_id!r}")
        _validate_structured_ref(self.action_id, "action_id", allowed_prefixes=frozenset({"actn"}))
        _validate_structured_ref(self.source_ref, "source_ref")
        if not isinstance(self.authority_domain, str) or not self.authority_domain.strip():
            raise ContractViolationError("authority_domain must be non-empty")
        if not isinstance(self.evidence_kind, str) or not self.evidence_kind.strip():
            raise ContractViolationError("evidence_kind must be non-empty")
        if self.authority_grade not in VALID_AUTHORITY_GRADES:
            raise ContractViolationError(f"invalid authority_grade={self.authority_grade!r}")
        if self.authority_grade == AUTH_GRADE_UNTRUSTED:
            raise UntrustedEvidenceError(f"UNTRUSTED evidence {self.evidence_id!r} cannot enter Result Settlement")
        validate_four_timestamps(
            occurred_at=self.occurred_at,
            observed_at=self.observed_at,
            recorded_at=self.recorded_at,
            enqueued_at=self.recorded_at,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "evidence_id": self.evidence_id,
            "root_evidence_id": self.root_evidence_id,
            "action_id": self.action_id,
            "source_ref": self.source_ref,
            "authority_domain": self.authority_domain,
            "evidence_kind": self.evidence_kind,
            "authority_grade": self.authority_grade,
            "claim_kind": self.claim_kind,
            "verified": bool(self.verified),
            "occurred_at": self.occurred_at,
            "observed_at": self.observed_at,
            "recorded_at": self.recorded_at,
            "target_ref": self.target_ref,
            "domain_fact_ref": self.domain_fact_ref,
            "expected_hash": self.expected_hash,
            "observed_hash": self.observed_hash,
            "details": copy.deepcopy(self.details),
        }

    @staticmethod
    def from_action_receipt(receipt: Mapping[str, Any] | ActionReceipt) -> SettlementEvidenceInput:
        r = receipt.to_dict() if isinstance(receipt, ActionReceipt) else dict(receipt)
        ev_kind = str(r["evidence_kind"])
        claim = str(r["status_claim"])
        if ev_kind == EVIDENCE_KIND_WORLD_AUTHORITATIVE:
            grade = AUTH_GRADE_DOMAIN_FACT
        elif ev_kind == EVIDENCE_KIND_PROVIDER_REPORTED:
            grade = AUTH_GRADE_PROVIDER_REPORTED
        elif ev_kind == EVIDENCE_KIND_FILESYSTEM_OBSERVED:
            # Adapter's own receipt is an executor receipt; independent FileArtifactObserver is OBSERVER_VERIFIED
            grade = (
                AUTH_GRADE_EXECUTOR_RECEIPT
                if r.get("provider") == "sandbox_file_adapter"
                else AUTH_GRADE_OBSERVER_VERIFIED
            )
        else:
            grade = AUTH_GRADE_EXECUTOR_RECEIPT

        op_ref = r.get("provider_operation_ref") or r["receipt_id"]
        root_hash_input = f"{r['action_id']}:{op_ref}:{claim}"
        root_id = f"rootev:{sha256_hex(root_hash_input)[:16]}"
        verified = claim in {
            RECEIPT_CLAIM_ACCEPTED,
            RECEIPT_CLAIM_DELIVERED,
            RECEIPT_CLAIM_READ,
            RECEIPT_CLAIM_SUCCEEDED,
            RECEIPT_CLAIM_PROCESS_EXIT_0,
        }
        domain_fact_ref = None
        if ev_kind == EVIDENCE_KIND_WORLD_AUTHORITATIVE:
            ev_refs = r.get("evidence_refs") or []
            domain_fact_ref = ev_refs[0] if ev_refs else r.get("provider_operation_ref")

        return SettlementEvidenceInput(
            evidence_id=str(r["receipt_id"]),
            root_evidence_id=root_id,
            action_id=str(r["action_id"]),
            source_ref=str(r["receipt_id"]),
            authority_domain=str(r["authority_domain"]),
            evidence_kind=ev_kind,
            authority_grade=grade,
            claim_kind=f"receipt:{claim}",
            verified=verified,
            occurred_at=str(r["occurred_at"]),
            observed_at=str(r["observed_at"]),
            recorded_at=str(r["recorded_at"]),
            target_ref=r.get("details", {}).get("path") or r.get("provider_operation_ref"),
            domain_fact_ref=domain_fact_ref,
            expected_hash=r.get("source_hash"),
            observed_hash=r.get("source_hash"),
            details=dict(r.get("details", {})),
        )

    @staticmethod
    def from_result_evidence(evidence: Mapping[str, Any] | ResultEvidence) -> SettlementEvidenceInput:
        e = evidence.to_dict() if isinstance(evidence, ResultEvidence) else dict(evidence)
        obs_kind = str(e["observer_kind"])
        if obs_kind in {"filesystem_observer", "artifact_observer"}:
            grade = AUTH_GRADE_OBSERVER_VERIFIED
            auth_domain = "artifact_authority"
            ev_kind = EVIDENCE_KIND_FILESYSTEM_OBSERVED
        elif obs_kind in {"world_authority", "world_observer"}:
            grade = AUTH_GRADE_DOMAIN_FACT
            auth_domain = "world_authority"
            ev_kind = EVIDENCE_KIND_WORLD_AUTHORITATIVE
        elif obs_kind in {"inferred_observer", "model_inference", "stdout_parser"}:
            grade = AUTH_GRADE_INFERRED
            auth_domain = "inferred_non_authoritative"
            ev_kind = "inferred-evidence"
        else:
            grade = AUTH_GRADE_OBSERVER_VERIFIED
            auth_domain = obs_kind
            ev_kind = "observer-verified"

        art_ref = str(e["artifact_ref"])
        obs_hash = e.get("observed_hash") or "none"
        root_hash_input = f"{e['action_id']}:{art_ref}:{obs_hash}:{e['verified']}"
        root_id = f"rootev:{sha256_hex(root_hash_input)[:16]}"
        return SettlementEvidenceInput(
            evidence_id=str(e["evidence_id"]),
            root_evidence_id=root_id,
            action_id=str(e["action_id"]),
            source_ref=str(e["evidence_id"]),
            authority_domain=auth_domain,
            evidence_kind=ev_kind,
            authority_grade=grade,
            claim_kind=f"observer:{obs_kind}",
            verified=bool(e["verified"]),
            occurred_at=str(e["occurred_at"]),
            observed_at=str(e["observed_at"]),
            recorded_at=str(e["recorded_at"]),
            target_ref=art_ref,
            domain_fact_ref=str(e["evidence_id"]),
            expected_hash=e.get("expected_hash"),
            observed_hash=e.get("observed_hash"),
            details=dict(e.get("details", {})),
        )


# ---------------------------------------------------------------------------
# Canonical Domain Models (Sections 10-18, 35, 53, 67-68)
# ---------------------------------------------------------------------------


@dataclass
class EffectClaim:
    """Section 14-16: Bounded statement of settled effect with mandatory provenance & authority refs."""

    claim_id: str
    settlement_id: str
    action_id: str
    domain: str
    predicate: str
    evidence_refs: list[str]
    authority_refs: list[str]
    authority_domain: str
    authority_grade: str
    epistemic_status: str
    occurred_at: str
    observed_at: str
    recorded_at: str
    subject_ref: Optional[str] = None
    object_ref: Optional[str] = None
    details: dict[str, Any] = field(default_factory=dict)
    schema_version: str = SCHEMA_EFFECT_CLAIM

    def __post_init__(self) -> None:
        _validate_structured_ref(self.claim_id, "claim_id", allowed_prefixes=frozenset({"eclm"}))
        _validate_structured_ref(self.settlement_id, "settlement_id", allowed_prefixes=frozenset({"rset"}))
        _validate_structured_ref(self.action_id, "action_id", allowed_prefixes=frozenset({"actn"}))
        if not isinstance(self.domain, str) or not self.domain.strip():
            raise ContractViolationError("EffectClaim.domain must be non-empty")
        if not isinstance(self.predicate, str) or not self.predicate.strip():
            raise ContractViolationError("EffectClaim.predicate must be non-empty")
        self.evidence_refs = normalize_ref_list(self.evidence_refs, "evidence_refs", allow_empty=False)
        self.authority_refs = normalize_ref_list(self.authority_refs, "authority_refs", allow_empty=False)
        if self.authority_grade not in VALID_AUTHORITY_GRADES:
            raise ContractViolationError(f"invalid authority_grade={self.authority_grade!r}")
        if self.epistemic_status not in VALID_EPISTEMIC_STATUSES:
            raise ContractViolationError(f"invalid epistemic_status={self.epistemic_status!r}")

        # Section 15: World EffectClaim cannot usurp World Authority
        if self.domain == "world" and self.epistemic_status == EPISTEMIC_CONFIRMED:
            if self.authority_grade != AUTH_GRADE_DOMAIN_FACT or self.authority_domain not in {
                "world_authority",
                "sandbox_world_authority",
            }:
                raise AuthorityUsurpationError(
                    f"World EffectClaim {self.claim_id!r} cannot claim CONFIRMED effect without "
                    f"AUTHORITATIVE_DOMAIN_FACT from world_authority (got {self.authority_grade!r} / {self.authority_domain!r})"
                )

        # Section 16: Artifact EffectClaim cannot usurp Artifact/File Observer Authority
        if self.domain == "artifact" and self.epistemic_status == EPISTEMIC_CONFIRMED:
            if self.authority_grade != AUTH_GRADE_OBSERVER_VERIFIED:
                raise AuthorityUsurpationError(
                    f"Artifact EffectClaim {self.claim_id!r} requires OBSERVER_VERIFIED evidence "
                    f"(got {self.authority_grade!r})"
                )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "claim_id": self.claim_id,
            "settlement_id": self.settlement_id,
            "action_id": self.action_id,
            "domain": self.domain,
            "predicate": self.predicate,
            "subject_ref": self.subject_ref,
            "object_ref": self.object_ref,
            "evidence_refs": list(self.evidence_refs),
            "authority_refs": list(self.authority_refs),
            "authority_domain": self.authority_domain,
            "authority_grade": self.authority_grade,
            "epistemic_status": self.epistemic_status,
            "occurred_at": self.occurred_at,
            "observed_at": self.observed_at,
            "recorded_at": self.recorded_at,
            "details": copy.deepcopy(self.details),
        }


@dataclass
class SettlementConflict:
    """Section 34 & 35: Auditable conflict record when evidences contradict."""

    conflict_id: str
    settlement_id: str
    action_id: str
    claim_refs: list[str]
    evidence_refs: list[str]
    authority_domains: list[str]
    reason_code: str
    detected_at: str
    status: str = "OPEN"
    details: dict[str, Any] = field(default_factory=dict)
    schema_version: str = SCHEMA_SETTLEMENT_CONFLICT

    def __post_init__(self) -> None:
        _validate_structured_ref(self.conflict_id, "conflict_id", allowed_prefixes=frozenset({"sconf"}))
        _validate_structured_ref(self.settlement_id, "settlement_id", allowed_prefixes=frozenset({"rset"}))
        _validate_structured_ref(self.action_id, "action_id", allowed_prefixes=frozenset({"actn"}))
        self.evidence_refs = normalize_ref_list(self.evidence_refs, "evidence_refs", allow_empty=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "conflict_id": self.conflict_id,
            "settlement_id": self.settlement_id,
            "action_id": self.action_id,
            "claim_refs": list(self.claim_refs),
            "evidence_refs": list(self.evidence_refs),
            "authority_domains": list(self.authority_domains),
            "reason_code": self.reason_code,
            "detected_at": self.detected_at,
            "status": self.status,
            "details": copy.deepcopy(self.details),
        }


@dataclass
class ResultSettlementRecord:
    """Section 10-13: Canonical ResultSettlementRecord aggregate."""

    settlement_id: str
    revision: int
    subject_id: str
    action_id: str
    action_revision_ref: str
    root_result_id: str
    correlation_id: str
    settlement_status: str
    outcome_kind: str
    receipt_refs: list[str]
    evidence_refs: list[str]
    authoritative_result_refs: list[str]
    root_evidence_ids: list[str]
    effect_claims: list[dict[str, Any]]
    effect_claim_refs: list[str]
    confirmed_effect_refs: list[str]
    missing_effect_refs: list[str]
    failed_effect_refs: list[str]
    unresolved_claims: list[str]
    conflict_refs: list[str]
    effect_occurred_at: Optional[str]
    effect_observed_at: str
    settled_at: Optional[str]
    recorded_at: str
    supersedes_ref: Optional[str] = None
    policy_version: str = SETTLEMENT_POLICY_VERSION_V1
    schema_version: str = SCHEMA_SETTLEMENT_RECORD

    def __post_init__(self) -> None:
        validate_subject_id(self.subject_id)
        _validate_structured_ref(self.settlement_id, "settlement_id", allowed_prefixes=frozenset({"rset"}))
        _validate_structured_ref(self.action_id, "action_id", allowed_prefixes=frozenset({"actn"}))
        _validate_structured_ref(self.action_revision_ref, "action_revision_ref")
        _validate_structured_ref(self.root_result_id, "root_result_id", allowed_prefixes=frozenset({"rres"}))
        _validate_structured_ref(self.correlation_id, "correlation_id", allowed_prefixes=frozenset({"corr"}))
        if self.settlement_status not in VALID_SETTLEMENT_STATUSES:
            raise ContractViolationError(f"invalid settlement_status={self.settlement_status!r}")
        if self.outcome_kind not in VALID_OUTCOME_KINDS:
            raise ContractViolationError(f"invalid outcome_kind={self.outcome_kind!r}")
        if self.supersedes_ref is not None:
            _validate_structured_ref(self.supersedes_ref, "supersedes_ref", allowed_prefixes=frozenset({"rset"}))

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "settlement_id": self.settlement_id,
            "revision": int(self.revision),
            "subject_id": self.subject_id,
            "action_id": self.action_id,
            "action_revision_ref": self.action_revision_ref,
            "root_result_id": self.root_result_id,
            "correlation_id": self.correlation_id,
            "settlement_status": self.settlement_status,
            "outcome_kind": self.outcome_kind,
            "receipt_refs": list(self.receipt_refs),
            "evidence_refs": list(self.evidence_refs),
            "authoritative_result_refs": list(self.authoritative_result_refs),
            "root_evidence_ids": list(self.root_evidence_ids),
            "effect_claims": copy.deepcopy(self.effect_claims),
            "effect_claim_refs": list(self.effect_claim_refs),
            "confirmed_effect_refs": list(self.confirmed_effect_refs),
            "missing_effect_refs": list(self.missing_effect_refs),
            "failed_effect_refs": list(self.failed_effect_refs),
            "unresolved_claims": list(self.unresolved_claims),
            "conflict_refs": list(self.conflict_refs),
            "occurred_at": self.effect_occurred_at,
            "effect_occurred_at": self.effect_occurred_at,
            "observed_at": self.effect_observed_at,
            "effect_observed_at": self.effect_observed_at,
            "settled_at": self.settled_at,
            "recorded_at": self.recorded_at,
            "supersedes_ref": self.supersedes_ref,
            "policy_version": self.policy_version,
        }


@dataclass
class SettledResultEvent:
    """Section 17-18, 67-68: Standard outbox event (and correction event) emitted by Result Settlement."""

    event_id: str
    event_kind: str
    subject_id: str
    action_id: str
    settlement_ref: str
    root_result_id: str
    correlation_id: str
    settlement_status: str
    outcome_kind: str
    effect_claim_refs: list[str]
    evidence_refs: list[str]
    confirmed_effect_refs: list[str]
    missing_effect_refs: list[str]
    failed_effect_refs: list[str]
    occurred_at: str
    observed_at: str
    recorded_at: str
    enqueued_at: str
    causal_parent_refs: list[str]
    settlement_revision: int
    activity_ref: Optional[str] = None
    supersedes_settlement_ref: Optional[str] = None
    supersedes_event_ref: Optional[str] = None
    correction_reason: Optional[str] = None
    policy_version: str = SETTLEMENT_POLICY_VERSION_V1
    schema_version: str = SCHEMA_SETTLED_RESULT_EVENT

    def __post_init__(self) -> None:
        validate_subject_id(self.subject_id)
        _validate_structured_ref(self.event_id, "event_id", allowed_prefixes=frozenset({"srev", "scor"}))
        _validate_structured_ref(self.action_id, "action_id", allowed_prefixes=frozenset({"actn"}))
        _validate_structured_ref(self.settlement_ref, "settlement_ref", allowed_prefixes=frozenset({"rset"}))
        _validate_structured_ref(self.root_result_id, "root_result_id", allowed_prefixes=frozenset({"rres"}))
        _validate_structured_ref(self.correlation_id, "correlation_id", allowed_prefixes=frozenset({"corr"}))
        if self.outcome_kind not in VALID_OUTCOME_KINDS:
            raise ContractViolationError(f"invalid outcome_kind={self.outcome_kind!r}")
        validate_four_timestamps(
            occurred_at=self.occurred_at,
            observed_at=self.observed_at,
            recorded_at=self.recorded_at,
            enqueued_at=self.enqueued_at,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": (
                SCHEMA_SETTLED_CORRECTION_EVENT
                if self.event_kind == EVENT_KIND_CORRECTION
                else self.schema_version
            ),
            "event_id": self.event_id,
            "event_kind": self.event_kind,
            "subject_id": self.subject_id,
            "action_id": self.action_id,
            "activity_ref": self.activity_ref,
            "settlement_ref": self.settlement_ref,
            "supersedes_settlement_ref": self.supersedes_settlement_ref,
            "supersedes_event_ref": self.supersedes_event_ref,
            "correction_reason": self.correction_reason,
            "root_result_id": self.root_result_id,
            "correlation_id": self.correlation_id,
            "settlement_status": self.settlement_status,
            "outcome_kind": self.outcome_kind,
            "effect_claim_refs": list(self.effect_claim_refs),
            "evidence_refs": list(self.evidence_refs),
            "confirmed_effect_refs": list(self.confirmed_effect_refs),
            "missing_effect_refs": list(self.missing_effect_refs),
            "failed_effect_refs": list(self.failed_effect_refs),
            "occurred_at": self.occurred_at,
            "observed_at": self.observed_at,
            "recorded_at": self.recorded_at,
            "enqueued_at": self.enqueued_at,
            "causal_parent_refs": list(self.causal_parent_refs),
            "settlement_revision": int(self.settlement_revision),
            "policy_version": self.policy_version,
        }


@dataclass
class SettlementDeliveryRecord:
    """Section 53 & 57: Tracks internal outbox event delivery separately from consumer acceptance."""

    delivery_id: str
    event_id: str
    consumer_domain: str
    attempt: int
    status: str
    consumer_disposition: Optional[str] = None
    downstream_proposal_ref: Optional[str] = None
    last_error_ref: Optional[str] = None
    delivered_at: Optional[str] = None
    acknowledged_at: Optional[str] = None
    schema_version: str = SCHEMA_SETTLEMENT_DELIVERY

    def __post_init__(self) -> None:
        _validate_structured_ref(self.delivery_id, "delivery_id", allowed_prefixes=frozenset({"sdel"}))
        _validate_structured_ref(self.event_id, "event_id", allowed_prefixes=frozenset({"srev", "scor"}))
        if self.consumer_domain not in VALID_CONSUMER_DOMAINS:
            raise ContractViolationError(f"invalid consumer_domain={self.consumer_domain!r}")
        if self.status not in VALID_DELIVERY_STATUSES:
            raise ContractViolationError(f"invalid delivery status={self.status!r}")
        if self.consumer_disposition is not None and self.consumer_disposition not in VALID_CONSUMER_DISPOSITIONS:
            raise ContractViolationError(f"invalid consumer_disposition={self.consumer_disposition!r}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "delivery_id": self.delivery_id,
            "event_id": self.event_id,
            "consumer_domain": self.consumer_domain,
            "attempt": int(self.attempt),
            "status": self.status,
            "consumer_disposition": self.consumer_disposition,
            "downstream_proposal_ref": self.downstream_proposal_ref,
            "last_error_ref": self.last_error_ref,
            "delivered_at": self.delivered_at,
            "acknowledged_at": self.acknowledged_at,
        }


# ---------------------------------------------------------------------------
# WAL-Backed SettlementStore (Atomic Settlement + Outbox, Sections 19-20, 61)
# ---------------------------------------------------------------------------


class SettlementStore:
    """Crash-safe 4-stage WAL + hash-chained append-only SettlementJournal + atomic Outbox."""

    STATE_FILENAME = "result_settlement_state.json"
    JOURNAL_FILENAME = "result_settlement_journal.jsonl"
    WAL_FILENAME = "result_settlement.wal"

    def __init__(self, store_root: Path | str, *, namespace: str = NAMESPACE_ISOLATED_TEST):
        self.store_root = Path(store_root)
        self.namespace = namespace
        if is_production_path(self.store_root):
            raise WriterCapabilityError(
                f"Production cutover is forbidden in AR-1: cannot initialize SettlementStore at {self.store_root}"
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
            "schema_version": SCHEMA_SETTLEMENT_STATE,
            "subject_id": GLOBAL_SUBJECT_ID,
            "namespace": self.namespace,
            "revision": 0,
            "last_entry_hash": GENESIS_HASH,
            "updated_at": _iso(now()),
            "settlements_by_id": {},
            "latest_settlement_by_action": {},
            "settlement_history_by_action": {},
            "effect_claims_by_id": {},
            "conflicts_by_id": {},
            "ingested_evidences_by_action": {},
            "outbox_events": {},
            "pending_outbox_event_ids": [],
            "deliveries_by_id": {},
            "delivery_index": {},
        }

    def read_journal(self) -> list[dict[str, Any]]:
        self.flush_snapshot()
        if not self.journal_path.exists():
            return []
        raw = self.journal_path.read_text(encoding="utf-8")
        if raw and not raw.endswith("\n"):
            raise RecoveryRequiredError(
                "INCOMPLETE_SETTLEMENT_JOURNAL_TAIL",
                "result_settlement_journal.jsonl does not end with newline",
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
                    "CORRUPTED_SETTLEMENT_JOURNAL_JSON",
                    f"invalid JSON at line {idx}: {exc}",
                ) from exc
            if not isinstance(item, dict) or item.get("schema_version") != SCHEMA_SETTLEMENT_JOURNAL_ENTRY:
                raise RecoveryRequiredError(
                    "INVALID_SETTLEMENT_JOURNAL_SCHEMA",
                    f"invalid schema at line {idx}",
                )
            if item.get("store_revision") != expected_rev:
                raise RecoveryRequiredError(
                    "SETTLEMENT_JOURNAL_REVISION_GAP",
                    f"expected store_revision={expected_rev}, got {item.get('store_revision')}",
                )
            if item.get("prev_entry_hash") != prev_hash:
                raise RecoveryRequiredError(
                    "SETTLEMENT_JOURNAL_HASH_CHAIN_BROKEN",
                    f"broken hash chain at line {idx}",
                )
            recorded_hash = item.get("entry_hash")
            unsigned = dict(item)
            unsigned.pop("entry_hash", None)
            computed = sha256_hex(canonical_json_line(unsigned))
            if recorded_hash != computed:
                raise RecoveryRequiredError(
                    "SETTLEMENT_JOURNAL_ENTRY_HASH_MISMATCH",
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
                "CORRUPTED_SETTLEMENT_WAL",
                f"unreadable WAL {self.wal_path}: {exc}",
            ) from exc
        if not isinstance(wal, dict) or wal.get("schema_version") != SCHEMA_SETTLEMENT_WAL:
            raise RecoveryRequiredError("INVALID_SETTLEMENT_WAL_SCHEMA", "invalid WAL schema")

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
                    "SETTLEMENT_WAL_JOURNAL_DIVERGENCE",
                    "WAL journal entry does not match tail of result_settlement_journal.jsonl",
                )
        else:
            raise RecoveryRequiredError(
                "SETTLEMENT_WAL_REVISION_MISMATCH",
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
            if _mutable and len(cached.get("settlements_by_id", {})) > 25:
                return cached
            return copy.deepcopy(cached)

        entries = self.read_journal()
        if not self.state_path.exists():
            if entries:
                raise RecoveryRequiredError(
                    "MISSING_SETTLEMENT_STATE_SNAPSHOT",
                    "journal has entries but result_settlement_state.json is missing",
                )
            return self._empty_state()
        try:
            state = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise RecoveryRequiredError(
                "CORRUPTED_SETTLEMENT_STATE_JSON",
                f"invalid JSON in result_settlement_state.json: {exc}",
            ) from exc
        if not isinstance(state, dict) or state.get("schema_version") != SCHEMA_SETTLEMENT_STATE:
            raise RecoveryRequiredError("INVALID_SETTLEMENT_STATE_SCHEMA", "invalid state schema")
        validate_subject_id(state.get("subject_id"))
        expected_rev = len(entries)
        expected_hash = entries[-1]["entry_hash"] if entries else GENESIS_HASH
        if state.get("revision") != expected_rev or state.get("last_entry_hash") != expected_hash:
            raise RecoveryRequiredError(
                "SETTLEMENT_STATE_JOURNAL_DIVERGENCE",
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
        verify_settlement_writer_capability(lease, capability)
        if is_production_path(self.store_root):
            raise WriterCapabilityError("Production cutover is forbidden in AR-1")
        self.store_root.mkdir(parents=True, exist_ok=True)

        current = self.load_state(_mutable=True)
        next_rev = int(current["revision"]) + 1
        prev_hash = str(current["last_entry_hash"])
        recorded_iso = _iso(now())

        entry_unsigned = {
            "schema_version": SCHEMA_SETTLEMENT_JOURNAL_ENTRY,
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
        committed_state["schema_version"] = SCHEMA_SETTLEMENT_STATE
        committed_state["subject_id"] = GLOBAL_SUBJECT_ID
        committed_state["namespace"] = self.namespace
        committed_state["revision"] = next_rev
        committed_state["last_entry_hash"] = entry_hash
        committed_state["updated_at"] = recorded_iso

        is_bulk = len(committed_state.get("settlements_by_id", {})) > 25 and fault_hook is None
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
                "schema_version": SCHEMA_SETTLEMENT_WAL,
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
# SettlementPolicyRegistry (Sections 36-48, 0 LLM Deterministic Evaluation)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ActionKindSettlementPolicy:
    """Versioned domain-specific settlement rules for a given action_kind."""

    action_kind: str
    policy_version: str
    primary_authority_domain: str
    required_grade_for_full_success: str
    supported_predicates: tuple[str, ...]
    allows_partial_effect: bool = True


class SettlementPolicyRegistry:
    """Section 37 & 38: Deterministic 0-LLM Settlement Policy Registry per Action Kind."""

    def __init__(self, policy_version: str = SETTLEMENT_POLICY_VERSION_V1):
        self.policy_version = policy_version
        self._policies: dict[str, ActionKindSettlementPolicy] = {
            ACTION_KIND_MESSAGE: ActionKindSettlementPolicy(
                action_kind=ACTION_KIND_MESSAGE,
                policy_version=policy_version,
                primary_authority_domain="message_provider",
                required_grade_for_full_success=AUTH_GRADE_PROVIDER_REPORTED,
                supported_predicates=(
                    "message_provider_accepted",
                    "message_delivered",
                    "message_read_confirmed",
                ),
            ),
            ACTION_KIND_WORLD: ActionKindSettlementPolicy(
                action_kind=ACTION_KIND_WORLD,
                policy_version=policy_version,
                primary_authority_domain="world_authority",
                required_grade_for_full_success=AUTH_GRADE_DOMAIN_FACT,
                supported_predicates=(
                    "world_state_mutated",
                    "world_partial_state_mutated",
                ),
            ),
            ACTION_KIND_FILE: ActionKindSettlementPolicy(
                action_kind=ACTION_KIND_FILE,
                policy_version=policy_version,
                primary_authority_domain="artifact_authority",
                required_grade_for_full_success=AUTH_GRADE_OBSERVER_VERIFIED,
                supported_predicates=(
                    "artifact_written_verified",
                    "artifact_deleted_verified",
                ),
            ),
            ACTION_KIND_TOOL: ActionKindSettlementPolicy(
                action_kind=ACTION_KIND_TOOL,
                policy_version=policy_version,
                primary_authority_domain="artifact_authority",
                required_grade_for_full_success=AUTH_GRADE_OBSERVER_VERIFIED,
                supported_predicates=(
                    "tool_process_succeeded",
                    "business_artifact_verified",
                ),
            ),
        }

    def get_policy(self, action_kind: str) -> ActionKindSettlementPolicy:
        if action_kind not in self._policies:
            raise ContractViolationError(f"no settlement policy for action_kind={action_kind!r}")
        return self._policies[action_kind]

    def evaluate(
        self,
        *,
        action: Mapping[str, Any],
        attempts: Sequence[Mapping[str, Any]],
        evidences: Sequence[SettlementEvidenceInput],
        settlement_id: str,
        recorded_at: str,
    ) -> dict[str, Any]:
        """Deterministically evaluate action state + deduplicated evidences into Settlement outcome (0 LLM)."""
        action_id = str(action["action_id"])
        action_kind = str(action["action_kind"])
        action_status = str(action["status"])

        # Sort evidences deterministically by (occurred_at, observed_at, evidence_id) (Section 66)
        sorted_evs = sorted(
            evidences,
            key=lambda e: (e.occurred_at, e.observed_at, e.evidence_id),
        )

        # Deduplicate by root_evidence_id so repeated receipts/reconcile/observer/replay
        # never inflate support (Section 33, 73, C05)
        dedup_evs: list[SettlementEvidenceInput] = []
        seen_root_ids: set[str] = set()
        for ev in sorted_evs:
            if ev.root_evidence_id in seen_root_ids:
                continue
            seen_root_ids.add(ev.root_evidence_id)
            dedup_evs.append(ev)

        effect_claims: list[EffectClaim] = []
        conflicts: list[SettlementConflict] = []
        confirmed_effect_refs: list[str] = []
        missing_effect_refs: list[str] = []
        failed_effect_refs: list[str] = []
        unresolved_claims: list[str] = []
        authoritative_result_refs: list[str] = []

        # Helper to mint EffectClaim
        def _add_claim(
            *,
            domain: str,
            predicate: str,
            ev_list: Sequence[SettlementEvidenceInput],
            auth_refs: Sequence[str],
            auth_domain: str,
            auth_grade: str,
            epistemic: str,
            subject_ref: Optional[str] = GLOBAL_SUBJECT_ID,
            object_ref: Optional[str] = None,
            details: Optional[dict[str, Any]] = None,
        ) -> EffectClaim:
            ev_refs = [e.evidence_id for e in ev_list]
            first_occ = min(e.occurred_at for e in ev_list)
            last_obs = max(e.observed_at for e in ev_list)
            cid = f"eclm:{sha256_hex(f'{settlement_id}:{domain}:{predicate}:{object_ref or action_id}')[:16]}"
            claim = EffectClaim(
                claim_id=cid,
                settlement_id=settlement_id,
                action_id=action_id,
                domain=domain,
                predicate=predicate,
                subject_ref=subject_ref,
                object_ref=object_ref or str(action.get("target_ref") or action_id),
                evidence_refs=ev_refs,
                authority_refs=list(auth_refs),
                authority_domain=auth_domain,
                authority_grade=auth_grade,
                epistemic_status=epistemic,
                occurred_at=first_occ,
                observed_at=last_obs,
                recorded_at=recorded_at,
                details=details or {},
            )
            effect_claims.append(claim)
            if epistemic == EPISTEMIC_CONFIRMED:
                confirmed_effect_refs.append(cid)
            return claim

        # 1. Handle Action CANCELLED (Section 64)
        if action_status == ACTION_STATUS_CANCELLED:
            verified_effects = [
                e
                for e in dedup_evs
                if e.verified
                and e.authority_grade in {AUTH_GRADE_DOMAIN_FACT, AUTH_GRADE_OBSERVER_VERIFIED}
            ]
            if verified_effects:
                for ve in verified_effects:
                    dom = "world" if ve.authority_grade == AUTH_GRADE_DOMAIN_FACT else "artifact"
                    pred = "world_state_mutated" if dom == "world" else "artifact_written_verified"
                    _add_claim(
                        domain=dom,
                        predicate=pred,
                        ev_list=[ve],
                        auth_refs=[ve.domain_fact_ref or ve.evidence_id],
                        auth_domain=ve.authority_domain,
                        auth_grade=ve.authority_grade,
                        epistemic=EPISTEMIC_CONFIRMED,
                        object_ref=ve.target_ref,
                    )
                return self._pack_eval(
                    settlement_status=SETTLEMENT_STATUS_PARTIAL,
                    outcome_kind=OUTCOME_PARTIAL_EFFECT,
                    dedup_evs=dedup_evs,
                    effect_claims=effect_claims,
                    conflicts=conflicts,
                    confirmed_effect_refs=confirmed_effect_refs,
                    missing_effect_refs=["cancel:post_effect_cancellation"],
                    failed_effect_refs=failed_effect_refs,
                    unresolved_claims=[],
                    authoritative_result_refs=authoritative_result_refs,
                )
            return self._pack_eval(
                settlement_status=SETTLEMENT_STATUS_SETTLED,
                outcome_kind=OUTCOME_CANCELLED_NO_EFFECT,
                dedup_evs=dedup_evs,
                effect_claims=effect_claims,
                conflicts=conflicts,
                confirmed_effect_refs=[],
                missing_effect_refs=[],
                failed_effect_refs=[],
                unresolved_claims=[],
                authoritative_result_refs=[],
            )

        # 2. Handle Action UNKNOWN (Section 11, 28, 70, B03)
        # Action UNKNOWN can never be turned into SETTLED SUCCESS by AR-1 without authoritative resolution
        if action_status == ACTION_STATUS_UNKNOWN:
            # Even if partial evidence exists, record it as PARTIAL epistemic claim or unresolved,
            # never SETTLED SUCCESS_EFFECT
            for ev in dedup_evs:
                if ev.verified and ev.authority_grade in {AUTH_GRADE_DOMAIN_FACT, AUTH_GRADE_OBSERVER_VERIFIED}:
                    authoritative_result_refs.append(ev.domain_fact_ref or ev.evidence_id)
            return self._pack_eval(
                settlement_status=SETTLEMENT_STATUS_UNRESOLVED,
                outcome_kind=OUTCOME_UNKNOWN_EFFECT,
                dedup_evs=dedup_evs,
                effect_claims=effect_claims,
                conflicts=conflicts,
                confirmed_effect_refs=confirmed_effect_refs,
                missing_effect_refs=missing_effect_refs,
                failed_effect_refs=failed_effect_refs,
                unresolved_claims=["action_status_unknown"],
                authoritative_result_refs=authoritative_result_refs,
            )

        # 3. Domain-Specific Evaluation for MESSAGE_ACTION (Sections 39-41, 87)
        if action_kind == ACTION_KIND_MESSAGE:
            provider_evs = [
                e for e in dedup_evs if e.authority_grade == AUTH_GRADE_PROVIDER_REPORTED
            ]
            rejected_evs = [
                e for e in provider_evs if e.claim_kind in {"receipt:REJECTED", "receipt:FAILED"}
            ]
            accepted_evs = [
                e
                for e in provider_evs
                if e.claim_kind in {"receipt:ACCEPTED", "receipt:DELIVERED", "receipt:READ"}
            ]
            delivered_evs = [
                e for e in provider_evs if e.claim_kind in {"receipt:DELIVERED", "receipt:READ"}
            ]
            read_evs = [e for e in provider_evs if e.claim_kind == "receipt:READ"]

            if rejected_evs and not accepted_evs:
                failed_effect_refs.extend(e.evidence_id for e in rejected_evs)
                return self._pack_eval(
                    settlement_status=SETTLEMENT_STATUS_SETTLED,
                    outcome_kind=OUTCOME_FAILURE_NO_EFFECT,
                    dedup_evs=dedup_evs,
                    effect_claims=effect_claims,
                    conflicts=conflicts,
                    confirmed_effect_refs=[],
                    missing_effect_refs=[],
                    failed_effect_refs=failed_effect_refs,
                    unresolved_claims=[],
                    authoritative_result_refs=[e.evidence_id for e in rejected_evs],
                )

            if accepted_evs:
                _add_claim(
                    domain="message",
                    predicate="message_provider_accepted",
                    ev_list=accepted_evs,
                    auth_refs=[e.evidence_id for e in accepted_evs],
                    auth_domain="message_provider",
                    auth_grade=AUTH_GRADE_PROVIDER_REPORTED,
                    epistemic=EPISTEMIC_CONFIRMED,
                )
                authoritative_result_refs.extend(e.evidence_id for e in accepted_evs)

            if delivered_evs:
                _add_claim(
                    domain="message",
                    predicate="message_delivered",
                    ev_list=delivered_evs,
                    auth_refs=[e.evidence_id for e in delivered_evs],
                    auth_domain="message_provider",
                    auth_grade=AUTH_GRADE_PROVIDER_REPORTED,
                    epistemic=EPISTEMIC_CONFIRMED,
                )

            if read_evs:
                _add_claim(
                    domain="message",
                    predicate="message_read_confirmed",
                    ev_list=read_evs,
                    auth_refs=[e.evidence_id for e in read_evs],
                    auth_domain="message_provider",
                    auth_grade=AUTH_GRADE_PROVIDER_REPORTED,
                    epistemic=EPISTEMIC_CONFIRMED,
                )
            else:
                unresolved_claims.append("message_read_unknown")

            if accepted_evs:
                return self._pack_eval(
                    settlement_status=SETTLEMENT_STATUS_SETTLED,
                    outcome_kind=OUTCOME_SUCCESS_EFFECT,
                    dedup_evs=dedup_evs,
                    effect_claims=effect_claims,
                    conflicts=conflicts,
                    confirmed_effect_refs=confirmed_effect_refs,
                    missing_effect_refs=missing_effect_refs,
                    failed_effect_refs=failed_effect_refs,
                    unresolved_claims=unresolved_claims,
                    authoritative_result_refs=authoritative_result_refs,
                )

            # Only INFERRED or transport evidence -> remains OPEN
            unresolved_claims.append("message_provider_receipt_missing")
            return self._pack_eval(
                settlement_status=SETTLEMENT_STATUS_OPEN,
                outcome_kind=OUTCOME_UNKNOWN_EFFECT,
                dedup_evs=dedup_evs,
                effect_claims=effect_claims,
                conflicts=conflicts,
                confirmed_effect_refs=[],
                missing_effect_refs=[],
                failed_effect_refs=[],
                unresolved_claims=unresolved_claims,
                authoritative_result_refs=[],
            )

        # 4. Domain-Specific Evaluation for WORLD_ACTION (Sections 42-44, 86)
        if action_kind == ACTION_KIND_WORLD:
            world_fact_evs = [
                e for e in dedup_evs if e.authority_grade == AUTH_GRADE_DOMAIN_FACT
            ]
            exec_success_evs = [
                e
                for e in dedup_evs
                if e.authority_grade in {AUTH_GRADE_EXECUTOR_RECEIPT, AUTH_GRADE_PROVIDER_REPORTED, AUTH_GRADE_TRANSPORT_RECEIPT}
                and e.verified
            ]
            world_rejected = [
                e
                for e in world_fact_evs
                if e.claim_kind in {"receipt:REJECTED", "receipt:FAILED"}
                and not e.details.get("partial_effects")
            ]
            world_absent_conflict = [
                e
                for e in world_fact_evs
                if not e.verified and e.details.get("world_mutation_absent") is True
            ]
            world_partial = [
                e for e in world_fact_evs if e.details.get("partial_effects")
            ]
            world_succeeded = [
                e
                for e in world_fact_evs
                if e.verified and not e.details.get("partial_effects")
            ]

            # Conflict: provider/executor claims success, but World Authority says mutation absent (Section 34, G05, K02)
            if world_absent_conflict and (exec_success_evs or action_status == ACTION_STATUS_SUCCEEDED):
                # If a later higher-revision authoritative world fact explicitly supersedes the conflict, check timestamps
                latest_absent_ts = max(e.observed_at for e in world_absent_conflict)
                later_authoritative = [
                    e
                    for e in world_succeeded
                    if e.observed_at > latest_absent_ts or e.details.get("supersedes_conflict")
                ]
                if not later_authoritative:
                    c_id = f"sconf:{sha256_hex(f'{settlement_id}:world_conflict')[:16]}"
                    ev_ids = [e.evidence_id for e in (exec_success_evs + world_absent_conflict)]
                    conflicts.append(
                        SettlementConflict(
                            conflict_id=c_id,
                            settlement_id=settlement_id,
                            action_id=action_id,
                            claim_refs=[],
                            evidence_refs=ev_ids,
                            authority_domains=["executor_or_provider", "world_authority"],
                            reason_code="PROVIDER_SUCCESS_VS_WORLD_MUTATION_ABSENT",
                            detected_at=recorded_at,
                        )
                    )
                    return self._pack_eval(
                        settlement_status=SETTLEMENT_STATUS_CONFLICT,
                        outcome_kind=OUTCOME_UNKNOWN_EFFECT,
                        dedup_evs=dedup_evs,
                        effect_claims=effect_claims,
                        conflicts=conflicts,
                        confirmed_effect_refs=[],
                        missing_effect_refs=["world:expected_mutation_absent"],
                        failed_effect_refs=[],
                        unresolved_claims=["world_conflict_unresolved"],
                        authoritative_result_refs=[
                            e.domain_fact_ref or e.evidence_id for e in world_absent_conflict
                        ],
                    )

            # Partial World Effect (Section 44, G04)
            if world_partial:
                pe = world_partial[-1]
                applied = list(pe.details.get("confirmed_effects", ["world_fact:partial_applied"]))
                missing = list(pe.details.get("missing_effects", ["world_fact:remaining_unapplied"]))
                failed = list(pe.details.get("failed_effects", ["world_fact:step_failed"]))
                claim = _add_claim(
                    domain="world",
                    predicate="world_partial_state_mutated",
                    ev_list=[pe],
                    auth_refs=[pe.domain_fact_ref or pe.evidence_id],
                    auth_domain="world_authority",
                    auth_grade=AUTH_GRADE_DOMAIN_FACT,
                    epistemic=EPISTEMIC_CONFIRMED,
                    details={"confirmed_effects": applied, "failed_effects": failed},
                )
                return self._pack_eval(
                    settlement_status=SETTLEMENT_STATUS_PARTIAL,
                    outcome_kind=OUTCOME_PARTIAL_EFFECT,
                    dedup_evs=dedup_evs,
                    effect_claims=effect_claims,
                    conflicts=conflicts,
                    confirmed_effect_refs=[claim.claim_id, *applied],
                    missing_effect_refs=missing,
                    failed_effect_refs=failed,
                    unresolved_claims=[],
                    authoritative_result_refs=[pe.domain_fact_ref or pe.evidence_id],
                )

            # World Rejection with confirmed 0 mutation (Section 43)
            if world_rejected and not world_succeeded:
                return self._pack_eval(
                    settlement_status=SETTLEMENT_STATUS_SETTLED,
                    outcome_kind=OUTCOME_FAILURE_NO_EFFECT,
                    dedup_evs=dedup_evs,
                    effect_claims=effect_claims,
                    conflicts=conflicts,
                    confirmed_effect_refs=[],
                    missing_effect_refs=[],
                    failed_effect_refs=[e.evidence_id for e in world_rejected],
                    unresolved_claims=[],
                    authoritative_result_refs=[e.domain_fact_ref or e.evidence_id for e in world_rejected],
                )

            # Authoritative World Success (Section 42, G01, G02)
            if world_succeeded:
                we = world_succeeded[-1]
                fact_ref = we.domain_fact_ref or we.evidence_id
                _add_claim(
                    domain="world",
                    predicate="world_state_mutated",
                    ev_list=world_succeeded,
                    auth_refs=[fact_ref],
                    auth_domain="world_authority",
                    auth_grade=AUTH_GRADE_DOMAIN_FACT,
                    epistemic=EPISTEMIC_CONFIRMED,
                    details={"world_revision": we.details.get("world_revision")},
                )
                return self._pack_eval(
                    settlement_status=SETTLEMENT_STATUS_SETTLED,
                    outcome_kind=OUTCOME_SUCCESS_EFFECT,
                    dedup_evs=dedup_evs,
                    effect_claims=effect_claims,
                    conflicts=conflicts,
                    confirmed_effect_refs=confirmed_effect_refs,
                    missing_effect_refs=[],
                    failed_effect_refs=[],
                    unresolved_claims=[],
                    authoritative_result_refs=[fact_ref],
                )

            # Only executor/transport/inferred claim without World Authority -> OPEN (Section 8, 9, C03)
            unresolved_claims.append("world_authoritative_fact_missing")
            return self._pack_eval(
                settlement_status=SETTLEMENT_STATUS_OPEN,
                outcome_kind=OUTCOME_UNKNOWN_EFFECT,
                dedup_evs=dedup_evs,
                effect_claims=effect_claims,
                conflicts=conflicts,
                confirmed_effect_refs=[],
                missing_effect_refs=[],
                failed_effect_refs=[],
                unresolved_claims=unresolved_claims,
                authoritative_result_refs=[],
            )

        # 5. Domain-Specific Evaluation for FILE_ACTION (Sections 13, 45-46, 85)
        if action_kind == ACTION_KIND_FILE:
            obs_evs = [
                e for e in dedup_evs if e.authority_grade == AUTH_GRADE_OBSERVER_VERIFIED
            ]
            exec_evs = [
                e for e in dedup_evs if e.authority_grade == AUTH_GRADE_EXECUTOR_RECEIPT
            ]

            # Group observer evidences by target_ref (latest observation per target_ref wins only if authoritative supersession)
            obs_by_target: dict[str, list[SettlementEvidenceInput]] = {}
            for oe in obs_evs:
                t_key = oe.target_ref or action_id
                obs_by_target.setdefault(t_key, []).append(oe)

            verified_targets: list[SettlementEvidenceInput] = []
            mismatch_targets: list[SettlementEvidenceInput] = []
            missing_targets: list[SettlementEvidenceInput] = []

            for t_key, t_list in obs_by_target.items():
                latest_obs = t_list[-1]
                has_mismatch_history = any(
                    (not x.verified and x.observed_hash is not None and x.expected_hash is not None and x.observed_hash != x.expected_hash)
                    for x in t_list
                )
                if latest_obs.verified:
                    # Only allow superseding a prior mismatch if explicitly marked or newer observation
                    verified_targets.append(latest_obs)
                else:
                    if (
                        latest_obs.expected_hash is not None
                        and latest_obs.observed_hash is not None
                        and latest_obs.observed_hash != latest_obs.expected_hash
                    ) or latest_obs.details.get("hash_mismatch"):
                        mismatch_targets.append(latest_obs)
                    else:
                        missing_targets.append(latest_obs)

            # Check for hash mismatch conflict against executor success (Section 46, F04, K01, K04)
            if mismatch_targets:
                c_id = f"sconf:{sha256_hex(f'{settlement_id}:file_hash_mismatch')[:16]}"
                conflict_ev_ids = [e.evidence_id for e in (exec_evs + mismatch_targets)]
                conflicts.append(
                    SettlementConflict(
                        conflict_id=c_id,
                        settlement_id=settlement_id,
                        action_id=action_id,
                        claim_refs=[],
                        evidence_refs=conflict_ev_ids,
                        authority_domains=["sandbox_file_adapter", "artifact_authority"],
                        reason_code="EXECUTOR_VS_OBSERVER_HASH_MISMATCH",
                        detected_at=recorded_at,
                        details={
                            "mismatched_targets": [m.target_ref for m in mismatch_targets],
                        },
                    )
                )
                return self._pack_eval(
                    settlement_status=SETTLEMENT_STATUS_CONFLICT,
                    outcome_kind=OUTCOME_UNKNOWN_EFFECT,
                    dedup_evs=dedup_evs,
                    effect_claims=effect_claims,
                    conflicts=conflicts,
                    confirmed_effect_refs=confirmed_effect_refs,
                    missing_effect_refs=[m.target_ref or m.evidence_id for m in missing_targets],
                    failed_effect_refs=[m.target_ref or m.evidence_id for m in mismatch_targets],
                    unresolved_claims=["artifact_hash_mismatch_conflict"],
                    authoritative_result_refs=[m.evidence_id for m in mismatch_targets],
                )

            # Multi-file partial effect (Section 13: e.g., write 3 files -> 2 succeeded, 1 failed)
            expected_files = list((action.get("frozen_payload") or {}).get("batch_files", []))
            if verified_targets and (missing_targets or len(verified_targets) < len(expected_files)):
                for vt in verified_targets:
                    _add_claim(
                        domain="artifact",
                        predicate="artifact_written_verified",
                        ev_list=[vt],
                        auth_refs=[vt.evidence_id],
                        auth_domain="artifact_authority",
                        auth_grade=AUTH_GRADE_OBSERVER_VERIFIED,
                        epistemic=EPISTEMIC_CONFIRMED,
                        object_ref=vt.target_ref,
                        details={"observed_hash": vt.observed_hash},
                    )
                    authoritative_result_refs.append(vt.evidence_id)
                for mt in missing_targets:
                    missing_effect_refs.append(str(mt.target_ref or mt.evidence_id))
                    failed_effect_refs.append(str(mt.evidence_id))
                return self._pack_eval(
                    settlement_status=SETTLEMENT_STATUS_PARTIAL,
                    outcome_kind=OUTCOME_PARTIAL_EFFECT,
                    dedup_evs=dedup_evs,
                    effect_claims=effect_claims,
                    conflicts=conflicts,
                    confirmed_effect_refs=confirmed_effect_refs,
                    missing_effect_refs=missing_effect_refs,
                    failed_effect_refs=failed_effect_refs,
                    unresolved_claims=[],
                    authoritative_result_refs=authoritative_result_refs,
                )

            # Full verified file success (Section 45, F01-F03)
            if verified_targets and not missing_targets:
                for vt in verified_targets:
                    _add_claim(
                        domain="artifact",
                        predicate="artifact_written_verified",
                        ev_list=[vt],
                        auth_refs=[vt.evidence_id],
                        auth_domain="artifact_authority",
                        auth_grade=AUTH_GRADE_OBSERVER_VERIFIED,
                        epistemic=EPISTEMIC_CONFIRMED,
                        object_ref=vt.target_ref,
                        details={"observed_hash": vt.observed_hash},
                    )
                    authoritative_result_refs.append(vt.evidence_id)
                return self._pack_eval(
                    settlement_status=SETTLEMENT_STATUS_SETTLED,
                    outcome_kind=OUTCOME_SUCCESS_EFFECT,
                    dedup_evs=dedup_evs,
                    effect_claims=effect_claims,
                    conflicts=conflicts,
                    confirmed_effect_refs=confirmed_effect_refs,
                    missing_effect_refs=[],
                    failed_effect_refs=[],
                    unresolved_claims=[],
                    authoritative_result_refs=authoritative_result_refs,
                )

            # Definitive pre-submit failure (e.g. FileAlreadyExistsError before write) + observer confirms no effect
            if action_status == ACTION_STATUS_FAILED or (
                attempts and all(a.get("status") == ATTEMPT_STATUS_PRE_SUBMIT_FAILED for a in attempts)
            ):
                if missing_targets or (
                    attempts and all(a.get("status") == ATTEMPT_STATUS_PRE_SUBMIT_FAILED for a in attempts)
                ):
                    return self._pack_eval(
                        settlement_status=SETTLEMENT_STATUS_SETTLED,
                        outcome_kind=OUTCOME_FAILURE_NO_EFFECT,
                        dedup_evs=dedup_evs,
                        effect_claims=effect_claims,
                        conflicts=conflicts,
                        confirmed_effect_refs=[],
                        missing_effect_refs=[],
                        failed_effect_refs=[e.evidence_id for e in dedup_evs],
                        unresolved_claims=[],
                        authoritative_result_refs=[m.evidence_id for m in missing_targets],
                    )
                # Failed without proof of 0 side effect -> UNRESOLVED (Section 27, B02)
                return self._pack_eval(
                    settlement_status=SETTLEMENT_STATUS_UNRESOLVED,
                    outcome_kind=OUTCOME_UNKNOWN_EFFECT,
                    dedup_evs=dedup_evs,
                    effect_claims=effect_claims,
                    conflicts=conflicts,
                    confirmed_effect_refs=[],
                    missing_effect_refs=[],
                    failed_effect_refs=[],
                    unresolved_claims=["failed_action_side_effect_unverified"],
                    authoritative_result_refs=[],
                )

            # Executor receipt says SUCCEEDED, but no FileArtifactObserver evidence yet -> OPEN (Section 45, F05)
            unresolved_claims.append("artifact_observer_verification_pending")
            return self._pack_eval(
                settlement_status=SETTLEMENT_STATUS_OPEN,
                outcome_kind=OUTCOME_UNKNOWN_EFFECT,
                dedup_evs=dedup_evs,
                effect_claims=effect_claims,
                conflicts=conflicts,
                confirmed_effect_refs=[],
                missing_effect_refs=[],
                failed_effect_refs=[],
                unresolved_claims=unresolved_claims,
                authoritative_result_refs=[],
            )

        # 6. Domain-Specific Evaluation for TOOL_ACTION (Sections 26, 47-48, 88)
        if action_kind == ACTION_KIND_TOOL:
            proc_evs = [
                e
                for e in dedup_evs
                if e.authority_grade == AUTH_GRADE_EXECUTOR_RECEIPT
                and e.claim_kind in {"receipt:PROCESS_EXIT_0", "receipt:SUCCEEDED"}
            ]
            proc_fail_evs = [
                e
                for e in dedup_evs
                if e.authority_grade == AUTH_GRADE_EXECUTOR_RECEIPT
                and e.claim_kind in {"receipt:FAILED", "receipt:REJECTED"}
            ]
            obs_evs = [
                e for e in dedup_evs if e.authority_grade == AUTH_GRADE_OBSERVER_VERIFIED
            ]
            inferred_stdout_evs = [
                e for e in dedup_evs if e.authority_grade == AUTH_GRADE_INFERRED
            ]

            if proc_evs:
                _add_claim(
                    domain="tool",
                    predicate="tool_process_succeeded",
                    ev_list=proc_evs,
                    auth_refs=[e.evidence_id for e in proc_evs],
                    auth_domain="tool_executor",
                    auth_grade=AUTH_GRADE_EXECUTOR_RECEIPT,
                    epistemic=EPISTEMIC_CONFIRMED,
                    details={"exit_code": 0},
                )

            # If observer explicitly found artifact missing/corrupted while tool/stdout claimed success -> CONFLICT (I04)
            obs_failed = [e for e in obs_evs if not e.verified]
            obs_verified = [e for e in obs_evs if e.verified]

            if obs_failed and (proc_evs or inferred_stdout_evs):
                c_id = f"sconf:{sha256_hex(f'{settlement_id}:tool_vs_observer')[:16]}"
                conflicts.append(
                    SettlementConflict(
                        conflict_id=c_id,
                        settlement_id=settlement_id,
                        action_id=action_id,
                        claim_refs=[c.claim_id for c in effect_claims],
                        evidence_refs=[e.evidence_id for e in (proc_evs + inferred_stdout_evs + obs_failed)],
                        authority_domains=["tool_executor", "artifact_authority"],
                        reason_code="TOOL_STDOUT_OR_EXIT0_VS_OBSERVER_MISMATCH",
                        detected_at=recorded_at,
                    )
                )
                return self._pack_eval(
                    settlement_status=SETTLEMENT_STATUS_CONFLICT,
                    outcome_kind=OUTCOME_PARTIAL_EFFECT if proc_evs else OUTCOME_UNKNOWN_EFFECT,
                    dedup_evs=dedup_evs,
                    effect_claims=effect_claims,
                    conflicts=conflicts,
                    confirmed_effect_refs=confirmed_effect_refs,
                    missing_effect_refs=[e.target_ref or e.evidence_id for e in obs_failed],
                    failed_effect_refs=[e.evidence_id for e in obs_failed],
                    unresolved_claims=["business_artifact_conflict"],
                    authoritative_result_refs=[e.evidence_id for e in obs_failed],
                )

            if obs_verified:
                for ov in obs_verified:
                    _add_claim(
                        domain="artifact",
                        predicate="business_artifact_verified",
                        ev_list=[ov],
                        auth_refs=[ov.evidence_id],
                        auth_domain="artifact_authority",
                        auth_grade=AUTH_GRADE_OBSERVER_VERIFIED,
                        epistemic=EPISTEMIC_CONFIRMED,
                        object_ref=ov.target_ref,
                        details={"observed_hash": ov.observed_hash},
                    )
                    authoritative_result_refs.append(ov.evidence_id)
                return self._pack_eval(
                    settlement_status=SETTLEMENT_STATUS_SETTLED,
                    outcome_kind=OUTCOME_SUCCESS_EFFECT,
                    dedup_evs=dedup_evs,
                    effect_claims=effect_claims,
                    conflicts=conflicts,
                    confirmed_effect_refs=confirmed_effect_refs,
                    missing_effect_refs=[],
                    failed_effect_refs=[],
                    unresolved_claims=[],
                    authoritative_result_refs=authoritative_result_refs,
                )

            if proc_fail_evs:
                # Tool failed: only FAILURE_NO_EFFECT if explicitly confirmed no side effects
                if all(e.details.get("confirmed_no_side_effect") is True for e in proc_fail_evs):
                    return self._pack_eval(
                        settlement_status=SETTLEMENT_STATUS_SETTLED,
                        outcome_kind=OUTCOME_FAILURE_NO_EFFECT,
                        dedup_evs=dedup_evs,
                        effect_claims=effect_claims,
                        conflicts=conflicts,
                        confirmed_effect_refs=[],
                        missing_effect_refs=[],
                        failed_effect_refs=[e.evidence_id for e in proc_fail_evs],
                        unresolved_claims=[],
                        authoritative_result_refs=[e.evidence_id for e in proc_fail_evs],
                    )
                return self._pack_eval(
                    settlement_status=SETTLEMENT_STATUS_UNRESOLVED,
                    outcome_kind=OUTCOME_UNKNOWN_EFFECT,
                    dedup_evs=dedup_evs,
                    effect_claims=effect_claims,
                    conflicts=conflicts,
                    confirmed_effect_refs=confirmed_effect_refs,
                    missing_effect_refs=[],
                    failed_effect_refs=[e.evidence_id for e in proc_fail_evs],
                    unresolved_claims=["tool_failure_side_effects_unverified"],
                    authoritative_result_refs=[],
                )

            # Process exit 0 (or inferred stdout) without external business artifact verification -> OPEN (Section 26, 47, B01, I01, I02)
            unresolved_claims.append("business_outcome_verified")
            return self._pack_eval(
                settlement_status=SETTLEMENT_STATUS_OPEN,
                outcome_kind=OUTCOME_UNKNOWN_EFFECT,
                dedup_evs=dedup_evs,
                effect_claims=effect_claims,
                conflicts=conflicts,
                confirmed_effect_refs=confirmed_effect_refs,
                missing_effect_refs=[],
                failed_effect_refs=[],
                unresolved_claims=unresolved_claims,
                authoritative_result_refs=[],
            )

        raise ContractViolationError(f"unsupported action_kind={action_kind!r}")

    @staticmethod
    def _pack_eval(
        *,
        settlement_status: str,
        outcome_kind: str,
        dedup_evs: Sequence[SettlementEvidenceInput],
        effect_claims: Sequence[EffectClaim],
        conflicts: Sequence[SettlementConflict],
        confirmed_effect_refs: Sequence[str],
        missing_effect_refs: Sequence[str],
        failed_effect_refs: Sequence[str],
        unresolved_claims: Sequence[str],
        authoritative_result_refs: Sequence[str],
    ) -> dict[str, Any]:
        receipt_refs = [e.evidence_id for e in dedup_evs if e.evidence_id.startswith("arcpt:")]
        evidence_refs = [e.evidence_id for e in dedup_evs]
        root_evidence_ids = [e.root_evidence_id for e in dedup_evs]
        occurred_at = min((e.occurred_at for e in dedup_evs), default=None)
        observed_at = max((e.observed_at for e in dedup_evs), default=_iso(now()))
        return {
            "settlement_status": settlement_status,
            "outcome_kind": outcome_kind,
            "receipt_refs": receipt_refs,
            "evidence_refs": evidence_refs,
            "root_evidence_ids": root_evidence_ids,
            "authoritative_result_refs": list(dict.fromkeys(authoritative_result_refs)),
            "effect_claims": [c.to_dict() for c in effect_claims],
            "effect_claim_refs": [c.claim_id for c in effect_claims],
            "conflicts": [c.to_dict() for c in conflicts],
            "conflict_refs": [c.conflict_id for c in conflicts],
            "confirmed_effect_refs": list(dict.fromkeys(confirmed_effect_refs)),
            "missing_effect_refs": list(dict.fromkeys(missing_effect_refs)),
            "failed_effect_refs": list(dict.fromkeys(failed_effect_refs)),
            "unresolved_claims": list(dict.fromkeys(unresolved_claims)),
            "effect_occurred_at": occurred_at,
            "effect_observed_at": observed_at,
        }


# ---------------------------------------------------------------------------
# Consumer Contracts & Sandbox Fake Consumers (Sections 21-22, 49-58, 98-102)
# ---------------------------------------------------------------------------


@dataclass
class ConsumerDispositionResult:
    """Result returned by a domain consumer upon receiving a SettledResultEvent."""

    consumer_domain: str
    event_id: str
    disposition: str
    idempotent_replay: bool = False
    downstream_proposal_ref: Optional[str] = None
    reason: Optional[str] = None

    def __post_init__(self) -> None:
        if self.consumer_domain not in VALID_CONSUMER_DOMAINS:
            raise ContractViolationError(f"invalid consumer_domain={self.consumer_domain!r}")
        if self.disposition not in VALID_CONSUMER_DISPOSITIONS:
            raise ContractViolationError(f"invalid disposition={self.disposition!r}")


class ResultConsumerProtocol:
    """Base protocol for independent domain consumers of SettledResultEvent."""

    consumer_domain: str
    domain_write_count: int = 0

    def receive_settled_event(self, event: Mapping[str, Any]) -> ConsumerDispositionResult:
        raise NotImplementedError


class BaseSandboxDomainConsumer(ResultConsumerProtocol):
    """Sandbox consumer supporting ACCEPT/IGNORE/DEFER/REJECT/CONFLICT, corrections, and idempotency with 0 domain writes."""

    def __init__(
        self,
        consumer_domain: str,
        *,
        default_disposition: str = DISPOSITION_ACCEPT,
    ):
        if consumer_domain not in VALID_CONSUMER_DOMAINS:
            raise ContractViolationError(f"invalid consumer_domain={consumer_domain!r}")
        self.consumer_domain = consumer_domain
        self.default_disposition = default_disposition
        self.domain_write_count = 0
        self.received_events_by_id: dict[str, dict[str, Any]] = {}
        self.dispositions_by_event_id: dict[str, ConsumerDispositionResult] = {}
        self.latest_settlement_by_action: dict[str, str] = {}
        self.correction_history_by_action: dict[str, list[str]] = {}
        self.disposition_policy_fn: Optional[Callable[[Mapping[str, Any]], str]] = None
        self.simulate_down: bool = False
        self.simulate_slow_defer: bool = False

    def attempt_forbidden_cross_domain_write(self, target_domain: str) -> None:
        """Section 84 E04: Consumers are strictly forbidden from mutating any canonical domain."""
        raise ConsumerIsolationError(
            f"Consumer {self.consumer_domain!r} is forbidden from mutating domain {target_domain!r}"
        )

    def receive_settled_event(self, event: Mapping[str, Any]) -> ConsumerDispositionResult:
        if self.simulate_down:
            raise RuntimeError(f"CONSUMER_DOWN:{self.consumer_domain}")

        event_id = str(event["event_id"])
        # Section 54 & 69: At-least-once consumer idempotency by event_id
        if event_id in self.dispositions_by_event_id:
            prev = self.dispositions_by_event_id[event_id]
            if prev.disposition != DISPOSITION_DEFER or self.simulate_slow_defer:
                return ConsumerDispositionResult(
                    consumer_domain=self.consumer_domain,
                    event_id=event_id,
                    disposition=prev.disposition,
                    idempotent_replay=True,
                    downstream_proposal_ref=prev.downstream_proposal_ref,
                    reason=prev.reason,
                )

        if self.simulate_slow_defer:
            disp = DISPOSITION_DEFER
        elif self.disposition_policy_fn is not None:
            disp = self.disposition_policy_fn(event)
        else:
            disp = self.default_disposition

        act_id = str(event["action_id"])
        self.received_events_by_id[event_id] = copy.deepcopy(dict(event))
        if disp == DISPOSITION_ACCEPT:
            self.latest_settlement_by_action[act_id] = str(event["settlement_ref"])
            if (
                event.get("event_kind") == EVENT_KIND_CORRECTION
                or event.get("supersedes_settlement_ref") is not None
            ):
                self.correction_history_by_action.setdefault(act_id, []).append(event_id)

        # Section 58: Downstream impact is at most a read-only proposal ref, never a domain mutation!
        prop_ref = (
            f"proposal:{self.consumer_domain}_eval:{sha256_hex(f'{self.consumer_domain}:{event_id}')[:12]}"
            if disp == DISPOSITION_ACCEPT
            else None
        )
        res = ConsumerDispositionResult(
            consumer_domain=self.consumer_domain,
            event_id=event_id,
            disposition=disp,
            idempotent_replay=False,
            downstream_proposal_ref=prop_ref,
            reason=f"{self.consumer_domain}_handled_{disp.lower()}",
        )
        self.dispositions_by_event_id[event_id] = res
        return res


class FakeLifeConsumer(BaseSandboxDomainConsumer):
    """Section 49, 102, 103: Reads action_id, activity_ref, effect_claims, outcome; NEVER mutates Activity."""

    def __init__(self, *, default_disposition: str = DISPOSITION_ACCEPT):
        super().__init__(CONSUMER_DOMAIN_LIFE, default_disposition=default_disposition)


class FakeMemoryConsumer(BaseSandboxDomainConsumer):
    """Section 50, 100: Receives SettledResultEvent; NEVER writes Native Memory or forms Experience in AR-1."""

    def __init__(self, *, default_disposition: str = DISPOSITION_ACCEPT):
        super().__init__(CONSUMER_DOMAIN_MEMORY, default_disposition=default_disposition)


class FakeGoalConsumer(BaseSandboxDomainConsumer):
    """Section 51, 101: Receives SettledResultEvent; NEVER marks Goal completed."""

    def __init__(self, *, default_disposition: str = DISPOSITION_ACCEPT):
        super().__init__(CONSUMER_DOMAIN_GOAL, default_disposition=default_disposition)


class FakeWorldConsumer(BaseSandboxDomainConsumer):
    """Section 52: Receives cross-domain consequence notification; NEVER overwrites World authority."""

    def __init__(self, *, default_disposition: str = DISPOSITION_ACCEPT):
        super().__init__(CONSUMER_DOMAIN_WORLD, default_disposition=default_disposition)


# ---------------------------------------------------------------------------
# SettlementReadService (Sections 74-75)
# ---------------------------------------------------------------------------


class SettlementReadService:
    """Read-only query and provenance lineage service for AR-1 Result Settlement."""

    def __init__(
        self,
        *,
        settlement_store: SettlementStore,
        action_store: Optional[ActionStore] = None,
    ):
        self.settlement_store = settlement_store
        self.action_store = action_store
        self.action_read = ActionReadService(action_store) if action_store is not None else None

    def get_settlement(self, action_id: str) -> Optional[dict[str, Any]]:
        state = self.settlement_store.load_state(_mutable=True)
        sid = state["latest_settlement_by_action"].get(action_id)
        if not sid:
            return None
        rec = state["settlements_by_id"].get(sid)
        return copy.deepcopy(rec) if rec else None

    def get_settlement_by_id(self, settlement_id: str) -> Optional[dict[str, Any]]:
        state = self.settlement_store.load_state(_mutable=True)
        rec = state["settlements_by_id"].get(settlement_id)
        return copy.deepcopy(rec) if rec else None

    def get_settlement_history(self, action_id: str) -> list[dict[str, Any]]:
        state = self.settlement_store.load_state(_mutable=True)
        sids = state["settlement_history_by_action"].get(action_id, [])
        return [
            copy.deepcopy(state["settlements_by_id"][sid])
            for sid in sids
            if sid in state["settlements_by_id"]
        ]

    def get_effect_claims(self, settlement_id: str) -> list[dict[str, Any]]:
        state = self.settlement_store.load_state(_mutable=True)
        rec = state["settlements_by_id"].get(settlement_id)
        if not rec:
            return []
        return copy.deepcopy(rec.get("effect_claims", []))

    def get_outbox_event(self, event_id: str) -> Optional[dict[str, Any]]:
        state = self.settlement_store.load_state(_mutable=True)
        ev = state["outbox_events"].get(event_id)
        return copy.deepcopy(ev) if ev else None

    def list_outbox_events(self, *, action_id: Optional[str] = None) -> list[dict[str, Any]]:
        state = self.settlement_store.load_state(_mutable=True)
        items = list(state["outbox_events"].values())
        if action_id is not None:
            items = [e for e in items if e["action_id"] == action_id]
        items.sort(key=lambda x: (int(x["settlement_revision"]), str(x["enqueued_at"])))
        return copy.deepcopy(items)

    def get_delivery_status(self, event_id: str) -> list[dict[str, Any]]:
        state = self.settlement_store.load_state(_mutable=True)
        items = [d for d in state["deliveries_by_id"].values() if d["event_id"] == event_id]
        items.sort(key=lambda x: str(x["consumer_domain"]))
        return copy.deepcopy(items)

    def list_unresolved_settlements(self) -> list[dict[str, Any]]:
        state = self.settlement_store.load_state(_mutable=True)
        latest_ids = set(state["latest_settlement_by_action"].values())
        items = [
            state["settlements_by_id"][sid]
            for sid in latest_ids
            if sid in state["settlements_by_id"]
            and state["settlements_by_id"][sid]["settlement_status"]
            in {SETTLEMENT_STATUS_OPEN, SETTLEMENT_STATUS_UNRESOLVED}
        ]
        items.sort(key=lambda x: str(x["settlement_id"]))
        return copy.deepcopy(items)

    def list_conflicts(self, *, action_id: Optional[str] = None) -> list[dict[str, Any]]:
        state = self.settlement_store.load_state(_mutable=True)
        items = list(state["conflicts_by_id"].values())
        if action_id is not None:
            items = [c for c in items if c["action_id"] == action_id]
        items.sort(key=lambda x: str(x["conflict_id"]))
        return copy.deepcopy(items)

    def trace_result_lineage(self, action_id: str) -> dict[str, Any]:
        """Section 25 & 74: Trace complete provenance graph from ActionAttempt -> Consumer Delivery."""
        state = self.settlement_store.load_state(_mutable=True)
        history = self.get_settlement_history(action_id)
        latest = history[-1] if history else None
        outbox_evs = self.list_outbox_events(action_id=action_id)
        deliveries: list[dict[str, Any]] = []
        for ev in outbox_evs:
            deliveries.extend(self.get_delivery_status(ev["event_id"]))

        action_rec = self.action_read.get_action(action_id) if self.action_read else None
        attempts = self.action_read.list_attempts(action_id) if self.action_read else []
        receipts = self.action_read.list_receipts(action_id) if self.action_read else []
        result_evidences = self.action_read.list_result_evidences(action_id) if self.action_read else []

        return {
            "action_id": action_id,
            "action_record": action_rec,
            "action_attempts": attempts,
            "action_receipts": receipts,
            "result_evidences": result_evidences,
            "ingested_settlement_evidences": copy.deepcopy(
                state["ingested_evidences_by_action"].get(action_id, [])
            ),
            "effect_claims": copy.deepcopy(latest.get("effect_claims", [])) if latest else [],
            "latest_settlement": latest,
            "settlement_history": history,
            "conflicts": self.list_conflicts(action_id=action_id),
            "settled_result_events": outbox_evs,
            "consumer_deliveries": deliveries,
        }


# ---------------------------------------------------------------------------
# ResultSettlementService & Outbox Dispatcher (Sections 5-48, 53-79)
# ---------------------------------------------------------------------------


class ResultSettlementService:
    """Sole authorized mutation & outbox dispatch service for AR-1 Result Settlement (0 LLM)."""

    def __init__(
        self,
        *,
        settlement_store: SettlementStore,
        action_store: ActionStore,
        lease: ActivityAuthorityLease,
        capability: SettlementWriterCapability,
        policy_registry: Optional[SettlementPolicyRegistry] = None,
        consumers: Optional[Mapping[str, ResultConsumerProtocol]] = None,
    ):
        verify_settlement_writer_capability(lease, capability)
        self.settlement_store = settlement_store
        self.action_store = action_store
        self.action_read = ActionReadService(action_store)
        self.lease = lease
        self.capability = capability
        self.policy_registry = policy_registry or SettlementPolicyRegistry()
        self.read_service = SettlementReadService(
            settlement_store=settlement_store,
            action_store=action_store,
        )
        self.consumers: dict[str, ResultConsumerProtocol] = dict(consumers or {})

    def register_consumer(self, consumer: ResultConsumerProtocol) -> None:
        if consumer.consumer_domain not in VALID_CONSUMER_DOMAINS:
            raise ContractViolationError(f"invalid consumer_domain={consumer.consumer_domain!r}")
        self.consumers[consumer.consumer_domain] = consumer

    def reject_narrative_or_unstructured_evidence(self, raw_claim: Any) -> None:
        """Section 7, 9, B05: Free-form strings ('应该成功了') or model text cannot enter Settlement."""
        raise UntrustedEvidenceError(
            f"Free-form narrative or unstructured string cannot settle Action Reality: {raw_claim!r}"
        )

    def settle_action(
        self,
        *,
        action_id: str,
        additional_evidences: Optional[Sequence[SettlementEvidenceInput | ActionReceipt | ResultEvidence | Mapping[str, Any] | str]] = None,
        correlation_id: Optional[str] = None,
        occurred_at: Optional[str] = None,
        fault_hook: Optional[Callable[[str], None]] = None,
    ) -> dict[str, Any]:
        """Deterministically evaluate or supersede settlement for an ActionRecord and atomically enqueue Outbox event."""
        verify_settlement_writer_capability(self.lease, self.capability)
        act = self.action_read.get_action(action_id)
        if act is None:
            raise ContractViolationError(
                f"Root action_id={action_id!r} not found in ActionStore (Section 24: action-less settlement forbidden)"
            )
        attempts = self.action_read.list_attempts(action_id)

        # Normalize & validate any additional evidences (reject strings / model text!)
        normalized_new_evs: list[SettlementEvidenceInput] = []
        for item in additional_evidences or []:
            if isinstance(item, str):
                self.reject_narrative_or_unstructured_evidence(item)
            elif isinstance(item, SettlementEvidenceInput):
                normalized_new_evs.append(item)
            elif isinstance(item, ActionReceipt):
                normalized_new_evs.append(SettlementEvidenceInput.from_action_receipt(item))
            elif isinstance(item, ResultEvidence):
                normalized_new_evs.append(SettlementEvidenceInput.from_result_evidence(item))
            elif isinstance(item, Mapping):
                if "narrative_text" in item or item.get("evidence_kind") in {"model_text", "narrative"}:
                    self.reject_narrative_or_unstructured_evidence(item)
                if item.get("schema_version") == "chiyo.action_reality.receipt.v1" or "receipt_id" in item:
                    normalized_new_evs.append(SettlementEvidenceInput.from_action_receipt(item))
                elif item.get("schema_version") == "chiyo.action_reality.result_evidence.v1" or "observer_kind" in item:
                    normalized_new_evs.append(SettlementEvidenceInput.from_result_evidence(item))
                else:
                    normalized_new_evs.append(
                        SettlementEvidenceInput(
                            evidence_id=str(item["evidence_id"]),
                            root_evidence_id=str(
                                item.get(
                                    "root_evidence_id",
                                    f"rootev:{sha256_hex(str(item['evidence_id']))[:16]}",
                                )
                            ),
                            action_id=str(item.get("action_id", action_id)),
                            source_ref=str(item["source_ref"]),
                            authority_domain=str(item["authority_domain"]),
                            evidence_kind=str(item["evidence_kind"]),
                            authority_grade=str(item["authority_grade"]),
                            claim_kind=str(item.get("claim_kind", "domain_fact")),
                            verified=bool(item.get("verified", True)),
                            occurred_at=str(item["occurred_at"]),
                            observed_at=str(item["observed_at"]),
                            recorded_at=str(item.get("recorded_at", item["observed_at"])),
                            target_ref=item.get("target_ref"),
                            domain_fact_ref=item.get("domain_fact_ref"),
                            expected_hash=item.get("expected_hash"),
                            observed_hash=item.get("observed_hash"),
                            details=dict(item.get("details", {})),
                        )
                    )
            else:
                raise ContractViolationError(f"unsupported evidence input type: {type(item)!r}")

        # Also collect receipts and result_evidences already recorded in AR-0 ActionStore for this action
        ar0_receipts = self.action_read.list_receipts(action_id)
        ar0_evidences = self.action_read.list_result_evidences(action_id)

        state = self.settlement_store.load_state(_mutable=True)
        existing_ev_dicts: list[dict[str, Any]] = list(
            state["ingested_evidences_by_action"].get(action_id, [])
        )
        existing_ev_by_id: dict[str, SettlementEvidenceInput] = {
            d["evidence_id"]: SettlementEvidenceInput(**d) for d in existing_ev_dicts
        }
        existing_root_ids: set[str] = {e.root_evidence_id for e in existing_ev_by_id.values()}

        new_root_evidence_added = False
        for r_dict in ar0_receipts:
            ev_obj = SettlementEvidenceInput.from_action_receipt(r_dict)
            if ev_obj.evidence_id not in existing_ev_by_id and ev_obj.root_evidence_id not in existing_root_ids:
                existing_ev_by_id[ev_obj.evidence_id] = ev_obj
                existing_root_ids.add(ev_obj.root_evidence_id)
                new_root_evidence_added = True

        for e_dict in ar0_evidences:
            ev_obj = SettlementEvidenceInput.from_result_evidence(e_dict)
            if ev_obj.evidence_id not in existing_ev_by_id and ev_obj.root_evidence_id not in existing_root_ids:
                existing_ev_by_id[ev_obj.evidence_id] = ev_obj
                existing_root_ids.add(ev_obj.root_evidence_id)
                new_root_evidence_added = True

        for ev_obj in normalized_new_evs:
            if ev_obj.action_id != action_id:
                raise ContractViolationError(
                    f"evidence action_id={ev_obj.action_id!r} does not match target action_id={action_id!r}"
                )
            if ev_obj.evidence_id not in existing_ev_by_id and ev_obj.root_evidence_id not in existing_root_ids:
                existing_ev_by_id[ev_obj.evidence_id] = ev_obj
                existing_root_ids.add(ev_obj.root_evidence_id)
                new_root_evidence_added = True

        prev_sid = state["latest_settlement_by_action"].get(action_id)
        prev_settlement = state["settlements_by_id"].get(prev_sid) if prev_sid else None
        action_rev_ref = f"actn_rev:{action_id}:v{act['revision']}"

        # Section 33: Duplicate evidence idempotency — if a settlement already exists for this action,
        # no new root evidence was added, and action revision/status is unchanged, return idempotently!
        if (
            prev_settlement is not None
            and not new_root_evidence_added
            and prev_settlement["action_revision_ref"] == action_rev_ref
        ):
            existing_evs = [
                ev
                for ev in state["outbox_events"].values()
                if ev["settlement_ref"] == prev_settlement["settlement_id"]
            ]
            return {
                "idempotent_replay": True,
                "settlement": copy.deepcopy(prev_settlement),
                "outbox_event": copy.deepcopy(existing_evs[0]) if existing_evs else None,
            }

        ts_rec = occurred_at or _iso(now())
        next_revision = int(prev_settlement["revision"]) + 1 if prev_settlement else 1
        settlement_id = f"rset:{sha256_hex(f'{action_id}:v{next_revision}')[:16]}"
        root_result_id = f"rres:{action_id}"
        resolved_corr_id = (
            correlation_id
            or (prev_settlement["correlation_id"] if prev_settlement else None)
            or f"corr:{sha256_hex(f'corr:{action_id}')[:16]}"
        )

        all_ev_inputs = list(existing_ev_by_id.values())
        eval_res = self.policy_registry.evaluate(
            action=act,
            attempts=attempts,
            evidences=all_ev_inputs,
            settlement_id=settlement_id,
            recorded_at=ts_rec,
        )

        # If semantics are identical to prev_settlement despite duplicate/non-escalating evidence, return idempotently
        if prev_settlement is not None and (
            prev_settlement["settlement_status"] == eval_res["settlement_status"]
            and prev_settlement["outcome_kind"] == eval_res["outcome_kind"]
            and prev_settlement["confirmed_effect_refs"]
            == [
                c["predicate"] + ":" + str(c.get("object_ref"))
                for c in eval_res["effect_claims"]
            ]
        ):
            pass

        if prev_settlement is not None:
            prev_predicates = sorted(
                (c["domain"], c["predicate"], c.get("object_ref"), c["epistemic_status"])
                for c in prev_settlement.get("effect_claims", [])
            )
            new_predicates = sorted(
                (c["domain"], c["predicate"], c.get("object_ref"), c["epistemic_status"])
                for c in eval_res["effect_claims"]
            )
            if (
                prev_settlement["settlement_status"] == eval_res["settlement_status"]
                and prev_settlement["outcome_kind"] == eval_res["outcome_kind"]
                and prev_predicates == new_predicates
                and len(prev_settlement.get("conflict_refs", [])) == len(eval_res["conflict_refs"])
            ):
                existing_evs = [
                    ev
                    for ev in state["outbox_events"].values()
                    if ev["settlement_ref"] == prev_settlement["settlement_id"]
                ]
                return {
                    "idempotent_replay": True,
                    "settlement": copy.deepcopy(prev_settlement),
                    "outbox_event": copy.deepcopy(existing_evs[0]) if existing_evs else None,
                }

        is_supersession = prev_settlement is not None
        if is_supersession and fault_hook is not None:
            fault_hook("before_superseding_settlement")
        if fault_hook is not None:
            fault_hook("before_settlement_commit")

        settled_at = (
            ts_rec
            if eval_res["settlement_status"]
            in {SETTLEMENT_STATUS_SETTLED, SETTLEMENT_STATUS_PARTIAL, SETTLEMENT_STATUS_CONFLICT}
            else None
        )

        record = ResultSettlementRecord(
            settlement_id=settlement_id,
            revision=next_revision,
            subject_id=GLOBAL_SUBJECT_ID,
            action_id=action_id,
            action_revision_ref=action_rev_ref,
            root_result_id=root_result_id,
            correlation_id=resolved_corr_id,
            settlement_status=eval_res["settlement_status"],
            outcome_kind=eval_res["outcome_kind"],
            receipt_refs=eval_res["receipt_refs"],
            evidence_refs=eval_res["evidence_refs"],
            authoritative_result_refs=eval_res["authoritative_result_refs"],
            root_evidence_ids=eval_res["root_evidence_ids"],
            effect_claims=eval_res["effect_claims"],
            effect_claim_refs=eval_res["effect_claim_refs"],
            confirmed_effect_refs=eval_res["confirmed_effect_refs"],
            missing_effect_refs=eval_res["missing_effect_refs"],
            failed_effect_refs=eval_res["failed_effect_refs"],
            unresolved_claims=eval_res["unresolved_claims"],
            conflict_refs=eval_res["conflict_refs"],
            effect_occurred_at=eval_res["effect_occurred_at"],
            effect_observed_at=eval_res["effect_observed_at"],
            settled_at=settled_at,
            recorded_at=ts_rec,
            supersedes_ref=prev_settlement["settlement_id"] if prev_settlement else None,
            policy_version=self.policy_registry.policy_version,
        )
        rec_dict = record.to_dict()

        # Section 31 & 67: Historical settlement remains immutable in settlements_by_id!
        # We never overwrite or delete prev_settlement["settlement_id"].
        state["settlements_by_id"][settlement_id] = rec_dict
        state["latest_settlement_by_action"][action_id] = settlement_id
        history_list = list(state["settlement_history_by_action"].get(action_id, []))
        history_list.append(settlement_id)
        state["settlement_history_by_action"][action_id] = history_list

        for c_dict in eval_res["effect_claims"]:
            state["effect_claims_by_id"][c_dict["claim_id"]] = c_dict
        for conf_dict in eval_res["conflicts"]:
            state["conflicts_by_id"][conf_dict["conflict_id"]] = conf_dict

        # If supersession resolved an open conflict from previous settlement, keep historical conflict record
        # intact and record resolution metadata if needed
        state["ingested_evidences_by_action"][action_id] = [
            e.to_dict() for e in sorted(all_ev_inputs, key=lambda x: (x.occurred_at, x.observed_at, x.evidence_id))
        ]

        # Section 19, 20, 67, 68: Atomically create Outbox event in the same commit!
        outbox_event_dict: Optional[dict[str, Any]] = None
        should_emit_event = eval_res["settlement_status"] in {
            SETTLEMENT_STATUS_SETTLED,
            SETTLEMENT_STATUS_PARTIAL,
            SETTLEMENT_STATUS_CONFLICT,
        }
        if should_emit_event:
            if fault_hook is not None:
                fault_hook("before_outbox_persist")

            prev_events = [
                ev
                for ev in state["outbox_events"].values()
                if ev["action_id"] == action_id
            ]
            prev_events.sort(key=lambda x: int(x["settlement_revision"]))
            prev_emitted = prev_events[-1] if prev_events else None

            is_correction = (
                prev_emitted is not None
                and (
                    prev_emitted["outcome_kind"] != record.outcome_kind
                    or prev_emitted["settlement_status"] != record.settlement_status
                    or prev_emitted["effect_claim_refs"] != record.effect_claim_refs
                )
            )
            ev_prefix = "scor" if is_correction else "srev"
            ev_kind = EVENT_KIND_CORRECTION if is_correction else EVENT_KIND_SETTLED
            ev_id = f"{ev_prefix}:{sha256_hex(f'{settlement_id}:{ev_kind}')[:16]}"

            occ_iso = record.effect_occurred_at or record.effect_observed_at
            obs_iso = record.effect_observed_at
            causal_parents = [action_id, settlement_id]
            if prev_settlement is not None:
                causal_parents.append(prev_settlement["settlement_id"])

            outbox_ev = SettledResultEvent(
                event_id=ev_id,
                event_kind=ev_kind,
                subject_id=GLOBAL_SUBJECT_ID,
                action_id=action_id,
                activity_ref=act.get("activity_ref"),
                settlement_ref=settlement_id,
                supersedes_settlement_ref=record.supersedes_ref,
                supersedes_event_ref=prev_emitted["event_id"] if prev_emitted else None,
                correction_reason=(
                    f"superseded_{prev_emitted['settlement_status']}_to_{record.settlement_status}"
                    if is_correction and prev_emitted
                    else None
                ),
                root_result_id=root_result_id,
                correlation_id=resolved_corr_id,
                settlement_status=record.settlement_status,
                outcome_kind=record.outcome_kind,
                effect_claim_refs=record.effect_claim_refs,
                evidence_refs=record.evidence_refs,
                confirmed_effect_refs=record.confirmed_effect_refs,
                missing_effect_refs=record.missing_effect_refs,
                failed_effect_refs=record.failed_effect_refs,
                occurred_at=occ_iso,
                observed_at=obs_iso,
                recorded_at=ts_rec,
                enqueued_at=ts_rec,
                causal_parent_refs=causal_parents,
                settlement_revision=record.revision,
                policy_version=record.policy_version,
            )
            outbox_event_dict = outbox_ev.to_dict()
            state["outbox_events"][ev_id] = outbox_event_dict
            if ev_id not in state["pending_outbox_event_ids"]:
                state["pending_outbox_event_ids"].append(ev_id)

        self.settlement_store.commit_mutation(
            lease=self.lease,
            capability=self.capability,
            next_state=state,
            journal_payload={
                "event_type": "SETTLEMENT_COMMITTED_WITH_OUTBOX" if outbox_event_dict else "SETTLEMENT_COMMITTED",
                "action_id": action_id,
                "settlement_id": settlement_id,
                "settlement_revision": next_revision,
                "settlement_status": record.settlement_status,
                "outcome_kind": record.outcome_kind,
                "supersedes_ref": record.supersedes_ref,
                "outbox_event_id": outbox_event_dict["event_id"] if outbox_event_dict else None,
            },
            fault_hook=fault_hook,
        )

        if fault_hook is not None:
            fault_hook("after_settlement_commit")
            if outbox_event_dict is not None:
                fault_hook("after_outbox_persist")
            if is_supersession:
                fault_hook("after_superseding_settlement")

        return {
            "idempotent_replay": False,
            "settlement": copy.deepcopy(rec_dict),
            "outbox_event": copy.deepcopy(outbox_event_dict),
        }

    # -- Outbox Dispatcher (Sections 19, 21-22, 53-57, 98-99) ----------------

    def dispatch_outbox(
        self,
        *,
        event_id: Optional[str] = None,
        target_domains: Optional[Sequence[str]] = None,
        fault_hook: Optional[Callable[[str], None]] = None,
    ) -> dict[str, Any]:
        """Deliver committed outbox events to registered sandbox consumers with per-consumer isolation."""
        verify_settlement_writer_capability(self.lease, self.capability)
        state = self.settlement_store.load_state(_mutable=True)

        if event_id is not None:
            ev_ids = [event_id] if event_id in state["outbox_events"] else []
        else:
            ev_ids = list(state["pending_outbox_event_ids"])

        domains = list(target_domains) if target_domains is not None else sorted(self.consumers.keys())
        delivery_results: list[dict[str, Any]] = []

        for eid in ev_ids:
            ev = state["outbox_events"][eid]
            all_domains_delivered = True

            for dom in domains:
                consumer = self.consumers.get(dom)
                if consumer is None:
                    continue

                idx_key = f"{eid}:{dom}"
                existing_del_id = state["delivery_index"].get(idx_key)
                existing_del = (
                    state["deliveries_by_id"].get(existing_del_id) if existing_del_id else None
                )

                # If already delivered & acknowledged with non-DEFER disposition, re-Verify consumer idempotency
                # when explicitly dispatching event_id, or skip if in pending scan
                attempt_no = (int(existing_del["attempt"]) + 1) if existing_del else 1
                del_id = existing_del_id or f"sdel:{sha256_hex(f'{eid}:{dom}')[:16]}"

                if fault_hook is not None:
                    fault_hook("before_dispatch")

                ts_now = _iso(now())
                try:
                    disp_res = consumer.receive_settled_event(ev)
                    if fault_hook is not None:
                        fault_hook("after_dispatch_before_delivery_ack")

                    del_rec = SettlementDeliveryRecord(
                        delivery_id=del_id,
                        event_id=eid,
                        consumer_domain=dom,
                        attempt=attempt_no if existing_del is None or existing_del["status"] != DELIVERY_STATUS_DELIVERED else int(existing_del["attempt"]),
                        status=DELIVERY_STATUS_DELIVERED,
                        consumer_disposition=disp_res.disposition,
                        downstream_proposal_ref=disp_res.downstream_proposal_ref,
                        last_error_ref=None,
                        delivered_at=existing_del["delivered_at"] if (existing_del and existing_del.get("delivered_at")) else ts_now,
                        acknowledged_at=ts_now,
                    )
                    if disp_res.disposition == DISPOSITION_DEFER:
                        all_domains_delivered = False
                except Exception as exc:
                    if isinstance(exc, RuntimeError) and str(exc).startswith("CRASH_INJECTED:"):
                        raise
                    # Section 55: One consumer down/failing MUST NOT block other consumers or rollback Settlement!
                    all_domains_delivered = False
                    err_ref = f"err:{dom}:{type(exc).__name__}"
                    del_rec = SettlementDeliveryRecord(
                        delivery_id=del_id,
                        event_id=eid,
                        consumer_domain=dom,
                        attempt=attempt_no,
                        status=DELIVERY_STATUS_FAILED,
                        consumer_disposition=None,
                        downstream_proposal_ref=None,
                        last_error_ref=err_ref,
                        delivered_at=None,
                        acknowledged_at=None,
                    )

                del_dict = del_rec.to_dict()
                state["deliveries_by_id"][del_id] = del_dict
                state["delivery_index"][idx_key] = del_id
                delivery_results.append(del_dict)

            if all_domains_delivered and eid in state["pending_outbox_event_ids"]:
                state["pending_outbox_event_ids"].remove(eid)

            self.settlement_store.commit_mutation(
                lease=self.lease,
                capability=self.capability,
                next_state=state,
                journal_payload={
                    "event_type": "OUTBOX_DISPATCH_RECORDED",
                    "outbox_event_id": eid,
                    "deliveries": [
                        {
                            "delivery_id": d["delivery_id"],
                            "consumer_domain": d["consumer_domain"],
                            "status": d["status"],
                            "consumer_disposition": d["consumer_disposition"],
                        }
                        for d in delivery_results
                        if d["event_id"] == eid
                    ],
                },
            )
            if fault_hook is not None:
                fault_hook("after_delivery_ack")

        return {
            "dispatched_count": len(delivery_results),
            "deliveries": copy.deepcopy(delivery_results),
            "pending_outbox_event_ids": list(state["pending_outbox_event_ids"]),
        }

    # -- Cold-Start Recovery (Sections 61-63) --------------------------------

    def recover(self) -> dict[str, Any]:
        """Recover WAL, verify settlement + outbox atomicity, and resume pending outbox deliveries."""
        self.settlement_store.flush_snapshot()
        self.settlement_store._verified_cache = None
        wal_recovered = self.settlement_store.recover_from_wal_if_needed()
        state = self.settlement_store.load_state(_mutable=True)

        # Ensure every SETTLED / PARTIAL / CONFLICT settlement has its corresponding outbox event
        # and any undelivered outbox event is in pending_outbox_event_ids
        requeued_events: list[str] = []
        for ev_id, ev in state["outbox_events"].items():
            doms = sorted(self.consumers.keys())
            if doms:
                needs_delivery = False
                for dom in doms:
                    idx_key = f"{ev_id}:{dom}"
                    did = state["delivery_index"].get(idx_key)
                    drec = state["deliveries_by_id"].get(did) if did else None
                    if drec is None or drec["status"] != DELIVERY_STATUS_DELIVERED or drec.get("consumer_disposition") == DISPOSITION_DEFER:
                        needs_delivery = True
                        break
                if needs_delivery and ev_id not in state["pending_outbox_event_ids"]:
                    state["pending_outbox_event_ids"].append(ev_id)
                    requeued_events.append(ev_id)

        if requeued_events:
            self.settlement_store.commit_mutation(
                lease=self.lease,
                capability=self.capability,
                next_state=state,
                journal_payload={
                    "event_type": "RECOVERY_REQUEUED_PENDING_OUTBOX",
                    "requeued_event_ids": requeued_events,
                },
            )

        return {
            "wal_recovered": wal_recovered,
            "settlement_count": len(state["settlements_by_id"]),
            "outbox_event_count": len(state["outbox_events"]),
            "pending_outbox_event_ids": list(state["pending_outbox_event_ids"]),
            "conflict_count": len(state["conflicts_by_id"]),
        }


# ---------------------------------------------------------------------------
# Zero-Side-Effect ResultSettlementReplay (Sections 59-60, 91)
# ---------------------------------------------------------------------------


class ResultSettlementReplay:
    """Section 59 & 60: Rebuilds Settlement, Outbox, and Delivery projections from journals with 0 side effects."""

    @staticmethod
    def replay_and_verify(
        *,
        action_store: ActionStore,
        settlement_store: SettlementStore,
        adapters: Optional[Mapping[str, ActionAdapterProtocol]] = None,
        consumers: Optional[Mapping[str, ResultConsumerProtocol]] = None,
    ) -> dict[str, Any]:
        before_adapter_calls = {
            k: adp.submit_call_count for k, adp in (adapters or {}).items()
        }
        before_consumer_writes = {
            k: c.domain_write_count for k, c in (consumers or {}).items()
        }
        before_consumer_events = {
            k: len(getattr(c, "received_events_by_id", {})) for k, c in (consumers or {}).items()
        }

        action_entries = action_store.read_journal()
        settlement_entries = settlement_store.read_journal()
        settlement_snapshot = settlement_store.load_state()

        after_adapter_calls = {
            k: adp.submit_call_count for k, adp in (adapters or {}).items()
        }
        after_consumer_writes = {
            k: c.domain_write_count for k, c in (consumers or {}).items()
        }
        after_consumer_events = {
            k: len(getattr(c, "received_events_by_id", {})) for k, c in (consumers or {}).items()
        }

        if before_adapter_calls != after_adapter_calls:
            raise ShadowIsolationError("ResultSettlementReplay triggered Action adapter calls!")
        if before_consumer_writes != after_consumer_writes or before_consumer_events != after_consumer_events:
            raise ShadowIsolationError("ResultSettlementReplay triggered consumer domain mutations!")

        assert settlement_snapshot["revision"] == len(settlement_entries)
        if settlement_entries:
            assert settlement_snapshot["last_entry_hash"] == settlement_entries[-1]["entry_hash"]

        return {
            "action_journal_count": len(action_entries),
            "settlement_journal_count": len(settlement_entries),
            "settlement_count": len(settlement_snapshot["settlements_by_id"]),
            "outbox_event_count": len(settlement_snapshot["outbox_events"]),
            "delivery_count": len(settlement_snapshot["deliveries_by_id"]),
            "conflict_count": len(settlement_snapshot["conflicts_by_id"]),
            "settlements_by_id": copy.deepcopy(settlement_snapshot["settlements_by_id"]),
            "latest_settlement_by_action": copy.deepcopy(settlement_snapshot["latest_settlement_by_action"]),
            "outbox_events": copy.deepcopy(settlement_snapshot["outbox_events"]),
            "deliveries_by_id": copy.deepcopy(settlement_snapshot["deliveries_by_id"]),
            "adapter_call_deltas": {
                k: after_adapter_calls[k] - before_adapter_calls[k] for k in before_adapter_calls
            },
            "consumer_write_deltas": {
                k: after_consumer_writes[k] - before_consumer_writes[k] for k in before_consumer_writes
            },
        }
