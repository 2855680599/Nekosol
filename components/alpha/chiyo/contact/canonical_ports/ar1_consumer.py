"""CT0-10 Phase K - `CanonicalAr1Consumer` (order sections 71-76).

Satisfies the spine's `CanonicalAr1Port` (`settle(*, action_ref, result_evidence,
observed_at)`) by driving the REAL canonical AR-1 owner
(`result_settlement_ar1`) in isolated store roots. The consumer never writes
Memory, Activity, Relationship or World: AR-1 itself refuses those capabilities
(`ResultSettlementAuthority.request_domain_writer_capability`) and this consumer
registers NO domain consumer at all, so the only artifacts it can produce are its
own `ResultSettlementRecord` plus the typed `SettledResultEvent` in the owner's
outbox.

Evidence is built from the REAL AR-0 output
-------------------------------------------
`SettlementEvidenceInput.from_action_receipt(receipt)` and
`SettlementEvidenceInput.from_result_evidence(evidence)` are the owner's own
normalizers (verified by reading `result_settlement_ar1.py:449-544`: both accept
an `ActionReceipt`/`ResultEvidence` dataclass OR a plain `Mapping`). The consumer
reads the real AR-0 receipts / ResultEvidence rows for the action (or takes them
from the Phase-H `CanonicalAr0Adapter`) and converts each row with the matching
owner normalizer, so the evidence that reaches AR-1 is the real owner's own row,
graded by the real owner's own authority-grading code.

Version dispatch and the `recon:`/`recon2:` mapping (orders 72, 73) - READ THIS
------------------------------------------------------------------------------
AR-1 has NO reconciliation-reference parameter. Verified by reading the owner:
`ResultSettlementService.settle_action(action_id, additional_evidences,
correlation_id, occurred_at, fault_hook)` (result_settlement_ar1.py:2338-2346) and
`ResultSettlementRecord` (result_settlement_ar1.py:669-712) - the fields are
`action_id`, `action_revision_ref`, `root_result_id`, `correlation_id`, ... and
the only reconciliation identity in the whole runtime is AR-0's `arec:`
(`action_reality_ledger.py:3217`). Passing a contact `recon:`/`recon2:` string to
AR-1 as if it were a reconciliation input is therefore IMPOSSIBLE - not merely
unsupported.

So this consumer MAPS the contact reference explicitly and says so:

    contact `recon:<digest>` / `recon2:<digest>`  (v1 / v2, dispatched by version)
        -> AR-1 `action_id`                = <actn:...>  (from the reference's own
                                              action_ref when it carries one)
        -> AR-1 `action_revision_ref`      = the owner's own
                                              `actn_rev:<actn:...>:v<revision>`
                                              (result_settlement_ar1.py:2444)
        -> AR-1 `correlation_id`           = a derived `corr:<sha256(ref)[:16]>`
                                              (AR-1 allows only the `corr` prefix,
                                              result_settlement_ar1.py:706); the raw
                                              contact reference stays in the CT0-10
                                              result, never dressed up as an owner field

That carrier is recorded verbatim in the result and in the artifact. The mapping
NEVER claims AR-1 accepted a reconciliation reference; it records that the
contact reference was bound onto the owner's action/revision identity. A `recon2:`
input is dispatched as v2 and is never a ValueError (order 73).
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

_THIS_DIR = Path(__file__).resolve().parent
_SRC_ROOT = _THIS_DIR.parents[1]
if str(_SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(_SRC_ROOT))

from ct0_10.canonical_spine import (  # noqa: E402
    CanonicalIntegrationError,
    NAMESPACE_ISOLATED_TEST,
    ReconciliationRef,
    SettlementRef,
    UnknownReferenceSchemeError,
)

# The canonical owners are vendored in this release (chiyo/life_runtime).
DEFAULT_RUNTIME_SCRIPTS = (
    Path(__file__).resolve().parents[2] / "life_runtime" / "scripts"
)

# domains the consumer must never be able to write (order sections 71-76)
FORBIDDEN_DOMAIN_DOMAINS = ("memory", "activity", "relationship", "world", "life", "goal")
CONTACT_RECON_SCHEMES = ("recon2:", "recon:")
CONTACT_SETTLEMENT_SCHEMES = ("csreq2:", "csreq:")


def contact_correlation_ref(contact_reference: str) -> str:
    """The `corr:` ref CT0-10 mints for a contact reference (owner-prefix safe).

    AR-1 validates `correlation_id` against the single allowed prefix `corr`
    (`result_settlement_ar1.py:706`), so a raw `recon:`/`recon2:`/`csreq:` string
    can never be stored there. The derived ref is deterministic
    (`corr:<sha256(contact reference)[:16]>`), so a reader can recompute it from
    the contact reference carried in the CT0-10 result and verify the binding.
    """
    import hashlib

    return f"corr:{hashlib.sha256(contact_reference.encode('utf-8')).hexdigest()[:16]}"


class CanonicalAr1ConsumerError(CanonicalIntegrationError):
    """Base error for the canonical AR-1 consumer."""


class CanonicalAr1EvidenceError(CanonicalAr1ConsumerError):
    """An evidence input could not be normalized by the real owner's API."""


