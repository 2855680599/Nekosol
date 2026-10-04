"""Immutable reconciliation records derived from accepted evidence only."""
from __future__ import annotations

from dataclasses import dataclass
import threading
from typing import Iterable

from contact_receipt_model import ContactTransportReceiptEvidence, stable_hash
from contact_receipt_store import ContactReceiptStore

STATES = frozenset({"UNKNOWN", "SUBMITTED", "ACKNOWLEDGED", "DELIVERED", "FAILURE", "CONFLICT"})
REASONS = frozenset({"UNKNOWN_BEFORE_SUBMIT", "UNKNOWN_AFTER_SUBMIT", "UNKNOWN_AFTER_ACCEPT",
                     "UNKNOWN_AFTER_DELIVERY", "UNKNOWN_PROVIDER_STATE", "RECOVERY_GAP",
                     "EVIDENCE_OBSERVED", "CONTRADICTORY_EVIDENCE", "STILL_UNKNOWN", "CORRECTED"})


@dataclass(frozen=True, slots=True)
class ContactReconciliationRecord:
    reconciliation_ref: str
    action_ref: str
    evidence_fingerprint: str
    state: str
    reason: str
    supporting_evidence_refs: tuple[str, ...]
    supersession_refs: tuple[str, ...]
    recorded_at: str

    def __post_init__(self) -> None:
        if not self.reconciliation_ref.startswith("recon:") or not self.action_ref.startswith("actn:"):
            raise ValueError("invalid reconciliation/action reference")
        if self.state not in STATES or self.reason not in REASONS:
            raise ValueError("invalid reconciliation state or reason")
        if tuple(sorted(set(self.supporting_evidence_refs))) != self.supporting_evidence_refs:
            raise ValueError("support refs must be sorted and unique")
        if tuple(sorted(set(self.supersession_refs))) != self.supersession_refs:
            raise ValueError("supersession refs must be sorted and unique")
        expected = stable_hash((self.action_ref, self.evidence_fingerprint, self.state, self.reason,
                                self.supporting_evidence_refs, self.supersession_refs))
        if self.reconciliation_ref != f"recon:{expected}":
            raise ValueError("reconciliation identity mismatch")


