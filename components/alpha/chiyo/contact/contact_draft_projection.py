"""Pure read-only CT0-5 Draft grounding and ownership projection."""

from __future__ import annotations

from functools import lru_cache

from contact_intent_model import ContactIntent
from contact_draft_model import (
    DECISION_CONTACT_SELECTED,
    ContactDecisionReceipt,
    ContactDraftContractError,
    ContactGroundingSource,
    ContactMessageDraft,
    InvalidGroundingSourceError,
)


class ContactDraftProjectionError(ContactDraftContractError):
    """A frozen Draft does not match its committed owners or source projections."""


def _detached_intent(value: object) -> ContactIntent:
    if not isinstance(value, ContactIntent):
        raise ContactDraftProjectionError("projection requires a ContactIntent")
    try:
        return ContactIntent.from_mapping(value.to_dict())
    except Exception as exc:
        raise ContactDraftProjectionError("ContactIntent failed projection revalidation") from exc


def _detached_decision(value: object) -> ContactDecisionReceipt:
    if not isinstance(value, ContactDecisionReceipt):
        raise ContactDraftProjectionError("projection requires a committed decision receipt")
    try:
        return ContactDecisionReceipt.from_mapping(
            {
                "schema_version": value.schema_version,
                "committed": value.committed,
                "intent_ref": value.intent_ref,
                "candidate_ref": value.candidate_ref,
                "decision_ref": value.decision_ref,
                "outcome": value.outcome,
                "selected_candidate_ref": value.selected_candidate_ref,
                "committed_at": value.committed_at,
            }
        )
    except Exception as exc:
        raise ContactDraftProjectionError("Decision receipt failed projection revalidation") from exc


def _detached_draft(value: object) -> ContactMessageDraft:
    if not isinstance(value, ContactMessageDraft):
        raise ContactDraftProjectionError("projection requires a frozen ContactMessageDraft")
    try:
        return ContactMessageDraft.from_mapping(
            {
                "draft_id": value.draft_id,
                "schema_version": value.schema_version,
                "status": value.status,
                "intent_ref": value.intent_ref,
                "candidate_ref": value.candidate_ref,
                "decision_ref": value.decision_ref,
                "recipient_ref": value.recipient_ref,
                "channel_scope": value.channel_scope,
                "source_refs": value.source_refs,
                "content": value.content,
                "content_hash": value.content_hash,
                "idempotency_key": value.idempotency_key,
                "created_at": value.created_at,
                "policy_version": value.policy_version,
            }
        )
    except Exception as exc:
        raise ContactDraftProjectionError("ContactMessageDraft failed projection revalidation") from exc


def _is_exact_ordered_grounding(
    content: str,
    source_refs: tuple[str, ...],
    sources: dict[str, ContactGroundingSource],
) -> bool:
    """Check one verbatim contiguous excerpt per cited source, ordered by refs."""
    separators = [index for index, char in enumerate(content) if char == " "]
    boundaries = (-1, *separators, len(content))
    if len(source_refs) > len(boundaries) - 1:
        return False

    @lru_cache(maxsize=None)
    def match(source_index: int, start: int) -> bool:
        if source_index == len(source_refs):
            return start == len(content)
        source_text = sources[source_refs[source_index]].canonical_text
        for boundary in boundaries:
            end = boundary
            if end <= start:
                continue
            excerpt = content[start:end]
            if excerpt not in source_text:
                continue
            if source_index == len(source_refs) - 1:
                if end == len(content):
                    return True
                continue
            if end < len(content) and content[end] == " " and match(source_index + 1, end + 1):
                return True
        return False

    return match(0, 0)


def validate_frozen_draft_projection(
    *,
    intent: object,
    decision: object,
    draft: object,
    grounding_sources: tuple[ContactGroundingSource, ...],
) -> ContactMessageDraft:
    """Revalidate detached Draft provenance and exact grounding without generation."""
    intent = _detached_intent(intent)
    decision = _detached_decision(decision)
    draft = _detached_draft(draft)
    if (
        not decision.committed
        or decision.outcome != DECISION_CONTACT_SELECTED
        or decision.intent_ref != intent.intent_id
        or decision.candidate_ref != intent.candidate_ref
        or decision.selected_candidate_ref != intent.candidate_ref
    ):
        raise ContactDraftProjectionError("Decision is not a committed selection for this Intent")
    if intent.status != "SELECTED":
        raise ContactDraftProjectionError("ContactIntent is not in SELECTED state")
    if (
        draft.intent_ref != intent.intent_id
        or draft.candidate_ref != intent.candidate_ref
        or draft.decision_ref != decision.decision_ref
        or draft.recipient_ref != intent.recipient_ref
        or draft.channel_scope != intent.channel_scope
    ):
        raise ContactDraftProjectionError("Draft owner binding differs from Intent/Decision")
    if not isinstance(grounding_sources, tuple) or any(
        not isinstance(source, ContactGroundingSource) for source in grounding_sources
    ):
        raise ContactDraftProjectionError("grounding source projection must be an immutable tuple")
    allowed_refs = set(intent.source_refs) & set(intent.content_scope_refs)
    sources: dict[str, ContactGroundingSource] = {}
    for source in grounding_sources:
        try:
            detached = ContactGroundingSource.from_mapping(
                {
                    "schema_version": source.schema_version,
                    "source_ref": source.source_ref,
                    "canonical_text": source.canonical_text,
                    "content_hash": source.content_hash,
                }
            )
        except (InvalidGroundingSourceError, TypeError, ValueError) as exc:
            raise ContactDraftProjectionError("grounding source failed projection validation") from exc
        if detached.source_ref not in allowed_refs:
            raise ContactDraftProjectionError("grounding source is outside Intent content scope")
        if detached.source_ref in sources:
            raise ContactDraftProjectionError("duplicate grounding source reference")
        sources[detached.source_ref] = detached

    if not draft.source_refs or any(ref not in sources for ref in draft.source_refs):
        raise ContactDraftProjectionError("Draft source refs are not all resolved grounding sources")
    if not _is_exact_ordered_grounding(draft.content, draft.source_refs, sources):
        raise ContactDraftProjectionError("Draft is not an exact ordered concatenation of source excerpts")
    return draft


__all__ = ["ContactDraftProjectionError", "validate_frozen_draft_projection"]
