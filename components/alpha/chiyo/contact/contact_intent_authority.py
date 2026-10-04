"""Pure in-memory CT0-3 ContactIntent authority, journal, and recovery seam.

This module performs no file, database, network, service, Agency, Activity,
Action, Memory, or Relationship I/O. Its append-only journal is an in-memory
contract fixture that can be exported and replay-validated by tests.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
import threading
from typing import Mapping, Sequence

from contact_candidate_model import ContactCandidate, ContactContractError, _require_ref
from contact_intent_model import (
    ALLOWED_INTENT_TRANSITIONS,
    CONTACT_INTENT_SCHEMA,
    CONTACT_INTENT_STATUSES,
    STATUS_CANCELLED,
    STATUS_CONSUMED,
    STATUS_DEFERRED,
    STATUS_EXPIRED,
    STATUS_OPEN,
    STATUS_SELECTED,
    STATUS_STALE,
    TERMINAL_INTENT_STATUSES,
    ContactIntent,
    InvalidContactCandidateErrorForIntent,
    InvalidContactIntentError,
    _validated_candidate,
    derive_intent_identity,
    normalize_utc,
)


_AUTHORITY_CLAIM_TOKEN = object()
CONTACT_INTENT_STORE_SCHEMA = "chiyo.contact.intent_store.v1"
CONTACT_INTENT_JOURNAL_SCHEMA = "chiyo.contact.intent_journal_entry.v1"


class ContactIntentAuthorityError(ContactContractError):
    """Base error for ContactIntent authority operations."""


class ContactIntentNotFoundError(ContactIntentAuthorityError):
    """No ContactIntent exists for the requested reference."""


class ContactIntentStaleSourceError(ContactIntentAuthorityError):
    """The Candidate's canonical source observation is too old or in the future."""


class ContactIntentRevisionConflictError(ContactIntentAuthorityError):
    """The caller's expected revision does not match the current Intent revision."""


class ContactIntentTransitionError(ContactIntentAuthorityError):
    """The requested lifecycle transition is not legal for the current state."""


class ContactIntentRecoveryError(ContactIntentAuthorityError):
    """An exported Intent journal is malformed, tampered, or unreplayable."""


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _aware_utc(value: str, field: str) -> datetime:
    normalized = normalize_utc(value, field)
    parsed = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
    return parsed.astimezone(timezone.utc)


