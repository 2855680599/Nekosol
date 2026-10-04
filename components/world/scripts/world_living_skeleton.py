#!/usr/bin/env python3
"""Shiomi City World Living Skeleton V1.

This module reuses the existing V0 World State and read-only perception bridge.
It is intentionally explicit: observation is pull-only, and movement is a
verified MOVE_TO operation. No chat text, Life choice, wake, reminder, or
Timeline process is created here.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Mapping
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Optional

try:
    import fcntl
except ImportError:  # pragma: no cover - production is POSIX
    fcntl = None

import world_ambient_events as wae
import world_daily_rhythm as wdr
import world_foundation as wf
import world_home_space as whs
import world_school_day as wsd
import world_school_continuity as wsc
import world_school_assignments as wsa
import world_school_events as wse
import world_school_event_knowledge as wsek
import world_social_presence as wsp
import world_timeline as wt
import world_travel_delivery as wtd

FEATURE_ENV = "CHIYO_WORLD_LIVING_SKELETON_ENABLED"
WORLD_ID = "shiomi_city"
LEGACY_WORLD_ID = "chiyo-world"
WORLD_TIMEZONE = "Asia/Tokyo"
SCHEMA_VERSION = "world.living.skeleton.v1"
OBSERVATION_KIND = "shiomi_world_observation"
MOVE_KIND = "shiomi_world_move"
MAX_TEXT_LENGTH = 240
MAX_OBSERVATION_BYTES = 16_000
MAX_TOOL_ID_LENGTH = 256

_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
_HOME_LOCATION_ID = "home"

_LOCATIONS: dict[str, dict[str, Any]] = {
    "home": {
        "location_id": "home",
        "name": "\u5343\u4ee3\u7684\u5bb6 / \u623f\u95f4",
        "kind": "home",
        "area_id": "bedroom",
        "description": "\u5343\u4ee3\u81ea\u5df1\u7684\u623f\u95f4",
        "reachable": ["nearby_street"],
    },
    "nearby_street": {
        "location_id": "nearby_street",
        "name": "\u5bb6\u9644\u8fd1\u8857\u9053",
        "kind": "street",
        "area_id": "nearby_street",
        "description": "\u5bb6\u9644\u8fd1\u7684\u8857\u9053",
        "reachable": ["home", "school", "convenience_store", "park", "home_goods_store"],
    },
    "school": {
        "location_id": "school",
        "name": "\u5b66\u6821",
        "kind": "school",
        "area_id": "school",
        "description": "\u6c50\u89c1\u5e02\u7684\u5b66\u6821",
        "reachable": ["nearby_street"],
    },
    "convenience_store": {
        "location_id": "convenience_store",
        "name": "\u4fbf\u5229\u5e97",
        "kind": "store",
        "area_id": "convenience_store",
        "description": "\u9644\u8fd1\u7684\u4fbf\u5229\u5e97",
        "reachable": ["nearby_street"],
    },
    "park": {
        "location_id": "park",
        "name": "\u516c\u56ed",
        "kind": "park",
        "area_id": "park",
        "description": "\u9644\u8fd1\u7684\u516c\u56ed",
        "reachable": ["nearby_street"],
    },
    "home_goods_store": {
        "location_id": "home_goods_store",
        "name": "家居用品店",
        "kind": "store",
        "area_id": "home_goods_store",
        "description": "可以买到一些家居用品的店",
        "reachable": ["nearby_street"],
    },
}

_DEFAULT_WEATHER = {
    "condition": "clear",
    "description": "\u6674\u6717",
    "source": "configured_v1",
}


class WorldLivingSkeletonError(ValueError):
    """Raised for malformed explicit World Living Skeleton inputs."""


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
        raise WorldLivingSkeletonError("value is not bounded JSON") from exc


def _truthy(value: Any) -> bool:
    return str(value or "").strip().lower() in _TRUE_VALUES


def feature_enabled(environment: Optional[Mapping[str, str]] = None) -> bool:
    return _truthy((environment or os.environ).get(FEATURE_ENV))


def _home(hermes_home: Optional[Path | str] = None) -> Path:
    value = hermes_home if hermes_home is not None else os.environ.get("HERMES_HOME")
    path = Path(value).expanduser() if value else Path.home() / ".hermes"
    if not path.is_absolute():
        raise WorldLivingSkeletonError("hermes_home must be absolute")
    return path


def world_state_path(hermes_home: Optional[Path | str] = None) -> Path:
    return _home(hermes_home) / "data" / "world" / "world_state.json"


def world_timeline_path(hermes_home: Optional[Path | str] = None) -> Path:
    return _home(hermes_home) / "data" / "world" / "world_timeline.json"


def location_catalog() -> list[dict[str, Any]]:
    return [_copy_json(_LOCATIONS[key]) for key in _LOCATIONS]


def _bounded_text(value: Any, field: str, maximum: int = MAX_TEXT_LENGTH) -> str:
    if not isinstance(value, str):
        raise WorldLivingSkeletonError(f"{field} must be a string")
    result = value.strip()
    if not result or len(result) > maximum or any(char.isspace() for char in result):
        raise WorldLivingSkeletonError(f"{field} is invalid")
    return result


def _location(location_id: Any) -> Optional[dict[str, Any]]:
    if not isinstance(location_id, str):
        return None
    return _LOCATIONS.get(location_id.strip())


def _location_projection(location_id: str) -> dict[str, Any]:
    item = _LOCATIONS[location_id]
    return {
        "location_id": item["location_id"],
        "name": item["name"],
        "kind": item["kind"],
        "area_id": item["area_id"],
    }


def _load_state(hermes_home: Optional[Path | str]) -> Optional[dict[str, Any]]:
    state = wf.WorldStateStore(world_state_path(hermes_home)).load()
    if state is None:
        return None
    if state.get("world_id") != WORLD_ID or state.get("timezone") != WORLD_TIMEZONE:
        return None
    current = state.get("location", {}).get("place_id")
    if _location(current) is None:
        return None
    return state


def _season(month: int) -> str:
    if month in (3, 4, 5):
        return "spring"
    if month in (6, 7, 8):
        return "summer"
    if month in (9, 10, 11):
        return "autumn"
    return "winter"


def _weather_projection(weather: Optional[Mapping[str, Any]]) -> dict[str, str]:
    source = _DEFAULT_WEATHER if weather is None else weather
    if not isinstance(source, Mapping):
        return _copy_json(_DEFAULT_WEATHER)
    result: dict[str, str] = {}
    for field in ("condition", "description", "source"):
        value = source.get(field)
        if not isinstance(value, str) or not value.strip() or len(value.strip()) > 80:
            return _copy_json(_DEFAULT_WEATHER)
        result[field] = value.strip()
    return result


def observe_world(
    *,
    hermes_home: Optional[Path | str] = None,
    now: Optional[datetime] = None,
    weather: Optional[Mapping[str, Any]] = None,
    environment: Optional[Mapping[str, str]] = None,
    perception_ref: Optional[str] = None,
    main_self_projection: bool = False,
    observer_subspace_id: Optional[str] = None,
) -> dict[str, Any]:
    """Return one bounded factual World projection with lazy arrival realization."""

    if not feature_enabled(environment):
        return {
            "kind": OBSERVATION_KIND,
            "schema_version": SCHEMA_VERSION,
            "status": "disabled",
            "reason_code": "FEATURE_DISABLED",
        }
    instant = datetime.now(timezone.utc) if now is None else now
    if wtd.travel_enabled(environment):
        try:
            wtd.realize_due_travel(
                hermes_home=hermes_home,
                environment=environment,
                now=instant,
            )
        except Exception:
            pass
    state = _load_state(hermes_home)
    if state is None:
        return {
            "kind": OBSERVATION_KIND,
            "schema_version": SCHEMA_VERSION,
            "status": "unavailable",
            "reason_code": "WORLD_NOT_INITIALIZED",
        }
    local = wf.world_local_time(state, instant)
    current_id = state["location"]["place_id"]
    current = _location_projection(current_id)
    home = _location_projection(_HOME_LOCATION_ID)
    reachable = [
        _location_projection(item)
        for item in _LOCATIONS[current_id]["reachable"]
    ]
    perceived_facts: list[dict[str, Any]] = []
    try:
        import world_perception_bridge

        bridge = world_perception_bridge.WorldPerceptionBridge(
            world_state_path(hermes_home),
            observer_location_id=current_id,
            available_channels=("sight", "sound", "presence"),
            attention_budget=3,
        )
        bridge_projection = bridge.read(now=instant)
        if isinstance(bridge_projection, Mapping):
            raw_facts = bridge_projection.get("perceived_facts")
            if isinstance(raw_facts, list):
                perceived_facts = _copy_json(raw_facts[:3])
    except Exception:
        perceived_facts = []
    ambient_projection: Optional[list[dict[str, Any]]] = None
    if wae.feature_enabled(environment):
        try:
            relevant_locations = {
                current_id,
                *_LOCATIONS[current_id]["reachable"],
            }
            ambient_projection = wae.active_ambient_events(
                local.date(),
                local,
                timezone_name=WORLD_TIMEZONE,
                allowed_location_ids=_LOCATIONS,
                relevant_location_ids=relevant_locations,
            )
        except Exception:
            ambient_projection = []
    daily_projection: Optional[dict[str, Any]] = None
    if wdr.feature_enabled(environment):
        try:
            daily_projection = {
                "daily_rhythm": wdr.daily_rhythm(local, timezone_name=WORLD_TIMEZONE),
                "location_availability": wdr.location_availability(
                    current_id,
                    local,
                    timezone_name=WORLD_TIMEZONE,
                ),
                "affordances": wdr.affordances_for_location(
                    current_id,
                    local,
                    timezone_name=WORLD_TIMEZONE,
                ),
            }
        except Exception:
            daily_projection = {}
    school_projection: Optional[dict[str, Any]] = None
    if wsd.feature_enabled(environment):
        try:
            school_projection = wsd.observe_school_day(
                now=local,
                current_location_id=current_id,
                environment=environment,
            )
        except Exception:
            school_projection = {
                "kind": wsd.PROJECTION_KIND,
                "schema_version": wsd.SCHEMA_VERSION,
                "status": "unavailable",
                "reason_code": "SCHOOL_DAY_PROJECTION_FAILED",
            }
    school_events_projection: Optional[dict[str, Any]] = None
    if wse.feature_enabled(environment):
        try:
            school_events_projection = wse.observe_school_events(
                hermes_home=hermes_home,
                now=instant,
                current_location_id=current_id,
                environment=environment,
                school_day_projection=school_projection,
            )
        except Exception:
            school_events_projection = {
                "kind": wse.PROJECTION_KIND,
                "schema_version": wse.SCHEMA_VERSION,
                "status": "unavailable",
                "reason_code": "SCHOOL_EVENTS_PROJECTION_FAILED",
            }
    school_life_projection: Optional[dict[str, Any]] = None
    if wsc.feature_enabled(environment):
        try:
            school_life_projection = wsc.observe_school_life(
                hermes_home=hermes_home,
                now=instant,
                current_location_id=current_id,
                environment=environment,
                school_day_projection=school_projection,
                school_event_projection=school_events_projection,
            )
        except Exception:
            school_life_projection = {
                "kind": wsc.PROJECTION_KIND,
                "schema_version": wsc.SCHEMA_VERSION,
                "status": "unavailable",
                "reason_code": "SCHOOL_CONTINUITY_PROJECTION_FAILED",
            }
    school_assignments_projection: Optional[dict[str, Any]] = None
    if wsa.feature_enabled(environment):
        try:
            school_assignments_projection = wsa.observe_school_assignments(
                hermes_home=hermes_home,
                now=instant,
                current_location_id=current_id,
                environment=environment,
                school_day_projection=school_projection,
            )
        except Exception:
            school_assignments_projection = {
                "kind": wsa.PROJECTION_KIND,
                "schema_version": wsa.SCHEMA_VERSION,
                "status": "unavailable",
                "reason_code": "SCHOOL_ASSIGNMENTS_PROJECTION_FAILED",
            }
    if (
        isinstance(school_events_projection, Mapping)
        and isinstance(school_assignments_projection, Mapping)
    ):
        try:
            school_events_projection = wsa.merge_into_school_events(
                school_events_projection,
                school_assignments_projection,
            )
        except Exception:
            school_assignments_projection = {
                "kind": wsa.PROJECTION_KIND,
                "schema_version": wsa.SCHEMA_VERSION,
                "status": "unavailable",
                "reason_code": "SCHOOL_ASSIGNMENTS_EVENT_MERGE_FAILED",
            }
    if (
        isinstance(school_events_projection, Mapping)
        and school_events_projection.get("status") == "available"
    ):
        try:
            school_events_projection = wse.attach_exposure(
                school_events_projection,
                school_life_projection,
            )
        except Exception:
            school_events_projection = {
                "kind": wse.PROJECTION_KIND,
                "schema_version": wse.SCHEMA_VERSION,
                "status": "unavailable",
                "reason_code": "SCHOOL_EVENTS_EXPOSURE_FAILED",
            }
    home_projection: Optional[dict[str, Any]] = None
    if whs.feature_enabled(environment):
        try:
            home_projection = whs.observe_home(
                hermes_home=hermes_home,
                world_state=state,
                environment=environment,
            )
        except Exception:
            home_projection = {
                "kind": whs.PROJECTION_KIND,
                "schema_version": whs.SCHEMA_VERSION,
                "status": "unavailable",
                "reason_code": "HOME_PROJECTION_FAILED",
            }
    social_projection: Optional[dict[str, Any]] = None
    if wsp.feature_enabled(environment):
        try:
            social_projection = wsp.observe_social_presence(
                current_id,
                now=local,
                environment=environment,
            )
        except Exception:
            social_projection = {
                "kind": wsp.PROJECTION_KIND,
                "schema_version": wsp.SCHEMA_VERSION,
                "status": "unavailable",
                "reason_code": "SOCIAL_PROJECTION_FAILED",
            }
    travel_projection: Optional[dict[str, Any]] = None
    if wtd.travel_enabled(environment):
        try:
            candidate = wtd.observe_travel(
                hermes_home=hermes_home,
                environment=environment,
                now=instant,
            )
            if candidate.get("status") == "in_progress":
                travel_projection = candidate
        except Exception:
            travel_projection = None
    result = {
        "kind": OBSERVATION_KIND,
        "schema_version": SCHEMA_VERSION,
        "status": "available",
        "world_id": WORLD_ID,
        "timezone": WORLD_TIMEZONE,
        "world_revision": state["revision"],
        "local_time": local.isoformat(timespec="seconds"),
        "local_date": local.date().isoformat(),
        "weekday": local.isoweekday(),
        "season": _season(local.month),
        "weather": _weather_projection(weather),
        "current_location": current,
        "home_location": home,
        "reachable_locations": reachable,
        "locations": location_catalog(),
        "scene": _copy_json(state["scene"]),
        "perceived_facts": perceived_facts,
    }
    if ambient_projection is not None:
        result["ambient_events"] = ambient_projection
    if daily_projection is not None:
        result.update(daily_projection)
    if school_projection is not None:
        result["school_day"] = school_projection
    if school_life_projection is not None:
        result["school_life"] = school_life_projection
    if school_events_projection is not None:
        result["school_events"] = school_events_projection
    if school_assignments_projection is not None:
        result["school_assignments"] = wsa.public_projection(school_assignments_projection)
    if home_projection is not None:
        result["home_space"] = home_projection
    if social_projection is not None:
        result["social_presence"] = social_projection
    if travel_projection is not None:
        result["travel"] = travel_projection
    if main_self_projection and wsek.feature_enabled(environment):
        try:
            wsek.record_first_hand_perception(
                school_events_projection,
                hermes_home=hermes_home,
                now=instant,
                perception_ref=perception_ref,
                observer_subspace_id=observer_subspace_id,
                environment=environment,
            )
        except Exception:
            pass
        try:
            result = wsek.project_for_main_self(
                result,
                hermes_home=hermes_home,
                environment=environment,
            )
        except Exception:
            result = _copy_json(result)
            result.pop("school_events", None)
    if main_self_projection and wsa.feature_enabled(environment):
        try:
            result = wsa.project_for_main_self(
                result,
                hermes_home=hermes_home,
                environment=environment,
            )
        except Exception:
            result = _copy_json(result)
            result.pop("school_assignments", None)
    encoded = json.dumps(
        result,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(encoded) > MAX_OBSERVATION_BYTES:
        return {
            "kind": OBSERVATION_KIND,
            "schema_version": SCHEMA_VERSION,
            "status": "unavailable",
            "reason_code": "PROJECTION_BOUNDS_EXCEEDED",
        }
    return _copy_json(result)


def read_move_state(
    *,
    hermes_home: Optional[Path | str] = None,
    environment: Optional[Mapping[str, str]] = None,
    now: Optional[datetime] = None,
) -> Optional[dict[str, str]]:
    if wtd.travel_enabled(environment):
        try:
            wtd.realize_due_travel(
                hermes_home=hermes_home,
                environment=environment,
                now=now,
            )
        except Exception:
            pass
    state = _load_state(hermes_home)
    if state is None:
        return None
    return {
        "world_id": WORLD_ID,
        "world_location_id": state["location"]["place_id"],
        "world_scene_id": state["scene"]["scene_id"],
        "world_revision": str(state["revision"]),
    }


def _before_summary(state: Mapping[str, Any]) -> dict[str, str]:
    return {
        "world_id": WORLD_ID,
        "world_location_id": str(state["location"]["place_id"]),
        "world_scene_id": str(state["scene"]["scene_id"]),
        "world_revision": str(state["revision"]),
    }


def plan_move_to(
    location_id: Any,
    *,
    hermes_home: Optional[Path | str] = None,
    environment: Optional[Mapping[str, str]] = None,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """Validate a MOVE_TO without writing World State."""

    env = environment or os.environ
    if not feature_enabled(env):
        return {"kind": MOVE_KIND, "status": "disabled", "reason_code": "FEATURE_DISABLED"}
    try:
        target = _bounded_text(location_id, "location_id", maximum=96)
    except WorldLivingSkeletonError:
        return {"kind": MOVE_KIND, "status": "rejected", "reason_code": "INVALID_LOCATION_ID"}
    destination = _location(target)
    if destination is None:
        return {"kind": MOVE_KIND, "status": "rejected", "reason_code": "UNKNOWN_LOCATION"}
    if wtd.travel_enabled(env):
        try:
            wtd.realize_due_travel(
                hermes_home=hermes_home,
                environment=env,
                now=now,
            )
        except Exception:
            pass
    state = _load_state(hermes_home)
    if state is None:
        return {"kind": MOVE_KIND, "status": "unavailable", "reason_code": "WORLD_NOT_INITIALIZED"}
    before = _before_summary(state)
    current_id = state["location"]["place_id"]
    if target == current_id:
        return {
            "kind": MOVE_KIND,
            "status": "noop",
            "reason_code": "ALREADY_AT_LOCATION",
            "target_location_id": target,
            "before": before,
        }
    if not wtd.travel_enabled(env) and wdr.feature_enabled(env):
        try:
            availability = wdr.location_availability(
                target,
                wf.world_local_time(
                    state,
                    datetime.now(timezone.utc) if now is None else now,
                ),
                timezone_name=WORLD_TIMEZONE,
            )
        except Exception:
            return {
                "kind": MOVE_KIND,
                "status": "rejected",
                "reason_code": "DAILY_RHYTHM_UNAVAILABLE",
                "target_location_id": target,
                "before": before,
            }
        if availability["status"] != "OPEN":
            return {
                "kind": MOVE_KIND,
                "status": "rejected",
                "reason_code": "LOCATION_CLOSED",
                "target_location_id": target,
                "before": before,
                "availability": availability,
            }
    if target not in _LOCATIONS[current_id]["reachable"]:
        return {
            "kind": MOVE_KIND,
            "status": "rejected",
            "reason_code": "LOCATION_NOT_REACHABLE",
            "target_location_id": target,
            "before": before,
        }
    if wtd.travel_enabled(env):
        travel_plan = wtd.plan_travel(
            origin_location_id=current_id,
            destination_location_id=target,
            hermes_home=hermes_home,
            environment=env,
            now=now,
        )
        if travel_plan.get("status") != "ready":
            return {
                "kind": MOVE_KIND,
                "status": "rejected",
                "reason_code": travel_plan.get("reason_code", "TRAVEL_NOT_ALLOWED"),
                "target_location_id": target,
                "before": before,
                **(
                    {"travel": travel_plan["travel"]}
                    if isinstance(travel_plan.get("travel"), Mapping)
                    else {}
                ),
            }
        return {
            "kind": MOVE_KIND,
            "status": "ready",
            "reason_code": "TRAVEL_ROUTE_READY",
            "target_location_id": target,
            "before": before,
            "destination": _location_projection(target),
            "travel": travel_plan,
        }
    return {
        "kind": MOVE_KIND,
        "status": "ready",
        "reason_code": "REACHABLE",
        "target_location_id": target,
        "before": before,
        "destination": _location_projection(target),
    }


@contextmanager
def _world_lock(path: Path) -> Iterator[None]:
    if fcntl is None:
        raise WorldLivingSkeletonError("world lock unavailable")
    lock_path = path.with_name(path.name + ".move.lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def apply_move_to(
    location_id: Any,
    *,
    hermes_home: Optional[Path | str] = None,
    environment: Optional[Mapping[str, str]] = None,
    now: Optional[datetime] = None,
    execution_id: Optional[str] = None,
) -> dict[str, Any]:
    """Apply one explicit reachable MOVE_TO with exactly one World save."""

    env = environment or os.environ
    if not feature_enabled(env):
        return {"kind": MOVE_KIND, "status": "disabled", "reason_code": "FEATURE_DISABLED"}
    try:
        target = _bounded_text(location_id, "location_id", maximum=96)
    except WorldLivingSkeletonError:
        return {"kind": MOVE_KIND, "status": "rejected", "reason_code": "INVALID_LOCATION_ID"}
    if _location(target) is None:
        return {
            "kind": MOVE_KIND,
            "status": "rejected",
            "reason_code": "UNKNOWN_LOCATION",
            "target_location_id": target,
        }
    state_path = world_state_path(hermes_home)
    if wtd.travel_enabled(env):
        try:
            wtd.realize_due_travel(
                hermes_home=hermes_home,
                environment=env,
                now=now,
            )
        except Exception:
            pass
    try:
        with _world_lock(state_path):
            state = _load_state(hermes_home)
            if state is None:
                return {
                    "kind": MOVE_KIND,
                    "status": "unavailable",
                    "reason_code": "WORLD_NOT_INITIALIZED",
                    "target_location_id": target,
                }
            before = _before_summary(state)
            current_id = state["location"]["place_id"]
            if target == current_id:
                return {
                    "kind": MOVE_KIND,
                    "status": "noop",
                    "reason_code": "ALREADY_AT_LOCATION",
                    "target_location_id": target,
                    "before": before,
                    "after": before,
                }
            if not wtd.travel_enabled(env) and wdr.feature_enabled(env):
                try:
                    availability = wdr.location_availability(
                        target,
                        wf.world_local_time(
                            state,
                            datetime.now(timezone.utc) if now is None else now,
                        ),
                        timezone_name=WORLD_TIMEZONE,
                    )
                except Exception:
                    return {
                        "kind": MOVE_KIND,
                        "status": "rejected",
                        "reason_code": "DAILY_RHYTHM_UNAVAILABLE",
                        "target_location_id": target,
                        "before": before,
                    }
                if availability["status"] != "OPEN":
                    return {
                        "kind": MOVE_KIND,
                        "status": "rejected",
                        "reason_code": "LOCATION_CLOSED",
                        "target_location_id": target,
                        "before": before,
                        "availability": availability,
                    }
            if target not in _LOCATIONS[current_id]["reachable"]:
                return {
                    "kind": MOVE_KIND,
                    "status": "rejected",
                    "reason_code": "LOCATION_NOT_REACHABLE",
                    "target_location_id": target,
                    "before": before,
                }
            if wtd.travel_enabled(env):
                route_plan = wtd.plan_travel(
                    origin_location_id=current_id,
                    destination_location_id=target,
                    hermes_home=hermes_home,
                    environment=env,
                    now=now,
                )
                if route_plan.get("status") != "ready":
                    return {
                        "kind": MOVE_KIND,
                        "status": "rejected",
                        "reason_code": route_plan.get(
                            "reason_code", "TRAVEL_NOT_ALLOWED"
                        ),
                        "target_location_id": target,
                        "before": before,
                    }
                stable_execution_id = (
                    execution_id
                    if isinstance(execution_id, str) and execution_id.strip()
                    else wtd.direct_execution_id(
                        current_id,
                        target,
                        state["revision"],
                    )
                )
                started = wtd.start_travel(
                    execution_id=stable_execution_id,
                    origin_location_id=current_id,
                    destination_location_id=target,
                    destination_area_id=_LOCATIONS[target]["area_id"],
                    hermes_home=hermes_home,
                    environment=env,
                    now=now,
                )
                if started.get("status") not in {"started", "already_started"}:
                    return {
                        "kind": MOVE_KIND,
                        "status": "rejected",
                        "reason_code": started.get(
                            "reason_code", "TRAVEL_START_FAILED"
                        ),
                        "target_location_id": target,
                        "before": before,
                    }
                travel = started
                return {
                    "kind": MOVE_KIND,
                    "status": "applied",
                    "reason_code": (
                        "TRAVEL_ALREADY_STARTED"
                        if started.get("status") == "already_started"
                        else "TRAVEL_STARTED"
                    ),
                    "target_location_id": target,
                    "before": before,
                    "after": before,
                    "travel": travel,
                    "postcondition": {
                        "verified": True,
                        "expected_world_location_id": current_id,
                        "expected_destination_location_id": target,
                        "expected_travel_id": travel.get("travel_id"),
                    },
                }
            destination = _LOCATIONS[target]
            updated = _copy_json(state)
            updated["revision"] = state["revision"] + 1
            updated["location"] = {
                "place_id": target,
                "area_id": destination["area_id"],
            }
            updated["scene"] = {
                "scene_id": f"{target}.{destination['area_id']}",
                "revision": state["scene"]["revision"] + 1,
            }
            wf.WorldStateStore(state_path).save(
                updated,
                expected_revision=updated["revision"],
            )
            loaded = wf.WorldStateStore(state_path).load()
            if loaded is None or loaded["location"]["place_id"] != target:
                return {
                    "kind": MOVE_KIND,
                    "status": "uncertain",
                    "reason_code": "WORLD_POSTCONDITION_FAILED",
                    "target_location_id": target,
                    "before": before,
                }
            after = _before_summary(loaded)
            return {
                "kind": MOVE_KIND,
                "status": "applied",
                "reason_code": "MOVE_COMMITTED",
                "target_location_id": target,
                "before": before,
                "after": after,
                "postcondition": {
                    "verified": True,
                    "expected_world_location_id": target,
                },
            }
    except Exception:
        return {
            "kind": MOVE_KIND,
            "status": "failed",
            "reason_code": "WORLD_WRITE_FAILED",
            "target_location_id": target,
        }


def execution_id_for_tool(
    session_id: Any,
    turn_id: Any,
    tool_call_id: Any,
) -> str:
    values = []
    for field, value in (
        ("session_id", session_id),
        ("turn_id", turn_id),
        ("tool_call_id", tool_call_id),
    ):
        values.append((field, _bounded_text(value, field, MAX_TOOL_ID_LENGTH)))
    payload = json.dumps(
        dict(values),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "world-move-" + hashlib.sha256(payload).hexdigest()


def _legacy_state_allowed(state: Mapping[str, Any]) -> bool:
    return (
        state.get("world_id") == LEGACY_WORLD_ID
        and state.get("timezone") == WORLD_TIMEZONE
        and state.get("revision") == 1
        and state.get("facts") == []
        and state.get("location") == {"place_id": "home", "area_id": "bedroom"}
        and state.get("scene") == {
            "scene_id": "home.bedroom.initial",
            "revision": 1,
        }
    )


def migrate_legacy_world(
    *,
    hermes_home: Optional[Path | str] = None,
) -> dict[str, Any]:
    """Migrate only the untouched empty V0 genesis shell to Shiomi City."""

    home = _home(hermes_home)
    state_path = world_state_path(home)
    timeline_path = world_timeline_path(home)
    state_store = wf.WorldStateStore(state_path)
    timeline_store = wt.WorldTimelineStore(timeline_path)
    state = state_store.load()
    timeline = timeline_store.load()
    if state is None or timeline is None:
        return {"status": "rejected", "reason_code": "GENESIS_UNAVAILABLE"}
    if not _legacy_state_allowed(state):
        return {"status": "rejected", "reason_code": "LEGACY_WORLD_HAS_HISTORY"}
    if (
        timeline.get("world_id") != LEGACY_WORLD_ID
        or timeline.get("revision") != 1
        or timeline.get("processes") != []
    ):
        return {"status": "rejected", "reason_code": "LEGACY_TIMELINE_HAS_HISTORY"}
    original_state = _copy_json(state)
    original_timeline = _copy_json(timeline)
    new_state = _copy_json(state)
    new_state["world_id"] = WORLD_ID
    new_timeline = wt.build_initial_timeline(WORLD_ID)
    try:
        state_store.save(new_state, expected_revision=1)
        timeline_store.save(new_timeline, expected_revision=1)
        verified_state = state_store.load()
        verified_timeline = timeline_store.load()
        if (
            verified_state is None
            or verified_timeline is None
            or verified_state["world_id"] != WORLD_ID
            or verified_timeline["world_id"] != WORLD_ID
            or verified_timeline["processes"] != []
        ):
            raise WorldLivingSkeletonError("migration postcondition failed")
        return {
            "status": "migrated",
            "world_id": WORLD_ID,
            "state_revision": verified_state["revision"],
            "timeline_revision": verified_timeline["revision"],
            "processes_count": len(verified_timeline["processes"]),
        }
    except Exception as exc:
        try:
            state_store.save(original_state, expected_revision=1)
            timeline_store.save(original_timeline, expected_revision=1)
        except Exception:
            pass
        return {
            "status": "failed",
            "reason_code": "MIGRATION_WRITE_FAILED",
            "error_type": type(exc).__name__,
        }


__all__ = [
    "FEATURE_ENV",
    "LEGACY_WORLD_ID",
    "MOVE_KIND",
    "OBSERVATION_KIND",
    "SCHEMA_VERSION",
    "WORLD_ID",
    "WORLD_TIMEZONE",
    "WorldLivingSkeletonError",
    "apply_move_to",
    "execution_id_for_tool",
    "feature_enabled",
    "location_catalog",
    "migrate_legacy_world",
    "observe_world",
    "plan_move_to",
    "read_move_state",
    "world_state_path",
    "world_timeline_path",
]
