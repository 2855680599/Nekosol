#!/usr/bin/env python3
"""Bounded World State, Scene State, and Perception contracts.

The module is intentionally standalone.  A caller must inject a store path
and explicitly project a perception; importing it has no runtime effect.
"""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


WORLD_SCHEMA_VERSION = "world.foundation.v0"
PERCEPTION_SCHEMA_VERSION = "world.perception.v0"
MAX_FACTS = 256
MAX_FACT_CHANNELS = 8
MAX_AVAILABLE_CHANNELS = 16
MAX_ATTENTION_BUDGET = 32
MAX_TEXT_LENGTH = 240
MAX_ID_LENGTH = 128

_WORLD_KEYS = {
    "schema_version",
    "revision",
    "world_id",
    "timezone",
    "location",
    "scene",
    "facts",
}
_LOCATION_KEYS = {"place_id", "area_id"}
_SCENE_KEYS = {"scene_id", "revision"}
_FACT_KEYS = {
    "fact_id",
    "kind",
    "location_id",
    "channels",
    "perceivable",
    "noticeability",
    "percept",
}
_PERCEPT_KEYS = {"summary"}


class WorldFoundationError(ValueError):
    """Raised when a caller supplies an invalid V0 contract."""


def _require_text(value: Any, *, field: str, maximum: int = MAX_TEXT_LENGTH) -> str:
    if not isinstance(value, str):
        raise WorldFoundationError(f"{field} must be a string")
    result = value.strip()
    if not result or len(result) > maximum:
        raise WorldFoundationError(f"{field} must be non-empty and bounded")
    return result


