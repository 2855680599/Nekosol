#!/usr/bin/env python3
"""Deterministic school-day facts for the Shiomi City World V1.

The module is a read-only calendar/timetable authority.  It exposes bounded
facts for explicit World perception and validates an explicit ATTEND_CLASS
affordance into the existing Life START candidate shape.  It never moves,
starts, completes, schedules, reminds, wakes, or writes any state.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from typing import Any, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


FEATURE_ENV = "CHIYO_WORLD_SCHOOL_DAY_ENABLED"
WORLD_ID = "shiomi_city"
WORLD_TIMEZONE = "Asia/Tokyo"
SCHEMA_VERSION = "world.school.day.v1"
PROJECTION_KIND = "shiomi_school_day"
ATTENDANCE_CANDIDATE_KIND = "world_school_class_attendance"
ATTENDANCE_SOURCE = "shiomi_school_day"
SCHOOL_ID = "shiomi_school"
SCHOOL_NAME = "汐见市学校"
SCHOOL_LOCATION_ID = "school"
CLASSROOM_ID = "school.classroom"
CLASSROOM_NAME = "教室"
MAX_PROJECTION_BYTES = 12_000
_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
_SUBJECTS = frozenset({"国语", "数学", "英语", "历史", "体育"})


class SchoolDayError(ValueError):
    """Raised for malformed bounded school-day input."""


@dataclass(frozen=True)
class _Period:
    period: str
    start_minute: int
    end_minute: int
    kind: str


# One central, editable timetable.  It intentionally has no lesson material,
# teacher, grade, homework, attendance, or performance fields.
TIMETABLE: tuple[_Period, ...] = (
    _Period("before_school", 7 * 60 + 30, 8 * 60, "boundary"),
    _Period("morning_class_1", 8 * 60, 9 * 60 + 30, "class"),
    _Period("morning_class_2", 9 * 60 + 30, 11 * 60, "class"),
    _Period("lunch", 11 * 60, 12 * 60, "lunch"),
    _Period("afternoon_class_1", 12 * 60, 13 * 60 + 30, "class"),
    _Period("afternoon_class_2", 13 * 60 + 30, 15 * 60, "class"),
    _Period("after_school", 15 * 60, 18 * 60, "boundary"),
)

# Weekday variation keeps the fixed subject set small while making the daily
# schedule a real date fact rather than a single hard-coded class forever.
_WEEKDAY_SUBJECTS: dict[int, tuple[str, str, str, str]] = {
    1: ("国语", "数学", "英语", "历史"),
    2: ("数学", "英语", "历史", "体育"),
    3: ("英语", "历史", "体育", "国语"),
    4: ("历史", "体育", "国语", "数学"),
    5: ("体育", "国语", "数学", "英语"),
}
_CLASS_PERIODS = tuple(item for item in TIMETABLE if item.kind == "class")
_PERIOD_BY_NAME = {item.period: item for item in TIMETABLE}


def _copy_json(value: Any) -> Any:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        return json.loads(encoded)
    except (TypeError, ValueError) as exc:
        raise SchoolDayError("value is not bounded JSON") from exc


def _environment(environment: Optional[Mapping[str, str]]) -> Mapping[str, str]:
    return os.environ if environment is None else environment


def _truthy(value: Any) -> bool:
    return str(value or "").strip().lower() in _TRUE_VALUES


def feature_enabled(environment: Optional[Mapping[str, str]] = None) -> bool:
    return _truthy(_environment(environment).get(FEATURE_ENV))


def _zone() -> ZoneInfo:
    try:
        return ZoneInfo(WORLD_TIMEZONE)
    except ZoneInfoNotFoundError as exc:  # pragma: no cover - platform data
        raise SchoolDayError("school timezone is unavailable") from exc


def _local(now: Optional[datetime]) -> datetime:
    instant = datetime.now(timezone.utc) if now is None else now
    if not isinstance(instant, datetime):
        raise SchoolDayError("now must be a datetime")
    if instant.tzinfo is None or instant.utcoffset() is None:
        instant = instant.replace(tzinfo=timezone.utc)
    return instant.astimezone(_zone())


def _as_date(value: date | datetime | str) -> date:
    if isinstance(value, datetime):
        return _local(value).date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value.strip())
        except ValueError as exc:
            raise SchoolDayError("local_date is invalid") from exc
    raise SchoolDayError("local_date is invalid")


def _closed_dates(closed_dates: Optional[Iterable[date | str]]) -> set[date]:
    if closed_dates is None:
        return set()
    result: set[date] = set()
    for item in closed_dates:
        result.add(_as_date(item))
    return result


def is_school_day(
    local_date: date | datetime | str,
    *,
    closed_dates: Optional[Iterable[date | str]] = None,
) -> bool:
    day = _as_date(local_date)
    return day.weekday() < 5 and day not in _closed_dates(closed_dates)


def _timestamp(day: date, minute: int) -> str:
    value = datetime.combine(day, time(minute // 60, minute % 60), tzinfo=_zone())
    return value.isoformat(timespec="seconds")


def _period_status(item: _Period, minute: int) -> str:
    if minute < item.start_minute:
        return "upcoming"
    if minute >= item.end_minute:
        return "ended"
    return "active"


def _subject_for(day: date, period: _Period) -> Optional[str]:
    if period.kind != "class":
        return None
    subjects = _WEEKDAY_SUBJECTS.get(day.isoweekday())
    if subjects is None:
        return None
    index = [item.period for item in _CLASS_PERIODS].index(period.period)
    return subjects[index]


def _period_fact(day: date, item: _Period, *, minute: Optional[int] = None) -> dict[str, Any]:
    current_minute = minute if minute is not None else item.start_minute
    return {
        "period": item.period,
        "kind": item.kind,
        "subject": _subject_for(day, item),
        "starts_at": _timestamp(day, item.start_minute),
        "ends_at": _timestamp(day, item.end_minute),
        "status": _period_status(item, current_minute),
        "provenance": {
            "kind": "configured_school_timetable",
            "source": "shiomi_school_day_v1",
            "factual": True,
        },
    }


def daily_timetable(
    local_date: date | datetime | str,
    *,
    closed_dates: Optional[Iterable[date | str]] = None,
) -> list[dict[str, Any]]:
    """Return the full deterministic timetable for direct factual callers."""

    day = _as_date(local_date)
    if not is_school_day(day, closed_dates=closed_dates):
        return []
    return [_period_fact(day, item) for item in TIMETABLE]


def class_sessions(
    local_date: date | datetime | str,
    *,
    minute: Optional[int] = None,
    closed_dates: Optional[Iterable[date | str]] = None,
) -> list[dict[str, Any]]:
    day = _as_date(local_date)
    if not is_school_day(day, closed_dates=closed_dates):
        return []
    selected_minute = minute
    result: list[dict[str, Any]] = []
    for item in _CLASS_PERIODS:
        subject = _subject_for(day, item)
        assert subject in _SUBJECTS
        fact = _period_fact(day, item, minute=selected_minute)
        result.append(
            {
                "session_id": f"{SCHOOL_ID}:{day.isoformat()}:{item.period}",
                "local_date": day.isoformat(),
                "period": item.period,
                "subject": subject,
                "starts_at": fact["starts_at"],
                "ends_at": fact["ends_at"],
                "room": {
                    "location_id": SCHOOL_LOCATION_ID,
                    "subspace_id": CLASSROOM_ID,
                    "name": CLASSROOM_NAME,
                },
                "status": fact["status"],
                "provenance": fact["provenance"],
            }
        )
    return _copy_json(result)


def active_class_session(
    now: Optional[datetime] = None,
    *,
    closed_dates: Optional[Iterable[date | str]] = None,
) -> Optional[dict[str, Any]]:
    local = _local(now)
    if not is_school_day(local.date(), closed_dates=closed_dates):
        return None
    sessions = class_sessions(
        local.date(),
        minute=local.hour * 60 + local.minute,
        closed_dates=closed_dates,
    )
    return _copy_json(next((item for item in sessions if item["status"] == "active"), None))


def _bounded_location(value: Any) -> str:
    if not isinstance(value, str):
        raise SchoolDayError("current_location_id must be a string")
    result = value.strip()
    if result not in {SCHOOL_LOCATION_ID, CLASSROOM_ID}:
        raise SchoolDayError("current_location_id is not school-valid")
    return result


def validate_attend_class(
    *,
    current_location_id: Any,
    now: Optional[datetime] = None,
    environment: Optional[Mapping[str, str]] = None,
) -> dict[str, Any]:
    """Validate an explicit ATTEND_CLASS selection without executing it."""

    if not feature_enabled(environment):
        return {"status": "rejected", "reason_code": "SCHOOL_DAY_FEATURE_DISABLED"}
    try:
        location = _bounded_location(current_location_id)
    except SchoolDayError:
        return {"status": "rejected", "reason_code": "INVALID_SCHOOL_LOCATION"}
    session = active_class_session(now)
    if session is None:
        return {
            "status": "rejected",
            "reason_code": "NO_ACTIVE_CLASS_SESSION",
            "current_location_id": location,
        }
    return {
        "status": "ready",
        "reason_code": "ATTEND_CLASS_READY",
        "current_location_id": location,
        "classroom": {
            "parent_location_id": SCHOOL_LOCATION_ID,
            "subspace_id": CLASSROOM_ID,
            "relation": "child",
            "name": CLASSROOM_NAME,
        },
        "session": _copy_json(session),
    }


def build_attend_class_candidate(
    *,
    candidate_id: Any,
    session: Mapping[str, Any],
) -> dict[str, Any]:
    if not isinstance(candidate_id, str) or not candidate_id.strip():
        raise SchoolDayError("candidate_id is invalid")
    if not isinstance(session, Mapping):
        raise SchoolDayError("session is invalid")
    subject = session.get("subject")
    starts_at = session.get("starts_at")
    ends_at = session.get("ends_at")
    session_id = session.get("session_id")
    if not all(isinstance(value, str) and value.strip() for value in (subject, starts_at, ends_at, session_id)):
        raise SchoolDayError("session facts are incomplete")
    return {
        "candidate_id": candidate_id.strip(),
        "kind": ATTENDANCE_CANDIDATE_KIND,
        "title": f"上{subject}课",
        "source": ATTENDANCE_SOURCE,
        "expected_effort": "medium",
        "interruptibility": "normal",
        "hard_requirement": {
            "kind": "active_school_class_at_valid_school_location",
            "satisfied": True,
            "detail": f"{session_id} is active in the school classroom",
        },
        "deadline": None,
        "continuation_of": None,
        "estimated_duration": None,
        "schedule_window": {"starts_at": starts_at, "ends_at": ends_at},
    }


def observe_school_day(
    *,
    now: Optional[datetime] = None,
    current_location_id: Any = None,
    environment: Optional[Mapping[str, str]] = None,
    closed_dates: Optional[Iterable[date | str]] = None,
) -> dict[str, Any]:
    """Return bounded school facts; never include the full schedule here."""

    if not feature_enabled(environment):
        return {
            "kind": PROJECTION_KIND,
            "schema_version": SCHEMA_VERSION,
            "status": "disabled",
            "reason_code": "FEATURE_DISABLED",
        }
    try:
        local = _local(now)
        day = local.date()
        school_day = is_school_day(day, closed_dates=closed_dates)
        minute = local.hour * 60 + local.minute
        location = current_location_id.strip() if isinstance(current_location_id, str) else "unknown"
    except SchoolDayError:
        return {
            "kind": PROJECTION_KIND,
            "schema_version": SCHEMA_VERSION,
            "status": "unavailable",
            "reason_code": "INVALID_SCHOOL_TIME",
        }

    current_period = None
    current_period_fact = None
    next_class = None
    current_class = None
    lunch = None
    after_school = None
    if school_day:
        current_period_item = next(
            (item for item in TIMETABLE if item.start_minute <= minute < item.end_minute),
            None,
        )
        if current_period_item is not None:
            current_period = current_period_item.period
            current_period_fact = _period_fact(day, current_period_item, minute=minute)
        else:
            current_period = "outside_school_hours"
        sessions = class_sessions(day, minute=minute, closed_dates=closed_dates)
        current_class = next((item for item in sessions if item["status"] == "active"), None)
        next_class = next((item for item in sessions if item["status"] == "upcoming"), None)
        lunch_item = _PERIOD_BY_NAME["lunch"]
        lunch = _period_fact(day, lunch_item, minute=minute)
        after_school_item = _PERIOD_BY_NAME["after_school"]
        after_school = _period_fact(day, after_school_item, minute=minute)

    classroom_available = bool(
        school_day
        and location in {SCHOOL_LOCATION_ID, CLASSROOM_ID}
        and 8 * 60 <= minute < 18 * 60
    )
    result = {
        "kind": PROJECTION_KIND,
        "schema_version": SCHEMA_VERSION,
        "status": "available",
        "world_id": WORLD_ID,
        "school_id": SCHOOL_ID,
        "school_name": SCHOOL_NAME,
        "location_id": SCHOOL_LOCATION_ID,
        "timezone": WORLD_TIMEZONE,
        "local_date": day.isoformat(),
        "weekday": day.isoweekday(),
        "school_day": school_day,
        "day_type": "school_day" if school_day else "non_school_day",
        "current_school_period": "non_school_day" if not school_day else current_period,
        "current_period": current_period_fact,
        "current_class": _copy_json(current_class),
        "next_class": _copy_json(next_class),
        "lunch": _copy_json(lunch),
        "after_school": _copy_json(after_school),
        "classroom": {
            "parent_location_id": SCHOOL_LOCATION_ID,
            "subspace_id": CLASSROOM_ID,
            "relation": "child",
            "name": CLASSROOM_NAME,
            "available": classroom_available,
        },
    }
    encoded = json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    if len(encoded) > MAX_PROJECTION_BYTES:
        return {
            "kind": PROJECTION_KIND,
            "schema_version": SCHEMA_VERSION,
            "status": "unavailable",
            "reason_code": "PROJECTION_BOUNDS_EXCEEDED",
        }
    return _copy_json(result)


__all__ = [
    "ATTENDANCE_CANDIDATE_KIND",
    "ATTENDANCE_SOURCE",
    "CLASSROOM_ID",
    "FEATURE_ENV",
    "PROJECTION_KIND",
    "SCHOOL_ID",
    "SCHOOL_LOCATION_ID",
    "SCHEMA_VERSION",
    "TIMETABLE",
    "active_class_session",
    "build_attend_class_candidate",
    "class_sessions",
    "daily_timetable",
    "feature_enabled",
    "is_school_day",
    "observe_school_day",
    "validate_attend_class",
]
