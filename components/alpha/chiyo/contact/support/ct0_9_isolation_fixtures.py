"""CT0-9 isolated qualification fixtures.

ISOLATED QUALIFICATION FIXTURES. None of the classes in this module is a
canonical owner. They exist so the CT0-9 end-to-end pipeline qualification can
run deterministically without canonical AG-0 / AG-1 / Grounding / AR-0 / AR-1 /
real Telegram.

Identity of every object here
-----------------------------
    role = "isolated qualification carrier" / "isolated qualification fixture"
    canonical owner      = NO
    production writer    = NO
    authority over any canonical domain = NO

Hard separation rules encoded below (and asserted by the gates):
  * the capacity carrier is READ-ONLY to the pipeline: it exposes no mutator.
    Only `IsolatedCapacityPublisher`, held by the test harness and never passed
    into the pipeline, advances the carrier.
  * the capacity carrier produces ONLY snapshots. It cannot create a
    ContactIntent, an opportunity, a Decision, or a submit.
  * the recording AG-1 decision service mints a *fixture* decision identity
    (`dec:` namespace) - it is NOT a canonical AG-1 implementation.
  * the deterministic expression renderer answers only "how to say it". It has
    no `submit`/`select`/`decide` capability and records any attempt as a
    violation.
  * the AR-0 recording ledger is a fake action ledger. It has no transport: a
    `submit` call raises and is counted as a violation.
  * `SEND` is a *scenario term*. Its contract term is the existing Decision
    enum value `CONTACT_SELECTED`. No new decision verb is introduced and the
    CT0-4 / CT0-5 Decision schema and meaning version are untouched.
"""

from __future__ import annotations

import hashlib
import json
import sys
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Mapping, Sequence

# --------------------------------------------------------------------------
# import bootstrap: identical to tests/test_contact_durable_store.py so that
# class identity is shared with the sandbox modules (flat imports for
# src/ct0/*.py, package imports only for ct0.durable.*).
# --------------------------------------------------------------------------
_SANDBOX_ROOT = Path(__file__).resolve().parents[2]
_SRC_CT0 = _SANDBOX_ROOT / "src" / "ct0"
for _entry in (str(_SRC_CT0), str(_SRC_CT0.parent)):
    if _entry not in sys.path:
        sys.path.insert(0, _entry)

from contact_candidate_model import (  # noqa: E402
    AG2_HANDOFF_KINDS,
    AG2_HANDOFF_SCHEMA,
    CANDIDATE_SHARE_EXPERIENCE,
    CONTACT_CANDIDATE_POLICY_VERSION,
    CONTACT_CANDIDATE_SCHEMA,
    GLOBAL_SUBJECT_ID,
    SOURCE_LIFE_EXPERIENCE,
)
from contact_candidate_sources import (  # noqa: E402
    CONTACT_SOURCE_KINDS,
    SOURCE_DISABLED_NO_CANONICAL_OWNER,
    SOURCE_ENABLED_ISOLATED,
    SOURCE_EXPLICIT_FOLLOWUP,
    SOURCE_REPAIR_ITEM,
    SOURCE_SHARED_CONTEXT_RESULT,
    SOURCE_SOCIAL_COMMITMENT,
)
from contact_draft_model import (  # noqa: E402
    CONTACT_DECISION_RECEIPT_SCHEMA,
    CONTACT_DRAFT_RENDER_SCHEMA,
    DECISION_CONTACT_SELECTED,
    DECISION_DEFER,
    DECISION_NO_ACTION,
    ContactDecisionReceipt,
    ContactDraftRender,
    GroundedExcerpt,
)
from contact_intent_agency_bridge import ContactCapacitySnapshot  # noqa: E402
from contact_message_action_model import (  # noqa: E402
    ACTION_KIND_MESSAGE_ACTION,
    CONTACT_MESSAGE_ACTION_REQUEST_SCHEMA,
    sha256_text,
)


FIXTURE_ROLE = "isolated qualification fixture"
CARRIER_ROLE = "isolated qualification carrier"

# ==========================================================================
# 1. scenario term  <->  contract term
# ==========================================================================
SCENARIO_TERM_SEND = "SEND"
SCENARIO_TERM_DEFER = "DEFER"
SCENARIO_TERM_NO_ACTION = "NO_ACTION"

MAPPING_RULE = "scenario term -> existing Decision contract value; no new verb"

SCENARIO_TO_CONTRACT: dict[str, str] = {
    SCENARIO_TERM_SEND: DECISION_CONTACT_SELECTED,
    SCENARIO_TERM_DEFER: DECISION_DEFER,
    SCENARIO_TERM_NO_ACTION: DECISION_NO_ACTION,
}
CONTRACT_TO_SCENARIO: dict[str, str] = {
    contract: scenario for scenario, contract in SCENARIO_TO_CONTRACT.items()
}


class UnmappedScenarioTermError(ValueError):
    """A scenario term outside the frozen mapping was requested."""


def contract_term_for(scenario_term: str) -> str:
    """Map a scenario term onto the existing Decision contract value."""
    try:
        return SCENARIO_TO_CONTRACT[scenario_term]
    except KeyError:  # pragma: no cover - guard
        raise UnmappedScenarioTermError(
            f"scenario term {scenario_term!r} has no Decision contract mapping"
        ) from None


def scenario_term_for(contract_term: str) -> str:
    try:
        return CONTRACT_TO_SCENARIO[contract_term]
    except KeyError:  # pragma: no cover - guard
        raise UnmappedScenarioTermError(
            f"contract decision {contract_term!r} has no scenario term"
        ) from None


def decision_term_record() -> dict[str, Any]:
    """The record every CT0-9 artifact must carry about the SEND mapping."""
    return {
        "scenario_term": SCENARIO_TERM_SEND,
        "contract_term": DECISION_CONTACT_SELECTED,
        "mapping_rule": MAPPING_RULE,
        "decision_contract_schema": CONTACT_DECISION_RECEIPT_SCHEMA,
        "new_decision_verb_introduced": False,
        "contract_modules_modified": [],
        "scenario_terms": dict(SCENARIO_TO_CONTRACT),
    }