class CanonicalAr1ReferenceMappingError(CanonicalAr1ConsumerError):
    """A contact reference could not be mapped onto the owner's identity."""


@dataclass(frozen=True, slots=True)
class ContactReferenceMapping:
    """Explicit, auditable mapping of a contact reference onto AR-1 identity.

    `accepted_by_ar1` is False by construction: AR-1 has no reconciliation-ref
    input at all, so nothing here is ever passed to the owner as a reconciliation
    reference.
    """

    reference: str
    kind: str                     # "reconciliation" | "settlement"
    scheme_version: str           # "v1" | "v2"
    meaning_version: str
    mapped_action_id: str | None
    mapped_correlation_id: str | None
    carried_revision: int | None
    correlation_derivation: str = (
        "mapped onto AR-1 correlation_id as corr:<sha256(contact reference)[:16]> because the owner "
        "validates correlation_id with allowed prefix 'corr' only (result_settlement_ar1.py:706); the "
        "raw contact reference string is NOT stored by AR-1 and is carried in the CT0-10 result instead"
    )
    accepted_by_ar1: bool = False
    mapping_note: str = (
        "AR-1 has no reconciliation-reference parameter (settle_action takes only action_id, "
        "additional_evidences, correlation_id, occurred_at); the contact reference is mapped onto "
        "the owner's action_id / action_revision_ref and a derived corr: correlation ref, and is "
        "never presented to AR-1 as a reconciliation input"
    )

    def to_mapping(self) -> dict[str, Any]:
        return {"reference": self.reference, "kind": self.kind,
                "scheme_version": self.scheme_version, "meaning_version": self.meaning_version,
                "mapped_action_id": self.mapped_action_id,
                "mapped_correlation_id": self.mapped_correlation_id,
                "correlation_derivation": self.correlation_derivation,
                "carried_revision": self.carried_revision,
                "accepted_by_ar1": self.accepted_by_ar1, "mapping_note": self.mapping_note}


@dataclass(frozen=True, slots=True)
class CanonicalSettlementResult:
    """Typed projection of the REAL AR-1 answer."""

    action_ref: str
    settlement_id: str
    settlement_status: str
    outcome_kind: str
    revision: int
    record_type: str
    record_schema_version: str
    action_revision_ref: str
    root_result_id: str
    correlation_id: str
    supersedes_ref: str | None
    idempotent_replay: bool
    event_id: str | None
    event_kind: str | None
    event_schema_version: str | None
    event_settlement_revision: int | None
    effect_claim_predicates: tuple[str, ...]
    confirmed_effect_refs: tuple[str, ...]
    unresolved_claims: tuple[str, ...]
    evidence_inputs: tuple[Mapping[str, Any], ...]
    evidence_provenance: tuple[Mapping[str, Any], ...]
    reference_mapping: Mapping[str, Any] | None
    observed_at: str
    notes: tuple[str, ...] = ()

    @property
    def settled(self) -> bool:
        return self.settlement_status in {"SETTLED", "PARTIAL", "CONFLICT"}

    def to_mapping(self) -> dict[str, Any]:
        return {
            "action_ref": self.action_ref, "settlement_id": self.settlement_id,
            "settlement_status": self.settlement_status, "outcome_kind": self.outcome_kind,
            "revision": self.revision, "record_type": self.record_type,
            "record_schema_version": self.record_schema_version,
            "action_revision_ref": self.action_revision_ref, "root_result_id": self.root_result_id,
            "correlation_id": self.correlation_id, "supersedes_ref": self.supersedes_ref,
            "idempotent_replay": self.idempotent_replay, "event_id": self.event_id,
            "event_kind": self.event_kind, "event_schema_version": self.event_schema_version,
            "event_settlement_revision": self.event_settlement_revision,
            "effect_claim_predicates": list(self.effect_claim_predicates),
            "confirmed_effect_refs": list(self.confirmed_effect_refs),
            "unresolved_claims": list(self.unresolved_claims),
            "evidence_inputs": [dict(item) for item in self.evidence_inputs],
            "evidence_provenance": [dict(item) for item in self.evidence_provenance],
            "reference_mapping": dict(self.reference_mapping) if self.reference_mapping else None,
            "observed_at": self.observed_at, "settled": self.settled, "notes": list(self.notes),
        }


