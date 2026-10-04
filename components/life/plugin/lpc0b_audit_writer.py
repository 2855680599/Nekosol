"""LPC0B WP2 Audit Writer -- the single journal writer and the audit health publisher.

Authority
---------
Order ``LPC0B-R3-Lite`` sections 三A (responsibility split), 三B, 三C, 三D.
Design record: ``LPC0B_WP2_R3_DESIGN_DECISION.md`` sections 4.2, 5, 6, 7.

One thread per process.  It is the only code that opens a journal for writing, the only
code that runs startup recovery, and the only code that rotates or prunes.  The
``pre_llm_call`` hook never calls into this module's disk paths -- it only enqueues.

Everything this class does is optional observability.  Therefore:

* every failure is caught, counted and published -- never propagated to the turn;
* a failure never makes the process exit, and never makes the hook block;
* when the audit cannot be trusted the published ``cognition_effect_permitted`` is
  ``false``, which is the fail-closed gate section 三B/三D requires.

Fault-injection seams
---------------------
``delay_s`` and ``fault_injector`` exist so WP-C can measure a slow writer and an
``ENOSPC`` disk.  Both default to inert: ``delay_s=0.0`` and ``fault_injector=None``.  No
production path sets them.
"""
from __future__ import annotations

import json
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

from lpc0b_audit_events import (  # type: ignore[import-not-found]
    KIND_CANDIDATE,
    KIND_GAP,
    KIND_INTEGRITY,
    KIND_METRICS,
    KIND_OBSERVATION,
    SCHEMA_HEALTH,
    AuditRecord,
    monotonic_ms,
    now_iso,
)
from lpc0b_audit_journal import (  # type: ignore[import-not-found]
    CORRUPT,
    DEGRADED,
    HEALTHY,
    UNAVAILABLE,
    JournalUnavailable,
    JournalWriteError,
    SegmentJournal,
)

#: A writer that has not beaten for this long is reported as not alive.
WRITER_BEAT_TIMEOUT_MS = 5000