# ==========================================================================
# 2. capacity carrier
# ==========================================================================
INTERRUPTIBILITY_FREE = "FREE"
INTERRUPTIBILITY_LIGHT = "LIGHT"
INTERRUPTIBILITY_FOCUSED = "FOCUSED"
INTERRUPTIBILITY_ATOMIC = "ATOMIC"
INTERRUPTIBILITY_LEVELS = (
    INTERRUPTIBILITY_FREE,
    INTERRUPTIBILITY_LIGHT,
    INTERRUPTIBILITY_FOCUSED,
    INTERRUPTIBILITY_ATOMIC,
)

AUTHORIZATION_GRANTED = "GRANTED"
AUTHORIZATION_REVOKED = "REVOKED"

# Only ATOMIC is a blocker. The frozen CT0-4 capacity contract expresses
# interruptibility as a single `atomic` flag; FREE / LIGHT / FOCUSED are not
# blockers there. Whether a real Activity owner should soften capacity for
# FOCUSED is a CT0-10 canonical question and is deliberately NOT decided here.
BLOCKING_INTERRUPTIBILITY = (INTERRUPTIBILITY_ATOMIC,)

# `ContactCapacitySnapshot.quiet_window_open=True` means "a contact window is
# open" (BLOCK_QUIET_WINDOW == "QUIET_WINDOW_CLOSED"). This fixture stores the
# *blocking* fact (`quiet_window=True` == quiet hours are active) and inverts it
# when building the snapshot, so the gate cannot be silently built backwards.
QUIET_WINDOW_INVERSION_NOTE = (
    "state.quiet_window=True means quiet hours are active (blocking); "
    "snapshot.quiet_window_open = not state.quiet_window"
)

DEFAULT_OBSERVED_AT = "2026-09-30T00:00:00Z"


@dataclass(frozen=True, slots=True)
class IsolatedCapacityState:
    """One published, immutable capacity observation."""

    revision: int
    observed_at: str
    interruptibility: str = INTERRUPTIBILITY_FREE
    device_available: bool = True
    channel_available: bool = True
    quiet_window: bool = False
    pending_proactive: int = 0
    previous_unknown: bool = False
    unresolved_inbound: bool = False
    authorization: str = AUTHORIZATION_GRANTED

    def __post_init__(self) -> None:
        if self.interruptibility not in INTERRUPTIBILITY_LEVELS:
            raise ValueError(f"unsupported interruptibility {self.interruptibility!r}")
        if self.authorization not in (AUTHORIZATION_GRANTED, AUTHORIZATION_REVOKED):
            raise ValueError(f"unsupported authorization {self.authorization!r}")
        for name in (
            "device_available",
            "channel_available",
            "quiet_window",
            "previous_unknown",
            "unresolved_inbound",
        ):
            if not isinstance(getattr(self, name), bool):
                raise ValueError(f"{name} must be a bool")
        if not isinstance(self.pending_proactive, int) or isinstance(
            self.pending_proactive, bool
        ):
            raise ValueError("pending_proactive must be an int")
        if self.pending_proactive < 0:
            raise ValueError("pending_proactive must be >= 0")
        if not isinstance(self.revision, int) or isinstance(self.revision, bool):
            raise ValueError("revision must be an int")
        if self.revision < 1:
            raise ValueError("revision must be >= 1")

    @property
    def atomic(self) -> bool:
        return self.interruptibility in BLOCKING_INTERRUPTIBILITY

    @property
    def authorization_granted(self) -> bool:
        return self.authorization == AUTHORIZATION_GRANTED

    def to_mapping(self) -> dict[str, Any]:
        return {
            "revision": self.revision,
            "observed_at": self.observed_at,
            "interruptibility": self.interruptibility,
            "device_available": self.device_available,
            "channel_available": self.channel_available,
            "quiet_window": self.quiet_window,
            "pending_proactive": self.pending_proactive,
            "previous_unknown": self.previous_unknown,
            "unresolved_inbound": self.unresolved_inbound,
            "authorization": self.authorization,
        }

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "IsolatedCapacityState":
        return cls(
            revision=int(raw["revision"]),
            observed_at=str(raw["observed_at"]),
            interruptibility=str(raw["interruptibility"]),
            device_available=bool(raw["device_available"]),
            channel_available=bool(raw["channel_available"]),
            quiet_window=bool(raw["quiet_window"]),
            pending_proactive=int(raw["pending_proactive"]),
            previous_unknown=bool(raw["previous_unknown"]),
            unresolved_inbound=bool(raw["unresolved_inbound"]),
            authorization=str(raw["authorization"]),
        )

    def to_snapshot(self) -> ContactCapacitySnapshot:
        """Project onto the frozen CT0-4 snapshot; read-only, no side effect."""
        return ContactCapacitySnapshot(
            observed_at=self.observed_at,
            revision=self.revision,
            quiet_window_open=not self.quiet_window,
            device_available=self.device_available,
            channel_available=self.channel_available,
            atomic=self.atomic,
            pending_outbound_count=self.pending_proactive,
            previous_unknown=self.previous_unknown,
            unresolved_inbound=self.unresolved_inbound,
        )


class CapacityCarrierNotReadOnlyError(RuntimeError):
    """Raised when a mutator is offered to the pipeline-visible carrier."""


