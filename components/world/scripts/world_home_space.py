#!/usr/bin/env python3
"""Bounded factual Home / Personal Space V1.

World facts are static, explicitly authored facts.  Self-owned details live in
a separate small state file with provenance; this module never infers them
from prose, creates Life work, or touches World Timeline/Experience.
"""

from __future__ import annotations

import copy
import json
import os
import tempfile
from collections.abc import Mapping
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Optional

try:
    import fcntl
except ImportError:  # pragma: no cover - production is POSIX
    fcntl = None


FEATURE_ENV = "CHIYO_WORLD_HOME_SPACE_ENABLED"
WORLD_ID = "shiomi_city"
HOME_LOCATION_ID = "home"
BEDROOM_ID = "bedroom"
COURTYARD_ID = "courtyard"
SCHEMA_VERSION = "world.home.personal_space.v1"
PROJECTION_KIND = "shiomi_home_observation"
STATE_FILENAME = "home_personal_space.json"
MAX_VALUE_LENGTH = 160
MAX_SOURCE_ID_LENGTH = 256
MAX_PROJECTION_BYTES = 12_000

_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
_STATE_KEYS = frozenset({"schema_version", "revision", "details"})
_DETAIL_KEYS = frozenset(
    {
        "field",
        "value",
        "chosen_by",
        "chosen_at",
        "source_turn_id",
        "source_message_id",
        "revision",
    }
)

# These are deliberately fields, not a general JSON editor.  Values are
# bounded scalars and the orientation has a closed vocabulary.
ALLOWED_FIELDS = frozenset(
    {
        "bedroom.window_orientation",
        "bedroom.window_view",
        "courtyard.seating",
        "courtyard.decorative_detail",
    }
)
ORIENTATION_VALUES = frozenset({"north", "east", "south", "west"})
UNDECIDED_FIELDS = (
    "bedroom.window_orientation",
    "bedroom.window_view",
    "bedroom.sleeping_area",
    "bedroom.desk_present",
    "bedroom.storage_present",
    "courtyard.seating",
    "courtyard.decorative_detail",
)

_INITIAL_TREE = {
    "object_id": "osmanthus_tree",
    "object_type": "tree",
    "species": "osmanthus",
    "location_id": COURTYARD_ID,
    "description": "院子里有一棵桂花树",
    "origin": "world_initial_design",
}


class HomeSpaceError(ValueError):
    """Raised when a Home / Personal Space contract is invalid."""


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
        raise HomeSpaceError("value is not bounded JSON") from exc


def _environment(environment: Optional[Mapping[str, str]]) -> Mapping[str, str]:
    return os.environ if environment is None else environment


def _truthy(value: Any) -> bool:
    return str(value or "").strip().lower() in _TRUE_VALUES


def feature_enabled(environment: Optional[Mapping[str, str]] = None) -> bool:
    return _truthy(_environment(environment).get(FEATURE_ENV))


def _home(hermes_home: Optional[Path | str] = None) -> Path:
    value = hermes_home if hermes_home is not None else os.environ.get("HERMES_HOME")
    path = Path(value).expanduser() if value else Path.home() / ".hermes"
    if not path.is_absolute():
        raise HomeSpaceError("hermes_home must be absolute")
    return path


def personal_space_path(hermes_home: Optional[Path | str] = None) -> Path:
    return _home(hermes_home) / "data" / "world" / STATE_FILENAME


