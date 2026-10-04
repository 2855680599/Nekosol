"""Read-only, support-bound facts projection for a contact action."""
from __future__ import annotations

from dataclasses import dataclass, field

from contact_reconciliation import ContactReconciliationAuthority, ContactReconciliationRecord
from contact_receipt_model import stable_hash
from contact_receipt_store import ContactReceiptStore
from contact_reconciliation_v2 import ReconciliationView

_PROJECTION_SUPPORT_TOKEN = object()


@dataclass(frozen=True, slots=True)
class ContactActionResultProjection:
    action_ref: str
    current_state: str
    submitted: bool | None
    acknowledged: bool | None
    delivered: bool | None
    read_status: str
    failure: bool | None
    unknown: bool
    supporting_evidence_refs: tuple[str, ...]
    conflict_refs: tuple[str, ...]
    last_reconciled_ref: str | None
    evidence_fingerprint: str
    _support_token: object = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._support_token is not _PROJECTION_SUPPORT_TOKEN:
            raise ValueError("result projections are constructed only by the trusted projector")
        if not self.action_ref.startswith("actn:"):
            raise ValueError("projection requires action reference")
        if self.current_state not in {"UNKNOWN", "SUBMITTED", "ACKNOWLEDGED", "DELIVERED", "FAILURE", "CONFLICT"}:
            raise ValueError("invalid projection state")
        if not self.supporting_evidence_refs and self.current_state != "UNKNOWN":
            raise ValueError("a result cannot claim state without evidence")
        if self.read_status not in {"CONFIRMED", "NOT_CONFIRMED", "UNKNOWN"}:
            raise ValueError("invalid tri-state read status")
        if self.delivered is True and self.current_state not in {"DELIVERED", "CONFLICT"}:
            raise ValueError("delivery claim lacks delivery state")


class ContactActionResultProjector:
    def __init__(self, *, store: ContactReceiptStore) -> None:
        self.store = store

    def project(self, *, action_ref: str,
                reconciliations: tuple[ContactReconciliationRecord, ...] | list[ContactReconciliationRecord]) -> ContactActionResultProjection:
        if not isinstance(action_ref, str) or not action_ref.startswith("actn:"):
            raise ValueError("projection requires explicit action reference")
        evidence = self.store.by_action(action_ref)
        supersessions = tuple(item for item in self.store.supersessions if item.action_ref == action_ref)
        superseded = {item.old_evidence_ref for item in supersessions}
        current = tuple(item for item in evidence if item.evidence_ref not in superseded)
        refs = tuple(sorted(item.evidence_ref for item in current))
        supersession_refs = tuple(sorted(item.supersession_ref for item in supersessions))
        fingerprint = stable_hash((refs, supersession_refs))
        relevant = [item for item in reconciliations if item.action_ref == action_ref]
        record = relevant[-1] if relevant else None
        if record is not None:
            if type(record) is ContactReconciliationRecord:
                contract_version = "v1"
            elif type(record) is ReconciliationView:
                contract_version = record.contract_version
            else:
                raise TypeError("projection accepts only typed reconciliation records or read views")
            if (record.supporting_evidence_refs != refs or record.supersession_refs != supersession_refs
                    or record.evidence_fingerprint != fingerprint):
                raise ValueError("reconciliation record is stale or its evidence binding is invalid")
            expected_state, _ = ContactReconciliationAuthority._project(current, bool(supersessions))
            if record.state != expected_state:
                raise ValueError("reconciliation state is not supported by current receipt evidence")
            state = record.state
        else:
            state = "UNKNOWN"

        kinds = {item.observation_kind for item in current}
        positive_delivery = {"DELIVERY_CONFIRMED", "READ_CONFIRMED"}
        if kinds & positive_delivery and ("DELIVERY_FAILED" in kinds or "FAILED_BEFORE_ACCEPT" in kinds):
            state = "CONFLICT"
        submitted = True if any(item.effect_applied for item in current) else (
            False if current and all(item.observation_kind == "FAILED_BEFORE_ACCEPT"
                                     and not item.effect_applied and item.effect_count_for_key == 0
                                     for item in current) else None)
        acknowledged = True if kinds & {"PROVIDER_ACCEPTED", "DELIVERY_CONFIRMED", "READ_CONFIRMED"} else (
            False if current and all(item.observation_kind == "FAILED_BEFORE_ACCEPT" for item in current) else None)
        delivered = True if kinds & positive_delivery else (
            False if "FAILED_BEFORE_ACCEPT" in kinds and all(
                item.observation_kind == "FAILED_BEFORE_ACCEPT" and not item.effect_applied
                and item.effect_count_for_key == 0 for item in current) else None)
        read_status = "CONFIRMED" if "READ_CONFIRMED" in kinds else "UNKNOWN"
        failure = True if state == "FAILURE" else (False if state == "DELIVERED" else None)
        conflict_refs = tuple(sorted(item.evidence_ref for item in current
                                     if item.observation_kind in {"DELIVERY_CONFIRMED", "READ_CONFIRMED",
                                                                  "DELIVERY_FAILED", "FAILED_BEFORE_ACCEPT"})) if state == "CONFLICT" else ()
        return ContactActionResultProjection(
            action_ref=action_ref, current_state=state,
            submitted=submitted, acknowledged=acknowledged, delivered=delivered,
            read_status=read_status, failure=failure, unknown=state == "UNKNOWN",
            supporting_evidence_refs=refs, conflict_refs=conflict_refs,
            last_reconciled_ref=record.reconciliation_ref if record else None,
            evidence_fingerprint=record.evidence_fingerprint if record else "",
            _support_token=_PROJECTION_SUPPORT_TOKEN,
        )

    def handoff_facts(self, projection: ContactActionResultProjection) -> dict[str, object]:
        if not isinstance(projection, ContactActionResultProjection):
            raise TypeError("handoff requires a read-only result projection")
        return {"action_ref": projection.action_ref, "state": projection.current_state,
                "submitted": projection.submitted, "acknowledged": projection.acknowledged,
                "delivered": projection.delivered, "read_status": projection.read_status,
                "failure": projection.failure, "unknown": projection.unknown,
                "evidence_refs": projection.supporting_evidence_refs,
                "conflict_refs": projection.conflict_refs}


__all__ = ["ContactActionResultProjection", "ContactActionResultProjector"]
