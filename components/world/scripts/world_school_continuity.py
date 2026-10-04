#!/usr/bin/env python3
"""Durable, lazy school-life continuity for the Shiomi City World.

The existing ``world_school_day`` module remains the only timetable authority.
This module records bounded observations of arrival/departure and finalized
class attendance. It is not an action, scheduler, messaging, or scoring
system: a legal observation may realize a fact, but it never chooses or
starts an activity and never writes World State or Timeline.
"""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Iterable, Mapping
from contextlib import contextmanager
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

try:
    import fcntl
except ImportError:  # pragma: no cover - production is POSIX
    fcntl = None

import world_school_day as school_day
import world_school_events as school_events
import world_travel_delivery as travel


FEATURE_ENV = "CHIYO_WORLD_SCHOOL_LIFE_CONTINUITY_ENABLED"
WORLD_ID = school_day.WORLD_ID
WORLD_TIMEZONE = school_day.WORLD_TIMEZONE
SCHEMA_VERSION = "world.school.life.continuity.v1"
PROJECTION_KIND = "shiomi_school_life_continuity"
STATE_FILENAME = "school_life_continuity.json"
SCHEDULE_REVISION = school_day.SCHEMA_VERSION
SCHOOL_ID = school_day.SCHOOL_ID
SCHOOL_LOCATION_ID = school_day.SCHOOL_LOCATION_ID
CLASSROOM_ID = school_day.CLASSROOM_ID

PRESENCE_NOT_ARRIVED = "NOT_ARRIVED"
PRESENCE_PRESENT = "PRESENT"
PRESENCE_LEFT = "LEFT"
PRESENCE_UNKNOWN = "UNKNOWN"
PRESENCE_TRAVEL_PENDING = "TRAVEL_PENDING"
ATTENDANCE_PRESENT = "PRESENT"
ATTENDANCE_LATE = "LATE"
ATTENDANCE_ABSENT = "ABSENT"
ATTENDANCE_UNKNOWN = "UNKNOWN"

MAX_SCHOOL_DAYS = 31
MAX_PRESENCE_SEGMENTS = 32
MAX_PROJECTION_BYTES = 12_000
MAX_STATE_BYTES = 128_000
MAX_TEXT_LENGTH = 240
_ATTENDANCE_VALUES = frozenset(
    {ATTENDANCE_PRESENT, ATTENDANCE_LATE, ATTENDANCE_ABSENT, ATTENDANCE_UNKNOWN}
)
_STATE_KEYS = frozenset(
    {"schema_version", "world_id", "revision", "continuity_started_at", "school_days"}
)
_DAY_KEYS = frozenset(
    {
        "school_day_id",
        "local_date",
        "weekday",
        "schedule_revision",
        "status",
        "first_arrival_at",
        "last_departure_at",
        "last_location_id",
        "last_observed_at",
        "presence_segments",
        "periods",
        "created_at",
        "updated_at",
        "provenance",
    }
)
_SEGMENT_KEYS = frozenset({"arrival_at", "departure_at"})
_PERIOD_KEYS = frozenset(
    {"period_id", "attendance", "arrival_at", "departure_at", "provenance"}
)
_CLASS_PERIOD_IDS = frozenset(
    item.period for item in school_day.TIMETABLE if item.kind == "class"
)
_PRE_CONTINUITY_UNKNOWN_BASIS = "before_continuity_started"


class SchoolContinuityError(ValueError):
    """Raised when the bounded school-continuity contract is invalid."""


def _copy_json(value: Any, *, maximum: int = MAX_STATE_BYTES) -> Any:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        if len(encoded.encode("utf-8")) > maximum:
            raise SchoolContinuityError("value exceeds bounded JSON size")
        return json.loads(encoded)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise SchoolContinuityError("value is not bounded JSON") from exc


def _environment(environment: Optional[Mapping[str, str]]) -> Mapping[str, str]:
    return os.environ if environment is None else environment


