#!/usr/bin/env python3
"""Canonical Execution Ledger (M3).

Generalizes the existing ``life_execute.ExecutionJournal`` into one append-only,
hash-chained execution ledger that World and Life can both depend on -- **without
building a second mechanism and without rewriting the old journal.**

Design (frozen by the M3/M4/M5 ticket):

* **Evidence, not authority.**  The ledger records what was attempted and what
  happened.  World state remains the authority.  Nothing here replays side
  effects; replay is only used to *reconcile* and to *audit*.
* **Append-only, never rewritten.**  Reading is tolerant of both representations;
  writing only ever appends.
* **Two on-disk representations coexist.**  Entries written by the legacy
  ``ExecutionJournal`` keep ``kind="life_loop_execution_journal_entry"`` and
  ``schema_version=1`` and are returned verbatim.  Entries written here use
  ``kind="world_execution_ledger_entry"`` and ``schema_version="…v2"``.
* **Hash chaining.**  Legacy lines are hashed into a single deterministic
  *anchor*; every v2 entry carries ``prev_hash`` and ``entry_hash`` chained from
  that anchor.  Any edit to the legacy prefix or to a v2 entry is detected.
* **No scheduler.**  Pure read/write helpers; a caller drives everything.

Result vocabulary is the frozen three-value contract: ``SUCCESS`` / ``FAILURE`` /
``UNKNOWN``.  ``UNKNOWN`` is never silently downgraded to ``FAILURE`` and never
auto-retried.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

LEDGER_ENTRY_KIND = "world_execution_ledger_entry"
LEDGER_SCHEMA_VERSION = "world.execution.ledger.v2"
LEGACY_ENTRY_KIND = "life_loop_execution_journal_entry"
LEGACY_SCHEMA_VERSION = 1
DEFAULT_JOURNAL_NAME = "life_execution.jsonl"

RESULT_SUCCESS = "SUCCESS"
RESULT_FAILURE = "FAILURE"
RESULT_UNKNOWN = "UNKNOWN"
RESULTS = (RESULT_SUCCESS, RESULT_FAILURE, RESULT_UNKNOWN)

MAX_LINE_BYTES = 1 << 20
MAX_ID_LENGTH = 256

# Status values that are terminal vs in-flight.  These mirror the statuses the
# legacy journal already emitted, so old evidence keeps its original meaning.
TERMINAL_SUCCESS_STATUSES = frozenset({"committed"})
TERMINAL_FAILURE_STATUSES = frozenset({"rejected", "failed"})
IN_FLIGHT_STATUSES = frozenset({"prepared", "uncertain"})


class LedgerError(RuntimeError):
    """The ledger could not be read or written."""


class LedgerCorruptError(LedgerError):
    """A ledger line violates its contract."""


class LedgerTamperError(LedgerCorruptError):
    """The hash chain does not verify."""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def canonical_json(value: Any) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise LedgerError("value is not bounded JSON") from exc


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def ledger_path(hermes_home: Optional[Path | str] = None) -> Path:
    home = (
        Path(hermes_home).expanduser()
        if hermes_home is not None
        else Path(os.environ.get("HERMES_HOME") or (Path.home() / ".hermes"))
    )
    return home / "data" / DEFAULT_JOURNAL_NAME


# --------------------------------------------------------------------------
# normalized view over BOTH representations
# --------------------------------------------------------------------------


def _bounded(value: Any, field: str, *, maximum: int = MAX_ID_LENGTH) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str):
        raise LedgerCorruptError(f"{field} must be a string")
    text = value.strip()
    if not text or len(text) > maximum:
        raise LedgerCorruptError(f"{field} must be non-empty and bounded")
    return text


def normalize_record(record: Mapping[str, Any]) -> dict[str, Any]:
    """Map a legacy journal entry or a v2 ledger entry onto one bounded view."""

    if not isinstance(record, Mapping):
        raise LedgerCorruptError("ledger record must be an object")
    kind = record.get("kind")

    if kind == LEGACY_ENTRY_KIND:
        representation = "legacy"
        if record.get("schema_version") != LEGACY_SCHEMA_VERSION:
            raise LedgerCorruptError("legacy journal schema is not supported")
    elif kind == LEDGER_ENTRY_KIND:
        representation = "v2"
        if record.get("schema_version") != LEDGER_SCHEMA_VERSION:
            raise LedgerCorruptError("ledger schema is not supported")
    else:
        raise LedgerCorruptError("ledger record kind is not supported")

    status = _bounded(record.get("status"), "status", maximum=64) or ""
    result = (
        RESULT_SUCCESS
        if status in TERMINAL_SUCCESS_STATUSES
        else RESULT_FAILURE
        if status in TERMINAL_FAILURE_STATUSES
        else RESULT_UNKNOWN
    )

    return {
        "representation": representation,
        "schema_version": record.get("schema_version"),
        "execution_id": _bounded(record.get("execution_id"), "execution_id"),
        "action_type": _bounded(record.get("action_type") or record.get("action"), "action"),
        "actor_id": _bounded(record.get("actor_id"), "actor_id"),
        "status": status,
        "reason_code": _bounded(record.get("reason_code"), "reason_code"),
        "result": result,
        "timestamp": _bounded(record.get("timestamp") or record.get("started_at"), "timestamp"),
        "reconciliation_required": bool(
            record.get("reconciliation_required") or status == "uncertain"
        ),
        "before_state_ref": _bounded(record.get("before_state_ref"), "before_state_ref"),
        "after_state_ref": _bounded(record.get("after_state_ref"), "after_state_ref"),
        "prev_hash": _bounded(record.get("prev_hash"), "prev_hash"),
        "entry_hash": _bounded(record.get("entry_hash"), "entry_hash"),
        "raw": dict(record),
    }


def frame_execution(records: list[dict[str, Any]], execution_id: str) -> dict[str, Any]:
    """Terminal view of one execution across every record it produced."""

    wanted = _bounded(execution_id, "execution_id")
    mine = [r for r in records if r["execution_id"] == wanted]
    if not mine:
        return {
            "execution_id": wanted,
            "known": False,
            "state": None,
            "result": None,
            "reason_code": None,
            "records": [],
        }

    last = mine[-1]
    state = last["status"]
    # A terminal status wins over a later in-flight artefact of the same run.
    terminal = [r for r in mine if r["status"] in TERMINAL_SUCCESS_STATUSES | TERMINAL_FAILURE_STATUSES]
    if terminal:
        last = terminal[-1]
        state = last["status"]

    return {
        "execution_id": wanted,
        "known": True,
        "state": state,
        "result": last["result"],
        "reason_code": last["reason_code"],
        "action_type": last["action_type"],
        "reconciliation_required": any(r["reconciliation_required"] for r in mine),
        "records": mine,
        "attempts": sum(1 for r in mine if r["status"] == "prepared"),
    }


# --------------------------------------------------------------------------
# the ledger
# --------------------------------------------------------------------------


class ExecutionLedger:
    """Append-only, hash-chained, dual-representation execution ledger."""

    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.lock_path = self.path.with_name(self.path.name + ".lock")

    # -- locking (same discipline as the legacy ExecutionJournal) ----------

    @contextmanager
    def locked(self) -> Iterator[list[dict[str, Any]]]:
        if fcntl is None:
            raise LedgerError("execution ledger lock unavailable")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.lock_path.open("a+") as handle:
            deadline = time.monotonic() + 10.0
            while True:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError as exc:
                    if time.monotonic() >= deadline:
                        raise LedgerError("execution ledger lock timeout") from exc
                    time.sleep(0.02)
            try:
                yield self.read_all()
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    # -- reading -----------------------------------------------------------

    def _raw_lines(self) -> list[str]:
        if not self.path.exists():
            return []
        try:
            text = self.path.read_text(encoding="utf-8")
        except OSError as exc:
            raise LedgerError("execution ledger cannot be read") from exc
        return [line for line in text.splitlines() if line.strip()]

    def read_all(self) -> list[dict[str, Any]]:
        """Every record, in file order, normalized.  Legacy entries included."""

        records: list[dict[str, Any]] = []
        for number, line in enumerate(self._raw_lines(), 1):
            if len(line.encode("utf-8")) > MAX_LINE_BYTES:
                raise LedgerCorruptError(f"ledger line {number} exceeds the size bound")
            try:
                record = json.loads(line)
            except (TypeError, ValueError) as exc:
                raise LedgerCorruptError(f"ledger malformed at line {number}") from exc
            if not isinstance(record, Mapping):
                raise LedgerCorruptError(f"ledger contract invalid at line {number}")
            records.append(normalize_record(record))
        return records

    def _legacy_anchor(self) -> tuple[str, int, int]:
        """Deterministic anchor over the legacy prefix.

        Returns ``(anchor, legacy_count, v2_count)``.  The anchor is the digest
        of the *raw* legacy lines, so editing any legacy byte changes it.
        """

        legacy_lines: list[str] = []
        v2_lines: list[str] = []
        seen_v2 = False
        for number, line in enumerate(self._raw_lines(), 1):
            try:
                record = json.loads(line)
            except (TypeError, ValueError) as exc:
                raise LedgerCorruptError(f"ledger malformed at line {number}") from exc
            if isinstance(record, Mapping) and record.get("kind") == LEDGER_ENTRY_KIND:
                seen_v2 = True
                v2_lines.append(line)
            else:
                if seen_v2:
                    raise LedgerCorruptError(
                        f"legacy entry appended after a v2 entry at line {number}"
                    )
                legacy_lines.append(line)
        anchor = _digest("legacy-prefix:" + "\n".join(legacy_lines))
        return anchor, len(legacy_lines), len(v2_lines)

    def chain_status(self) -> dict[str, Any]:
        """Verify the hash chain.  Read-only; safe to call any time."""

        anchor, legacy_count, v2_count = self._legacy_anchor()
        errors: list[str] = []
        expected_prev = anchor
        checked = 0

        for record in self.read_all():
            if record["representation"] != "v2":
                continue
            raw = record["raw"]
            stored_prev = raw.get("prev_hash")
            stored_hash = raw.get("entry_hash")
            if not stored_prev or not stored_hash:
                errors.append(f"{record['execution_id']}: missing chain fields")
                continue
            if stored_prev != expected_prev:
                errors.append(
                    f"{record['execution_id']}: prev_hash does not match the chain"
                )
            recomputed = _chain_hash(raw)
            if recomputed != stored_hash:
                errors.append(f"{record['execution_id']}: entry_hash does not verify")
            expected_prev = stored_hash
            checked += 1

        return {
            "valid": not errors,
            "anchor": anchor,
            "legacy_entries": legacy_count,
            "v2_entries": v2_count,
            "verified_v2_entries": checked,
            "errors": errors,
        }

    def verify(self) -> dict[str, Any]:
        """Verify and raise on suspicion; returns the chain status otherwise."""

        status = self.chain_status()
        if not status["valid"]:
            raise LedgerTamperError("execution ledger hash chain does not verify")
        return status

    # -- frames ------------------------------------------------------------

    def frame(self, execution_id: str) -> dict[str, Any]:
        return frame_execution(self.read_all(), execution_id)

    # -- writing -----------------------------------------------------------

    def append_unlocked(self, entry: Mapping[str, Any]) -> dict[str, Any]:
        """Append one v2 entry, chained onto the current tail.  Caller holds lock."""

        anchor, _legacy, _v2 = self._legacy_anchor()
        existing = self.read_all()
        prev = anchor
        for record in existing:
            if record["representation"] == "v2" and record["entry_hash"]:
                prev = record["entry_hash"]

        payload: dict[str, Any] = {
            "kind": LEDGER_ENTRY_KIND,
            "schema_version": LEDGER_SCHEMA_VERSION,
            "execution_id": _bounded(entry.get("execution_id"), "execution_id"),
            "action_type": _bounded(entry.get("action_type"), "action_type"),
            "actor_id": _bounded(entry.get("actor_id"), "actor_id"),
            "status": _bounded(entry.get("status"), "status", maximum=64),
            "reason_code": _bounded(entry.get("reason_code"), "reason_code"),
            "timestamp": _bounded(entry.get("timestamp") or _now_iso(), "timestamp"),
            "proposal_ref": _bounded(entry.get("proposal_ref"), "proposal_ref"),
            "observed_dependencies": entry.get("observed_dependencies") or [],
            "expected_versions": entry.get("expected_versions") or {},
            "world_mutation_refs": entry.get("world_mutation_refs") or [],
            "body_consequence_refs": entry.get("body_consequence_refs") or [],
            "cross_authority_refs": entry.get("cross_authority_refs") or [],
            "before_state_ref": _bounded(entry.get("before_state_ref"), "before_state_ref"),
            "after_state_ref": _bounded(entry.get("after_state_ref"), "after_state_ref"),
            "before": entry.get("before"),
            "after": entry.get("after"),
            "reconciliation_required": bool(entry.get("reconciliation_required")),
            "previous_hash": prev,
            "prev_hash": prev,
        }
        payload["entry_hash"] = _chain_hash(payload)

        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(canonical_json(payload) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
        except OSError as exc:
            raise LedgerError("execution ledger cannot be appended") from exc
        return payload

    def append(self, entry: Mapping[str, Any]) -> dict[str, Any]:
        with self.locked():
            return self.append_unlocked(entry)

    # -- evidence helpers --------------------------------------------------

    def state_ref(self, value: Optional[Mapping[str, Any]]) -> Optional[str]:
        if value is None:
            return None
        return _digest(canonical_json(value))


def _chain_hash(payload: Mapping[str, Any]) -> str:
    """Digest over everything except the hash fields themselves."""

    body = {
        key: value
        for key, value in payload.items()
        if key not in {"entry_hash", "previous_hash", "prev_hash"}
    }
    return _digest(canonical_json(body))
