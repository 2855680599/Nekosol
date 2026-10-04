#!/usr/bin/env python3
"""Deterministic, public-only Shiomi City Social Presence Skeleton V1.

NPC identity and ordinary presence are World facts.  This module has no
dependency on Chiyo state, writes no state, and never creates interaction,
relationship, Life, wake, or messaging side effects.
"""

from __future__ import annotations

import copy
import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional


FEATURE_ENV = "CHIYO_WORLD_SOCIAL_PRESENCE_ENABLED"
WORLD_ID = "shiomi_city"
SCHEMA_VERSION = "world.social.presence.v1"
PROJECTION_KIND = "shiomi_social_presence"
MAX_PUBLIC_TEXT_LENGTH = 160
MAX_PRESENCE_COUNT = 3
_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})


class SocialPresenceError(ValueError):
    """Raised for malformed bounded Social Presence input."""


@dataclass(frozen=True)
class _PresenceWindow:
    weekdays: frozenset[int]
    start_minute: int
    end_minute: int
    location_id: str

    def active(self, now: datetime) -> bool:
        minute = now.hour * 60 + now.minute
        return now.isoweekday() in self.weekdays and self.start_minute <= minute < self.end_minute


@dataclass(frozen=True)
class _Npc:
    npc_id: str
    display_name: str
    age_classification: str
    public_role: str
    home_base_area: str
    public_description: str
    windows: tuple[_PresenceWindow, ...]


# Fictional public identities only.  No personality, relationship, private,
# memory, emotion, or desire fields are part of this schema.
_NPCS = (
    _Npc(
        npc_id="shiomi_npc_mio_shiraishi",
        display_name="白石美绪",
        age_classification="minor",
        public_role="同班同学",
        home_base_area="school",
        public_description="汐见市学校的同班同学。",
        windows=(
            _PresenceWindow(frozenset({1, 2, 3, 4, 5}), 8 * 60, 16 * 60, "school"),
        ),
    ),
    _Npc(
        npc_id="shiomi_npc_rin_yamamoto",
        display_name="山本凛",
        age_classification="minor",
        public_role="学校里的学生",
        home_base_area="school",
        public_description="汐见市学校里的学生。",
        windows=(
            _PresenceWindow(frozenset({1, 2, 3, 4, 5}), 12 * 60, 18 * 60, "school"),
        ),
    ),
    _Npc(
        npc_id="shiomi_npc_yuki_takahashi",
        display_name="高桥由纪",
        age_classification="adult",
        public_role="便利店店员",
        home_base_area="convenience_store",
        public_description="汐见市便利店的值班店员。",
        windows=(
            _PresenceWindow(frozenset({1, 2, 3, 4, 5, 6, 7}), 9 * 60, 22 * 60, "convenience_store"),
        ),
    ),
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
        raise SocialPresenceError("value is not bounded JSON") from exc


def _environment(environment: Optional[Mapping[str, str]]) -> Mapping[str, str]:
    return os.environ if environment is None else environment


def _truthy(value: Any) -> bool:
    return str(value or "").strip().lower() in _TRUE_VALUES


def feature_enabled(environment: Optional[Mapping[str, str]] = None) -> bool:
    return _truthy(_environment(environment).get(FEATURE_ENV))


def _location_id(value: Any) -> str:
    if not isinstance(value, str):
        raise SocialPresenceError("location_id must be a string")
    result = value.strip()
    if not result or len(result) > 96 or any(char.isspace() for char in result):
        raise SocialPresenceError("location_id is invalid")
    return result


def _resolved_time(value: Optional[datetime]) -> datetime:
    now = datetime.now(timezone.utc) if value is None else value
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise SocialPresenceError("now must be timezone-aware")
    return now


def _public_identity(npc: _Npc) -> dict[str, str]:
    return {
        "npc_id": npc.npc_id,
        "display_name": npc.display_name,
        "age_classification": npc.age_classification,
        "public_role": npc.public_role,
        "home_base_area": npc.home_base_area,
        "public_description": npc.public_description,
    }


def npc_catalog() -> list[dict[str, str]]:
    """Return canonical public identities for deterministic tests/admin views."""

    return [_public_identity(npc) for npc in _NPCS]


def _active_window(npc: _Npc, now: datetime, location_id: str) -> Optional[_PresenceWindow]:
    for window in npc.windows:
        if window.location_id == location_id and window.active(now):
            return window
    return None


def present_npcs(
    location_id: Any,
    *,
    now: Optional[datetime] = None,
) -> list[dict[str, Any]]:
    """Return only public NPC presence at the supplied current location."""

    location = _location_id(location_id)
    instant = _resolved_time(now)
    result: list[dict[str, Any]] = []
    for npc in _NPCS:
        if _active_window(npc, instant, location) is None:
            continue
        result.append(
            {
                "npc_id": npc.npc_id,
                "display_name": npc.display_name,
                "age_classification": npc.age_classification,
                "public_role": npc.public_role,
                "location_id": location,
                "presence": "present",
                "public_description": npc.public_description,
            }
        )
        if len(result) >= MAX_PRESENCE_COUNT:
            break
    return _copy_json(result)


def observe_social_presence(
    location_id: Any,
    *,
    now: Optional[datetime] = None,
    environment: Optional[Mapping[str, str]] = None,
) -> dict[str, Any]:
    """Project current public presence; never writes and never initiates contact."""

    if not feature_enabled(environment):
        return {
            "kind": PROJECTION_KIND,
            "schema_version": SCHEMA_VERSION,
            "status": "disabled",
            "reason_code": "FEATURE_DISABLED",
        }
    try:
        location = _location_id(location_id)
        instant = _resolved_time(now)
        present = present_npcs(location, now=instant)
    except SocialPresenceError:
        return {
            "kind": PROJECTION_KIND,
            "schema_version": SCHEMA_VERSION,
            "status": "unavailable",
            "reason_code": "INVALID_PRESENCE_INPUT",
        }
    result = {
        "kind": PROJECTION_KIND,
        "schema_version": SCHEMA_VERSION,
        "status": "available",
        "world_id": WORLD_ID,
        "location_id": location,
        "presence_date": instant.date().isoformat(),
        "presence_time": instant.strftime("%H:%M"),
        "present_npcs": present,
        "encounter_possible": bool(present),
    }
    encoded = json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    if len(encoded) > 8_000:
        return {
            "kind": PROJECTION_KIND,
            "schema_version": SCHEMA_VERSION,
            "status": "unavailable",
            "reason_code": "PROJECTION_BOUNDS_EXCEEDED",
        }
    return _copy_json(result)


__all__ = [
    "FEATURE_ENV",
    "MAX_PRESENCE_COUNT",
    "PROJECTION_KIND",
    "SCHEMA_VERSION",
    "SocialPresenceError",
    "WORLD_ID",
    "feature_enabled",
    "npc_catalog",
    "observe_social_presence",
    "present_npcs",
]