def _truthy(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def feature_enabled(environment: Optional[Mapping[str, str]] = None) -> bool:
    return _truthy(_environment(environment).get(FEATURE_ENV))


def _zone() -> ZoneInfo:
    try:
        return ZoneInfo(WORLD_TIMEZONE)
    except ZoneInfoNotFoundError as exc:  # pragma: no cover - platform data
        raise SchoolContinuityError("school timezone is unavailable") from exc


def _home(hermes_home: Optional[Path | str] = None) -> Path:
    raw = hermes_home if hermes_home is not None else os.environ.get("HERMES_HOME")
    path = Path(raw).expanduser() if raw else Path.home() / ".hermes"
    if not path.is_absolute():
        raise SchoolContinuityError("hermes_home must be absolute")
    return path


def state_path(hermes_home: Optional[Path | str] = None) -> Path:
    return _home(hermes_home) / "data" / "world" / STATE_FILENAME


def _text(value: Any, field: str, maximum: int = MAX_TEXT_LENGTH) -> str:
    if not isinstance(value, str):
        raise SchoolContinuityError(f"{field} must be a string")
    result = value.strip()
    if not result or len(result) > maximum or any(char in result for char in "\r\n\x00"):
        raise SchoolContinuityError(f"{field} is invalid")
    return result


def _optional_text(value: Any, field: str, maximum: int = MAX_TEXT_LENGTH) -> Optional[str]:
    if value is None:
        return None
    return _text(value, field, maximum)


def _timestamp(value: Any = None, field: str = "timestamp") -> str:
    if value is None:
        instant = datetime.now(timezone.utc)
    elif isinstance(value, datetime):
        instant = value
    elif isinstance(value, str):
        normalized = value.strip()
        if normalized.endswith(("Z", "z")):
            normalized = normalized[:-1] + "+00:00"
        try:
            instant = datetime.fromisoformat(normalized)
        except ValueError as exc:
            raise SchoolContinuityError(f"{field} must be ISO datetime") from exc
    else:
        raise SchoolContinuityError(f"{field} must be ISO datetime")
    if instant.tzinfo is None or instant.utcoffset() is None:
        raise SchoolContinuityError(f"{field} must include timezone")
    return instant.astimezone(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def _instant(value: Any, field: str = "timestamp") -> datetime:
    return datetime.fromisoformat(_timestamp(value, field).replace("Z", "+00:00"))


def _local(now: Optional[datetime]) -> datetime:
    return _instant(now, "now").astimezone(_zone())


def _revision(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise SchoolContinuityError(f"{field} must be a non-negative integer")
    return value


def _provenance(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise SchoolContinuityError(f"{field} must be an object")
    result = _copy_json(dict(value), maximum=2_000)
    if not isinstance(result, dict):
        raise SchoolContinuityError(f"{field} is invalid")
    return result


def _validate_segment(value: Any, index: int) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != _SEGMENT_KEYS:
        raise SchoolContinuityError(f"presence_segments[{index}] has invalid fields")
    arrival = _timestamp(value.get("arrival_at"), f"presence_segments[{index}].arrival_at")
    departure = value.get("departure_at")
    departure_value = (
        _timestamp(departure, f"presence_segments[{index}].departure_at")
        if departure is not None
        else None
    )
    if departure_value is not None and _instant(departure_value) < _instant(arrival):
        raise SchoolContinuityError("presence segment departs before arrival")
    return {"arrival_at": arrival, "departure_at": departure_value}


def _validate_period(value: Any, index: int) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != _PERIOD_KEYS:
        raise SchoolContinuityError(f"periods[{index}] has invalid fields")
    period_id = _text(value.get("period_id"), f"periods[{index}].period_id", 96)
    attendance = _text(value.get("attendance"), f"periods[{index}].attendance", 32)
    if attendance not in _ATTENDANCE_VALUES:
        raise SchoolContinuityError("period attendance is invalid")
    arrival = value.get("arrival_at")
    departure = value.get("departure_at")
    arrival_value = _timestamp(arrival, f"periods[{index}].arrival_at") if arrival is not None else None
    departure_value = (
        _timestamp(departure, f"periods[{index}].departure_at")
        if departure is not None
        else None
    )
    if (
        arrival_value is not None
        and departure_value is not None
        and _instant(departure_value) < _instant(arrival_value)
    ):
        raise SchoolContinuityError("period departure precedes arrival")
    return {
        "period_id": period_id,
        "attendance": attendance,
        "arrival_at": arrival_value,
        "departure_at": departure_value,
        "provenance": _provenance(value.get("provenance"), f"periods[{index}].provenance"),
    }


def _validate_day(value: Any, index: int) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != _DAY_KEYS:
        raise SchoolContinuityError(f"school_days[{index}] has invalid fields")
    local_date = _text(value.get("local_date"), f"school_days[{index}].local_date", 32)
    try:
        parsed_date = date.fromisoformat(local_date)
    except ValueError as exc:
        raise SchoolContinuityError("school day local_date is invalid") from exc
    school_day_id = _text(value.get("school_day_id"), f"school_days[{index}].school_day_id", 128)
    if school_day_id != f"{SCHOOL_ID}:{local_date}":
        raise SchoolContinuityError("school_day_id is not stable")
    weekday = value.get("weekday")
    if isinstance(weekday, bool) or not isinstance(weekday, int) or not 1 <= weekday <= 7:
        raise SchoolContinuityError("school day weekday is invalid")
    if weekday != parsed_date.isoweekday():
        raise SchoolContinuityError("school day weekday does not match date")
    schedule_revision = _text(
        value.get("schedule_revision"), f"school_days[{index}].schedule_revision", 96
    )
    if schedule_revision != SCHEDULE_REVISION:
        raise SchoolContinuityError("unsupported schedule revision")
    status = _text(value.get("status"), f"school_days[{index}].status", 16)
    if status not in {"OPEN", "CLOSED"}:
        raise SchoolContinuityError("school day status is invalid")
    first_arrival = value.get("first_arrival_at")
    last_departure = value.get("last_departure_at")
    first_arrival_value = (
        _timestamp(first_arrival, f"school_days[{index}].first_arrival_at")
        if first_arrival is not None
        else None
    )
    last_departure_value = (
        _timestamp(last_departure, f"school_days[{index}].last_departure_at")
        if last_departure is not None
        else None
    )
    last_observed_value = _timestamp(
        value.get("last_observed_at"), f"school_days[{index}].last_observed_at"
    )
    if (
        first_arrival_value is not None
        and last_departure_value is not None
        and _instant(last_departure_value) < _instant(first_arrival_value)
    ):
        raise SchoolContinuityError("school day departs before first arrival")
    last_location = _optional_text(value.get("last_location_id"), "school_days.last_location_id", 96)
    raw_segments = value.get("presence_segments")
    if not isinstance(raw_segments, list) or len(raw_segments) > MAX_PRESENCE_SEGMENTS:
        raise SchoolContinuityError("presence_segments are invalid")
    segments = [_validate_segment(item, item_index) for item_index, item in enumerate(raw_segments)]
    raw_periods = value.get("periods")
    if not isinstance(raw_periods, list) or len(raw_periods) > len(_CLASS_PERIOD_IDS):
        raise SchoolContinuityError("school periods are invalid")
    periods = [_validate_period(item, item_index) for item_index, item in enumerate(raw_periods)]
    period_ids = [item["period_id"] for item in periods]
    if len(period_ids) != len(set(period_ids)) or not set(period_ids).issubset(_CLASS_PERIOD_IDS):
        raise SchoolContinuityError("school period identities are invalid")
    created_at = _timestamp(value.get("created_at"), f"school_days[{index}].created_at")
    updated_at = _timestamp(value.get("updated_at"), f"school_days[{index}].updated_at")
    return {
        "school_day_id": school_day_id,
        "local_date": local_date,
        "weekday": weekday,
        "schedule_revision": schedule_revision,
        "status": status,
        "first_arrival_at": first_arrival_value,
        "last_departure_at": last_departure_value,
        "last_location_id": last_location,
        "last_observed_at": last_observed_value,
        "presence_segments": segments,
        "periods": periods,
        "created_at": created_at,
        "updated_at": updated_at,
        "provenance": _provenance(value.get("provenance"), f"school_days[{index}].provenance"),
    }


def validate_state(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != _STATE_KEYS:
        raise SchoolContinuityError("school continuity state has invalid fields")
    if value.get("schema_version") != SCHEMA_VERSION:
        raise SchoolContinuityError("unsupported school continuity schema")
    if value.get("world_id") != WORLD_ID:
        raise SchoolContinuityError("school continuity world is invalid")
    revision = _revision(value.get("revision"), "revision")
    continuity_started_at = _timestamp(
        value.get("continuity_started_at"), "continuity_started_at"
    )
    raw_days = value.get("school_days")
    if not isinstance(raw_days, list) or len(raw_days) > MAX_SCHOOL_DAYS:
        raise SchoolContinuityError("school_days are invalid")
    days = [_validate_day(item, index) for index, item in enumerate(raw_days)]
    ids = [item["school_day_id"] for item in days]
    if len(ids) != len(set(ids)):
        raise SchoolContinuityError("school_day_id must be unique")
    return _copy_json(
        {
            "schema_version": SCHEMA_VERSION,
            "world_id": WORLD_ID,
            "revision": revision,
            "continuity_started_at": continuity_started_at,
            "school_days": days,
        }
    )


def _empty_state(continuity_started_at: str) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "world_id": WORLD_ID,
        "revision": 1,
        "continuity_started_at": _timestamp(continuity_started_at, "continuity_started_at"),
        "school_days": [],
    }


def load_state(hermes_home: Optional[Path | str] = None) -> Optional[dict[str, Any]]:
    path = state_path(hermes_home)
    try:
        return validate_state(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, TypeError, ValueError, json.JSONDecodeError, SchoolContinuityError):
        return None


def _save(path: Path, state: Mapping[str, Any]) -> dict[str, Any]:
    normalized = validate_state(state)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(
        normalized,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ) + "\n"
    temporary_path: Optional[str] = None
    try:
        descriptor, temporary_path = tempfile.mkstemp(
            prefix=f".{path.name}.", dir=str(path.parent), text=True
        )
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
        temporary_path = None
    except OSError as exc:
        raise SchoolContinuityError("school continuity state could not be written") from exc
    finally:
        if temporary_path is not None:
            try:
                os.unlink(temporary_path)
            except OSError:
                pass
    return normalized


@contextmanager
def _lock(path: Path):
    if fcntl is None:
        raise SchoolContinuityError("school continuity lock unavailable")
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(path.name + ".lock")
    with lock_path.open("a+") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _class_sessions(local_date: date) -> list[dict[str, Any]]:
    return school_day.class_sessions(local_date)


def _travel_pending(home: Path) -> bool:
    try:
        state = travel.load_state(home)
    except Exception:
        return False
    if not isinstance(state, Mapping):
        return False
    return any(
        isinstance(item, Mapping)
        and item.get("status") == travel.TRAVEL_STATUS_IN_PROGRESS
        and item.get("destination_location_id") == SCHOOL_LOCATION_ID
        for item in state.get("travels", [])
    )


def _known_location(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    if not normalized or len(normalized) > 96 or any(char.isspace() for char in normalized):
        return None
    return normalized


def _is_school_location(location_id: Optional[str]) -> bool:
    return location_id in {SCHOOL_LOCATION_ID, CLASSROOM_ID}


def _is_known_non_school(location_id: Optional[str]) -> bool:
    return location_id is not None and not _is_school_location(location_id)


def _new_day(local: datetime, observed_at: str) -> dict[str, Any]:
    local_date = local.date().isoformat()
    return {
        "school_day_id": f"{SCHOOL_ID}:{local_date}",
        "local_date": local_date,
        "weekday": local.isoweekday(),
        "schedule_revision": SCHEDULE_REVISION,
        "status": "OPEN",
        "first_arrival_at": None,
        "last_departure_at": None,
        "last_location_id": None,
        "last_observed_at": observed_at,
        "presence_segments": [],
        "periods": [],
        "created_at": observed_at,
        "updated_at": observed_at,
        "provenance": {
            "kind": "configured_school_day",
            "source": SCHEDULE_REVISION,
            "factual": True,
        },
    }


def _update_presence(
    day: dict[str, Any],
    *,
    location_id: Optional[str],
    observed_at: str,
) -> None:
    previous_location = day.get("last_location_id")
    segments = day["presence_segments"]
    if _is_school_location(location_id):
        if previous_location not in {SCHOOL_LOCATION_ID, CLASSROOM_ID}:
            if len(segments) >= MAX_PRESENCE_SEGMENTS:
                raise SchoolContinuityError("presence segment bound exceeded")
            segments.append({"arrival_at": observed_at, "departure_at": None})
        if day.get("first_arrival_at") is None:
            day["first_arrival_at"] = segments[0]["arrival_at"]
    elif _is_known_non_school(location_id):
        if previous_location in {SCHOOL_LOCATION_ID, CLASSROOM_ID} and segments:
            current = segments[-1]
            if current.get("departure_at") is None:
                current["departure_at"] = observed_at
                day["last_departure_at"] = observed_at
    day["last_location_id"] = location_id or "unknown"
    day["last_observed_at"] = observed_at


def _segment_covers_period(
    segment: Mapping[str, Any],
    *,
    starts_at: str,
    ends_at: str,
    currently_at_school: bool,
) -> bool:
    arrival = _instant(segment["arrival_at"])
    end = _instant(ends_at)
    if arrival > end:
        return False
    departure = segment.get("departure_at")
    if departure is None:
        return currently_at_school
    return _instant(departure) >= _instant(starts_at)


def _attendance_for(
    day: Mapping[str, Any],
    session: Mapping[str, Any],
    *,
    now: datetime,
    continuity_started_at: datetime,
    location_id: Optional[str],
    travel_pending: bool,
) -> Optional[dict[str, Any]]:
    if now < _instant(session["ends_at"]):
        return None
    if _instant(session["ends_at"]) <= continuity_started_at:
        return {
            "attendance": ATTENDANCE_UNKNOWN,
            "arrival_at": None,
            "departure_at": None,
            "basis": _PRE_CONTINUITY_UNKNOWN_BASIS,
        }
    if location_id is None:
        attendance = ATTENDANCE_UNKNOWN
        arrival = None
        departure = None
        basis = "authoritative_world_location_unavailable"
    else:
        currently_at_school = _is_school_location(location_id)
        covers = any(
            _segment_covers_period(
                segment,
                starts_at=session["starts_at"],
                ends_at=session["ends_at"],
                currently_at_school=currently_at_school,
            )
            for segment in day["presence_segments"]
        )
        first_arrival = day.get("first_arrival_at")
        if covers and isinstance(first_arrival, str):
            arrival_instant = _instant(first_arrival)
            attendance = (
                ATTENDANCE_LATE
                if arrival_instant > _instant(session["starts_at"])
                else ATTENDANCE_PRESENT
            )
            arrival = first_arrival
            departure = day.get("last_departure_at") if not currently_at_school else None
            basis = "authoritative_world_school_presence"
        else:
            attendance = ATTENDANCE_ABSENT
            arrival = None
            departure = None
            basis = (
                "travel_pending_or_not_at_school"
                if travel_pending
                else "period_ended_without_authoritative_school_presence"
            )
    return {
        "attendance": attendance,
        "arrival_at": arrival,
        "departure_at": departure,
        "basis": basis,
    }


def _record_attendance(
    day: dict[str, Any],
    *,
    sessions: Iterable[Mapping[str, Any]],
    now: datetime,
    continuity_started_at: datetime,
    location_id: Optional[str],
    travel_pending: bool,
    observed_at: str,
) -> None:
    session_list = list(sessions)
    by_id = {item["period_id"]: item for item in day["periods"]}
    for session in session_list:
        period_id = str(session["period"])
        if session.get("status") == "cancelled":
            continue
        derived = _attendance_for(
            day,
            session,
            now=now,
            continuity_started_at=continuity_started_at,
            location_id=location_id,
            travel_pending=travel_pending,
        )
        if derived is None:
            continue
        existing = by_id.get(period_id)
        if existing is not None:
            if existing["attendance"] != ATTENDANCE_UNKNOWN:
                continue
            provenance = existing.get("provenance")
            if (
                isinstance(provenance, Mapping)
                and provenance.get("basis") == _PRE_CONTINUITY_UNKNOWN_BASIS
            ):
                continue
        by_id[period_id] = {
            "period_id": period_id,
            "attendance": derived["attendance"],
            "arrival_at": derived["arrival_at"],
            "departure_at": derived["departure_at"],
            "provenance": {
                "kind": "lazy_school_attendance_observation",
                "source": PROJECTION_KIND,
                "basis": derived["basis"],
                "observed_at": observed_at,
                "factual": True,
            },
        }
    order = {str(item["period"]): index for index, item in enumerate(session_list)}
    day["periods"] = sorted(by_id.values(), key=lambda item: order[item["period_id"]])


def _presence_status(
    day: Mapping[str, Any],
    *,
    location_id: Optional[str],
    travel_pending: bool,
) -> str:
    if _is_school_location(location_id):
        return PRESENCE_PRESENT
    if travel_pending:
        return PRESENCE_TRAVEL_PENDING
    if location_id is None:
        return PRESENCE_UNKNOWN
    if day.get("first_arrival_at") or any(
        item.get("attendance") in {ATTENDANCE_PRESENT, ATTENDANCE_LATE}
        for item in day.get("periods", [])
    ):
        return PRESENCE_LEFT
    return PRESENCE_NOT_ARRIVED


def _project(
    day: Mapping[str, Any],
    school_projection: Mapping[str, Any],
    *,
    continuity_started_at: str,
    location_id: Optional[str],
    presence_status: str,
    observed_at: str,
) -> dict[str, Any]:
    periods = [
        {
            "period_id": item["period_id"],
            "attendance": item["attendance"],
            "arrival_at": item["arrival_at"],
            "departure_at": item["departure_at"],
            "provenance": _copy_json(item["provenance"], maximum=2_000),
        }
        for item in day["periods"]
    ]
    counts = {value: 0 for value in _ATTENDANCE_VALUES}
    for item in periods:
        counts[item["attendance"]] += 1
    result = {
        "kind": PROJECTION_KIND,
        "schema_version": SCHEMA_VERSION,
        "status": "available",
        "continuity_started_at": continuity_started_at,
        "school_day": {
            "school_day_id": day["school_day_id"],
            "local_date": day["local_date"],
            "weekday": day["weekday"],
            "schedule_revision": day["schedule_revision"],
            "status": day["status"],
        },
        "current_period": _copy_json(school_projection.get("current_period"), maximum=4_000),
        "current_class": _copy_json(school_projection.get("current_class"), maximum=4_000),
        "presence": {
            "status": presence_status,
            "location_id": location_id,
            "observed_at": observed_at,
            "provenance": "shiomi_world_location_observation",
        },
        "attendance": periods,
        "attendance_summary": {
            "finalized_count": len(periods),
            "present_count": counts[ATTENDANCE_PRESENT],
            "late_count": counts[ATTENDANCE_LATE],
            "absent_count": counts[ATTENDANCE_ABSENT],
            "unknown_count": counts[ATTENDANCE_UNKNOWN],
        },
        "provenance": "shiomi_school_day_and_world_location",
    }
    encoded = json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    if len(encoded) > MAX_PROJECTION_BYTES:
        raise SchoolContinuityError("school projection exceeds bounded size")
    return _copy_json(result, maximum=MAX_PROJECTION_BYTES)


def observe_school_life(
    *,
    hermes_home: Optional[Path | str] = None,
    now: Optional[datetime] = None,
    current_location_id: Any = None,
    environment: Optional[Mapping[str, str]] = None,
    closed_dates: Optional[Iterable[date | str]] = None,
    school_day_projection: Optional[Mapping[str, Any]] = None,
    school_event_projection: Optional[Mapping[str, Any]] = None,
) -> dict[str, Any]:
    """Lazily realize one bounded school-life observation.

    The caller must provide the current World location. No location is ever
    inferred from chat, current activity text, or a planned action.
    """

    env = _environment(environment)
    if not feature_enabled(env):
        return {
            "kind": PROJECTION_KIND,
            "schema_version": SCHEMA_VERSION,
            "status": "disabled",
            "reason_code": "FEATURE_DISABLED",
        }
    if not school_day.feature_enabled(env):
        return {
            "kind": PROJECTION_KIND,
            "schema_version": SCHEMA_VERSION,
            "status": "unavailable",
            "reason_code": "SCHOOL_DAY_FEATURE_DISABLED",
        }
    try:
        local = _local(now)
        school_day_value = school_day_projection
        if school_day_value is None:
            school_day_value = school_day.observe_school_day(
                now=local,
                current_location_id=current_location_id,
                environment=env,
                closed_dates=closed_dates,
            )
        if not isinstance(school_day_value, Mapping):
            raise SchoolContinuityError("school day projection unavailable")
        if school_day_value.get("status") != "available":
            return {
                "kind": PROJECTION_KIND,
                "schema_version": SCHEMA_VERSION,
                "status": "unavailable",
                "reason_code": str(
                    school_day_value.get("reason_code") or "SCHOOL_DAY_UNAVAILABLE"
                ),
            }
        if not bool(school_day_value.get("school_day")):
            return {
                "kind": PROJECTION_KIND,
                "schema_version": SCHEMA_VERSION,
                "status": "not_school_day",
                "local_date": local.date().isoformat(),
                "weekday": local.isoweekday(),
                "provenance": SCHEDULE_REVISION,
            }
        location_id = _known_location(current_location_id)
        observed_at = _timestamp(local, "observed_at")
        home = _home(hermes_home)
        continuity_path = state_path(home)
        pending = _travel_pending(home)
        sessions = _class_sessions(local.date())
        event_projection = school_event_projection
        if event_projection is None and school_events.feature_enabled(env):
            event_projection = school_events.observe_school_events(
                hermes_home=home,
                now=local,
                current_location_id=current_location_id,
                environment=env,
                school_day_projection=school_day_value,
            )
        if (
            isinstance(event_projection, Mapping)
            and event_projection.get("status") == "available"
        ):
            sessions = school_events.effective_class_sessions(
                sessions,
                event_projection,
            )
        with _lock(continuity_path):
            if continuity_path.exists():
                state = load_state(home)
                if state is None:
                    return {
                        "kind": PROJECTION_KIND,
                        "schema_version": SCHEMA_VERSION,
                        "status": "unavailable",
                        "reason_code": "STATE_CORRUPT",
                    }
            else:
                state = _empty_state(observed_at)
            assert state is not None
            continuity_started_at = _instant(
                state["continuity_started_at"], "continuity_started_at"
            )
            school_day_id = f"{SCHOOL_ID}:{local.date().isoformat()}"
            day = next(
                (item for item in state["school_days"] if item["school_day_id"] == school_day_id),
                None,
            )
            day = _copy_json(day) if isinstance(day, Mapping) else _new_day(local, observed_at)
            before = _copy_json(day)
            _update_presence(day, location_id=location_id, observed_at=observed_at)
            _record_attendance(
                day,
                sessions=sessions,
                now=local,
                continuity_started_at=continuity_started_at,
                location_id=location_id,
                travel_pending=pending,
                observed_at=observed_at,
            )
            last_period = school_day.TIMETABLE[-1]
            if local.hour * 60 + local.minute >= last_period.end_minute:
                day["status"] = "CLOSED"
            day["updated_at"] = observed_at
            existing_day = any(
                item["school_day_id"] == school_day_id for item in state["school_days"]
            )
            changed = day != before or not existing_day
            if changed:
                updated = _copy_json(state)
                days = [
                    day if item["school_day_id"] == school_day_id else item
                    for item in state["school_days"]
                ]
                if not existing_day:
                    days.append(day)
                days.sort(key=lambda item: item["local_date"])
                updated["school_days"] = days[-MAX_SCHOOL_DAYS:]
                updated["revision"] = state["revision"] + 1
                _save(continuity_path, updated)
            presence = _presence_status(
                day,
                location_id=location_id,
                travel_pending=pending,
            )
            return _project(
                day,
                school_day_value,
                continuity_started_at=state["continuity_started_at"],
                location_id=location_id,
                presence_status=presence,
                observed_at=observed_at,
            )
    except (OSError, SchoolContinuityError, TypeError, ValueError):
        return {
            "kind": PROJECTION_KIND,
            "schema_version": SCHEMA_VERSION,
            "status": "unavailable",
            "reason_code": "SCHOOL_CONTINUITY_UNAVAILABLE",
        }


__all__ = [
    "ATTENDANCE_ABSENT",
    "ATTENDANCE_LATE",
    "ATTENDANCE_PRESENT",
    "ATTENDANCE_UNKNOWN",
    "CLASSROOM_ID",
    "FEATURE_ENV",
    "MAX_SCHOOL_DAYS",
    "PRESENCE_LEFT",
    "PRESENCE_NOT_ARRIVED",
    "PRESENCE_PRESENT",
    "PRESENCE_TRAVEL_PENDING",
    "PRESENCE_UNKNOWN",
    "PROJECTION_KIND",
    "SCHEDULE_REVISION",
    "SCHEMA_VERSION",
    "SCHOOL_ID",
    "SCHOOL_LOCATION_ID",
    "SchoolContinuityError",
    "feature_enabled",
    "load_state",
    "observe_school_life",
    "state_path",
    "validate_state",
]
