#!/usr/bin/env python3
"""Chiyo Life Runtime | LR-5 Natural Progress / Completion.

Formal Stage: LR-5 Natural Progress / Completion

Connects Action Reality (AR-0) + Result Settlement (AR-1) + Canonical Observations
to Life Runtime (LR-2 / LR-3 / LR-4) so that when an Activity Chiyo is doing produces
real, verified external results, Life Runtime deterministically tracks where the
Activity has progressed (`checkpoint_ref`) and naturally completes (`COMPLETED`)
when its explicit `CompletionContract` is satisfied by real evidence.

Top-Level Permanent Invariants:
- Action SUCCEEDED != Activity progressed
- SettledResultEvent != Activity progressed
- Activity progressed != Activity completed
- LLM says done != Activity completed
- No CompletionContract => NO_AUTOMATIC_COMPLETION (can progress, never auto-completes)
- CompletionEligibility != COMPLETED (must execute through LR-2 ActivityCommandService.complete_activity)
- WAITING + progress != RESUME (unless CompletionContract is 100% satisfied, in which case WAITING -> COMPLETED is legal)
- Terminal Activities (COMPLETED / ABANDONED / CANCELLED / EXPIRED) never revive; late conflicting correction on COMPLETED records COMPLETION_CONFLICT / RECONCILIATION_REQUIRED.
- 0 LLM calls, 0 Memory writes, 0 Goal writes, 0 World writes, 0 Artifact writes, 0 Outbound messages.
"""

from __future__ import annotations

import copy
import hashlib
import hmac
import json
import os
import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence

from activity_compat import (
    ActivityError,
    ActivityLockError,
    ActivityTransitionError,
    CompletionEvidenceError,
    _compact_value,
    _iso,
    _parse,
    now,
)
from activity_admission import (
    ActivityConsequencePort,
    CompletionProposal,
    InterruptionProposal,
    REASON_KIND_RUNTIME_CONSTRAINT,
    WaitingProposal,
)
from activity_continuity import (
    ALL_ACTIVITY_STATUSES,
    FORBIDDEN_WRITER_DOMAINS,
    GENESIS_HASH,
    GLOBAL_SUBJECT_ID,
    LIFE_STATE_IDLE,
    NAMESPACE_CANONICAL,
    NAMESPACE_ISOLATED_TEST,
    NON_PRODUCTION_NAMESPACES,
    OPEN_ACTIVITY_STATUSES,
    PRODUCTION_CUTOVER_ENV,
    STATUS_ABANDONED,
    STATUS_ACTIVE,
    STATUS_CANCELLED,
    STATUS_COMPLETED,
    STATUS_EXPIRED,
    STATUS_PAUSED,
    STATUS_WAITING,
    TERMINAL_ACTIVITY_STATUSES,
    TRANSITION_CHECKPOINT,
    TRANSITION_COMPLETE,
    VALID_CHECKPOINT_PREFIXES,
    VALID_DECISION_PREFIXES,
    VALID_EVIDENCE_PREFIXES,
    WRITER_DOMAIN_CANONICAL,
    ActivityAuthorityLease,
    ActivityCommandService,
    ActivityReadService,
    CanonicalActivityStore,
    CommandResult,
    ContractViolationError,
    IdempotencyConflictError,
    RecoveryRequiredError,
    ReplayRuntime,
    RevisionConflictError,
    SubjectIdentityError,
    TerminalActivityImmutableError,
    WriterCapability,
    WriterCapabilityError,
    _STRUCTURED_REF_RE,
    _fsync_dir,
    _validate_structured_ref,
    build_life_frame_read_view,
    canonical_json_line,
    is_production_path,
    normalize_ref_list,
    sha256_hex,
    validate_completion_evidence_ref,
    validate_subject_id,
)
from result_settlement_ar1 import (
    CONSUMER_DOMAIN_LIFE,
    DISPOSITION_ACCEPT,
    DISPOSITION_CONFLICT,
    DISPOSITION_IGNORE,
    EVENT_KIND_CORRECTION,
    EVENT_KIND_SETTLED,
    OUTCOME_CANCELLED_NO_EFFECT,
    OUTCOME_FAILURE_NO_EFFECT,
    OUTCOME_PARTIAL_EFFECT,
    OUTCOME_SUCCESS_EFFECT,
    OUTCOME_UNKNOWN_EFFECT,
    SETTLEMENT_STATUS_CONFLICT,
    SETTLEMENT_STATUS_OPEN,
    SETTLEMENT_STATUS_PARTIAL,
    SETTLEMENT_STATUS_SETTLED,
    SETTLEMENT_STATUS_SUPERSEDED,
    SETTLEMENT_STATUS_UNRESOLVED,
    ConsumerDispositionResult,
    ResultConsumerProtocol,
    SettlementReadService,
)

OUTCOME_NO_OP = OUTCOME_CANCELLED_NO_EFFECT

# ---------------------------------------------------------------------------
# Constants, Schemas & Authority Names (Sections 1-23, 55-56)
# ---------------------------------------------------------------------------

FORMAL_STAGE_NAME = "LR-5 Natural Progress / Completion"
WRITER_DOMAIN_PROGRESS = "life_progress_authority"

SCHEMA_PROGRESS_EVIDENCE = "chiyo.life.progress_evidence.v1"
SCHEMA_ACTIVITY_RESULT_BINDING = "chiyo.life.activity_result_binding.v1"
SCHEMA_PROGRESS_CONTRACT = "chiyo.life.progress_contract.v1"
SCHEMA_COMPLETION_CONTRACT = "chiyo.life.completion_contract.v1"
SCHEMA_CHECKPOINT_UPDATE = "chiyo.life.checkpoint_update.v1"
SCHEMA_COMPLETION_ELIGIBILITY = "chiyo.life.completion_eligibility.v1"
SCHEMA_COMPLETION_RECONCILIATION = "chiyo.life.completion_reconciliation.v1"

SCHEMA_PROGRESS_STATE = "chiyo.life.progress_state.v1"
SCHEMA_PROGRESS_JOURNAL_ENTRY = "chiyo.life.progress_journal_entry.v1"
SCHEMA_PROGRESS_WAL = "chiyo.life.progress_wal.v1"

POLICY_ARTIFACT_V1 = "chiyo.life.progress.artifact.v1"
POLICY_POSITION_V1 = "chiyo.life.progress.position.v1"
POLICY_WORLD_FACT_V1 = "chiyo.life.progress.world_fact.v1"
POLICY_EXTERNAL_RESULT_V1 = "chiyo.life.progress.external_result.v1"
POLICY_COMPOSITE_V1 = "chiyo.life.progress.composite.v1"
POLICY_OPEN_ENDED_V1 = "chiyo.life.progress.open_ended.v1"

VALID_POLICY_VERSIONS = frozenset(
    {
        POLICY_ARTIFACT_V1,
        POLICY_POSITION_V1,
        POLICY_WORLD_FACT_V1,
        POLICY_EXTERNAL_RESULT_V1,
        POLICY_COMPOSITE_V1,
        POLICY_OPEN_ENDED_V1,
    }
)

# Section 6-8: Allowed Evidence Source Kinds & Authority Domains
SOURCE_KIND_SETTLED_EVENT = "SETTLED_RESULT_EVENT"
SOURCE_KIND_CORRECTION_EVENT = "SETTLED_RESULT_CORRECTION_EVENT"
SOURCE_KIND_WORLD_FACT = "WORLD_CANONICAL_OBSERVATION"
SOURCE_KIND_ARTIFACT_STATE = "ARTIFACT_CANONICAL_STATE"
SOURCE_KIND_DOCUMENT_STATE = "DOCUMENT_CURSOR_STATE"
SOURCE_KIND_GAME_STATE = "GAME_CANONICAL_STATE"
SOURCE_KIND_TOOL_RESULT = "TOOL_SETTLED_RESULT"
SOURCE_KIND_EXTERNAL_RESULT = "EXTERNAL_CANONICAL_RESULT"

VALID_EVIDENCE_SOURCE_KINDS = frozenset(
    {
        SOURCE_KIND_SETTLED_EVENT,
        SOURCE_KIND_CORRECTION_EVENT,
        SOURCE_KIND_WORLD_FACT,
        SOURCE_KIND_ARTIFACT_STATE,
        SOURCE_KIND_DOCUMENT_STATE,
        SOURCE_KIND_GAME_STATE,
        SOURCE_KIND_TOOL_RESULT,
        SOURCE_KIND_EXTERNAL_RESULT,
    }
)

FORBIDDEN_EVIDENCE_SOURCE_KINDS = frozenset(
    {
        "LLM_TEXT",
        "MODEL_NARRATION",
        "RAW_PROVIDER_RESPONSE",
        "RAW_ADAPTER_RESPONSE",
        "RAW_ACTION_RECEIPT",
        "USER_CHAT_CLAIM",
        "SELF_ASSERTED_PROGRESS",
    }
)

VALID_EVIDENCE_AUTHORITY_DOMAINS = frozenset(
    {
        "result_settlement_authority",
        "world_authority",
        "artifact_authority",
        "document_authority",
        "game_authority",
        "tool_result_authority",
        "external_result_authority",
    }
)

EPISTEMIC_SETTLED_FACT = "SETTLED_FACT"
EPISTEMIC_CANONICAL_OBSERVATION = "CANONICAL_OBSERVATION"
EPISTEMIC_PARTIAL_FACT = "PARTIAL_FACT"
EPISTEMIC_CONFLICTED_CLAIM = "CONFLICTED_CLAIM"
EPISTEMIC_UNKNOWN_EVIDENCE = "UNKNOWN_EVIDENCE"

VALID_EPISTEMIC_KINDS = frozenset(
    {
        EPISTEMIC_SETTLED_FACT,
        EPISTEMIC_CANONICAL_OBSERVATION,
        EPISTEMIC_PARTIAL_FACT,
        EPISTEMIC_CONFLICTED_CLAIM,
        EPISTEMIC_UNKNOWN_EVIDENCE,
    }
)

# Section 17: ProgressState Values
PROGRESS_STATE_UNKNOWN = "UNKNOWN"
PROGRESS_STATE_UNCHANGED = "UNCHANGED"
PROGRESS_STATE_ADVANCED = "ADVANCED"
PROGRESS_STATE_REGRESSED = "REGRESSED"
PROGRESS_STATE_BLOCKED = "BLOCKED"
PROGRESS_STATE_COMPLETION_ELIGIBLE = "COMPLETION_ELIGIBLE"

VALID_PROGRESS_STATES = frozenset(
    {
        PROGRESS_STATE_UNKNOWN,
        PROGRESS_STATE_UNCHANGED,
        PROGRESS_STATE_ADVANCED,
        PROGRESS_STATE_REGRESSED,
        PROGRESS_STATE_BLOCKED,
        PROGRESS_STATE_COMPLETION_ELIGIBLE,
    }
)

# Section 42 & 53: Activity Progress Kinds & Checkpoint Semantics
PROGRESS_KIND_ARTIFACT_EDIT = "ARTIFACT_EDIT"
PROGRESS_KIND_POSITIONAL_READING = "POSITIONAL_READING"
PROGRESS_KIND_WORLD_ACTION_BOUNDED = "WORLD_ACTION_BOUNDED"
PROGRESS_KIND_WAIT_FOR_EXTERNAL_RESULT = "WAIT_FOR_EXTERNAL_RESULT"
PROGRESS_KIND_MULTI_ACTION_CHECKLIST = "MULTI_ACTION_CHECKLIST"
PROGRESS_KIND_OPEN_ENDED = "OPEN_ENDED"

VALID_PROGRESS_KINDS = frozenset(
    {
        PROGRESS_KIND_ARTIFACT_EDIT,
        PROGRESS_KIND_POSITIONAL_READING,
        PROGRESS_KIND_WORLD_ACTION_BOUNDED,
        PROGRESS_KIND_WAIT_FOR_EXTERNAL_RESULT,
        PROGRESS_KIND_MULTI_ACTION_CHECKLIST,
        PROGRESS_KIND_OPEN_ENDED,
    }
)

CHECKPOINT_SEMANTICS_LATEST_POSITION = "LATEST_POSITION"
CHECKPOINT_SEMANTICS_MAX_CONFIRMED_POSITION = "MAX_CONFIRMED_POSITION"
CHECKPOINT_SEMANTICS_LATEST_ARTIFACT_VERSION = "LATEST_ARTIFACT_VERSION"
CHECKPOINT_SEMANTICS_TARGET_STATE = "TARGET_STATE"

VALID_CHECKPOINT_SEMANTICS = frozenset(
    {
        CHECKPOINT_SEMANTICS_LATEST_POSITION,
        CHECKPOINT_SEMANTICS_MAX_CONFIRMED_POSITION,
        CHECKPOINT_SEMANTICS_LATEST_ARTIFACT_VERSION,
        CHECKPOINT_SEMANTICS_TARGET_STATE,
    }
)

# Section 18-19: Scale Kinds (Provable vs None)
SCALE_KIND_NONE = "NONE"
SCALE_KIND_PROVABLE_PAGE_RATIO = "PROVABLE_PAGE_RATIO"
SCALE_KIND_PROVABLE_BYTE_RATIO = "PROVABLE_BYTE_RATIO"
SCALE_KIND_PROVABLE_CHECKLIST = "PROVABLE_CHECKLIST_COUNT"

VALID_SCALE_KINDS = frozenset(
    {
        SCALE_KIND_NONE,
        SCALE_KIND_PROVABLE_PAGE_RATIO,
        SCALE_KIND_PROVABLE_BYTE_RATIO,
        SCALE_KIND_PROVABLE_CHECKLIST,
    }
)

# Section 15: Bounded Deterministic Completion Criteria Kinds
CRITERIA_ARTIFACT_VERSION_REACHED = "ARTIFACT_VERSION_REACHED"
CRITERIA_DOCUMENT_CURSOR_REACHED = "DOCUMENT_CURSOR_REACHED"
CRITERIA_PAGE_POSITION_REACHED = "PAGE_POSITION_REACHED"
CRITERIA_WORLD_FACT_MATCH = "WORLD_FACT_MATCH"
CRITERIA_ACTION_RESULT_MATCH = "ACTION_RESULT_MATCH"
CRITERIA_TOOL_RESULT_MATCH = "TOOL_RESULT_MATCH"
CRITERIA_EXTERNAL_RESULT_MATCH = "EXTERNAL_RESULT_MATCH"
CRITERIA_ALL_OF = "ALL_OF"
CRITERIA_ANY_OF = "ANY_OF"

LEAF_CRITERIA_KINDS = frozenset(
    {
        CRITERIA_ARTIFACT_VERSION_REACHED,
        CRITERIA_DOCUMENT_CURSOR_REACHED,
        CRITERIA_PAGE_POSITION_REACHED,
        CRITERIA_WORLD_FACT_MATCH,
        CRITERIA_ACTION_RESULT_MATCH,
        CRITERIA_TOOL_RESULT_MATCH,
        CRITERIA_EXTERNAL_RESULT_MATCH,
    }
)
COMPOSITE_CRITERIA_KINDS = frozenset({CRITERIA_ALL_OF, CRITERIA_ANY_OF})
VALID_CRITERIA_KINDS = LEAF_CRITERIA_KINDS | COMPOSITE_CRITERIA_KINDS

FORBIDDEN_CRITERIA_KINDS = frozenset(
    {
        "LLM_JUDGES_DONE",
        "SEMANTICALLY_GOOD_ENOUGH",
        "FEELS_FINISHED",
        "MODEL_SAYS_COMPLETE",
        "ARBITRARY_PERCENT_REACHED",
    }
)

FORBIDDEN_CRITERIA_PARAM_KEYS = frozenset(
    {
        "llm_judges_done",
        "semantically_good_enough",
        "feels_finished",
        "llm_prompt",
        "prompt",
        "eval",
        "exec",
        "lambda",
        "subjective_score",
    }
)

MAX_CRITERIA_DEPTH = 2
MAX_CRITERIA_CHILDREN = 16

CONTRACT_STATUS_ACTIVE = "ACTIVE"
CONTRACT_STATUS_SUPERSEDED = "SUPERSEDED"
CONTRACT_STATUS_CANCELLED = "CANCELLED"
VALID_CONTRACT_STATUSES = frozenset(
    {
        CONTRACT_STATUS_ACTIVE,
        CONTRACT_STATUS_SUPERSEDED,
        CONTRACT_STATUS_CANCELLED,
    }
)

# Section 27: NaturalCompletionEvaluator Output Statuses
EVAL_NOT_COMPLETE = "NOT_COMPLETE"
EVAL_COMPLETION_ELIGIBLE = "COMPLETION_ELIGIBLE"
EVAL_INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"
EVAL_CONFLICT = "CONFLICT"
EVAL_STALE = "STALE"

VALID_COMPLETION_EVAL_OUTPUTS = frozenset(
    {
        EVAL_NOT_COMPLETE,
        EVAL_COMPLETION_ELIGIBLE,
        EVAL_INSUFFICIENT_EVIDENCE,
        EVAL_CONFLICT,
        EVAL_STALE,
    }
)

# Section 28: ActivityCompletionEligibility Statuses
COMP_ELIG_STATUS_OPEN = "OPEN"
COMP_ELIG_STATUS_CONSUMED = "CONSUMED"
COMP_ELIG_STATUS_STALE = "STALE"
COMP_ELIG_STATUS_CONFLICTED = "CONFLICTED"

VALID_COMP_ELIG_STATUSES = frozenset(
    {
        COMP_ELIG_STATUS_OPEN,
        COMP_ELIG_STATUS_CONSUMED,
        COMP_ELIG_STATUS_STALE,
        COMP_ELIG_STATUS_CONFLICTED,
    }
)

# Section 39: Reconciliation Statuses
RECON_STATUS_REQUIRED = "RECONCILIATION_REQUIRED"
RECON_STATUS_RESOLVED = "RESOLVED"
CONFLICT_KIND_COMPLETION = "COMPLETION_CONFLICT"
CONFLICT_KIND_PROGRESS = "PROGRESS_CONFLICT"


# ---------------------------------------------------------------------------
# LR-5 Specific Exceptions
# ---------------------------------------------------------------------------


class ProgressCompletionError(ActivityError):
    """Base exception for LR-5 Natural Progress / Completion errors."""


class UntrustedProgressEvidenceError(ProgressCompletionError):
    """Raised when LLM text, unstructured narration, or raw unsettled receipt attempts to drive progress."""


class IllegalProgressScaleError(ProgressCompletionError):
    """Raised when an unprovable or arbitrary percentage is supplied as progress."""


class UnboundProgressEvidenceError(ProgressCompletionError):
    """Raised when evidence has no legal binding to an Activity and strict mode is requested."""


class CompletionContractValidationError(ContractViolationError):
    """Raised when a CompletionContract uses forbidden subjective/LLM criteria or exceeds depth bounds."""


class IllegalContractLoweringError(ProgressCompletionError):
    """Raised when a CompletionContract revision attempts to lower target criteria without authorized decision provenance."""


class CrossDomainWriteForbiddenError(WriterCapabilityError):
    """Raised when LR-5 or Grounding attempts to write to World, Artifact, Memory, Goal, or bypass Activity Authority."""


class ShadowMutationForbiddenError(WriterCapabilityError):
    """Raised when Replay or Shadow mode attempts to mutate canonical Activity or Progress stores."""


# ---------------------------------------------------------------------------
# Authority & Capability Model (Section 22, 61, 104)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProgressWriterCapability:
    """Capability token issued by LifeProgressAuthority for mutating ProgressStore."""

    writer_domain: str
    namespace: str
    store_root: Path
    lease_instance_id: str
    _signature: str = field(repr=False)


