"""LPC0B WP2 audit bridge -- the hook-side facade and the drop-in candidate journal.

Authority
---------
Order ``LPC0B-R3-Lite`` sections 三A (hook writes only into a bounded queue), 三B, 三C, 三D.
Design record: ``LPC0B_WP2_R3_DESIGN_DECISION.md`` sections 4, 8, 9.

Two responsibilities
--------------------
1. **Hook-side facade.** ``enqueue_*`` builds a carrier, hands it to the bounded queue and
   returns.  No file is opened here, no lock is taken that the writer holds, no exception
   escapes.  This is the only audit API the ``pre_llm_call`` hook touches.

2. **Drop-in candidate journal.** ``CandidateAuditJournal`` lives in the *frozen* module
   ``candidate_sources_ag0.py`` (sha256
   ``7558dd0f4760d5a282937392461a12c1a8df4dd55962a7d265675c3f4671a1fa``) and may not be
   edited.  ``DropInCandidateAuditJournal`` therefore presents the *same duck-typed
   surface* -- ``journal_path``, ``append(...)``, ``list_entries(...)``,
   ``verify_integrity()`` -- and is injected at the non-frozen wiring point
   ``life_runtime_production.py`` via ``audit_journal_factory``.  Substitution, not
   modification.

   ``append`` still builds a real ``CandidateMaterializationRecord`` synchronously and
   returns it, so the caller's return value, its ``record_id`` shape and the
   ``CandidateMaterializer`` bookkeeping are unchanged.  Only the *durability timing*
   changes: the file write moves off the hook thread.  That change is declared in the
   design record (conflicts C-2 and C-3) and is the point of the order.

Chain continuity across restarts
--------------------------------
The frozen class seeds ``prev_entry_hash`` from an empty in-memory list, so the first append
after a restart writes ``"GENESIS"`` into a file that already has a chain.  The drop-in
seeds ``(next_seq, head_hash)`` from the **durable** candidate records instead, so a restart
continues the chain.  See ``LPC0B_R3_CANDIDATE_AUDIT_RECOVERY.json`` for the measured defect.
"""
from __future__ import annotations

import hashlib
import os
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Optional

from lpc0b_audit_events import (  # type: ignore[import-not-found]
    GENESIS,
    KIND_CANDIDATE,
    KIND_GAP,
    KIND_INTEGRITY,
    KIND_METRICS,
    KIND_OBSERVATION,
    SCHEMA_HEALTH,
    gap_payload,
    monotonic_ms,
    now_iso,
    shallow_public,
)
from lpc0b_audit_journal import (  # type: ignore[import-not-found]
    DEFAULT_MAX_SEGMENTS,
    DEFAULT_RETENTION_SECONDS,
    DEFAULT_SEGMENT_CAP_BYTES,
    CORRUPT,
    UNAVAILABLE,
    SegmentJournal,
)
from lpc0b_audit_queue import BoundedAuditQueue  # type: ignore[import-not-found]
from lpc0b_audit_writer import AuditWriter  # type: ignore[import-not-found]

#: Environment seam so a responsible owner can move the audit journals without a code
#: change (design record conflict C-4).
AUDIT_ROOT_ENV = "CHIYO_LPC0B_AUDIT_ROOT"

DEFAULT_QUEUE_CAPACITY = 1024


def _sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _frozen_ag0() -> Any:
    """Return the frozen AG-0 module object that the running runtime already imported."""
    module = sys.modules.get("candidate_sources_ag0")
    if module is not None:
        return module
    import candidate_sources_ag0 as ag0  # noqa: WPS433 - lazy by design

    return ag0


@dataclass
class AuditCarrier:
    """A queued unit of work.  Duck-typed against what the queue and writer need."""

    kind: str
    payload: dict[str, Any] = field(default_factory=dict)
    at: str = ""

    def __post_init__(self) -> None:
        if not self.at:
            self.at = now_iso()

    def size_bytes(self) -> int:
        import json as _json

        return len(_json.dumps({"kind": self.kind, "payload": self.payload},
                               ensure_ascii=False).encode("utf-8")) + 32


