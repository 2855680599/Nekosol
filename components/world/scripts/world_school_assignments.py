#!/usr/bin/env python3
"""Bounded School Assignment facts for the Shiomi World.

Assignments are institutional World facts derived from the existing School Day
and School Event authorities.  This module has no task, reminder, Life, or
LLM authority.  It keeps one durable activation epoch so enabling the feature
cannot backfill assignments from before the feature was first observed.

The existing School Event Knowledge state remains the only knowledge
authority: assignment events are validated as School Events, and a Main Self
projection is allowed only after the existing first-hand/reported knowledge
seam has established that event.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections.abc import Mapping
from contextlib import contextmanager
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

try:
    import fcntl
except ImportError:  # pragma: no cover - production is POSIX
    fcntl = None

import world_school_day as school_day
import world_school_event_knowledge as school_knowledge
import world_school_events as school_events


FEATURE_ENV = "CHIYO_WORLD_SCHOOL_ASSIGNMENTS_V1_ENABLED"
WORLD_ID = school_day.WORLD_ID
SCHOOL_ID = school_day.SCHOOL_ID
WORLD_TIMEZONE = school_day.WORLD_TIMEZONE
SCHEMA_VERSION = "world.school.assignments.v1"
STATE_SCHEMA_VERSION = "world.school.assignments.state.v1"
PROJECTION_KIND = "shiomi_school_assignments"
STATE_KIND = "shiomi_school_assignment_continuity"
STATE_FILENAME = "school_assignment_continuity.json"

ASSIGNMENT_KINDS = frozenset(
    {"HOMEWORK", "READING", "WORKSHEET", "PREPARATION", "COURSEWORK"}
)
SOURCE_CLASSES = frozenset(
    {"TEACHER", "SCHOOL_INSTITUTION", "COURSE_SCHEDULE", "SCHOOL_EVENT"}
)
ASSIGNMENT_STATUSES = frozenset({"OPEN", "DUE", "CLOSED"})
MAX_ASSIGNMENTS_PER_DAY = 1
MAX_STATE_BYTES = 4_000
MAX_PROJECTION_BYTES = 12_000
MAX_TEXT_LENGTH = 160

# Sparse deterministic institution rules.  These are not a daily minimum or
# an obligation generator.  They are used only while this feature is enabled.
_ASSIGNMENT_SLOTS = frozenset({1, 6, 11})
_SUBJECT_BY_SLOT = {1: "数学", 6: "国語", 11: "理科"}
_KIND_BY_SLOT = {1: "HOMEWORK", 6: "READING", 11: "PREPARATION"}
_PERIOD_ID = "afternoon_class_1"
_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})

_STATE_KEYS = frozenset(
    {"kind", "schema_version", "world_id", "revision", "assignment_started_at"}
)
_ASSIGNMENT_KEYS = frozenset(
    {
        "assignment_id",
        "school_day_id",
        "subject",
        "assignment_kind",
        "issued_at",
        "due_at",
        "source_class",
        "source_event_id",
        "status",
        "revision",
        "provenance",
        "description",
    }
)
_PROVENANCE_KEYS = frozenset({"kind", "source", "factual"})
_ASSIGNMENT_FACT_KEYS = frozenset(
    {
        "effect",
        "assignment_id",
        "subject",
        "assignment_kind",
        "issued_at",
        "due_at",
        "description",
        "period_id",
    }
)


class SchoolAssignmentsError(ValueError):
    """Raised when bounded assignment facts are malformed."""


def _copy_json(value: Any, *, maximum: int = MAX_PROJECTION_BYTES) -> Any:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise SchoolAssignmentsError("value is not bounded JSON") from exc
    if len(encoded) > maximum:
        raise SchoolAssignmentsError("value exceeds bounded JSON size")
    return json.loads(encoded)


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
        raise SchoolAssignmentsError("school timezone is unavailable") from exc


def _local(now: Optional[datetime]) -> datetime:
    instant = datetime.now(timezone.utc) if now is None else now
    if not isinstance(instant, datetime) or instant.tzinfo is None or instant.utcoffset() is None:
        raise SchoolAssignmentsError("now must be timezone-aware")
    return instant.astimezone(_zone())


def _timestamp(value: Any, field: str) -> str:
    if isinstance(value, datetime):
        instant = value
    elif isinstance(value, str):
        text = value.strip()
        if text.endswith(("Z", "z")):
            text = text[:-1] + "+00:00"
        try:
            instant = datetime.fromisoformat(text)
        except ValueError as exc:
            raise SchoolAssignmentsError(f"{field} must be ISO datetime") from exc
    else:
        raise SchoolAssignmentsError(f"{field} must be ISO datetime")
    if instant.tzinfo is None or instant.utcoffset() is None:
        raise SchoolAssignmentsError(f"{field} must include timezone")
    return instant.astimezone(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def _instant(value: Any, field: str = "timestamp") -> datetime:
    return datetime.fromisoformat(_timestamp(value, field).replace("Z", "+00:00"))


def _text(value: Any, field: str, maximum: int = MAX_TEXT_LENGTH) -> str:
    if not isinstance(value, str):
        raise SchoolAssignmentsError(f"{field} must be a string")
    result = value.strip()
    if not result or len(result) > maximum or any(char in result for char in "\r\n\x00"):
        raise SchoolAssignmentsError(f"{field} is invalid")
    return result


def _id(value: Any, field: str) -> str:
    result = _text(value, field, 200)
    if any(char.isspace() for char in result):
        raise SchoolAssignmentsError(f"{field} must not contain whitespace")
    return result


def _revision(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise SchoolAssignmentsError(f"{field} must be a positive integer")
    return value


def _home(hermes_home: Optional[Path | str] = None) -> Path:
    raw = hermes_home if hermes_home is not None else os.environ.get("HERMES_HOME")
    path = Path(raw).expanduser() if raw else Path.home() / ".hermes"
    if not path.is_absolute():
        raise SchoolAssignmentsError("hermes_home must be absolute")
    return path


def state_path(hermes_home: Optional[Path | str] = None) -> Path:
    return _home(hermes_home) / "data" / "world" / STATE_FILENAME


def assignment_id_for(source_event_id: Any) -> str:
    event_id = _id(source_event_id, "source_event_id")
    digest = hashlib.sha256(event_id.encode("utf-8")).hexdigest()
    return f"school-assignment-{digest}"


def _next_school_day(local_date: date) -> date:
    for offset in range(1, 15):
        candidate = local_date + timedelta(days=offset)
        if school_day.is_school_day(candidate):
            return candidate
    raise SchoolAssignmentsError("next school day is unavailable")


def _period_start(local_date: date, period_id: str) -> datetime:
    for period in school_day.TIMETABLE:
        if period.period == period_id:
            return datetime.combine(
                local_date,
                time(period.start_minute // 60, period.start_minute % 60),
                tzinfo=_zone(),
            )
    raise SchoolAssignmentsError("assignment period is not in the school timetable")


def _specs(local_date: date) -> tuple[dict[str, Any], ...]:
    if not school_day.is_school_day(local_date):
        return ()
    slot = local_date.toordinal() % 14
    if slot not in _ASSIGNMENT_SLOTS:
        return ()
    school_day_id = f"{SCHOOL_ID}:{local_date.isoformat()}"
    event_slot = f"assignment-{slot}"
    event_id = school_events.event_id_for(
        school_day_id,
        "TEACHER_EVENT",
        slot=event_slot,
    )
    issued_at = _period_start(local_date, _PERIOD_ID)
    due_at = datetime.combine(
        _next_school_day(local_date),
        time(18, 0),
        tzinfo=_zone(),
    )
    subject = _SUBJECT_BY_SLOT[slot]
    assignment_kind = _KIND_BY_SLOT[slot]
    description = {
        "HOMEWORK": "完成课后练习",
        "READING": "阅读指定页数",
        "PREPARATION": "预习下一节内容",
    }[assignment_kind]
    return (
        {
            "event_kind": "TEACHER_EVENT",
            "source_category": "TEACHER",
            "source_actor": "teacher:course",
            "visibility": "class_public",
            "period_id": _PERIOD_ID,
            "facts": {
                "effect": "ASSIGNMENT_ISSUED",
                "assignment_id": assignment_id_for(event_id),
                "subject": subject,
                "assignment_kind": assignment_kind,
                "issued_at": _timestamp(issued_at, "issued_at"),
                "due_at": _timestamp(due_at, "due_at"),
                "description": description,
            },
            "slot": event_slot,
        },
    )


def assignment_specs_for(local_date: date) -> list[dict[str, Any]]:
    """Return the bounded deterministic fixture rule without writing state."""

    if isinstance(local_date, datetime) or not isinstance(local_date, date):
        raise SchoolAssignmentsError("local_date must be a date")
    return _copy_json(list(_specs(local_date)), maximum=4_000)


def _validate_provenance(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != _PROVENANCE_KEYS:
        raise SchoolAssignmentsError("assignment provenance is invalid")
    kind = _text(value.get("kind"), "provenance.kind", 96)
    source = _text(value.get("source"), "provenance.source", 160)
    if value.get("factual") is not True:
        raise SchoolAssignmentsError("assignment provenance must be factual")
    return {"kind": kind, "source": source, "factual": True}


def validate_assignment(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != _ASSIGNMENT_KEYS:
        raise SchoolAssignmentsError("assignment has an invalid schema")
    source_event_id = _id(value.get("source_event_id"), "source_event_id")
    assignment_id = _id(value.get("assignment_id"), "assignment_id")
    if assignment_id != assignment_id_for(source_event_id):
        raise SchoolAssignmentsError("assignment identity is not stable")
    school_day_id = _id(value.get("school_day_id"), "school_day_id")
    if not school_day_id.startswith(f"{SCHOOL_ID}:"):
        raise SchoolAssignmentsError("assignment school_day_id is not canonical")
    subject = _text(value.get("subject"), "subject", 64)
    assignment_kind = _text(value.get("assignment_kind"), "assignment_kind", 32).upper()
    if assignment_kind not in ASSIGNMENT_KINDS:
        raise SchoolAssignmentsError("assignment kind is unsupported")
    issued_at = _timestamp(value.get("issued_at"), "issued_at")
    due_at = _timestamp(value.get("due_at"), "due_at")
    if _instant(issued_at) >= _instant(due_at):
        raise SchoolAssignmentsError("assignment due_at is not after issued_at")
    source_class = _text(value.get("source_class"), "source_class", 64).upper()
    if source_class not in SOURCE_CLASSES:
        raise SchoolAssignmentsError("assignment source class is unsupported")
    status = _text(value.get("status"), "status", 16).upper()
    if status not in ASSIGNMENT_STATUSES:
        raise SchoolAssignmentsError("assignment status is unsupported")
    revision = _revision(value.get("revision"), "revision")
    description = _text(value.get("description"), "description", 240)
    provenance = _validate_provenance(value.get("provenance"))
    return {
        "assignment_id": assignment_id,
        "school_day_id": school_day_id,
        "subject": subject,
        "assignment_kind": assignment_kind,
        "issued_at": issued_at,
        "due_at": due_at,
        "source_class": source_class,
        "source_event_id": source_event_id,
        "status": status,
        "revision": revision,
        "provenance": provenance,
        "description": description,
    }


def _validate_assignment_event(value: Any) -> dict[str, Any]:
    event = school_events.validate_event(value)
    facts = event["facts"]
    if event["event_kind"] != "TEACHER_EVENT" or event["source_category"] != "TEACHER":
        raise SchoolAssignmentsError("assignment event authority is invalid")
    if set(facts) != _ASSIGNMENT_FACT_KEYS or facts.get("effect") != "ASSIGNMENT_ISSUED":
        raise SchoolAssignmentsError("assignment event facts are invalid")
    assignment_id = _id(facts.get("assignment_id"), "facts.assignment_id")
    if assignment_id != assignment_id_for(event["event_id"]):
        raise SchoolAssignmentsError("assignment event identity is not stable")
    school_day_id = _id(event.get("school_day_id"), "school_day_id")
    issued_at = _timestamp(facts.get("issued_at"), "facts.issued_at")
    due_at = _timestamp(facts.get("due_at"), "facts.due_at")
    if _instant(issued_at) >= _instant(due_at):
        raise SchoolAssignmentsError("assignment event due_at is invalid")
    _text(facts.get("subject"), "facts.subject", 64)
    assignment_kind = _text(facts.get("assignment_kind"), "facts.assignment_kind", 32).upper()
    if assignment_kind not in ASSIGNMENT_KINDS:
        raise SchoolAssignmentsError("assignment event kind is unsupported")
    _text(facts.get("description"), "facts.description", 240)
    if facts.get("period_id") != _PERIOD_ID:
        raise SchoolAssignmentsError("assignment event period is invalid")
    if not school_day_id.startswith(f"{SCHOOL_ID}:"):
        raise SchoolAssignmentsError("assignment event day is not canonical")
    return event


def _validate_state(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != _STATE_KEYS:
        raise SchoolAssignmentsError("assignment continuity state is invalid")
    if value.get("kind") != STATE_KIND or value.get("schema_version") != STATE_SCHEMA_VERSION:
        raise SchoolAssignmentsError("assignment continuity identity is invalid")
    if value.get("world_id") != WORLD_ID:
        raise SchoolAssignmentsError("assignment continuity world is invalid")
    return {
        "kind": STATE_KIND,
        "schema_version": STATE_SCHEMA_VERSION,
        "world_id": WORLD_ID,
        "revision": _revision(value.get("revision"), "revision"),
        "assignment_started_at": _timestamp(
            value.get("assignment_started_at"), "assignment_started_at"
        ),
    }


def load_state(hermes_home: Optional[Path | str] = None) -> Optional[dict[str, Any]]:
    path = state_path(hermes_home)
    try:
        return _validate_state(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, TypeError, ValueError, json.JSONDecodeError, SchoolAssignmentsError):
        return None


def _save(path: Path, state: Mapping[str, Any]) -> dict[str, Any]:
    normalized = _validate_state(state)
    payload = json.dumps(
        normalized,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ) + "\n"
    if len(payload.encode("utf-8")) > MAX_STATE_BYTES:
        raise SchoolAssignmentsError("assignment state is too large")
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, raw_temp = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent), text=True)
    temporary_path = Path(raw_temp)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary_path, 0o600)
        os.replace(temporary_path, path)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()
    return normalized


@contextmanager
def _lock(path: Path):
    if fcntl is None:
        raise SchoolAssignmentsError("assignment state lock unavailable")
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(path.name + ".lock")
    with lock_path.open("a+") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _ensure_state(home: Path, started_at: str) -> dict[str, Any]:
    path = state_path(home)
    with _lock(path):
        if path.exists():
            loaded = load_state(home)
            if loaded is None:
                raise SchoolAssignmentsError("assignment state is corrupt")
            return loaded
        return _save(
            path,
            {
                "kind": STATE_KIND,
                "schema_version": STATE_SCHEMA_VERSION,
                "world_id": WORLD_ID,
                "revision": 1,
                "assignment_started_at": started_at,
            },
        )


def _assignment_from_event(event: Mapping[str, Any], now: datetime) -> dict[str, Any]:
    normalized = _validate_assignment_event(event)
    facts = normalized["facts"]
    status = "OPEN" if now < _instant(facts["due_at"]) else "DUE"
    return validate_assignment(
        {
            "assignment_id": facts["assignment_id"],
            "school_day_id": normalized["school_day_id"],
            "subject": facts["subject"],
            "assignment_kind": facts["assignment_kind"],
            "issued_at": facts["issued_at"],
            "due_at": facts["due_at"],
            "source_class": normalized["source_category"],
            "source_event_id": normalized["event_id"],
            "status": status,
            "revision": 1,
            "provenance": {
                "kind": "school_assignment_rule",
                "source": school_events.PROJECTION_KIND,
                "factual": True,
            },
            "description": facts["description"],
        }
    )


def observe_school_assignments(
    *,
    hermes_home: Optional[Path | str] = None,
    now: Optional[datetime] = None,
    current_location_id: Any = None,
    environment: Optional[Mapping[str, str]] = None,
    school_day_projection: Optional[Mapping[str, Any]] = None,
) -> dict[str, Any]:
    """Lazily realize current institutional assignment facts.

    The only durable write is the activation epoch.  Assignment facts are
    derived from the existing School Event authority and are never converted
    into a Todo, Goal, Life action, reminder, message, or Experience.
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
        instant = datetime.now(timezone.utc) if now is None else now
        local = _local(instant)
        projection = school_day_projection
        if projection is None:
            projection = school_day.observe_school_day(
                now=local,
                current_location_id=current_location_id,
                environment=env,
            )
        if not isinstance(projection, Mapping) or projection.get("status") != "available":
            return {
                "kind": PROJECTION_KIND,
                "schema_version": SCHEMA_VERSION,
                "status": "unavailable",
                "reason_code": str(
                    projection.get("reason_code")
                    if isinstance(projection, Mapping)
                    else "SCHOOL_DAY_UNAVAILABLE"
                ),
            }
        observed_at = _timestamp(local, "observed_at")
        home = _home(hermes_home)
        state = _ensure_state(home, observed_at)
        started_at = _instant(state["assignment_started_at"])
        if not bool(projection.get("school_day")):
            return {
                "kind": PROJECTION_KIND,
                "schema_version": SCHEMA_VERSION,
                "status": "not_school_day",
                "world_id": WORLD_ID,
                "continuity_started_at": state["assignment_started_at"],
                "assignments": [],
                "events": [],
                "knowledge_boundary": {"automatic_main_self_projection": False},
                "provenance": PROJECTION_KIND,
            }
        events = school_events.materialize_school_events(
            local.date(),
            continuity_started_at=state["assignment_started_at"],
            event_specs=_specs(local.date()),
        )
        visible_events = []
        assignments = []
        now_utc = instant.astimezone(timezone.utc)
        for event in events:
            normalized = _validate_assignment_event(event)
            issued_at = _instant(normalized["facts"]["issued_at"])
            if issued_at < started_at or issued_at > now_utc:
                continue
            visible_events.append(normalized)
            assignments.append(_assignment_from_event(normalized, now_utc))
        return {
            "kind": PROJECTION_KIND,
            "schema_version": SCHEMA_VERSION,
            "status": "available",
            "world_id": WORLD_ID,
            "school_day_id": f"{SCHOOL_ID}:{local.date().isoformat()}",
            "continuity_started_at": state["assignment_started_at"],
            "assignments": _copy_json(assignments, maximum=MAX_PROJECTION_BYTES),
            # Internal bridge input; stripped before a public World projection.
            "events": _copy_json(visible_events, maximum=MAX_PROJECTION_BYTES),
            "knowledge_boundary": {
                "automatic_main_self_projection": False,
                "requires_existing_school_event_knowledge": True,
            },
            "provenance": PROJECTION_KIND,
        }
    except (OSError, SchoolAssignmentsError, TypeError, ValueError):
        return {
            "kind": PROJECTION_KIND,
            "schema_version": SCHEMA_VERSION,
            "status": "unavailable",
            "reason_code": "SCHOOL_ASSIGNMENTS_UNAVAILABLE",
        }


