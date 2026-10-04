"""LPC0B WP2 segment journal -- recovery, corruption detection, rotation, retention.

Authority
---------
Order ``LPC0B-R3-Lite`` sections 三A (requirements 2, 4), 三B (recovery and corruption),
三C (retention / rotation), 三D (non-blocking).
Design record: ``LPC0B_WP2_R3_DESIGN_DECISION.md`` sections 5.2, 5.3, 5.6.

Only the Audit Writer thread calls ``commit``.  The hook never imports this module.

Why the frozen ``CandidateAuditJournal`` could not be reused
-----------------------------------------------------------
``candidate_sources_ag0.CandidateAuditJournal`` (a frozen module, sha256
``7558dd0f4760d5a282937392461a12c1a8df4dd55962a7d265675c3f4671a1fa``) starts from
``self._entries = []``, never reads the existing file, derives ``prev_entry_hash`` from the
in-memory head, and verifies only the in-memory list.  Measured consequences
(``LPC0B_R3_CANDIDATE_AUDIT_RECOVERY.json``, ``LPC0B_R3_AUDIT_CORRUPTION_BEHAVIOR.json``,
``LPC0B_R3_AUDIT_RETENTION_ROTATION.json``): a restart begins a second ``GENESIS`` chain, a
malformed file is reported valid, and the file grows without bound.

This module fixes exactly those four defects and nothing else.  The frozen class is not
edited; a duck-typed replacement is injected at the non-frozen wiring point (design record
section 8).

Recovery decision table (order section 三B, verbatim mapping)
------------------------------------------------------------
===========================================  ==========================================
complete well-formed records                 read normally
last record identifiably truncated           preserve raw bytes, quarantine the damaged
                                             tail, record an explicit recovery gap
a corrupted record in the MIDDLE             refuse to call the journal complete/valid
damage that cannot be classified             fail-closed
===========================================  ==========================================

Nothing here ever deletes a record that is still inside the retention window, and nothing
here silently skips an unreadable record.
"""
from __future__ import annotations
from contextlib import contextmanager


@contextmanager
def _private_append(path):
    flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, 0o600)
    try:
        os.fchmod(fd, 0o600)
        handle = os.fdopen(fd, "ab")
    except BaseException:
        os.close(fd)
        raise
    with handle:
        yield handle


import hashlib
import json
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any, Mapping, Optional

from lpc0b_audit_events import (  # type: ignore[import-not-found]
    GENESIS,
    KIND_RECOVERY_GAP,
    KIND_SEGMENT_HEADER,
    AuditRecord,
    MAX_RECORD_BYTES,
    now_iso,
    recovery_gap_payload,
    segment_header_payload,
)

# -- states -------------------------------------------------------------------------

HEALTHY = "HEALTHY"
DEGRADED = "DEGRADED"
CORRUPT = "CORRUPT"
UNAVAILABLE = "UNAVAILABLE"

# -- defaults (order section 三C "建议初版配置") -------------------------------------

DEFAULT_SEGMENT_CAP_BYTES = 8 * 1024 * 1024
DEFAULT_MAX_SEGMENTS = 4
DEFAULT_RETENTION_SECONDS = 7 * 24 * 3600

_SEGMENT_RE = re.compile(r"^[A-Za-z0-9_.-]+-(\d{6})\.jsonl$")


class JournalUnavailable(RuntimeError):
    """The journal refuses to accept new records (CORRUPT / UNAVAILABLE / paused)."""


class JournalWriteError(OSError):
    """The underlying write failed.  The writer maps this to a DEGRADED health state."""


