"""Isolated CT0-4 bridge seam for ContactIntent -> AG-0 -> AG-1.

Canonical AG-0/AG-1 implementations are not vendored in this sandbox. Callers
must inject adapters owned by those systems. This module contains no second
decision engine and no Draft, Action, transport, or persistence API.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import math
from typing import Protocol

from contact_intent_authority import ContactIntentAuthorityError
from contact_intent_model import STATUS_OPEN, ContactIntent, normalize_utc


CONTACT_INTENT_TRIGGER_KIND = "CONTACT_INTENT"
CAPACITY_AVAILABLE = "AVAILABLE"
CAPACITY_UNAVAILABLE = "UNAVAILABLE"

BLOCK_ATOMIC = "ATOMIC"
BLOCK_QUIET_WINDOW = "QUIET_WINDOW_CLOSED"
BLOCK_DEVICE = "DEVICE_UNAVAILABLE"
BLOCK_CHANNEL = "CHANNEL_UNAVAILABLE"
BLOCK_PENDING_OUTBOUND = "PENDING_OUTBOUND"
BLOCK_PREVIOUS_UNKNOWN = "PREVIOUS_UNKNOWN"
BLOCK_UNRESOLVED_INBOUND = "UNRESOLVED_INBOUND"
BLOCK_INTENT_NOT_OPEN = "INTENT_NOT_OPEN"
BLOCK_INTENT_EXPIRED = "INTENT_EXPIRED"
BLOCK_CAPACITY_FUTURE = "CAPACITY_OBSERVATION_IN_FUTURE"
BLOCK_CAPACITY_STALE = "CAPACITY_OBSERVATION_STALE"
BLOCK_CAPACITY_REVISION = "CAPACITY_REVISION_NOT_ADVANCED"
BLOCK_CAPACITY_EDGE_NOT_OBSERVED = "UNAVAILABLE_BASELINE_NOT_OBSERVED"


def _instant(value: str, name: str) -> datetime:
    normalized = normalize_utc(value, name)
    parsed = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
    return parsed.astimezone(timezone.utc)


def _positive_seconds(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ContactIntentAuthorityError(f"{name} must be finite positive seconds")
    seconds = float(value)
    if not math.isfinite(seconds) or seconds <= 0:
        raise ContactIntentAuthorityError(f"{name} must be finite positive seconds")
    return seconds


@dataclass(frozen=True, slots=True)
class ContactCapacitySnapshot:
    """Detached evidence about whether contact is possible now; never a motive."""

    observed_at: str
    revision: int
    quiet_window_open: bool
    device_available: bool
    channel_available: bool
    atomic: bool
    pending_outbound_count: int
    previous_unknown: bool
    unresolved_inbound: bool

    def __post_init__(self) -> None:
        normalize_utc(self.observed_at, "observed_at")
        if not isinstance(self.revision, int) or isinstance(self.revision, bool) or self.revision < 1:
            raise ContactIntentAuthorityError("capacity revision must be an integer >= 1")
        for name in (
            "quiet_window_open", "device_available", "channel_available", "atomic",
            "previous_unknown", "unresolved_inbound",
        ):
            if not isinstance(getattr(self, name), bool):
                raise ContactIntentAuthorityError(f"{name} must be a bool")
        if (
            not isinstance(self.pending_outbound_count, int)
            or isinstance(self.pending_outbound_count, bool)
            or self.pending_outbound_count < 0
        ):
            raise ContactIntentAuthorityError("pending_outbound_count must be an integer >= 0")


@dataclass(frozen=True, slots=True)
class ContactCapacityResult:
    status: str
    observed_at: str
    revision: int
    reasons: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.status, str) or self.status not in {CAPACITY_AVAILABLE, CAPACITY_UNAVAILABLE}:
            raise ContactIntentAuthorityError("unsupported ContactCapacity status")
        if not isinstance(self.revision, int) or isinstance(self.revision, bool) or self.revision < 1:
            raise ContactIntentAuthorityError("capacity result revision must be >= 1")
        normalize_utc(self.observed_at, "observed_at")
        if not isinstance(self.reasons, tuple) or any(not isinstance(reason, str) for reason in self.reasons):
            raise ContactIntentAuthorityError("capacity reasons must be immutable strings")
        if len(set(self.reasons)) != len(self.reasons):
            raise ContactIntentAuthorityError("capacity reasons must be unique")
        if (self.status == CAPACITY_AVAILABLE) != (not self.reasons):
            raise ContactIntentAuthorityError("capacity status and blocking reasons disagree")


def evaluate_contact_capacity(
    intent: object,
    snapshot: ContactCapacitySnapshot,
    *,
    now: str,
    max_snapshot_age_seconds: float = 300.0,
) -> ContactCapacityResult:
    """Return deterministic capacity only; never create or select an Intent."""
    if not isinstance(intent, ContactIntent):
        raise ContactIntentAuthorityError("capacity requires an existing ContactIntent")
    if not isinstance(snapshot, ContactCapacitySnapshot):
        raise ContactIntentAuthorityError("capacity requires a ContactCapacitySnapshot")
    max_age = _positive_seconds(max_snapshot_age_seconds, "max_snapshot_age_seconds")
    instant = _instant(now, "now")
    observed = _instant(snapshot.observed_at, "capacity.observed_at")
    reasons: list[str] = []
    if intent.status != STATUS_OPEN:
        reasons.append(BLOCK_INTENT_NOT_OPEN)
    if instant >= _instant(intent.valid_until, "intent.valid_until"):
        reasons.append(BLOCK_INTENT_EXPIRED)
    age = (instant - observed).total_seconds()
    if age < 0:
        reasons.append(BLOCK_CAPACITY_FUTURE)
    elif age > max_age:
        reasons.append(BLOCK_CAPACITY_STALE)
    if snapshot.atomic:
        reasons.append(BLOCK_ATOMIC)
    if not snapshot.quiet_window_open:
        reasons.append(BLOCK_QUIET_WINDOW)
    if not snapshot.device_available:
        reasons.append(BLOCK_DEVICE)
    if not snapshot.channel_available:
        reasons.append(BLOCK_CHANNEL)
    if snapshot.pending_outbound_count:
        reasons.append(BLOCK_PENDING_OUTBOUND)
    if snapshot.previous_unknown:
        reasons.append(BLOCK_PREVIOUS_UNKNOWN)
    if snapshot.unresolved_inbound:
        reasons.append(BLOCK_UNRESOLVED_INBOUND)
    return ContactCapacityResult(
        status=CAPACITY_UNAVAILABLE if reasons else CAPACITY_AVAILABLE,
        observed_at=normalize_utc(snapshot.observed_at, "observed_at"),
        revision=snapshot.revision,
        reasons=tuple(reasons),
    )


class Ag0ContactCandidatePort(Protocol):
    """Adapter to a versioned AG-0 ContactIntent source and CandidateSet builder."""

    def build_candidate_set(self, *, intent: ContactIntent, observed_at: str) -> object:
        """Materialize a CONTACT_INTENT AG-0 candidate and canonical CandidateSet."""
        ...


class DecisionOpportunityFactory(Protocol):
    """The canonical AG-1 DecisionOpportunity.create factory, injected by owner."""

    def create(
        self,
        *,
        trigger_kind: str,
        trigger_ref: str,
        candidate_set: object,
        current_activity_ref: str | None,
        observed_at: str,
        causal_parent_refs: tuple[str, ...],
        valid_until: str,
    ) -> object:
        ...


class AgencyDecisionServicePort(Protocol):
    """The canonical AG-1 AgencyDecisionService.evaluate_opportunity port."""

    def evaluate_opportunity(self, *, opportunity: object, candidate_set: object) -> object:
        ...


@dataclass(frozen=True, slots=True)
class ContactAgencyEvaluation:
    intent_id: str
    capacity: ContactCapacityResult
    evaluated: bool
    candidate_set: object | None
    opportunity: object | None
    decision_result: object | None


class ContactIntentAgencyBridge:
    """Gate an existing OPEN Intent, then delegate candidate and decision ownership.

    A new opportunity is emitted only on a fresh, revision-advanced
    UNAVAILABLE -> AVAILABLE edge. Invalid-time snapshots never establish an
    edge baseline. The edge is consumed immediately before entering AG-1 so an
    ambiguous service error cannot cause duplicate decision attempts. Errors in
    AG-0/factory leave the edge unconsumed for safe retry. State is an in-memory
    contract fixture; production owners must provide durable idempotency.
    """

    def __init__(
        self,
        *,
        ag0: Ag0ContactCandidatePort,
        opportunity_factory: DecisionOpportunityFactory,
        decision_service: AgencyDecisionServicePort,
        max_snapshot_age_seconds: float = 300.0,
    ) -> None:
        self._ag0 = ag0
        self._opportunity_factory = opportunity_factory
        self._decision_service = decision_service
        self.max_snapshot_age_seconds = _positive_seconds(max_snapshot_age_seconds, "max_snapshot_age_seconds")
        self._last_capacity: dict[str, tuple[str, int]] = {}

    def evaluate(
        self,
        intent: object,
        capacity_snapshot: ContactCapacitySnapshot,
        *,
        now: str,
        current_activity_ref: str | None = None,
    ) -> ContactAgencyEvaluation:
        capacity = evaluate_contact_capacity(
            intent,
            capacity_snapshot,
            now=now,
            max_snapshot_age_seconds=self.max_snapshot_age_seconds,
        )
        if not isinstance(intent, ContactIntent):
            raise ContactIntentAuthorityError("bridge requires an existing ContactIntent")

        invalid_time = BLOCK_CAPACITY_FUTURE in capacity.reasons or BLOCK_CAPACITY_STALE in capacity.reasons
        previous = self._last_capacity.get(intent.intent_id)
        if invalid_time:
            return ContactAgencyEvaluation(intent.intent_id, capacity, False, None, None, None)
        if previous is not None and capacity.revision <= previous[1]:
            stale_revision = ContactCapacityResult(
                status=CAPACITY_UNAVAILABLE,
                observed_at=capacity.observed_at,
                revision=capacity.revision,
                reasons=tuple(dict.fromkeys((*capacity.reasons, BLOCK_CAPACITY_REVISION))),
            )
            return ContactAgencyEvaluation(intent.intent_id, stale_revision, False, None, None, None)

        if previous is None:
            if capacity.status == CAPACITY_UNAVAILABLE:
                self._last_capacity[intent.intent_id] = (CAPACITY_UNAVAILABLE, capacity.revision)
                return ContactAgencyEvaluation(intent.intent_id, capacity, False, None, None, None)
            no_edge = ContactCapacityResult(
                status=CAPACITY_UNAVAILABLE,
                observed_at=capacity.observed_at,
                revision=capacity.revision,
                reasons=(BLOCK_CAPACITY_EDGE_NOT_OBSERVED,),
            )
            self._last_capacity[intent.intent_id] = (CAPACITY_AVAILABLE, capacity.revision)
            return ContactAgencyEvaluation(intent.intent_id, no_edge, False, None, None, None)

        if capacity.status != CAPACITY_AVAILABLE or previous[0] != CAPACITY_UNAVAILABLE:
            self._last_capacity[intent.intent_id] = (capacity.status, capacity.revision)
            return ContactAgencyEvaluation(intent.intent_id, capacity, False, None, None, None)

        # Build upstream records before consuming the edge. These ports are
        # expected to be deterministic/idempotent under the same source refs.
        observed_at = capacity.observed_at
        candidate_set = self._ag0.build_candidate_set(intent=intent, observed_at=observed_at)
        candidate_set_id = getattr(candidate_set, "candidate_set_id", None)
        if not isinstance(candidate_set_id, str) or not candidate_set_id:
            raise ContactIntentAuthorityError("AG-0 adapter returned no canonical candidate_set_id")
        opportunity = self._opportunity_factory.create(
            trigger_kind=CONTACT_INTENT_TRIGGER_KIND,
            trigger_ref=intent.intent_id,
            candidate_set=candidate_set,
            current_activity_ref=current_activity_ref,
            observed_at=observed_at,
            causal_parent_refs=(intent.intent_id, candidate_set_id),
            valid_until=intent.valid_until,
        )

        # Commit the edge before the sole decision owner call: an exception
        # there may mean the decision was committed, so replay must fail closed.
        self._last_capacity[intent.intent_id] = (CAPACITY_AVAILABLE, capacity.revision)
        decision_result = self._decision_service.evaluate_opportunity(
            opportunity=opportunity,
            candidate_set=candidate_set,
        )
        return ContactAgencyEvaluation(
            intent_id=intent.intent_id,
            capacity=capacity,
            evaluated=True,
            candidate_set=candidate_set,
            opportunity=opportunity,
            decision_result=decision_result,
        )


__all__ = [
    "BLOCK_ATOMIC", "BLOCK_CAPACITY_EDGE_NOT_OBSERVED", "BLOCK_CAPACITY_FUTURE",
    "BLOCK_CAPACITY_REVISION", "BLOCK_CAPACITY_STALE", "BLOCK_CHANNEL", "BLOCK_DEVICE",
    "BLOCK_INTENT_EXPIRED", "BLOCK_INTENT_NOT_OPEN", "BLOCK_PENDING_OUTBOUND",
    "BLOCK_PREVIOUS_UNKNOWN", "BLOCK_QUIET_WINDOW", "BLOCK_UNRESOLVED_INBOUND",
    "CAPACITY_AVAILABLE", "CAPACITY_UNAVAILABLE", "CONTACT_INTENT_TRIGGER_KIND",
    "AgencyDecisionServicePort", "Ag0ContactCandidatePort", "ContactAgencyEvaluation",
    "ContactCapacityResult", "ContactCapacitySnapshot", "ContactIntentAgencyBridge",
    "DecisionOpportunityFactory", "evaluate_contact_capacity",
]