def public_projection(projection: Mapping[str, Any]) -> dict[str, Any]:
    """Remove internal event-bridge material from the World-facing view."""

    if not isinstance(projection, Mapping):
        raise SchoolAssignmentsError("assignment projection is invalid")
    result = _copy_json(dict(projection), maximum=MAX_PROJECTION_BYTES)
    result.pop("events", None)
    return result


def merge_into_school_events(
    school_event_projection: Mapping[str, Any],
    assignment_projection: Optional[Mapping[str, Any]],
) -> dict[str, Any]:
    """Add assignment events to the existing School Event projection only."""

    result = _copy_json(dict(school_event_projection), maximum=MAX_PROJECTION_BYTES)
    if not isinstance(assignment_projection, Mapping):
        return result
    if assignment_projection.get("status") != "available":
        return result
    assignment_events = assignment_projection.get("events")
    if not isinstance(assignment_events, list):
        raise SchoolAssignmentsError("assignment events are invalid")
    if not assignment_events:
        return result
    if result.get("status") != "available" or not isinstance(result.get("events"), list):
        return result
    combined = list(result["events"])
    existing_ids = {item.get("event_id") for item in combined if isinstance(item, Mapping)}
    for raw_event in assignment_events:
        event = _validate_assignment_event(raw_event)
        if event["event_id"] not in existing_ids:
            combined.append(event)
            existing_ids.add(event["event_id"])
    if len(combined) > school_events.MAX_EVENTS_PER_DAY:
        raise SchoolAssignmentsError("combined school event bound exceeded")
    combined.sort(key=lambda item: item["event_id"])
    result["events"] = combined
    return _copy_json(result, maximum=MAX_PROJECTION_BYTES)


