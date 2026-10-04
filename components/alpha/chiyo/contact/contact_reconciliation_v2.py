"""Reconciliation contract v2: incremental evidence chain + cumulative commitment.

Why v2 exists
-------------
Contract v1 (`contact_reconciliation.ContactReconciliationRecord`) embeds the
action's *entire* ordered evidence list in every revision.  On one action with N
distinct evidence events that stores O(N^2) reference bytes: measured, 1,000
events already produced a 222,576,640-byte journal.  That is a representation
defect, not a durability requirement.

v2 keeps exactly the same observable semantics but stores only the delta plus a
length-framed, domain-separated cumulative commitment, so journal growth is
O(N) in the number of events.

Compatibility rules (permanent)
-------------------------------
* v1 records stay readable, replayable and hash-verifiable forever; no existing
  v1 journal row, fixture or hash is rewritten.
* An action pins its reconciliation contract version at its first reconciliation
  and keeps it: a v1 action keeps producing v1 and a v2 action keeps producing
  v2.  There is no silent mid-action switch and no online v1->v2 upgrade here.
* A reader dispatches on the *declared* contract identity - the payload's
  ``meaning_version`` when present, otherwise the reconciliation reference
  prefix minted by the writer (``recon2:`` for v2, ``recon:`` for v1).  It never
  infers a version from "which fields happen to be present", and an unknown
  identity is rejected fail-closed.

The commitment is *not* evidence
--------------------------------
``evidence_chain_commitment`` only commits to which evidence sequence this
revision summarizes.  It can never be read as DELIVERED/FAILED/READ evidence,
and it never replaces the canonical receipt rows.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any, Iterable

from contact_receipt_model import stable_hash

MEANING_VERSION = "contact-reconciliation/v2"
SCHEMA_VERSION = "chiyo.contact.reconciliation_record.v2"
V1_MEANING_VERSION = "contact-reconciliation/v1"
V1_REF_PREFIX = "recon:"
V2_REF_PREFIX = "recon2:"
CHAIN_FORMAT = "chiyo.contact.evidence_chain.v1"
DELTA_FORMAT = "chiyo.contact.evidence_delta.v1"
EMPTY_COMMITMENT = "0" * 64
LEGACY_EMPTY_COMMITMENT = "0" * 64

STATES = frozenset({"UNKNOWN", "SUBMITTED", "ACKNOWLEDGED", "DELIVERED", "FAILURE", "CONFLICT"})
REASONS = frozenset({"UNKNOWN_BEFORE_SUBMIT", "UNKNOWN_AFTER_SUBMIT", "UNKNOWN_AFTER_ACCEPT",
                     "UNKNOWN_AFTER_DELIVERY", "UNKNOWN_PROVIDER_STATE", "RECOVERY_GAP",
                     "EVIDENCE_OBSERVED", "CONTRADICTORY_EVIDENCE", "STILL_UNKNOWN", "CORRECTED"})


class ReconciliationChainError(ValueError):
    """A v2 evidence chain is broken, ambiguous or tampered with.

    `kind` names the exact structural fault so callers can map it to a typed
    durable error without inventing a message.
    """

    def __init__(self, message: str, *, kind: str) -> None:
        super().__init__(message)
        self.kind = kind


CHAIN_FAULT_KINDS = (
    "missing_parent",
    "revision_gap",
    "wrong_action_parent",
    "commitment_mismatch",
    "delta_digest_mismatch",
    "count_mismatch",
    "duplicate_evidence_in_delta",
    "unknown_contract_version",
    "malformed_record",
)


def frame_refs(refs: Iterable[str]) -> bytes:
    """Deterministic, length-framed encoding of an ordered reference sequence."""
    out = bytearray()
    for ref in refs:
        if not isinstance(ref, str):
            raise ReconciliationChainError("evidence reference must be a string", kind="malformed_record")
        encoded = ref.encode("utf-8")
        out += len(encoded).to_bytes(4, "big")
        out += encoded
    return bytes(out)


def delta_digest(new_refs: Iterable[str]) -> str:
    payload = frame_refs(new_refs)
    digest = hashlib.sha256()
    digest.update(DELTA_FORMAT.encode("utf-8") + b"\x1f")
    digest.update(len(payload).to_bytes(8, "big"))
    digest.update(payload)
    return digest.hexdigest()


def chain_commitment(previous_commitment: str, new_refs: Iterable[str]) -> str:
    """C(n+1) = H(format, Cn, framed(delta)); C(0) = EMPTY_COMMITMENT."""
    if not isinstance(previous_commitment, str) or len(previous_commitment) != 64:
        raise ReconciliationChainError("previous commitment must be a 64-hex digest",
                                       kind="malformed_record")
    payload = frame_refs(new_refs)
    digest = hashlib.sha256()
    digest.update(CHAIN_FORMAT.encode("utf-8") + b"\x1f")
    digest.update(previous_commitment.encode("ascii"))
    digest.update(len(payload).to_bytes(8, "big"))
    digest.update(payload)
    return digest.hexdigest()


def contract_version_for(*, payload: dict[str, Any], reference: str) -> str:
    """Declared contract identity for a journaled reconciliation row.

    Fails closed for anything that is not explicitly v1 or v2.
    """
    declared = payload.get("meaning_version")
    if declared is not None:
        if declared == MEANING_VERSION:
            return "v2"
        if declared == V1_MEANING_VERSION:
            return "v1"
        raise ReconciliationChainError(f"unknown reconciliation contract {declared!r}",
                                       kind="unknown_contract_version")
    if reference.startswith(V2_REF_PREFIX):
        return "v2"
    if reference.startswith(V1_REF_PREFIX):
        return "v1"
    raise ReconciliationChainError("reconciliation reference has no known contract identity",
                                   kind="unknown_contract_version")


@dataclass(frozen=True, slots=True)
class ContactReconciliationRecordV2:
    reconciliation_ref: str
    action_ref: str
    revision: int
    previous_reconciliation_ref: str | None
    new_evidence_refs: tuple[str, ...]
    new_evidence_digest: str
    total_evidence_count: int
    evidence_chain_commitment: str
    new_supersession_refs: tuple[str, ...]
    total_supersession_count: int
    status: str
    reason_code: str
    result_hash: str
    schema_version: str = SCHEMA_VERSION
    meaning_version: str = MEANING_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION or self.meaning_version != MEANING_VERSION:
            raise ReconciliationChainError("unsupported v2 schema/meaning version",
                                           kind="unknown_contract_version")
        if not isinstance(self.action_ref, str) or not self.action_ref.startswith("actn:"):
            raise ReconciliationChainError("invalid action reference", kind="malformed_record")
        if type(self.revision) is not int or self.revision < 1:
            raise ReconciliationChainError("revision must be a positive integer", kind="malformed_record")
        if self.revision == 1:
            if self.previous_reconciliation_ref is not None:
                raise ReconciliationChainError("revision 1 must not name a parent",
                                               kind="malformed_record")
        elif (not isinstance(self.previous_reconciliation_ref, str)
              or not self.previous_reconciliation_ref.startswith(V2_REF_PREFIX)):
            raise ReconciliationChainError("revision > 1 requires a v2 parent reference",
                                           kind="missing_parent")
        for name in ("new_evidence_refs", "new_supersession_refs"):
            refs = getattr(self, name)
            if not isinstance(refs, tuple) or tuple(sorted(set(refs))) != refs:
                raise ReconciliationChainError(f"{name} must be a sorted, unique tuple",
                                               kind="duplicate_evidence_in_delta"
                                               if 'evidence' in name else "malformed_record")
        if len(set(self.new_evidence_refs)) != len(self.new_evidence_refs):
            raise ReconciliationChainError("delta contains duplicate evidence references",
                                           kind="duplicate_evidence_in_delta")
        if self.new_evidence_digest != delta_digest(self.new_evidence_refs):
            raise ReconciliationChainError("delta digest does not match its references",
                                           kind="delta_digest_mismatch")
        if len(self.evidence_chain_commitment) != 64 or len(self.result_hash) != 64:
            raise ReconciliationChainError("commitment and result hash must be SHA-256",
                                           kind="malformed_record")
        if type(self.total_evidence_count) is not int or self.total_evidence_count < len(self.new_evidence_refs):
            raise ReconciliationChainError("total evidence count is inconsistent",
                                           kind="count_mismatch")
        if type(self.total_supersession_count) is not int or self.total_supersession_count < len(self.new_supersession_refs):
            raise ReconciliationChainError("total supersession count is inconsistent",
                                           kind="count_mismatch")
        if self.status not in STATES or self.reason_code not in REASONS:
            raise ReconciliationChainError("invalid status or reason code", kind="malformed_record")
        expected = result_hash_for(
            action_ref=self.action_ref, revision=self.revision,
            total_evidence_count=self.total_evidence_count,
            evidence_chain_commitment=self.evidence_chain_commitment,
            total_supersession_count=self.total_supersession_count,
            status=self.status, reason_code=self.reason_code)
        if self.result_hash != expected:
            raise ReconciliationChainError("result hash mismatch", kind="commitment_mismatch")
        if self.reconciliation_ref != f"{V2_REF_PREFIX}{expected}":
            raise ReconciliationChainError("reconciliation reference is not derived from its result hash",
                                           kind="commitment_mismatch")

    @classmethod
    def create(cls, *, action_ref: str, revision: int, previous_reconciliation_ref: str | None,
               previous_commitment: str, new_evidence_refs: Iterable[str],
               previous_total_evidence: int, new_supersession_refs: Iterable[str] = (),
               previous_total_supersessions: int = 0, status: str, reason_code: str) -> "ContactReconciliationRecordV2":
        evidence_delta = tuple(sorted(set(new_evidence_refs)))
        supersession_delta = tuple(sorted(set(new_supersession_refs)))
        commitment = chain_commitment(previous_commitment, evidence_delta)
        total_evidence = int(previous_total_evidence) + len(evidence_delta)
        total_supersessions = int(previous_total_supersessions) + len(supersession_delta)
        result = result_hash_for(action_ref=action_ref, revision=revision,
                                 total_evidence_count=total_evidence,
                                 evidence_chain_commitment=commitment,
                                 total_supersession_count=total_supersessions,
                                 status=status, reason_code=reason_code)
        return cls(reconciliation_ref=f"{V2_REF_PREFIX}{result}", action_ref=action_ref,
                   revision=revision, previous_reconciliation_ref=previous_reconciliation_ref,
                   new_evidence_refs=evidence_delta, new_evidence_digest=delta_digest(evidence_delta),
                   total_evidence_count=total_evidence, evidence_chain_commitment=commitment,
                   new_supersession_refs=supersession_delta,
                   total_supersession_count=total_supersessions, status=status,
                   reason_code=reason_code, result_hash=result)


def result_hash_for(*, action_ref: str, revision: int, total_evidence_count: int,
                    evidence_chain_commitment: str, total_supersession_count: int,
                    status: str, reason_code: str) -> str:
    """Fixed-size result hash: binds the whole sequence through the commitment."""
    return stable_hash((action_ref, int(revision), int(total_evidence_count),
                        evidence_chain_commitment, int(total_supersession_count),
                        status, reason_code, MEANING_VERSION))


def payload(record: ContactReconciliationRecordV2) -> dict[str, Any]:
    return {
        "reconciliation_ref": record.reconciliation_ref, "action_ref": record.action_ref,
        "revision": record.revision,
        "previous_reconciliation_ref": record.previous_reconciliation_ref,
        "new_evidence_refs": list(record.new_evidence_refs),
        "new_evidence_digest": record.new_evidence_digest,
        "total_evidence_count": record.total_evidence_count,
        "evidence_chain_commitment": record.evidence_chain_commitment,
        "new_supersession_refs": list(record.new_supersession_refs),
        "total_supersession_count": record.total_supersession_count,
        "status": record.status, "reason_code": record.reason_code,
        "result_hash": record.result_hash, "schema_version": record.schema_version,
        "meaning_version": record.meaning_version,
    }


def load(record_payload: dict[str, Any]) -> ContactReconciliationRecordV2:
    """Decode a persisted v2 record; any structural fault raises fail-closed."""
    try:
        data = dict(record_payload)
        data["new_evidence_refs"] = tuple(data["new_evidence_refs"])
        data["new_supersession_refs"] = tuple(data["new_supersession_refs"])
        return ContactReconciliationRecordV2(**data)
    except ReconciliationChainError:
        raise
    except (AttributeError, KeyError, TypeError, ValueError):
        raise ReconciliationChainError("v2 reconciliation payload is malformed",
                                       kind="malformed_record") from None


def validate_chain(records: Iterable[ContactReconciliationRecordV2]) -> None:
    """Verify a complete v2 chain for one action (parents, revisions, commitments)."""
    ordered = sorted(records, key=lambda record: record.revision)
    expected_revision = 1
    previous: ContactReconciliationRecordV2 | None = None
    for record in ordered:
        if record.revision != expected_revision:
            raise ReconciliationChainError(
                f"expected revision {expected_revision}, found {record.revision}",
                kind="revision_gap")
        if previous is None:
            if record.previous_reconciliation_ref is not None:
                raise ReconciliationChainError("first revision must not name a parent",
                                               kind="missing_parent")
            expected_commitment = chain_commitment(EMPTY_COMMITMENT, record.new_evidence_refs)
        else:
            if record.previous_reconciliation_ref != previous.reconciliation_ref:
                raise ReconciliationChainError("previous revision reference does not match",
                                               kind="missing_parent"
                                               if record.previous_reconciliation_ref is None
                                               else "wrong_action_parent")
            if record.action_ref != previous.action_ref:
                raise ReconciliationChainError("parent belongs to a different action",
                                               kind="wrong_action_parent")
            expected_commitment = chain_commitment(previous.evidence_chain_commitment,
                                                   record.new_evidence_refs)
            if record.total_evidence_count != previous.total_evidence_count + len(record.new_evidence_refs):
                raise ReconciliationChainError("evidence count does not continue the chain",
                                               kind="count_mismatch")
        if record.evidence_chain_commitment != expected_commitment:
            raise ReconciliationChainError("evidence chain commitment mismatch",
                                           kind="commitment_mismatch")
        previous = record
        expected_revision += 1


def materialize_evidence_refs(records: Iterable[ContactReconciliationRecordV2], *,
                              through_revision: int | None = None) -> tuple[str, ...]:
    """Derived read view: the ordered, unique evidence references of an action.

    Rebuilt purely from the v2 delta chain - never a new canonical list.
    """
    ordered = sorted(records, key=lambda record: record.revision)
    if through_revision is not None:
        ordered = [record for record in ordered if record.revision <= through_revision]
    validate_chain(ordered)
    refs: list[str] = []
    seen: set[str] = set()
    for record in ordered:
        for ref in record.new_evidence_refs:
            if ref in seen:
                raise ReconciliationChainError("evidence reference repeated across deltas",
                                               kind="duplicate_evidence_in_delta")
            seen.add(ref)
            refs.append(ref)
    return tuple(refs)


def materialize_supersession_refs(records: Iterable[ContactReconciliationRecordV2]) -> tuple[str, ...]:
    ordered = sorted(records, key=lambda record: record.revision)
    validate_chain(ordered)
    refs: list[str] = []
    seen: set[str] = set()
    for record in ordered:
        for ref in record.new_supersession_refs:
            if ref in seen:
                continue
            seen.add(ref)
            refs.append(ref)
    return tuple(refs)


@dataclass(frozen=True, slots=True)
class ReconciliationView:
    """Uniform read view over a v1 record or one v2 revision.

    Both contracts expose the same read surface, so consumers (result
    projection, handoff facts) never need to know which contract produced the
    action's latest reconciliation. A view is derived data: it is rebuilt from
    the canonical journal and is never persisted.

    A v2 view stores only its own delta plus a *pointer* to the previous
    revision; the active evidence list is folded lazily, once, and memoized.
    Writing a revision therefore costs O(delta) in time and memory instead of
    copying and re-sorting the whole accumulated history - the difference
    between a linear journal and a quadratic one. v1 supplies its already-final
    refs, which stay materialized.
    """

    action_ref: str
    reconciliation_ref: str
    contract_version: str
    revision: int
    state: str
    reason: str
    accumulated_evidence_refs: tuple[str, ...] = ()
    accumulated_supersession_refs: tuple[str, ...] = ()
    superseded_refs: tuple[str, ...] = ()
    evidence_chain_commitment: str = ""
    result_hash: str = ""
    accumulated_evidence_count: int = 0
    new_evidence_refs: tuple[str, ...] = ()
    previous: "ReconciliationView | None" = field(default=None, compare=False, repr=False)
    _supporting: tuple[str, ...] | None = field(default=None, compare=False, repr=False)

    def __post_init__(self) -> None:
        if self.contract_version not in {"v1", "v2"}:
            raise ReconciliationChainError("unknown reconciliation contract version",
                                           kind="unknown_contract_version")
        if not isinstance(self.action_ref, str) or not self.action_ref.startswith("actn:"):
            raise ReconciliationChainError("view requires an action reference", kind="malformed_record")
        if self.state not in STATES:
            raise ReconciliationChainError("view requires a valid state", kind="malformed_record")
        if type(self.revision) is not int or self.revision < 1:
            raise ReconciliationChainError("view requires a positive revision", kind="malformed_record")

    @property
    def evidence_count(self) -> int:
        """Active evidence count without materializing the reference list."""
        if self.accumulated_evidence_count:
            return self.accumulated_evidence_count
        return len(self.supporting_evidence_refs)

    @property
    def supporting_evidence_refs(self) -> tuple[str, ...]:
        """Active evidence: accumulated history minus the superseded references.

        Folded iteratively from the delta chain (no recursion, so a long action
        cannot hit the recursion limit) and memoized on this instance. The
        result of a historical revision never changes when a later correction
        arrives, because each node only ever reads its own predecessors.
        """
        cached = self._supporting
        if cached is not None:
            return cached
        deltas: list[tuple[str, ...]] = []
        superseded: set[str] = set()
        refs: list[str] = []
        node: ReconciliationView | None = self
        while node is not None:
            if node._supporting is not None:
                refs = list(node._supporting)
                break
            if node.contract_version == "v2":
                if node.new_evidence_refs:
                    deltas.append(node.new_evidence_refs)
                superseded.update(node.superseded_refs)
                node = node.previous
                continue
            # v1 tail: the record already stores its final active refs.
            refs = list(node.accumulated_evidence_refs)
            superseded.update(node.superseded_refs)
            break
        for delta in reversed(deltas):
            refs.extend(delta)
        if superseded:
            refs = [ref for ref in refs if ref not in superseded]
        result = tuple(sorted(refs))
        object.__setattr__(self, "_supporting", result)
        return result

    @property
    def supersession_refs(self) -> tuple[str, ...]:
        return tuple(sorted(self.accumulated_supersession_refs))

    @property
    def evidence_fingerprint(self) -> str:
        """v1-compatible evidence fingerprint, computed on demand."""
        return stable_hash((self.supporting_evidence_refs, self.supersession_refs))


def v1_view(record: Any, *, revision: int) -> ReconciliationView:
    """Read view for a v1 record (the record already stores its final refs)."""
    return ReconciliationView(
        action_ref=record.action_ref, reconciliation_ref=record.reconciliation_ref,
        contract_version="v1", revision=revision, state=record.state, reason=record.reason,
        accumulated_evidence_refs=tuple(record.supporting_evidence_refs),
        accumulated_supersession_refs=tuple(record.supersession_refs),
        accumulated_evidence_count=len(record.supporting_evidence_refs))


def next_v2_view(previous: ReconciliationView | None, record: ContactReconciliationRecordV2, *,
                 superseded_refs: Iterable[str] = ()) -> ReconciliationView:
    """Derive the newest v2 revision's view incrementally from the previous one.

    Only the revision's own delta is stored; the active evidence list is folded
    on demand by `ReconciliationView.supporting_evidence_refs`, so this call is
    O(delta) in both time and memory.
    """
    prior_supersessions = previous.accumulated_supersession_refs if previous else ()
    known = set(prior_supersessions)
    return ReconciliationView(
        action_ref=record.action_ref, reconciliation_ref=record.reconciliation_ref,
        contract_version="v2", revision=record.revision, state=record.status,
        reason=record.reason_code,
        accumulated_supersession_refs=prior_supersessions + tuple(
            ref for ref in record.new_supersession_refs if ref not in known),
        superseded_refs=tuple(sorted(set(superseded_refs))),
        evidence_chain_commitment=record.evidence_chain_commitment,
        result_hash=record.result_hash,
        accumulated_evidence_count=record.total_evidence_count,
        new_evidence_refs=tuple(record.new_evidence_refs),
        previous=previous)


def v2_views(records: Iterable[ContactReconciliationRecordV2], *,
             supersession_owners: Any = None) -> tuple[ReconciliationView, ...]:
    """Materialize one read view per v2 revision, in revision order.

    Each revision's active evidence excludes only the evidence superseded *by
    the supersessions that revision already commits to*, so the materialized
    view of a historical revision never changes because of a later correction.
    `supersession_owners` maps a supersession reference to the evidence
    reference it supersedes.
    """
    ordered = sorted(records, key=lambda record: record.revision)
    validate_chain(ordered)
    owners = supersession_owners or {}
    views: list[ReconciliationView] = []
    previous: ReconciliationView | None = None
    seen: set[str] = set()
    for record in ordered:
        if seen.intersection(record.new_evidence_refs):
            raise ReconciliationChainError("evidence reference repeated across deltas",
                                           kind="duplicate_evidence_in_delta")
        seen.update(record.new_evidence_refs)
        superseded = set(previous.superseded_refs) if previous else set()
        superseded |= {owners[ref] for ref in record.new_supersession_refs if ref in owners}
        previous = next_v2_view(previous, record, superseded_refs=superseded)
        views.append(previous)
    return tuple(views)


__all__ = [
    "MEANING_VERSION", "SCHEMA_VERSION", "V1_MEANING_VERSION", "V1_REF_PREFIX", "V2_REF_PREFIX",
    "CHAIN_FORMAT", "DELTA_FORMAT", "EMPTY_COMMITMENT", "STATES", "REASONS", "CHAIN_FAULT_KINDS",
    "ReconciliationChainError", "ContactReconciliationRecordV2", "frame_refs", "delta_digest",
    "chain_commitment", "contract_version_for", "result_hash_for", "payload", "load",
    "validate_chain", "materialize_evidence_refs", "materialize_supersession_refs",
    "ReconciliationView", "v1_view", "v2_views", "next_v2_view",
]
