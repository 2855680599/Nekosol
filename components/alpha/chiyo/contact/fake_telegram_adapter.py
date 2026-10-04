"""CT0-7 fake-only adapter for frozen CT0-6 Message Action requests.

The adapter cannot import, connect to, or invoke real Telegram/network clients.
It submits only to RecordingTelegramTransport.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
from typing import Iterable

from contact_message_action_model import ContactMessageActionRequest
from recording_telegram_transport import (
    FAKE_OUTCOMES,
    FakeSubmissionReceipt,
    OUTCOME_SUBMITTED_ONLY,
    RecordingTelegramTransport,
    STATUS_CONFLICT,
    STATUS_REJECTED,
)


STATUS_BLOCKED: str = "BLOCKED"
STATUS_CRASHED_BEFORE_SUBMIT: str = "CRASHED_BEFORE_SUBMIT"


class FakeTelegramAdapterError(ValueError):
    """Base error for fake adapter configuration errors."""


@dataclass(frozen=True, slots=True)
class FakeTelegramAdapterResult:
    status: str
    request_id: str
    idempotency_key: str
    receipt: FakeSubmissionReceipt | None
    failure_code: str | None
    replayed: bool
    transport_submit_called: bool


@dataclass(frozen=True, slots=True)
class _AdapterRecord:
    request_fingerprint: str
    result: FakeTelegramAdapterResult


class FakeTelegramAdapter:
    """Fail-closed local adapter with explicit sandbox allowlists and replay guard."""

    def __init__(
        self,
        *,
        transport: RecordingTelegramTransport,
        allowed_recipients: Iterable[str] = (),
        allowed_channels: Iterable[str] = (),
        scripted_outcome: str = OUTCOME_SUBMITTED_ONLY,
    ) -> None:
        if scripted_outcome not in FAKE_OUTCOMES:
            raise FakeTelegramAdapterError("scripted_outcome is not a supported fake result")
        if not isinstance(transport, RecordingTelegramTransport):
            raise FakeTelegramAdapterError("adapter requires the recording fake transport")
        self._transport = transport
        self._allowed_recipients = frozenset(allowed_recipients)
        self._allowed_channels = frozenset(allowed_channels)
        if any(not isinstance(value, str) or not value.startswith("recipient:") for value in self._allowed_recipients):
            raise FakeTelegramAdapterError("recipient allowlist must contain sandbox recipient refs")
        if any(not isinstance(value, str) or not value.startswith("channel:") for value in self._allowed_channels):
            raise FakeTelegramAdapterError("channel allowlist must contain sandbox channel refs")
        self.scripted_outcome = scripted_outcome
        self._records: dict[str, _AdapterRecord] = {}
        self.transport_submit_call_count = 0

    @staticmethod
    def _fingerprint(request: ContactMessageActionRequest) -> str:
        canonical = json.dumps(request.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @staticmethod
    def _detached_request(value: object) -> ContactMessageActionRequest:
        if not isinstance(value, ContactMessageActionRequest):
            raise FakeTelegramAdapterError("adapter accepts only a typed CT0-6 Message Action request")
        try:
            return ContactMessageActionRequest.from_mapping(asdict(value))
        except Exception as exc:
            raise FakeTelegramAdapterError("Message Action request failed frozen-contract revalidation") from exc

    @staticmethod
    def _blocked(request: ContactMessageActionRequest, code: str) -> FakeTelegramAdapterResult:
        return FakeTelegramAdapterResult(
            status=STATUS_BLOCKED,
            request_id=request.request_id,
            idempotency_key=request.idempotency_key,
            receipt=None,
            failure_code=code,
            replayed=False,
            transport_submit_called=False,
        )

    def _conflict(self, request: ContactMessageActionRequest, prior: _AdapterRecord | None = None) -> FakeTelegramAdapterResult:
        count = prior.result.receipt.effect_count_for_key if prior and prior.result.receipt else 0
        receipt = FakeSubmissionReceipt(
            request_id=request.request_id,
            idempotency_key=request.idempotency_key,
            status=STATUS_CONFLICT,
            effect_applied=False,
            acknowledged=False,
            delivered=False,
            read_status="READ_UNKNOWN",
            effect_count_for_key=count,
            replayed=True,
        )
        result = FakeTelegramAdapterResult(
            status=STATUS_CONFLICT,
            request_id=request.request_id,
            idempotency_key=request.idempotency_key,
            receipt=receipt,
            failure_code="IDEMPOTENCY_PAYLOAD_CONFLICT",
            replayed=True,
            transport_submit_called=False,
        )
        self._records[request.idempotency_key] = _AdapterRecord(self._fingerprint(request), result)
        return result

    def submit(
        self,
        *,
        request: object,
        crash_before_submit: bool = False,
        crash_after_fake_accept_before_receipt: bool = False,
    ) -> FakeTelegramAdapterResult:
        """Submit one frozen request to the recording fake, never to Telegram."""
        frozen = self._detached_request(request)
        fingerprint = self._fingerprint(frozen)
        key = frozen.idempotency_key

        # Authorization precedes all cache/status reads to avoid leaking status
        # for a request outside this adapter instance's explicit sandbox scope.
        if frozen.recipient_ref not in self._allowed_recipients:
            return self._blocked(frozen, "RECIPIENT_NOT_ALLOWLISTED")
        if frozen.channel not in self._allowed_channels:
            return self._blocked(frozen, "CHANNEL_NOT_ALLOWLISTED")
        if not frozen.would_submit:
            return self._blocked(frozen, "SUBMISSION_GATE_CLOSED")

        prior = self._records.get(key)
        if prior is not None:
            if prior.request_fingerprint != fingerprint:
                return self._conflict(frozen, prior)
            cached = prior.result
            if cached.status == STATUS_CONFLICT:
                return cached
            receipt = cached.receipt
            if receipt is not None:
                observed = self._transport.query(idempotency_key=key)
                if observed is not None:
                    receipt = observed
            replayed = FakeTelegramAdapterResult(
                status=receipt.status if receipt else cached.status,
                request_id=frozen.request_id,
                idempotency_key=key,
                receipt=receipt,
                failure_code=cached.failure_code,
                replayed=True,
                transport_submit_called=False,
            )
            self._records[key] = _AdapterRecord(fingerprint, replayed)
            return replayed

        existing = self._transport.lookup_existing(frozen)
        if existing is not None:
            if existing.status == STATUS_CONFLICT:
                return self._conflict(frozen)
            result = FakeTelegramAdapterResult(
                status=existing.status,
                request_id=frozen.request_id,
                idempotency_key=key,
                receipt=existing,
                failure_code=None,
                replayed=True,
                transport_submit_called=False,
            )
            self._records[key] = _AdapterRecord(fingerprint, result)
            return result

        if crash_before_submit:
            result = FakeTelegramAdapterResult(
                status=STATUS_CRASHED_BEFORE_SUBMIT,
                request_id=frozen.request_id,
                idempotency_key=key,
                receipt=None,
                failure_code="SYNTHETIC_CRASH_BEFORE_TRANSPORT_CALL",
                replayed=False,
                transport_submit_called=False,
            )
            self._records[key] = _AdapterRecord(fingerprint, result)
            return result

        self.transport_submit_call_count += 1
        receipt = self._transport.submit(
            request=frozen,
            outcome=self.scripted_outcome,
            crash_after_accept_before_receipt=crash_after_fake_accept_before_receipt,
        )
        result = FakeTelegramAdapterResult(
            status=receipt.status,
            request_id=frozen.request_id,
            idempotency_key=key,
            receipt=receipt,
            failure_code="TRANSPORT_REJECTED" if receipt.status == STATUS_REJECTED else None,
            replayed=receipt.replayed,
            transport_submit_called=True,
        )
        self._records[key] = _AdapterRecord(fingerprint, result)
        return result

    def query_status(self, *, idempotency_key: str) -> FakeSubmissionReceipt | None:
        """Read-only fake status surface; does not reconcile or submit."""
        return self._transport.query(idempotency_key=idempotency_key)

    def replay(self, *, request: object) -> FakeTelegramAdapterResult | None:
        """Return stored fake outcome without calling submit."""
        if not isinstance(request, ContactMessageActionRequest):
            raise FakeTelegramAdapterError("replay accepts only a typed CT0-6 Message Action request")
        try:
            frozen = self._detached_request(request)
        except FakeTelegramAdapterError:
            return None
        if frozen.recipient_ref not in self._allowed_recipients or frozen.channel not in self._allowed_channels:
            return None
        record = self._records.get(frozen.idempotency_key)
        if record is None or record.request_fingerprint != self._fingerprint(frozen):
            return None
        if record.result.status == STATUS_CONFLICT:
            return record.result
        receipt = record.result.receipt
        if receipt is not None:
            observed = self._transport.query(idempotency_key=frozen.idempotency_key)
            if observed is not None:
                receipt = observed
        return FakeTelegramAdapterResult(
            status=receipt.status if receipt else record.result.status,
            request_id=frozen.request_id,
            idempotency_key=frozen.idempotency_key,
            receipt=receipt,
            failure_code=record.result.failure_code,
            replayed=True,
            transport_submit_called=False,
        )


__all__ = [
    "FakeTelegramAdapter", "FakeTelegramAdapterError", "FakeTelegramAdapterResult",
    "STATUS_BLOCKED", "STATUS_CRASHED_BEFORE_SUBMIT",
]
