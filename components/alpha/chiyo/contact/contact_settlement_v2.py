"""Settlement / outbox contract v2: reference the verified history, never copy it.

Why v2 exists
-------------
Contract v1 (`contact_settlement_bridge.ContactSettlementRequest`) binds the
action's *entire* active evidence reference list into its identity and payload.
Because a revision is emitted on every reconciliation, one action with N distinct
evidence events re-stores the growing list N times.  Measured on the derived
outbox projection: 1,000 events -> 72,590,036 bytes of outbox payload,
2,500 events -> 451,475,036 bytes, i.e. ~6.2x bytes for 2.5x events.  That is a
representation defect in *delivery infrastructure*, not a durable requirement.

The rule v2 implements:

    "this settlement is based on which verified result" must be durable;
    "copy ten thousand historical evidence rows so we can prove it" must not.

v2 therefore stores a fixed-size reference to an authoritative reconciliation
revision plus its cumulative evidence commitment.  The full evidence history
stays where it already lives - the canonical reconciliation journal - and is
materialized on demand by a read-only resolver.

Compatibility rules (permanent)
-------------------------------
* v1 settlement rows stay readable, validatable and replayable; no existing v1
  journal row, outbox row, fixture or hash is rewritten and nothing is migrated
  in bulk.
* An action pins its settlement contract at its first settlement: a v1
  reconciliation keeps producing v1 settlements, a v2 reconciliation produces v2.
* Readers dispatch on the declared contract identity (the payload's
  ``meaning_version`` when present, otherwise the reference prefix minted by the
  writer: ``csreq2:`` for v2, ``csreq:`` for v1).  An unknown identity fails closed.
* The commitment is not evidence: it proves which verified evidence history the
  settlement is bound to, and never by itself asserts DELIVERED/READ/FAILED.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from contact_receipt_model import stable_hash

MEANING_VERSION = "contact-settlement/v2"
SCHEMA_VERSION = "chiyo.contact.settlement_request.v2"
V1_MEANING_VERSION = "contact-settlement/v1"
V1_REF_PREFIX = "csreq:"
V2_REF_PREFIX = "csreq2:"
DEFAULT_TARGET_CONSUMER = "consumer:contact-result-handoff"

SETTLEMENT_CANDIDATES = frozenset({"UNRESOLVED", "SUCCESS_EFFECT", "FAILURE_NO_EFFECT",
                                   "UNKNOWN_EFFECT", "CONFLICT"})


class SettlementContractError(ValueError):
    """A v2 settlement reference is malformed, stale or inconsistent.

    `kind` names the exact fault so callers can map it onto the existing typed
    durability errors instead of inventing a message.
    """

    def __init__(self, message: str, *, kind: str) -> None:
        super().__init__(message)
        self.kind = kind


FAULT_KINDS = (
    "malformed",
    "unknown_contract_version",
    "stale_revision",
    "commitment_mismatch",
    "result_hash_mismatch",
    "action_mismatch",
    "missing_reconciliation",
    "candidate_unsupported",
)


def contract_version_for(*, payload: dict[str, Any], reference: str) -> str:
    """Declared settlement contract identity; anything undeclared fails closed."""
    declared = payload.get("meaning_version")
    if declared is not None:
        if declared == MEANING_VERSION:
            return "v2"
        if declared == V1_MEANING_VERSION:
            return "v1"
        raise SettlementContractError(f"unknown settlement contract {declared!r}",
                                      kind="unknown_contract_version")
    if reference.startswith(V2_REF_PREFIX):
        return "v2"
    if reference.startswith(V1_REF_PREFIX):
        return "v1"
    raise SettlementContractError("settlement reference has no known contract identity",
                                  kind="unknown_contract_version")


@dataclass(frozen=True, slots=True)
class ContactSettlementRequestV2:
    settlement_request_ref: str
    action_ref: str
    reconciliation_ref: str
    reconciliation_revision: int
    evidence_count: int
    evidence_chain_commitment: str
    result_status: str
    result_hash: str
    candidate: str
    target_consumer: str
    schema_version: str = SCHEMA_VERSION
    meaning_version: str = MEANING_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION or self.meaning_version != MEANING_VERSION:
            raise SettlementContractError("unsupported v2 settlement version",
                                          kind="unknown_contract_version")
        if not self.action_ref.startswith("actn:"):
            raise SettlementContractError("invalid action reference", kind="action_mismatch")
        if not self.reconciliation_ref.startswith("recon2:"):
            raise SettlementContractError("v2 settlement requires a v2 reconciliation reference",
                                          kind="commitment_mismatch")
        if type(self.reconciliation_revision) is not int or self.reconciliation_revision < 1:
            raise SettlementContractError("reconciliation revision must be positive", kind="malformed")
        if type(self.evidence_count) is not int or self.evidence_count < 1:
            raise SettlementContractError("evidence count must be positive", kind="malformed")
        for name in ("evidence_chain_commitment", "result_hash"):
            value = getattr(self, name)
            if not isinstance(value, str) or len(value) != 64:
                raise SettlementContractError(f"{name} must be a SHA-256 digest", kind="malformed")
        if self.candidate not in SETTLEMENT_CANDIDATES:
            raise SettlementContractError("unsupported settlement candidate",
                                          kind="candidate_unsupported")
        if not isinstance(self.target_consumer, str) or not self.target_consumer:
            raise SettlementContractError("target consumer identity is required", kind="malformed")
        if self.settlement_request_ref != f"{V2_REF_PREFIX}{settlement_identity(self)}":
            raise SettlementContractError("settlement request identity mismatch",
                                          kind="commitment_mismatch")

    @property
    def supporting_evidence_refs(self) -> tuple[str, ...]:
        """v2 deliberately carries no evidence list; the resolver materializes it."""
        return ()

    @property
    def evidence_fingerprint(self) -> str:
        """Fixed-size stand-in for v1's fingerprint: the cumulative commitment."""
        return self.evidence_chain_commitment