class LifeProgressAuthority:
    """Sole canonical owner for LR-5 Progress & Completion state and journal (`life_progress_authority`).

    Note: `LifeProgressAuthority` owns `ProgressStore`, NOT `CanonicalActivityStore`.
    Updating `Activity.checkpoint_ref` or completing an `Activity` MUST go through
    `LR-2` `ActivityCommandService` holding `life_activity_authority`.
    """

    authority_name = WRITER_DOMAIN_PROGRESS

    @classmethod
    def issue_capability(
        cls,
        *,
        lease: ActivityAuthorityLease,
        writer_domain: str = WRITER_DOMAIN_PROGRESS,
        namespace: str = NAMESPACE_ISOLATED_TEST,
        allow_production_cutover: bool = False,
    ) -> ProgressWriterCapability:
        if not isinstance(lease, ActivityAuthorityLease) or not lease.held:
            raise WriterCapabilityError("cannot issue ProgressWriterCapability without a held ActivityAuthorityLease")
        domain = str(writer_domain or "").strip().lower()
        if domain != WRITER_DOMAIN_PROGRESS:
            raise WriterCapabilityError(
                f"writer_domain={writer_domain!r} is DENIED ProgressStore write capability; "
                f"only {WRITER_DOMAIN_PROGRESS!r} is permitted"
            )
        ns = str(namespace or "").strip().lower()
        if ns in NON_PRODUCTION_NAMESPACES:
            raise ShadowMutationForbiddenError(
                f"namespace={namespace!r} is permanently DENIED ProgressStore write capability"
            )
        if ns not in {NAMESPACE_CANONICAL, NAMESPACE_ISOLATED_TEST}:
            raise WriterCapabilityError(f"unsupported ProgressStore namespace={namespace!r}")

        if is_production_path(lease.store_root):
            cutover_env = os.environ.get(PRODUCTION_CUTOVER_ENV, "off").strip().lower()
            if not allow_production_cutover or cutover_env != "canonical":
                raise WriterCapabilityError(
                    f"LR-5 production non-cutover guard: writing to {lease.store_root} is blocked"
                )

        sig = lease.sign_capability(domain, ns)
        return ProgressWriterCapability(
            writer_domain=domain,
            namespace=ns,
            store_root=lease.store_root,
            lease_instance_id=lease.instance_id,
            _signature=sig,
        )

    @staticmethod
    def verify_capability(lease: ActivityAuthorityLease, capability: ProgressWriterCapability) -> None:
        if not isinstance(capability, ProgressWriterCapability):
            raise WriterCapabilityError("expected a valid ProgressWriterCapability")
        if not isinstance(lease, ActivityAuthorityLease) or not lease.held:
            raise WriterCapabilityError("ProgressWriterCapability requires a held ActivityAuthorityLease")
        if capability.writer_domain != WRITER_DOMAIN_PROGRESS:
            raise WriterCapabilityError(f"invalid capability domain={capability.writer_domain!r}")
        if capability.store_root != lease.store_root:
            raise WriterCapabilityError("ProgressWriterCapability store_root mismatch")
        expected = lease.sign_capability(capability.writer_domain, capability.namespace)
        if not hmac.compare_digest(expected, capability._signature):
            raise WriterCapabilityError("ProgressWriterCapability HMAC signature verification failed")

    @staticmethod
    def request_external_domain_writer(target_domain: str) -> None:
        """Section 68, 69, 91 (I09): LR-5 is permanently forbidden from writing to World, Artifact, Memory, or Goal."""
        raise CrossDomainWriteForbiddenError(
            f"LR-5 ({WRITER_DOMAIN_PROGRESS}) is permanently forbidden from obtaining writer capability "
            f"for domain {target_domain!r}"
        )


# ---------------------------------------------------------------------------
# Helper Validators
# ---------------------------------------------------------------------------


def _require_iso(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ContractViolationError(f"{field_name} must be a non-empty ISO-8601 string")
    parsed = _parse(value)
    if parsed is None:
        raise ContractViolationError(f"{field_name} is not a valid ISO-8601 timestamp: {value!r}")
    return _iso(parsed)


def _optional_iso(value: Any, field_name: str) -> Optional[str]:
    if value is None:
        return None
    return _require_iso(value, field_name)


def validate_checkpoint_external_ref(value: Any, field_name: str = "checkpoint_ref") -> str:
    """Section 20 & I03: checkpoint_ref must reference a real external/canonical state, never free text like '做到一半'."""
    if not isinstance(value, str) or not value.strip():
        raise ContractViolationError(f"{field_name} must be a non-empty structured reference")
    return _validate_structured_ref(
        value,
        field_name,
        allowed_prefixes=VALID_CHECKPOINT_PREFIXES,
        error_cls=ContractViolationError,
    )


def validate_provable_progress_facts(observed_facts: Mapping[str, Any]) -> dict[str, Any]:
    """Sections 18 & 19, B02: Reject arbitrary percentages unless backed by provable finite numerator & denominator."""
    facts = copy.deepcopy(dict(observed_facts or {}))
    forbidden_text_keys = {"llm_text", "model_narration", "narrative_claim", "feels_done"}
    bad_keys = forbidden_text_keys.intersection({str(k).lower() for k in facts.keys()})
    if bad_keys:
        raise UntrustedProgressEvidenceError(
            f"LLM or narrative keys are forbidden in observed_facts: {sorted(bad_keys)}"
        )

    if "progress_percent" in facts or "percent" in facts or "progress_ratio" in facts:
        # Only legal if provable finite scale numerator & denominator exist and match!
        has_page_scale = (
            isinstance(facts.get("page"), int)
            and isinstance(facts.get("total_pages"), int)
            and int(facts["total_pages"]) > 0
        )
        has_byte_scale = (
            isinstance(facts.get("downloaded_bytes"), int)
            and isinstance(facts.get("total_bytes"), int)
            and int(facts["total_bytes"]) > 0
        )
        has_checklist_scale = (
            isinstance(facts.get("completed_count"), int)
            and isinstance(facts.get("total_count"), int)
            and int(facts["total_count"]) > 0
        )
        if not (has_page_scale or has_byte_scale or has_checklist_scale):
            raise IllegalProgressScaleError(
                "Section 18/19 violation: arbitrary progress percentage/ratio is forbidden without "
                "a provable finite scale (e.g., page/total_pages, downloaded_bytes/total_bytes)"
            )
    return facts


# ---------------------------------------------------------------------------
# Canonical Data Models (Sections 6-29, 50-56, 104)
# ---------------------------------------------------------------------------


@dataclass
class LifeProgressEvidence:
    """Section 6 & 8: Standardized canonical progress evidence input (`chiyo.life.progress_evidence.v1`)."""

    progress_evidence_id: str
    source_kind: str
    source_ref: str
    authority_domain: str
    epistemic_kind: str
    occurred_at: str
    observed_at: str
    recorded_at: str
    idempotency_key: str
    subject_id: str = GLOBAL_SUBJECT_ID
    activity_id: Optional[str] = None
    activity_revision_hint: Optional[int] = None
    settlement_ref: Optional[str] = None
    supersedes_settlement_ref: Optional[str] = None
    action_ref: Optional[str] = None
    effect_claim_refs: list[str] = field(default_factory=list)
    effect_claims: list[dict[str, Any]] = field(default_factory=list)
    settlement_status: Optional[str] = None
    outcome_kind: Optional[str] = None
    artifact_ref: Optional[str] = None
    world_fact_ref: Optional[str] = None
    external_result_ref: Optional[str] = None
    target_ref: Optional[str] = None
    observed_facts: dict[str, Any] = field(default_factory=dict)
    causal_parent_refs: list[str] = field(default_factory=list)
    correlation_id: Optional[str] = None
    schema_version: str = SCHEMA_PROGRESS_EVIDENCE

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_PROGRESS_EVIDENCE:
            raise ContractViolationError(f"invalid LifeProgressEvidence schema_version={self.schema_version!r}")
        self.subject_id = validate_subject_id(self.subject_id)
        self.progress_evidence_id = _validate_structured_ref(
            self.progress_evidence_id,
            "progress_evidence_id",
            allowed_prefixes=frozenset({"pevid", "progress_evidence"}),
        )
        if self.source_kind in FORBIDDEN_EVIDENCE_SOURCE_KINDS:
            raise UntrustedProgressEvidenceError(
                f"source_kind={self.source_kind!r} is forbidden from entering LR-5 Progress/Completion"
            )
        if self.source_kind not in VALID_EVIDENCE_SOURCE_KINDS:
            raise UntrustedProgressEvidenceError(
                f"unsupported source_kind={self.source_kind!r}; allowed={sorted(VALID_EVIDENCE_SOURCE_KINDS)}"
            )
        self.source_ref = _validate_structured_ref(self.source_ref, "source_ref")
        if self.authority_domain not in VALID_EVIDENCE_AUTHORITY_DOMAINS:
            raise UntrustedProgressEvidenceError(
                f"untrusted authority_domain={self.authority_domain!r}; "
                f"allowed={sorted(VALID_EVIDENCE_AUTHORITY_DOMAINS)}"
            )
        if self.epistemic_kind not in VALID_EPISTEMIC_KINDS:
            raise ContractViolationError(f"invalid epistemic_kind={self.epistemic_kind!r}")
        if not isinstance(self.idempotency_key, str) or not self.idempotency_key.strip():
            raise ContractViolationError("idempotency_key must be a non-empty string")
        self.idempotency_key = self.idempotency_key.strip()
        self.occurred_at = _require_iso(self.occurred_at, "occurred_at")
        self.observed_at = _require_iso(self.observed_at, "observed_at")
        self.recorded_at = _require_iso(self.recorded_at, "recorded_at")
        if self.settlement_ref is not None:
            self.settlement_ref = _validate_structured_ref(self.settlement_ref, "settlement_ref")
        if self.supersedes_settlement_ref is not None:
            self.supersedes_settlement_ref = _validate_structured_ref(
                self.supersedes_settlement_ref, "supersedes_settlement_ref"
            )
        if self.action_ref is not None:
            self.action_ref = _validate_structured_ref(self.action_ref, "action_ref")
        if self.artifact_ref is not None:
            self.artifact_ref = _validate_structured_ref(self.artifact_ref, "artifact_ref")
        if self.world_fact_ref is not None:
            self.world_fact_ref = _validate_structured_ref(self.world_fact_ref, "world_fact_ref")
        if self.external_result_ref is not None:
            self.external_result_ref = _validate_structured_ref(
                self.external_result_ref, "external_result_ref"
            )
        if self.target_ref is not None:
            self.target_ref = _validate_structured_ref(self.target_ref, "target_ref")
        self.effect_claim_refs = normalize_ref_list(
            self.effect_claim_refs, "effect_claim_refs", allow_empty=True
        )
        self.causal_parent_refs = normalize_ref_list(
            self.causal_parent_refs, "causal_parent_refs", allow_empty=True
        )
        self.observed_facts = validate_provable_progress_facts(self.observed_facts)

    @property
    def provable_percentage(self) -> Optional[float]:
        """Section 19: Compute provable percentage ONLY if real finite numerator & denominator exist."""
        f = self.observed_facts
        if isinstance(f.get("page"), int) and isinstance(f.get("total_pages"), int) and f["total_pages"] > 0:
            return round((float(f["page"]) / float(f["total_pages"])) * 100.0, 2)
        if isinstance(f.get("downloaded_bytes"), int) and isinstance(f.get("total_bytes"), int) and f["total_bytes"] > 0:
            return round((float(f["downloaded_bytes"]) / float(f["total_bytes"])) * 100.0, 2)
        if isinstance(f.get("completed_count"), int) and isinstance(f.get("total_count"), int) and f["total_count"] > 0:
            return round((float(f["completed_count"]) / float(f["total_count"])) * 100.0, 2)
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "progress_evidence_id": self.progress_evidence_id,
            "subject_id": self.subject_id,
            "activity_id": self.activity_id,
            "activity_revision_hint": self.activity_revision_hint,
            "source_kind": self.source_kind,
            "source_ref": self.source_ref,
            "settlement_ref": self.settlement_ref,
            "supersedes_settlement_ref": self.supersedes_settlement_ref,
            "action_ref": self.action_ref,
            "effect_claim_refs": list(self.effect_claim_refs),
            "effect_claims": copy.deepcopy(self.effect_claims),
            "settlement_status": self.settlement_status,
            "outcome_kind": self.outcome_kind,
            "artifact_ref": self.artifact_ref,
            "world_fact_ref": self.world_fact_ref,
            "external_result_ref": self.external_result_ref,
            "target_ref": self.target_ref,
            "observed_facts": copy.deepcopy(self.observed_facts),
            "occurred_at": self.occurred_at,
            "observed_at": self.observed_at,
            "recorded_at": self.recorded_at,
            "causal_parent_refs": list(self.causal_parent_refs),
            "correlation_id": self.correlation_id,
            "authority_domain": self.authority_domain,
            "epistemic_kind": self.epistemic_kind,
            "idempotency_key": self.idempotency_key,
        }

    @classmethod
    def from_settled_result_event(
        cls,
        event: Mapping[str, Any],
        *,
        settlement_record: Optional[Mapping[str, Any]] = None,
        observed_facts_override: Optional[Mapping[str, Any]] = None,
        activity_revision_hint: Optional[int] = None,
    ) -> "LifeProgressEvidence":
        """Convert an AR-1 SettledResultEvent / SettledResultCorrectionEvent into a LifeProgressEvidence."""
        if not isinstance(event, Mapping):
            raise UntrustedProgressEvidenceError("SettledResultEvent must be a mapping")
        event_id = str(event["event_id"])
        settlement_ref = str(event["settlement_ref"])
        ev_kind = str(event.get("event_kind", EVENT_KIND_SETTLED))
        s_status = str(event.get("settlement_status", SETTLEMENT_STATUS_SETTLED))
        outcome = str(event.get("outcome_kind", OUTCOME_UNKNOWN_EFFECT))

        s_rec = dict(settlement_record or {})
        claims = list(event.get("effect_claims") or s_rec.get("effect_claims") or [])
        claim_refs = list(
            event.get("effect_claim_refs")
            or s_rec.get("effect_claim_refs")
            or [str(c["claim_id"]) for c in claims if isinstance(c, Mapping) and "claim_id" in c]
        )
        auth_refs = [
            str(r)
            for r in (
                event.get("authoritative_result_refs")
                or s_rec.get("authoritative_result_refs")
                or event.get("evidence_refs")
                or []
            )
        ]

        if s_status == SETTLEMENT_STATUS_CONFLICT:
            epistemic = EPISTEMIC_CONFLICTED_CLAIM
        elif s_status == SETTLEMENT_STATUS_PARTIAL or outcome == OUTCOME_PARTIAL_EFFECT:
            epistemic = EPISTEMIC_PARTIAL_FACT
        elif s_status == SETTLEMENT_STATUS_SETTLED and outcome == OUTCOME_SUCCESS_EFFECT:
            epistemic = EPISTEMIC_SETTLED_FACT
        else:
            epistemic = EPISTEMIC_UNKNOWN_EVIDENCE

        confirmed_refs = list(event.get("confirmed_effect_refs") or s_rec.get("confirmed_effect_refs") or [])
        missing_refs = list(event.get("missing_effect_refs") or s_rec.get("missing_effect_refs") or [])
        failed_refs = list(event.get("failed_effect_refs") or s_rec.get("failed_effect_refs") or [])
        conflict_refs = list(event.get("conflict_refs") or s_rec.get("conflict_refs") or [])

        merged_facts: dict[str, Any] = {
            "confirmed_effect_refs": confirmed_refs,
            "missing_effect_refs": missing_refs,
            "failed_effect_refs": failed_refs,
            "unresolved_claims": list(event.get("unresolved_claims") or s_rec.get("unresolved_claims") or []),
            "conflict_refs": conflict_refs,
            "authoritative_result_refs": auth_refs,
            "predicates": [c.get("predicate") for c in claims if isinstance(c, Mapping)],
        }
        artifact_ref: Optional[str] = None
        world_fact_ref: Optional[str] = None
        external_result_ref: Optional[str] = None

        for c in claims:
            if not isinstance(c, Mapping):
                continue
            dom = c.get("domain")
            obj_ref = c.get("object_ref")
            if c.get("epistemic_status") == "CONFIRMED" and obj_ref:
                obj_str = str(obj_ref)
                if obj_str not in confirmed_refs:
                    confirmed_refs.append(obj_str)
                if obj_str.startswith("artifact_file:"):
                    norm_file_ref = f"file:{obj_str.split(':', 1)[1]}"
                    if norm_file_ref not in confirmed_refs:
                        confirmed_refs.append(norm_file_ref)
            details = c.get("details") or {}
            if isinstance(details, Mapping):
                for dk, dv in details.items():
                    if dk not in merged_facts:
                        merged_facts[dk] = dv
            if dom == "artifact" and obj_ref and _STRUCTURED_REF_RE.fullmatch(str(obj_ref)):
                obj_str = str(obj_ref)
                artifact_ref = f"file:{obj_str.split(':', 1)[1]}" if obj_str.startswith("artifact_file:") else obj_str
                if c.get("epistemic_status") == "CONFIRMED":
                    merged_facts["artifact_verified"] = True
            elif dom == "world":
                for ar in auth_refs:
                    if ar.startswith(("world_fact:", "world:")):
                        world_fact_ref = ar
                        break

        for cr in confirmed_refs + missing_refs + failed_refs + auth_refs:
            if cr.startswith("artifact_file:"):
                artifact_ref = artifact_ref or f"file:{cr.split(':', 1)[1]}"
            elif cr.startswith(("artifact:", "file:", "artifact_rev:", "art_rev:", "aevid:")):
                artifact_ref = artifact_ref or cr
            elif cr.startswith(("world_fact:", "world:")):
                world_fact_ref = world_fact_ref or cr
            elif cr.startswith(("external_result:", "export:", "tool_result:", "tool:")):
                external_result_ref = external_result_ref or cr

        if observed_facts_override:
            merged_facts.update(dict(observed_facts_override))

        occ_at = str(event.get("occurred_at") or event.get("emitted_at") or _iso(now()))
        obs_at = str(event.get("observed_at") or occ_at)
        rec_at = str(event.get("recorded_at") or obs_at)
        pevid = f"pevid:{sha256_hex(f'{event_id}:{settlement_ref}')[:16]}"
        return cls(
            progress_evidence_id=pevid,
            subject_id=str(event.get("subject_id", GLOBAL_SUBJECT_ID)),
            activity_id=event.get("activity_ref"),
            activity_revision_hint=activity_revision_hint,
            source_kind=(
                SOURCE_KIND_CORRECTION_EVENT
                if ev_kind == EVENT_KIND_CORRECTION or event.get("supersedes_settlement_ref")
                else SOURCE_KIND_SETTLED_EVENT
            ),
            source_ref=event_id,
            settlement_ref=settlement_ref,
            supersedes_settlement_ref=event.get("supersedes_settlement_ref"),
            action_ref=str(event["action_id"]) if event.get("action_id") else None,
            effect_claim_refs=claim_refs,
            effect_claims=claims,
            settlement_status=s_status,
            outcome_kind=outcome,
            artifact_ref=artifact_ref,
            world_fact_ref=world_fact_ref,
            external_result_ref=external_result_ref,
            target_ref=event.get("target_ref") or artifact_ref or world_fact_ref or external_result_ref,
            observed_facts=merged_facts,
            occurred_at=occ_at,
            observed_at=obs_at,
            recorded_at=rec_at,
            causal_parent_refs=[settlement_ref],
            correlation_id=event.get("correlation_id"),
            authority_domain="result_settlement_authority",
            epistemic_kind=epistemic,
            idempotency_key=f"idem_pevid:{event_id}",
        )


@dataclass
class ActivityResultBinding:
    """Sections 10 & 11: Frozen correlation contract defining what real results may affect an Activity."""

    binding_id: str
    activity_id: str
    target_refs: list[str]
    allowed_evidence_kinds: list[str]
    authority_domains: list[str]
    created_at: str
    policy_version: str = POLICY_ARTIFACT_V1
    correlation_ids: list[str] = field(default_factory=list)
    schema_version: str = SCHEMA_ACTIVITY_RESULT_BINDING

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_ACTIVITY_RESULT_BINDING:
            raise ContractViolationError(f"invalid ActivityResultBinding schema_version={self.schema_version!r}")
        self.binding_id = _validate_structured_ref(
            self.binding_id,
            "binding_id",
            allowed_prefixes=frozenset({"abind", "binding"}),
        )
        if not isinstance(self.activity_id, str) or not self.activity_id.strip():
            raise ContractViolationError("activity_id must be a non-empty string")
        self.target_refs = normalize_ref_list(self.target_refs, "target_refs", allow_empty=False)
        if not self.allowed_evidence_kinds:
            raise ContractViolationError("allowed_evidence_kinds must not be empty")
        for ek in self.allowed_evidence_kinds:
            if ek not in VALID_EVIDENCE_SOURCE_KINDS:
                raise ContractViolationError(f"invalid allowed_evidence_kind={ek!r}")
        if not self.authority_domains:
            raise ContractViolationError("authority_domains must not be empty")
        for ad in self.authority_domains:
            if ad not in VALID_EVIDENCE_AUTHORITY_DOMAINS:
                raise ContractViolationError(f"invalid authority_domain={ad!r}")
        self.created_at = _require_iso(self.created_at, "created_at")
        if self.policy_version not in VALID_POLICY_VERSIONS:
            raise ContractViolationError(f"invalid policy_version={self.policy_version!r}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "binding_id": self.binding_id,
            "activity_id": self.activity_id,
            "target_refs": list(self.target_refs),
            "allowed_evidence_kinds": list(self.allowed_evidence_kinds),
            "authority_domains": list(self.authority_domains),
            "correlation_ids": list(self.correlation_ids),
            "created_at": self.created_at,
            "policy_version": self.policy_version,
        }


