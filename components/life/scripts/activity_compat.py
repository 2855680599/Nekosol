"""Behavior-preserving helper compatibility module for Activity runtime."""
from __future__ import annotations
import json, os
from datetime import datetime
from typing import Any, Optional
from zoneinfo import ZoneInfo
BJT = ZoneInfo("Asia/Shanghai")

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
