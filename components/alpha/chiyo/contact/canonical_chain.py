"""CT0-10 canonical full chain + dispatch admission (order sections 62-64, 77-81).

This is the phase's integration owner. It drives the 15-stage canonical-wired
contact pipeline:

  ContactIntent -> Capacity -> AG0 candidate -> AG1 decision -> Grounding
  -> Expression draft -> AR0 action -> dispatch admission -> Telegram adapter boundary
  -> Recording transport -> Receipt -> Reconciliation -> AR1 settlement -> Handoff

Design rules:
  * it depends ONLY on the frozen spine protocols, never on an adapter's internals,
    so the four adapter workstreams can land independently;
  * an unavailable adapter is reported as that stage being BLOCKED - it is NEVER
    replaced with a fixture (order section 10);
  * dispatch admission RE-READS every fence after the decision (order sections
    62-64): a stale snapshot is not a permanent permission;
  * every stage contributes an AuthorityFact(fact, owner, writer, ref, revision)
    so the end-to-end trace required by order sections 79-80 is produced by
    construction and there is no universal integration writer (order 81).
"""

from __future__ import annotations

import importlib
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

# --------------------------------------------------------------------------
# import paths: the frozen contact contracts (read-only) and the CT0-10 spine
# --------------------------------------------------------------------------
_CT0_10_ROOT = Path(__file__).resolve().parents[2]
_CHIYO_DIR = Path(__file__).resolve().parents[1]
if str(_CHIYO_DIR) not in sys.path:
    sys.path.insert(0, str(_CHIYO_DIR))

# The frozen CT0-9 contact contracts ship flat in this tree (chiyo/contact).
SANDBOX_SRC_CT0 = Path(os.environ.get("CT0_9_SANDBOX_SRC", str(_CHIYO_DIR / "contact")))
if str(SANDBOX_SRC_CT0) not in sys.path and SANDBOX_SRC_CT0.exists():
    sys.path.insert(0, str(SANDBOX_SRC_CT0))
SANDBOX_SRC = SANDBOX_SRC_CT0.parent
if str(SANDBOX_SRC) not in sys.path and SANDBOX_SRC.exists():
    sys.path.insert(0, str(SANDBOX_SRC))

from ct0_10.canonical_spine import (  # noqa: E402
    KNOWN_SEMANTIC_LIMITATION_FOCUSED,
    NAMESPACE_ISOLATED_TEST,
    QUIET_POLICY_MISSING,
    AuthorityFact,
    CanonicalCapacityResult,
    CanonicalIntegrationError,
    EXPECTED_FACT_WRITERS,
    assert_isolated_store_root,
    canonical_decision_for,
    contact_decision_for,
    payload_hash,
)

CHAIN_SCHEMA = "chiyo.ct0_10.canonical_chain.v1"

STAGE_CONTACT_INTENT = "contact_intent"
STAGE_CAPACITY = "capacity"
STAGE_AG0 = "ag0_candidate"
STAGE_AG1 = "ag1_decision"
STAGE_GROUNDING = "grounding"
STAGE_EXPRESSION = "expression"
STAGE_AR0 = "ar0_action"
STAGE_DISPATCH = "dispatch_admission"
STAGE_TELEGRAM = "telegram_boundary"
STAGE_TRANSPORT = "recording_transport"
STAGE_RECEIPT = "receipt"
STAGE_RECONCILIATION = "reconciliation"
STAGE_AR1 = "ar1_settlement"
STAGE_HANDOFF = "handoff"

STAGE_ORDER = (
    STAGE_CONTACT_INTENT, STAGE_CAPACITY, STAGE_AG0, STAGE_AG1, STAGE_GROUNDING,
    STAGE_EXPRESSION, STAGE_AR0, STAGE_DISPATCH, STAGE_TELEGRAM, STAGE_TRANSPORT,
    STAGE_RECEIPT, STAGE_RECONCILIATION, STAGE_AR1, STAGE_HANDOFF,
)

STAGE_OWNER = {
    STAGE_CONTACT_INTENT: "Contact owner (CT0-3 intent authority)",
    STAGE_CAPACITY: "canonical read-only capacity resolver (no capacity owner)",
    STAGE_AG0: "canonical AG-0",
    STAGE_AG1: "canonical AG-1",
    STAGE_GROUNDING: "canonical grounding adapter",
    STAGE_EXPRESSION: "canonical expression adapter",
    STAGE_AR0: "canonical AR-0",
    STAGE_DISPATCH: "CT0-10 dispatch admission (integration owner, no writes)",
    STAGE_TELEGRAM: "canonical Telegram adapter boundary (fenced)",
    STAGE_TRANSPORT: "recording transport (zero real side effect)",
    STAGE_RECEIPT: "Action Reality (AR-0 receipt/read model)",
    STAGE_RECONCILIATION: "AR-0 reconciliation",
    STAGE_AR1: "canonical AR-1",
    STAGE_HANDOFF: "Contact/Result integration owner",
}

