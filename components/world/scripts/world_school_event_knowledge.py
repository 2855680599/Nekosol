"""Durable, bounded knowledge for explicitly perceived Shiomi school events.

World event facts and exposure eligibility remain owned by
``world_school_events``.  This module records knowledge only when an existing
verified LOOK_WORLD perception supplies an explicit reference.  It does not
infer noticing from presence, time, or event existence.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections.abc import Mapping, Sequence
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

try:
    import fcntl
except ImportError:  # pragma: no cover - production is POSIX
    fcntl = None

import world_school_events as school_events


FEATURE_ENV = "CHIYO_WORLD_SCHOOL_EVENT_KNOWLEDGE_V1_ENABLED"
WORLD_ID = school_events.WORLD_ID
WORLD_TIMEZONE = school_events.WORLD_TIMEZONE
SCHEMA_VERSION = "world.school.event.knowledge.v1"
STATE_SCHEMA_VERSION = "world.school.event.knowledge.state.v1"
PROJECTION_KIND = "shiomi_school_event_knowledge"
STATE_KIND = "shiomi_school_event_knowledge_state"
STATE_FILENAME = "school_event_knowledge.json"
OBSERVATION_GRACE_SECONDS = 15 * 60
MAX_ITEMS = 64
MAX_REFERENCE_LENGTH = 256
MAX_STATE_BYTES = 20_000
MAX_PROJECTION_BYTES = 8_000

KNOWLEDGE_STATUSES = frozenset(
    {"UNKNOWN", "NOT_EXPOSED", "EXPOSURE_POSSIBLE", "OBSERVED", "REPORTED_LATER"}
)
SOURCE_CLASSES = frozenset(
    {"FIRST_HAND_PERCEPTION", "INSTITUTION_NOTICE", "REPORTED_SOURCE", "UNKNOWN"}
)
REPORT_SOURCES = {
    "school_notice": "INSTITUTION_NOTICE",
    "teacher": "REPORTED_SOURCE",
    "classmate": "REPORTED_SOURCE",
    "phone_local_messaging": "REPORTED_SOURCE",
}
_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
_ESTABLISHED_STATUSES = frozenset({"OBSERVED", "REPORTED_LATER"})
_STATE_KEYS = frozenset(
    {"kind", "schema_version", "world_id", "revision", "knowledge_started_at", "items"}
)
_ITEM_KEYS = frozenset(
    {
        "knowledge_id",
        "event_id",
        "knowledge_status",
        "learned_at",
        "source_class",
        "perception_ref",
        "revision",
    }
)


class SchoolEventKnowledgeError(ValueError):
    """Raised for malformed knowledge input or durable state."""


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
        raise SchoolEventKnowledgeError("value is not bounded JSON") from exc
    if len(encoded) > maximum:
        raise SchoolEventKnowledgeError("value exceeds bounded JSON size")
    return json.loads(encoded)


def _environment(environment: Optional[Mapping[str, str]]) -> Mapping[str, str]:
    return os.environ if environment is None else environment


def _truthy(value: Any) -> bool:
    return str(value or "").strip().lower() in _TRUE_VALUES


def feature_enabled(environment: Optional[Mapping[str, str]] = None) -> bool:
    return _truthy(_environment(environment).get(FEATURE_ENV))


def _home(hermes_home: Optional[Path | str] = None) -> Path:
    raw = hermes_home if hermes_home is not None else os.environ.get("HERMES_HOME")
    path = Path(raw).expanduser() if raw else Path.home() / ".hermes"
    if not path.is_absolute():
        raise SchoolEventKnowledgeError("hermes_home must be absolute")
    return path


def state_path(hermes_home: Optional[Path | str] = None) -> Path:
    return _home(hermes_home) / "data" / "world" / STATE_FILENAME


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
            raise SchoolEventKnowledgeError(f"{field} must be ISO datetime") from exc
    else:
        raise SchoolEventKnowledgeError(f"{field} must be ISO datetime")
    if instant.tzinfo is None or instant.utcoffset() is None:
        raise SchoolEventKnowledgeError(f"{field} must include timezone")
    return instant.astimezone(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def _instant(value: Any, field: str = "timestamp") -> datetime:
    return datetime.fromisoformat(_timestamp(value, field).replace("Z", "+00:00"))


def _text(value: Any, field: str, maximum: int = MAX_REFERENCE_LENGTH) -> str:
    if not isinstance(value, str):
        raise SchoolEventKnowledgeError(f"{field} must be a string")
    result = value.strip()
    if (
        not result
        or len(result) > maximum
        or any(char in result for char in "\r\n\x00")
        or any(char.isspace() for char in result)
    ):
        raise SchoolEventKnowledgeError(f"{field} is invalid")
    return result


def _revision(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise SchoolEventKnowledgeError(f"{field} must be a positive integer")
    return value


def _knowledge_id(event_id: str) -> str:
    digest = hashlib.sha256(event_id.encode("utf-8")).hexdigest()
    return f"school-knowledge-{digest}"


def _validate_item(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != _ITEM_KEYS:
        raise SchoolEventKnowledgeError("knowledge item has an invalid schema")
    event_id = _text(value.get("event_id"), "event_id", 200)
    knowledge_id = _text(value.get("knowledge_id"), "knowledge_id", 240)
    if knowledge_id != _knowledge_id(event_id):
        raise SchoolEventKnowledgeError("knowledge identity is not stable")
    status = _text(value.get("knowledge_status"), "knowledge_status", 40).upper()
    if status not in _ESTABLISHED_STATUSES:
        raise SchoolEventKnowledgeError("knowledge status is not established")
    learned_at = _timestamp(value.get("learned_at"), "learned_at")
    source_class = _text(value.get("source_class"), "source_class", 64).upper()
    if source_class not in SOURCE_CLASSES:
        raise SchoolEventKnowledgeError("source class is unsupported")
    if status == "OBSERVED" and source_class != "FIRST_HAND_PERCEPTION":
        raise SchoolEventKnowledgeError("observed knowledge needs first-hand provenance")
    if status == "REPORTED_LATER" and source_class == "FIRST_HAND_PERCEPTION":
        raise SchoolEventKnowledgeError("reported knowledge needs report provenance")
    perception_ref = _text(value.get("perception_ref"), "perception_ref")
    revision = _revision(value.get("revision"), "revision")
    return {
        "knowledge_id": knowledge_id,
        "event_id": event_id,
        "knowledge_status": status,
        "learned_at": learned_at,
        "source_class": source_class,
        "perception_ref": perception_ref,
        "revision": revision,
    }


def validate_state(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != _STATE_KEYS:
        raise SchoolEventKnowledgeError("knowledge state has an invalid schema")
    if value.get("kind") != STATE_KIND or value.get("schema_version") != STATE_SCHEMA_VERSION:
        raise SchoolEventKnowledgeError("knowledge state identity is invalid")
    if value.get("world_id") != WORLD_ID:
        raise SchoolEventKnowledgeError("knowledge state world is invalid")
    revision = _revision(value.get("revision"), "revision")
    started = _timestamp(value.get("knowledge_started_at"), "knowledge_started_at")
    raw_items = value.get("items")
    if not isinstance(raw_items, list) or len(raw_items) > MAX_ITEMS:
        raise SchoolEventKnowledgeError("knowledge items are invalid")
    items = [_validate_item(item) for item in raw_items]
    event_ids = [item["event_id"] for item in items]
    if len(event_ids) != len(set(event_ids)):
        raise SchoolEventKnowledgeError("knowledge items must be unique by event")
    return {
        "kind": STATE_KIND,
        "schema_version": STATE_SCHEMA_VERSION,
        "world_id": WORLD_ID,
        "revision": revision,
        "knowledge_started_at": started,
        "items": items,
    }


def load_state(hermes_home: Optional[Path | str] = None) -> Optional[dict[str, Any]]:
    path = state_path(hermes_home)
    try:
        return validate_state(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, TypeError, ValueError, json.JSONDecodeError, SchoolEventKnowledgeError):
        return None


def _save(path: Path, state: Mapping[str, Any]) -> dict[str, Any]:
    normalized = validate_state(state)
    payload = json.dumps(
        normalized,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ) + "\n"
    if len(payload.encode("utf-8")) > MAX_STATE_BYTES:
        raise SchoolEventKnowledgeError("knowledge state is too large")
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
        raise SchoolEventKnowledgeError("knowledge state lock unavailable")
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(path.name + ".lock")
    with lock_path.open("a+") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _empty_state(started_at: str) -> dict[str, Any]:
    return {
        "kind": STATE_KIND,
        "schema_version": STATE_SCHEMA_VERSION,
        "world_id": WORLD_ID,
        "revision": 1,
        "knowledge_started_at": _timestamp(started_at, "knowledge_started_at"),
        "items": [],
    }


def _load_for_update(path: Path, home: Path, started_at: Any) -> dict[str, Any]:
    if not path.exists():
        return _empty_state(_timestamp(started_at, "knowledge_started_at"))
    value = load_state(home)
    if value is None:
        raise SchoolEventKnowledgeError("knowledge state is corrupt")
    return value


def _events_from_projection(projection: Any) -> tuple[dict[str, Any], ...]:
    if not isinstance(projection, Mapping) or projection.get("status") != "available":
        return ()
    raw_events = projection.get("events")
    if not isinstance(raw_events, list) or len(raw_events) > school_events.MAX_EVENTS_PER_DAY:
        raise SchoolEventKnowledgeError("event projection is invalid")
    return tuple(school_events.validate_event(item) for item in raw_events)


def _exposure_by_event(projection: Mapping[str, Any]) -> dict[str, str]:
    raw_exposure = projection.get("exposure")
    if not isinstance(raw_exposure, list):
        return {}
    result: dict[str, str] = {}
    for item in raw_exposure:
        if not isinstance(item, Mapping):
            continue
        event_id = item.get("event_id")
        status = item.get("status")
        if isinstance(event_id, str) and isinstance(status, str):
            result[event_id] = status
    return result


def _in_observation_window(
    event: Mapping[str, Any],
    *,
    now: datetime,
    knowledge_started_at: datetime,
) -> bool:
    interval = event.get("interval")
    if not isinstance(interval, Mapping):
        return False
    try:
        starts_at = _instant(interval.get("starts_at"), "interval.starts_at")
        ends_at = _instant(interval.get("ends_at"), "interval.ends_at")
    except SchoolEventKnowledgeError:
        return False
    if now < starts_at:
        return False
    if ends_at <= knowledge_started_at:
        return False
    return now <= ends_at + timedelta(seconds=OBSERVATION_GRACE_SECONDS)


def _room_is_proven(event: Mapping[str, Any], observer_subspace_id: Optional[str]) -> bool:
    if event.get("event_kind") != "CLASSROOM_CHANGE":
        return True
    facts = event.get("facts")
    effective = facts.get("effective_location_id") if isinstance(facts, Mapping) else None
    return isinstance(effective, str) and observer_subspace_id == effective


def _add_item(
    state: dict[str, Any],
    *,
    event_id: str,
    status: str,
    learned_at: str,
    source_class: str,
    perception_ref: str,
) -> tuple[dict[str, Any], bool]:
    for existing in state["items"]:
        if existing["event_id"] != event_id:
            continue
        if existing["knowledge_status"] == "OBSERVED" or status == existing["knowledge_status"]:
            return existing, False
        updated = dict(existing)
        updated.update(
            {
                "knowledge_status": status,
                "learned_at": learned_at,
                "source_class": source_class,
                "perception_ref": perception_ref,
                "revision": existing["revision"] + 1,
            }
        )
        state["items"] = [
            updated if item["event_id"] == event_id else item for item in state["items"]
        ]
        state["revision"] += 1
        return updated, True
    if len(state["items"]) >= MAX_ITEMS:
        raise SchoolEventKnowledgeError("knowledge item bound exceeded")
    item = {
        "knowledge_id": _knowledge_id(event_id),
        "event_id": event_id,
        "knowledge_status": status,
        "learned_at": learned_at,
        "source_class": source_class,
        "perception_ref": perception_ref,
        "revision": 1,
    }
    item = _validate_item(item)
    state["items"].append(item)
    state["revision"] += 1
    return item, True


def record_first_hand_perception(
    event_projection: Mapping[str, Any],
    *,
    hermes_home: Optional[Path | str] = None,
    now: Optional[datetime] = None,
    perception_ref: Any,
    observer_subspace_id: Optional[str] = None,
    environment: Optional[Mapping[str, str]] = None,
) -> dict[str, Any]:
    """Record only events explicitly perceived through verified LOOK_WORLD."""

    if not feature_enabled(environment):
        return {"status": "disabled", "reason_code": "FEATURE_DISABLED", "items": []}
    try:
        reference = _text(perception_ref, "perception_ref")
        if not reference.startswith("LOOK_WORLD:"):
            return {"status": "no_op", "reason_code": "PERCEPTION_AUTHORITY_REQUIRED", "items": []}
        instant = datetime.now(timezone.utc) if now is None else now
        if instant.tzinfo is None or instant.utcoffset() is None:
            raise SchoolEventKnowledgeError("now must be timezone-aware")
        projection = event_projection
        events = _events_from_projection(projection)
        exposure = _exposure_by_event(projection)
        home = _home(hermes_home)
        path = state_path(home)
        learned_at = _timestamp(instant, "learned_at")
        with _lock(path):
            existed = path.exists()
            state = _load_for_update(path, home, learned_at)
            started = _instant(state["knowledge_started_at"], "knowledge_started_at")
            added: list[dict[str, Any]] = []
            for event in events:
                event_id = event["event_id"]
                if exposure.get(event_id) != "EXPOSURE_POSSIBLE":
                    continue
                if not _in_observation_window(
                    event,
                    now=instant.astimezone(timezone.utc),
                    knowledge_started_at=started,
                ):
                    continue
                if not _room_is_proven(event, observer_subspace_id):
                    continue
                item, changed = _add_item(
                    state,
                    event_id=event_id,
                    status="OBSERVED",
                    learned_at=learned_at,
                    source_class="FIRST_HAND_PERCEPTION",
                    perception_ref=reference,
                )
                if changed:
                    added.append(item)
            if added or not existed:
                _save(path, state)
            return {
                "status": "observed" if added else "no_op",
                "reason_code": (
                    None
                    if added
                    else ("NO_EVENT_FACTS" if not events else "NO_LEGITIMATE_OBSERVATION")
                ),
                "items": _copy_json(added, maximum=MAX_PROJECTION_BYTES),
            }
    except (OSError, TypeError, ValueError, SchoolEventKnowledgeError):
        return {"status": "unavailable", "reason_code": "KNOWLEDGE_UNAVAILABLE", "items": []}


def record_reported_later(
    event: Mapping[str, Any],
    *,
    report_source: Any,
    source_ref: Any,
    hermes_home: Optional[Path | str] = None,
    learned_at: Optional[datetime] = None,
    environment: Optional[Mapping[str, str]] = None,
) -> dict[str, Any]:
    """Provide a bounded future report seam without implementing gossip."""

    if not feature_enabled(environment):
        return {"status": "disabled", "reason_code": "FEATURE_DISABLED", "item": None}
    try:
        source = _text(report_source, "report_source", 64)
        source_class = REPORT_SOURCES.get(source)
        if source_class is None:
            return {"status": "no_op", "reason_code": "REPORT_SOURCE_NOT_ALLOWED", "item": None}
        reference = _text(source_ref, "source_ref")
        normalized_event = school_events.validate_event(event)
        instant = datetime.now(timezone.utc) if learned_at is None else learned_at
        if instant.tzinfo is None or instant.utcoffset() is None:
            raise SchoolEventKnowledgeError("learned_at must be timezone-aware")
        home = _home(hermes_home)
        path = state_path(home)
        learned = _timestamp(instant, "learned_at")
        with _lock(path):
            existed = path.exists()
            state = _load_for_update(path, home, learned)
            item, changed = _add_item(
                state,
                event_id=normalized_event["event_id"],
                status="REPORTED_LATER",
                learned_at=learned,
                source_class=source_class,
                perception_ref=f"REPORT:{reference}",
            )
            if changed or not existed:
                _save(path, state)
            return {
                "status": "reported" if changed else "already_known",
                "reason_code": None if changed else "KNOWLEDGE_ALREADY_EXISTS",
                "item": _copy_json(item, maximum=2_000),
            }
    except (OSError, TypeError, ValueError, SchoolEventKnowledgeError):
        return {"status": "unavailable", "reason_code": "KNOWLEDGE_UNAVAILABLE", "item": None}


def project_for_main_self(
    world_projection: Mapping[str, Any],
    *,
    hermes_home: Optional[Path | str] = None,
    environment: Optional[Mapping[str, str]] = None,
) -> dict[str, Any]:
    """Replace raw school-event facts with only established bounded knowledge."""

    result = _copy_json(world_projection, maximum=16_000)
    if not feature_enabled(environment):
        return result
    raw_projection = result.pop("school_events", None)
    if not isinstance(raw_projection, Mapping):
        return result
    try:
        events = _events_from_projection(raw_projection)
        state = load_state(hermes_home)
        if not events or state is None:
            return result
        known = {
            item["event_id"]: item
            for item in state["items"]
            if item["knowledge_status"] in _ESTABLISHED_STATUSES
        }
        projected: list[dict[str, Any]] = []
        for event in events:
            if event["event_id"] not in known:
                continue
            projected.append(
                {
                    "event_kind": event["event_kind"],
                    "occurred_at": event["occurred_at"],
                    "location": event["location"],
                    "facts": event["facts"],
                }
            )
        if projected:
            result["school_event_knowledge"] = {"events": projected}
        return _copy_json(result, maximum=16_000)
    except (TypeError, ValueError, SchoolEventKnowledgeError):
        return result


__all__ = [
    "FEATURE_ENV",
    "KNOWLEDGE_STATUSES",
    "MAX_ITEMS",
    "OBSERVATION_GRACE_SECONDS",
    "PROJECTION_KIND",
    "REPORT_SOURCES",
    "SCHEMA_VERSION",
    "SOURCE_CLASSES",
    "STATE_FILENAME",
    "SchoolEventKnowledgeError",
    "feature_enabled",
    "load_state",
    "project_for_main_self",
    "record_first_hand_perception",
    "record_reported_later",
    "state_path",
    "validate_state",
]
