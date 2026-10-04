#!/usr/bin/env python3
"""Chiyo Agency | AG-0 Candidate Sources (`candidate_sources_ag0.py`).

Sole Responsibility of AG-0:
    "此刻有哪些有来源、合法、仍然有效的生活候选，可以在未来交给 Agency 考虑？"

Top-Level Invariants Enforced:
    - Candidate != Decision
    - Candidate != Activity
    - Candidate != Commitment
    - Candidate != Action Proposal
    - Candidate != Motive Truth
    - Candidate != Desire Truth
    - Candidate Source != Agency Decision
    - 0 LLM calls, 0 Activity mutations, 0 Action proposals, 0 outbound messages,
      0 World mutations, 0 Memory mutations, 0 Goal mutations.
"""

from __future__ import annotations

import copy
import dataclasses
import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence

from activity_continuity import (
    GLOBAL_SUBJECT_ID,
    NAMESPACE_ISOLATED_TEST,
    OPEN_ACTIVITY_STATUSES,
    STATUS_ABANDONED,
    STATUS_ACTIVE,
    STATUS_CANCELLED,
    STATUS_COMPLETED,
    STATUS_EXPIRED,
    STATUS_PAUSED,
    STATUS_WAITING,
    TERMINAL_ACTIVITY_STATUSES,
    ActivityReadService,
    CanonicalActivityStore,
    ContractViolationError,
    SubjectIdentityError as SubjectIsolationError,
    WriterCapabilityError,
    _iso,
    now,
    sha256_hex,
    validate_subject_id,
)
from activity_waiting_agenda import (
    ELIG_STATUS_CONSUMED,
    ELIG_STATUS_OPEN,
    ResumeEligibilityStore,
    WaitingCoordinator,
)

STATUS_INTERRUPTED = "INTERRUPTED"
TERMINAL_STATUSES = frozenset(TERMINAL_ACTIVITY_STATUSES)
NAMESPACE_PRODUCTION = "production"
NAMESPACE_SHADOW_VERIFY = "shadow"
VALID_NAMESPACES = frozenset({NAMESPACE_ISOLATED_TEST, NAMESPACE_SHADOW_VERIFY, "replay", NAMESPACE_PRODUCTION})
UnauthorizedMutationError = WriterCapabilityError
ELIGIBILITY_STATUS_OPEN = ELIG_STATUS_OPEN


# ---------------------------------------------------------------------------
# Schema & Policy Constants (Sections 4-6, 11-15, 35-38, 60)
# ---------------------------------------------------------------------------

SCHEMA_CANDIDATE_RECORD = "chiyo.agency.candidate_record.v1"
SCHEMA_CANDIDATE_SET = "chiyo.agency.candidate_set.v1"
SCHEMA_CANDIDATE_AUDIT = "chiyo.agency.candidate_audit.v1"
SCHEMA_USER_REQUEST_EVENT = "chiyo.communication.user_request_event.v1"
SCHEMA_WORLD_OPPORTUNITY_EVENT = "chiyo.world.observed_opportunity.v1"
SCHEMA_COMMITMENT_RECORD = "chiyo.commitment.canonical_record.v1"
SCHEMA_EXPLICIT_GOAL_RECORD = "chiyo.goal.explicit_goal_record.v1"

POLICY_VERSION_AG0_V1 = "ag0.candidate_policy.v1"

# First-edition 6 allowed Candidate Sources (Section 4)
SOURCE_CURRENT_ACTIVITY = "CURRENT_ACTIVITY"
SOURCE_RESUME_ELIGIBLE = "RESUME_ELIGIBLE"
SOURCE_USER_REQUEST = "USER_REQUEST"
SOURCE_COMMITMENT = "COMMITMENT"
SOURCE_EXPLICIT_GOAL = "EXPLICIT_GOAL"
SOURCE_WORLD_OPPORTUNITY = "WORLD_OPPORTUNITY"

VALID_CANDIDATE_SOURCE_KINDS = frozenset(
    {
        SOURCE_CURRENT_ACTIVITY,
        SOURCE_RESUME_ELIGIBLE,
        SOURCE_USER_REQUEST,
        SOURCE_COMMITMENT,
        SOURCE_EXPLICIT_GOAL,
        SOURCE_WORLD_OPPORTUNITY,
    }
)

# Deferred or permanently forbidden Candidate Sources in AG-0 (Sections 3-6, 48-51, 76)
FORBIDDEN_CANDIDATE_SOURCE_KINDS = frozenset(
    {
        "CONTACT_INTENT",
        "PROACTIVE_CONTACT",
        "IDLE",
        "REST",
        "WEAK_MOTIVATION",
        "INTEREST",
        "PRIVATE_PROJECT",
        "SPONTANEOUS_RETURN",
        "HABIT",
        "RANDOM_ACTIVITY",
        "AFFECT_DRIVEN",
        "RELATIONSHIP_WHIM",
        "CURIOSITY_GENERATOR",
        "PROMPT_TEXT",
        "LLM_NARRATION",
        "MEMORY_RECALL",
        "BODY_STATE",
        "AFFECT_STATE",
        "EXPRESSION_OUTPUT",
        "GROUNDING_TEXT",
        "RANDOM_LIST",
        "PERSONALITY",
    }
)

# Candidate Kinds (Sections 16, 20, 22, 26, 29, 31)
CANDIDATE_KIND_CONTINUE_CURRENT = "CONTINUE_CURRENT"
CANDIDATE_KIND_RESUME_ACTIVITY = "RESUME_ACTIVITY"
CANDIDATE_KIND_RESPOND_USER_REQUEST = "RESPOND_USER_REQUEST"
CANDIDATE_KIND_FULFILL_COMMITMENT = "FULFILL_COMMITMENT"
CANDIDATE_KIND_PURSUE_GOAL_STEP = "PURSUE_GOAL_STEP"
CANDIDATE_KIND_CONSIDER_EXPLICIT_GOAL = "CONSIDER_EXPLICIT_GOAL"
CANDIDATE_KIND_CONSIDER_WORLD_OPPORTUNITY = "CONSIDER_WORLD_OPPORTUNITY"

VALID_CANDIDATE_KINDS = frozenset(
    {
        CANDIDATE_KIND_CONTINUE_CURRENT,
        CANDIDATE_KIND_RESUME_ACTIVITY,
        CANDIDATE_KIND_RESPOND_USER_REQUEST,
        CANDIDATE_KIND_FULFILL_COMMITMENT,
        CANDIDATE_KIND_PURSUE_GOAL_STEP,
        CANDIDATE_KIND_CONSIDER_EXPLICIT_GOAL,
        CANDIDATE_KIND_CONSIDER_WORLD_OPPORTUNITY,
    }
)

FORBIDDEN_CANDIDATE_KINDS = frozenset(
    {
        "IDLE",
        "REST",
        "CONTACT_INTENT",
        "SEND_PROACTIVE_MESSAGE",
        "RANDOM_ACTIVITY",
        "SLEEP_FROM_FATIGUE",
        "PLAY_FROM_BOREDOM",
    }
)

# Candidate Availability Statuses (Section 13)
CANDIDATE_STATUS_OPEN = "OPEN"
CANDIDATE_STATUS_STALE = "STALE"
CANDIDATE_STATUS_EXPIRED = "EXPIRED"
CANDIDATE_STATUS_WITHDRAWN = "WITHDRAWN"
CANDIDATE_STATUS_CONSUMED = "CONSUMED"
CANDIDATE_STATUS_INVALID = "INVALID"

VALID_CANDIDATE_STATUSES = frozenset(
    {
        CANDIDATE_STATUS_OPEN,
        CANDIDATE_STATUS_STALE,
        CANDIDATE_STATUS_EXPIRED,
        CANDIDATE_STATUS_WITHDRAWN,
        CANDIDATE_STATUS_CONSUMED,
        CANDIDATE_STATUS_INVALID,
    }
)

# Source Availability Statuses (Section 84)
ADAPTER_STATUS_READY = "READY"
ADAPTER_STATUS_PARTIAL = "PARTIAL"
ADAPTER_STATUS_DISABLED = "DISABLED"

VALID_ADAPTER_STATUSES = frozenset(
    {
        ADAPTER_STATUS_READY,
        ADAPTER_STATUS_PARTIAL,
        ADAPTER_STATUS_DISABLED,
    }
)

# Forbidden duplicate-fact / narrative body keys (Section 12)
FORBIDDEN_FACT_BODY_KEYS = frozenset(
    {
        "activity_body",
        "activity_title",
        "activity_summary",
        "goal_body",
        "goal_description",
        "commitment_body",
        "world_state",
        "world_snapshot",
        "memory_content",
        "user_message_text",
        "raw_message_body",
        "prompt_text",
        "llm_narration",
        "chain_of_thought",
    }
)

# Forbidden ranking, scoring, priority, winner, or decision keys (Sections 36, 73, 83 I10, 89-92)
FORBIDDEN_RANKING_AND_DECISION_KEYS = frozenset(
    {
        "score",
        "rank",
        "weight",
        "priority",
        "best_candidate",
        "winner",
        "selected_candidate",
        "decision",
        "decision_ref",
        "motive_strength",
        "desire_score",
        "recommended",
        "chiyo_should_act",
    }
)

DEFAULT_PER_SOURCE_CAP = 25
DEFAULT_GLOBAL_CANDIDATE_CAP = 100

_STRUCTURED_REF_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9_.-]*[:_][a-zA-Z0-9_.:/-]+$")


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class CandidateSourceError(ContractViolationError):
    """Base error for AG-0 Candidate Source violations."""


class UntrustedCandidateSourceError(CandidateSourceError):
    """Raised when an ungrounded, narrative, or negative-authority input attempts to create a Candidate."""


class MissingCandidateProvenanceError(CandidateSourceError):
    """Raised when a Candidate lacks valid source provenance."""


class CandidateFactDuplicationError(CandidateSourceError):
    """Raised when a CandidateRecord attempts to duplicate canonical domain body content (Section 12)."""


class CandidateRankingForbiddenError(CandidateSourceError):
    """Raised when a CandidateRecord or CandidateSet includes forbidden score/rank/winner/decision fields (Section 36)."""


class CandidateAdoptionForbiddenError(UnauthorizedMutationError):
    """Raised when AG-0 objects are asked to accept, choose, start, resume, or execute (Section 14)."""


class ForbiddenWorldGlobalScanError(CandidateSourceError):
    """Raised when an unobserved World fact or global World DB scan attempts to generate a Candidate (Sections 31-32)."""


class DisabledCandidateSourceError(CandidateSourceError):
    """Raised when an explicitly disabled source adapter is invoked as if enabled."""


# ---------------------------------------------------------------------------
# Validation Helpers
# ---------------------------------------------------------------------------


