"""Receipt ingress and acceptance authority; deliberately owns no transport action."""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import threading
from typing import Protocol

from contact_message_action_model import ContactMessageActionRequest, sha256_text
from contact_receipt_model import ContactTransportReceiptEvidence, ReceiptSupersession, stable_hash
from contact_receipt_store import ContactReceiptStore
from recording_telegram_transport import (FakeSubmissionReceipt, STATUS_ACKNOWLEDGED,
    STATUS_DELIVERED, STATUS_FAILED_BEFORE_ACCEPT, STATUS_SUBMITTED, STATUS_UNKNOWN)


class ReceiptAuthorityError(ValueError):
    """Untrusted or mismatched transport evidence refused fail-closed."""


class QueryOnlyTransport(Protocol):
    def query(self, *, idempotency_key: str) -> FakeSubmissionReceipt | None: ...


class ContactReceiptAuthority:
    """Validates, accepts and deduplicates receipt observations; no submit/send method."""
    def __init__(self, *, store: ContactReceiptStore, authority_ref: str = "rauth:ct0-8") -> None:
        if not isinstance(store, ContactReceiptStore):
            raise ReceiptAuthorityError("authority requires ContactReceiptStore")
        if not isinstance(authority_ref, str) or not authority_ref.startswith("rauth:"):
            raise ReceiptAuthorityError("authority_ref must use rauth:")
        self.store = store
        self.authority_ref = authority_ref
        self._actions: dict[str, ContactMessageActionRequest] = {}
        self._lock = threading.RLock()
        self.accepted_count = 0
        self.rejected_count = 0

    def register_action(self, *, action_ref: str, request: ContactMessageActionRequest) -> None:
        if not isinstance(action_ref, str) or not action_ref.startswith("actn:"):
            raise ReceiptAuthorityError("action_ref must be explicit actn: reference")
        frozen = self._frozen_request(request)
        with self._lock:
            prior = self._actions.get(action_ref)
            if prior is not None and prior != frozen:
                raise ReceiptAuthorityError("action is already bound to a different request")
            if any(item.idempotency_key == frozen.idempotency_key and key != action_ref
                   for key, item in self._actions.items()):
                raise ReceiptAuthorityError("submission key already belongs to another action")
            self._actions[action_ref] = frozen

    @staticmethod
    def _frozen_request(request: object) -> ContactMessageActionRequest:
        if type(request) is not ContactMessageActionRequest:
            raise ReceiptAuthorityError("ingress requires typed frozen CT0-6 request")
        try:
            return ContactMessageActionRequest.from_mapping(request.to_dict())
        except Exception as exc:
            raise ReceiptAuthorityError("CT0-6 request failed detached validation") from exc

    def observe(self, *, action_ref: str, request: ContactMessageActionRequest,
                observation: FakeSubmissionReceipt, transport: QueryOnlyTransport,
                observed_at: str | None = None, recorded_at: str | None = None,
                transport_record_ref: str | None = None, crash_after_journal_append: bool = False
                ) -> ContactTransportReceiptEvidence:
        with self._lock:
            try:
                evidence = self._build(action_ref=action_ref, request=request, observation=observation,
                    transport=transport, observed_at=observed_at, recorded_at=recorded_at,
                    transport_record_ref=transport_record_ref)
                self.store.append(evidence, crash_after_journal_append=crash_after_journal_append)
                canonical = self.store.find(evidence.evidence_ref)
                if canonical is None:
                    raise ReceiptAuthorityError("receipt append completed without a recoverable canonical row")
                self.accepted_count += 1
                return canonical
            except Exception:
                self.rejected_count += 1
                raise

    def _build(self, *, action_ref: str, request: ContactMessageActionRequest,
               observation: FakeSubmissionReceipt, transport: QueryOnlyTransport,
               observed_at: str | None, recorded_at: str | None,
               transport_record_ref: str | None) -> ContactTransportReceiptEvidence:
        if not isinstance(action_ref, str) or not action_ref.startswith("actn:"):
            raise ReceiptAuthorityError("explicit action_ref must use actn:")
        frozen = self._frozen_request(request)
        registered = self._actions.get(action_ref)
        if registered is None or registered != frozen:
            raise ReceiptAuthorityError("unknown action/request binding")
        if type(observation) is not FakeSubmissionReceipt:
            raise ReceiptAuthorityError("only an injected CT0-7 fake receipt is accepted")
        if observation.request_id != frozen.request_id or observation.idempotency_key != frozen.idempotency_key:
            raise ReceiptAuthorityError("fake receipt request/key mismatch")
        if not callable(getattr(transport, "query", None)):
            raise ReceiptAuthorityError("query-only transport observation is required")
        queried = transport.query(idempotency_key=frozen.idempotency_key)
        if not isinstance(queried, FakeSubmissionReceipt) or replace(queried, replayed=False) != replace(observation, replayed=False):
            raise ReceiptAuthorityError("transport.query(key) is absent or not equal to injected observation")
        status = observation.status
        mapping = {STATUS_SUBMITTED: "SUBMIT_OBSERVED", STATUS_ACKNOWLEDGED: "PROVIDER_ACCEPTED",
                   STATUS_DELIVERED: "DELIVERY_CONFIRMED", STATUS_UNKNOWN: "STATUS_UNKNOWN"}
        if status == STATUS_FAILED_BEFORE_ACCEPT:
            if observation.effect_count_for_key != 0 or observation.effect_applied:
                raise ReceiptAuthorityError("pre-accept failure requires zero effects")
            kind = "FAILED_BEFORE_ACCEPT"
        elif status in mapping:
            kind = mapping[status]
        else:
            raise ReceiptAuthorityError("unknown/rejected/conflicting fake transport status")
        if type(observation.effect_applied) is not bool or type(observation.effect_count_for_key) is not int or observation.effect_count_for_key < 0:
            raise ReceiptAuthorityError("invalid fake effect fields")
        if any(type(value) is not bool for value in (observation.acknowledged, observation.delivered)):
            raise ReceiptAuthorityError("fake acknowledgement/delivery flags must be booleans")
        if status in (STATUS_SUBMITTED, STATUS_ACKNOWLEDGED, STATUS_DELIVERED, STATUS_UNKNOWN):
            if observation.effect_count_for_key < 1 or not observation.effect_applied:
                raise ReceiptAuthorityError("accepted statuses require a recorded fake effect")
        if status == STATUS_SUBMITTED and (observation.acknowledged or observation.delivered):
            raise ReceiptAuthorityError("submitted observation cannot claim provider acceptance")
        if status == STATUS_ACKNOWLEDGED and (not observation.acknowledged or observation.delivered):
            raise ReceiptAuthorityError("ACK observation has inconsistent fake flags")
        if status == STATUS_DELIVERED and (not observation.acknowledged or not observation.delivered):
            raise ReceiptAuthorityError("delivery observation has inconsistent fake flags")
        if status in (STATUS_UNKNOWN, STATUS_FAILED_BEFORE_ACCEPT) and (observation.acknowledged or observation.delivered):
            raise ReceiptAuthorityError("unknown/pre-accept status cannot assert provider state")
        if observation.read_status != "READ_UNKNOWN":
            raise ReceiptAuthorityError("CT0-7 transport cannot establish read status")
        expected_record_ref = f"tlog:{stable_hash((frozen.idempotency_key, status, observation.effect_count_for_key))}"
        record_ref = transport_record_ref or expected_record_ref
        if record_ref != expected_record_ref:
            raise ReceiptAuthorityError("transport_record_ref does not match queried transport observation")
        if not isinstance(observed_at, str):
            observed_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        if not isinstance(recorded_at, str):
            recorded_at = observed_at
        return ContactTransportReceiptEvidence.create(
            action_ref=action_ref, submission_key=frozen.idempotency_key, namespace="sandbox.telegram",
            transport_record_ref=record_ref, observation_kind=kind, provider_status=status,
            recipient_ref=frozen.recipient_ref, channel=frozen.channel,
            payload_hash=sha256_text(frozen.payload_json), content_hash=frozen.content_hash,
            provider_message_ref=None, provider_time=None, observed_at=observed_at,
            recorded_at=recorded_at, effect_applied=observation.effect_applied,
            effect_count_for_key=observation.effect_count_for_key,
            provenance_refs=tuple(dict.fromkeys((*frozen.source_refs, frozen.request_id, frozen.payload_ref))))

    def correct(self, *, action_ref: str, request: ContactMessageActionRequest,
                observation: FakeSubmissionReceipt, transport: QueryOnlyTransport, old_evidence_ref: str,
                reason: str, recorded_at: str, observed_at: str | None = None
                ) -> tuple[ContactTransportReceiptEvidence, ReceiptSupersession]:
        old = self.store.find(old_evidence_ref)
        if old is None or old.action_ref != action_ref:
            raise ReceiptAuthorityError("correction target does not belong to action")
        new = self.observe(action_ref=action_ref, request=request, observation=observation,
                           transport=transport, observed_at=observed_at, recorded_at=recorded_at)
        item = ReceiptSupersession.create(action_ref=action_ref, old_evidence_ref=old.evidence_ref,
            new_evidence_ref=new.evidence_ref, reason=reason, authority_ref=self.authority_ref,
            recorded_at=recorded_at)
        self.store.add_supersession(item)
        return new, item


__all__ = ["ContactReceiptAuthority", "ReceiptAuthorityError", "QueryOnlyTransport"]