class FixtureBackedIsolatedContactCapacity:
    """READ-ONLY capacity carrier. Produces snapshots only.

    NOT A CANONICAL OWNER. NOT AN ACTIVITY WRITER. NOT AN AGENCY WRITER.
    NOT A CONTACTINTENT WRITER.

    The pipeline receives this object and can only read it. Advancing the
    carrier requires `IsolatedCapacityPublisher`, which the harness keeps
    private. Changing capacity can therefore never create an Intent, never
    cause a Decision by itself and never auto-send; at most the harness may
    re-evaluate an *existing* Intent.

    Public (pipeline-visible) surface is exactly READ_ONLY_SURFACE.
    """

    READ_ONLY_SURFACE = frozenset(
        {
            "role",
            "canonical_owner",
            "writes_canonical_state",
            "revision",
            "observed_at",
            "current",
            "state",
            "ledger",
            "read_count",
            "snapshot",
            "interruptibility",
            "device_available",
            "channel_available",
            "quiet_window",
            "pending_proactive",
            "previous_unknown",
            "unresolved_inbound",
            "authorization_granted",
            "to_mapping",
            "assert_read_only_surface",
        }
    )
    MUTATOR_NAMES = frozenset(
        {
            "publish",
            "set",
            "update",
            "advance",
            "observe",
            "write",
            "revoke",
            "grant",
            "mutate",
            "reset",
            "clear",
            "delete",
            "remove",
        }
    )

    role = CARRIER_ROLE
    canonical_owner = False
    writes_canonical_state = False

    def __init__(self, state: IsolatedCapacityState | None = None) -> None:
        initial = state or IsolatedCapacityState(
            revision=1, observed_at=DEFAULT_OBSERVED_AT
        )
        self.__ledger: list[IsolatedCapacityState] = [initial]
        self.__reads = 0

    # -- read-only surface -------------------------------------------------
    @property
    def ledger(self) -> tuple[IsolatedCapacityState, ...]:
        return tuple(self.__ledger)

    @property
    def current(self) -> IsolatedCapacityState:
        return self.__ledger[-1]

    # `state` is an alias kept for artifact bookkeeping.
    @property
    def state(self) -> IsolatedCapacityState:
        return self.current

    @property
    def revision(self) -> int:
        return self.current.revision

    @property
    def observed_at(self) -> str:
        return self.current.observed_at

    @property
    def interruptibility(self) -> str:
        return self.current.interruptibility

    @property
    def device_available(self) -> bool:
        return self.current.device_available

    @property
    def channel_available(self) -> bool:
        return self.current.channel_available

    @property
    def quiet_window(self) -> bool:
        return self.current.quiet_window

    @property
    def pending_proactive(self) -> int:
        return self.current.pending_proactive

    @property
    def previous_unknown(self) -> bool:
        return self.current.previous_unknown

    @property
    def unresolved_inbound(self) -> bool:
        return self.current.unresolved_inbound


    @property
    def authorization_granted(self) -> bool:
        return self.current.authorization_granted

    @property
    def read_count(self) -> int:
        return self.__reads

    def snapshot(self) -> ContactCapacitySnapshot:
        """The only thing the pipeline may obtain: a detached snapshot."""
        self.__reads += 1
        return self.current.to_snapshot()

    def to_mapping(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "canonical_owner": False,
            "writes_canonical_state": False,
            "reads": self.__reads,
            "ledger": [state.to_mapping() for state in self.__ledger],
        }

    # -- publisher-only hook ----------------------------------------------
    def _publish_state(self, state: IsolatedCapacityState) -> None:
        """Only IsolatedCapacityPublisher may call this (name-underscored)."""
        self.__ledger.append(state)

    # -- self defence ------------------------------------------------------
    def __getattr__(self, name: str) -> Any:
        if name in self.MUTATOR_NAMES:
            raise CapacityCarrierNotReadOnlyError(
                f"the isolated capacity carrier is read-only; {name!r} must go "
                "through IsolatedCapacityPublisher which the pipeline never holds"
            )
        raise AttributeError(name)

    def assert_read_only_surface(self) -> tuple[str, ...]:
        """Return any public name outside READ_ONLY_SURFACE (must be empty)."""
        public = {
            name
            for name in dir(type(self))
            if not name.startswith("_")
        }
        metadata = {"READ_ONLY_SURFACE", "MUTATOR_NAMES"}
        return tuple(sorted(public - set(self.READ_ONLY_SURFACE) - metadata))


class IsolatedCapacityPublisher:
    """Test-owned mutator for `FixtureBackedIsolatedContactCapacity`.

    Held by the harness only. It is deliberately a different object so the
    pipeline cannot mutate capacity by construction. Every publish advances the
    revision by exactly 1 and records the fact in the carrier ledger.
    """

    def __init__(
        self,
        carrier: FixtureBackedIsolatedContactCapacity,
        *,
        step_seconds: int = 60,
    ) -> None:
        if step_seconds <= 0:
            raise ValueError("step_seconds must be positive")
        self._carrier = carrier
        self._step = step_seconds
        self.mutations: list[dict[str, Any]] = []

    @property
    def carrier(self) -> FixtureBackedIsolatedContactCapacity:
        return self._carrier

    def _next_observed_at(self, seconds: int | None = None) -> str:
        from datetime import datetime, timedelta, timezone

        current = datetime.fromisoformat(
            self._carrier.current.observed_at.replace("Z", "+00:00")
        ).astimezone(timezone.utc)
        moved = current + timedelta(seconds=self._step if seconds is None else seconds)
        return moved.isoformat(timespec="microseconds").replace("+00:00", "Z")

    def publish(
        self,
        *,
        advance_seconds: int | None = None,
        **changes: Any,
    ) -> IsolatedCapacityState:
        """Publish the next revision of capacity facts."""
        previous = self._carrier.current
        candidate = replace(
            previous,
            revision=previous.revision + 1,
            observed_at=self._next_observed_at(advance_seconds),
            **changes,
        )
        self._carrier._publish_state(candidate)
        self.mutations.append(
            {
                "revision": candidate.revision,
                "observed_at": candidate.observed_at,
                "changes": {
                    key: getattr(candidate, key) for key in sorted(changes)
                },
                "published_by": "fixture",
            }
        )
        return candidate

    def publish_explicit(self, state: IsolatedCapacityState) -> IsolatedCapacityState:
        """Publish a fully formed state; revision must strictly advance."""
        if state.revision <= self._carrier.revision:
            raise ValueError("published capacity revision must strictly advance")
        self._carrier._publish_state(state)
        self.mutations.append(
            {
                "revision": state.revision,
                "observed_at": state.observed_at,
                "changes": state.to_mapping(),
                "published_by": "fixture",
            }
        )
        return state


