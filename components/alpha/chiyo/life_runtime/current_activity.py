#!/usr/bin/env python3
"""Persistent current-activity contract for Life Loop Phase 2-A.

This module is deliberately an activity fact store, not a life-loop scheduler.
Only explicit lifecycle transitions change the activity.  The JSON state is the
single durable source of truth; activity.txt remains a compatibility view.
"""

from __future__ import annotations

import argparse
import errno
import hashlib
import json
import os
import re
import tempfile
import time
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator, Optional
from zoneinfo import ZoneInfo

try:
    import fcntl
except ImportError:  # pragma: no cover - production target is POSIX
    fcntl = None

BJT = ZoneInfo("Asia/Shanghai")
XINXI_DIR = Path(os.environ.get("XINXI_DIR", Path.home() / ".hermes" / "xinxi"))
STATE_PATH = XINXI_DIR / "current_activity.json"
VIEW_PATH = XINXI_DIR / "activity.txt"
LOCK_PATH = XINXI_DIR / ".current_activity.lock"
LIFECYCLE_LOG_PATH = Path(
    os.environ.get("ACTIVITY_LIFECYCLE_LOG_PATH", XINXI_DIR / "activity_lifecycle.jsonl")
)
VALID_STATUSES = {"active", "paused", "completed", "abandoned", "idle"}
SCHEMA_VERSION = 2
LOCK_TIMEOUT_SECONDS = 10.0
RUNTIME_ID = uuid.uuid4().hex
_MACHINE_REASON = re.compile(r"^[a-z0-9_]+$")
_EXPECTED_UNSET = object()


class ActivityError(RuntimeError):
    """Base class for safe activity storage/transition failures."""


class ActivityTransitionError(ActivityError):
    """Raised when a requested lifecycle transition is not allowed."""


class CompletionEvidenceError(ActivityError):
    """Raised when COMPLETE lacks the required structured evidence."""


class ActivityLockError(ActivityError):
    """Raised when the cross-process lock cannot be acquired safely."""


def now() -> datetime:
    override = os.environ.get("CURRENT_ACTIVITY_NOW")
    if override:
        return datetime.fromisoformat(override).astimezone(BJT)
    return datetime.now(BJT)


def _iso(value: datetime) -> str:
    return value.astimezone(BJT).isoformat(timespec="seconds")


def _parse(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=BJT)
        return parsed.astimezone(BJT)
    except (TypeError, ValueError):
        return None


def _reason(value: str, default: str = "manual") -> str:
    text = str(value or default).strip().lower()
    if not _MACHINE_REASON.fullmatch(text):
        raise ValueError(f"invalid machine transition reason: {value!r}")
    return text


def _planned_duration_seconds(value: Any) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError("planned_duration_seconds must be a positive integer")
    return value


def _compact_value(value: Any, limit: int = 500) -> Any:
    if isinstance(value, str):
        return value[:limit]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    try:
        encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        encoded = str(value)
    return encoded[:limit]


def _normalize_evidence(evidence: Any) -> dict[str, Any]:
    if not isinstance(evidence, dict):
        raise CompletionEvidenceError(
            "completion requires structured evidence: type/source/timestamp/result/reference"
        )
    required = ("type", "source", "timestamp", "result", "reference")
    missing = [key for key in required if evidence.get(key) in (None, "")]
    if missing:
        raise CompletionEvidenceError("completion evidence missing: " + ",".join(missing))
    timestamp = _parse(str(evidence["timestamp"]))
    if timestamp is None:
        raise CompletionEvidenceError("completion evidence timestamp must be valid ISO time")
    return {
        "type": str(evidence["type"])[:80],
        "source": str(evidence["source"])[:80],
        "timestamp": _iso(timestamp),
        "result": _compact_value(evidence["result"]),
        "reference": _compact_value(evidence["reference"], 240),
    }


def _legacy_activity_id(data: dict[str, Any]) -> str:
    raw = "|".join(str(data.get(key) or "") for key in ("activity", "started_at", "source"))
    return "legacy-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]


def _idle(note: str = "", at: Optional[datetime] = None) -> dict[str, Any]:
    stamp = _iso(at or now())
    return {
        "schema_version": SCHEMA_VERSION,
        "activity_id": None,
        "activity": "",
        "kind": "",
        "goal": "",
        "status": "idle",
        "started_at": None,
        "updated_at": stamp,
        "paused_at": None,
        "completed_at": None,
        "source": "system",
        "created_by": "system",
        "interruptibility": "normal",
        "note": note,
        "active_seconds": 0,
        "last_resumed_at": None,
        "last_transition": "INIT",
        "last_transition_reason": "initial_state",
        "last_transition_detail": note,
        "completion_criteria": {},
        "last_evidence": None,
        "recovery_state": "none",
        "last_runtime_seen_at": None,
        "runtime_id": RUNTIME_ID,
    }


