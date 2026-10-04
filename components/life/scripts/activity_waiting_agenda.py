#!/usr/bin/env python3
"""Chiyo Life Runtime | LR-4 Waiting / Resume / Agenda.

Formal Stage: Waiting / Resume / Agenda

Solves one strictly bounded problem:
When an Activity Chiyo is doing cannot currently proceed due to a real blocking
condition, how the runtime waits quietly (0 LLM), and when the condition or time
later changes, how the runtime deterministically produces a `ResumeEligibility`
("worth considering resuming") without ever mechanically auto-resuming.

Core Causal Chain:
    Activity -> WAITING -> ResumeCondition -> Agenda / Event Fabric
    -> RESUME_ELIGIBLE (ResumeEligibility) -> Future Decision / Adoption
    -> Explicit RESUME (via LR-2 ActivityCommandService)

Permanently Forbidden Shortcuts:
- Condition SATISFIED -> ACTIVE
- Agenda DUE -> RESUME
- RESUME_ELIGIBLE -> Activity.status
- Hidden World fact (unobserved by Chiyo) -> Condition SATISFIED
- Waiting / Due Scanner -> LLM polling or outbound messages
"""

from __future__ import annotations

import copy
import errno
import fcntl
import hashlib
import hmac
import json
import os
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Optional, Sequence

from activity_compat import (
    ActivityError,
    ActivityLockError,
    ActivityTransitionError,
    _compact_value,
    _iso,
    _parse,
    now,
)
from activity_admission import (
    ActivityConsequencePort,
    CompletionProposal,
    InterruptionProposal,
    REASON_KIND_AGENCY_CHOICE,
    REASON_KIND_RUNTIME_CONSTRAINT,
    WaitingProposal,
)
from activity_continuity import (
    ALL_ACTIVITY_STATUSES,
    COMMIT_INTENT_FILENAME,
    FORBIDDEN_ADOPTION_PREFIXES,
    GENESIS_HASH,
    GLOBAL_SUBJECT_ID,
    LIFE_STATE_IDLE,
    LIFE_STATE_WAITING,
    NAMESPACE_CANONICAL,
    NAMESPACE_ISOLATED_TEST,
    NON_PRODUCTION_NAMESPACES,
    OPEN_ACTIVITY_STATUSES,
    PRODUCTION_CUTOVER_ENV,
    STATUS_ACTIVE,
    STATUS_PAUSED,
    STATUS_WAITING,
    TERMINAL_ACTIVITY_STATUSES,
    TRANSITION_RESUME,
    VALID_ADOPTION_PREFIXES,
    VALID_DECISION_PREFIXES,
    WRITER_DOMAIN_CANONICAL,
    ActivityAuthorityLease,
    ActivityCommandService,
    ActivityReadService,
    AdoptionProvenanceError,
    CanonicalActivityStore,
    CommandResult,
    ContractViolationError,
    ForegroundConflictError,
    IdempotencyConflictError,
    RecoveryRequiredError,
    RevisionConflictError,
    SubjectIdentityError,
    TerminalActivityImmutableError,
    WriterCapabilityError,
    _fsync_dir,
    _validate_structured_ref,
    build_life_frame_read_view,
    canonical_json_line,
    is_production_path,
    normalize_ref_list,
    sha256_hex,
    validate_adoption_provenance,
    validate_subject_id,
)

# ---------------------------------------------------------------------------
# Section 2, 5, 12, 15, 45: Frozen Stage Name, Schemas & Authority Domains
# ---------------------------------------------------------------------------

FORMAL_STAGE_NAME = "Waiting / Resume / Agenda"
POLICY_VERSION_LR4 = "chiyo.life.waiting_resume_policy.v1"

SCHEMA_RESUME_CONDITION = "chiyo.life.resume_condition.v1"
SCHEMA_RESUME_CONDITION_STATE = "chiyo.life.resume_condition_state.v1"
SCHEMA_RESUME_CONDITION_TRANSITION = "chiyo.life.resume_condition_transition.v1"

SCHEMA_AGENDA_ITEM = "chiyo.life.agenda_item.v1"
SCHEMA_AGENDA_STATE = "chiyo.life.agenda_state.v1"
SCHEMA_AGENDA_TRANSITION = "chiyo.life.agenda_transition.v1"

SCHEMA_RESUME_ELIGIBILITY = "chiyo.life.resume_eligibility.v1"
SCHEMA_RESUME_ELIGIBILITY_STATE = "chiyo.life.resume_eligibility_state.v1"
SCHEMA_RESUME_ELIGIBILITY_TRANSITION = "chiyo.life.resume_eligibility_transition.v1"

SCHEMA_LR4_COMMIT_INTENT = "chiyo.life.lr4_commit_intent.v1"

# Section 45: Four Independent Canonical Owners
WRITER_DOMAIN_ACTIVITY = WRITER_DOMAIN_CANONICAL
WRITER_DOMAIN_RESUME_CONDITION = "life_resume_condition_authority"
WRITER_DOMAIN_AGENDA = "life_agenda_authority"
WRITER_DOMAIN_ELIGIBILITY = "life_resume_eligibility_authority"

LR4_WRITER_DOMAINS = frozenset(
    {
        WRITER_DOMAIN_RESUME_CONDITION,
        WRITER_DOMAIN_AGENDA,
        WRITER_DOMAIN_ELIGIBILITY,
    }
)

# Section 6: ResumeCondition Lifecycle
COND_STATUS_PENDING = "PENDING"
COND_STATUS_SATISFIED = "SATISFIED"
COND_STATUS_CANCELLED = "CANCELLED"
COND_STATUS_EXPIRED = "EXPIRED"
COND_STATUS_SUPERSEDED = "SUPERSEDED"
COND_STATUS_STALE = "STALE"

OPEN_CONDITION_STATUSES = (COND_STATUS_PENDING,)
TERMINAL_CONDITION_STATUSES = (
    COND_STATUS_SATISFIED,
    COND_STATUS_CANCELLED,
    COND_STATUS_EXPIRED,
    COND_STATUS_SUPERSEDED,
    COND_STATUS_STALE,
)
ALL_CONDITION_STATUSES = OPEN_CONDITION_STATUSES + TERMINAL_CONDITION_STATUSES

# Section 7 & 34: Bounded Condition Kinds
COND_KIND_EVENT_MATCH = "EVENT_MATCH"
COND_KIND_TIME_AT_OR_AFTER = "TIME_AT_OR_AFTER"
COND_KIND_TIME_WINDOW_OPEN = "TIME_WINDOW_OPEN"
COND_KIND_EXTERNAL_RESULT_EVENT = "EXTERNAL_RESULT_EVENT"
COND_KIND_OBSERVED_CONDITION = "OBSERVED_CONDITION"
COND_KIND_ALL = "ALL"
COND_KIND_ANY = "ANY"

LEAF_CONDITION_KINDS = (
    COND_KIND_EVENT_MATCH,
    COND_KIND_TIME_AT_OR_AFTER,
    COND_KIND_TIME_WINDOW_OPEN,
    COND_KIND_EXTERNAL_RESULT_EVENT,
    COND_KIND_OBSERVED_CONDITION,
)
COMPOSITE_CONDITION_KINDS = (COND_KIND_ALL, COND_KIND_ANY)
ALL_CONDITION_KINDS = LEAF_CONDITION_KINDS + COMPOSITE_CONDITION_KINDS

MAX_COMPOSITE_DEPTH = 2
MAX_COMPOSITE_CHILDREN = 8

# Forbidden dynamic evaluation keys (Section 7)
FORBIDDEN_CONDITION_PARAM_KEYS = frozenset(
    {
        "eval",
        "exec",
        "python_eval",
        "sql",
        "sql_query",
        "llm_prompt",
        "prompt",
        "model_prompt",
        "lambda",
        "function",
        "script",
        "score_weight",
        "priority_score",
        "argmax",
    }
)

# Section 13: AgendaItem Lifecycle
AGENDA_STATUS_SCHEDULED = "SCHEDULED"
AGENDA_STATUS_DUE = "DUE"
AGENDA_STATUS_CONSUMED = "CONSUMED"
AGENDA_STATUS_CANCELLED = "CANCELLED"
AGENDA_STATUS_EXPIRED = "EXPIRED"
AGENDA_STATUS_SUPERSEDED = "SUPERSEDED"

OPEN_AGENDA_STATUSES = (AGENDA_STATUS_SCHEDULED, AGENDA_STATUS_DUE)
TERMINAL_AGENDA_STATUSES = (
    AGENDA_STATUS_CONSUMED,
    AGENDA_STATUS_CANCELLED,
    AGENDA_STATUS_EXPIRED,
    AGENDA_STATUS_SUPERSEDED,
)
ALL_AGENDA_STATUSES = OPEN_AGENDA_STATUSES + TERMINAL_AGENDA_STATUSES

# Section 12: AgendaItem Trigger Kinds
TRIGGER_KIND_TIME_AT = "TIME_AT"
TRIGGER_KIND_TIME_WINDOW = "TIME_WINDOW"
TRIGGER_KIND_RECHECK_AT = "RECHECK_AT"
ALL_TRIGGER_KINDS = (
    TRIGGER_KIND_TIME_AT,
    TRIGGER_KIND_TIME_WINDOW,
    TRIGGER_KIND_RECHECK_AT,
)

# Section 16: ResumeEligibility Lifecycle
ELIG_STATUS_OPEN = "OPEN"
ELIG_STATUS_CONSUMED = "CONSUMED"
ELIG_STATUS_STALE = "STALE"
ELIG_STATUS_CANCELLED = "CANCELLED"
ELIG_STATUS_EXPIRED = "EXPIRED"
ELIG_STATUS_SUPERSEDED = "SUPERSEDED"

OPEN_ELIGIBILITY_STATUSES = (ELIG_STATUS_OPEN,)
TERMINAL_ELIGIBILITY_STATUSES = (
    ELIG_STATUS_CONSUMED,
    ELIG_STATUS_STALE,
    ELIG_STATUS_CANCELLED,
    ELIG_STATUS_EXPIRED,
    ELIG_STATUS_SUPERSEDED,
)
ALL_ELIGIBILITY_STATUSES = OPEN_ELIGIBILITY_STATUSES + TERMINAL_ELIGIBILITY_STATUSES

# Normalized Event Categories
EVENT_CATEGORY_EXTERNAL_RESULT = "EXTERNAL_RESULT_EVENT"
EVENT_CATEGORY_WORLD_OBSERVATION = "WORLD_OBSERVATION_EVENT"
EVENT_CATEGORY_HIDDEN_WORLD_FACT = "HIDDEN_WORLD_FACT"
EVENT_CATEGORY_GENERIC_EVENT = "GENERIC_EVENT"
EVENT_CATEGORY_USER_RESUME_HINT = "USER_RESUME_HINT"
EVENT_CATEGORY_MEMORY_RECALL = "MEMORY_RECALL_EVENT"

VALID_OBSERVATION_PREFIXES = frozenset({"obs", "percept", "world_obs", "perception", "seen"})


# ---------------------------------------------------------------------------
# Specific LR-4 Exceptions
# ---------------------------------------------------------------------------


class WaitingAgendaError(ActivityError):
    """Base error for LR-4 Waiting / Resume / Agenda violations."""


class ConditionValidationError(ContractViolationError):
    """Raised when a ResumeCondition violates bounded deterministic rules."""


class StaleResumeConditionError(WaitingAgendaError):
    """Raised when a ResumeCondition targets a stale Activity revision or episode."""


class EligibilityNotOpenError(WaitingAgendaError):
    """Raised when attempting to consume a non-OPEN or stale ResumeEligibility."""


class WorldObservationBoundaryError(WaitingAgendaError):
    """Raised when a hidden/unobserved World fact attempts to satisfy an OBSERVED_CONDITION."""


class AgendaAuthorityViolationError(WriterCapabilityError):
    """Raised when Agenda or Condition evaluators attempt direct Activity mutations."""


# ---------------------------------------------------------------------------
# Section 45 & 46: LR-4 Aggregate Writer Capability Model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LR4WriterCapability:
    """Domain-bound writer capability for a specific LR-4 aggregate store.

    Cannot be used across aggregate boundaries and is permanently rejected by
    LR-2 ActivityCommandService (Agenda/Condition/Eligibility cannot call RESUME).
    """

    writer_domain: str
    namespace: str
    store_root: Path
    lease_instance_id: str
    _signature: str = field(repr=False)


def issue_lr4_writer_capability(
    *,
    lease: ActivityAuthorityLease,
    writer_domain: str,
    namespace: str = NAMESPACE_ISOLATED_TEST,
    allow_production_cutover: bool = False,
) -> LR4WriterCapability:
    """Issue a strictly scoped writer capability for one LR-4 aggregate owner."""
    if not isinstance(lease, ActivityAuthorityLease) or not lease.held:
        raise WriterCapabilityError("cannot issue LR4WriterCapability without a held ActivityAuthorityLease")

    domain = str(writer_domain or "").strip().lower()
    if domain not in LR4_WRITER_DOMAINS:
        raise WriterCapabilityError(
            f"writer_domain={writer_domain!r} is not a valid LR-4 aggregate authority domain; "
            f"expected one of {sorted(LR4_WRITER_DOMAINS)}"
        )

    ns = str(namespace or "").strip().lower()
    if ns in NON_PRODUCTION_NAMESPACES:
        raise WriterCapabilityError(
            f"namespace={namespace!r} (Shadow/Replay/Research) is permanently DENIED "
            f"LR-4 canonical write capability"
        )
    if ns not in {NAMESPACE_CANONICAL, NAMESPACE_ISOLATED_TEST}:
        raise WriterCapabilityError(f"unsupported LR-4 writer namespace={namespace!r}")

    if is_production_path(lease.store_root):
        cutover_env = os.environ.get(PRODUCTION_CUTOVER_ENV, "off").strip().lower()
        if not allow_production_cutover or cutover_env != "canonical":
            raise WriterCapabilityError(
                f"LR-4 production non-cutover guard: writing to production path {lease.store_root} "
                f"is blocked while {PRODUCTION_CUTOVER_ENV}={cutover_env!r}"
            )

    sig = lease.sign_capability(domain, ns)
    return LR4WriterCapability(
        writer_domain=domain,
        namespace=ns,
        store_root=lease.store_root,
        lease_instance_id=lease.instance_id,
        _signature=sig,
    )


# ---------------------------------------------------------------------------
# Timestamp & Validation Helpers
# ---------------------------------------------------------------------------