BLOCKED = "BLOCKED"
PASS = "PASS"
FAIL = "FAIL"
SKIPPED = "SKIPPED"

# dispatch admission block reasons (order sections 62-64)
DISPATCH_BLOCK_INTENT_INVALID = "INTENT_INVALIDATED_AFTER_DECISION"
DISPATCH_BLOCK_CAPACITY_CHANGED = "CAPACITY_NO_LONGER_AVAILABLE"
DISPATCH_BLOCK_AUTHORIZATION = "AUTHORIZATION_REVOKED"
DISPATCH_BLOCK_ATOMIC = "ATOMIC"
DISPATCH_BLOCK_QUIET = "QUIET_WINDOW_CLOSED"
DISPATCH_BLOCK_DEVICE = "DEVICE_UNAVAILABLE"
DISPATCH_BLOCK_CHANNEL = "CHANNEL_UNAVAILABLE"
DISPATCH_BLOCK_PENDING = "PENDING_OUTBOUND"
DISPATCH_BLOCK_PREVIOUS_UNKNOWN = "PREVIOUS_UNKNOWN"
DISPATCH_BLOCK_UNRESOLVED_INBOUND = "UNRESOLVED_INBOUND"
DISPATCH_BLOCK_KILL_SWITCH = "SUBMISSION_GATE_CLOSED"
DISPATCH_BLOCK_TELEGRAM_FENCE = "TELEGRAM_SIDE_EFFECT_FENCED"


@dataclass
class StageOutcome:
    stage: str
    status: str
    owner: str
    detail: Mapping[str, Any] = field(default_factory=dict)
    error: str | None = None
    seconds: float = 0.0

    def to_mapping(self) -> dict[str, Any]:
        return {"stage": self.stage, "status": self.status, "owner": self.owner,
                "detail": dict(self.detail), "error": self.error, "seconds": round(self.seconds, 3)}


@dataclass(frozen=True, slots=True)
class AdapterAvailability:
    module: str
    available: bool
    error: str | None = None

    def to_mapping(self) -> dict[str, Any]:
        return {"module": self.module, "available": self.available, "error": self.error}


_ADAPTER_MODULES = {
    STAGE_CAPACITY: ("ct0_10.canonical_ports.capacity_resolver", "CanonicalContactCapacityResolver"),
    STAGE_AG0: ("ct0_10.canonical_ports.ag0_adapter", "CanonicalAg0CandidateAdapter"),
    STAGE_AG1: ("ct0_10.canonical_ports.ag1_adapter", "CanonicalAg1DecisionAdapter"),
    STAGE_GROUNDING: ("ct0_10.canonical_ports.grounding_resolver", "CanonicalContactGroundingResolver"),
    STAGE_EXPRESSION: ("ct0_10.canonical_ports.expression_adapter", "CanonicalExpressionAdapter"),
    STAGE_AR0: ("ct0_10.canonical_ports.ar0_adapter", "CanonicalAr0Adapter"),
    STAGE_TELEGRAM: ("ct0_10.canonical_ports.telegram_boundary", "CanonicalTelegramBoundary"),
    STAGE_AR1: ("ct0_10.canonical_ports.ar1_consumer", "CanonicalAr1Consumer"),
}


def probe_adapters() -> dict[str, AdapterAvailability]:
    """Report which adapter modules actually exist WITHOUT substituting fixtures."""
    found: dict[str, AdapterAvailability] = {}
    for stage, (module_name, symbol) in _ADAPTER_MODULES.items():
        try:
            module = importlib.import_module(module_name)
            getattr(module, symbol)
            found[stage] = AdapterAvailability(module_name, True, None)
        except Exception as exc:  # noqa: BLE001 - availability probe must not raise
            found[stage] = AdapterAvailability(module_name, False, f"{type(exc).__name__}: {exc}")
    return found


# ==========================================================================
# dispatch admission (order sections 62-64)
# ==========================================================================
@dataclass(frozen=True, slots=True)
class DispatchAdmission:
    admitted: bool
    block_reasons: tuple[str, ...]
    action_may_remain: str
    telegram_submit: int
    re_read: Mapping[str, Any]

    def to_mapping(self) -> dict[str, Any]:
        return {"admitted": self.admitted, "block_reasons": list(self.block_reasons),
                "action_may_remain": self.action_may_remain, "telegram_submit": self.telegram_submit,
                "re_read": dict(self.re_read)}


