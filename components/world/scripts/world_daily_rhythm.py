#!/usr/bin/env python3
"""Fact-only daily rhythm, availability, and location affordance projection."""

from __future__ import annotations

import os
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

FEATURE_ENV = "CHIYO_WORLD_DAILY_RHYTHM_ENABLED"
WORLD_ID = "shiomi_city"
WORLD_TIMEZONE = "Asia/Tokyo"
SCHEMA_VERSION = "world.daily.rhythm.v1"


@dataclass(frozen=True)
class _LocationRule:
    location_id: str
    open_hour: Optional[int]
    close_hour: Optional[int]
    weekdays_only: bool
    affordances: tuple[tuple[str, str], ...]


_RULES = {
    "home": _LocationRule(
        "home", None, None, False,
        (("REST", "\u5728\u8fd9\u91cc\u4f11\u606f"), ("READ", "\u5728\u8fd9\u91cc\u9605\u8bfb"), ("USE_PHONE", "\u5728\u8fd9\u91cc\u4f7f\u7528\u624b\u673a")),
    ),
    "nearby_street": _LocationRule(
        "nearby_street", None, None, False,
        (("WALK", "\u5728\u9644\u8fd1\u8857\u9053\u6563\u6b65"), ("LOOK_AROUND", "\u5728\u8fd9\u91cc\u770b\u770b\u5468\u56f4")),
    ),
    "school": _LocationRule(
        "school", 8, 18, True,
        (("ATTEND_CLASS", "\u5728\u5b66\u6821\u4e0a\u8bfe"), ("SELF_STUDY", "\u5728\u5b66\u6821\u81ea\u4e60"), ("STAY_AT_SCHOOL", "\u7559\u5728\u5b66\u6821")),
    ),
    "convenience_store": _LocationRule(
        "convenience_store", 7, 23, False,
        (("BROWSE_STORE", "\u5728\u4fbf\u5229\u5e97\u770b\u770b"),),
    ),
    "park": _LocationRule(
        "park", 6, 22, False,
        (("WALK", "\u5728\u516c\u56ed\u6563\u6b65"), ("SIT", "\u5728\u516c\u56ed\u5750\u4e00\u4f1a\u513f"), ("LOOK_AROUND", "\u5728\u8fd9\u91cc\u770b\u770b\u5468\u56f4")),
    ),
    "home_goods_store": _LocationRule(
        "home_goods_store", 10, 20, False,
        (),
    ),
}


class DailyRhythmError(ValueError):
    """Raised for malformed daily-rhythm inputs."""


def _truthy(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def feature_enabled(environment: Optional[Mapping[str, str]] = None) -> bool:
    return _truthy((environment or os.environ).get(FEATURE_ENV))


def _zone(timezone_name: str) -> ZoneInfo:
    if not isinstance(timezone_name, str) or not timezone_name.strip():
        raise DailyRhythmError("timezone_name must be a non-empty string")
    try:
        return ZoneInfo(timezone_name.strip())
    except ZoneInfoNotFoundError as exc:
        raise DailyRhythmError("unknown timezone") from exc


def _local(now: Optional[datetime], timezone_name: str) -> datetime:
    instant = datetime.now(timezone.utc) if now is None else now
    if not isinstance(instant, datetime):
        raise DailyRhythmError("now must be a datetime")
    if instant.tzinfo is None:
        instant = instant.replace(tzinfo=timezone.utc)
    return instant.astimezone(_zone(timezone_name))


def daily_rhythm(
    now: Optional[datetime],
    *,
    timezone_name: str = WORLD_TIMEZONE,
) -> dict[str, Any]:
    local = _local(now, timezone_name)
    if 5 <= local.hour < 12:
        daypart = "morning"
    elif 12 <= local.hour < 18:
        daypart = "daytime"
    elif 18 <= local.hour < 22:
        daypart = "evening"
    else:
        daypart = "night"
    return {
        "schema_version": SCHEMA_VERSION,
        "world_id": WORLD_ID,
        "timezone": timezone_name,
        "local_date": local.date().isoformat(),
        "weekday": local.isoweekday(),
        "day_type": "weekend" if local.weekday() >= 5 else "weekday",
        "daypart": daypart,
    }


def _rule(location_id: str) -> _LocationRule:
    if not isinstance(location_id, str) or location_id not in _RULES:
        raise DailyRhythmError("unknown location_id")
    return _RULES[location_id]


def location_availability(
    location_id: str,
    now: Optional[datetime],
    *,
    timezone_name: str = WORLD_TIMEZONE,
) -> dict[str, str]:
    rule = _rule(location_id)
    local = _local(now, timezone_name)
    if rule.open_hour is None:
        status, reason = "OPEN", "ALWAYS_AVAILABLE"
    elif rule.weekdays_only and local.weekday() >= 5:
        status, reason = "CLOSED", "WEEKEND_CLOSED"
    elif rule.open_hour <= local.hour < (rule.close_hour or 0):
        status, reason = "OPEN", "WITHIN_OPEN_HOURS"
    else:
        status, reason = "CLOSED", "OUTSIDE_OPEN_HOURS"
    return {
        "location_id": rule.location_id,
        "status": status,
        "reason_code": reason,
    }


def affordances_for_location(
    location_id: str,
    now: Optional[datetime],
    *,
    timezone_name: str = WORLD_TIMEZONE,
) -> list[dict[str, str]]:
    rule = _rule(location_id)
    if location_availability(location_id, now, timezone_name=timezone_name)["status"] != "OPEN":
        return []
    return [
        {"affordance_id": affordance_id, "description": description}
        for affordance_id, description in rule.affordances
    ]


def all_location_availability(
    now: Optional[datetime],
    *,
    timezone_name: str = WORLD_TIMEZONE,
    location_ids: Optional[Iterable[str]] = None,
) -> list[dict[str, str]]:
    selected = list(_RULES) if location_ids is None else list(location_ids)
    return [
        location_availability(location_id, now, timezone_name=timezone_name)
        for location_id in selected
    ]


__all__ = [
    "DailyRhythmError",
    "FEATURE_ENV",
    "SCHEMA_VERSION",
    "WORLD_ID",
    "WORLD_TIMEZONE",
    "affordances_for_location",
    "all_location_availability",
    "daily_rhythm",
    "feature_enabled",
    "location_availability",
]