def identity_material(*, action_ref: str, reconciliation_ref: str, reconciliation_revision: int,
                      evidence_count: int, evidence_chain_commitment: str, result_hash: str,
                      candidate: str, target_consumer: str,
                      meaning_version: str = MEANING_VERSION) -> tuple[Any, ...]:
    """The fixed-size tuple a v2 settlement identity is derived from."""
    return (action_ref, reconciliation_ref, int(reconciliation_revision), int(evidence_count),
            evidence_chain_commitment, result_hash, candidate, target_consumer, meaning_version)


def settlement_identity(request: ContactSettlementRequestV2) -> str:
    """Fixed-size settlement identity: no evidence list enters this hash."""
    return stable_hash(identity_material(
        action_ref=request.action_ref, reconciliation_ref=request.reconciliation_ref,
        reconciliation_revision=request.reconciliation_revision,
        evidence_count=request.evidence_count,
        evidence_chain_commitment=request.evidence_chain_commitment,
        result_hash=request.result_hash, candidate=request.candidate,
        target_consumer=request.target_consumer, meaning_version=request.meaning_version))


def create(*, action_ref: str, reconciliation_ref: str, reconciliation_revision: int,
           evidence_count: int, evidence_chain_commitment: str, result_status: str,
           result_hash: str, candidate: str,
           target_consumer: str = DEFAULT_TARGET_CONSUMER) -> ContactSettlementRequestV2:
    identity = stable_hash(identity_material(
        action_ref=action_ref, reconciliation_ref=reconciliation_ref,
        reconciliation_revision=reconciliation_revision, evidence_count=evidence_count,
        evidence_chain_commitment=evidence_chain_commitment, result_hash=result_hash,
        candidate=candidate, target_consumer=target_consumer))
    return ContactSettlementRequestV2(
        settlement_request_ref=f"{V2_REF_PREFIX}{identity}", action_ref=action_ref,
        reconciliation_ref=reconciliation_ref, reconciliation_revision=reconciliation_revision,
        evidence_count=evidence_count, evidence_chain_commitment=evidence_chain_commitment,
        result_status=result_status, result_hash=result_hash, candidate=candidate,
        target_consumer=target_consumer)


def payload(request: ContactSettlementRequestV2) -> dict[str, Any]:
    return {
        "kind": "SETTLEMENT_REQUEST",
        "settlement_request_ref": request.settlement_request_ref,
        "request_ref": request.settlement_request_ref,
        "action_ref": request.action_ref,
        "reconciliation_ref": request.reconciliation_ref,
        "reconciliation_revision": request.reconciliation_revision,
        "evidence_count": request.evidence_count,
        "evidence_chain_commitment": request.evidence_chain_commitment,
        "result_status": request.result_status,
        "result_hash": request.result_hash,
        "candidate": request.candidate,
        "target_consumer": request.target_consumer,
        "schema_version": request.schema_version,
        "meaning_version": request.meaning_version,
    }


def load(document: dict[str, Any]) -> ContactSettlementRequestV2:
    try:
        data = {key: value for key, value in dict(document).items()
                if key not in ("kind", "request_ref")}
        return ContactSettlementRequestV2(**data)
    except SettlementContractError:
        raise
    except (AttributeError, KeyError, TypeError, ValueError):
        raise SettlementContractError("v2 settlement payload is malformed",
                                      kind="malformed") from None