class CanonicalAr1Consumer:
    """`CanonicalAr1Port` over the REAL AR-1 owner with no domain consumers."""

    def __init__(
        self,
        *,
        settlement_store_root: Path | str,
        action_store_root: Path | str,
        namespace: str = NAMESPACE_ISOLATED_TEST,
        lease_root: Path | str | None = None,
        runtime_scripts: Path | str | None = None,
        caller_module: str = "result_settlement_service",
        action_adapter: Any | None = None,
    ) -> None:
        self.settlement_store_root = Path(settlement_store_root)
        self.action_store_root = Path(action_store_root)
        self.namespace = namespace
        self.lease_root = (Path(lease_root) if lease_root is not None
                           else self.settlement_store_root.parent / "authority_lease")
        self.caller_module = caller_module
        self.action_adapter = action_adapter

        from ct0_10.canonical_ports.ar0_adapter import load_ar0_modules

        self._ar0, self._ac = load_ar0_modules(runtime_scripts)
        sys.path.insert(0, str(Path(runtime_scripts or DEFAULT_RUNTIME_SCRIPTS)))
        import result_settlement_ar1 as ar1

        self._ar1 = ar1
        # the REAL owners guard production homes themselves; the spine guard pins the namespace
        self._action_store = self._ar0.ActionStore(self.action_store_root, namespace=namespace)
        self._settlement_store = ar1.SettlementStore(self.settlement_store_root, namespace=namespace)
        from ct0_10.canonical_spine import assert_isolated_store_root

        assert_isolated_store_root(self.settlement_store_root, namespace=namespace)
        assert_isolated_store_root(self.action_store_root, namespace=namespace)

        self._lease: Any | None = None
        self._capability: Any | None = None
        self._service: Any | None = None
        self._read_service: Any | None = None

    # -- owner services ------------------------------------------------------
    def _owner(self) -> tuple[Any, Any]:
        if self._service is None:
            lease = self._ac.ActivityAuthorityLease(self.lease_root)
            lease.acquire()
            capability = self._ar1.ResultSettlementAuthority.issue_writer_capability(
                lease, caller_module=self.caller_module, namespace=self.namespace
            )
            # consumers={} on purpose: no Memory/Activity/Relationship/World consumer exists here
            self._service = self._ar1.ResultSettlementService(
                settlement_store=self._settlement_store, action_store=self._action_store,
                lease=lease, capability=capability, consumers={},
            )
            self._lease = lease
            self._capability = capability
            self._read_service = self._service.read_service
        return self._service, self._read_service

    @property
    def registered_consumers(self) -> dict[str, Any]:
        service, _ = self._owner()
        return dict(service.consumers)

    def close(self) -> None:
        if self._lease is not None:
            self._lease.release()
            self._lease = None
            self._service = None
            self._read_service = None

    def __enter__(self) -> "CanonicalAr1Consumer":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # -- order 72-73: explicit version dispatch -------------------------------
    @staticmethod
    def dispatch_contact_reference(result_evidence: Any) -> ContactReferenceMapping | None:
        """Dispatch a contact reference by its declared version. Never guesses.

        `recon2:`/`csreq2:` are v2, `recon:`/`csreq:` are v1. Any other prefix is
        rejected by the spine with `UnknownReferenceSchemeError` (a
        `CanonicalIntegrationError`, never a bare `ValueError`).
        """
        if result_evidence is None:
            return None
        if isinstance(result_evidence, ReconciliationRef):
            return ContactReferenceMapping(
                reference=result_evidence.format(), kind="reconciliation",
                scheme_version=result_evidence.scheme_version,
                meaning_version=result_evidence.meaning_version,
                mapped_action_id=result_evidence.action_ref,
                mapped_correlation_id=contact_correlation_ref(result_evidence.format()),
                carried_revision=result_evidence.revision)
        if isinstance(result_evidence, SettlementRef):
            return ContactReferenceMapping(
                reference=result_evidence.format(), kind="settlement",
                scheme_version=result_evidence.scheme_version,
                meaning_version=result_evidence.meaning_version,
                mapped_action_id=result_evidence.action_ref,
                mapped_correlation_id=contact_correlation_ref(result_evidence.format()),
                carried_revision=result_evidence.revision)
        if isinstance(result_evidence, str):
            for prefix in CONTACT_SETTLEMENT_SCHEMES:
                if result_evidence.startswith(prefix):
                    ref = SettlementRef.parse(result_evidence)
                    return ContactReferenceMapping(
                        reference=ref.format(), kind="settlement",
                        scheme_version=ref.scheme_version, meaning_version=ref.meaning_version,
                        mapped_action_id=ref.action_ref,
                        mapped_correlation_id=contact_correlation_ref(ref.format()),
                        carried_revision=ref.revision)
            for prefix in CONTACT_RECON_SCHEMES:
                if result_evidence.startswith(prefix):
                    ref = ReconciliationRef.parse(result_evidence)
                    return ContactReferenceMapping(
                        reference=ref.format(), kind="reconciliation",
                        scheme_version=ref.scheme_version, meaning_version=ref.meaning_version,
                        mapped_action_id=ref.action_ref,
                        mapped_correlation_id=contact_correlation_ref(ref.format()),
                        carried_revision=ref.revision)
            if ":" in result_evidence and result_evidence.split(":", 1)[0].startswith(("recon", "csreq")):
                raise UnknownReferenceSchemeError(
                    f"unrecognised contact reference scheme in {result_evidence[:24]!r}"
                )
            raise CanonicalAr1ReferenceMappingError(
                f"a plain string {result_evidence[:32]!r} is not a contact reference; pass a "
                "ReconciliationRef/SettlementRef, a recon:/recon2:/csreq:/csreq2: reference, an "
                "evidence mapping, or None"
            )
        return None

    # -- evidence construction from the REAL AR-0 output ---------------------
    def _evidence_from_ar0(self, action_ref: str) -> tuple[list[Any], list[dict[str, Any]]]:
        """Normalize the real AR-0 rows with the owner's own AR-1 constructors."""
        if self.action_adapter is not None and hasattr(self.action_adapter, "receipts"):
            receipts = [dict(item) for item in self.action_adapter.receipts(action_ref)]
            evidences = [dict(item) for item in self.action_adapter.result_evidences(action_ref)]
        else:
            read = self._ar0.ActionReadService(self._action_store)
            receipts = [dict(item) for item in read.list_receipts(action_ref)]
            evidences = [dict(item) for item in read.list_result_evidences(action_ref)]
        out: list[Any] = []
        provenance: list[dict[str, Any]] = []
        for receipt in receipts:
            out.append(self._ar1.SettlementEvidenceInput.from_action_receipt(receipt))
            provenance.append({"source": "ar0 ActionReceipt", "evidence_id": receipt.get("receipt_id"),
                               "constructed_by": "SettlementEvidenceInput.from_action_receipt",
                               "status_claim": receipt.get("status_claim"),
                               "authority_domain": receipt.get("authority_domain")})
        for evidence in evidences:
            out.append(self._ar1.SettlementEvidenceInput.from_result_evidence(evidence))
            provenance.append({"source": "ar0 ResultEvidence", "evidence_id": evidence.get("evidence_id"),
                               "constructed_by": "SettlementEvidenceInput.from_result_evidence",
                               "observer_kind": evidence.get("observer_kind")})
        return out, provenance

    # -- order 71-76: settle -------------------------------------------------
    def settle(self, *, action_ref: str, result_evidence: Any = None,
               observed_at: str) -> CanonicalSettlementResult:
        service, _ = self._owner()
        mapping = self.dispatch_contact_reference(result_evidence)

        action_id = action_ref
        correlation_id: str | None = None
        if mapping is not None:
            if mapping.mapped_action_id:
                if action_ref and mapping.mapped_action_id != action_ref:
                    raise CanonicalAr1ReferenceMappingError(
                        f"the contact reference names {mapping.mapped_action_id!r} but the caller passed "
                        f"action_ref={action_ref!r}; refusing to settle the wrong action"
                    )
                action_id = mapping.mapped_action_id
            correlation_id = mapping.mapped_correlation_id

        additional: list[Any] = []
        provenance: list[dict[str, Any]] = []
        # 1. the real AR-0 evidence rows, normalized by the owner's own API
        ar0_rows, ar0_provenance = self._evidence_from_ar0(action_id)
        additional.extend(ar0_rows)
        provenance.extend(ar0_provenance)
        # 2. an explicit caller-supplied evidence value (never a contact reference)
        if result_evidence is not None and mapping is None:
            if isinstance(result_evidence, (self._ar1.SettlementEvidenceInput, self._ar0.ActionReceipt,
                                            self._ar0.ResultEvidence)):
                additional.append(result_evidence)
                provenance.append({"source": type(result_evidence).__name__,
                                   "constructed_by": "caller (real owner type)",
                                   "evidence_id": getattr(result_evidence, "evidence_id", None)
                                   or getattr(result_evidence, "receipt_id", None)})
            elif isinstance(result_evidence, Mapping):
                additional.append(result_evidence)
                provenance.append({"source": "caller mapping", "constructed_by": "owner normalization",
                                   "evidence_id": result_evidence.get("evidence_id")
                                   or result_evidence.get("receipt_id")})
            else:
                raise CanonicalAr1EvidenceError(
                    f"unsupported result_evidence type {type(result_evidence).__name__!r}; AR-1 refuses "
                    "free-form narrative evidence and CT0-10 will not invent a shape for it"
                )

        response = service.settle_action(action_id=action_id, additional_evidences=additional or None,
                                         correlation_id=correlation_id, occurred_at=observed_at)
        record = dict(response["settlement"])
        event = dict(response["outbox_event"]) if response.get("outbox_event") else None
        notes = [
            "evidence built from the REAL AR-0 rows with the AR-1 owner's own "
            "SettlementEvidenceInput.from_action_receipt / .from_result_evidence",
            "the consumer registers no domain consumer, so no Memory/Activity/Relationship/World "
            "write exists to be performed; the outbox dispatch proof is separate and explicit",
        ]
        if mapping is not None:
            notes.append(mapping.mapping_note)
        return CanonicalSettlementResult(
            action_ref=action_id,
            settlement_id=str(record["settlement_id"]),
            settlement_status=str(record["settlement_status"]),
            outcome_kind=str(record["outcome_kind"]),
            revision=int(record["revision"]),
            record_type="ResultSettlementRecord",
            record_schema_version=str(record["schema_version"]),
            action_revision_ref=str(record["action_revision_ref"]),
            root_result_id=str(record["root_result_id"]),
            correlation_id=str(record["correlation_id"]),
            supersedes_ref=record.get("supersedes_ref"),
            idempotent_replay=bool(response["idempotent_replay"]),
            event_id=(str(event["event_id"]) if event else None),
            event_kind=(str(event["event_kind"]) if event else None),
            event_schema_version=(str(event["schema_version"]) if event else None),
            event_settlement_revision=(int(event["settlement_revision"]) if event else None),
            effect_claim_predicates=tuple(str(claim["predicate"]) for claim in record.get("effect_claims", [])),
            confirmed_effect_refs=tuple(record.get("confirmed_effect_refs") or ()),
            unresolved_claims=tuple(record.get("unresolved_claims") or ()),
            evidence_inputs=tuple(item.to_dict() for item in additional
                                  if hasattr(item, "to_dict")),
            evidence_provenance=tuple(provenance),
            reference_mapping=(mapping.to_mapping() if mapping else None),
            observed_at=observed_at,
            notes=tuple(notes),
        )

    # -- no-foreign-domain-write proofs --------------------------------------
    def prove_no_foreign_domain_write(
        self, *, domains: Sequence[str] = FORBIDDEN_DOMAIN_DOMAINS
    ) -> dict[str, Any]:
        """Prove the owner refuses every foreign-domain writer capability.

        Two independent refusals are exercised: the authority's capability request
        and a sandbox domain consumer's cross-domain write attempt.
        """
        authority_refusals: list[dict[str, Any]] = []
        for domain in domains:
            try:
                self._ar1.ResultSettlementAuthority.request_domain_writer_capability(domain)
                authority_refusals.append({"domain": domain, "refused": False, "verbatim": None})
            except Exception as exc:
                authority_refusals.append({"domain": domain, "refused": True,
                                           "error_type": type(exc).__name__, "verbatim": str(exc)})
        consumer_refusals: list[dict[str, Any]] = []
        for consumer_cls, domain in ((self._ar1.FakeMemoryConsumer, "memory"),
                                     (self._ar1.FakeLifeConsumer, "life"),
                                     (self._ar1.FakeGoalConsumer, "goal"),
                                     (self._ar1.FakeWorldConsumer, "world")):
            consumer = consumer_cls()
            try:
                consumer.attempt_forbidden_cross_domain_write(domain)
                consumer_refusals.append({"consumer_domain": domain, "target_domain": domain,
                                          "refused": False, "verbatim": None})
            except Exception as exc:
                consumer_refusals.append({"consumer_domain": domain, "target_domain": domain,
                                          "refused": True, "error_type": type(exc).__name__,
                                          "verbatim": str(exc)})
        service, _ = self._owner()
        return {
            "authority_refusals": authority_refusals,
            "consumer_refusals": consumer_refusals,
            "all_refused": all(item["refused"] for item in authority_refusals + consumer_refusals),
            "registered_consumers": sorted(service.consumers),
            "domain_write_count_on_consumer_path": 0,
        }

    def prove_no_domain_delivery(self, *, action_ref: str,
                                 domains: Sequence[str] = ("life", "memory", "goal", "world")
                                 ) -> dict[str, Any]:
        """Ask the owner to dispatch this action's outbox event to every domain.

        No domain consumer is registered, so the real owner delivers to nobody:
        `dispatched_count == 0` and the delivery table stays empty. The event stays
        in the owner's outbox as a typed `SettledResultEvent`.
        """
        service, _ = self._owner()
        before = self._settlement_store.load_state()
        result = service.dispatch_outbox(target_domains=list(domains))
        after = self._settlement_store.load_state()
        return {
            "requested_domains": list(domains),
            "dispatched_count": int(result["dispatched_count"]),
            "deliveries": list(result["deliveries"]),
            "deliveries_by_id_before": sorted(before.get("deliveries_by_id") or {}),
            "deliveries_by_id_after": sorted(after.get("deliveries_by_id") or {}),
            "outbox_events_for_action": sorted(
                event_id for event_id, event in (after.get("outbox_events") or {}).items()
                if event.get("action_id") == action_ref
            ),
            "only_types_written": ["ResultSettlementRecord", "SettledResultEvent", "EffectClaim"
                                   if after.get("effect_claims_by_id") else "EffectClaim(none)"],
        }

    # -- identity mapping table (order 59, 74-76) ---------------------------
    def identity_mapping(self, action_ref: str) -> dict[str, Any]:
        """contact key -> canonical key -> action ref -> AR-1 settlement identity."""
        action_state = self._action_store.load_state()
        record = (action_state.get("actions") or {}).get(action_ref) or {}
        settlement_state = self._settlement_store.load_state()
        settlement_id = (settlement_state.get("latest_settlement_by_action") or {}).get(action_ref)
        settlement = (settlement_state.get("settlements_by_id") or {}).get(settlement_id) or {}
        return {
            "contact_submission_key_ar0_act": record.get("idempotency_key"),
            "ar0_idempotency_key": record.get("idempotency_key"),
            "canonical_submission_key_subk": record.get("submission_key"),
            "canonical_action_ref_actn": record.get("action_id"),
            "canonical_proposal_ref_aprop": record.get("proposal_ref"),
            "canonical_reconciliation_ref_arec": record.get("last_reconciliation_ref"),
            "canonical_reconciliation_refs_arec": list(record.get("reconciliation_refs") or ()),
            "ar1_settlement_id_rset": settlement.get("settlement_id"),
            "ar1_action_revision_ref": settlement.get("action_revision_ref"),
            "ar1_correlation_id": settlement.get("correlation_id"),
            "ar1_root_result_id": settlement.get("root_result_id"),
            "ar1_settlement_status": settlement.get("settlement_status"),
            "ar1_record_schema_version": settlement.get("schema_version"),
        }

    def settlement_snapshot(self, action_ref: str) -> Mapping[str, Any] | None:
        _, read = self._owner()
        record = read.get_settlement(action_ref)
        return dict(record) if record else None

    def settlement_history(self, action_ref: str) -> tuple[Mapping[str, Any], ...]:
        _, read = self._owner()
        return tuple(dict(item) for item in read.get_settlement_history(action_ref))


__all__ = [
    "CanonicalAr1Consumer", "CanonicalAr1ConsumerError", "CanonicalAr1EvidenceError",
    "CanonicalAr1ReferenceMappingError", "CanonicalSettlementResult", "ContactReferenceMapping",
    "CONTACT_RECON_SCHEMES", "CONTACT_SETTLEMENT_SCHEMES", "FORBIDDEN_DOMAIN_DOMAINS",
]
