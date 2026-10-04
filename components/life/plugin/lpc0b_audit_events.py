"""LPC0B WP2 audit events -- pure in-memory event construction.

Authority
---------
Order ``LPC0B-R3-Lite`` (千代第一版｜LPC0B-R3-Lite 交接施工单) section 三A/三B/三C/三D.
Design record: ``LPC0B_WP2_R3_DESIGN_DECISION.md`` (WP-A).

Why this module has no filesystem code
-------------------------------------
Section 三A requirement 1 and 2: the ``pre_llm_call`` hook must not wait for disk and must
not repair or rotate a journal.  Everything in this module is therefore allocation and
serialisation only -- it is imported by the hook thread, so it must never open a file,
never ``fsync``, and never block.

Record shape
------------
One JSONL line per record.  Every record participates in a single global hash chain, so a
segment boundary cannot hide a break::

    {"schema": "lpc0b.audit_record.v1",
     "seq": 17,
     "kind": "candidate_materialization",
     "at": "2026-10-01T14:02:11+08:00",
     "prev_entry_hash": "<hex or GENESIS>",
     "payload": {...},
     "entry_hash": "<hex>"}

``entry_hash`` covers the canonical JSON of every other field, so a reordered or edited
record is detectable.  The chain is *self-integrity only*: it is not an authority over any
canonical store (design record section 3.2).
"""
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Optional

# ---------------------------------------------------------------------------------
# schemas and kinds
# ---------------------------------------------------------------------------------

SCHEMA_RECORD = "lpc0b.audit_record.v1"
SCHEMA_HEALTH = "lpc0b.audit_health.v1"
SCHEMA_GAP = "lpc0b.audit_gap.v1"
SCHEMA_RECOVERY_GAP = "lpc0b.audit_recovery_gap.v1"
SCHEMA_SEGMENT = "lpc0b.audit_segment.v1"

KIND_CANDIDATE = "candidate_materialization"
KIND_OBSERVATION = "hook_observation"
KIND_METRICS = "metrics_snapshot"
KIND_INTEGRITY = "integrity_alarm"
KIND_GAP = "audit_gap"
KIND_RECOVERY_GAP = "audit_recovery_gap"
KIND_SEGMENT_HEADER = "segment_header"

#: The chain starts here.  A second ``GENESIS`` anywhere but the first record is a break.
GENESIS = "GENESIS"

#: Single hard cap on one serialised record.  Bounds writer-side memory and one disk write.
MAX_RECORD_BYTES = 8192

BJT = timezone(timedelta(hours=8))


# ---------------------------------------------------------------------------------
# small helpers (no I/O)
# ---------------------------------------------------------------------------------