def _normalize_state(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict):
        return _idle("current_activity.json 不是对象")
    state = _idle()
    state.update(data)
    if "runtime_id" not in data:
        state["runtime_id"] = "legacy"
    if state.get("status") not in VALID_STATUSES:
        state["status"] = "idle"
    state["schema_version"] = SCHEMA_VERSION
    try:
        state["active_seconds"] = max(0.0, float(state.get("active_seconds") or 0))
    except (TypeError, ValueError):
        state["active_seconds"] = 0.0
    if "planned_duration_seconds" in state:
        try:
            planned_duration = _planned_duration_seconds(state.get("planned_duration_seconds"))
        except ValueError:
            state.pop("planned_duration_seconds", None)
        else:
            if planned_duration is None:
                state.pop("planned_duration_seconds", None)
            else:
                state["planned_duration_seconds"] = planned_duration
    status = state["status"]
    if status != "idle" and not state.get("activity_id"):
        state["activity_id"] = _legacy_activity_id(data)
    if status != "idle":
        state["kind"] = str(state.get("kind") or "legacy")
        state["source"] = str(state.get("source") or "self")
        state["created_by"] = str(state.get("created_by") or state["source"])
        if status == "paused" and not state.get("paused_at"):
            state["paused_at"] = state.get("updated_at")
    if not isinstance(state.get("completion_criteria"), dict):
        state["completion_criteria"] = {}
    if state.get("last_evidence") is not None and not isinstance(state.get("last_evidence"), dict):
        state["last_evidence"] = None
    if not state.get("last_transition"):
        state["last_transition"] = "LEGACY_IMPORT"
    if not state.get("last_transition_reason"):
        state["last_transition_reason"] = "schema_migration"
    if not state.get("updated_at"):
        state["updated_at"] = _iso(now())
    if status in {"active", "paused"} and state.get("runtime_id") != RUNTIME_ID:
        state["recovery_state"] = "needs_review"
    state.setdefault("recovery_state", "none")
    state.setdefault("last_runtime_seen_at", None)
    state["runtime_id"] = state.get("runtime_id") or "legacy"
    return state


def _read_state_unlocked() -> tuple[dict[str, Any], bool]:
    if not STATE_PATH.exists():
        return _idle(), True
    try:
        with STATE_PATH.open(encoding="utf-8") as handle:
            return _normalize_state(json.load(handle)), True
    except (OSError, ValueError, TypeError) as exc:
        return _idle(f"current_activity.json 无法读取: {type(exc).__name__}"), False


@contextmanager
def _activity_lock() -> Iterator[None]:
    """Acquire a bounded POSIX lock; a dead process cannot leave it held."""
    if fcntl is None:
        raise ActivityLockError("cross-process activity lock unavailable")
    XINXI_DIR.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + LOCK_TIMEOUT_SECONDS
    with LOCK_PATH.open("a+") as handle:
        while True:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as exc:
                if exc.errno not in (errno.EACCES, errno.EAGAIN):
                    raise ActivityLockError(f"activity lock failed: {exc}") from exc
                if time.monotonic() >= deadline:
                    raise ActivityLockError("activity lock timeout; no state was written")
                time.sleep(0.05)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def load_state() -> dict[str, Any]:
    with _activity_lock():
        state, _ = _read_state_unlocked()
        return state


def _load_for_mutation_unlocked() -> dict[str, Any]:
    state, valid = _read_state_unlocked()
    if not valid:
        raise ActivityError("current_activity.json unreadable; refusing to overwrite it")
    return state


def _assert_expected_target_unlocked(
    state: dict[str, Any],
    *,
    expected_activity_id: Any = _EXPECTED_UNSET,
    expected_status: Optional[str] = None,
    expected_interruptibility: Optional[str] = None,
) -> None:
    """Reject a stale target while the lifecycle lock is still held."""

    if expected_activity_id is not _EXPECTED_UNSET:
        if state.get("activity_id") != expected_activity_id:
            raise ActivityTransitionError("stale_activity_target")
    if expected_status is not None and state.get("status") != expected_status:
        raise ActivityTransitionError("stale_activity_target")
    if (
        expected_interruptibility is not None
        and state.get("interruptibility") != expected_interruptibility
    ):
        raise ActivityTransitionError("stale_activity_target")