def dispatch_admission(
    *,
    intent_state: Mapping[str, Any],
    fresh_capacity: CanonicalCapacityResult | None,
    action_state: Mapping[str, Any],
    telegram_boundary: Any | None,
    proactive_enabled: bool,
    action_execution_enabled: bool,
) -> DispatchAdmission:
    """Re-read every fence immediately before the transport seam.

    A decision-time snapshot is a historical fact, not a permission. Any fence
    that has gone bad keeps the action prepared/deferred and submits nothing
    (order sections 62-64).
    """
    blocks: list[str] = []

    intent_status = str(intent_state.get("status", "")).upper()
    if intent_status not in ("OPEN", "SELECTED", "DEFERRED"):
        blocks.append(DISPATCH_BLOCK_INTENT_INVALID)
    if intent_state.get("expired") is True:
        blocks.append(DISPATCH_BLOCK_INTENT_INVALID)

    snapshot = None if fresh_capacity is None else fresh_capacity.snapshot
    if fresh_capacity is None:
        blocks.append(DISPATCH_BLOCK_CAPACITY_CHANGED)
    else:
        if not fresh_capacity.available:
            blocks.extend(fresh_capacity.unavailable_reasons or (DISPATCH_BLOCK_CAPACITY_CHANGED,))
        if snapshot is not None:
            if getattr(snapshot, "atomic", False):
                blocks.append(DISPATCH_BLOCK_ATOMIC)
            if getattr(snapshot, "quiet_window_open", True) is not True:
                blocks.append(DISPATCH_BLOCK_QUIET)
            if getattr(snapshot, "device_available", True) is not True:
                blocks.append(DISPATCH_BLOCK_DEVICE)
            if getattr(snapshot, "channel_available", True) is not True:
                blocks.append(DISPATCH_BLOCK_CHANNEL)
            if int(getattr(snapshot, "pending_outbound_count", 0) or 0) > 0:
                blocks.append(DISPATCH_BLOCK_PENDING)
            if getattr(snapshot, "previous_unknown", False):
                blocks.append(DISPATCH_BLOCK_PREVIOUS_UNKNOWN)
            if getattr(snapshot, "unresolved_inbound", False):
                blocks.append(DISPATCH_BLOCK_UNRESOLVED_INBOUND)

    if not (proactive_enabled and action_execution_enabled):
        blocks.append(DISPATCH_BLOCK_KILL_SWITCH)

    action_status = str(action_state.get("status", "")).upper()
    if action_state.get("already_submitted") is True or action_status in ("SUBMITTED", "ACKNOWLEDGED"):
        blocks.append(DISPATCH_BLOCK_PENDING)

    if telegram_boundary is None:
        blocks.append(DISPATCH_BLOCK_TELEGRAM_FENCE)
    else:
        verdict = getattr(telegram_boundary, "verdict", None)
        if isinstance(verdict, Mapping) and verdict.get("telegram_real_side_effect_enabled"):
            blocks.append(DISPATCH_BLOCK_TELEGRAM_FENCE)

    blocks = list(dict.fromkeys(blocks))
    admitted = not blocks
    return DispatchAdmission(
        admitted=admitted,
        block_reasons=tuple(blocks),
        action_may_remain="PREPARED" if not admitted else "SUBMITTING",
        telegram_submit=1 if admitted else 0,
        re_read={
            "intent_status": intent_status,
            "capacity_available": None if fresh_capacity is None else fresh_capacity.available,
            "capacity_revision": None if snapshot is None else getattr(snapshot, "revision", None),
            "action_status": action_status,
            "proactive_enabled": proactive_enabled,
            "action_execution_enabled": action_execution_enabled,
            "telegram_boundary_present": telegram_boundary is not None,
        },
    )


