"""Narrow future canonical adapter contracts; no owner is imported or invoked."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class Ar0ActionSnapshot:
    action_ref: str
    submission_key: str
    payload_hash: str
    content_hash: str
    recipient_ref: str
    channel_ref: str
    revision: int
    status: str

    def __post_init__(self) -> None:
        if not self.action_ref.startswith("actn:") or not self.submission_key.startswith("ar0_act:"):
            raise ValueError("invalid canonical action/submission reference")
        if self.revision < 1 or self.status not in {"SUBMITTED", "UNKNOWN", "ACKNOWLEDGED", "DELIVERED", "FAILED", "CONFLICT"}:
            raise ValueError("invalid action revision/status")
        for value in (self.payload_hash, self.content_hash):
            if len(value) != 64 or any(ch not in "0123456789abcdef" for ch in value):
                raise ValueError("action hashes must be lowercase SHA-256")


@dataclass(frozen=True, slots=True)
class SettlementEvent:
    outbox_id: str
    action_ref: str
    expected_revision: int
    result_ref: str
    state: str
    evidence_refs: tuple[str, ...]
    payload_hash: str

    def __post_init__(self) -> None:
        if not self.outbox_id.startswith("settlement:") or not self.action_ref.startswith("actn:"):
            raise ValueError("invalid settlement identity")
        if self.expected_revision < 1 or not self.result_ref.startswith("recon:"):
            raise ValueError("invalid settlement revision/result ref")
        if self.state not in {"UNKNOWN", "SUBMITTED", "ACKNOWLEDGED", "DELIVERED", "FAILURE", "CONFLICT"}:
            raise ValueError("invalid settlement state")
        if tuple(sorted(set(self.evidence_refs))) != self.evidence_refs:
            raise ValueError("evidence refs must be canonical sorted unique refs")
        if len(self.payload_hash) != 64:
            raise ValueError("payload hash must be SHA-256")


@dataclass(frozen=True, slots=True)
class OwnerReceipt:
    outbox_id: str
    outcome: str
    receipt_ref: str
    observed_revision: int

    def __post_init__(self) -> None:
        if self.outcome not in {"ACCEPT", "IGNORE", "DEFER", "REJECT", "CONFLICT"}:
            raise ValueError("unsupported owner outcome")
        if not self.outbox_id.startswith("settlement:") or not self.receipt_ref.startswith("ccons:"):
            raise ValueError("invalid owner receipt identity")
        if self.observed_revision < 0:
            raise ValueError("observed revision cannot be negative")


@dataclass(frozen=True, slots=True)
class ContactIntentResultEvent:
    intent_ref: str
    action_ref: str
    settled_result_ref: str
    delivery_status: str
    evidence_refs: tuple[str, ...]
    causal_refs: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.intent_ref.startswith("cintent:") or not self.action_ref.startswith("actn:"):
            raise ValueError("invalid intent/action reference")
        if not self.settled_result_ref.startswith("recon:"):
            raise ValueError("invalid settled result reference")
        if self.delivery_status not in {"UNKNOWN", "SUBMITTED", "ACKNOWLEDGED", "DELIVERED", "FAILURE", "CONFLICT"}:
            raise ValueError("invalid delivery status")
        for refs in (self.evidence_refs, self.causal_refs):
            if tuple(sorted(set(refs))) != refs:
                raise ValueError("event references must be canonical sorted unique")


@dataclass(frozen=True, slots=True)
class ExperienceHandoffEvent:
    action_ref: str
    result_ref: str
    truthful_statement: str
    evidence_refs: tuple[str, ...]
    causal_refs: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.action_ref.startswith("actn:") or not self.result_ref.startswith("recon:"):
            raise ValueError("invalid experience handoff identity")
        if not self.truthful_statement.strip():
            raise ValueError("handoff statement must be explicit")
        if self.delivery_status_claims_sent and self.state_is_unknown:
            raise ValueError("UNKNOWN cannot be phrased as confirmed sent")

    @property
    def delivery_status_claims_sent(self) -> bool:
        text = self.truthful_statement.casefold()
        return "sent" in text or "delivered" in text

    @property
    def state_is_unknown(self) -> bool:
        text = self.truthful_statement.casefold()
        return "unknown" in text or "unresolved" in text


class Ar0ActionReadPort(Protocol):
    def read_action(self, action_ref: str) -> Ar0ActionSnapshot | None: ...


class Ar1SettlementPort(Protocol):
    def deliver_settlement(self, event: SettlementEvent) -> OwnerReceipt: ...
    def query_settlement(self, outbox_id: str) -> OwnerReceipt | None: ...


class ContactIntentOwnerPort(Protocol):
    def record_result(self, event: ContactIntentResultEvent) -> OwnerReceipt: ...


class ExperienceHandoffPort(Protocol):
    def publish_handoff(self, event: ExperienceHandoffEvent) -> OwnerReceipt: ...


__all__ = ["Ar0ActionSnapshot", "SettlementEvent", "OwnerReceipt", "ContactIntentResultEvent",
    "ExperienceHandoffEvent", "Ar0ActionReadPort", "Ar1SettlementPort",
    "ContactIntentOwnerPort", "ExperienceHandoffPort"]