def project_for_main_self(
    world_projection: Mapping[str, Any],
    *,
    hermes_home: Optional[Path | str] = None,
    environment: Optional[Mapping[str, str]] = None,
) -> dict[str, Any]:
    """Project only assignment facts established by the existing knowledge authority."""

    result = _copy_json(world_projection, maximum=16_000)
    if not feature_enabled(environment):
        return result
    raw_projection = result.pop("school_assignments", None)
    if not isinstance(raw_projection, Mapping):
        return result
    raw_assignments = raw_projection.get("assignments")
    if not isinstance(raw_assignments, list) or len(raw_assignments) > MAX_ASSIGNMENTS_PER_DAY:
        return result
    knowledge_state = school_knowledge.load_state(hermes_home)
    if knowledge_state is None:
        return result
    known = {
        item["event_id"]: item
        for item in knowledge_state["items"]
        if item["knowledge_status"] in {"OBSERVED", "REPORTED_LATER"}
    }
    projected = []
    for raw_assignment in raw_assignments:
        try:
            assignment = validate_assignment(raw_assignment)
        except (SchoolAssignmentsError, TypeError, ValueError):
            continue
        knowledge = known.get(assignment["source_event_id"])
        if knowledge is None:
            continue
        item = _copy_json(assignment, maximum=4_000)
        item["knowledge_status"] = knowledge["knowledge_status"]
        projected.append(item)
    if projected:
        result["school_assignments"] = {
            "status": "known",
            "assignments": projected,
            "knowledge_boundary": {
                "source": "school_event_knowledge",
                "bounded": True,
            },
        }
    return _copy_json(result, maximum=16_000)


__all__ = [
    "ASSIGNMENT_KINDS",
    "ASSIGNMENT_STATUSES",
    "FEATURE_ENV",
    "MAX_ASSIGNMENTS_PER_DAY",
    "PROJECTION_KIND",
    "SCHEMA_VERSION",
    "SOURCE_CLASSES",
    "STATE_FILENAME",
    "STATE_SCHEMA_VERSION",
    "SchoolAssignmentsError",
    "assignment_id_for",
    "assignment_specs_for",
    "feature_enabled",
    "load_state",
    "merge_into_school_events",
    "observe_school_assignments",
    "project_for_main_self",
    "public_projection",
    "state_path",
    "validate_assignment",
]