def _text(value: Any, field: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise HomeSpaceError(f"{field} must be a string")
    result = value.strip()
    if not result or len(result) > maximum or any(char in result for char in "\r\n\x00"):
        raise HomeSpaceError(f"{field} is invalid")
    return result


def _identity(value: Any, field: str, *, optional: bool = False) -> Optional[str]:
    if value is None and optional:
        return None
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise HomeSpaceError(f"{field} is invalid")
    result = _text(str(value), field, MAX_SOURCE_ID_LENGTH)
    if any(char.isspace() for char in result):
        raise HomeSpaceError(f"{field} is invalid")
    return result


def _value(field: Any, value: Any) -> tuple[str, str]:
    normalized_field = _text(field, "field", 96)
    if normalized_field not in ALLOWED_FIELDS:
        raise HomeSpaceError("unsupported home detail field")
    normalized_value = _text(value, "value", MAX_VALUE_LENGTH)
    if normalized_field == "bedroom.window_orientation" and normalized_value not in ORIENTATION_VALUES:
        raise HomeSpaceError("window_orientation value is unsupported")
    return normalized_field, normalized_value


def _timestamp(value: Any = None) -> str:
    instant = datetime.now(timezone.utc) if value is None else value
    if not isinstance(instant, datetime) or instant.tzinfo is None or instant.utcoffset() is None:
        raise HomeSpaceError("chosen_at must be timezone-aware")
    return instant.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _empty_state() -> dict[str, Any]:
    return {"schema_version": SCHEMA_VERSION, "revision": 0, "details": []}


def _validate_state(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != _STATE_KEYS:
        raise HomeSpaceError("personal space state shape is invalid")
    if value.get("schema_version") != SCHEMA_VERSION:
        raise HomeSpaceError("personal space schema is unsupported")
    revision = value.get("revision")
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
        raise HomeSpaceError("personal space revision is invalid")
    details = value.get("details")
    if not isinstance(details, list) or len(details) > len(ALLOWED_FIELDS):
        raise HomeSpaceError("personal space details are invalid")
    seen: set[str] = set()
    normalized: list[dict[str, Any]] = []
    for index, detail in enumerate(details):
        if not isinstance(detail, Mapping) or set(detail) != _DETAIL_KEYS:
            raise HomeSpaceError(f"personal space detail {index} is invalid")
        field, normalized_value = _value(detail.get("field"), detail.get("value"))
        if field in seen:
            raise HomeSpaceError("personal space fields must be unique")
        seen.add(field)
        chosen_by = _text(detail.get("chosen_by"), "chosen_by", 64)
        if chosen_by != "verified_main_self":
            raise HomeSpaceError("personal detail provenance is invalid")
        chosen_at = _text(detail.get("chosen_at"), "chosen_at", 96)
        source_turn_id = _identity(detail.get("source_turn_id"), "source_turn_id")
        source_message_id = _identity(
            detail.get("source_message_id"),
            "source_message_id",
            optional=True,
        )
        detail_revision = detail.get("revision")
        if (
            isinstance(detail_revision, bool)
            or not isinstance(detail_revision, int)
            or detail_revision < 1
            or detail_revision > revision
        ):
            raise HomeSpaceError("personal detail revision is invalid")
        normalized.append(
            {
                "field": field,
                "value": normalized_value,
                "chosen_by": chosen_by,
                "chosen_at": chosen_at,
                "source_turn_id": source_turn_id,
                "source_message_id": source_message_id,
                "revision": detail_revision,
            }
        )
    normalized.sort(key=lambda item: item["field"])
    return _copy_json(
        {"schema_version": SCHEMA_VERSION, "revision": revision, "details": normalized}
    )


def _load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return _empty_state()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise HomeSpaceError("personal space state cannot be read") from exc
    return _validate_state(raw)


def _save_state(path: Path, state: Mapping[str, Any]) -> dict[str, Any]:
    normalized = _validate_state(state)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(
        normalized,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ) + "\n"
    temporary: Optional[str] = None
    try:
        descriptor, temporary = tempfile.mkstemp(
            prefix=f".{path.name}.",
            dir=str(path.parent),
            text=True,
        )
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
    except OSError as exc:
        raise HomeSpaceError("personal space state could not be written") from exc
    finally:
        if temporary is not None:
            try:
                os.unlink(temporary)
            except OSError:
                pass
    return normalized


@contextmanager
def _state_lock(path: Path) -> Iterator[None]:
    if fcntl is None:
        raise HomeSpaceError("personal space lock unavailable")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_name(path.name + ".lock").open("a+") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _world_state(hermes_home: Path, supplied: Optional[Mapping[str, Any]]) -> Optional[Mapping[str, Any]]:
    if supplied is not None:
        return supplied
    try:
        from world_foundation import WorldStateStore

        return WorldStateStore(hermes_home / "data" / "world" / "world_state.json").load()
    except Exception:
        return None


def _detail_projection(detail: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "field": detail["field"],
        "value": detail["value"],
        "chosen_by": detail["chosen_by"],
        "revision": detail["revision"],
    }


def _bedroom_facts(details: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    facts: dict[str, Any] = {"room_id": BEDROOM_ID}
    for field in UNDECIDED_FIELDS:
        if not field.startswith("bedroom."):
            continue
        short = field.split(".", 1)[1]
        detail = details.get(field)
        facts[short] = (
            {"status": "SET_BY_SELF", "value": detail["value"], "revision": detail["revision"]}
            if detail is not None
            else {"status": "UNSET"}
        )
    return facts


def observe_home(
    *,
    hermes_home: Optional[Path | str] = None,
    world_state: Optional[Mapping[str, Any]] = None,
    environment: Optional[Mapping[str, str]] = None,
) -> dict[str, Any]:
    """Return a bounded Home projection without writing any state."""

    if not feature_enabled(environment):
        return {
            "kind": PROJECTION_KIND,
            "schema_version": SCHEMA_VERSION,
            "status": "disabled",
            "reason_code": "FEATURE_DISABLED",
        }
    home = _home(hermes_home)
    state = _world_state(home, world_state)
    if not isinstance(state, Mapping) or state.get("world_id") != WORLD_ID:
        return {
            "kind": PROJECTION_KIND,
            "schema_version": SCHEMA_VERSION,
            "status": "unavailable",
            "reason_code": "WORLD_NOT_INITIALIZED",
        }
    location = state.get("location")
    if not isinstance(location, Mapping) or not isinstance(location.get("place_id"), str):
        return {
            "kind": PROJECTION_KIND,
            "schema_version": SCHEMA_VERSION,
            "status": "unavailable",
            "reason_code": "WORLD_LOCATION_UNAVAILABLE",
        }
    try:
        personal = _load_state(personal_space_path(home))
    except HomeSpaceError:
        return {
            "kind": PROJECTION_KIND,
            "schema_version": SCHEMA_VERSION,
            "status": "unavailable",
            "reason_code": "PERSONAL_SPACE_STATE_INVALID",
        }
    details = {item["field"]: item for item in personal["details"]}
    undecided = [
        {"field": field, "status": "UNSET"}
        for field in UNDECIDED_FIELDS
        if field not in details
    ]
    bedroom = {
        "space_id": BEDROOM_ID,
        "location_id": HOME_LOCATION_ID,
        "kind": "bedroom",
        "facts": _bedroom_facts(details),
    }
    courtyard = {
        "space_id": COURTYARD_ID,
        "location_id": HOME_LOCATION_ID,
        "kind": "courtyard",
        "facts": {},
        "objects": [_copy_json(_INITIAL_TREE)],
    }
    result = {
        "kind": PROJECTION_KIND,
        "schema_version": SCHEMA_VERSION,
        "status": "available",
        "world_id": WORLD_ID,
        "home_location": {
            "location_id": HOME_LOCATION_ID,
            "area_id": BEDROOM_ID,
            "identity": "home/bedroom",
        },
        "current_location": {
            "location_id": str(location["place_id"]),
            "area_id": str(location.get("area_id") or ""),
        },
        "spaces": [bedroom, courtyard],
        "personal_details": [_detail_projection(details[field]) for field in sorted(details)],
        "undecided_fields": undecided,
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


def customize_home_detail(
    *,
    field: Any,
    value: Any,
    main_self_verified: bool,
    source_turn_id: Any,
    source_message_id: Any = None,
    chosen_at: Any = None,
    hermes_home: Optional[Path | str] = None,
    environment: Optional[Mapping[str, str]] = None,
) -> dict[str, Any]:
    """Persist one explicit verified Main Self detail from the strict allowlist."""

    if not feature_enabled(environment):
        return {
            "kind": PROJECTION_KIND,
            "status": "disabled",
            "reason_code": "FEATURE_DISABLED",
        }
    if main_self_verified is not True:
        return {
            "kind": PROJECTION_KIND,
            "status": "rejected",
            "reason_code": "VERIFIED_MAIN_SELF_REQUIRED",
        }
    try:
        normalized_field, normalized_value = _value(field, value)
        normalized_turn = _identity(source_turn_id, "source_turn_id")
        normalized_message = _identity(
            source_message_id,
            "source_message_id",
            optional=True,
        )
        timestamp = _timestamp(chosen_at)
        home = _home(hermes_home)
        path = personal_space_path(home)
        with _state_lock(path):
            current = _load_state(path)
            existing = next(
                (item for item in current["details"] if item["field"] == normalized_field),
                None,
            )
            if existing is not None and existing["value"] == normalized_value:
                return {
                    "kind": PROJECTION_KIND,
                    "status": "already_set",
                    "reason_code": "IDEMPOTENT_NOOP",
                    "field": normalized_field,
                    "value": normalized_value,
                    "revision": existing["revision"],
                }
            next_revision = current["revision"] + 1
            detail_revision = (existing["revision"] + 1) if existing is not None else 1
            updated_detail = {
                "field": normalized_field,
                "value": normalized_value,
                "chosen_by": "verified_main_self",
                "chosen_at": timestamp,
                "source_turn_id": normalized_turn,
                "source_message_id": normalized_message,
                "revision": detail_revision,
            }
            updated_details = [
                item for item in current["details"] if item["field"] != normalized_field
            ]
            updated_details.append(updated_detail)
            saved = _save_state(
                path,
                {
                    "schema_version": SCHEMA_VERSION,
                    "revision": next_revision,
                    "details": updated_details,
                },
            )
            verified = next(
                item for item in saved["details"] if item["field"] == normalized_field
            )
        return {
            "kind": PROJECTION_KIND,
            "status": "applied",
            "reason_code": "HOME_DETAIL_COMMITTED",
            "field": normalized_field,
            "value": normalized_value,
            "revision": verified["revision"],
            "provenance": {
                "chosen_by": verified["chosen_by"],
                "chosen_at": verified["chosen_at"],
                "source_turn_id": verified["source_turn_id"],
                "source_message_id": verified["source_message_id"],
                "revision": verified["revision"],
            },
        }
    except HomeSpaceError as exc:
        reason = str(exc)
        return {
            "kind": PROJECTION_KIND,
            "status": "rejected",
            "reason_code": "INVALID_HOME_DETAIL",
            "error_type": type(exc).__name__,
            "detail": reason,
        }
    except Exception:
        return {
            "kind": PROJECTION_KIND,
            "status": "failed",
            "reason_code": "HOME_DETAIL_WRITE_FAILED",
        }


__all__ = [
    "ALLOWED_FIELDS",
    "BEDROOM_ID",
    "COURTYARD_ID",
    "FEATURE_ENV",
    "HOME_LOCATION_ID",
    "HomeSpaceError",
    "ORIENTATION_VALUES",
    "PROJECTION_KIND",
    "SCHEMA_VERSION",
    "customize_home_detail",
    "feature_enabled",
    "observe_home",
    "personal_space_path",
]
