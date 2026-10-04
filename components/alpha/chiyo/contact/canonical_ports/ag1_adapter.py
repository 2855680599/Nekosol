"""CT0-10 Phase E - canonical AG-1 decision port (order sections 38-43).

``CanonicalAg1DecisionAdapter`` implements ``ct0_10.canonical_spine.CanonicalAg1DecisionPort`` by
driving the REAL AG-1 owner ``scripts/agency_decision_ag1.py`` (aliased ``ag1`` below) with a real
``AgencyDecisionAuthority`` capability, a real ``AgencyDecisionStore`` and a real
``AgencyDecisionService``:

    real AG-0 CandidateSet (Phase D)
        -> DecisionOpportunity.create                     (real, event-driven trigger)
        -> AgencyDecisionService.evaluate_opportunity     (real 3-layer pipeline)
        -> real DecisionRecord inside the real store       (immutable, pre-adoption)

CONTACT_SELECTED is NOT an AG-1 verb
------------------------------------
* the real AG-1 decision vocabulary is ``VALID_DECISION_OUTPUTS`` (agency_decision_ag1.py:178-198):
  START / CONTINUE / PAUSE / WAIT / RESUME / ABANDON / DEFER / NO_ACTION;
* ``CONTACT_SELECTED`` has 0 occurrences in the real owner (and in AG-0);
* the spine's declared mapping ``CONTACT_SELECTED -> ACTION_SELECTED`` is the *runtime producer's*
  outcome family (``service/bridge/decision_trigger.py:51-54``), NOT an AG-1 decision verb:
  ``DecisionOutputValidator.validate_decision_output`` refuses it with
  ``DecisionValidationError(..., failure_class="INVALID_MODEL_OUTPUT:ILLEGAL_DECISION_ENUM")``
  (:2323-2327). The test proves that refusal and then uses the real verb the owner accepts.
* this port therefore declares its own explicit table
  ``CONTACT_TO_REAL_AG1_DECISION = {CONTACT_SELECTED: START, NO_ACTION: NO_ACTION, DEFER: DEFER}``
  and validates it against ``VALID_DECISION_OUTPUTS`` / ``FORBIDDEN_DECISION_OUTPUTS`` at
  construction time. It never adds a verb to the AG-1 contract.
* the real trigger vocabulary has no CONTACT_INTENT (``VALID_OPPORTUNITY_TRIGGERS``, :229-240);
  the port uses the real ``CANDIDATE_SET_CHANGED`` trigger because its input is a freshly built
  canonical CandidateSet.

``CONTACT_SELECTED != Send`` (order 41): the real DecisionRecord has zero Action/Outbound authority
(``DecisionRecord.create_action_proposal`` :1695-1698,
``AgencyDecisionService.send_outbound_message`` :3375-3376), the port submits nothing, and it reports
``submitted=False`` plus the still-required downstream steps
(Expression render -> AR-0 action proposal -> dispatch admission).
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

_SPINE_SRC_ROOT = Path(__file__).resolve().parents[2]
if str(_SPINE_SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(_SPINE_SRC_ROOT))

from ct0_10.canonical_spine import (  # noqa: E402
    CANONICAL_AG1_VERBS,
    CanonicalCapacityResult,
    CanonicalIntegrationError,
    CONTACT_DECISION_DEFER,
    CONTACT_DECISION_NO_ACTION,
    CONTACT_DECISION_SEND,
    CONTACT_TO_CANONICAL_DECISION,
    EXPECTED_FACT_WRITERS,
    NAMESPACE_ISOLATED_TEST,
    QUIET_POLICY_MISSING,
    UnmappableDecisionError,
    assert_isolated_store_root,
)

from ct0_10.canonical_ports import ag0_adapter  # noqa: E402

RUNTIME_ROOT_ENV = "CHIYO_CANONICAL_RUNTIME_ROOT"
_DEFAULT_RUNTIME_ROOT = str(Path(__file__).resolve().parents[2] / "life_runtime")

AG1_OWNER_MODULE = "agency_decision_ag1"
AG1_OWNER_FILE = "scripts/agency_decision_ag1.py"
AG1_RUNTIME_PRODUCER_FILE = "service/bridge/decision_trigger.py"
PORT_ID = "ct0_10.canonical_ports.ag1_adapter.CanonicalAg1DecisionAdapter"

# ============================================================================
# 1. explicit declared mapping tables (order sections 38, 40)
# ============================================================================
#: contact outcome -> REAL AG-1 decision verb (the only verbs the real owner accepts)
CONTACT_TO_REAL_AG1_DECISION: dict[str, str] = {
    CONTACT_DECISION_SEND: "START",       # "consider this contact now" == start the contact activity
    CONTACT_DECISION_NO_ACTION: "NO_ACTION",
    CONTACT_DECISION_DEFER: "DEFER",
}
REAL_AG1_TO_CONTACT_DECISION: dict[str, str] = {
    verb: contact for contact, verb in CONTACT_TO_REAL_AG1_DECISION.items()
}
#: contact outcome -> the bridge/runtime producer's own outcome label (NEVER an AG-1 verb)
CONTACT_TO_RUNTIME_OUTCOME: dict[str, str] = {
    CONTACT_DECISION_SEND: "ACTION_SELECTED",
    CONTACT_DECISION_NO_ACTION: "NO_ACTION",
    CONTACT_DECISION_DEFER: "DEFER",
}
#: order 41: a selected contact is still NOT a send (Expression -> AR-0 -> dispatch admission)
CONTACT_DOWNSTREAM_REQUIRED: tuple[str, ...] = (
    "EXPRESSION_RENDER",
    "AR0_ACTION_PROPOSAL",
    "DISPATCH_ADMISSION",
)
#: contact outcome -> registered real DecisionReasonCode VALUE (no "REASON_" prefix in the real
#: registry: agency_decision_ag1.py:393-398) and the real constant name that must equal it.
CONTACT_TO_AG1_REASON_CODES: dict[str, tuple[str, ...]] = {
    CONTACT_DECISION_SEND: ("USER_REQUEST_VALID",),
    CONTACT_DECISION_NO_ACTION: ("NO_ACTION_CHOSEN",),
    CONTACT_DECISION_DEFER: ("USER_REQUEST_DEFERRED",),
}
CONTACT_TO_AG1_REASON_CODE_CONSTANTS: dict[str, tuple[str, ...]] = {
    CONTACT_DECISION_SEND: ("REASON_USER_REQUEST_VALID",),
    CONTACT_DECISION_NO_ACTION: ("REASON_NO_ACTION_CHOSEN",),
    CONTACT_DECISION_DEFER: ("REASON_USER_REQUEST_DEFERRED",),
}
CONTACT_TRIGGER_KIND_REJECTED_BY_REAL_OWNER = "CONTACT_INTENT"
CANONICAL_TRIGGER_KIND = "CANDIDATE_SET_CHANGED"
SPINE_MAPPING_REJECTED_NOTE = (
    "CONTACT_SELECTED and ACTION_SELECTED are BOTH refused by the real AG-1 validator "
    "(DecisionValidationError, failure_class INVALID_MODEL_OUTPUT:ILLEGAL_DECISION_ENUM, "
    "agency_decision_ag1.py:2323-2327): neither is an AG-1 decision verb. CONTACT_SELECTED is the "
    "frozen contact scenario term and ACTION_SELECTED is the runtime producer's outcome family "
    "(service/bridge/decision_trigger.py:51-54). The spine declares the adaptation and the port "
    "submits the real verb START for the contact SEND decision; CT0-10 adds no verb to the AG-1 "
    "contract and keeps ACTION_SELECTED only as the runtime-outcome projection."
)
#: declared capacity projection (order 24/29: never fabricate AVAILABLE)
CAPACITY_FACT_TO_AG1_INPUT: dict[str, str] = {
    "device_available": "device_availability_state",
    "channel_available": "resource_availability_state",
    "atomic": "life_frame.interruptibility",
}
CAPACITY_UNAVAILABLE_PROJECTION_NOTE = (
    "contact capacity result UNAVAILABLE -> AG-1 device+resource dimensions forced UNAVAILABLE; the "
    "real AG-1 has no quiet-window / pending-outbound / unknown-baseline dimension, so this is a "
    "declared conservative projection and never a silent pass"
)


class CanonicalAg1PortError(CanonicalIntegrationError):
    """The AG-1 port could not perform its job (fail closed)."""


class IntentNotEligibleAtDecisionError(CanonicalAg1PortError):
    """The contact intent was not eligible when the decision input was taken."""

    def __init__(self, message: str, *, case: str, contact_intent_ref: str, status: str) -> None:
        super().__init__(message)
        self.case = case
        self.contact_intent_ref = contact_intent_ref
        self.status = status

    def to_mapping(self) -> dict[str, Any]:
        return {
            "error": type(self).__name__,
            "message": str(self),
            "case": self.case,
            "contact_intent_ref": self.contact_intent_ref,
            "contact_intent_status": self.status,
        }


class IntentChangedBeforeCommitError(CanonicalAg1PortError):
    """Order 43: the intent changed between the decision input and the decision commit."""

    def __init__(
        self,
        message: str,
        *,
        stage: str,
        intent_at_input: Mapping[str, Any],
        intent_at_commit: Mapping[str, Any] | None,
        changed_fields: Sequence[str],
        case: str,
    ) -> None:
        super().__init__(message)
        self.stage = stage
        self.intent_at_input = dict(intent_at_input)
        self.intent_at_commit = dict(intent_at_commit) if intent_at_commit is not None else None
        self.changed_fields = tuple(changed_fields)
        self.case = case

    def to_mapping(self) -> dict[str, Any]:
        return {
            "error": type(self).__name__,
            "message": str(self),
            "stage": self.stage,
            "case": self.case,
            "changed_fields": list(self.changed_fields),
            "intent_at_input": dict(self.intent_at_input),
            "intent_at_commit": dict(self.intent_at_commit) if self.intent_at_commit else None,
            "decision_committed": False,
        }


class CanonicalAg1PortRefusalError(CanonicalAg1PortError):
    """The real AG-1 service returned a non-SUCCEEDED outcome; nothing was submitted."""

    def __init__(self, message: str, *, real_status: str, failure_class: str | None,
                 opportunity_ref: str | None, attempt_ref: str | None) -> None:
        super().__init__(message)
        self.real_status = real_status
        self.failure_class = failure_class
        self.opportunity_ref = opportunity_ref
        self.attempt_ref = attempt_ref

    def to_mapping(self) -> dict[str, Any]:
        return {
            "error": type(self).__name__,
            "message": str(self),
            "real_status": self.real_status,
            "failure_class": self.failure_class,
            "opportunity_ref": self.opportunity_ref,
            "attempt_ref": self.attempt_ref,
            "decision_record": None,
        }


class MutableIntentSource:
    """Test seam: the live contact-intent owner view the commit gate re-reads (order 43)."""

    def __init__(self, intent: Any, *, commit_now: str | None = None) -> None:
        self._intent = intent
        self.commit_now = commit_now

    def get(self) -> Any:
        return self._intent

    def supersede(self, intent: Any, *, commit_now: str | None = None) -> None:
        self._intent = intent
        if commit_now is not None:
            self.commit_now = commit_now


# ============================================================================
# 2. real owner loading
# ============================================================================
_REAL_AG1: Any | None = None


def load_real_ag1() -> Any:
    global _REAL_AG1
    if _REAL_AG1 is None:
        root = ag0_adapter.host_path(os.environ.get(RUNTIME_ROOT_ENV, _DEFAULT_RUNTIME_ROOT))
        scripts = root / "scripts"
        if not scripts.is_dir():
            raise CanonicalAg1PortError(f"canonical runtime scripts directory not found: {scripts}")
        if str(scripts) not in sys.path:
            sys.path.insert(0, str(scripts))
        try:
            import agency_decision_ag1 as owner  # noqa: PLC0415
        except ImportError as exc:  # pragma: no cover - Windows host
            raise CanonicalAg1PortError(
                "the canonical AG-1 owner is not importable here "
                f"({type(exc).__name__}: {exc}); it requires POSIX fcntl - run under Linux/WSL"
            ) from exc
        _REAL_AG1 = owner
    return _REAL_AG1


def _to_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


# ============================================================================
# 3. portable result shape
# ============================================================================
@dataclass(frozen=True, slots=True)
class CanonicalAg1DecisionResult:
    """The REAL decision record plus the explicit contact<->canonical mapping and binding."""

    real_decision_record: Any
    real_result_status: str
    real_decision_id: str
    canonical_decision_verb: str
    contact_outcome: str
    proposed_contact_outcome: str
    runtime_outcome_projection: str
    selected_candidate_ref: str | None
    reason_codes: tuple[str, ...]
    decision_route: str
    decision_mapping: Mapping[str, Any]
    binding: Mapping[str, Any]
    intent_gate: Mapping[str, Any]
    capacity_projection: Mapping[str, Any]
    downstream_required: tuple[str, ...]
    submitted: bool
    notes: tuple[str, ...] = ()

    def to_mapping(self) -> dict[str, Any]:
        record = self.real_decision_record
        return {
            "real_result_status": self.real_result_status,
            "real_decision_record_id": self.real_decision_id,
            "real_decision_record": record.to_dict() if record is not None else None,
            "real_decision_verb": self.canonical_decision_verb,
            "real_decision_verb_is_registered_ag1_output": self.canonical_decision_verb
            in CANONICAL_AG1_VERBS,
            "contact_outcome": self.contact_outcome,
            "proposed_contact_outcome": self.proposed_contact_outcome,
            "runtime_outcome_projection": self.runtime_outcome_projection,
            "selected_candidate_ref": self.selected_candidate_ref,
            "reason_codes": list(self.reason_codes),
            "decision_route": self.decision_route,
            "decision_mapping": dict(self.decision_mapping),
            "binding": dict(self.binding),
            "intent_gate": dict(self.intent_gate),
            "capacity_projection": dict(self.capacity_projection),
            "downstream_required": list(self.downstream_required),
            "submitted": self.submitted,
            "decision_writer": EXPECTED_FACT_WRITERS["decision"],
            "notes": list(self.notes),
        }


# ============================================================================
# 4. the port
# ============================================================================
class CanonicalAg1DecisionAdapter:
    """Real-owner AG-1 port: ``spine.CanonicalAg1DecisionPort``."""

    port_id = PORT_ID

    def __init__(
        self,
        *,
        store_root: str | Path,
        ag0_port: ag0_adapter.CanonicalAg0CandidateAdapter,
        caller_module: str = "ag1_test_harness",
        lease_root: str | Path | None = None,
        intent_state: MutableIntentSource | None = None,
        owner: Any | None = None,
    ) -> None:
        self.ag1 = owner if owner is not None else load_real_ag1()
        self.owner_module_file = str(Path(self.ag1.__file__).resolve())
        self.ag0_port = ag0_port
        self.intent_state = intent_state
        self.store_root = assert_isolated_store_root(store_root, namespace=NAMESPACE_ISOLATED_TEST)
        self.namespace = NAMESPACE_ISOLATED_TEST
        self.store = self.ag1.AgencyDecisionStore(self.store_root, namespace=self.namespace)
        lease_path = Path(lease_root) if lease_root is not None else (self.store_root / "life_authority")
        self.lease = self.ag1.ActivityAuthorityLease(assert_isolated_store_root(lease_path))
        self.lease.acquire(timeout_seconds=10.0)
        self.capability = self.ag1.AgencyDecisionAuthority.issue_capability(
            self.lease,
            caller_module=caller_module,
            store_root=self.store_root,
            namespace=self.namespace,
        )
        self.ag1.AgencyDecisionAuthority.verify_capability(self.lease, self.capability)
        self.service = self.ag1.AgencyDecisionService(
            store=self.store,
            lease=self.lease,
            capability=self.capability,
            candidate_read_service=ag0_port.read_service,
            activity_read_service=None,
            resume_eligibility_store=None,
            max_schema_retries=0,
        )
        self.total_outbound_submissions = 0
        self.total_action_proposals = 0
        self._runs: dict[str, dict[str, Any]] = {}
        self.worker_started_with = caller_module
        self.decision_authority_class = type(self.ag1.AgencyDecisionAuthority).__name__
        self.agency_enabled_by_default = bool(self.ag1.is_agency_enabled({}))
        self._assert_declared_mapping_against_real_contract()

    # -- contract checks --------------------------------------------------
    def _assert_declared_mapping_against_real_contract(self) -> None:
        for contact, verb in CONTACT_TO_REAL_AG1_DECISION.items():
            if verb not in self.ag1.VALID_DECISION_OUTPUTS:
                raise CanonicalAg1PortError(
                    f"declared mapping {contact}->{verb} is not a registered real AG-1 decision verb; "
                    "CT0-10 never adds a verb to the AG-1 contract"
                )
            if verb in self.ag1.FORBIDDEN_DECISION_OUTPUTS:
                raise CanonicalAg1PortError(
                    f"declared mapping {contact}->{verb} is a forbidden AG-1 decision output"
                )
            for code, constant_name in zip(
                CONTACT_TO_AG1_REASON_CODES[contact],
                CONTACT_TO_AG1_REASON_CODE_CONSTANTS[contact],
            ):
                if not self.ag1.DecisionReasonCodeRegistry.is_valid(code):
                    raise CanonicalAg1PortError(
                        f"declared reason code {code!r} is not registered in the real AG-1 registry"
                    )
                real_value = getattr(self.ag1, constant_name, None)
                if real_value != code:
                    raise CanonicalAg1PortError(
                        f"declared reason code {code!r} does not match the real AG-1 constant "
                        f"{constant_name}={real_value!r}"
                    )

    # -- helpers ----------------------------------------------------------
    @staticmethod
    def _intent_facts(intent: Any) -> dict[str, Any]:
        return {
            "contact_intent_ref": str(intent.intent_id),
            "revision": int(intent.revision),
            "status": str(intent.status),
            "updated_at": str(intent.updated_at),
            "valid_until": str(intent.valid_until),
            "candidate_ref": str(intent.candidate_ref),
            "candidate_idempotency_key": str(intent.candidate_idempotency_key),
        }

    def _assert_intent_current(
        self, intent: Any, *, stage: str, at: str, intent_at_input: Mapping[str, Any]
    ) -> dict[str, Any]:
        """Order 43: fail closed if the intent changed or expired (real expiry inside AG-1 too)."""
        if intent is None:
            raise IntentChangedBeforeCommitError(
                f"the contact intent is no longer readable at {stage}; refusing to commit a decision",
                stage=stage,
                intent_at_input=intent_at_input,
                intent_at_commit=None,
                changed_fields=("intent_missing",),
                case="INTENT_UNAVAILABLE",
            )
        now_facts = self._intent_facts(intent)
        changed = sorted(
            key for key, value in now_facts.items() if intent_at_input.get(key) != value
        )
        if str(intent.status) not in ag0_adapter.ELIGIBLE_CONTACT_INTENT_STATUSES:
            changed.append("status_not_eligible")
        if _to_utc(at) >= _to_utc(intent.valid_until):
            changed.append("expired_at_decision_time")
        if changed:
            case = "EXPIRED"
            if "status_not_eligible" in changed:
                case = "REVOKED" if str(intent.status) == "CANCELLED" else "SUPERSEDED"
            raise IntentChangedBeforeCommitError(
                "order 43 violation: the contact intent changed between the decision input and the "
                f"decision {stage} ({changed}); refusing to commit a decision",
                stage=stage,
                intent_at_input=intent_at_input,
                intent_at_commit=now_facts,
                changed_fields=changed,
                case=case,
            )
        return now_facts

    def _resolve_candidate_set(self, candidate_set: Any) -> tuple[Any, tuple[Any, ...]]:
        if isinstance(candidate_set, ag0_adapter.CanonicalAg0CandidateResult):
            return candidate_set.candidate_set, candidate_set.candidates
        if isinstance(candidate_set, self.ag1.CandidateSet):
            return candidate_set, self.ag0_port.candidates_for(candidate_set.candidate_set_id)
        raise CanonicalAg1PortError(
            "AG-1 port requires the Phase D CanonicalAg0CandidateResult or a real CandidateSet, got "
            f"{type(candidate_set).__name__}"
        )

    def _capacity_inputs(self, capacity: CanonicalCapacityResult | None) -> tuple[dict[str, Any], dict[str, Any]]:
        snapshot = getattr(capacity, "snapshot", None) if capacity is not None else None
        notes: list[str] = []
        kwargs: dict[str, Any] = {}
        atomic: bool | None = None
        if snapshot is None:
            notes.append(
                "no capacity snapshot available -> AG-1 dimensions stay UNKNOWN; the port never "
                "fabricates AVAILABLE (CapacityEvaluator Section 32)"
            )
        else:
            atomic = bool(getattr(snapshot, "atomic", False))
            if capacity.available:
                kwargs["device_availability_state"] = (
                    self.ag1.CAPACITY_AVAILABLE
                    if bool(getattr(snapshot, "device_available", False))
                    else self.ag1.CAPACITY_UNAVAILABLE
                )
                kwargs["resource_availability_state"] = (
                    self.ag1.CAPACITY_AVAILABLE
                    if bool(getattr(snapshot, "channel_available", False))
                    else self.ag1.CAPACITY_UNAVAILABLE
                )
            else:
                kwargs["device_availability_state"] = self.ag1.CAPACITY_UNAVAILABLE
                kwargs["resource_availability_state"] = self.ag1.CAPACITY_UNAVAILABLE
                notes.append(CAPACITY_UNAVAILABLE_PROJECTION_NOTE)
        if atomic:
            notes.append("contact capacity atomic=True -> AG-1 interruptibility=ATOMIC (order 34)")
        quiet = getattr(capacity, "provenance", None)
        quiet_policy = getattr(quiet, "quiet_policy", QUIET_POLICY_MISSING) if quiet else QUIET_POLICY_MISSING
        projection = {
            "declared_fact_mapping": dict(CAPACITY_FACT_TO_AG1_INPUT),
            "capacity_inputs_passed_to_real_owner": dict(kwargs),
            "contact_capacity_available": bool(getattr(capacity, "available", False)),
            "contact_capacity_unavailable_reasons": list(getattr(capacity, "unavailable_reasons", ())),
            "contact_quiet_policy": quiet_policy,
            "quiet_policy_note": QUIET_POLICY_MISSING,
            "notes": notes,
        }
        return kwargs, projection

    def _contact_proposal(
        self, intent: Any, candidate: Any | None, capacity: CanonicalCapacityResult | None
    ) -> tuple[str, str]:
        if candidate is None:
            return CONTACT_DECISION_NO_ACTION, "NO_AG0_CANDIDATE_FOR_INTENT"
        if capacity is not None and not capacity.available:
            return CONTACT_DECISION_DEFER, "CONTACT_CAPACITY_UNAVAILABLE"
        return CONTACT_DECISION_SEND, "ELIGIBLE_INTENT_WITH_OPEN_CANDIDATE_AND_CAPACITY"

    def _raw_output_for(self, proposal: str, candidate: Any | None) -> dict[str, Any]:
        verb = CONTACT_TO_REAL_AG1_DECISION[proposal]
        selected = candidate.candidate_id if (candidate is not None and verb != "NO_ACTION") else None
        if verb == "DEFER" and candidate is not None:
            deferred = [candidate.candidate_id]
        else:
            deferred = []
        return {
            "decision": verb,
            "selected_candidate_id": selected,
            "reason_codes": list(CONTACT_TO_AG1_REASON_CODES[proposal]),
            "short_rationale": "CT0-10 contact port: canonical AG-1 decision for one eligible "
                               "contact intent (bounded structural statement).",
            "deferred_candidate_ids": deferred,
        }

    def _build_opportunity(self, intent: Any, candidate_set: Any) -> Any:
        return self.ag1.DecisionOpportunity.create(
            trigger_kind=(
                self.ag1.TRIGGER_CANDIDATE_SET_CHANGED
                if CANONICAL_TRIGGER_KIND == self.ag1.TRIGGER_CANDIDATE_SET_CHANGED
                else CANONICAL_TRIGGER_KIND
            ),
            trigger_ref=str(intent.intent_id),
            candidate_set=candidate_set,
            observed_at=str(candidate_set.observed_at),
            occurred_at=str(candidate_set.observed_at),
            causal_parent_refs=(str(intent.intent_id), str(candidate_set.candidate_set_id)),
            valid_until=str(intent.valid_until),
        )

    def _evaluate(
        self,
        *,
        opportunity: Any,
        candidate_set: Any,
        candidates: Sequence[Any],
        raw_output: Mapping[str, Any],
        capacity_kwargs: Mapping[str, Any],
        life_frame: Mapping[str, Any],
        channel: str | None,
        post_cognition_hook: Callable[[], Any] | None,
    ) -> dict[str, Any]:
        self.service.cognition_adapter = self.ag1.FakeCognitionAdapter(
            model_id="ct0-10-ag1-fake-cognition",
            model_version="ct0-10.g1",
            default_decision=dict(raw_output),
        )
        return self.service.evaluate_opportunity(
            opportunity=opportunity,
            candidate_set=candidate_set,
            candidates=list(candidates),
            life_frame=dict(life_frame),
            channel=channel,
            observed_facts={},
            post_cognition_mutation_hook=post_cognition_hook,
            **dict(capacity_kwargs),
        )

    # -- the port method --------------------------------------------------
    def decide(
        self,
        *,
        intent: Any,
        candidate_set: Any,
        capacity: CanonicalCapacityResult,
        observed_at: str,
        intent_state: MutableIntentSource | None = None,
        commit_gate_now: str | None = None,
    ) -> CanonicalAg1DecisionResult:
        """Submit the contact decision to the REAL AG-1 owner and return its real DecisionRecord."""
        intent = self.ag0_port._require_contact_intent(intent)
        observed_at = str(observed_at)
        intent_at_input = self._intent_facts(intent)
        if str(intent.status) not in ag0_adapter.ELIGIBLE_CONTACT_INTENT_STATUSES:
            raise IntentNotEligibleAtDecisionError(
                f"contact intent status {intent.status!r} is not eligible for an AG-1 decision",
                case="REVOKED" if str(intent.status) == "CANCELLED" else str(intent.status),
                contact_intent_ref=str(intent.intent_id),
                status=str(intent.status),
            )
        gate_input = self._assert_intent_current(
            intent, stage="INPUT", at=observed_at, intent_at_input=intent_at_input
        )
        real_candidate_set, candidates = self._resolve_candidate_set(candidate_set)
        event_ref = self.ag0_port.solve_event_for_intent(intent)
        mine = [c for c in candidates if c.user_event_ref == event_ref]
        candidate = mine[0] if len(mine) == 1 else None

        proposal, proposal_reason = self._contact_proposal(intent, candidate, capacity)
        canonical_verb = CONTACT_TO_REAL_AG1_DECISION[proposal]
        capacity_kwargs, capacity_projection = self._capacity_inputs(capacity)
        atomic = bool(getattr(getattr(capacity, "snapshot", None), "atomic", False))
        life_frame = {
            "life_frame_id": f"lframe:{intent.candidate_ref}",
            "interruptibility": (
                self.ag1.INTERRUPT_MODE_ATOMIC if atomic else self.ag1.INTERRUPT_MODE_IMMEDIATE
            ),
            "foreground_activity_ref": None,
            "foreground_status": None,
        }

        live_state = intent_state or self.intent_state
        gate_now = str(commit_gate_now or observed_at)

        def commit_gate_hook() -> None:
            self._assert_intent_current(
                live_state.get() if live_state is not None else intent,
                stage="COMMIT_GATE",
                at=gate_now,
                intent_at_input=intent_at_input,
            )
            return None

        opportunity = self._build_opportunity(intent, real_candidate_set)
        raw_output = self._raw_output_for(proposal, candidate)
        result = self._evaluate(
            opportunity=opportunity,
            candidate_set=real_candidate_set,
            candidates=candidates,
            raw_output=raw_output,
            capacity_kwargs=capacity_kwargs,
            life_frame=life_frame,
            channel=(str(intent.channel_scope[0]) if intent.channel_scope else None),
            post_cognition_hook=commit_gate_hook if live_state is not None else None,
        )

        status = str(result.get("status"))
        record = result.get("decision_record")
        if status != self.ag1.ATTEMPT_STATUS_SUCCEEDED or record is None:
            attempt = result.get("attempt")
            raise CanonicalAg1PortRefusalError(
                f"the real AG-1 service returned {status!r} (no decision committed); "
                f"failure_class={result.get('failure_class') or getattr(attempt, 'failure_class', None)!r}",
                real_status=status,
                failure_class=result.get("failure_class") or getattr(attempt, "failure_class", None),
                opportunity_ref=opportunity.opportunity_id,
                attempt_ref=getattr(attempt, "attempt_id", None),
            )

        gate_commit = self._assert_intent_current(
            live_state.get() if live_state is not None else intent,
            stage="COMMIT_VERIFIED",
            at=gate_now,
            intent_at_input=intent_at_input,
        )

        real_verb = str(record.decision)
        if real_verb not in REAL_AG1_TO_CONTACT_DECISION:
            raise UnmappableDecisionError(
                f"the real AG-1 owner produced {real_verb!r}, which has no declared contact mapping; "
                "the contact pipeline ignores outcomes it does not understand"
            )
        contact_outcome = REAL_AG1_TO_CONTACT_DECISION[real_verb]

        constraint_results = list(result.get("constraint_results") or ())
        capacity_snapshot = result.get("capacity_snapshot")
        accessibility_view = result.get("accessibility_view")
        binding = {
            "contact_intent_ref": intent_at_input["contact_intent_ref"],
            "contact_intent_revision": intent_at_input["revision"],
            "contact_intent_status": intent_at_input["status"],
            "contact_intent_updated_at": intent_at_input["updated_at"],
            "contact_intent_valid_until": intent_at_input["valid_until"],
            "contact_candidate_ref": intent_at_input["candidate_ref"],
            "ag0_candidate_set_ref": real_candidate_set.candidate_set_id,
            "ag0_candidate_set_source_snapshot_refs": list(real_candidate_set.source_snapshot_refs),
            "ag0_candidate_set_builder_policy_version": real_candidate_set.builder_policy_version,
            "ag0_candidate_ref": getattr(candidate, "candidate_id", None),
            "ag0_candidate_revision": getattr(candidate, "revision", None),
            "ag0_candidate_source_revision": getattr(candidate, "source_revision", None),
            "ag0_candidate_valid_until": getattr(candidate, "valid_until", None),
            "capacity_contact_observed_at": getattr(
                getattr(capacity, "snapshot", None), "observed_at", None
            ),
            "capacity_contact_revision": getattr(getattr(capacity, "snapshot", None), "revision", None),
            "capacity_contact_ref": getattr(getattr(capacity, "provenance", None), "activity_ref", None),
            "capacity_contact_policy_ref": getattr(
                getattr(capacity, "provenance", None), "policy_ref", None
            ),
            "capacity_contact_policy_revision": getattr(
                getattr(capacity, "provenance", None), "policy_revision", None
            ),
            "capacity_contact_source_modules": list(
                getattr(getattr(capacity, "provenance", None), "source_modules", ()) or ()
            ),
            "canonical_capacity_snapshot_ref": getattr(capacity_snapshot, "capacity_snapshot_id", None),
            "canonical_capacity_snapshot_source_revisions": list(
                getattr(capacity_snapshot, "source_revisions", ()) or ()
            ),
            "canonical_capacity_overall_state": getattr(capacity_snapshot, "overall_capacity_state", None),
            "canonical_interruptibility": getattr(capacity_snapshot, "interruptibility", None),
            "canonical_hard_constraint_refs": [r.constraint_result_id for r in constraint_results],
            "canonical_hard_constraint_status": {
                r.candidate_ref: r.status for r in constraint_results
            },
            "policy_version": record.policy_version,
            "canonical_policy_version": getattr(opportunity, "policy_version", None),
            "opportunity_ref": record.decision_opportunity_ref,
            "context_snapshot_ref": record.context_snapshot_ref,
            "context_snapshot_fingerprint": record.snapshot_fingerprint,
            "accessibility_ref": getattr(accessibility_view, "accessibility_id", None),
            "attempt_ref": record.attempt_ref,
            "decision_id": record.decision_id,
            "decision_route": record.decision_route,
            "canonical_decision_verb": real_verb,
            "contact_outcome": contact_outcome,
            "decision_writer": EXPECTED_FACT_WRITERS["decision"],
        }
        notes = (
            SPINE_MAPPING_REJECTED_NOTE,
            f"the real trigger vocabulary has no {CONTACT_TRIGGER_KIND_REJECTED_BY_REAL_OWNER!r}; the "
            f"port uses the real {CANONICAL_TRIGGER_KIND!r} trigger (agency_decision_ag1.py:229-240)",
            f"CONTACT_SELECTED != Send: nothing was submitted; still required downstream: "
            f"{list(CONTACT_DOWNSTREAM_REQUIRED)}",
            "the real owner's own freshness recheck (verify_snapshot_freshness, :3380-3462) re-validated "
            "the AG-0 candidate and the opportunity expiry at commit time",
            f"production kill switch AGENCY_ENABLED default off: is_agency_enabled({{}})=="
            f"{self.agency_enabled_by_default}; the port runs in namespace "
            f"{NAMESPACE_ISOLATED_TEST!r} and never in production",
        )
        decision_mapping = {
            "contact_to_real_ag1_decision": dict(CONTACT_TO_REAL_AG1_DECISION),
            "real_ag1_to_contact_decision": dict(REAL_AG1_TO_CONTACT_DECISION),
            "contact_to_runtime_outcome_projection": dict(CONTACT_TO_RUNTIME_OUTCOME),
            "contact_to_ag1_reason_codes": {k: list(v) for k, v in CONTACT_TO_AG1_REASON_CODES.items()},
            "real_ag1_decision_vocabulary": list(CANONICAL_AG1_VERBS),
            "real_ag1_runtime_outcome_projection_is_not_a_verb": True,
            "spine_declared_mapping_rejected": dict(CONTACT_TO_CANONICAL_DECISION),
            "spine_declared_mapping_rejected_note": SPINE_MAPPING_REJECTED_NOTE,
            "proposal_reason": proposal_reason,
            "real_owner_rejected_contact_flag": True,
            "unmappable_real_verb_policy": "raise UnmappableDecisionError (never invent a verb)",
        }

        self._runs[record.decision_id] = {
            "candidate_set": real_candidate_set,
            "candidates": tuple(candidates),
            "constraint_results": tuple(constraint_results),
            "capacity_snapshot": capacity_snapshot,
            "context_snapshot_ref": record.context_snapshot_ref,
            "opportunity_ref": record.decision_opportunity_ref,
            "decision_record": record,
            "binding": binding,
        }
        return CanonicalAg1DecisionResult(
            real_decision_record=record,
            real_result_status=status,
            real_decision_id=record.decision_id,
            canonical_decision_verb=real_verb,
            contact_outcome=contact_outcome,
            proposed_contact_outcome=proposal,
            runtime_outcome_projection=CONTACT_TO_RUNTIME_OUTCOME[contact_outcome],
            selected_candidate_ref=record.selected_candidate_ref,
            reason_codes=tuple(record.reason_codes),
            decision_route=record.decision_route,
            decision_mapping=decision_mapping,
            binding=binding,
            intent_gate={
                "intent_at_input": intent_at_input,
                "intent_at_commit": gate_commit,
                "commit_gate_time": gate_now,
                "input_gate": gate_input,
                "changed": False,
            },
            capacity_projection=capacity_projection,
            downstream_required=CONTACT_DOWNSTREAM_REQUIRED,
            submitted=False,
            notes=notes,
        )

    # -- probes (used by the test; the port itself submits nothing) --------
    def submit_raw_decision_output(
        self,
        *,
        intent: Any,
        candidate_set: Any,
        capacity: CanonicalCapacityResult,
        observed_at: str,
        raw_decision_output: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Feed a caller-supplied AG-1 model output to the REAL pipeline (probe only)."""
        intent = self.ag0_port._require_contact_intent(intent)
        real_candidate_set, candidates = self._resolve_candidate_set(candidate_set)
        capacity_kwargs, _ = self._capacity_inputs(capacity)
        opportunity = self._build_opportunity(intent, real_candidate_set)
        result = self._evaluate(
            opportunity=opportunity,
            candidate_set=real_candidate_set,
            candidates=candidates,
            raw_output=raw_decision_output,
            capacity_kwargs=capacity_kwargs,
            life_frame={
                "life_frame_id": f"lframe:{intent.candidate_ref}",
                "interruptibility": self.ag1.INTERRUPT_MODE_IMMEDIATE,
            },
            channel=None,
            post_cognition_hook=None,
        )
        attempt = result.get("attempt")
        return {
            "requested_verb": raw_decision_output.get("decision"),
            "real_status": result.get("status"),
            "failure_class": result.get("failure_class") or getattr(attempt, "failure_class", None),
            "decision_record": (
                result["decision_record"].to_dict() if result.get("decision_record") else None
            ),
            "opportunity_ref": opportunity.opportunity_id,
            "attempt_ref": getattr(attempt, "attempt_id", None),
        }

    def _validate_raw_or_raise(self, *, decision_id: str, raw_decision_output: Mapping[str, Any]) -> dict[str, Any]:
        """Call the REAL validator and let it raise (the recorded refusal is the evidence)."""
        run = self._runs.get(decision_id)
        if run is None:
            raise CanonicalAg1PortError(f"no recorded evidence for decision {decision_id!r}")
        state = self.store.load_state()
        snapshot_dict = state["context_snapshots_by_id"][run["context_snapshot_ref"]]
        context_snapshot = self.ag1.DecisionContextSnapshot.from_dict(snapshot_dict)
        return self.ag1.DecisionOutputValidator.validate_decision_output(
            dict(raw_decision_output),
            candidate_set=run["candidate_set"],
            candidates_by_id={c.candidate_id: c for c in run["candidates"]},
            constraint_results_by_id={r.candidate_ref: r for r in run["constraint_results"]},
            context_snapshot=context_snapshot,
            capacity_snapshot=run["capacity_snapshot"],
        )

    def _validator_probe(self, *, decision_id: str, raw_decision_output: Mapping[str, Any]) -> dict[str, Any]:
        run = self._runs.get(decision_id)
        if run is None:
            raise CanonicalAg1PortError(f"no recorded evidence for decision {decision_id!r}")
        try:
            validated = self._validate_raw_or_raise(
                decision_id=decision_id, raw_decision_output=raw_decision_output
            )
        except BaseException as exc:  # noqa: BLE001 - capturing the real refusal is the point
            return {
                "raised": type(exc).__name__,
                "module": type(exc).__module__,
                "text": str(exc),
                "failure_class": getattr(exc, "failure_class", None),
            }
        return {"raised": None, "validated": dict(validated), "unexpected": "no error raised"}
    def prove_real_validator_refusal(self, *, decision_id: str,
                                     raw_decision_output: Mapping[str, Any]) -> dict[str, Any]:
        """Public evidence API: the REAL validator's refusal text for a proposed decision verb."""
        return self._validator_probe(decision_id=decision_id, raw_decision_output=raw_decision_output)

    def prove_real_ag1_prohibitions(
        self, *, decision_id: str
    ) -> dict[str, Any]:
        """Exercise the REAL AG-1 prohibitions and record the exact error texts."""
        ag1 = self.ag1
        run = self._runs[decision_id]
        probe_out: dict[str, Any] = {}

        def record(name: str, call: Callable[[], Any], *, note: str) -> None:
            try:
                call()
            except BaseException as exc:  # noqa: BLE001
                probe_out[name] = {
                    "raised": type(exc).__name__,
                    "text": str(exc),
                    "note": note,
                    "failure_class": getattr(exc, "failure_class", None),
                }
            else:
                probe_out[name] = {"raised": None, "text": None, "note": note,
                                   "unexpected": "no error raised"}

        record(
            "DecisionOpportunity.create(PERIODIC_LLM_TICK)",
            lambda: ag1.DecisionOpportunity.create(
                trigger_kind="PERIODIC_LLM_TICK",
                trigger_ref=run["opportunity_ref"],
                candidate_set=run["candidate_set"],
            ),
            note="Section 17: periodic tick triggers are permanently forbidden",
        )
        opportunity = ag1.DecisionOpportunity.create(
            trigger_kind=ag1.TRIGGER_EXPLICIT_REEVALUATION,
            trigger_ref="cintent:providerb",
            candidate_set=run["candidate_set"],
        )
        record(
            "DecisionContextSnapshot.build(unobserved_world_refs=...)",
            lambda: ag1.DecisionContextSnapshot.build(
                opportunity=opportunity,
                candidate_set=run["candidate_set"],
                candidates={c.candidate_id: c for c in run["candidates"]},
                unobserved_world_refs=("unobserved:world_db_facts",),
            ),
            note="Section 21: unobserved World facts may not enter a decision context",
        )
        record(
            "DecisionOutputValidator(hidden chain_of_thought)",
            lambda: self._validate_raw_or_raise(
                decision_id=decision_id,
                raw_decision_output={
                    "decision": "NO_ACTION",
                    "selected_candidate_id": None,
                    "reason_codes": ["NO_ACTION_CHOSEN"],
                    "short_rationale": "bounded",
                    "deferred_candidate_ids": [],
                    "chain_of_thought": "hidden reasoning dump",
                },
            ),
            note="Section 10/75: hidden chain-of-thought must never be persisted",
        )
        record(
            "DecisionOutputValidator(universal utility score)",
            lambda: self._validate_raw_or_raise(
                decision_id=decision_id,
                raw_decision_output={
                    "decision": "NO_ACTION",
                    "selected_candidate_id": None,
                    "reason_codes": ["NO_ACTION_CHOSEN"],
                    "short_rationale": "bounded",
                    "deferred_candidate_ids": [],
                    "utility": 0.99,
                },
            ),
            note="Section 22/41-42: universal utility scoring / argmax is forbidden",
        )
        record(
            "DecisionOutputValidator(unregistered reason code)",
            lambda: ag1.DecisionReasonCodeRegistry.validate_codes(["CT0_10_CONTACT_REASON"]),
            note="Section 73: only registry reason codes may be persisted",
        )
        record(
            "DecisionRecord.mutate()",
            run["decision_record"].mutate,
            note="Section 79: a committed DecisionRecord is immutable",
        )
        record(
            "AgencyDecisionStore.commit_delta(overwrite committed decision)",
            lambda: self.store.commit_delta(
                lease=self.lease,
                capability=self.capability,
                map_updates={
                    "decisions_by_id": {
                        decision_id: {
                            **run["decision_record"].to_dict(),
                            "decision": "NO_ACTION",
                        }
                    }
                },
                journal_payload={"event_type": "CT0_10_IMMUTABILITY_PROBE"},
            ),
            note="Section 79: committed decisions cannot be modified by any writer",
        )
        record(
            "DecisionRecord.create_action_proposal()",
            run["decision_record"].create_action_proposal,
            note="Section 84/157: an AG-1 decision cannot create an ActionProposal",
        )
        record(
            "AgencyDecisionService.send_outbound_message()",
            self.service.send_outbound_message,
            note="Section 156: AG-1 cannot send outbound messages",
        )
        record(
            "AgencyDecisionAuthority.acquire_action_writer()",
            ag1.AgencyDecisionAuthority.acquire_action_writer,
            note="Section 84: AG-1 cannot hold an Action Reality writer capability",
        )
        record(
            "AgencyDecisionAuthority.acquire_goal_writer()",
            ag1.AgencyDecisionAuthority.acquire_goal_writer,
            note="Section 87: AG-1 cannot hold a Goal writer capability",
        )
        return probe_out

    def close(self) -> None:
        try:
            self.store.flush_snapshot()
        finally:
            self.lease.release()


__all__ = [
    "AG1_OWNER_FILE", "AG1_OWNER_MODULE", "AG1_RUNTIME_PRODUCER_FILE", "CANONICAL_TRIGGER_KIND",
    "CAPACITY_FACT_TO_AG1_INPUT", "CAPACITY_UNAVAILABLE_PROJECTION_NOTE",
    "CONTACT_DOWNSTREAM_REQUIRED", "CONTACT_TO_AG1_REASON_CODES", "CONTACT_TO_REAL_AG1_DECISION",
    "CONTACT_TO_RUNTIME_OUTCOME", "CONTACT_TRIGGER_KIND_REJECTED_BY_REAL_OWNER",
    "CanonicalAg1DecisionAdapter", "CanonicalAg1DecisionResult", "CanonicalAg1PortError",
    "CanonicalAg1PortRefusalError", "IntentChangedBeforeCommitError", "IntentNotEligibleAtDecisionError",
    "MutableIntentSource", "PORT_ID", "REAL_AG1_TO_CONTACT_DECISION", "RUNTIME_ROOT_ENV",
    "SPINE_MAPPING_REJECTED_NOTE", "load_real_ag1",
]
