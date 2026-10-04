"""Verified durable checkpoint: a *discardable bookmark* over the canonical journal.

Authority boundary (permanent invariant)
----------------------------------------
    journal     = canonical durable truth
    checkpoint  = derived state, rebuildable, deletable, never a truth source
    projection  = derived, rebuildable
    index/cache = derived, rebuildable

A checkpoint records "the derived state below was produced from journal records
1..through_sequence whose hash chain ends at through_record_hash".  It is only
ever used after its self-hash, schema, journal identity, anchor and prefix
digests have been re-verified against the journal itself.  Any problem at all -
missing, corrupt, stale, future, wrong journal, anchor mismatch, unsupported
schema - is reported as :class:`CheckpointInvalid` (never as journal corruption)
and the caller falls back to a full journal replay.

A checkpoint may never delete history, never introduce a fact that the journal
does not support, and may never be the authority for a semantic decision.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from durable.durable_file_ops import DurableFileOps, atomic_write_bytes

CHECKPOINT_FORMAT_VERSION = "chiyo.ct0.durable_checkpoint.format.v1"
CHECKPOINT_SCHEMA_VERSION = "chiyo.ct0.durable_checkpoint.v1"
CHECKPOINT_POLICY_VERSION = "ct0.durable_checkpoint.policy.v1"
DEFAULT_CHECKPOINT_EVERY_RECORDS = 500

INVALID_KINDS = (
    "MISSING",
    "CORRUPT",
    "SCHEMA_UNSUPPORTED",
    "FORMAT_UNSUPPORTED",
    "WRONG_JOURNAL",
    "ANCHOR_MISMATCH",
    "STALE",
    "FUTURE",
    "PAYLOAD_INVALID",
)

GENESIS = "0" * 64


class CheckpointInvalid(RuntimeError):
    """A checkpoint cannot be trusted.  This is never journal corruption."""

    def __init__(self, message: str, *, kind: str) -> None:
        if kind not in INVALID_KINDS:
            raise ValueError("unsupported checkpoint invalid kind")
        super().__init__(message)
        self.kind = kind


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def checkpoint_path(store_path: Path | str) -> Path:
    """Sidecar path for a store's checkpoint (never inside the journal itself)."""
    target = Path(store_path)
    return target.with_name(f"{target.name}.checkpoint")


def structural_digest(rows: Iterable[tuple[Any, ...]]) -> str:
    """Digest of (sequence, record_hash, previous_hash) for rows 1..N.

    Cheap on purpose: no payload parsing and no per-row content hashing, yet any
    deletion, reordering or record_hash/previous_hash tampering in the prefix
    changes the digest.
    """
    digest = hashlib.sha256()
    for sequence, record_hash, previous_hash in rows:
        digest.update(f"{int(sequence)}\u0000{record_hash}\u0000{previous_hash}\u0001".encode("utf-8"))
    return digest.hexdigest()


def payload_digest(rows: Iterable[tuple[Any, ...]]) -> str:
    """Digest over the raw stored payload text of rows 1..N.

    Detects a prefix payload edit that left the stored record_hash untouched -
    the same tampering the full row validator catches - without paying for a
    JSON parse or a per-row canonical re-serialisation.
    """
    digest = hashlib.sha256()
    for sequence, payload_text in rows:
        digest.update(f"{int(sequence)}\u0000".encode("utf-8"))
        digest.update(payload_text.encode("utf-8") if isinstance(payload_text, str) else bytes(payload_text))
        digest.update(b"\x01")
    return digest.hexdigest()