def _seconds_policy(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ContactIntentAuthorityError(f"{name} must be a finite positive number of seconds")
    result = float(value)
    if not math.isfinite(result) or result <= 0:
        raise ContactIntentAuthorityError(f"{name} must be a finite positive number of seconds")
    return result


def _entry_hash_body(entry: "ContactIntentJournalEntry") -> dict[str, object]:
    return {
        "schema_version": entry.schema_version,
        "sequence": entry.sequence,
        "event_key": entry.event_key,
        "event_kind": entry.event_kind,
        "intent_id": entry.intent_id,
        "revision": entry.revision,
        "status": entry.status,
        "evidence_ref": entry.evidence_ref,
        "occurred_at": entry.occurred_at,
        "intent_json": entry.intent_json,
        "previous_hash": entry.previous_hash,
    }


def _hash_entry_body(body: Mapping[str, object]) -> str:
    return hashlib.sha256(_canonical_json(dict(body)).encode("utf-8")).hexdigest()


def _make_event_key(intent_id: str, event_kind: str, evidence_ref: str) -> str:
    digest = hashlib.sha256(f"{intent_id}\x00{event_kind}\x00{evidence_ref}".encode("utf-8")).hexdigest()
    return f"cintent-event:{digest}"


def _is_sha256(value: object) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(ch in "0123456789abcdef" for ch in value)


@dataclass(frozen=True, slots=True)
class ContactIntentJournalEntry:
    schema_version: str
    sequence: int
    event_key: str
    event_kind: str
    intent_id: str
    revision: int
    status: str
    evidence_ref: str
    occurred_at: str
    intent_json: str
    previous_hash: str
    entry_hash: str

    def to_dict(self) -> dict[str, object]:
        return asdict(self)

    @classmethod
    def from_mapping(cls, raw: object) -> "ContactIntentJournalEntry":
        if not isinstance(raw, Mapping):
            raise ContactIntentRecoveryError("journal entry must be a mapping")
        if set(raw) != set(cls.__dataclass_fields__):
            raise ContactIntentRecoveryError("journal entry fields do not match the frozen schema")
        try:
            entry = cls(**dict(raw))
        except TypeError as exc:
            raise ContactIntentRecoveryError("journal entry fields have invalid types") from exc
        if entry.schema_version != CONTACT_INTENT_JOURNAL_SCHEMA:
            raise ContactIntentRecoveryError("unsupported ContactIntent journal schema")
        if not isinstance(entry.sequence, int) or isinstance(entry.sequence, bool) or entry.sequence < 1:
            raise ContactIntentRecoveryError("journal sequence must be an integer >= 1")
        if not isinstance(entry.revision, int) or isinstance(entry.revision, bool) or entry.revision < 1:
            raise ContactIntentRecoveryError("journal revision must be an integer >= 1")
        if not isinstance(entry.status, str) or entry.status not in CONTACT_INTENT_STATUSES:
            raise ContactIntentRecoveryError("journal status is invalid")
        for name in ("event_key", "event_kind", "intent_id", "evidence_ref"):
            try:
                _require_ref(getattr(entry, name), name)
            except ContactContractError as exc:
                raise ContactIntentRecoveryError(f"journal {name} is invalid") from exc
        if not _is_sha256(entry.previous_hash) or not _is_sha256(entry.entry_hash):
            raise ContactIntentRecoveryError("journal hash fields are invalid")
        try:
            normalize_utc(entry.occurred_at, "occurred_at")
        except ContactContractError as exc:
            raise ContactIntentRecoveryError("journal occurred_at is invalid") from exc
        if not isinstance(entry.intent_json, str) or len(entry.intent_json) > 131072:
            raise ContactIntentRecoveryError("journal intent snapshot exceeds the bound")
        if _hash_entry_body(_entry_hash_body(entry)) != entry.entry_hash:
            raise ContactIntentRecoveryError("journal entry hash mismatch")
        return entry


@dataclass(frozen=True, slots=True)
class IntentCreationResult:
    intent: ContactIntent
    created: bool
    journal_sequence: int


class ContactIntentStore:
    """Append-only in-memory Intent store with deterministic export/recovery."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.__writer_capability = object()
        self.__authority_claimed = False
        self._intents: dict[str, ContactIntent] = {}
        self._journal: list[ContactIntentJournalEntry] = []
        self._event_keys: dict[str, ContactIntentJournalEntry] = {}

    def _claim_authority(self, authority: object, claim_token: object) -> object:
        """Bind this store to its owning Authority exactly once."""
        with self._lock:
            if claim_token is not _AUTHORITY_CLAIM_TOKEN or not isinstance(authority, ContactIntentAuthority):
                raise ContactIntentAuthorityError("only a ContactIntentAuthority may claim this store")
            if getattr(authority, "_store", None) is not self:
                raise ContactIntentAuthorityError("authority does not own this ContactIntentStore")
            if self.__authority_claimed:
                raise ContactIntentAuthorityError("ContactIntentStore already has an authority")
            self.__authority_claimed = True
            return self.__writer_capability

    def _require_writer(self, capability: object | None) -> None:
        if capability is not self.__writer_capability:
            raise ContactIntentAuthorityError("ContactIntentStore writes require authority capability")

    @property
    def journal(self) -> tuple[ContactIntentJournalEntry, ...]:
        with self._lock:
            return tuple(self._journal)

    def get(self, intent_id: str) -> ContactIntent | None:
        _require_ref(intent_id, "intent_id")
        with self._lock:
            return self._intents.get(intent_id)

    def list_intents(self) -> tuple[ContactIntent, ...]:
        with self._lock:
            return tuple(self._intents[key] for key in sorted(self._intents))

    def _append(
        self,
        intent: ContactIntent,
        *,
        event_kind: str,
        evidence_ref: str,
        occurred_at: str,
    ) -> ContactIntentJournalEntry:
        canonical_intent = _canonical_json(intent.to_dict())
        sequence = len(self._journal) + 1
        previous_hash = self._journal[-1].entry_hash if self._journal else "0" * 64
        event_key = _make_event_key(intent.intent_id, event_kind, evidence_ref)
        provisional = ContactIntentJournalEntry(
            schema_version=CONTACT_INTENT_JOURNAL_SCHEMA,
            sequence=sequence,
            event_key=event_key,
            event_kind=event_kind,
            intent_id=intent.intent_id,
            revision=intent.revision,
            status=intent.status,
            evidence_ref=evidence_ref,
            occurred_at=occurred_at,
            intent_json=canonical_intent,
            previous_hash=previous_hash,
            entry_hash="0" * 64,
        )
        entry = replace(provisional, entry_hash=_hash_entry_body(_entry_hash_body(provisional)))
        self._journal.append(entry)
        self._event_keys[event_key] = entry
        self._intents[intent.intent_id] = intent
        return entry

    def create(self, intent: ContactIntent, *, capability: object | None = None) -> IntentCreationResult:
        self._require_writer(capability)
        if not isinstance(intent, ContactIntent):
            raise InvalidContactIntentError("store accepts only ContactIntent values")
        intent = ContactIntent.from_mapping(intent.to_dict())
        evidence_ref = intent.candidate_ref
        event_key = _make_event_key(intent.intent_id, "CREATED", evidence_ref)
        with self._lock:
            prior = self._event_keys.get(event_key)
            if prior is not None:
                replayed = ContactIntent.from_mapping(json.loads(prior.intent_json))
                if replayed.candidate_idempotency_key != intent.candidate_idempotency_key:
                    raise ContactIntentRecoveryError("semantic Intent identity collision")
                return IntentCreationResult(replayed, False, prior.sequence)
            if intent.intent_id in self._intents:
                raise ContactIntentRecoveryError("Intent ID exists without matching creation identity")
            if intent.revision != 1 or intent.status != STATUS_OPEN:
                raise InvalidContactIntentError("new Intent must start at revision 1 in OPEN")
            entry = self._append(intent, event_kind="CREATED", evidence_ref=evidence_ref, occurred_at=intent.created_at)
            return IntentCreationResult(intent, True, entry.sequence)

    def transition(
        self,
        *,
        intent_id: str,
        target_status: str,
        expected_revision: int,
        evidence_ref: str,
        occurred_at: str,
        capability: object | None = None,
        valid_until: str | None = None,
    ) -> IntentCreationResult:
        """Atomically replay, compare revision, enforce expiry, and append."""
        self._require_writer(capability)
        _require_ref(intent_id, "intent_id")
        _require_ref(evidence_ref, "evidence_ref")
        timestamp = normalize_utc(occurred_at, "occurred_at")
        if target_status not in CONTACT_INTENT_STATUSES:
            raise ContactIntentTransitionError("target ContactIntent status is invalid")
        if not isinstance(expected_revision, int) or isinstance(expected_revision, bool) or expected_revision < 1:
            raise ContactIntentRevisionConflictError("expected_revision must be an integer >= 1")
        if valid_until is not None:
            normalize_utc(valid_until, "valid_until")

        event_kind = f"TRANSITION_{target_status}"
        event_key = _make_event_key(intent_id, event_kind, evidence_ref)
        with self._lock:
            prior = self._event_keys.get(event_key)
            if prior is not None:
                replayed = ContactIntent.from_mapping(json.loads(prior.intent_json))
                return IntentCreationResult(replayed, False, prior.sequence)

            current = self._intents.get(intent_id)
            if current is None:
                raise ContactIntentNotFoundError("ContactIntent does not exist")
            if current.revision != expected_revision:
                raise ContactIntentRevisionConflictError("ContactIntent revision changed")

            expired = (
                valid_until is not None
                and current.status not in TERMINAL_INTENT_STATUSES
                and _aware_utc(timestamp, "occurred_at") >= _aware_utc(valid_until, "valid_until")
            )
            if expired:
                target_status = STATUS_EXPIRED
                evidence_ref = f"expiry:{current.intent_id}:r{current.revision}"
                event_kind = f"TRANSITION_{target_status}"
                event_key = _make_event_key(intent_id, event_kind, evidence_ref)
                prior_expiry = self._event_keys.get(event_key)
                if prior_expiry is not None:
                    replayed = ContactIntent.from_mapping(json.loads(prior_expiry.intent_json))
                    return IntentCreationResult(replayed, False, prior_expiry.sequence)

            if target_status not in ALLOWED_INTENT_TRANSITIONS.get(current.status, frozenset()):
                raise ContactIntentTransitionError(f"illegal ContactIntent transition {current.status}->{target_status}")
            updated = ContactIntent.from_mapping(
                replace(current, revision=current.revision + 1, status=target_status, updated_at=timestamp).to_dict()
            )
            entry = self._append(updated, event_kind=event_kind, evidence_ref=evidence_ref, occurred_at=timestamp)
            return IntentCreationResult(updated, True, entry.sequence)

    def export_state(self) -> dict[str, object]:
        with self._lock:
            return {"schema_version": CONTACT_INTENT_STORE_SCHEMA, "journal": [entry.to_dict() for entry in self._journal]}

    @classmethod
    def recover(cls, snapshot: object) -> "ContactIntentStore":
        if not isinstance(snapshot, Mapping) or set(snapshot) != {"schema_version", "journal"}:
            raise ContactIntentRecoveryError("store snapshot shape is invalid")
        if snapshot.get("schema_version") != CONTACT_INTENT_STORE_SCHEMA:
            raise ContactIntentRecoveryError("unsupported ContactIntent store snapshot")
        raw_journal = snapshot.get("journal")
        if not isinstance(raw_journal, list) or len(raw_journal) > 1_000_000:
            raise ContactIntentRecoveryError("store journal is missing or exceeds bounds")
        return cls._replay(raw_journal)

    @classmethod
    def _replay(cls, raw_journal: Sequence[object]) -> "ContactIntentStore":
        recovered = cls()
        previous_hash = "0" * 64
        for expected_sequence, raw in enumerate(raw_journal, start=1):
            entry = ContactIntentJournalEntry.from_mapping(raw)
            if entry.sequence != expected_sequence or entry.previous_hash != previous_hash:
                raise ContactIntentRecoveryError("journal sequence or hash-chain link is invalid")
            previous_hash = entry.entry_hash
            try:
                intent_data = json.loads(entry.intent_json)
            except (TypeError, json.JSONDecodeError) as exc:
                raise ContactIntentRecoveryError("journal snapshot JSON is invalid") from exc
            intent = ContactIntent.from_mapping(intent_data)
            if (intent.intent_id, intent.revision, intent.status) != (entry.intent_id, entry.revision, entry.status):
                raise ContactIntentRecoveryError("journal metadata does not match its Intent snapshot")
            event_key = _make_event_key(entry.intent_id, entry.event_kind, entry.evidence_ref)
            if entry.event_key != event_key or event_key in recovered._event_keys:
                raise ContactIntentRecoveryError("journal event identity is invalid or duplicated")

            previous = recovered._intents.get(intent.intent_id)
            if entry.event_kind == "CREATED":
                if previous is not None or intent.revision != 1 or intent.status != STATUS_OPEN:
                    raise ContactIntentRecoveryError("journal creation event is not a unique OPEN revision 1")
                if entry.evidence_ref != intent.candidate_ref:
                    raise ContactIntentRecoveryError("journal creation provenance does not match Candidate")
            else:
                if previous is None:
                    raise ContactIntentRecoveryError("journal transition precedes Intent creation")
                if entry.event_kind != f"TRANSITION_{intent.status}":
                    raise ContactIntentRecoveryError("journal transition kind does not match target status")
                if intent.revision != previous.revision + 1:
                    raise ContactIntentRecoveryError("journal transition revision is not consecutive")
                if intent.status not in ALLOWED_INTENT_TRANSITIONS.get(previous.status, frozenset()):
                    raise ContactIntentRecoveryError("journal contains an illegal lifecycle transition")
                expected = replace(previous, revision=intent.revision, status=intent.status, updated_at=intent.updated_at)
                if expected != intent or entry.occurred_at != intent.updated_at:
                    raise ContactIntentRecoveryError("journal transition mutated fields outside lifecycle state")
            recovered._journal.append(entry)
            recovered._event_keys[event_key] = entry
            recovered._intents[intent.intent_id] = intent
        return recovered


class ContactIntentAuthority:
    """Own ContactIntent lifecycle only; never owns Agency or external effects."""

    def __init__(
        self,
        *,
        store: ContactIntentStore | None = None,
        max_source_age_seconds: float = 7 * 24 * 60 * 60,
        intent_ttl_seconds: float = 7 * 24 * 60 * 60,
        policy_version: str = "ct0.contact_intent_policy.v1",
    ) -> None:
        self._store = store if store is not None else ContactIntentStore()
        self.max_source_age_seconds = _seconds_policy(max_source_age_seconds, "max_source_age_seconds")
        self.intent_ttl_seconds = _seconds_policy(intent_ttl_seconds, "intent_ttl_seconds")
        self.policy_version = _require_ref(policy_version, "policy_version")
        self._write_capability = self._store._claim_authority(self, _AUTHORITY_CLAIM_TOKEN)

    @property
    def store(self) -> ContactIntentStore:
        return self._store

    def create_intent(self, candidate: object, *, now: str) -> IntentCreationResult:
        source = _validated_candidate(candidate)
        intent_id, _ = derive_intent_identity(source.idempotency_key)
        existing = self._store.get(intent_id)
        if existing is not None:
            if existing.candidate_idempotency_key != source.idempotency_key or existing.candidate_ref != source.candidate_id:
                raise ContactIntentRecoveryError("semantic Intent identity collision")
            created_entry = next(
                entry for entry in self._store.journal
                if entry.event_kind == "CREATED" and entry.intent_id == intent_id
            )
            return IntentCreationResult(existing, False, created_entry.sequence)

        now_utc = _aware_utc(now, "now")
        source_observed = _aware_utc(source.observed_at, "candidate.observed_at")
        age_seconds = (now_utc - source_observed).total_seconds()
        if age_seconds < 0:
            raise ContactIntentStaleSourceError("Candidate source observation is in the future")
        if age_seconds > self.max_source_age_seconds:
            raise ContactIntentStaleSourceError("Candidate source observation is stale")

        source_stale_at = source_observed + timedelta(seconds=self.max_source_age_seconds)
        ttl_expires_at = now_utc + timedelta(seconds=self.intent_ttl_seconds)
        valid_until = min(source_stale_at, ttl_expires_at)
        if valid_until <= now_utc:
            raise ContactIntentStaleSourceError("Candidate source has no remaining valid lifetime")
        intent = ContactIntent.from_candidate(
            source,
            created_at=now_utc.isoformat(timespec="microseconds").replace("+00:00", "Z"),
            valid_until=valid_until.isoformat(timespec="microseconds").replace("+00:00", "Z"),
            policy_version=self.policy_version,
        )
        return self._store.create(intent, capability=self._write_capability)

    def defer(self, intent_id: str, *, expected_revision: int, evidence_ref: str, now: str) -> IntentCreationResult:
        return self._transition_with_expiry(
            intent_id, STATUS_DEFERRED, expected_revision=expected_revision, evidence_ref=evidence_ref, now=now
        )

    def reconsider(self, intent_id: str, *, expected_revision: int, opportunity_ref: str, now: str) -> IntentCreationResult:
        return self._transition_with_expiry(
            intent_id, STATUS_OPEN, expected_revision=expected_revision, evidence_ref=opportunity_ref, now=now
        )

    def record_selected(self, intent_id: str, *, expected_revision: int, decision_ref: str, now: str) -> IntentCreationResult:
        _require_typed_ref(decision_ref, "dec:", "decision_ref")
        return self._transition_with_expiry(
            intent_id, STATUS_SELECTED, expected_revision=expected_revision, evidence_ref=decision_ref, now=now
        )

    def record_consumed(self, intent_id: str, *, expected_revision: int, action_ref: str, now: str) -> IntentCreationResult:
        _require_typed_ref(action_ref, ("act:", "actn:"), "action_ref")
        return self._transition_with_expiry(
            intent_id, STATUS_CONSUMED, expected_revision=expected_revision, evidence_ref=action_ref, now=now
        )

    def cancel(self, intent_id: str, *, expected_revision: int, cancellation_ref: str, now: str) -> IntentCreationResult:
        return self._transition_with_expiry(
            intent_id, STATUS_CANCELLED, expected_revision=expected_revision, evidence_ref=cancellation_ref, now=now
        )

    def mark_stale(self, intent_id: str, *, expected_revision: int, invalidation_ref: str, now: str) -> IntentCreationResult:
        return self._transition_with_expiry(
            intent_id, STATUS_STALE, expected_revision=expected_revision, evidence_ref=invalidation_ref, now=now
        )

    def expire_due(self, *, now: str) -> tuple[ContactIntent, ...]:
        timestamp = normalize_utc(now, "now")
        instant = _aware_utc(timestamp, "now")
        expired: list[ContactIntent] = []
        for intent in self._store.list_intents():
            if intent.status in TERMINAL_INTENT_STATUSES:
                continue
            if instant >= _aware_utc(intent.valid_until, "valid_until"):
                evidence_ref = f"expiry:{intent.intent_id}:r{intent.revision}"
                result = self._store.transition(
                    intent_id=intent.intent_id,
                    target_status=STATUS_EXPIRED,
                    expected_revision=intent.revision,
                    evidence_ref=evidence_ref,
                    occurred_at=timestamp,
                    capability=self._write_capability,
                )
                expired.append(result.intent)
        return tuple(expired)

    def _transition_with_expiry(
        self,
        intent_id: str,
        target_status: str,
        *,
        expected_revision: int,
        evidence_ref: str,
        now: str,
    ) -> IntentCreationResult:
        timestamp = normalize_utc(now, "now")
        current = self._store.get(intent_id)
        if current is None:
            raise ContactIntentNotFoundError("ContactIntent does not exist")
        result = self._store.transition(
            intent_id=intent_id,
            target_status=target_status,
            expected_revision=expected_revision,
            evidence_ref=evidence_ref,
            occurred_at=timestamp,
            capability=self._write_capability,
            valid_until=current.valid_until,
        )
        if result.intent.status == STATUS_EXPIRED and target_status != STATUS_EXPIRED:
            raise ContactIntentTransitionError("ContactIntent expired before requested transition")
        return result


def _require_typed_ref(value: str, prefixes: str | tuple[str, ...], name: str) -> None:
    _require_ref(value, name)
    accepted = (prefixes,) if isinstance(prefixes, str) else prefixes
    if not value.startswith(accepted):
        raise ContactIntentAuthorityError(f"{name} has an unsupported evidence namespace")


__all__ = [
    "CONTACT_INTENT_JOURNAL_SCHEMA", "CONTACT_INTENT_STORE_SCHEMA", "ContactIntentAuthority",
    "ContactIntentAuthorityError", "ContactIntentJournalEntry", "ContactIntentNotFoundError",
    "ContactIntentRecoveryError", "ContactIntentRevisionConflictError", "ContactIntentStaleSourceError",
    "ContactIntentStore", "ContactIntentTransitionError", "IntentCreationResult",
]
