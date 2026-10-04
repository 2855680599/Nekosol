"""Evidence-backed candidate settlement sent only to an injected recording port.

AR1_CANONICAL_ADAPTER NOT INTEGRATED. This module is a sandbox fixture and is
not a second Action Reality owner or canonical settlement implementation.
"""
from __future__ import annotations

from dataclasses import dataclass
import threading

from contact_reconciliation import ContactReconciliationRecord
from contact_settlement_v2 import ContactSettlementRequestV2
from contact_receipt_model import stable_hash
from contact_receipt_store import ContactReceiptStore

SETTLEMENT_CANDIDATES = frozenset({"UNRESOLVED", "SUCCESS_EFFECT", "FAILURE_NO_EFFECT", "UNKNOWN_EFFECT", "CONFLICT"})


@dataclass(frozen=True, slots=True)
class ContactSettlementRequest:
    settlement_request_ref: str
    action_ref: str
    evidence_fingerprint: str
    candidate: str
    supporting_evidence_refs: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.settlement_request_ref.startswith("csreq:") or not self.action_ref.startswith("actn:"):
            raise ValueError("invalid settlement request/action ref")
        if self.candidate not in SETTLEMENT_CANDIDATES or not self.supporting_evidence_refs:
            raise ValueError("settlement candidate requires evidence support")
        if tuple(sorted(set(self.supporting_evidence_refs))) != self.supporting_evidence_refs:
            raise ValueError("settlement support refs must be sorted and unique")
        expected = stable_hash((self.action_ref, self.evidence_fingerprint, self.candidate,
                                self.supporting_evidence_refs))
        if self.settlement_request_ref != f"csreq:{expected}":
            raise ValueError("settlement request identity mismatch")

    @classmethod
    def create(cls, *, action_ref: str, evidence_fingerprint: str, candidate: str,
               supporting_evidence_refs: tuple[str, ...]) -> "ContactSettlementRequest":
        digest = stable_hash((action_ref, evidence_fingerprint, candidate, supporting_evidence_refs))
        return cls(f"csreq:{digest}", action_ref, evidence_fingerprint, candidate, supporting_evidence_refs)


@dataclass(frozen=True, slots=True)
class RecordingSettlementReceipt:
    settlement_ref: str
    request_ref: str
    candidate: str
    replayed: bool


class RecordingSettlementPort:
    """Idempotent local fake; never persists outside this process."""
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._receipts: dict[str, RecordingSettlementReceipt] = {}
        self.accept_count = 0

    def accept(self, *, request: ContactSettlementRequest) -> RecordingSettlementReceipt:
        if type(request) not in (ContactSettlementRequest, ContactSettlementRequestV2):
            raise TypeError("recording settlement port accepts typed evidence-backed requests only")
        with self._lock:
            prior = self._receipts.get(request.settlement_request_ref)
            if prior is not None:
                return RecordingSettlementReceipt(prior.settlement_ref, prior.request_ref, prior.candidate, True)
            result = RecordingSettlementReceipt(
                settlement_ref=f"csettle:{stable_hash((request.settlement_request_ref, request.candidate))}",
                request_ref=request.settlement_request_ref, candidate=request.candidate, replayed=False)
            self._receipts[request.settlement_request_ref] = result
            self.accept_count += 1
            return result

    def query(self, request_ref: str) -> RecordingSettlementReceipt | None:
        with self._lock:
            return self._receipts.get(request_ref)