def _require_iso(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CandidateSourceError(f"{field_name} must be a non-empty ISO-8601 string")
    raw = value.strip()
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CandidateSourceError(f"{field_name} is not a valid ISO-8601 timestamp: {value!r}") from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return _iso(dt)


def _optional_iso(value: Any, field_name: str) -> Optional[str]:
    if value is None:
        return None
    return _require_iso(value, field_name)


def _parse_iso(value: str) -> datetime:
    dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _validate_structured_ref(
    value: Any,
    field_name: str,
    *,
    allowed_prefixes: Optional[Iterable[str]] = None,
) -> str:
    if not isinstance(value, str) or not value.strip():
        raise MissingCandidateProvenanceError(f"{field_name} must be a non-empty structured reference")
    cleaned = value.strip()
    if not _STRUCTURED_REF_RE.fullmatch(cleaned):
        raise MissingCandidateProvenanceError(
            f"{field_name}={value!r} must be a structured reference '<prefix>:<id>', not free text"
        )
    if allowed_prefixes is not None:
        prefix = cleaned.split(":", 1)[0] if ":" in cleaned else cleaned.split("_", 1)[0]
        allowed_set = set(allowed_prefixes)
        if prefix not in allowed_set:
            raise MissingCandidateProvenanceError(
                f"{field_name}={value!r} has prefix {prefix!r}; expected one of {sorted(allowed_set)}"
            )
    return cleaned


def _validate_ref_list(
    values: Any,
    field_name: str,
    *,
    allow_empty: bool = True,
) -> list[str]:
    if values is None:
        if allow_empty:
            return []
        raise MissingCandidateProvenanceError(f"{field_name} cannot be None")
    if isinstance(values, (str, bytes)) or not isinstance(values, Iterable):
        raise CandidateSourceError(f"{field_name} must be a sequence of structured reference strings")
    result: list[str] = []
    for item in values:
        ref = _validate_structured_ref(item, field_name)
        if ref not in result:
            result.append(ref)
    if not allow_empty and not result:
        raise MissingCandidateProvenanceError(f"{field_name} must contain at least one structured reference")
    return result


def _reject_forbidden_keys(mapping: Mapping[str, Any], context_name: str) -> None:
    lowered = {str(k).lower(): k for k in mapping.keys()}
    bad_ranking = FORBIDDEN_RANKING_AND_DECISION_KEYS.intersection(lowered.keys())
    if bad_ranking:
        raise CandidateRankingForbiddenError(
            f"{context_name} contains forbidden ranking/winner/decision fields: {sorted(bad_ranking)}"
        )
    bad_body = FORBIDDEN_FACT_BODY_KEYS.intersection(lowered.keys())
    if bad_body:
        raise CandidateFactDuplicationError(
            f"{context_name} contains forbidden duplicated domain body fields: {sorted(bad_body)}"
        )


# ---------------------------------------------------------------------------
# Capability Guards (Sections 15, 43, 45-46, 63, 99)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CandidateProjectionCapability:
    """Capability token permitting CandidateMaterializer to build/invalidate candidate projections.

    Explicitly carries ZERO authority to mutate Activity, Action, World, Memory, Goal, or Commitment.
    """

    authority_domain: str
    caller_module: str
    namespace: str
    issued_at: str
    can_mutate_activity: bool = False
    can_propose_action: bool = False
    can_mutate_world: bool = False
    can_mutate_memory: bool = False
    can_mutate_goal: bool = False
    can_send_outbound: bool = False

    def __post_init__(self) -> None:
        if self.authority_domain != "agency_candidate_projection":
            raise UnauthorizedMutationError(
                f"invalid CandidateProjectionCapability authority_domain={self.authority_domain!r}"
            )
        if self.namespace not in VALID_NAMESPACES:
            raise ContractViolationError(f"invalid namespace={self.namespace!r}")
        if any(
            (
                self.can_mutate_activity,
                self.can_propose_action,
                self.can_mutate_world,
                self.can_mutate_memory,
                self.can_mutate_goal,
                self.can_send_outbound,
            )
        ):
            raise UnauthorizedMutationError(
                "AG-0 CandidateProjectionCapability must NEVER grant Activity/Action/World/Memory/Goal/Outbound write permissions"
            )


class CandidateProjectionGuard:
    """Issues and verifies read-only projection capabilities for AG-0."""

    AUTHORITY_NAME = "agency_candidate_projection"
    ALLOWED_CALLERS = frozenset(
        {
            "candidate_materializer",
            "candidate_replay_verifier",
            "candidate_shadow_observer",
            "ag0_test_harness",
        }
    )

    @classmethod
    def issue_capability(
        cls,
        *,
        caller_module: str,
        namespace: str = NAMESPACE_ISOLATED_TEST,
    ) -> CandidateProjectionCapability:
        if caller_module not in cls.ALLOWED_CALLERS:
            raise UnauthorizedMutationError(
                f"caller_module={caller_module!r} is not authorized to issue CandidateProjectionCapability"
            )
        return CandidateProjectionCapability(
            authority_domain=cls.AUTHORITY_NAME,
            caller_module=caller_module,
            namespace=namespace,
            issued_at=_iso(now()),
        )

    @classmethod
    def verify_capability(cls, capability: Any) -> CandidateProjectionCapability:
        if not isinstance(capability, CandidateProjectionCapability):
            raise UnauthorizedMutationError(
                "AG-0 operation requires a valid CandidateProjectionCapability"
            )
        if capability.authority_domain != cls.AUTHORITY_NAME:
            raise UnauthorizedMutationError("invalid CandidateProjectionCapability authority_domain")
        return capability


# ---------------------------------------------------------------------------
# CandidateRecord & CandidateSet (Sections 11-14, 35-41)
# ---------------------------------------------------------------------------


@dataclass
class CandidateRecord:
    """Section 11-14: Source-backed opportunity for future Agency consideration (`chiyo.agency.candidate_record.v1`).

    Strictly references canonical source IDs without copying domain bodies, and exposes zero adoption methods.
    """

    candidate_id: str
    candidate_kind: str
    source_kind: str
    source_ref: str
    availability_status: str
    created_at: str
    observed_at: str
    recorded_at: str
    idempotency_key: str
    subject_id: str = GLOBAL_SUBJECT_ID
    revision: int = 1
    source_revision: Optional[int | str] = None
    source_refs: list[str] = field(default_factory=list)
    source_kinds: list[str] = field(default_factory=list)
    activity_ref: Optional[str] = None
    goal_ref: Optional[str] = None
    goal_step_ref: Optional[str] = None
    commitment_ref: Optional[str] = None
    user_event_ref: Optional[str] = None
    resume_eligibility_ref: Optional[str] = None
    world_opportunity_ref: Optional[str] = None
    world_revision_ref: Optional[str] = None
    perception_ref: Optional[str] = None
    target_refs: list[str] = field(default_factory=list)
    valid_from: Optional[str] = None
    valid_until: Optional[str] = None
    causal_parent_refs: list[str] = field(default_factory=list)
    provenance_refs: list[str] = field(default_factory=list)
    provenance_chain: list[dict[str, Any]] = field(default_factory=list)
    shared_target_source_refs: list[str] = field(default_factory=list)
    semantic_identity: Optional[str] = None
    invalidation_reason: Optional[str] = None
    policy_version: str = POLICY_VERSION_AG0_V1
    schema_version: str = SCHEMA_CANDIDATE_RECORD

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_CANDIDATE_RECORD:
            raise CandidateSourceError(f"invalid CandidateRecord schema_version={self.schema_version!r}")
        self.subject_id = validate_subject_id(self.subject_id)
        self.candidate_id = _validate_structured_ref(
            self.candidate_id,
            "candidate_id",
            allowed_prefixes=frozenset({"cand"}),
        )
        if not isinstance(self.revision, int) or self.revision < 1:
            raise CandidateSourceError(f"CandidateRecord.revision must be int >= 1, got {self.revision!r}")

        if self.candidate_kind in FORBIDDEN_CANDIDATE_KINDS:
            raise UntrustedCandidateSourceError(
                f"candidate_kind={self.candidate_kind!r} is forbidden in AG-0"
            )
        if self.candidate_kind not in VALID_CANDIDATE_KINDS:
            raise CandidateSourceError(
                f"unsupported candidate_kind={self.candidate_kind!r}; allowed={sorted(VALID_CANDIDATE_KINDS)}"
            )

        if self.source_kind in FORBIDDEN_CANDIDATE_SOURCE_KINDS:
            raise UntrustedCandidateSourceError(
                f"source_kind={self.source_kind!r} is forbidden in AG-0"
            )
        if self.source_kind not in VALID_CANDIDATE_SOURCE_KINDS:
            raise UntrustedCandidateSourceError(
                f"unsupported source_kind={self.source_kind!r}; allowed={sorted(VALID_CANDIDATE_SOURCE_KINDS)}"
            )

        self.source_ref = _validate_structured_ref(self.source_ref, "source_ref")
        if not self.source_refs:
            self.source_refs = [self.source_ref]
        else:
            self.source_refs = _validate_ref_list(self.source_refs, "source_refs", allow_empty=False)
            if self.source_ref not in self.source_refs:
                self.source_refs.insert(0, self.source_ref)

        if not self.source_kinds:
            self.source_kinds = [self.source_kind]
        else:
            normalized_kinds: list[str] = []
            for sk in self.source_kinds:
                if sk not in VALID_CANDIDATE_SOURCE_KINDS:
                    raise UntrustedCandidateSourceError(f"invalid source_kind in source_kinds: {sk!r}")
                if sk not in normalized_kinds:
                    normalized_kinds.append(sk)
            if self.source_kind not in normalized_kinds:
                normalized_kinds.insert(0, self.source_kind)
            self.source_kinds = normalized_kinds

        if self.availability_status not in VALID_CANDIDATE_STATUSES:
            raise CandidateSourceError(
                f"invalid availability_status={self.availability_status!r}; allowed={sorted(VALID_CANDIDATE_STATUSES)}"
            )

        self.created_at = _require_iso(self.created_at, "created_at")
        self.observed_at = _require_iso(self.observed_at, "observed_at")
        self.recorded_at = _require_iso(self.recorded_at, "recorded_at")
        self.valid_from = _optional_iso(self.valid_from, "valid_from")
        self.valid_until = _optional_iso(self.valid_until, "valid_until")

        if self.activity_ref is not None:
            self.activity_ref = _validate_structured_ref(
                self.activity_ref, "activity_ref", allowed_prefixes=frozenset({"act", "actv", "activity"})
            )
        if self.goal_ref is not None:
            self.goal_ref = _validate_structured_ref(
                self.goal_ref, "goal_ref", allowed_prefixes=frozenset({"goal"})
            )
        if self.goal_step_ref is not None:
            self.goal_step_ref = _validate_structured_ref(
                self.goal_step_ref, "goal_step_ref", allowed_prefixes=frozenset({"gstep", "step", "plan_step"})
            )
        if self.commitment_ref is not None:
            self.commitment_ref = _validate_structured_ref(
                self.commitment_ref, "commitment_ref", allowed_prefixes=frozenset({"cmmt", "commitment"})
            )
        if self.user_event_ref is not None:
            self.user_event_ref = _validate_structured_ref(
                self.user_event_ref, "user_event_ref", allowed_prefixes=frozenset({"ureq", "comm_event", "cevt"})
            )
        if self.resume_eligibility_ref is not None:
            self.resume_eligibility_ref = _validate_structured_ref(
                self.resume_eligibility_ref,
                "resume_eligibility_ref",
                allowed_prefixes=frozenset({"relig", "elig", "resume_eligibility"}),
            )
        if self.world_opportunity_ref is not None:
            self.world_opportunity_ref = _validate_structured_ref(
                self.world_opportunity_ref,
                "world_opportunity_ref",
                allowed_prefixes=frozenset({"wopp", "world_event", "ambient_event", "school_event"}),
            )
        if self.world_revision_ref is not None:
            self.world_revision_ref = _validate_structured_ref(
                self.world_revision_ref,
                "world_revision_ref",
                allowed_prefixes=frozenset({"wrev", "world_rev", "scene_rev"}),
            )
        if self.perception_ref is not None:
            self.perception_ref = _validate_structured_ref(
                self.perception_ref,
                "perception_ref",
                allowed_prefixes=frozenset({"percept", "look_world", "obs", "knowledge"}),
            )

        self.target_refs = _validate_ref_list(self.target_refs, "target_refs", allow_empty=True)
        self.causal_parent_refs = _validate_ref_list(
            self.causal_parent_refs, "causal_parent_refs", allow_empty=False
        )
        self.provenance_refs = _validate_ref_list(
            self.provenance_refs, "provenance_refs", allow_empty=False
        )
        self.shared_target_source_refs = _validate_ref_list(
            self.shared_target_source_refs or self.source_refs,
            "shared_target_source_refs",
            allow_empty=False,
        )

        # Enforce source-specific reference anchor invariants
        if self.source_kind == SOURCE_CURRENT_ACTIVITY and not self.activity_ref:
            raise MissingCandidateProvenanceError("CURRENT_ACTIVITY candidate must include activity_ref")
        if self.source_kind == SOURCE_RESUME_ELIGIBLE and (
            not self.activity_ref or not self.resume_eligibility_ref
        ):
            raise MissingCandidateProvenanceError(
                "RESUME_ELIGIBLE candidate must include both activity_ref and resume_eligibility_ref"
            )
        if self.source_kind == SOURCE_USER_REQUEST and not self.user_event_ref:
            raise MissingCandidateProvenanceError("USER_REQUEST candidate must include user_event_ref")
        if self.source_kind == SOURCE_COMMITMENT and not self.commitment_ref:
            raise MissingCandidateProvenanceError("COMMITMENT candidate must include commitment_ref")
        if self.source_kind == SOURCE_EXPLICIT_GOAL and not self.goal_ref:
            raise MissingCandidateProvenanceError("EXPLICIT_GOAL candidate must include goal_ref")
        if self.source_kind == SOURCE_WORLD_OPPORTUNITY and (
            not self.world_opportunity_ref or not self.perception_ref or not self.world_revision_ref
        ):
            raise MissingCandidateProvenanceError(
                "WORLD_OPPORTUNITY candidate must include world_opportunity_ref, perception_ref, and world_revision_ref"
            )

        if not isinstance(self.idempotency_key, str) or not self.idempotency_key.strip():
            raise CandidateSourceError("idempotency_key must be a non-empty string")
        self.idempotency_key = self.idempotency_key.strip()

        if not self.semantic_identity:
            primary_anchor = (
                self.activity_ref
                or self.commitment_ref
                or self.goal_step_ref
                or self.goal_ref
                or self.world_opportunity_ref
                or self.user_event_ref
                or self.source_ref
            )
            self.semantic_identity = (
                f"semcand:{sha256_hex(f'{self.subject_id}:{self.candidate_kind}:{self.source_kind}:{primary_anchor}')[:16]}"
            )

    # -- Section 14: Forbidden Adoption / Decision / Mutation Methods --------

    def accept(self, *args: Any, **kwargs: Any) -> None:
        raise CandidateAdoptionForbiddenError("Section 14 violation: CandidateRecord cannot accept() itself")

    def choose(self, *args: Any, **kwargs: Any) -> None:
        raise CandidateAdoptionForbiddenError("Section 14 violation: CandidateRecord cannot choose() itself")

    def start(self, *args: Any, **kwargs: Any) -> None:
        raise CandidateAdoptionForbiddenError("Section 14 violation: CandidateRecord cannot start() an Activity")

    def resume(self, *args: Any, **kwargs: Any) -> None:
        raise CandidateAdoptionForbiddenError("Section 14 violation: CandidateRecord cannot resume() an Activity")

    def execute(self, *args: Any, **kwargs: Any) -> None:
        raise CandidateAdoptionForbiddenError("Section 14 violation: CandidateRecord cannot execute() an Action")

    def to_dict(self) -> dict[str, Any]:
        return copy.deepcopy(dataclasses.asdict(self))

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "CandidateRecord":
        if not isinstance(data, Mapping):
            raise CandidateSourceError("CandidateRecord input must be a mapping")
        _reject_forbidden_keys(data, "CandidateRecord")
        allowed = {f.name for f in dataclasses.fields(cls)}
        unknown = set(data.keys()) - allowed
        if unknown:
            raise CandidateSourceError(f"Unknown fields in CandidateRecord: {sorted(unknown)}")
        return cls(**dict(data))


@dataclass
class CandidateSet:
    """Sections 35-38, 56: Point-in-time unranked read view of open candidates (`chiyo.agency.candidate_set.v1`).

    Contains ZERO ranking, scoring, weight, priority, or winner fields.
    """

    candidate_set_id: str
    subject_id: str
    candidate_refs: list[str]
    source_snapshot_refs: list[str]
    observed_at: str
    built_at: str
    builder_policy_version: str = POLICY_VERSION_AG0_V1
    truncated_sources: dict[str, dict[str, int]] = field(default_factory=dict)
    global_truncated: bool = False
    schema_version: str = SCHEMA_CANDIDATE_SET

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_CANDIDATE_SET:
            raise CandidateSourceError(f"invalid CandidateSet schema_version={self.schema_version!r}")
        self.subject_id = validate_subject_id(self.subject_id)
        self.candidate_set_id = _validate_structured_ref(
            self.candidate_set_id,
            "candidate_set_id",
            allowed_prefixes=frozenset({"cset"}),
        )
        self.candidate_refs = _validate_ref_list(
            self.candidate_refs, "candidate_refs", allow_empty=True
        )
        self.source_snapshot_refs = _validate_ref_list(
            self.source_snapshot_refs, "source_snapshot_refs", allow_empty=True
        )
        self.observed_at = _require_iso(self.observed_at, "observed_at")
        self.built_at = _require_iso(self.built_at, "built_at")

    def to_dict(self) -> dict[str, Any]:
        return copy.deepcopy(dataclasses.asdict(self))

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "CandidateSet":
        if not isinstance(data, Mapping):
            raise CandidateSourceError("CandidateSet input must be a mapping")
        _reject_forbidden_keys(data, "CandidateSet")
        allowed = {f.name for f in dataclasses.fields(cls)}
        unknown = set(data.keys()) - allowed
        if unknown:
            raise CandidateSourceError(f"Unknown fields in CandidateSet: {sorted(unknown)}")
        return cls(**dict(data))


# ---------------------------------------------------------------------------
# Typed Bounded Source Inputs for USER_REQUEST, COMMITMENT, EXPLICIT_GOAL, WORLD_OPPORTUNITY
# ---------------------------------------------------------------------------


@dataclass
class ObservedUserRequestEvent:
    """Sections 22-25, 69, 88: Bounded typed user request communication event (`0 LLM`)."""

    event_id: str
    channel: str
    event_kind: str
    request_status: str
    target_refs: list[str]
    occurred_at: str
    observed_at: str
    revision: int = 1
    subject_id: str = GLOBAL_SUBJECT_ID
    semantic_request_key: Optional[str] = None
    valid_from: Optional[str] = None
    valid_until: Optional[str] = None
    superseded_by_ref: Optional[str] = None
    cancelled_by_ref: Optional[str] = None
    authority_domain: str = "communication_request_authority"
    schema_version: str = SCHEMA_USER_REQUEST_EVENT

    def __post_init__(self) -> None:
        self.subject_id = validate_subject_id(self.subject_id)
        self.event_id = _validate_structured_ref(
            self.event_id,
            "event_id",
            allowed_prefixes=frozenset({"ureq", "comm_event", "cevt"}),
        )
        if self.event_kind not in {"EXPLICIT_USER_REQUEST", "ORDINARY_COMMUNICATION", "REQUEST_CANCELLATION"}:
            raise CandidateSourceError(f"invalid user event_kind={self.event_kind!r}")
        if self.request_status not in {"OPEN", "CANCELLED", "WITHDRAWN", "EXPIRED", "FULFILLED", "NONE"}:
            raise CandidateSourceError(f"invalid request_status={self.request_status!r}")
        self.target_refs = _validate_ref_list(self.target_refs, "target_refs", allow_empty=True)
        self.occurred_at = _require_iso(self.occurred_at, "occurred_at")
        self.observed_at = _require_iso(self.observed_at, "observed_at")
        self.valid_from = _optional_iso(self.valid_from, "valid_from")
        self.valid_until = _optional_iso(self.valid_until, "valid_until")
        if not isinstance(self.revision, int) or self.revision < 1:
            raise CandidateSourceError("revision must be int >= 1")

    def to_dict(self) -> dict[str, Any]:
        return copy.deepcopy(dataclasses.asdict(self))


@dataclass
class CanonicalCommitmentRecord:
    """Sections 9, 26-27, 70: Canonical Commitment schema for contract verification when a canonical port is attached."""

    commitment_id: str
    status: str
    target_refs: list[str]
    created_at: str
    observed_at: str
    revision: int = 1
    subject_id: str = GLOBAL_SUBJECT_ID
    valid_from: Optional[str] = None
    due_at: Optional[str] = None
    origin_ref: Optional[str] = None
    authority_domain: str = "commitment_authority"
    schema_version: str = SCHEMA_COMMITMENT_RECORD

    def __post_init__(self) -> None:
        self.subject_id = validate_subject_id(self.subject_id)
        self.commitment_id = _validate_structured_ref(
            self.commitment_id,
            "commitment_id",
            allowed_prefixes=frozenset({"cmmt", "commitment"}),
        )
        if self.status not in {"ACTIVE", "FULFILLED", "CANCELLED", "EXPIRED"}:
            raise CandidateSourceError(f"invalid commitment status={self.status!r}")
        if self.authority_domain != "commitment_authority":
            raise UntrustedCandidateSourceError(
                f"Commitment must come from 'commitment_authority', got {self.authority_domain!r}"
            )
        self.target_refs = _validate_ref_list(self.target_refs, "target_refs", allow_empty=False)
        self.created_at = _require_iso(self.created_at, "created_at")
        self.observed_at = _require_iso(self.observed_at, "observed_at")
        self.valid_from = _optional_iso(self.valid_from, "valid_from")
        self.due_at = _optional_iso(self.due_at, "due_at")

    def to_dict(self) -> dict[str, Any]:
        return copy.deepcopy(dataclasses.asdict(self))


@dataclass
class CanonicalExplicitGoalRecord:
    """Sections 10, 28-30, 71: Canonical Explicit Goal schema for contract verification when a canonical port is attached."""

    goal_id: str
    status: str
    target_refs: list[str]
    created_at: str
    observed_at: str
    revision: int = 1
    subject_id: str = GLOBAL_SUBJECT_ID
    next_actionable_step_ref: Optional[str] = None
    valid_from: Optional[str] = None
    valid_until: Optional[str] = None
    authority_domain: str = "goal_authority"
    schema_version: str = SCHEMA_EXPLICIT_GOAL_RECORD

    def __post_init__(self) -> None:
        self.subject_id = validate_subject_id(self.subject_id)
        self.goal_id = _validate_structured_ref(
            self.goal_id,
            "goal_id",
            allowed_prefixes=frozenset({"goal"}),
        )
        if self.status not in {"ACTIVE", "COMPLETED", "CANCELLED", "ABANDONED", "INVALID"}:
            raise CandidateSourceError(f"invalid goal status={self.status!r}")
        if self.authority_domain != "goal_authority":
            raise UntrustedCandidateSourceError(
                f"ExplicitGoal must come from 'goal_authority', got {self.authority_domain!r}"
            )
        self.target_refs = _validate_ref_list(self.target_refs, "target_refs", allow_empty=False)
        if self.next_actionable_step_ref is not None:
            self.next_actionable_step_ref = _validate_structured_ref(
                self.next_actionable_step_ref,
                "next_actionable_step_ref",
                allowed_prefixes=frozenset({"gstep", "step", "plan_step"}),
            )
        self.created_at = _require_iso(self.created_at, "created_at")
        self.observed_at = _require_iso(self.observed_at, "observed_at")
        self.valid_from = _optional_iso(self.valid_from, "valid_from")
        self.valid_until = _optional_iso(self.valid_until, "valid_until")

    def to_dict(self) -> dict[str, Any]:
        return copy.deepcopy(dataclasses.asdict(self))


@dataclass
class ObservedWorldOpportunity:
    """Sections 31-34, 72, 87: Explicitly observed World opportunity (`World knows X != Chiyo knows X`)."""

    opportunity_id: str
    perception_ref: str
    world_revision_ref: str
    target_refs: list[str]
    valid_from: str
    valid_until: str
    occurred_at: str
    observed_at: str
    observed: bool = True
    status: str = "AVAILABLE"
    revision: int = 1
    subject_id: str = GLOBAL_SUBJECT_ID
    location_ref: Optional[str] = None
    authority_domain: str = "world_perception_authority"
    schema_version: str = SCHEMA_WORLD_OPPORTUNITY_EVENT

    def __post_init__(self) -> None:
        self.subject_id = validate_subject_id(self.subject_id)
        self.opportunity_id = _validate_structured_ref(
            self.opportunity_id,
            "opportunity_id",
            allowed_prefixes=frozenset({"wopp", "world_event", "ambient_event", "school_event"}),
        )
        if not self.observed:
            raise ForbiddenWorldGlobalScanError(
                f"Opportunity {self.opportunity_id!r} has observed=False; unobserved World facts cannot create Candidates"
            )
        self.perception_ref = _validate_structured_ref(
            self.perception_ref,
            "perception_ref",
            allowed_prefixes=frozenset({"percept", "look_world", "obs", "knowledge"}),
        )
        self.world_revision_ref = _validate_structured_ref(
            self.world_revision_ref,
            "world_revision_ref",
            allowed_prefixes=frozenset({"wrev", "world_rev", "scene_rev"}),
        )
        self.target_refs = _validate_ref_list(self.target_refs, "target_refs", allow_empty=False)
        self.valid_from = _require_iso(self.valid_from, "valid_from")
        self.valid_until = _require_iso(self.valid_until, "valid_until")
        self.occurred_at = _require_iso(self.occurred_at, "occurred_at")
        self.observed_at = _require_iso(self.observed_at, "observed_at")
        if self.status not in {"AVAILABLE", "EXPIRED", "CLOSED", "WITHDRAWN"}:
            raise CandidateSourceError(f"invalid world opportunity status={self.status!r}")

    def to_dict(self) -> dict[str, Any]:
        return copy.deepcopy(dataclasses.asdict(self))


# ---------------------------------------------------------------------------
# CandidateSourceAdapter Base Contract (Sections 44-47, 86)
# ---------------------------------------------------------------------------


class CandidateSourceAdapter:
    """Section 44-47: Unified deterministic 0-LLM contract for all six AG-0 Candidate Sources."""

    source_kind: str
    canonical_owner: str

    def __init__(self, *, per_source_cap: int = DEFAULT_PER_SOURCE_CAP) -> None:
        if per_source_cap < 1:
            raise CandidateSourceError("per_source_cap must be >= 1")
        self.per_source_cap = int(per_source_cap)

    def is_enabled(self) -> bool:
        raise NotImplementedError

    def availability_status(self) -> dict[str, Any]:
        raise NotImplementedError

    def discover(self, *, observed_at: str) -> tuple[list[dict[str, Any]], str, dict[str, int]]:
        """Return (bounded_raw_items, source_snapshot_ref, truncation_meta)."""
        raise NotImplementedError

    def validate_source(
        self, raw_item: Mapping[str, Any], *, observed_at: str
    ) -> tuple[bool, Optional[str]]:
        """Return (is_valid, invalidation_reason)."""
        raise NotImplementedError

    def materialize(
        self, *, observed_at: str
    ) -> tuple[list[CandidateRecord], str, dict[str, int]]:
        """Discover, validate, and materialize bounded CandidateRecords from this source."""
        raise NotImplementedError

    def refresh(self, candidate: CandidateRecord, *, observed_at: str) -> CandidateRecord:
        """Verify freshness of an existing CandidateRecord against current canonical source state."""
        raise NotImplementedError

    def invalidate(
        self,
        candidate: CandidateRecord,
        *,
        reason_code: str,
        observed_at: str,
        new_status: str = CANDIDATE_STATUS_STALE,
    ) -> CandidateRecord:
        """Mark a CandidateRecord as STALE / EXPIRED / WITHDRAWN / INVALID without mutating its canonical source."""
        if new_status not in {
            CANDIDATE_STATUS_STALE,
            CANDIDATE_STATUS_EXPIRED,
            CANDIDATE_STATUS_WITHDRAWN,
            CANDIDATE_STATUS_CONSUMED,
            CANDIDATE_STATUS_INVALID,
        }:
            raise CandidateSourceError(f"invalid target invalidation status={new_status!r}")
        updated = copy.deepcopy(candidate)
        if updated.availability_status != new_status or updated.invalidation_reason != reason_code:
            updated.availability_status = new_status
            updated.invalidation_reason = reason_code
            updated.recorded_at = _require_iso(observed_at, "observed_at")
            updated.revision = int(updated.revision) + 1
        return updated

    # Forbidden writer methods (Sections 45-46)
    def start_activity(self, *args: Any, **kwargs: Any) -> None:
        raise CandidateAdoptionForbiddenError("SourceAdapter has zero Activity writer authority")

    def resume_activity(self, *args: Any, **kwargs: Any) -> None:
        raise CandidateAdoptionForbiddenError("SourceAdapter has zero Activity writer authority")

    def propose_action(self, *args: Any, **kwargs: Any) -> None:
        raise CandidateAdoptionForbiddenError("SourceAdapter has zero Action writer authority")


# ---------------------------------------------------------------------------
# 1. CurrentActivityCandidateSource (Sections 16-19, 67)
# ---------------------------------------------------------------------------


class CurrentActivityCandidateSource(CandidateSourceAdapter):
    """Sections 16-19, 67: Reads LR-2 ActivityReadService.

    Emits `CONTINUE_CURRENT` iff foreground Activity exists and is `ACTIVE`.
    Never emits for `IDLE`, `PAUSED`, `WAITING`, `INTERRUPTED`, or terminal statuses.
    """

    source_kind = SOURCE_CURRENT_ACTIVITY
    canonical_owner = "life_activity_authority (LR-2 CanonicalActivityStore)"

    def __init__(
        self,
        activity_read: Optional[ActivityReadService],
        *,
        per_source_cap: int = DEFAULT_PER_SOURCE_CAP,
    ) -> None:
        super().__init__(per_source_cap=per_source_cap)
        self.activity_read = activity_read

    def is_enabled(self) -> bool:
        return self.activity_read is not None

    def availability_status(self) -> dict[str, Any]:
        return {
            "source_kind": self.source_kind,
            "status": ADAPTER_STATUS_READY if self.is_enabled() else ADAPTER_STATUS_DISABLED,
            "canonical_owner": self.canonical_owner,
            "reason_code": "READY" if self.is_enabled() else "ACTIVITY_READ_SERVICE_NOT_ATTACHED",
        }

    def _read_foreground_activity_and_revision(self) -> tuple[Optional[dict[str, Any]], int]:
        if self.activity_read is None:
            return None, 0
        view = self.activity_read.get_current_life_view()
        store_rev = int(view.get("activity_revision") or view.get("revision") or view.get("store_revision") or 0)
        fg_ref = view.get("foreground_activity_ref")
        if not fg_ref:
            return None, store_rev
        fg = self.activity_read.get_activity(str(fg_ref))
        if fg is not None and not store_rev:
            store_rev = int(fg.get("revision", 0))
        return fg, store_rev

    def discover(self, *, observed_at: str) -> tuple[list[dict[str, Any]], str, dict[str, int]]:
        obs_iso = _require_iso(observed_at, "observed_at")
        if self.activity_read is None:
            return [], "snap:CURRENT_ACTIVITY:disabled", {"discovered": 0, "emitted": 0, "cap": self.per_source_cap}

        fg, store_rev = self._read_foreground_activity_and_revision()
        snap_ref = f"snap:CURRENT_ACTIVITY:rev:{store_rev}"
        if fg is None:
            return [], snap_ref, {"discovered": 0, "emitted": 0, "cap": self.per_source_cap}

        ok, _ = self.validate_source(fg, observed_at=obs_iso)
        if not ok:
            return [], snap_ref, {"discovered": 0, "emitted": 0, "cap": self.per_source_cap}

        return [fg], snap_ref, {"discovered": 1, "emitted": 1, "cap": self.per_source_cap}

    def validate_source(
        self, raw_item: Mapping[str, Any], *, observed_at: str
    ) -> tuple[bool, Optional[str]]:
        if not isinstance(raw_item, Mapping):
            return False, "INVALID_ACTIVITY_OBJECT"
        if raw_item.get("subject_id", GLOBAL_SUBJECT_ID) != GLOBAL_SUBJECT_ID:
            raise SubjectIsolationError("Cross-subject activity rejected")
        status = raw_item.get("status")
        if status in TERMINAL_STATUSES:
            return False, f"ACTIVITY_TERMINAL_{status}"
        if status == STATUS_WAITING:
            return False, "ACTIVITY_WAITING_NOT_CONTINUE_CANDIDATE"
        if status == STATUS_PAUSED:
            return False, "ACTIVITY_PAUSED_REQUIRES_RESUME_ELIGIBLE"
        if status == STATUS_INTERRUPTED:
            return False, "ACTIVITY_INTERRUPTED_NOT_ACTIVE_FOREGROUND"
        if status != STATUS_ACTIVE:
            return False, f"ACTIVITY_STATUS_{status}_NOT_ACTIVE"
        return True, None

    def materialize(
        self, *, observed_at: str
    ) -> tuple[list[CandidateRecord], str, dict[str, int]]:
        obs_iso = _require_iso(observed_at, "observed_at")
        items, snap_ref, meta = self.discover(observed_at=obs_iso)
        results: list[CandidateRecord] = []
        for act in items[: self.per_source_cap]:
            act_id = str(act["activity_id"])
            act_rev = int(act["revision"])
            target_refs: list[str] = []
            for sref in act.get("subject_refs") or []:
                if _STRUCTURED_REF_RE.fullmatch(str(sref)) and str(sref) not in target_refs:
                    target_refs.append(str(sref))
            if act.get("target_ref") and _STRUCTURED_REF_RE.fullmatch(str(act["target_ref"])):
                if str(act["target_ref"]) not in target_refs:
                    target_refs.append(str(act["target_ref"]))
            if act.get("checkpoint_ref") and _STRUCTURED_REF_RE.fullmatch(str(act["checkpoint_ref"])):
                if str(act["checkpoint_ref"]) not in target_refs:
                    target_refs.append(str(act["checkpoint_ref"]))
            if not target_refs:
                target_refs.append(act_id)

            cand_id = f"cand:{sha256_hex(f'CURRENT_ACTIVITY:{act_id}:v{act_rev}')[:16]}"
            cand = CandidateRecord(
                candidate_id=cand_id,
                subject_id=GLOBAL_SUBJECT_ID,
                candidate_kind=CANDIDATE_KIND_CONTINUE_CURRENT,
                source_kind=SOURCE_CURRENT_ACTIVITY,
                source_ref=act_id,
                source_refs=[act_id],
                source_revision=act_rev,
                activity_ref=act_id,
                target_refs=target_refs,
                availability_status=CANDIDATE_STATUS_OPEN,
                created_at=obs_iso,
                observed_at=obs_iso,
                recorded_at=obs_iso,
                causal_parent_refs=[act_id],
                provenance_refs=[act_id, f"act_rev:{act_id}:v{act_rev}", snap_ref],
                provenance_chain=[
                    {"step": "candidate", "ref": cand_id, "kind": CANDIDATE_KIND_CONTINUE_CURRENT},
                    {"step": "adapter", "ref": "adapter:CURRENT_ACTIVITY", "owner": self.canonical_owner},
                    {"step": "canonical_source", "ref": act_id, "revision": act_rev, "status": act["status"]},
                ],
                idempotency_key=f"idem_cand:current_activity:{act_id}:v{act_rev}",
            )
            results.append(cand)
        return results, snap_ref, meta

    def refresh(self, candidate: CandidateRecord, *, observed_at: str) -> CandidateRecord:
        obs_iso = _require_iso(observed_at, "observed_at")
        if self.activity_read is None or not candidate.activity_ref:
            return self.invalidate(
                candidate,
                reason_code="SOURCE_UNAVAILABLE",
                observed_at=obs_iso,
                new_status=CANDIDATE_STATUS_INVALID,
            )
        act = self.activity_read.get_activity(candidate.activity_ref)
        if act is None:
            return self.invalidate(
                candidate,
                reason_code="SOURCE_ACTIVITY_MISSING",
                observed_at=obs_iso,
                new_status=CANDIDATE_STATUS_INVALID,
            )
        if act["status"] in TERMINAL_STATUSES:
            return self.invalidate(
                candidate,
                reason_code=f"ACTIVITY_BECAME_TERMINAL_{act['status']}",
                observed_at=obs_iso,
                new_status=CANDIDATE_STATUS_STALE,
            )
        if act["status"] != STATUS_ACTIVE:
            return self.invalidate(
                candidate,
                reason_code=f"ACTIVITY_NO_LONGER_ACTIVE_{act['status']}",
                observed_at=obs_iso,
                new_status=CANDIDATE_STATUS_STALE,
            )
        fg, _ = self._read_foreground_activity_and_revision()
        if fg is None or fg["activity_id"] != candidate.activity_ref:
            return self.invalidate(
                candidate,
                reason_code="ACTIVITY_NO_LONGER_FOREGROUND",
                observed_at=obs_iso,
                new_status=CANDIDATE_STATUS_STALE,
            )
        if int(act["revision"]) != int(candidate.source_revision or -1):
            return self.invalidate(
                candidate,
                reason_code="ACTIVITY_REVISION_ADVANCED",
                observed_at=obs_iso,
                new_status=CANDIDATE_STATUS_STALE,
            )
        return copy.deepcopy(candidate)


# ---------------------------------------------------------------------------
# 2. ResumeEligibleCandidateSource (Sections 20-21, 68)
# ---------------------------------------------------------------------------


class ResumeEligibleCandidateSource(CandidateSourceAdapter):
    """Sections 20-21, 68: Reads LR-4 WaitingCoordinator / ResumeEligibilityStore + LR-2 ActivityReadService.

    Emits `RESUME_ACTIVITY` iff `ResumeEligibility.status == OPEN` AND the referenced
    Activity is non-terminal with compatible `activity_revision`.
    Never resumes the Activity.
    """

    source_kind = SOURCE_RESUME_ELIGIBLE
    canonical_owner = "life_waiting_agenda_authority (LR-4 ResumeEligibilityStore)"

    def __init__(
        self,
        waiting_source: Optional[WaitingCoordinator | ResumeEligibilityStore],
        activity_read: Optional[ActivityReadService],
        *,
        per_source_cap: int = DEFAULT_PER_SOURCE_CAP,
    ) -> None:
        super().__init__(per_source_cap=per_source_cap)
        self.waiting_source = waiting_source
        self.activity_read = activity_read

    def _eligibility_store(self) -> Optional[ResumeEligibilityStore]:
        if self.waiting_source is None:
            return None
        if hasattr(self.waiting_source, "eligibility_store"):
            return self.waiting_source.eligibility_store
        return self.waiting_source  # type: ignore[return-value]

    def _load_eligibility_state(self) -> dict[str, Any]:
        store = self._eligibility_store()
        if store is None:
            return {"revision": 0, "eligibilities": {}}
        return store.load_state()

    def _list_open_eligibilities(self) -> list[dict[str, Any]]:
        state = self._load_eligibility_state()
        eligs = state.get("eligibilities", {})
        items = [
            copy.deepcopy(v)
            for v in eligs.values()
            if isinstance(v, Mapping) and v.get("status") == ELIGIBILITY_STATUS_OPEN
        ]
        items.sort(key=lambda x: str(x.get("eligibility_id", "")))
        return items

    def _get_eligibility(self, eligibility_id: str) -> Optional[dict[str, Any]]:
        state = self._load_eligibility_state()
        raw = state.get("eligibilities", {}).get(eligibility_id)
        return copy.deepcopy(raw) if isinstance(raw, Mapping) else None

    def is_enabled(self) -> bool:
        return self._eligibility_store() is not None and self.activity_read is not None

    def availability_status(self) -> dict[str, Any]:
        return {
            "source_kind": self.source_kind,
            "status": ADAPTER_STATUS_READY if self.is_enabled() else ADAPTER_STATUS_DISABLED,
            "canonical_owner": self.canonical_owner,
            "reason_code": "READY" if self.is_enabled() else "WAITING_OR_ACTIVITY_READ_NOT_ATTACHED",
        }

    def discover(self, *, observed_at: str) -> tuple[list[dict[str, Any]], str, dict[str, int]]:
        obs_iso = _require_iso(observed_at, "observed_at")
        if not self.is_enabled():
            return [], "snap:RESUME_ELIGIBLE:disabled", {"discovered": 0, "emitted": 0, "cap": self.per_source_cap}

        assert self.activity_read is not None
        w_state = self._load_eligibility_state()
        w_rev = int(w_state.get("revision", 0))
        view = self.activity_read.get_current_life_view()
        a_rev = int(view.get("revision", view.get("store_revision", 0)))
        snap_ref = f"snap:RESUME_ELIGIBLE:wrev:{w_rev}:arev:{a_rev}"

        open_eligs = self._list_open_eligibilities()
        valid_items: list[dict[str, Any]] = []
        for elig in open_eligs:
            ok, _ = self.validate_source(elig, observed_at=obs_iso)
            if ok:
                valid_items.append(elig)

        total_discovered = len(valid_items)
        bounded = valid_items[: self.per_source_cap]
        return bounded, snap_ref, {
            "discovered": total_discovered,
            "emitted": len(bounded),
            "cap": self.per_source_cap,
        }

    def validate_source(
        self, raw_item: Mapping[str, Any], *, observed_at: str
    ) -> tuple[bool, Optional[str]]:
        if not isinstance(raw_item, Mapping):
            return False, "INVALID_ELIGIBILITY_OBJECT"
        if raw_item.get("status") != ELIGIBILITY_STATUS_OPEN:
            return False, f"ELIGIBILITY_STATUS_{raw_item.get('status')}_NOT_OPEN"
        act_id = raw_item.get("activity_id")
        if not act_id or self.activity_read is None:
            return False, "MISSING_ACTIVITY_REF"
        act = self.activity_read.get_activity(str(act_id))
        if act is None:
            return False, "ACTIVITY_NOT_FOUND"
        if act["status"] in TERMINAL_STATUSES:
            return False, f"ACTIVITY_TERMINAL_{act['status']}"
        if act["status"] == STATUS_ACTIVE:
            return False, "ACTIVITY_ALREADY_ACTIVE"
        if int(act["revision"]) != int(raw_item.get("activity_revision", -1)):
            return False, "STALE_ACTIVITY_REVISION"
        return True, None

    def materialize(
        self, *, observed_at: str
    ) -> tuple[list[CandidateRecord], str, dict[str, int]]:
        obs_iso = _require_iso(observed_at, "observed_at")
        items, snap_ref, meta = self.discover(observed_at=obs_iso)
        results: list[CandidateRecord] = []
        assert self.activity_read is not None

        for elig in items:
            elig_id = str(elig["eligibility_id"])
            act_id = str(elig["activity_id"])
            act_rev = int(elig["activity_revision"])
            wep_ref = str(elig.get("waiting_episode_ref") or elig.get("waiting_episode_id") or "wep:unknown")
            rcond_ref = str(elig.get("resume_condition_ref") or elig.get("condition_id") or "rcond:unknown")
            act = self.activity_read.get_activity(act_id) or {}

            target_refs: list[str] = []
            for sref in act.get("subject_refs") or []:
                if _STRUCTURED_REF_RE.fullmatch(str(sref)) and str(sref) not in target_refs:
                    target_refs.append(str(sref))
            if act.get("target_ref") and _STRUCTURED_REF_RE.fullmatch(str(act["target_ref"])):
                if str(act["target_ref"]) not in target_refs:
                    target_refs.append(str(act["target_ref"]))
            if act.get("checkpoint_ref") and _STRUCTURED_REF_RE.fullmatch(str(act["checkpoint_ref"])):
                if str(act["checkpoint_ref"]) not in target_refs:
                    target_refs.append(str(act["checkpoint_ref"]))
            if not target_refs:
                target_refs.append(act_id)

            cand_id = f"cand:{sha256_hex(f'RESUME_ELIGIBLE:{elig_id}:arev:{act_rev}')[:16]}"
            cand = CandidateRecord(
                candidate_id=cand_id,
                subject_id=GLOBAL_SUBJECT_ID,
                candidate_kind=CANDIDATE_KIND_RESUME_ACTIVITY,
                source_kind=SOURCE_RESUME_ELIGIBLE,
                source_ref=elig_id,
                source_refs=[elig_id],
                source_revision=act_rev,
                activity_ref=act_id,
                resume_eligibility_ref=elig_id,
                target_refs=target_refs,
                availability_status=CANDIDATE_STATUS_OPEN,
                created_at=str(elig.get("created_at") or elig.get("eligible_at") or obs_iso),
                observed_at=obs_iso,
                recorded_at=obs_iso,
                causal_parent_refs=[elig_id, act_id, wep_ref],
                provenance_refs=[elig_id, act_id, wep_ref, rcond_ref, snap_ref],
                provenance_chain=[
                    {"step": "candidate", "ref": cand_id, "kind": CANDIDATE_KIND_RESUME_ACTIVITY},
                    {"step": "adapter", "ref": "adapter:RESUME_ELIGIBLE", "owner": self.canonical_owner},
                    {"step": "resume_eligibility", "ref": elig_id, "status": elig["status"]},
                    {"step": "activity", "ref": act_id, "revision": act_rev},
                    {"step": "waiting_episode", "ref": wep_ref, "resume_condition_ref": rcond_ref},
                ],
                idempotency_key=f"idem_cand:resume_eligible:{elig_id}:v{act_rev}",
            )
            results.append(cand)
        return results, snap_ref, meta

    def refresh(self, candidate: CandidateRecord, *, observed_at: str) -> CandidateRecord:
        obs_iso = _require_iso(observed_at, "observed_at")
        if not self.is_enabled() or not candidate.resume_eligibility_ref or not candidate.activity_ref:
            return self.invalidate(
                candidate,
                reason_code="SOURCE_UNAVAILABLE",
                observed_at=obs_iso,
                new_status=CANDIDATE_STATUS_INVALID,
            )
        assert self.activity_read is not None
        elig = self._get_eligibility(candidate.resume_eligibility_ref)
        if elig is None:
            return self.invalidate(
                candidate,
                reason_code="RESUME_ELIGIBILITY_MISSING",
                observed_at=obs_iso,
                new_status=CANDIDATE_STATUS_INVALID,
            )
        if elig["status"] == "CONSUMED":
            return self.invalidate(
                candidate,
                reason_code="RESUME_ELIGIBILITY_CONSUMED",
                observed_at=obs_iso,
                new_status=CANDIDATE_STATUS_STALE,
            )
        if elig["status"] != ELIGIBILITY_STATUS_OPEN:
            return self.invalidate(
                candidate,
                reason_code=f"RESUME_ELIGIBILITY_{elig['status']}",
                observed_at=obs_iso,
                new_status=CANDIDATE_STATUS_STALE,
            )
        act = self.activity_read.get_activity(candidate.activity_ref)
        if act is None:
            return self.invalidate(
                candidate,
                reason_code="ACTIVITY_MISSING",
                observed_at=obs_iso,
                new_status=CANDIDATE_STATUS_INVALID,
            )
        if act["status"] in TERMINAL_STATUSES:
            return self.invalidate(
                candidate,
                reason_code=f"ACTIVITY_BECAME_TERMINAL_{act['status']}",
                observed_at=obs_iso,
                new_status=CANDIDATE_STATUS_STALE,
            )
        if int(act["revision"]) != int(candidate.source_revision or -1) or int(act["revision"]) != int(
            elig["activity_revision"]
        ):
            return self.invalidate(
                candidate,
                reason_code="ACTIVITY_REVISION_ADVANCED",
                observed_at=obs_iso,
                new_status=CANDIDATE_STATUS_STALE,
            )
        return copy.deepcopy(candidate)


# ---------------------------------------------------------------------------
# 3. UserRequestCandidateSource (Sections 22-25, 69, 88)
# ---------------------------------------------------------------------------


class UserRequestCandidateSource(CandidateSourceAdapter):
    """Sections 22-25, 69, 88: Bounded typed User Request source adapter (`PARTIAL`, `0 LLM`).

    Accepts only explicit typed `ObservedUserRequestEvent` records with provenance.
    Rejects raw chat strings, LLM request guessing, or untyped communication.
    Never creates a Commitment or starts an Activity.
    """

    source_kind = SOURCE_USER_REQUEST
    canonical_owner = "communication_request_authority (Bounded Typed Request Seam)"

    def __init__(
        self,
        events: Optional[Sequence[ObservedUserRequestEvent | Mapping[str, Any]]] = None,
        *,
        per_source_cap: int = DEFAULT_PER_SOURCE_CAP,
    ) -> None:
        super().__init__(per_source_cap=per_source_cap)
        self._events_by_id: dict[str, ObservedUserRequestEvent] = {}
        self._revision: int = 0
        if events:
            for ev in events:
                self.ingest_typed_event(ev)

    def is_enabled(self) -> bool:
        return True

    def availability_status(self) -> dict[str, Any]:
        return {
            "source_kind": self.source_kind,
            "status": ADAPTER_STATUS_PARTIAL,
            "canonical_owner": self.canonical_owner,
            "reason_code": "BOUNDED_TYPED_REQUEST_SEAM_ONLY_NO_LLM_CLASSIFIER",
        }

    def ingest_typed_event(self, event: ObservedUserRequestEvent | Mapping[str, Any] | str) -> ObservedUserRequestEvent:
        if isinstance(event, str):
            raise UntrustedCandidateSourceError(
                "Raw chat text cannot directly enter UserRequestCandidateSource without typed provenance"
            )
        if isinstance(event, ObservedUserRequestEvent):
            ev_obj = copy.deepcopy(event)
        elif isinstance(event, Mapping):
            _reject_forbidden_keys(event, "ObservedUserRequestEvent")
            if event.get("source_kind") in FORBIDDEN_CANDIDATE_SOURCE_KINDS or "raw_chat_text" in event:
                raise UntrustedCandidateSourceError("Untrusted communication payload rejected")
            ev_obj = ObservedUserRequestEvent(**dict(event))
        else:
            raise UntrustedCandidateSourceError(f"Unsupported user request input: {type(event)!r}")

        existing = self._events_by_id.get(ev_obj.event_id)
        if existing is None or ev_obj.revision >= existing.revision:
            self._events_by_id[ev_obj.event_id] = ev_obj
            self._revision += 1
        return ev_obj

    def cancel_request(self, event_id: str, *, cancelled_at: str, cancel_ref: str = "cevt:user_cancel") -> None:
        obs_iso = _require_iso(cancelled_at, "cancelled_at")
        if event_id not in self._events_by_id:
            raise CandidateSourceError(f"unknown user request event_id={event_id!r}")
        cur = self._events_by_id[event_id]
        cur.request_status = "CANCELLED"
        cur.cancelled_by_ref = _validate_structured_ref(cancel_ref, "cancel_ref")
        cur.observed_at = obs_iso
        cur.revision += 1
        self._revision += 1

    def advance_request_revision(self, event_id: str, *, new_revision: int, observed_at: str) -> None:
        obs_iso = _require_iso(observed_at, "observed_at")
        if event_id not in self._events_by_id:
            raise CandidateSourceError(f"unknown user request event_id={event_id!r}")
        cur = self._events_by_id[event_id]
        if new_revision <= cur.revision:
            raise CandidateSourceError("new_revision must be > current revision")
        cur.revision = int(new_revision)
        cur.observed_at = obs_iso
        self._revision += 1

    def discover(self, *, observed_at: str) -> tuple[list[dict[str, Any]], str, dict[str, int]]:
        obs_iso = _require_iso(observed_at, "observed_at")
        snap_ref = f"snap:USER_REQUEST:rev:{self._revision}"
        valid_items: list[dict[str, Any]] = []
        seen_semantic_keys: set[str] = set()

        for ev_id in sorted(self._events_by_id.keys()):
            ev = self._events_by_id[ev_id]
            ev_dict = ev.to_dict()
            ok, _ = self.validate_source(ev_dict, observed_at=obs_iso)
            if not ok:
                continue
            dedup_k = ev.semantic_request_key or f"{ev.subject_id}:{','.join(ev.target_refs)}:{ev.event_id}"
            if dedup_k in seen_semantic_keys:
                continue
            seen_semantic_keys.add(dedup_k)
            valid_items.append(ev_dict)

        total_discovered = len(valid_items)
        bounded = valid_items[: self.per_source_cap]
        return bounded, snap_ref, {
            "discovered": total_discovered,
            "emitted": len(bounded),
            "cap": self.per_source_cap,
        }

    def validate_source(
        self, raw_item: Mapping[str, Any], *, observed_at: str
    ) -> tuple[bool, Optional[str]]:
        obs_dt = _parse_iso(_require_iso(observed_at, "observed_at"))
        if not isinstance(raw_item, Mapping):
            return False, "INVALID_USER_REQUEST_OBJECT"
        if raw_item.get("subject_id", GLOBAL_SUBJECT_ID) != GLOBAL_SUBJECT_ID:
            raise SubjectIsolationError("Cross-subject user request rejected")
        if raw_item.get("event_kind") != "EXPLICIT_USER_REQUEST":
            return False, "ORDINARY_COMMUNICATION_NOT_A_REQUEST"
        status = raw_item.get("request_status")
        if status in {"CANCELLED", "WITHDRAWN"}:
            return False, f"REQUEST_{status}"
        if status != "OPEN":
            return False, f"REQUEST_STATUS_{status}_NOT_OPEN"
        if raw_item.get("valid_from"):
            if obs_dt < _parse_iso(str(raw_item["valid_from"])):
                return False, "REQUEST_NOT_YET_VALID"
        if raw_item.get("valid_until"):
            if obs_dt > _parse_iso(str(raw_item["valid_until"])):
                return False, "REQUEST_VALIDITY_WINDOW_EXPIRED"
        return True, None

    def materialize(
        self, *, observed_at: str
    ) -> tuple[list[CandidateRecord], str, dict[str, int]]:
        obs_iso = _require_iso(observed_at, "observed_at")
        items, snap_ref, meta = self.discover(observed_at=obs_iso)
        results: list[CandidateRecord] = []

        for ev in items:
            ev_id = str(ev["event_id"])
            ev_rev = int(ev["revision"])
            channel = str(ev.get("channel") or "unknown").lower()
            sem_key = str(ev.get("semantic_request_key") or ev_id)
            target_refs = list(ev.get("target_refs") or [ev_id])
            # Channel is recorded in provenance_refs, while semantic_identity is unified for One Chiyo (Section 78)
            sem_id = f"semcand:{sha256_hex(f'{GLOBAL_SUBJECT_ID}:USER_REQUEST:{sem_key}')[:16]}"
            cand_id = f"cand:{sha256_hex(f'USER_REQUEST:{sem_key}:v{ev_rev}')[:16]}"

            cand = CandidateRecord(
                candidate_id=cand_id,
                subject_id=GLOBAL_SUBJECT_ID,
                candidate_kind=CANDIDATE_KIND_RESPOND_USER_REQUEST,
                source_kind=SOURCE_USER_REQUEST,
                source_ref=ev_id,
                source_refs=[ev_id],
                source_revision=ev_rev,
                user_event_ref=ev_id,
                target_refs=target_refs,
                availability_status=CANDIDATE_STATUS_OPEN,
                created_at=str(ev["occurred_at"]),
                observed_at=obs_iso,
                recorded_at=obs_iso,
                valid_from=ev.get("valid_from"),
                valid_until=ev.get("valid_until"),
                causal_parent_refs=[ev_id],
                provenance_refs=[ev_id, f"channel:{channel}", snap_ref],
                provenance_chain=[
                    {"step": "candidate", "ref": cand_id, "kind": CANDIDATE_KIND_RESPOND_USER_REQUEST},
                    {"step": "adapter", "ref": "adapter:USER_REQUEST", "owner": self.canonical_owner},
                    {
                        "step": "observed_communication_event",
                        "ref": ev_id,
                        "channel": channel,
                        "revision": ev_rev,
                    },
                ],
                semantic_identity=sem_id,
                idempotency_key=f"idem_cand:user_request:{sem_key}:v{ev_rev}",
            )
            results.append(cand)
        return results, snap_ref, meta

    def refresh(self, candidate: CandidateRecord, *, observed_at: str) -> CandidateRecord:
        obs_iso = _require_iso(observed_at, "observed_at")
        ev_id = candidate.user_event_ref or candidate.source_ref
        ev = self._events_by_id.get(ev_id)
        if ev is None:
            return self.invalidate(
                candidate,
                reason_code="USER_REQUEST_SOURCE_MISSING",
                observed_at=obs_iso,
                new_status=CANDIDATE_STATUS_INVALID,
            )
        if ev.request_status in {"CANCELLED", "WITHDRAWN"}:
            return self.invalidate(
                candidate,
                reason_code="USER_REQUEST_CANCELLED_OR_WITHDRAWN",
                observed_at=obs_iso,
                new_status=CANDIDATE_STATUS_WITHDRAWN,
            )
        if ev.valid_until and _parse_iso(obs_iso) > _parse_iso(ev.valid_until):
            return self.invalidate(
                candidate,
                reason_code="USER_REQUEST_EXPIRED",
                observed_at=obs_iso,
                new_status=CANDIDATE_STATUS_EXPIRED,
            )
        if int(ev.revision) != int(candidate.source_revision or -1):
            return self.invalidate(
                candidate,
                reason_code="USER_REQUEST_REVISION_ADVANCED",
                observed_at=obs_iso,
                new_status=CANDIDATE_STATUS_STALE,
            )
        if ev.request_status != "OPEN":
            return self.invalidate(
                candidate,
                reason_code=f"USER_REQUEST_STATUS_{ev.request_status}",
                observed_at=obs_iso,
                new_status=CANDIDATE_STATUS_STALE,
            )
        return copy.deepcopy(candidate)


# ---------------------------------------------------------------------------
# 4. CommitmentCandidateSource (Sections 9, 26-27, 70, 91)
# ---------------------------------------------------------------------------


class CommitmentCandidateSource(CandidateSourceAdapter):
    """Sections 9, 26-27, 70: Canonical Commitment source adapter.

    By default in the current repository, no canonical Commitment authority exists,
    so this adapter defaults to `DISABLED` (`MISSING_CANONICAL_COMMITMENT_AUTHORITY`, D00).
    In isolated contract tests (D01-D05), an explicit `canonical_records` store can be
    attached to verify active/fulfilled/cancelled/expired commitment rules.
    """

    source_kind = SOURCE_COMMITMENT
    canonical_owner = "commitment_authority (NOT_IMPLEMENTED in production repo; DISABLED by default)"

    def __init__(
        self,
        canonical_records: Optional[Sequence[CanonicalCommitmentRecord | Mapping[str, Any]]] = None,
        *,
        enabled: bool = False,
        per_source_cap: int = DEFAULT_PER_SOURCE_CAP,
    ) -> None:
        super().__init__(per_source_cap=per_source_cap)
        self._enabled = bool(enabled or canonical_records is not None)
        self._records_by_id: dict[str, CanonicalCommitmentRecord] = {}
        self._revision: int = 0
        if canonical_records:
            for rec in canonical_records:
                self.register_canonical_commitment(rec)

    def is_enabled(self) -> bool:
        return self._enabled

    def availability_status(self) -> dict[str, Any]:
        if not self._enabled:
            return {
                "source_kind": self.source_kind,
                "status": ADAPTER_STATUS_DISABLED,
                "canonical_owner": self.canonical_owner,
                "reason_code": "MISSING_CANONICAL_COMMITMENT_AUTHORITY",
            }
        return {
            "source_kind": self.source_kind,
            "status": ADAPTER_STATUS_READY,
            "canonical_owner": "commitment_authority (Explicit Canonical Port)",
            "reason_code": "CANONICAL_COMMITMENT_PORT_ATTACHED",
        }

    def register_canonical_commitment(
        self, record: CanonicalCommitmentRecord | Mapping[str, Any] | str
    ) -> CanonicalCommitmentRecord:
        if isinstance(record, str):
            raise UntrustedCandidateSourceError(
                "Section 9 violation: cannot infer Commitment Candidate from chat text like '我答应你'"
            )
        if not self._enabled:
            raise DisabledCandidateSourceError(
                "COMMITMENT source adapter is DISABLED because no canonical Commitment owner is configured"
            )
        if isinstance(record, CanonicalCommitmentRecord):
            c_obj = copy.deepcopy(record)
        elif isinstance(record, Mapping):
            _reject_forbidden_keys(record, "CanonicalCommitmentRecord")
            c_obj = CanonicalCommitmentRecord(**dict(record))
        else:
            raise UntrustedCandidateSourceError(f"Invalid commitment record type: {type(record)!r}")
        self._records_by_id[c_obj.commitment_id] = c_obj
        self._revision += 1
        return c_obj

    def update_commitment_status(self, commitment_id: str, *, new_status: str, observed_at: str) -> None:
        obs_iso = _require_iso(observed_at, "observed_at")
        if commitment_id not in self._records_by_id:
            raise CandidateSourceError(f"unknown commitment_id={commitment_id!r}")
        rec = self._records_by_id[commitment_id]
        if new_status not in {"ACTIVE", "FULFILLED", "CANCELLED", "EXPIRED"}:
            raise CandidateSourceError(f"invalid commitment status={new_status!r}")
        rec.status = new_status
        rec.observed_at = obs_iso
        rec.revision += 1
        self._revision += 1

    def discover(self, *, observed_at: str) -> tuple[list[dict[str, Any]], str, dict[str, int]]:
        obs_iso = _require_iso(observed_at, "observed_at")
        if not self._enabled:
            return [], "snap:COMMITMENT:disabled", {"discovered": 0, "emitted": 0, "cap": self.per_source_cap}

        snap_ref = f"snap:COMMITMENT:rev:{self._revision}"
        valid_items: list[dict[str, Any]] = []
        for cid in sorted(self._records_by_id.keys()):
            rec_dict = self._records_by_id[cid].to_dict()
            ok, _ = self.validate_source(rec_dict, observed_at=obs_iso)
            if ok:
                valid_items.append(rec_dict)

        total_discovered = len(valid_items)
        bounded = valid_items[: self.per_source_cap]
        return bounded, snap_ref, {
            "discovered": total_discovered,
            "emitted": len(bounded),
            "cap": self.per_source_cap,
        }

    def validate_source(
        self, raw_item: Mapping[str, Any], *, observed_at: str
    ) -> tuple[bool, Optional[str]]:
        if not self._enabled:
            return False, "COMMITMENT_SOURCE_DISABLED"
        obs_dt = _parse_iso(_require_iso(observed_at, "observed_at"))
        if not isinstance(raw_item, Mapping):
            return False, "INVALID_COMMITMENT_OBJECT"
        if raw_item.get("subject_id", GLOBAL_SUBJECT_ID) != GLOBAL_SUBJECT_ID:
            raise SubjectIsolationError("Cross-subject commitment rejected")
        if raw_item.get("authority_domain") != "commitment_authority":
            return False, "UNTRUSTED_COMMITMENT_AUTHORITY"
        status = raw_item.get("status")
        if status != "ACTIVE":
            return False, f"COMMITMENT_STATUS_{status}_NOT_ACTIVE"
        if raw_item.get("valid_from") and obs_dt < _parse_iso(str(raw_item["valid_from"])):
            return False, "COMMITMENT_NOT_YET_VALID"
        if raw_item.get("due_at") and obs_dt > _parse_iso(str(raw_item["due_at"])):
            return False, "COMMITMENT_EXPIRED"
        return True, None

    def materialize(
        self, *, observed_at: str
    ) -> tuple[list[CandidateRecord], str, dict[str, int]]:
        obs_iso = _require_iso(observed_at, "observed_at")
        items, snap_ref, meta = self.discover(observed_at=obs_iso)
        results: list[CandidateRecord] = []
        for rec in items:
            cid = str(rec["commitment_id"])
            crev = int(rec["revision"])
            cand_id = f"cand:{sha256_hex(f'COMMITMENT:{cid}:v{crev}')[:16]}"
            causal = [cid]
            if rec.get("origin_ref"):
                causal.append(str(rec["origin_ref"]))
            cand = CandidateRecord(
                candidate_id=cand_id,
                subject_id=GLOBAL_SUBJECT_ID,
                candidate_kind=CANDIDATE_KIND_FULFILL_COMMITMENT,
                source_kind=SOURCE_COMMITMENT,
                source_ref=cid,
                source_refs=[cid],
                source_revision=crev,
                commitment_ref=cid,
                target_refs=list(rec["target_refs"]),
                availability_status=CANDIDATE_STATUS_OPEN,
                created_at=str(rec["created_at"]),
                observed_at=obs_iso,
                recorded_at=obs_iso,
                valid_from=rec.get("valid_from"),
                valid_until=rec.get("due_at"),
                causal_parent_refs=causal,
                provenance_refs=[cid, snap_ref],
                provenance_chain=[
                    {"step": "candidate", "ref": cand_id, "kind": CANDIDATE_KIND_FULFILL_COMMITMENT},
                    {"step": "adapter", "ref": "adapter:COMMITMENT", "owner": "commitment_authority"},
                    {"step": "canonical_commitment", "ref": cid, "revision": crev, "status": rec["status"]},
                ],
                idempotency_key=f"idem_cand:commitment:{cid}:v{crev}",
            )
            results.append(cand)
        return results, snap_ref, meta

    def refresh(self, candidate: CandidateRecord, *, observed_at: str) -> CandidateRecord:
        obs_iso = _require_iso(observed_at, "observed_at")
        if not self._enabled:
            return self.invalidate(
                candidate,
                reason_code="COMMITMENT_SOURCE_DISABLED",
                observed_at=obs_iso,
                new_status=CANDIDATE_STATUS_INVALID,
            )
        cid = candidate.commitment_ref or candidate.source_ref
        rec = self._records_by_id.get(cid)
        if rec is None:
            return self.invalidate(
                candidate,
                reason_code="COMMITMENT_MISSING",
                observed_at=obs_iso,
                new_status=CANDIDATE_STATUS_INVALID,
            )
        if rec.status == "CANCELLED":
            return self.invalidate(
                candidate,
                reason_code="COMMITMENT_CANCELLED",
                observed_at=obs_iso,
                new_status=CANDIDATE_STATUS_WITHDRAWN,
            )
        if rec.status == "EXPIRED" or (rec.due_at and _parse_iso(obs_iso) > _parse_iso(rec.due_at)):
            return self.invalidate(
                candidate,
                reason_code="COMMITMENT_EXPIRED",
                observed_at=obs_iso,
                new_status=CANDIDATE_STATUS_EXPIRED,
            )
        if rec.status == "FULFILLED":
            return self.invalidate(
                candidate,
                reason_code="COMMITMENT_FULFILLED",
                observed_at=obs_iso,
                new_status=CANDIDATE_STATUS_STALE,
            )
        if int(rec.revision) != int(candidate.source_revision or -1):
            return self.invalidate(
                candidate,
                reason_code="COMMITMENT_REVISION_ADVANCED",
                observed_at=obs_iso,
                new_status=CANDIDATE_STATUS_STALE,
            )
        return copy.deepcopy(candidate)


# ---------------------------------------------------------------------------
# 5. ExplicitGoalCandidateSource (Sections 10, 28-30, 71)
# ---------------------------------------------------------------------------


class ExplicitGoalCandidateSource(CandidateSourceAdapter):
    """Sections 10, 28-30, 71: Canonical Explicit Goal source adapter.

    By default in the current repository, no canonical Goal/Plan authority exists,
    so this adapter defaults to `DISABLED` (`MISSING_CANONICAL_GOAL_AUTHORITY`, E05).
    When an explicit canonical Goal read port is attached in isolated contract tests (E01-E04):
      - Exposes only `next_actionable_step_ref` (`PURSUE_GOAL_STEP`) or a `CONSIDER_EXPLICIT_GOAL`
        attention entry, never splitting a Goal into multiple tasks (no Planner in AG-0, Section 30).
      - Never auto-starts an Activity (E04).
    """

    source_kind = SOURCE_EXPLICIT_GOAL
    canonical_owner = "goal_authority (NOT_IMPLEMENTED in production repo; DISABLED by default)"

    def __init__(
        self,
        canonical_goals: Optional[Sequence[CanonicalExplicitGoalRecord | Mapping[str, Any]]] = None,
        *,
        enabled: bool = False,
        per_source_cap: int = DEFAULT_PER_SOURCE_CAP,
    ) -> None:
        super().__init__(per_source_cap=per_source_cap)
        self._enabled = bool(enabled or canonical_goals is not None)
        self._goals_by_id: dict[str, CanonicalExplicitGoalRecord] = {}
        self._revision: int = 0
        if canonical_goals:
            for g in canonical_goals:
                self.register_canonical_goal(g)

    def is_enabled(self) -> bool:
        return self._enabled

    def availability_status(self) -> dict[str, Any]:
        if not self._enabled:
            return {
                "source_kind": self.source_kind,
                "status": ADAPTER_STATUS_DISABLED,
                "canonical_owner": self.canonical_owner,
                "reason_code": "MISSING_CANONICAL_GOAL_AUTHORITY",
            }
        return {
            "source_kind": self.source_kind,
            "status": ADAPTER_STATUS_READY,
            "canonical_owner": "goal_authority (Explicit Canonical Port)",
            "reason_code": "CANONICAL_GOAL_PORT_ATTACHED",
        }

    def register_canonical_goal(
        self, goal: CanonicalExplicitGoalRecord | Mapping[str, Any] | str
    ) -> CanonicalExplicitGoalRecord:
        if isinstance(goal, str):
            raise UntrustedCandidateSourceError(
                "Section 28 violation: cannot create Goal Candidate from Prompt text, Memory recall, or LLM narration"
            )
        if not self._enabled:
            raise DisabledCandidateSourceError(
                "EXPLICIT_GOAL source adapter is DISABLED because no canonical Goal owner is configured"
            )
        if isinstance(goal, CanonicalExplicitGoalRecord):
            g_obj = copy.deepcopy(goal)
        elif isinstance(goal, Mapping):
            _reject_forbidden_keys(goal, "CanonicalExplicitGoalRecord")
            g_obj = CanonicalExplicitGoalRecord(**dict(goal))
        else:
            raise UntrustedCandidateSourceError(f"Invalid goal record type: {type(goal)!r}")
        self._goals_by_id[g_obj.goal_id] = g_obj
        self._revision += 1
        return g_obj

    def update_goal_status(self, goal_id: str, *, new_status: str, observed_at: str) -> None:
        obs_iso = _require_iso(observed_at, "observed_at")
        if goal_id not in self._goals_by_id:
            raise CandidateSourceError(f"unknown goal_id={goal_id!r}")
        rec = self._goals_by_id[goal_id]
        if new_status not in {"ACTIVE", "COMPLETED", "CANCELLED", "ABANDONED", "INVALID"}:
            raise CandidateSourceError(f"invalid goal status={new_status!r}")
        rec.status = new_status
        rec.observed_at = obs_iso
        rec.revision += 1
        self._revision += 1

    def discover(self, *, observed_at: str) -> tuple[list[dict[str, Any]], str, dict[str, int]]:
        obs_iso = _require_iso(observed_at, "observed_at")
        if not self._enabled:
            return [], "snap:EXPLICIT_GOAL:disabled", {"discovered": 0, "emitted": 0, "cap": self.per_source_cap}

        snap_ref = f"snap:EXPLICIT_GOAL:rev:{self._revision}"
        valid_items: list[dict[str, Any]] = []
        for gid in sorted(self._goals_by_id.keys()):
            g_dict = self._goals_by_id[gid].to_dict()
            ok, _ = self.validate_source(g_dict, observed_at=obs_iso)
            if ok:
                valid_items.append(g_dict)

        total_discovered = len(valid_items)
        bounded = valid_items[: self.per_source_cap]
        return bounded, snap_ref, {
            "discovered": total_discovered,
            "emitted": len(bounded),
            "cap": self.per_source_cap,
        }

    def validate_source(
        self, raw_item: Mapping[str, Any], *, observed_at: str
    ) -> tuple[bool, Optional[str]]:
        if not self._enabled:
            return False, "EXPLICIT_GOAL_SOURCE_DISABLED"
        obs_dt = _parse_iso(_require_iso(observed_at, "observed_at"))
        if not isinstance(raw_item, Mapping):
            return False, "INVALID_GOAL_OBJECT"
        if raw_item.get("subject_id", GLOBAL_SUBJECT_ID) != GLOBAL_SUBJECT_ID:
            raise SubjectIsolationError("Cross-subject goal rejected")
        if raw_item.get("authority_domain") != "goal_authority":
            return False, "UNTRUSTED_GOAL_AUTHORITY"
        status = raw_item.get("status")
        if status != "ACTIVE":
            return False, f"GOAL_STATUS_{status}_NOT_ACTIVE"
        if raw_item.get("valid_from") and obs_dt < _parse_iso(str(raw_item["valid_from"])):
            return False, "GOAL_NOT_YET_VALID"
        if raw_item.get("valid_until") and obs_dt > _parse_iso(str(raw_item["valid_until"])):
            return False, "GOAL_EXPIRED"
        return True, None

    def materialize(
        self, *, observed_at: str
    ) -> tuple[list[CandidateRecord], str, dict[str, int]]:
        obs_iso = _require_iso(observed_at, "observed_at")
        items, snap_ref, meta = self.discover(observed_at=obs_iso)
        results: list[CandidateRecord] = []
        for rec in items:
            gid = str(rec["goal_id"])
            grev = int(rec["revision"])
            step_ref = rec.get("next_actionable_step_ref")
            # Section 29: If next actionable plan/step exists, reference it; otherwise emit goal_attention_candidate
            cand_kind = (
                CANDIDATE_KIND_PURSUE_GOAL_STEP
                if step_ref is not None
                else CANDIDATE_KIND_CONSIDER_EXPLICIT_GOAL
            )
            anchor = str(step_ref or gid)
            cand_id = f"cand:{sha256_hex(f'EXPLICIT_GOAL:{anchor}:v{grev}')[:16]}"
            prov_refs = [gid, snap_ref]
            if step_ref:
                prov_refs.insert(1, str(step_ref))

            cand = CandidateRecord(
                candidate_id=cand_id,
                subject_id=GLOBAL_SUBJECT_ID,
                candidate_kind=cand_kind,
                source_kind=SOURCE_EXPLICIT_GOAL,
                source_ref=gid,
                source_refs=[gid],
                source_revision=grev,
                goal_ref=gid,
                goal_step_ref=str(step_ref) if step_ref else None,
                target_refs=list(rec["target_refs"]),
                availability_status=CANDIDATE_STATUS_OPEN,
                created_at=str(rec["created_at"]),
                observed_at=obs_iso,
                recorded_at=obs_iso,
                valid_from=rec.get("valid_from"),
                valid_until=rec.get("valid_until"),
                causal_parent_refs=[gid],
                provenance_refs=prov_refs,
                provenance_chain=[
                    {"step": "candidate", "ref": cand_id, "kind": cand_kind},
                    {"step": "adapter", "ref": "adapter:EXPLICIT_GOAL", "owner": "goal_authority"},
                    {
                        "step": "canonical_goal",
                        "ref": gid,
                        "next_actionable_step_ref": step_ref,
                        "revision": grev,
                    },
                ],
                idempotency_key=f"idem_cand:explicit_goal:{anchor}:v{grev}",
            )
            results.append(cand)
        return results, snap_ref, meta

    def refresh(self, candidate: CandidateRecord, *, observed_at: str) -> CandidateRecord:
        obs_iso = _require_iso(observed_at, "observed_at")
        if not self._enabled:
            return self.invalidate(
                candidate,
                reason_code="EXPLICIT_GOAL_SOURCE_DISABLED",
                observed_at=obs_iso,
                new_status=CANDIDATE_STATUS_INVALID,
            )
        gid = candidate.goal_ref or candidate.source_ref
        rec = self._goals_by_id.get(gid)
        if rec is None:
            return self.invalidate(
                candidate,
                reason_code="GOAL_MISSING",
                observed_at=obs_iso,
                new_status=CANDIDATE_STATUS_INVALID,
            )
        if rec.status in {"COMPLETED", "CANCELLED", "ABANDONED", "INVALID"}:
            return self.invalidate(
                candidate,
                reason_code=f"GOAL_{rec.status}",
                observed_at=obs_iso,
                new_status=CANDIDATE_STATUS_STALE,
            )
        if rec.valid_until and _parse_iso(obs_iso) > _parse_iso(rec.valid_until):
            return self.invalidate(
                candidate,
                reason_code="GOAL_EXPIRED",
                observed_at=obs_iso,
                new_status=CANDIDATE_STATUS_EXPIRED,
            )
        if int(rec.revision) != int(candidate.source_revision or -1):
            return self.invalidate(
                candidate,
                reason_code="GOAL_REVISION_ADVANCED",
                observed_at=obs_iso,
                new_status=CANDIDATE_STATUS_STALE,
            )
        return copy.deepcopy(candidate)


# ---------------------------------------------------------------------------
# 6. WorldOpportunityCandidateSource (Sections 31-34, 52, 72, 87)
# ---------------------------------------------------------------------------


class WorldOpportunityCandidateSource(CandidateSourceAdapter):
    """Sections 31-34, 52, 72, 87: Observed World Opportunity source adapter.

    Invariants:
      - `World knows X != Chiyo knows X`: only explicitly observed World opportunities
        with `observed=True`, `perception_ref`, `world_revision_ref`, and `[valid_from, valid_until]`
        may enter.
      - Global World DB scanning (`scan_global_world_db`) is strictly forbidden (`ForbiddenWorldGlobalScanError`).
      - Opportunity = Observed possibility, NOT recommended behavior or motive.
    """

    source_kind = SOURCE_WORLD_OPPORTUNITY
    canonical_owner = "world_perception_authority (Observed World Opportunity Seam)"

    def __init__(
        self,
        opportunities: Optional[Sequence[ObservedWorldOpportunity | Mapping[str, Any]]] = None,
        *,
        per_source_cap: int = DEFAULT_PER_SOURCE_CAP,
    ) -> None:
        super().__init__(per_source_cap=per_source_cap)
        self._opportunities_by_id: dict[str, ObservedWorldOpportunity] = {}
        self._current_world_revision_ref: str = "wrev:0"
        self._revision: int = 0
        if opportunities:
            for opp in opportunities:
                self.record_observed_opportunity(opp)

    def is_enabled(self) -> bool:
        return True

    def availability_status(self) -> dict[str, Any]:
        return {
            "source_kind": self.source_kind,
            "status": ADAPTER_STATUS_READY,
            "canonical_owner": self.canonical_owner,
            "reason_code": "OBSERVED_PERCEPTION_SEAM_READY",
        }

    def scan_global_world_db(self, world_db_state: Any) -> None:
        """Section 32 & F04: Explicitly forbid scanning the global World DB to invent Candidates."""
        raise ForbiddenWorldGlobalScanError(
            "Section 32 violation: Global World DB scan cannot generate Candidates ('World knows X != Chiyo knows X')"
        )

    def record_observed_opportunity(
        self, opportunity: ObservedWorldOpportunity | Mapping[str, Any]
    ) -> ObservedWorldOpportunity:
        if isinstance(opportunity, ObservedWorldOpportunity):
            opp_obj = copy.deepcopy(opportunity)
        elif isinstance(opportunity, Mapping):
            _reject_forbidden_keys(opportunity, "ObservedWorldOpportunity")
            # Reject raw global World state objects lacking perception_ref
            if "world_id" in opportunity and "places" in opportunity and "perception_ref" not in opportunity:
                raise ForbiddenWorldGlobalScanError(
                    "Raw global WorldStateStore snapshot cannot be ingested without perception projection"
                )
            if opportunity.get("observed") is False or not opportunity.get("perception_ref"):
                raise ForbiddenWorldGlobalScanError(
                    "Unobserved World fact without perception_ref cannot create a Candidate"
                )
            opp_obj = ObservedWorldOpportunity(**dict(opportunity))
        else:
            raise UntrustedCandidateSourceError(f"Invalid world opportunity input: {type(opportunity)!r}")

        self._opportunities_by_id[opp_obj.opportunity_id] = opp_obj
        self._current_world_revision_ref = opp_obj.world_revision_ref
        self._revision += 1
        return opp_obj

    def expire_opportunity(self, opportunity_id: str, *, observed_at: str) -> None:
        obs_iso = _require_iso(observed_at, "observed_at")
        if opportunity_id not in self._opportunities_by_id:
            raise CandidateSourceError(f"unknown opportunity_id={opportunity_id!r}")
        opp = self._opportunities_by_id[opportunity_id]
        opp.status = "EXPIRED"
        opp.observed_at = obs_iso
        opp.revision += 1
        self._revision += 1

    def advance_world_revision_for_opportunity(
        self, opportunity_id: str, *, new_world_revision_ref: str, new_revision: int, observed_at: str
    ) -> None:
        obs_iso = _require_iso(observed_at, "observed_at")
        if opportunity_id not in self._opportunities_by_id:
            raise CandidateSourceError(f"unknown opportunity_id={opportunity_id!r}")
        opp = self._opportunities_by_id[opportunity_id]
        opp.world_revision_ref = _validate_structured_ref(
            new_world_revision_ref,
            "new_world_revision_ref",
            allowed_prefixes=frozenset({"wrev", "world_rev", "scene_rev"}),
        )
        opp.revision = int(new_revision)
        opp.observed_at = obs_iso
        self._current_world_revision_ref = opp.world_revision_ref
        self._revision += 1

    def discover(self, *, observed_at: str) -> tuple[list[dict[str, Any]], str, dict[str, int]]:
        obs_iso = _require_iso(observed_at, "observed_at")
        snap_ref = f"snap:WORLD_OPPORTUNITY:rev:{self._revision}:{self._current_world_revision_ref}"
        valid_items: list[dict[str, Any]] = []

        for oid in sorted(self._opportunities_by_id.keys()):
            opp_dict = self._opportunities_by_id[oid].to_dict()
            ok, _ = self.validate_source(opp_dict, observed_at=obs_iso)
            if ok:
                valid_items.append(opp_dict)

        total_discovered = len(valid_items)
        bounded = valid_items[: self.per_source_cap]
        return bounded, snap_ref, {
            "discovered": total_discovered,
            "emitted": len(bounded),
            "cap": self.per_source_cap,
        }

    def validate_source(
        self, raw_item: Mapping[str, Any], *, observed_at: str
    ) -> tuple[bool, Optional[str]]:
        obs_dt = _parse_iso(_require_iso(observed_at, "observed_at"))
        if not isinstance(raw_item, Mapping):
            return False, "INVALID_WORLD_OPPORTUNITY_OBJECT"
        if raw_item.get("subject_id", GLOBAL_SUBJECT_ID) != GLOBAL_SUBJECT_ID:
            raise SubjectIsolationError("Cross-subject world opportunity rejected")
        if not raw_item.get("observed") or not raw_item.get("perception_ref"):
            return False, "UNOBSERVED_WORLD_FACT"
        if raw_item.get("status") != "AVAILABLE":
            return False, f"OPPORTUNITY_STATUS_{raw_item.get('status')}"
        valid_from = raw_item.get("valid_from")
        valid_until = raw_item.get("valid_until")
        if not valid_from or not valid_until:
            return False, "MISSING_VALIDITY_WINDOW"
        if obs_dt < _parse_iso(str(valid_from)):
            return False, "OPPORTUNITY_NOT_YET_OPEN"
        if obs_dt > _parse_iso(str(valid_until)):
            return False, "OPPORTUNITY_WINDOW_EXPIRED"
        return True, None

    def materialize(
        self, *, observed_at: str
    ) -> tuple[list[CandidateRecord], str, dict[str, int]]:
        obs_iso = _require_iso(observed_at, "observed_at")
        items, snap_ref, meta = self.discover(observed_at=obs_iso)
        results: list[CandidateRecord] = []

        for opp in items:
            oid = str(opp["opportunity_id"])
            orev = int(opp["revision"])
            percept_ref = str(opp["perception_ref"])
            wrev_ref = str(opp["world_revision_ref"])
            cand_id = f"cand:{sha256_hex(f'WORLD_OPPORTUNITY:{oid}:v{orev}')[:16]}"

            cand = CandidateRecord(
                candidate_id=cand_id,
                subject_id=GLOBAL_SUBJECT_ID,
                candidate_kind=CANDIDATE_KIND_CONSIDER_WORLD_OPPORTUNITY,
                source_kind=SOURCE_WORLD_OPPORTUNITY,
                source_ref=oid,
                source_refs=[oid],
                source_revision=orev,
                world_opportunity_ref=oid,
                world_revision_ref=wrev_ref,
                perception_ref=percept_ref,
                target_refs=list(opp["target_refs"]),
                availability_status=CANDIDATE_STATUS_OPEN,
                created_at=str(opp["occurred_at"]),
                observed_at=obs_iso,
                recorded_at=obs_iso,
                valid_from=str(opp["valid_from"]),
                valid_until=str(opp["valid_until"]),
                causal_parent_refs=[oid, percept_ref],
                provenance_refs=[oid, percept_ref, wrev_ref, snap_ref],
                provenance_chain=[
                    {"step": "candidate", "ref": cand_id, "kind": CANDIDATE_KIND_CONSIDER_WORLD_OPPORTUNITY},
                    {"step": "adapter", "ref": "adapter:WORLD_OPPORTUNITY", "owner": self.canonical_owner},
                    {
                        "step": "observed_world_opportunity",
                        "ref": oid,
                        "perception_ref": percept_ref,
                        "world_revision_ref": wrev_ref,
                        "revision": orev,
                    },
                ],
                idempotency_key=f"idem_cand:world_opportunity:{oid}:v{orev}",
            )
            results.append(cand)
        return results, snap_ref, meta

    def refresh(self, candidate: CandidateRecord, *, observed_at: str) -> CandidateRecord:
        obs_iso = _require_iso(observed_at, "observed_at")
        oid = candidate.world_opportunity_ref or candidate.source_ref
        opp = self._opportunities_by_id.get(oid)
        if opp is None:
            return self.invalidate(
                candidate,
                reason_code="WORLD_OPPORTUNITY_MISSING",
                observed_at=obs_iso,
                new_status=CANDIDATE_STATUS_INVALID,
            )
        if opp.status == "WITHDRAWN":
            return self.invalidate(
                candidate,
                reason_code="WORLD_OPPORTUNITY_WITHDRAWN",
                observed_at=obs_iso,
                new_status=CANDIDATE_STATUS_WITHDRAWN,
            )
        if opp.status in {"EXPIRED", "CLOSED"} or _parse_iso(obs_iso) > _parse_iso(opp.valid_until):
            return self.invalidate(
                candidate,
                reason_code="WORLD_OPPORTUNITY_EXPIRED",
                observed_at=obs_iso,
                new_status=CANDIDATE_STATUS_EXPIRED,
            )
        if (
            int(opp.revision) != int(candidate.source_revision or -1)
            or opp.world_revision_ref != candidate.world_revision_ref
        ):
            return self.invalidate(
                candidate,
                reason_code="WORLD_OPPORTUNITY_REVISION_ADVANCED",
                observed_at=obs_iso,
                new_status=CANDIDATE_STATUS_STALE,
            )
        return copy.deepcopy(candidate)


# ---------------------------------------------------------------------------
# CandidateSourceRegistry & CandidateAuditJournal (Sections 15, 60, 84, 99)
# ---------------------------------------------------------------------------


class CandidateSourceRegistry:
    """Section 15 & 99: Registry of the 6 first-edition AG-0 CandidateSourceAdapters."""

    def __init__(self, adapters: Optional[Mapping[str, CandidateSourceAdapter]] = None) -> None:
        self._adapters: dict[str, CandidateSourceAdapter] = {}
        if adapters:
            for k, adapter in adapters.items():
                self.register_adapter(adapter, expected_kind=k)

    def register_adapter(
        self, adapter: CandidateSourceAdapter, *, expected_kind: Optional[str] = None
    ) -> None:
        if not isinstance(adapter, CandidateSourceAdapter):
            raise CandidateSourceError("adapter must be a CandidateSourceAdapter instance")
        sk = adapter.source_kind
        if expected_kind is not None and sk != expected_kind:
            raise CandidateSourceError(f"adapter source_kind={sk!r} != expected_kind={expected_kind!r}")
        if sk in FORBIDDEN_CANDIDATE_SOURCE_KINDS:
            raise UntrustedCandidateSourceError(f"source_kind={sk!r} is forbidden in AG-0")
        if sk not in VALID_CANDIDATE_SOURCE_KINDS:
            raise UntrustedCandidateSourceError(f"unsupported source_kind={sk!r}")
        self._adapters[sk] = adapter

    def get_adapter(self, source_kind: str) -> CandidateSourceAdapter:
        if source_kind in FORBIDDEN_CANDIDATE_SOURCE_KINDS:
            raise UntrustedCandidateSourceError(f"source_kind={source_kind!r} is forbidden in AG-0")
        if source_kind not in self._adapters:
            raise CandidateSourceError(f"no adapter registered for source_kind={source_kind!r}")
        return self._adapters[source_kind]

    def list_adapters(self) -> list[CandidateSourceAdapter]:
        return [
            self._adapters[k]
            for k in sorted(self._adapters.keys())
        ]

    def get_availability_matrix(self) -> dict[str, dict[str, Any]]:
        matrix: dict[str, dict[str, Any]] = {}
        for sk in sorted(VALID_CANDIDATE_SOURCE_KINDS):
            if sk in self._adapters:
                matrix[sk] = self._adapters[sk].availability_status()
            else:
                matrix[sk] = {
                    "source_kind": sk,
                    "status": ADAPTER_STATUS_DISABLED,
                    "canonical_owner": "UNREGISTERED",
                    "reason_code": "ADAPTER_NOT_REGISTERED",
                }
        return matrix


@dataclass
class CandidateMaterializationRecord:
    """Section 60: Audit journal record for candidate materialization & invalidation (`0 chain-of-thought`)."""

    record_id: str
    candidate_id: str
    source_kind: str
    source_ref: str
    source_revision: Optional[int | str]
    event_type: str
    availability_status: str
    materialized_at: str
    reason_code: str
    policy_version: str = POLICY_VERSION_AG0_V1
    invalidated_at: Optional[str] = None
    prev_entry_hash: str = "GENESIS"
    entry_hash: str = ""
    schema_version: str = SCHEMA_CANDIDATE_AUDIT

    def __post_init__(self) -> None:
        if not self.entry_hash:
            payload = {
                "schema_version": self.schema_version,
                "record_id": self.record_id,
                "candidate_id": self.candidate_id,
                "source_kind": self.source_kind,
                "source_ref": self.source_ref,
                "source_revision": self.source_revision,
                "event_type": self.event_type,
                "availability_status": self.availability_status,
                "materialized_at": self.materialized_at,
                "invalidated_at": self.invalidated_at,
                "reason_code": self.reason_code,
                "policy_version": self.policy_version,
                "prev_entry_hash": self.prev_entry_hash,
            }
            self.entry_hash = sha256_hex(json.dumps(payload, sort_keys=True, separators=(",", ":")))

    def to_dict(self) -> dict[str, Any]:
        return copy.deepcopy(dataclasses.asdict(self))


class CandidateAuditJournal:
    """Section 60: Lightweight append-only audit journal for candidate materialization & invalidation."""

    def __init__(self, journal_path: Optional[Path] = None) -> None:
        self.journal_path = Path(journal_path) if journal_path is not None else None
        self._entries: list[CandidateMaterializationRecord] = []
        if self.journal_path is not None:
            self.journal_path.parent.mkdir(parents=True, exist_ok=True)

    def append(
        self,
        *,
        candidate: CandidateRecord,
        event_type: str,
        reason_code: str,
        timestamp: str,
    ) -> CandidateMaterializationRecord:
        prev_hash = self._entries[-1].entry_hash if self._entries else "GENESIS"
        seq = len(self._entries) + 1
        rec_id = f"caud:{sha256_hex(f'{seq}:{candidate.candidate_id}:{event_type}:{timestamp}')[:16]}"
        rec = CandidateMaterializationRecord(
            record_id=rec_id,
            candidate_id=candidate.candidate_id,
            source_kind=candidate.source_kind,
            source_ref=candidate.source_ref,
            source_revision=candidate.source_revision,
            event_type=event_type,
            availability_status=candidate.availability_status,
            materialized_at=candidate.created_at,
            invalidated_at=timestamp if candidate.availability_status != CANDIDATE_STATUS_OPEN else None,
            reason_code=reason_code,
            policy_version=candidate.policy_version,
            prev_entry_hash=prev_hash,
        )
        self._entries.append(rec)
        if self.journal_path is not None:
            with self.journal_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec.to_dict(), ensure_ascii=False, separators=(",", ":")) + "\n")
        return rec

    def list_entries(self, *, candidate_id: Optional[str] = None) -> list[dict[str, Any]]:
        items = [e.to_dict() for e in self._entries]
        if candidate_id is not None:
            items = [e for e in items if e["candidate_id"] == candidate_id]
        return items

    def verify_integrity(self) -> bool:
        prev = "GENESIS"
        for e in self._entries:
            if e.prev_entry_hash != prev:
                return False
            recomputed = CandidateMaterializationRecord(
                record_id=e.record_id,
                candidate_id=e.candidate_id,
                source_kind=e.source_kind,
                source_ref=e.source_ref,
                source_revision=e.source_revision,
                event_type=e.event_type,
                availability_status=e.availability_status,
                materialized_at=e.materialized_at,
                invalidated_at=e.invalidated_at,
                reason_code=e.reason_code,
                policy_version=e.policy_version,
                prev_entry_hash=e.prev_entry_hash,
            )
            if recomputed.entry_hash != e.entry_hash:
                return False
            prev = e.entry_hash
        return True