class SegmentJournal:
    """Append-only, size-rotated JSONL journal with a single global hash chain."""

    def __init__(
        self,
        root: Path | str,
        *,
        base_name: str = "audit",
        segment_cap_bytes: int = DEFAULT_SEGMENT_CAP_BYTES,
        max_segments: int = DEFAULT_MAX_SEGMENTS,
        retention_seconds: int = DEFAULT_RETENTION_SECONDS,
        max_record_bytes: int = MAX_RECORD_BYTES,
    ) -> None:
        self.root = Path(root)
        self.base_name = str(base_name)
        self.segment_cap_bytes = int(segment_cap_bytes)
        self.max_segments = int(max_segments)
        self.retention_seconds = int(retention_seconds)
        self.max_record_bytes = int(max_record_bytes)
        if self.max_segments < 1:
            raise ValueError("max_segments must be >= 1")
        if self.segment_cap_bytes < self.max_record_bytes + 1024:
            raise ValueError(
                "segment_cap_bytes must exceed max_record_bytes by at least 1024 bytes "
                "so a segment header plus one record always fit")

        self.state = HEALTHY
        self.reasons: list[str] = []
        self.seq = 0
        self.head_hash = GENESIS
        self.recovery_report: dict[str, Any] = {}
        #: A recovery hole that must be written as the very next durable record.
        self.pending_recovery_gap: Optional[dict[str, Any]] = None

        self._segment_index = 0
        self._segment_path: Optional[Path] = None
        self._segment_bytes = 0
        self.rotation_count = 0
        self.pruned_segments: list[str] = []
        self.written_total = 0
        self.fsync_count = 0

    # -- paths ------------------------------------------------------------------------

    def segment_path(self, index: int) -> Path:
        return self.root / f"{self.base_name}-{int(index):06d}.jsonl"

    def segments(self) -> list[tuple[int, Path]]:
        found: list[tuple[int, Path]] = []
        try:
            entries = list(self.root.iterdir())
        except OSError:
            return found
        for entry in entries:
            if not entry.is_file():
                continue
            match = _SEGMENT_RE.match(entry.name)
            if not match or not entry.name.startswith(self.base_name + "-"):
                continue
            found.append((int(match.group(1)), entry))
        found.sort(key=lambda pair: pair[0])
        return found

    # -- state helpers ----------------------------------------------------------------

    def _note(self, reason: str) -> None:
        """Record a reason once.  A repeated condition is the same signal, not a new one."""
        if reason not in self.reasons:
            self.reasons.append(reason)

    def _degrade(self, reason: str) -> None:
        if self.state == HEALTHY:
            self.state = DEGRADED
        self._note(reason)

    def _corrupt(self, reason: str) -> None:
        self.state = CORRUPT
        self._note(reason)

    def _unavailable(self, reason: str) -> None:
        self.state = UNAVAILABLE
        self._note(reason)

    @property
    def accepting(self) -> bool:
        return self.state in (HEALTHY, DEGRADED) and self.pending_recovery_gap is None

    # -- recovery ---------------------------------------------------------------------

    def recover(self) -> dict[str, Any]:
        """Startup recovery.  Read-only except for a *proven* truncated tail.

        Returns the recovery report.  Never raises: a journal that cannot be trusted is
        reported as ``CORRUPT`` / ``UNAVAILABLE`` and refuses further appends.
        """
        report: dict[str, Any] = {
            "state": HEALTHY,
            "reasons": [],
            "segments": 0,
            "records": 0,
            "recovered_seq": 0,
            "recovered_head_hash": GENESIS,
            "quarantined": None,
            "terminating_newline_added": False,
            "front_pruned": False,
        }
        try:
            self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(self.root, 0o700)
        except OSError as exc:
            self._unavailable(f"ROOT_NOT_CREATABLE:{exc.__class__.__name__}")
            report.update(state=self.state, reasons=list(self.reasons))
            self.recovery_report = report
            return report

        segments = self.segments()
        report["segments"] = len(segments)
        if not segments:
            self.recovery_report = report
            return report

        expected_seq = 1
        head = GENESIS
        records = 0
        last_index = segments[-1][0]
        front_anchored = False

        for position, (index, path) in enumerate(segments):
            is_last = position == len(segments) - 1
            try:
                raw = path.read_bytes()
            except OSError as exc:
                self._corrupt(f"SEGMENT_UNREADABLE:{path.name}:{exc.__class__.__name__}")
                break
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                self._corrupt(f"SEGMENT_NOT_UTF8:{path.name}")
                break

            if raw == b"":
                if is_last:
                    # A created-but-never-written segment holds no records.  Reuse it.
                    self._segment_index = index
                    self._segment_path = path
                    self._segment_bytes = 0
                    break
                self._corrupt(f"EMPTY_MIDDLE_SEGMENT:{path.name}")
                break

            last_newline = raw.rfind(b"\n")
            if last_newline == -1:
                complete_bytes, tail_text = b"", text
            else:
                complete_bytes = raw[: last_newline + 1]
                tail_text = text[last_newline + 1:]

            complete_lines = complete_bytes.decode("utf-8").split("\n")
            if complete_lines and complete_lines[-1] == "":
                complete_lines.pop()

            bad = False
            for line_no, line in enumerate(complete_lines, start=1):
                if line.strip() == "":
                    self._corrupt(f"EMPTY_LINE:{path.name}:{line_no}")
                    bad = True
                    break
                try:
                    record = AuditRecord.from_line(line)
                except (ValueError, KeyError, TypeError):
                    self._corrupt(f"INVALID_JSON:{path.name}:{line_no}")
                    bad = True
                    break
                if not front_anchored:
                    # Anchoring: the oldest surviving record either really is the genesis of
                    # the chain, or it is the visible front of a chain whose earlier segments
                    # retention removed.  Both are decided here and reported; neither is
                    # guessed at, and every link after the anchor is still checked strictly.
                    front_anchored = True
                    if not (record.seq == 1 and record.prev_entry_hash == GENESIS):
                        expected_seq = record.seq
                        head = record.prev_entry_hash
                        report["front_pruned"] = True
                if record.seq != expected_seq:
                    self._corrupt(f"SEQUENCE_BREAK:{path.name}:{line_no}:"
                                  f"expected={expected_seq}:found={record.seq}")
                    bad = True
                    break
                if record.prev_entry_hash != head:
                    self._corrupt(f"CHAIN_BREAK:{path.name}:{line_no}")
                    bad = True
                    break
                if not record.verify_self():
                    self._corrupt(f"HASH_MISMATCH:{path.name}:{line_no}")
                    bad = True
                    break
                expected_seq += 1
                head = record.entry_hash
                records += 1
            if bad:
                break

            tail = tail_text.strip()
            if tail:
                if not is_last:
                    # The journal continued past a segment that did not end cleanly.
                    self._corrupt(f"BROKEN_NON_FINAL_SEGMENT:{path.name}")
                    break
                try:
                    candidate = AuditRecord.from_line(tail_text)
                except (ValueError, KeyError, TypeError):
                    candidate = None
                if (candidate is not None and candidate.seq == expected_seq
                        and candidate.prev_entry_hash == head and candidate.verify_self()):
                    # A complete record that merely lacked its terminating newline.
                    expected_seq += 1
                    head = candidate.entry_hash
                    records += 1
                    report["terminating_newline_added"] = True
                    self._add_terminating_newline(path, raw)
                else:
                    self._quarantine_truncated_tail(path, complete_bytes, raw, report)
                    if self.state == CORRUPT:
                        break
            elif tail_text:
                # Whitespace-only trailing bytes: complete the line terminator, drop nothing.
                self._add_terminating_newline(path, raw)
                report["terminating_newline_added"] = True

            self._segment_index = index
            self._segment_path = path
            try:
                # The file may have been shortened (quarantined tail) or lengthened
                # (terminating newline added), so measure it instead of trusting `raw`.
                self._segment_bytes = path.stat().st_size
            except OSError:
                self._segment_bytes = len(raw)

        self.seq = expected_seq - 1 if records else 0
        self.head_hash = head
        report["records"] = records
        report["recovered_seq"] = self.seq
        report["recovered_head_hash"] = self.head_hash
        if report.get("front_pruned"):
            # The chain start is gone because retention removed the oldest segments.  That
            # is a reported degradation, not a silent one, and it is never called CORRUPT.
            self._degrade("FRONT_PRUNED_BY_RETENTION")
            report["front_pruned_reason"] = "oldest segments removed by retention"
        report.update(state=self.state, reasons=list(self.reasons))
        if last_index != self._segment_index and self.state in (HEALTHY, DEGRADED):
            # Only reached when every segment was walked; keep the highest index.
            self._segment_index = last_index
            self._segment_path = self.segment_path(last_index)
        self.recovery_report = report
        return report

    def _add_terminating_newline(self, path: Path, raw: bytes) -> None:
        try:
            with _private_append(path) as handle:
                handle.write(b"\n")
                handle.flush()
        except OSError as exc:
            self._degrade(f"TERMINATOR_WRITE_FAILED:{exc.__class__.__name__}")

    def _quarantine_truncated_tail(self, path: Path, complete_bytes: bytes, raw: bytes,
                                   report: dict[str, Any]) -> None:
        """Preserve the original bytes, isolate the damaged tail, record the hole.

        Order section 三B: the raw bytes are kept (never deleted), the damaged tail is
        isolated, and an explicit recovery gap is recorded as data.
        """
        stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        quarantine = path.with_name(f"{path.name}.recovered-{stamp}.raw")
        try:
            tmp = quarantine.with_name(f".{quarantine.name}.tmp.{uuid.uuid4().hex}")
            tmp.write_bytes(raw)
            os.replace(tmp, quarantine)
        except OSError as exc:
            # Cannot isolate the tail safely -> do not touch the file.  Fail closed.
            self._corrupt(f"QUARANTINE_FAILED:{exc.__class__.__name__}")
            return
        try:
            tmp = path.with_name(f".{path.name}.tmp.{uuid.uuid4().hex}")
            tmp.write_bytes(complete_bytes)
            os.replace(tmp, path)
        except OSError as exc:
            self._corrupt(f"TRUNCATE_FAILED:{exc.__class__.__name__}")
            return

        tail_bytes = raw[len(complete_bytes):]
        self.pending_recovery_gap = recovery_gap_payload(
            segment=path.name,
            quarantined_from_byte=len(complete_bytes),
            quarantined_bytes=len(tail_bytes),
            quarantined_sha256=hashlib.sha256(tail_bytes).hexdigest(),
            quarantine_path=str(quarantine),
            reason="TRUNCATED_TAIL_QUARANTINED",
        )
        self._degrade("RECOVERED_TRUNCATED_TAIL")
        report["quarantined"] = {
            "segment": path.name,
            "from_byte": len(complete_bytes),
            "bytes": len(tail_bytes),
            "sha256": hashlib.sha256(tail_bytes).hexdigest(),
            "quarantine_path": str(quarantine),
        }

    # -- appends ----------------------------------------------------------------------

    def ensure_segment(self) -> None:
        """Make sure an open segment -- including its header record -- exists.

        Writer-side only.  The segment header participates in the same global hash chain, so
        it must be written *before* the next record's sequence and previous hash are
        allocated.  Writing it afterwards made the header consume a sequence the caller had
        already taken, which produced a duplicate ``seq`` and a false ``CORRUPT`` on reopen.
        """
        if self._segment_path is None:
            if self.state in (CORRUPT, UNAVAILABLE) or self.pending_recovery_gap is not None:
                raise JournalUnavailable(
                    f"audit journal not accepting records: {self.state} {self.reasons}")
            self._create_segment(1)

    def next_record(self, kind: str, payload: Mapping[str, Any],
                    *, at: Optional[str] = None) -> AuditRecord:
        """Allocate the next chained record, opening or rotating a segment when required.

        Writer-side only (it may create a file).  Never called by the hook.

        The rotation decision is based on ``max_record_bytes`` rather than on the record
        about to be written, so it cannot depend on a sequence number this call is about to
        mint.  The cost is up to ``max_record_bytes`` of unused room per segment.
        """
        if self.state in (CORRUPT, UNAVAILABLE) or self.pending_recovery_gap is not None:
            raise JournalUnavailable(
                f"audit journal not accepting records: {self.state} {self.reasons}")
        self.ensure_segment()
        if self._segment_bytes + self.max_record_bytes > self.segment_cap_bytes:
            allowed, deletions, why = self._retention_gate()
            if not allowed:
                self._degrade("RETENTION_BUDGET_EXHAUSTED:" + str(why.get("reason")))
                raise JournalUnavailable(
                    "rotation refused: no retention budget left and no segment is past the "
                    f"retention window ({why}); audit paused rather than growing without bound")
            self._rotate(deletions=deletions)
        return AuditRecord(
            seq=self.seq + 1,
            kind=str(kind),
            at=at or now_iso(),
            prev_entry_hash=self.head_hash,
            payload=dict(payload),
        )

    def commit(self, record: AuditRecord) -> None:
        """Append ``record``.  No rotation and no segment creation happen here.

        Raises ``JournalUnavailable`` when the journal is CORRUPT / UNAVAILABLE / paused or
        when the record does not continue the chain, and ``JournalWriteError`` when the
        write itself fails.
        """
        if self.state in (CORRUPT, UNAVAILABLE) or self.pending_recovery_gap is not None:
            raise JournalUnavailable(
                f"audit journal not accepting records: {self.state} {self.reasons}")
        if record.seq != self.seq + 1:
            raise JournalUnavailable(
                f"record seq {record.seq} does not continue head {self.seq}")
        if record.prev_entry_hash != self.head_hash:
            raise JournalUnavailable("record does not continue the chain head")

        data = (record.to_line() + "\n").encode("utf-8")
        if len(data) > self.max_record_bytes:
            raise JournalUnavailable(
                f"record of {len(data)} bytes exceeds cap {self.max_record_bytes}")

        path = self._segment_path
        if path is None:
            raise JournalUnavailable("no open segment")
        try:
            with _private_append(path) as handle:
                handle.write(data)
                handle.flush()
        except OSError as exc:
            raise JournalWriteError(
                f"audit append failed on {path.name}: {exc.__class__.__name__}: {exc}") from exc

        self.seq = record.seq
        self.head_hash = record.entry_hash
        self._segment_bytes += len(data)
        self.written_total += 1

    def commit_new(self, kind: str, payload: Mapping[str, Any],
                   *, at: Optional[str] = None) -> AuditRecord:
        record = self.next_record(kind, payload, at=at)
        self.commit(record)
        return record

    # -- rotation, retention ----------------------------------------------------------

    def _create_segment(self, index: int) -> None:
        path = self.segment_path(index)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.root, 0o700)
        with _private_append(path) as handle:
            handle.flush()
        self._segment_index = index
        self._segment_path = path
        self._segment_bytes = 0
        header = AuditRecord(
            seq=self.seq + 1,
            kind=KIND_SEGMENT_HEADER,
            at=now_iso(),
            prev_entry_hash=self.head_hash,
            payload=segment_header_payload(
                segment_index=index,
                previous_segment=(index - 1) if index > 1 else None,
                prev_entry_hash=self.head_hash,
            ),
        )
        data = (header.to_line() + "\n").encode("utf-8")
        try:
            with _private_append(path) as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
                self.fsync_count += 1
        except OSError as exc:
            raise JournalWriteError(
                f"segment header write failed on {path.name}: {exc.__class__.__name__}") from exc
        self.seq = header.seq
        self.head_hash = header.entry_hash
        self._segment_bytes = len(data)
        self.written_total += 1

    def _rotate(self, *, deletions: Optional[list[Path]] = None) -> None:
        """Close the current segment and open the next one.

        Order section 三C: rotation happens only here, only by the Audit Writer, using a
        fresh file (never a rewrite of a partially written one), and it must not silently
        drop history.  ``deletions`` are the oldest segments that :meth:`_retention_gate`
        has already proven to be past the retention window.
        """
        for path in (deletions or []):
            try:
                path.unlink()
                self.pruned_segments.append(path.name)
            except OSError as exc:
                self._degrade(f"PRUNE_FAILED:{path.name}:{exc.__class__.__name__}")
        self._create_segment(self._segment_index + 1)
        self.rotation_count += 1

    def _retention_gate(self) -> tuple[bool, list[Path], dict[str, Any]]:
        """Decide whether one more segment may be created.

        Returns ``(allowed, deletable_oldest_segments, explanation)``.  Rotation is allowed
        only when the resulting count can be brought back to ``max_segments`` by deleting
        segments that are genuinely past ``retention_seconds``.

        This is the bounded-disk guard.  Without it, a busy journal whose history has not yet
        aged out would keep creating segments forever -- the order section 六 stop condition
        "queue or audit writer can consume resources without bound".  When the gate refuses,
        the caller pauses and reports DEGRADED instead of deleting anything still inside the
        retention window (order section 三C, final paragraph).
        """
        segments = self.segments()
        overflow = len(segments) + 1 - self.max_segments
        if overflow <= 0:
            return True, [], {"overflow": 0}
        deletable: list[Path] = []
        now = time.time()
        for _index, path in segments[:overflow]:
            try:
                age = now - path.stat().st_mtime
            except OSError as exc:
                return False, [], {"reason": "STAT_FAILED", "segment": path.name,
                                   "detail": exc.__class__.__name__}
            if age < self.retention_seconds:
                return False, [], {
                    "reason": "SEGMENT_STILL_WITHIN_RETENTION",
                    "segment": path.name,
                    "age_s": round(age, 1),
                    "retention_seconds": self.retention_seconds,
                }
            deletable.append(path)
        return True, deletable, {"overflow": overflow,
                                 "deleting": [p.name for p in deletable]}

    def prune(self) -> dict[str, Any]:
        """Report-only view of what the retention gate would do right now."""
        allowed, deletable, why = self._retention_gate()
        segments = self.segments()
        return {
            "deleted": [],
            "kept": [path.name for _i, path in segments if path not in deletable],
            "would_delete": [p.name for p in deletable],
            "paused": not allowed,
            "reason": why.get("reason"),
        }

    # -- reads ------------------------------------------------------------------------

    def read_all(self) -> list[dict[str, Any]]:
        """Parse every segment in order.  Read-only: never repairs, never truncates."""
        out: list[dict[str, Any]] = []
        for _index, path in self.segments():
            try:
                raw = path.read_bytes()
            except OSError:
                continue
            last_newline = raw.rfind(b"\n")
            chunk = raw if last_newline == -1 else raw[: last_newline + 1]
            for line in chunk.decode("utf-8", errors="replace").split("\n"):
                if not line.strip():
                    continue
                try:
                    parsed = json.loads(line)
                except ValueError:
                    continue
                if isinstance(parsed, Mapping):
                    out.append(dict(parsed))
        return out

    def verify_integrity(self) -> bool:
        """Strict chain verification over what is on disk.

        Returns ``False`` for a corrupt journal, an unreadable segment, or a chain break.
        This is deliberately independent of ``read_all`` so a lenient reader cannot make a
        damaged file look complete (order section 三B).
        """
        expected_seq: Optional[int] = None
        head: Optional[str] = None
        seen_any = False
        for _index, path in self.segments():
            try:
                raw = path.read_bytes()
            except OSError:
                return False
            if raw == b"":
                continue
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                return False
            last_newline = raw.rfind(b"\n")
            chunk_text = text if last_newline == -1 else text[: last_newline + 1]
            tail_text = "" if last_newline == -1 else text[last_newline + 1:]
            pending: list[AuditRecord] = []
            for line in chunk_text.split("\n"):
                if line == "":
                    continue
                try:
                    pending.append(AuditRecord.from_line(line))
                except (ValueError, KeyError, TypeError):
                    return False
            if tail_text.strip():
                try:
                    pending.append(AuditRecord.from_line(tail_text))
                except (ValueError, KeyError, TypeError):
                    return False
            for record in pending:
                if expected_seq is None:
                    # Anchoring rule.  The oldest surviving record is either the true genesis
                    # of the chain, or the visible front of a chain whose earlier segments
                    # were removed by retention.  Every link after the anchor is still
                    # verified strictly, so a broken middle can never look complete.
                    if record.seq == 1 and record.prev_entry_hash == GENESIS:
                        expected_seq, head = 1, GENESIS
                    else:
                        expected_seq, head = record.seq, record.prev_entry_hash
                if record.seq != expected_seq or record.prev_entry_hash != head:
                    return False
                if not record.verify_self():
                    return False
                expected_seq += 1
                head = record.entry_hash
                seen_any = True
        if not seen_any:
            return self.state in (HEALTHY, DEGRADED)
        return True

    # -- reporting --------------------------------------------------------------------

    def stats(self) -> dict[str, Any]:
        return {
            "root": str(self.root),
            "base_name": self.base_name,
            "state": self.state,
            "reasons": list(self.reasons),
            "segments": len(self.segments()),
            "segment_index": self._segment_index,
            "segment_bytes": self._segment_bytes,
            "segment_cap_bytes": self.segment_cap_bytes,
            "max_segments": self.max_segments,
            "retention_seconds": self.retention_seconds,
            "seq": self.seq,
            "head_hash": self.head_hash,
            "written_total": self.written_total,
            "rotation_count": self.rotation_count,
            "fsync_count": self.fsync_count,
            "pruned_segments": list(self.pruned_segments),
            "pending_recovery_gap": self.pending_recovery_gap is not None,
            "accepting": self.accepting,
        }

    def close(self) -> None:
        """Durability boundary: fsync the current segment if it exists."""
        path = self._segment_path
        if path is None:
            return
        try:
            with _private_append(path) as handle:
                handle.flush()
                os.fsync(handle.fileno())
                self.fsync_count += 1
        except OSError as exc:
            self._degrade(f"CLOSE_FSYNC_FAILED:{exc.__class__.__name__}")