class DropInCandidateAuditJournal:
    """Duck-typed replacement for the frozen ``CandidateAuditJournal``.

    Same constructor, same attribute, same three methods.  ``append`` returns a real
    ``CandidateMaterializationRecord``; the file write is queued.
    """

    def __init__(self, journal_path: Optional[Path] = None,
                 *, bridge: Optional["AuditBridge"] = None) -> None:
        self.journal_path = Path(journal_path) if journal_path is not None else None
        self._bridge = bridge
        self._lock = threading.RLock()
        self._pending: list[dict[str, Any]] = []
        self._accepted_chain: list[tuple[str, str]] = []   # (prev_entry_hash, entry_hash)
        self._next_seq = 1
        self._head_hash = GENESIS
        self._seed_report: dict[str, Any] = {}
        self._legacy_file_present = False
        self._seed_from_durable()

    # -- seeding ----------------------------------------------------------------------

    def _durable_candidate_payloads(self) -> list[dict[str, Any]]:
        if self._bridge is None:
            return []
        journal = self._bridge.candidate_journal
        out: list[dict[str, Any]] = []
        for record in journal.read_all():
            if record.get("kind") == KIND_CANDIDATE:
                payload = record.get("payload")
                if isinstance(payload, Mapping):
                    out.append(dict(payload))
        return out

    def _seed_from_durable(self) -> None:
        """Continue the chain from what is already durable.  Never invents a record."""
        legacy = None
        if self.journal_path is not None:
            legacy = self.journal_path
            if legacy.exists():
                # The pre-fix journal was a single unsegmented file with a broken chain.
                # Refuse to treat it as this journal's history (order section 三B: never
                # present a damaged file as complete history, never skip it silently).
                self._legacy_file_present = True
        durable = self._durable_candidate_payloads()
        if durable:
            last = durable[-1]
            self._head_hash = str(last.get("entry_hash") or GENESIS)
            self._next_seq = len(durable) + 1
        self._seed_report = {
            "durable_candidate_records": len(durable),
            "seeded_next_seq": self._next_seq,
            "seeded_head_hash": self._head_hash,
            "legacy_unsegmented_journal_present": self._legacy_file_present,
            "method": "seeded from durable candidate records, not from an empty in-memory list",
        }

    # -- frozen surface ---------------------------------------------------------------

    def append(self, *, candidate: Any, event_type: str, reason_code: str,
               timestamp: str) -> Any:
        """Build and return the frozen record; enqueue its durability.  Never blocks."""
        ag0 = _frozen_ag0()
        with self._lock:
            seq = self._next_seq
            prev_hash = self._head_hash
            rec_id = f"caud:{_sha256_hex(f'{seq}:{candidate.candidate_id}:{event_type}:{timestamp}')[:16]}"
            record = ag0.CandidateMaterializationRecord(
                record_id=rec_id,
                candidate_id=candidate.candidate_id,
                source_kind=candidate.source_kind,
                source_ref=candidate.source_ref,
                source_revision=candidate.source_revision,
                event_type=event_type,
                availability_status=candidate.availability_status,
                materialized_at=candidate.created_at,
                invalidated_at=(timestamp
                                if candidate.availability_status != ag0.CANDIDATE_STATUS_OPEN
                                else None),
                reason_code=reason_code,
                policy_version=candidate.policy_version,
                prev_entry_hash=prev_hash,
            )
            self._next_seq = seq + 1
            self._head_hash = record.entry_hash
            self._accepted_chain.append((record.prev_entry_hash, record.entry_hash))
            payload = record.to_dict()
            self._pending.append(payload)
        if self._bridge is not None:
            self._bridge.enqueue_candidate(payload)
        return record

    def list_entries(self, *, candidate_id: Optional[str] = None) -> list[dict[str, Any]]:
        """Durable entries first, then entries accepted but not yet durable.

        Returns exactly the frozen shape (``record.to_dict()``).  The count of entries that
        are not yet durable is exposed separately via :meth:`pending_not_yet_durable` and in
        ``AUDIT_HEALTH``, so the frozen return shape is not silently altered.
        """
        rows = [dict(p) for p in self._durable_candidate_payloads()]
        with self._lock:
            rows.extend(dict(p) for p in self._pending)
        if candidate_id is not None:
            rows = [r for r in rows if r.get("candidate_id") == candidate_id]
        return rows

    def pending_not_yet_durable(self) -> int:
        with self._lock:
            return len(self._pending)

    def _drain_wait(self, timeout_s: Optional[float]) -> None:
        """Wait, with a hard bound, for this journal's own backlog to become durable.

        Called only from shutdown.  Never called by the hook.
        """
        import time as _time

        deadline = _time.monotonic() + float(timeout_s or 5.0)
        while self.pending_not_yet_durable() > 0 and _time.monotonic() < deadline:
            _time.sleep(0.01)

    def verify_integrity(self) -> bool:
        """Verify the durable chain *and* the in-process chain, and report pending work.

        The frozen implementation returns ``True`` while ``self._entries`` is empty, which
        is why a corrupt file reported valid.  This one is deliberately strict: any pending
        record, any recovery hole, a legacy unsegmented file, or any chain break yields
        ``False``.
        """
        if self._legacy_file_present:
            return False
        if self._bridge is None:
            return False
        journal = self._bridge.candidate_journal
        if not journal.verify_integrity():
            return False
        if journal.pending_recovery_gap is not None:
            return False
        with self._lock:
            if self._pending:
                return False
        durable = self._durable_candidate_payloads()
        head = GENESIS
        for _index, payload in enumerate(durable):
            prev = str(payload.get("prev_entry_hash") or "")
            if prev != head and not (head == GENESIS and prev != GENESIS and _index == 0):
                # A non-GENESIS start is only acceptable as the visible tail of a chain
                # whose earlier segments were pruned; the links after it must still hold.
                return False
            head = str(payload.get("entry_hash") or "")
        if durable and head != self._head_hash:
            return False
        with self._lock:
            chain = list(self._accepted_chain)
        prev = GENESIS
        if durable:
            prev = str(self._seed_report.get("seeded_head_hash") or GENESIS)
        for link_prev, link_hash in chain:
            if link_prev != prev:
                return False
            prev = link_hash
        return True

    # -- reporting --------------------------------------------------------------------

    def stats(self) -> dict[str, Any]:
        with self._lock:
            pending = len(self._pending)
        return {
            "journal_path": str(self.journal_path) if self.journal_path else None,
            "next_seq": self._next_seq,
            "head_hash": self._head_hash,
            "pending_not_yet_durable": pending,
            "legacy_unsegmented_journal_present": self._legacy_file_present,
            "seed": dict(self._seed_report),
        }