def _require_iso(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ContractViolationError(f"{field_name} must be a non-empty ISO-8601 timestamp string")
    parsed = _parse(value)
    if parsed is None:
        raise ContractViolationError(f"{field_name} is not a valid ISO-8601 timestamp: {value!r}")
    return _iso(parsed)


def _optional_iso(value: Any, field_name: str) -> Optional[str]:
    if value is None:
        return None
    return _require_iso(value, field_name)


def _ts_epoch(iso_str: Optional[str]) -> float:
    if not iso_str:
        return 0.0
    dt = _parse(iso_str)
    if dt is None:
        raise ContractViolationError(f"invalid timestamp string: {iso_str!r}")
    return dt.timestamp()


def _validate_condition_parameters(
    condition_kind: str,
    parameters: Mapping[str, Any],
    *,
    waiting_on_ref: Optional[str],
    depth: int = 0,
) -> dict[str, Any]:
    """Enforce Section 7 & 34 bounded deterministic condition parameter rules."""
    if depth > MAX_COMPOSITE_DEPTH:
        raise ConditionValidationError(
            f"composite condition depth {depth} exceeds MAX_COMPOSITE_DEPTH={MAX_COMPOSITE_DEPTH}"
        )
    if not isinstance(parameters, Mapping):
        raise ConditionValidationError("condition parameters must be a mapping")

    forbidden = FORBIDDEN_CONDITION_PARAM_KEYS.intersection(
        {str(k).strip().lower() for k in parameters.keys()}
    )
    if forbidden:
        raise ConditionValidationError(
            f"forbidden dynamic/LLM/SQL/scoring keys in ResumeCondition.parameters: {sorted(forbidden)}"
        )

    for k, v in parameters.items():
        if callable(v):
            raise ConditionValidationError(f"callable parameter {k!r} is forbidden in ResumeCondition")

    norm_params = copy.deepcopy(dict(parameters))

    if condition_kind == COND_KIND_TIME_AT_OR_AFTER:
        target_time = norm_params.get("target_time") or norm_params.get("due_at")
        if not target_time:
            raise ConditionValidationError("TIME_AT_OR_AFTER requires 'target_time' (or 'due_at') in parameters")
        norm_params["target_time"] = _require_iso(target_time, "parameters.target_time")

    elif condition_kind == COND_KIND_TIME_WINDOW_OPEN:
        ws = norm_params.get("window_start")
        we = norm_params.get("window_end")
        if not ws or not we:
            raise ConditionValidationError("TIME_WINDOW_OPEN requires 'window_start' and 'window_end'")
        norm_ws = _require_iso(ws, "parameters.window_start")
        norm_we = _require_iso(we, "parameters.window_end")
        if _ts_epoch(norm_ws) > _ts_epoch(norm_we):
            raise ConditionValidationError("window_start cannot be after window_end")
        norm_params["window_start"] = norm_ws
        norm_params["window_end"] = norm_we

    elif condition_kind == COND_KIND_EXTERNAL_RESULT_EVENT:
        target_ref = norm_params.get("waiting_on_ref") or waiting_on_ref
        if not target_ref:
            raise ConditionValidationError("EXTERNAL_RESULT_EVENT requires a structured waiting_on_ref")
        norm_params["waiting_on_ref"] = _validate_structured_ref(
            target_ref,
            "waiting_on_ref",
            error_cls=ConditionValidationError,
        )

    elif condition_kind == COND_KIND_OBSERVED_CONDITION:
        fact_key = norm_params.get("fact_key")
        if not isinstance(fact_key, str) or not fact_key.strip():
            raise ConditionValidationError("OBSERVED_CONDITION requires a non-empty 'fact_key' in parameters")
        if "expected_value" not in norm_params:
            raise ConditionValidationError("OBSERVED_CONDITION requires 'expected_value' in parameters")
        if norm_params.get("subject_ref") is not None:
            norm_params["subject_ref"] = _validate_structured_ref(
                norm_params["subject_ref"],
                "parameters.subject_ref",
                error_cls=ConditionValidationError,
            )

    elif condition_kind == COND_KIND_EVENT_MATCH:
        expected_event_kind = norm_params.get("expected_event_kind")
        expected_ref = norm_params.get("waiting_on_ref") or waiting_on_ref
        if not expected_event_kind and not expected_ref:
            raise ConditionValidationError(
                "EVENT_MATCH requires at least 'expected_event_kind' or 'waiting_on_ref'"
            )
        if expected_ref is not None:
            norm_params["waiting_on_ref"] = _validate_structured_ref(
                expected_ref,
                "waiting_on_ref",
                error_cls=ConditionValidationError,
            )

    elif condition_kind in COMPOSITE_CONDITION_KINDS:
        if depth + 1 > MAX_COMPOSITE_DEPTH:
            raise ConditionValidationError(
                f"composite condition nesting exceeds MAX_COMPOSITE_DEPTH={MAX_COMPOSITE_DEPTH}"
            )
        sub_list = norm_params.get("subconditions")
        if not isinstance(sub_list, Sequence) or isinstance(sub_list, (str, bytes)):
            raise ConditionValidationError(f"{condition_kind} requires a 'subconditions' sequence")
        if len(sub_list) < 1 or len(sub_list) > MAX_COMPOSITE_CHILDREN:
            raise ConditionValidationError(
                f"{condition_kind} subconditions count must be between 1 and {MAX_COMPOSITE_CHILDREN}"
            )
        normalized_subs: list[dict[str, Any]] = []
        seen_sub_ids: set[str] = set()
        for idx, sub in enumerate(sub_list):
            if not isinstance(sub, Mapping):
                raise ConditionValidationError(f"subcondition[{idx}] must be a mapping")
            sub_id = str(sub.get("subcondition_id") or f"sub_{idx}").strip()
            if not sub_id or sub_id in seen_sub_ids:
                raise ConditionValidationError(f"duplicate or empty subcondition_id={sub_id!r}")
            seen_sub_ids.add(sub_id)
            sub_kind = str(sub.get("condition_kind") or "").strip()
            if sub_kind not in ALL_CONDITION_KINDS:
                raise ConditionValidationError(f"invalid subcondition kind={sub_kind!r}")
            sub_wait_ref = sub.get("waiting_on_ref")
            if sub_wait_ref is not None:
                sub_wait_ref = _validate_structured_ref(
                    sub_wait_ref,
                    f"subcondition[{idx}].waiting_on_ref",
                    error_cls=ConditionValidationError,
                )
            sub_params = _validate_condition_parameters(
                sub_kind,
                sub.get("parameters") or {},
                waiting_on_ref=sub_wait_ref,
                depth=depth + 1,
            )
            normalized_subs.append(
                {
                    "subcondition_id": sub_id,
                    "condition_kind": sub_kind,
                    "waiting_on_ref": sub_wait_ref,
                    "parameters": sub_params,
                }
            )
        norm_params["subconditions"] = normalized_subs
    else:
        raise ConditionValidationError(f"unsupported condition_kind={condition_kind!r}")

    return norm_params


# ---------------------------------------------------------------------------
# Section 5, 12, 15, 21: Canonical Data Models
# ---------------------------------------------------------------------------


@dataclass
class ResumeCondition:
    """Independent canonical aggregate representing a condition that unblocks a WAITING Activity.

    SATISFIED != Activity resumed.
    """

    condition_id: str
    schema_version: str
    revision: int
    subject_id: str
    activity_id: str
    activity_revision_at_bind: int
    waiting_episode_id: str
    condition_kind: str
    source_refs: list[str]
    causal_parent_refs: list[str]
    waiting_on_ref: Optional[str]
    parameters: dict[str, Any]
    status: str
    created_at: str
    observed_at: Optional[str] = None
    satisfied_at: Optional[str] = None
    cancelled_at: Optional[str] = None
    expires_at: Optional[str] = None
    last_event_ref: Optional[str] = None
    satisfied_subcondition_ids: list[str] = field(default_factory=list)
    resolution_reason_code: Optional[str] = None
    idempotency_key: str = ""

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_RESUME_CONDITION:
            raise ConditionValidationError(f"invalid ResumeCondition schema_version={self.schema_version!r}")
        self.subject_id = validate_subject_id(self.subject_id)
        self.condition_id = _validate_structured_ref(
            self.condition_id,
            "condition_id",
            allowed_prefixes=frozenset({"rcond", "resume_cond"}),
            error_cls=ConditionValidationError,
        )
        if not isinstance(self.activity_id, str) or not self.activity_id.strip():
            raise ConditionValidationError("activity_id must be non-empty")
        if isinstance(self.activity_revision_at_bind, bool) or not isinstance(self.activity_revision_at_bind, int) or self.activity_revision_at_bind < 1:
            raise ConditionValidationError("activity_revision_at_bind must be a positive integer")
        self.waiting_episode_id = _validate_structured_ref(
            self.waiting_episode_id,
            "waiting_episode_id",
            allowed_prefixes=frozenset({"wep", "wait_ep"}),
            error_cls=ConditionValidationError,
        )
        if self.condition_kind not in ALL_CONDITION_KINDS:
            raise ConditionValidationError(f"invalid condition_kind={self.condition_kind!r}")
        if self.status not in ALL_CONDITION_STATUSES:
            raise ConditionValidationError(f"invalid ResumeCondition status={self.status!r}")
        self.source_refs = normalize_ref_list(self.source_refs, "source_refs", allow_empty=False)
        self.causal_parent_refs = normalize_ref_list(
            self.causal_parent_refs, "causal_parent_refs", allow_empty=True
        )
        if self.waiting_on_ref is not None:
            self.waiting_on_ref = _validate_structured_ref(
                self.waiting_on_ref,
                "waiting_on_ref",
                error_cls=ConditionValidationError,
            )
        self.parameters = _validate_condition_parameters(
            self.condition_kind,
            self.parameters,
            waiting_on_ref=self.waiting_on_ref,
        )
        self.created_at = _require_iso(self.created_at, "created_at")
        self.observed_at = _optional_iso(self.observed_at, "observed_at")
        self.satisfied_at = _optional_iso(self.satisfied_at, "satisfied_at")
        self.cancelled_at = _optional_iso(self.cancelled_at, "cancelled_at")
        self.expires_at = _optional_iso(self.expires_at, "expires_at")
        if self.last_event_ref is not None:
            self.last_event_ref = _validate_structured_ref(
                self.last_event_ref,
                "last_event_ref",
                error_cls=ConditionValidationError,
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "condition_id": self.condition_id,
            "schema_version": self.schema_version,
            "revision": self.revision,
            "subject_id": self.subject_id,
            "activity_id": self.activity_id,
            "activity_revision_at_bind": self.activity_revision_at_bind,
            "waiting_episode_id": self.waiting_episode_id,
            "condition_kind": self.condition_kind,
            "source_refs": list(self.source_refs),
            "causal_parent_refs": list(self.causal_parent_refs),
            "waiting_on_ref": self.waiting_on_ref,
            "parameters": copy.deepcopy(self.parameters),
            "status": self.status,
            "created_at": self.created_at,
            "observed_at": self.observed_at,
            "satisfied_at": self.satisfied_at,
            "cancelled_at": self.cancelled_at,
            "expires_at": self.expires_at,
            "last_event_ref": self.last_event_ref,
            "satisfied_subcondition_ids": list(self.satisfied_subcondition_ids),
            "resolution_reason_code": self.resolution_reason_code,
            "idempotency_key": self.idempotency_key,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ResumeCondition":
        return cls(
            condition_id=str(data["condition_id"]),
            schema_version=str(data.get("schema_version", SCHEMA_RESUME_CONDITION)),
            revision=int(data["revision"]),
            subject_id=str(data.get("subject_id", GLOBAL_SUBJECT_ID)),
            activity_id=str(data["activity_id"]),
            activity_revision_at_bind=int(data["activity_revision_at_bind"]),
            waiting_episode_id=str(data["waiting_episode_id"]),
            condition_kind=str(data["condition_kind"]),
            source_refs=list(data.get("source_refs") or []),
            causal_parent_refs=list(data.get("causal_parent_refs") or []),
            waiting_on_ref=data.get("waiting_on_ref"),
            parameters=dict(data.get("parameters") or {}),
            status=str(data["status"]),
            created_at=str(data["created_at"]),
            observed_at=data.get("observed_at"),
            satisfied_at=data.get("satisfied_at"),
            cancelled_at=data.get("cancelled_at"),
            expires_at=data.get("expires_at"),
            last_event_ref=data.get("last_event_ref"),
            satisfied_subcondition_ids=list(data.get("satisfied_subcondition_ids") or []),
            resolution_reason_code=data.get("resolution_reason_code"),
            idempotency_key=str(data.get("idempotency_key") or ""),
        )


@dataclass
class AgendaItem:
    """Independent canonical aggregate representing when a waiting item is worth re-evaluating.

    DUE != RESUMED. Agenda has ZERO Activity write authority.
    """

    agenda_id: str
    schema_version: str
    revision: int
    subject_id: str
    activity_id: Optional[str]
    activity_revision_at_bind: Optional[int]
    waiting_episode_id: Optional[str]
    resume_condition_ref: Optional[str]
    trigger_kind: str
    due_at: Optional[str]
    window_start: Optional[str]
    window_end: Optional[str]
    source_refs: list[str]
    causal_parent_refs: list[str]
    status: str
    created_at: str
    due_observed_at: Optional[str] = None
    consumed_at: Optional[str] = None
    cancelled_at: Optional[str] = None
    expires_at: Optional[str] = None
    resolution_reason_code: Optional[str] = None
    idempotency_key: str = ""

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_AGENDA_ITEM:
            raise ContractViolationError(f"invalid AgendaItem schema_version={self.schema_version!r}")
        self.subject_id = validate_subject_id(self.subject_id)
        self.agenda_id = _validate_structured_ref(
            self.agenda_id,
            "agenda_id",
            allowed_prefixes=frozenset({"agnd", "agenda"}),
        )
        if self.trigger_kind not in ALL_TRIGGER_KINDS:
            raise ContractViolationError(f"invalid trigger_kind={self.trigger_kind!r}")
        if self.status not in ALL_AGENDA_STATUSES:
            raise ContractViolationError(f"invalid AgendaItem status={self.status!r}")
        if self.resume_condition_ref is not None:
            self.resume_condition_ref = _validate_structured_ref(
                self.resume_condition_ref,
                "resume_condition_ref",
                allowed_prefixes=frozenset({"rcond", "resume_cond"}),
            )
        if self.waiting_episode_id is not None:
            self.waiting_episode_id = _validate_structured_ref(
                self.waiting_episode_id,
                "waiting_episode_id",
                allowed_prefixes=frozenset({"wep", "wait_ep"}),
            )
        self.source_refs = normalize_ref_list(self.source_refs, "source_refs", allow_empty=False)
        self.causal_parent_refs = normalize_ref_list(
            self.causal_parent_refs, "causal_parent_refs", allow_empty=True
        )
        self.created_at = _require_iso(self.created_at, "created_at")
        self.due_at = _optional_iso(self.due_at, "due_at")
        self.window_start = _optional_iso(self.window_start, "window_start")
        self.window_end = _optional_iso(self.window_end, "window_end")
        self.due_observed_at = _optional_iso(self.due_observed_at, "due_observed_at")
        self.consumed_at = _optional_iso(self.consumed_at, "consumed_at")
        self.cancelled_at = _optional_iso(self.cancelled_at, "cancelled_at")
        self.expires_at = _optional_iso(self.expires_at, "expires_at")

        if self.trigger_kind == TRIGGER_KIND_TIME_WINDOW:
            if not self.window_start or not self.window_end:
                raise ContractViolationError("TIME_WINDOW AgendaItem requires window_start and window_end")
            if _ts_epoch(self.window_start) > _ts_epoch(self.window_end):
                raise ContractViolationError("AgendaItem window_start cannot be after window_end")
            if self.due_at is None:
                self.due_at = self.window_start
            if self.expires_at is None:
                self.expires_at = self.window_end
        else:
            if not self.due_at:
                raise ContractViolationError(f"{self.trigger_kind} AgendaItem requires due_at")

        if not isinstance(self.idempotency_key, str) or not self.idempotency_key.strip():
            raise ContractViolationError("AgendaItem requires a non-empty idempotency_key")

    def to_dict(self) -> dict[str, Any]:
        return {
            "agenda_id": self.agenda_id,
            "schema_version": self.schema_version,
            "revision": self.revision,
            "subject_id": self.subject_id,
            "activity_id": self.activity_id,
            "activity_revision_at_bind": self.activity_revision_at_bind,
            "waiting_episode_id": self.waiting_episode_id,
            "resume_condition_ref": self.resume_condition_ref,
            "trigger_kind": self.trigger_kind,
            "due_at": self.due_at,
            "window_start": self.window_start,
            "window_end": self.window_end,
            "source_refs": list(self.source_refs),
            "causal_parent_refs": list(self.causal_parent_refs),
            "status": self.status,
            "created_at": self.created_at,
            "due_observed_at": self.due_observed_at,
            "consumed_at": self.consumed_at,
            "cancelled_at": self.cancelled_at,
            "expires_at": self.expires_at,
            "resolution_reason_code": self.resolution_reason_code,
            "idempotency_key": self.idempotency_key,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "AgendaItem":
        return cls(
            agenda_id=str(data["agenda_id"]),
            schema_version=str(data.get("schema_version", SCHEMA_AGENDA_ITEM)),
            revision=int(data["revision"]),
            subject_id=str(data.get("subject_id", GLOBAL_SUBJECT_ID)),
            activity_id=data.get("activity_id"),
            activity_revision_at_bind=(
                int(data["activity_revision_at_bind"])
                if data.get("activity_revision_at_bind") is not None
                else None
            ),
            waiting_episode_id=data.get("waiting_episode_id"),
            resume_condition_ref=data.get("resume_condition_ref"),
            trigger_kind=str(data["trigger_kind"]),
            due_at=data.get("due_at"),
            window_start=data.get("window_start"),
            window_end=data.get("window_end"),
            source_refs=list(data.get("source_refs") or []),
            causal_parent_refs=list(data.get("causal_parent_refs") or []),
            status=str(data["status"]),
            created_at=str(data["created_at"]),
            due_observed_at=data.get("due_observed_at"),
            consumed_at=data.get("consumed_at"),
            cancelled_at=data.get("cancelled_at"),
            expires_at=data.get("expires_at"),
            resolution_reason_code=data.get("resolution_reason_code"),
            idempotency_key=str(data["idempotency_key"]),
        )


@dataclass
class ResumeEligibility:
    """Independent canonical aggregate expressing that a WAITING Activity is now eligible to be reconsidered.

    It is NEVER Activity.status, ResumeCondition.status, or AgendaItem.status.
    It does NOT mean Chiyo has decided to resume.
    """

    eligibility_id: str
    schema_version: str
    revision: int
    subject_id: str
    activity_id: str
    activity_revision: int
    waiting_episode_id: str
    resume_condition_ref: str
    source_event_refs: list[str]
    causal_parent_refs: list[str]
    eligible_at: str
    recorded_at: str
    policy_version: str
    status: str
    idempotency_key: str
    agenda_ref: Optional[str] = None
    expires_at: Optional[str] = None
    consumed_at: Optional[str] = None
    consumed_by_transition_ref: Optional[str] = None
    consumed_decision_ref: Optional[str] = None
    consumed_adoption_ref: Optional[str] = None
    resolution_reason_code: Optional[str] = None

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_RESUME_ELIGIBILITY:
            raise ContractViolationError(
                f"invalid ResumeEligibility schema_version={self.schema_version!r}"
            )
        self.subject_id = validate_subject_id(self.subject_id)
        self.eligibility_id = _validate_structured_ref(
            self.eligibility_id,
            "eligibility_id",
            allowed_prefixes=frozenset({"relig", "elig"}),
        )
        if not isinstance(self.activity_id, str) or not self.activity_id.strip():
            raise ContractViolationError("activity_id must be non-empty")
        if isinstance(self.activity_revision, bool) or not isinstance(self.activity_revision, int) or self.activity_revision < 1:
            raise ContractViolationError("activity_revision must be a positive integer")
        self.waiting_episode_id = _validate_structured_ref(
            self.waiting_episode_id,
            "waiting_episode_id",
            allowed_prefixes=frozenset({"wep", "wait_ep"}),
        )
        self.resume_condition_ref = _validate_structured_ref(
            self.resume_condition_ref,
            "resume_condition_ref",
            allowed_prefixes=frozenset({"rcond", "resume_cond"}),
        )
        if self.agenda_ref is not None:
            self.agenda_ref = _validate_structured_ref(
                self.agenda_ref,
                "agenda_ref",
                allowed_prefixes=frozenset({"agnd", "agenda"}),
            )
        self.source_event_refs = normalize_ref_list(
            self.source_event_refs, "source_event_refs", allow_empty=False
        )
        self.causal_parent_refs = normalize_ref_list(
            self.causal_parent_refs, "causal_parent_refs", allow_empty=False
        )
        self.eligible_at = _require_iso(self.eligible_at, "eligible_at")
        self.recorded_at = _require_iso(self.recorded_at, "recorded_at")
        self.expires_at = _optional_iso(self.expires_at, "expires_at")
        self.consumed_at = _optional_iso(self.consumed_at, "consumed_at")
        if self.status not in ALL_ELIGIBILITY_STATUSES:
            raise ContractViolationError(f"invalid ResumeEligibility status={self.status!r}")
        if not isinstance(self.idempotency_key, str) or not self.idempotency_key.strip():
            raise ContractViolationError("ResumeEligibility requires a non-empty idempotency_key")

    def to_dict(self) -> dict[str, Any]:
        return {
            "eligibility_id": self.eligibility_id,
            "schema_version": self.schema_version,
            "revision": self.revision,
            "subject_id": self.subject_id,
            "activity_id": self.activity_id,
            "activity_revision": self.activity_revision,
            "waiting_episode_id": self.waiting_episode_id,
            "resume_condition_ref": self.resume_condition_ref,
            "agenda_ref": self.agenda_ref,
            "source_event_refs": list(self.source_event_refs),
            "causal_parent_refs": list(self.causal_parent_refs),
            "eligible_at": self.eligible_at,
            "recorded_at": self.recorded_at,
            "expires_at": self.expires_at,
            "policy_version": self.policy_version,
            "status": self.status,
            "consumed_at": self.consumed_at,
            "consumed_by_transition_ref": self.consumed_by_transition_ref,
            "consumed_decision_ref": self.consumed_decision_ref,
            "consumed_adoption_ref": self.consumed_adoption_ref,
            "resolution_reason_code": self.resolution_reason_code,
            "idempotency_key": self.idempotency_key,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ResumeEligibility":
        return cls(
            eligibility_id=str(data["eligibility_id"]),
            schema_version=str(data.get("schema_version", SCHEMA_RESUME_ELIGIBILITY)),
            revision=int(data["revision"]),
            subject_id=str(data.get("subject_id", GLOBAL_SUBJECT_ID)),
            activity_id=str(data["activity_id"]),
            activity_revision=int(data["activity_revision"]),
            waiting_episode_id=str(data["waiting_episode_id"]),
            resume_condition_ref=str(data["resume_condition_ref"]),
            agenda_ref=data.get("agenda_ref"),
            source_event_refs=list(data.get("source_event_refs") or []),
            causal_parent_refs=list(data.get("causal_parent_refs") or []),
            eligible_at=str(data["eligible_at"]),
            recorded_at=str(data["recorded_at"]),
            expires_at=data.get("expires_at"),
            policy_version=str(data.get("policy_version", POLICY_VERSION_LR4)),
            status=str(data["status"]),
            consumed_at=data.get("consumed_at"),
            consumed_by_transition_ref=data.get("consumed_by_transition_ref"),
            consumed_decision_ref=data.get("consumed_decision_ref"),
            consumed_adoption_ref=data.get("consumed_adoption_ref"),
            resolution_reason_code=data.get("resolution_reason_code"),
            idempotency_key=str(data["idempotency_key"]),
        )


# ---------------------------------------------------------------------------
# Section 8, 9, 21, 39, 40: Event Contract & Observation Boundary Adapters
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class NormalizedWaitingEvent:
    """Frozen LR-4 event envelope preserving occurred_at, observed_at, recorded_at, enqueued_at."""

    event_id: str
    event_category: str
    event_kind: str
    occurred_at: str
    observed_at: str
    recorded_at: str
    enqueued_at: str
    source_refs: tuple[str, ...]
    causal_parent_refs: tuple[str, ...] = ()
    waiting_on_ref: Optional[str] = None
    is_observed_by_chiyo: bool = False
    observation_ref: Optional[str] = None
    fact_key: Optional[str] = None
    fact_value: Any = None
    subject_ref: Optional[str] = None
    attributes: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        norm_id = _validate_structured_ref(self.event_id, "event_id")
        object.__setattr__(self, "event_id", norm_id)
        object.__setattr__(self, "occurred_at", _require_iso(self.occurred_at, "occurred_at"))
        object.__setattr__(self, "observed_at", _require_iso(self.observed_at, "observed_at"))
        object.__setattr__(self, "recorded_at", _require_iso(self.recorded_at, "recorded_at"))
        object.__setattr__(self, "enqueued_at", _require_iso(self.enqueued_at, "enqueued_at"))
        norm_sources = tuple(normalize_ref_list(self.source_refs, "source_refs", allow_empty=False))
        norm_parents = tuple(
            normalize_ref_list(self.causal_parent_refs, "causal_parent_refs", allow_empty=True)
        )
        object.__setattr__(self, "source_refs", norm_sources)
        object.__setattr__(self, "causal_parent_refs", norm_parents)
        if self.waiting_on_ref is not None:
            object.__setattr__(
                self,
                "waiting_on_ref",
                _validate_structured_ref(self.waiting_on_ref, "waiting_on_ref"),
            )
        if self.observation_ref is not None:
            object.__setattr__(
                self,
                "observation_ref",
                _validate_structured_ref(
                    self.observation_ref,
                    "observation_ref",
                    allowed_prefixes=VALID_OBSERVATION_PREFIXES,
                ),
            )
        if self.subject_ref is not None:
            object.__setattr__(
                self,
                "subject_ref",
                _validate_structured_ref(self.subject_ref, "subject_ref"),
            )


class WaitingEventAdapter:
    """Normalizes external tool results, world observations, user hints, and memory recalls."""

    @staticmethod
    def adapt_tool_result(payload: Mapping[str, Any]) -> NormalizedWaitingEvent:
        """Section 8: Tool Result is normalized as an Event (without claiming Action Reality truth)."""
        if not isinstance(payload, Mapping):
            raise ContractViolationError("tool result payload must be a mapping")
        rec_ts = _iso(now())
        occ_ts = _require_iso(payload.get("occurred_at") or rec_ts, "occurred_at")
        obs_ts = _require_iso(payload.get("observed_at") or occ_ts, "observed_at")
        enq_ts = _require_iso(payload.get("enqueued_at") or obs_ts, "enqueued_at")
        rec_final = _require_iso(payload.get("recorded_at") or rec_ts, "recorded_at")
        job_ref = payload.get("waiting_on_ref") or payload.get("job_ref")
        if not job_ref:
            raise ContractViolationError("ToolResultEvent requires 'waiting_on_ref' or 'job_ref'")
        ev_id = str(payload.get("event_id") or f"ev_tool:{sha256_hex(str(job_ref))[:12]}")
        sources = list(payload.get("source_refs") or [job_ref])
        return NormalizedWaitingEvent(
            event_id=ev_id,
            event_category=EVENT_CATEGORY_EXTERNAL_RESULT,
            event_kind=str(payload.get("event_kind") or "TOOL_RESULT"),
            occurred_at=occ_ts,
            observed_at=obs_ts,
            recorded_at=rec_final,
            enqueued_at=enq_ts,
            source_refs=tuple(sources),
            causal_parent_refs=tuple(payload.get("causal_parent_refs") or ()),
            waiting_on_ref=str(job_ref),
            is_observed_by_chiyo=True,
            attributes=dict(payload.get("attributes") or {}),
        )

    @staticmethod
    def adapt_world_event(payload: Mapping[str, Any]) -> NormalizedWaitingEvent:
        """Section 9: Enforces 'World knows X != Chiyo knows X' observation boundary."""
        if not isinstance(payload, Mapping):
            raise ContractViolationError("world event payload must be a mapping")
        rec_ts = _iso(now())
        occ_ts = _require_iso(payload.get("occurred_at") or rec_ts, "occurred_at")
        obs_ts = _require_iso(payload.get("observed_at") or occ_ts, "observed_at")
        enq_ts = _require_iso(payload.get("enqueued_at") or obs_ts, "enqueued_at")
        rec_final = _require_iso(payload.get("recorded_at") or rec_ts, "recorded_at")

        is_obs = bool(payload.get("is_observed_by_chiyo", False))
        obs_ref = payload.get("observation_ref")
        if is_obs:
            if not obs_ref:
                is_obs = False
            else:
                obs_ref = _validate_structured_ref(
                    obs_ref,
                    "observation_ref",
                    allowed_prefixes=VALID_OBSERVATION_PREFIXES,
                )

        ev_id = str(payload.get("event_id") or f"ev_world:{uuid.uuid4().hex[:12]}")
        sources = list(payload.get("source_refs") or ([obs_ref] if obs_ref else [ev_id]))
        category = (
            EVENT_CATEGORY_WORLD_OBSERVATION
            if is_obs
            else EVENT_CATEGORY_HIDDEN_WORLD_FACT
        )
        return NormalizedWaitingEvent(
            event_id=ev_id,
            event_category=category,
            event_kind=str(payload.get("event_kind") or "WORLD_STATE_CHANGE"),
            occurred_at=occ_ts,
            observed_at=obs_ts,
            recorded_at=rec_final,
            enqueued_at=enq_ts,
            source_refs=tuple(sources),
            causal_parent_refs=tuple(payload.get("causal_parent_refs") or ()),
            waiting_on_ref=payload.get("waiting_on_ref"),
            is_observed_by_chiyo=is_obs,
            observation_ref=obs_ref if is_obs else None,
            fact_key=payload.get("fact_key"),
            fact_value=payload.get("fact_value"),
            subject_ref=payload.get("subject_ref"),
            attributes=dict(payload.get("attributes") or {}),
        )

    @staticmethod
    def adapt_generic_event(payload: Mapping[str, Any]) -> NormalizedWaitingEvent:
        if not isinstance(payload, Mapping):
            raise ContractViolationError("event payload must be a mapping")
        rec_ts = _iso(now())
        occ_ts = _require_iso(payload.get("occurred_at") or rec_ts, "occurred_at")
        obs_ts = _require_iso(payload.get("observed_at") or occ_ts, "observed_at")
        enq_ts = _require_iso(payload.get("enqueued_at") or obs_ts, "enqueued_at")
        rec_final = _require_iso(payload.get("recorded_at") or rec_ts, "recorded_at")
        ev_id = str(payload["event_id"])
        sources = list(payload.get("source_refs") or [ev_id])
        return NormalizedWaitingEvent(
            event_id=ev_id,
            event_category=str(payload.get("event_category") or EVENT_CATEGORY_GENERIC_EVENT),
            event_kind=str(payload.get("event_kind") or "GENERIC_EVENT"),
            occurred_at=occ_ts,
            observed_at=obs_ts,
            recorded_at=rec_final,
            enqueued_at=enq_ts,
            source_refs=tuple(sources),
            causal_parent_refs=tuple(payload.get("causal_parent_refs") or ()),
            waiting_on_ref=payload.get("waiting_on_ref"),
            is_observed_by_chiyo=bool(payload.get("is_observed_by_chiyo", True)),
            observation_ref=payload.get("observation_ref"),
            fact_key=payload.get("fact_key"),
            fact_value=payload.get("fact_value"),
            subject_ref=payload.get("subject_ref"),
            attributes=dict(payload.get("attributes") or {}),
        )

    @staticmethod
    def adapt_user_resume_hint(payload: Mapping[str, Any]) -> NormalizedWaitingEvent:
        """Section 39: User saying 'continue what you were doing' is a hint candidate, NOT a resume command."""
        rec_ts = _iso(now())
        occ_ts = _require_iso(payload.get("occurred_at") or rec_ts, "occurred_at")
        obs_ts = _require_iso(payload.get("observed_at") or occ_ts, "observed_at")
        enq_ts = _require_iso(payload.get("enqueued_at") or obs_ts, "enqueued_at")
        rec_final = _require_iso(payload.get("recorded_at") or rec_ts, "recorded_at")
        ev_id = str(payload.get("event_id") or f"user_comm:{uuid.uuid4().hex[:12]}")
        sources = list(payload.get("source_refs") or [ev_id])
        return NormalizedWaitingEvent(
            event_id=ev_id,
            event_category=EVENT_CATEGORY_USER_RESUME_HINT,
            event_kind="USER_RESUME_HINT",
            occurred_at=occ_ts,
            observed_at=obs_ts,
            recorded_at=rec_final,
            enqueued_at=enq_ts,
            source_refs=tuple(sources),
            waiting_on_ref=payload.get("waiting_on_ref"),
            is_observed_by_chiyo=True,
            attributes={"text": str(payload.get("text") or ""), "target_activity_id": payload.get("activity_id")},
        )

    @staticmethod
    def adapt_memory_recall(payload: Mapping[str, Any]) -> NormalizedWaitingEvent:
        """Section 40: Memory Recall != ResumeCondition satisfied != RESUME."""
        rec_ts = _iso(now())
        occ_ts = _require_iso(payload.get("occurred_at") or rec_ts, "occurred_at")
        obs_ts = _require_iso(payload.get("observed_at") or occ_ts, "observed_at")
        enq_ts = _require_iso(payload.get("enqueued_at") or obs_ts, "enqueued_at")
        rec_final = _require_iso(payload.get("recorded_at") or rec_ts, "recorded_at")
        ev_id = str(payload.get("event_id") or f"memory_recall:{uuid.uuid4().hex[:12]}")
        sources = list(payload.get("source_refs") or [ev_id])
        return NormalizedWaitingEvent(
            event_id=ev_id,
            event_category=EVENT_CATEGORY_MEMORY_RECALL,
            event_kind="MEMORY_RECALL",
            occurred_at=occ_ts,
            observed_at=obs_ts,
            recorded_at=rec_final,
            enqueued_at=enq_ts,
            source_refs=tuple(sources),
            is_observed_by_chiyo=False,
            attributes={"memory_ref": payload.get("memory_ref")},
        )


# ---------------------------------------------------------------------------
# Section 65 & 66: WAL-Protected Versioned Aggregate Store Base
# ---------------------------------------------------------------------------


class WALAggregateStore:
    """Crash-safe versioned store using the LR-2/LR-3 WAL commit_intent.json protocol.

    Supports fault injection at:
    - 'prepare' / 'before_intent_replace'
    - 'persist' / 'after_intent_replace_before_journal_replace'
    - 'commit' / 'after_journal_replace_before_state_replace'
    - 'recovery' / 'after_state_replace_before_intent_unlink'
    """

    def __init__(
        self,
        store_dir: Path | str,
        *,
        expected_writer_domain: str,
        state_schema_version: str,
        transition_schema_version: str,
        collection_key: str,
        id_field: str,
        state_filename: str,
        journal_filename: str,
    ) -> None:
        self.store_dir = Path(store_dir).expanduser().resolve()
        self.expected_writer_domain = expected_writer_domain
        self.state_schema_version = state_schema_version
        self.transition_schema_version = transition_schema_version
        self.collection_key = collection_key
        self.id_field = id_field
        self.state_filename = state_filename
        self.journal_filename = journal_filename
        self.state_path = self.store_dir / state_filename
        self.journal_path = self.store_dir / journal_filename
        self.intent_path = self.store_dir / COMMIT_INTENT_FILENAME
        self.run_dir = self.store_dir / "run"

    @contextmanager
    def _io_lock(self, *, shared: bool = False, timeout_seconds: float = 5.0) -> Iterator[None]:
        self.run_dir.mkdir(parents=True, exist_ok=True)
        lock_file = self.run_dir / ".aggregate_io.lock"
        mode = fcntl.LOCK_SH if shared else fcntl.LOCK_EX
        deadline = time.monotonic() + max(0.0, timeout_seconds)
        with lock_file.open("a+", encoding="utf-8") as handle:
            while True:
                try:
                    fcntl.flock(handle.fileno(), mode | fcntl.LOCK_NB)
                    break
                except OSError as exc:
                    if exc.errno not in (errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK):
                        raise ActivityLockError(f"LR-4 store IO lock failed: {exc}") from exc
                    if time.monotonic() >= deadline:
                        raise ActivityLockError("LR-4 store IO lock timeout") from exc
                    time.sleep(0.005)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _empty_state(self, at_iso: Optional[str] = None) -> dict[str, Any]:
        stamp = at_iso or _iso(now())
        return {
            "schema_version": self.state_schema_version,
            "subject_id": GLOBAL_SUBJECT_ID,
            "writer_domain": self.expected_writer_domain,
            "revision": 0,
            "last_transition_ref": None,
            "last_transition_hash": GENESIS_HASH,
            "updated_at": stamp,
            self.collection_key: {},
            "processed_idempotency_keys": {},
        }

    def _cleanup_stray_temps_unlocked(self) -> None:
        if not self.store_dir.exists():
            return
        for item in self.store_dir.iterdir():
            if item.is_file() and (
                item.name.startswith(f".{self.journal_filename}.tmp.")
                or item.name.startswith(f".{self.state_filename}.tmp.")
                or item.name.startswith(f".{COMMIT_INTENT_FILENAME}.tmp.")
            ):
                try:
                    item.unlink(missing_ok=True)
                except OSError:
                    pass

    def project_from_journal(self, entries: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        state = self._empty_state()
        expected_rev = 1
        prev_hash = GENESIS_HASH
        for entry in entries:
            if entry.get("expected_revision") != expected_rev - 1 or entry.get("result_revision") != expected_rev:
                raise RecoveryRequiredError(
                    "LR4_BAD_REVISION_CHAIN",
                    f"invalid revision sequence in {self.journal_filename}",
                )
            if entry.get("prev_hash") != prev_hash:
                raise RecoveryRequiredError(
                    "LR4_BAD_HASH_CHAIN",
                    f"broken prev_hash chain in {self.journal_filename}",
                )
            records = entry.get("records_snapshot")
            if isinstance(records, list):
                for rec in records:
                    rec_copy = copy.deepcopy(dict(rec))
                    state[self.collection_key][rec_copy[self.id_field]] = rec_copy
            else:
                rec_copy = copy.deepcopy(dict(entry["record_snapshot"]))
                state[self.collection_key][rec_copy[self.id_field]] = rec_copy

            idem_key = entry.get("idempotency_key")
            if idem_key:
                state["processed_idempotency_keys"][idem_key] = entry["transition_id"]
            for sub_idem in entry.get("batch_idempotency_keys") or []:
                state["processed_idempotency_keys"][sub_idem] = entry["transition_id"]

            state["revision"] = expected_rev
            state["last_transition_ref"] = entry["transition_id"]
            state["last_transition_hash"] = entry["entry_hash"]
            state["updated_at"] = entry["recorded_at"]
            prev_hash = entry["entry_hash"]
            expected_rev += 1
        return state

    def _read_journal_unlocked(self) -> list[dict[str, Any]]:
        if not self.journal_path.exists():
            return []
        raw_bytes = self.journal_path.read_bytes()
        if not raw_bytes:
            return []
        text = raw_bytes.decode("utf-8")
        if not text.endswith("\n"):
            raise RecoveryRequiredError(
                "LR4_INCOMPLETE_JOURNAL_TAIL",
                f"{self.journal_filename} does not end with newline",
            )
        entries: list[dict[str, Any]] = []
        prev_hash = GENESIS_HASH
        expected_rev = 1
        for line_no, line in enumerate(text.splitlines(), start=1):
            if not line.strip():
                raise RecoveryRequiredError("LR4_BLANK_JOURNAL_LINE", f"blank line {line_no}")
            entry = json.loads(line)
            if entry.get("schema_version") != self.transition_schema_version:
                raise RecoveryRequiredError(
                    "LR4_UNKNOWN_TRANSITION_SCHEMA",
                    f"unexpected schema_version at line {line_no}",
                )
            if entry.get("expected_revision") != expected_rev - 1 or entry.get("result_revision") != expected_rev:
                raise RecoveryRequiredError("LR4_BAD_REVISION_CHAIN", f"revision mismatch at line {line_no}")
            if entry.get("prev_hash") != prev_hash:
                raise RecoveryRequiredError("LR4_BAD_HASH_CHAIN", f"hash chain broken at line {line_no}")
            recorded_hash = entry.get("entry_hash")
            unsigned = dict(entry)
            unsigned.pop("entry_hash", None)
            if sha256_hex(canonical_json_line(unsigned)) != recorded_hash:
                raise RecoveryRequiredError("LR4_JOURNAL_TAMPERED", f"entry_hash mismatch at line {line_no}")
            prev_hash = recorded_hash
            expected_rev += 1
            entries.append(entry)
        return entries

    def _reconcile_intent_unlocked(self, journal_entries: list[dict[str, Any]]) -> None:
        self._cleanup_stray_temps_unlocked()
        if not self.intent_path.exists():
            return
        try:
            intent = json.loads(self.intent_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise RecoveryRequiredError("LR4_CORRUPT_COMMIT_INTENT", str(exc)) from exc

        if not isinstance(intent, dict) or intent.get("schema_version") != SCHEMA_LR4_COMMIT_INTENT:
            raise RecoveryRequiredError("LR4_INVALID_COMMIT_INTENT", "invalid commit_intent schema")

        target_rev = intent.get("target_revision")
        expected_prev_rev = intent.get("expected_revision")
        cur_rev = len(journal_entries)

        if cur_rev == expected_prev_rev:
            self.intent_path.unlink(missing_ok=True)
            _fsync_dir(self.store_dir)
            return

        if cur_rev == target_rev and cur_rev > 0:
            last_entry = journal_entries[-1]
            if (
                last_entry.get("transition_id") == intent.get("transition_id")
                and last_entry.get("entry_hash") == intent.get("entry_hash")
            ):
                rebuilt = self.project_from_journal(journal_entries)
                payload = json.dumps(rebuilt, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
                if intent.get("state_sha256") and sha256_hex(payload) != intent.get("state_sha256"):
                    raise RecoveryRequiredError(
                        "LR4_INTENT_STATE_DIGEST_MISMATCH",
                        "rebuilt state digest mismatch against commit_intent",
                    )
                state_tmp = self.store_dir / f".{self.state_filename}.tmp.{os.getpid()}.{uuid.uuid4().hex}"
                try:
                    with state_tmp.open("w", encoding="utf-8") as sh:
                        sh.write(payload)
                        sh.flush()
                        os.fsync(sh.fileno())
                    os.replace(state_tmp, self.state_path)
                    self.intent_path.unlink(missing_ok=True)
                    _fsync_dir(self.store_dir)
                finally:
                    if state_tmp.exists():
                        state_tmp.unlink(missing_ok=True)
                return

        raise RecoveryRequiredError(
            "LR4_UNRECONCILABLE_COMMIT_INTENT",
            f"cannot reconcile target_revision={target_rev} with journal length={cur_rev}",
        )

    def _read_state_unlocked(self, journal_entries: list[dict[str, Any]]) -> dict[str, Any]:
        self._reconcile_intent_unlocked(journal_entries)
        if not self.state_path.exists():
            if journal_entries:
                raise RecoveryRequiredError(
                    "LR4_MISSING_SNAPSHOT",
                    f"{self.state_filename} missing while journal has {len(journal_entries)} entries",
                )
            return self._empty_state()

        state = json.loads(self.state_path.read_text(encoding="utf-8"))
        if state.get("schema_version") != self.state_schema_version:
            raise RecoveryRequiredError("LR4_UNKNOWN_STATE_SCHEMA", f"invalid schema in {self.state_filename}")
        validate_subject_id(state.get("subject_id"))
        if state.get("revision") != len(journal_entries):
            raise RecoveryRequiredError(
                "LR4_REVISION_MISMATCH",
                f"state revision={state.get('revision')} != journal len={len(journal_entries)}",
            )
        return state

    def load_state_and_journal(self) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        with self._io_lock(shared=False):
            journal = self._read_journal_unlocked()
            state = self._read_state_unlocked(journal)
            return copy.deepcopy(state), copy.deepcopy(journal)

    def load_state(self) -> dict[str, Any]:
        state, _ = self.load_state_and_journal()
        return state

    def _verify_writer(
        self,
        lease: ActivityAuthorityLease,
        capability: LR4WriterCapability,
    ) -> None:
        if not isinstance(lease, ActivityAuthorityLease) or not lease.held:
            raise WriterCapabilityError(f"{self.expected_writer_domain} commit requires a held ActivityAuthorityLease")
        if not isinstance(capability, LR4WriterCapability):
            raise WriterCapabilityError(
                f"expected LR4WriterCapability for {self.expected_writer_domain}, got {type(capability).__name__}"
            )
        if capability.writer_domain != self.expected_writer_domain:
            raise WriterCapabilityError(
                f"cross-authority violation: capability domain {capability.writer_domain!r} "
                f"cannot write to {self.expected_writer_domain!r}"
            )
        if not lease.verify_capability(capability):
            raise WriterCapabilityError("LR4WriterCapability signature verification failed")

    def commit_records(
        self,
        *,
        lease: ActivityAuthorityLease,
        capability: LR4WriterCapability,
        records: Sequence[Mapping[str, Any]],
        action: str,
        idempotency_key: str,
        batch_idempotency_keys: Optional[Sequence[str]] = None,
        fault_hook: Optional[Callable[[str], None]] = None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Atomically commit one or more aggregate records with WAL commit_intent.json."""
        self._verify_writer(lease, capability)
        if not records:
            raise ContractViolationError("commit_records requires at least one record")
        if not isinstance(idempotency_key, str) or not idempotency_key.strip():
            raise ContractViolationError("idempotency_key must be non-empty")

        with self._io_lock(shared=False):
            journal = self._read_journal_unlocked()
            state = self._read_state_unlocked(journal)

            idem = idempotency_key.strip()
            existing_trn_id = state.get("processed_idempotency_keys", {}).get(idem)
            if existing_trn_id is not None:
                for entry in reversed(journal):
                    if entry["transition_id"] == existing_trn_id:
                        return copy.deepcopy(state), copy.deepcopy(entry)

            expected_rev = state["revision"]
            result_rev = expected_rev + 1
            rec_stamp = _iso(now())
            trn_id = f"lr4trn:{uuid.uuid4().hex[:16]}"
            prev_hash = journal[-1]["entry_hash"] if journal else GENESIS_HASH

            new_state = copy.deepcopy(state)
            snapshots: list[dict[str, Any]] = []
            for rec in records:
                rec_dict = copy.deepcopy(dict(rec))
                rec_dict["revision"] = result_rev
                new_state[self.collection_key][rec_dict[self.id_field]] = rec_dict
                snapshots.append(rec_dict)

            new_state["revision"] = result_rev
            new_state["updated_at"] = rec_stamp
            new_state["processed_idempotency_keys"][idem] = trn_id
            for b_key in batch_idempotency_keys or []:
                new_state["processed_idempotency_keys"][b_key] = trn_id

            unsigned_entry: dict[str, Any] = {
                "schema_version": self.transition_schema_version,
                "transition_id": trn_id,
                "subject_id": GLOBAL_SUBJECT_ID,
                "writer_domain": self.expected_writer_domain,
                "action": action,
                "expected_revision": expected_rev,
                "result_revision": result_rev,
                "recorded_at": rec_stamp,
                "idempotency_key": idem,
                "batch_idempotency_keys": list(batch_idempotency_keys or []),
                "record_snapshot": snapshots[0],
                "records_snapshot": snapshots,
                "prev_hash": prev_hash,
            }
            entry_hash = sha256_hex(canonical_json_line(unsigned_entry))
            signed_entry = dict(unsigned_entry)
            signed_entry["entry_hash"] = entry_hash

            new_state["last_transition_ref"] = trn_id
            new_state["last_transition_hash"] = entry_hash

            self.store_dir.mkdir(parents=True, exist_ok=True)
            journal_tmp = self.store_dir / f".{self.journal_filename}.tmp.{os.getpid()}.{uuid.uuid4().hex}"
            state_tmp = self.store_dir / f".{self.state_filename}.tmp.{os.getpid()}.{uuid.uuid4().hex}"
            intent_tmp = self.store_dir / f".{COMMIT_INTENT_FILENAME}.tmp.{os.getpid()}.{uuid.uuid4().hex}"

            try:
                state_payload = json.dumps(new_state, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
                with state_tmp.open("w", encoding="utf-8") as sh:
                    sh.write(state_payload)
                    sh.flush()
                    os.fsync(sh.fileno())

                intent_payload = (
                    json.dumps(
                        {
                            "schema_version": SCHEMA_LR4_COMMIT_INTENT,
                            "writer_domain": self.expected_writer_domain,
                            "expected_revision": expected_rev,
                            "target_revision": result_rev,
                            "transition_id": trn_id,
                            "entry_hash": entry_hash,
                            "state_sha256": sha256_hex(state_payload),
                            "created_at": rec_stamp,
                        },
                        ensure_ascii=False,
                        indent=2,
                        sort_keys=True,
                    )
                    + "\n"
                )
                with intent_tmp.open("w", encoding="utf-8") as ih:
                    ih.write(intent_payload)
                    ih.flush()
                    os.fsync(ih.fileno())

                if fault_hook is not None:
                    fault_hook("prepare")
                    fault_hook("before_intent_replace")

                os.replace(intent_tmp, self.intent_path)
                _fsync_dir(self.store_dir)

                if fault_hook is not None:
                    fault_hook("persist")
                    fault_hook("after_intent_replace_before_journal_replace")

                all_lines = [canonical_json_line(e) for e in journal] + [canonical_json_line(signed_entry)]
                journal_payload = "\n".join(all_lines) + "\n"
                with journal_tmp.open("w", encoding="utf-8") as jh:
                    jh.write(journal_payload)
                    jh.flush()
                    os.fsync(jh.fileno())

                os.replace(journal_tmp, self.journal_path)
                _fsync_dir(self.store_dir)

                if fault_hook is not None:
                    fault_hook("commit")
                    fault_hook("after_journal_replace_before_state_replace")

                os.replace(state_tmp, self.state_path)
                _fsync_dir(self.store_dir)

                if fault_hook is not None:
                    fault_hook("recovery")
                    fault_hook("after_state_replace_before_intent_unlink")

                self.intent_path.unlink(missing_ok=True)
                _fsync_dir(self.store_dir)
            finally:
                for tf in (journal_tmp, state_tmp, intent_tmp):
                    if tf.exists():
                        tf.unlink(missing_ok=True)

            return copy.deepcopy(new_state), copy.deepcopy(signed_entry)


class ResumeConditionStore(WALAggregateStore):
    """Canonical owner for ResumeCondition aggregate."""

    def __init__(self, store_root: Path | str) -> None:
        base = Path(store_root).expanduser().resolve() / "waiting_agenda" / "resume_conditions"
        super().__init__(
            base,
            expected_writer_domain=WRITER_DOMAIN_RESUME_CONDITION,
            state_schema_version=SCHEMA_RESUME_CONDITION_STATE,
            transition_schema_version=SCHEMA_RESUME_CONDITION_TRANSITION,
            collection_key="conditions",
            id_field="condition_id",
            state_filename="resume_conditions_state.json",
            journal_filename="resume_condition_transitions.jsonl",
        )


class AgendaStore(WALAggregateStore):
    """Canonical owner for AgendaItem aggregate."""

    def __init__(self, store_root: Path | str) -> None:
        base = Path(store_root).expanduser().resolve() / "waiting_agenda" / "agenda"
        super().__init__(
            base,
            expected_writer_domain=WRITER_DOMAIN_AGENDA,
            state_schema_version=SCHEMA_AGENDA_STATE,
            transition_schema_version=SCHEMA_AGENDA_TRANSITION,
            collection_key="agenda_items",
            id_field="agenda_id",
            state_filename="agenda_state.json",
            journal_filename="agenda_transitions.jsonl",
        )


class ResumeEligibilityStore(WALAggregateStore):
    """Canonical owner for ResumeEligibility aggregate."""

    def __init__(self, store_root: Path | str) -> None:
        base = Path(store_root).expanduser().resolve() / "waiting_agenda" / "eligibility"
        super().__init__(
            base,
            expected_writer_domain=WRITER_DOMAIN_ELIGIBILITY,
            state_schema_version=SCHEMA_RESUME_ELIGIBILITY_STATE,
            transition_schema_version=SCHEMA_RESUME_ELIGIBILITY_TRANSITION,
            collection_key="eligibilities",
            id_field="eligibility_id",
            state_filename="resume_eligibility_state.json",
            journal_filename="resume_eligibility_transitions.jsonl",
        )


# ---------------------------------------------------------------------------
# Section 7, 8, 9, 10, 20, 23, 33, 34, 35: Deterministic Condition Evaluator
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ConditionEvaluationResult:
    """Pure deterministic outcome of evaluating a ResumeCondition (0 LLM, 0 Activity writes)."""

    condition_id: str
    previous_status: str
    new_status: str
    mutated: bool
    satisfied: bool
    expired: bool
    satisfied_subcondition_ids: tuple[str, ...]
    matched_event_ref: Optional[str]
    evaluated_at: str
    reason_code: str


class ResumeConditionEvaluator:
    """Pure deterministic evaluator for ResumeCondition (0 LLM, 0 World DB polling)."""

    @classmethod
    def _matches_leaf(
        cls,
        *,
        condition_kind: str,
        waiting_on_ref: Optional[str],
        parameters: Mapping[str, Any],
        event: Optional[NormalizedWaitingEvent],
        eval_time_iso: str,
    ) -> tuple[bool, bool, str]:
        """Return (satisfied, expired, reason_code) for a single leaf condition."""
        eval_epoch = _ts_epoch(eval_time_iso)

        if condition_kind == COND_KIND_TIME_AT_OR_AFTER:
            target_epoch = _ts_epoch(parameters["target_time"])
            if eval_epoch >= target_epoch:
                return True, False, "TIME_AT_OR_AFTER_REACHED"
            return False, False, "TIME_NOT_YET_REACHED"

        if condition_kind == COND_KIND_TIME_WINDOW_OPEN:
            ws_epoch = _ts_epoch(parameters["window_start"])
            we_epoch = _ts_epoch(parameters["window_end"])
            if eval_epoch > we_epoch:
                return False, True, "TIME_WINDOW_EXPIRED"
            if ws_epoch <= eval_epoch <= we_epoch:
                return True, False, "TIME_WINDOW_OPEN_MATCHED"
            return False, False, "TIME_WINDOW_NOT_OPEN_YET"

        # Event-based leaf conditions require a concrete event
        if event is None:
            return False, False, "NO_EVENT_SUPPLIED"

        # Section 39 & 40: User resume hints and Memory recalls NEVER satisfy blocking conditions
        if event.event_category in (EVENT_CATEGORY_USER_RESUME_HINT, EVENT_CATEGORY_MEMORY_RECALL):
            return False, False, "NON_CONDITION_INPUT_IGNORED"

        if condition_kind == COND_KIND_EXTERNAL_RESULT_EVENT:
            if event.event_category != EVENT_CATEGORY_EXTERNAL_RESULT:
                return False, False, "EVENT_CATEGORY_MISMATCH"
            expected_ref = parameters.get("waiting_on_ref") or waiting_on_ref
            # Section 23: Exact waiting_on_ref matching (no fuzzy matching)
            if not expected_ref or event.waiting_on_ref != expected_ref:
                return False, False, "WAITING_ON_REF_MISMATCH"
            return True, False, "EXTERNAL_RESULT_MATCHED"

        if condition_kind == COND_KIND_OBSERVED_CONDITION:
            # Section 9 & Test Group H: Hidden World fact cannot satisfy OBSERVED_CONDITION
            if (
                event.event_category != EVENT_CATEGORY_WORLD_OBSERVATION
                or not event.is_observed_by_chiyo
                or not event.observation_ref
            ):
                return False, False, "HIDDEN_OR_UNOBSERVED_WORLD_FACT_REJECTED"
            expected_fact_key = parameters.get("fact_key")
            if event.fact_key != expected_fact_key:
                return False, False, "OBSERVED_FACT_KEY_MISMATCH"
            if event.fact_value != parameters.get("expected_value"):
                return False, False, "OBSERVED_FACT_VALUE_MISMATCH"
            expected_subj = parameters.get("subject_ref")
            if expected_subj is not None and event.subject_ref != expected_subj:
                return False, False, "OBSERVED_SUBJECT_REF_MISMATCH"
            expected_wait = parameters.get("waiting_on_ref") or waiting_on_ref
            if expected_wait is not None and event.waiting_on_ref not in (None, expected_wait):
                return False, False, "OBSERVED_WAITING_ON_REF_MISMATCH"
            return True, False, "OBSERVED_CONDITION_MATCHED"

        if condition_kind == COND_KIND_EVENT_MATCH:
            if event.event_category == EVENT_CATEGORY_HIDDEN_WORLD_FACT:
                return False, False, "HIDDEN_WORLD_FACT_REJECTED"
            expected_ref = parameters.get("waiting_on_ref") or waiting_on_ref
            if expected_ref is not None and event.waiting_on_ref != expected_ref:
                return False, False, "EVENT_WAITING_ON_REF_MISMATCH"
            expected_kind = parameters.get("expected_event_kind")
            if expected_kind is not None and event.event_kind != expected_kind:
                return False, False, "EVENT_KIND_MISMATCH"
            expected_attrs = parameters.get("expected_attributes")
            if isinstance(expected_attrs, Mapping):
                for k, v in expected_attrs.items():
                    if event.attributes.get(k) != v:
                        return False, False, "EVENT_ATTRIBUTE_MISMATCH"
            return True, False, "EVENT_MATCH_SATISFIED"

        return False, False, "UNSUPPORTED_CONDITION_KIND"

    @classmethod
    def evaluate(
        cls,
        condition: ResumeCondition,
        *,
        event: Optional[NormalizedWaitingEvent] = None,
        current_time: Optional[str] = None,
    ) -> ConditionEvaluationResult:
        """Deterministically evaluate a ResumeCondition against an event or current_time."""
        # Section 21 & Test Group G: Use event.observed_at (or occurred_at) rather than recorded_at/enqueued_at
        eval_iso = (
            _require_iso(current_time, "current_time")
            if current_time is not None
            else (event.observed_at if event is not None else _iso(now()))
        )

        if condition.status != COND_STATUS_PENDING:
            return ConditionEvaluationResult(
                condition_id=condition.condition_id,
                previous_status=condition.status,
                new_status=condition.status,
                mutated=False,
                satisfied=(condition.status == COND_STATUS_SATISFIED),
                expired=(condition.status == COND_STATUS_EXPIRED),
                satisfied_subcondition_ids=tuple(condition.satisfied_subcondition_ids),
                matched_event_ref=condition.last_event_ref,
                evaluated_at=eval_iso,
                reason_code="ALREADY_NON_PENDING",
            )

        # Section 33 & B04: Condition Expiry Check
        # For event-driven evaluation, compare against event.observed_at / occurred_at (G01/G02)
        effective_check_iso = event.observed_at if event is not None else eval_iso
        if condition.expires_at is not None and _ts_epoch(effective_check_iso) > _ts_epoch(condition.expires_at):
            return ConditionEvaluationResult(
                condition_id=condition.condition_id,
                previous_status=COND_STATUS_PENDING,
                new_status=COND_STATUS_EXPIRED,
                mutated=True,
                satisfied=False,
                expired=True,
                satisfied_subcondition_ids=tuple(condition.satisfied_subcondition_ids),
                matched_event_ref=event.event_id if event else None,
                evaluated_at=effective_check_iso,
                reason_code="CONDITION_EXPIRED",
            )

        if condition.condition_kind in LEAF_CONDITION_KINDS:
            sat, exp, reason = cls._matches_leaf(
                condition_kind=condition.condition_kind,
                waiting_on_ref=condition.waiting_on_ref,
                parameters=condition.parameters,
                event=event,
                eval_time_iso=effective_check_iso,
            )
            if exp:
                return ConditionEvaluationResult(
                    condition_id=condition.condition_id,
                    previous_status=COND_STATUS_PENDING,
                    new_status=COND_STATUS_EXPIRED,
                    mutated=True,
                    satisfied=False,
                    expired=True,
                    satisfied_subcondition_ids=tuple(condition.satisfied_subcondition_ids),
                    matched_event_ref=event.event_id if event else None,
                    evaluated_at=effective_check_iso,
                    reason_code=reason,
                )
            if sat:
                return ConditionEvaluationResult(
                    condition_id=condition.condition_id,
                    previous_status=COND_STATUS_PENDING,
                    new_status=COND_STATUS_SATISFIED,
                    mutated=True,
                    satisfied=True,
                    expired=False,
                    satisfied_subcondition_ids=tuple(condition.satisfied_subcondition_ids),
                    matched_event_ref=event.event_id if event else None,
                    evaluated_at=effective_check_iso,
                    reason_code=reason,
                )
            return ConditionEvaluationResult(
                condition_id=condition.condition_id,
                previous_status=COND_STATUS_PENDING,
                new_status=COND_STATUS_PENDING,
                mutated=False,
                satisfied=False,
                expired=False,
                satisfied_subcondition_ids=tuple(condition.satisfied_subcondition_ids),
                matched_event_ref=condition.last_event_ref,
                evaluated_at=effective_check_iso,
                reason_code=reason,
            )

        # Section 34 & 35: Composite ALL / ANY evaluation (bounded, zero scoring)
        subs = condition.parameters.get("subconditions") or []
        sat_ids = list(condition.satisfied_subcondition_ids)
        newly_matched_sub = False
        any_expired = False

        for sub in subs:
            sub_id = sub["subcondition_id"]
            if sub_id in sat_ids:
                continue
            s_sat, s_exp, _ = cls._matches_leaf(
                condition_kind=sub["condition_kind"],
                waiting_on_ref=sub.get("waiting_on_ref"),
                parameters=sub.get("parameters") or {},
                event=event,
                eval_time_iso=effective_check_iso,
            )
            if s_exp:
                any_expired = True
            elif s_sat:
                sat_ids.append(sub_id)
                newly_matched_sub = True

        total_subs = len(subs)
        if condition.condition_kind == COND_KIND_ALL:
            if any_expired and len(sat_ids) < total_subs:
                return ConditionEvaluationResult(
                    condition_id=condition.condition_id,
                    previous_status=COND_STATUS_PENDING,
                    new_status=COND_STATUS_EXPIRED,
                    mutated=True,
                    satisfied=False,
                    expired=True,
                    satisfied_subcondition_ids=tuple(sat_ids),
                    matched_event_ref=event.event_id if event else condition.last_event_ref,
                    evaluated_at=effective_check_iso,
                    reason_code="COMPOSITE_ALL_SUBCONDITION_EXPIRED",
                )
            if len(sat_ids) == total_subs:
                return ConditionEvaluationResult(
                    condition_id=condition.condition_id,
                    previous_status=COND_STATUS_PENDING,
                    new_status=COND_STATUS_SATISFIED,
                    mutated=True,
                    satisfied=True,
                    expired=False,
                    satisfied_subcondition_ids=tuple(sat_ids),
                    matched_event_ref=event.event_id if event else condition.last_event_ref,
                    evaluated_at=effective_check_iso,
                    reason_code="COMPOSITE_ALL_SATISFIED",
                )
            return ConditionEvaluationResult(
                condition_id=condition.condition_id,
                previous_status=COND_STATUS_PENDING,
                new_status=COND_STATUS_PENDING,
                mutated=newly_matched_sub,
                satisfied=False,
                expired=False,
                satisfied_subcondition_ids=tuple(sat_ids),
                matched_event_ref=(
                    event.event_id if (newly_matched_sub and event) else condition.last_event_ref
                ),
                evaluated_at=effective_check_iso,
                reason_code="COMPOSITE_ALL_PARTIAL",
            )

        # COND_KIND_ANY (Section 35: no ranking/scoring; >=1 satisfied -> SATISFIED)
        if len(sat_ids) >= 1:
            return ConditionEvaluationResult(
                condition_id=condition.condition_id,
                previous_status=COND_STATUS_PENDING,
                new_status=COND_STATUS_SATISFIED,
                mutated=True,
                satisfied=True,
                expired=False,
                satisfied_subcondition_ids=tuple(sat_ids),
                matched_event_ref=event.event_id if event else condition.last_event_ref,
                evaluated_at=effective_check_iso,
                reason_code="COMPOSITE_ANY_SATISFIED",
            )
        if any_expired:
            return ConditionEvaluationResult(
                condition_id=condition.condition_id,
                previous_status=COND_STATUS_PENDING,
                new_status=COND_STATUS_EXPIRED,
                mutated=True,
                satisfied=False,
                expired=True,
                satisfied_subcondition_ids=tuple(sat_ids),
                matched_event_ref=event.event_id if event else condition.last_event_ref,
                evaluated_at=effective_check_iso,
                reason_code="COMPOSITE_ANY_EXPIRED",
            )
        return ConditionEvaluationResult(
            condition_id=condition.condition_id,
            previous_status=COND_STATUS_PENDING,
            new_status=COND_STATUS_PENDING,
            mutated=False,
            satisfied=False,
            expired=False,
            satisfied_subcondition_ids=tuple(sat_ids),
            matched_event_ref=condition.last_event_ref,
            evaluated_at=effective_check_iso,
            reason_code="COMPOSITE_ANY_PENDING",
        )


# ---------------------------------------------------------------------------
# Section 11, 13, 14, 28, 30, 31, 32, 42, 43: Deterministic Agenda Due Scanner
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReevaluationOpportunity:
    """Deterministic signal emitted when an AgendaItem becomes DUE.

    Never resumes an Activity; only triggers deterministic condition re-evaluation.
    """

    opportunity_id: str
    agenda_id: str
    activity_id: Optional[str]
    waiting_episode_id: Optional[str]
    resume_condition_ref: Optional[str]
    trigger_kind: str
    due_at: str
    observed_at: str


class AgendaDueScanner:
    """Deterministic, 0-LLM, 0-World-read time scanner for AgendaItems."""

    @staticmethod
    def evaluate_item(
        item: AgendaItem,
        *,
        now_iso: str,
    ) -> tuple[str, Optional[str], Optional[ReevaluationOpportunity]]:
        """Return (new_status, reason_code, reevaluation_opportunity_or_None)."""
        now_epoch = _ts_epoch(now_iso)

        if item.status not in OPEN_AGENDA_STATUSES:
            return item.status, item.resolution_reason_code, None

        # Section 32 & 43: Missed time window or expired agenda item -> EXPIRED (never backfills!)
        window_end_str = item.window_end or item.expires_at
        if window_end_str is not None and now_epoch > _ts_epoch(window_end_str):
            return AGENDA_STATUS_EXPIRED, "MISSED_TIME_WINDOW_EXPIRED", None

        if item.status == AGENDA_STATUS_DUE:
            # Already DUE and still within valid window: duplicate due scan is idempotent (C04)
            return AGENDA_STATUS_DUE, item.resolution_reason_code, None

        # item.status == AGENDA_STATUS_SCHEDULED
        if item.trigger_kind == TRIGGER_KIND_TIME_WINDOW:
            ws_epoch = _ts_epoch(item.window_start)
            we_epoch = _ts_epoch(item.window_end)
            if now_epoch < ws_epoch:
                return AGENDA_STATUS_SCHEDULED, None, None
            if ws_epoch <= now_epoch <= we_epoch:
                opp_seed = f"opp:{item.agenda_id}:{item.revision}:{item.window_start}"
                opp = ReevaluationOpportunity(
                    opportunity_id=f"opp:{sha256_hex(opp_seed)[:16]}",
                    agenda_id=item.agenda_id,
                    activity_id=item.activity_id,
                    waiting_episode_id=item.waiting_episode_id,
                    resume_condition_ref=item.resume_condition_ref,
                    trigger_kind=item.trigger_kind,
                    due_at=item.window_start or now_iso,
                    observed_at=now_iso,
                )
                return AGENDA_STATUS_DUE, "WINDOW_OPEN_DUE", opp

        due_epoch = _ts_epoch(item.due_at)
        if now_epoch < due_epoch:
            return AGENDA_STATUS_SCHEDULED, None, None

        opp_seed = f"opp:{item.agenda_id}:{item.revision}:{item.due_at}"
        opp = ReevaluationOpportunity(
            opportunity_id=f"opp:{sha256_hex(opp_seed)[:16]}",
            agenda_id=item.agenda_id,
            activity_id=item.activity_id,
            waiting_episode_id=item.waiting_episode_id,
            resume_condition_ref=item.resume_condition_ref,
            trigger_kind=item.trigger_kind,
            due_at=item.due_at or now_iso,
            observed_at=now_iso,
        )
        return AGENDA_STATUS_DUE, "DUE_TIME_REACHED", opp


# ---------------------------------------------------------------------------
# Section 17..48, 71: WaitingCoordinator (Orchestrates LR-2 + LR-4 Aggregates)
# ---------------------------------------------------------------------------


class WaitingCoordinator:
    """Coordinates WAITING episodes, ResumeConditions, AgendaItems, and ResumeEligibility.

    Guarantees:
    - 0 LLM calls
    - 0 outbound messages
    - 0 automatic Activity resume
    - 0 foreground preemption by background eligibility
    - Explicit RESUME requires valid LR-2 decision_ref / adoption_ref and routes
      exclusively through LR-2 ActivityCommandService.resume_activity().
    """

    def __init__(
        self,
        *,
        activity_store: CanonicalActivityStore,
        lease: ActivityAuthorityLease,
        command_service: Optional[ActivityCommandService] = None,
        condition_store: Optional[ResumeConditionStore] = None,
        agenda_store: Optional[AgendaStore] = None,
        eligibility_store: Optional[ResumeEligibilityStore] = None,
        condition_capability: Optional[LR4WriterCapability] = None,
        agenda_capability: Optional[LR4WriterCapability] = None,
        eligibility_capability: Optional[LR4WriterCapability] = None,
        namespace: str = NAMESPACE_ISOLATED_TEST,
    ) -> None:
        self.activity_store = activity_store
        self.lease = lease
        self.command_service = command_service
        self.read_service = ActivityReadService(activity_store)
        self.store_root = activity_store.store_root
        self.condition_store = condition_store or ResumeConditionStore(self.store_root)
        self.agenda_store = agenda_store or AgendaStore(self.store_root)
        self.eligibility_store = eligibility_store or ResumeEligibilityStore(self.store_root)

        self.condition_capability = condition_capability or issue_lr4_writer_capability(
            lease=lease,
            writer_domain=WRITER_DOMAIN_RESUME_CONDITION,
            namespace=namespace,
        )
        self.agenda_capability = agenda_capability or issue_lr4_writer_capability(
            lease=lease,
            writer_domain=WRITER_DOMAIN_AGENDA,
            namespace=namespace,
        )
        self.eligibility_capability = eligibility_capability or issue_lr4_writer_capability(
            lease=lease,
            writer_domain=WRITER_DOMAIN_ELIGIBILITY,
            namespace=namespace,
        )

        # Deterministic telemetry counters for zero-LLM / zero-outbound / zero-auto-resume proofs
        self.llm_call_count: int = 0
        self.outbound_message_count: int = 0
        self.auto_resume_count: int = 0
        self.user_resume_hints: list[dict[str, Any]] = []

    # -- Helper:Supersede / Invalidate Prior Records ------------------------

    def _collect_stale_and_terminal_updates(
        self,
        *,
        canonical_activities: Mapping[str, Mapping[str, Any]],
        cond_state: Mapping[str, Any],
        agenda_state: Mapping[str, Any],
        elig_state: Mapping[str, Any],
        now_iso: str,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
        """Identify any open Condition, AgendaItem, or Eligibility invalidated by Activity revision/terminal state."""
        cond_updates: list[dict[str, Any]] = []
        agenda_updates: list[dict[str, Any]] = []
        elig_updates: list[dict[str, Any]] = []

        for cid, cdict in (cond_state.get("conditions") or {}).items():
            if cdict.get("status") not in OPEN_CONDITION_STATUSES:
                continue
            act_id = cdict.get("activity_id")
            act = canonical_activities.get(act_id)
            if act is None or act.get("status") in TERMINAL_ACTIVITY_STATUSES:
                updated = copy.deepcopy(dict(cdict))
                updated["status"] = COND_STATUS_CANCELLED
                updated["cancelled_at"] = now_iso
                updated["resolution_reason_code"] = "TERMINAL_ACTIVITY_INVALIDATED"
                cond_updates.append(updated)
            elif (
                act.get("status") != STATUS_WAITING
                or int(act.get("revision", 0)) != int(cdict.get("activity_revision_at_bind", -1))
                or (
                    act.get("waiting_episode_id") is not None
                    and act.get("waiting_episode_id") != cdict.get("waiting_episode_id")
                )
            ):
                updated = copy.deepcopy(dict(cdict))
                updated["status"] = COND_STATUS_STALE
                updated["cancelled_at"] = now_iso
                updated["resolution_reason_code"] = "STALE_ACTIVITY_REVISION"
                cond_updates.append(updated)

        for aid, adict in (agenda_state.get("agenda_items") or {}).items():
            if adict.get("status") not in OPEN_AGENDA_STATUSES:
                continue
            act_id = adict.get("activity_id")
            if not act_id:
                continue
            act = canonical_activities.get(act_id)
            if act is None or act.get("status") in TERMINAL_ACTIVITY_STATUSES:
                updated = copy.deepcopy(dict(adict))
                updated["status"] = AGENDA_STATUS_CANCELLED
                updated["cancelled_at"] = now_iso
                updated["resolution_reason_code"] = "TERMINAL_ACTIVITY_INVALIDATED"
                agenda_updates.append(updated)
            elif (
                act.get("status") != STATUS_WAITING
                or (
                    adict.get("activity_revision_at_bind") is not None
                    and int(act.get("revision", 0)) != int(adict.get("activity_revision_at_bind", -1))
                )
                or (
                    adict.get("waiting_episode_id") is not None
                    and act.get("waiting_episode_id") is not None
                    and act.get("waiting_episode_id") != adict.get("waiting_episode_id")
                )
            ):
                updated = copy.deepcopy(dict(adict))
                updated["status"] = AGENDA_STATUS_CANCELLED
                updated["cancelled_at"] = now_iso
                updated["resolution_reason_code"] = "STALE_ACTIVITY_REVISION"
                agenda_updates.append(updated)

        for eid, edict in (elig_state.get("eligibilities") or {}).items():
            if edict.get("status") not in OPEN_ELIGIBILITY_STATUSES:
                continue
            act_id = edict.get("activity_id")
            act = canonical_activities.get(act_id)
            if act is None or act.get("status") in TERMINAL_ACTIVITY_STATUSES:
                updated = copy.deepcopy(dict(edict))
                updated["status"] = ELIG_STATUS_CANCELLED
                updated["resolution_reason_code"] = "TERMINAL_ACTIVITY_INVALIDATED"
                elig_updates.append(updated)
            elif (
                act.get("status") != STATUS_WAITING
                or int(act.get("revision", 0)) != int(edict.get("activity_revision", -1))
                or (
                    act.get("waiting_episode_id") is not None
                    and act.get("waiting_episode_id") != edict.get("waiting_episode_id")
                )
            ):
                updated = copy.deepcopy(dict(edict))
                updated["status"] = ELIG_STATUS_STALE
                updated["resolution_reason_code"] = "STALE_ACTIVITY_REVISION"
                elig_updates.append(updated)

        return cond_updates, agenda_updates, elig_updates

    def reconcile_with_canonical_activities(
        self,
        *,
        now_at: Optional[str] = None,
    ) -> dict[str, int]:
        """Synchronize Condition/Agenda/Eligibility lifecycle with Activity revision & terminal states."""
        now_iso = _require_iso(now_at, "now_at") if now_at else _iso(now())
        can_state, _ = self.activity_store.load_verified_state_and_journal()
        activities = can_state.get("activities") or {}

        cond_state = self.condition_store.load_state()
        agenda_state = self.agenda_store.load_state()
        elig_state = self.eligibility_store.load_state()

        c_upd, a_upd, e_upd = self._collect_stale_and_terminal_updates(
            canonical_activities=activities,
            cond_state=cond_state,
            agenda_state=agenda_state,
            elig_state=elig_state,
            now_iso=now_iso,
        )

        if c_upd:
            self.condition_store.commit_records(
                lease=self.lease,
                capability=self.condition_capability,
                records=c_upd,
                action="INVALIDATE_STALE_OR_TERMINAL_CONDITIONS",
                idempotency_key=f"reconcile_cond:{can_state['revision']}:{ len(c_upd)}:{c_upd[0]['condition_id']}",
            )
        if a_upd:
            self.agenda_store.commit_records(
                lease=self.lease,
                capability=self.agenda_capability,
                records=a_upd,
                action="INVALIDATE_STALE_OR_TERMINAL_AGENDA",
                idempotency_key=f"reconcile_agnd:{can_state['revision']}:{len(a_upd)}:{a_upd[0]['agenda_id']}",
            )
        if e_upd:
            self.eligibility_store.commit_records(
                lease=self.lease,
                capability=self.eligibility_capability,
                records=e_upd,
                action="INVALIDATE_STALE_OR_TERMINAL_ELIGIBILITY",
                idempotency_key=f"reconcile_elig:{can_state['revision']}:{len(e_upd)}:{e_upd[0]['eligibility_id']}",
            )

        return {
            "invalidated_conditions": len(c_upd),
            "invalidated_agenda_items": len(a_upd),
            "invalidated_eligibilities": len(e_upd),
        }

    # -- Entering WAITING & Binding Conditions / Agenda (Sections 5..12, 26, 27) --

    def set_consequence_port(self, port: Any) -> None:
        """Composition root only: hand LR-4 its typed ACTIVITY_CONSEQUENCE port."""
        if port is not None and not isinstance(port, ActivityConsequencePort):
            raise TypeError("LR-4 requires an ActivityConsequencePort")
        self.activity_consequence_port = port

    def enter_waiting_with_condition(
        self,
        *,
        activity_id: str,
        expected_revision: int,
        waiting_on_ref: str,
        condition_kind: str,
        parameters: Mapping[str, Any],
        source_refs: Sequence[str],
        idempotency_key: str,
        causal_parent_refs: Optional[Sequence[str]] = None,
        condition_expires_at: Optional[str] = None,
        schedule_agenda: Optional[Mapping[str, Any]] = None,
        release_foreground: bool = False,
        occurred_at: Optional[str] = None,
        observed_at: Optional[str] = None,
        fault_hook: Optional[Callable[[str], None]] = None,
        reason_kind: str = REASON_KIND_RUNTIME_CONSTRAINT,
        decision_ref: Optional[str] = None,
        adoption_ref: Optional[str] = None,
    ) -> dict[str, Any]:
        """Transition an ACTIVE/PAUSED Activity to WAITING and bind a new ResumeCondition (+ optional AgendaItem)."""
        if self.command_service is None:
            raise WriterCapabilityError("enter_waiting_with_condition requires an ActivityCommandService")

        now_iso = _require_iso(occurred_at, "occurred_at") if occurred_at else _iso(now())
        obs_iso = _require_iso(observed_at, "observed_at") if observed_at else now_iso

        # Generate deterministic waiting_episode_id and condition_id (Section 26 & 27)
        ep_seed = f"wep:{activity_id}:{expected_revision}:{idempotency_key}"
        waiting_episode_id = f"wep:{sha256_hex(ep_seed)[:16]}"
        cond_seed = f"rcond:{activity_id}:{waiting_episode_id}:{condition_kind}:{idempotency_key}"
        condition_id = f"rcond:{sha256_hex(cond_seed)[:16]}"

        # Pre-validate ResumeCondition before mutating Activity
        target_act_revision = expected_revision + 1
        preview_condition = ResumeCondition(
            condition_id=condition_id,
            schema_version=SCHEMA_RESUME_CONDITION,
            revision=1,
            subject_id=GLOBAL_SUBJECT_ID,
            activity_id=activity_id,
            activity_revision_at_bind=target_act_revision,
            waiting_episode_id=waiting_episode_id,
            condition_kind=condition_kind,
            source_refs=list(source_refs),
            causal_parent_refs=list(causal_parent_refs or []),
            waiting_on_ref=waiting_on_ref,
            parameters=dict(parameters),
            status=COND_STATUS_PENDING,
            created_at=now_iso,
            observed_at=obs_iso,
            expires_at=condition_expires_at,
            idempotency_key=f"cond_bind:{idempotency_key}",
        )

        # 1. Two legal sources reach the same WAITING state, with different provenance:
        #    * AGENCY_AUTHORIZED: an adopted Decision to wait, carrying the Decision and
        #      the Adoption owner's command-scoped grant through LR-2;
        #    * ACTIVITY_CONSEQUENCE: an LR-4 runtime constraint, proposed through the
        #      typed consequence port the composition root handed to LR-4.
        #    They are deliberately NOT merged into one fake source.
        if decision_ref or adoption_ref:
            if reason_kind != REASON_KIND_AGENCY_CHOICE:
                raise WriterCapabilityError(
                    "LR-4 agency WAIT requires reason_kind=AGENCY_CHOICE"
                )
            if not (decision_ref and adoption_ref):
                raise WriterCapabilityError(
                    "LR-4 agency WAIT requires both a Decision and an Adoption authorization"
                )
            wait_res = self.command_service.wait_activity(
                activity_id=activity_id,
                waiting_on_ref=waiting_on_ref,
                resume_condition_ref=condition_id,
                waiting_episode_id=waiting_episode_id,
                release_foreground=release_foreground,
                expected_revision=expected_revision,
                idempotency_key=idempotency_key,
                source_refs=source_refs,
                causal_parent_refs=causal_parent_refs,
                decision_ref=decision_ref,
                adoption_ref=adoption_ref,
                reason_code="ADOPTED_DECISION",
                occurred_at=now_iso,
                observed_at=obs_iso,
            )
        else:
            if reason_kind == REASON_KIND_AGENCY_CHOICE:
                raise WriterCapabilityError(
                    "an agency-choice WAIT cannot be laundered as a runtime consequence; "
                    "it needs a Decision and an Adoption authorization"
                )
            _consequence_port = getattr(self, "activity_consequence_port", None)
            if _consequence_port is None:
                raise WriterCapabilityError(
                    "LR-4 WAIT requires an ActivityConsequencePort from the composition root"
                )
            wait_res = _consequence_port.propose_waiting(
                WaitingProposal(
                    activity_id=activity_id,
                    expected_revision=expected_revision,
                    waiting_on_ref=waiting_on_ref,
                    proposal_id=idempotency_key,
                    reason_kind=reason_kind,
                    resume_condition_ref=condition_id,
                    waiting_episode_id=waiting_episode_id,
                    release_foreground=release_foreground,
                    idempotency_key=idempotency_key,
                    source_refs=tuple(source_refs or ()),
                    causal_parent_refs=tuple(causal_parent_refs or ()),
                    occurred_at=now_iso,
                    observed_at=obs_iso,
                )
            )
        actual_act_rev = int(wait_res.activity["revision"])
        preview_condition.activity_revision_at_bind = actual_act_rev

        # 2. Supersede any prior open conditions for this activity (Section 26)
        cond_state = self.condition_store.load_state()
        records_to_commit: list[dict[str, Any]] = []
        for existing_cid, existing_cdict in (cond_state.get("conditions") or {}).items():
            if (
                existing_cdict.get("activity_id") == activity_id
                and existing_cid != condition_id
                and existing_cdict.get("status") in OPEN_CONDITION_STATUSES
            ):
                sup = copy.deepcopy(dict(existing_cdict))
                sup["status"] = COND_STATUS_SUPERSEDED
                sup["cancelled_at"] = now_iso
                sup["resolution_reason_code"] = "SUPERSEDED_BY_NEW_WAITING_EPISODE"
                records_to_commit.append(sup)

        records_to_commit.append(preview_condition.to_dict())

        if fault_hook is not None:
            fault_hook("before_condition_commit")

        self.condition_store.commit_records(
            lease=self.lease,
            capability=self.condition_capability,
            records=records_to_commit,
            action="BIND_RESUME_CONDITION",
            idempotency_key=f"cond_bind:{idempotency_key}",
            fault_hook=fault_hook,
        )

        if fault_hook is not None:
            fault_hook("after_condition_commit")

        # Supersede any prior open eligibilities for this activity
        elig_state = self.eligibility_store.load_state()
        sup_eligs: list[dict[str, Any]] = []
        for eid, edict in (elig_state.get("eligibilities") or {}).items():
            if (
                edict.get("activity_id") == activity_id
                and edict.get("status") in OPEN_ELIGIBILITY_STATUSES
                and edict.get("waiting_episode_id") != waiting_episode_id
            ):
                sup_e = copy.deepcopy(dict(edict))
                sup_e["status"] = ELIG_STATUS_SUPERSEDED
                sup_e["resolution_reason_code"] = "SUPERSEDED_BY_NEW_WAITING_EPISODE"
                sup_eligs.append(sup_e)
        if sup_eligs:
            self.eligibility_store.commit_records(
                lease=self.lease,
                capability=self.eligibility_capability,
                records=sup_eligs,
                action="SUPERSEDE_PRIOR_ELIGIBILITIES",
                idempotency_key=f"elig_sup:{idempotency_key}",
            )

        # 3. Optional AgendaItem creation
        agenda_dict: Optional[dict[str, Any]] = None
        if schedule_agenda is not None:
            agenda_dict = self.schedule_agenda_item(
                activity_id=activity_id,
                activity_revision_at_bind=actual_act_rev,
                waiting_episode_id=waiting_episode_id,
                resume_condition_ref=condition_id,
                trigger_kind=str(schedule_agenda.get("trigger_kind") or TRIGGER_KIND_TIME_AT),
                due_at=schedule_agenda.get("due_at"),
                window_start=schedule_agenda.get("window_start"),
                window_end=schedule_agenda.get("window_end"),
                expires_at=schedule_agenda.get("expires_at"),
                source_refs=source_refs,
                causal_parent_refs=[condition_id, *(causal_parent_refs or [])],
                created_at=now_iso,
                idempotency_key=str(schedule_agenda.get("idempotency_key") or f"agnd_bind:{idempotency_key}"),
            )

        return {
            "wait_result": wait_res,
            "activity": copy.deepcopy(wait_res.activity),
            "waiting_episode_id": waiting_episode_id,
            "resume_condition": preview_condition.to_dict(),
            "agenda_item": agenda_dict,
        }

    def schedule_agenda_item(
        self,
        *,
        trigger_kind: str,
        source_refs: Sequence[str],
        idempotency_key: str,
        activity_id: Optional[str] = None,
        activity_revision_at_bind: Optional[int] = None,
        waiting_episode_id: Optional[str] = None,
        resume_condition_ref: Optional[str] = None,
        due_at: Optional[str] = None,
        window_start: Optional[str] = None,
        window_end: Optional[str] = None,
        expires_at: Optional[str] = None,
        causal_parent_refs: Optional[Sequence[str]] = None,
        created_at: Optional[str] = None,
        fault_hook: Optional[Callable[[str], None]] = None,
    ) -> dict[str, Any]:
        """Create and persist an AgendaItem in AgendaStore."""
        now_iso = _require_iso(created_at, "created_at") if created_at else _iso(now())
        agnd_seed = f"agnd:{activity_id}:{resume_condition_ref}:{trigger_kind}:{idempotency_key}"
        agenda_id = f"agnd:{sha256_hex(agnd_seed)[:16]}"

        item = AgendaItem(
            agenda_id=agenda_id,
            schema_version=SCHEMA_AGENDA_ITEM,
            revision=1,
            subject_id=GLOBAL_SUBJECT_ID,
            activity_id=activity_id,
            activity_revision_at_bind=activity_revision_at_bind,
            waiting_episode_id=waiting_episode_id,
            resume_condition_ref=resume_condition_ref,
            trigger_kind=trigger_kind,
            due_at=due_at,
            window_start=window_start,
            window_end=window_end,
            source_refs=list(source_refs),
            causal_parent_refs=list(causal_parent_refs or []),
            status=AGENDA_STATUS_SCHEDULED,
            created_at=now_iso,
            expires_at=expires_at,
            idempotency_key=idempotency_key,
        )

        # Supersede prior open agenda items for the same activity if from an older waiting episode
        agenda_state = self.agenda_store.load_state()
        records_to_commit: list[dict[str, Any]] = []
        if activity_id and waiting_episode_id:
            for existing_id, existing_dict in (agenda_state.get("agenda_items") or {}).items():
                if (
                    existing_dict.get("activity_id") == activity_id
                    and existing_id != agenda_id
                    and existing_dict.get("status") in OPEN_AGENDA_STATUSES
                    and existing_dict.get("waiting_episode_id") != waiting_episode_id
                ):
                    sup = copy.deepcopy(dict(existing_dict))
                    sup["status"] = AGENDA_STATUS_SUPERSEDED
                    sup["cancelled_at"] = now_iso
                    sup["resolution_reason_code"] = "SUPERSEDED_BY_NEW_WAITING_EPISODE"
                    records_to_commit.append(sup)

        records_to_commit.append(item.to_dict())
        committed_state, _ = self.agenda_store.commit_records(
            lease=self.lease,
            capability=self.agenda_capability,
            records=records_to_commit,
            action="SCHEDULE_AGENDA_ITEM",
            idempotency_key=idempotency_key,
            fault_hook=fault_hook,
        )
        return copy.deepcopy(committed_state["agenda_items"][agenda_id])

    def cancel_agenda_item(
        self,
        agenda_id: str,
        *,
        reason_code: str = "CANCELLED_EXPLICITLY",
        cancelled_at: Optional[str] = None,
    ) -> dict[str, Any]:
        now_iso = _require_iso(cancelled_at, "cancelled_at") if cancelled_at else _iso(now())
        state = self.agenda_store.load_state()
        adict = state.get("agenda_items", {}).get(agenda_id)
        if adict is None:
            raise WaitingAgendaError(f"agenda_id={agenda_id!r} not found")
        if adict["status"] in TERMINAL_AGENDA_STATUSES:
            return copy.deepcopy(adict)
        updated = copy.deepcopy(adict)
        updated["status"] = AGENDA_STATUS_CANCELLED
        updated["cancelled_at"] = now_iso
        updated["resolution_reason_code"] = reason_code
        committed, _ = self.agenda_store.commit_records(
            lease=self.lease,
            capability=self.agenda_capability,
            records=[updated],
            action="CANCEL_AGENDA_ITEM",
            idempotency_key=f"cancel_agnd:{agenda_id}:{adict['revision']}",
        )
        return copy.deepcopy(committed["agenda_items"][agenda_id])

    # -- Emitting ResumeEligibility (Section 15, 16, 22) --------------------

    def _build_or_get_eligibility_record(
        self,
        *,
        condition: ResumeCondition,
        elig_state: Mapping[str, Any],
        source_event_refs: Sequence[str],
        causal_parent_refs: Sequence[str],
        eligible_at: str,
        agenda_ref: Optional[str] = None,
        expires_at: Optional[str] = None,
    ) -> tuple[Optional[dict[str, Any]], dict[str, Any]]:
        """Return (new_record_to_commit_or_None, existing_or_new_record)."""
        # Section 22 & D04 & I07: At most 1 OPEN ResumeEligibility per (activity_id, activity_revision, waiting_episode_id, condition_id)
        for existing in (elig_state.get("eligibilities") or {}).values():
            if (
                existing.get("activity_id") == condition.activity_id
                and int(existing.get("activity_revision", -1)) == condition.activity_revision_at_bind
                and existing.get("waiting_episode_id") == condition.waiting_episode_id
                and existing.get("resume_condition_ref") == condition.condition_id
                and existing.get("status") in (ELIG_STATUS_OPEN, ELIG_STATUS_CONSUMED)
            ):
                return None, copy.deepcopy(dict(existing))

        idem_key = f"elig:{condition.condition_id}:{condition.activity_revision_at_bind}:{condition.waiting_episode_id}"
        elig_id = f"relig:{sha256_hex(idem_key)[:16]}"
        rec_ts = _iso(now())
        elig = ResumeEligibility(
            eligibility_id=elig_id,
            schema_version=SCHEMA_RESUME_ELIGIBILITY,
            revision=1,
            subject_id=GLOBAL_SUBJECT_ID,
            activity_id=condition.activity_id,
            activity_revision=condition.activity_revision_at_bind,
            waiting_episode_id=condition.waiting_episode_id,
            resume_condition_ref=condition.condition_id,
            agenda_ref=agenda_ref,
            source_event_refs=list(source_event_refs),
            causal_parent_refs=list(causal_parent_refs),
            eligible_at=eligible_at,
            recorded_at=rec_ts,
            expires_at=expires_at or condition.expires_at,
            policy_version=POLICY_VERSION_LR4,
            status=ELIG_STATUS_OPEN,
            idempotency_key=idem_key,
        )
        d = elig.to_dict()
        return d, d

    # -- Event Evaluation & Event Storm Processing (Sections 20..25, 63) ----

    def evaluate_event(
        self,
        event_input: NormalizedWaitingEvent | Mapping[str, Any],
        *,
        fault_hook: Optional[Callable[[str], None]] = None,
    ) -> dict[str, Any]:
        """Evaluate a single normalized event against open ResumeConditions (0 LLM, 0 auto-resume)."""
        if isinstance(event_input, NormalizedWaitingEvent):
            event = event_input
        else:
            event = WaitingEventAdapter.adapt_generic_event(event_input)
        res = self.evaluate_event_batch([event], fault_hook=fault_hook)
        return {
            "event_id": event.event_id,
            "satisfied_condition_ids": res["satisfied_condition_ids"],
            "expired_condition_ids": res["expired_condition_ids"],
            "emitted_eligibilities": res["emitted_eligibilities"],
            "llm_calls": 0,
            "auto_resumed": False,
        }

    def evaluate_event_batch(
        self,
        events: Sequence[NormalizedWaitingEvent | Mapping[str, Any]],
        *,
        fault_hook: Optional[Callable[[str], None]] = None,
    ) -> dict[str, Any]:
        """Deterministically evaluate a batch of events ordered by (observed_at, occurred_at, event_id).

        Queue arrival order (`enqueued_at`) NEVER determines priority or outcome (Section 21, G03).
        """
        normalized_events: list[NormalizedWaitingEvent] = []
        for ev in events:
            if isinstance(ev, NormalizedWaitingEvent):
                normalized_events.append(ev)
            else:
                normalized_events.append(WaitingEventAdapter.adapt_generic_event(ev))

        # Sort causally by (observed_at, occurred_at, event_id), ignoring queue arrival order
        normalized_events.sort(
            key=lambda e: (_ts_epoch(e.observed_at), _ts_epoch(e.occurred_at), e.event_id)
        )

        # Reconcile stale/terminal conditions first
        self.reconcile_with_canonical_activities()

        can_state, _ = self.activity_store.load_verified_state_and_journal()
        activities = can_state.get("activities") or {}
        cond_state = self.condition_store.load_state()
        elig_state = self.eligibility_store.load_state()

        conditions_map: dict[str, ResumeCondition] = {}
        by_waiting_ref: dict[str, list[str]] = {}
        generic_open_ids: list[str] = []

        for cid, cdict in (cond_state.get("conditions") or {}).items():
            if cdict.get("status") in OPEN_CONDITION_STATUSES:
                rc = ResumeCondition.from_dict(cdict)
                conditions_map[cid] = rc
                if rc.waiting_on_ref and rc.condition_kind in (
                    COND_KIND_EXTERNAL_RESULT_EVENT,
                    COND_KIND_EVENT_MATCH,
                ):
                    by_waiting_ref.setdefault(rc.waiting_on_ref, []).append(cid)
                else:
                    generic_open_ids.append(cid)

        mutated_conditions: dict[str, dict[str, Any]] = {}
        new_eligibilities: dict[str, dict[str, Any]] = {}
        all_emitted_eligibilities: list[dict[str, Any]] = []
        satisfied_ids: list[str] = []
        expired_ids: list[str] = []
        batch_cond_idems: list[str] = []
        batch_elig_idems: list[str] = []

        for event in normalized_events:
            if event.event_category == EVENT_CATEGORY_USER_RESUME_HINT:
                self.user_resume_hints.append(
                    {
                        "event_id": event.event_id,
                        "observed_at": event.observed_at,
                        "attributes": dict(event.attributes),
                    }
                )
                continue
            if event.event_category == EVENT_CATEGORY_MEMORY_RECALL:
                continue

            candidate_cids: list[str] = []
            if event.waiting_on_ref and event.waiting_on_ref in by_waiting_ref:
                candidate_cids.extend(by_waiting_ref[event.waiting_on_ref])
            candidate_cids.extend(generic_open_ids)

            for cid in candidate_cids:
                rc = conditions_map.get(cid)
                if rc is None or rc.status != COND_STATUS_PENDING:
                    continue

                # Verify Activity is still WAITING at bound revision
                act = activities.get(rc.activity_id)
                if (
                    act is None
                    or act.get("status") != STATUS_WAITING
                    or int(act.get("revision", -1)) != rc.activity_revision_at_bind
                ):
                    continue

                eval_res = ResumeConditionEvaluator.evaluate(rc, event=event)
                if not eval_res.mutated:
                    continue

                rc.status = eval_res.new_status
                rc.satisfied_subcondition_ids = list(eval_res.satisfied_subcondition_ids)
                rc.last_event_ref = eval_res.matched_event_ref
                rc.resolution_reason_code = eval_res.reason_code
                if eval_res.satisfied:
                    rc.satisfied_at = eval_res.evaluated_at
                    rc.observed_at = event.observed_at
                    satisfied_ids.append(cid)
                elif eval_res.expired:
                    rc.cancelled_at = eval_res.evaluated_at
                    expired_ids.append(cid)

                mutated_conditions[cid] = rc.to_dict()
                batch_cond_idems.append(f"ev_cond:{event.event_id}:{cid}")

                if eval_res.satisfied:
                    new_elig, elig_record = self._build_or_get_eligibility_record(
                        condition=rc,
                        elig_state=elig_state,
                        source_event_refs=[event.event_id, *event.source_refs],
                        causal_parent_refs=[rc.condition_id, event.event_id],
                        eligible_at=eval_res.evaluated_at,
                    )
                    if new_elig is not None and new_elig["eligibility_id"] not in new_eligibilities:
                        new_eligibilities[new_elig["eligibility_id"]] = new_elig
                        elig_state.setdefault("eligibilities", {})[new_elig["eligibility_id"]] = new_elig
                        batch_elig_idems.append(new_elig["idempotency_key"])
                    all_emitted_eligibilities.append(elig_record)

        if fault_hook is not None:
            fault_hook("before_condition_commit")

        if mutated_conditions:
            first_key = batch_cond_idems[0]
            self.condition_store.commit_records(
                lease=self.lease,
                capability=self.condition_capability,
                records=list(mutated_conditions.values()),
                action="EVALUATE_EVENT_CONDITIONS",
                idempotency_key=f"batch_cond:{sha256_hex('|'.join(batch_cond_idems))[:16]}",
                batch_idempotency_keys=batch_cond_idems,
                fault_hook=fault_hook,
            )

        if fault_hook is not None:
            fault_hook("after_condition_commit")
            fault_hook("before_eligibility_emit")

        if new_eligibilities:
            self.eligibility_store.commit_records(
                lease=self.lease,
                capability=self.eligibility_capability,
                records=list(new_eligibilities.values()),
                action="EMIT_RESUME_ELIGIBILITY",
                idempotency_key=f"batch_elig:{sha256_hex('|'.join(batch_elig_idems))[:16]}",
                batch_idempotency_keys=batch_elig_idems,
                fault_hook=fault_hook,
            )

        if fault_hook is not None:
            fault_hook("after_eligibility_emit")

        return {
            "processed_event_count": len(normalized_events),
            "satisfied_condition_ids": satisfied_ids,
            "expired_condition_ids": expired_ids,
            "emitted_eligibilities": all_emitted_eligibilities,
            "llm_calls": 0,
            "auto_resumed": False,
        }

    # -- Deterministic Time / Agenda Due Scanner (Sections 14, 30..32, 42, 43, 64) --

    def scan_due_agenda(
        self,
        *,
        now_at: Optional[str] = None,
        fault_hook: Optional[Callable[[str], None]] = None,
    ) -> dict[str, Any]:
        """Run deterministic due scan across AgendaItems and time conditions (0 LLM, 0 World read, 0 auto-resume)."""
        now_iso = _require_iso(now_at, "now_at") if now_at else _iso(now())
        now_epoch = _ts_epoch(now_iso)

        self.reconcile_with_canonical_activities(now_at=now_iso)

        can_state, _ = self.activity_store.load_verified_state_and_journal()
        activities = can_state.get("activities") or {}
        cond_state = self.condition_store.load_state()
        agenda_state = self.agenda_store.load_state()
        elig_state = self.eligibility_store.load_state()

        conditions_map: dict[str, ResumeCondition] = {
            cid: ResumeCondition.from_dict(cdict)
            for cid, cdict in (cond_state.get("conditions") or {}).items()
        }

        mutated_agendas: dict[str, dict[str, Any]] = {}
        mutated_conditions: dict[str, dict[str, Any]] = {}
        mutated_eligibilities: dict[str, dict[str, Any]] = {}
        opportunities: list[dict[str, Any]] = []
        due_agenda_ids: list[str] = []
        expired_agenda_ids: list[str] = []
        emitted_eligibilities: list[dict[str, Any]] = []

        agenda_items_raw = agenda_state.get("agenda_items") or {}
        for aid in sorted(agenda_items_raw.keys()):
            adict = agenda_items_raw[aid]
            if adict.get("status") not in OPEN_AGENDA_STATUSES:
                continue
            item = AgendaItem.from_dict(adict)
            new_status, reason_code, opp = AgendaDueScanner.evaluate_item(item, now_iso=now_iso)

            if new_status != item.status:
                item.status = new_status
                item.resolution_reason_code = reason_code
                if new_status == AGENDA_STATUS_DUE:
                    item.due_observed_at = now_iso
                    due_agenda_ids.append(aid)
                elif new_status == AGENDA_STATUS_EXPIRED:
                    item.cancelled_at = now_iso
                    expired_agenda_ids.append(aid)
                mutated_agendas[aid] = item.to_dict()

            # Section 32: If AgendaItem expired (e.g. window_end passed without adoption),
            # expire any open linked condition and open linked eligibility, while keeping Activity WAITING
            if new_status == AGENDA_STATUS_EXPIRED:
                if item.resume_condition_ref and item.resume_condition_ref in conditions_map:
                    rc = conditions_map[item.resume_condition_ref]
                    if rc.status == COND_STATUS_PENDING:
                        rc.status = COND_STATUS_EXPIRED
                        rc.cancelled_at = now_iso
                        rc.resolution_reason_code = "TIME_WINDOW_EXPIRED"
                        mutated_conditions[rc.condition_id] = rc.to_dict()
                for eid, edict in (elig_state.get("eligibilities") or {}).items():
                    if (
                        edict.get("status") == ELIG_STATUS_OPEN
                        and (
                            edict.get("agenda_ref") == aid
                            or (
                                item.resume_condition_ref
                                and edict.get("resume_condition_ref") == item.resume_condition_ref
                            )
                        )
                    ):
                        exp_e = copy.deepcopy(dict(edict))
                        exp_e["status"] = ELIG_STATUS_EXPIRED
                        exp_e["resolution_reason_code"] = "TIME_WINDOW_EXPIRED_WITHOUT_ADOPTION"
                        mutated_eligibilities[eid] = exp_e
                        elig_state["eligibilities"][eid] = exp_e

            if opp is not None:
                opportunities.append(
                    {
                        "opportunity_id": opp.opportunity_id,
                        "agenda_id": opp.agenda_id,
                        "activity_id": opp.activity_id,
                        "resume_condition_ref": opp.resume_condition_ref,
                        "observed_at": opp.observed_at,
                    }
                )
                # Section 14: Agenda DUE -> Re-evaluation Opportunity -> check ResumeCondition
                if opp.resume_condition_ref and opp.resume_condition_ref in conditions_map:
                    rc = conditions_map[opp.resume_condition_ref]
                    act = activities.get(rc.activity_id)
                    if (
                        act is not None
                        and act.get("status") == STATUS_WAITING
                        and int(act.get("revision", -1)) == rc.activity_revision_at_bind
                    ):
                        if rc.status == COND_STATUS_PENDING:
                            eval_res = ResumeConditionEvaluator.evaluate(rc, current_time=now_iso)
                            if eval_res.mutated:
                                rc.status = eval_res.new_status
                                rc.satisfied_subcondition_ids = list(eval_res.satisfied_subcondition_ids)
                                rc.resolution_reason_code = eval_res.reason_code
                                if eval_res.satisfied:
                                    rc.satisfied_at = now_iso
                                    rc.observed_at = now_iso
                                elif eval_res.expired:
                                    rc.cancelled_at = now_iso
                                mutated_conditions[rc.condition_id] = rc.to_dict()

                        if rc.status == COND_STATUS_SATISFIED:
                            new_elig, elig_rec = self._build_or_get_eligibility_record(
                                condition=rc,
                                elig_state=elig_state,
                                source_event_refs=[item.agenda_id, *item.source_refs],
                                causal_parent_refs=[rc.condition_id, item.agenda_id],
                                eligible_at=now_iso,
                                agenda_ref=item.agenda_id,
                                expires_at=item.window_end or item.expires_at or rc.expires_at,
                            )
                            if new_elig is not None and new_elig["eligibility_id"] not in mutated_eligibilities:
                                mutated_eligibilities[new_elig["eligibility_id"]] = new_elig
                                elig_state.setdefault("eligibilities", {})[new_elig["eligibility_id"]] = new_elig
                            emitted_eligibilities.append(elig_rec)

        # Also check any standalone open time/expiry conditions and open eligibilities with expires_at
        for cid, rc in conditions_map.items():
            if rc.status != COND_STATUS_PENDING or cid in mutated_conditions:
                continue
            act = activities.get(rc.activity_id)
            if (
                act is None
                or act.get("status") != STATUS_WAITING
                or int(act.get("revision", -1)) != rc.activity_revision_at_bind
            ):
                continue
            if (
                rc.condition_kind in (COND_KIND_TIME_AT_OR_AFTER, COND_KIND_TIME_WINDOW_OPEN, COND_KIND_ALL, COND_KIND_ANY)
                or (rc.expires_at is not None and now_epoch > _ts_epoch(rc.expires_at))
            ):
                eval_res = ResumeConditionEvaluator.evaluate(rc, current_time=now_iso)
                if eval_res.mutated:
                    rc.status = eval_res.new_status
                    rc.satisfied_subcondition_ids = list(eval_res.satisfied_subcondition_ids)
                    rc.resolution_reason_code = eval_res.reason_code
                    if eval_res.satisfied:
                        rc.satisfied_at = now_iso
                        rc.observed_at = now_iso
                    elif eval_res.expired:
                        rc.cancelled_at = now_iso
                    mutated_conditions[cid] = rc.to_dict()
                    if eval_res.satisfied:
                        new_elig, elig_rec = self._build_or_get_eligibility_record(
                            condition=rc,
                            elig_state=elig_state,
                            source_event_refs=rc.source_refs,
                            causal_parent_refs=[rc.condition_id],
                            eligible_at=now_iso,
                        )
                        if new_elig is not None and new_elig["eligibility_id"] not in mutated_eligibilities:
                            mutated_eligibilities[new_elig["eligibility_id"]] = new_elig
                            elig_state.setdefault("eligibilities", {})[new_elig["eligibility_id"]] = new_elig
                        emitted_eligibilities.append(elig_rec)

        for eid, edict in list((elig_state.get("eligibilities") or {}).items()):
            if edict.get("status") == ELIG_STATUS_OPEN and edict.get("expires_at"):
                if now_epoch > _ts_epoch(edict["expires_at"]) and eid not in mutated_eligibilities:
                    exp_e = copy.deepcopy(dict(edict))
                    exp_e["status"] = ELIG_STATUS_EXPIRED
                    exp_e["resolution_reason_code"] = "ELIGIBILITY_WINDOW_EXPIRED"
                    mutated_eligibilities[eid] = exp_e

        if fault_hook is not None:
            fault_hook("before_agenda_due_commit")

        if mutated_agendas:
            agnd_keys = sorted(mutated_agendas.keys())
            self.agenda_store.commit_records(
                lease=self.lease,
                capability=self.agenda_capability,
                records=[mutated_agendas[k] for k in agnd_keys],
                action="SCAN_DUE_AGENDA",
                idempotency_key=f"scan_agnd:{now_iso}:{sha256_hex('|'.join(agnd_keys))[:12]}",
                fault_hook=fault_hook,
            )

        if fault_hook is not None:
            fault_hook("after_agenda_due_commit")
            fault_hook("before_condition_commit")

        if mutated_conditions:
            cond_keys = sorted(mutated_conditions.keys())
            self.condition_store.commit_records(
                lease=self.lease,
                capability=self.condition_capability,
                records=[mutated_conditions[k] for k in cond_keys],
                action="SCAN_TIME_CONDITIONS",
                idempotency_key=f"scan_cond:{now_iso}:{sha256_hex('|'.join(cond_keys))[:12]}",
                fault_hook=fault_hook,
            )

        if fault_hook is not None:
            fault_hook("after_condition_commit")
            fault_hook("before_eligibility_emit")

        if mutated_eligibilities:
            elig_keys = sorted(mutated_eligibilities.keys())
            self.eligibility_store.commit_records(
                lease=self.lease,
                capability=self.eligibility_capability,
                records=[mutated_eligibilities[k] for k in elig_keys],
                action="SCAN_EMIT_OR_EXPIRE_ELIGIBILITY",
                idempotency_key=f"scan_elig:{now_iso}:{sha256_hex('|'.join(elig_keys))[:12]}",
                fault_hook=fault_hook,
            )

        if fault_hook is not None:
            fault_hook("after_eligibility_emit")

        return {
            "scanned_at": now_iso,
            "due_agenda_ids": due_agenda_ids,
            "expired_agenda_ids": expired_agenda_ids,
            "reevaluation_opportunities": opportunities,
            "emitted_eligibilities": emitted_eligibilities,
            "llm_calls": 0,
            "auto_resumed": False,
        }

    # -- Explicit Resume via LR-2 Adoption Gate (Sections 17, 19, 37, 55) ---

    def explicit_resume_from_eligibility(
        self,
        *,
        eligibility_id: str,
        expected_revision: int,
        idempotency_key: str,
        decision_ref: Optional[str] = None,
        adoption_ref: Optional[str] = None,
        source_refs: Optional[Sequence[str]] = None,
        allow_background_waiting: bool = False,
        occurred_at: Optional[str] = None,
        observed_at: Optional[str] = None,
        fault_hook: Optional[Callable[[str], None]] = None,
    ) -> CommandResult:
        """Execute an explicit RESUME from an OPEN ResumeEligibility after verifying LR-2 adoption provenance."""
        if self.command_service is None:
            raise WriterCapabilityError("explicit_resume_from_eligibility requires an ActivityCommandService")

        # Section 17 & E02: Validate decision_ref / adoption_ref FIRST (rejects None, eligibility_id, agenda_id, tool_result, etc.)
        norm_dec, norm_adp = validate_adoption_provenance(
            decision_ref=decision_ref,
            adoption_ref=adoption_ref,
            transition_type=TRANSITION_RESUME,
        )

        self.reconcile_with_canonical_activities(now_at=occurred_at)

        elig_state = self.eligibility_store.load_state()
        edict = (elig_state.get("eligibilities") or {}).get(eligibility_id)
        if edict is None:
            raise EligibilityNotOpenError(f"eligibility_id={eligibility_id!r} not found")

        elig = ResumeEligibility.from_dict(edict)
        if elig.status != ELIG_STATUS_OPEN:
            raise EligibilityNotOpenError(
                f"eligibility_id={eligibility_id!r} is not OPEN (status={elig.status!r})"
            )

        now_iso = _require_iso(occurred_at, "occurred_at") if occurred_at else _iso(now())
        if elig.expires_at is not None and _ts_epoch(now_iso) > _ts_epoch(elig.expires_at):
            raise EligibilityNotOpenError(f"eligibility_id={eligibility_id!r} has expired at {elig.expires_at}")

        can_state, _ = self.activity_store.load_verified_state_and_journal()
        act = (can_state.get("activities") or {}).get(elig.activity_id)
        if act is None or act.get("status") != STATUS_WAITING:
            raise EligibilityNotOpenError(
                f"target activity {elig.activity_id!r} is not in WAITING status"
            )
        if int(act.get("revision", -1)) != elig.activity_revision:
            raise StaleResumeConditionError(
                f"eligibility activity_revision={elig.activity_revision} does not match "
                f"current activity revision={act.get('revision')}"
            )

        # Section 55 E05: Route RESUME strictly through LR-2 ActivityCommandService
        merged_sources = list(source_refs or elig.source_event_refs)
        resume_res = self.command_service.resume_activity(
            activity_id=elig.activity_id,
            expected_revision=expected_revision,
            idempotency_key=idempotency_key,
            source_refs=merged_sources,
            causal_parent_refs=[elig.eligibility_id, elig.resume_condition_ref],
            decision_ref=norm_dec,
            adoption_ref=norm_adp,
            allow_background_waiting=allow_background_waiting,
            occurred_at=now_iso,
            observed_at=observed_at or now_iso,
            fault_hook=fault_hook,
        )

        trn_id = resume_res.transition["transition_id"] if resume_res.transition else None
        elig.status = ELIG_STATUS_CONSUMED
        elig.consumed_at = now_iso
        elig.consumed_by_transition_ref = trn_id
        elig.consumed_decision_ref = norm_dec
        elig.consumed_adoption_ref = norm_adp
        elig.resolution_reason_code = "CONSUMED_BY_EXPLICIT_RESUME"

        self.eligibility_store.commit_records(
            lease=self.lease,
            capability=self.eligibility_capability,
            records=[elig.to_dict()],
            action="CONSUME_ELIGIBILITY_ON_EXPLICIT_RESUME",
            idempotency_key=f"consume_elig:{eligibility_id}:{idempotency_key}",
        )

        if elig.agenda_ref:
            agnd_state = self.agenda_store.load_state()
            adict = (agnd_state.get("agenda_items") or {}).get(elig.agenda_ref)
            if adict and adict.get("status") in OPEN_AGENDA_STATUSES:
                upd_a = copy.deepcopy(dict(adict))
                upd_a["status"] = AGENDA_STATUS_CONSUMED
                upd_a["consumed_at"] = now_iso
                upd_a["resolution_reason_code"] = "CONSUMED_BY_EXPLICIT_RESUME"
                self.agenda_store.commit_records(
                    lease=self.lease,
                    capability=self.agenda_capability,
                    records=[upd_a],
                    action="CONSUME_AGENDA_ON_EXPLICIT_RESUME",
                    idempotency_key=f"consume_agnd:{elig.agenda_ref}:{idempotency_key}",
                )

        return resume_res

    # -- Cold-Start Recovery & Crash Reconciliation (Sections 41..44, 59) ----

    def recover_cold_start(
        self,
        *,
        now_at: Optional[str] = None,
    ) -> dict[str, Any]:
        """Deterministic cold-start recovery across Activity, Condition, Agenda, and Eligibility stores.

        - Never calls LLM (0 LLM)
        - Never auto-resumes any Activity (0 auto-resume)
        - Reconciles crash between condition commit and eligibility emit
        - Reconciles overdue AgendaItems and expired time windows if now_at is provided
        """
        can_state, can_journal = self.activity_store.load_verified_state_and_journal()
        cond_state, _ = self.condition_store.load_state_and_journal()
        agenda_state, _ = self.agenda_store.load_state_and_journal()
        elig_state, _ = self.eligibility_store.load_state_and_journal()

        now_iso = _require_iso(now_at, "now_at") if now_at else _iso(now())
        activities = can_state.get("activities") or {}

        # 1. If a RESUME transition already committed in LR-2 journal with causal_parent_refs containing an OPEN eligibility, mark it CONSUMED
        reconciled_consumed: list[dict[str, Any]] = []
        for entry in can_journal:
            if entry.get("transition_type") == TRANSITION_RESUME:
                for parent_ref in entry.get("causal_parent_refs") or []:
                    edict = (elig_state.get("eligibilities") or {}).get(parent_ref)
                    if edict and edict.get("status") == ELIG_STATUS_OPEN:
                        upd = copy.deepcopy(dict(edict))
                        upd["status"] = ELIG_STATUS_CONSUMED
                        upd["consumed_at"] = entry.get("occurred_at") or now_iso
                        upd["consumed_by_transition_ref"] = entry["transition_id"]
                        upd["consumed_decision_ref"] = entry.get("decision_ref")
                        upd["consumed_adoption_ref"] = entry.get("adoption_ref")
                        upd["resolution_reason_code"] = "RECONCILED_FROM_COMMITTED_RESUME"
                        reconciled_consumed.append(upd)
                        elig_state["eligibilities"][parent_ref] = upd

        if reconciled_consumed:
            self.eligibility_store.commit_records(
                lease=self.lease,
                capability=self.eligibility_capability,
                records=reconciled_consumed,
                action="RECONCILE_CONSUMED_ELIGIBILITY",
                idempotency_key=f"rec_consumed:{can_state['revision']}:{reconciled_consumed[0]['eligibility_id']}",
            )

        # 2. Invalidate stale or terminal records
        self.reconcile_with_canonical_activities(now_at=now_iso)

        # 3. I02/I03 crash reconciliation: if a ResumeCondition is already SATISFIED for a currently WAITING
        # activity at its bound revision, but a crash occurred before ResumeEligibility was emitted, emit it now
        cond_state = self.condition_store.load_state()
        elig_state = self.eligibility_store.load_state()
        missing_eligs: list[dict[str, Any]] = []
        for cid, cdict in (cond_state.get("conditions") or {}).items():
            if cdict.get("status") != COND_STATUS_SATISFIED:
                continue
            rc = ResumeCondition.from_dict(cdict)
            act = activities.get(rc.activity_id)
            if (
                act is not None
                and act.get("status") == STATUS_WAITING
                and int(act.get("revision", -1)) == rc.activity_revision_at_bind
            ):
                new_elig, _ = self._build_or_get_eligibility_record(
                    condition=rc,
                    elig_state=elig_state,
                    source_event_refs=[rc.last_event_ref] if rc.last_event_ref else rc.source_refs,
                    causal_parent_refs=[rc.condition_id],
                    eligible_at=rc.satisfied_at or now_iso,
                )
                if new_elig is not None:
                    missing_eligs.append(new_elig)
                    elig_state.setdefault("eligibilities", {})[new_elig["eligibility_id"]] = new_elig

        if missing_eligs:
            self.eligibility_store.commit_records(
                lease=self.lease,
                capability=self.eligibility_capability,
                records=missing_eligs,
                action="RECOVER_MISSING_ELIGIBILITY_AFTER_CRASH",
                idempotency_key=f"rec_missing_elig:{missing_eligs[0]['eligibility_id']}:{len(missing_eligs)}",
            )

        # 4. Section 42 & 43: Overdue Agenda & Missed Time Window reconciliation
        scan_report = None
        if now_at is not None:
            scan_report = self.scan_due_agenda(now_at=now_iso)

        life_view = self.get_current_life_view()
        return {
            "recovered": True,
            "activity_revision": can_state["revision"],
            "condition_revision": self.condition_store.load_state()["revision"],
            "agenda_revision": self.agenda_store.load_state()["revision"],
            "eligibility_revision": self.eligibility_store.load_state()["revision"],
            "overdue_scan": scan_report,
            "life_view": life_view,
            "llm_calls": 0,
            "auto_resumed": False,
        }

    # -- Read Queries & LifeFrameReadView Overlay (Sections 48 & 49) --------

    def list_conditions(
        self,
        *,
        activity_id: Optional[str] = None,
        only_open: bool = False,
    ) -> list[dict[str, Any]]:
        state = self.condition_store.load_state()
        out: list[dict[str, Any]] = []
        for cdict in (state.get("conditions") or {}).values():
            if activity_id is not None and cdict.get("activity_id") != activity_id:
                continue
            if only_open and cdict.get("status") not in OPEN_CONDITION_STATUSES:
                continue
            out.append(copy.deepcopy(dict(cdict)))
        return out

    def list_agenda_items(
        self,
        *,
        activity_id: Optional[str] = None,
        only_open: bool = False,
    ) -> list[dict[str, Any]]:
        state = self.agenda_store.load_state()
        out: list[dict[str, Any]] = []
        for adict in (state.get("agenda_items") or {}).values():
            if activity_id is not None and adict.get("activity_id") != activity_id:
                continue
            if only_open and adict.get("status") not in OPEN_AGENDA_STATUSES:
                continue
            out.append(copy.deepcopy(dict(adict)))
        return out

    def get_eligibility(self, eligibility_id: str) -> Optional[dict[str, Any]]:
        """Read-only ResumeEligibility lookup (LR-4 owned)."""
        state = self.eligibility_store.load_state()
        elig = (state.get("eligibilities") or {}).get(str(eligibility_id))
        return copy.deepcopy(elig) if elig is not None else None

    def get_eligibility(self, eligibility_id: str) -> Optional[dict[str, Any]]:
        """Read-only ResumeEligibility lookup (LR-4 owned)."""
        state = self.eligibility_store.load_state()
        elig = (state.get("eligibilities") or {}).get(str(eligibility_id))
        return copy.deepcopy(elig) if elig is not None else None

    def list_eligibilities(
        self,
        *,
        activity_id: Optional[str] = None,
        only_open: bool = False,
    ) -> list[dict[str, Any]]:
        state = self.eligibility_store.load_state()
        out: list[dict[str, Any]] = []
        for edict in (state.get("eligibilities") or {}).values():
            if activity_id is not None and edict.get("activity_id") != activity_id:
                continue
            if only_open and edict.get("status") not in OPEN_ELIGIBILITY_STATUSES:
                continue
            out.append(copy.deepcopy(dict(edict)))
        return out

    def get_current_life_view(
        self,
        *,
        caller_context: Optional[Mapping[str, Any]] = None,
        interruption_overlay: Optional[Mapping[str, Any]] = None,
    ) -> dict[str, Any]:
        """Compose read-only LifeFrameReadView enriched with LR-4 derived waiting/eligibility pointers."""
        can_state, _ = self.activity_store.load_verified_state_and_journal()
        fg_ref = can_state.get("foreground_activity_ref")
        activities = can_state.get("activities") or {}
        fg_act = activities.get(fg_ref) if fg_ref else None

        open_eligs = self.list_eligibilities(only_open=True)
        open_agendas = self.list_agenda_items(only_open=True)
        waiting_act_refs = [
            aid for aid, a in activities.items() if a.get("status") == STATUS_WAITING
        ]

        fg_eligible = any(e["activity_id"] == fg_ref for e in open_eligs) if fg_ref else bool(open_eligs)

        next_recheck_candidates: list[tuple[float, str]] = []
        for a in open_agendas:
            if a.get("status") == AGENDA_STATUS_SCHEDULED and a.get("due_at"):
                next_recheck_candidates.append((_ts_epoch(a["due_at"]), a["due_at"]))
        next_recheck_candidates.sort(key=lambda pair: pair[0])
        next_recheck_at = next_recheck_candidates[0][1] if next_recheck_candidates else None

        waiting_overlay = {
            "waiting_on_ref": fg_act.get("waiting_on_ref") if fg_act else None,
            "resume_condition_ref": fg_act.get("resume_condition_ref") if fg_act else None,
            "resume_eligible": fg_eligible,
            "next_recheck_at": next_recheck_at,
            "open_eligibility_refs": [e["eligibility_id"] for e in open_eligs],
            "waiting_activity_refs": waiting_act_refs,
        }
        return build_life_frame_read_view(
            can_state,
            caller_context=caller_context,
            interruption_overlay=interruption_overlay,
            waiting_overlay=waiting_overlay,
        )


# ---------------------------------------------------------------------------
# Section 47: Deterministic Offline Replay & Shadow Sandbox Isolation
# ---------------------------------------------------------------------------


class WaitingReplayEngine:
    """Deterministic offline replay verifier for LR-4 Condition, Agenda, and Eligibility journals."""

    @staticmethod
    def verify_all_stores(
        *,
        activity_store: CanonicalActivityStore,
        condition_store: ResumeConditionStore,
        agenda_store: AgendaStore,
        eligibility_store: ResumeEligibilityStore,
    ) -> dict[str, Any]:
        c_state, c_journal = condition_store.load_state_and_journal()
        a_state, a_journal = agenda_store.load_state_and_journal()
        e_state, e_journal = eligibility_store.load_state_and_journal()

        c_replayed = condition_store.project_from_journal(c_journal)
        a_replayed = agenda_store.project_from_journal(a_journal)
        e_replayed = eligibility_store.project_from_journal(e_journal)

        c_ok = (
            c_state["revision"] == c_replayed["revision"]
            and c_state["conditions"] == c_replayed["conditions"]
            and c_state["last_transition_hash"] == c_replayed["last_transition_hash"]
        )
        a_ok = (
            a_state["revision"] == a_replayed["revision"]
            and a_state["agenda_items"] == a_replayed["agenda_items"]
            and a_state["last_transition_hash"] == a_replayed["last_transition_hash"]
        )
        e_ok = (
            e_state["revision"] == e_replayed["revision"]
            and e_state["eligibilities"] == e_replayed["eligibilities"]
            and e_state["last_transition_hash"] == e_replayed["last_transition_hash"]
        )

        return {
            "consistent": c_ok and a_ok and e_ok,
            "condition_consistent": c_ok,
            "agenda_consistent": a_ok,
            "eligibility_consistent": e_ok,
            "condition_revision": c_state["revision"],
            "agenda_revision": a_state["revision"],
            "eligibility_revision": e_state["revision"],
        }


class WaitingAgendaSandboxRuntime:
    """Isolated shadow / replay / research sandbox that cannot write to production stores."""

    def __init__(self, sandbox_root: Path | str, *, mode: str = "shadow") -> None:
        if mode not in NON_PRODUCTION_NAMESPACES:
            raise ContractViolationError(f"invalid sandbox mode={mode!r}")
        resolved = Path(sandbox_root).expanduser().resolve()
        if is_production_path(resolved):
            raise WriterCapabilityError("sandbox_root cannot reside inside a production home")
        self.mode = mode
        self.sandbox_root = resolved
        self.conditions: dict[str, dict[str, Any]] = {}
        self.agenda_items: dict[str, dict[str, Any]] = {}
        self.eligibilities: dict[str, dict[str, Any]] = {}


# ---------------------------------------------------------------------------
# S7 Phase-F (order section 12): the waiting domain owns the canonical WaitingRef schema.
# ---------------------------------------------------------------------------
# LR-2 and the ACTIVITY_CONSEQUENCE admission boundary both DERIVE their allow-sets from
# these names, so the waiting vocabulary has exactly one owner and cannot drift again.
from waiting_ref_vocab import (  # noqa: E402
    ADMISSIBLE_WAITING_EVIDENCE_KINDS,
    CONDITION_KIND_WAITING_REF_KINDS,
    EXCLUDED_LEGACY_PREFIXES,
    VALID_WAITING_REF_KINDS,
    VALID_WAITING_REF_PREFIXES,
    WaitingRefKind,
    WaitingRefRejected,
    is_valid_waiting_ref,
    parse_waiting_ref,
    validate_waiting_ref,
    waiting_ref_kind,
)