def _require_revision(value: Any, *, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise WorldFoundationError(f"{field} must be a non-negative integer")
    return value


def _require_object(value: Any, *, field: str, expected_fields: set[str]) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != expected_fields:
        raise WorldFoundationError(f"{field} must have exactly the V0 fields")
    return value


def _canonical_copy(value: Any) -> Any:
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
        raise WorldFoundationError("value is not bounded JSON") from exc


def _validate_timezone(value: Any) -> str:
    name = _require_text(value, field="timezone", maximum=128)
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise WorldFoundationError("timezone is not a valid IANA timezone") from exc
    return name


def _validate_channels(value: Any, *, field: str, maximum: int) -> list[str]:
    if not isinstance(value, list) or not value or len(value) > maximum:
        raise WorldFoundationError(f"{field} must be a bounded non-empty list")
    result: list[str] = []
    for index, channel in enumerate(value):
        normalized = _require_text(channel, field=f"{field}[{index}]", maximum=64)
        if normalized in result:
            raise WorldFoundationError(f"{field} must not contain duplicates")
        result.append(normalized)
    return result


def validate_world_state(state: Mapping[str, Any]) -> dict[str, Any]:
    """Validate and return a detached, normalized World State V0."""

    if not isinstance(state, Mapping) or set(state) != _WORLD_KEYS:
        raise WorldFoundationError("world state must have exactly the V0 fields")

    location = _require_object(
        state["location"], field="location", expected_fields=_LOCATION_KEYS
    )
    scene = _require_object(state["scene"], field="scene", expected_fields=_SCENE_KEYS)
    facts = state["facts"]
    if not isinstance(facts, list) or len(facts) > MAX_FACTS:
        raise WorldFoundationError("facts must be a bounded list")

    normalized_facts: list[dict[str, Any]] = []
    fact_ids: set[str] = set()
    for index, fact in enumerate(facts):
        if not isinstance(fact, Mapping) or set(fact) != _FACT_KEYS:
            raise WorldFoundationError(f"facts[{index}] has an invalid V0 shape")
        fact_id = _require_text(fact["fact_id"], field=f"facts[{index}].fact_id", maximum=MAX_ID_LENGTH)
        if fact_id in fact_ids:
            raise WorldFoundationError("fact_id must be unique within a World State")
        fact_ids.add(fact_id)
        kind = _require_text(fact["kind"], field=f"facts[{index}].kind", maximum=80)
        location_id = _require_text(
            fact["location_id"], field=f"facts[{index}].location_id", maximum=MAX_ID_LENGTH
        )
        channels = _validate_channels(
            fact["channels"], field=f"facts[{index}].channels", maximum=MAX_FACT_CHANNELS
        )
        if not isinstance(fact["perceivable"], bool):
            raise WorldFoundationError(f"facts[{index}].perceivable must be boolean")
        noticeability = fact["noticeability"]
        if (
            isinstance(noticeability, bool)
            or not isinstance(noticeability, int)
            or not 0 <= noticeability <= 100
        ):
            raise WorldFoundationError(f"facts[{index}].noticeability must be an integer from 0 to 100")
        percept = _require_object(
            fact["percept"],
            field=f"facts[{index}].percept",
            expected_fields=_PERCEPT_KEYS,
        )
        summary = _require_text(
            percept["summary"], field=f"facts[{index}].percept.summary", maximum=MAX_TEXT_LENGTH
        )
        normalized_facts.append(
            {
                "fact_id": fact_id,
                "kind": kind,
                "location_id": location_id,
                "channels": channels,
                "perceivable": fact["perceivable"],
                "noticeability": noticeability,
                "percept": {"summary": summary},
            }
        )

    normalized = {
        "schema_version": _require_text(
            state["schema_version"], field="schema_version", maximum=80
        ),
        "revision": _require_revision(state["revision"], field="revision"),
        "world_id": _require_text(state["world_id"], field="world_id", maximum=MAX_ID_LENGTH),
        "timezone": _validate_timezone(state["timezone"]),
        "location": {
            "place_id": _require_text(
                location["place_id"], field="location.place_id", maximum=MAX_ID_LENGTH
            ),
            "area_id": _require_text(
                location["area_id"], field="location.area_id", maximum=MAX_ID_LENGTH
            ),
        },
        "scene": {
            "scene_id": _require_text(scene["scene_id"], field="scene.scene_id", maximum=MAX_ID_LENGTH),
            "revision": _require_revision(scene["revision"], field="scene.revision"),
        },
        "facts": normalized_facts,
    }
    if normalized["schema_version"] != WORLD_SCHEMA_VERSION:
        raise WorldFoundationError("unsupported World State schema_version")
    return _canonical_copy(normalized)


class WorldStateStore:
    """Minimal injected-path store with strict read and atomic write semantics."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        if not self.path.is_absolute():
            raise WorldFoundationError("WorldStateStore path must be absolute and injected")
        if self.path.exists() and self.path.is_dir():
            raise WorldFoundationError("WorldStateStore path must be a file")

    def load(self) -> Optional[dict[str, Any]]:
        """Return valid state or None for missing/malformed state."""

        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            return validate_world_state(raw)
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            return None

    def save(
        self,
        state: Mapping[str, Any],
        *,
        expected_revision: Optional[int] = None,
    ) -> dict[str, Any]:
        normalized = validate_world_state(state)
        if expected_revision is not None:
            if isinstance(expected_revision, bool) or not isinstance(expected_revision, int):
                raise WorldFoundationError("expected_revision must be an integer")
            if normalized["revision"] != expected_revision:
                raise WorldFoundationError("world revision mismatch")

        parent = self.path.parent
        parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(
            normalized,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ) + "\n"
        temporary_path: Optional[str] = None
        try:
            descriptor, temporary_path = tempfile.mkstemp(
                prefix=f".{self.path.name}.",
                dir=str(parent),
                text=True,
            )
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_path, self.path)
            temporary_path = None
        except OSError as exc:
            raise WorldFoundationError("World State could not be written atomically") from exc
        finally:
            if temporary_path is not None:
                try:
                    os.unlink(temporary_path)
                except OSError:
                    pass
        return _canonical_copy(normalized)


def world_local_time(
    world_state: Mapping[str, Any],
    now: Optional[datetime] = None,
) -> datetime:
    """Resolve caller-supplied or current time in the state's explicit timezone."""

    state = validate_world_state(world_state)
    instant = datetime.now(timezone.utc) if now is None else now
    if not isinstance(instant, datetime) or instant.tzinfo is None or instant.utcoffset() is None:
        raise WorldFoundationError("now must be a timezone-aware datetime")
    try:
        target_zone = ZoneInfo(state["timezone"])
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise WorldFoundationError("timezone is not a valid IANA timezone") from exc
    return instant.astimezone(target_zone)


def project_perception(
    world_state: Mapping[str, Any],
    *,
    observer_location_id: str,
    available_channels: Sequence[str],
    attention_budget: int = 3,
) -> dict[str, Any]:
    """Project a bounded, location/channel/attention-limited perception."""

    state = validate_world_state(world_state)
    location_id = _require_text(
        observer_location_id, field="observer_location_id", maximum=MAX_ID_LENGTH
    )
    if isinstance(available_channels, (str, bytes)) or not isinstance(available_channels, Sequence):
        raise WorldFoundationError("available_channels must be a bounded sequence")
    channels = list(available_channels)
    if len(channels) > MAX_AVAILABLE_CHANNELS:
        raise WorldFoundationError("available_channels exceeds the V0 bound")
    normalized_channels = [
        _require_text(channel, field=f"available_channels[{index}]", maximum=64)
        for index, channel in enumerate(channels)
    ]
    if len(set(normalized_channels)) != len(normalized_channels):
        raise WorldFoundationError("available_channels must not contain duplicates")
    if (
        isinstance(attention_budget, bool)
        or not isinstance(attention_budget, int)
        or not 0 <= attention_budget <= MAX_ATTENTION_BUDGET
    ):
        raise WorldFoundationError("attention_budget is outside the V0 bound")

    available = set(normalized_channels)
    eligible = [
        fact
        for fact in state["facts"]
        if fact["location_id"] == location_id
        and fact["perceivable"]
        and available.intersection(fact["channels"])
    ]
    eligible.sort(key=lambda fact: (-fact["noticeability"], fact["fact_id"]))

    visible_facts = [
        {
            "fact_id": fact["fact_id"],
            "kind": fact["kind"],
            "location_id": fact["location_id"],
            "channels": [channel for channel in fact["channels"] if channel in available],
            "percept": {"summary": fact["percept"]["summary"]},
        }
        for fact in eligible[:attention_budget]
    ]
    return {
        "schema_version": PERCEPTION_SCHEMA_VERSION,
        "world_id": state["world_id"],
        "scene_id": state["scene"]["scene_id"],
        "scene_revision": state["scene"]["revision"],
        "observer_location_id": location_id,
        "facts": visible_facts,
    }


__all__ = [
    "MAX_ATTENTION_BUDGET",
    "MAX_FACTS",
    "PERCEPTION_SCHEMA_VERSION",
    "WORLD_SCHEMA_VERSION",
    "WorldFoundationError",
    "WorldStateStore",
    "project_perception",
    "validate_world_state",
    "world_local_time",
]
