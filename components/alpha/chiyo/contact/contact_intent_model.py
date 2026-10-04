"""Pure CT0-3 ContactIntent schema and lifecycle transition contract."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json
from typing import Mapping

from contact_candidate_model import (
    CANDIDATE_SHARE_EXPERIENCE,
    CONTACT_CANDIDATE_SCHEMA,
    GLOBAL_SUBJECT_ID,
    ContactCandidate,
    ContactContractError,
    _require_ref,
    _require_timestamp,
)


CONTACT_INTENT_SCHEMA = "chiyo.contact.intent.v1"
CONTACT_INTENT_POLICY_VERSION = "ct0.contact_intent_policy.v1"

STATUS_OPEN = "OPEN"
STATUS_DEFERRED = "DEFERRED"
STATUS_SELECTED = "SELECTED"
STATUS_CONSUMED = "CONSUMED"
STATUS_EXPIRED = "EXPIRED"
STATUS_CANCELLED = "CANCELLED"
STATUS_STALE = "STALE"
CONTACT_INTENT_STATUSES = frozenset(
    {
        STATUS_OPEN,
        STATUS_DEFERRED,
        STATUS_SELECTED,
        STATUS_CONSUMED,
        STATUS_EXPIRED,
        STATUS_CANCELLED,
        STATUS_STALE,
    }
)
TERMINAL_INTENT_STATUSES = frozenset(
    {STATUS_CONSUMED, STATUS_EXPIRED, STATUS_CANCELLED, STATUS_STALE}
)
ALLOWED_INTENT_TRANSITIONS = {
    STATUS_OPEN: frozenset({STATUS_DEFERRED, STATUS_SELECTED, STATUS_EXPIRED, STATUS_CANCELLED, STATUS_STALE}),
    STATUS_DEFERRED: frozenset({STATUS_OPEN, STATUS_SELECTED, STATUS_EXPIRED, STATUS_CANCELLED, STATUS_STALE}),
    STATUS_SELECTED: frozenset({STATUS_DEFERRED, STATUS_CONSUMED, STATUS_EXPIRED, STATUS_CANCELLED, STATUS_STALE}),
    STATUS_CONSUMED: frozenset(),
    STATUS_EXPIRED: frozenset(),
    STATUS_CANCELLED: frozenset(),
    STATUS_STALE: frozenset(),
}


class ContactIntentError(ContactContractError):
    """Base error for invalid ContactIntent contracts."""


class InvalidContactIntentError(ContactIntentError):
    """A ContactIntent value or lifecycle transition is invalid."""


class InvalidContactCandidateErrorForIntent(ContactIntentError):
    """An input Candidate does not satisfy the frozen CT0-2 contract."""


def _require_aware_datetime(value: str, name: str) -> datetime:
    _require_timestamp(value, name)
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.astimezone(timezone.utc)


def normalize_utc(value: str, name: str = "timestamp") -> str:
    parsed = _require_aware_datetime(value, name)
    return parsed.isoformat(timespec="microseconds").replace("+00:00", "Z")


def derive_intent_identity(candidate_idempotency_key: str) -> tuple[str, str]:
    """Derive semantic identity from Candidate identity, not delivery-attempt identity."""
    _require_ref(candidate_idempotency_key, "candidate_idempotency_key")
    digest = hashlib.sha256(candidate_idempotency_key.encode("utf-8")).hexdigest()
    return f"cintent:{digest[:32]}", f"cintent-idem:{digest}"


def _validated_candidate(candidate: object) -> ContactCandidate:
    if not isinstance(candidate, ContactCandidate):
        raise InvalidContactCandidateErrorForIntent("ContactIntent requires a ContactCandidate")
    try:
        # Re-run the frozen CT0-2 constructor validation, including its derived
        # candidate_id/idempotency checks; do not trust a typed instance blindly.
        return ContactCandidate(**asdict(candidate))
    except (TypeError, ValueError) as exc:
        raise InvalidContactCandidateErrorForIntent("ContactCandidate failed contract revalidation") from exc


@dataclass(frozen=True, slots=True)
class ContactIntent:
    """A bounded, source-backed intent to consider one specific contact."""

    intent_id: str
    schema_version: str
    revision: int
    subject_id: str
    recipient_ref: str
    candidate_ref: str
    candidate_idempotency_key: str
    source_refs: tuple[str, ...]
    intent_kind: str
    content_scope_refs: tuple[str, ...]
    topic_refs: tuple[str, ...]
    channel_scope: tuple[str, ...]
    created_at: str
    observed_at: str
    valid_until: str
    status: str
    causal_parent_refs: tuple[str, ...]
    policy_version: str
    idempotency_key: str
    updated_at: str

    def __post_init__(self) -> None:
        if self.schema_version != CONTACT_INTENT_SCHEMA:
            raise InvalidContactIntentError("unsupported ContactIntent schema")
        if not isinstance(self.revision, int) or isinstance(self.revision, bool) or self.revision < 1:
            raise InvalidContactIntentError("ContactIntent revision must be an integer >= 1")
        if self.subject_id != GLOBAL_SUBJECT_ID:
            raise InvalidContactIntentError("ContactIntent subject must be chiyo.global")
        if self.intent_kind != CANDIDATE_SHARE_EXPERIENCE:
            raise InvalidContactIntentError("unsupported ContactIntent kind")
        if self.status not in CONTACT_INTENT_STATUSES:
            raise InvalidContactIntentError("unsupported ContactIntent status")
        for name, value in (
            ("intent_id", self.intent_id),
            ("recipient_ref", self.recipient_ref),
            ("candidate_ref", self.candidate_ref),
            ("candidate_idempotency_key", self.candidate_idempotency_key),
            ("policy_version", self.policy_version),
            ("idempotency_key", self.idempotency_key),
        ):
            _require_ref(value, name)
        if not self.intent_id.startswith("cintent:"):
            raise InvalidContactIntentError("intent_id must use the cintent namespace")
        if not self.candidate_ref.startswith("ccand:"):
            raise InvalidContactIntentError("candidate_ref must reference a ContactCandidate")
        expected_intent_id, expected_idempotency = derive_intent_identity(self.candidate_idempotency_key)
        if self.intent_id != expected_intent_id or self.idempotency_key != expected_idempotency:
            raise InvalidContactIntentError("ContactIntent identity is not derived from its Candidate")

        for name, refs, allow_empty in (
            ("source_refs", self.source_refs, False),
            ("content_scope_refs", self.content_scope_refs, False),
            ("topic_refs", self.topic_refs, False),
            ("channel_scope", self.channel_scope, False),
            ("causal_parent_refs", self.causal_parent_refs, True),
        ):
            if not isinstance(refs, tuple) or (not allow_empty and not refs):
                raise InvalidContactIntentError(f"{name} must be an immutable tuple with required provenance")
            if len(refs) > 256 or len(set(refs)) != len(refs):
                raise InvalidContactIntentError(f"{name} exceeds bounds or contains duplicates")
            for ref in refs:
                _require_ref(ref, name)

        if self.candidate_ref not in self.source_refs:
            raise InvalidContactIntentError("source_refs must retain the canonical Candidate reference")
        if not set(self.content_scope_refs) <= set(self.source_refs):
            raise InvalidContactIntentError("content_scope_refs must be bounded by Candidate sources")
        _require_timestamp(self.created_at, "created_at")
        _require_timestamp(self.observed_at, "observed_at")
        _require_timestamp(self.valid_until, "valid_until")
        _require_timestamp(self.updated_at, "updated_at")
        if _require_aware_datetime(self.valid_until, "valid_until") <= _require_aware_datetime(self.created_at, "created_at"):
            raise InvalidContactIntentError("valid_until must be later than created_at")

    @classmethod
    def from_candidate(
        cls,
        candidate: object,
        *,
        created_at: str,
        valid_until: str,
        policy_version: str = CONTACT_INTENT_POLICY_VERSION,
    ) -> "ContactIntent":
        source = _validated_candidate(candidate)
        intent_id, idempotency_key = derive_intent_identity(source.idempotency_key)
        source_refs: list[str] = []
        for ref in (source.candidate_id, *source.source_refs):
            if ref not in source_refs:
                source_refs.append(ref)
        parents: list[str] = []
        for ref in (source.candidate_id, *source.causal_parent_refs):
            if ref not in parents:
                parents.append(ref)
        created = normalize_utc(created_at, "created_at")
        return cls(
            intent_id=intent_id,
            schema_version=CONTACT_INTENT_SCHEMA,
            revision=1,
            subject_id=source.subject_id,
            recipient_ref=source.recipient_ref,
            candidate_ref=source.candidate_id,
            candidate_idempotency_key=source.idempotency_key,
            source_refs=tuple(source_refs),
            intent_kind=source.candidate_kind,
            content_scope_refs=tuple(source.source_refs),
            topic_refs=(source.activity_ref,),
            channel_scope=source.channel_options,
            created_at=created,
            observed_at=normalize_utc(source.observed_at, "observed_at"),
            valid_until=normalize_utc(valid_until, "valid_until"),
            status=STATUS_OPEN,
            causal_parent_refs=tuple(parents),
            policy_version=policy_version,
            idempotency_key=idempotency_key,
            updated_at=created,
        )

    def to_dict(self) -> dict[str, object]:
        data = asdict(self)
        for name in ("source_refs", "content_scope_refs", "topic_refs", "channel_scope", "causal_parent_refs"):
            data[name] = list(data[name])
        return data

    @classmethod
    def from_mapping(cls, raw: object) -> "ContactIntent":
        if not isinstance(raw, Mapping):
            raise InvalidContactIntentError("ContactIntent input must be a mapping")
        expected = set(cls.__dataclass_fields__)
        if set(raw) != expected:
            raise InvalidContactIntentError("ContactIntent fields do not match the frozen schema")
        data = dict(raw)
        for name in ("source_refs", "content_scope_refs", "topic_refs", "channel_scope", "causal_parent_refs"):
            value = data[name]
            if not isinstance(value, (tuple, list)):
                raise InvalidContactIntentError(f"{name} must be a list/tuple of refs")
            data[name] = tuple(value)
        try:
            return cls(**data)
        except (TypeError, ValueError) as exc:
            if isinstance(exc, InvalidContactIntentError):
                raise
            raise InvalidContactIntentError("ContactIntent mapping failed validation") from exc


__all__ = [
    "ALLOWED_INTENT_TRANSITIONS", "CONTACT_INTENT_POLICY_VERSION", "CONTACT_INTENT_SCHEMA",
    "CONTACT_INTENT_STATUSES", "ContactIntent", "ContactIntentError", "InvalidContactCandidateErrorForIntent",
    "InvalidContactIntentError", "STATUS_CANCELLED", "STATUS_CONSUMED", "STATUS_DEFERRED",
    "STATUS_EXPIRED", "STATUS_OPEN", "STATUS_SELECTED", "STATUS_STALE", "TERMINAL_INTENT_STATUSES",
    "derive_intent_identity", "normalize_utc",
]