def encoded_size_bound() -> int:
    """Upper bound on an encoded v2 settlement request, independent of history size."""
    return len(payload(create(action_ref="actn:" + "0" * 40, reconciliation_ref="recon2:" + "0" * 64,
                              reconciliation_revision=10 ** 6, evidence_count=10 ** 6,
                              evidence_chain_commitment="0" * 64, result_status="CONFLICT",
                              result_hash="0" * 64, candidate="CONFLICT")).__str__()) + 64
# -- outbox v2: result handoff ------------------------------------------------
#
# Same rule, second leak site.  Contract v1's handoff payload embeds the whole
# active evidence list and the derived outbox row stores that list again in its
# `evidence_refs` column, so one action with N distinct evidence events writes
# O(N^2) delivery bytes.  Measured: 1,000 events -> 37,081,826 outbox payload
# bytes with a 72,272 byte largest item, i.e. the same defect one layer up.
# v2 stores the fixed-size reference to the verified revision instead; the
# evidence itself stays in the canonical reconciliation journal.

HANDOFF_MEANING_VERSION = "contact-result-handoff/v2"
HANDOFF_SCHEMA_VERSION = "chiyo.contact.result_handoff.v2"
V1_HANDOFF_FIELDS = frozenset({"kind", "action_ref", "state", "delivery_status",
                               "evidence_refs", "causal_refs", "statement"})


def handoff_statement(status: str) -> str:
    """The frozen handoff wording; UNKNOWN never claims an effect was applied."""
    return ("action unresolved; delivery status remains unknown" if status == "UNKNOWN"
            else f"contact action result: {status}")


def handoff_payload(*, action_ref: str, status: str, reconciliation_ref: str,
                    reconciliation_revision: int, evidence_count: int,
                    evidence_chain_commitment: str, result_hash: str) -> dict[str, Any]:
    """Fixed-size outbox v2 handoff: it references the verified history, never copies it."""
    return {
        "kind": "CONTACT_RESULT_HANDOFF",
        "action_ref": action_ref,
        "state": status,
        "delivery_status": status,
        "reconciliation_ref": reconciliation_ref,
        "reconciliation_revision": int(reconciliation_revision),
        "evidence_count": int(evidence_count),
        "evidence_chain_commitment": evidence_chain_commitment,
        "result_hash": result_hash,
        "causal_refs": [reconciliation_ref],
        "statement": handoff_statement(status),
        "schema_version": HANDOFF_SCHEMA_VERSION,
        "meaning_version": HANDOFF_MEANING_VERSION,
    }


def handoff_contract_version_for(*, payload: dict[str, Any]) -> str:
    """Declared handoff contract identity; an undeclared shape fails closed.

    A legacy v1 row is recognised by its *exact* frozen field set - never by
    "which fields happen to be present" - so a half-migrated row cannot pass as
    either version.
    """
    if not isinstance(payload, dict):
        raise SettlementContractError("handoff payload is not an object", kind="malformed")
    declared = payload.get("meaning_version")
    if declared is not None:
        if declared == HANDOFF_MEANING_VERSION:
            return "v2"
        raise SettlementContractError(f"unknown handoff contract {declared!r}",
                                      kind="unknown_contract_version")
    if set(payload) == V1_HANDOFF_FIELDS and payload.get("kind") == "CONTACT_RESULT_HANDOFF":
        return "v1"
    raise SettlementContractError("handoff payload has no known contract identity",
                                  kind="unknown_contract_version")


def encoded_handoff_size_bound() -> int:
    """Upper bound on an encoded v2 handoff, independent of history size."""
    return len(str(handoff_payload(action_ref="actn:" + "0" * 40, status="CONFLICT",
                                   reconciliation_ref="recon2:" + "0" * 64,
                                   reconciliation_revision=10 ** 6, evidence_count=10 ** 6,
                                   evidence_chain_commitment="0" * 64,
                                   result_hash="0" * 64))) + 64


__all__ = [
    "MEANING_VERSION", "SCHEMA_VERSION", "V1_MEANING_VERSION", "V1_REF_PREFIX", "V2_REF_PREFIX",
    "HANDOFF_MEANING_VERSION", "HANDOFF_SCHEMA_VERSION", "V1_HANDOFF_FIELDS",
    "DEFAULT_TARGET_CONSUMER", "SETTLEMENT_CANDIDATES", "FAULT_KINDS", "SettlementContractError",
    "ContactSettlementRequestV2", "contract_version_for", "settlement_identity", "create",
    "payload", "load", "encoded_size_bound",
    "handoff_payload", "handoff_contract_version_for", "handoff_statement",
    "encoded_handoff_size_bound",
]