class ContactReconciliationAuthority:
    """Reconcile accepted receipt facts. Has no retry, transport, or action-creation API."""
    def __init__(self, *, store: ContactReceiptStore) -> None:
        self.store = store
        self._lock = threading.RLock()
        self._records: list[ContactReconciliationRecord] = []
        self._by_identity: dict[tuple[str, str], ContactReconciliationRecord] = {}
        self.append_count = 0

    @property
    def records(self) -> tuple[ContactReconciliationRecord, ...]:
        with self._lock:
            return tuple(self._records)

    def build(self, *, action_ref: str, recorded_at: str,
              reason: str | None = None) -> ContactReconciliationRecord:
        """Pure projection of the reconciliation for `action_ref`; mutates nothing.

        Returns the already-indexed record when this evidence fingerprint was
        reconciled before. Callers append the returned record to the canonical
        journal and only then `attach()` it, so a rolled-back transaction can
        never leave this authority ahead of the journal.
        """
        evidence = self.store.by_action(action_ref)
        supersessions = tuple(item for item in self.store.supersessions if item.action_ref == action_ref)
        superseded = {item.old_evidence_ref for item in supersessions}
        current = tuple(item for item in evidence if item.evidence_ref not in superseded)
        refs = tuple(sorted(item.evidence_ref for item in current))
        supersession_refs = tuple(sorted(item.supersession_ref for item in supersessions))
        fingerprint = stable_hash((refs, supersession_refs))
        state, inferred_reason = self._project(current, bool(supersessions))
        chosen_reason = reason or inferred_reason
        if chosen_reason not in REASONS:
            raise ValueError("unsupported reconciliation reason")
        key = (action_ref, fingerprint)
        with self._lock:
            prior = self._by_identity.get(key)
        if prior is not None:
            return prior
        material = (action_ref, fingerprint, state, chosen_reason, refs, supersession_refs)
        return ContactReconciliationRecord(
            reconciliation_ref=f"recon:{stable_hash(material)}", action_ref=action_ref,
            evidence_fingerprint=fingerprint, state=state, reason=chosen_reason,
            supporting_evidence_refs=refs, supersession_refs=supersession_refs, recorded_at=recorded_at,
        )

    def attach(self, record: ContactReconciliationRecord) -> bool:
        """Index an already-journaled reconciliation record (replay/restore path)."""
        if not isinstance(record, ContactReconciliationRecord):
            raise TypeError("only ContactReconciliationRecord can be attached")
        key = (record.action_ref, record.evidence_fingerprint)
        with self._lock:
            if key in self._by_identity:
                raise ValueError("duplicate reconciliation journal identity")
            self._records.append(record)
            self._by_identity[key] = record
            self.append_count += 1
            return True

    def reconcile(self, *, action_ref: str, recorded_at: str,
                  reason: str | None = None) -> ContactReconciliationRecord:
        """Project and index a reconciliation in one step (in-memory contract)."""
        record = self.build(action_ref=action_ref, recorded_at=recorded_at, reason=reason)
        key = (record.action_ref, record.evidence_fingerprint)
        with self._lock:
            prior = self._by_identity.get(key)
        if prior is not None:
            return prior
        self.attach(record)
        return record

    def knows(self, *, action_ref: str, fingerprint: str) -> bool:
        """True when this evidence fingerprint is already indexed for the action."""
        with self._lock:
            return (action_ref, fingerprint) in self._by_identity

    def replay(self) -> tuple[ContactReconciliationRecord, ...]:
        with self._lock:
            return tuple(self._records)

    def load(self, records: Iterable[ContactReconciliationRecord]) -> None:
        with self._lock:
            self._records.clear()
            self._by_identity.clear()
            for record in records:
                key = (record.action_ref, record.evidence_fingerprint)
                if key in self._by_identity:
                    raise ValueError("duplicate reconciliation journal identity")
                self._records.append(record)
                self._by_identity[key] = record
            self.append_count = len(self._records)

    @staticmethod
    def _project(evidence: tuple[ContactTransportReceiptEvidence, ...],
                 corrected: bool) -> tuple[str, str]:
        return ContactReconciliationAuthority._project_kinds(
            frozenset(item.observation_kind for item in evidence), corrected)

    @staticmethod
    def _project_kinds(kinds: frozenset[str], corrected: bool) -> tuple[str, str]:
        """Project a delivery state from the *set* of active observation kinds.

        The only thing the projection reads is which kinds are present and
        whether the action has been corrected; keeping that as the sole input
        lets an incremental store maintain the kind set in O(1) per event
        instead of re-scanning the action's whole evidence history.
        """
        preaccept_conflicts = {"SUBMIT_OBSERVED", "PROVIDER_ACCEPTED", "DELIVERY_CONFIRMED",
                               "DELIVERY_FAILED", "READ_CONFIRMED", "STATUS_UNKNOWN", "TRANSPORT_ERROR"}
        positive_delivery = {"DELIVERY_CONFIRMED", "READ_CONFIRMED"}
        if "FAILED_BEFORE_ACCEPT" in kinds and kinds & preaccept_conflicts:
            return "CONFLICT", "CONTRADICTORY_EVIDENCE"
        if kinds & positive_delivery and "DELIVERY_FAILED" in kinds:
            return "CONFLICT", "CONTRADICTORY_EVIDENCE"
        # Delivery/read facts resolve UNKNOWN; uncertain negative observations dominate ACK/SUBMITTED.
        if kinds & positive_delivery:
            return "DELIVERED", "CORRECTED" if corrected else "EVIDENCE_OBSERVED"
        if "DELIVERY_FAILED" in kinds or "STATUS_UNKNOWN" in kinds or "TRANSPORT_ERROR" in kinds:
            return "UNKNOWN", "UNKNOWN_PROVIDER_STATE"
        if "FAILED_BEFORE_ACCEPT" in kinds:
            return "FAILURE", "CORRECTED" if corrected else "EVIDENCE_OBSERVED"
        if "PROVIDER_ACCEPTED" in kinds:
            return "ACKNOWLEDGED", "CORRECTED" if corrected else "EVIDENCE_OBSERVED"
        if "SUBMIT_OBSERVED" in kinds:
            return "SUBMITTED", "CORRECTED" if corrected else "EVIDENCE_OBSERVED"
        return "UNKNOWN", "RECOVERY_GAP" if not kinds else "STILL_UNKNOWN"


__all__ = ["ContactReconciliationAuthority", "ContactReconciliationRecord", "REASONS", "STATES"]