class AuditBridge:
    """Owns the queue, the writer and the journals for this process."""

    def __init__(
        self,
        *,
        install_root: Path | str,
        audit_root: Optional[Path | str] = None,
        capacity: int = DEFAULT_QUEUE_CAPACITY,
        segment_cap_bytes: int = DEFAULT_SEGMENT_CAP_BYTES,
        max_segments: int = DEFAULT_MAX_SEGMENTS,
        retention_seconds: int = DEFAULT_RETENTION_SECONDS,
        delay_s: float = 0.0,
        fault_injector: Any = None,
        candidate_journal: Optional[SegmentJournal] = None,
    ) -> None:
        self.install_root = Path(install_root)
        self.audit_root = Path(
            audit_root or os.environ.get(AUDIT_ROOT_ENV) or (self.install_root / "logs" / "audit"))
        self.queue = BoundedAuditQueue(capacity=capacity)
        self._shared = dict(
            segment_cap_bytes=int(segment_cap_bytes),
            max_segments=int(max_segments),
            retention_seconds=int(retention_seconds),
        )
        if candidate_journal is None:
            self.candidate_journal = SegmentJournal(
                self.audit_root / "candidate", base_name="candidate_audit_journal", **self._shared)
            self._candidate_journal_is_shared_audit_root = True
        else:
            self.candidate_journal = candidate_journal
            self._candidate_journal_is_shared_audit_root = False
        self.observation_journal = SegmentJournal(
            self.audit_root / "observation", base_name="observation", **self._shared)
        self.writer = AuditWriter(
            queue=self.queue,
            candidate_journal=self.candidate_journal,
            observation_journal=self.observation_journal,
            metrics_path=self.audit_root / "audit_metrics.json",
            health_path=self.health_path,
            delay_s=delay_s,
            fault_injector=fault_injector,
        )
        self._journal_lock = threading.RLock()
        self._journals: list[DropInCandidateAuditJournal] = []

    @property
    def health_path(self) -> Path:
        return self.audit_root / "AUDIT_HEALTH.json"

    # -- lifecycle --------------------------------------------------------------------

    def start(self) -> bool:
        try:
            self.writer.start()
        except Exception:  # noqa: BLE001 - audit start must never break plugin load
            return False
        return True

    def stop(self, *, drain: bool = True, timeout_s: Optional[float] = None) -> dict[str, Any]:
        for journal in list(self._journals):
            try:
                journal._drain_wait(timeout_s)  # noqa: SLF001 - same subsystem
            except Exception:  # noqa: BLE001
                pass
        return self.writer.stop(drain=drain, timeout_s=timeout_s)

    # -- hook-side facade (no disk, no blocking) ---------------------------------------

    def _carrier(self, kind: str, payload: Mapping[str, Any]) -> AuditCarrier:
        return AuditCarrier(kind=kind, payload=shallow_public(payload))

    def enqueue_candidate(self, record: Mapping[str, Any]) -> bool:
        return self.writer.enqueue(self._carrier(KIND_CANDIDATE, record))

    def enqueue_observation(self, record: Mapping[str, Any]) -> bool:
        return self.writer.enqueue(self._carrier(KIND_OBSERVATION, record))

    def enqueue_metrics(self, snapshot: Mapping[str, Any]) -> bool:
        return self.writer.enqueue(self._carrier(KIND_METRICS, snapshot))

    def enqueue_integrity(self, reason: str) -> bool:
        return self.writer.enqueue(self._carrier(KIND_INTEGRITY, {"reason": str(reason)}))

    def enqueue_gap(self, *, dropped_count: int, reason: str) -> bool:
        """Record an observable hole.  Uses the reserved queue slot."""
        carrier = self._carrier(
            KIND_GAP, gap_payload(dropped_count=max(1, int(dropped_count)), reason=reason))
        return self.writer.enqueue(carrier, is_gap=True)

    # -- candidate journal factory ------------------------------------------------------

    def candidate_journal_factory(self, journal_path: Path) -> DropInCandidateAuditJournal:
        """Build the drop-in journal for ``state_root/candidate_audit_journal.jsonl``."""
        with self._journal_lock:
            journal = DropInCandidateAuditJournal(journal_path, bridge=self)
            self._journals.append(journal)
        if journal._legacy_file_present:  # noqa: SLF001 - same subsystem
            self.candidate_journal._corrupt(  # noqa: SLF001
                "LEGACY_UNSEGMENTED_JOURNAL_PRESENT")
        return journal

    # -- reporting ----------------------------------------------------------------------

    def health(self) -> dict[str, Any]:
        doc = self.writer.health()
        with self._journal_lock:
            doc["candidate_journals"] = [j.stats() for j in self._journals]
        doc["audit_root"] = str(self.audit_root)
        doc["install_root"] = str(self.install_root)
        return doc

    def cognition_effect_permitted(self) -> bool:
        if not self.writer.is_writer_alive():
            return False
        with self._journal_lock:
            if any(j._legacy_file_present for j in self._journals):  # noqa: SLF001
                return False
            if any(j.pending_not_yet_durable() for j in self._journals):
                return False
        return self.writer.cognition_effect_permitted()

