#!/usr/bin/env python3
"""Chiyo Life Runtime | LR-3 Interrupt & Narrow Attention Core.

Implements the frozen LR-1/LR-2/LR-3 contracts for:
- Narrow Attention Occupancy (FREE, LIGHT, FOCUSED, ATOMIC) strictly separated
  from Psychological/Perceptual Attention.
- Physical/Logical Interruptibility (IMMEDIATE, SAFE_BOUNDARY, ATOMIC) strictly
  separated from Attention Occupancy.
- Normalized Event & Constraint Input Adapters:
  - CommunicationEventAdapter (user messages are CommunicationEvents, never auto-PAUSE)
  - ObservedWorldUrgencyAdapter (enforces World knows X != Chiyo knows X)
  - BodyCapacityAdapter (enforces Body provides capacity/constraint only, never selects or pauses Activity)
  - BoundaryEventAdapter (validates external safe boundary / atomic exit evidence)
- Deterministic InterruptionPolicy (chiyo.life.interruption_policy.v1, 0 LLM)
  producing InterruptionAssessment with independent activity_effect and
  communication_availability.
- Persistent PendingInterruption store with revision protection
  (STALE_PENDING_INTERRUPT), multi-event dedupe/merge/supersede/coexistence,
  and causal ordering (never queue arrival order).
- Safe Boundary handling routing all Activity PAUSE mutations exclusively through
  LR-2 ActivityCommandService.
- Crash-safe recovery & 5-stage failure injection support.
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
    ActivityCommandService,
    ActivityError,
    ActivityReadService,
    ActivityTransitionError,
    CanonicalActivityStore,
    CommandResult,
    ContractViolationError,
    GENESIS_HASH,
    GLOBAL_SUBJECT_ID,
    LIFE_STATE_ACTIVE,
    LIFE_STATE_IDLE,
    LIFE_STATE_PAUSED,
    LIFE_STATE_WAITING,
    NAMESPACE_CANONICAL,
    OPEN_ACTIVITY_STATUSES,
    RecoveryRequiredError,
    RevisionConflictError,
    STATUS_ACTIVE,
    STATUS_PAUSED,
    STATUS_WAITING,
    SubjectIdentityError,
    WriterCapability,
    WriterCapabilityError,
    _fsync_dir,
    _iso,
    _parse,
    _validate_structured_ref,
    build_life_frame_read_view,
    canonical_json_line,
    is_production_path,
    issue_writer_capability,
    normalize_ref_list,
    now,
    sha256_hex,
    validate_subject_id,
)


def validate_source_refs(
    refs: Optional[Sequence[str]],
    *,
    field_name: str = "source_refs",
    allow_empty: bool = False,
) -> list[str]:
    return normalize_ref_list(refs if refs is not None else ([] if allow_empty else None), field_name, allow_empty=allow_empty)


def validate_timestamps(
    *,
    occurred_at: Optional[str] = None,
    observed_at: Optional[str] = None,
    recorded_at: Optional[str] = None,
    enqueued_at: Optional[str] = None,
) -> dict[str, str]:
    rec_dt = _parse(recorded_at) if recorded_at else now()
    if recorded_at and rec_dt is None:
        raise ContractViolationError(f"invalid ISO-8601 recorded_at={recorded_at!r}")
    rec_iso = _iso(rec_dt or now())

    occ_dt = _parse(occurred_at) if occurred_at else (rec_dt or now())
    if occurred_at and occ_dt is None:
        raise ContractViolationError(f"invalid ISO-8601 occurred_at={occurred_at!r}")
    occ_iso = _iso(occ_dt or now())

    obs_dt = _parse(observed_at) if observed_at else occ_dt
    if observed_at and obs_dt is None:
        raise ContractViolationError(f"invalid ISO-8601 observed_at={observed_at!r}")
    obs_iso = _iso(obs_dt or now())

    enq_dt = _parse(enqueued_at) if enqueued_at else obs_dt
    if enqueued_at and enq_dt is None:
        raise ContractViolationError(f"invalid ISO-8601 enqueued_at={enqueued_at!r}")
    enq_iso = _iso(enq_dt or now())

    return {
        "occurred_at": occ_iso,
        "observed_at": obs_iso,
        "recorded_at": rec_iso,
        "enqueued_at": enq_iso,
    }

# ---------------------------------------------------------------------------
# Frozen Constants & Schemas (LR-3)
# ---------------------------------------------------------------------------

POLICY_VERSION_V1 = "chiyo.life.interruption_policy.v1"

SCHEMA_NARROW_ATTENTION_PROFILE = "chiyo.life.narrow_attention_profile.v1"
SCHEMA_ATOMIC_REGION_STATE = "chiyo.life.atomic_region_state.v1"
SCHEMA_COMMUNICATION_EVENT = "chiyo.life.communication_event.v1"
SCHEMA_OBSERVED_WORLD_URGENCY = "chiyo.life.observed_world_urgency.v1"
SCHEMA_BODY_CAPACITY_CONSTRAINT = "chiyo.life.body_capacity_constraint.v1"
SCHEMA_BOUNDARY_REACHED_EVENT = "chiyo.life.boundary_reached_event.v1"
SCHEMA_INTERRUPTION_ASSESSMENT = "chiyo.life.interruption_assessment.v1"
SCHEMA_PENDING_INTERRUPTION = "chiyo.life.pending_interruption.v1"
SCHEMA_INTERRUPTION_STATE = "chiyo.life.interruption_state.v1"
SCHEMA_INTERRUPTION_LOG_ENTRY = "chiyo.life.interruption_log.v1"

# Section 3: Narrow Attention Occupancy
OCCUPANCY_FREE = "FREE"
OCCUPANCY_LIGHT = "LIGHT"
OCCUPANCY_FOCUSED = "FOCUSED"
OCCUPANCY_ATOMIC = "ATOMIC"
ATTENTION_OCCUPANCY_LEVELS = (
    OCCUPANCY_FREE,
    OCCUPANCY_LIGHT,
    OCCUPANCY_FOCUSED,
    OCCUPANCY_ATOMIC,
)

# Section 5: Interruptibility
INTERRUPTIBILITY_IMMEDIATE = "IMMEDIATE"
INTERRUPTIBILITY_SAFE_BOUNDARY = "SAFE_BOUNDARY"
INTERRUPTIBILITY_ATOMIC = "ATOMIC"
INTERRUPTIBILITY_MODES = (
    INTERRUPTIBILITY_IMMEDIATE,
    INTERRUPTIBILITY_SAFE_BOUNDARY,
    INTERRUPTIBILITY_ATOMIC,
)

# Section 6: Legal (AttentionOccupancy, Interruptibility) combinations
VALID_ATTENTION_COMBINATIONS = frozenset(
    {
        (OCCUPANCY_FREE, INTERRUPTIBILITY_IMMEDIATE),
        (OCCUPANCY_FREE, INTERRUPTIBILITY_SAFE_BOUNDARY),
        (OCCUPANCY_LIGHT, INTERRUPTIBILITY_IMMEDIATE),
        (OCCUPANCY_LIGHT, INTERRUPTIBILITY_SAFE_BOUNDARY),
        (OCCUPANCY_FOCUSED, INTERRUPTIBILITY_IMMEDIATE),
        (OCCUPANCY_FOCUSED, INTERRUPTIBILITY_SAFE_BOUNDARY),
        (OCCUPANCY_ATOMIC, INTERRUPTIBILITY_ATOMIC),
    }
)

# Section 3: Short-lived constraint for ATOMIC region (max 5 minutes, never 2 hours)
MAX_ATOMIC_REGION_DURATION_SECONDS = 300

# Section 4: Forbidden Psychological / Perceptual Attention fields in LR-3
FORBIDDEN_PSYCHOLOGICAL_ATTENTION_KEYS = frozenset(
    {
        "visual_focus",
        "thought_focus",
        "salience_map",
        "semantic_attention",
        "perception_awareness",
        "auditory_focus",
        "global_salience",
        "attention_ranking",
        "belief_update",
        "emotional_rationale",
    }
)

# Section 11: Activity Effect
EFFECT_NO_INTERRUPT = "NO_INTERRUPT"
EFFECT_LIGHT_CONTACT = "LIGHT_CONTACT"
EFFECT_PAUSE_AT_BOUNDARY = "PAUSE_AT_BOUNDARY"
EFFECT_INTERRUPT_NOW = "INTERRUPT_NOW"
ACTIVITY_EFFECTS = (
    EFFECT_NO_INTERRUPT,
    EFFECT_LIGHT_CONTACT,
    EFFECT_PAUSE_AT_BOUNDARY,
    EFFECT_INTERRUPT_NOW,
)

FORBIDDEN_REPLY_OR_ACTION_EFFECTS = frozenset(
    {
        "SEND_REPLY",
        "IGNORE_USER",
        "PROACTIVE_MESSAGE",
        "FORCE_ABORT_ACTION",
        "START_CHATTING",
        "AUTO_RESUME",
    }
)

# Section 12: Communication Availability (strictly distinct from whether to reply)
COMM_AVAILABILITY_OPEN = "OPEN"
COMM_AVAILABILITY_LIMITED = "LIMITED"
COMM_AVAILABILITY_BOUNDARY_DEFERRED = "BOUNDARY_DEFERRED"
COMMUNICATION_AVAILABILITY_MODES = (
    COMM_AVAILABILITY_OPEN,
    COMM_AVAILABILITY_LIMITED,
    COMM_AVAILABILITY_BOUNDARY_DEFERRED,
)

# Section 15: PendingInterruption statuses
PENDING_STATUS_PENDING = "PENDING"
PENDING_STATUS_READY_AT_BOUNDARY = "READY_AT_BOUNDARY"
PENDING_STATUS_APPLIED = "APPLIED"
PENDING_STATUS_SUPERSEDED = "SUPERSEDED"
PENDING_STATUS_CANCELLED = "CANCELLED"
PENDING_STATUS_EXPIRED = "EXPIRED"

OPEN_PENDING_STATUSES = (
    PENDING_STATUS_PENDING,
    PENDING_STATUS_READY_AT_BOUNDARY,
)
TERMINAL_PENDING_STATUSES = (
    PENDING_STATUS_APPLIED,
    PENDING_STATUS_SUPERSEDED,
    PENDING_STATUS_CANCELLED,
    PENDING_STATUS_EXPIRED,
)
ALL_PENDING_STATUSES = OPEN_PENDING_STATUSES + TERMINAL_PENDING_STATUSES

# Event Classes & Urgency Levels
EVENT_KIND_COMMUNICATION = "COMMUNICATION"
EVENT_KIND_OBSERVED_WORLD_URGENCY = "OBSERVED_WORLD_URGENCY"
EVENT_KIND_BODY_CONSTRAINT = "BODY_CONSTRAINT"

MESSAGE_CLASS_ORDINARY = "ORDINARY"
MESSAGE_CLASS_EXPLICIT_HOLD_REQUEST = "EXPLICIT_HOLD_REQUEST"
VALID_MESSAGE_CLASSES = (
    MESSAGE_CLASS_ORDINARY,
    MESSAGE_CLASS_EXPLICIT_HOLD_REQUEST,
)

WORLD_URGENCY_LOW = "LOW"
WORLD_URGENCY_MODERATE = "MODERATE"
WORLD_URGENCY_HIGH = "HIGH"
WORLD_URGENCY_CRITICAL = "CRITICAL"
WORLD_URGENCY_LEVELS = (
    WORLD_URGENCY_LOW,
    WORLD_URGENCY_MODERATE,
    WORLD_URGENCY_HIGH,
    WORLD_URGENCY_CRITICAL,
)
WORLD_URGENCY_RANK = {
    WORLD_URGENCY_LOW: 1,
    WORLD_URGENCY_MODERATE: 2,
    WORLD_URGENCY_HIGH: 3,
    WORLD_URGENCY_CRITICAL: 4,
}

BODY_CAPACITY_NORMAL = "NORMAL"
BODY_CAPACITY_REDUCED = "REDUCED"
BODY_CAPACITY_DEPLETED = "DEPLETED"
BODY_CAPACITY_COLLAPSED = "COLLAPSED"
BODY_CAPACITY_LEVELS = (
    BODY_CAPACITY_NORMAL,
    BODY_CAPACITY_REDUCED,
    BODY_CAPACITY_DEPLETED,
    BODY_CAPACITY_COLLAPSED,
)

CONTINUATION_VIABLE = "VIABLE"
CONTINUATION_STRAINED = "STRAINED"
CONTINUATION_NOT_VIABLE = "NOT_VIABLE"
CONTINUATION_FEASIBILITY_LEVELS = (
    CONTINUATION_VIABLE,
    CONTINUATION_STRAINED,
    CONTINUATION_NOT_VIABLE,
)

BOUNDARY_KIND_SAFE_BOUNDARY = "SAFE_BOUNDARY"
BOUNDARY_KIND_ATOMIC_EXIT = "ATOMIC_EXIT"
VALID_BOUNDARY_KINDS = (
    BOUNDARY_KIND_SAFE_BOUNDARY,
    BOUNDARY_KIND_ATOMIC_EXIT,
)

VALID_OBSERVATION_PREFIXES = frozenset({"obs", "percept_obs", "world_obs", "observed"})
VALID_BOUNDARY_REF_PREFIXES = frozenset({"boundary", "safe_boundary", "atomic_exit", "bnd"})
VALID_BOUNDARY_SOURCE_PREFIXES = frozenset(
    {
        "fixture_boundary",
        "action_receipt",
        "act_receipt",
        "artifact_rev",
        "art_rev",
        "step_boundary",
        "executor_boundary",
        "boundary_adapter",
    }
)
FORBIDDEN_BOUNDARY_SOURCE_PREFIXES = frozenset(
    {
        "llm_text",
        "fake",
        "unverified",
        "guess",
        "timer_guess",
        "chat_text",
    }
)

INTERRUPTION_STATE_FILENAME = "interruption_state.json"
INTERRUPTION_LOG_FILENAME = "interruption_log.jsonl"


# ---------------------------------------------------------------------------
# Specific Exceptions (LR-3)
# ---------------------------------------------------------------------------


class InvalidAttentionOccupancyError(ContractViolationError):
    """Raised when an attention_occupancy value is invalid or psychological."""


class InvalidInterruptibilityError(ContractViolationError):
    """Raised when an interruptibility mode is invalid."""


class InvalidAttentionCombinationError(ContractViolationError):
    """Raised when (attention_occupancy, interruptibility) is an illegal pair."""


class UnobservedWorldInputError(ContractViolationError):
    """Raised when an unobserved/hidden world event attempts to enter LR-3."""


class BodyAuthorityViolationError(ContractViolationError):
    """Raised when Body input attempts to select an Activity or directly mutate status."""


class InvalidBoundaryEvidenceError(ContractViolationError):
    """Raised when a BoundaryReachedEvent lacks valid external boundary evidence."""


class BoundaryMismatchError(ActivityTransitionError):
    """Raised when a BoundaryReachedEvent targets the wrong activity or atomic region."""


class StalePendingInterruptionError(ActivityTransitionError):
    """Raised when a PendingInterruption targets an outdated Activity revision."""


# ---------------------------------------------------------------------------
# Narrow Attention & Interruptibility Models (Sections 3, 4, 5, 6)
# ---------------------------------------------------------------------------


def validate_no_psychological_attention(payload: Mapping[str, Any]) -> None:
    forbidden = FORBIDDEN_PSYCHOLOGICAL_ATTENTION_KEYS.intersection(payload.keys())
    if forbidden:
        raise InvalidAttentionOccupancyError(
            f"LR-3 Narrow Attention is strictly activity-occupancy based and forbids "
            f"psychological/perceptual fields: {sorted(forbidden)}"
        )


def validate_attention_occupancy(value: Any) -> str:
    if not isinstance(value, str) or value not in ATTENTION_OCCUPANCY_LEVELS:
        raise InvalidAttentionOccupancyError(
            f"attention_occupancy must be one of {ATTENTION_OCCUPANCY_LEVELS}, got {value!r}"
        )
    return value


def validate_interruptibility_mode(value: Any) -> str:
    if not isinstance(value, str) or value not in INTERRUPTIBILITY_MODES:
        raise InvalidInterruptibilityError(
            f"interruptibility must be one of {INTERRUPTIBILITY_MODES}, got {value!r}"
        )
    return value


def validate_attention_and_interruptibility(
    attention_occupancy: Any,
    interruptibility: Any,
    *,
    atomic_region_ref: Optional[str] = None,
) -> tuple[str, str]:
    occ = validate_attention_occupancy(attention_occupancy)
    intr = validate_interruptibility_mode(interruptibility)
    if (occ, intr) not in VALID_ATTENTION_COMBINATIONS:
        raise InvalidAttentionCombinationError(
            f"invalid (attention_occupancy={occ!r}, interruptibility={intr!r}) combination; "
            f"allowed combinations: {sorted(VALID_ATTENTION_COMBINATIONS)}"
        )
    if occ == OCCUPANCY_ATOMIC and not atomic_region_ref:
        raise InvalidAttentionCombinationError(
            "ATOMIC occupancy + ATOMIC interruptibility requires a valid short-lived atomic_region_ref"
        )
    return occ, intr


@dataclass(frozen=True)
class AtomicRegionState:
    atomic_region_ref: str
    activity_id: str
    activity_revision: int
    entered_at: str
    max_duration_seconds: int = 60
    action_ref: Optional[str] = None
    active: bool = True
    reconciliation_status: str = "CONFIRMED_ACTIVE"

    def __post_init__(self) -> None:
        _validate_structured_ref(
            self.atomic_region_ref,
            "atomic_region_ref",
            allowed_prefixes=frozenset({"atomic_reg", "atomic_region", "atomic"}),
        )
        if not isinstance(self.activity_id, str) or not self.activity_id.startswith("actv_"):
            raise ContractViolationError(f"invalid activity_id={self.activity_id!r}")
        if isinstance(self.activity_revision, bool) or not isinstance(self.activity_revision, int) or self.activity_revision < 1:
            raise ContractViolationError(f"invalid activity_revision={self.activity_revision!r}")
        if (
            isinstance(self.max_duration_seconds, bool)
            or not isinstance(self.max_duration_seconds, int)
            or self.max_duration_seconds <= 0
            or self.max_duration_seconds > MAX_ATOMIC_REGION_DURATION_SECONDS
        ):
            raise InvalidAttentionOccupancyError(
                f"ATOMIC region must be short-lived (1..{MAX_ATOMIC_REGION_DURATION_SECONDS}s), "
                f"got max_duration_seconds={self.max_duration_seconds!r}"
            )
        if self.action_ref is not None:
            _validate_structured_ref(self.action_ref, "action_ref")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_ATOMIC_REGION_STATE,
            "atomic_region_ref": self.atomic_region_ref,
            "activity_id": self.activity_id,
            "activity_revision": self.activity_revision,
            "entered_at": self.entered_at,
            "max_duration_seconds": self.max_duration_seconds,
            "action_ref": self.action_ref,
            "active": self.active,
            "reconciliation_status": self.reconciliation_status,
        }


@dataclass(frozen=True)
class NarrowAttentionProfile:
    activity_id: str
    attention_occupancy: str
    interruptibility: str
    atomic_region_ref: Optional[str] = None
    updated_at: str = field(default_factory=lambda: _iso(now()))

    def __post_init__(self) -> None:
        if not isinstance(self.activity_id, str) or not self.activity_id.startswith("actv_"):
            raise ContractViolationError(f"invalid activity_id={self.activity_id!r}")
        validate_attention_and_interruptibility(
            self.attention_occupancy,
            self.interruptibility,
            atomic_region_ref=self.atomic_region_ref,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_NARROW_ATTENTION_PROFILE,
            "activity_id": self.activity_id,
            "attention_occupancy": self.attention_occupancy,
            "interruptibility": self.interruptibility,
            "atomic_region_ref": self.atomic_region_ref,
            "updated_at": self.updated_at,
        }


# ---------------------------------------------------------------------------
# Event & Constraint Input Adapters (Sections 7, 8, 9, 16, 37)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CommunicationEvent:
    event_id: str
    subject_id: str
    channel_ref: str
    message_ref: str
    occurred_at: str
    observed_at: str
    recorded_at: str
    enqueued_at: str
    causal_parent_refs: tuple[str, ...]
    source_refs: tuple[str, ...]
    conversation_ref: Optional[str] = None
    message_class: str = MESSAGE_CLASS_ORDINARY
    request_boundary_pause: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_COMMUNICATION_EVENT,
            "event_kind": EVENT_KIND_COMMUNICATION,
            "event_id": self.event_id,
            "subject_id": self.subject_id,
            "channel_ref": self.channel_ref,
            "conversation_ref": self.conversation_ref,
            "message_ref": self.message_ref,
            "message_class": self.message_class,
            "request_boundary_pause": self.request_boundary_pause,
            "occurred_at": self.occurred_at,
            "observed_at": self.observed_at,
            "recorded_at": self.recorded_at,
            "enqueued_at": self.enqueued_at,
            "causal_parent_refs": list(self.causal_parent_refs),
            "source_refs": list(self.source_refs),
        }


class CommunicationEventAdapter:
    """Normalizes incoming channel payloads into CommunicationEvent (0 mutation)."""

    @staticmethod
    def adapt(payload: Mapping[str, Any]) -> CommunicationEvent:
        if not isinstance(payload, Mapping):
            raise ContractViolationError("communication payload must be a mapping")
        validate_no_psychological_attention(payload)

        # Forbid direct status mutation or activity replacement in communication adapter
        for forbidden_key in ("target_activity", "force_status", "auto_pause", "set_activity"):
            if forbidden_key in payload:
                raise ContractViolationError(
                    f"CommunicationEvent cannot directly mutate or select Activity ({forbidden_key!r})"
                )

        event_id = _validate_structured_ref(
            payload.get("event_id"),
            "event_id",
            allowed_prefixes=frozenset({"comm_evt", "evt", "msg_evt"}),
        )
        subject_id = validate_subject_id(payload.get("subject_id"))
        channel_ref = _validate_structured_ref(
            payload.get("channel_ref"),
            "channel_ref",
            allowed_prefixes=frozenset({"channel", "chan"}),
        )
        conv_raw = payload.get("conversation_ref")
        conversation_ref = (
            _validate_structured_ref(
                conv_raw,
                "conversation_ref",
                allowed_prefixes=frozenset({"conv", "conversation", "chat"}),
            )
            if conv_raw is not None
            else None
        )
        message_ref = _validate_structured_ref(
            payload.get("message_ref"),
            "message_ref",
            allowed_prefixes=frozenset({"msg", "message", "tg_msg", "qq_msg", "desk_msg"}),
        )
        message_class = payload.get("message_class", MESSAGE_CLASS_ORDINARY)
        if message_class not in VALID_MESSAGE_CLASSES:
            raise ContractViolationError(
                f"message_class must be one of {VALID_MESSAGE_CLASSES}, got {message_class!r}"
            )
        request_boundary_pause = bool(payload.get("request_boundary_pause", False))

        ts = validate_timestamps(
            occurred_at=payload.get("occurred_at"),
            observed_at=payload.get("observed_at"),
            recorded_at=payload.get("recorded_at"),
            enqueued_at=payload.get("enqueued_at"),
        )
        source_refs = validate_source_refs(
            payload.get("source_refs") or [channel_ref, message_ref],
            field_name="source_refs",
            allow_empty=False,
        )
        causal_parent_refs = validate_source_refs(
            payload.get("causal_parent_refs"),
            field_name="causal_parent_refs",
            allow_empty=True,
        )
        return CommunicationEvent(
            event_id=event_id,
            subject_id=subject_id,
            channel_ref=channel_ref,
            conversation_ref=conversation_ref,
            message_ref=message_ref,
            message_class=message_class,
            request_boundary_pause=request_boundary_pause,
            occurred_at=ts["occurred_at"],
            observed_at=ts["observed_at"],
            recorded_at=ts["recorded_at"],
            enqueued_at=ts["enqueued_at"],
            causal_parent_refs=tuple(causal_parent_refs),
            source_refs=tuple(source_refs),
        )


@dataclass(frozen=True)
class ObservedWorldUrgency:
    event_id: str
    subject_id: str
    observation_ref: str
    world_event_ref: str
    urgency_level: str
    requires_activity_pause: bool
    occurred_at: str
    observed_at: str
    recorded_at: str
    enqueued_at: str
    causal_parent_refs: tuple[str, ...]
    source_refs: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_OBSERVED_WORLD_URGENCY,
            "event_kind": EVENT_KIND_OBSERVED_WORLD_URGENCY,
            "event_id": self.event_id,
            "subject_id": self.subject_id,
            "observation_ref": self.observation_ref,
            "world_event_ref": self.world_event_ref,
            "urgency_level": self.urgency_level,
            "requires_activity_pause": self.requires_activity_pause,
            "occurred_at": self.occurred_at,
            "observed_at": self.observed_at,
            "recorded_at": self.recorded_at,
            "enqueued_at": self.enqueued_at,
            "causal_parent_refs": list(self.causal_parent_refs),
            "source_refs": list(self.source_refs),
        }


class ObservedWorldUrgencyAdapter:
    """Normalizes observed world urgency events while enforcing 'World knows X != Chiyo knows X'."""

    @staticmethod
    def adapt(payload: Mapping[str, Any]) -> ObservedWorldUrgency:
        if not isinstance(payload, Mapping):
            raise ContractViolationError("world urgency payload must be a mapping")
        validate_no_psychological_attention(payload)

        # G05: World state cannot select another Activity
        for forbidden_key in (
            "target_activity",
            "next_activity_kind",
            "select_activity",
            "auto_start_activity",
            "force_activity",
        ):
            if forbidden_key in payload:
                raise UnobservedWorldInputError(
                    f"World input cannot select or start another Activity ({forbidden_key!r})"
                )

        # G01: Unobserved / hidden world facts cannot enter LR-3
        if payload.get("observed") is False:
            raise UnobservedWorldInputError(
                "World knows X != Chiyo knows X: unobserved world event (observed=False) rejected"
            )
        visibility = payload.get("visibility", "observed")
        if visibility in ("hidden", "unobserved", "world_internal", "god_view"):
            raise UnobservedWorldInputError(
                f"World knows X != Chiyo knows X: world event with visibility={visibility!r} rejected"
            )
        source_type = payload.get("source_type", "observation")
        if source_type in ("world_authority_raw_scan", "world_menu_dump", "hidden_event_table"):
            raise UnobservedWorldInputError(
                f"Direct scan of World Authority ({source_type!r}) is forbidden in LR-3"
            )

        obs_raw = payload.get("observation_ref")
        if not obs_raw:
            raise UnobservedWorldInputError(
                "ObservedWorldUrgency requires a legal observation_ref proving Chiyo observed the event"
            )
        observation_ref = _validate_structured_ref(
            obs_raw,
            "observation_ref",
            allowed_prefixes=VALID_OBSERVATION_PREFIXES,
            error_cls=UnobservedWorldInputError,
        )

        event_id = _validate_structured_ref(
            payload.get("event_id"),
            "event_id",
            allowed_prefixes=frozenset({"world_urg", "evt", "obs_evt"}),
        )
        subject_id = validate_subject_id(payload.get("subject_id"))
        world_event_ref = _validate_structured_ref(
            payload.get("world_event_ref"),
            "world_event_ref",
            allowed_prefixes=frozenset({"world_evt", "world_event", "env_evt"}),
        )
        urgency_level = payload.get("urgency_level", WORLD_URGENCY_LOW)
        if urgency_level not in WORLD_URGENCY_LEVELS:
            raise ContractViolationError(
                f"urgency_level must be one of {WORLD_URGENCY_LEVELS}, got {urgency_level!r}"
            )
        requires_pause = bool(
            payload.get(
                "requires_activity_pause",
                urgency_level in (WORLD_URGENCY_HIGH, WORLD_URGENCY_CRITICAL),
            )
        )
        ts = validate_timestamps(
            occurred_at=payload.get("occurred_at"),
            observed_at=payload.get("observed_at"),
            recorded_at=payload.get("recorded_at"),
            enqueued_at=payload.get("enqueued_at"),
        )
        source_refs = validate_source_refs(
            payload.get("source_refs") or [observation_ref, world_event_ref],
            field_name="source_refs",
            allow_empty=False,
        )
        causal_parent_refs = validate_source_refs(
            payload.get("causal_parent_refs"),
            field_name="causal_parent_refs",
            allow_empty=True,
        )
        return ObservedWorldUrgency(
            event_id=event_id,
            subject_id=subject_id,
            observation_ref=observation_ref,
            world_event_ref=world_event_ref,
            urgency_level=urgency_level,
            requires_activity_pause=requires_pause,
            occurred_at=ts["occurred_at"],
            observed_at=ts["observed_at"],
            recorded_at=ts["recorded_at"],
            enqueued_at=ts["enqueued_at"],
            causal_parent_refs=tuple(causal_parent_refs),
            source_refs=tuple(source_refs),
        )


@dataclass(frozen=True)
class BodyCapacityConstraint:
    event_id: str
    subject_id: str
    body_capacity_ref: str
    capacity_level: str
    fatigue_load: float
    continuation_feasibility: str
    physical_constraint_code: str
    occurred_at: str
    observed_at: str
    recorded_at: str
    enqueued_at: str
    causal_parent_refs: tuple[str, ...]
    source_refs: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_BODY_CAPACITY_CONSTRAINT,
            "event_kind": EVENT_KIND_BODY_CONSTRAINT,
            "event_id": self.event_id,
            "subject_id": self.subject_id,
            "body_capacity_ref": self.body_capacity_ref,
            "capacity_level": self.capacity_level,
            "fatigue_load": self.fatigue_load,
            "continuation_feasibility": self.continuation_feasibility,
            "physical_constraint_code": self.physical_constraint_code,
            "occurred_at": self.occurred_at,
            "observed_at": self.observed_at,
            "recorded_at": self.recorded_at,
            "enqueued_at": self.enqueued_at,
            "causal_parent_refs": list(self.causal_parent_refs),
            "source_refs": list(self.source_refs),
        }


class BodyCapacityAdapter:
    """Normalizes Body capacity/constraint inputs; strictly forbids Activity selection or direct PAUSE."""

    @staticmethod
    def adapt(payload: Mapping[str, Any]) -> BodyCapacityConstraint:
        if not isinstance(payload, Mapping):
            raise ContractViolationError("body capacity payload must be a mapping")
        validate_no_psychological_attention(payload)

        # Section 9 & G03/G04: Body cannot select an Activity (e.g. sleep) or directly force PAUSED/action_ended
        for forbidden_key in (
            "target_activity",
            "activity_kind",
            "select_activity",
            "start_activity",
            "force_status",
            "direct_pause",
            "action_ended",
        ):
            if forbidden_key in payload:
                raise BodyAuthorityViolationError(
                    f"Body is a constraint source only, not an Activity owner; forbidden field {forbidden_key!r}"
                )

        event_id = _validate_structured_ref(
            payload.get("event_id"),
            "event_id",
            allowed_prefixes=frozenset({"body_evt", "evt", "cap_evt"}),
        )
        subject_id = validate_subject_id(payload.get("subject_id"))
        body_capacity_ref = _validate_structured_ref(
            payload.get("body_capacity_ref"),
            "body_capacity_ref",
            allowed_prefixes=frozenset({"body_cap", "body_state", "cap_ref"}),
        )
        capacity_level = payload.get("capacity_level", BODY_CAPACITY_NORMAL)
        if capacity_level not in BODY_CAPACITY_LEVELS:
            raise ContractViolationError(
                f"capacity_level must be one of {BODY_CAPACITY_LEVELS}, got {capacity_level!r}"
            )
        fatigue_raw = payload.get("fatigue_load", 0.0)
        if isinstance(fatigue_raw, bool) or not isinstance(fatigue_raw, (int, float)):
            raise ContractViolationError(f"fatigue_load must be a number in [0.0, 1.0], got {fatigue_raw!r}")
        fatigue_load = float(fatigue_raw)
        if fatigue_load < 0.0 or fatigue_load > 1.0:
            raise ContractViolationError(f"fatigue_load must be in [0.0, 1.0], got {fatigue_load}")

        feasibility = payload.get("continuation_feasibility", CONTINUATION_VIABLE)
        if feasibility not in CONTINUATION_FEASIBILITY_LEVELS:
            raise ContractViolationError(
                f"continuation_feasibility must be one of {CONTINUATION_FEASIBILITY_LEVELS}, got {feasibility!r}"
            )
        constraint_code = str(payload.get("physical_constraint_code") or "NONE").strip()
        if not constraint_code:
            raise ContractViolationError("physical_constraint_code must be non-empty")

        ts = validate_timestamps(
            occurred_at=payload.get("occurred_at"),
            observed_at=payload.get("observed_at"),
            recorded_at=payload.get("recorded_at"),
            enqueued_at=payload.get("enqueued_at"),
        )
        source_refs = validate_source_refs(
            payload.get("source_refs") or [body_capacity_ref],
            field_name="source_refs",
            allow_empty=False,
        )
        causal_parent_refs = validate_source_refs(
            payload.get("causal_parent_refs"),
            field_name="causal_parent_refs",
            allow_empty=True,
        )
        return BodyCapacityConstraint(
            event_id=event_id,
            subject_id=subject_id,
            body_capacity_ref=body_capacity_ref,
            capacity_level=capacity_level,
            fatigue_load=fatigue_load,
            continuation_feasibility=feasibility,
            physical_constraint_code=constraint_code,
            occurred_at=ts["occurred_at"],
            observed_at=ts["observed_at"],
            recorded_at=ts["recorded_at"],
            enqueued_at=ts["enqueued_at"],
            causal_parent_refs=tuple(causal_parent_refs),
            source_refs=tuple(source_refs),
        )


@dataclass(frozen=True)
class BoundaryReachedEvent:
    boundary_ref: str
    activity_ref: str
    source_ref: str
    boundary_kind: str
    occurred_at: str
    observed_at: str
    recorded_at: str
    enqueued_at: str
    causal_parent_refs: tuple[str, ...]
    source_refs: tuple[str, ...]
    atomic_region_ref: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_BOUNDARY_REACHED_EVENT,
            "boundary_ref": self.boundary_ref,
            "activity_ref": self.activity_ref,
            "source_ref": self.source_ref,
            "boundary_kind": self.boundary_kind,
            "atomic_region_ref": self.atomic_region_ref,
            "occurred_at": self.occurred_at,
            "observed_at": self.observed_at,
            "recorded_at": self.recorded_at,
            "enqueued_at": self.enqueued_at,
            "causal_parent_refs": list(self.causal_parent_refs),
            "source_refs": list(self.source_refs),
        }


class BoundaryEventAdapter:
    """Normalizes and validates external BoundaryReachedEvent fixtures/evidence."""

    @staticmethod
    def adapt(payload: Mapping[str, Any]) -> BoundaryReachedEvent:
        if not isinstance(payload, Mapping):
            raise InvalidBoundaryEvidenceError("boundary event payload must be a mapping")

        if payload.get("verified_boundary") is False or payload.get("fabricated") is True:
            raise InvalidBoundaryEvidenceError("fake or unverified boundary evidence rejected")

        boundary_ref = _validate_structured_ref(
            payload.get("boundary_ref"),
            "boundary_ref",
            allowed_prefixes=VALID_BOUNDARY_REF_PREFIXES,
            error_cls=InvalidBoundaryEvidenceError,
        )
        activity_ref = payload.get("activity_ref")
        if not isinstance(activity_ref, str) or not activity_ref.startswith("actv_"):
            raise InvalidBoundaryEvidenceError(
                f"activity_ref must be a valid activity ID ('actv_*'), got {activity_ref!r}"
            )
        source_ref = _validate_structured_ref(
            payload.get("source_ref"),
            "source_ref",
            allowed_prefixes=VALID_BOUNDARY_SOURCE_PREFIXES,
            forbidden_prefixes=FORBIDDEN_BOUNDARY_SOURCE_PREFIXES,
            error_cls=InvalidBoundaryEvidenceError,
        )
        boundary_kind = payload.get("boundary_kind", BOUNDARY_KIND_SAFE_BOUNDARY)
        if boundary_kind not in VALID_BOUNDARY_KINDS:
            raise InvalidBoundaryEvidenceError(
                f"boundary_kind must be one of {VALID_BOUNDARY_KINDS}, got {boundary_kind!r}"
            )
        atomic_region_raw = payload.get("atomic_region_ref")
        atomic_region_ref = (
            _validate_structured_ref(
                atomic_region_raw,
                "atomic_region_ref",
                allowed_prefixes=frozenset({"atomic_reg", "atomic_region", "atomic"}),
                error_cls=InvalidBoundaryEvidenceError,
            )
            if atomic_region_raw is not None
            else None
        )
        ts = validate_timestamps(
            occurred_at=payload.get("occurred_at"),
            observed_at=payload.get("observed_at"),
            recorded_at=payload.get("recorded_at"),
            enqueued_at=payload.get("enqueued_at"),
        )
        source_refs = validate_source_refs(
            payload.get("source_refs") or [boundary_ref, source_ref],
            field_name="source_refs",
            allow_empty=False,
        )
        causal_parent_refs = validate_source_refs(
            payload.get("causal_parent_refs"),
            field_name="causal_parent_refs",
            allow_empty=True,
        )
        return BoundaryReachedEvent(
            boundary_ref=boundary_ref,
            activity_ref=activity_ref,
            source_ref=source_ref,
            boundary_kind=boundary_kind,
            atomic_region_ref=atomic_region_ref,
            occurred_at=ts["occurred_at"],
            observed_at=ts["observed_at"],
            recorded_at=ts["recorded_at"],
            enqueued_at=ts["enqueued_at"],
            causal_parent_refs=tuple(causal_parent_refs),
            source_refs=tuple(source_refs),
        )


# ---------------------------------------------------------------------------
# InterruptionAssessment & PendingInterruption Schemas (Sections 10, 14, 15)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class InterruptionAssessment:
    assessment_id: str
    activity_id: Optional[str]
    activity_revision: int
    source_event_refs: tuple[str, ...]
    attention_occupancy: str
    interruptibility: str
    activity_effect: str
    communication_availability: str
    reason_codes: tuple[str, ...]
    occurred_at: str
    recorded_at: str
    policy_version: str = POLICY_VERSION_V1
    observed_world_urgency: Optional[str] = None
    body_capacity_ref: Optional[str] = None
    atomic_region_ref: Optional[str] = None
    boundary_requirement: Optional[str] = None
    pending_interruption_ref: Optional[str] = None
    continuation_viable_after_boundary: bool = True
    applied_transition_ref: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_INTERRUPTION_ASSESSMENT,
            "assessment_id": self.assessment_id,
            "activity_id": self.activity_id,
            "activity_revision": self.activity_revision,
            "source_event_refs": list(self.source_event_refs),
            "attention_occupancy": self.attention_occupancy,
            "interruptibility": self.interruptibility,
            "observed_world_urgency": self.observed_world_urgency,
            "body_capacity_ref": self.body_capacity_ref,
            "atomic_region_ref": self.atomic_region_ref,
            "activity_effect": self.activity_effect,
            "communication_availability": self.communication_availability,
            "boundary_requirement": self.boundary_requirement,
            "pending_interruption_ref": self.pending_interruption_ref,
            "continuation_viable_after_boundary": self.continuation_viable_after_boundary,
            "applied_transition_ref": self.applied_transition_ref,
            "reason_codes": list(self.reason_codes),
            "occurred_at": self.occurred_at,
            "recorded_at": self.recorded_at,
            "policy_version": self.policy_version,
        }


@dataclass
class PendingInterruption:
    pending_id: str
    activity_id: str
    activity_revision_at_request: int
    source_event_refs: list[str]
    source_kind: str
    requested_effect: str
    boundary_requirement: str
    urgency_rank: int
    created_at: str
    observed_at: str
    status: str = PENDING_STATUS_PENDING
    boundary_ref: Optional[str] = None
    expiry_ref: Optional[str] = None
    atomic_region_ref: Optional[str] = None
    continuation_viable_after_boundary: bool = True
    last_evaluated_at: Optional[str] = None
    superseded_by_ref: Optional[str] = None
    resolution_reason_code: Optional[str] = None
    applied_transition_ref: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_PENDING_INTERRUPTION,
            "pending_id": self.pending_id,
            "activity_id": self.activity_id,
            "activity_revision_at_request": self.activity_revision_at_request,
            "source_event_refs": list(self.source_event_refs),
            "source_kind": self.source_kind,
            "requested_effect": self.requested_effect,
            "boundary_requirement": self.boundary_requirement,
            "urgency_rank": self.urgency_rank,
            "created_at": self.created_at,
            "observed_at": self.observed_at,
            "boundary_ref": self.boundary_ref,
            "expiry_ref": self.expiry_ref,
            "atomic_region_ref": self.atomic_region_ref,
            "continuation_viable_after_boundary": self.continuation_viable_after_boundary,
            "status": self.status,
            "last_evaluated_at": self.last_evaluated_at,
            "superseded_by_ref": self.superseded_by_ref,
            "resolution_reason_code": self.resolution_reason_code,
            "applied_transition_ref": self.applied_transition_ref,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "PendingInterruption":
        return cls(
            pending_id=str(data["pending_id"]),
            activity_id=str(data["activity_id"]),
            activity_revision_at_request=int(data["activity_revision_at_request"]),
            source_event_refs=list(data["source_event_refs"]),
            source_kind=str(data.get("source_kind", EVENT_KIND_COMMUNICATION)),
            requested_effect=str(data["requested_effect"]),
            boundary_requirement=str(data.get("boundary_requirement", BOUNDARY_KIND_SAFE_BOUNDARY)),
            urgency_rank=int(data.get("urgency_rank", 1)),
            created_at=str(data["created_at"]),
            observed_at=str(data["observed_at"]),
            status=str(data["status"]),
            boundary_ref=data.get("boundary_ref"),
            expiry_ref=data.get("expiry_ref"),
            atomic_region_ref=data.get("atomic_region_ref"),
            continuation_viable_after_boundary=bool(data.get("continuation_viable_after_boundary", True)),
            last_evaluated_at=data.get("last_evaluated_at"),
            superseded_by_ref=data.get("superseded_by_ref"),
            resolution_reason_code=data.get("resolution_reason_code"),
            applied_transition_ref=data.get("applied_transition_ref"),
        )


# ---------------------------------------------------------------------------
# Causal Event Ordering (Section 19: Never Queue Arrival Order)
# ---------------------------------------------------------------------------


UnionEvent = CommunicationEvent | ObservedWorldUrgency | BodyCapacityConstraint


def sort_events_causally(events: Sequence[UnionEvent]) -> list[UnionEvent]:
    """Order events strictly by causal parent graph + observed_at + occurred_at + event_id.

    NEVER uses queue arrival order or enqueued_at.
    """
    by_id: dict[str, UnionEvent] = {e.event_id: e for e in events}
    depth_Memo: dict[str, int] = {}

    def _causal_depth(eid: str, visiting: set[str]) -> int:
        if eid in depth_Memo:
            return depth_Memo[eid]
        if eid in visiting or eid not in by_id:
            return 0
        visiting.add(eid)
        ev = by_id[eid]
        d = 0
        for parent_ref in ev.causal_parent_refs:
            if parent_ref in by_id:
                d = max(d, 1 + _causal_depth(parent_ref, visiting))
        visiting.remove(eid)
        depth_Memo[eid] = d
        return d

    def _key(ev: UnionEvent) -> tuple[Any, ...]:
        obs_dt = _parse(ev.observed_at)
        occ_dt = _parse(ev.occurred_at)
        obs_ts = obs_dt.timestamp() if obs_dt else 0.0
        occ_ts = occ_dt.timestamp() if occ_dt else 0.0
        c_depth = _causal_depth(ev.event_id, set())
        # Note: enqueued_at and list index are intentionally excluded!
        return (obs_ts, c_depth, occ_ts, ev.event_id)

    return sorted(events, key=_key)


# ---------------------------------------------------------------------------
# Deterministic InterruptionPolicy (chiyo.life.interruption_policy.v1, 0 LLM)
# ---------------------------------------------------------------------------


class InterruptionPolicy:
    """Pure deterministic interruption policy engine (0 LLM calls, 0 side effects)."""

    policy_version = POLICY_VERSION_V1

    @classmethod
    def compute_communication_availability(
        cls,
        *,
        life_state: str,
        attention_occupancy: str,
        interruptibility: str,
    ) -> str:
        if life_state != LIFE_STATE_ACTIVE:
            return COMM_AVAILABILITY_OPEN
        if attention_occupancy == OCCUPANCY_ATOMIC or interruptibility == INTERRUPTIBILITY_ATOMIC:
            return COMM_AVAILABILITY_BOUNDARY_DEFERRED
        if attention_occupancy == OCCUPANCY_FOCUSED:
            return COMM_AVAILABILITY_LIMITED
        return COMM_AVAILABILITY_OPEN

    @classmethod
    def evaluate_event(
        cls,
        *,
        event: UnionEvent,
        life_state: str,
        current_activity: Optional[Mapping[str, Any]],
        attention_occupancy: str,
        interruptibility: str,
        atomic_region: Optional[AtomicRegionState] = None,
        recorded_at: Optional[str] = None,
    ) -> dict[str, Any]:
        """Deterministically evaluate a single normalized event against current Life & Attention state."""
        stamp = recorded_at or _iso(now())

        # Validate occupancy & interruptibility combination
        if life_state == LIFE_STATE_ACTIVE:
            validate_attention_and_interruptibility(
                attention_occupancy,
                interruptibility,
                atomic_region_ref=atomic_region.atomic_region_ref if atomic_region else None,
            )

        comm_availability = cls.compute_communication_availability(
            life_state=life_state,
            attention_occupancy=attention_occupancy,
            interruptibility=interruptibility,
        )

        # Case 1: IDLE or non-ACTIVE (PAUSED / WAITING) -> Never auto-start chatting, never auto-resume
        if life_state == LIFE_STATE_IDLE or current_activity is None:
            return {
                "activity_id": None,
                "activity_revision": 0,
                "attention_occupancy": OCCUPANCY_FREE,
                "interruptibility": INTERRUPTIBILITY_IMMEDIATE,
                "activity_effect": EFFECT_NO_INTERRUPT,
                "communication_availability": COMM_AVAILABILITY_OPEN,
                "boundary_requirement": None,
                "requires_pending": False,
                "urgency_rank": 0,
                "continuation_viable_after_boundary": True,
                "reason_codes": [
                    "NO_CURRENT_ACTIVITY",
                    cls._event_reason_code(event),
                ],
                "occurred_at": event.occurred_at,
                "recorded_at": stamp,
            }

        act_id = str(current_activity["activity_id"])
        act_rev = int(current_activity["revision"])
        act_status = str(current_activity["status"])

        if act_status in (STATUS_PAUSED, STATUS_WAITING):
            return {
                "activity_id": act_id,
                "activity_revision": act_rev,
                "attention_occupancy": OCCUPANCY_FREE,
                "interruptibility": INTERRUPTIBILITY_IMMEDIATE,
                "activity_effect": EFFECT_NO_INTERRUPT,
                "communication_availability": COMM_AVAILABILITY_OPEN,
                "boundary_requirement": None,
                "requires_pending": False,
                "urgency_rank": 0,
                "continuation_viable_after_boundary": True,
                "reason_codes": [
                    f"ACTIVITY_{act_status}_NO_AUTO_RESUME",
                    cls._event_reason_code(event),
                ],
                "occurred_at": event.occurred_at,
                "recorded_at": stamp,
            }

        # Current activity is ACTIVE
        # Case 2: ATOMIC occupancy / interruptibility (Sections 24, 25, 26, 51)
        if attention_occupancy == OCCUPANCY_ATOMIC or interruptibility == INTERRUPTIBILITY_ATOMIC:
            if isinstance(event, CommunicationEvent):
                if event.message_class == MESSAGE_CLASS_EXPLICIT_HOLD_REQUEST or event.request_boundary_pause:
                    return {
                        "activity_id": act_id,
                        "activity_revision": act_rev,
                        "attention_occupancy": OCCUPANCY_ATOMIC,
                        "interruptibility": INTERRUPTIBILITY_ATOMIC,
                        "activity_effect": EFFECT_PAUSE_AT_BOUNDARY,
                        "communication_availability": COMM_AVAILABILITY_BOUNDARY_DEFERRED,
                        "boundary_requirement": BOUNDARY_KIND_ATOMIC_EXIT,
                        "requires_pending": True,
                        "urgency_rank": 2,
                        "continuation_viable_after_boundary": True,
                        "reason_codes": [
                            "EXPLICIT_HOLD_COMMUNICATION",
                            "ATOMIC_REGION_ACTIVE",
                            "AWAIT_ATOMIC_EXIT_BOUNDARY",
                        ],
                        "occurred_at": event.occurred_at,
                        "recorded_at": stamp,
                    }
                # Normal user message during ATOMIC: foreground continues, no immediate mutation,
                # optional low-priority pending hold is NOT forced unless requested; wait:
                # Section 24 says: "Activity ACTIVE + atomic region active + incoming message ->
                # foreground continues, pending interruption may exist, no immediate pause, until BoundaryReachedEvent".
                return {
                    "activity_id": act_id,
                    "activity_revision": act_rev,
                    "attention_occupancy": OCCUPANCY_ATOMIC,
                    "interruptibility": INTERRUPTIBILITY_ATOMIC,
                    "activity_effect": EFFECT_NO_INTERRUPT,
                    "communication_availability": COMM_AVAILABILITY_BOUNDARY_DEFERRED,
                    "boundary_requirement": BOUNDARY_KIND_ATOMIC_EXIT,
                    "requires_pending": False,
                    "urgency_rank": 1,
                    "continuation_viable_after_boundary": True,
                    "reason_codes": [
                        "NORMAL_COMMUNICATION",
                        "ATOMIC_REGION_PROTECTED",
                        "NO_IMMEDIATE_PAUSE",
                    ],
                    "occurred_at": event.occurred_at,
                    "recorded_at": stamp,
                }

            if isinstance(event, ObservedWorldUrgency):
                rank = WORLD_URGENCY_RANK[event.urgency_level]
                if event.requires_activity_pause or rank >= WORLD_URGENCY_RANK[WORLD_URGENCY_HIGH]:
                    return {
                        "activity_id": act_id,
                        "activity_revision": act_rev,
                        "attention_occupancy": OCCUPANCY_ATOMIC,
                        "interruptibility": INTERRUPTIBILITY_ATOMIC,
                        "activity_effect": EFFECT_PAUSE_AT_BOUNDARY,
                        "communication_availability": COMM_AVAILABILITY_BOUNDARY_DEFERRED,
                        "boundary_requirement": BOUNDARY_KIND_ATOMIC_EXIT,
                        "requires_pending": True,
                        "urgency_rank": 10 + rank,
                        "continuation_viable_after_boundary": True,
                        "reason_codes": [
                            f"OBSERVED_WORLD_URGENCY_{event.urgency_level}",
                            "ATOMIC_REGION_ACTIVE",
                            "NO_FORCE_ABORT_AWAIT_BOUNDARY",
                        ],
                        "occurred_at": event.occurred_at,
                        "recorded_at": stamp,
                    }
                return {
                    "activity_id": act_id,
                    "activity_revision": act_rev,
                    "attention_occupancy": OCCUPANCY_ATOMIC,
                    "interruptibility": INTERRUPTIBILITY_ATOMIC,
                    "activity_effect": EFFECT_NO_INTERRUPT,
                    "communication_availability": COMM_AVAILABILITY_BOUNDARY_DEFERRED,
                    "boundary_requirement": None,
                    "requires_pending": False,
                    "urgency_rank": rank,
                    "continuation_viable_after_boundary": True,
                    "reason_codes": [
                        f"OBSERVED_WORLD_URGENCY_{event.urgency_level}",
                        "ATOMIC_REGION_PROTECTED",
                    ],
                    "occurred_at": event.occurred_at,
                    "recorded_at": stamp,
                }

            if isinstance(event, BodyCapacityConstraint):
                viable_after = event.continuation_feasibility != CONTINUATION_NOT_VIABLE
                needs_pending = event.continuation_feasibility in (
                    CONTINUATION_STRAINED,
                    CONTINUATION_NOT_VIABLE,
                )
                return {
                    "activity_id": act_id,
                    "activity_revision": act_rev,
                    "attention_occupancy": OCCUPANCY_ATOMIC,
                    "interruptibility": INTERRUPTIBILITY_ATOMIC,
                    "activity_effect": (
                        EFFECT_PAUSE_AT_BOUNDARY if needs_pending else EFFECT_NO_INTERRUPT
                    ),
                    "communication_availability": COMM_AVAILABILITY_BOUNDARY_DEFERRED,
                    "boundary_requirement": BOUNDARY_KIND_ATOMIC_EXIT if needs_pending else None,
                    "requires_pending": needs_pending,
                    "urgency_rank": 8 if not viable_after else 4,
                    "continuation_viable_after_boundary": viable_after,
                    "reason_codes": [
                        f"BODY_CAPACITY_{event.capacity_level}",
                        f"CONTINUATION_{event.continuation_feasibility}",
                        "ATOMIC_REGION_ACTIVE",
                        "REASSESS_AT_ATOMIC_BOUNDARY",
                    ],
                    "occurred_at": event.occurred_at,
                    "recorded_at": stamp,
                }

        # Case 3: Non-ATOMIC ACTIVE activity (FREE, LIGHT, FOCUSED) with IMMEDIATE or SAFE_BOUNDARY
        if isinstance(event, CommunicationEvent):
            # Explicit hold request or boundary pause request on SAFE_BOUNDARY / IMMEDIATE
            if event.message_class == MESSAGE_CLASS_EXPLICIT_HOLD_REQUEST or event.request_boundary_pause:
                if interruptibility == INTERRUPTIBILITY_SAFE_BOUNDARY:
                    return {
                        "activity_id": act_id,
                        "activity_revision": act_rev,
                        "attention_occupancy": attention_occupancy,
                        "interruptibility": interruptibility,
                        "activity_effect": EFFECT_PAUSE_AT_BOUNDARY,
                        "communication_availability": comm_availability,
                        "boundary_requirement": BOUNDARY_KIND_SAFE_BOUNDARY,
                        "requires_pending": True,
                        "urgency_rank": 3,
                        "continuation_viable_after_boundary": True,
                        "reason_codes": [
                            "COMMUNICATION_BOUNDARY_HOLD_REQUEST",
                            f"FOREGROUND_{attention_occupancy}",
                            "SAFE_BOUNDARY_REQUIRED",
                        ],
                        "occurred_at": event.occurred_at,
                        "recorded_at": stamp,
                    }
                return {
                    "activity_id": act_id,
                    "activity_revision": act_rev,
                    "attention_occupancy": attention_occupancy,
                    "interruptibility": interruptibility,
                    "activity_effect": EFFECT_INTERRUPT_NOW,
                    "communication_availability": comm_availability,
                    "boundary_requirement": None,
                    "requires_pending": False,
                    "urgency_rank": 3,
                    "continuation_viable_after_boundary": True,
                    "reason_codes": [
                        "EXPLICIT_HOLD_COMMUNICATION",
                        f"FOREGROUND_{attention_occupancy}",
                        "IMMEDIATE_PAUSE_PERMITTED",
                    ],
                    "occurred_at": event.occurred_at,
                    "recorded_at": stamp,
                }

            # Ordinary user message (Section 22, 23, 46 C01..C04):
            # NEVER forces PAUSE or INTERRUPT_NOW!
            if attention_occupancy in (OCCUPANCY_FREE, OCCUPANCY_LIGHT):
                return {
                    "activity_id": act_id,
                    "activity_revision": act_rev,
                    "attention_occupancy": attention_occupancy,
                    "interruptibility": interruptibility,
                    "activity_effect": EFFECT_LIGHT_CONTACT,
                    "communication_availability": COMM_AVAILABILITY_OPEN,
                    "boundary_requirement": None,
                    "requires_pending": False,
                    "urgency_rank": 1,
                    "continuation_viable_after_boundary": True,
                    "reason_codes": [
                        "NORMAL_COMMUNICATION",
                        f"FOREGROUND_{attention_occupancy}",
                        "SIDE_INTERACTION_ALLOWED",
                    ],
                    "occurred_at": event.occurred_at,
                    "recorded_at": stamp,
                }

            # FOCUSED + ordinary user message (Section 23 & C03):
            # Allows brief side contact (LIGHT_CONTACT) with LIMITED availability,
            # never blind INTERRUPT_NOW.
            return {
                "activity_id": act_id,
                "activity_revision": act_rev,
                "attention_occupancy": attention_occupancy,
                "interruptibility": interruptibility,
                "activity_effect": EFFECT_LIGHT_CONTACT,
                "communication_availability": COMM_AVAILABILITY_LIMITED,
                "boundary_requirement": None,
                "requires_pending": False,
                "urgency_rank": 1,
                "continuation_viable_after_boundary": True,
                "reason_codes": [
                    "NORMAL_COMMUNICATION",
                    "FOREGROUND_FOCUSED",
                    "NO_BLIND_INTERRUPT",
                ],
                "occurred_at": event.occurred_at,
                "recorded_at": stamp,
            }

        if isinstance(event, ObservedWorldUrgency):
            rank = WORLD_URGENCY_RANK[event.urgency_level]
            if not event.requires_activity_pause and rank <= WORLD_URGENCY_RANK[WORLD_URGENCY_MODERATE]:
                effect = (
                    EFFECT_LIGHT_CONTACT
                    if attention_occupancy in (OCCUPANCY_FREE, OCCUPANCY_LIGHT)
                    else EFFECT_NO_INTERRUPT
                )
                return {
                    "activity_id": act_id,
                    "activity_revision": act_rev,
                    "attention_occupancy": attention_occupancy,
                    "interruptibility": interruptibility,
                    "activity_effect": effect,
                    "communication_availability": comm_availability,
                    "boundary_requirement": None,
                    "requires_pending": False,
                    "urgency_rank": rank,
                    "continuation_viable_after_boundary": True,
                    "reason_codes": [
                        f"OBSERVED_WORLD_URGENCY_{event.urgency_level}",
                        f"FOREGROUND_{attention_occupancy}",
                        "NO_PAUSE_REQUIRED",
                    ],
                    "occurred_at": event.occurred_at,
                    "recorded_at": stamp,
                }

            # HIGH / CRITICAL or requires_activity_pause == True
            if interruptibility == INTERRUPTIBILITY_SAFE_BOUNDARY:
                return {
                    "activity_id": act_id,
                    "activity_revision": act_rev,
                    "attention_occupancy": attention_occupancy,
                    "interruptibility": interruptibility,
                    "activity_effect": EFFECT_PAUSE_AT_BOUNDARY,
                    "communication_availability": comm_availability,
                    "boundary_requirement": BOUNDARY_KIND_SAFE_BOUNDARY,
                    "requires_pending": True,
                    "urgency_rank": 10 + rank,
                    "continuation_viable_after_boundary": True,
                    "reason_codes": [
                        f"OBSERVED_WORLD_URGENCY_{event.urgency_level}",
                        f"FOREGROUND_{attention_occupancy}",
                        "SAFE_BOUNDARY_REQUIRED",
                    ],
                    "occurred_at": event.occurred_at,
                    "recorded_at": stamp,
                }

            return {
                "activity_id": act_id,
                "activity_revision": act_rev,
                "attention_occupancy": attention_occupancy,
                "interruptibility": interruptibility,
                "activity_effect": EFFECT_INTERRUPT_NOW,
                "communication_availability": comm_availability,
                "boundary_requirement": None,
                "requires_pending": False,
                "urgency_rank": 10 + rank,
                "continuation_viable_after_boundary": True,
                "reason_codes": [
                    f"OBSERVED_WORLD_URGENCY_{event.urgency_level}",
                    f"FOREGROUND_{attention_occupancy}",
                    "IMMEDIATE_INTERRUPT_ELIGIBLE",
                ],
                "occurred_at": event.occurred_at,
                "recorded_at": stamp,
            }

        if isinstance(event, BodyCapacityConstraint):
            # Section 9 & G03: Body constraint can NEVER directly force INTERRUPT_NOW -> PAUSE!
            # It only records continuation feasibility and (if STRAINED/NOT_VIABLE) schedules
            # a boundary reassessment / PendingInterruption.
            viable_after = event.continuation_feasibility != CONTINUATION_NOT_VIABLE
            needs_pending = event.continuation_feasibility in (
                CONTINUATION_STRAINED,
                CONTINUATION_NOT_VIABLE,
            )
            return {
                "activity_id": act_id,
                "activity_revision": act_rev,
                "attention_occupancy": attention_occupancy,
                "interruptibility": interruptibility,
                "activity_effect": (
                    EFFECT_PAUSE_AT_BOUNDARY if needs_pending else EFFECT_NO_INTERRUPT
                ),
                "communication_availability": comm_availability,
                "boundary_requirement": BOUNDARY_KIND_SAFE_BOUNDARY if needs_pending else None,
                "requires_pending": needs_pending,
                "urgency_rank": 8 if not viable_after else 4,
                "continuation_viable_after_boundary": viable_after,
                "reason_codes": [
                    f"BODY_CAPACITY_{event.capacity_level}",
                    f"CONTINUATION_{event.continuation_feasibility}",
                    "BODY_CONSTRAINT_ONLY_NO_DIRECT_PAUSE",
                ],
                "occurred_at": event.occurred_at,
                "recorded_at": stamp,
            }

        raise ContractViolationError(f"unsupported event type: {type(event)!r}")

    @staticmethod
    def _event_reason_code(event: UnionEvent) -> str:
        if isinstance(event, CommunicationEvent):
            return (
                "NORMAL_COMMUNICATION"
                if event.message_class == MESSAGE_CLASS_ORDINARY
                else "EXPLICIT_HOLD_COMMUNICATION"
            )
        if isinstance(event, ObservedWorldUrgency):
            return f"OBSERVED_WORLD_URGENCY_{event.urgency_level}"
        if isinstance(event, BodyCapacityConstraint):
            return f"BODY_CAPACITY_{event.capacity_level}"
        return "EXTERNAL_EVENT"


# ---------------------------------------------------------------------------
# Persistent Interruption State Store (Sections 14, 34, 35, 54)
# ---------------------------------------------------------------------------


def _empty_interruption_state(at_iso: Optional[str] = None) -> dict[str, Any]:
    stamp = at_iso or _iso(now())
    return {
        "schema_version": SCHEMA_INTERRUPTION_STATE,
        "subject_id": GLOBAL_SUBJECT_ID,
        "revision": 0,
        "updated_at": stamp,
        "attention_profiles": {},
        "atomic_regions": {},
        "pending_interruptions": {},
        "assessments": {},
        "processed_events": {},
        "applied_boundaries": {},
    }


class InterruptionStateStore:
    """Crash-safe persistent store for Narrow Attention profiles, AtomicRegions, Assessments & Pendings."""

    def __init__(self, store_root: Path | str):
        self.store_root = Path(store_root).expanduser().resolve()
        self.state_path = self.store_root / INTERRUPTION_STATE_FILENAME
        self.log_path = self.store_root / INTERRUPTION_LOG_FILENAME

    def _cleanup_stray_temp_files(self) -> None:
        if not self.store_root.exists():
            return
        for item in self.store_root.iterdir():
            if item.is_file() and (
                item.name.startswith(f".{INTERRUPTION_STATE_FILENAME}.tmp.")
                or item.name.startswith(f".{INTERRUPTION_LOG_FILENAME}.tmp.")
            ):
                try:
                    item.unlink(missing_ok=True)
                except OSError:
                    pass

    def load_state(self) -> dict[str, Any]:
        self._cleanup_stray_temp_files()
        if not self.state_path.exists():
            return _empty_interruption_state()
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise RecoveryRequiredError(
                "CORRUPTED_INTERRUPTION_STATE",
                f"interruption_state.json is unreadable or invalid JSON: {exc}",
            ) from exc
        if not isinstance(data, dict) or data.get("schema_version") != SCHEMA_INTERRUPTION_STATE:
            raise RecoveryRequiredError(
                "INVALID_INTERRUPTION_STATE_SCHEMA",
                f"unsupported schema in interruption_state.json: {data!r}",
            )
        validate_subject_id(data.get("subject_id"))
        return copy.deepcopy(data)

    def save_state(
        self,
        state: dict[str, Any],
        *,
        log_entry: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        if is_production_path(self.store_root):
            raise WriterCapabilityError(
                f"Production cutover is forbidden in LR-3: cannot write interruption state to {self.store_root}"
            )
        self.store_root.mkdir(parents=True, exist_ok=True)
        next_state = copy.deepcopy(state)
        next_state["schema_version"] = SCHEMA_INTERRUPTION_STATE
        next_state["subject_id"] = GLOBAL_SUBJECT_ID
        next_state["revision"] = int(next_state.get("revision", 0)) + 1
        next_state["updated_at"] = _iso(now())

        state_tmp = self.store_root / f".{INTERRUPTION_STATE_FILENAME}.tmp.{os.getpid()}.{uuid.uuid4().hex}"
        log_tmp = self.store_root / f".{INTERRUPTION_LOG_FILENAME}.tmp.{os.getpid()}.{uuid.uuid4().hex}"
        try:
            if log_entry is not None:
                existing_lines: list[str] = []
                if self.log_path.exists():
                    existing_lines = [
                        ln for ln in self.log_path.read_text(encoding="utf-8").splitlines() if ln.strip()
                    ]
                entry_with_rev = dict(log_entry)
                entry_with_rev["schema_version"] = SCHEMA_INTERRUPTION_LOG_ENTRY
                entry_with_rev["interruption_revision"] = next_state["revision"]
                existing_lines.append(canonical_json_line(entry_with_rev))
                with log_tmp.open("w", encoding="utf-8") as lh:
                    lh.write("\n".join(existing_lines) + "\n")
                    lh.flush()
                    os.fsync(lh.fileno())
                os.replace(log_tmp, self.log_path)

            payload = json.dumps(next_state, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
            with state_tmp.open("w", encoding="utf-8") as sh:
                sh.write(payload)
                sh.flush()
                os.fsync(sh.fileno())
            os.replace(state_tmp, self.state_path)
            _fsync_dir(self.store_root)
        finally:
            if state_tmp.exists():
                state_tmp.unlink(missing_ok=True)
            if log_tmp.exists():
                log_tmp.unlink(missing_ok=True)

        return next_state


# ---------------------------------------------------------------------------
# InterruptionCoordinator (Sections 14-21, 32-36, 48-54)
# ---------------------------------------------------------------------------


class InterruptionCoordinator:
    """Coordinates Narrow Attention, InterruptionAssessment, PendingInterruption, and Safe Boundaries.

    All canonical Activity mutations (PAUSE) MUST go through LR-2 ActivityCommandService.
    Zero LLM calls, zero outbound messages, zero automatic activity selection, zero automatic resume.
    """

    def __init__(
        self,
        *,
        activity_store: CanonicalActivityStore,
        read_service: ActivityReadService,
        command_service: Optional[ActivityCommandService] = None,
        interruption_store: Optional[InterruptionStateStore] = None,
    ):
        self.activity_store = activity_store
        self.read_service = read_service
        self.command_service = command_service
        self.interruption_store = interruption_store or InterruptionStateStore(activity_store.store_root)

    # -- Attention Profile & Atomic Region Management -----------------------

    def configure_activity_attention(
        self,
        *,
        activity_id: str,
        attention_occupancy: str,
        interruptibility: str,
        atomic_region_ref: Optional[str] = None,
        max_atomic_duration_seconds: int = 60,
        action_ref: Optional[str] = None,
        extra_fields: Optional[Mapping[str, Any]] = None,
    ) -> NarrowAttentionProfile:
        """Set or update the NarrowAttentionProfile for an open Activity without mutating Activity status."""
        if extra_fields:
            validate_no_psychological_attention(extra_fields)

        act = self.read_service.get_activity(activity_id)
        if act is None:
            raise ContractViolationError(f"activity {activity_id!r} does not exist")
        if act["status"] not in OPEN_ACTIVITY_STATUSES:
            raise ContractViolationError(
                f"cannot configure attention for non-open activity {activity_id!r} (status={act['status']!r})"
            )

        profile = NarrowAttentionProfile(
            activity_id=activity_id,
            attention_occupancy=attention_occupancy,
            interruptibility=interruptibility,
            atomic_region_ref=atomic_region_ref,
        )
        istate = self.interruption_store.load_state()
        istate["attention_profiles"][activity_id] = profile.to_dict()

        if attention_occupancy == OCCUPANCY_ATOMIC and atomic_region_ref:
            region = AtomicRegionState(
                atomic_region_ref=atomic_region_ref,
                activity_id=activity_id,
                activity_revision=int(act["revision"]),
                entered_at=_iso(now()),
                max_duration_seconds=max_atomic_duration_seconds,
                action_ref=action_ref,
                active=True,
                reconciliation_status="CONFIRMED_ACTIVE",
            )
            istate["atomic_regions"][atomic_region_ref] = region.to_dict()

        self.interruption_store.save_state(
            istate,
            log_entry={
                "action": "CONFIGURE_ATTENTION",
                "activity_id": activity_id,
                "profile": profile.to_dict(),
            },
        )
        return profile

    def enter_atomic_region(
        self,
        *,
        activity_id: str,
        atomic_region_ref: str,
        max_duration_seconds: int = 60,
        action_ref: Optional[str] = None,
    ) -> AtomicRegionState:
        """Enter a short-lived ATOMIC region on an active Activity."""
        act = self.read_service.get_activity(activity_id)
        if act is None or act["status"] != STATUS_ACTIVE:
            raise ContractViolationError(
                f"can only enter atomic region on ACTIVE activity, got {activity_id!r}"
            )
        region = AtomicRegionState(
            atomic_region_ref=atomic_region_ref,
            activity_id=activity_id,
            activity_revision=int(act["revision"]),
            entered_at=_iso(now()),
            max_duration_seconds=max_duration_seconds,
            action_ref=action_ref,
            active=True,
            reconciliation_status="CONFIRMED_ACTIVE",
        )
        istate = self.interruption_store.load_state()
        istate["atomic_regions"][atomic_region_ref] = region.to_dict()
        self.interruption_store.save_state(
            istate,
            log_entry={
                "action": "ENTER_ATOMIC_REGION",
                "activity_id": activity_id,
                "atomic_region": region.to_dict(),
            },
        )
        return region

    def get_effective_attention(self, activity: Optional[Mapping[str, Any]]) -> tuple[str, str, Optional[AtomicRegionState]]:
        """Resolve effective (attention_occupancy, interruptibility, active_atomic_region) for an Activity."""
        if activity is None or activity.get("status") != STATUS_ACTIVE:
            return OCCUPANCY_FREE, INTERRUPTIBILITY_IMMEDIATE, None

        act_id = str(activity["activity_id"])
        istate = self.interruption_store.load_state()

        # Check if there is an active short-lived atomic region for this activity
        for reg_dict in istate.get("atomic_regions", {}).values():
            if reg_dict.get("activity_id") == act_id and reg_dict.get("active") is True:
                reg = AtomicRegionState(
                    atomic_region_ref=reg_dict["atomic_region_ref"],
                    activity_id=reg_dict["activity_id"],
                    activity_revision=int(reg_dict["activity_revision"]),
                    entered_at=reg_dict["entered_at"],
                    max_duration_seconds=int(reg_dict.get("max_duration_seconds", 60)),
                    action_ref=reg_dict.get("action_ref"),
                    active=True,
                    reconciliation_status=str(reg_dict.get("reconciliation_status", "CONFIRMED_ACTIVE")),
                )
                return OCCUPANCY_ATOMIC, INTERRUPTIBILITY_ATOMIC, reg

        prof_dict = istate.get("attention_profiles", {}).get(act_id)
        if prof_dict is not None:
            occ = prof_dict["attention_occupancy"]
            intr = prof_dict["interruptibility"]
            return occ, intr, None

        # Derive default from LR-2 activity interruptibility field
        base_occ = str(activity.get("interruptibility", OCCUPANCY_LIGHT))
        if base_occ not in ATTENTION_OCCUPANCY_LEVELS:
            base_occ = OCCUPANCY_LIGHT
        if base_occ == OCCUPANCY_ATOMIC:
            default_reg = AtomicRegionState(
                atomic_region_ref=f"atomic_reg:{act_id}_default",
                activity_id=act_id,
                activity_revision=int(activity["revision"]),
                entered_at=str(activity["updated_at"]),
                max_duration_seconds=60,
            )
            return OCCUPANCY_ATOMIC, INTERRUPTIBILITY_ATOMIC, default_reg
        default_intr = (
            INTERRUPTIBILITY_SAFE_BOUNDARY
            if base_occ == OCCUPANCY_FOCUSED
            else INTERRUPTIBILITY_IMMEDIATE
        )
        return base_occ, default_intr, None

    # -- Event Assessment & Multi-Event Coordination ------------------------

    def assess_events(
        self,
        events: Sequence[UnionEvent | Mapping[str, Any]],
        *,
        apply_immediate_pause: bool = False,
        fault_hook: Optional[Callable[[str], None]] = None,
    ) -> list[dict[str, Any]]:
        """Normalize, causally order, and deterministically assess one or more events."""
        normalized: list[UnionEvent] = []
        for raw in events:
            if isinstance(raw, (CommunicationEvent, ObservedWorldUrgency, BodyCapacityConstraint)):
                normalized.append(raw)
            elif isinstance(raw, Mapping):
                kind = raw.get("event_kind")
                if kind == EVENT_KIND_COMMUNICATION or "message_ref" in raw or "channel_ref" in raw:
                    normalized.append(CommunicationEventAdapter.adapt(raw))
                elif kind == EVENT_KIND_OBSERVED_WORLD_URGENCY or "world_event_ref" in raw or "observation_ref" in raw or "observed" in raw:
                    normalized.append(ObservedWorldUrgencyAdapter.adapt(raw))
                elif kind == EVENT_KIND_BODY_CONSTRAINT or "body_capacity_ref" in raw or "capacity_level" in raw:
                    normalized.append(BodyCapacityAdapter.adapt(raw))
                else:
                    raise ContractViolationError(f"cannot determine adapter for event payload: {raw!r}")
            else:
                raise ContractViolationError(f"unsupported event input: {raw!r}")

        # Section 19: Order strictly by causal graph + observed_at + occurred_at + event_id
        ordered = sort_events_causally(normalized)
        results: list[dict[str, Any]] = []
        for ev in ordered:
            res = self._assess_single_event(
                ev,
                apply_immediate_pause=apply_immediate_pause,
                fault_hook=fault_hook,
            )
            results.append(res)
        return results

    def _assess_single_event(
        self,
        event: UnionEvent,
        *,
        apply_immediate_pause: bool = False,
        fault_hook: Optional[Callable[[str], None]] = None,
    ) -> dict[str, Any]:
        istate = self.interruption_store.load_state()

        # F05: Duplicate event_id idempotency
        existing_assessment_id = istate.get("processed_events", {}).get(event.event_id)
        if existing_assessment_id and existing_assessment_id in istate.get("assessments", {}):
            return copy.deepcopy(istate["assessments"][existing_assessment_id])

        canonical_state, _ = self.activity_store.load_verified_state_and_journal()
        fg_ref = canonical_state.get("foreground_activity_ref")
        activities = canonical_state.get("activities", {})
        current_act = activities.get(fg_ref) if fg_ref else None
        life_state = current_act["status"] if current_act else LIFE_STATE_IDLE

        occ, intr, atomic_reg = self.get_effective_attention(current_act)
        decision = InterruptionPolicy.evaluate_event(
            event=event,
            life_state=life_state,
            current_activity=current_act,
            attention_occupancy=occ,
            interruptibility=intr,
            atomic_region=atomic_reg,
        )

        if fault_hook is not None:
            fault_hook("before_assessment_persist")

        pending_ref: Optional[str] = None
        if decision["requires_pending"] and current_act is not None:
            pending_obj = self._merge_or_create_pending_unlocked(
                istate=istate,
                event=event,
                activity_id=str(current_act["activity_id"]),
                activity_revision=int(current_act["revision"]),
                requested_effect=decision["activity_effect"],
                boundary_requirement=decision["boundary_requirement"] or BOUNDARY_KIND_SAFE_BOUNDARY,
                urgency_rank=int(decision["urgency_rank"]),
                atomic_region_ref=atomic_reg.atomic_region_ref if atomic_reg else None,
                continuation_viable_after_boundary=bool(decision["continuation_viable_after_boundary"]),
            )
            pending_ref = pending_obj.pending_id

        deterministic_seed = f"asmt:{event.event_id}:{decision['activity_id']}:{decision['activity_revision']}"
        assessment_id = f"asmt:{sha256_hex(deterministic_seed)[:16]}"

        assessment = InterruptionAssessment(
            assessment_id=assessment_id,
            activity_id=decision["activity_id"],
            activity_revision=decision["activity_revision"],
            source_event_refs=(event.event_id, *event.source_refs),
            attention_occupancy=decision["attention_occupancy"],
            interruptibility=decision["interruptibility"],
            observed_world_urgency=(
                event.urgency_level if isinstance(event, ObservedWorldUrgency) else None
            ),
            body_capacity_ref=(
                event.body_capacity_ref if isinstance(event, BodyCapacityConstraint) else None
            ),
            atomic_region_ref=atomic_reg.atomic_region_ref if atomic_reg else None,
            activity_effect=decision["activity_effect"],
            communication_availability=decision["communication_availability"],
            boundary_requirement=decision["boundary_requirement"],
            pending_interruption_ref=pending_ref,
            continuation_viable_after_boundary=decision["continuation_viable_after_boundary"],
            reason_codes=tuple(decision["reason_codes"]),
            occurred_at=decision["occurred_at"],
            recorded_at=decision["recorded_at"],
            policy_version=POLICY_VERSION_V1,
        )

        asmt_dict = assessment.to_dict()
        istate["assessments"][assessment_id] = asmt_dict
        istate["processed_events"][event.event_id] = assessment_id

        self.interruption_store.save_state(
            istate,
            log_entry={
                "action": "ASSESS_EVENT",
                "event_id": event.event_id,
                "assessment": asmt_dict,
            },
        )

        if fault_hook is not None:
            fault_hook("after_assessment")
            if pending_ref is not None:
                fault_hook("after_pending_persist")

        # Optional immediate pause application via LR-2 ActivityCommandService
        if (
            apply_immediate_pause
            and decision["activity_effect"] == EFFECT_INTERRUPT_NOW
            and current_act is not None
        ):
            if self.command_service is None:
                raise WriterCapabilityError(
                    "INTERRUPT_NOW application requires an authorized LR-2 ActivityCommandService"
                )
            pause_res = self.command_service.pause_activity(
                activity_id=str(current_act["activity_id"]),
                expected_revision=int(current_act["revision"]),
                idempotency_key=f"lr3_immediate_pause:{assessment_id}",
                source_refs=[event.event_id, *event.source_refs],
                causal_parent_refs=[event.event_id],
                reason_code="USER_INTERRUPTION",
            )
            trn_obj = pause_res.transition if isinstance(pause_res, CommandResult) else pause_res["transition"]
            assert trn_obj is not None
            istate_after = self.interruption_store.load_state()
            asmt_dict["applied_transition_ref"] = trn_obj["transition_id"]
            istate_after["assessments"][assessment_id] = asmt_dict
            self.interruption_store.save_state(
                istate_after,
                log_entry={
                    "action": "IMMEDIATE_PAUSE_APPLIED",
                    "assessment_id": assessment_id,
                    "transition_id": trn_obj["transition_id"],
                },
            )

        return asmt_dict

    def _merge_or_create_pending_unlocked(
        self,
        *,
        istate: dict[str, Any],
        event: UnionEvent,
        activity_id: str,
        activity_revision: int,
        requested_effect: str,
        boundary_requirement: str,
        urgency_rank: int,
        atomic_region_ref: Optional[str],
        continuation_viable_after_boundary: bool,
    ) -> PendingInterruption:
        """Implement F01 (dedupe/merge), F02 (coexist across source kinds), F03 (supersede without blind overwrite)."""
        if isinstance(event, CommunicationEvent):
            source_kind = EVENT_KIND_COMMUNICATION
        elif isinstance(event, ObservedWorldUrgency):
            source_kind = EVENT_KIND_OBSERVED_WORLD_URGENCY
        else:
            source_kind = EVENT_KIND_BODY_CONSTRAINT

        pendings_map: dict[str, dict[str, Any]] = istate.setdefault("pending_interruptions", {})
        open_same_kind: list[PendingInterruption] = []
        for p_dict in pendings_map.values():
            if (
                p_dict.get("activity_id") == activity_id
                and p_dict.get("status") in OPEN_PENDING_STATUSES
                and p_dict.get("source_kind") == source_kind
            ):
                open_same_kind.append(PendingInterruption.from_dict(p_dict))

        now_iso = _iso(now())

        # F01: Communication events on the same activity & revision merge into existing open communication pending
        if source_kind == EVENT_KIND_COMMUNICATION and open_same_kind:
            existing = open_same_kind[0]
            if existing.activity_revision_at_request == activity_revision:
                if event.event_id not in existing.source_event_refs:
                    existing.source_event_refs.append(event.event_id)
                existing.last_evaluated_at = now_iso
                existing.urgency_rank = max(existing.urgency_rank, urgency_rank)
                pendings_map[existing.pending_id] = existing.to_dict()
                return existing

        # F03: For world urgency or body constraint of the SAME source_kind:
        # - If an existing open pending has HIGHER or EQUAL urgency_rank on the same revision:
        #   Preserve the higher-priority pending, append event_id into its source_event_refs (no blind overwrite!)
        # - If the new event has strictly HIGHER urgency_rank:
        #   Supersede the older lower-priority pending of the same source_kind.
        for existing in open_same_kind:
            if existing.activity_revision_at_request == activity_revision:
                if existing.urgency_rank >= urgency_rank:
                    if event.event_id not in existing.source_event_refs:
                        existing.source_event_refs.append(event.event_id)
                    existing.last_evaluated_at = now_iso
                    existing.continuation_viable_after_boundary = (
                        existing.continuation_viable_after_boundary and continuation_viable_after_boundary
                    )
                    pendings_map[existing.pending_id] = existing.to_dict()
                    return existing

        # Create new PendingInterruption (coexists with open pendings of different source_kind -> F02)
        seed = f"pend:{activity_id}:{activity_revision}:{source_kind}:{event.event_id}"
        pending_id = f"pend:{sha256_hex(seed)[:16]}"
        new_pending = PendingInterruption(
            pending_id=pending_id,
            activity_id=activity_id,
            activity_revision_at_request=activity_revision,
            source_event_refs=[event.event_id],
            source_kind=source_kind,
            requested_effect=requested_effect,
            boundary_requirement=boundary_requirement,
            urgency_rank=urgency_rank,
            created_at=now_iso,
            observed_at=event.observed_at,
            status=PENDING_STATUS_PENDING,
            atomic_region_ref=atomic_region_ref,
            continuation_viable_after_boundary=continuation_viable_after_boundary,
            last_evaluated_at=now_iso,
        )

        # Mark superseded lower-urgency pendings of the same source_kind
        for existing in open_same_kind:
            if existing.activity_revision_at_request == activity_revision and existing.urgency_rank < urgency_rank:
                existing.status = PENDING_STATUS_SUPERSEDED
                existing.superseded_by_ref = new_pending.pending_id
                existing.resolution_reason_code = "SUPERSEDED_BY_HIGHER_URGENCY"
                existing.last_evaluated_at = now_iso
                pendings_map[existing.pending_id] = existing.to_dict()

        pendings_map[new_pending.pending_id] = new_pending.to_dict()
        return new_pending

    # -- Safe Boundary Handling & Revision Protection -----------------------

    def handle_boundary_reached(
        self,
        boundary_input: BoundaryReachedEvent | Mapping[str, Any],
        *,
        fault_hook: Optional[Callable[[str], None]] = None,
    ) -> dict[str, Any]:
        """Validate BoundaryReachedEvent, enforce revision protection, and pause via LR-2 ActivityCommandService."""
        boundary = (
            boundary_input
            if isinstance(boundary_input, BoundaryReachedEvent)
            else BoundaryEventAdapter.adapt(boundary_input)
        )

        # Reconcile any interrupted crash state first
        self.recover_cold_start()

        istate = self.interruption_store.load_state()

        # E06: Duplicate boundary event idempotency
        if boundary.boundary_ref in istate.get("applied_boundaries", {}):
            return copy.deepcopy(istate["applied_boundaries"][boundary.boundary_ref])

        canonical_state, _ = self.activity_store.load_verified_state_and_journal()
        fg_ref = canonical_state.get("foreground_activity_ref")
        activities = canonical_state.get("activities", {})

        # E05: Wrong activity boundary rejected
        target_act = activities.get(boundary.activity_ref)
        if target_act is None or fg_ref != boundary.activity_ref:
            raise BoundaryMismatchError(
                f"WRONG_ACTIVITY_BOUNDARY: boundary activity_ref={boundary.activity_ref!r} "
                f"does not match current foreground_activity_ref={fg_ref!r}"
            )

        current_rev = int(target_act["revision"])

        # Find all open pendings for this activity
        pendings_map: dict[str, dict[str, Any]] = istate.get("pending_interruptions", {})
        open_pendings = [
            PendingInterruption.from_dict(p)
            for p in pendings_map.values()
            if p.get("activity_id") == boundary.activity_ref and p.get("status") in OPEN_PENDING_STATUSES
        ]
        if not open_pendings:
            raise BoundaryMismatchError(
                f"NO_OPEN_PENDING_INTERRUPTION: no open pending interruption for {boundary.activity_ref!r}"
            )

        # Sort open pendings deterministically by (-urgency_rank, observed_at, pending_id)
        open_pendings.sort(
            key=lambda p: (
                -p.urgency_rank,
                _parse(p.observed_at).timestamp() if _parse(p.observed_at) else 0.0,
                p.pending_id,
            )
        )

        # Section 18 & E04: Revision Protection (reject stale pending interruptions)
        stale_pendings = [
            p for p in open_pendings if p.activity_revision_at_request != current_rev
        ]
        valid_pendings = [
            p for p in open_pendings if p.activity_revision_at_request == current_rev
        ]

        now_iso = _iso(now())
        for sp in stale_pendings:
            sp.status = PENDING_STATUS_CANCELLED
            sp.resolution_reason_code = "STALE_PENDING_INTERRUPT"
            sp.last_evaluated_at = now_iso
            pendings_map[sp.pending_id] = sp.to_dict()

        if not valid_pendings:
            self.interruption_store.save_state(
                istate,
                log_entry={
                    "action": "REJECT_STALE_PENDING_AT_BOUNDARY",
                    "boundary_ref": boundary.boundary_ref,
                    "activity_id": boundary.activity_ref,
                    "current_revision": current_rev,
                    "stale_pending_ids": [sp.pending_id for sp in stale_pendings],
                },
            )
            raise StalePendingInterruptionError(
                f"STALE_PENDING_INTERRUPT: pending interruption(s) {[sp.pending_id for sp in stale_pendings]} "
                f"requested at revision {[sp.activity_revision_at_request for sp in stale_pendings]}, "
                f"but activity {boundary.activity_ref!r} is now at revision {current_rev}"
            )

        primary = valid_pendings[0]

        # H04/H05: If an atomic region is active on this pending or activity, verify atomic_region_ref match
        if primary.atomic_region_ref is not None:
            if boundary.atomic_region_ref is not None and boundary.atomic_region_ref != primary.atomic_region_ref:
                raise InvalidBoundaryEvidenceError(
                    f"ATOMIC_REGION_MISMATCH: boundary atomic_region_ref={boundary.atomic_region_ref!r} "
                    f"does not match active atomic_region_ref={primary.atomic_region_ref!r}"
                )

        # Stage READY_AT_BOUNDARY
        primary.status = PENDING_STATUS_READY_AT_BOUNDARY
        primary.boundary_ref = boundary.boundary_ref
        primary.last_evaluated_at = now_iso
        pendings_map[primary.pending_id] = primary.to_dict()
        self.interruption_store.save_state(
            istate,
            log_entry={
                "action": "STAGE_READY_AT_BOUNDARY",
                "pending_id": primary.pending_id,
                "boundary_ref": boundary.boundary_ref,
            },
        )

        if fault_hook is not None:
            fault_hook("before_pause_command")

        if self.command_service is None:
            raise WriterCapabilityError(
                "Safe boundary PAUSE requires an authorized LR-2 ActivityCommandService"
            )

        all_source_refs: list[str] = []
        for vp in valid_pendings:
            for sref in vp.source_event_refs:
                if sref not in all_source_refs:
                    all_source_refs.append(sref)
        for sref in (boundary.boundary_ref, boundary.source_ref):
            if sref not in all_source_refs:
                all_source_refs.append(sref)

        # E03 & Section 17: Execute PAUSE strictly through LR-2 ActivityCommandService
        pause_idempotency_key = f"lr3_boundary_pause:{primary.pending_id}:{boundary.boundary_ref}"
        pause_result = self.command_service.pause_activity(
            activity_id=boundary.activity_ref,
            expected_revision=current_rev,
            idempotency_key=pause_idempotency_key,
            source_refs=all_source_refs,
            causal_parent_refs=[primary.pending_id, boundary.boundary_ref],
            reason_code="SAFE_BOUNDARY_PAUSE",
            occurred_at=boundary.occurred_at,
            observed_at=boundary.observed_at,
        )
        pause_trn = pause_result.transition if isinstance(pause_result, CommandResult) else pause_result["transition"]
        assert pause_trn is not None

        if fault_hook is not None:
            fault_hook("after_pause_command")

        # Mark primary as APPLIED and coexisting valid pendings as APPLIED (coalesced at same boundary)
        istate_post = self.interruption_store.load_state()
        pendings_post = istate_post.get("pending_interruptions", {})
        trn_id = pause_trn["transition_id"]

        for vp in valid_pendings:
            vp.status = PENDING_STATUS_APPLIED
            vp.boundary_ref = boundary.boundary_ref
            vp.applied_transition_ref = trn_id
            vp.resolution_reason_code = "APPLIED_AT_SAFE_BOUNDARY"
            vp.last_evaluated_at = _iso(now())
            pendings_post[vp.pending_id] = vp.to_dict()

        # Close active atomic region if any
        for reg_id, reg_dict in istate_post.get("atomic_regions", {}).items():
            if reg_dict.get("activity_id") == boundary.activity_ref and reg_dict.get("active") is True:
                reg_dict["active"] = False
                reg_dict["reconciliation_status"] = "EXITED_AT_BOUNDARY"

        self.interruption_store.save_state(
            istate_post,
            log_entry={
                "action": "BOUNDARY_PAUSE_APPLIED",
                "boundary_ref": boundary.boundary_ref,
                "pending_id": primary.pending_id,
                "transition_id": trn_id,
            },
        )
        enriched_view = self.get_current_life_view()
        response_record = {
            "boundary_ref": boundary.boundary_ref,
            "activity_id": boundary.activity_ref,
            "applied_pending_id": primary.pending_id,
            "coalesced_pending_ids": [vp.pending_id for vp in valid_pendings],
            "transition": pause_trn,
            "life_view": enriched_view,
        }
        istate_final = self.interruption_store.load_state()
        istate_final.setdefault("applied_boundaries", {})[boundary.boundary_ref] = response_record
        self.interruption_store.save_state(istate_final)
        return copy.deepcopy(response_record)

    # -- Read View & Recovery (Sections 32, 34, 35, 54) ---------------------

    def get_primary_open_pending(self, activity_id: Optional[str]) -> Optional[dict[str, Any]]:
        if not activity_id:
            return None
        istate = self.interruption_store.load_state()
        open_pendings = [
            PendingInterruption.from_dict(p)
            for p in istate.get("pending_interruptions", {}).values()
            if p.get("activity_id") == activity_id and p.get("status") in OPEN_PENDING_STATUSES
        ]
        if not open_pendings:
            return None
        open_pendings.sort(
            key=lambda p: (
                -p.urgency_rank,
                _parse(p.observed_at).timestamp() if _parse(p.observed_at) else 0.0,
                p.pending_id,
            )
        )
        return open_pendings[0].to_dict()

    def list_pending_interruptions(
        self,
        *,
        activity_id: Optional[str] = None,
        only_open: bool = False,
    ) -> list[dict[str, Any]]:
        istate = self.interruption_store.load_state()
        items: list[dict[str, Any]] = []
        for p_dict in istate.get("pending_interruptions", {}).values():
            if activity_id is not None and p_dict.get("activity_id") != activity_id:
                continue
            if only_open and p_dict.get("status") not in OPEN_PENDING_STATUSES:
                continue
            items.append(copy.deepcopy(p_dict))
        return items

    def get_current_life_view(self, *, caller_context: Optional[str] = None) -> dict[str, Any]:
        """Return the derived LifeFrameReadView enriched with LR-3 Narrow Attention & Interruptibility."""
        canonical_state, _ = self.activity_store.load_verified_state_and_journal()
        fg_ref = canonical_state.get("foreground_activity_ref")
        activities = canonical_state.get("activities", {})
        fg_act = activities.get(fg_ref) if fg_ref else None
        life_state = fg_act["status"] if fg_act else LIFE_STATE_IDLE

        occ, intr, _ = self.get_effective_attention(fg_act)
        primary_pending = self.get_primary_open_pending(fg_ref)
        comm_avail = InterruptionPolicy.compute_communication_availability(
            life_state=life_state,
            attention_occupancy=occ,
            interruptibility=intr,
        )

        overlay = {
            "attention_occupancy": occ,
            "interruptibility": intr,
            "pending_interruption_ref": primary_pending["pending_id"] if primary_pending else None,
            "communication_availability": comm_avail,
        }
        return build_life_frame_read_view(
            canonical_state,
            interruption_overlay=overlay,
        )

    def recover_cold_start(
        self,
        *,
        atomic_truth_known: bool = True,
    ) -> dict[str, Any]:
        """Cold-start recovery of both LR-2 canonical store and LR-3 pending interruption state.

        - Never auto-pauses an Activity on restart (Section 34).
        - Never triggers an LLM on restart (Section 34).
        - Reconciles READY_AT_BOUNDARY pendings if the pause transition already committed in LR-2 journal (Section 54).
        - Marks atomic region UNKNOWN_RECONCILIATION_REQUIRED if atomic truth cannot be confirmed after crash (Section 35).
        """
        canonical_state, journal_entries = self.activity_store.load_verified_state_and_journal()
        istate = self.interruption_store.load_state()
        mutated = False

        # Build index of committed LR-2 transitions by idempotency_key
        transitions_by_idem: dict[str, dict[str, Any]] = {
            entry["idempotency_key"]: entry for entry in journal_entries if "idempotency_key" in entry
        }

        pendings_map = istate.get("pending_interruptions", {})
        reconciled_boundaries: list[tuple[str, str, list[str], dict[str, Any]]] = []
        for pid, p_dict in list(pendings_map.items()):
            if p_dict.get("status") in OPEN_PENDING_STATUSES and p_dict.get("boundary_ref"):
                b_ref = p_dict["boundary_ref"]
                idem_key = f"lr3_boundary_pause:{pid}:{b_ref}"
                if idem_key in transitions_by_idem:
                    trn = transitions_by_idem[idem_key]
                    coalesced_ids: list[str] = []
                    for other_id, other_dict in pendings_map.items():
                        if (
                            other_dict.get("activity_id") == p_dict.get("activity_id")
                            and other_dict.get("activity_revision_at_request") == p_dict.get("activity_revision_at_request")
                            and other_dict.get("status") in OPEN_PENDING_STATUSES
                        ):
                            other_dict["status"] = PENDING_STATUS_APPLIED
                            other_dict["boundary_ref"] = b_ref
                            other_dict["applied_transition_ref"] = trn["transition_id"]
                            other_dict["resolution_reason_code"] = "RECONCILED_FROM_COMMITTED_TRANSITION"
                            other_dict["last_evaluated_at"] = _iso(now())
                            coalesced_ids.append(other_id)
                    for reg_dict in istate.get("atomic_regions", {}).values():
                        if reg_dict.get("activity_id") == p_dict["activity_id"] and reg_dict.get("active") is True:
                            reg_dict["active"] = False
                            reg_dict["reconciliation_status"] = "EXITED_AT_BOUNDARY"
                    reconciled_boundaries.append((b_ref, pid, coalesced_ids, trn))
                    mutated = True

        # Section 35: If an atomic region was active during crash and external action truth is unconfirmed
        if not atomic_truth_known:
            for reg_dict in istate.get("atomic_regions", {}).values():
                if reg_dict.get("active") is True:
                    reg_dict["reconciliation_status"] = "UNKNOWN_RECONCILIATION_REQUIRED"
                    mutated = True

        if mutated:
            istate = self.interruption_store.save_state(
                istate,
                log_entry={"action": "COLD_START_RECONCILIATION"},
            )

        life_view = self.get_current_life_view()
        if reconciled_boundaries:
            istate = self.interruption_store.load_state()
            for b_ref, pid, coalesced_ids, trn in reconciled_boundaries:
                istate.setdefault("applied_boundaries", {})[b_ref] = {
                    "boundary_ref": b_ref,
                    "activity_id": trn["activity_id"],
                    "applied_pending_id": pid,
                    "coalesced_pending_ids": coalesced_ids,
                    "transition": trn,
                    "life_view": life_view,
                }
            istate = self.interruption_store.save_state(istate)

        open_pendings = [
            copy.deepcopy(p)
            for p in istate.get("pending_interruptions", {}).values()
            if p.get("status") in OPEN_PENDING_STATUSES
        ]
        active_atomic_regions = [
            copy.deepcopy(r)
            for r in istate.get("atomic_regions", {}).values()
            if r.get("active") is True
        ]
        return {
            "recovered": True,
            "canonical_revision": canonical_state["revision"],
            "interruption_revision": istate["revision"],
            "life_view": life_view,
            "open_pending_interruptions": open_pendings,
            "active_atomic_regions": active_atomic_regions,
        }