class ContactSettlementBridge:
    def __init__(self, *, store: ContactReceiptStore, port: RecordingSettlementPort) -> None:
        if not isinstance(port, RecordingSettlementPort):
            raise TypeError("settlement must use injected RecordingSettlementPort")
        self.store = store
        self.port = port
        self._lock = threading.RLock()
        self._local: dict[str, RecordingSettlementReceipt] = {}
        self.local_commit_count = 0

    def settle(self, *, reconciliation: ContactReconciliationRecord,
               crash_after_accept_before_local_commit: bool = False) -> RecordingSettlementReceipt:
        if not isinstance(reconciliation, ContactReconciliationRecord):
            raise TypeError("typed reconciliation required")
        if not reconciliation.supporting_evidence_refs:
            raise ValueError("cannot settle without accepted receipt evidence")
        supersessions = tuple(item for item in self.store.supersessions if item.action_ref == reconciliation.action_ref)
        superseded = {item.old_evidence_ref for item in supersessions}
        current_refs = tuple(sorted(item.evidence_ref for item in self.store.by_action(reconciliation.action_ref)
                                    if item.evidence_ref not in superseded))
        current_supersession_refs = tuple(sorted(item.supersession_ref for item in supersessions))
        if (reconciliation.supporting_evidence_refs != current_refs
                or reconciliation.supersession_refs != current_supersession_refs
                or reconciliation.evidence_fingerprint != stable_hash((current_refs, current_supersession_refs))):
            raise ValueError("settlement reconciliation is stale or has invalid evidence fingerprint")
        evidence = tuple(self.store.find(ref) for ref in current_refs)
        if any(item is None for item in evidence):
            raise ValueError("settlement evidence disappeared from receipt store")
        kinds = {item.observation_kind for item in evidence if item is not None}
        preaccept_conflicts = {"SUBMIT_OBSERVED", "PROVIDER_ACCEPTED", "DELIVERY_CONFIRMED",
                               "DELIVERY_FAILED", "READ_CONFIRMED", "STATUS_UNKNOWN", "TRANSPORT_ERROR"}
        positive_delivery = {"DELIVERY_CONFIRMED", "READ_CONFIRMED"}
        if "FAILED_BEFORE_ACCEPT" in kinds and kinds & preaccept_conflicts:
            expected_state = "CONFLICT"
        elif kinds & positive_delivery and "DELIVERY_FAILED" in kinds:
            expected_state = "CONFLICT"
        elif kinds & positive_delivery:
            expected_state = "DELIVERED"
        elif kinds & {"DELIVERY_FAILED", "STATUS_UNKNOWN", "TRANSPORT_ERROR"}:
            expected_state = "UNKNOWN"
        elif "FAILED_BEFORE_ACCEPT" in kinds:
            expected_state = "FAILURE"
        elif "PROVIDER_ACCEPTED" in kinds:
            expected_state = "ACKNOWLEDGED"
        elif "SUBMIT_OBSERVED" in kinds:
            expected_state = "SUBMITTED"
        else:
            expected_state = "UNKNOWN"
        if expected_state != reconciliation.state:
            raise ValueError("settlement state is not supported by current receipt evidence")
        failure_no_effect = (kinds == {"FAILED_BEFORE_ACCEPT"} and all(
            item is not None and item.provider_status == "FAILED_BEFORE_ACCEPT"
            and item.effect_applied is False and item.effect_count_for_key == 0 for item in evidence))
        candidate = {"UNKNOWN": "UNKNOWN_EFFECT", "SUBMITTED": "UNKNOWN_EFFECT",
                     "ACKNOWLEDGED": "UNRESOLVED", "DELIVERED": "SUCCESS_EFFECT",
                     "FAILURE": "FAILURE_NO_EFFECT" if failure_no_effect else "UNKNOWN_EFFECT",
                     "CONFLICT": "CONFLICT"}[reconciliation.state]
        request = ContactSettlementRequest.create(
            action_ref=reconciliation.action_ref, evidence_fingerprint=reconciliation.evidence_fingerprint,
            candidate=candidate, supporting_evidence_refs=reconciliation.supporting_evidence_refs)
        with self._lock:
            prior = self._local.get(request.settlement_request_ref)
            if prior is not None:
                return RecordingSettlementReceipt(prior.settlement_ref, prior.request_ref, prior.candidate, True)
            accepted = self.port.query(request.settlement_request_ref)
            was_already_accepted = accepted is not None
            if accepted is None:
                accepted = self.port.accept(request=request)
            elif accepted.candidate != candidate or accepted.request_ref != request.settlement_request_ref:
                raise ValueError("recording settlement receipt does not match typed request")
            if crash_after_accept_before_local_commit:
                raise RuntimeError("synthetic crash after settlement accept before local commit")
            self._local[request.settlement_request_ref] = accepted
            self.local_commit_count += 1
            return RecordingSettlementReceipt(accepted.settlement_ref, accepted.request_ref, accepted.candidate,
                                              accepted.replayed or was_already_accepted)

    def recover(self, *, reconciliation: ContactReconciliationRecord) -> RecordingSettlementReceipt:
        return self.settle(reconciliation=reconciliation)


__all__ = ["ContactSettlementBridge", "ContactSettlementRequest", "RecordingSettlementPort",
           "RecordingSettlementReceipt"]