# ---------------------------------------------------------------------------------
# module-level singleton
# ---------------------------------------------------------------------------------

_LOCK = threading.RLock()
_BRIDGE: Optional[AuditBridge] = None


def configure(**kwargs: Any) -> AuditBridge:
    """Build (once) and return the process-wide bridge.  Idempotent."""
    global _BRIDGE
    with _LOCK:
        if _BRIDGE is None:
            _BRIDGE = AuditBridge(**kwargs)
        return _BRIDGE


def bridge() -> Optional[AuditBridge]:
    return _BRIDGE


def start() -> bool:
    with _LOCK:
        current = _BRIDGE
    if current is None:
        return False
    return current.start()

def is_running() -> bool:
    """True only when a configured bridge has a live writer thread.

    The plugin consults this before injecting the journal factory: injecting it while the
    writer is down would route audit writes into a queue nobody drains, which is worse than
    leaving the frozen synchronous journal in place for that run.
    """
    current = _BRIDGE
    if current is None:
        return False
    try:
        return bool(current.writer._started) and current.writer.is_writer_alive()  # noqa: SLF001
    except Exception:  # noqa: BLE001
        return False


def stop(**kwargs: Any) -> dict[str, Any]:
    with _LOCK:
        current = _BRIDGE
    if current is None:
        return unresolvable_health("NOT_CONFIGURED")
    return current.stop(**kwargs)