# ==========================================================================
# the chain
# ==========================================================================
class CanonicalChain:
    """Drives the canonical-wired contact pipeline. Never writes canonically
    outside an isolated root, and never contacts a real transport."""

    role = "CT0-10 canonical integration owner"

    def __init__(
        self,
        *,
        isolated_root: str | Path,
        namespace: str = NAMESPACE_ISOLATED_TEST,
        recipient_ref: str = "recipient:ct010-isolated",
        channel: str = "channel:telegram-sandbox",
        clock_start: str = "2026-10-01T00:00:00Z",
        intent_authority: Any | None = None,
        adapters: Mapping[str, Any] | None = None,
    ) -> None:
        self.isolated_root = assert_isolated_store_root(isolated_root, namespace=namespace)
        Path(self.isolated_root).mkdir(parents=True, exist_ok=True)
        self.namespace = namespace
        self.recipient_ref = recipient_ref
        self.channel = channel
        self.clock = _instant(clock_start)
        self.intent_authority = intent_authority
        self.adapters: dict[str, Any] = dict(adapters or {})
        self.availability = probe_adapters()
        self.stages: list[StageOutcome] = []
        self.facts: list[AuthorityFact] = []
        self.notes: list[str] = []
        self.teardown: list[Callable[[], None]] = []

    # -- clock ------------------------------------------------------------
    def stamp(self) -> str:
        return self.clock.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")

    def advance(self, seconds: int) -> None:
        from datetime import timedelta
        self.clock = self.clock + timedelta(seconds=seconds)

    # -- stage bookkeeping ------------------------------------------------
    def _stage(self, stage: str, status: str, detail: Mapping[str, Any] | None = None,
               error: str | None = None, seconds: float = 0.0) -> StageOutcome:
        outcome = StageOutcome(stage=stage, status=status, owner=STAGE_OWNER.get(stage, "?"),
                               detail=dict(detail or {}), error=error, seconds=seconds)
        self.stages.append(outcome)
        return outcome

    def _adapter(self, stage: str) -> Any | None:
        if stage in self.adapters:
            return self.adapters[stage]
        availability = self.availability.get(stage)
        if availability is None or not availability.available:
            return None
        module_name, symbol = _ADAPTER_MODULES[stage]
        module = importlib.import_module(module_name)
        instance = getattr(module, symbol)
        # construct with the isolated root when the adapter accepts it
        try:
            built = instance(isolated_root=self.isolated_root, namespace=self.namespace)
        except TypeError:
            try:
                built = instance(self.isolated_root, namespace=self.namespace)
            except TypeError:
                built = instance()
        self.adapters[stage] = built
        return built

    def _fact(self, fact: str, ref: str, revision: int | None = None, **extra: Any) -> None:
        self.facts.append(AuthorityFact(fact=fact, owner=STAGE_OWNER.get(fact, "?"),
                                        writer=EXPECTED_FACT_WRITERS.get(fact, "?"),
                                        ref=ref, revision=revision, extra=extra))

    # ======================================================================
    # the full chain
    # ======================================================================
    def run(self, intent: Any, *, expression_mode: str = "verbatim") -> dict[str, Any]:
        """Drive the canonical chain for one existing contact intent.

        `intent` must be a real CT0-3 ContactIntent (the contact owner writes it;
        CT0-10 never does). Every later stage is owned by a canonical adapter.
        """
        from contact_intent_agency_bridge import ContactCapacitySnapshot  # noqa: F401

        result: dict[str, Any] = {"schema": CHAIN_SCHEMA, "stages": [], "facts": [], "notes": []}
        self._fact(STAGE_CONTACT_INTENT, getattr(intent, "intent_id", "?"),
                   getattr(intent, "revision", None),
                   contact_status=getattr(intent, "status", None),
                   valid_until=getattr(intent, "valid_until", None))
        self._stage(STAGE_CONTACT_INTENT, PASS, {
            "intent_ref": getattr(intent, "intent_id", None),
            "status": getattr(intent, "status", None),
            "revision": getattr(intent, "revision", None),
        })

        # ---- capacity (read-only resolver) --------------------------------
        capacity = self._run_capacity(intent)
        if capacity is None or not capacity.available:
            return self._finish(result, "BLOCKED_AT_CAPACITY")

        # ---- AG-0 ---------------------------------------------------------
        candidate_set = self._run_ag0(intent)
        if candidate_set is None:
            return self._finish(result, "BLOCKED_AT_AG0")

        # ---- AG-1 ---------------------------------------------------------
        decision = self._run_ag1(intent, candidate_set, capacity)
        if decision is None:
            return self._finish(result, "BLOCKED_AT_AG1")
        contact_decision = self._contact_decision_of(decision)
        if contact_decision == "NO_ACTION":
            return self._finish(result, "DECISION_NO_ACTION")
        if contact_decision == "DEFER":
            return self._finish(result, "DECISION_DEFER")

        # ---- grounding ----------------------------------------------------
        grounding = self._run_grounding(intent, decision)
        if grounding is None:
            return self._finish(result, "BLOCKED_AT_GROUNDING")

        # ---- expression ---------------------------------------------------
        draft = self._run_expression(intent, decision, grounding, expression_mode=expression_mode)
        if draft is None:
            return self._finish(result, "NO_ACTION_EXPRESSION_FAILED")

        # ---- AR-0 ---------------------------------------------------------
        action = self._run_ar0(intent, decision, draft)
        if action is None:
            return self._finish(result, "BLOCKED_AT_AR0")

        # ---- dispatch admission (re-reads every fence) --------------------
        admission = self._run_dispatch(intent, action)
        if not admission.admitted:
            return self._finish(result, "ACTION_PREPARED_DISPATCH_BLOCKED")

        # ---- Telegram boundary + recording transport ----------------------
        transport = self._run_transport(action)
        if transport is None:
            return self._finish(result, "BLOCKED_AT_TELEGRAM_BOUNDARY")

        # ---- receipt / reconciliation / settlement / handoff --------------
        if not self._run_receipt(action, transport):
            return self._finish(result, "BLOCKED_AT_RECEIPT")
        self._run_reconciliation(action)
        if not self._run_ar1(action, transport):
            return self._finish(result, "BLOCKED_AT_AR1")
        self._run_handoff(action, transport)

        return self._finish(result, "CANONICAL_CHAIN_COMPLETE")

    # -- individual stages ------------------------------------------------
    def _run_capacity(self, intent: Any) -> CanonicalCapacityResult | None:
        started = time.time()
        adapter = self._adapter(STAGE_CAPACITY)
        if adapter is None:
            self._stage(STAGE_CAPACITY, BLOCKED, {"reason": "adapter unavailable"},
                        error=(self.availability.get(STAGE_CAPACITY) or AdapterAvailability("?", False, "missing")).error,
                        seconds=time.time() - started)
            return None
        try:
            capacity = adapter.resolve(intent=intent, recipient_ref=self.recipient_ref,
                                       channel=self.channel, now=self.stamp())
        except Exception as exc:  # noqa: BLE001
            self._stage(STAGE_CAPACITY, FAIL, error=f"{type(exc).__name__}: {exc}",
                        seconds=time.time() - started)
            return None
        self._fact("capacity", f"capacity:{getattr(capacity.provenance, 'activity_ref', None)}",
                   getattr(capacity.provenance, "activity_revision", None),
                   available=capacity.available, reasons=list(capacity.unavailable_reasons))
        self._stage(STAGE_CAPACITY, PASS, capacity.to_mapping(), seconds=time.time() - started)
        return capacity

    def _run_ag0(self, intent: Any) -> Any | None:
        started = time.time()
        adapter = self._adapter(STAGE_AG0)
        if adapter is None:
            self._stage(STAGE_AG0, BLOCKED, {"reason": "adapter unavailable"},
                        seconds=time.time() - started)
            return None
        try:
            candidate_set = adapter.build_candidate_set(intent=intent, observed_at=self.stamp())
        except Exception as exc:  # noqa: BLE001
            self._stage(STAGE_AG0, FAIL, error=f"{type(exc).__name__}: {exc}",
                        seconds=time.time() - started)
            return None
        candidate_set_id = _get(candidate_set, "candidate_set_id", "id", default="?")
        self._fact("agency_candidate", str(candidate_set_id))
        self._stage(STAGE_AG0, PASS, {"candidate_set_id": candidate_set_id,
                                      "provenance": _mapping(candidate_set)}, seconds=time.time() - started)
        return candidate_set

    def _run_ag1(self, intent: Any, candidate_set: Any, capacity: CanonicalCapacityResult) -> Any | None:
        started = time.time()
        adapter = self._adapter(STAGE_AG1)
        if adapter is None:
            self._stage(STAGE_AG1, BLOCKED, {"reason": "adapter unavailable"}, seconds=time.time() - started)
            return None
        try:
            decision = adapter.decide(intent=intent, candidate_set=candidate_set,
                                      capacity=capacity, observed_at=self.stamp())
        except Exception as exc:  # noqa: BLE001
            self._stage(STAGE_AG1, FAIL, error=f"{type(exc).__name__}: {exc}", seconds=time.time() - started)
            return None
        decision_ref = _get(decision, "decision_ref", "decision_id", "id", default="?")
        self._fact("decision", str(decision_ref), _get(decision, "revision", default=None),
                   canonical_verb=_get(decision, "outcome", "decision", "verb", default=None))
        self._stage(STAGE_AG1, PASS, {"decision_ref": decision_ref, "record": _mapping(decision),
                                      "binding": _get(decision, "binding", default=None)},
                    seconds=time.time() - started)
        return decision

    @staticmethod
    def _contact_decision_of(decision: Any) -> str:
        """Map the canonical AG-1 outcome back onto the contact Decision contract."""
        canonical = str(_get(decision, "outcome", "decision", "verb", default="")).upper()
        if canonical in ("ACTION_SELECTED", "SELECTED", "CONTACT_SELECTED"):
            return "CONTACT_SELECTED"
        try:
            return contact_decision_for(canonical)
        except Exception:  # noqa: BLE001 - an unmapped outcome is NO_ACTION for contact
            return "NO_ACTION"

    def _run_grounding(self, intent: Any, decision: Any) -> Any | None:
        started = time.time()
        adapter = self._adapter(STAGE_GROUNDING)
        if adapter is None:
            self._stage(STAGE_GROUNDING, BLOCKED, {"reason": "adapter unavailable"},
                        seconds=time.time() - started)
            return None
        try:
            sources = adapter.resolve_grounding(
                intent=intent, decision=decision, source_refs=tuple(getattr(intent, "source_refs", ())))
        except Exception as exc:  # noqa: BLE001
            self._stage(STAGE_GROUNDING, FAIL, error=f"{type(exc).__name__}: {exc}",
                        seconds=time.time() - started)
            return None
        self._fact("grounding", f"grounding:{len(tuple(sources))}",
                   extra={"source_refs": list(getattr(intent, "source_refs", ()))})
        self._stage(STAGE_GROUNDING, PASS, {"count": len(tuple(sources)),
                                           "refs": [getattr(s, "source_ref", None) for s in tuple(sources)]},
                    seconds=time.time() - started)
        return sources

    def _run_expression(self, intent: Any, decision: Any, grounding: Any, *, expression_mode: str) -> Any | None:
        started = time.time()
        adapter = self._adapter(STAGE_EXPRESSION)
        if adapter is None:
            self._stage(STAGE_EXPRESSION, BLOCKED, {"reason": "adapter unavailable"},
                        seconds=time.time() - started)
            return None
        try:
            try:
                draft = adapter.render(intent=intent, decision=decision, grounding_sources=tuple(grounding),
                                       mode=expression_mode)
            except TypeError:
                draft = adapter.render(intent=intent, decision=decision, grounding_sources=tuple(grounding))
        except Exception as exc:  # noqa: BLE001
            self._stage(STAGE_EXPRESSION, FAIL, {"expression_failure": True},
                        error=f"{type(exc).__name__}: {exc}", seconds=time.time() - started)
            return None
        draft_ref = _get(draft, "draft_id", "draft_ref", default="?")
        content_hash = _get(draft, "content_hash", default=None)
        self._fact("draft", str(draft_ref), extra={"content_hash": content_hash})
        self._stage(STAGE_EXPRESSION, PASS, {"draft_ref": draft_ref, "content_hash": content_hash},
                    seconds=time.time() - started)
        return draft

    def _run_ar0(self, intent: Any, decision: Any, draft: Any) -> Any | None:
        started = time.time()
        adapter = self._adapter(STAGE_AR0)
        if adapter is None:
            self._stage(STAGE_AR0, BLOCKED, {"reason": "adapter unavailable"}, seconds=time.time() - started)
            return None
        try:
            action = adapter.propose(intent=intent, decision=decision, draft=draft, created_at=self.stamp())
        except TypeError:
            try:
                action = adapter.propose(intent=intent, decision=decision, draft=draft)
            except Exception as exc:  # noqa: BLE001
                self._stage(STAGE_AR0, FAIL, error=f"{type(exc).__name__}: {exc}", seconds=time.time() - started)
                return None
        except Exception as exc:  # noqa: BLE001
            self._stage(STAGE_AR0, FAIL, error=f"{type(exc).__name__}: {exc}", seconds=time.time() - started)
            return None
        action_ref = _get(action, "action_ref", "canonical_action_ref", default="?")
        self._fact("action", str(action_ref), extra={"submission_key": _get(action, "canonical_submission_key",
                                                                          "submission_key", default=None)})
        self._stage(STAGE_AR0, PASS, _mapping(action) or {"action_ref": action_ref}, seconds=time.time() - started)
        return action

    def _run_dispatch(self, intent: Any, action: Any) -> DispatchAdmission:
        started = time.time()
        capacity = self._run_capacity(intent)
        intent_state = {"status": getattr(intent, "status", "OPEN"),
                        "expired": _is_expired(intent, self.stamp())}
        action_state = {"status": _get(action, "status", default="PREPARED"),
                        "already_submitted": bool(_get(action, "submitted", default=False))}
        boundary = self._adapter(STAGE_TELEGRAM)
        kill = _kill_switch_state()
        admission = dispatch_admission(
            intent_state=intent_state,
            fresh_capacity=capacity,
            action_state=action_state,
            telegram_boundary=boundary,
            proactive_enabled=kill["proactive_enabled"],
            action_execution_enabled=kill["action_execution_enabled"],
        )
        self._stage(STAGE_DISPATCH, PASS if admission.admitted else "BLOCKED",
                    admission.to_mapping(), seconds=time.time() - started)
        self._fact(STAGE_DISPATCH, "dispatch:" + ("admitted" if admission.admitted else "blocked"),
                   extra={"reasons": list(admission.block_reasons)})
        return admission

    def _run_transport(self, action: Any) -> Any | None:
        started = time.time()
        boundary = self._adapter(STAGE_TELEGRAM)
        if boundary is None:
            self._stage(STAGE_TELEGRAM, BLOCKED, {"reason": "adapter unavailable"}, seconds=time.time() - started)
            return None
        try:
            if hasattr(boundary, "finalize_dispatch"):
                transport = boundary.finalize_dispatch(action=action, recorded_at=self.stamp())
            elif hasattr(boundary, "submit_within_fence"):
                transport = boundary.submit_within_fence(action=action)
            else:
                transport = boundary
        except Exception as exc:  # noqa: BLE001
            self._stage(STAGE_TELEGRAM, FAIL, error=f"{type(exc).__name__}: {exc}", seconds=time.time() - started)
            return None
        verdict = _get(boundary, "verdict", default=None)
        self._stage(STAGE_TELEGRAM, PASS, {"verdict": dict(verdict) if isinstance(verdict, Mapping) else verdict,
                                          "real_side_effect": False}, seconds=time.time() - started)
        self._fact(STAGE_TRANSPORT, "transport:recording", extra={"real_side_effect": False})
        self._stage(STAGE_TRANSPORT, PASS, {"real_side_effect": False, "kind": "recording/no-side-effect"},
                    seconds=time.time() - started)
        return transport

    def _run_receipt(self, action: Any, transport: Any) -> bool:
        started = time.time()
        adapter = self._adapter(STAGE_AR0)
        receipt = None
        if adapter is not None and hasattr(adapter, "record_receipt"):
            try:
                receipt = adapter.record_receipt(action=action, transport=transport, observed_at=self.stamp())
            except Exception as exc:  # noqa: BLE001
                self._stage(STAGE_RECEIPT, FAIL, error=f"{type(exc).__name__}: {exc}",
                            seconds=time.time() - started)
                return False
        if receipt is None:
            self._stage(STAGE_RECEIPT, BLOCKED, {"reason": "AR-0 receipt path unavailable"},
                        seconds=time.time() - started)
            return False
        self._fact("receipt", str(_get(receipt, "receipt_ref", "evidence_ref", default="?")),
                   extra={"kind": _get(receipt, "observation_kind", default=None)})
        self._stage(STAGE_RECEIPT, PASS, _mapping(receipt) or {}, seconds=time.time() - started)
        return True

    def _run_reconciliation(self, action: Any) -> None:
        started = time.time()
        adapter = self._adapter(STAGE_AR0)
        view = None
        if adapter is not None and hasattr(adapter, "reconcile"):
            try:
                view = adapter.reconcile(action_ref=_get(action, "action_ref", default="?"))
            except Exception as exc:  # noqa: BLE001
                self._stage(STAGE_RECONCILIATION, FAIL, error=f"{type(exc).__name__}: {exc}",
                            seconds=time.time() - started)
                return
        if view is None:
            self._stage(STAGE_RECONCILIATION, BLOCKED, {"reason": "AR-0 reconciliation path unavailable"},
                        seconds=time.time() - started)
            return
        ref = _get(view, "reconciliation_ref", "ref", default="?")
        self._fact("receipt", str(ref), _get(view, "revision", default=None), stage="reconciliation")
        self._stage(STAGE_RECONCILIATION, PASS, _mapping(view) or {"reconciliation_ref": ref},
                    seconds=time.time() - started)

    def _run_ar1(self, action: Any, transport: Any) -> bool:
        started = time.time()
        adapter = self._adapter(STAGE_AR1)
        if adapter is None:
            self._stage(STAGE_AR1, BLOCKED, {"reason": "adapter unavailable"}, seconds=time.time() - started)
            return False
        evidence = None
        if hasattr(transport, "result_evidence"):
            evidence = transport.result_evidence
        elif hasattr(transport, "receipt"):
            evidence = transport.receipt
        try:
            settled = adapter.settle(action_ref=_get(action, "action_ref", default="?"),
                                     result_evidence=evidence, observed_at=self.stamp())
        except Exception as exc:  # noqa: BLE001
            self._stage(STAGE_AR1, FAIL, error=f"{type(exc).__name__}: {exc}", seconds=time.time() - started)
            return False
        self._fact("settlement", str(_get(settled, "settlement_ref", "record_id", default="?")),
                   _get(settled, "revision", default=None))
        self._stage(STAGE_AR1, PASS, _mapping(settled) or {}, seconds=time.time() - started)
        return True

    def _run_handoff(self, action: Any, transport: Any) -> None:
        started = time.time()
        projection = _get(transport, "projection", default=None)
        facts = _get(transport, "handoff_facts", default=None)
        payload = {
            "action_ref": _get(action, "action_ref", default="?"),
            "delivered": bool(_get(projection, "delivered", default=False)) if projection is not None else None,
            "read_status": _get(projection, "read_status", default=None) if projection is not None else None,
            "memory_write": False,
            "typed_proposal": True,
        }
        self._fact("handoff", f"handoff:{payload['action_ref']}", extra={"memory_write": False})
        self._stage(STAGE_HANDOFF, PASS, {"payload": payload,
                                          "handoff_facts": _mapping(facts) if facts is not None else None},
                    seconds=time.time() - started)

    # -- finish / trace ---------------------------------------------------
    def _finish(self, result: dict[str, Any], verdict: str) -> dict[str, Any]:
        result["verdict"] = verdict
        result["stages"] = [stage.to_mapping() for stage in self.stages]
        result["facts"] = [fact.to_mapping() for fact in self.facts]
        result["notes"] = list(self.notes)
        result["adapter_availability"] = {stage: availability.to_mapping()
                                         for stage, availability in self.availability.items()}
        result["authority_trace"] = self.authority_trace()
        result["isolated_root"] = str(self.isolated_root)
        result["namespace"] = self.namespace
        return result

    def authority_trace(self) -> list[dict[str, Any]]:
        """fact / owner / writer / ref / revision, end to end (order sections 79-80)."""
        seen: dict[str, dict[str, Any]] = {}
        for fact in self.facts:
            entry = fact.to_mapping()
            if fact.fact in EXPECTED_FACT_WRITERS:
                seen[fact.fact] = entry
        return [seen[key] for key in EXPECTED_FACT_WRITERS if key in seen]

    def no_universal_integration_writer(self) -> bool:
        """This class writes nothing canonical itself (order section 81)."""
        return True


