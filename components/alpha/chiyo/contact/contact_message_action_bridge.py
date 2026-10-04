"""Isolated CT0-6 bridge: frozen Draft -> AR-0 `MESSAGE_ACTION` proposal request.

Ownership contract:
  * AR-0 remains the only Message Action lifecycle owner. This module never
    mints, stores, or transitions an AR-0 record; it *asks* an injected AR-0
    owner port to propose.
  * The canonical AR-0 implementation is **not vendored** in this sandbox, so
    the port is verified with a recording fake. `AR0_CANONICAL_ADAPTER` is
    recorded as integration debt in the CT0-6 report; nothing here claims real
    AR-0 runtime integration.
  * There is deliberately no submit / send / delivery / persistence / Telegram /
    network / scheduling API. `ContactMessageActionRequest.would_submit` only
    reports what an authoritative submit gate *would* decide.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Mapping, Protocol

from contact_draft_model import ContactDecisionReceipt, ContactGroundingSource, ContactMessageDraft
from contact_draft_projection import validate_frozen_draft_projection
from contact_intent_model import STATUS_SELECTED, ContactIntent
from contact_message_action_model import (
    ACTION_KIND_MESSAGE_ACTION,
    CONTACT_MESSAGE_ACTION_POLICY_VERSION,
    ContactActionGate,
    ContactMessageActionContractError,
    ContactMessageActionRequest,
    InvalidContactMessageActionRequestError,
    InvalidMessageActionSourceBindingError,
    build_message_action_request,
    sha256_text,
)
from contact_message_action_projection import validate_message_action_request_projection


RESULT_PROPOSAL_CREATED = "PROPOSAL_CREATED"
RESULT_PROPOSAL_REPLAYED = "PROPOSAL_REPLAYED"
RESULT_BLOCKED = "BLOCKED"
RESULT_CONFLICT = "LOGICAL_ACTION_CONFLICT"
RESULT_AR0_CONTRACT_VIOLATION = "AR0_ACKNOWLEDGEMENT_CONTRACT_VIOLATION"

FAILURE_DRAFT_BINDING_INVALID = "DRAFT_OWNER_BINDING_INVALID"
FAILURE_CHANNEL_OUT_OF_SCOPE = "CHANNEL_OUT_OF_DRAFT_SCOPE"
FAILURE_REQUEST_NOT_FROZEN = "REQUEST_NOT_FROZEN"
FAILURE_PROJECTION_FAILED = "REQUEST_PROJECTION_FAILED"
FAILURE_AR0_ACKNOWLEDGEMENT_INVALID = "AR0_ACKNOWLEDGEMENT_INVALID"
FAILURE_LOGICAL_ACTION_ALREADY_EXISTS = "LOGICAL_ACTION_ALREADY_EXISTS"


class ContactMessageActionBridgeError(ContactMessageActionContractError):
    """Base error for isolated Message Action bridge input/state failures."""


class Ar0AcknowledgementContractError(ContactMessageActionBridgeError):
    """An injected AR-0 port returned an acknowledgement outside the contract."""


class Ar0MessageActionPort(Protocol):
    """Adapter owned by the canonical AR-0 Action Reality Ledger.

    Implementations must be idempotent on `request.idempotency_key` and must
    return an acknowledgement shaped like `propose_action`'s canonical result:
    `{"idempotent_replay": bool, "proposal": {...}, "action": {...}}`.
    The port must not expose submit/send capability to the CT0-6 bridge.
    """

    def propose_message_action(self, *, request: ContactMessageActionRequest) -> object:
        ...


@dataclass(frozen=True, slots=True)
class Ar0ProposalAcknowledgement:
    """Detached, validated projection of an AR-0 propose result."""

    idempotent_replay: bool
    proposal_id: str
    action_id: str
    action_status: str
    idempotency_key: str
    action_kind: str
    target_ref: str
    payload_ref: str
    decision_ref: str
    authorization_ref: str
    source_refs: tuple[str, ...]
    causal_parent_refs: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.idempotent_replay, bool):
            raise Ar0AcknowledgementContractError("idempotent_replay must be a bool")
        if not isinstance(self.proposal_id, str) or not self.proposal_id.startswith("aprop:"):
            raise Ar0AcknowledgementContractError("AR-0 proposal_id must use the aprop: namespace")
        if not isinstance(self.action_id, str) or not self.action_id.startswith("actn:"):
            raise Ar0AcknowledgementContractError("AR-0 action_id must use the actn: namespace")
        for name in ("action_status", "idempotency_key", "action_kind", "target_ref", "payload_ref"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value:
                raise Ar0AcknowledgementContractError(f"AR-0 {name} must be a non-empty string")
        for name in ("source_refs", "causal_parent_refs"):
            value = getattr(self, name)
            if not isinstance(value, tuple) or any(not isinstance(ref, str) or not ref for ref in value):
                raise Ar0AcknowledgementContractError(f"AR-0 {name} must be a tuple of references")


def _refs(value: object, name: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)):
        raise Ar0AcknowledgementContractError(f"AR-0 {name} must be a list/tuple")
    return tuple(value)


def project_ar0_acknowledgement(response: object, request: ContactMessageActionRequest) -> Ar0ProposalAcknowledgement:
    """Validate that the injected AR-0 port honoured the frozen request.

    Fail-closed: any missing field, wrong namespace, or a mismatched
    idempotency key / lineage means the acknowledgement cannot be trusted as
    the logical Action for this request.
    """
    if not isinstance(response, Mapping):
        raise Ar0AcknowledgementContractError("AR-0 port response must be a mapping")
    proposal = response.get("proposal")
    action = response.get("action")
    if not isinstance(proposal, Mapping) or not isinstance(action, Mapping):
        raise Ar0AcknowledgementContractError("AR-0 response must carry proposal and action mappings")
    if not isinstance(response.get("idempotent_replay"), bool):
        raise Ar0AcknowledgementContractError("AR-0 response must carry a boolean idempotent_replay")
    for field in (
        "action_id", "status", "idempotency_key",
        "action_kind", "target_ref", "payload_ref", "decision_ref", "authorization_ref",
    ):
        if field not in action:
            raise Ar0AcknowledgementContractError(f"AR-0 action is missing {field!r}")
    if "proposal_ref" not in action:
        raise Ar0AcknowledgementContractError("AR-0 action is missing 'proposal_ref'")
    if "proposal_id" not in proposal or "payload_ref" not in proposal:
        raise Ar0AcknowledgementContractError("AR-0 proposal is missing proposal_id/payload_ref")
    if str(action["proposal_ref"]) != str(proposal["proposal_id"]):
        raise Ar0AcknowledgementContractError("AR-0 action.proposal_ref does not match proposal.proposal_id")

    acknowledgement = Ar0ProposalAcknowledgement(
        idempotent_replay=bool(response["idempotent_replay"]),
        proposal_id=str(proposal["proposal_id"]),
        action_id=str(action["action_id"]),
        action_status=str(action["status"]),
        idempotency_key=str(action["idempotency_key"]),
        action_kind=str(action["action_kind"]),
        target_ref=str(action["target_ref"]),
        payload_ref=str(action["payload_ref"]),
        decision_ref=str(action.get("decision_ref") or ""),
        authorization_ref=str(action.get("authorization_ref") or ""),
        source_refs=_refs(action.get("source_refs"), "source_refs"),
        causal_parent_refs=_refs(action.get("causal_parent_refs"), "causal_parent_refs"),
    )
    if acknowledgement.idempotency_key != request.idempotency_key:
        raise Ar0AcknowledgementContractError("AR-0 recorded a different idempotency key than the frozen request")
    if acknowledgement.action_kind != ACTION_KIND_MESSAGE_ACTION:
        raise Ar0AcknowledgementContractError("AR-0 recorded a different action_kind")
    if acknowledgement.target_ref != request.target_ref:
        raise Ar0AcknowledgementContractError("AR-0 recorded a different target_ref")
    if acknowledgement.payload_ref != request.payload_ref:
        raise Ar0AcknowledgementContractError("AR-0 recorded a different payload_ref")
    if acknowledgement.decision_ref != request.decision_ref:
        raise Ar0AcknowledgementContractError("AR-0 recorded a different decision_ref")
    if acknowledgement.authorization_ref != request.authorization_ref:
        raise Ar0AcknowledgementContractError("AR-0 recorded a different authorization_ref")
    lineage = set(acknowledgement.source_refs) | set(acknowledgement.causal_parent_refs)
    if not set(request.causal_parent_refs) <= lineage:
        raise Ar0AcknowledgementContractError("AR-0 dropped a required causal parent reference")
    if not set(request.source_refs) <= set(acknowledgement.source_refs):
        raise Ar0AcknowledgementContractError("AR-0 dropped a required source reference")
    if not set(request.causal_parent_refs) <= set(acknowledgement.causal_parent_refs):
        raise Ar0AcknowledgementContractError("AR-0 dropped a required causal parent reference field")
    return acknowledgement


@dataclass(frozen=True, slots=True)
class ContactMessageActionResult:
    status: str
    intent_ref: str
    decision_ref: str
    draft_ref: str
    request_id: str | None
    idempotency_key: str
    ar0_proposal_ref: str | None
    ar0_action_ref: str | None
    ar0_action_status: str | None
    logical_proposal_created: bool
    ar0_port_called: bool
    failure_code: str | None
    replayed: bool


@dataclass(frozen=True, slots=True)
class _StoredBridgeOutcome:
    result_status: str
    intent_ref: str
    decision_ref: str
    draft_ref: str
    content_hash: str
    request_id: str
    request_fingerprint: str
    idempotency_key: str
    ar0_proposal_ref: str | None
    ar0_action_ref: str | None
    ar0_action_status: str | None
    logical_proposal_created: bool
    failure_code: str | None


class ContactMessageActionBridge:
    """Create at most one logical AR-0 Message Action per Intent/Decision/content.

    State is an in-memory contract fixture. Durable idempotency belongs to the
    canonical AR-0 owner; this bridge additionally refuses to re-enter the port
    for an already-recorded logical Action so a replay cannot become a second
    logical proposal.
    """

    def __init__(
        self,
        *,
        ar0_port: Ar0MessageActionPort,
        policy_version: str = CONTACT_MESSAGE_ACTION_POLICY_VERSION,
    ) -> None:
        if ar0_port is None or not hasattr(ar0_port, "propose_message_action"):
            raise ContactMessageActionBridgeError("bridge requires an injected AR-0 Message Action port")
        if not isinstance(policy_version, str) or not policy_version:
            raise ContactMessageActionBridgeError("policy_version is required")
        self._ar0_port = ar0_port
        self.policy_version = policy_version
        self._outcomes: dict[str, _StoredBridgeOutcome] = {}
        self._logical_keys: dict[tuple[str, str, str], str] = {}
        self.ar0_port_call_count = 0

    @property
    def logical_proposal_count(self) -> int:
        return sum(1 for outcome in self._outcomes.values() if outcome.logical_proposal_created)

    @property
    def stored_outcome_count(self) -> int:
        return len(self._outcomes)

    @staticmethod
    def _logical_key(intent: ContactIntent, decision: ContactDecisionReceipt, draft: ContactMessageDraft):
        # One committed decision authorizes at most ONE logical Message Action.
        # The key deliberately omits content hash and channel: a second, differently
        # shaped request for the same Intent/Decision is a conflict, not a new action.
        return (intent.intent_id, decision.decision_ref)

    @staticmethod
    def _request_fingerprint(request: ContactMessageActionRequest) -> str:
        return sha256_text(json.dumps(request.to_dict(), sort_keys=True, separators=(",", ":")))

    def propose(
        self,
        *,
        intent: object,
        decision: object,
        draft: object,
        gate: ContactActionGate,
        channel: str,
        created_at: str,
        grounding_sources: tuple[ContactGroundingSource, ...] | None = None,
    ) -> ContactMessageActionResult:
        if not isinstance(intent, ContactIntent) or not isinstance(decision, ContactDecisionReceipt) or not isinstance(draft, ContactMessageDraft):
            raise ContactMessageActionBridgeError("propose requires typed ContactIntent/Decision/Draft values")
        if not isinstance(gate, ContactActionGate):
            raise ContactMessageActionBridgeError("propose requires a ContactActionGate")

        # Fail closed before any request/port work: an unsupported channel can
        # never become an AR-0 proposal.
        if not isinstance(channel, str) or channel not in draft.channel_scope:
            return self._blocked(intent, decision, draft, FAILURE_CHANNEL_OUT_OF_SCOPE)
        if intent.status != STATUS_SELECTED:
            return self._blocked(intent, decision, draft, FAILURE_DRAFT_BINDING_INVALID)

        try:
            request = build_message_action_request(
                intent=intent,
                decision=decision,
                draft=draft,
                gate=gate,
                channel=channel,
                created_at=created_at,
                policy_version=self.policy_version,
            )
        except InvalidMessageActionSourceBindingError:
            return self._blocked(intent, decision, draft, FAILURE_DRAFT_BINDING_INVALID)
        except (InvalidContactMessageActionRequestError, ContactMessageActionContractError) as exc:
            return self._blocked(intent, decision, draft, f"{FAILURE_REQUEST_NOT_FROZEN}:{type(exc).__name__}")

        key = request.idempotency_key
        logical_key = self._logical_key(intent, decision, draft)
        fingerprint = self._request_fingerprint(request)

        prior = self._outcomes.get(key)
        if prior is not None:
            if prior.request_fingerprint != fingerprint:
                return self._result(prior, replayed=True, status=RESULT_CONFLICT, ar0_port_called=False)
            return self._result(prior, replayed=True, status=self._replay_status(prior), ar0_port_called=False)

        existing_key = self._logical_keys.get(logical_key)
        if existing_key is not None:
            conflicting = self._outcomes[existing_key]
            return ContactMessageActionResult(
                status=RESULT_CONFLICT,
                intent_ref=intent.intent_id,
                decision_ref=decision.decision_ref,
                draft_ref=draft.draft_id,
                request_id=request.request_id,
                idempotency_key=request.idempotency_key,
                ar0_proposal_ref=conflicting.ar0_proposal_ref,
                ar0_action_ref=conflicting.ar0_action_ref,
                ar0_action_status=conflicting.ar0_action_status,
                logical_proposal_created=False,
                ar0_port_called=False,
                failure_code=FAILURE_LOGICAL_ACTION_ALREADY_EXISTS,
                replayed=False,
            )

        # Optional CT0-5 grounding re-validation. When the caller supplies the
        # same hash-bound source projections the Draft was built from, the
        # bridge re-verifies exact quotation grounding before AR-0 is reachable.
        # CT0-6 does not own grounding resolution; it only refuses an ungrounded
        # Draft that someone tries to promote into an Action.
        if grounding_sources is not None:
            try:
                validate_frozen_draft_projection(
                    intent=intent, decision=decision, draft=draft, grounding_sources=grounding_sources
                )
            except Exception:
                return self._blocked(intent, decision, draft, FAILURE_PROJECTION_FAILED)

        # Revalidate the frozen request against its owners before reaching AR-0.
        try:
            validate_message_action_request_projection(
                intent=intent, decision=decision, draft=draft, request=request
            )
        except Exception as exc:
            outcome = _StoredBridgeOutcome(
                RESULT_BLOCKED, intent.intent_id, decision.decision_ref, draft.draft_id,
                draft.content_hash, request.request_id, fingerprint, key, None, None, None, False,
                f"{FAILURE_PROJECTION_FAILED}:{type(exc).__name__}",
            )
            self._outcomes[key] = outcome
            return self._result(outcome, replayed=False, status=RESULT_BLOCKED, ar0_port_called=False)

        self.ar0_port_call_count += 1
        try:
            response = self._ar0_port.propose_message_action(request=request)
            acknowledgement = project_ar0_acknowledgement(response, request)
        except Exception as exc:
            outcome = _StoredBridgeOutcome(
                RESULT_AR0_CONTRACT_VIOLATION, intent.intent_id, decision.decision_ref, draft.draft_id,
                draft.content_hash, request.request_id, fingerprint, key, None, None, None, False,
                f"{FAILURE_AR0_ACKNOWLEDGEMENT_INVALID}:{type(exc).__name__}",
            )
            self._outcomes[key] = outcome
            return self._result(outcome, replayed=False, status=RESULT_AR0_CONTRACT_VIOLATION, ar0_port_called=True)

        logical_created = not acknowledgement.idempotent_replay
        outcome = _StoredBridgeOutcome(
            RESULT_PROPOSAL_CREATED if logical_created else RESULT_PROPOSAL_REPLAYED,
            intent.intent_id, decision.decision_ref, draft.draft_id, draft.content_hash,
            request.request_id, fingerprint, key, acknowledgement.proposal_id, acknowledgement.action_id,
            acknowledgement.action_status, logical_created, None,
        )
        self._outcomes[key] = outcome
        if logical_created:
            self._logical_keys[logical_key] = key
        return self._result(outcome, replayed=acknowledgement.idempotent_replay, status=outcome.result_status, ar0_port_called=True)

    def replay(
        self,
        *,
        intent: object,
        decision: object,
        draft: object,
        gate: ContactActionGate,
        channel: str,
        grounding_sources: tuple[ContactGroundingSource, ...] | None = None,
    ) -> ContactMessageActionResult | None:
        """Read the stored outcome without re-entering the AR-0 port."""
        if not isinstance(intent, ContactIntent) or not isinstance(decision, ContactDecisionReceipt) or not isinstance(draft, ContactMessageDraft):
            raise ContactMessageActionBridgeError("replay requires typed ContactIntent/Decision/Draft values")
        if not isinstance(gate, ContactActionGate):
            raise ContactMessageActionBridgeError("replay requires a ContactActionGate")
        if grounding_sources is not None:
            try:
                validate_frozen_draft_projection(
                    intent=intent, decision=decision, draft=draft, grounding_sources=grounding_sources
                )
            except Exception:
                return None
        if not isinstance(channel, str) or channel not in draft.channel_scope:
            return None
        try:
            request = build_message_action_request(
                intent=intent, decision=decision, draft=draft, gate=gate, channel=channel,
                created_at=draft.created_at, policy_version=self.policy_version,
            )
        except Exception:
            return None
        outcome = self._outcomes.get(request.idempotency_key)
        if outcome is None:
            return None
        return self._result(outcome, replayed=True, status=self._replay_status(outcome), ar0_port_called=False)

    @staticmethod
    def _replay_status(outcome: _StoredBridgeOutcome) -> str:
        if outcome.result_status == RESULT_PROPOSAL_CREATED:
            return RESULT_PROPOSAL_REPLAYED
        return outcome.result_status

    def _blocked(
        self,
        intent: ContactIntent,
        decision: ContactDecisionReceipt,
        draft: ContactMessageDraft,
        failure_code: str,
    ) -> ContactMessageActionResult:
        return ContactMessageActionResult(
            status=RESULT_BLOCKED,
            intent_ref=intent.intent_id,
            decision_ref=decision.decision_ref,
            draft_ref=draft.draft_id,
            request_id=None,
            idempotency_key=f"mreq-blocked:{draft.draft_id}",
            ar0_proposal_ref=None,
            ar0_action_ref=None,
            ar0_action_status=None,
            logical_proposal_created=False,
            ar0_port_called=False,
            failure_code=failure_code,
            replayed=False,
        )

    def _result(
        self,
        outcome: _StoredBridgeOutcome,
        *,
        replayed: bool,
        status: str,
        ar0_port_called: bool,
    ) -> ContactMessageActionResult:
        return ContactMessageActionResult(
            status=status,
            intent_ref=outcome.intent_ref,
            decision_ref=outcome.decision_ref,
            draft_ref=outcome.draft_ref,
            request_id=outcome.request_id,
            idempotency_key=outcome.idempotency_key,
            ar0_proposal_ref=outcome.ar0_proposal_ref,
            ar0_action_ref=outcome.ar0_action_ref,
            ar0_action_status=outcome.ar0_action_status,
            logical_proposal_created=outcome.logical_proposal_created and not replayed,
            ar0_port_called=ar0_port_called,
            failure_code=outcome.failure_code,
            replayed=replayed,
        )


__all__ = [
    "FAILURE_AR0_ACKNOWLEDGEMENT_INVALID", "FAILURE_CHANNEL_OUT_OF_SCOPE",
    "FAILURE_DRAFT_BINDING_INVALID", "FAILURE_LOGICAL_ACTION_ALREADY_EXISTS",
    "FAILURE_PROJECTION_FAILED", "FAILURE_REQUEST_NOT_FROZEN",
    "RESULT_AR0_CONTRACT_VIOLATION", "RESULT_BLOCKED", "RESULT_CONFLICT",
    "RESULT_PROPOSAL_CREATED", "RESULT_PROPOSAL_REPLAYED",
    "Ar0AcknowledgementContractError", "Ar0MessageActionPort", "Ar0ProposalAcknowledgement",
    "ContactMessageActionResult", "ContactMessageActionBridge", "ContactMessageActionBridgeError",
    "project_ar0_acknowledgement",
]
