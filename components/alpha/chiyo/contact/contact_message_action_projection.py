"""Pure read-only CT0-6 request lineage and ownership projection."""

from __future__ import annotations

from dataclasses import asdict

from contact_draft_model import (
    DECISION_CONTACT_SELECTED,
    ContactDecisionReceipt,
    ContactMessageDraft,
)
from contact_intent_model import STATUS_SELECTED, ContactIntent
from contact_message_action_model import (
    ContactMessageActionContractError,
    ContactMessageActionRequest,
    InvalidContactMessageActionRequestError,
    InvalidMessageActionSourceBindingError,
    derive_message_action_request_identity,
)


class ContactMessageActionProjectionError(ContactMessageActionContractError):
    """A frozen Message Action request does not match its owners or lineage."""


def _detached_intent(value: object) -> ContactIntent:
    if not isinstance(value, ContactIntent):
        raise ContactMessageActionProjectionError("projection requires a ContactIntent")
    try:
        return ContactIntent.from_mapping(value.to_dict())
    except Exception as exc:
        raise ContactMessageActionProjectionError("ContactIntent failed projection revalidation") from exc


def _detached_decision(value: object) -> ContactDecisionReceipt:
    if not isinstance(value, ContactDecisionReceipt):
        raise ContactMessageActionProjectionError("projection requires a committed Decision receipt")
    try:
        return ContactDecisionReceipt.from_mapping(asdict(value))
    except Exception as exc:
        raise ContactMessageActionProjectionError("Decision receipt failed projection revalidation") from exc


def _detached_draft(value: object) -> ContactMessageDraft:
    if not isinstance(value, ContactMessageDraft):
        raise ContactMessageActionProjectionError("projection requires a frozen ContactMessageDraft")
    try:
        return ContactMessageDraft.from_mapping(asdict(value))
    except Exception as exc:
        raise ContactMessageActionProjectionError("ContactMessageDraft failed projection revalidation") from exc


def validate_message_action_request_projection(
    *,
    intent: object,
    decision: object,
    draft: object,
    request: object,
) -> ContactMessageActionRequest:
    """Revalidate a frozen request's owner binding, lineage, and payload without proposing."""
    intent = _detached_intent(intent)
    decision = _detached_decision(decision)
    draft = _detached_draft(draft)
    if not isinstance(request, ContactMessageActionRequest):
        raise ContactMessageActionProjectionError("projection requires a typed Message Action request")
    try:
        request = ContactMessageActionRequest.from_mapping(request.to_dict())
    except (InvalidContactMessageActionRequestError, TypeError, ValueError) as exc:
        raise ContactMessageActionProjectionError("request failed projection revalidation") from exc

    if (
        not decision.committed
        or decision.outcome != DECISION_CONTACT_SELECTED
        or decision.intent_ref != intent.intent_id
        or decision.candidate_ref != intent.candidate_ref
        or decision.selected_candidate_ref != intent.candidate_ref
    ):
        raise ContactMessageActionProjectionError("Decision is not a committed selection for this Intent")
    if intent.status != STATUS_SELECTED:
        raise ContactMessageActionProjectionError("ContactIntent is not in SELECTED state")
    if (
        draft.intent_ref != intent.intent_id
        or draft.candidate_ref != intent.candidate_ref
        or draft.decision_ref != decision.decision_ref
        or draft.recipient_ref != intent.recipient_ref
        or draft.channel_scope != intent.channel_scope
    ):
        raise ContactMessageActionProjectionError("Draft owner binding differs from Intent/Decision")
    if (
        request.intent_ref != intent.intent_id
        or request.candidate_ref != intent.candidate_ref
        or request.decision_ref != decision.decision_ref
        or request.draft_ref != draft.draft_id
        or request.recipient_ref != draft.recipient_ref
        or request.content_hash != draft.content_hash
    ):
        raise ContactMessageActionProjectionError("request owner binding differs from Draft/Intent/Decision")
    if request.channel not in draft.channel_scope:
        raise ContactMessageActionProjectionError("request channel is outside the frozen Draft channel scope")

    identity = derive_message_action_request_identity(
        draft_ref=draft.draft_id,
        content_hash=draft.content_hash,
        recipient_ref=draft.recipient_ref,
        channel=request.channel,
    )
    if (
        request.request_id != identity.request_id
        or request.idempotency_key != identity.idempotency_key
        or request.payload_ref != identity.payload_ref
        or request.correlation_id != identity.correlation_id
    ):
        raise ContactMessageActionProjectionError("request identity is not the deterministic Draft identity")

    payload = request.payload_data
    if payload.get("text") != draft.content:
        raise ContactMessageActionProjectionError("request payload text differs from frozen Draft content")
    if payload.get("draft_ref") != draft.draft_id:
        raise ContactMessageActionProjectionError("request payload draft_ref differs from the frozen Draft")

    # Lineage must retain every owner reference, without inventing new ones.
    lineage = set(request.causal_parent_refs) | set(request.source_refs)
    required = {intent.candidate_ref, intent.intent_id, decision.decision_ref, draft.draft_id}
    if not required <= lineage:
        raise ContactMessageActionProjectionError("request lineage dropped a required owner reference")
    if not set(draft.source_refs) <= set(request.source_refs):
        raise ContactMessageActionProjectionError("request source refs dropped a Draft grounding source")
    return request


__all__ = [
    "ContactMessageActionProjectionError",
    "validate_message_action_request_projection",
]
