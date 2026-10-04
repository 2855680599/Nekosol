#!/usr/bin/env python3
"""Deterministic Shiomi City travel and home-delivery timing primitives."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from collections.abc import Mapping, Sequence
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator, Optional

import world_foundation as wf
import world_timeline as wt

try:
    import fcntl
except ImportError:  # pragma: no cover - production is POSIX
    fcntl = None


TRAVEL_FEATURE_ENV = "CHIYO_WORLD_TRAVEL_TIME_ENABLED"
DELIVERY_FEATURE_ENV = "CHIYO_WORLD_DELIVERY_TIME_ENABLED"
WORLD_ID = "shiomi_city"
WORLD_TIMEZONE = "Asia/Tokyo"
SCHEMA_VERSION = "world.travel_delivery.v1"
TRAVEL_MODE_WALK = "WALK"
TRAVEL_STATUS_IN_PROGRESS = "IN_PROGRESS"
TRAVEL_STATUS_ARRIVED = "ARRIVED"
TRAVEL_PROJECTION_KIND = "shiomi_world_travel"
STATE_FILENAME = "world_travel_delivery.json"
ROUTE_REVISION = "shiomi-walk-v1"
DELIVERY_MODE = "LOCAL_HOME_DELIVERY"
DELIVERY_DURATION_SECONDS = 2 * 60 * 60
MAX_TRAVELS = 64
MAX_ID_LENGTH = 160
MAX_TEXT_LENGTH = 240

# One deterministic duration for each existing adjacent graph edge.
WALKING_DURATIONS: dict[tuple[str, str], int] = {
    ("home", "nearby_street"): 300,
    ("nearby_street", "home"): 300,
    ("nearby_street", "convenience_store"): 300,
    ("convenience_store", "nearby_street"): 300,
    ("nearby_street", "school"): 780,
    ("school", "nearby_street"): 780,
    ("nearby_street", "park"): 540,
    ("park", "nearby_street"): 540,
    ("nearby_street", "home_goods_store"): 720,
    ("home_goods_store", "nearby_street"): 720,
}

_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,159}$")
_STATE_KEYS = frozenset({"schema_version", "world_id", "revision", "travels"})
_TRAVEL_KEYS = frozenset(
    {
        "travel_id",
        "execution_id",
        "actor",
        "origin_location_id",
        "destination_location_id",
        "started_at",
        "expected_arrival_at",
        "arrived_at",
        "travel_mode",
        "route_id",
        "route_revision",
        "route_duration_seconds",
        "status",
        "provenance",
    }
)
_VALID_STATUSES = frozenset({TRAVEL_STATUS_IN_PROGRESS, TRAVEL_STATUS_ARRIVED})


class TravelDeliveryError(ValueError):
    """A travel or delivery contract is malformed or unavailable."""


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
        raise TravelDeliveryError("value is not bounded JSON") from exc


def _truthy(value: Any) -> bool:
    return str(value or "").strip().lower() in _TRUE_VALUES


def travel_enabled(environment: Optional[Mapping[str, str]] = None) -> bool:
    return _truthy((environment or os.environ).get(TRAVEL_FEATURE_ENV))


def delivery_enabled(environment: Optional[Mapping[str, str]] = None) -> bool:
    return _truthy((environment or os.environ).get(DELIVERY_FEATURE_ENV))


def feature_enabled(environment: Optional[Mapping[str, str]] = None) -> bool:
    return travel_enabled(environment)


def _home(hermes_home: Optional[Path | str] = None) -> Path:
    raw = hermes_home if hermes_home is not None else os.environ.get("HERMES_HOME")
    path = Path(raw).expanduser() if raw else Path.home() / ".hermes"
    if not path.is_absolute():
        raise TravelDeliveryError("hermes_home must be absolute")
    return path


def state_path(hermes_home: Optional[Path | str] = None) -> Path:
    return _home(hermes_home) / "data" / "world" / STATE_FILENAME


def world_state_path(hermes_home: Optional[Path | str] = None) -> Path:
    return _home(hermes_home) / "data" / "world" / "world_state.json"


def timeline_path(hermes_home: Optional[Path | str] = None) -> Path:
    return _home(hermes_home) / "data" / "world" / "world_timeline.json"


def _text(value: Any, field: str, maximum: int = MAX_TEXT_LENGTH) -> str:
    if not isinstance(value, str):
        raise TravelDeliveryError(f"{field} must be a string")
    result = value.strip()
    if (
        not result
        or len(result) > maximum
        or any(char in result for char in "\r\n\x00")
    ):
        raise TravelDeliveryError(f"{field} is invalid")
    return result


def _id(value: Any, field: str) -> str:
    result = _text(value, field, MAX_ID_LENGTH)
    if not _ID_RE.fullmatch(result):
        raise TravelDeliveryError(f"{field} has invalid characters")
    return result


def _timestamp(value: Any = None, field: str = "timestamp") -> str:
    if value is None:
        instant = datetime.now(timezone.utc)
    elif isinstance(value, datetime):
        instant = value
    else:
        text = _text(value, field, 96)
        normalized = text[:-1] + "+00:00" if text.endswith(("Z", "z")) else text
        try:
            instant = datetime.fromisoformat(normalized)
        except ValueError as exc:
            raise TravelDeliveryError(f"{field} must be ISO datetime") from exc
    if instant.tzinfo is None or instant.utcoffset() is None:
        raise TravelDeliveryError(f"{field} must include timezone")
    return instant.astimezone(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def _instant(value: Any = None, field: str = "now") -> datetime:
    normalized = _timestamp(value, field)
    return datetime.fromisoformat(normalized.replace("Z", "+00:00"))


def route_duration_seconds(origin_location_id: Any, destination_location_id: Any) -> Optional[int]:
    try:
        origin = _id(origin_location_id, "origin_location_id")
        destination = _id(destination_location_id, "destination_location_id")
    except TravelDeliveryError:
        return None
    return WALKING_DURATIONS.get((origin, destination))


def route_id_for(origin_location_id: Any, destination_location_id: Any) -> str:
    origin = _id(origin_location_id, "origin_location_id")
    destination = _id(destination_location_id, "destination_location_id")
    if (origin, destination) not in WALKING_DURATIONS:
        raise TravelDeliveryError("route is not a V1 walking edge")
    return f"{origin}->{destination}"


def direct_execution_id(
    origin_location_id: Any,
    destination_location_id: Any,
    world_revision: Any,
) -> str:
    material = "|".join(
        (
            _id(origin_location_id, "origin_location_id"),
            _id(destination_location_id, "destination_location_id"),
            str(world_revision),
        )
    )
    return "world-direct-" + hashlib.sha256(material.encode("utf-8")).hexdigest()


def _stable_id(execution_id: str, origin: str, destination: str) -> str:
    material = "|".join((execution_id, origin, destination, ROUTE_REVISION))
    return "travel-" + hashlib.sha256(material.encode("utf-8")).hexdigest()


def _validate_travel(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != _TRAVEL_KEYS:
        raise TravelDeliveryError("travel record has an invalid strict shape")
    travel_id = _id(value.get("travel_id"), "travel_id")
    execution_id = _id(value.get("execution_id"), "execution_id")
    origin = _id(value.get("origin_location_id"), "origin_location_id")
    destination = _id(value.get("destination_location_id"), "destination_location_id")
    if origin == destination:
        raise TravelDeliveryError("travel origin and destination must differ")
    duration = value.get("route_duration_seconds")
    if isinstance(duration, bool) or not isinstance(duration, int) or duration <= 0:
        raise TravelDeliveryError("route_duration_seconds is invalid")
    if route_duration_seconds(origin, destination) != duration:
        raise TravelDeliveryError("route duration does not match route authority")
    if value.get("actor") != "chiyo":
        raise TravelDeliveryError("travel actor is invalid")
    if value.get("travel_mode") != TRAVEL_MODE_WALK:
        raise TravelDeliveryError("travel mode is invalid")
    route_id = _text(value.get("route_id"), "route_id", MAX_TEXT_LENGTH)
    if route_id != f"{origin}->{destination}":
        raise TravelDeliveryError("route_id is invalid")
    if value.get("route_revision") != ROUTE_REVISION:
        raise TravelDeliveryError("route_revision is invalid")
    if value.get("provenance") != "verified_main_self_move_to":
        raise TravelDeliveryError("travel provenance is invalid")
    started_at = _timestamp(value.get("started_at"), "started_at")
    expected = _timestamp(value.get("expected_arrival_at"), "expected_arrival_at")
    if _instant(expected) <= _instant(started_at):
        raise TravelDeliveryError("expected_arrival_at must be later than started_at")
    status = value.get("status")
    if status not in _VALID_STATUSES:
        raise TravelDeliveryError("travel status is invalid")
    arrived_at = value.get("arrived_at")
    if status == TRAVEL_STATUS_IN_PROGRESS:
        if arrived_at is not None:
            raise TravelDeliveryError("in-progress travel cannot have arrived_at")
    else:
        if arrived_at is None:
            raise TravelDeliveryError("arrived travel must have arrived_at")
        arrived_at = _timestamp(arrived_at, "arrived_at")
        if _instant(arrived_at) < _instant(expected):
            raise TravelDeliveryError("arrived_at precedes expected arrival")
    if travel_id != _stable_id(execution_id, origin, destination):
        raise TravelDeliveryError("travel_id is not stable")
    return {
        "travel_id": travel_id,
        "execution_id": execution_id,
        "actor": "chiyo",
        "origin_location_id": origin,
        "destination_location_id": destination,
        "started_at": started_at,
        "expected_arrival_at": expected,
        "arrived_at": arrived_at,
        "travel_mode": TRAVEL_MODE_WALK,
        "route_id": route_id,
        "route_revision": ROUTE_REVISION,
        "route_duration_seconds": duration,
        "status": status,
        "provenance": "verified_main_self_move_to",
    }


def validate_state(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != _STATE_KEYS:
        raise TravelDeliveryError("travel state has an invalid strict shape")
    if value.get("schema_version") != SCHEMA_VERSION:
        raise TravelDeliveryError("travel state schema is unsupported")
    if value.get("world_id") != WORLD_ID:
        raise TravelDeliveryError("travel state world_id is invalid")
    revision = value.get("revision")
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
        raise TravelDeliveryError("travel state revision is invalid")
    raw = value.get("travels")
    if (
        not isinstance(raw, Sequence)
        or isinstance(raw, (str, bytes, bytearray))
        or len(raw) > MAX_TRAVELS
    ):
        raise TravelDeliveryError("travel state travels are invalid")
    travels = [_validate_travel(item) for item in raw]
    ids = [item["travel_id"] for item in travels]
    executions = [item["execution_id"] for item in travels]
    if len(ids) != len(set(ids)) or len(executions) != len(set(executions)):
        raise TravelDeliveryError("travel identities must be unique")
    return {
        "schema_version": SCHEMA_VERSION,
        "world_id": WORLD_ID,
        "revision": revision,
        "travels": travels,
    }


def _load(path: Path) -> Optional[dict[str, Any]]:
    if not path.exists():
        return None
    try:
        return validate_state(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise TravelDeliveryError("travel state cannot be read") from exc


def load_state(hermes_home: Optional[Path | str] = None) -> Optional[dict[str, Any]]:
    return _load(state_path(hermes_home))


def _save(path: Path, state: Mapping[str, Any]) -> dict[str, Any]:
    normalized = validate_state(state)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (
        json.dumps(
            normalized,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")
    temporary: Optional[str] = None
    try:
        descriptor, temporary = tempfile.mkstemp(
            prefix=f".{path.name}.", dir=str(path.parent), text=False
        )
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
        os.chmod(path, 0o600)
    except OSError as exc:
        raise TravelDeliveryError("travel state could not be atomically written") from exc
    finally:
        if temporary is not None:
            try:
                os.unlink(temporary)
            except OSError:
                pass
    return normalized


@contextmanager
def _lock(path: Path) -> Iterator[None]:
    if fcntl is None:
        raise TravelDeliveryError("travel state lock unavailable")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_name(path.name + ".lock").open("a+") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextmanager
def _world_lock(path: Path) -> Iterator[None]:
    if fcntl is None:
        raise TravelDeliveryError("world state lock unavailable")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_name(path.name + ".move.lock").open("a+") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _empty_state() -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "world_id": WORLD_ID,
        "revision": 1,
        "travels": [],
    }


def _current_activity_status(hermes_home: Path) -> Optional[str]:
    raw_dir = os.environ.get("XINXI_DIR")
    directory = Path(raw_dir).expanduser() if raw_dir else hermes_home / "xinxi"
    path = directory / "current_activity.json"
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return "unavailable"
    if not isinstance(payload, Mapping):
        return "unavailable"
    status = payload.get("status")
    return status.strip().lower() if isinstance(status, str) else "unavailable"


def _active_travel(state: Optional[Mapping[str, Any]]) -> Optional[dict[str, Any]]:
    if not isinstance(state, Mapping):
        return None
    for travel in state.get("travels", []):
        if isinstance(travel, Mapping) and travel.get("status") == TRAVEL_STATUS_IN_PROGRESS:
            return dict(travel)
    return None


def _timeline_process_id(travel_id: str) -> str:
    return "travel-process-" + hashlib.sha256(travel_id.encode("utf-8")).hexdigest()


def _travel_projection(
    travel: Mapping[str, Any],
    *,
    hermes_home: Path,
    now: Any = None,
) -> dict[str, Any]:
    normalized = _validate_travel(travel)
    result: dict[str, Any] = {
        "kind": TRAVEL_PROJECTION_KIND,
        "status": (
            "in_progress"
            if normalized["status"] == TRAVEL_STATUS_IN_PROGRESS
            else "arrived"
        ),
        "travel_id": normalized["travel_id"],
        "origin_location_id": normalized["origin_location_id"],
        "destination_location_id": normalized["destination_location_id"],
        "started_at": normalized["started_at"],
        "expected_arrival_at": normalized["expected_arrival_at"],
        "arrived_at": normalized["arrived_at"],
        "travel_mode": normalized["travel_mode"],
        "route_id": normalized["route_id"],
        "route_revision": normalized["route_revision"],
        "route_duration_seconds": normalized["route_duration_seconds"],
    }
    timeline = wt.WorldTimelineStore(timeline_path(hermes_home)).load()
    if timeline is not None:
        process = next(
            (
                item
                for item in timeline["processes"]
                if item.get("process_id") == _timeline_process_id(normalized["travel_id"])
            ),
            None,
        )
        if process is not None:
            try:
                result["phase"] = wt.resolve_phase(process, _instant(now))
            except Exception:
                pass
    return _copy_json(result)


def plan_travel(
    *,
    origin_location_id: Any,
    destination_location_id: Any,
    hermes_home: Optional[Path | str] = None,
    environment: Optional[Mapping[str, str]] = None,
    now: Any = None,
) -> dict[str, Any]:
    """Validate one bounded WALK route without writing state."""

    if not travel_enabled(environment):
        return {
            "kind": TRAVEL_PROJECTION_KIND,
            "status": "disabled",
            "reason_code": "FEATURE_DISABLED",
        }
    try:
        origin = _id(origin_location_id, "origin_location_id")
        destination = _id(destination_location_id, "destination_location_id")
    except TravelDeliveryError:
        return {
            "kind": TRAVEL_PROJECTION_KIND,
            "status": "rejected",
            "reason_code": "INVALID_ROUTE",
        }
    duration = route_duration_seconds(origin, destination)
    if duration is None:
        return {
            "kind": TRAVEL_PROJECTION_KIND,
            "status": "rejected",
            "reason_code": "ROUTE_NOT_REACHABLE",
            "origin_location_id": origin,
            "destination_location_id": destination,
        }
    activity_status = _current_activity_status(_home(hermes_home))
    if activity_status == "unavailable":
        return {
            "kind": TRAVEL_PROJECTION_KIND,
            "status": "rejected",
            "reason_code": "CURRENT_ACTIVITY_UNAVAILABLE",
        }
    if activity_status == "active":
        return {
            "kind": TRAVEL_PROJECTION_KIND,
            "status": "rejected",
            "reason_code": "ACTIVE_ACTIVITY_CONFLICT",
        }
    state = load_state(hermes_home)
    active = _active_travel(state)
    if active is not None:
        return {
            "kind": TRAVEL_PROJECTION_KIND,
            "status": "rejected",
            "reason_code": "TRAVEL_ALREADY_IN_PROGRESS",
            "travel": _travel_projection(active, hermes_home=_home(hermes_home), now=now),
        }
    started = _instant(now)
    expected = started + timedelta(seconds=duration)
    return {
        "kind": TRAVEL_PROJECTION_KIND,
        "status": "ready",
        "reason_code": "ROUTE_READY",
        "origin_location_id": origin,
        "destination_location_id": destination,
        "travel_mode": TRAVEL_MODE_WALK,
        "route_id": f"{origin}->{destination}",
        "route_revision": ROUTE_REVISION,
        "route_duration_seconds": duration,
        "started_at": _timestamp(started, "started_at"),
        "expected_arrival_at": _timestamp(expected, "expected_arrival_at"),
    }


def ensure_travel_timeline_process(
    travel: Mapping[str, Any],
    *,
    hermes_home: Optional[Path | str] = None,
    destination_area_id: Optional[str] = None,
) -> dict[str, Any]:
    normalized = _validate_travel(travel)
    home = _home(hermes_home)
    path = timeline_path(home)
    if not path.exists() or fcntl is None:
        return {"status": "unavailable", "reason_code": "TIMELINE_NOT_INITIALIZED"}
    process_id = _timeline_process_id(normalized["travel_id"])
    location_area = _text(
        destination_area_id or normalized["destination_location_id"],
        "destination_area_id",
        MAX_ID_LENGTH,
    )
    process = {
        "process_id": process_id,
        "kind": "world_travel_walk",
        "starts_at": normalized["started_at"],
        "expected_end_at": normalized["expected_arrival_at"],
        "location": {
            "place_id": normalized["destination_location_id"],
            "area_id": location_area,
        },
        "status_flags": {"cancelled": False},
        "metadata": {
            "travel_id": normalized["travel_id"],
            "execution_id": normalized["execution_id"],
            "mode": TRAVEL_MODE_WALK,
            "route_id": normalized["route_id"],
            "route_revision": normalized["route_revision"],
            "route_duration_seconds": normalized["route_duration_seconds"],
        },
    }
    lock_path = path.with_name(path.name + ".travel.lock")
    with lock_path.open("a+") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            store = wt.WorldTimelineStore(path)
            timeline = store.load()
            if timeline is None:
                return {"status": "unavailable", "reason_code": "TIMELINE_NOT_INITIALIZED"}
            if any(item.get("process_id") == process_id for item in timeline["processes"]):
                return {"status": "already_present", "process_id": process_id}
            updated = _copy_json(timeline)
            updated["revision"] = timeline["revision"] + 1
            updated["processes"].append(process)
            store.save(updated, expected_revision=updated["revision"])
            return {
                "status": "created",
                "process_id": process_id,
                "revision": updated["revision"],
            }
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def start_travel(
    *,
    execution_id: Any,
    origin_location_id: Any,
    destination_location_id: Any,
    destination_area_id: Optional[str] = None,
    hermes_home: Optional[Path | str] = None,
    environment: Optional[Mapping[str, str]] = None,
    now: Any = None,
) -> dict[str, Any]:
    """Durably start one WALK process; it does not change World location."""

    if not travel_enabled(environment):
        return {
            "kind": TRAVEL_PROJECTION_KIND,
            "status": "disabled",
            "reason_code": "FEATURE_DISABLED",
        }
    try:
        execution = _id(execution_id, "execution_id")
        origin = _id(origin_location_id, "origin_location_id")
        destination = _id(destination_location_id, "destination_location_id")
        duration = route_duration_seconds(origin, destination)
        if duration is None:
            raise TravelDeliveryError("route is not reachable")
        started = _instant(now)
    except TravelDeliveryError as exc:
        return {
            "kind": TRAVEL_PROJECTION_KIND,
            "status": "rejected",
            "reason_code": "INVALID_TRAVEL_REQUEST",
            "error_type": type(exc).__name__,
        }
    expected = started + timedelta(seconds=duration)
    record = {
        "travel_id": _stable_id(execution, origin, destination),
        "execution_id": execution,
        "actor": "chiyo",
        "origin_location_id": origin,
        "destination_location_id": destination,
        "started_at": _timestamp(started, "started_at"),
        "expected_arrival_at": _timestamp(expected, "expected_arrival_at"),
        "arrived_at": None,
        "travel_mode": TRAVEL_MODE_WALK,
        "route_id": f"{origin}->{destination}",
        "route_revision": ROUTE_REVISION,
        "route_duration_seconds": duration,
        "status": TRAVEL_STATUS_IN_PROGRESS,
        "provenance": "verified_main_self_move_to",
    }
    path = state_path(hermes_home)
    try:
        with _lock(path):
            state = _load(path) or _empty_state()
            existing = next(
                (item for item in state["travels"] if item["execution_id"] == execution),
                None,
            )
            if existing is not None:
                if (
                    existing["origin_location_id"] != origin
                    or existing["destination_location_id"] != destination
                ):
                    return {
                        "kind": TRAVEL_PROJECTION_KIND,
                        "status": "rejected",
                        "reason_code": "EXECUTION_ID_ROUTE_CONFLICT",
                    }
                projection = _travel_projection(
                    existing,
                    hermes_home=_home(hermes_home),
                    now=now,
                )
                return {
                    **projection,
                    "status": "already_started",
                    "reason_code": "IDEMPOTENT_REPLAY",
                    "timeline_status": "already_present",
                }
            active = _active_travel(state)
            if active is not None:
                return {
                    "kind": TRAVEL_PROJECTION_KIND,
                    "status": "rejected",
                    "reason_code": "TRAVEL_ALREADY_IN_PROGRESS",
                }
            normalized = _validate_travel(record)
            updated = dict(state)
            updated["revision"] = state["revision"] + 1
            updated["travels"] = [*state["travels"], normalized]
            _save(path, updated)
        timeline_result = ensure_travel_timeline_process(
            normalized,
            hermes_home=hermes_home,
            destination_area_id=destination_area_id,
        )
        projection = _travel_projection(
            normalized,
            hermes_home=_home(hermes_home),
            now=now,
        )
        return {
            **projection,
            "status": "started",
            "reason_code": "TRAVEL_STARTED",
            "timeline_status": timeline_result.get("status"),
        }
    except TravelDeliveryError as exc:
        return {
            "kind": TRAVEL_PROJECTION_KIND,
            "status": "failed",
            "reason_code": "TRAVEL_STATE_ERROR",
            "error_type": type(exc).__name__,
        }


def observe_travel(
    *,
    hermes_home: Optional[Path | str] = None,
    environment: Optional[Mapping[str, str]] = None,
    now: Any = None,
) -> dict[str, Any]:
    if not travel_enabled(environment):
        return {
            "kind": TRAVEL_PROJECTION_KIND,
            "status": "disabled",
            "reason_code": "FEATURE_DISABLED",
        }
    state = load_state(hermes_home)
    active = _active_travel(state)
    if active is None:
        return {"kind": TRAVEL_PROJECTION_KIND, "status": "none"}
    return _travel_projection(active, hermes_home=_home(hermes_home), now=now)


def realize_due_travel(
    *,
    hermes_home: Optional[Path | str] = None,
    environment: Optional[Mapping[str, str]] = None,
    now: Any = None,
) -> dict[str, Any]:
    """Materialize one due arrival with crash-safe, idempotent ordering."""

    if not travel_enabled(environment):
        return {
            "kind": TRAVEL_PROJECTION_KIND,
            "status": "disabled",
            "reason_code": "FEATURE_DISABLED",
        }
    home = _home(hermes_home)
    travel_path = state_path(home)
    if not travel_path.exists():
        return {"kind": TRAVEL_PROJECTION_KIND, "status": "none"}
    instant = _instant(now)
    world_path = world_state_path(home)
    try:
        # Arrival writers take world lock before travel lock.  MOVE_TO starts
        # while holding the same world lock, so lock acquisition is ordered.
        with _world_lock(world_path):
            with _lock(travel_path):
                state = _load(travel_path)
                active = _active_travel(state)
                if active is None:
                    return {"kind": TRAVEL_PROJECTION_KIND, "status": "none"}
                if instant < _instant(active["expected_arrival_at"]):
                    return {
                        **_travel_projection(active, hermes_home=home, now=instant),
                        "status": "in_progress",
                        "reason_code": "ARRIVAL_NOT_DUE",
                    }
                world_store = wf.WorldStateStore(world_path)
                world_state = world_store.load()
                if world_state is None:
                    return {
                        "kind": TRAVEL_PROJECTION_KIND,
                        "status": "unavailable",
                        "reason_code": "WORLD_NOT_INITIALIZED",
                    }
                current = world_state["location"]["place_id"]
                destination = active["destination_location_id"]
                origin = active["origin_location_id"]
                world_changed = False
                if current == origin:
                    destination_area = "bedroom" if destination == "home" else destination
                    updated_world = _copy_json(world_state)
                    updated_world["revision"] = world_state["revision"] + 1
                    updated_world["location"] = {
                        "place_id": destination,
                        "area_id": destination_area,
                    }
                    updated_world["scene"] = {
                        "scene_id": f"{destination}.{destination_area}",
                        "revision": world_state["scene"]["revision"] + 1,
                    }
                    world_store.save(
                        updated_world,
                        expected_revision=updated_world["revision"],
                    )
                    verified_world = world_store.load()
                    if (
                        verified_world is None
                        or verified_world["location"]["place_id"] != destination
                    ):
                        return {
                            "kind": TRAVEL_PROJECTION_KIND,
                            "status": "uncertain",
                            "reason_code": "WORLD_POSTCONDITION_FAILED",
                        }
                    world_changed = True
                elif current != destination:
                    return {
                        "kind": TRAVEL_PROJECTION_KIND,
                        "status": "rejected",
                        "reason_code": "WORLD_LOCATION_CONFLICT",
                        "current_location_id": current,
                    }
                completed = dict(active)
                completed["status"] = TRAVEL_STATUS_ARRIVED
                completed["arrived_at"] = _timestamp(instant, "arrived_at")
                updated_state = dict(state)
                updated_state["revision"] = state["revision"] + 1
                updated_state["travels"] = [
                    completed
                    if item["travel_id"] == active["travel_id"]
                    else item
                    for item in state["travels"]
                ]
                _save(travel_path, updated_state)
                projection = _travel_projection(
                    completed,
                    hermes_home=home,
                    now=instant,
                )
                return {
                    **projection,
                    "status": "arrived",
                    "reason_code": "ARRIVAL_REALIZED",
                    "world_changed": world_changed,
                }
    except TravelDeliveryError as exc:
        return {
            "kind": TRAVEL_PROJECTION_KIND,
            "status": "failed",
            "reason_code": "ARRIVAL_REALIZATION_FAILED",
            "error_type": type(exc).__name__,
        }


def ensure_delivery_timeline_process(
    delivery: Mapping[str, Any],
    *,
    hermes_home: Optional[Path | str] = None,
) -> dict[str, Any]:
    """Append one idempotent factual home-delivery process to Timeline."""

    required = {
        "delivery_id",
        "purchase_transaction_id",
        "purchase_intent_id",
        "item_instance_id",
        "destination_location",
        "purchased_at",
        "expected_delivery_at",
        "status",
        "delivery_provenance",
    }
    if not isinstance(delivery, Mapping) or not required.issubset(set(delivery)):
        return {"status": "rejected", "reason_code": "INVALID_DELIVERY"}
    try:
        delivery_id = _id(delivery.get("delivery_id"), "delivery_id")
        purchased_at = _timestamp(delivery.get("purchased_at"), "purchased_at")
        expected = _timestamp(
            delivery.get("expected_delivery_at"),
            "expected_delivery_at",
        )
        if _instant(expected) <= _instant(purchased_at):
            raise TravelDeliveryError("delivery ETA must be later than purchase")
        transaction_id = _id(
            delivery.get("purchase_transaction_id"),
            "purchase_transaction_id",
        )
        intent_id = _id(delivery.get("purchase_intent_id"), "purchase_intent_id")
        item_id = _id(delivery.get("item_instance_id"), "item_instance_id")
    except TravelDeliveryError as exc:
        return {
            "status": "rejected",
            "reason_code": "INVALID_DELIVERY",
            "error_type": type(exc).__name__,
        }
    if delivery.get("destination_location") != "home/bedroom":
        return {"status": "rejected", "reason_code": "INVALID_DELIVERY_DESTINATION"}
    if delivery.get("status") not in {"PENDING", "DELIVERED"}:
        return {"status": "rejected", "reason_code": "INVALID_DELIVERY_STATUS"}
    if delivery.get("delivery_provenance") != "temporal_home_delivery":
        return {"status": "rejected", "reason_code": "INVALID_DELIVERY_PROVENANCE"}
    path = timeline_path(hermes_home)
    if not path.exists() or fcntl is None:
        return {"status": "unavailable", "reason_code": "TIMELINE_NOT_INITIALIZED"}
    process_id = "delivery-process-" + hashlib.sha256(
        delivery_id.encode("utf-8")
    ).hexdigest()
    process = {
        "process_id": process_id,
        "kind": "world_home_delivery",
        "starts_at": purchased_at,
        "expected_end_at": expected,
        "location": {"place_id": "home", "area_id": "bedroom"},
        "status_flags": {"cancelled": False},
        "metadata": {
            "delivery_id": delivery_id,
            "purchase_transaction_id": transaction_id,
            "purchase_intent_id": intent_id,
            "item_instance_id": item_id,
            "mode": DELIVERY_MODE,
            "delivery_duration_seconds": DELIVERY_DURATION_SECONDS,
        },
    }
    lock_path = path.with_name(path.name + ".delivery.lock")
    with lock_path.open("a+") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            store = wt.WorldTimelineStore(path)
            timeline = store.load()
            if timeline is None:
                return {
                    "status": "unavailable",
                    "reason_code": "TIMELINE_NOT_INITIALIZED",
                }
            if any(item.get("process_id") == process_id for item in timeline["processes"]):
                return {"status": "already_present", "process_id": process_id}
            updated = _copy_json(timeline)
            updated["revision"] = timeline["revision"] + 1
            updated["processes"].append(process)
            store.save(updated, expected_revision=updated["revision"])
            return {
                "status": "created",
                "process_id": process_id,
                "revision": updated["revision"],
            }
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


__all__ = [
    "DELIVERY_DURATION_SECONDS",
    "DELIVERY_FEATURE_ENV",
    "DELIVERY_MODE",
    "ROUTE_REVISION",
    "SCHEMA_VERSION",
    "STATE_FILENAME",
    "TRAVEL_FEATURE_ENV",
    "TRAVEL_MODE_WALK",
    "TRAVEL_PROJECTION_KIND",
    "TRAVEL_STATUS_ARRIVED",
    "TRAVEL_STATUS_IN_PROGRESS",
    "TravelDeliveryError",
    "WALKING_DURATIONS",
    "delivery_enabled",
    "direct_execution_id",
    "ensure_delivery_timeline_process",
    "ensure_travel_timeline_process",
    "feature_enabled",
    "load_state",
    "observe_travel",
    "plan_travel",
    "realize_due_travel",
    "route_duration_seconds",
    "route_id_for",
    "start_travel",
    "state_path",
    "timeline_path",
    "travel_enabled",
    "validate_state",
]
