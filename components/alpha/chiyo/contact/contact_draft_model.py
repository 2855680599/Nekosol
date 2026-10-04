"""Frozen CT0-5 decision, grounding, and contact Draft contracts.

Draft values contain text and provenance only. They confer no send, Action,
Telegram, persistence, or delivery authority.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
import hashlib
import re
from typing import Mapping

from contact_candidate_model import _require_ref, _require_timestamp
from contact_intent_model import ContactIntent


CONTACT_DECISION_RECEIPT_SCHEMA = "chiyo.contact.decision_receipt.v1"
CONTACT_GROUNDING_SOURCE_SCHEMA = "chiyo.contact.grounding_source.v1"
CONTACT_DRAFT_RENDER_SCHEMA = "chiyo.contact.draft_render.v1"
CONTACT_MESSAGE_DRAFT_SCHEMA = "chiyo.contact.message_draft.v1"
DRAFT_POLICY_VERSION = "ct0.contact_draft_grounding.v1"

DECISION_CONTACT_SELECTED = "CONTACT_SELECTED"
DECISION_NO_ACTION = "NO_ACTION"
DECISION_DEFER = "DEFER"
CONTACT_DECISION_OUTCOMES = frozenset(
    {DECISION_CONTACT_SELECTED, DECISION_NO_ACTION, DECISION_DEFER}
)
DRAFT_STATUS_FROZEN = "FROZEN"

_REF_PREFIX_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,63}$")


class ContactDraftContractError(ValueError):
    """Base error for invalid isolated Draft / grounding contracts."""


class InvalidContactDecisionReceiptError(ContactDraftContractError):
    """A supplied decision receipt is not a committed canonical decision projection."""


class InvalidGroundingSourceError(ContactDraftContractError):
    """Grounding source data is malformed or not hash-bound."""


class InvalidContactDraftError(ContactDraftContractError):
    """A Draft violates provenance, frozen-content, or identity constraints."""


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _aware_timestamp(value: object, name: str) -> str:
    result = _require_timestamp(value, name)
    parsed = datetime.fromisoformat(result.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ContactDraftContractError(f"{name} must include a timezone")
    return result


def derive_draft_identity(*, intent_ref: str, decision_ref: str, content_hash: str) -> tuple[str, str]:
    _require_ref(intent_ref, "intent_ref")
    _require_ref(decision_ref, "decision_ref")
    if not isinstance(content_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", content_hash):
        raise InvalidContactDraftError("content_hash must be a lowercase SHA-256 hex digest")
    material = f"{intent_ref}\x00{decision_ref}\x00{content_hash}"
    digest = sha256_text(material)
    return f"cdraft:{digest[:32]}", f"cdraft-idem:{digest}"


@dataclass(frozen=True, slots=True)
class ContactDecisionReceipt:
    """Detached projection of an already committed AG-1 Decision."""

    schema_version: str
    committed: bool
    intent_ref: str
    candidate_ref: str
    decision_ref: str
    outcome: str
    selected_candidate_ref: str | None
    committed_at: str

    def __post_init__(self) -> None:
        if self.schema_version != CONTACT_DECISION_RECEIPT_SCHEMA:
            raise InvalidContactDecisionReceiptError("unsupported committed decision receipt schema")
        if not isinstance(self.committed, bool):
            raise InvalidContactDecisionReceiptError("committed must be bool")
        for name in ("intent_ref", "candidate_ref", "decision_ref"):
            try:
                _require_ref(getattr(self, name), name)
            except ValueError as exc:
                raise InvalidContactDecisionReceiptError(f"{name} is invalid") from exc
        if not self.intent_ref.startswith("cintent:") or not self.candidate_ref.startswith("ccand:") or not self.decision_ref.startswith("dec:"):
            raise InvalidContactDecisionReceiptError("decision receipt refs use unsupported namespaces")
        if not isinstance(self.outcome, str) or self.outcome not in CONTACT_DECISION_OUTCOMES:
            raise InvalidContactDecisionReceiptError("unsupported contact decision outcome")
        if self.outcome == DECISION_CONTACT_SELECTED:
            if self.selected_candidate_ref != self.candidate_ref:
                raise InvalidContactDecisionReceiptError("CONTACT_SELECTED must select this Intent's Candidate")
        elif self.selected_candidate_ref is not None:
            raise InvalidContactDecisionReceiptError("NO_ACTION/DEFER must not select a Candidate")
        _aware_timestamp(self.committed_at, "committed_at")

    @classmethod
    def from_mapping(cls, raw: object) -> "ContactDecisionReceipt":
        if not isinstance(raw, Mapping) or set(raw) != set(cls.__dataclass_fields__):
            raise InvalidContactDecisionReceiptError("decision receipt fields do not match the frozen schema")
        try:
            return cls(**dict(raw))
        except (TypeError, ValueError) as exc:
            if isinstance(exc, InvalidContactDecisionReceiptError):
                raise
            raise InvalidContactDecisionReceiptError("decision receipt failed validation") from exc


@dataclass(frozen=True, slots=True)
class ContactGroundingSource:
    """Hash-bound source projection supplied by an explicitly injected resolver."""

    schema_version: str
    source_ref: str
    canonical_text: str
    content_hash: str

    def __post_init__(self) -> None:
        if self.schema_version != CONTACT_GROUNDING_SOURCE_SCHEMA:
            raise InvalidGroundingSourceError("unsupported ContactGroundingSource schema")
        try:
            _require_ref(self.source_ref, "source_ref")
        except ValueError as exc:
            raise InvalidGroundingSourceError("source_ref is invalid") from exc
        if not isinstance(self.canonical_text, str) or not self.canonical_text or len(self.canonical_text) > 65536:
            raise InvalidGroundingSourceError("canonical_text must be non-empty and bounded")
        if not isinstance(self.content_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", self.content_hash):
            raise InvalidGroundingSourceError("grounding content_hash is invalid")
        if sha256_text(self.canonical_text) != self.content_hash:
            raise InvalidGroundingSourceError("grounding source content hash mismatch")

    @classmethod
    def create(cls, *, source_ref: str, canonical_text: str) -> "ContactGroundingSource":
        return cls(
            schema_version=CONTACT_GROUNDING_SOURCE_SCHEMA,
            source_ref=source_ref,
            canonical_text=canonical_text,
            content_hash=sha256_text(canonical_text),
        )

    @classmethod
    def from_mapping(cls, raw: object) -> "ContactGroundingSource":
        if not isinstance(raw, Mapping) or set(raw) != set(cls.__dataclass_fields__):
            raise InvalidGroundingSourceError("grounding source fields do not match the frozen schema")
        try:
            return cls(**dict(raw))
        except (TypeError, ValueError) as exc:
            if isinstance(exc, InvalidGroundingSourceError):
                raise
            raise InvalidGroundingSourceError("grounding source failed validation") from exc


@dataclass(frozen=True, slots=True)
class GroundedExcerpt:
    source_ref: str
    exact_text: str

    def __post_init__(self) -> None:
        try:
            _require_ref(self.source_ref, "source_ref")
        except ValueError as exc:
            raise InvalidGroundingSourceError("excerpt source_ref is invalid") from exc
        if not isinstance(self.exact_text, str) or not self.exact_text or len(self.exact_text) > 4000:
            raise InvalidGroundingSourceError("excerpt exact_text must be non-empty and bounded")


@dataclass(frozen=True, slots=True)
class ContactDraftRender:
    """Expression output is restricted to ordered, verbatim source excerpts."""

    schema_version: str
    excerpts: tuple[GroundedExcerpt, ...]

    def __post_init__(self) -> None:
        if self.schema_version != CONTACT_DRAFT_RENDER_SCHEMA:
            raise InvalidContactDraftError("unsupported contact Draft render schema")
        if not isinstance(self.excerpts, tuple) or not self.excerpts or len(self.excerpts) > 64:
            raise InvalidContactDraftError("render excerpts must be a non-empty immutable tuple")
        if any(not isinstance(excerpt, GroundedExcerpt) for excerpt in self.excerpts):
            raise InvalidContactDraftError("render contains a non-GroundedExcerpt value")

    @classmethod
    def from_mapping(cls, raw: object) -> "ContactDraftRender":
        if not isinstance(raw, Mapping) or set(raw) != set(cls.__dataclass_fields__):
            raise InvalidContactDraftError("Draft render fields do not match the frozen schema")
        excerpts_raw = raw["excerpts"]
        if not isinstance(excerpts_raw, (tuple, list)):
            raise InvalidContactDraftError("render excerpts must be a list/tuple")
        excerpts: list[GroundedExcerpt] = []
        for item in excerpts_raw:
            if isinstance(item, GroundedExcerpt):
                excerpts.append(GroundedExcerpt(**asdict(item)))
            elif isinstance(item, Mapping) and set(item) == set(GroundedExcerpt.__dataclass_fields__):
                excerpts.append(GroundedExcerpt(**dict(item)))
            else:
                raise InvalidContactDraftError("render excerpt does not match its frozen schema")
        try:
            return cls(schema_version=raw["schema_version"], excerpts=tuple(excerpts))
        except (TypeError, ValueError) as exc:
            if isinstance(exc, InvalidContactDraftError):
                raise
            raise InvalidContactDraftError("Draft render failed validation") from exc


@dataclass(frozen=True, slots=True)
class ContactMessageDraft:
    """Frozen text projection, not an authorization or Message Action."""

    draft_id: str
    schema_version: str
    status: str
    intent_ref: str
    candidate_ref: str
    decision_ref: str
    recipient_ref: str
    channel_scope: tuple[str, ...]
    source_refs: tuple[str, ...]
    content: str
    content_hash: str
    idempotency_key: str
    created_at: str
    policy_version: str

    def __post_init__(self) -> None:
        if self.schema_version != CONTACT_MESSAGE_DRAFT_SCHEMA or self.status != DRAFT_STATUS_FROZEN:
            raise InvalidContactDraftError("unsupported or non-frozen ContactMessageDraft")
        for name, value in (
            ("draft_id", self.draft_id), ("intent_ref", self.intent_ref),
            ("candidate_ref", self.candidate_ref), ("decision_ref", self.decision_ref),
            ("recipient_ref", self.recipient_ref), ("idempotency_key", self.idempotency_key),
            ("policy_version", self.policy_version),
        ):
            try:
                _require_ref(value, name)
            except ValueError as exc:
                raise InvalidContactDraftError(f"{name} is invalid") from exc
        if not self.intent_ref.startswith("cintent:") or not self.candidate_ref.startswith("ccand:") or not self.decision_ref.startswith("dec:"):
            raise InvalidContactDraftError("Draft references use unsupported namespaces")
        for name, refs in (("channel_scope", self.channel_scope), ("source_refs", self.source_refs)):
            if not isinstance(refs, tuple) or not refs or len(refs) > 256 or len(set(refs)) != len(refs):
                raise InvalidContactDraftError(f"{name} must be a non-empty unique immutable tuple")
            for ref in refs:
                try:
                    _require_ref(ref, name)
                except ValueError as exc:
                    raise InvalidContactDraftError(f"{name} contains an invalid reference") from exc
        if not isinstance(self.content, str) or not self.content or len(self.content) > 12000:
            raise InvalidContactDraftError("Draft content must be non-empty and bounded")
        if sha256_text(self.content) != self.content_hash:
            raise InvalidContactDraftError("Draft content hash mismatch")
        expected_id, expected_idempotency = derive_draft_identity(
            intent_ref=self.intent_ref,
            decision_ref=self.decision_ref,
            content_hash=self.content_hash,
        )
        if self.draft_id != expected_id or self.idempotency_key != expected_idempotency:
            raise InvalidContactDraftError("Draft identity is not derived from frozen content and owners")
        _aware_timestamp(self.created_at, "created_at")

    @classmethod
    def from_mapping(cls, raw: object) -> "ContactMessageDraft":
        if not isinstance(raw, Mapping) or set(raw) != set(cls.__dataclass_fields__):
            raise InvalidContactDraftError("ContactMessageDraft fields do not match the frozen schema")
        data = dict(raw)
        for name in ("channel_scope", "source_refs"):
            value = data[name]
            if not isinstance(value, (list, tuple)):
                raise InvalidContactDraftError(f"{name} must be a list/tuple")
            data[name] = tuple(value)
        try:
            return cls(**data)
        except (TypeError, ValueError) as exc:
            if isinstance(exc, InvalidContactDraftError):
                raise
            raise InvalidContactDraftError("ContactMessageDraft mapping failed validation") from exc


def _revalidate_intent(value: object) -> ContactIntent:
    if not isinstance(value, ContactIntent):
        raise InvalidContactDraftError("Draft requires a typed ContactIntent")
    try:
        return ContactIntent.from_mapping(asdict(value))
    except Exception as exc:
        raise InvalidContactDraftError("ContactIntent failed contract revalidation") from exc


def _revalidate_decision(value: object) -> ContactDecisionReceipt:
    if not isinstance(value, ContactDecisionReceipt):
        raise InvalidContactDraftError("Draft requires a typed Decision receipt")
    try:
        return ContactDecisionReceipt.from_mapping(asdict(value))
    except Exception as exc:
        raise InvalidContactDraftError("Decision receipt failed contract revalidation") from exc


def build_frozen_draft(
    *,
    intent: ContactIntent,
    decision: ContactDecisionReceipt,
    grounding_sources: tuple[ContactGroundingSource, ...],
    render: ContactDraftRender,
    created_at: str,
    policy_version: str = DRAFT_POLICY_VERSION,
) -> ContactMessageDraft:
    """Validate owner binding and exact quotation grounding, then freeze Draft."""
    intent = _revalidate_intent(intent)
    decision = _revalidate_decision(decision)
    if decision.intent_ref != intent.intent_id or decision.candidate_ref != intent.candidate_ref:
        raise InvalidContactDraftError("committed Decision does not belong to this ContactIntent/Candidate")
    if not decision.committed or decision.outcome != DECISION_CONTACT_SELECTED:
        raise InvalidContactDraftError("Draft requires a committed CONTACT_SELECTED Decision")
    if decision.selected_candidate_ref != intent.candidate_ref:
        raise InvalidContactDraftError("committed Decision selected a different Candidate")
    if intent.status != "SELECTED":
        raise InvalidContactDraftError("ContactIntent must be SELECTED before Draft generation")
    if not isinstance(grounding_sources, tuple) or not grounding_sources:
        raise InvalidGroundingSourceError("grounding sources must be a non-empty immutable tuple")
    if any(not isinstance(source, ContactGroundingSource) for source in grounding_sources):
        raise InvalidGroundingSourceError("grounding sources contain an invalid value")
    if not isinstance(render, ContactDraftRender):
        raise InvalidContactDraftError("renderer must return a ContactDraftRender")

    allowed_sources = set(intent.source_refs) & set(intent.content_scope_refs)
    source_by_ref: dict[str, ContactGroundingSource] = {}
    for source in grounding_sources:
        try:
            source = ContactGroundingSource.from_mapping(asdict(source))
        except Exception as exc:
            raise InvalidGroundingSourceError("grounding source failed contract revalidation") from exc
        if source.source_ref not in allowed_sources:
            raise InvalidGroundingSourceError("grounding source is outside ContactIntent content scope")
        if source.source_ref in source_by_ref:
            raise InvalidGroundingSourceError("duplicate grounding source_ref")
        source_by_ref[source.source_ref] = source

    try:
        render = ContactDraftRender.from_mapping(asdict(render))
    except Exception as exc:
        raise InvalidContactDraftError("renderer output failed contract revalidation") from exc
    selected_source_refs: list[str] = []
    exact_segments: list[str] = []
    for excerpt in render.excerpts:
        source = source_by_ref.get(excerpt.source_ref)
        if source is None:
            raise InvalidGroundingSourceError("render cites a source not supplied to the grounding guard")
        if excerpt.source_ref in selected_source_refs:
            raise InvalidGroundingSourceError("one source may contribute only one continuous excerpt per Draft")
        if excerpt.exact_text not in source.canonical_text:
            raise InvalidGroundingSourceError("render contains text not present verbatim in its cited source")
        exact_segments.append(excerpt.exact_text)
        selected_source_refs.append(excerpt.source_ref)
    content = " ".join(exact_segments)
    content_hash = sha256_text(content)
    draft_id, idempotency_key = derive_draft_identity(
        intent_ref=intent.intent_id,
        decision_ref=decision.decision_ref,
        content_hash=content_hash,
    )
    return ContactMessageDraft(
        draft_id=draft_id,
        schema_version=CONTACT_MESSAGE_DRAFT_SCHEMA,
        status=DRAFT_STATUS_FROZEN,
        intent_ref=intent.intent_id,
        candidate_ref=intent.candidate_ref,
        decision_ref=decision.decision_ref,
        recipient_ref=intent.recipient_ref,
        channel_scope=intent.channel_scope,
        source_refs=tuple(selected_source_refs),
        content=content,
        content_hash=content_hash,
        idempotency_key=idempotency_key,
        created_at=_aware_timestamp(created_at, "created_at"),
        policy_version=policy_version,
    )


__all__ = [
    "CONTACT_DECISION_RECEIPT_SCHEMA", "CONTACT_DRAFT_RENDER_SCHEMA", "CONTACT_GROUNDING_SOURCE_SCHEMA",
    "CONTACT_MESSAGE_DRAFT_SCHEMA", "CONTACT_DECISION_OUTCOMES", "DECISION_CONTACT_SELECTED",
    "DECISION_DEFER", "DECISION_NO_ACTION", "DRAFT_POLICY_VERSION", "DRAFT_STATUS_FROZEN",
    "ContactDecisionReceipt", "ContactDraftContractError", "ContactDraftRender", "ContactGroundingSource",
    "ContactMessageDraft", "GroundedExcerpt", "InvalidContactDecisionReceiptError",
    "InvalidContactDraftError", "InvalidGroundingSourceError", "build_frozen_draft",
    "derive_draft_identity", "sha256_text",
]