@dataclass
class ProgressContract:
    """Sections 16 & 53: Defines what fact changes count as legal progress and checkpoint semantics."""

    progress_contract_id: str
    activity_id: str
    progress_kind: str
    target_refs: list[str]
    checkpoint_semantics: str
    created_at: str
    policy_version: str
    revision: int = 1
    allow_checkpoint_regression: bool = False
    scale_kind: str = SCALE_KIND_NONE
    schema_version: str = SCHEMA_PROGRESS_CONTRACT

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_PROGRESS_CONTRACT:
            raise ContractViolationError(f"invalid ProgressContract schema_version={self.schema_version!r}")
        self.progress_contract_id = _validate_structured_ref(
            self.progress_contract_id,
            "progress_contract_id",
            allowed_prefixes=frozenset({"pcont", "prog_contract"}),
        )
        if not isinstance(self.activity_id, str) or not self.activity_id.strip():
            raise ContractViolationError("activity_id must be non-empty")
        if self.progress_kind not in VALID_PROGRESS_KINDS:
            raise ContractViolationError(f"invalid progress_kind={self.progress_kind!r}")
        self.target_refs = normalize_ref_list(self.target_refs, "target_refs", allow_empty=False)
        if self.checkpoint_semantics not in VALID_CHECKPOINT_SEMANTICS:
            raise ContractViolationError(f"invalid checkpoint_semantics={self.checkpoint_semantics!r}")
        if self.scale_kind not in VALID_SCALE_KINDS:
            raise ContractViolationError(f"invalid scale_kind={self.scale_kind!r}")
        if self.policy_version not in VALID_POLICY_VERSIONS:
            raise ContractViolationError(f"invalid policy_version={self.policy_version!r}")
        self.created_at = _require_iso(self.created_at, "created_at")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "progress_contract_id": self.progress_contract_id,
            "revision": self.revision,
            "activity_id": self.activity_id,
            "progress_kind": self.progress_kind,
            "target_refs": list(self.target_refs),
            "checkpoint_semantics": self.checkpoint_semantics,
            "allow_checkpoint_regression": self.allow_checkpoint_regression,
            "scale_kind": self.scale_kind,
            "created_at": self.created_at,
            "policy_version": self.policy_version,
        }


def _validate_completion_criteria_tree(
    criteria_kind: str,
    criteria: Mapping[str, Any],
    *,
    depth: int = 0,
) -> dict[str, Any]:
    """Sections 15 & 96: Enforce bounded deterministic completion criteria (depth <= 2, 0 LLM)."""
    if criteria_kind in FORBIDDEN_CRITERIA_KINDS:
        raise CompletionContractValidationError(
            f"Section 15 violation: forbidden subjective/LLM criteria_kind={criteria_kind!r}"
        )
    if criteria_kind not in VALID_CRITERIA_KINDS:
        raise CompletionContractValidationError(
            f"unsupported criteria_kind={criteria_kind!r}; allowed={sorted(VALID_CRITERIA_KINDS)}"
        )
    if depth > MAX_CRITERIA_DEPTH:
        raise CompletionContractValidationError(
            f"CompletionContract criteria depth {depth} exceeds MAX_CRITERIA_DEPTH={MAX_CRITERIA_DEPTH}"
        )
    if not isinstance(criteria, Mapping):
        raise CompletionContractValidationError("criteria must be a mapping")

    bad_keys = FORBIDDEN_CRITERIA_PARAM_KEYS.intersection(
        {str(k).strip().lower() for k in criteria.keys()}
    )
    if bad_keys:
        raise CompletionContractValidationError(
            f"forbidden subjective/LLM/eval keys in CompletionContract.criteria: {sorted(bad_keys)}"
        )

    norm = copy.deepcopy(dict(criteria))
    if criteria_kind == CRITERIA_ARTIFACT_VERSION_REACHED:
        if "min_version" not in norm or not isinstance(norm["min_version"], int) or norm["min_version"] < 1:
            raise CompletionContractValidationError(
                "ARTIFACT_VERSION_REACHED requires integer min_version >= 1"
            )
    elif criteria_kind == CRITERIA_DOCUMENT_CURSOR_REACHED:
        if "min_cursor" not in norm or not isinstance(norm["min_cursor"], int) or norm["min_cursor"] < 0:
            raise CompletionContractValidationError(
                "DOCUMENT_CURSOR_REACHED requires integer min_cursor >= 0"
            )
    elif criteria_kind == CRITERIA_PAGE_POSITION_REACHED:
        if "target_page" not in norm or not isinstance(norm["target_page"], int) or norm["target_page"] < 1:
            raise CompletionContractValidationError(
                "PAGE_POSITION_REACHED requires integer target_page >= 1"
            )
    elif criteria_kind == CRITERIA_WORLD_FACT_MATCH:
        if not norm.get("fact_key") or "expected_value" not in norm:
            raise CompletionContractValidationError(
                "WORLD_FACT_MATCH requires 'fact_key' and 'expected_value'"
            )
    elif criteria_kind in {
        CRITERIA_ACTION_RESULT_MATCH,
        CRITERIA_TOOL_RESULT_MATCH,
        CRITERIA_EXTERNAL_RESULT_MATCH,
    }:
        if not norm.get("required_predicate") and not norm.get("required_effect_ref") and not norm.get("expected_artifact_ref"):
            raise CompletionContractValidationError(
                f"{criteria_kind} requires at least one of 'required_predicate', 'required_effect_ref', or 'expected_artifact_ref'"
            )
    elif criteria_kind in COMPOSITE_CRITERIA_KINDS:
        if depth + 1 > MAX_CRITERIA_DEPTH:
            raise CompletionContractValidationError(
                f"composite criteria nesting exceeds MAX_CRITERIA_DEPTH={MAX_CRITERIA_DEPTH}"
            )
        children = norm.get("subcriteria")
        if not isinstance(children, Sequence) or isinstance(children, (str, bytes)):
            raise CompletionContractValidationError(f"{criteria_kind} requires a 'subcriteria' sequence")
        if len(children) < 1 or len(children) > MAX_CRITERIA_CHILDREN:
            raise CompletionContractValidationError(
                f"{criteria_kind} subcriteria count must be between 1 and {MAX_CRITERIA_CHILDREN}"
            )
        norm_children: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        for idx, child in enumerate(children):
            if not isinstance(child, Mapping):
                raise CompletionContractValidationError(f"subcriteria[{idx}] must be a mapping")
            cid = str(child.get("criterion_id") or f"crit_{idx}").strip()
            if not cid or cid in seen_ids:
                raise CompletionContractValidationError(f"duplicate or empty criterion_id={cid!r}")
            seen_ids.add(cid)
            ckind = str(child.get("criteria_kind") or "").strip()
            cparams = _validate_completion_criteria_tree(
                ckind,
                child.get("criteria") or {},
                depth=depth + 1,
            )
            ctarget = child.get("target_ref")
            if ctarget is not None:
                ctarget = _validate_structured_ref(
                    ctarget,
                    f"subcriteria[{idx}].target_ref",
                    error_cls=CompletionContractValidationError,
                )
            norm_children.append(
                {
                    "criterion_id": cid,
                    "criteria_kind": ckind,
                    "target_ref": ctarget,
                    "criteria": cparams,
                }
            )
        norm["subcriteria"] = norm_children
    return norm


@dataclass
class CompletionContract:
    """Sections 12-15, 50-51: Versioned deterministic completion criteria contract (`chiyo.life.completion_contract.v1`)."""

    contract_id: str
    activity_id: str
    criteria_kind: str
    target_refs: list[str]
    criteria: dict[str, Any]
    required_evidence_kinds: list[str]
    required_authority_domains: list[str]
    created_at: str
    policy_version: str
    revision: int = 1
    status: str = CONTRACT_STATUS_ACTIVE
    supersedes_contract_ref: Optional[str] = None
    superseded_by_contract_ref: Optional[str] = None
    modification_decision_ref: Optional[str] = None
    modification_reason: Optional[str] = None
    schema_version: str = SCHEMA_COMPLETION_CONTRACT

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_COMPLETION_CONTRACT:
            raise CompletionContractValidationError(
                f"invalid CompletionContract schema_version={self.schema_version!r}"
            )
        self.contract_id = _validate_structured_ref(
            self.contract_id,
            "contract_id",
            allowed_prefixes=frozenset({"ccont", "comp_contract"}),
            error_cls=CompletionContractValidationError,
        )
        if not isinstance(self.activity_id, str) or not self.activity_id.strip():
            raise CompletionContractValidationError("activity_id must be non-empty")
        if isinstance(self.revision, bool) or not isinstance(self.revision, int) or self.revision < 1:
            raise CompletionContractValidationError("revision must be >= 1")
        self.target_refs = normalize_ref_list(self.target_refs, "target_refs", allow_empty=False)
        self.criteria = _validate_completion_criteria_tree(self.criteria_kind, self.criteria)
        if not self.required_evidence_kinds:
            raise CompletionContractValidationError("required_evidence_kinds must not be empty")
        for ek in self.required_evidence_kinds:
            if ek not in VALID_EVIDENCE_SOURCE_KINDS:
                raise CompletionContractValidationError(f"invalid required_evidence_kind={ek!r}")
        if not self.required_authority_domains:
            raise CompletionContractValidationError("required_authority_domains must not be empty")
        for ad in self.required_authority_domains:
            if ad not in VALID_EVIDENCE_AUTHORITY_DOMAINS:
                raise CompletionContractValidationError(f"invalid required_authority_domain={ad!r}")
        if self.status not in VALID_CONTRACT_STATUSES:
            raise CompletionContractValidationError(f"invalid CompletionContract status={self.status!r}")
        if self.policy_version not in VALID_POLICY_VERSIONS:
            raise CompletionContractValidationError(f"invalid policy_version={self.policy_version!r}")
        self.created_at = _require_iso(self.created_at, "created_at")
        if self.revision > 1:
            if not self.supersedes_contract_ref:
                raise CompletionContractValidationError(
                    "CompletionContract revision > 1 requires supersedes_contract_ref"
                )
            if not self.modification_decision_ref:
                raise IllegalContractLoweringError(
                    "Section 50/51 violation: CompletionContract revision > 1 requires explicit modification_decision_ref"
                )
            self.modification_decision_ref = _validate_structured_ref(
                self.modification_decision_ref,
                "modification_decision_ref",
                allowed_prefixes=VALID_DECISION_PREFIXES,
                error_cls=IllegalContractLoweringError,
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "contract_id": self.contract_id,
            "revision": self.revision,
            "activity_id": self.activity_id,
            "criteria_kind": self.criteria_kind,
            "target_refs": list(self.target_refs),
            "criteria": copy.deepcopy(self.criteria),
            "required_evidence_kinds": list(self.required_evidence_kinds),
            "required_authority_domains": list(self.required_authority_domains),
            "created_at": self.created_at,
            "policy_version": self.policy_version,
            "status": self.status,
            "supersedes_contract_ref": self.supersedes_contract_ref,
            "superseded_by_contract_ref": self.superseded_by_contract_ref,
            "modification_decision_ref": self.modification_decision_ref,
            "modification_reason": self.modification_reason,
        }


@dataclass
class CheckpointUpdate:
    """Section 21: Canonical record of a checkpoint advancement or regression (`chiyo.life.checkpoint_update.v1`)."""

    update_id: str
    activity_id: str
    previous_checkpoint_ref: Optional[str]
    new_checkpoint_ref: str
    progress_state: str
    progress_evidence_refs: list[str]
    settlement_refs: list[str]
    activity_revision_before: int
    activity_revision_after: int
    occurred_at: str
    recorded_at: str
    policy_version: str
    idempotency_key: str
    provable_percentage: Optional[float] = None
    schema_version: str = SCHEMA_CHECKPOINT_UPDATE

    def __post_init__(self) -> None:
        self.update_id = _validate_structured_ref(
            self.update_id,
            "update_id",
            allowed_prefixes=frozenset({"chkup", "checkpoint_update"}),
        )
        if self.previous_checkpoint_ref is not None:
            self.previous_checkpoint_ref = validate_checkpoint_external_ref(
                self.previous_checkpoint_ref, "previous_checkpoint_ref"
            )
        self.new_checkpoint_ref = validate_checkpoint_external_ref(
            self.new_checkpoint_ref, "new_checkpoint_ref"
        )
        if self.progress_state not in VALID_PROGRESS_STATES:
            raise ContractViolationError(f"invalid progress_state={self.progress_state!r}")
        self.progress_evidence_refs = normalize_ref_list(
            self.progress_evidence_refs, "progress_evidence_refs", allow_empty=False
        )
        self.settlement_refs = normalize_ref_list(
            self.settlement_refs, "settlement_refs", allow_empty=True
        )
        self.occurred_at = _require_iso(self.occurred_at, "occurred_at")
        self.recorded_at = _require_iso(self.recorded_at, "recorded_at")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "update_id": self.update_id,
            "activity_id": self.activity_id,
            "previous_checkpoint_ref": self.previous_checkpoint_ref,
            "new_checkpoint_ref": self.new_checkpoint_ref,
            "progress_state": self.progress_state,
            "progress_evidence_refs": list(self.progress_evidence_refs),
            "settlement_refs": list(self.settlement_refs),
            "activity_revision_before": self.activity_revision_before,
            "activity_revision_after": self.activity_revision_after,
            "occurred_at": self.occurred_at,
            "recorded_at": self.recorded_at,
            "policy_version": self.policy_version,
            "idempotency_key": self.idempotency_key,
            "provable_percentage": self.provable_percentage,
        }


@dataclass
class ActivityCompletionEligibility:
    """Section 28 & 29: Canonical record that real evidence satisfies CompletionContract (`COMPLETION_ELIGIBLE != COMPLETED`)."""

    eligibility_id: str
    activity_id: str
    activity_revision: int
    completion_contract_ref: str
    completion_contract_revision: int
    evidence_refs: list[str]
    settlement_refs: list[str]
    authoritative_result_refs: list[str]
    created_at: str
    policy_version: str
    status: str = COMP_ELIG_STATUS_OPEN
    consumed_at: Optional[str] = None
    completion_transition_ref: Optional[str] = None
    schema_version: str = SCHEMA_COMPLETION_ELIGIBILITY

    def __post_init__(self) -> None:
        self.eligibility_id = _validate_structured_ref(
            self.eligibility_id,
            "eligibility_id",
            allowed_prefixes=frozenset({"celig", "comp_elig", "completion_eligibility"}),
        )
        self.completion_contract_ref = _validate_structured_ref(
            self.completion_contract_ref, "completion_contract_ref"
        )
        self.evidence_refs = normalize_ref_list(self.evidence_refs, "evidence_refs", allow_empty=False)
        self.settlement_refs = normalize_ref_list(self.settlement_refs, "settlement_refs", allow_empty=True)
        self.authoritative_result_refs = normalize_ref_list(
            self.authoritative_result_refs, "authoritative_result_refs", allow_empty=True
        )
        if self.status not in VALID_COMP_ELIG_STATUSES:
            raise ContractViolationError(f"invalid ActivityCompletionEligibility status={self.status!r}")
        self.created_at = _require_iso(self.created_at, "created_at")
        self.consumed_at = _optional_iso(self.consumed_at, "consumed_at")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "eligibility_id": self.eligibility_id,
            "activity_id": self.activity_id,
            "activity_revision": self.activity_revision,
            "completion_contract_ref": self.completion_contract_ref,
            "completion_contract_revision": self.completion_contract_revision,
            "evidence_refs": list(self.evidence_refs),
            "settlement_refs": list(self.settlement_refs),
            "authoritative_result_refs": list(self.authoritative_result_refs),
            "status": self.status,
            "created_at": self.created_at,
            "consumed_at": self.consumed_at,
            "completion_transition_ref": self.completion_transition_ref,
            "policy_version": self.policy_version,
        }


@dataclass
class CompletionReconciliationRecord:
    """Section 39 & 98: Recorded when late correction conflicts with an already COMPLETED Activity (no silent reopen)."""

    reconciliation_id: str
    activity_id: str
    completed_at_revision: int
    completion_evidence_ref: str
    conflicting_evidence_ref: str
    conflicting_settlement_ref: Optional[str]
    conflict_kind: str
    status: str
    reason_code: str
    detected_at: str
    schema_version: str = SCHEMA_COMPLETION_RECONCILIATION

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "reconciliation_id": self.reconciliation_id,
            "activity_id": self.activity_id,
            "completed_at_revision": self.completed_at_revision,
            "completion_evidence_ref": self.completion_evidence_ref,
            "conflicting_evidence_ref": self.conflicting_evidence_ref,
            "conflicting_settlement_ref": self.conflicting_settlement_ref,
            "conflict_kind": self.conflict_kind,
            "status": self.status,
            "reason_code": self.reason_code,
            "detected_at": self.detected_at,
        }


# ---------------------------------------------------------------------------
# ActivityProgressPolicyRegistry & Deterministic Evaluators (Sections 27, 42-59)
# ---------------------------------------------------------------------------


def _extract_numeric_suffix(ref: Optional[str]) -> Optional[int]:
    """Extract trailing integer version/page/cursor from structured refs like 'artifact:postcard:v18' or 'book:page:114'."""
    if not ref:
        return None
    m = re.search(r"(?:v|rev|page|cursor|slot|:)(\d+)$", str(ref))
    if m:
        return int(m.group(1))
    return None