def canonical_json(value: Any) -> str:
    """Deterministic JSON: sorted keys, no whitespace, unicode preserved."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def now_iso(at: Optional[float] = None) -> str:
    """Wall-clock stamp used for human reading only.  Never used for ordering."""
    dt = datetime.fromtimestamp(at, BJT) if at is not None else datetime.now(BJT)
    return dt.isoformat(timespec="seconds")


def monotonic_ms() -> int:
    """Ordering / liveness clock.  Immune to wall-clock steps."""
    return int(time.monotonic() * 1000)


def _clip(value: Any, limit: int = 300) -> Any:
    if isinstance(value, str) and len(value) > limit:
        return value[:limit] + f"...<clipped {len(value)} chars>"
    return value


def shallow_public(mapping: Mapping[str, Any], *, limit: int = 300) -> dict[str, Any]:
    """Copy a mapping, clipping long strings.

    The observation records already carry only hashed correlation ids and counts; the clip
    is a second line of defence so no unrelated caller can smuggle a long private string
    into the audit journal.
    """
    out: dict[str, Any] = {}
    for key, value in mapping.items():
        if isinstance(value, Mapping):
            out[str(key)] = shallow_public(value, limit=limit)
        elif isinstance(value, (list, tuple)):
            out[str(key)] = [_clip(v, limit) if not isinstance(v, (Mapping, list, tuple)) else v
                             for v in list(value)[:64]]
        else:
            out[str(key)] = _clip(value, limit)
    return out


# ---------------------------------------------------------------------------------
# the record
# ---------------------------------------------------------------------------------


def compute_entry_hash(seq: int, kind: str, at: str, payload: Mapping[str, Any],
                       prev_entry_hash: str) -> str:
    """Hash over every non-``entry_hash`` field, in canonical form."""
    return sha256_hex(canonical_json({
        "schema": SCHEMA_RECORD,
        "seq": int(seq),
        "kind": str(kind),
        "at": str(at),
        "prev_entry_hash": str(prev_entry_hash),
        "payload": dict(payload),
    }))


@dataclass
class AuditRecord:
    """One journal record.  Construction is pure; nothing here touches the disk."""

    seq: int
    kind: str
    at: str
    prev_entry_hash: str
    payload: dict[str, Any] = field(default_factory=dict)
    schema: str = SCHEMA_RECORD
    entry_hash: str = ""

    def __post_init__(self) -> None:
        if not self.entry_hash:
            self.entry_hash = compute_entry_hash(
                self.seq, self.kind, self.at, self.payload, self.prev_entry_hash)

    # -- serialisation ------------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "seq": int(self.seq),
            "kind": self.kind,
            "at": self.at,
            "prev_entry_hash": self.prev_entry_hash,
            "payload": dict(self.payload),
            "entry_hash": self.entry_hash,
        }

    def to_line(self) -> str:
        return canonical_json(self.to_dict())

    def size_bytes(self) -> int:
        return len(self.to_line().encode("utf-8")) + 1

    # -- validation ---------------------------------------------------------------

    def recomputed_hash(self) -> str:
        return compute_entry_hash(self.seq, self.kind, self.at, self.payload,
                                  self.prev_entry_hash)

    def verify_self(self) -> bool:
        return self.entry_hash == self.recomputed_hash()

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "AuditRecord":
        return cls(
            seq=int(raw["seq"]),
            kind=str(raw["kind"]),
            at=str(raw["at"]),
            prev_entry_hash=str(raw.get("prev_entry_hash", GENESIS)),
            payload=dict(raw.get("payload") or {}),
            schema=str(raw.get("schema") or SCHEMA_RECORD),
            entry_hash=str(raw.get("entry_hash") or ""),
        )

    @classmethod
    def from_line(cls, line: str) -> "AuditRecord":
        parsed = json.loads(line)
        if not isinstance(parsed, Mapping):
            raise ValueError("audit record line is not a JSON object")
        return cls.from_mapping(parsed)


class AuditEventError(ValueError):
    """Raised only by pure helpers; never by the hook path."""


# ---------------------------------------------------------------------------------
# special payload builders
# ---------------------------------------------------------------------------------


def gap_payload(*, dropped_count: int, reason: str, first_dropped_seq: Optional[int] = None,
                at: Optional[str] = None) -> dict[str, Any]:
    """Payload for ``KIND_GAP``: an explicitly recorded, never silent, hole.

    Section 三A requirement 5.  The hook cannot know the journal sequence, so it sends
    ``first_dropped_seq=None`` and the Audit Writer fills in the sequence at which the gap
    became durable.
    """
    return {
        "schema": SCHEMA_GAP,
        "first_dropped_seq": first_dropped_seq,
        "dropped_count": int(dropped_count),
        "reason": str(reason),
        "recorded_at": at or now_iso(),
    }


def recovery_gap_payload(*, segment: str, quarantined_from_byte: int, quarantined_bytes: int,
                         quarantined_sha256: str, quarantine_path: str,
                         reason: str) -> dict[str, Any]:
    """Payload for ``KIND_RECOVERY_GAP`` written by the Audit Writer after tail recovery.

    Section 三B: the damaged tail is isolated, the original bytes are preserved, and the
    recovery hole is recorded as data -- not merely as a log line.
    """
    return {
        "schema": SCHEMA_RECOVERY_GAP,
        "segment": str(segment),
        "quarantined_from_byte": int(quarantined_from_byte),
        "quarantined_bytes": int(quarantined_bytes),
        "quarantined_sha256": str(quarantined_sha256),
        "quarantine_path": str(quarantine_path),
        "reason": str(reason),
        "recorded_at": now_iso(),
    }


def segment_header_payload(*, segment_index: int, previous_segment: Optional[int],
                           prev_entry_hash: str) -> dict[str, Any]:
    """Payload for ``KIND_SEGMENT_HEADER`` -- keeps one chain across many segment files."""
    return {
        "schema": SCHEMA_SEGMENT,
        "segment_index": int(segment_index),
        "previous_segment": previous_segment,
        "previous_segment_head_hash": str(prev_entry_hash),
    }