def _active_seconds(state: dict[str, Any], at: Optional[datetime] = None) -> float:
    seconds = float(state.get("active_seconds") or 0)
    if state.get("status") == "active":
        resumed = _parse(state.get("last_resumed_at")) or _parse(state.get("started_at"))
        if resumed:
            seconds += max(0.0, ((at or now()) - resumed).total_seconds())
    return seconds


def duration_seconds(state: Optional[dict[str, Any]] = None, at: Optional[datetime] = None) -> int:
    return int(round(_active_seconds(state or load_state(), at)))


def _with_display(state: dict[str, Any]) -> dict[str, Any]:
    result = dict(state)
    seconds = duration_seconds(result)
    result["duration_seconds"] = seconds
    result["duration_minutes"] = round(seconds / 60, 1)
    return result


def _compat_text(state: dict[str, Any]) -> str:
    if state.get("status") in {"active", "paused"} and state.get("activity"):
        return str(state["activity"]).strip()
    return ""


def _append_log_unlocked(
    state: dict[str, Any],
    *,
    transition: str,
    before_status: str,
    reason: str,
    detail: str,
    evidence: Optional[dict[str, Any]],
    source: str,
) -> None:
    LIFECYCLE_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "activity_id": state.get("activity_id"),
        "timestamp": _iso(now()),
        "transition": transition,
        "before_status": before_status,
        "after_status": state.get("status"),
        "reason": reason,
        "detail": str(detail or "")[:500],
        "evidence": evidence,
        "source": str(source or state.get("source") or "system"),
    }
    with LIFECYCLE_LOG_PATH.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, ensure_ascii=False, separators=(",", ":")) + "\n")
        handle.flush()
        os.fsync(handle.fileno())



_PRODUCTION_HOMES = (
    Path("./data/world").resolve(),
    Path("./data/persona").resolve(),
    # Retain denial-only compatibility guards; these paths are never read.
    Path("/root/.hermes-world-hk"),
    Path("/root/.hermes-stock"),
    Path("/root/.hermes"),
)


def _assert_legacy_v2_not_production_canonical(target_dir: Path) -> None:
    resolved = Path(target_dir).expanduser().resolve()
    for prod in _PRODUCTION_HOMES:
        prod_resolved = prod.resolve()
        if resolved == prod_resolved or prod_resolved in resolved.parents:
            raise ActivityError(
                f"legacy_v2_production_write_denied: refusing legacy v2 write inside {prod_resolved}; "
                "use activity_continuity.ActivityCommandService"
            )


def _persist_unlocked(
    state: dict[str, Any],
    *,
    transition: Optional[str] = None,
    before_status: Optional[str] = None,
    reason: str = "manual",
    detail: str = "",
    evidence: Optional[dict[str, Any]] = None,
    source: str = "",
) -> dict[str, Any]:
    _assert_legacy_v2_not_production_canonical(STATE_PATH)
    state = _normalize_state(state)
    if transition:
        state["last_transition"] = transition
        state["last_transition_reason"] = reason
        state["last_transition_detail"] = str(detail or "")[:500]
    state["last_runtime_seen_at"] = _iso(now())
    state["runtime_id"] = RUNTIME_ID
    state["schema_version"] = SCHEMA_VERSION
    encoded = json.dumps(state, ensure_ascii=False, indent=2) + "\n"
    XINXI_DIR.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=".current_activity.", dir=XINXI_DIR)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, STATE_PATH)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)
    VIEW_PATH.write_text(_compat_text(state), encoding="utf-8")
    if transition:
        _append_log_unlocked(
            state,
            transition=transition,
            before_status=before_status or "unknown",
            reason=reason,
            detail=detail,
            evidence=evidence,
            source=source,
        )
    return _with_display(state)


def show() -> dict[str, Any]:
    return _with_display(load_state())