class NaturalProgressEvaluator:
    """Deterministic progress reducer for an Activity given its ProgressContract and matched evidences (0 LLM)."""

    @staticmethod
    def evaluate_progress(
        *,
        activity: Mapping[str, Any],
        progress_contract: Optional[Mapping[str, Any]],
        evidence: LifeProgressEvidence,
        prior_checkpoint_ref: Optional[str],
        active_evidences: Sequence[Mapping[str, Any]],
    ) -> dict[str, Any]:
        """Return {'progress_state', 'new_checkpoint_ref', 'policy_version', 'confirmed_sub_refs', 'reason'}."""
        policy_ver = (
            str(progress_contract["policy_version"])
            if progress_contract
            else POLICY_OPEN_ENDED_V1
        )
        semantics = (
            str(progress_contract["checkpoint_semantics"])
            if progress_contract
            else CHECKPOINT_SEMANTICS_TARGET_STATE
        )
        allow_reg = (
            bool(progress_contract.get("allow_checkpoint_regression", False))
            if progress_contract
            else False
        )
        facts = evidence.observed_facts

        # 1. Conflict check (Section 35, G03)
        if (
            evidence.epistemic_kind == EPISTEMIC_CONFLICTED_CLAIM
            or evidence.settlement_status == SETTLEMENT_STATUS_CONFLICT
            or facts.get("conflict_refs")
            or facts.get("hash_mismatch") is True
            or facts.get("world_mutation_absent") is True
        ):
            return {
                "progress_state": PROGRESS_STATE_BLOCKED,
                "new_checkpoint_ref": prior_checkpoint_ref,
                "policy_version": policy_ver,
                "confirmed_sub_refs": [],
                "reason": "CONFLICT_BLOCKS_PROGRESS",
            }

        # 2. UNKNOWN / UNRESOLVED check (Section 36, B03, F04)
        if (
            evidence.epistemic_kind == EPISTEMIC_UNKNOWN_EVIDENCE
            or evidence.settlement_status in {SETTLEMENT_STATUS_UNRESOLVED, SETTLEMENT_STATUS_OPEN}
            or evidence.outcome_kind in {OUTCOME_UNKNOWN_EFFECT, OUTCOME_FAILURE_NO_EFFECT, OUTCOME_NO_OP}
        ):
            # Special case: tool exit 0 (SETTLEMENT_STATUS_OPEN) where only process exit 0 is known,
            # not business outcome verified (Section 47, F01):
            if evidence.settlement_status == SETTLEMENT_STATUS_OPEN and "tool_process_succeeded" in (facts.get("predicates") or []):
                return {
                    "progress_state": PROGRESS_STATE_UNKNOWN if prior_checkpoint_ref is None else PROGRESS_STATE_UNCHANGED,
                    "new_checkpoint_ref": prior_checkpoint_ref,
                    "policy_version": policy_ver,
                    "confirmed_sub_refs": [],
                    "reason": "PROCESS_EXIT_0_WITHOUT_BUSINESS_EVIDENCE",
                }
            return {
                "progress_state": PROGRESS_STATE_UNKNOWN if prior_checkpoint_ref is None else PROGRESS_STATE_UNCHANGED,
                "new_checkpoint_ref": prior_checkpoint_ref,
                "policy_version": policy_ver,
                "confirmed_sub_refs": [],
                "reason": "UNKNOWN_OR_NO_EFFECT_EVIDENCE",
            }

        # 3. PARTIAL_EFFECT (Section 34, G01, G02)
        if (
            evidence.epistemic_kind == EPISTEMIC_PARTIAL_FACT
            or evidence.settlement_status == SETTLEMENT_STATUS_PARTIAL
            or evidence.outcome_kind == OUTCOME_PARTIAL_EFFECT
        ):
            confirmed_refs = [
                str(r)
                for r in (facts.get("confirmed_effects") or facts.get("confirmed_effect_refs") or [])
                if isinstance(r, str) and _STRUCTURED_REF_RE.fullmatch(r)
            ]
            candidate_chk = (
                facts.get("checkpoint_ref")
                or (confirmed_refs[-1] if confirmed_refs else None)
                or evidence.artifact_ref
                or evidence.world_fact_ref
                or evidence.settlement_ref
                or evidence.source_ref
            )
            candidate_chk = validate_checkpoint_external_ref(candidate_chk)
            if candidate_chk == prior_checkpoint_ref:
                return {
                    "progress_state": PROGRESS_STATE_UNCHANGED,
                    "new_checkpoint_ref": prior_checkpoint_ref,
                    "policy_version": policy_ver,
                    "confirmed_sub_refs": confirmed_refs,
                    "reason": "PARTIAL_CHECKPOINT_UNCHANGED",
                }
            return {
                "progress_state": PROGRESS_STATE_ADVANCED,
                "new_checkpoint_ref": candidate_chk,
                "policy_version": policy_ver,
                "confirmed_sub_refs": confirmed_refs,
                "reason": "PARTIAL_CONFIRMED_EFFECT_ADVANCED",
            }

        # 4. Determine candidate checkpoint_ref from canonical evidence (Section 20)
        candidate_chk: Optional[str] = None
        if facts.get("checkpoint_ref"):
            candidate_chk = str(facts["checkpoint_ref"])
        elif facts.get("artifact_version") is not None:
            base_art = evidence.artifact_ref or evidence.target_ref or "artifact:item"
            candidate_chk = f"{base_art}:v{int(facts['artifact_version'])}"
        elif facts.get("page") is not None:
            base_book = evidence.target_ref or "book:reading"
            candidate_chk = f"{base_book}:page:{int(facts['page'])}"
        elif facts.get("cursor_position") is not None:
            base_doc = evidence.target_ref or "document:doc"
            candidate_chk = f"{base_doc}:cursor:{int(facts['cursor_position'])}"
        elif facts.get("world_location") is not None:
            candidate_chk = f"world:location:{facts['world_location']}"
        elif evidence.artifact_ref:
            candidate_chk = evidence.artifact_ref
        elif evidence.world_fact_ref:
            candidate_chk = evidence.world_fact_ref
        elif evidence.external_result_ref:
            candidate_chk = evidence.external_result_ref
        elif evidence.settlement_ref:
            candidate_chk = f"settlement:{evidence.settlement_ref.split(':', 1)[1]}"
        else:
            candidate_chk = evidence.source_ref

        candidate_chk = validate_checkpoint_external_ref(candidate_chk)

        # 5. Compare against prior_checkpoint_ref according to checkpoint_semantics (Sections 52, 53, 59)
        if prior_checkpoint_ref is None:
            return {
                "progress_state": PROGRESS_STATE_ADVANCED,
                "new_checkpoint_ref": candidate_chk,
                "policy_version": policy_ver,
                "confirmed_sub_refs": [candidate_chk],
                "reason": "INITIAL_CHECKPOINT_ESTABLISHED",
            }

        if candidate_chk == prior_checkpoint_ref:
            return {
                "progress_state": PROGRESS_STATE_UNCHANGED,
                "new_checkpoint_ref": prior_checkpoint_ref,
                "policy_version": policy_ver,
                "confirmed_sub_refs": [candidate_chk],
                "reason": "CHECKPOINT_IDENTICAL",
            }

        prev_num = _extract_numeric_suffix(prior_checkpoint_ref)
        new_num = _extract_numeric_suffix(candidate_chk)

        if prev_num is not None and new_num is not None:
            if new_num < prev_num:
                # Section 52, 53, 59: Is this an out-of-order stale observation or an intentional position regression?
                # If evidence occurred_at is older than the latest consumed evidence occurred_at, it is out-of-order!
                latest_occ = max(
                    (str(e.get("occurred_at", "")) for e in active_evidences),
                    default="",
                )
                is_out_of_order = bool(latest_occ and evidence.occurred_at < latest_occ)

                if semantics in {
                    CHECKPOINT_SEMANTICS_LATEST_ARTIFACT_VERSION,
                    CHECKPOINT_SEMANTICS_MAX_CONFIRMED_POSITION,
                }:
                    if allow_reg and not is_out_of_order:
                        return {
                            "progress_state": PROGRESS_STATE_REGRESSED,
                            "new_checkpoint_ref": candidate_chk,
                            "policy_version": policy_ver,
                            "confirmed_sub_refs": [candidate_chk],
                            "reason": "EXPLICIT_ROLLBACK_REGRESSED",
                        }
                    # Out-of-order or monotonic policy -> ignore lower version/position (Section 59)
                    return {
                        "progress_state": PROGRESS_STATE_UNCHANGED,
                        "new_checkpoint_ref": prior_checkpoint_ref,
                        "policy_version": policy_ver,
                        "confirmed_sub_refs": [],
                        "reason": "OUT_OF_ORDER_LOWER_VERSION_IGNORED",
                    }

                if semantics == CHECKPOINT_SEMANTICS_LATEST_POSITION:
                    if is_out_of_order and not allow_reg:
                        return {
                            "progress_state": PROGRESS_STATE_UNCHANGED,
                            "new_checkpoint_ref": prior_checkpoint_ref,
                            "policy_version": policy_ver,
                            "confirmed_sub_refs": [],
                            "reason": "OUT_OF_ORDER_POSITION_IGNORED",
                        }
                    return {
                        "progress_state": PROGRESS_STATE_REGRESSED,
                        "new_checkpoint_ref": candidate_chk,
                        "policy_version": policy_ver,
                        "confirmed_sub_refs": [candidate_chk],
                        "reason": "POSITION_REGRESSED",
                    }

        return {
            "progress_state": PROGRESS_STATE_ADVANCED,
            "new_checkpoint_ref": candidate_chk,
            "policy_version": policy_ver,
            "confirmed_sub_refs": [candidate_chk],
            "reason": "CHECKPOINT_ADVANCED",
        }


class NaturalCompletionEvaluator:
    """Section 27: Pure deterministic evaluator checking whether CompletionContract is satisfied (0 LLM)."""

    @classmethod
    def evaluate_completion(
        cls,
        *,
        activity: Mapping[str, Any],
        completion_contract: Optional[Mapping[str, Any]],
        active_evidences: Sequence[Mapping[str, Any]],
        current_checkpoint_ref: Optional[str],
        expected_activity_revision: Optional[int] = None,
    ) -> dict[str, Any]:
        """Return {'eval_status', 'satisfying_evidence_refs', 'settlement_refs', 'authoritative_result_refs', 'reason'}."""
        # Section 57: Activity Revision Guard
        if (
            expected_activity_revision is not None
            and int(activity["revision"]) != int(expected_activity_revision)
        ):
            return {
                "eval_status": EVAL_STALE,
                "satisfying_evidence_refs": [],
                "settlement_refs": [],
                "authoritative_result_refs": [],
                "reason": f"STALE_ACTIVITY_REVISION:expected={expected_activity_revision},actual={activity['revision']}",
            }

        # Section 12 & 41: Without CompletionContract, Activity can NEVER auto-complete
        if completion_contract is None or completion_contract.get("status") != CONTRACT_STATUS_ACTIVE:
            return {
                "eval_status": EVAL_NOT_COMPLETE,
                "satisfying_evidence_refs": [],
                "settlement_refs": [],
                "authoritative_result_refs": [],
                "reason": "NO_AUTOMATIC_COMPLETION_WITHOUT_CONTRACT",
            }

        if not active_evidences:
            return {
                "eval_status": EVAL_INSUFFICIENT_EVIDENCE,
                "satisfying_evidence_refs": [],
                "settlement_refs": [],
                "authoritative_result_refs": [],
                "reason": "NO_ACTIVE_EVIDENCE",
            }

        # Filter evidences that match contract required_evidence_kinds and required_authority_domains
        req_kinds = set(completion_contract.get("required_evidence_kinds") or [])
        req_auths = set(completion_contract.get("required_authority_domains") or [])

        qualifying_evs = [
            ev
            for ev in active_evidences
            if (not req_kinds or ev.get("source_kind") in req_kinds)
            and (not req_auths or ev.get("authority_domain") in req_auths)
        ]

        # Check if any qualifying evidence is in active unresolved conflict
        conflicted_evs = [
            ev
            for ev in qualifying_evs
            if ev.get("epistemic_kind") == EPISTEMIC_CONFLICTED_CLAIM
            or ev.get("settlement_status") == SETTLEMENT_STATUS_CONFLICT
            or (ev.get("observed_facts") or {}).get("hash_mismatch") is True
            or (ev.get("observed_facts") or {}).get("world_mutation_absent") is True
        ]
        if conflicted_evs:
            return {
                "eval_status": EVAL_CONFLICT,
                "satisfying_evidence_refs": [str(e["progress_evidence_id"]) for e in conflicted_evs],
                "settlement_refs": [
                    str(e["settlement_ref"]) for e in conflicted_evs if e.get("settlement_ref")
                ],
                "authoritative_result_refs": [],
                "reason": "CONFLICTING_EVIDENCE_BLOCKS_COMPLETION",
            }

        # Only SETTLED_FACT or CANONICAL_OBSERVATION or confirmed PARTIAL_FACT can satisfy completion criteria
        verified_evs = [
            ev
            for ev in qualifying_evs
            if ev.get("epistemic_kind")
            in {EPISTEMIC_SETTLED_FACT, EPISTEMIC_CANONICAL_OBSERVATION, EPISTEMIC_PARTIAL_FACT}
            and ev.get("settlement_status") not in {SETTLEMENT_STATUS_OPEN, SETTLEMENT_STATUS_UNRESOLVED, SETTLEMENT_STATUS_CONFLICT}
            and ev.get("outcome_kind") not in {OUTCOME_FAILURE_NO_EFFECT, OUTCOME_UNKNOWN_EFFECT, OUTCOME_NO_OP}
        ]
        if not verified_evs:
            return {
                "eval_status": EVAL_INSUFFICIENT_EVIDENCE,
                "satisfying_evidence_refs": [],
                "settlement_refs": [],
                "authoritative_result_refs": [],
                "reason": "NO_VERIFIED_SETTLED_OR_CANONICAL_EVIDENCE",
            }

        matched, matched_evs, reason = cls._eval_criterion_node(
            criteria_kind=str(completion_contract["criteria_kind"]),
            criteria=completion_contract["criteria"],
            target_refs=list(completion_contract.get("target_refs") or []),
            verified_evs=verified_evs,
        )
        if not matched:
            return {
                "eval_status": EVAL_NOT_COMPLETE,
                "satisfying_evidence_refs": [],
                "settlement_refs": [],
                "authoritative_result_refs": [],
                "reason": reason,
            }

        ev_refs = list(dict.fromkeys(str(e["progress_evidence_id"]) for e in matched_evs))
        set_refs = list(
            dict.fromkeys(str(e["settlement_ref"]) for e in matched_evs if e.get("settlement_ref"))
        )
        auth_refs: list[str] = []
        for e in matched_evs:
            facts = e.get("observed_facts") or {}
            for ar in facts.get("authoritative_result_refs") or []:
                if isinstance(ar, str) and _STRUCTURED_REF_RE.fullmatch(ar) and ar not in auth_refs:
                    auth_refs.append(ar)
            for ref_field in (
                "artifact_ref",
                "world_fact_ref",
                "external_result_ref",
                "settlement_ref",
                "source_ref",
                "target_ref",
                "progress_evidence_id",
            ):
                val = e.get(ref_field)
                if isinstance(val, str) and val in VALID_EVIDENCE_PREFIXES or (
                    isinstance(val, str) and val.split(":", 1)[0] in VALID_EVIDENCE_PREFIXES
                ):
                    if val not in auth_refs:
                        auth_refs.append(val)

        if not ev_refs or not auth_refs:
            return {
                "eval_status": EVAL_INSUFFICIENT_EVIDENCE,
                "satisfying_evidence_refs": ev_refs,
                "settlement_refs": set_refs,
                "authoritative_result_refs": auth_refs,
                "reason": "MISSING_COMPLETION_EVIDENCE_REF",
            }

        return {
            "eval_status": EVAL_COMPLETION_ELIGIBLE,
            "satisfying_evidence_refs": ev_refs,
            "settlement_refs": set_refs,
            "authoritative_result_refs": auth_refs,
            "reason": "COMPLETION_CRITERIA_SATISFIED",
        }

    @classmethod
    def _eval_criterion_node(
        cls,
        *,
        criteria_kind: str,
        criteria: Mapping[str, Any],
        target_refs: Sequence[str],
        verified_evs: Sequence[Mapping[str, Any]],
    ) -> tuple[bool, list[Mapping[str, Any]], str]:
        if criteria_kind == CRITERIA_ARTIFACT_VERSION_REACHED:
            min_ver = int(criteria["min_version"])
            req_hash = criteria.get("expected_hash")
            req_verified = bool(criteria.get("require_verified_hash", True))
            # Use the latest active evidence for the target artifact
            matching: list[Mapping[str, Any]] = []
            for ev in verified_evs:
                facts = ev.get("observed_facts") or {}
                if facts.get("hash_mismatch") is True or facts.get("verified") is False:
                    continue
                if req_verified and facts.get("verified") is False:
                    continue
                if req_hash is not None and facts.get("observed_hash") != req_hash and facts.get("verified_hash") != req_hash:
                    continue
                ver = facts.get("artifact_version")
                if ver is None:
                    ver = _extract_numeric_suffix(ev.get("artifact_ref") or facts.get("checkpoint_ref"))
                if isinstance(ver, int) and ver >= min_ver:
                    # Also ensure artifact_verified / file_created / file_updated or canonical artifact authority
                    preds = set(facts.get("predicates") or [])
                    if (
                        ev.get("authority_domain") == "artifact_authority"
                        or "file_hash_matched" in preds
                        or "file_created" in preds
                        or "file_updated" in preds
                        or "business_artifact_verified" in preds
                        or facts.get("artifact_verified") is True
                    ):
                        matching.append(ev)
            if matching:
                return True, [matching[-1]], "ARTIFACT_VERSION_SATISFIED"
            return False, [], f"ARTIFACT_VERSION_BELOW_{min_ver}_OR_UNVERIFIED"

        if criteria_kind == CRITERIA_PAGE_POSITION_REACHED:
            target_page = int(criteria["target_page"])
            req_book = criteria.get("book_ref") or (target_refs[0] if target_refs else None)
            # Evaluate against latest position if LATEST_POSITION or any verified if MAX
            matching = []
            for ev in verified_evs:
                facts = ev.get("observed_facts") or {}
                ev_target = ev.get("target_ref") or facts.get("book_ref")
                if req_book and ev_target and ev_target != req_book:
                    continue
                page = facts.get("page")
                if page is None:
                    page = _extract_numeric_suffix(facts.get("checkpoint_ref") or ev.get("source_ref"))
                if isinstance(page, int) and page >= target_page:
                    matching.append(ev)
            if matching:
                return True, [matching[-1]], "PAGE_POSITION_SATISFIED"
            return False, [], f"PAGE_POSITION_BELOW_{target_page}"

        if criteria_kind == CRITERIA_DOCUMENT_CURSOR_REACHED:
            min_cursor = int(criteria["min_cursor"])
            req_chapter = criteria.get("chapter")
            matching = []
            for ev in verified_evs:
                facts = ev.get("observed_facts") or {}
                if req_chapter and facts.get("chapter") != req_chapter:
                    continue
                cursor = facts.get("cursor_position")
                if cursor is None:
                    cursor = _extract_numeric_suffix(facts.get("checkpoint_ref") or ev.get("source_ref"))
                if isinstance(cursor, int) and cursor >= min_cursor:
                    matching.append(ev)
            if matching:
                return True, [matching[-1]], "DOCUMENT_CURSOR_SATISFIED"
            return False, [], f"DOCUMENT_CURSOR_BELOW_{min_cursor}"

        if criteria_kind == CRITERIA_WORLD_FACT_MATCH:
            fact_key = str(criteria["fact_key"])
            expected_val = criteria["expected_value"]
            # Must look at the LATEST authoritative world fact for fact_key!
            world_evs: list[Mapping[str, Any]] = []
            for ev in verified_evs:
                facts = ev.get("observed_facts") or {}
                preds = set(facts.get("predicates") or [])
                # Section 45 & 48: Move Action success alone cannot satisfy WORLD_FACT_MATCH;
                # requires authoritative world fact (world_authority or world_state_mutated with matching fact_key=expected_val)
                has_world_auth = (
                    ev.get("authority_domain") == "world_authority"
                    or "world_state_mutated" in preds
                    or bool(ev.get("world_fact_ref"))
                )
                if not has_world_auth:
                    continue
                if fact_key in facts:
                    world_evs.append(ev)
            if not world_evs:
                return False, [], f"WORLD_FACT_{fact_key}_MISSING"
            latest_wev = world_evs[-1]
            actual_val = (latest_wev.get("observed_facts") or {}).get(fact_key)
            if actual_val == expected_val:
                return True, [latest_wev], "WORLD_FACT_MATCHED"
            return False, [], f"WORLD_FACT_MISMATCH:expected={expected_val!r},actual={actual_val!r}"

        if criteria_kind in {
            CRITERIA_ACTION_RESULT_MATCH,
            CRITERIA_TOOL_RESULT_MATCH,
            CRITERIA_EXTERNAL_RESULT_MATCH,
        }:
            req_pred = criteria.get("required_predicate")
            req_effect = criteria.get("required_effect_ref")
            exp_art = criteria.get("expected_artifact_ref")
            matching = []
            for ev in verified_evs:
                # Section 34: PARTIAL_EFFECT cannot over-complete a single full-result criterion unless req_effect is in confirmed_effects
                if ev.get("epistemic_kind") == EPISTEMIC_PARTIAL_FACT and not req_effect:
                    continue
                facts = ev.get("observed_facts") or {}
                preds = set(facts.get("predicates") or [])
                confirmed_refs = set(
                    (facts.get("confirmed_effects") or [])
                    + (facts.get("confirmed_effect_refs") or [])
                )
                if req_pred and req_pred not in preds:
                    continue
                if req_effect and req_effect not in confirmed_refs and ev.get("external_result_ref") != req_effect:
                    continue
                if exp_art:
                    if ev.get("artifact_ref") != exp_art and facts.get("artifact_ref") != exp_art:
                        continue
                    # Section 47: Tool exit 0 alone is insufficient when expected_artifact_ref is required;
                    # must have verified artifact evidence!
                    if (
                        "business_artifact_verified" not in preds
                        and "file_created" not in preds
                        and "file_hash_matched" not in preds
                        and ev.get("authority_domain") not in {"artifact_authority", "external_result_authority"}
                        and facts.get("artifact_verified") is not True
                    ):
                        continue
                matching.append(ev)
            if matching:
                return True, [matching[-1]], f"{criteria_kind}_SATISFIED"
            return False, [], f"{criteria_kind}_UNSATISFIED"

        if criteria_kind == CRITERIA_ALL_OF:
            subcriteria = criteria.get("subcriteria") or []
            collected: list[Mapping[str, Any]] = []
            for sub in subcriteria:
                sub_ok, sub_evs, sub_reason = cls._eval_criterion_node(
                    criteria_kind=str(sub["criteria_kind"]),
                    criteria=sub["criteria"],
                    target_refs=[sub["target_ref"]] if sub.get("target_ref") else target_refs,
                    verified_evs=verified_evs,
                )
                if not sub_ok:
                    return False, [], f"ALL_OF_UNSATISFIED:{sub['criterion_id']}:{sub_reason}"
                collected.extend(sub_evs)
            return True, collected, "ALL_OF_SATISFIED"

        if criteria_kind == CRITERIA_ANY_OF:
            subcriteria = criteria.get("subcriteria") or []
            for sub in subcriteria:
                sub_ok, sub_evs, _ = cls._eval_criterion_node(
                    criteria_kind=str(sub["criteria_kind"]),
                    criteria=sub["criteria"],
                    target_refs=[sub["target_ref"]] if sub.get("target_ref") else target_refs,
                    verified_evs=verified_evs,
                )
                if sub_ok:
                    return True, sub_evs, f"ANY_OF_SATISFIED:{sub['criterion_id']}"
            return False, [], "ANY_OF_NONE_SATISFIED"

        return False, [], f"UNSUPPORTED_CRITERIA_KIND:{criteria_kind}"