def enqueue_candidate(record: Mapping[str, Any]) -> bool:
    current = _BRIDGE
    if current is None or not current.writer._started:  # noqa: SLF001 - same subsystem
        return False
    ok = current.enqueue_candidate(record)
    if not ok:
        current.enqueue_gap(dropped_count=1, reason="QUEUE_OVERFLOW_CANDIDATE")
    return ok


def enqueue_observation(record: Mapping[str, Any]) -> bool:
    current = _BRIDGE
    if current is None or not current.writer._started:  # noqa: SLF001
        return False
    ok = current.enqueue_observation(record)
    if not ok:
        current.enqueue_gap(dropped_count=1, reason="QUEUE_OVERFLOW_OBSERVATION")
    return ok


def enqueue_metrics(snapshot: Mapping[str, Any]) -> bool:
    current = _BRIDGE
    if current is None or not current.writer._started:  # noqa: SLF001
        return False
    return current.enqueue_metrics(snapshot)


def enqueue_integrity(reason: str) -> bool:
    current = _BRIDGE
    if current is None or not current.writer._started:  # noqa: SLF001
        return False
    return current.enqueue_integrity(reason)


def candidate_journal_factory(journal_path: Path) -> Any:
    """Factory for ``life_runtime_production.build_production_life_runtime``."""
    current = _BRIDGE
    if current is None:
        # Fail closed: fall back to the frozen journal rather than to a half-built one.
        ag0 = _frozen_ag0()
        return ag0.CandidateAuditJournal(journal_path)
    return current.candidate_journal_factory(journal_path)


def unresolvable_health(reason: str) -> dict[str, Any]:
    """The fail-closed answer when no bridge exists (order sections 三B/三D)."""
    return {
        "schema": SCHEMA_HEALTH,
        "at": now_iso(),
        "state": UNAVAILABLE,
        "reasons": [str(reason)],
        "writer": {"started": False, "alive": False},
        "queue": {},
        "counters": {},
        "cognition_effect_permitted": False,
        "method": "no in-process audit bridge; reporting UNAVAILABLE rather than guessing",
    }


def audit_health() -> dict[str, Any]:
    current = _BRIDGE
    if current is None:
        return unresolvable_health("NOT_CONFIGURED")
    try:
        return current.health()
    except Exception as exc:  # noqa: BLE001 - health reporting must never raise
        return unresolvable_health(f"HEALTH_BUILD_FAILED:{type(exc).__name__}")


def cognition_effect_permitted() -> bool:
    """Fail-closed gate consulted by the hook before any effect could run."""
    current = _BRIDGE
    if current is None:
        return False
    try:
        return bool(current.cognition_effect_permitted())
    except Exception:  # noqa: BLE001
        return False


def reset() -> None:
    """Test-only: drop the singleton so a disposable root can be configured afresh."""
    global _BRIDGE
    with _LOCK:
        _BRIDGE = None


def queue_depth() -> int:
    current = _BRIDGE
    return len(current.queue) if current is not None else 0


def monotonic_age_ms() -> int:
    return monotonic_ms()