def derived_state_digest(state: Any) -> str:
    """Digest of a derived-state payload (canonical JSON)."""
    return hashlib.sha256(_canonical(state).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class VerifiedDurableCheckpoint:
    checkpoint_id: str
    format_version: str
    schema_version: str
    policy_version: str
    journal_identity: str
    through_sequence: int
    through_record_id: str
    through_record_hash: str
    created_at: str
    prefix_structural_digest: str
    prefix_payload_digest: str
    derived_state_hash: str
    checkpoint_payload: dict[str, Any]
    payload_digest: str
    checkpoint_hash: str


def _hash_material(*, format_version: str, schema_version: str, policy_version: str,
                   journal_identity: str, through_sequence: int, through_record_id: str,
                   through_record_hash: str, created_at: str, prefix_structural_digest: str,
                   prefix_payload_digest: str, derived_state_hash: str,
                   payload_digest: str) -> dict[str, Any]:
    return {
        "format_version": format_version,
        "schema_version": schema_version,
        "policy_version": policy_version,
        "journal_identity": journal_identity,
        "through_sequence": int(through_sequence),
        "through_record_id": through_record_id,
        "through_record_hash": through_record_hash,
        "created_at": created_at,
        "prefix_structural_digest": prefix_structural_digest,
        "prefix_payload_digest": prefix_payload_digest,
        "derived_state_hash": derived_state_hash,
        "payload_digest": payload_digest,
    }


def build_checkpoint(*, journal_identity: str, through_sequence: int, through_record_id: str,
                     through_record_hash: str, created_at: str, prefix_structural_digest: str,
                     prefix_payload_digest: str, payload: dict[str, Any],
                     derived_state_hash: str | None = None) -> VerifiedDurableCheckpoint:
    """Mint a checkpoint record; the caller is responsible for its correctness."""
    if int(through_sequence) < 1:
        raise ValueError("a checkpoint must anchor on at least one committed record")
    state_hash = derived_state_hash or derived_state_digest(payload)
    payload_digest_value = hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()
    material = _hash_material(
        format_version=CHECKPOINT_FORMAT_VERSION, schema_version=CHECKPOINT_SCHEMA_VERSION,
        policy_version=CHECKPOINT_POLICY_VERSION, journal_identity=journal_identity,
        through_sequence=through_sequence, through_record_id=through_record_id,
        through_record_hash=through_record_hash, created_at=created_at,
        prefix_structural_digest=prefix_structural_digest, prefix_payload_digest=prefix_payload_digest,
        derived_state_hash=state_hash, payload_digest=payload_digest_value)
    checkpoint_hash = hashlib.sha256(_canonical(material).encode("utf-8")).hexdigest()
    return VerifiedDurableCheckpoint(
        checkpoint_id=f"ckpt:{checkpoint_hash[:32]}", format_version=CHECKPOINT_FORMAT_VERSION,
        schema_version=CHECKPOINT_SCHEMA_VERSION, policy_version=CHECKPOINT_POLICY_VERSION,
        journal_identity=journal_identity, through_sequence=int(through_sequence),
        through_record_id=through_record_id, through_record_hash=through_record_hash,
        created_at=created_at, prefix_structural_digest=prefix_structural_digest,
        prefix_payload_digest=prefix_payload_digest, derived_state_hash=state_hash,
        checkpoint_payload=payload, payload_digest=payload_digest_value,
        checkpoint_hash=checkpoint_hash)


def checkpoint_document(checkpoint: VerifiedDurableCheckpoint) -> dict[str, Any]:
    return {
        "checkpoint_id": checkpoint.checkpoint_id,
        "format_version": checkpoint.format_version,
        "schema_version": checkpoint.schema_version,
        "policy_version": checkpoint.policy_version,
        "journal_identity": checkpoint.journal_identity,
        "through_sequence": checkpoint.through_sequence,
        "through_record_id": checkpoint.through_record_id,
        "through_record_hash": checkpoint.through_record_hash,
        "created_at": checkpoint.created_at,
        "prefix_structural_digest": checkpoint.prefix_structural_digest,
        "prefix_payload_digest": checkpoint.prefix_payload_digest,
        "derived_state_hash": checkpoint.derived_state_hash,
        "payload_digest": checkpoint.payload_digest,
        "checkpoint_hash": checkpoint.checkpoint_hash,
        "checkpoint_payload": checkpoint.checkpoint_payload,
    }


def write_checkpoint(path: Path | str, checkpoint: VerifiedDurableCheckpoint, *,
                     ops: DurableFileOps | None = None) -> dict[str, Any]:
    """Atomically publish a checkpoint through the durable IO seam."""
    document = _canonical(checkpoint_document(checkpoint))
    result = atomic_write_bytes(path, (document + "\n").encode("utf-8"), ops=ops)
    return {**result, "checkpoint_id": checkpoint.checkpoint_id,
            "through_sequence": checkpoint.through_sequence}


def read_checkpoint(path: Path | str) -> VerifiedDurableCheckpoint | None:
    """Read and self-verify a checkpoint; `None` means "no checkpoint present"."""
    target = Path(path)
    if not target.exists():
        return None
    try:
        raw = target.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise CheckpointInvalid(f"checkpoint is unreadable: {type(exc).__name__}",
                                kind="CORRUPT") from exc
    try:
        document = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise CheckpointInvalid("checkpoint is not valid JSON", kind="CORRUPT") from exc
    if not isinstance(document, dict):
        raise CheckpointInvalid("checkpoint document must be an object", kind="CORRUPT")
    for field in ("checkpoint_id", "format_version", "schema_version", "policy_version",
                  "journal_identity", "through_sequence", "through_record_id",
                  "through_record_hash", "created_at", "prefix_structural_digest",
                  "prefix_payload_digest", "derived_state_hash", "payload_digest",
                  "checkpoint_hash", "checkpoint_payload"):
        if field not in document:
            raise CheckpointInvalid(f"checkpoint is missing field {field!r}", kind="CORRUPT")
    if document["format_version"] != CHECKPOINT_FORMAT_VERSION:
        raise CheckpointInvalid("unsupported checkpoint format version", kind="FORMAT_UNSUPPORTED")
    if document["schema_version"] != CHECKPOINT_SCHEMA_VERSION:
        raise CheckpointInvalid("unsupported checkpoint schema version", kind="SCHEMA_UNSUPPORTED")
    try:
        payload = document["checkpoint_payload"]
        if not isinstance(payload, dict):
            raise TypeError("payload must be an object")
        sequence = int(document["through_sequence"])
    except (TypeError, ValueError) as exc:
        raise CheckpointInvalid("checkpoint fields are malformed", kind="CORRUPT") from exc
    recomputed_payload_digest = hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()
    if recomputed_payload_digest != document["payload_digest"]:
        raise CheckpointInvalid("checkpoint payload does not match its digest", kind="CORRUPT")
    material = _hash_material(
        format_version=document["format_version"], schema_version=document["schema_version"],
        policy_version=document["policy_version"], journal_identity=document["journal_identity"],
        through_sequence=sequence, through_record_id=document["through_record_id"],
        through_record_hash=document["through_record_hash"], created_at=document["created_at"],
        prefix_structural_digest=document["prefix_structural_digest"],
        prefix_payload_digest=document["prefix_payload_digest"],
        derived_state_hash=document["derived_state_hash"], payload_digest=document["payload_digest"])
    recomputed_hash = hashlib.sha256(_canonical(material).encode("utf-8")).hexdigest()
    if recomputed_hash != document["checkpoint_hash"]:
        raise CheckpointInvalid("checkpoint self hash mismatch", kind="CORRUPT")
    if document["checkpoint_id"] != f"ckpt:{recomputed_hash[:32]}":
        raise CheckpointInvalid("checkpoint id does not match its hash", kind="CORRUPT")
    if derived_state_digest(payload) != document["derived_state_hash"]:
        raise CheckpointInvalid("checkpoint derived state hash mismatch", kind="PAYLOAD_INVALID")
    return VerifiedDurableCheckpoint(
        checkpoint_id=document["checkpoint_id"], format_version=document["format_version"],
        schema_version=document["schema_version"], policy_version=document["policy_version"],
        journal_identity=document["journal_identity"], through_sequence=sequence,
        through_record_id=document["through_record_id"],
        through_record_hash=document["through_record_hash"], created_at=document["created_at"],
        prefix_structural_digest=document["prefix_structural_digest"],
        prefix_payload_digest=document["prefix_payload_digest"],
        derived_state_hash=document["derived_state_hash"], checkpoint_payload=payload,
        payload_digest=document["payload_digest"], checkpoint_hash=document["checkpoint_hash"])


def verify_against_journal(checkpoint: VerifiedDurableCheckpoint, *,
                           journal_identity: str, journal_max_sequence: int,
                           prefix_structural: str, prefix_payload: str,
                           anchor_record_hash: str | None) -> None:
    """Re-verify a checkpoint against the journal it claims to summarize."""
    if checkpoint.journal_identity != journal_identity:
        raise CheckpointInvalid("checkpoint belongs to a different journal",
                                kind="WRONG_JOURNAL")
    if checkpoint.through_sequence > int(journal_max_sequence):
        raise CheckpointInvalid("checkpoint is ahead of the journal", kind="FUTURE")
    if anchor_record_hash is None:
        raise CheckpointInvalid("checkpoint anchor record is absent from the journal",
                                kind="ANCHOR_MISMATCH")
    if anchor_record_hash != checkpoint.through_record_hash:
        raise CheckpointInvalid("checkpoint anchor hash mismatch", kind="ANCHOR_MISMATCH")
    if prefix_structural != checkpoint.prefix_structural_digest:
        raise CheckpointInvalid("journal prefix structure changed since the checkpoint",
                                kind="ANCHOR_MISMATCH")
    if prefix_payload != checkpoint.prefix_payload_digest:
        raise CheckpointInvalid("journal prefix payload changed since the checkpoint",
                                kind="ANCHOR_MISMATCH")


def drop_checkpoint(path: Path | str) -> bool:
    """Discard a checkpoint.  Never touches the journal."""
    target = Path(path)
    try:
        target.unlink()
    except FileNotFoundError:
        return False
    return True


__all__ = [
    "CHECKPOINT_FORMAT_VERSION", "CHECKPOINT_SCHEMA_VERSION", "CHECKPOINT_POLICY_VERSION",
    "DEFAULT_CHECKPOINT_EVERY_RECORDS", "INVALID_KINDS", "GENESIS", "CheckpointInvalid",
    "VerifiedDurableCheckpoint", "checkpoint_path", "structural_digest", "payload_digest",
    "derived_state_digest", "build_checkpoint", "checkpoint_document", "write_checkpoint",
    "read_checkpoint", "verify_against_journal", "drop_checkpoint",
]