class ActivityProgressPolicyRegistry:
    """Sections 55 & 56: Versioned registry of deterministic evidence matchers, reducers, and completion evaluators."""

    def __init__(self) -> None:
        self._policies: dict[str, dict[str, Any]] = {
            POLICY_ARTIFACT_V1: {
                "policy_version": POLICY_ARTIFACT_V1,
                "progress_kind": PROGRESS_KIND_ARTIFACT_EDIT,
                "default_checkpoint_semantics": CHECKPOINT_SEMANTICS_LATEST_ARTIFACT_VERSION,
            },
            POLICY_POSITION_V1: {
                "policy_version": POLICY_POSITION_V1,
                "progress_kind": PROGRESS_KIND_POSITIONAL_READING,
                "default_checkpoint_semantics": CHECKPOINT_SEMANTICS_LATEST_POSITION,
            },
            POLICY_WORLD_FACT_V1: {
                "policy_version": POLICY_WORLD_FACT_V1,
                "progress_kind": PROGRESS_KIND_WORLD_ACTION_BOUNDED,
                "default_checkpoint_semantics": CHECKPOINT_SEMANTICS_TARGET_STATE,
            },
            POLICY_EXTERNAL_RESULT_V1: {
                "policy_version": POLICY_EXTERNAL_RESULT_V1,
                "progress_kind": PROGRESS_KIND_WAIT_FOR_EXTERNAL_RESULT,
                "default_checkpoint_semantics": CHECKPOINT_SEMANTICS_TARGET_STATE,
            },
            POLICY_COMPOSITE_V1: {
                "policy_version": POLICY_COMPOSITE_V1,
                "progress_kind": PROGRESS_KIND_MULTI_ACTION_CHECKLIST,
                "default_checkpoint_semantics": CHECKPOINT_SEMANTICS_TARGET_STATE,
            },
            POLICY_OPEN_ENDED_V1: {
                "policy_version": POLICY_OPEN_ENDED_V1,
                "progress_kind": PROGRESS_KIND_OPEN_ENDED,
                "default_checkpoint_semantics": CHECKPOINT_SEMANTICS_TARGET_STATE,
            },
        }

    def get_policy(self, policy_version: str) -> dict[str, Any]:
        if policy_version not in self._policies:
            raise ContractViolationError(f"unregistered policy_version={policy_version!r}")
        return copy.deepcopy(self._policies[policy_version])

    @staticmethod
    def match_evidence_to_binding(
        *,
        evidence: LifeProgressEvidence,
        binding: Mapping[str, Any],
    ) -> tuple[bool, str]:
        """Sections 9-11: Verify whether evidence is legitimately correlated and allowed by ActivityResultBinding."""
        act_id = str(binding["activity_id"])
        allowed_kinds = set(binding.get("allowed_evidence_kinds") or [])
        allowed_auths = set(binding.get("authority_domains") or [])
        target_refs = set(binding.get("target_refs") or [])
        corr_ids = set(binding.get("correlation_ids") or [])

        if evidence.source_kind not in allowed_kinds:
            return False, "SOURCE_KIND_NOT_ALLOWED_BY_BINDING"
        if evidence.authority_domain not in allowed_auths:
            return False, "AUTHORITY_DOMAIN_NOT_ALLOWED_BY_BINDING"

        # Correlation check:
        # 1. Explicit activity_id on evidence must match binding.activity_id if present
        if evidence.activity_id is not None and evidence.activity_id != act_id:
            return False, "ACTIVITY_ID_MISMATCH"

        # 2. Target ref check: at least one target_ref / artifact_ref / world_fact_ref / external_result_ref
        # or correlation_id must match the binding!
        ev_targets = {
            r
            for r in (
                evidence.target_ref,
                evidence.artifact_ref,
                evidence.world_fact_ref,
                evidence.external_result_ref,
                (evidence.observed_facts or {}).get("target_ref"),
                (evidence.observed_facts or {}).get("book_ref"),
            )
            if isinstance(r, str)
        }
        target_matched = bool(ev_targets.intersection(target_refs))
        # Also allow prefix-base match if binding target_ref is 'artifact:postcard' and evidence artifact_ref is 'artifact:postcard:v18'
        if not target_matched and ev_targets:
            for et in ev_targets:
                for bt in target_refs:
                    if et == bt or et.startswith(bt + ":"):
                        target_matched = True
                        break

        corr_matched = bool(evidence.correlation_id and evidence.correlation_id in corr_ids)

        # If evidence has NO explicit activity_id, NO correlation_id match, and NO explicit binding target match -> UNBOUND!
        if evidence.activity_id is None and not corr_matched:
            # Section 9: Even if a file changes, unless the evidence is explicitly bound by target_ref AND
            # the binding allows canonical target observation, check if target_matched and source is non-action observation
            if not target_matched:
                return False, "UNBOUND_EVIDENCE"
            if evidence.action_ref is not None:
                # Action result without activity_ref or correlation_id cannot be guessed onto an Activity!
                return False, "UNBOUND_ACTION_RESULT_EVIDENCE"

        if ev_targets and not target_matched:
            return False, "IRRELEVANT_TARGET_REF"

        if not target_matched and not corr_matched:
            return False, "NO_TARGET_OR_CORRELATION_MATCH"

        return True, "BOUND_AND_MATCHED"


# ---------------------------------------------------------------------------
# ProgressStore & ActivityProgressJournal (Sections 61, 70-76)
# ---------------------------------------------------------------------------