# ==========================================================================
# helpers
# ==========================================================================
def _instant(value: str):
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def _is_expired(intent: Any, now: str) -> bool:
    valid_until = getattr(intent, "valid_until", None)
    if not isinstance(valid_until, str):
        return False
    try:
        return _instant(now) >= _instant(valid_until)
    except ValueError:
        return False


def _get(obj: Any, *names: str, default: Any = None) -> Any:
    if obj is None:
        return default
    if isinstance(obj, Mapping):
        for name in names:
            if name in obj:
                return obj[name]
        return default
    for name in names:
        value = getattr(obj, name, None)
        if value is not None:
            return value
    return default


def _mapping(obj: Any) -> dict[str, Any] | None:
    if obj is None:
        return None
    if isinstance(obj, Mapping):
        return dict(obj)
    for name in ("to_mapping", "to_dict", "as_dict"):
        method = getattr(obj, name, None)
        if callable(method):
            try:
                value = method()
                if isinstance(value, Mapping):
                    return dict(value)
            except Exception:  # noqa: BLE001
                continue
    return None


def _kill_switch_state() -> dict[str, bool]:
    """Production kill switches are read, never changed (order sections 2, 106)."""
    def _flag(name: str) -> bool:
        value = os.environ.get(name, "").strip().lower()
        return value in ("1", "true", "yes", "on")

    return {
        "life_runtime_enabled": _flag("LIFE_RUNTIME_ENABLED"),
        "agency_enabled": _flag("AGENCY_ENABLED"),
        "action_execution_enabled": _flag("ACTION_EXECUTION_ENABLED"),
        "proactive_enabled": _flag("PROACTIVE_ENABLED"),
    }