def start(
    activity: str,
    *,
    source: str = "self",
    interruptibility: str = "normal",
    note: str = "",
    at: Optional[datetime] = None,
    kind: str = "",
    goal: str = "",
    completion_criteria: Optional[dict[str, Any]] = None,
    created_by: str = "",
    reason: str = "manual",
    detail: str = "",
    transition_source: str = "",
    planned_duration_seconds: Optional[int] = None,
) -> dict[str, Any]:
    if not activity or not activity.strip():
        raise ValueError("activity cannot be empty")
    if interruptibility not in {"normal", "high", "never"}:
        raise ValueError("interruptibility must be normal, high, or never")
    reason = _reason(reason)
    transition_source = str(transition_source or source or "self")[:80]
    planned_duration = _planned_duration_seconds(planned_duration_seconds)
    with _activity_lock():
        previous = _load_for_mutation_unlocked()
        before = previous.get("status", "idle")
        if before != "idle":
            raise ActivityTransitionError(
                f"cannot START from {before}; complete or abandon the current activity first"
            )
        stamp = _iso(at or now())
        state = _idle(at=at or now())
        state.update({
            "activity_id": uuid.uuid4().hex,
            "activity": activity.strip(),
            "kind": str(kind or "")[:80],
            "goal": str(goal or "")[:500],
            "status": "active",
            "started_at": stamp,
            "updated_at": stamp,
            "paused_at": None,
            "completed_at": None,
            "source": str(source or "self")[:80],
            "created_by": str(created_by or source or "self")[:80],
            "interruptibility": interruptibility,
            "note": str(note or "")[:500],
            "active_seconds": 0,
            "last_resumed_at": stamp,
            "last_transition": "START",
            "last_transition_reason": reason,
            "last_transition_detail": str(detail or note or "")[:500],
            "completion_criteria": completion_criteria if isinstance(completion_criteria, dict) else {},
            "last_evidence": None,
            "recovery_state": "none",
        })
        if planned_duration is not None:
            state["planned_duration_seconds"] = planned_duration
        return _persist_unlocked(
            state,
            transition="START",
            before_status=before,
            reason=reason,
            detail=detail or note,
            source=transition_source,
        )


def pause(
    note: str = "",
    *,
    reason: str = "manual",
    source: str = "manual",
    expected_activity_id: Optional[str] = None,
    expected_status: Optional[str] = None,
    expected_interruptibility: Optional[str] = None,
) -> dict[str, Any]:
    reason = _reason(reason)
    with _activity_lock():
        state = _load_for_mutation_unlocked()
        _assert_expected_target_unlocked(
            state,
            expected_activity_id=expected_activity_id
            if expected_activity_id is not None else _EXPECTED_UNSET,
            expected_status=expected_status,
            expected_interruptibility=expected_interruptibility,
        )
        before = state.get("status", "idle")
        if before != "active":
            return _with_display(state)
        stamp = _iso(now())
        state["active_seconds"] = _active_seconds(state)
        state["last_resumed_at"] = None
        state["paused_at"] = stamp
        state["status"] = "paused"
        state["updated_at"] = stamp
        if note:
            state["note"] = str(note)[:500]
        return _persist_unlocked(
            state,
            transition="PAUSE",
            before_status=before,
            reason=reason,
            detail=note,
            source=source,
        )


def resume(
    *,
    reason: str = "manual",
    source: str = "manual",
    expected_activity_id: Optional[str] = None,
    expected_status: Optional[str] = None,
) -> dict[str, Any]:
    reason = _reason(reason)
    with _activity_lock():
        state = _load_for_mutation_unlocked()
        _assert_expected_target_unlocked(
            state,
            expected_activity_id=expected_activity_id
            if expected_activity_id is not None else _EXPECTED_UNSET,
            expected_status=expected_status,
        )
        before = state.get("status", "idle")
        if before != "paused":
            return _with_display(state)
        stamp = _iso(now())
        state["status"] = "active"
        state["last_resumed_at"] = stamp
        state["updated_at"] = stamp
        return _persist_unlocked(
            state,
            transition="RESUME",
            before_status=before,
            reason=reason,
            source=source,
        )