class ProgressStore:
    """Crash-safe WAL + hash-chained ActivityProgressJournal + state store (`activity_progress_state.json`)."""

    STATE_FILENAME = "activity_progress_state.json"
    JOURNAL_FILENAME = "activity_progress_journal.jsonl"
    WAL_FILENAME = "activity_progress.wal"

    def __init__(self, store_root: Path | str, *, namespace: str = NAMESPACE_ISOLATED_TEST):
        self.store_root = Path(store_root).expanduser().resolve()
        self.namespace = namespace
        self.state_path = self.store_root / self.STATE_FILENAME
        self.journal_path = self.store_root / self.JOURNAL_FILENAME
        self.wal_path = self.store_root / self.WAL_FILENAME
        self._verified_cache: Optional[dict[str, Any]] = None
        self._dirty_snapshot: Optional[dict[str, Any]] = None

    def _empty_state(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_PROGRESS_STATE,
            "subject_id": GLOBAL_SUBJECT_ID,
            "namespace": self.namespace,
            "revision": 0,
            "last_entry_hash": GENESIS_HASH,
            "updated_at": _iso(now()),
            "bindings_by_activity": {},
            "progress_contracts_by_activity": {},
            "completion_contracts_by_activity": {},
            "completion_contract_history_by_activity": {},
            "consumed_evidences_by_id": {},
            "pending_evidence_eval_by_id": {},
            "evidence_id_by_idempotency_key": {},
            "active_evidences_by_activity": {},
            "superseded_settlement_refs_by_activity": {},
            "progress_state_by_activity": {},
            "checkpoint_updates_by_activity": {},
            "pending_checkpoint_sync_by_activity": {},
            "completion_eligibilities_by_id": {},
            "open_eligibility_by_activity": {},
            "completed_once_by_activity": {},
            "reconciliations_by_activity": {},
            "unbound_evidence_log": [],
            "late_terminal_evidence_log": [],
        }

    def _disk_signature(self) -> Optional[tuple[int, int, int, int]]:
        if not self.state_path.exists() or not self.journal_path.exists():
            return None
        try:
            st_s = self.state_path.stat()
            st_j = self.journal_path.stat()
            return (st_s.st_mtime_ns, st_s.st_size, st_j.st_mtime_ns, st_j.st_size)
        except OSError:
            return None

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
            sig = self._disk_signature()
            if sig is not None:
                self._verified_cache = {"sig": sig, "state": state_to_write}
        finally:
            if state_tmp.exists():
                state_tmp.unlink(missing_ok=True)

    def load_state(self, *, _mutable: bool = False) -> dict[str, Any]:
        self.store_root.mkdir(parents=True, exist_ok=True)
        if self.wal_path.exists():
            self._dirty_snapshot = None
            self._verified_cache = None
            self.recover_from_wal_if_needed()

        if self._dirty_snapshot is not None:
            return self._dirty_snapshot if _mutable else copy.deepcopy(self._dirty_snapshot)

        sig = self._disk_signature()
        if (
            self._verified_cache is not None
            and sig is not None
            and self._verified_cache.get("sig") == sig
        ):
            cached = self._verified_cache["state"]
            return cached if _mutable else copy.deepcopy(cached)

        if not self.state_path.exists() and not self.journal_path.exists():
            empty = self._empty_state()
            return empty if _mutable else copy.deepcopy(empty)

        entries = self.read_journal()
        if not self.state_path.exists():
            rebuilt = self.rebuild_state_from_journal(entries)
            self._write_snapshot_sync(rebuilt)
            return rebuilt if _mutable else copy.deepcopy(rebuilt)

        try:
            state = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise RecoveryRequiredError(
                "CORRUPTED_PROGRESS_STATE",
                f"activity_progress_state.json is corrupted: {exc}",
            ) from exc

        if state.get("schema_version") != SCHEMA_PROGRESS_STATE:
            raise RecoveryRequiredError("INVALID_PROGRESS_STATE_SCHEMA", "invalid progress state schema")
        validate_subject_id(state.get("subject_id"))

        if state.get("revision") != len(entries):
            rebuilt = self.rebuild_state_from_journal(entries)
            self._write_snapshot_sync(rebuilt)
            state = rebuilt

        new_sig = self._disk_signature()
        if new_sig is not None:
            self._verified_cache = {"sig": new_sig, "state": state}
        return state if _mutable else copy.deepcopy(state)

    def _write_snapshot_sync(self, state_obj: dict[str, Any]) -> None:
        payload = json.dumps(state_obj, ensure_ascii=False, separators=(",", ":")) + "\n"
        tmp = self.store_root / f".{self.STATE_FILENAME}.tmp.{os.getpid()}.{uuid.uuid4().hex}"
        try:
            with tmp.open("w", encoding="utf-8") as sh:
                sh.write(payload)
                sh.flush()
                os.fsync(sh.fileno())
            os.replace(tmp, self.state_path)
            _fsync_dir(self.store_root)
        finally:
            if tmp.exists():
                tmp.unlink(missing_ok=True)

    def read_journal(self) -> list[dict[str, Any]]:
        self.flush_snapshot()
        if not self.journal_path.exists():
            return []
        raw = self.journal_path.read_text(encoding="utf-8")
        if raw and not raw.endswith("\n"):
            raise RecoveryRequiredError(
                "INCOMPLETE_PROGRESS_JOURNAL_TAIL",
                "activity_progress_journal.jsonl does not end with newline",
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
                    "CORRUPTED_PROGRESS_JOURNAL_JSON",
                    f"invalid JSON at line {idx}: {exc}",
                ) from exc
            if not isinstance(item, dict) or item.get("schema_version") != SCHEMA_PROGRESS_JOURNAL_ENTRY:
                raise RecoveryRequiredError(
                    "INVALID_PROGRESS_JOURNAL_SCHEMA",
                    f"invalid schema at line {idx}",
                )
            if item.get("store_revision") != expected_rev:
                raise RecoveryRequiredError(
                    "PROGRESS_JOURNAL_REVISION_GAP",
                    f"expected store_revision={expected_rev}, got {item.get('store_revision')}",
                )
            if item.get("prev_entry_hash") != prev_hash:
                raise RecoveryRequiredError(
                    "PROGRESS_JOURNAL_HASH_CHAIN_BROKEN",
                    f"broken hash chain at line {idx}",
                )
            rec_hash = item.get("entry_hash")
            unsigned = dict(item)
            unsigned.pop("entry_hash", None)
            if rec_hash != sha256_hex(canonical_json_line(unsigned)):
                raise RecoveryRequiredError(
                    "PROGRESS_JOURNAL_ENTRY_TAMPERED",
                    f"entry_hash mismatch at line {idx}",
                )
            prev_hash = rec_hash
            expected_rev += 1
            entries.append(item)
        return entries

    def rebuild_state_from_journal(self, entries: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        state = self._empty_state()
        if not entries:
            return state
        for entry in entries:
            delta = entry.get("state_delta") or {}
            act_id = delta.get("activity_id")
            if act_id is not None:
                for k, v in (delta.get("act_delta") or {}).items():
                    if v == "__ABSENT__":
                        state[k].pop(act_id, None)
                    else:
                        state[k][act_id] = copy.deepcopy(v)
            pevid = delta.get("progress_evidence_id")
            if pevid is not None:
                ev_d = delta.get("ev_delta") or {}
                if ev_d.get("consumed", "__ABSENT__") == "__ABSENT__":
                    state["consumed_evidences_by_id"].pop(pevid, None)
                else:
                    state["consumed_evidences_by_id"][pevid] = copy.deepcopy(ev_d["consumed"])
                if ev_d.get("pending", "__ABSENT__") == "__ABSENT__":
                    state["pending_evidence_eval_by_id"].pop(pevid, None)
                else:
                    state["pending_evidence_eval_by_id"][pevid] = copy.deepcopy(ev_d["pending"])
                if ev_d.get("idempotency_key"):
                    state["evidence_id_by_idempotency_key"][ev_d["idempotency_key"]] = pevid
            elig_id = delta.get("eligibility_id")
            if elig_id is not None and delta.get("elig_delta", "__UNCHANGED__") != "__UNCHANGED__":
                if delta["elig_delta"] == "__ABSENT__":
                    state["completion_eligibilities_by_id"].pop(elig_id, None)
                else:
                    state["completion_eligibilities_by_id"][elig_id] = copy.deepcopy(delta["elig_delta"])
            if delta.get("unbound_append") is not None:
                state["unbound_evidence_log"].append(copy.deepcopy(delta["unbound_append"]))
            if delta.get("late_terminal_append") is not None:
                state["late_terminal_evidence_log"].append(copy.deepcopy(delta["late_terminal_append"]))
            state["revision"] = int(entry["store_revision"])
            state["last_entry_hash"] = str(entry["entry_hash"])
            state["updated_at"] = str(entry["recorded_at"])
        return state

    def recover_from_wal_if_needed(
        self,
        *,
        fault_hook: Optional[Callable[[str], None]] = None,
    ) -> bool:
        self.store_root.mkdir(parents=True, exist_ok=True)
        self._dirty_snapshot = None
        self._verified_cache = None

        # Repair torn journal tail if WAL exists
        if self.journal_path.exists():
            raw_j = self.journal_path.read_text(encoding="utf-8")
            if raw_j and not raw_j.endswith("\n"):
                lines = raw_j.splitlines()
                valid_lines: list[str] = []
                for ln in lines:
                    try:
                        json.loads(ln)
                        valid_lines.append(ln)
                    except ValueError:
                        break
                repaired = ("\n".join(valid_lines) + "\n") if valid_lines else ""
                self.journal_path.write_text(repaired, encoding="utf-8")

        if not self.wal_path.exists():
            if self.journal_path.exists():
                entries = self.read_journal()
                if not self.state_path.exists():
                    self._write_snapshot_sync(self.rebuild_state_from_journal(entries))
                else:
                    try:
                        st = json.loads(self.state_path.read_text(encoding="utf-8"))
                        if st.get("revision") != len(entries):
                            self._write_snapshot_sync(self.rebuild_state_from_journal(entries))
                    except ValueError:
                        self._write_snapshot_sync(self.rebuild_state_from_journal(entries))
            return False

        if fault_hook is not None:
            fault_hook("wal_recovery")

        try:
            wal_data = json.loads(self.wal_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.wal_path.unlink(missing_ok=True)
            return True

        if not isinstance(wal_data, dict) or wal_data.get("schema_version") != SCHEMA_PROGRESS_WAL:
            self.wal_path.unlink(missing_ok=True)
            return True

        journal_entry = wal_data.get("journal_entry")
        if not isinstance(journal_entry, dict):
            self.wal_path.unlink(missing_ok=True)
            return True

        entries = self.read_journal()
        target_rev = int(journal_entry["store_revision"])
        if len(entries) == target_rev - 1:
            j_line = canonical_json_line(journal_entry) + "\n"
            with self.journal_path.open("a", encoding="utf-8") as jh:
                jh.write(j_line)
                jh.flush()
                os.fsync(jh.fileno())
            entries = self.read_journal()

        if len(entries) == target_rev:
            rebuilt = self.rebuild_state_from_journal(entries)
            self._write_snapshot_sync(rebuilt)

        self.wal_path.unlink(missing_ok=True)
        _fsync_dir(self.store_root)
        return True

    def commit_mutation(
        self,
        *,
        lease: ActivityAuthorityLease,
        capability: ProgressWriterCapability,
        next_state: dict[str, Any],
        journal_payload: Mapping[str, Any],
        fault_hook: Optional[Callable[[str], None]] = None,
    ) -> dict[str, Any]:
        LifeProgressAuthority.verify_capability(lease, capability)
        if capability.store_root != self.store_root:
            raise WriterCapabilityError("ProgressStore root mismatch")

        current = self.load_state(_mutable=True)
        next_rev = int(current["revision"]) + 1
        prev_hash = str(current["last_entry_hash"])
        ts_now = _iso(now())

        act_id = journal_payload.get("activity_id")
        pevid = journal_payload.get("progress_evidence_id")
        elig_id = journal_payload.get("eligibility_id")

        by_activity_keys = (
            "bindings_by_activity",
            "progress_contracts_by_activity",
            "completion_contracts_by_activity",
            "completion_contract_history_by_activity",
            "active_evidences_by_activity",
            "superseded_settlement_refs_by_activity",
            "progress_state_by_activity",
            "checkpoint_updates_by_activity",
            "pending_checkpoint_sync_by_activity",
            "open_eligibility_by_activity",
            "completed_once_by_activity",
            "reconciliations_by_activity",
        )
        act_delta: dict[str, Any] = {}
        if act_id is not None:
            for k in by_activity_keys:
                if act_id in next_state[k]:
                    act_delta[k] = copy.deepcopy(next_state[k][act_id])
                else:
                    act_delta[k] = "__ABSENT__"

        ev_delta: dict[str, Any] = {}
        if pevid is not None:
            ev_delta["consumed"] = copy.deepcopy(
                next_state["consumed_evidences_by_id"].get(pevid, "__ABSENT__")
            )
            ev_delta["pending"] = copy.deepcopy(
                next_state.get("pending_evidence_eval_by_id", {}).get(pevid, "__ABSENT__")
            )
            cons = next_state["consumed_evidences_by_id"].get(pevid)
            if isinstance(cons, Mapping) and isinstance(cons.get("evidence"), Mapping):
                ik = cons["evidence"].get("idempotency_key")
                if ik:
                    ev_delta["idempotency_key"] = ik

        elig_delta: Any = "__UNCHANGED__"
        if elig_id is not None:
            elig_delta = copy.deepcopy(
                next_state["completion_eligibilities_by_id"].get(elig_id, "__ABSENT__")
            )

        state_delta: dict[str, Any] = {
            "activity_id": act_id,
            "act_delta": act_delta,
            "progress_evidence_id": pevid,
            "ev_delta": ev_delta,
            "eligibility_id": elig_id,
            "elig_delta": elig_delta,
            "unbound_append": (
                copy.deepcopy(next_state["unbound_evidence_log"][-1])
                if journal_payload.get("event_type") == "UNBOUND_OR_IRRELEVANT_EVIDENCE_IGNORED"
                and next_state["unbound_evidence_log"]
                else None
            ),
            "late_terminal_append": (
                copy.deepcopy(next_state["late_terminal_evidence_log"][-1])
                if journal_payload.get("event_type")
                in {"COMPLETION_CONFLICT_DETECTED", "LATE_EVIDENCE_ON_TERMINAL_ACTIVITY"}
                and next_state["late_terminal_evidence_log"]
                else None
            ),
        }

        unsigned_entry: dict[str, Any] = {
            "schema_version": SCHEMA_PROGRESS_JOURNAL_ENTRY,
            "entry_id": f"pjen:{next_rev:08d}:{uuid.uuid4().hex[:8]}",
            "store_revision": next_rev,
            "prev_entry_hash": prev_hash,
            "recorded_at": ts_now,
            "writer_domain": capability.writer_domain,
            "payload": copy.deepcopy(dict(journal_payload)),
            "state_delta": state_delta,
        }
        entry_hash = sha256_hex(canonical_json_line(unsigned_entry))
        signed_entry = dict(unsigned_entry)
        signed_entry["entry_hash"] = entry_hash

        next_state["schema_version"] = SCHEMA_PROGRESS_STATE
        next_state["subject_id"] = GLOBAL_SUBJECT_ID
        next_state["namespace"] = self.namespace
        next_state["revision"] = next_rev
        next_state["updated_at"] = ts_now
        next_state["last_entry_hash"] = entry_hash

        if fault_hook is not None:
            fault_hook("wal_prepare")

        wal_doc = {
            "schema_version": SCHEMA_PROGRESS_WAL,
            "store_revision": next_rev,
            "journal_entry": signed_entry,
        }
        wal_tmp = self.store_root / f".{self.WAL_FILENAME}.tmp.{os.getpid()}.{uuid.uuid4().hex}"
        with wal_tmp.open("w", encoding="utf-8") as wh:
            wh.write(canonical_json_line(wal_doc) + "\n")
            wh.flush()
            if fault_hook is not None:
                os.fsync(wh.fileno())
        os.replace(wal_tmp, self.wal_path)

        if fault_hook is not None:
            fault_hook("wal_persist")

        j_line = canonical_json_line(signed_entry) + "\n"
        with self.journal_path.open("a", encoding="utf-8") as jh:
            jh.write(j_line)
            jh.flush()
            if fault_hook is not None:
                os.fsync(jh.fileno())

        if fault_hook is not None:
            fault_hook("wal_commit")

        if fault_hook is not None:
            self._write_snapshot_sync(next_state)
            self.wal_path.unlink(missing_ok=True)
            _fsync_dir(self.store_root)
            sig = self._disk_signature()
            if sig is not None:
                self._verified_cache = {"sig": sig, "state": copy.deepcopy(next_state)}
        else:
            self.wal_path.unlink(missing_ok=True)
            self._dirty_snapshot = next_state
            self._verified_cache = None

        return copy.deepcopy(signed_entry)


# ---------------------------------------------------------------------------
# ActivityProgressReadService (Sections 61, 100)
# ---------------------------------------------------------------------------


class ActivityProgressReadService:
    """Read-only query and Grounding overlay service for LR-5 Progress & Completion (0 write capability)."""

    def __init__(
        self,
        *,
        progress_store: ProgressStore,
        activity_store: Optional[CanonicalActivityStore] = None,
    ):
        self.progress_store = progress_store
        self.activity_store = activity_store
        self.activity_read = ActivityReadService(activity_store) if activity_store else None

    def get_binding(self, activity_id: str) -> Optional[dict[str, Any]]:
        state = self.progress_store.load_state(_mutable=True)
        b = state["bindings_by_activity"].get(activity_id)
        return copy.deepcopy(b) if b is not None else None

    def get_progress_contract(self, activity_id: str) -> Optional[dict[str, Any]]:
        state = self.progress_store.load_state(_mutable=True)
        pc = state["progress_contracts_by_activity"].get(activity_id)
        return copy.deepcopy(pc) if pc is not None else None

    def get_completion_contract(self, activity_id: str) -> Optional[dict[str, Any]]:
        state = self.progress_store.load_state(_mutable=True)
        cc = state["completion_contracts_by_activity"].get(activity_id)
        return copy.deepcopy(cc) if cc is not None else None

    def get_completion_contract_history(self, activity_id: str) -> list[dict[str, Any]]:
        state = self.progress_store.load_state(_mutable=True)
        return copy.deepcopy(state["completion_contract_history_by_activity"].get(activity_id, []))

    def get_progress_state(self, activity_id: str) -> dict[str, Any]:
        state = self.progress_store.load_state(_mutable=True)
        ps = state["progress_state_by_activity"].get(activity_id)
        if ps is not None:
            return copy.deepcopy(ps)
        return {
            "activity_id": activity_id,
            "progress_state": PROGRESS_STATE_UNKNOWN,
            "checkpoint_ref": None,
            "completion_status": EVAL_NOT_COMPLETE,
            "provable_percentage": None,
        }

    def list_checkpoint_updates(self, activity_id: str) -> list[dict[str, Any]]:
        state = self.progress_store.load_state(_mutable=True)
        return copy.deepcopy(state["checkpoint_updates_by_activity"].get(activity_id, []))

    def list_completion_eligibilities(self, *, activity_id: Optional[str] = None) -> list[dict[str, Any]]:
        state = self.progress_store.load_state(_mutable=True)
        items = list(state["completion_eligibilities_by_id"].values())
        if activity_id is not None:
            items = [e for e in items if e["activity_id"] == activity_id]
        items.sort(key=lambda x: str(x["eligibility_id"]))
        return copy.deepcopy(items)

    def list_reconciliations(self, *, activity_id: Optional[str] = None) -> list[dict[str, Any]]:
        state = self.progress_store.load_state(_mutable=True)
        if activity_id is not None:
            return copy.deepcopy(state["reconciliations_by_activity"].get(activity_id, []))
        all_recs: list[dict[str, Any]] = []
        for recs in state["reconciliations_by_activity"].values():
            all_recs.extend(recs)
        return copy.deepcopy(all_recs)

    def list_unbound_evidences(self) -> list[dict[str, Any]]:
        state = self.progress_store.load_state(_mutable=True)
        return copy.deepcopy(state["unbound_evidence_log"])

    def list_late_terminal_evidences(self, *, activity_id: Optional[str] = None) -> list[dict[str, Any]]:
        state = self.progress_store.load_state(_mutable=True)
        items = list(state["late_terminal_evidence_log"])
        if activity_id is not None:
            items = [x for x in items if x.get("activity_id") == activity_id]
        return copy.deepcopy(items)

    def get_progress_overlay(self, activity_id: Optional[str] = None) -> dict[str, Any]:
        """Section 100: Build read-only Grounding progress overlay (`checkpoint_ref`, `completion_status`, `progress_kind`)."""
        state = self.progress_store.load_state(_mutable=True)
        target_id = activity_id
        if target_id is None and self.activity_read is not None:
            view = self.activity_read.get_current_life_view()
            target_id = view.get("foreground_activity_ref")
        if target_id is None:
            return {
                "checkpoint_ref": None,
                "completion_status": None,
                "progress_kind": None,
            }
        ps = self.get_progress_state(target_id)
        pc = state["progress_contracts_by_activity"].get(target_id)
        return {
            "checkpoint_ref": ps.get("checkpoint_ref"),
            "completion_status": ps.get("completion_status", EVAL_NOT_COMPLETE),
            "progress_kind": pc.get("progress_kind") if pc else PROGRESS_KIND_OPEN_ENDED,
        }

    def get_grounding_life_view(
        self,
        *,
        caller_context: Optional[Mapping[str, Any]] = None,
    ) -> dict[str, Any]:
        """Section 99 & 100: Return One Chiyo Grounding LifeFrameReadView with read-only progress overlay."""
        if self.activity_read is None:
            raise ContractViolationError("activity_store is required for get_grounding_life_view")
        overlay = self.get_progress_overlay()
        return self.activity_read.get_current_life_view(
            caller_context=caller_context,
            progress_overlay=overlay,
        )


# ---------------------------------------------------------------------------
# ActivityProgressCoordinator (Sections 5-76, 97-104)
# ---------------------------------------------------------------------------


class ActivityProgressCoordinator(ResultConsumerProtocol):
    """Coordinates LR-5 Progress Evidence Intake, Checkpoint Advancement, Completion Eligibility,
    Natural Completion (via LR-2 ActivityCommandService), Reconciliation, and Recovery (0 LLM).

    Also implements `ResultConsumerProtocol` (`consumer_domain = "LIFE"`) to consume AR-1
    `SettledResultEvent` / `SettledResultCorrectionEvent` from the AR-1 Outbox.
    """

    consumer_domain = CONSUMER_DOMAIN_LIFE

    def __init__(
        self,
        *,
        activity_store: CanonicalActivityStore,
        activity_command: ActivityCommandService,
        progress_store: ProgressStore,
        lease: ActivityAuthorityLease,
        progress_capability: ProgressWriterCapability,
        policy_registry: Optional[ActivityProgressPolicyRegistry] = None,
        settlement_read: Optional[SettlementReadService] = None,
        auto_apply_natural_completion: bool = True,
    ):
        LifeProgressAuthority.verify_capability(lease, progress_capability)
        self.activity_store = activity_store
        self.activity_command = activity_command
        self.activity_read = ActivityReadService(activity_store)
        self.progress_store = progress_store
        self.lease = lease
        self.progress_capability = progress_capability
        self.policy_registry = policy_registry or ActivityProgressPolicyRegistry()
        self.settlement_read = settlement_read
        self.auto_apply_natural_completion = auto_apply_natural_completion
        self.read_service = ActivityProgressReadService(
            progress_store=progress_store,
            activity_store=activity_store,
        )
        # Typed ACTIVITY_CONSEQUENCE ports are handed in by the composition root.
        self.activity_consequence_port = None
        self.progress_consequence_port = None
        # Domain write counters for invariant verification (Section 68, 69, 91 I09)
        self.domain_write_count = 0
        self.memory_write_count = 0
        self.goal_write_count = 0
        self.world_write_count = 0
        self.artifact_write_count = 0
        self.llm_call_count = 0

    # -- Registration of Bindings, ProgressContracts & CompletionContracts ---

    def register_result_binding(
        self,
        binding: ActivityResultBinding,
        *,
        fault_hook: Optional[Callable[[str], None]] = None,
    ) -> dict[str, Any]:
        LifeProgressAuthority.verify_capability(self.lease, self.progress_capability)
        act = self.activity_read.get_activity(binding.activity_id)
        if act is None:
            raise ContractViolationError(f"cannot bind result to non-existent activity_id={binding.activity_id!r}")
        state = self.progress_store.load_state(_mutable=True)
        b_dict = binding.to_dict()
        state["bindings_by_activity"][binding.activity_id] = b_dict
        self.progress_store.commit_mutation(
            lease=self.lease,
            capability=self.progress_capability,
            next_state=state,
            journal_payload={
                "event_type": "ACTIVITY_RESULT_BINDING_REGISTERED",
                "activity_id": binding.activity_id,
                "binding_id": binding.binding_id,
                "target_refs": binding.target_refs,
            },
            fault_hook=fault_hook,
        )
        return copy.deepcopy(b_dict)

    def register_progress_contract(
        self,
        contract: ProgressContract,
        *,
        fault_hook: Optional[Callable[[str], None]] = None,
    ) -> dict[str, Any]:
        LifeProgressAuthority.verify_capability(self.lease, self.progress_capability)
        act = self.activity_read.get_activity(contract.activity_id)
        if act is None:
            raise ContractViolationError(f"activity_id={contract.activity_id!r} not found")
        state = self.progress_store.load_state(_mutable=True)
        c_dict = contract.to_dict()
        state["progress_contracts_by_activity"][contract.activity_id] = c_dict
        if contract.activity_id not in state["progress_state_by_activity"]:
            state["progress_state_by_activity"][contract.activity_id] = {
                "activity_id": contract.activity_id,
                "progress_state": PROGRESS_STATE_UNKNOWN,
                "checkpoint_ref": act.get("checkpoint_ref"),
                "completion_status": EVAL_NOT_COMPLETE,
                "provable_percentage": None,
            }
        self.progress_store.commit_mutation(
            lease=self.lease,
            capability=self.progress_capability,
            next_state=state,
            journal_payload={
                "event_type": "PROGRESS_CONTRACT_REGISTERED",
                "activity_id": contract.activity_id,
                "progress_contract_id": contract.progress_contract_id,
                "progress_kind": contract.progress_kind,
            },
            fault_hook=fault_hook,
        )
        return copy.deepcopy(c_dict)

    def register_completion_contract(
        self,
        contract: CompletionContract,
        *,
        allow_lowered_target_with_decision: bool = False,
        fault_hook: Optional[Callable[[str], None]] = None,
    ) -> dict[str, Any]:
        """Sections 12-15, 50-51: Register or supersede a versioned CompletionContract."""
        LifeProgressAuthority.verify_capability(self.lease, self.progress_capability)
        act = self.activity_read.get_activity(contract.activity_id)
        if act is None:
            raise ContractViolationError(f"activity_id={contract.activity_id!r} not found")
        state = self.progress_store.load_state(_mutable=True)
        existing = state["completion_contracts_by_activity"].get(contract.activity_id)

        if existing is not None:
            expected_rev = int(existing["revision"]) + 1
            if contract.revision != expected_rev:
                raise CompletionContractValidationError(
                    f"CompletionContract revision must increment from {existing['revision']} to {expected_rev}"
                )
            if contract.supersedes_contract_ref != existing["contract_id"]:
                raise CompletionContractValidationError(
                    f"supersedes_contract_ref={contract.supersedes_contract_ref!r} must match "
                    f"active contract_id={existing['contract_id']!r}"
                )
            if not contract.modification_decision_ref:
                raise IllegalContractLoweringError(
                    "Section 51 violation: modifying CompletionContract requires modification_decision_ref"
                )
            # Check if numeric target was lowered without explicit allow_lowered_target_with_decision
            old_c = existing.get("criteria") or {}
            new_c = contract.criteria
            for num_key in ("min_version", "min_cursor", "target_page"):
                if num_key in old_c and num_key in new_c:
                    if int(new_c[num_key]) < int(old_c[num_key]) and not allow_lowered_target_with_decision:
                        raise IllegalContractLoweringError(
                            f"Section 51 violation: silently lowering {num_key} from {old_c[num_key]} "
                            f"to {new_c[num_key]} is forbidden"
                        )
            existing["status"] = CONTRACT_STATUS_SUPERSEDED
            existing["superseded_by_contract_ref"] = contract.contract_id
            hist = state["completion_contract_history_by_activity"].setdefault(contract.activity_id, [])
            if hist:
                hist[-1] = copy.deepcopy(existing)
            else:
                hist.append(copy.deepcopy(existing))

        c_dict = contract.to_dict()
        state["completion_contracts_by_activity"][contract.activity_id] = c_dict
        state["completion_contract_history_by_activity"].setdefault(contract.activity_id, []).append(
            copy.deepcopy(c_dict)
        )

        self.progress_store.commit_mutation(
            lease=self.lease,
            capability=self.progress_capability,
            next_state=state,
            journal_payload={
                "event_type": "COMPLETION_CONTRACT_REGISTERED",
                "activity_id": contract.activity_id,
                "contract_id": contract.contract_id,
                "revision": contract.revision,
                "supersedes_contract_ref": contract.supersedes_contract_ref,
            },
            fault_hook=fault_hook,
        )
        return copy.deepcopy(c_dict)

    # -- Evidence Intake & Evaluation (Sections 6-41, 57-69, 97-98) ----------

    def reject_untrusted_input(self, raw_input: Any) -> None:
        """Section 40, 80 (B01, B02, D05), 97: Reject LLM text, arbitrary percent, or raw provider responses."""
        raise UntrustedProgressEvidenceError(
            f"Untrusted or narrative input rejected by LR-5 Progress/Completion gate: {raw_input!r}"
        )

    def set_consequence_port(self, port: Any) -> None:
        """Composition root only: hand LR-5 its COMPLETE consequence port."""
        if port is not None and not isinstance(port, ActivityConsequencePort):
            raise TypeError("LR-5 requires an ActivityConsequencePort")
        self.activity_consequence_port = port

    def set_progress_consequence_port(self, port: Any) -> None:
        """Composition root only: hand LR-5 its CHECKPOINT/PROGRESS consequence port."""
        if port is not None and not isinstance(port, ActivityConsequencePort):
            raise TypeError("LR-5 requires an ActivityConsequencePort")
        self.progress_consequence_port = port

    def ingest_progress_evidence(
        self,
        evidence_input: LifeProgressEvidence | Mapping[str, Any] | str,
        *,
        apply_natural_completion: Optional[bool] = None,
        fault_hook: Optional[Callable[[str], None]] = None,
    ) -> dict[str, Any]:
        """Ingest canonical progress evidence, evaluate checkpoint advancement & completion eligibility,
        and (if eligible & enabled) execute natural completion through LR-2 ActivityCommandService.
        """
        LifeProgressAuthority.verify_capability(self.lease, self.progress_capability)

        if fault_hook is not None:
            fault_hook("before_evidence_consume")

        # 1. Validate & normalize input (reject free-form strings, LLM narration, arbitrary %, raw receipts)
        if isinstance(evidence_input, str):
            self.reject_untrusted_input(evidence_input)
        elif isinstance(evidence_input, LifeProgressEvidence):
            evidence = evidence_input
        elif isinstance(evidence_input, Mapping):
            # Check if it's an AR-1 SettledResultEvent / SettledResultCorrectionEvent
            if evidence_input.get("schema_version") == "chiyo.action_reality.settled_event.v1" or (
                "event_id" in evidence_input and "settlement_ref" in evidence_input
            ):
                s_rec = None
                if self.settlement_read is not None:
                    getter = getattr(
                        self.settlement_read,
                        "get_settlement_by_id",
                        getattr(self.settlement_read, "get_settlement", None),
                    )
                    if callable(getter):
                        s_rec = getter(str(evidence_input["settlement_ref"]))
                evidence = LifeProgressEvidence.from_settled_result_event(
                    evidence_input,
                    settlement_record=s_rec,
                    observed_facts_override=evidence_input.get("observed_facts_override"),
                )
            elif evidence_input.get("schema_version") == "chiyo.action_reality.receipt.v1" or "receipt_id" in evidence_input:
                # Section 6 & 97: Do NOT bypass AR-1 Settlement to read raw ActionReceipt!
                raise UntrustedProgressEvidenceError(
                    "Section 6/97 violation: raw ActionReceipt cannot bypass AR-1 Result Settlement to enter LR-5"
                )
            elif (
                "llm_text" in evidence_input
                or "model_narration" in evidence_input
                or evidence_input.get("source_kind") in FORBIDDEN_EVIDENCE_SOURCE_KINDS
            ):
                self.reject_untrusted_input(evidence_input)
            else:
                evidence = LifeProgressEvidence(
                    progress_evidence_id=str(evidence_input["progress_evidence_id"]),
                    subject_id=str(evidence_input.get("subject_id", GLOBAL_SUBJECT_ID)),
                    activity_id=evidence_input.get("activity_id"),
                    activity_revision_hint=evidence_input.get("activity_revision_hint"),
                    source_kind=str(evidence_input["source_kind"]),
                    source_ref=str(evidence_input["source_ref"]),
                    settlement_ref=evidence_input.get("settlement_ref"),
                    supersedes_settlement_ref=evidence_input.get("supersedes_settlement_ref"),
                    action_ref=evidence_input.get("action_ref"),
                    effect_claim_refs=list(evidence_input.get("effect_claim_refs") or []),
                    effect_claims=list(evidence_input.get("effect_claims") or []),
                    settlement_status=evidence_input.get("settlement_status"),
                    outcome_kind=evidence_input.get("outcome_kind"),
                    artifact_ref=evidence_input.get("artifact_ref"),
                    world_fact_ref=evidence_input.get("world_fact_ref"),
                    external_result_ref=evidence_input.get("external_result_ref"),
                    target_ref=evidence_input.get("target_ref"),
                    observed_facts=dict(evidence_input.get("observed_facts") or {}),
                    occurred_at=str(evidence_input.get("occurred_at") or _iso(now())),
                    observed_at=str(evidence_input.get("observed_at") or _iso(now())),
                    recorded_at=str(evidence_input.get("recorded_at") or _iso(now())),
                    causal_parent_refs=list(evidence_input.get("causal_parent_refs") or []),
                    correlation_id=evidence_input.get("correlation_id"),
                    authority_domain=str(evidence_input["authority_domain"]),
                    epistemic_kind=str(evidence_input.get("epistemic_kind", EPISTEMIC_CANONICAL_OBSERVATION)),
                    idempotency_key=str(
                        evidence_input.get("idempotency_key")
                        or f"idem:{evidence_input['progress_evidence_id']}"
                    ),
                )
        else:
            raise UntrustedProgressEvidenceError(f"unsupported evidence input type: {type(evidence_input)!r}")

        state = self.progress_store.load_state(_mutable=True)

        # 2. Idempotency check (Section 58, A04, C04, I07):
        # Delivering the same evidence / SettledResultEvent 100 times produces at most 1 progress update & 1 completion!
        existing_pevid = state["evidence_id_by_idempotency_key"].get(evidence.idempotency_key)
        if existing_pevid is not None or evidence.progress_evidence_id in state["consumed_evidences_by_id"]:
            rec_id = existing_pevid or evidence.progress_evidence_id
            prev_record = state["consumed_evidences_by_id"][rec_id]
            act_id = prev_record.get("bound_activity_id")
            ps = self.read_service.get_progress_state(act_id) if act_id else None
            act_now = self.activity_read.get_activity(act_id) if act_id else None
            return {
                "idempotent_replay": True,
                "disposition": prev_record.get("disposition", "CONSUMED"),
                "activity_id": act_id,
                "progress_state": ps["progress_state"] if ps else PROGRESS_STATE_UNKNOWN,
                "checkpoint_ref": act_now["checkpoint_ref"] if act_now else None,
                "completion_status": ps["completion_status"] if ps else EVAL_NOT_COMPLETE,
                "activity_status": act_now["status"] if act_now else None,
                "completed_transition": False,
            }

        # 3. Resolve ActivityResultBinding (Sections 9-11, A02, A03, D04)
        matched_activity_id: Optional[str] = None
        match_reason = "UNBOUND_EVIDENCE"

        if evidence.activity_id is not None:
            b = state["bindings_by_activity"].get(evidence.activity_id)
            if b is not None:
                ok, reason = self.policy_registry.match_evidence_to_binding(
                    evidence=evidence, binding=b
                )
                if ok:
                    matched_activity_id = evidence.activity_id
                    match_reason = reason
                else:
                    match_reason = reason
            else:
                match_reason = "NO_BINDING_FOR_ACTIVITY"
        else:
            # Check if any registered binding explicitly matches by correlation_id or unique non-action target_ref
            for cand_act_id, b in state["bindings_by_activity"].items():
                ok, reason = self.policy_registry.match_evidence_to_binding(
                    evidence=evidence, binding=b
                )
                if ok:
                    matched_activity_id = cand_act_id
                    match_reason = reason
                    break
                if reason in {"IRRELEVANT_TARGET_REF", "UNBOUND_ACTION_RESULT_EVIDENCE"}:
                    match_reason = reason

        if matched_activity_id is None:
            # Record unbound / irrelevant evidence in journal without mutating any Activity (A02, A03, D04)
            unbound_entry = {
                "progress_evidence_id": evidence.progress_evidence_id,
                "source_ref": evidence.source_ref,
                "target_ref": evidence.target_ref,
                "reason": match_reason,
                "recorded_at": evidence.recorded_at,
            }
            state["unbound_evidence_log"].append(unbound_entry)
            state["consumed_evidences_by_id"][evidence.progress_evidence_id] = {
                "evidence": evidence.to_dict(),
                "bound_activity_id": None,
                "disposition": match_reason,
            }
            state["evidence_id_by_idempotency_key"][evidence.idempotency_key] = evidence.progress_evidence_id
            self.progress_store.commit_mutation(
                lease=self.lease,
                capability=self.progress_capability,
                next_state=state,
                journal_payload={
                    "event_type": "UNBOUND_OR_IRRELEVANT_EVIDENCE_IGNORED",
                    "progress_evidence_id": evidence.progress_evidence_id,
                    "reason": match_reason,
                },
            )
            return {
                "idempotent_replay": False,
                "disposition": match_reason,
                "activity_id": None,
                "progress_state": PROGRESS_STATE_UNKNOWN,
                "checkpoint_ref": None,
                "completion_status": EVAL_NOT_COMPLETE,
                "activity_status": None,
                "completed_transition": False,
            }

        act = self.activity_read.get_activity(matched_activity_id)
        if act is None:
            raise ContractViolationError(f"bound activity_id={matched_activity_id!r} not found in CanonicalActivityStore")

        if fault_hook is not None:
            state.setdefault("pending_evidence_eval_by_id", {})[evidence.progress_evidence_id] = evidence.to_dict()
            self.progress_store.commit_mutation(
                lease=self.lease,
                capability=self.progress_capability,
                next_state=state,
                journal_payload={
                    "event_type": "PROGRESS_EVIDENCE_INTAKE_STAGED",
                    "activity_id": matched_activity_id,
                    "progress_evidence_id": evidence.progress_evidence_id,
                },
            )
            state = self.progress_store.load_state(_mutable=True)
            fault_hook("after_evidence_consume")

        # 4. Terminal Activity Protection & Post-Completion Correction Reconciliation (Sections 38, 39, 87, 88, 98)
        if act["status"] in TERMINAL_ACTIVITY_STATUSES:
            state.get("pending_evidence_eval_by_id", {}).pop(evidence.progress_evidence_id, None)
            return self._handle_evidence_on_terminal_activity(
                state=state,
                activity=act,
                evidence=evidence,
                fault_hook=fault_hook,
            )

        # 5. Activity Revision Guard (Section 57, A05):
        # If evidence carries an activity_revision_hint < current activity['revision'],
        # we re-evaluate against the current Activity state & contracts (never blindly overwrite with stale assumptions).
        re_evaluated_from_stale_hint = (
            evidence.activity_revision_hint is not None
            and int(evidence.activity_revision_hint) != int(act["revision"])
        )

        # 6. Handle AR-1 Supersession / Correction on active evidences (Sections 60, 88, 98)
        act_ev_list: list[dict[str, Any]] = list(
            state["active_evidences_by_activity"].get(matched_activity_id, [])
        )
        if evidence.supersedes_settlement_ref:
            sup_ref = evidence.supersedes_settlement_ref
            state["superseded_settlement_refs_by_activity"].setdefault(
                matched_activity_id, []
            ).append(sup_ref)
            # Remove superseded settlement evidence from active_evidences_by_activity so invalidated evidence is NEVER silently retained!
            act_ev_list = [
                e for e in act_ev_list if e.get("settlement_ref") != sup_ref
            ]

        ev_dict = evidence.to_dict()
        ev_dict["activity_id"] = matched_activity_id
        act_ev_list.append(ev_dict)
        state["active_evidences_by_activity"][matched_activity_id] = act_ev_list

        # 7. Evaluate Progress & Checkpoint Advancement (Sections 16-26, 52-59)
        prog_contract = state["progress_contracts_by_activity"].get(matched_activity_id)
        prior_chk = act.get("checkpoint_ref")
        prog_eval = NaturalProgressEvaluator.evaluate_progress(
            activity=act,
            progress_contract=prog_contract,
            evidence=evidence,
            prior_checkpoint_ref=prior_chk,
            active_evidences=act_ev_list,
        )

        new_chk = prog_eval["new_checkpoint_ref"]
        prog_state_val = prog_eval["progress_state"]
        checkpoint_changed = (
            new_chk is not None
            and new_chk != prior_chk
            and prog_state_val in {PROGRESS_STATE_ADVANCED, PROGRESS_STATE_REGRESSED}
        )

        # 8. Evaluate Completion Contract (Sections 12-15, 27-36)
        comp_contract = state["completion_contracts_by_activity"].get(matched_activity_id)
        comp_eval = NaturalCompletionEvaluator.evaluate_completion(
            activity=act,
            completion_contract=comp_contract,
            active_evidences=act_ev_list,
            current_checkpoint_ref=new_chk or prior_chk,
        )
        comp_eval_status = comp_eval["eval_status"]
        if comp_eval_status == EVAL_COMPLETION_ELIGIBLE:
            prog_state_val = PROGRESS_STATE_COMPLETION_ELIGIBLE
        elif comp_eval_status == EVAL_CONFLICT:
            prog_state_val = PROGRESS_STATE_BLOCKED

        # Record CheckpointUpdate in ProgressStore BEFORE mutating Activity (Section 74 & 75)
        chk_update_obj: Optional[CheckpointUpdate] = None
        if checkpoint_changed and new_chk is not None:
            if fault_hook is not None:
                fault_hook("before_checkpoint_journal")

            upd_id = f"chkup:{sha256_hex(f'{matched_activity_id}:{evidence.progress_evidence_id}:{new_chk}')[:16]}"
            chk_update_obj = CheckpointUpdate(
                update_id=upd_id,
                activity_id=matched_activity_id,
                previous_checkpoint_ref=prior_chk,
                new_checkpoint_ref=new_chk,
                progress_state=prog_eval["progress_state"],
                progress_evidence_refs=[evidence.progress_evidence_id],
                settlement_refs=[evidence.settlement_ref] if evidence.settlement_ref else [],
                activity_revision_before=int(act["revision"]),
                activity_revision_after=int(act["revision"]) + 1,
                occurred_at=evidence.occurred_at,
                recorded_at=evidence.recorded_at,
                policy_version=prog_eval["policy_version"],
                idempotency_key=f"idem_chk:{matched_activity_id}:{evidence.progress_evidence_id}",
                provable_percentage=evidence.provable_percentage,
            )
            state["checkpoint_updates_by_activity"].setdefault(matched_activity_id, []).append(
                chk_update_obj.to_dict()
            )
            state["pending_checkpoint_sync_by_activity"][matched_activity_id] = {
                "checkpoint_ref": new_chk,
                "last_action_ref": evidence.action_ref,
                "source_refs": [evidence.progress_evidence_id, evidence.source_ref],
                "idempotency_key": chk_update_obj.idempotency_key,
                "occurred_at": evidence.occurred_at,
                "observed_at": evidence.observed_at,
            }

        state.get("pending_evidence_eval_by_id", {}).pop(evidence.progress_evidence_id, None)
        state["progress_state_by_activity"][matched_activity_id] = {
            "activity_id": matched_activity_id,
            "progress_state": prog_state_val,
            "checkpoint_ref": new_chk or prior_chk,
            "completion_status": comp_eval_status,
            "provable_percentage": evidence.provable_percentage,
            "re_evaluated_from_stale_hint": re_evaluated_from_stale_hint,
        }
        state["consumed_evidences_by_id"][evidence.progress_evidence_id] = {
            "evidence": ev_dict,
            "bound_activity_id": matched_activity_id,
            "disposition": "CONSUMED",
        }
        state["evidence_id_by_idempotency_key"][evidence.idempotency_key] = evidence.progress_evidence_id

        self.progress_store.commit_mutation(
            lease=self.lease,
            capability=self.progress_capability,
            next_state=state,
            journal_payload={
                "event_type": "CHECKPOINT_AND_PROGRESS_RECORDED" if checkpoint_changed else "PROGRESS_EVIDENCE_EVALUATED",
                "activity_id": matched_activity_id,
                "progress_evidence_id": evidence.progress_evidence_id,
                "progress_state": prog_state_val,
                "new_checkpoint_ref": new_chk,
                "completion_eval_status": comp_eval_status,
            },
            fault_hook=fault_hook,
        )

        if checkpoint_changed and fault_hook is not None:
            fault_hook("after_checkpoint_journal")

        # 9. Apply Checkpoint Update to LR-2 CanonicalActivityStore via ActivityCommandService (Section 22, 24, 25)
        if checkpoint_changed and new_chk is not None:
            if fault_hook is not None:
                fault_hook("before_checkpoint_activity_update")

            cur_act = self.activity_read.get_activity(matched_activity_id)
            if cur_act is not None and cur_act["status"] in OPEN_ACTIVITY_STATUSES:
                _progress_port = getattr(self, "progress_consequence_port", None)
                if _progress_port is None:
                    raise WriterCapabilityError(
                        "LR-5 checkpoint sync requires a progress consequence port"
                    )
                cmd_res = _progress_port.propose_checkpoint_or_progress(
                    "CHECKPOINT",
                    activity_id=matched_activity_id,
                    checkpoint_ref=new_chk,
                    last_action_ref=evidence.action_ref,
                    expected_revision=self._current_store_revision(),
                    idempotency_key=f"idem_lr2_chk:{matched_activity_id}:{evidence.progress_evidence_id}",
                    source_refs=[evidence.progress_evidence_id, evidence.source_ref],
                    occurred_at=evidence.occurred_at,
                    observed_at=evidence.observed_at,
                )
                act = cmd_res.activity or self.activity_read.get_activity(matched_activity_id)

            # Clear pending_checkpoint_sync_by_activity
            state = self.progress_store.load_state(_mutable=True)
            state["pending_checkpoint_sync_by_activity"].pop(matched_activity_id, None)
            self.progress_store.commit_mutation(
                lease=self.lease,
                capability=self.progress_capability,
                next_state=state,
                journal_payload={
                    "event_type": "CHECKPOINT_ACTIVITY_SYNC_CONFIRMED",
                    "activity_id": matched_activity_id,
                    "checkpoint_ref": new_chk,
                    "activity_revision": act["revision"] if act else None,
                },
            )

            if fault_hook is not None:
                fault_hook("after_checkpoint_activity_update")

        # 10. Handle CompletionEligibility & Natural Completion (Sections 28-31, 63, 73)
        should_complete = (
            self.auto_apply_natural_completion
            if apply_natural_completion is None
            else apply_natural_completion
        )
        completed_transition = False
        eligibility_dict: Optional[dict[str, Any]] = None

        if comp_eval_status == EVAL_COMPLETION_ELIGIBLE and comp_contract is not None:
            if fault_hook is not None:
                fault_hook("before_completion_eligibility")

            cur_act = self.activity_read.get_activity(matched_activity_id)
            assert cur_act is not None
            elig_hash_input = f"{matched_activity_id}:{comp_contract['contract_id']}:v{comp_contract['revision']}"
            elig_id = f"celig:{sha256_hex(elig_hash_input)[:16]}"
            elig_obj = ActivityCompletionEligibility(
                eligibility_id=elig_id,
                activity_id=matched_activity_id,
                activity_revision=int(cur_act["revision"]),
                completion_contract_ref=str(comp_contract["contract_id"]),
                completion_contract_revision=int(comp_contract["revision"]),
                evidence_refs=comp_eval["satisfying_evidence_refs"],
                settlement_refs=comp_eval["settlement_refs"],
                authoritative_result_refs=comp_eval["authoritative_result_refs"],
                created_at=evidence.recorded_at,
                policy_version=str(comp_contract["policy_version"]),
                status=COMP_ELIG_STATUS_OPEN,
            )
            eligibility_dict = elig_obj.to_dict()

            state = self.progress_store.load_state(_mutable=True)
            state["completion_eligibilities_by_id"][elig_id] = eligibility_dict
            state["open_eligibility_by_activity"][matched_activity_id] = elig_id
            self.progress_store.commit_mutation(
                lease=self.lease,
                capability=self.progress_capability,
                next_state=state,
                journal_payload={
                    "event_type": "COMPLETION_ELIGIBILITY_CREATED",
                    "activity_id": matched_activity_id,
                    "eligibility_id": elig_id,
                    "completion_contract_ref": comp_contract["contract_id"],
                },
                fault_hook=fault_hook,
            )

            if fault_hook is not None:
                fault_hook("after_completion_eligibility")

            if should_complete:
                comp_res = self.execute_natural_completion(
                    activity_id=matched_activity_id,
                    eligibility_id=elig_id,
                    occurred_at=evidence.occurred_at,
                    observed_at=evidence.observed_at,
                    fault_hook=fault_hook,
                )
                completed_transition = bool(comp_res["completed"])
                eligibility_dict = comp_res["eligibility"]

        final_act = self.activity_read.get_activity(matched_activity_id)
        final_ps = self.read_service.get_progress_state(matched_activity_id)
        return {
            "idempotent_replay": False,
            "disposition": "CONSUMED",
            "activity_id": matched_activity_id,
            "progress_state": final_ps["progress_state"],
            "checkpoint_ref": final_act["checkpoint_ref"] if final_act else None,
            "completion_status": final_ps["completion_status"],
            "completion_eligibility": eligibility_dict,
            "activity_status": final_act["status"] if final_act else None,
            "completed_transition": completed_transition,
            "re_evaluated_from_stale_hint": re_evaluated_from_stale_hint,
        }

    def execute_natural_completion(
        self,
        *,
        activity_id: str,
        eligibility_id: Optional[str] = None,
        occurred_at: Optional[str] = None,
        observed_at: Optional[str] = None,
        fault_hook: Optional[Callable[[str], None]] = None,
    ) -> dict[str, Any]:
        """Sections 29-31, 63, 73: Consume an OPEN ActivityCompletionEligibility and execute
        `ActivityCommandService.complete_activity(...)` with real `completion_evidence_ref`.
        Guarantees semantic completion at most once per Activity.
        """
        LifeProgressAuthority.verify_capability(self.lease, self.progress_capability)
        state = self.progress_store.load_state(_mutable=True)

        target_elig_id = eligibility_id or state["open_eligibility_by_activity"].get(activity_id)
        if not target_elig_id or target_elig_id not in state["completion_eligibilities_by_id"]:
            raise ContractViolationError(
                f"no ActivityCompletionEligibility found for activity_id={activity_id!r}"
            )
        elig = state["completion_eligibilities_by_id"][target_elig_id]

        cur_act = self.activity_read.get_activity(activity_id)
        if cur_act is None:
            raise ContractViolationError(f"activity_id={activity_id!r} not found")

        # Exactly-once semantic completion guard (Section 73, I01, I07)
        if cur_act["status"] == STATUS_COMPLETED or activity_id in state["completed_once_by_activity"]:
            if elig["status"] != COMP_ELIG_STATUS_CONSUMED:
                elig["status"] = COMP_ELIG_STATUS_CONSUMED
                elig["consumed_at"] = occurred_at or _iso(now())
                state["open_eligibility_by_activity"].pop(activity_id, None)
                state["completed_once_by_activity"][activity_id] = {
                    "eligibility_id": target_elig_id,
                    "completion_evidence_ref": cur_act.get("completion_evidence_ref") or target_elig_id,
                }
                self.progress_store.commit_mutation(
                    lease=self.lease,
                    capability=self.progress_capability,
                    next_state=state,
                    journal_payload={
                        "event_type": "COMPLETION_ELIGIBILITY_RECONCILED_CONSUMED",
                        "activity_id": activity_id,
                        "eligibility_id": target_elig_id,
                    },
                )
            return {
                "completed": False,
                "idempotent_replay": True,
                "activity": cur_act,
                "eligibility": copy.deepcopy(elig),
            }

        if cur_act["status"] in TERMINAL_ACTIVITY_STATUSES:
            raise TerminalActivityImmutableError(
                f"cannot complete activity {activity_id!r} in terminal status {cur_act['status']!r}"
            )

        # Pick canonical completion_evidence_ref (Section 31: points to authoritative result / settlement / eligibility)
        auth_refs = elig.get("authoritative_result_refs") or []
        comp_ev_ref = auth_refs[0] if auth_refs else target_elig_id
        validate_completion_evidence_ref(comp_ev_ref)

        if fault_hook is not None:
            fault_hook("before_complete_command")

        src_refs = list(
            dict.fromkeys(
                [target_elig_id, comp_ev_ref]
                + list(elig.get("evidence_refs") or [])
                + list(elig.get("settlement_refs") or [])
            )
        )
        _completion_port = getattr(self, "activity_consequence_port", None)
        if _completion_port is None:
            raise WriterCapabilityError(
                "LR-5 completion requires an ActivityConsequencePort from the composition root"
            )
        cmd_res = _completion_port.propose_completion(
            CompletionProposal(
                activity_id=activity_id,
                expected_revision=self._current_store_revision(),
                completion_evidence_ref=comp_ev_ref,
                proposal_id=f"lr5_complete:{activity_id}:{target_elig_id}",
                eligibility_id=target_elig_id,
                idempotency_key=f"idem_lr5_complete:{activity_id}",
                checkpoint_ref=cur_act.get("checkpoint_ref"),
                last_action_ref=cur_act.get("last_action_ref"),
                source_refs=tuple(src_refs),
                causal_parent_refs=(target_elig_id,),
                occurred_at=occurred_at,
                observed_at=observed_at,
            )
        )

        if fault_hook is not None:
            fault_hook("after_complete_command")
            fault_hook("before_completion_evidence_persist")

        state = self.progress_store.load_state(_mutable=True)
        elig = state["completion_eligibilities_by_id"][target_elig_id]
        elig["status"] = COMP_ELIG_STATUS_CONSUMED
        elig["consumed_at"] = occurred_at or _iso(now())
        elig["completion_transition_ref"] = (
            cmd_res.transition["transition_id"] if cmd_res.transition else None
        )
        state["open_eligibility_by_activity"].pop(activity_id, None)
        state["completed_once_by_activity"][activity_id] = {
            "eligibility_id": target_elig_id,
            "completion_evidence_ref": comp_ev_ref,
            "transition_id": elig["completion_transition_ref"],
            "completed_at_revision": cmd_res.revision,
        }
        if activity_id in state["progress_state_by_activity"]:
            state["progress_state_by_activity"][activity_id]["completion_status"] = STATUS_COMPLETED

        self.progress_store.commit_mutation(
            lease=self.lease,
            capability=self.progress_capability,
            next_state=state,
            journal_payload={
                "event_type": "ACTIVITY_COMPLETED_NATURALLY",
                "activity_id": activity_id,
                "eligibility_id": target_elig_id,
                "completion_evidence_ref": comp_ev_ref,
                "transition_id": elig["completion_transition_ref"],
            },
            fault_hook=fault_hook,
        )

        if fault_hook is not None:
            fault_hook("after_completion_evidence_persist")

        return {
            "completed": True,
            "idempotent_replay": cmd_res.idempotent_replay,
            "activity": cmd_res.activity,
            "eligibility": copy.deepcopy(elig),
        }

    def _handle_evidence_on_terminal_activity(
        self,
        *,
        state: dict[str, Any],
        activity: Mapping[str, Any],
        evidence: LifeProgressEvidence,
        fault_hook: Optional[Callable[[str], None]] = None,
    ) -> dict[str, Any]:
        """Sections 38, 39, 87, 88, 98:
        - Never reopen/revive a terminal Activity (COMPLETED, ABANDONED, CANCELLED, EXPIRED).
        - If Activity is COMPLETED and a late correction conflicts with or invalidates its completion evidence,
          record COMPLETION_CONFLICT / RECONCILIATION_REQUIRED without silently reopening!
        """
        act_id = str(activity["activity_id"])
        act_status = str(activity["status"])
        ev_dict = evidence.to_dict()
        ev_dict["activity_id"] = act_id

        is_conflicting_correction = False
        if act_status == STATUS_COMPLETED:
            if (
                evidence.source_kind == SOURCE_KIND_CORRECTION_EVENT
                or evidence.supersedes_settlement_ref is not None
                or evidence.epistemic_kind == EPISTEMIC_CONFLICTED_CLAIM
                or evidence.settlement_status == SETTLEMENT_STATUS_CONFLICT
            ):
                # Check if the correction reports conflict, failure, or invalidates the criterion
                if (
                    evidence.epistemic_kind == EPISTEMIC_CONFLICTED_CLAIM
                    or evidence.settlement_status in {SETTLEMENT_STATUS_CONFLICT, SETTLEMENT_STATUS_UNRESOLVED}
                    or evidence.outcome_kind in {OUTCOME_FAILURE_NO_EFFECT, OUTCOME_UNKNOWN_EFFECT}
                    or (evidence.observed_facts or {}).get("hash_mismatch") is True
                    or (evidence.observed_facts or {}).get("world_mutation_absent") is True
                ):
                    is_conflicting_correction = True

        reconciliation_dict: Optional[dict[str, Any]] = None
        if is_conflicting_correction:
            rec_id = f"crecon:{sha256_hex(f'{act_id}:{evidence.progress_evidence_id}')[:16]}"
            rec_obj = CompletionReconciliationRecord(
                reconciliation_id=rec_id,
                activity_id=act_id,
                completed_at_revision=int(activity["revision"]),
                completion_evidence_ref=str(activity.get("completion_evidence_ref") or "evidence:unknown"),
                conflicting_evidence_ref=evidence.progress_evidence_id,
                conflicting_settlement_ref=evidence.settlement_ref,
                conflict_kind=CONFLICT_KIND_COMPLETION,
                status=RECON_STATUS_REQUIRED,
                reason_code="POST_COMPLETION_SETTLEMENT_CONFLICT",
                detected_at=evidence.recorded_at,
            )
            reconciliation_dict = rec_obj.to_dict()
            state["reconciliations_by_activity"].setdefault(act_id, []).append(reconciliation_dict)

        late_entry = {
            "activity_id": act_id,
            "terminal_status": act_status,
            "progress_evidence_id": evidence.progress_evidence_id,
            "source_ref": evidence.source_ref,
            "is_conflicting_correction": is_conflicting_correction,
            "reconciliation_id": reconciliation_dict["reconciliation_id"] if reconciliation_dict else None,
            "recorded_at": evidence.recorded_at,
        }
        state["late_terminal_evidence_log"].append(late_entry)
        state["consumed_evidences_by_id"][evidence.progress_evidence_id] = {
            "evidence": ev_dict,
            "bound_activity_id": act_id,
            "disposition": "COMPLETION_CONFLICT_RECORDED" if is_conflicting_correction else "LATE_TERMINAL_EVIDENCE_RECORDED",
        }
        state["evidence_id_by_idempotency_key"][evidence.idempotency_key] = evidence.progress_evidence_id

        self.progress_store.commit_mutation(
            lease=self.lease,
            capability=self.progress_capability,
            next_state=state,
            journal_payload={
                "event_type": (
                    "COMPLETION_CONFLICT_DETECTED"
                    if is_conflicting_correction
                    else "LATE_EVIDENCE_ON_TERMINAL_ACTIVITY"
                ),
                "activity_id": act_id,
                "terminal_status": act_status,
                "progress_evidence_id": evidence.progress_evidence_id,
                "reconciliation_id": reconciliation_dict["reconciliation_id"] if reconciliation_dict else None,
            },
            fault_hook=fault_hook,
        )

        return {
            "idempotent_replay": False,
            "disposition": (
                "COMPLETION_CONFLICT_RECORDED"
                if is_conflicting_correction
                else "LATE_TERMINAL_EVIDENCE_RECORDED"
            ),
            "activity_id": act_id,
            "progress_state": (
                PROGRESS_STATE_BLOCKED
                if is_conflicting_correction
                else self.read_service.get_progress_state(act_id)["progress_state"]
            ),
            "checkpoint_ref": activity.get("checkpoint_ref"),
            "completion_status": (
                RECON_STATUS_REQUIRED if is_conflicting_correction else act_status
            ),
            "reconciliation": reconciliation_dict,
            "activity_status": act_status,
            "completed_transition": False,
        }

    # -- AR-1 Outbox Consumer Protocol Implementation ------------------------

    def receive_settled_event(self, event: Mapping[str, Any]) -> ConsumerDispositionResult:
        """Consume an AR-1 SettledResultEvent / SettledResultCorrectionEvent from ResultSettlementService.dispatch_outbox."""
        event_id = str(event["event_id"])
        res = self.ingest_progress_evidence(event)
        disp_str = res["disposition"]
        if disp_str == "COMPLETION_CONFLICT_RECORDED" or res.get("progress_state") == PROGRESS_STATE_BLOCKED:
            disposition = DISPOSITION_CONFLICT
        elif disp_str in {"UNBOUND_EVIDENCE", "IRRELEVANT_TARGET_REF", "NO_BINDING_FOR_ACTIVITY", "ACTIVITY_ID_MISMATCH"}:
            disposition = DISPOSITION_IGNORE
        else:
            disposition = DISPOSITION_ACCEPT

        return ConsumerDispositionResult(
            consumer_domain=CONSUMER_DOMAIN_LIFE,
            event_id=event_id,
            disposition=disposition,
            idempotent_replay=bool(res.get("idempotent_replay", False)),
            downstream_proposal_ref=(
                res["completion_eligibility"]["eligibility_id"]
                if res.get("completion_eligibility")
                else f"life_prog:{sha256_hex(event_id)[:12]}"
            ),
            reason=disp_str,
        )

    # -- Cold-Start Recovery & Cross-Store Reconciliation (Sections 70-75) ---

    def recover(self) -> dict[str, Any]:
        """Recover WAL, reconcile pending checkpoint updates & open completion eligibilities
        against LR-2 CanonicalActivityStore, and optionally reconcile with AR-1 SettlementReadService (0 LLM).
        """
        LifeProgressAuthority.verify_capability(self.lease, self.progress_capability)
        self.progress_store.flush_snapshot()
        self.progress_store._verified_cache = None

        # 1. Recover LR-2 Activity store & LR-5 Progress store WAL
        act_state, _ = self.activity_store.load_verified_state_and_journal()
        wal_recovered = self.progress_store.recover_from_wal_if_needed()
        state = self.progress_store.load_state(_mutable=True)

        synced_checkpoints: list[str] = []
        completed_on_recovery: list[str] = []

        # 1b. Reconcile any staged evidence intake interrupted at after_evidence_consume / before_checkpoint_journal
        for pevid, pending_ev_dict in list(state.get("pending_evidence_eval_by_id", {}).items()):
            state = self.progress_store.load_state(_mutable=True)
            state.get("pending_evidence_eval_by_id", {}).pop(pevid, None)
            self.progress_store.commit_mutation(
                lease=self.lease,
                capability=self.progress_capability,
                next_state=state,
                journal_payload={
                    "event_type": "RECOVERY_PENDING_EVIDENCE_UNSTAGED",
                    "progress_evidence_id": pevid,
                },
            )
            res = self.ingest_progress_evidence(pending_ev_dict)
            if res.get("completed_transition") and res.get("activity_id"):
                completed_on_recovery.append(str(res["activity_id"]))
            state = self.progress_store.load_state(_mutable=True)

        # 2. Reconcile any pending checkpoint syncs interrupted between after_checkpoint_journal and after_checkpoint_activity_update
        for act_id, pending_chk in list(state["pending_checkpoint_sync_by_activity"].items()):
            cur_act = self.activity_read.get_activity(act_id)
            if cur_act is not None and cur_act["status"] in OPEN_ACTIVITY_STATUSES:
                if cur_act.get("checkpoint_ref") != pending_chk["checkpoint_ref"]:
                    _progress_port = getattr(self, "progress_consequence_port", None)
                    if _progress_port is None:
                        raise WriterCapabilityError(
                            "LR-5 checkpoint reconciliation requires a progress consequence port"
                        )
                    _progress_port.propose_checkpoint_or_progress(
                        "CHECKPOINT",
                        activity_id=act_id,
                        checkpoint_ref=pending_chk["checkpoint_ref"],
                        last_action_ref=pending_chk.get("last_action_ref"),
                        expected_revision=self._current_store_revision(),
                        idempotency_key=f"idem_lr2_chk_rec:{pending_chk['idempotency_key']}",
                        source_refs=pending_chk["source_refs"],
                        occurred_at=pending_chk.get("occurred_at"),
                        observed_at=pending_chk.get("observed_at"),
                    )
                    synced_checkpoints.append(act_id)
            state = self.progress_store.load_state(_mutable=True)
            state["pending_checkpoint_sync_by_activity"].pop(act_id, None)
            self.progress_store.commit_mutation(
                lease=self.lease,
                capability=self.progress_capability,
                next_state=state,
                journal_payload={
                    "event_type": "RECOVERY_CHECKPOINT_SYNCED",
                    "activity_id": act_id,
                },
            )

        # 3. Reconcile open or satisfied CompletionContracts (Section 72, K03, K04, K05)
        state = self.progress_store.load_state(_mutable=True)
        for act_id, comp_contract in list(state["completion_contracts_by_activity"].items()):
            cur_act = self.activity_read.get_activity(act_id)
            if cur_act is None:
                continue

            # If Activity is already COMPLETED in LR-2 store (e.g., crash happened after_complete_command / before_completion_evidence_persist):
            if cur_act["status"] == STATUS_COMPLETED:
                open_elig_id = state["open_eligibility_by_activity"].get(act_id)
                if open_elig_id and open_elig_id in state["completion_eligibilities_by_id"]:
                    elig = state["completion_eligibilities_by_id"][open_elig_id]
                    elig["status"] = COMP_ELIG_STATUS_CONSUMED
                    elig["consumed_at"] = _iso(now())
                    state["open_eligibility_by_activity"].pop(act_id, None)
                    state["completed_once_by_activity"][act_id] = {
                        "eligibility_id": open_elig_id,
                        "completion_evidence_ref": cur_act.get("completion_evidence_ref"),
                        "completed_at_revision": cur_act["revision"],
                    }
                    self.progress_store.commit_mutation(
                        lease=self.lease,
                        capability=self.progress_capability,
                        next_state=state,
                        journal_payload={
                            "event_type": "RECOVERY_FINALIZED_COMPLETED_ELIGIBILITY",
                            "activity_id": act_id,
                            "eligibility_id": open_elig_id,
                        },
                    )
                continue

            if cur_act["status"] in OPEN_ACTIVITY_STATUSES:
                # Re-evaluate completion criteria if not yet in open_eligibility_by_activity (e.g. crash before_completion_eligibility)
                open_elig_id = state["open_eligibility_by_activity"].get(act_id)
                if not open_elig_id:
                    active_evs = state["active_evidences_by_activity"].get(act_id, [])
                    comp_eval = NaturalCompletionEvaluator.evaluate_completion(
                        activity=cur_act,
                        completion_contract=comp_contract,
                        active_evidences=active_evs,
                        current_checkpoint_ref=cur_act.get("checkpoint_ref"),
                    )
                    if comp_eval["eval_status"] == EVAL_COMPLETION_ELIGIBLE:
                        elig_hash_input = f"{act_id}:{comp_contract['contract_id']}:v{comp_contract['revision']}"
                        elig_id = f"celig:{sha256_hex(elig_hash_input)[:16]}"
                        elig_obj = ActivityCompletionEligibility(
                            eligibility_id=elig_id,
                            activity_id=act_id,
                            activity_revision=int(cur_act["revision"]),
                            completion_contract_ref=str(comp_contract["contract_id"]),
                            completion_contract_revision=int(comp_contract["revision"]),
                            evidence_refs=comp_eval["satisfying_evidence_refs"],
                            settlement_refs=comp_eval["settlement_refs"],
                            authoritative_result_refs=comp_eval["authoritative_result_refs"],
                            created_at=_iso(now()),
                            policy_version=str(comp_contract["policy_version"]),
                            status=COMP_ELIG_STATUS_OPEN,
                        )
                        state["completion_eligibilities_by_id"][elig_id] = elig_obj.to_dict()
                        state["open_eligibility_by_activity"][act_id] = elig_id
                        self.progress_store.commit_mutation(
                            lease=self.lease,
                            capability=self.progress_capability,
                            next_state=state,
                            journal_payload={
                                "event_type": "RECOVERY_CREATED_COMPLETION_ELIGIBILITY",
                                "activity_id": act_id,
                                "eligibility_id": elig_id,
                            },
                        )
                        open_elig_id = elig_id

                if open_elig_id and self.auto_apply_natural_completion:
                    comp_res = self.execute_natural_completion(
                        activity_id=act_id,
                        eligibility_id=open_elig_id,
                    )
                    if comp_res["completed"]:
                        completed_on_recovery.append(act_id)

        # 4. Section 71: If settlement_read is attached, poll any committed outbox events not yet ingested
        if self.settlement_read is not None:
            for ev in self.settlement_read.list_outbox_events():
                self.ingest_progress_evidence(ev)

        final_state = self.progress_store.load_state(_mutable=True)
        return {
            "wal_recovered": wal_recovered,
            "synced_checkpoints": synced_checkpoints,
            "completed_on_recovery": completed_on_recovery,
            "progress_revision": final_state["revision"],
            "consumed_evidence_count": len(final_state["consumed_evidences_by_id"]),
        }

    def _current_store_revision(self) -> int:
        st, _ = self.activity_store.load_verified_state_and_journal(_include_journal=False)
        return int(st["revision"])


# ---------------------------------------------------------------------------
# Zero-Side-Effect ActivityProgressReplay (Sections 77, 78, 90)
# ---------------------------------------------------------------------------


class ActivityProgressReplay:
    """Sections 77, 78, 90: Rebuilds progress journal, latest checkpoints, and completion eligibility
    projections from journals with 0 mutations to canonical ActivityStore or external domains.
    """

    @staticmethod
    def replay_and_verify(
        *,
        activity_store: CanonicalActivityStore,
        progress_store: ProgressStore,
    ) -> dict[str, Any]:
        before_act_state, before_act_journal = activity_store.load_verified_state_and_journal()
        before_act_mtime = (
            activity_store.state_path.stat().st_mtime_ns
            if activity_store.state_path.exists()
            else None
        )

        prog_entries = progress_store.read_journal()
        rebuilt_prog_state = progress_store.rebuild_state_from_journal(prog_entries)
        live_prog_state = progress_store.load_state()

        after_act_state, after_act_journal = activity_store.load_verified_state_and_journal()
        after_act_mtime = (
            activity_store.state_path.stat().st_mtime_ns
            if activity_store.state_path.exists()
            else None
        )

        if before_act_state != after_act_state or before_act_journal != after_act_journal or before_act_mtime != after_act_mtime:
            raise ShadowMutationForbiddenError("ActivityProgressReplay mutated CanonicalActivityStore!")

        assert rebuilt_prog_state["revision"] == live_prog_state["revision"]
        assert rebuilt_prog_state["last_entry_hash"] == live_prog_state["last_entry_hash"]
        assert rebuilt_prog_state["progress_state_by_activity"] == live_prog_state["progress_state_by_activity"]
        assert rebuilt_prog_state["checkpoint_updates_by_activity"] == live_prog_state["checkpoint_updates_by_activity"]
        assert rebuilt_prog_state["completion_eligibilities_by_id"] == live_prog_state["completion_eligibilities_by_id"]

        # Section 75: Verify State + Progress Consistency across stores
        for act_id, act in after_act_state.get("activities", {}).items():
            if act.get("status") == STATUS_COMPLETED:
                if not act.get("completion_evidence_ref"):
                    raise RecoveryRequiredError(
                        "COMPLETED_WITHOUT_EVIDENCE_REF",
                        f"Activity {act_id!r} is COMPLETED without completion_evidence_ref",
                    )

        return {
            "consistent": True,
            "production_mutation_count": 0,
            "progress_journal_count": len(prog_entries),
            "rebuilt_progress_state_by_activity": copy.deepcopy(
                rebuilt_prog_state["progress_state_by_activity"]
            ),
            "rebuilt_checkpoint_updates_by_activity": copy.deepcopy(
                rebuilt_prog_state["checkpoint_updates_by_activity"]
            ),
            "rebuilt_completion_eligibilities_by_id": copy.deepcopy(
                rebuilt_prog_state["completion_eligibilities_by_id"]
            ),
            "completed_activities_in_canonical": {
                aid: a["completion_evidence_ref"]
                for aid, a in after_act_state.get("activities", {}).items()
                if a.get("status") == STATUS_COMPLETED
            },
        }

    @staticmethod
    def simulate_natural_completion_in_replay(
        *,
        activity_store: CanonicalActivityStore,
        progress_store: ProgressStore,
        activity_id: str,
        simulated_evidences: Sequence[LifeProgressEvidence],
    ) -> dict[str, Any]:
        """Section 78: Simulate natural completion in replay mode (`would_complete = True/False`)
        without calling `ActivityCommandService.complete_activity()` or touching canonical files.
        """
        before_act_state, _ = activity_store.load_verified_state_and_journal()
        prog_state = progress_store.load_state()

        act = before_act_state.get("activities", {}).get(activity_id)
        if act is None:
            raise ContractViolationError(f"activity_id={activity_id!r} not found")

        comp_contract = prog_state["completion_contracts_by_activity"].get(activity_id)
        prog_contract = prog_state["progress_contracts_by_activity"].get(activity_id)
        active_evs = list(prog_state["active_evidences_by_activity"].get(activity_id, []))
        chk = act.get("checkpoint_ref")

        for ev in simulated_evidences:
            ev_d = ev.to_dict()
            active_evs.append(ev_d)
            p_res = NaturalProgressEvaluator.evaluate_progress(
                activity=act,
                progress_contract=prog_contract,
                evidence=ev,
                prior_checkpoint_ref=chk,
                active_evidences=active_evs,
            )
            if p_res["new_checkpoint_ref"] is not None:
                chk = p_res["new_checkpoint_ref"]

        c_res = NaturalCompletionEvaluator.evaluate_completion(
            activity=act,
            completion_contract=comp_contract,
            active_evidences=active_evs,
            current_checkpoint_ref=chk,
        )

        after_act_state, _ = activity_store.load_verified_state_and_journal()
        assert before_act_state == after_act_state, "Replay simulation must NEVER mutate canonical store"

        return {
            "replay_simulation": True,
            "activity_id": activity_id,
            "simulated_checkpoint_ref": chk,
            "completion_eval_status": c_res["eval_status"],
            "would_complete": c_res["eval_status"] == EVAL_COMPLETION_ELIGIBLE,
            "production_mutation_count": 0,
        }