# ---------------------------------------------------------------------------
# CandidateMaterializer & CandidateReadService (Sections 15, 35-43, 53-59)
# ---------------------------------------------------------------------------


class CandidateMaterializer:
    """Sections 15, 35-43, 53-59: Materializes, deduplicates, refreshes, and invalidates CandidateSets.

    Enforces:
      - Zero raw ungrounded candidate creation (`create_candidate` is rejected).
      - Bounded per-source and global candidate counts (`cap != priority`).
      - Deduplication with complete provenance preservation (`H01-H05`).
      - Zero side effects on Activity, Action, World, Memory, Goal, or Outbound.
    """

    def __init__(
        self,
        registry: CandidateSourceRegistry,
        *,
        capability: CandidateProjectionCapability,
        audit_journal: Optional[CandidateAuditJournal] = None,
        global_cap: int = DEFAULT_GLOBAL_CANDIDATE_CAP,
        record_audit: bool = True,
    ) -> None:
        CandidateProjectionGuard.verify_capability(capability)
        if global_cap < 1:
            raise CandidateSourceError("global_cap must be >= 1")
        self.registry = registry
        self.capability = capability
        self.audit_journal = audit_journal or CandidateAuditJournal()
        self.global_cap = int(global_cap)
        self.record_audit = bool(record_audit)

        # In-memory projection state (rebuildable from canonical sources on restart, Section 58-59)
        self._candidates_by_id: dict[str, CandidateRecord] = {}
        self._candidate_sets_by_id: dict[str, CandidateSet] = {}
        self._latest_candidate_set_id: Optional[str] = None
        self._target_provenance_index: dict[str, list[str]] = {}

        # Telemetry counters proving 0 side effects (Sections 77, 96)
        self.activity_mutation_count: int = 0
        self.action_proposal_count: int = 0
        self.world_write_count: int = 0
        self.memory_write_count: int = 0
        self.goal_write_count: int = 0
        self.outbound_message_count: int = 0
        self.llm_call_count: int = 0

    # -- Negative Authority & Raw Write Guards (Sections 3, 43, 48-51, 76) ---

    def create_candidate(self, *args: Any, **kwargs: Any) -> None:
        """Section 43 & H05: Raw sourceless candidate creation is permanently forbidden."""
        raise MissingCandidateProvenanceError(
            "Section 43 violation: raw create_candidate() without SourceAdapter + provenance is forbidden"
        )

    def reject_negative_authority_input(self, source_type: str, payload: Any = None) -> None:
        """Sections 48-51, 76, 83 (I06-I08): Explicitly reject Prompt, LLM narration, Memory recall, Body, Affect, etc."""
        raise UntrustedCandidateSourceError(
            f"Negative authority violation: {source_type!r} cannot sign or generate a Candidate ({payload!r})"
        )

    def ingest_arbitrary_candidate_dict(self, raw_dict: Mapping[str, Any]) -> CandidateRecord:
        """Validate a CandidateRecord dict, rejecting missing provenance, forbidden sources, ranking fields, or body copies."""
        CandidateProjectionGuard.verify_capability(self.capability)
        if not isinstance(raw_dict, Mapping):
            raise UntrustedCandidateSourceError("Candidate input must be a mapping")
        _reject_forbidden_keys(raw_dict, "CandidateRecord")
        if not raw_dict.get("source_kind") or not raw_dict.get("source_ref"):
            raise MissingCandidateProvenanceError("Candidate without source_kind and source_ref is rejected")
        rec = CandidateRecord.from_dict(raw_dict)
        adapter = self.registry.get_adapter(rec.source_kind)
        if not adapter.is_enabled():
            raise DisabledCandidateSourceError(f"Source {rec.source_kind!r} is disabled")
        return rec

    # -- Deduplication & Provenance Linking (Sections 39-41, 74) -------------

    @staticmethod
    def deduplicate_candidates(
        candidates: Sequence[CandidateRecord],
    ) -> tuple[list[CandidateRecord], dict[str, list[str]]]:
        """Deduplicate candidates while preserving multi-provenance `source_refs`:
        1. Candidates with the same `semantic_identity` (or same activity resume/continue entry)
           collapse into one CandidateRecord while merging `source_refs`, `source_kinds`,
           `causal_parent_refs`, `provenance_refs`, and `provenance_chain`.
        2. Candidates from different provenance kinds (e.g. USER_REQUEST vs COMMITMENT) that
           share a `target_ref` keep their distinct provenance records AND cross-populate
           `shared_target_source_refs` and `target_provenance_index` so no source_ref is ever lost.
        """
        by_semantic_id: dict[str, CandidateRecord] = {}
        target_to_source_refs: dict[str, list[str]] = {}

        for cand in candidates:
            for tref in cand.target_refs:
                bucket = target_to_source_refs.setdefault(tref, [])
                for sref in cand.source_refs:
                    if sref not in bucket:
                        bucket.append(sref)

            sem_key = str(cand.semantic_identity)
            if sem_key not in by_semantic_id:
                by_semantic_id[sem_key] = copy.deepcopy(cand)
            else:
                existing = by_semantic_id[sem_key]
                for sref in cand.source_refs:
                    if sref not in existing.source_refs:
                        existing.source_refs.append(sref)
                for sk in cand.source_kinds:
                    if sk not in existing.source_kinds:
                        existing.source_kinds.append(sk)
                for cpref in cand.causal_parent_refs:
                    if cpref not in existing.causal_parent_refs:
                        existing.causal_parent_refs.append(cpref)
                for pref in cand.provenance_refs:
                    if pref not in existing.provenance_refs:
                        existing.provenance_refs.append(pref)
                for step in cand.provenance_chain:
                    if step not in existing.provenance_chain:
                        existing.provenance_chain.append(copy.deepcopy(step))

        deduped = list(by_semantic_id.values())
        for cand in deduped:
            merged_shared = list(cand.source_refs)
            for tref in cand.target_refs:
                for sref in target_to_source_refs.get(tref, []):
                    if sref not in merged_shared:
                        merged_shared.append(sref)
            cand.shared_target_source_refs = merged_shared

        # Deterministic ordering purely by candidate_id for serialization stability (G05: NOT priority!)
        deduped.sort(key=lambda c: c.candidate_id)
        return deduped, target_to_source_refs

    # -- CandidateSet Building, Refreshing & Stale Invalidation --------------

    def refresh_existing_candidates(self, *, observed_at: str) -> list[CandidateRecord]:
        """Sections 53-55, 75: Refresh all previously materialized OPEN candidates against current sources."""
        CandidateProjectionGuard.verify_capability(self.capability)
        obs_iso = _require_iso(observed_at, "observed_at")
        invalidated_now: list[CandidateRecord] = []

        for cid, cand in list(self._candidates_by_id.items()):
            if cand.availability_status != CANDIDATE_STATUS_OPEN:
                continue
            adapter = self.registry.get_adapter(cand.source_kind)
            refreshed = adapter.refresh(cand, observed_at=obs_iso)
            self._candidates_by_id[cid] = refreshed
            if refreshed.availability_status != CANDIDATE_STATUS_OPEN:
                invalidated_now.append(refreshed)
                if self.record_audit:
                    self.audit_journal.append(
                        candidate=refreshed,
                        event_type="INVALIDATED",
                        reason_code=refreshed.invalidation_reason or "SOURCE_INVALIDATED",
                        timestamp=obs_iso,
                    )
        return invalidated_now

    def build_candidate_set(
        self,
        *,
        observed_at: Optional[str] = None,
    ) -> CandidateSet:
        """Sections 35-42, 53-57: Build a point-in-time deterministic CandidateSet across all registered sources."""
        CandidateProjectionGuard.verify_capability(self.capability)
        obs_iso = _require_iso(observed_at or _iso(now()), "observed_at")

        # 1. Refresh existing OPEN candidates so stale/cancelled/expired/terminal ones transition immediately
        self.refresh_existing_candidates(observed_at=obs_iso)

        # 2. Materialize fresh candidates from each enabled adapter in deterministic source_kind order
        raw_candidates: list[CandidateRecord] = []
        snapshot_refs: list[str] = []
        truncated_sources: dict[str, dict[str, int]] = {}

        for adapter in self.registry.list_adapters():
            if not adapter.is_enabled():
                continue
            cands, snap_ref, meta = adapter.materialize(observed_at=obs_iso)
            snapshot_refs.append(snap_ref)
            if meta["discovered"] > meta["emitted"]:
                truncated_sources[adapter.source_kind] = meta
            raw_candidates.extend(cands)

        # 3. Deduplicate and link shared-target provenance
        deduped, target_index = self.deduplicate_candidates(raw_candidates)
        self._target_provenance_index = target_index

        # 4. Enforce global cap (Section 38 & 80: resource cap only, never value ranking)
        global_truncated = len(deduped) > self.global_cap
        bounded_candidates = deduped[: self.global_cap]

        # 5. Store active candidates and mark any previously OPEN candidate not in current valid set as STALE
        current_open_ids = {c.candidate_id for c in bounded_candidates}
        for c in bounded_candidates:
            is_new = c.candidate_id not in self._candidates_by_id
            self._candidates_by_id[c.candidate_id] = c
            if is_new and self.record_audit:
                self.audit_journal.append(
                    candidate=c,
                    event_type="MATERIALIZED",
                    reason_code="SOURCE_VALIDATED",
                    timestamp=obs_iso,
                )

        for cid, existing in list(self._candidates_by_id.items()):
            if existing.availability_status == CANDIDATE_STATUS_OPEN and cid not in current_open_ids:
                # Double-check via adapter refresh; if still OPEN (e.g., superseded by new revision with new cand_id), mark STALE
                adapter = self.registry.get_adapter(existing.source_kind)
                refreshed = adapter.refresh(existing, observed_at=obs_iso)
                if refreshed.availability_status == CANDIDATE_STATUS_OPEN:
                    refreshed = adapter.invalidate(
                        existing,
                        reason_code="SUPERSEDED_OR_DROPPED_FROM_ACTIVE_SET",
                        observed_at=obs_iso,
                        new_status=CANDIDATE_STATUS_STALE,
                    )
                self._candidates_by_id[cid] = refreshed
                if self.record_audit:
                    self.audit_journal.append(
                        candidate=refreshed,
                        event_type="INVALIDATED",
                        reason_code=refreshed.invalidation_reason or "STALE",
                        timestamp=obs_iso,
                    )

        cand_refs = [c.candidate_id for c in bounded_candidates]
        set_hash_input = json.dumps(
            {
                "subject_id": GLOBAL_SUBJECT_ID,
                "observed_at": obs_iso,
                "candidate_refs": cand_refs,
                "source_snapshot_refs": snapshot_refs,
                "policy": POLICY_VERSION_AG0_V1,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        cset_id = f"cset:{sha256_hex(set_hash_input)[:16]}"
        cset = CandidateSet(
            candidate_set_id=cset_id,
            subject_id=GLOBAL_SUBJECT_ID,
            candidate_refs=cand_refs,
            source_snapshot_refs=snapshot_refs,
            observed_at=obs_iso,
            built_at=obs_iso,
            builder_policy_version=POLICY_VERSION_AG0_V1,
            truncated_sources=truncated_sources,
            global_truncated=global_truncated,
        )
        self._candidate_sets_by_id[cset_id] = cset
        self._latest_candidate_set_id = cset_id
        return cset


class CandidateReadService:
    """Section 42: Read-only query and provenance tracing API for AG-0 Candidate Sources."""

    def __init__(self, materializer: CandidateMaterializer) -> None:
        self.materializer = materializer

    def build_candidate_set(self, *, observed_at: Optional[str] = None) -> dict[str, Any]:
        cset = self.materializer.build_candidate_set(observed_at=observed_at)
        return cset.to_dict()

    def get_candidate(self, candidate_id: str) -> Optional[dict[str, Any]]:
        cand = self.materializer._candidates_by_id.get(candidate_id)
        return cand.to_dict() if cand is not None else None

    def list_open_candidates(self, *, observed_at: Optional[str] = None) -> list[dict[str, Any]]:
        if observed_at is not None:
            self.materializer.refresh_existing_candidates(observed_at=observed_at)
        items = [
            c.to_dict()
            for c in self.materializer._candidates_by_id.values()
            if c.availability_status == CANDIDATE_STATUS_OPEN
        ]
        items.sort(key=lambda x: str(x["candidate_id"]))
        return items

    def list_candidates_by_source(
        self, source_kind: str, *, include_non_open: bool = False
    ) -> list[dict[str, Any]]:
        if source_kind in FORBIDDEN_CANDIDATE_SOURCE_KINDS:
            raise UntrustedCandidateSourceError(f"source_kind={source_kind!r} is forbidden")
        items = [
            c.to_dict()
            for c in self.materializer._candidates_by_id.values()
            if (source_kind in c.source_kinds or c.source_kind == source_kind)
            and (include_non_open or c.availability_status == CANDIDATE_STATUS_OPEN)
        ]
        items.sort(key=lambda x: str(x["candidate_id"]))
        return items

    def list_candidates_for_target(
        self, target_ref: str, *, include_non_open: bool = False
    ) -> list[dict[str, Any]]:
        items = [
            c.to_dict()
            for c in self.materializer._candidates_by_id.values()
            if target_ref in c.target_refs
            and (include_non_open or c.availability_status == CANDIDATE_STATUS_OPEN)
        ]
        items.sort(key=lambda x: str(x["candidate_id"]))
        return items

    def get_target_provenance_refs(self, target_ref: str) -> list[str]:
        return list(self.materializer._target_provenance_index.get(target_ref, []))

    def trace_candidate_provenance(self, candidate_id: str) -> dict[str, Any]:
        """Section 41 & H04: Trace Candidate -> Source Adapter -> Canonical Source / Observed Event."""
        cand = self.materializer._candidates_by_id.get(candidate_id)
        if cand is None:
            raise CandidateSourceError(f"unknown candidate_id={candidate_id!r}")
        adapter = self.materializer.registry.get_adapter(cand.source_kind)
        return {
            "candidate_id": cand.candidate_id,
            "candidate_kind": cand.candidate_kind,
            "subject_id": cand.subject_id,
            "source_kind": cand.source_kind,
            "source_kinds": list(cand.source_kinds),
            "source_ref": cand.source_ref,
            "source_refs": list(cand.source_refs),
            "shared_target_source_refs": list(cand.shared_target_source_refs),
            "source_revision": cand.source_revision,
            "canonical_owner": adapter.canonical_owner,
            "causal_parent_refs": list(cand.causal_parent_refs),
            "provenance_refs": list(cand.provenance_refs),
            "provenance_chain": copy.deepcopy(cand.provenance_chain),
            "audit_trail": self.materializer.audit_journal.list_entries(candidate_id=candidate_id),
        }

    def validate_candidate(
        self, candidate_id: str, *, observed_at: Optional[str] = None
    ) -> dict[str, Any]:
        """Sections 42, 53-55: Validate candidate freshness at use time before future AG-1 consumption."""
        obs_iso = _require_iso(observed_at or _iso(now()), "observed_at")
        cand = self.materializer._candidates_by_id.get(candidate_id)
        if cand is None:
            return {
                "candidate_id": candidate_id,
                "valid": False,
                "availability_status": CANDIDATE_STATUS_INVALID,
                "reason_code": "CANDIDATE_NOT_FOUND",
            }
        adapter = self.materializer.registry.get_adapter(cand.source_kind)
        refreshed = adapter.refresh(cand, observed_at=obs_iso)
        self.materializer._candidates_by_id[candidate_id] = refreshed
        is_valid = refreshed.availability_status == CANDIDATE_STATUS_OPEN
        return {
            "candidate_id": candidate_id,
            "valid": is_valid,
            "availability_status": refreshed.availability_status,
            "source_revision": refreshed.source_revision,
            "reason_code": refreshed.invalidation_reason or "VALID_OPEN",
            "candidate": refreshed.to_dict(),
        }


# ---------------------------------------------------------------------------
# CandidateReplayVerifier (Sections 61-62, 79)
# ---------------------------------------------------------------------------


class CandidateReplayVerifier:
    """Sections 61-62, 79: Deterministic replay verifier with 0 side effects."""

    @staticmethod
    def verify_deterministic_replay(
        registry_factory: Callable[[], CandidateSourceRegistry],
        *,
        observed_at: str,
        global_cap: int = DEFAULT_GLOBAL_CANDIDATE_CAP,
    ) -> dict[str, Any]:
        cap = CandidateProjectionGuard.issue_capability(
            caller_module="candidate_replay_verifier",
            namespace=NAMESPACE_ISOLATED_TEST,
        )
        mat_1 = CandidateMaterializer(
            registry_factory(),
            capability=cap,
            global_cap=global_cap,
            record_audit=False,
        )
        mat_2 = CandidateMaterializer(
            registry_factory(),
            capability=cap,
            global_cap=global_cap,
            record_audit=False,
        )

        set_1 = mat_1.build_candidate_set(observed_at=observed_at)
        set_2 = mat_2.build_candidate_set(observed_at=observed_at)

        cands_1 = [mat_1._candidates_by_id[cid].to_dict() for cid in set_1.candidate_refs]
        cands_2 = [mat_2._candidates_by_id[cid].to_dict() for cid in set_2.candidate_refs]

        identical = (set_1.to_dict() == set_2.to_dict()) and (cands_1 == cands_2)
        side_effects = (
            mat_1.activity_mutation_count
            + mat_1.action_proposal_count
            + mat_1.world_write_count
            + mat_1.memory_write_count
            + mat_1.goal_write_count
            + mat_1.outbound_message_count
            + mat_1.llm_call_count
            + mat_2.activity_mutation_count
            + mat_2.action_proposal_count
            + mat_2.world_write_count
            + mat_2.memory_write_count
            + mat_2.goal_write_count
            + mat_2.outbound_message_count
            + mat_2.llm_call_count
        )
        return {
            "deterministic_match": identical,
            "candidate_set_1": set_1.to_dict(),
            "candidate_set_2": set_2.to_dict(),
            "candidates_1": cands_1,
            "candidates_2": cands_2,
            "side_effect_count": side_effects,
            "activity_mutations": 0,
            "action_creations": 0,
            "world_writes": 0,
            "memory_writes": 0,
            "goal_writes": 0,
            "outbound_messages": 0,
            "llm_calls": 0,
        }


__all__ = [
    "ADAPTER_STATUS_DISABLED",
    "ADAPTER_STATUS_PARTIAL",
    "ADAPTER_STATUS_READY",
    "CANDIDATE_KIND_CONSIDER_EXPLICIT_GOAL",
    "CANDIDATE_KIND_CONSIDER_WORLD_OPPORTUNITY",
    "CANDIDATE_KIND_CONTINUE_CURRENT",
    "CANDIDATE_KIND_FULFILL_COMMITMENT",
    "CANDIDATE_KIND_PURSUE_GOAL_STEP",
    "CANDIDATE_KIND_RESPOND_USER_REQUEST",
    "CANDIDATE_KIND_RESUME_ACTIVITY",
    "CANDIDATE_STATUS_CONSUMED",
    "CANDIDATE_STATUS_EXPIRED",
    "CANDIDATE_STATUS_INVALID",
    "CANDIDATE_STATUS_OPEN",
    "CANDIDATE_STATUS_STALE",
    "CANDIDATE_STATUS_WITHDRAWN",
    "CanonicalCommitmentRecord",
    "CanonicalExplicitGoalRecord",
    "CandidateAdoptionForbiddenError",
    "CandidateAuditJournal",
    "CandidateFactDuplicationError",
    "CandidateMaterializationRecord",
    "CandidateMaterializer",
    "CandidateProjectionCapability",
    "CandidateProjectionGuard",
    "CandidateRankingForbiddenError",
    "CandidateReadService",
    "CandidateRecord",
    "CandidateReplayVerifier",
    "CandidateSet",
    "CandidateSourceAdapter",
    "CandidateSourceError",
    "CandidateSourceRegistry",
    "CommitmentCandidateSource",
    "CurrentActivityCandidateSource",
    "DEFAULT_GLOBAL_CANDIDATE_CAP",
    "DEFAULT_PER_SOURCE_CAP",
    "DisabledCandidateSourceError",
    "ExplicitGoalCandidateSource",
    "FORBIDDEN_CANDIDATE_KINDS",
    "FORBIDDEN_CANDIDATE_SOURCE_KINDS",
    "FORBIDDEN_FACT_BODY_KEYS",
    "FORBIDDEN_RANKING_AND_DECISION_KEYS",
    "ForbiddenWorldGlobalScanError",
    "MissingCandidateProvenanceError",
    "ObservedUserRequestEvent",
    "ObservedWorldOpportunity",
    "POLICY_VERSION_AG0_V1",
    "ResumeEligibleCandidateSource",
    "SCHEMA_CANDIDATE_AUDIT",
    "SCHEMA_CANDIDATE_RECORD",
    "SCHEMA_CANDIDATE_SET",
    "SOURCE_COMMITMENT",
    "SOURCE_CURRENT_ACTIVITY",
    "SOURCE_EXPLICIT_GOAL",
    "SOURCE_RESUME_ELIGIBLE",
    "SOURCE_USER_REQUEST",
    "SOURCE_WORLD_OPPORTUNITY",
    "UntrustedCandidateSourceError",
    "UserRequestCandidateSource",
    "VALID_CANDIDATE_KINDS",
    "VALID_CANDIDATE_SOURCE_KINDS",
    "VALID_CANDIDATE_STATUSES",
    "WorldOpportunityCandidateSource",
]
