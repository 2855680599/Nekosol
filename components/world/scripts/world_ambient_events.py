#!/usr/bin/env python3
"""Deterministic, location-scoped ambient facts for the Shiomi World.

The daily set is materialized from the authoritative World local date. This
module deliberately has no store, Life authority, or background runner: an
observation can reconstruct the same bounded facts after a restart, while an
unobserved past day naturally expires.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


FEATURE_ENV = "CHIYO_WORLD_AMBIENT_EVENTS_ENABLED"
WORLD_ID = "shiomi_city"
WORLD_TIMEZONE = "Asia/Tokyo"
SCHEMA_VERSION = "world.ambient.event.v1"
MAX_EVENTS_PER_DAY = 3
MAX_SUMMARY_LENGTH = 240

KNOWN_LOCATION_IDS = frozenset(
    {"home", "nearby_street", "school", "convenience_store", "park", "home_goods_store"}
)
_EVENT_FIELDS = frozenset(
    {
        "event_id",
        "world_id",
        "local_date",
        "location_id",
        "event_type",
        "payload",
        "valid_from",
        "valid_until",
        "provenance",
    }
)
_FORBIDDEN_FIELDS = frozenset(
    {
        "priority",
        "importance",
        "interest_score",
        "chiyo_should_act",
        "reward",
        "penalty",
        "quest",
        "goal",
        "emotion_effect",
    }
)


class AmbientWorldEventError(ValueError):
    """Raised when an ambient event contract is malformed."""


@dataclass(frozen=True)
class _Template:
    event_type: str
    location_id: str
    summary: str
    start_hour: int
    end_hour: int


_TEMPLATES = (
    _Template("school_notice", "school", "今天公告栏贴了文化祭筹备通知", 0, 24),
    _Template("park_maintenance", "park", "草坪一部分正在维护", 8, 18),
    _Template(
        "convenience_store_promotion",
        "convenience_store",
        "晚间饭团打折",
        18,
        22,
    ),
    _Template("street_construction", "nearby_street", "路边正在修排水管", 6, 20),
    _Template("home_window_sound", "home", "窗外雨声变大", 0, 24),
)


def _copy_json(value: Any) -> Any:
    try:
        return json.loads(
            json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
        )
    except (TypeError, ValueError) as exc:
        raise AmbientWorldEventError("ambient event is not bounded JSON") from exc


def _truthy(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def feature_enabled(environment: Optional[Mapping[str, str]] = None) -> bool:
    return _truthy((environment or os.environ).get(FEATURE_ENV))


def _require_date(value: Any) -> date:
    if isinstance(value, datetime) or not isinstance(value, date):
        raise AmbientWorldEventError("local_date must be a date")
    return value


def _zone(timezone_name: str) -> ZoneInfo:
    if not isinstance(timezone_name, str) or not timezone_name.strip():
        raise AmbientWorldEventError("timezone_name is invalid")
    try:
        return ZoneInfo(timezone_name.strip())
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise AmbientWorldEventError("timezone_name is invalid") from exc


def _bounded_summary(value: Any) -> str:
    if not isinstance(value, str):
        raise AmbientWorldEventError("event summary must be text")
    result = value.strip()
    if not result or len(result) > MAX_SUMMARY_LENGTH:
        raise AmbientWorldEventError("event summary is outside its bound")
    return result


def _boundary(local_date: date, hour: int, zone: ZoneInfo) -> datetime:
    if hour == 24:
        local_date = local_date + timedelta(days=1)
        hour = 0
    if not isinstance(hour, int) or not 0 <= hour <= 23:
        raise AmbientWorldEventError("event boundary is invalid")
    return datetime.combine(local_date, time(hour=hour), tzinfo=zone)


def _build_event(template: _Template, local_date: date, zone: ZoneInfo) -> dict[str, Any]:
    valid_from = _boundary(local_date, template.start_hour, zone)
    valid_until = _boundary(local_date, template.end_hour, zone)
    event = {
        "event_id": f"ambient-{local_date.isoformat()}-{template.event_type}",
        "world_id": WORLD_ID,
        "local_date": local_date.isoformat(),
        "location_id": template.location_id,
        "event_type": template.event_type,
        "payload": {"summary": template.summary},
        "valid_from": valid_from.isoformat(timespec="seconds"),
        "valid_until": valid_until.isoformat(timespec="seconds"),
        "provenance": "world_local_day_deterministic_v1",
    }
    return _validate_event(event, zone)


def _validate_event(event: Mapping[str, Any], zone: ZoneInfo) -> dict[str, Any]:
    if not isinstance(event, Mapping) or set(event) != _EVENT_FIELDS:
        raise AmbientWorldEventError("ambient event has an invalid schema")
    if not isinstance(event["event_id"], str) or not event["event_id"].strip():
        raise AmbientWorldEventError("event_id is invalid")
    if event["world_id"] != WORLD_ID:
        raise AmbientWorldEventError("ambient event world mismatch")
    try:
        parsed_date = date.fromisoformat(event["local_date"])
    except (TypeError, ValueError) as exc:
        raise AmbientWorldEventError("local_date is invalid") from exc
    if event["location_id"] not in KNOWN_LOCATION_IDS:
        raise AmbientWorldEventError("ambient event location is invalid")
    if not isinstance(event["event_type"], str) or not event["event_type"].strip():
        raise AmbientWorldEventError("event_type is invalid")
    payload = event["payload"]
    if not isinstance(payload, Mapping) or set(payload) != {"summary"}:
        raise AmbientWorldEventError("ambient payload is not bounded factual data")
    _bounded_summary(payload["summary"])
    if not isinstance(event["provenance"], str) or not event["provenance"].strip():
        raise AmbientWorldEventError("provenance is invalid")
    try:
        valid_from = datetime.fromisoformat(event["valid_from"])
        valid_until = datetime.fromisoformat(event["valid_until"])
    except (TypeError, ValueError) as exc:
        raise AmbientWorldEventError("event validity window is invalid") from exc
    if (
        valid_from.tzinfo is None
        or valid_from.utcoffset() is None
        or valid_until.tzinfo is None
        or valid_until.utcoffset() is None
        or valid_from >= valid_until
        or valid_from.astimezone(zone).date() != parsed_date
        or valid_until.astimezone(zone).date()
        not in {parsed_date, parsed_date + timedelta(days=1)}
    ):
        raise AmbientWorldEventError("event validity window is inconsistent")
    if _FORBIDDEN_FIELDS.intersection(event) or _FORBIDDEN_FIELDS.intersection(payload):
        raise AmbientWorldEventError("ambient event contains a forbidden authority field")
    return _copy_json(dict(event))


def materialize_ambient_events(
    local_date: date,
    *,
    timezone_name: str = WORLD_TIMEZONE,
    allowed_location_ids: Optional[Iterable[str]] = None,
) -> list[dict[str, Any]]:
    """Derive the stable bounded event set for one World-local calendar day."""

    day = _require_date(local_date)
    zone = _zone(timezone_name)
    allowed = KNOWN_LOCATION_IDS if allowed_location_ids is None else set(allowed_location_ids)
    if not KNOWN_LOCATION_IDS.issubset(allowed):
        raise AmbientWorldEventError("ambient template location catalog is incomplete")
    start = day.toordinal() % len(_TEMPLATES)
    selected = [
        _TEMPLATES[(start + offset) % len(_TEMPLATES)]
        for offset in range(MAX_EVENTS_PER_DAY)
    ]
    events = [_build_event(template, day, zone) for template in selected]
    if len(events) > MAX_EVENTS_PER_DAY:
        raise AmbientWorldEventError("daily ambient event bound exceeded")
    return _copy_json(events)


def active_ambient_events(
    local_date: date,
    now: datetime,
    *,
    timezone_name: str = WORLD_TIMEZONE,
    allowed_location_ids: Optional[Iterable[str]] = None,
    relevant_location_ids: Optional[Iterable[str]] = None,
) -> list[dict[str, Any]]:
    """Return only current, location-relevant facts for one local day."""

    day = _require_date(local_date)
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise AmbientWorldEventError("now must be timezone-aware")
    zone = _zone(timezone_name)
    local_now = now.astimezone(zone)
    if local_now.date() != day:
        return []
    relevant = None
    if relevant_location_ids is not None:
        if isinstance(relevant_location_ids, (str, bytes)):
            raise AmbientWorldEventError("relevant_location_ids must be an iterable")
        relevant = set(relevant_location_ids)
        if not relevant.issubset(KNOWN_LOCATION_IDS):
            raise AmbientWorldEventError("relevant location is invalid")
    result: list[dict[str, Any]] = []
    for event in materialize_ambient_events(
        day,
        timezone_name=timezone_name,
        allowed_location_ids=allowed_location_ids,
    ):
        if relevant is not None and event["location_id"] not in relevant:
            continue
        valid_from = datetime.fromisoformat(event["valid_from"])
        valid_until = datetime.fromisoformat(event["valid_until"])
        if valid_from <= local_now < valid_until:
            result.append(event)
    return _copy_json(result)


__all__ = [
    "AmbientWorldEventError",
    "FEATURE_ENV",
    "KNOWN_LOCATION_IDS",
    "MAX_EVENTS_PER_DAY",
    "SCHEMA_VERSION",
    "WORLD_ID",
    "WORLD_TIMEZONE",
    "active_ambient_events",
    "feature_enabled",
    "materialize_ambient_events",
]
