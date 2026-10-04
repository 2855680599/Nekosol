"""Frozen CT0-6 Message Action Proposal *request* contract.

This module builds a lineage-complete, hash-bound request that a canonical AR-0
owner may use to create a `MESSAGE_ACTION` `ActionProposal`. It does not create
an AR-0 record, mint an Action identity, resolve a canonical source, or hold
any submit / send / delivery / persistence / Telegram / network capability.

Ownership boundaries:
  * AR-0 remains the only Message Action lifecycle owner.
  * The CT0-6 bridge only *asks* an injected AR-0 port to propose.
  * `request_id` / `payload_ref` / `correlation_id` are CT0-6 request refs and
    are deliberately NOT AR-0 `proposal_id` / `action_id` values.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
import hashlib
import json
import re
from typing import Mapping

from contact_candidate_model import GLOBAL_SUBJECT_ID, _require_ref, _require_timestamp, unique_refs
from contact_draft_model import (
    DECISION_CONTACT_SELECTED,
    ContactDecisionReceipt,
    ContactMessageDraft,
)
from contact_intent_model import STATUS_SELECTED, ContactIntent


CONTACT_MESSAGE_ACTION_REQUEST_SCHEMA = "chiyo.contact.message_action_request.v1"
CONTACT_MESSAGE_ACTION_PAYLOAD_SCHEMA = "chiyo.contact.message_action_payload.v1"
CONTACT_MESSAGE_ACTION_POLICY_VERSION = "ct0.contact_message_action_bridge.v1"

ACTION_KIND_MESSAGE_ACTION = "MESSAGE_ACTION"
REQUEST_STATUS_READY_TO_PROPOSE = "READY_TO_PROPOSE"

SANDBOX_CHANNEL_PREFIX = "channel:"
PRODUCTION_CHANNEL_DENYLIST = frozenset({"telegram_prod", "qq_prod"})

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_AWARE_Z_RE = re.compile(r"Z$")


class ContactMessageActionContractError(ValueError):
    """Base error for invalid isolated Message Action request contracts."""


class InvalidContactMessageActionRequestError(ContactMessageActionContractError):
    """A request, gate, or payload violates the frozen CT0-6 contract."""


class InvalidMessageActionSourceBindingError(ContactMessageActionContractError):
    """The Draft is not a committed, selected, grounded projection of its owners."""


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _aware_timestamp(value: object, name: str) -> str:
    result = _require_timestamp(value, name)
    parsed = datetime.fromisoformat(result.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ContactMessageActionContractError(f"{name} must include a timezone")
    return result


def _sha256_hex(value: object, name: str) -> str:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise ContactMessageActionContractError(f"{name} must be a lowercase SHA-256 hex digest")
    return value


def _ref_tuple(value: object, name: str, *, allow_empty: bool = False) -> tuple[str, ...]:
    if not isinstance(value, tuple) or (not allow_empty and not value):
        raise ContactMessageActionContractError(f"{name} must be a non-empty immutable tuple")
    if len(value) > 256 or len(set(value)) != len(value):
        raise ContactMessageActionContractError(f"{name} exceeds bounds or contains duplicates")
    for ref in value:
        try:
            _require_ref(ref, name)
        except ValueError as exc:
            raise ContactMessageActionContractError(f"{name} contains an invalid reference") from exc
    return value


def _sandbox_channel(value: object, name: str = "channel") -> str:
    try:
        _require_ref(value, name)
    except ValueError as exc:
        raise InvalidContactMessageActionRequestError(f"{name} is invalid") from exc
    if value in PRODUCTION_CHANNEL_DENYLIST:
        raise InvalidContactMessageActionRequestError(f"{name} is a production channel and is refused")
    if not value.startswith(SANDBOX_CHANNEL_PREFIX):
        raise InvalidContactMessageActionRequestError(
            f"{name} must use the sandbox {SANDBOX_CHANNEL_PREFIX!r} namespace"
        )
    return value


@dataclass(frozen=True, slots=True)
class ContactActionGate:
    """Detached kill-switch snapshot.

    Proposal construction is permitted with `action_execution_enabled=False`
    (the isolated CT0-1 contract: proposals and `would-submit` records are legal).
    Crossing the submission fence is *not* part of CT0-6 at all: the bridge has
    no submit capability, and `would_submit` only reports what an authoritative
    submit gate would decide. `PROACTIVE_ENABLED` is the outer hard gate.
    """

    life_runtime_enabled: bool = False
    agency_enabled: bool = False
    action_execution_enabled: bool = False
    proactive_enabled: bool = False

    def __post_init__(self) -> None:
        for name in (
            "life_runtime_enabled",
            "agency_enabled",
            "action_execution_enabled",
            "proactive_enabled",
        ):
            if not isinstance(getattr(self, name), bool):
                raise ContactMessageActionContractError(f"gate.{name} must be a bool")

    @property
    def would_submit(self) -> bool:
        """True only when both the action and outer proactive gates are enabled."""
        return self.action_execution_enabled and self.proactive_enabled


@dataclass(frozen=True, slots=True)
class MessageActionRequestIdentity:
    """Deterministic CT0-6 identity derived only from frozen Draft content."""

    request_id: str
    idempotency_key: str
    payload_ref: str
    correlation_id: str


def derive_message_action_request_identity(
    *,
    draft_ref: str,
    content_hash: str,
    recipient_ref: str,
    channel: str,
) -> MessageActionRequestIdentity:
    """Return the stable logical identity for one (Draft, recipient, channel).

    Because every input is frozen (Draft id, content hash, recipient, channel),
    a repeated Draft always maps to the same logical Action identity. This is
    the CT0-6 dedup authority; the AR-0 owner receives it as its own
    `idempotency_key`, so a second bridge instance still cannot create a second
    logical proposal.
    """
    try:
        _require_ref(draft_ref, "draft_ref")
        _require_ref(recipient_ref, "recipient_ref")
    except ValueError as exc:
        raise ContactMessageActionContractError("identity inputs contain an invalid reference") from exc
    if not draft_ref.startswith("cdraft:"):
        raise ContactMessageActionContractError("draft_ref must use the CT0-5 cdraft namespace")
    _sha256_hex(content_hash, "content_hash")
    _sandbox_channel(channel)
    material = "\x00".join((draft_ref, content_hash, recipient_ref, channel, ACTION_KIND_MESSAGE_ACTION))
    digest = sha256_text(material)
    return MessageActionRequestIdentity(
        request_id=f"mreq:{digest[:32]}",
        idempotency_key=f"ar0_act:{digest}",
        payload_ref=f"pay:{digest[:32]}",
        correlation_id=f"corr:{digest[:32]}",
    )


def _detached_intent(value: object) -> ContactIntent:
    if not isinstance(value, ContactIntent):
        raise InvalidMessageActionSourceBindingError("request requires a typed ContactIntent")
    try:
        return ContactIntent.from_mapping(value.to_dict())
    except Exception as exc:
        raise InvalidMessageActionSourceBindingError("ContactIntent failed contract revalidation") from exc


def _detached_decision(value: object) -> ContactDecisionReceipt:
    if not isinstance(value, ContactDecisionReceipt):
        raise InvalidMessageActionSourceBindingError("request requires a typed committed Decision receipt")
    try:
        return ContactDecisionReceipt.from_mapping(asdict(value))
    except Exception as exc:
        raise InvalidMessageActionSourceBindingError("Decision receipt failed contract revalidation") from exc


def _detached_draft(value: object) -> ContactMessageDraft:
    if not isinstance(value, ContactMessageDraft):
        raise InvalidMessageActionSourceBindingError("request requires a typed frozen ContactMessageDraft")
    try:
        return ContactMessageDraft.from_mapping(asdict(value))
    except Exception as exc:
        raise InvalidMessageActionSourceBindingError("ContactMessageDraft failed contract revalidation") from exc


@dataclass(frozen=True, slots=True)
class ContactMessageActionRequest:
    """Frozen request for an AR-0 `MESSAGE_ACTION` proposal. Not an Action."""

    request_id: str
    schema_version: str
    status: str
    subject_id: str
    action_kind: str
    intent_ref: str
    candidate_ref: str
    decision_ref: str
    draft_ref: str
    recipient_ref: str
    channel: str
    target_ref: str
    payload_ref: str
    payload_json: str
    content_hash: str
    idempotency_key: str
    correlation_id: str
    authorization_ref: str
    causal_parent_refs: tuple[str, ...]
    source_refs: tuple[str, ...]
    would_submit: bool
    created_at: str
    policy_version: str

    def __post_init__(self) -> None:
        if self.schema_version != CONTACT_MESSAGE_ACTION_REQUEST_SCHEMA:
            raise InvalidContactMessageActionRequestError("unsupported Message Action request schema")
        if self.status != REQUEST_STATUS_READY_TO_PROPOSE:
            raise InvalidContactMessageActionRequestError("unsupported Message Action request status")
        if self.subject_id != GLOBAL_SUBJECT_ID:
            raise InvalidContactMessageActionRequestError("request subject must be chiyo.global")
        if self.action_kind != ACTION_KIND_MESSAGE_ACTION:
            raise InvalidContactMessageActionRequestError("request action_kind must be MESSAGE_ACTION")

        for name, value in (
            ("request_id", self.request_id),
            ("intent_ref", self.intent_ref),
            ("candidate_ref", self.candidate_ref),
            ("decision_ref", self.decision_ref),
            ("draft_ref", self.draft_ref),
            ("recipient_ref", self.recipient_ref),
            ("target_ref", self.target_ref),
            ("payload_ref", self.payload_ref),
            ("idempotency_key", self.idempotency_key),
            ("correlation_id", self.correlation_id),
            ("authorization_ref", self.authorization_ref),
            ("policy_version", self.policy_version),
        ):
            try:
                _require_ref(value, name)
            except ValueError as exc:
                raise InvalidContactMessageActionRequestError(f"{name} is invalid") from exc
        for prefix, name, value in (
            ("mreq:", "request_id", self.request_id),
            ("cintent:", "intent_ref", self.intent_ref),
            ("ccand:", "candidate_ref", self.candidate_ref),
            ("dec:", "decision_ref", self.decision_ref),
            ("cdraft:", "draft_ref", self.draft_ref),
            ("cdraft:", "authorization_ref", self.authorization_ref),
            ("corr:", "correlation_id", self.correlation_id),
        ):
            if not value.startswith(prefix):
                raise InvalidContactMessageActionRequestError(f"{name} must use the {prefix!r} namespace")
        if not self.payload_ref.startswith("pay:"):
            raise InvalidContactMessageActionRequestError("payload_ref must use the pay: namespace")
        if not self.idempotency_key.startswith("ar0_act:"):
            raise InvalidContactMessageActionRequestError("idempotency_key must use the ar0_act: namespace")
        self._validate_idempotency_tail()
        _sandbox_channel(self.channel)
        if self.target_ref != self.recipient_ref:
            raise InvalidContactMessageActionRequestError("message target_ref must be the frozen recipient_ref")
        _ref_tuple(self.causal_parent_refs, "causal_parent_refs")
        _ref_tuple(self.source_refs, "source_refs")
        if not isinstance(self.would_submit, bool):
            raise InvalidContactMessageActionRequestError("would_submit must be a bool")
        _sha256_hex(self.content_hash, "content_hash")

        payload = self.payload_data
        if payload.get("schema_version") != CONTACT_MESSAGE_ACTION_PAYLOAD_SCHEMA:
            raise InvalidContactMessageActionRequestError("payload schema_version is invalid")
        if payload.get("channel") != self.channel:
            raise InvalidContactMessageActionRequestError("payload channel differs from the request channel")
        if payload.get("draft_ref") != self.draft_ref:
            raise InvalidContactMessageActionRequestError("payload draft_ref differs from the request draft_ref")
        if payload.get("content_hash") != self.content_hash:
            raise InvalidContactMessageActionRequestError("payload content_hash differs from the request")
        if payload.get("correlation_id") != self.correlation_id:
            raise InvalidContactMessageActionRequestError("payload correlation_id differs from the request")
        if payload.get("proactive") is not True:
            raise InvalidContactMessageActionRequestError("a contact message request must declare proactive intent")
        text = payload.get("text")
        if not isinstance(text, str) or not text:
            raise InvalidContactMessageActionRequestError("payload text must be a non-empty string")
        if sha256_text(text) != self.content_hash:
            raise InvalidContactMessageActionRequestError("payload text does not match the frozen content hash")
        # `correlation_id` is a request-scoped correlation ref (carried in the
        # payload), not a lineage parent; lineage is validated separately above.
        identity = derive_message_action_request_identity(
            draft_ref=self.draft_ref,
            content_hash=self.content_hash,
            recipient_ref=self.recipient_ref,
            channel=self.channel,
        )
        if (
            self.request_id != identity.request_id
            or self.idempotency_key != identity.idempotency_key
            or self.payload_ref != identity.payload_ref
            or self.correlation_id != identity.correlation_id
        ):
            raise InvalidContactMessageActionRequestError(
                "request identity is not derived from frozen Draft content, recipient and channel"
            )
        _aware_timestamp(self.created_at, "created_at")

    def _validate_idempotency_tail(self) -> None:
        tail = self.idempotency_key[len("ar0_act:") :]
        if not _SHA256_RE.fullmatch(tail):
            raise InvalidContactMessageActionRequestError("idempotency_key tail must be a SHA-256 hex digest")

    @property
    def payload_data(self) -> dict[str, object]:
        try:
            payload = json.loads(self.payload_json)
        except (TypeError, json.JSONDecodeError) as exc:
            raise InvalidContactMessageActionRequestError("payload_json is not valid JSON") from exc
        if not isinstance(payload, dict):
            raise InvalidContactMessageActionRequestError("payload_json must encode a mapping")
        if canonical_json(payload) != self.payload_json:
            raise InvalidContactMessageActionRequestError("payload_json is not in canonical form")
        return payload

    @classmethod
    def from_mapping(cls, raw: object) -> "ContactMessageActionRequest":
        if not isinstance(raw, Mapping) or set(raw) != set(cls.__dataclass_fields__):
            raise InvalidContactMessageActionRequestError("request fields do not match the frozen schema")
        data = dict(raw)
        for name in ("causal_parent_refs", "source_refs"):
            value = data[name]
            if not isinstance(value, (tuple, list)):
                raise InvalidContactMessageActionRequestError(f"{name} must be a list/tuple")
            data[name] = tuple(value)
        try:
            return cls(**data)
        except (TypeError, ValueError) as exc:
            if isinstance(exc, InvalidContactMessageActionRequestError):
                raise
            raise InvalidContactMessageActionRequestError("request mapping failed validation") from exc

    def to_dict(self) -> dict[str, object]:
        data = asdict(self)
        for name in ("causal_parent_refs", "source_refs"):
            data[name] = list(data[name])
        return data


def build_message_action_request(
    *,
    intent: object,
    decision: object,
    draft: object,
    gate: ContactActionGate,
    channel: str,
    created_at: str,
    policy_version: str = CONTACT_MESSAGE_ACTION_POLICY_VERSION,
) -> ContactMessageActionRequest:
    """Bind a frozen Draft to its committed owners and freeze a proposal request.

    Fail-closed: any missing commitment, owner mismatch, non-SELECTED Intent,
    channel outside the Draft's frozen scope, or production channel is refused
    here, before any AR-0 port is reachable.
    """
    if not isinstance(gate, ContactActionGate):
        raise InvalidContactMessageActionRequestError("gate must be a ContactActionGate")
    # Namespace/denylist validation happens before any owner binding so a
    # production channel is refused with a contract error, never merely as an
    # out-of-scope sandbox channel.
    _sandbox_channel(channel)
    intent = _detached_intent(intent)
    decision = _detached_decision(decision)
    draft = _detached_draft(draft)

    if draft.intent_ref != intent.intent_id or draft.candidate_ref != intent.candidate_ref:
        raise InvalidMessageActionSourceBindingError("Draft does not belong to this ContactIntent/Candidate")
    if draft.decision_ref != decision.decision_ref:
        raise InvalidMessageActionSourceBindingError("Draft was not authorized by this Decision receipt")
    if not decision.committed or decision.outcome != DECISION_CONTACT_SELECTED:
        raise InvalidMessageActionSourceBindingError("request requires a committed CONTACT_SELECTED Decision")
    if decision.intent_ref != intent.intent_id or decision.candidate_ref != intent.candidate_ref:
        raise InvalidMessageActionSourceBindingError("committed Decision belongs to another Intent/Candidate")
    if decision.selected_candidate_ref != intent.candidate_ref:
        raise InvalidMessageActionSourceBindingError("committed Decision selected a different Candidate")
    if intent.status != STATUS_SELECTED:
        raise InvalidMessageActionSourceBindingError("ContactIntent must be SELECTED before requesting an Action")
    if draft.recipient_ref != intent.recipient_ref:
        raise InvalidMessageActionSourceBindingError("Draft recipient differs from the ContactIntent recipient")
    if draft.channel_scope != intent.channel_scope:
        raise InvalidMessageActionSourceBindingError("Draft channel scope differs from the ContactIntent scope")
    if channel not in draft.channel_scope:
        raise InvalidMessageActionSourceBindingError("channel is not in the frozen Draft channel scope")

    identity = derive_message_action_request_identity(
        draft_ref=draft.draft_id,
        content_hash=draft.content_hash,
        recipient_ref=draft.recipient_ref,
        channel=channel,
    )
    payload = {
        "schema_version": CONTACT_MESSAGE_ACTION_PAYLOAD_SCHEMA,
        "channel": channel,
        "text": draft.content,
        "proactive": True,
        "draft_ref": draft.draft_id,
        "content_hash": draft.content_hash,
        "intent_ref": intent.intent_id,
        "candidate_ref": intent.candidate_ref,
        "decision_ref": decision.decision_ref,
        "correlation_id": identity.correlation_id,
    }
    source_refs = unique_refs(
        (draft.draft_id,),
        (decision.decision_ref,),
        (intent.intent_id,),
        (intent.candidate_ref,),
        draft.source_refs,
    )
    causal_parent_refs = unique_refs(
        (intent.candidate_ref,),
        (intent.intent_id,),
        (decision.decision_ref,),
        (draft.draft_id,),
    )
    return ContactMessageActionRequest(
        request_id=identity.request_id,
        schema_version=CONTACT_MESSAGE_ACTION_REQUEST_SCHEMA,
        status=REQUEST_STATUS_READY_TO_PROPOSE,
        subject_id=GLOBAL_SUBJECT_ID,
        action_kind=ACTION_KIND_MESSAGE_ACTION,
        intent_ref=intent.intent_id,
        candidate_ref=intent.candidate_ref,
        decision_ref=decision.decision_ref,
        draft_ref=draft.draft_id,
        recipient_ref=draft.recipient_ref,
        channel=channel,
        target_ref=draft.recipient_ref,
        payload_ref=identity.payload_ref,
        payload_json=canonical_json(payload),
        content_hash=draft.content_hash,
        idempotency_key=identity.idempotency_key,
        correlation_id=identity.correlation_id,
        authorization_ref=draft.draft_id,
        causal_parent_refs=causal_parent_refs,
        source_refs=source_refs,
        would_submit=gate.would_submit,
        created_at=_aware_timestamp(created_at, "created_at"),
        policy_version=policy_version,
    )


__all__ = [
    "ACTION_KIND_MESSAGE_ACTION", "CONTACT_MESSAGE_ACTION_PAYLOAD_SCHEMA",
    "CONTACT_MESSAGE_ACTION_POLICY_VERSION", "CONTACT_MESSAGE_ACTION_REQUEST_SCHEMA",
    "ContactActionGate", "ContactMessageActionContractError", "ContactMessageActionRequest",
    "InvalidContactMessageActionRequestError", "InvalidMessageActionSourceBindingError",
    "MessageActionRequestIdentity", "PRODUCTION_CHANNEL_DENYLIST", "REQUEST_STATUS_READY_TO_PROPOSE",
    "SANDBOX_CHANNEL_PREFIX", "build_message_action_request", "canonical_json",
    "derive_message_action_request_identity", "sha256_text",
]