class AuditWriter:
    """Owns the bounded queue's consumer side, the journals, and ``AUDIT_HEALTH``."""

    def __init__(
        self,
        *,
        queue: Any,
        candidate_journal: SegmentJournal,
        observation_journal: Optional[SegmentJournal] = None,
        metrics_path: Optional[Path | str] = None,
        health_path: Optional[Path | str] = None,
        poll_interval_s: float = 0.05,
        flush_batch: int = 256,
        metrics_interval_s: float = 1.0,
        health_interval_s: float = 1.0,
        drain_timeout_s: float = 5.0,
        delay_s: float = 0.0,
        fault_injector: Optional[Callable[[AuditRecord], None]] = None,
    ) -> None:
        self.queue = queue
        self.candidate_journal = candidate_journal
        self.observation_journal = observation_journal
        self.metrics_path = Path(metrics_path) if metrics_path else None
        self.health_path = Path(health_path) if health_path else None
        self.poll_interval_s = float(poll_interval_s)
        self.flush_batch = int(flush_batch)
        self.metrics_interval_s = float(metrics_interval_s)
        self.health_interval_s = float(health_interval_s)
        self.drain_timeout_s = float(drain_timeout_s)
        self.delay_s = float(delay_s)
        self.fault_injector = fault_injector

        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._drain_on_stop = False
        self._lock = threading.RLock()
        self._started = False
        self._last_beat_ms = monotonic_ms()
        self._last_health_ms = 0
        self._last_metrics_ms = 0
        self.started_at: Optional[str] = None

        self.counters: dict[str, Any] = {
            "records_committed": 0,
            "candidate_records_committed": 0,
            "observation_records_committed": 0,
            "gap_records_committed": 0,
            "metrics_snapshots": 0,
            "integrity_alarms": 0,
            "oversized_dropped_total": 0,
            "dropped_overflow_total": 0,
            "write_failures": 0,
            "rotation_failures": 0,
            "writer_crashes": 0,
            "journal_unavailable_events": 0,
            "latest_metrics": None,
            "integrity_alarm": None,
        }
        self.startup_recovery: dict[str, Any] = {}

    # -- lifecycle --------------------------------------------------------------------

    def start(self) -> None:
        """Run startup recovery, then start the single writer thread.  Idempotent."""
        with self._lock:
            if self._started:
                return
            self.startup_recovery = {
                "candidate": self.candidate_journal.recover(),
            }
            if self.observation_journal is not None:
                self.startup_recovery["observation"] = self.observation_journal.recover()
            # Order section 三B: a quarantine produced by recovery must be recorded as data
            # before anything else is appended, otherwise the hole is invisible.
            self._commit_pending_recovery_gap()
            self._started = True
            self.started_at = now_iso()
            self._last_beat_ms = monotonic_ms()
            self._thread = threading.Thread(
                target=self._run, name="lpc0b-audit-writer", daemon=True)
            self._thread.start()
            self._publish_health(force=True)

    def stop(self, *, drain: bool = True, timeout_s: Optional[float] = None) -> dict[str, Any]:
        """Stop the writer.  ``drain=True`` tries to persist what is already queued."""
        with self._lock:
            if not self._started:
                return self.health()
        budget = float(self.drain_timeout_s if timeout_s is None else timeout_s)
        deadline = time.monotonic() + max(0.0, budget)
        # The background thread remains the sole consumer even during shutdown.
        self._drain_on_stop = bool(drain)
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=max(0.0, deadline - time.monotonic()))
        if thread is not None and thread.is_alive():
            # Never close a journal while the consumer can still be committing.
            return {**self.health(), "shutdown_incomplete": True}
        try:
            self.candidate_journal.close()
        except Exception:  # noqa: BLE001 - shutdown must not raise
            pass
        if self.observation_journal is not None:
            try:
                self.observation_journal.close()
            except Exception:  # noqa: BLE001
                pass
        self._write_metrics(force=True)
        self._publish_health(force=True)
        with self._lock:
            self._started = False
        return self.health()

    # -- producer side (called by the bridge, never by a disk path) --------------------

    def enqueue(self, record: AuditRecord, *, is_gap: bool = False) -> bool:
        """Hand a record to the queue.  Never blocks, never raises."""
        if not self._started:
            self.counters["write_failures"] += 1
            return False
        try:
            result = self.queue.put(record, is_gap=is_gap)
        except Exception:  # noqa: BLE001 - the queue is defensive already
            self.counters["write_failures"] += 1
            return False
        return str(result) == "accepted"

    # -- writer thread ----------------------------------------------------------------

    def _run(self) -> None:
        while not self._stop.is_set() or (self._drain_on_stop and len(self.queue) > 0):
            try:
                self._pump_once()
            except BaseException as exc:  # noqa: BLE001 - the writer must survive anything
                self.counters["writer_crashes"] += 1
                self.counters["integrity_alarm"] = (
                    f"WRITER_CRASH:{type(exc).__name__}")
                try:
                    self._publish_health(force=True)
                except Exception:  # noqa: BLE001
                    pass
                self._stop.wait(self.poll_interval_s)
                continue
            self._last_beat_ms = monotonic_ms()
            self._write_metrics()
            self._publish_health()
            # Yield even when the queue is empty; shutdown interrupts this wait.
            self._stop.wait(self.poll_interval_s)
        self._last_beat_ms = monotonic_ms()

    def _pump_once(self) -> None:
        records = self.queue.drain(max_items=self.flush_batch)
        if not records:
            self._last_beat_ms = monotonic_ms()
            return
        for record in records:
            self._handle(record)
        self._last_beat_ms = monotonic_ms()

    def _handle(self, record: AuditRecord) -> None:
        if self.delay_s:
            # WP-C T07's slow-disk stand-in.  Inert unless a test sets it.
            time.sleep(self.delay_s)
        if self.fault_injector is not None:
            try:
                self.fault_injector(record)
            except OSError as exc:
                self.counters["write_failures"] += 1
                self.candidate_journal._degrade(  # noqa: SLF001 - deliberate, same owner
                    f"INJECTED_WRITE_FAILURE:{exc.__class__.__name__}")
                return

        kind = record.kind
        try:
            if kind == KIND_GAP:
                self._commit_gap(record)
            elif kind == KIND_METRICS:
                self.counters["latest_metrics"] = dict(record.payload)
                self.counters["metrics_snapshots"] += 1
            elif kind == KIND_INTEGRITY:
                self.counters["integrity_alarms"] += 1
                self.counters["integrity_alarm"] = record.payload.get("reason")
                self._publish_health(force=True)
            elif kind == KIND_OBSERVATION:
                journal = self.observation_journal or self.candidate_journal
                journal.commit(journal.next_record(KIND_OBSERVATION, record.payload))
                self.counters["records_committed"] += 1
                self.counters["observation_records_committed"] += 1
            else:
                journal = self.candidate_journal
                journal.commit(journal.next_record(kind, record.payload))
                self.counters["records_committed"] += 1
                if kind == KIND_CANDIDATE:
                    self.counters["candidate_records_committed"] += 1
        except JournalUnavailable:
            self.counters["journal_unavailable_events"] += 1
        except JournalWriteError as exc:
            journal = self.candidate_journal
            self.counters["write_failures"] += 1
            reason = "DISK_FULL" if getattr(exc, "errno", None) else "DISK_WRITE_FAILED"
            journal._degrade(f"{reason}:{exc.__class__.__name__}")  # noqa: SLF001
        except OSError as exc:
            self.counters["write_failures"] += 1
            self.candidate_journal._degrade(  # noqa: SLF001
                f"DISK_WRITE_FAILED:{exc.__class__.__name__}")

    def _commit_gap(self, record: AuditRecord) -> None:
        """Persist an explicit hole at the sequence where the journal resumes."""
        journal = self.candidate_journal
        payload = dict(record.payload)
        payload["first_dropped_seq"] = journal.seq + 1
        journal.commit(journal.next_record(KIND_GAP, payload))
        self.counters["records_committed"] += 1
        self.counters["gap_records_committed"] += 1
        self.counters["dropped_overflow_total"] += int(payload.get("dropped_count") or 0)

    def _commit_pending_recovery_gap(self) -> None:
        """Write the recovery hole as the next durable record, or stay fail-closed.

        ``SegmentJournal.commit`` refuses while ``pending_recovery_gap`` is set, so the flag
        has to be lifted for exactly the length of this one append and restored if it fails.
        Without that the hole could never be recorded and the journal would stay paused
        forever -- a permanent silent stop, which is worse than the hole it was reporting.
        """
        for journal in (self.candidate_journal, self.observation_journal):
            if journal is None or journal.pending_recovery_gap is None:
                continue
            payload = journal.pending_recovery_gap
            journal.pending_recovery_gap = None
            try:
                journal.commit(journal.next_record(KIND_GAP, payload))
                self.counters["gap_records_committed"] += 1
            except (JournalUnavailable, JournalWriteError, OSError):
                journal.pending_recovery_gap = payload
                journal._note("RECOVERY_GAP_NOT_RECORDED")  # noqa: SLF001 - same subsystem
                continue
            journal._note("RECOVERY_GAP_RECORDED")  # noqa: SLF001

    # -- metrics ----------------------------------------------------------------------

    def _write_metrics(self, *, force: bool = False) -> None:
        path = self.metrics_path
        if path is None:
            return
        now = monotonic_ms()
        if not force and (now - self._last_metrics_ms) < int(self.metrics_interval_s * 1000):
            return
        self._last_metrics_ms = now
        doc = {
            "schema": "lpc0b.audit_metrics.v1",
            "at": now_iso(),
            "writer_pid": os.getpid(),
            "queue": self.queue.snapshot() if hasattr(self.queue, "snapshot") else {},
            "counters": {k: v for k, v in self.counters.items() if k != "latest_metrics"},
            "candidate_journal": self.candidate_journal.stats(),
            "observation_journal": (self.observation_journal.stats()
                                    if self.observation_journal is not None else None),
            "latest_hook_metrics": self.counters.get("latest_metrics"),
        }
        self._atomic_write(path, doc)

    # -- health -----------------------------------------------------------------------

    def is_writer_alive(self) -> bool:
        thread = self._thread
        if thread is None or not thread.is_alive():
            return False
        return (monotonic_ms() - self._last_beat_ms) <= WRITER_BEAT_TIMEOUT_MS

    def audit_state(self) -> tuple[str, list[str]]:
        """Combine the journal states into one audit state (worst wins)."""
        journals = [self.candidate_journal]
        if self.observation_journal is not None:
            journals.append(self.observation_journal)
        state = HEALTHY
        reasons: list[str] = []
        for journal in journals:
            if journal.state == UNAVAILABLE:
                state = UNAVAILABLE
            elif journal.state == CORRUPT and state != UNAVAILABLE:
                state = CORRUPT
            elif journal.state == DEGRADED and state == HEALTHY:
                state = DEGRADED
            reasons.extend(str(r) for r in journal.reasons)
        if not self.is_writer_alive():
            reasons.append("WRITER_DEAD")
            if state == HEALTHY:
                state = DEGRADED
        if self.counters["dropped_overflow_total"] > 0:
            reasons.append("QUEUE_OVERFLOW")
            if state == HEALTHY:
                state = DEGRADED
        if self.counters["writer_crashes"] > 0:
            reasons.append("WRITER_CRASH")
            if state == HEALTHY:
                state = DEGRADED
        # de-duplicate, keep order
        seen: set[str] = set()
        unique = [r for r in reasons if not (r in seen or seen.add(r))]
        return state, unique

    def cognition_effect_permitted(self) -> bool:
        """Fail-closed gate: the Cognition Effect may only run on a fully healthy audit."""
        state, reasons = self.audit_state()
        if state != HEALTHY:
            return False
        if self.counters["dropped_overflow_total"] > 0:
            return False
        if self.candidate_journal.pending_recovery_gap is not None:
            return False
        return not reasons

    def health(self) -> dict[str, Any]:
        state, reasons = self.audit_state()
        return {
            "schema": SCHEMA_HEALTH,
            "at": now_iso(),
            "state": state,
            "reasons": reasons,
            "writer": {
                "pid": os.getpid(),
                "started": bool(self._started),
                "started_at": self.started_at,
                "alive": self.is_writer_alive(),
                "thread_alive": bool(self._thread is not None and self._thread.is_alive()),
                "beat_monotonic_ms": self._last_beat_ms,
                "beat_age_ms": max(0, monotonic_ms() - self._last_beat_ms),
            },
            "queue": self.queue.snapshot() if hasattr(self.queue, "snapshot") else {},
            "counters": {k: v for k, v in self.counters.items() if k != "latest_metrics"},
            "candidate_journal": self.candidate_journal.stats(),
            "observation_journal": (self.observation_journal.stats()
                                    if self.observation_journal is not None else None),
            "startup_recovery": self.startup_recovery,
            "cognition_effect_permitted": self.cognition_effect_permitted(),
            "method": ("built from in-process writer state; liveness uses the monotonic clock, "
                       "never a file mtime"),
        }

    def _publish_health(self, *, force: bool = False) -> None:
        path = self.health_path
        if path is None:
            return
        now = monotonic_ms()
        if not force and (now - self._last_health_ms) < int(self.health_interval_s * 1000):
            return
        self._last_health_ms = now
        self._atomic_write(path, self.health())

    # -- atomic publish ---------------------------------------------------------------

    @staticmethod
    def _atomic_write(path: Path, doc: Mapping[str, Any]) -> None:
        """Order section 三C: independent temporary file plus a safe publish boundary."""
        try:
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(path.parent, 0o700)
            tmp = path.with_name(f".{path.name}.tmp.{uuid.uuid4().hex}")
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(json.dumps(doc, indent=2, ensure_ascii=False) + "\n")
            os.replace(tmp, path)
        except OSError:
            # Health/metrics publication is itself optional observability.
            pass
