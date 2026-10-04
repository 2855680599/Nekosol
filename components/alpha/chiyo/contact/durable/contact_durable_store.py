"""Sandbox-only durable receipt journal and internal outboxes.

SQLite is the sole canonical journal.  Indexes, projections and the durable
checkpoint are *derived*: they are rebuilt from the append-only, SHA-256
hash-chained events.  The checkpoint is a discardable bookmark that only
accelerates reconstruction; it is never a source of truth.  This module cannot
submit/send.

Hot path vs recovery path
-------------------------
Mutating operations (`accept_evidence`, `accept_correction`, `reconcile`,
`record_consumer_receipt`) keep an in-memory derived view of the journal and
validate only newly appended records plus the unread tail.  Every read/repair
API (`recover`, `verify`, `journal_rows`, `outbox_items`, `project`) still
re-derives from the canonical journal with full row validation, so external
journal tampering is always caught there and on the next open.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
from chiyo.contact.durable.sqlite_safety import configure_journal
import sqlite3
import threading
import uuid
from typing import Any, Iterable

from contact_receipt_model import ContactTransportReceiptEvidence, ReceiptSupersession
from contact_receipt_store import ContactReceiptStore, ReceiptJournalEntry
from contact_reconciliation import ContactReconciliationAuthority, ContactReconciliationRecord
from contact_settlement_v2 import (
    ContactSettlementRequestV2, SettlementContractError, contract_version_for as settlement_contract_version,
    create as create_settlement_v2, handoff_payload as settlement_handoff_payload,
    load as load_settlement_v2, payload as settlement_v2_payload,
)
from contact_reconciliation_v2 import (
    ContactReconciliationRecordV2, ReconciliationChainError, ReconciliationView,
    contract_version_for, load as load_reconciliation_v2, next_v2_view,
    payload as reconciliation_v2_payload, v1_view, v2_views,
)
from contact_result_projection import ContactActionResultProjector, ContactActionResultProjection
from contact_settlement_bridge import ContactSettlementRequest

try:  # package import (src on sys.path, imported as ct0.durable.*)
    from ct0.durable.checkpoint import (
        CheckpointInvalid, DEFAULT_CHECKPOINT_EVERY_RECORDS, build_checkpoint, checkpoint_path,
        payload_digest, read_checkpoint, structural_digest, verify_against_journal, write_checkpoint,
    )
    from ct0.durable.durable_file_ops import DurableFileOps, DurableIOError
except ModuleNotFoundError:  # pragma: no cover - flat import (src/ct0 on sys.path)
    from durable.checkpoint import (
        CheckpointInvalid, DEFAULT_CHECKPOINT_EVERY_RECORDS, build_checkpoint, checkpoint_path,
        payload_digest, read_checkpoint, structural_digest, verify_against_journal, write_checkpoint,
    )
    from durable.durable_file_ops import DurableFileOps, DurableIOError

RECORD_SCHEMA = "ct0.durable_contact_record.v1"
STORE_SCHEMA_VERSION = 2
GENESIS_HASH = "0" * 64
OUTBOX_STATES = frozenset({"PENDING", "DELIVERED", "ACCEPTED", "REJECTED", "DEFERRED", "CONFLICT"})
CONSUMER_OUTCOMES = frozenset({"ACCEPT", "IGNORE", "DEFER", "REJECT", "CONFLICT"})
DEFAULT_RECONCILIATION_CONTRACT = "v2"

COUNTER_KEYS = (
    "full_journal_replays",
    "checkpoint_loads",
    "checkpoint_rejects",
    "checkpoint_fallbacks",
    "checkpoint_missing",
    "checkpoint_writes",
    "checkpoint_write_failures",
    "tail_records_replayed",
    "records_parsed",
    "record_cache_hits",
    "record_cache_misses",
    "incremental_apply_count",
    "journal_records_scanned",
    "journal_records_scanned_per_operation",
)


class DurableStoreError(RuntimeError):
    """Durable state is invalid, unsupported, or cannot be committed safely."""


class JournalCorruptionError(DurableStoreError):
    """Canonical journal integrity failed; operator maintenance is required.

    Context is deliberately limited to structural coordinates. Payload bytes
    and decoder messages are never copied into this exception.
    """

    def __init__(self, message: str, *, kind: str = "journal_corruption",
                 path: str | os.PathLike[str] | None = None, row: int | None = None,
                 line: int | None = None, offset: int | None = None,
                 recovery_possible: bool = False) -> None:
        super().__init__(message)
        self.kind = kind
        self.path_basename = Path(path).name if path is not None else None
        self.path = self.path_basename
        self.row = row
        self.line = line
        self.offset = offset
        self.recovery_possible = bool(recovery_possible)


class UnsupportedSchemaError(DurableStoreError):
    """A future/unknown schema is rejected fail-closed."""


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _payload_evidence(item: ContactTransportReceiptEvidence) -> dict[str, Any]:
    result = asdict(item)
    result["provenance_refs"] = list(item.provenance_refs)
    return result


def _load_evidence(value: dict[str, Any]) -> ContactTransportReceiptEvidence:
    if value.get("schema_version") != "chiyo.contact.transport_receipt_evidence.v1":
        raise UnsupportedSchemaError("unsupported receipt evidence schema")
    try:
        data = dict(value)
        data["provenance_refs"] = tuple(data["provenance_refs"])
        return ContactTransportReceiptEvidence(**data)
    except (AttributeError, KeyError, TypeError, ValueError):
        raise ValueError("receipt payload is invalid") from None


def _payload_recon(item: ContactReconciliationRecord) -> dict[str, Any]:
    result = asdict(item)
    result["supporting_evidence_refs"] = list(item.supporting_evidence_refs)
    result["supersession_refs"] = list(item.supersession_refs)
    return result


def _load_recon(value: dict[str, Any]) -> ContactReconciliationRecord:
    try:
        data = dict(value)
        data["supporting_evidence_refs"] = tuple(data["supporting_evidence_refs"])
        data["supersession_refs"] = tuple(data["supersession_refs"])
        return ContactReconciliationRecord(**data)
    except (AttributeError, KeyError, TypeError, ValueError):
        raise ValueError("reconciliation payload is invalid") from None


class _DerivedState:
    """In-memory derived view of the canonical journal (never a truth source).

    Rebuildable at any moment from journal rows alone: dropping this object and
    re-deriving produces identical content.
    """

    __slots__ = ("sequence", "head_hash", "events", "receipts", "supersessions",
                 "reconciliations", "receipt_store", "authority", "outbox",
                 "consumer_receipts", "revisions", "attempts", "receipts_by_attempt",
                 "read_views", "views_by_ref", "reconciliation_versions", "v2_records",
                 "settlement_versions", "active_stats", "evidence_totals",
                 "supersessions_by_action", "active_refs", "pending_refs",
                 "pending_supersessions")

    def __init__(self) -> None:
        self.sequence = 0
        self.head_hash = GENESIS_HASH
        self.events: list[dict[str, Any]] = []
        self.receipts: list[ContactTransportReceiptEvidence] = []
        self.supersessions: list[ReceiptSupersession] = []
        self.reconciliations: list[ContactReconciliationRecord] = []
        self.receipt_store = ContactReceiptStore()
        self.authority = ContactReconciliationAuthority(store=self.receipt_store)
        self.outbox: dict[str, dict[str, Any]] = {}
        self.consumer_receipts: dict[str, dict[str, Any]] = {}
        self.revisions: dict[str, int] = {}
        self.attempts: dict[str, int] = {}
        self.receipts_by_attempt: dict[tuple[str, str], dict[str, Any]] = {}
        self.read_views: list[ReconciliationView] = []
        self.views_by_ref: dict[str, ReconciliationView] = {}
        self.reconciliation_versions: dict[str, str] = {}
        self.v2_records: dict[str, list[ContactReconciliationRecordV2]] = {}
        self.settlement_versions: dict[str, str] = {}
        # Incremental projection counters. Every one of these is maintained in
        # O(1) (or O(delta)) per event so that reconciling an action costs the
        # same whether it has ten evidence rows or ten thousand. They are
        # derived data: a full replay rebuilds them from scratch, and the test
        # suite asserts the two paths agree.
        self.active_stats: dict[str, dict[str, Any]] = {}
        self.evidence_totals: dict[str, int] = {}
        self.supersessions_by_action: dict[str, list[ReceiptSupersession]] = {}
        self.active_refs: dict[str, set[str]] = {}
        self.pending_refs: dict[str, set[str]] = {}
        self.pending_supersessions: dict[str, set[str]] = {}


class DurableContactReceiptStore:
    """SQLite WAL journal with process-safe transactions and rebuildable views.

    Writers use `BEGIN IMMEDIATE`; SQLite OS locks serialize processes and are
    released after termination. WAL + synchronous=FULL requests durable commits,
    subject to the host filesystem/device honoring SQLite flush semantics.
    """

    def __init__(self, path: str | os.PathLike[str], *, busy_timeout_ms: int = 10000,
                 checkpoint_every_records: int = DEFAULT_CHECKPOINT_EVERY_RECORDS,
                 file_ops: DurableFileOps | None = None) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.busy_timeout_ms = int(busy_timeout_ms)
        if self.busy_timeout_ms < 1:
            raise ValueError("busy_timeout_ms must be positive")
        self.checkpoint_every_records = int(checkpoint_every_records)
        self._file_ops = file_ops
        self._thread_lock = threading.RLock()
        self._parsed_memo: dict[str, Any] = {}
        self._state: _DerivedState | None = None
        self._identity = ""
        self._last_checkpoint_sequence = 0
        self._counters: dict[str, Any] = {key: 0 for key in COUNTER_KEYS}
        self._counters["checkpoint_last_reject"] = None
        self._counters["checkpoint_last_write_error"] = None
        self._initialize()

    # -- durable IO seam -------------------------------------------------
    def _ops(self) -> DurableFileOps:
        if self._file_ops is None:
            try:
                from ct0.durable.durable_file_ops import RealDurableFileOps
            except ModuleNotFoundError:  # pragma: no cover
                from durable.durable_file_ops import RealDurableFileOps
            self._file_ops = RealDurableFileOps()
        return self._file_ops

    @contextmanager
    def _connect(self):
        conn = sqlite3.connect(str(self.path), timeout=self.busy_timeout_ms / 1000,
                               isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute(f"PRAGMA busy_timeout={self.busy_timeout_ms}")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA synchronous=FULL")
            yield conn
        finally:
            conn.close()

    def _initialize(self) -> None:
        with self._thread_lock, self._connect() as conn:
            configure_journal(conn)
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1, STORE_SCHEMA_VERSION):
                raise UnsupportedSchemaError(f"unsupported store schema version {version}")
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute("""CREATE TABLE IF NOT EXISTS journal (
                    sequence INTEGER PRIMARY KEY, record_id TEXT NOT NULL UNIQUE,
                    record_type TEXT NOT NULL, schema_version TEXT NOT NULL,
                    action_ref TEXT NOT NULL, causal_refs TEXT NOT NULL,
                    payload TEXT NOT NULL, recorded_at TEXT NOT NULL,
                    previous_hash TEXT NOT NULL, record_hash TEXT NOT NULL UNIQUE)""")
                conn.execute("""CREATE TABLE IF NOT EXISTS outbox_projection (
                    outbox_id TEXT PRIMARY KEY, action_ref TEXT NOT NULL,
                    result_revision INTEGER NOT NULL, evidence_refs TEXT NOT NULL,
                    payload TEXT NOT NULL, payload_hash TEXT NOT NULL, status TEXT NOT NULL,
                    attempt_count INTEGER NOT NULL, created_at TEXT NOT NULL,
                    delivered_at TEXT, consumer_receipt_ref TEXT)""")
                conn.execute("""CREATE TABLE IF NOT EXISTS consumer_receipt_projection (
                    outbox_id TEXT PRIMARY KEY, payload_hash TEXT NOT NULL,
                    outcome TEXT NOT NULL, receipt_ref TEXT NOT NULL, accepted_at TEXT NOT NULL)""")
                conn.execute("""CREATE TABLE IF NOT EXISTS store_meta (
                    key TEXT PRIMARY KEY, value TEXT NOT NULL)""")
                identity_row = conn.execute("SELECT value FROM store_meta WHERE key='identity'").fetchone()
                if identity_row is None:
                    identity = uuid.uuid4().hex
                    conn.execute("INSERT INTO store_meta VALUES('identity',?)", (identity,))
                else:
                    identity = identity_row["value"]
                self._identity = f"journal:{identity}"
                if version == 0:
                    conn.execute(f"PRAGMA user_version={STORE_SCHEMA_VERSION}")
                elif version == 1:
                    # Representation-only migration: preserve every old row/hash.
                    conn.execute(f"PRAGMA user_version={STORE_SCHEMA_VERSION}")
                    if conn.execute("SELECT COUNT(*) FROM journal").fetchone()[0]:
                        old_head = conn.execute("SELECT record_hash FROM journal ORDER BY sequence DESC LIMIT 1").fetchone()[0]
                        self._append_locked(conn, "SCHEMA_MIGRATION", "store:migration",
                            {"from_version": 1, "to_version": STORE_SCHEMA_VERSION,
                             "prior_head_hash": old_head}, _now(), "migration:v1-v2")
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    # -- identity / diagnostics -------------------------------------------
    @property
    def journal_identity(self) -> str:
        return self._identity

    def counters(self) -> dict[str, Any]:
        """Performance/authority diagnostics; safe to read at any time."""
        return {**self._counters, "identity": self._identity,
                "cached_sequence": None if self._state is None else self._state.sequence}

    def derived_state_digest(self) -> dict[str, Any]:
        """Digest of the *incremental* derived projection, for parity checking.

        Derived data only, never authority: it exists so a test or a
        qualification run can prove that the incremental counters agree with a
        full journal replay field for field, and that the counter-based
        candidate agrees with the reference scan.
        """
        with self._thread_lock, self._connect() as conn:
            return self._derived_state_digest(self._ensure_state(conn))

    def _derived_state_digest(self, state: _DerivedState) -> dict[str, Any]:
        latest: dict[str, ReconciliationView] = {}
        for view in state.read_views:
            latest[view.action_ref] = view
        active = {action: {"kinds": dict(sorted(stats["kinds"].items())), "total": stats["total"],
                           "zero_effect": stats["zero_effect"]}
                  for action, stats in sorted(state.active_stats.items())}
        candidate_stats = {}
        candidate_scan = {}
        for action, view in sorted(latest.items()):
            candidate_stats[action] = self._candidate_from_stats(state.active_stats.get(action),
                                                                 view.state)
            candidate_scan[action] = self._candidate(view.supporting_evidence_refs, view.state,
                                                     state.receipt_store.find)
        return {
            "active": active,
            "evidence_totals": dict(sorted(state.evidence_totals.items())),
            "pending_refs": {action: sorted(refs) for action, refs in sorted(state.pending_refs.items())
                             if refs},
            "pending_supersessions": {action: sorted(refs)
                                      for action, refs in sorted(state.pending_supersessions.items())
                                      if refs},
            "views": {ref: {"action_ref": view.action_ref, "contract_version": view.contract_version,
                            "revision": view.revision, "state": view.state, "reason": view.reason,
                            "evidence_count": view.evidence_count,
                            "evidence_refs": list(view.supporting_evidence_refs),
                            "commitment": view.evidence_chain_commitment}
                      for ref, view in sorted(state.views_by_ref.items())},
            "outbox": {outbox_id: {"action_ref": row["action_ref"],
                                   "result_revision": row["result_revision"],
                                   "status": row["status"], "attempt_count": row["attempt_count"],
                                   "payload_hash": row["payload_hash"],
                                   "payload": row["payload"]}
                       for outbox_id, row in sorted(state.outbox.items())},
            "candidate_stats": candidate_stats,
            "candidate_scan": candidate_scan,
        }

    def _invalidate_state(self) -> None:
        self._state = None

    # -- journal primitives ------------------------------------------------
    def _append_locked(self, conn: sqlite3.Connection, record_type: str, action_ref: str,
                       payload: dict[str, Any], recorded_at: str, record_id: str,
                       causal_refs: Iterable[str] = ()) -> dict[str, Any]:
        head = conn.execute("SELECT sequence,record_hash FROM journal ORDER BY sequence DESC LIMIT 1").fetchone()
        sequence = 1 if head is None else int(head["sequence"]) + 1
        previous = GENESIS_HASH if head is None else str(head["record_hash"])
        envelope = {"sequence": sequence, "record_id": record_id, "record_type": record_type,
            "schema_version": RECORD_SCHEMA, "action_ref": action_ref,
            "causal_refs": sorted(set(causal_refs)), "payload": payload,
            "recorded_at": recorded_at, "previous_hash": previous}
        digest = hashlib.sha256(_canonical(envelope).encode("utf-8")).hexdigest()
        conn.execute("INSERT INTO journal VALUES(?,?,?,?,?,?,?,?,?,?)",
            (sequence, record_id, record_type, RECORD_SCHEMA, action_ref,
             _canonical(envelope["causal_refs"]), _canonical(payload), recorded_at, previous, digest))
        envelope["record_hash"] = digest
        return envelope

    def _journal_corruption(self, kind: str, message: str,
                            event: dict[str, Any] | None = None, *, row: int | None = None,
                            line: int | None = None, offset: int | None = None,
                            recovery_possible: bool = False) -> JournalCorruptionError:
        if event is not None:
            row = event.get("sequence", row)
        return JournalCorruptionError(message, kind=kind, path=self.path.name, row=row,
            line=line, offset=offset, recovery_possible=recovery_possible)

    def _validate_rows(self, rows: Iterable[sqlite3.Row], *, previous: str = GENESIS_HASH,
                       expected: int = 1) -> list[dict[str, Any]]:
        rows = list(rows)
        events: list[dict[str, Any]] = []
        for row_index, row in enumerate(rows):
            sequence = row["sequence"]
            if sequence != expected:
                raise self._journal_corruption("sequence_gap",
                    "journal sequence gap; maintenance required", row=sequence)
            if row["schema_version"] != RECORD_SCHEMA:
                raise UnsupportedSchemaError(f"unsupported journal record schema {row['schema_version']!r}")
            try:
                causal_refs = json.loads(row["causal_refs"])
                payload = json.loads(row["payload"])
            except UnicodeDecodeError:
                raise self._journal_corruption("invalid_utf8",
                    "journal record contains invalid UTF-8", row=sequence,
                    recovery_possible=row_index == len(rows) - 1) from None
            except json.JSONDecodeError as exc:
                raise self._journal_corruption("malformed_json",
                    "journal record contains malformed JSON", row=sequence,
                    line=exc.lineno, offset=exc.colno,
                    recovery_possible=row_index == len(rows) - 1) from None
            envelope = {"sequence": sequence, "record_id": row["record_id"],
                "record_type": row["record_type"], "schema_version": row["schema_version"],
                "action_ref": row["action_ref"], "causal_refs": causal_refs,
                "payload": payload, "recorded_at": row["recorded_at"],
                "previous_hash": row["previous_hash"]}
            if envelope["previous_hash"] != previous:
                raise self._journal_corruption("hash_link_mismatch",
                    "journal hash-chain link mismatch; maintenance required", row=sequence)
            digest = hashlib.sha256(_canonical(envelope).encode("utf-8")).hexdigest()
            if digest != row["record_hash"]:
                raise self._journal_corruption("record_hash_mismatch",
                    "journal record hash mismatch; maintenance required", row=sequence)
            envelope["record_hash"] = digest
            events.append(envelope)
            previous = digest
            expected += 1
        return events

    def _events(self, conn: sqlite3.Connection) -> list[dict[str, Any]]:
        return self._validate_rows(conn.execute("SELECT * FROM journal ORDER BY sequence").fetchall())

    def _truth(self, events: Iterable[dict[str, Any]]):
        receipts: list[ContactTransportReceiptEvidence] = []
        supersessions: list[ReceiptSupersession] = []
        reconciliations: list[ContactReconciliationRecord] = []
        known = {"OUTBOX_STATUS", "CONSUMER_RECEIPT", "SCHEMA_MIGRATION"}
        for event in events:
            try:
                kind, value = event["record_type"], event["payload"]
                if kind in {"RECEIPT", "SUPERSESSION", "RECONCILIATION"} and not isinstance(value, dict):
                    raise TypeError
                if kind == "RECEIPT":
                    receipts.append(self._parsed(event, _load_evidence))
                elif kind == "SUPERSESSION":
                    supersessions.append(ReceiptSupersession(**value))
                elif kind == "RECONCILIATION":
                    reconciliations.append(self._parse_reconciliation(event))
                elif kind not in known:
                    raise UnsupportedSchemaError(f"unknown journal record type {kind!r}")
            except UnsupportedSchemaError:
                raise
            except (AttributeError, KeyError, TypeError, ValueError):
                raise self._journal_corruption("invalid_payload",
                    "journal record payload is invalid", event) from None
        return receipts, supersessions, reconciliations

    def _parsed(self, event: dict[str, Any], loader: Any) -> Any:
        """Content-addressed parse memo for journal rows.

        The cache key is the row's own verified ``record_hash``: `_validate_rows`
        recomputes every row digest from its stored bytes before `_truth` sees the
        row, so a hit proves the bytes equal those already parsed. A content edit
        without a matching hash update fails closed in `_validate_rows` before this
        lookup, and a consistent full-chain rewrite is already undetectable without
        an external anchor. The memo therefore holds a pure function of canonical
        journal content, is rebuildable by dropping it, and is never a second truth.
        """
        key = event.get("record_hash")
        if isinstance(key, str):
            cached = self._parsed_memo.get(key)
            if cached is not None:
                self._counters["record_cache_hits"] += 1
                return cached
            self._counters["record_cache_misses"] += 1
        value = loader(event["payload"])
        self._counters["records_parsed"] += 1
        if isinstance(key, str):
            self._parsed_memo[key] = value
        return value

    @staticmethod
    def _receipt_store(receipts: list[ContactTransportReceiptEvidence],
                       supersessions: list[ReceiptSupersession]) -> ContactReceiptStore:
        store = ContactReceiptStore()
        store.load((ReceiptJournalEntry(i, item) for i, item in enumerate(receipts, 1)), supersessions)
        return store

    @staticmethod
    def _check_submission_binding(prior_source: Any, incoming: ContactTransportReceiptEvidence) -> None:
        """Reject a submission key whose durable message identity differs.

        Accepts either the full receipt list (O(n) historical scan) or the
        indexed receipt store (O(refs bound to this key)); both decide identically.
        """
        identity_fields = ("action_ref", "payload_hash", "content_hash", "recipient_ref", "channel")
        if isinstance(prior_source, ContactReceiptStore):
            priors: Iterable[ContactTransportReceiptEvidence] = prior_source.by_submission(incoming.submission_key)
        else:
            priors = [item for item in prior_source if item.submission_key == incoming.submission_key]
        for prior in priors:
            if any(getattr(prior, name) != getattr(incoming, name) for name in identity_fields):
                raise DurableStoreError("submission key collision: durable message identity differs")

    @staticmethod
    def _candidate_for(kinds: frozenset[str], zero_effect: bool, state_of: str) -> str:
        """The single candidate mapping, shared by the scan and the incremental path."""
        return {"UNKNOWN": "UNKNOWN_EFFECT", "SUBMITTED": "UNKNOWN_EFFECT",
            "ACKNOWLEDGED": "UNRESOLVED", "DELIVERED": "SUCCESS_EFFECT",
            "FAILURE": "FAILURE_NO_EFFECT" if zero_effect else "UNKNOWN_EFFECT",
            "CONFLICT": "CONFLICT"}[state_of]

    @staticmethod
    def _candidate(evidence_refs: tuple[str, ...], state_of: str, lookup: Any) -> str:
        """Reference implementation: score the active evidence by scanning it."""
        if callable(lookup):
            support = [item for item in (lookup(ref) for ref in evidence_refs) if item is not None]
        else:
            support = [item for item in lookup if item.evidence_ref in evidence_refs]
        kinds = {item.observation_kind for item in support}
        zero_effect = bool(support) and kinds == {"FAILED_BEFORE_ACCEPT"} and all(
            item.effect_applied is False and item.effect_count_for_key == 0 for item in support)
        return DurableContactReceiptStore._candidate_for(frozenset(kinds), zero_effect, state_of)

    @staticmethod
    def _candidate_from_stats(stats: dict[str, Any] | None, state_of: str) -> str:
        """Same answer as `_candidate`, from counters maintained in O(1) per event."""
        if not stats:
            return DurableContactReceiptStore._candidate_for(frozenset(), False, state_of)
        kinds = frozenset(stats["kinds"])
        total = stats["total"]
        zero_effect = (total > 0 and kinds == frozenset({"FAILED_BEFORE_ACCEPT"})
                       and stats["zero_effect"] == total)
        return DurableContactReceiptStore._candidate_for(kinds, zero_effect, state_of)

    @staticmethod
    def _zero_effect(evidence: ContactTransportReceiptEvidence) -> bool:
        return evidence.effect_applied is False and evidence.effect_count_for_key == 0

    @classmethod
    def _active_add(cls, state: _DerivedState, evidence: ContactTransportReceiptEvidence) -> None:
        """Fold one newly accepted evidence row into the action's counters."""
        action_ref = evidence.action_ref
        stats = state.active_stats.get(action_ref)
        if stats is None:
            stats = state.active_stats[action_ref] = {"kinds": {}, "total": 0, "zero_effect": 0}
        kind = evidence.observation_kind
        stats["kinds"][kind] = stats["kinds"].get(kind, 0) + 1
        stats["total"] += 1
        if cls._zero_effect(evidence):
            stats["zero_effect"] += 1
        state.evidence_totals[action_ref] = state.evidence_totals.get(action_ref, 0) + 1
        state.active_refs.setdefault(action_ref, set()).add(evidence.evidence_ref)
        state.pending_refs.setdefault(action_ref, set()).add(evidence.evidence_ref)

    @classmethod
    def _active_remove(cls, state: _DerivedState, action_ref: str,
                       evidence: ContactTransportReceiptEvidence) -> None:
        """Fold a supersession into the counters; idempotent for a repeated ref."""
        superseded = state.active_refs.setdefault(action_ref, set())
        if evidence.evidence_ref not in superseded:
            return
        superseded.discard(evidence.evidence_ref)
        state.pending_refs.setdefault(action_ref, set()).discard(evidence.evidence_ref)
        stats = state.active_stats.get(action_ref)
        if stats is None:
            raise DurableStoreError("supersession without any active evidence for the action")
        remaining = stats["kinds"].get(evidence.observation_kind, 0) - 1
        if remaining > 0:
            stats["kinds"][evidence.observation_kind] = remaining
        else:
            stats["kinds"].pop(evidence.observation_kind, None)
        stats["total"] -= 1
        if cls._zero_effect(evidence):
            stats["zero_effect"] -= 1
        if stats["total"] < 0 or stats["zero_effect"] < 0:
            raise DurableStoreError("active evidence counters went negative")

    @staticmethod
    def _settlement_payload(view: ReconciliationView, request: ContactSettlementRequest) -> dict[str, Any]:
        return {"kind": "SETTLEMENT_REQUEST", "request_ref": request.settlement_request_ref,
                "candidate": request.candidate, "evidence_fingerprint": view.evidence_fingerprint,
                "evidence_refs": list(view.supporting_evidence_refs)}

    @staticmethod
    def _handoff_payload(view: ReconciliationView, *,
                         result_hash: str | None = None) -> dict[str, Any]:
        """Delivery handoff for one reconciled revision.

        v1 keeps its frozen shape, which embedded the active evidence list.
        v2 references the verified revision instead, so one action writes a
        constant number of delivery bytes per revision however long its
        evidence history grows.
        """
        if view.contract_version == "v2":
            if result_hash is None:
                raise DurableStoreError("v2 handoff requires the reconciled result hash")
            return settlement_handoff_payload(
                action_ref=view.action_ref, status=view.state,
                reconciliation_ref=view.reconciliation_ref, reconciliation_revision=view.revision,
                evidence_count=view.evidence_count,
                evidence_chain_commitment=view.evidence_chain_commitment, result_hash=result_hash)
        return {"kind": "CONTACT_RESULT_HANDOFF", "action_ref": view.action_ref,
                "state": view.state, "delivery_status": view.state,
                "evidence_refs": list(view.supporting_evidence_refs),
                "causal_refs": [view.reconciliation_ref],
                "statement": ("action unresolved; delivery status remains unknown" if view.state == "UNKNOWN"
                              else f"contact action result: {view.state}")}

    def _insert_outbox(self, conn: sqlite3.Connection, outbox_id: str,
                       record: ContactReconciliationRecord, payload: dict[str, Any], revision: int) -> None:
        body = _canonical(payload)
        digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
        conn.execute("INSERT OR IGNORE INTO outbox_projection VALUES(?,?,?,?,?,?, 'PENDING',0,?,NULL,NULL)",
            (outbox_id, record.action_ref, revision, _canonical(list(record.supporting_evidence_refs)),
             body, digest, record.recorded_at))

    # -- derived outbox state ---------------------------------------------
    @staticmethod
    def _insert_outbox_state(state: _DerivedState, outbox_id: str,
                             view: ReconciliationView, payload: dict[str, Any],
                             revision: int, created_at: str,
                             evidence_refs: tuple[str, ...] | None = None) -> None:
        if outbox_id in state.outbox:
            return
        body = _canonical(payload)
        state.outbox[outbox_id] = {
            "outbox_id": outbox_id, "action_ref": view.action_ref, "result_revision": revision,
            "evidence_refs": tuple(view.supporting_evidence_refs if evidence_refs is None
                                   else evidence_refs), "payload": payload,
            "payload_text": body, "payload_hash": hashlib.sha256(body.encode("utf-8")).hexdigest(),
            "status": "PENDING", "attempt_count": 0, "created_at": created_at,
            "delivered_at": None, "consumer_receipt_ref": None,
        }

    def _apply_outbox_event(self, state: _DerivedState, event: dict[str, Any]) -> tuple[str, ...]:
        """Fold one committed event into the derived delivery state.

        Returns the outbox rows this event (re)defined so the caller can publish
        exactly those projection rows, instead of diffing the whole outbox.
        """
        value, kind = event["payload"], event["record_type"]
        if kind == "RECONCILIATION":
            reference = value.get("reconciliation_ref")
            view = state.views_by_ref.get(reference)
            if view is None:
                raise self._journal_corruption("invalid_payload",
                    "reconciliation read view was not registered before outbox replay", event)
            state.revisions[view.action_ref] = view.revision
            candidate = self._candidate_from_stats(state.active_stats.get(view.action_ref), view.state)
            settlement_version = view.contract_version
            state.settlement_versions[view.action_ref] = settlement_version
            result_hash = (self._view_result_hash(state, view)
                           if settlement_version == "v2" else None)
            touched: list[str] = []
            if settlement_version == "v2":
                request_v2 = create_settlement_v2(
                    action_ref=view.action_ref, reconciliation_ref=view.reconciliation_ref,
                    reconciliation_revision=view.revision,
                    evidence_count=view.evidence_count,
                    evidence_chain_commitment=view.evidence_chain_commitment,
                    result_status=view.state, result_hash=result_hash,
                    candidate=candidate)
                settlement_id = f"settlement:{request_v2.settlement_request_ref}"
                self._insert_outbox_state(state, settlement_id,
                                          view, settlement_v2_payload(request_v2), view.revision,
                                          event["recorded_at"], evidence_refs=())
                touched.append(settlement_id)
            else:
                request = ContactSettlementRequest.create(action_ref=view.action_ref,
                    evidence_fingerprint=view.evidence_fingerprint, candidate=candidate,
                    supporting_evidence_refs=view.supporting_evidence_refs)
                settlement_id = f"settlement:{request.settlement_request_ref}"
                self._insert_outbox_state(state, settlement_id, view,
                                          self._settlement_payload(view, request), view.revision,
                                          event["recorded_at"])
                touched.append(settlement_id)
            handoff = self._handoff_payload(view, result_hash=result_hash)
            handoff_id = hashlib.sha256((view.action_ref + view.reconciliation_ref).encode()).hexdigest()
            # v2 handoffs reference the verified revision; they never copy the history.
            experience_id = f"experience:{handoff_id}"
            self._insert_outbox_state(state, experience_id, view, handoff,
                                      view.revision, event["recorded_at"],
                                      evidence_refs=() if settlement_version == "v2" else None)
            touched.append(experience_id)
            return tuple(touched)
        elif kind == "CONSUMER_RECEIPT":
            required = {"outbox_id", "payload_hash", "outcome", "receipt_ref", "attempt_ref",
                        "attempt_count", "accepted_at"}
            if not isinstance(value, dict) or not required.issubset(value):
                raise self._journal_corruption("invalid_payload",
                    "consumer receipt is missing required versioned attempt fields", event)
            outbox_id = value["outbox_id"]
            row = state.outbox.get(outbox_id)
            if row is None or value.get("payload_hash") != row["payload_hash"]:
                raise self._journal_corruption("invalid_payload",
                    "consumer receipt is not bound to canonical outbox payload", event)
            outcome, attempt_ref = value.get("outcome"), value.get("attempt_ref")
            try:
                supported_outcome = outcome in CONSUMER_OUTCOMES
            except TypeError:
                supported_outcome = False
            if not supported_outcome:
                raise self._journal_corruption("invalid_payload",
                    "consumer receipt has unsupported outcome", event)
            if not isinstance(attempt_ref, str) or not attempt_ref.startswith("catt:") or any(c.isspace() for c in attempt_ref):
                raise self._journal_corruption("invalid_payload",
                    "consumer receipt attempt identity is invalid", event)
            attempt_key = (outbox_id, attempt_ref)
            if attempt_key in state.receipts_by_attempt or row["status"] not in {"PENDING", "DEFERRED"}:
                raise self._journal_corruption("invalid_payload",
                    "duplicate attempt or attempt after terminal outbox state", event)
            expected_count = state.attempts.get(outbox_id, 0) + 1
            if value.get("attempt_count") != expected_count:
                raise self._journal_corruption("invalid_payload",
                    "outbox attempt_count is not contiguous", event)
            state_row = {"outbox_id": outbox_id, "payload_hash": value["payload_hash"],
                "outcome": outcome, "receipt_ref": value["receipt_ref"], "attempt_ref": attempt_ref,
                "attempt_count": expected_count, "accepted_at": value["accepted_at"],
                "status": {"ACCEPT": "ACCEPTED", "IGNORE": "DELIVERED", "DEFER": "DEFERRED",
                           "REJECT": "REJECTED", "CONFLICT": "CONFLICT"}[outcome]}
            state.receipts_by_attempt[attempt_key] = state_row
            state.attempts[outbox_id] = expected_count
            row["status"] = state_row["status"]
            row["attempt_count"] = expected_count
            row["delivered_at"] = value["accepted_at"]
            row["consumer_receipt_ref"] = value["receipt_ref"]
            state.consumer_receipts[outbox_id] = {"outbox_id": outbox_id,
                "payload_hash": value["payload_hash"], "outcome": outcome,
                "receipt_ref": value["receipt_ref"], "accepted_at": value["accepted_at"]}
            return (outbox_id,)
        elif kind == "OUTBOX_STATUS":
            required = {"outbox_id", "payload_hash", "status", "attempt_count", "delivered_at",
                        "consumer_receipt_ref", "attempt_ref"}
            if not isinstance(value, dict) or not required.issubset(value):
                raise self._journal_corruption("invalid_payload",
                    "outbox status is missing required versioned attempt fields", event)
            outbox_id, attempt_ref = value["outbox_id"], value.get("attempt_ref")
            receipt = state.receipts_by_attempt.get((outbox_id, attempt_ref))
            if receipt is None:
                raise self._journal_corruption("invalid_payload",
                    "outbox status has no matching consumer attempt", event)
            if (value.get("status") != receipt["status"]
                    or value.get("attempt_count") != receipt["attempt_count"]
                    or value.get("consumer_receipt_ref") != receipt["receipt_ref"]
                    or value.get("payload_hash") != receipt["payload_hash"]):
                raise self._journal_corruption("invalid_payload",
                    "outbox status conflicts with matching consumer receipt", event)
        return ()

    def _replay_outbox(self, state: _DerivedState) -> None:
        state.outbox = {}
        state.consumer_receipts = {}
        state.revisions = {}
        state.attempts = {}
        state.receipts_by_attempt = {}
        for event in state.events:
            self._apply_outbox_event(state, event)

    def _rebuild_outbox(self, conn: sqlite3.Connection, events: list[dict[str, Any]]) -> _DerivedState:
        conn.execute("DELETE FROM outbox_projection")
        conn.execute("DELETE FROM consumer_receipt_projection")
        state = self._state_from_events(events)
        self._write_projection_rows(conn, state)
        return state

    def _write_projection_rows(self, conn: sqlite3.Connection, state: _DerivedState) -> None:
        for row in state.outbox.values():
            conn.execute("INSERT OR REPLACE INTO outbox_projection VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (row["outbox_id"], row["action_ref"], row["result_revision"],
                 _canonical(list(row["evidence_refs"])), row["payload_text"], row["payload_hash"],
                 row["status"], row["attempt_count"], row["created_at"], row["delivered_at"],
                 row["consumer_receipt_ref"]))
        for row in state.consumer_receipts.values():
            conn.execute("INSERT OR REPLACE INTO consumer_receipt_projection VALUES(?,?,?,?,?)",
                (row["outbox_id"], row["payload_hash"], row["outcome"], row["receipt_ref"],
                 row["accepted_at"]))

    def _sync_outbox_rows(self, conn: sqlite3.Connection, state: _DerivedState,
                          outbox_ids: Iterable[str]) -> None:
        """Publish only the derived rows touched by one incremental operation."""
        for outbox_id in outbox_ids:
            row = state.outbox.get(outbox_id)
            if row is None:
                continue
            conn.execute("INSERT OR IGNORE INTO outbox_projection VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (row["outbox_id"], row["action_ref"], row["result_revision"],
                 _canonical(list(row["evidence_refs"])), row["payload_text"], row["payload_hash"],
                 row["status"], row["attempt_count"], row["created_at"], row["delivered_at"],
                 row["consumer_receipt_ref"]))
            conn.execute("UPDATE outbox_projection SET status=?,attempt_count=?,delivered_at=?,"
                         "consumer_receipt_ref=? WHERE outbox_id=?",
                (row["status"], row["attempt_count"], row["delivered_at"],
                 row["consumer_receipt_ref"], row["outbox_id"]))
            receipt = state.consumer_receipts.get(outbox_id)
            if receipt is not None:
                conn.execute("INSERT OR REPLACE INTO consumer_receipt_projection VALUES(?,?,?,?,?)",
                    (receipt["outbox_id"], receipt["payload_hash"], receipt["outcome"],
                     receipt["receipt_ref"], receipt["accepted_at"]))
            else:
                conn.execute("DELETE FROM consumer_receipt_projection WHERE outbox_id=?", (outbox_id,))

    # -- derived state: restore / incremental apply ------------------------
    def _state_from_events(self, events: list[dict[str, Any]]) -> _DerivedState:
        state = _DerivedState()
        state.events = list(events)
        state.receipts, state.supersessions, all_reconciliations = self._truth(events)
        v1_records = [item for item in all_reconciliations
                      if type(item) is ContactReconciliationRecord]
        state.reconciliations = v1_records
        state.receipt_store = self._receipt_store(state.receipts, state.supersessions)
        state.authority = ContactReconciliationAuthority(store=state.receipt_store)
        state.authority.load(v1_records)
        index = 0
        # Fold the same incremental counters `_apply_events` maintains, and fold
        # each event's derived outbox rows *in the same pass*: the candidate of a
        # revision is a function of the evidence known at that revision, so a
        # second pass over the whole journal would score every revision against
        # the final evidence set and disagree with the writer.
        for event in events:
            kind = event["record_type"]
            if kind == "RECEIPT":
                self._active_add(state, self._parsed(event, _load_evidence))
            elif kind == "SUPERSESSION":
                supersession = ReceiptSupersession(**event["payload"])
                state.supersessions_by_action.setdefault(supersession.action_ref, []).append(supersession)
                state.pending_supersessions.setdefault(supersession.action_ref, set()).add(
                    supersession.supersession_ref)
                superseded = state.receipt_store.find(supersession.old_evidence_ref)
                if superseded is None:
                    raise self._journal_corruption("invalid_payload",
                        "supersession does not reference a durable evidence row", event)
                self._active_remove(state, supersession.action_ref, superseded)
            if kind == "RECONCILIATION":
                self._register_reconciliation(state, event, all_reconciliations[index])
                index += 1
            self._apply_outbox_event(state, event)
        state.sequence = int(events[-1]["sequence"]) if events else 0
        state.head_hash = events[-1]["record_hash"] if events else GENESIS_HASH
        return state

    def _full_replay_state(self, conn: sqlite3.Connection) -> _DerivedState:
        self._counters["full_journal_replays"] += 1
        return self._state_from_events(self._events(conn))

    def _verify_checkpoint(self, conn: sqlite3.Connection, checkpoint: Any) -> None:
        through = int(checkpoint.through_sequence)
        structural = conn.execute("SELECT sequence,record_hash,previous_hash FROM journal "
                                  "WHERE sequence <= ? ORDER BY sequence", (through,)).fetchall()
        payloads = conn.execute("SELECT sequence,payload FROM journal WHERE sequence <= ? ORDER BY sequence",
                                (through,)).fetchall()
        max_sequence = int(conn.execute("SELECT COALESCE(MAX(sequence),0) FROM journal").fetchone()[0])
        verify_against_journal(checkpoint, journal_identity=self._identity,
            journal_max_sequence=max_sequence,
            prefix_structural=structural_digest(structural),
            prefix_payload=payload_digest(payloads),
            anchor_record_hash=structural[-1]["record_hash"] if structural else None)
        if len(structural) != through:
            raise CheckpointInvalid("checkpoint prefix is shorter than its anchor",
                                    kind="ANCHOR_MISMATCH")
        events = checkpoint.checkpoint_payload.get("events")
        if not isinstance(events, list) or len(events) != through:
            raise CheckpointInvalid("checkpoint payload does not cover its anchor",
                                    kind="PAYLOAD_INVALID")
        if events[-1].get("record_hash") != checkpoint.through_record_hash:
            raise CheckpointInvalid("checkpoint payload tail does not match its anchor",
                                    kind="PAYLOAD_INVALID")
        if [event.get("sequence") for event in events] != list(range(1, through + 1)):
            raise CheckpointInvalid("checkpoint payload sequence is not contiguous",
                                    kind="PAYLOAD_INVALID")

    def _restore_state(self, conn: sqlite3.Connection) -> _DerivedState:
        path = checkpoint_path(self.path)
        checkpoint = None
        try:
            checkpoint = read_checkpoint(path)
        except CheckpointInvalid as exc:
            self._counters["checkpoint_rejects"] += 1
            self._counters["checkpoint_fallbacks"] += 1
            self._counters["checkpoint_last_reject"] = exc.kind
        if checkpoint is None:
            if not path.exists():
                self._counters["checkpoint_missing"] += 1
            return self._full_replay_state(conn)
        try:
            self._verify_checkpoint(conn, checkpoint)
        except CheckpointInvalid as exc:
            self._counters["checkpoint_rejects"] += 1
            self._counters["checkpoint_fallbacks"] += 1
            self._counters["checkpoint_last_reject"] = exc.kind
            return self._full_replay_state(conn)
        self._counters["checkpoint_loads"] += 1
        self._last_checkpoint_sequence = int(checkpoint.through_sequence)
        state = self._state_from_events(list(checkpoint.checkpoint_payload["events"]))
        tail = conn.execute("SELECT * FROM journal WHERE sequence > ? ORDER BY sequence",
                            (checkpoint.through_sequence,)).fetchall()
        if tail:
            events = self._validate_rows(tail, previous=checkpoint.through_record_hash,
                                         expected=int(checkpoint.through_sequence) + 1)
            self._apply_events(state, events)
            self._counters["tail_records_replayed"] += len(events)
        return state

    def _apply_events(self, state: _DerivedState, events: Iterable[dict[str, Any]]) -> tuple[str, ...]:
        """Apply already-committed events to the derived state (no re-derivation).

        Returns the outbox rows (re)defined by these events. Every counter the
        incremental projection needs is maintained here, in the one funnel that
        both replay and the live write path share.
        """
        applied = 0
        touched: list[str] = []
        for event in events:
            kind = event["record_type"]
            if kind == "RECEIPT":
                evidence = self._parsed(event, _load_evidence)
                if not state.receipt_store.append(evidence, assume_indexed=True):
                    raise ValueError("duplicate evidence row in journal")
                state.receipts.append(evidence)
                self._active_add(state, evidence)
            elif kind == "SUPERSESSION":
                supersession = ReceiptSupersession(**event["payload"])
                state.receipt_store.add_supersession(supersession)
                state.supersessions.append(supersession)
                state.supersessions_by_action.setdefault(supersession.action_ref, []).append(supersession)
                state.pending_supersessions.setdefault(supersession.action_ref, set()).add(
                    supersession.supersession_ref)
                superseded = state.receipt_store.find(supersession.old_evidence_ref)
                if superseded is None:
                    raise self._journal_corruption("invalid_payload",
                        "supersession does not reference a durable evidence row", event)
                self._active_remove(state, supersession.action_ref, superseded)
            elif kind == "RECONCILIATION":
                record = self._parse_reconciliation(event)
                self._register_reconciliation(state, event, record)
                if type(record) is ContactReconciliationRecord:
                    state.reconciliations.append(record)
                    state.authority.attach(record)
            touched.extend(self._apply_outbox_event(state, event))
            state.events.append(event)
            state.sequence = int(event["sequence"])
            state.head_hash = event["record_hash"]
            applied += 1
        self._counters["incremental_apply_count"] += applied
        return tuple(touched)

    def _ensure_state(self, conn: sqlite3.Connection) -> _DerivedState:
        state = self._state
        if state is None:
            self._state = self._restore_state(conn)
            return self._state
        head = conn.execute("SELECT sequence,record_hash FROM journal ORDER BY sequence DESC LIMIT 1").fetchone()
        count = conn.execute("SELECT COUNT(*) FROM journal").fetchone()[0]
        scanned = 1
        max_sequence = 0 if head is None else int(head["sequence"])
        if max_sequence != count or max_sequence < state.sequence:
            # Deleted/truncated history: the cache must never be trusted.
            self._invalidate_state()
            self._state = self._full_replay_state(conn)
            return self._state
        if max_sequence == state.sequence:
            self._counters["journal_records_scanned"] += scanned
            self._counters["journal_records_scanned_per_operation"] = scanned
            return state
        anchor = conn.execute("SELECT record_hash FROM journal WHERE sequence=?", (state.sequence,)).fetchone()
        if anchor is None or anchor["record_hash"] != state.head_hash:
            self._invalidate_state()
            self._state = self._full_replay_state(conn)
            return self._state
        tail = conn.execute("SELECT * FROM journal WHERE sequence > ? ORDER BY sequence",
                            (state.sequence,)).fetchall()
        events = self._validate_rows(tail, previous=state.head_hash, expected=state.sequence + 1)
        self._apply_events(state, events)
        self._counters["tail_records_replayed"] += len(events)
        self._counters["journal_records_scanned"] += scanned + len(tail)
        self._counters["journal_records_scanned_per_operation"] = scanned + len(tail)
        return state

    # -- checkpoint publication -------------------------------------------
    def _maybe_checkpoint(self, conn: sqlite3.Connection, state: _DerivedState) -> None:
        """Publish a checkpoint bookmark after a committed journal advance.

        A checkpoint failure is never a durable failure: the journal commit is
        already valid and the bookmark is optional by construction.
        """
        try:
            if self.checkpoint_every_records <= 0 or state.sequence < 1:
                return
            if state.sequence - self._last_checkpoint_sequence < self.checkpoint_every_records:
                return
            through = state.sequence
            structural = conn.execute("SELECT sequence,record_hash,previous_hash FROM journal "
                                      "WHERE sequence <= ? ORDER BY sequence", (through,)).fetchall()
            payloads = conn.execute("SELECT sequence,payload FROM journal WHERE sequence <= ? ORDER BY sequence",
                                    (through,)).fetchall()
            checkpoint = build_checkpoint(journal_identity=self._identity, through_sequence=through,
                through_record_id=state.events[-1]["record_id"], through_record_hash=state.head_hash,
                created_at=_now(), prefix_structural_digest=structural_digest(structural),
                prefix_payload_digest=payload_digest(payloads),
                payload={"events": state.events, "through_record_id": state.events[-1]["record_id"]})
            write_checkpoint(checkpoint_path(self.path), checkpoint, ops=self._ops())
            self._last_checkpoint_sequence = through
            self._counters["checkpoint_writes"] += 1
        except Exception as exc:  # noqa: BLE001 - a bookmark must never fail the operation
            self._counters["checkpoint_write_failures"] += 1
            self._counters["checkpoint_last_write_error"] = type(exc).__name__

    # -- reconciliation append --------------------------------------------
    def _parse_reconciliation(self, event: dict[str, Any]) -> Any:
        """Decode a reconciliation row by its *declared* contract identity."""
        payload = event["payload"]
        reference = payload.get("reconciliation_ref", "")
        version = contract_version_for(payload=payload, reference=reference)
        if version == "v2":
            return load_reconciliation_v2(payload)
        return self._parsed(event, _load_recon)

    def _register_reconciliation(self, state: _DerivedState, event: dict[str, Any],
                                 record: Any) -> ReconciliationView:
        """Track one reconciliation revision and publish its uniform read view.

        The v2 branch derives its view from the previous revision's view, so
        applying one revision costs O(delta) rather than re-materializing the
        whole chain.
        """
        action_ref = record.action_ref
        previous_version = state.reconciliation_versions.get(action_ref)
        if type(record) is ContactReconciliationRecord:
            if previous_version == "v2":
                raise self._journal_corruption("invalid_payload",
                    "action reverts to the v1 reconciliation contract mid-history", event)
            state.reconciliation_versions[action_ref] = "v1"
            view = v1_view(record, revision=state.revisions.get(action_ref, 0) + 1)
        else:
            if previous_version == "v1":
                raise self._journal_corruption("invalid_payload",
                    "action switches to the v2 reconciliation contract mid-history", event)
            chain = state.v2_records.setdefault(action_ref, [])
            if record.revision != len(chain) + 1:
                raise self._journal_corruption("invalid_payload",
                    "v2 reconciliation revision does not continue its action chain", event)
            parent = chain[-1] if chain else None
            if parent is None:
                if record.previous_reconciliation_ref is not None:
                    raise self._journal_corruption("invalid_payload",
                        "v2 first revision must not name a parent", event)
            elif record.previous_reconciliation_ref != parent.reconciliation_ref:
                raise self._journal_corruption("invalid_payload",
                    "v2 parent reference does not match the previous revision", event)
            previous_view = state.views_by_ref.get(parent.reconciliation_ref) if parent else None
            owners = {item.supersession_ref: item.old_evidence_ref
                      for item in state.supersessions_by_action.get(action_ref, ())}
            superseded = set(previous_view.superseded_refs) if previous_view else set()
            superseded |= {owners[ref] for ref in record.new_supersession_refs if ref in owners}
            try:
                view = next_v2_view(previous_view, record, superseded_refs=superseded)
            except ReconciliationChainError as exc:
                raise self._journal_corruption("invalid_payload",
                    f"v2 evidence chain is invalid ({exc.kind})", event) from None
            chain.append(record)
            state.reconciliation_versions[action_ref] = "v2"
            # This revision now owns those refs, so they stop being pending.
            state.pending_refs.setdefault(action_ref, set()).difference_update(record.new_evidence_refs)
            state.pending_supersessions.setdefault(action_ref, set()).difference_update(
                record.new_supersession_refs)
        state.revisions[action_ref] = view.revision
        state.read_views.append(view)
        state.views_by_ref[view.reconciliation_ref] = view
        return view

    def _append_reconciliation(self, conn: sqlite3.Connection, state: _DerivedState,
                               action_ref: str, recorded_at: str) -> ReconciliationView:
        version = state.reconciliation_versions.get(action_ref, DEFAULT_RECONCILIATION_CONTRACT)
        if version == "v1":
            result = state.authority.build(action_ref=action_ref, recorded_at=recorded_at)
            if not state.authority.knows(action_ref=action_ref,
                                         fingerprint=result.evidence_fingerprint):
                envelope = self._append_locked(conn, "RECONCILIATION", action_ref, _payload_recon(result),
                    recorded_at, f"reconciliation:{result.reconciliation_ref}", result.supporting_evidence_refs)
                self._sync_outbox_rows(conn, state, self._apply_events(state, [envelope]))
            return state.views_by_ref[result.reconciliation_ref]
        return self._append_reconciliation_v2(conn, state, action_ref, recorded_at)

    def _append_reconciliation_v2(self, conn: sqlite3.Connection, state: _DerivedState,
                                  action_ref: str, recorded_at: str) -> ReconciliationView:
        chain = state.v2_records.get(action_ref, [])
        previous = chain[-1] if chain else None
        if not state.evidence_totals.get(action_ref):
            raise DurableStoreError("cannot reconcile an action without receipt evidence")
        supersessions = state.supersessions_by_action.get(action_ref, ())
        # Pending refs are exactly "active evidence not yet owned by a revision";
        # maintaining them costs O(1) per event instead of re-scanning the chain.
        delta = tuple(sorted(state.pending_refs.get(action_ref, ())))
        supersession_delta = tuple(sorted(state.pending_supersessions.get(action_ref, ())))
        status, inferred_reason = ContactReconciliationAuthority._project_kinds(
            frozenset(state.active_stats.get(action_ref, {"kinds": {}})["kinds"]), bool(supersessions))
        if (previous is not None and not delta and not supersession_delta
                and status == previous.status and inferred_reason == previous.reason_code):
            return state.views_by_ref[previous.reconciliation_ref]
        record = ContactReconciliationRecordV2.create(
            action_ref=action_ref, revision=(previous.revision + 1) if previous else 1,
            previous_reconciliation_ref=previous.reconciliation_ref if previous else None,
            previous_commitment=(previous.evidence_chain_commitment if previous else "0" * 64),
            new_evidence_refs=delta,
            previous_total_evidence=previous.total_evidence_count if previous else 0,
            new_supersession_refs=supersession_delta,
            previous_total_supersessions=previous.total_supersession_count if previous else 0,
            status=status, reason_code=inferred_reason)
        envelope = self._append_locked(conn, "RECONCILIATION", action_ref,
            reconciliation_v2_payload(record), recorded_at,
            f"reconciliation:{record.reconciliation_ref}", record.new_evidence_refs)
        self._sync_outbox_rows(conn, state, self._apply_events(state, [envelope]))
        return state.views_by_ref[record.reconciliation_ref]

    # -- public mutating API ----------------------------------------------
    def accept_evidence(self, evidence: ContactTransportReceiptEvidence, *, recorded_at: str | None = None) -> bool:
        if type(evidence) is not ContactTransportReceiptEvidence:
            raise TypeError("typed ContactTransportReceiptEvidence required")
        now = recorded_at or evidence.recorded_at or _now()
        with self._thread_lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                state = self._ensure_state(conn)
                prior = state.receipt_store.find(evidence.evidence_ref)
                if prior is not None:
                    if prior.semantic_fingerprint != evidence.semantic_fingerprint:
                        raise DurableStoreError("semantic evidence identity collision")
                    conn.rollback()
                    return False
                self._check_submission_binding(state.receipt_store, evidence)
                state.receipt_store.validate_unique(evidence)
                envelope = self._append_locked(conn, "RECEIPT", evidence.action_ref,
                    _payload_evidence(evidence), now, f"receipt:{evidence.evidence_ref}",
                    (evidence.submission_key,))
                self._apply_events(state, [envelope])
                self._append_reconciliation(conn, state, evidence.action_ref, now)
                conn.commit()
                self._maybe_checkpoint(conn, state)
                return True
            except Exception:
                conn.rollback()
                self._invalidate_state()
                raise

    def accept_correction(self, evidence: ContactTransportReceiptEvidence,
                          supersession: ReceiptSupersession, *, recorded_at: str | None = None) -> bool:
        if type(evidence) is not ContactTransportReceiptEvidence or type(supersession) is not ReceiptSupersession:
            raise TypeError("typed receipt and supersession required")
        if evidence.action_ref != supersession.action_ref or evidence.evidence_ref != supersession.new_evidence_ref:
            raise DurableStoreError("correction evidence/supersession mismatch")
        now = recorded_at or evidence.recorded_at or _now()
        with self._thread_lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                state = self._ensure_state(conn)
                prior = state.receipt_store.find(evidence.evidence_ref)
                if prior is not None:
                    if (prior.semantic_fingerprint != evidence.semantic_fingerprint
                            or supersession not in state.supersessions):
                        raise DurableStoreError("correction replay does not match existing history")
                    conn.rollback()
                    return False
                self._check_submission_binding(state.receipt_store, evidence)
                state.receipt_store.validate_unique(evidence)
                self._validate_supersession(state, supersession, evidence)
                receipt_envelope = self._append_locked(conn, "RECEIPT", evidence.action_ref,
                    _payload_evidence(evidence), now, f"receipt:{evidence.evidence_ref}",
                    (evidence.submission_key,))
                supersession_envelope = self._append_locked(conn, "SUPERSESSION", evidence.action_ref,
                    asdict(supersession), now, f"supersession:{supersession.supersession_ref}",
                    (supersession.old_evidence_ref, supersession.new_evidence_ref))
                self._apply_events(state, [receipt_envelope, supersession_envelope])
                self._append_reconciliation(conn, state, evidence.action_ref, now)
                conn.commit()
                self._maybe_checkpoint(conn, state)
                return True
            except Exception:
                conn.rollback()
                self._invalidate_state()
                raise

    @staticmethod
    def _validate_supersession(state: _DerivedState, supersession: ReceiptSupersession,
                               incoming: ContactTransportReceiptEvidence) -> None:
        """Pure mirror of `ContactReceiptStore.add_supersession` checks.

        `incoming` is the evidence this same operation is about to journal, which
        the in-memory contract sees as already appended.
        """
        old = state.receipt_store.find(supersession.old_evidence_ref)
        new = state.receipt_store.find(supersession.new_evidence_ref)
        if new is None and supersession.new_evidence_ref == incoming.evidence_ref:
            new = incoming
        if old is None or new is None:
            raise ValueError("supersession evidence must already be journaled")
        if old.action_ref != supersession.action_ref or new.action_ref != supersession.action_ref:
            raise ValueError("supersession evidence must belong to its action")
        if supersession in state.supersessions:
            raise ValueError("supersession replay does not match existing history")

    def reconcile(self, *, action_ref: str, recorded_at: str | None = None) -> ContactReconciliationRecord:
        now = recorded_at or _now()
        with self._thread_lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                state = self._ensure_state(conn)
                result = self._append_reconciliation(conn, state, action_ref, now)
                conn.commit()
                self._maybe_checkpoint(conn, state)
                return result
            except Exception:
                conn.rollback()
                self._invalidate_state()
                raise

    def record_consumer_receipt(self, *, outbox_id: str, payload_hash: str, outcome: str,
                                receipt_ref: str, attempt_ref: str,
                                accepted_at: str | None = None) -> str:
        if outcome not in CONSUMER_OUTCOMES or not receipt_ref.startswith("ccons:"):
            raise ValueError("invalid typed consumer receipt")
        if not isinstance(attempt_ref, str) or not attempt_ref.startswith("catt:") or any(c.isspace() for c in attempt_ref):
            raise ValueError("attempt_ref must be an explicit catt: idempotency identity")
        now = accepted_at or _now()
        with self._thread_lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                state = self._ensure_state(conn)
                row = state.outbox.get(outbox_id)
                if row is None or row["payload_hash"] != payload_hash:
                    raise DurableStoreError("outbox identity/payload hash mismatch")
                prior = state.receipts_by_attempt.get((outbox_id, attempt_ref))
                if prior is not None:
                    if prior["payload_hash"] != payload_hash or prior["outcome"] != outcome:
                        raise DurableStoreError("consumer attempt replay conflicts with recorded receipt")
                    conn.commit()
                    return prior["receipt_ref"]
                if row["status"] not in {"PENDING", "DEFERRED"}:
                    raise DurableStoreError("terminal outbox item cannot accept a new attempt")
                attempt_count = int(row["attempt_count"]) + 1
                receipt = {"outbox_id": outbox_id, "payload_hash": payload_hash,
                    "outcome": outcome, "receipt_ref": receipt_ref, "attempt_ref": attempt_ref,
                    "attempt_count": attempt_count, "accepted_at": now}
                state_payload = {"outbox_id": outbox_id, "payload_hash": payload_hash,
                    "status": {"ACCEPT": "ACCEPTED", "IGNORE": "DELIVERED", "DEFER": "DEFERRED",
                               "REJECT": "REJECTED", "CONFLICT": "CONFLICT"}[outcome],
                    "attempt_count": attempt_count, "delivered_at": now,
                    "consumer_receipt_ref": receipt_ref, "attempt_ref": attempt_ref}
                receipt_envelope = self._append_locked(conn, "CONSUMER_RECEIPT", f"outbox:{outbox_id}",
                    receipt, now, f"consumer-receipt:{outbox_id}:{attempt_ref}", (outbox_id, attempt_ref))
                status_envelope = self._append_locked(conn, "OUTBOX_STATUS", f"outbox:{outbox_id}",
                    state_payload, now, f"outbox-status:{outbox_id}:{attempt_ref}", (outbox_id, attempt_ref))
                self._apply_events(state, [receipt_envelope, status_envelope])
                self._sync_outbox_rows(conn, state, [outbox_id])
                conn.commit()
                self._maybe_checkpoint(conn, state)
                return receipt_ref
            except Exception:
                conn.rollback()
                self._invalidate_state()
                raise

    # -- recovery / read APIs (always re-derive from the canonical journal) --
    def recover(self) -> tuple[ContactReceiptStore, tuple[ContactReconciliationRecord, ...]]:
        with self._thread_lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                state = self._rebuild_outbox(conn, self._events(conn))
                conn.commit()
                self._state = state
                return state.receipt_store, tuple(state.read_views)
            except Exception:
                conn.rollback()
                self._invalidate_state()
                raise

    def project(self, *, action_ref: str) -> ContactActionResultProjection:
        store, records = self.recover()
        return ContactActionResultProjector(store=store).project(action_ref=action_ref, reconciliations=records)

    def outbox_items(self, *, status: str | None = None) -> tuple[dict[str, Any], ...]:
        self.recover()
        if status is not None and status not in OUTBOX_STATES:
            raise ValueError("invalid outbox status")
        with self._connect() as conn:
            sql = "SELECT * FROM outbox_projection" + (" WHERE status=?" if status else "") + " ORDER BY outbox_id"
            rows = conn.execute(sql, (status,) if status else ()).fetchall()
            return tuple({"outbox_id": row["outbox_id"], "action_ref": row["action_ref"],
                "result_revision": row["result_revision"], "evidence_refs": tuple(json.loads(row["evidence_refs"])),
                "payload": json.loads(row["payload"]), "payload_hash": row["payload_hash"],
                "status": row["status"], "attempt_count": row["attempt_count"],
                "created_at": row["created_at"], "delivered_at": row["delivered_at"],
                "consumer_receipt_ref": row["consumer_receipt_ref"]} for row in rows)

    @staticmethod
    def _view_result_hash(state: _DerivedState, view: ReconciliationView) -> str:
        """Result hash of the v2 reconciliation revision this settlement references.

        The view already carries its revision's result hash, so the incremental
        writer does not rescan the action's chain for every event.
        """
        if view.result_hash:
            return view.result_hash
        chain = state.v2_records.get(view.action_ref, ())
        for record in chain:
            if record.reconciliation_ref == view.reconciliation_ref:
                return record.result_hash
        raise DurableStoreError("settlement requires a v2 reconciliation revision")

    def resolve_settlement_evidence(self, settlement_request_ref: str, *,
                                    expected_revision: int | None = None,
                                    expected_commitment: str | None = None) -> dict[str, Any]:
        """Read-only resolver: materialize the evidence a v2 settlement references.

        Read port only - it never settles, sends, retries or mutates the journal.
        The evidence list is rebuilt from the canonical reconciliation chain and
        verified against the declared action, revision, commitment and result
        hash; any mismatch is rejected rather than silently accepted.
        """
        document = None
        for item in self.outbox_items():
            if item["payload"].get("settlement_request_ref") == settlement_request_ref:
                document = item["payload"]
                break
        if document is None:
            raise SettlementContractError("unknown settlement request reference",
                                          kind="missing_reconciliation")
        if settlement_contract_version(payload=document, reference=settlement_request_ref) != "v2":
            raise SettlementContractError("resolver only materializes v2 settlements",
                                          kind="unknown_contract_version")
        request = load_settlement_v2(document)
        if expected_revision is not None and request.reconciliation_revision != int(expected_revision):
            raise SettlementContractError("settlement references a different reconciliation revision",
                                          kind="stale_revision")
        if expected_commitment is not None and request.evidence_chain_commitment != expected_commitment:
            raise SettlementContractError("settlement commitment does not match the expectation",
                                          kind="commitment_mismatch")
        with self._connect() as conn:
            state = self._state_from_events(self._events(conn))
        chain = state.v2_records.get(request.action_ref)
        if not chain:
            raise SettlementContractError("action has no v2 reconciliation history",
                                          kind="missing_reconciliation")
        matching = [record for record in chain
                    if record.reconciliation_ref == request.reconciliation_ref]
        if not matching:
            raise SettlementContractError("referenced reconciliation revision is absent",
                                          kind="missing_reconciliation")
        record = matching[0]
        if record.revision != request.reconciliation_revision:
            raise SettlementContractError("referenced revision does not match the settlement",
                                          kind="stale_revision")
        if record.evidence_chain_commitment != request.evidence_chain_commitment:
            raise SettlementContractError("commitment mismatch against canonical history",
                                          kind="commitment_mismatch")
        if record.result_hash != request.result_hash or record.status != request.result_status:
            raise SettlementContractError("result hash or status mismatch against canonical history",
                                          kind="result_hash_mismatch")
        owners = {item.supersession_ref: item.old_evidence_ref
                  for item in state.supersessions if item.action_ref == request.action_ref}
        try:
            views = v2_views(chain, supersession_owners=owners)
        except ReconciliationChainError as exc:
            raise SettlementContractError(f"evidence chain is invalid ({exc.kind})",
                                          kind="commitment_mismatch") from None
        target = next((item for item in views if item.reconciliation_ref == request.reconciliation_ref),
                      None)
        if target is None:
            raise SettlementContractError("revision is not materializable", kind="missing_reconciliation")
        if target.evidence_count != request.evidence_count:
            raise SettlementContractError("evidence count mismatch against canonical history",
                                          kind="commitment_mismatch")
        return {"settlement_request_ref": settlement_request_ref, "action_ref": request.action_ref,
                "reconciliation_ref": request.reconciliation_ref,
                "reconciliation_revision": request.reconciliation_revision,
                "evidence_count": request.evidence_count,
                "evidence_chain_commitment": request.evidence_chain_commitment,
                "result_status": request.result_status, "result_hash": request.result_hash,
                "candidate": request.candidate, "target_consumer": request.target_consumer,
                "evidence_refs": target.supporting_evidence_refs, "read_only": True}

    def journal_rows(self) -> tuple[dict[str, Any], ...]:
        with self._connect() as conn:
            return tuple(self._events(conn))

    def verify(self) -> dict[str, Any]:
        events = self.journal_rows()
        receipts, supersessions, reconciliations = self._truth(events)
        self._receipt_store(receipts, supersessions)
        return {"sequence": len(events), "head_hash": events[-1]["record_hash"] if events else GENESIS_HASH,
                "receipts": len(receipts), "supersessions": len(supersessions),
                "reconciliations": len(reconciliations)}

    def _hold_uncommitted_for_test(self, ready_path: str, release_path: str) -> None:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._append_locked(conn, "TEST_UNCOMMITTED", "test:lock", {"marker": "rollback"},
                                _now(), "test:uncommitted")
            Path(ready_path).write_text("locked", encoding="utf-8")
            while not Path(release_path).exists():
                threading.Event().wait(0.02)
            conn.commit()


__all__ = ["DurableContactReceiptStore", "DurableStoreError", "JournalCorruptionError",
           "UnsupportedSchemaError", "STORE_SCHEMA_VERSION", "RECORD_SCHEMA"]