# ==========================================================================
# 3. recording AG-0 / opportunity factory / AG-1 decision service
# ==========================================================================
DECISION_OWNER_ISOLATED_FIXTURE = "ISOLATED_AG1_FIXTURE"
AG0_OWNER_ISOLATED_FIXTURE = "ISOLATED_AG0_FIXTURE"


def _digest(*parts: str) -> str:
    return hashlib.sha256("\x00".join(parts).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class IsolatedCandidateSet:
    candidate_set_id: str
    intent_ref: str
    candidate_ref: str
    observed_at: str
    owner: str = AG0_OWNER_ISOLATED_FIXTURE


@dataclass(frozen=True, slots=True)
class IsolatedDecisionOpportunity:
    opportunity_id: str
    trigger_kind: str
    trigger_ref: str
    candidate_set_id: str
    current_activity_ref: str | None
    observed_at: str
    valid_until: str
    causal_parent_refs: tuple[str, ...]
    owner: str = DECISION_OWNER_ISOLATED_FIXTURE


@dataclass(frozen=True, slots=True)
class IsolatedAgencyDecision:
    """What the isolated AG-1 fixture service chose. Not a canonical Decision."""

    scenario_term: str
    contract_term: str
    decision_ref: str
    reason: str
    resume_condition: str | None
    opportunity_ref: str
    owner: str = DECISION_OWNER_ISOLATED_FIXTURE

    @property
    def is_send(self) -> bool:
        return self.contract_term == DECISION_CONTACT_SELECTED

    def to_mapping(self) -> dict[str, Any]:
        return {
            "scenario_term": self.scenario_term,
            "contract_term": self.contract_term,
            "decision_ref": self.decision_ref,
            "reason": self.reason,
            "resume_condition": self.resume_condition,
            "opportunity_ref": self.opportunity_ref,
            "owner": self.owner,
        }


class IsolatedDecisionScript:
    """Deterministic decision policy for the isolated AG-1 fixture.

    The harness supplies the outcome per intent. Nothing in the pipeline can
    change it: the script is consulted only by `RecordingAgencyDecisionService`.
    """

    def __init__(
        self,
        outcomes: Mapping[str, str] | None = None,
        *,
        default: str = SCENARIO_TERM_SEND,
    ) -> None:
        self._outcomes = dict(outcomes or {})
        self._default = default
        for term in self._outcomes.values():
            contract_term_for(term)
        contract_term_for(default)

    def term_for(self, *, intent_ref: str, candidate_set_id: str) -> str:
        return self._outcomes.get(intent_ref, self._default)

    def set(self, intent_ref: str, scenario_term: str) -> None:
        contract_term_for(scenario_term)
        self._outcomes[intent_ref] = scenario_term

    def to_mapping(self) -> dict[str, Any]:
        return {"outcomes": dict(self._outcomes), "default": self._default}


class RecordingAg0Port:
    """Ag0ContactCandidatePort double. No canonical AG-0 is vendored."""

    owner = AG0_OWNER_ISOLATED_FIXTURE

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.fail_next: str | None = None

    def build_candidate_set(self, *, intent: Any, observed_at: str) -> IsolatedCandidateSet:
        if self.fail_next is not None:
            message, self.fail_next = self.fail_next, None
            raise RuntimeError(message)
        self.calls.append({"intent": intent, "observed_at": observed_at})
        return IsolatedCandidateSet(
            candidate_set_id=f"cset:{_digest('ct0-9-isolated-ag0', intent.intent_id)[:32]}",
            intent_ref=intent.intent_id,
            candidate_ref=intent.candidate_ref,
            observed_at=observed_at,
        )


class RecordingOpportunityFactory:
    """DecisionOpportunityFactory double."""

    owner = DECISION_OWNER_ISOLATED_FIXTURE

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.fail_next: str | None = None

    def create(
        self,
        *,
        trigger_kind: str,
        trigger_ref: str,
        candidate_set: Any,
        current_activity_ref: str | None,
        observed_at: str,
        causal_parent_refs: tuple[str, ...],
        valid_until: str,
    ) -> IsolatedDecisionOpportunity:
        if self.fail_next is not None:
            message, self.fail_next = self.fail_next, None
            raise RuntimeError(message)
        record = {
            "trigger_kind": trigger_kind,
            "trigger_ref": trigger_ref,
            "candidate_set_id": candidate_set.candidate_set_id,
            "current_activity_ref": current_activity_ref,
            "observed_at": observed_at,
            "causal_parent_refs": tuple(causal_parent_refs),
            "valid_until": valid_until,
        }
        self.calls.append(record)
        return IsolatedDecisionOpportunity(
            opportunity_id=f"opp:{_digest('ct0-9-isolated-opp', trigger_ref, candidate_set.candidate_set_id)[:32]}",
            trigger_kind=trigger_kind,
            trigger_ref=trigger_ref,
            candidate_set_id=candidate_set.candidate_set_id,
            current_activity_ref=current_activity_ref,
            observed_at=observed_at,
            valid_until=valid_until,
            causal_parent_refs=tuple(causal_parent_refs),
        )


class RecordingAgencyDecisionService:
    """AgencyDecisionServicePort double: the stand-in AG-1 *owner*.

    It mints a fixture decision identity in the `dec:` namespace so the real
    CT0-3 authority can bind it. It is NOT a canonical AG-1 implementation and
    the artifact must say so.
    """

    owner = DECISION_OWNER_ISOLATED_FIXTURE

    def __init__(self, script: IsolatedDecisionScript | None = None) -> None:
        self.script = script or IsolatedDecisionScript()
        self.calls: list[dict[str, Any]] = []
        self.decisions: list[IsolatedAgencyDecision] = []
        self.fail_next: str | None = None

    def evaluate_opportunity(
        self, *, opportunity: Any, candidate_set: Any
    ) -> IsolatedAgencyDecision:
        if self.fail_next is not None:
            message, self.fail_next = self.fail_next, None
            raise RuntimeError(message)
        self.calls.append({"opportunity": opportunity, "candidate_set": candidate_set})
        scenario_term = self.script.term_for(
            intent_ref=opportunity.trigger_ref,
            candidate_set_id=candidate_set.candidate_set_id,
        )
        contract_term = contract_term_for(scenario_term)
        decision = IsolatedAgencyDecision(
            scenario_term=scenario_term,
            contract_term=contract_term,
            decision_ref=f"dec:{_digest('ct0-9-isolated-ag1', opportunity.opportunity_id, contract_term)[:32]}",
            reason={
                SCENARIO_TERM_SEND: "isolated fixture: sendable window observed",
                SCENARIO_TERM_DEFER: "isolated fixture: defer until condition",
                SCENARIO_TERM_NO_ACTION: "isolated fixture: no action chosen",
            }[scenario_term],
            resume_condition=(
                "capacity_available_and_authorized"
                if scenario_term == SCENARIO_TERM_DEFER
                else None
            ),
            opportunity_ref=opportunity.opportunity_id,
        )
        self.decisions.append(decision)
        return decision


def decision_receipt_for(
    *,
    intent: Any,
    decision: IsolatedAgencyDecision,
    committed_at: str,
) -> ContactDecisionReceipt:
    """Project an isolated fixture decision onto the frozen Decision contract."""
    contract_term = decision.contract_term
    return ContactDecisionReceipt(
        schema_version=CONTACT_DECISION_RECEIPT_SCHEMA,
        committed=True,
        intent_ref=intent.intent_id,
        candidate_ref=intent.candidate_ref,
        decision_ref=decision.decision_ref,
        outcome=contract_term,
        selected_candidate_ref=(
            intent.candidate_ref if contract_term == DECISION_CONTACT_SELECTED else None
        ),
        committed_at=committed_at,
    )


# ==========================================================================
# 4. deterministic expression fixture
# ==========================================================================
EXPRESSION_OWNERSHIP_VIOLATION = "EXPRESSION_OWNERSHIP_VIOLATION"
EXPRESSION_FAILED = "EXPRESSION_FAILED"


class ExpressionOwnershipViolationError(AssertionError):
    """The expression fixture was asked to decide or to send."""


class IsolatedExpressionFailure(RuntimeError):
    """Deterministic, injectable expression generation failure."""


@dataclass(frozen=True, slots=True)
class ExpressionRenderRecord:
    intent_ref: str
    decision_ref: str
    source_refs: tuple[str, ...]
    content_hash: str
    render_index: int

    def to_mapping(self) -> dict[str, Any]:
        return {
            "intent_ref": self.intent_ref,
            "decision_ref": self.decision_ref,
            "source_refs": list(self.source_refs),
            "content_hash": self.content_hash,
            "render_index": self.render_index,
        }


class DeterministicExpressionRenderer:
    """ContactDraftRenderer fixture: Decision + grounded inputs -> Draft.

    It answers only *how to say it*. It never chooses whether to say it, and it
    has no capability to submit a CONTACT_SELECTED decision. Every attempt to
    ask it to decide or to send is recorded and raises.
    """

    role = FIXTURE_ROLE

    def __init__(self, *, mode: str = "verbatim") -> None:
        if mode not in ("verbatim", "fail"):
            raise ValueError("mode must be 'verbatim' or 'fail'")
        self.mode = mode
        self.calls: int = 0
        self.renders: list[ExpressionRenderRecord] = []
        self.ownership_violations: list[str] = []
        self.submissions: int = 0
        self.decisions_made: int = 0

    # -- forbidden capabilities -------------------------------------------
    def _forbidden(self, capability: str) -> None:
        self.ownership_violations.append(capability)
        raise ExpressionOwnershipViolationError(
            f"expression has no send/decide authority: {capability}"
        )

    def submit(self, *_args: Any, **_kwargs: Any) -> Any:
        self.submissions += 1
        self._forbidden("submit")

    def send(self, *_args: Any, **_kwargs: Any) -> Any:
        self.submissions += 1
        self._forbidden("send")

    def decide(self, *_args: Any, **_kwargs: Any) -> Any:
        self.decisions_made += 1
        self._forbidden("decide")

    def select_contact(self, *_args: Any, **_kwargs: Any) -> Any:
        self.decisions_made += 1
        self._forbidden("select_contact")

    # -- the one legitimate capability ------------------------------------
    def render(
        self,
        *,
        intent: Any,
        decision: ContactDecisionReceipt,
        grounding_sources: Sequence[Any],
    ) -> ContactDraftRender:
        self.calls += 1
        if not isinstance(decision, ContactDecisionReceipt):
            raise IsolatedExpressionFailure("expression requires a typed Decision receipt")
        if not decision.committed:
            raise IsolatedExpressionFailure("expression requires a committed Decision")
        if decision.outcome != DECISION_CONTACT_SELECTED:
            # Not allowed to substitute its own decision.
            raise IsolatedExpressionFailure(
                "expression may not act on a non-SELECTED decision"
            )
        if decision.intent_ref != intent.intent_id:
            raise IsolatedExpressionFailure("decision is not bound to this intent")
        if self.mode == "fail":
            raise IsolatedExpressionFailure("deterministic expression failure fixture")
        sources = tuple(grounding_sources)
        if not sources:
            raise IsolatedExpressionFailure("expression requires grounded inputs")
        excerpts = tuple(
            GroundedExcerpt(source.source_ref, source.canonical_text) for source in sources
        )
        render = ContactDraftRender(
            schema_version=CONTACT_DRAFT_RENDER_SCHEMA, excerpts=excerpts
        )
        content = " ".join(excerpt.exact_text for excerpt in excerpts)
        self.renders.append(
            ExpressionRenderRecord(
                intent_ref=intent.intent_id,
                decision_ref=decision.decision_ref,
                source_refs=tuple(source.source_ref for source in sources),
                content_hash=hashlib.sha256(content.encode("utf-8")).hexdigest(),
                render_index=self.calls,
            )
        )
        return render

    def to_mapping(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "mode": self.mode,
            "render_calls": self.calls,
            "ownership_violations": list(self.ownership_violations),
            "submissions": self.submissions,
            "decisions_made": self.decisions_made,
            "renders": [record.to_mapping() for record in self.renders],
        }


# ==========================================================================
# 5. AR-0 recording port + fake action ledger
# ==========================================================================
AR0_OWNER_ISOLATED_FIXTURE = "ISOLATED_AR0_FAKE_LEDGER"
AR0_NO_TRANSPORT_VIOLATION = "AR0_NO_TRANSPORT_VIOLATION"


class IsolatedAr0TransportViolationError(AssertionError):
    """The fake AR-0 ledger was asked to reach a real transport."""

def payload_hash_for(request: Any) -> str:
    """Canonical provider payload hash for one CT0-6 request.

    This mirrors the field the receipt authority itself uses
    (`sha256_text(request.payload_json)`), so the harness never invents a
    second payload-hash recipe.
    """
    return sha256_text(request.payload_json)



class IsolatedAr0Ledger:
    """Fake AR-0 action ledger. Owns the fake provider effect, not a transport.

    Idempotent on `request.idempotency_key`: the same logical action can be
    proposed any number of times and yields one logical record and at most one
    fake provider effect. No network capability exists: `submit`/`deliver`
    raise and are recorded as violations.
    """

    owner = AR0_OWNER_ISOLATED_FIXTURE

    def __init__(self) -> None:
        self.logical_records: dict[str, dict[str, Any]] = {}
        self.effects: dict[str, dict[str, Any]] = {}
        self.effect_order: list[str] = []
        self.proposals: int = 0
        self.replays: int = 0
        self.transport_violations: list[str] = []

    # -- forbidden capabilities -------------------------------------------
    def _forbidden(self, capability: str) -> None:
        self.transport_violations.append(capability)
        raise IsolatedAr0TransportViolationError(
            f"the isolated AR-0 ledger has no transport capability: {capability}"
        )

    def submit(self, *_args: Any, **_kwargs: Any) -> Any:
        self._forbidden("submit")

    def deliver(self, *_args: Any, **_kwargs: Any) -> Any:
        self._forbidden("deliver")

    def connect(self, *_args: Any, **_kwargs: Any) -> Any:
        self._forbidden("connect")

    # -- deterministic identity helpers -----------------------------------
    @staticmethod
    def _action_id_for(idempotency_key: str) -> str:
        return f"actn:{_digest('ct0-9-ar0-action', idempotency_key)[:16]}"

    def payload_hash_for(self, key: str) -> str | None:
        stored = self.logical_records.get(key)
        return stored["payload_hash"] if stored else None

    # -- the legitimate AR-0 proposal surface -----------------------------
    def propose(self, request: Any) -> dict[str, Any]:
        key = request.idempotency_key
        if key in self.logical_records:
            self.replays += 1
            stored = self.logical_records[key]
            return {
                "idempotent_replay": True,
                "proposal": dict(stored["proposal"]),
                "action": dict(stored["action"]),
            }
        self.proposals += 1
        proposal_id = f"aprop:{_digest('ct0-9-ar0', request.request_id)[:16]}"
        action_id = self._action_id_for(key)
        payload_hash = payload_hash_for(request)
        proposal = {
            "proposal_id": proposal_id,
            "action_kind": ACTION_KIND_MESSAGE_ACTION,
            "target_ref": request.target_ref,
            "payload_ref": request.payload_ref,
            "decision_ref": request.decision_ref,
            "authorization_ref": request.authorization_ref,
            "source_refs": list(request.source_refs),
            "causal_parent_refs": list(request.causal_parent_refs),
        }
        action = {
            "action_id": action_id,
            "status": "PROPOSED",
            "idempotency_key": key,
            "action_kind": ACTION_KIND_MESSAGE_ACTION,
            "proposal_ref": proposal_id,
            "target_ref": request.target_ref,
            "payload_ref": request.payload_ref,
            "decision_ref": request.decision_ref,
            "authorization_ref": request.authorization_ref,
            "source_refs": list(request.source_refs),
            "causal_parent_refs": list(request.causal_parent_refs),
        }
        stored = {
            "proposal": proposal,
            "action": action,
            "request_id": request.request_id,
            "content_hash": request.content_hash,
            "recipient_ref": request.recipient_ref,
            "channel": request.channel,
            "intent_ref": request.intent_ref,
            "candidate_ref": request.candidate_ref,
            "decision_ref": request.decision_ref,
            "draft_ref": request.draft_ref,
            "payload_hash": payload_hash,
        }
        self.logical_records[key] = stored
        return {"idempotent_replay": False, "proposal": proposal, "action": action}

    # -- fake provider effect ---------------------------------------------
    def commit_fake_effect(self, request: Any) -> dict[str, Any]:
        """Commit the fake provider-side effect for one logical action.

        At most one effect per idempotency key, exactly like the real provider
        contract this fixture stands in for. This is the object that survives a
        crash between effect and local receipt (§70).
        """
        key = request.idempotency_key
        if key in self.effects:
            return dict(self.effects[key])
        record = {
            "effect_ref": f"eff:{_digest('ct0-9-effect', key)[:32]}",
            "idempotency_key": key,
            "action_ref": self._action_id_for(key),
            "payload_hash": payload_hash_for(request),
            "recipient_ref": request.recipient_ref,
            "channel": request.channel,
            "content_hash": request.content_hash,
            "submission_key": key,
            "provider_message_ref": f"pmsg:{_digest('ct0-9-pmsg', key)[:16]}",
            "transport_record_ref": f"tlog:{_digest('ct0-9-tlog', key)[:16]}",
            "effect_count_for_key": 1,
        }
        self.effects[key] = record
        self.effect_order.append(key)
        return dict(record)

    def effect_count_for(self, key: str) -> int:
        return 1 if key in self.effects else 0

    @property
    def effect_count(self) -> int:
        return len(self.effects)

    def effect_for(self, key: str) -> dict[str, Any] | None:
        record = self.effects.get(key)
        return dict(record) if record else None

    def logical_action_ids(self) -> tuple[str, ...]:
        return tuple(
            sorted(stored["action"]["action_id"] for stored in self.logical_records.values())
        )

    def to_mapping(self) -> dict[str, Any]:
        return {
            "owner": self.owner,
            "logical_records": json.loads(json.dumps(self.logical_records)),
            "effects": json.loads(json.dumps(self.effects)),
            "effect_order": list(self.effect_order),
            "proposals": self.proposals,
            "replays": self.replays,
            "transport_violations": list(self.transport_violations),
        }

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "IsolatedAr0Ledger":
        ledger = cls()
        ledger.logical_records = json.loads(json.dumps(raw["logical_records"]))
        ledger.effects = json.loads(json.dumps(raw["effects"]))
        ledger.effect_order = list(raw["effect_order"])
        ledger.proposals = int(raw.get("proposals", 0))
        ledger.replays = int(raw.get("replays", 0))
        ledger.transport_violations = list(raw.get("transport_violations", []))
        return ledger


class RecordingAr0ActionPort:
    """Ar0MessageActionPort double over `IsolatedAr0Ledger`."""

    def __init__(self, *, owner: IsolatedAr0Ledger | None = None) -> None:
        self.owner = owner or IsolatedAr0Ledger()
        self.calls: list[Any] = []
        self.submit_count: int = 0

    def propose_message_action(self, *, request: Any) -> dict[str, Any]:
        self.calls.append(request)
        return self.owner.propose(request)

    def submit(self, *_args: Any, **_kwargs: Any) -> Any:
        self.submit_count += 1
        raise AssertionError(
            "the pipeline must never reach a submit path through the AR-0 bridge"
        )


# ==========================================================================
# 6. isolated source fixtures for the four non-life source kinds
# ==========================================================================
ISOLATED_SOURCE_FIXTURE = "ISOLATED_SOURCE_FIXTURE"
SOURCE_REQUIREMENT_NOT_MET = "REQUIREMENT_NOT_MET"
SOURCE_REQUIREMENT_MET_NO_CANONICAL_REPRESENTATION = (
    "REQUIREMENT_MET_NO_CANONICAL_REPRESENTATION"
)

# Required typed fixture ref per non-life source kind. Text alone is never
# enough; the fixture refuses a bare prose event.
NON_LIFE_SOURCE_REQUIREMENTS: dict[str, dict[str, Any]] = {
    SOURCE_SHARED_CONTEXT_RESULT: {
        "required_ref_prefix": "ctxresult:",
        "requirement": "a real fixture shared-context result ref",
    },
    SOURCE_SOCIAL_COMMITMENT: {
        "required_ref_prefix": "commitment:",
        "requirement": "a valid commitment ref, never text alone",
    },
    SOURCE_REPAIR_ITEM: {
        "required_ref_prefix": "repair:",
        "requirement": "an OPEN repair item (closed/expired cannot be contacted)",
    },
    SOURCE_EXPLICIT_FOLLOWUP: {
        "required_ref_prefix": "followup:",
        "requirement": "an explicit user follow-up request ref",
    },
}

NON_LIFE_SOURCE_KINDS = tuple(sorted(NON_LIFE_SOURCE_REQUIREMENTS))


class IsolatedNonLifeSourceFixture:
    """Requirement gate for source kinds with no canonical owner.

    It NEVER mints a ContactCandidate. The frozen CT0-2 candidate contract pins
    `source_kind == LIFE_EXPERIENCE`, so a candidate for these kinds is not
    representable at all; failing closed is the honest outcome and is recorded
    as such. Labelled ISOLATED_SOURCE_FIXTURE, never canonical integrated.
    """

    status_label = ISOLATED_SOURCE_FIXTURE
    canonical_integrated = False

    def __init__(self, *, registry: Any | None = None) -> None:
        self._registry = registry
        self.evaluations: list[dict[str, Any]] = []

    def registry_status(self) -> dict[str, str]:
        if self._registry is None:
            return {kind: SOURCE_DISABLED_NO_CANONICAL_OWNER for kind in CONTACT_SOURCE_KINDS}
        return {kind: status for kind, status in self._registry.source_status()}

    def evaluate(self, *, source_kind: str, ref: str | None, open_item: bool = True) -> dict[str, Any]:
        spec = NON_LIFE_SOURCE_REQUIREMENTS[source_kind]
        prefix = spec["required_ref_prefix"]
        text_only = ref is not None and not str(ref).startswith(prefix)
        met = (
            ref is not None
            and str(ref).startswith(prefix)
            and (open_item or source_kind != SOURCE_REPAIR_ITEM)
        )
        if not met:
            status = SOURCE_REQUIREMENT_NOT_MET
        else:
            status = SOURCE_REQUIREMENT_MET_NO_CANONICAL_REPRESENTATION
        record = {
            "source_kind": source_kind,
            "status": status,
            "status_label": self.status_label,
            "canonical_integrated": False,
            "requirement": spec["requirement"],
            "required_ref_prefix": prefix,
            "ref": ref,
            "text_alone_accepted": False,
            "refused_text_only": bool(text_only),
            "candidate_count": 0,
            "intent_count": 0,
            "action_count": 0,
            "registry_status": self.registry_status().get(source_kind),
            "reason": (
                "candidate contract pins source_kind == LIFE_EXPERIENCE; no candidate "
                "is representable, so the isolated fixture fails closed"
            ),
        }
        self.evaluations.append(record)
        return record

    def materialize_through_registry(self, *, source_kind: str, event: object) -> tuple[Any, ...]:
        if self._registry is None:
            return ()
        return tuple(self._registry.materialize(source_kind, event))

    def to_mapping(self) -> dict[str, Any]:
        return {
            "status_label": self.status_label,
            "canonical_integrated": False,
            "kinds": list(NON_LIFE_SOURCE_KINDS),
            "evaluations": list(self.evaluations),
        }


# ==========================================================================
# 7. handoff fixture (AG-2 v1 shape, copied from the frozen contract tests)
# ==========================================================================
HANDOFF_NOW = "2026-09-28T04:00:00+00:00"
SOURCE_TEXT = "We finished the small blue paper lantern together."


def life_experience_handoff(
    *,
    handoff_id: str = "lexp:handoff-001",
    correlation_id: str = "corr:integration-001",
    activity_ref: str = "act:activity-001",
    activity_kind: str = "SANDBOX_ARTWORK",
    milestone_kind: str = "COMPLETED",
    occurred_at: str = HANDOFF_NOW,
    observed_at: str | None = None,
    recorded_at: str | None = None,
    **overrides: Any,
) -> dict[str, Any]:
    """A minimal valid AG-2 v1 LifeExperience handoff mapping."""
    if milestone_kind not in AG2_HANDOFF_KINDS:
        raise ValueError(f"unsupported milestone kind {milestone_kind!r}")
    value: dict[str, Any] = {
        "handoff_id": handoff_id,
        "correlation_id": correlation_id,
        "activity_ref": activity_ref,
        "activity_revision": 4,
        "activity_kind": activity_kind,
        "terminal_or_milestone_kind": milestone_kind,
        "decision_refs": ["dec:decision-001"],
        "adoption_refs": ["adopt:adoption-001"],
        "action_refs": ["actn:action-001"],
        "settlement_refs": ["settle:settlement-001"],
        "progress_refs": ["progress:completion-001"],
        "completion_evidence_ref": "evidence:completion-001",
        "fact_refs": ["fact:fact-001"],
        "artifact_refs": ["artifact:artifact-001"],
        "world_refs": [],
        "occurred_at": occurred_at,
        "observed_at": observed_at or occurred_at,
        "recorded_at": recorded_at or occurred_at,
        "causal_parent_refs": [
            "act:activity-001",
            "dec:decision-001",
            "settle:settlement-001",
        ],
        "subject_id": GLOBAL_SUBJECT_ID,
        "policy_version": "ag2.life_integration.v1",
        "schema_version": AG2_HANDOFF_SCHEMA,
    }
    value.update(overrides)
    return value


def life_experience_authorization(
    *,
    activity_kind: str = "SANDBOX_ARTWORK",
    recipient_ref: str = "recipient:ct09-e2e",
    channel_options: tuple[str, ...] = ("channel:telegram-sandbox",),
    allowed_handoff_kinds: tuple[str, ...] = ("COMPLETED",),
) -> Any:
    from contact_candidate_sources import LifeExperienceAuthorization

    return LifeExperienceAuthorization(
        activity_kind=activity_kind,
        recipient_ref=recipient_ref,
        channel_options=channel_options,
        allowed_handoff_kinds=allowed_handoff_kinds,
    )


__all__ = [
    "AG0_OWNER_ISOLATED_FIXTURE",
    "AR0_NO_TRANSPORT_VIOLATION",
    "AR0_OWNER_ISOLATED_FIXTURE",
    "AUTHORIZATION_GRANTED",
    "AUTHORIZATION_REVOKED",
    "BLOCKING_INTERRUPTIBILITY",
    "CARRIER_ROLE",
    "CONTRACT_TO_SCENARIO",
    "CapacityCarrierNotReadOnlyError",
    "DECISION_OWNER_ISOLATED_FIXTURE",
    "DEFAULT_OBSERVED_AT",
    "DeterministicExpressionRenderer",
    "EXPRESSION_FAILED",
    "EXPRESSION_OWNERSHIP_VIOLATION",
    "ExpressionOwnershipViolationError",
    "ExpressionRenderRecord",
    "FIXTURE_ROLE",
    "FixtureBackedIsolatedContactCapacity",
    "HANDOFF_NOW",
    "ISOLATED_SOURCE_FIXTURE",
    "INTERRUPTIBILITY_ATOMIC",
    "INTERRUPTIBILITY_FOCUSED",
    "INTERRUPTIBILITY_FREE",
    "INTERRUPTIBILITY_LEVELS",
    "INTERRUPTIBILITY_LIGHT",
    "IsolatedAgencyDecision",
    "IsolatedAr0Ledger",
    "IsolatedAr0TransportViolationError",
    "IsolatedCandidateSet",
    "IsolatedCapacityPublisher",
    "IsolatedCapacityState",
    "IsolatedDecisionOpportunity",
    "IsolatedDecisionScript",
    "IsolatedExpressionFailure",
    "IsolatedNonLifeSourceFixture",
    "MAPPING_RULE",
    "NON_LIFE_SOURCE_KINDS",
    "NON_LIFE_SOURCE_REQUIREMENTS",
    "QUIET_WINDOW_INVERSION_NOTE",
    "RecordingAg0Port",
    "RecordingAgencyDecisionService",
    "RecordingAr0ActionPort",
    "RecordingOpportunityFactory",
    "SCENARIO_TERM_DEFER",
    "SCENARIO_TERM_NO_ACTION",
    "SCENARIO_TERM_SEND",
    "SCENARIO_TO_CONTRACT",
    "SOURCE_REQUIREMENT_MET_NO_CANONICAL_REPRESENTATION",
    "SOURCE_REQUIREMENT_NOT_MET",
    "SOURCE_TEXT",
    "UnmappedScenarioTermError",
    "contract_term_for",
    "decision_receipt_for",
    "decision_term_record",
    "life_experience_authorization",
    "life_experience_handoff",
    "payload_hash_for",
    "scenario_term_for",
]