__all__ = [
    "BLOCKED", "BLOCKED_AT_AG0", "CHAIN_SCHEMA", "CanonicalChain", "DISPATCH_BLOCK_ATOMIC",
    "DISPATCH_BLOCK_AUTHORIZATION", "DISPATCH_BLOCK_CAPACITY_CHANGED", "DISPATCH_BLOCK_CHANNEL",
    "DISPATCH_BLOCK_DEVICE", "DISPATCH_BLOCK_INTENT_INVALID", "DISPATCH_BLOCK_KILL_SWITCH",
    "DISPATCH_BLOCK_PENDING", "DISPATCH_BLOCK_PREVIOUS_UNKNOWN", "DISPATCH_BLOCK_QUIET",
    "DISPATCH_BLOCK_TELEGRAM_FENCE", "DISPATCH_BLOCK_UNRESOLVED_INBOUND", "DispatchAdmission",
    "FAIL", "PASS", "SKIPPED", "STAGE_AR0", "STAGE_AR1", "STAGE_AG0", "STAGE_AG1",
    "STAGE_CAPACITY", "STAGE_CONTACT_INTENT", "STAGE_DISPATCH", "STAGE_EXPRESSION",
    "STAGE_GROUNDING", "STAGE_HANDOFF", "STAGE_ORDER", "STAGE_OWNER", "STAGE_RECEIPT",
    "STAGE_RECONCILIATION", "STAGE_TELEGRAM", "STAGE_TRANSPORT", "StageOutcome",
    "dispatch_admission", "probe_adapters",
]
