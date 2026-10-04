"""Pure in-memory fake Telegram transport for isolated CT0-7 contract tests.

No networking, Telegram credential access, persistence, or live transport imports.
All effects are synthetic records held only in process memory.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import json
import threading
from typing import Final

from contact_message_action_model import ContactMessageActionRequest


OUTCOME_SUBMITTED_ONLY: Final = "SUBMITTED_ONLY"
OUTCOME_ACKNOWLEDGED: Final = "ACKNOWLEDGED"
OUTCOME_DELIVERED: Final = "DELIVERED"
OUTCOME_FAILED_BEFORE_ACCEPT: Final = "FAILED_BEFORE_ACCEPT"
OUTCOME_UNKNOWN_AFTER_SUBMIT: Final = "UNKNOWN_AFTER_SUBMIT"
FAKE_OUTCOMES: Final = frozenset({
    OUTCOME_SUBMITTED_ONLY,
    OUTCOME_ACKNOWLEDGED,
    OUTCOME_DELIVERED,
    OUTCOME_FAILED_BEFORE_ACCEPT,
    OUTCOME_UNKNOWN_AFTER_SUBMIT,
})

STATUS_SUBMITTED: Final = "SUBMITTED"
STATUS_ACKNOWLEDGED: Final = "ACKNOWLEDGED"
STATUS_DELIVERED: Final = "DELIVERED"
STATUS_FAILED_BEFORE_ACCEPT: Final = "FAILED_BEFORE_ACCEPT"
STATUS_UNKNOWN: Final = "UNKNOWN"
STATUS_CONFLICT: Final = "CONFLICT"
STATUS_REJECTED: Final = "REJECTED"
READ_UNKNOWN: Final = "READ_UNKNOWN"


@dataclass(frozen=True, slots=True)
class FakeSubmissionReceipt:
    """A synthetic transport observation, not a CT0-8 canonical receipt."""

    request_id: str
    idempotency_key: str
    status: str
    effect_applied: bool
    acknowledged: bool
    delivered: bool
    read_status: str
    effect_count_for_key: int
    replayed: bool


@dataclass(frozen=True, slots=True)
class RecordedFakeTelegramEffect:
    """One local in-memory fake effect; no message leaves this process."""

    request_id: str
    idempotency_key: str
    recipient_ref: str
    channel: str
    payload_ref: str
    content_hash: str
    text: str


@dataclass(frozen=True, slots=True)
class _TransportRecord:
    fingerprint: str
    receipt: FakeSubmissionReceipt


class RecordingTelegramTransportError(ValueError):
    """Invalid recording transport configuration or request."""


class FakeTransportCrashAfterAccept(RuntimeError):
    """Synthetic crash after one fake effect, before caller receives its receipt."""


class RecordingTelegramTransport:
    """Thread-safe, idempotent fake with explicit scripted transport outcomes."""

    def __init__(self) -> None:
        self._records: dict[str, _TransportRecord] = {}
        self._effects: list[RecordedFakeTelegramEffect] = []
        self._lock = threading.RLock()
        self.submit_call_count = 0
        self.lookup_count = 0
        self.query_count = 0

    @property
    def effects(self) -> tuple[RecordedFakeTelegramEffect, ...]:
        with self._lock:
            return tuple(self._effects)

    @staticmethod
    def _raw_identity(value: object) -> tuple[str, str, str] | None:
        if not isinstance(value, ContactMessageActionRequest):
            return None
        try:
            data = value.to_dict()
            key = data.get("idempotency_key")
            request_id = data.get("request_id")
            canonical = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        except Exception:
            return None
        if not isinstance(key, str) or not isinstance(request_id, str):
            return None
        return key, request_id, hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @staticmethod
    def _fingerprint(request: ContactMessageActionRequest) -> str:
        canonical = json.dumps(request.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @staticmethod
    def _receipt(
        request_id: str,
        idempotency_key: str,
        status: str,
        *,
        effect_applied: bool = False,
        acknowledged: bool = False,
        delivered: bool = False,
        effect_count_for_key: int = 0,
        replayed: bool = False,
    ) -> FakeSubmissionReceipt:
        return FakeSubmissionReceipt(
            request_id=request_id,
            idempotency_key=idempotency_key,
            status=status,
            effect_applied=effect_applied,
            acknowledged=acknowledged,
            delivered=delivered,
            read_status=READ_UNKNOWN,
            effect_count_for_key=effect_count_for_key,
            replayed=replayed,
        )

    def lookup_existing(self, request: ContactMessageActionRequest) -> FakeSubmissionReceipt | None:
        """Read-only idempotency lookup; no effect or submit is performed."""
        identity = self._raw_identity(request)
        if identity is None:
            return None
        key, request_id, fingerprint = identity
        with self._lock:
            self.lookup_count += 1
            prior = self._records.get(key)
            if prior is None:
                return None
            if prior.fingerprint != fingerprint:
                return self._receipt(
                    request_id, key, STATUS_CONFLICT,
                    effect_count_for_key=prior.receipt.effect_count_for_key,
                    replayed=True,
                )
            return replace(prior.receipt, replayed=True)

    def submit(
        self,
        *,
        request: object,
        outcome: str = OUTCOME_SUBMITTED_ONLY,
        crash_after_accept_before_receipt: bool = False,
    ) -> FakeSubmissionReceipt:
        """Atomically record at most one fake effect per key."""
        identity = self._raw_identity(request)
        if identity is None:
            return self._receipt("mreq:invalid", "ar0_act:invalid", STATUS_REJECTED)
        key, request_id, raw_fingerprint = identity
        with self._lock:
            prior = self._records.get(key)
            if prior is not None:
                if prior.fingerprint != raw_fingerprint:
                    return self._receipt(
                        request_id, key, STATUS_CONFLICT,
                        effect_count_for_key=prior.receipt.effect_count_for_key,
                        replayed=True,
                    )
                return replace(prior.receipt, replayed=True)

            try:
                frozen = ContactMessageActionRequest.from_mapping(request.to_dict())
            except Exception:
                return self._receipt(request_id, key, STATUS_REJECTED)
            if outcome not in FAKE_OUTCOMES:
                raise RecordingTelegramTransportError("unsupported fake transport outcome")
            if crash_after_accept_before_receipt and outcome == OUTCOME_FAILED_BEFORE_ACCEPT:
                raise RecordingTelegramTransportError("crash-after-accept conflicts with pre-accept failure")

            self.submit_call_count += 1
            if outcome == OUTCOME_FAILED_BEFORE_ACCEPT:
                receipt = self._receipt(request_id, key, STATUS_FAILED_BEFORE_ACCEPT)
            else:
                payload = frozen.payload_data
                self._effects.append(RecordedFakeTelegramEffect(
                    request_id=frozen.request_id,
                    idempotency_key=frozen.idempotency_key,
                    recipient_ref=frozen.recipient_ref,
                    channel=frozen.channel,
                    payload_ref=frozen.payload_ref,
                    content_hash=frozen.content_hash,
                    text=payload["text"],  # type: ignore[arg-type]
                ))
                effect_count = sum(1 for item in self._effects if item.idempotency_key == key)
                if outcome == OUTCOME_SUBMITTED_ONLY:
                    receipt = self._receipt(request_id, key, STATUS_SUBMITTED, effect_applied=True, effect_count_for_key=effect_count)
                elif outcome == OUTCOME_ACKNOWLEDGED:
                    receipt = self._receipt(request_id, key, STATUS_ACKNOWLEDGED, effect_applied=True, acknowledged=True, effect_count_for_key=effect_count)
                elif outcome == OUTCOME_DELIVERED:
                    receipt = self._receipt(request_id, key, STATUS_DELIVERED, effect_applied=True, acknowledged=True, delivered=True, effect_count_for_key=effect_count)
                else:
                    receipt = self._receipt(request_id, key, STATUS_UNKNOWN, effect_applied=True, effect_count_for_key=effect_count)
            if crash_after_accept_before_receipt and receipt.effect_applied:
                receipt = replace(receipt, status=STATUS_UNKNOWN, acknowledged=False, delivered=False)
            self._records[key] = _TransportRecord(self._fingerprint(frozen), receipt)
            if crash_after_accept_before_receipt:
                raise FakeTransportCrashAfterAccept("synthetic crash after fake accept, before local receipt")
            return receipt

    def query(self, *, idempotency_key: str) -> FakeSubmissionReceipt | None:
        """Read-only fake status surface; not a CT0-8 reconciliation owner."""
        with self._lock:
            self.query_count += 1
            record = self._records.get(idempotency_key)
            return replace(record.receipt, replayed=True) if record is not None else None

    @property
    def external_effect_count(self) -> int:
        """Count of in-memory fake effects, never real external side effects."""
        with self._lock:
            return len(self._effects)


__all__ = [
    "FAKE_OUTCOMES", "OUTCOME_ACKNOWLEDGED", "OUTCOME_DELIVERED",
    "OUTCOME_FAILED_BEFORE_ACCEPT", "OUTCOME_SUBMITTED_ONLY", "OUTCOME_UNKNOWN_AFTER_SUBMIT",
    "READ_UNKNOWN", "STATUS_ACKNOWLEDGED", "STATUS_CONFLICT", "STATUS_DELIVERED",
    "STATUS_FAILED_BEFORE_ACCEPT", "STATUS_REJECTED", "STATUS_SUBMITTED", "STATUS_UNKNOWN",
    "FakeSubmissionReceipt", "FakeTransportCrashAfterAccept", "RecordedFakeTelegramEffect",
    "RecordingTelegramTransport", "RecordingTelegramTransportError",
]