def finish(
    status: str,
    note: str = "",
    *,
    evidence: Optional[dict[str, Any]] = None,
    reason: str = "manual",
    source: str = "manual",
    expected_activity_id: Optional[str] = None,
    expected_status: Optional[str] = None,
    expected_evidence: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    if status not in {"completed", "abandoned"}:
        raise ValueError("finish status must be completed or abandoned")
    reason = _reason(reason)
    normalized_evidence = None
    expected_normalized_evidence = None
    if status == "completed":
        normalized_evidence = _normalize_evidence(evidence)
        if expected_evidence is not None:
            expected_normalized_evidence = _normalize_evidence(expected_evidence)
    with _activity_lock():
        state = _load_for_mutation_unlocked()
        _assert_expected_target_unlocked(
            state,
            expected_activity_id=expected_activity_id
            if expected_activity_id is not None else _EXPECTED_UNSET,
            expected_status=expected_status,
        )
        if (
            status == "completed"
            and expected_normalized_evidence is not None
            and state.get("last_evidence") != expected_normalized_evidence
        ):
            raise ActivityTransitionError("stale_completion_evidence")
        before = state.get("status", "idle")
        if before not in {"active", "paused"}:
            raise ActivityTransitionError(
                f"cannot {status.upper()} from {before}; only active or paused may terminate"
            )
        stamp = _iso(now())
        if before == "active":
            state["active_seconds"] = _active_seconds(state)
        state["last_resumed_at"] = None
        state["status"] = status
        state["updated_at"] = stamp
        state["completed_at"] = stamp if status == "completed" else None
        if note:
            state["note"] = str(note)[:500]
        state["last_evidence"] = normalized_evidence
        transition = "COMPLETE" if status == "completed" else "ABANDON"
        return _persist_unlocked(
            state,
            transition=transition,
            before_status=before,
            reason=reason,
            detail=note,
            evidence=normalized_evidence,
            source=source,
        )


def idle(note: str = "") -> dict[str, Any]:
    with _activity_lock():
        state = _load_for_mutation_unlocked()
        before = state.get("status", "idle")
        if before == "idle":
            return _with_display(state)
        if before not in {"completed", "abandoned"}:
            raise ActivityTransitionError(
                f"cannot reset {before} directly to idle; complete or abandon first"
            )
        return _persist_unlocked(
            _idle(note),
            transition="IDLE_CLEANUP",
            before_status=before,
            reason="terminal_cleanup",
            detail=note,
            source="system",
        )


def interrupt(note: str = "") -> dict[str, Any]:
    """Pause an interruptible activity and preserve it for later review."""
    with _activity_lock():
        state = _load_for_mutation_unlocked()
        before = state.get("status", "idle")
        if before != "active":
            return _with_display(state)
        if state.get("interruptibility") == "never":
            return _with_display(state)
        stamp = _iso(now())
        state["active_seconds"] = _active_seconds(state)
        state["last_resumed_at"] = None
        state["paused_at"] = stamp
        state["status"] = "paused"
        state["updated_at"] = stamp
        if note:
            state["note"] = str(note)[:500]
        return _persist_unlocked(
            state,
            transition="PAUSE",
            before_status=before,
            reason="owner_urgent_interrupt",
            detail=note,
            source="decision_gate",
        )


def sync_for_tick() -> dict[str, Any]:
    """Keep active/paused state; terminal states become idle on the next tick."""
    with _activity_lock():
        state = _load_for_mutation_unlocked()
        if state.get("status") in {"completed", "abandoned"}:
            before = state["status"]
            return _persist_unlocked(
                _idle(f"上一活动已{before}"),
                transition="IDLE_CLEANUP",
                before_status=before,
                reason="terminal_cleanup",
                detail=f"上一活动已{before}",
                source="xinxi_tick",
            )
        return _persist_unlocked(state)


def _manual_evidence(activity_id: Optional[str]) -> dict[str, Any]:
    stamp = _iso(now())
    return {
        "type": "explicit_manual_confirmation",
        "source": "cli",
        "timestamp": stamp,
        "result": "manual completion confirmation",
        "reference": activity_id or "cli",
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="manage xinxi current activity")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("show")
    start_parser = sub.add_parser("start")
    start_parser.add_argument("activity")
    start_parser.add_argument("--source", default="self")
    start_parser.add_argument("--created-by", default="")
    start_parser.add_argument("--kind", default="")
    start_parser.add_argument("--goal", default="")
    start_parser.add_argument("--interruptibility", choices=("normal", "high", "never"), default="normal")
    start_parser.add_argument("--note", default="")
    start_parser.add_argument("--at", default=None, help="ISO timestamp, useful for replay/tests")
    for command in ("pause", "complete", "abandon", "idle"):
        p = sub.add_parser(command)
        p.add_argument("--note", default="")
    sub.add_parser("resume")
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "show":
        result = show()
    elif args.command == "start":
        result = start(
            args.activity,
            source=args.source,
            created_by=args.created_by,
            kind=args.kind,
            goal=args.goal,
            interruptibility=args.interruptibility,
            note=args.note,
            at=_parse(args.at),
        )
    elif args.command == "pause":
        result = pause(args.note)
    elif args.command == "resume":
        result = resume()
    elif args.command == "complete":
        current = show()
        result = finish(
            "completed",
            args.note,
            evidence=_manual_evidence(current.get("activity_id")),
            reason="explicit_manual_request",
            source="cli",
        )
    elif args.command == "abandon":
        result = finish("abandoned", args.note, reason="manual", source="cli")
    else:
        result = idle(args.note)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
