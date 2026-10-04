#!/usr/bin/env python3
"""Bounded World Timeline and read-time Temporal Process contract."""

from __future__ import annotations

import argparse
import json
import math
import os
import tempfile
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from world_foundation import WorldStateStore

TIMELINE_SCHEMA_VERSION = "world.timeline.v1"
WORLD_ID = "chiyo-world"
LIVING_WORLD_ID = "shiomi_city"
SUPPORTED_WORLD_IDS = frozenset({WORLD_ID, LIVING_WORLD_ID})
MAX_PROCESSES = 100
MAX_METADATA_KEYS = 16
MAX_METADATA_LIST_ITEMS = 32
MAX_METADATA_DEPTH = 4
MAX_METADATA_BYTES = 2048
MAX_ID_LENGTH = 128
MAX_TEXT_LENGTH = 240

PHASE_SCHEDULED = "SCHEDULED"
PHASE_ACTIVE = "ACTIVE"
PHASE_ELAPSED = "ELAPSED"
PHASE_CANCELLED = "CANCELLED"

_TIMELINE_KEYS = {"schema_version", "revision", "world_id", "processes"}
_PROCESS_KEYS = {
    "process_id", "kind", "starts_at", "expected_end_at", "location",
    "status_flags", "metadata",
}
_LOCATION_KEYS = {"place_id", "area_id"}
_STATUS_FLAG_KEYS = {"cancelled"}
_FORBIDDEN_FIELDS = {
    "want", "priority", "desire", "utility", "score", "emotion",
    "recommendation", "action", "candidate",
}


class WorldTimelineError(ValueError):
    """Raised when a Timeline or Temporal Process violates its contract."""


class WorldTimelineGenesisError(WorldTimelineError):
    """Raised when one-time Timeline creation cannot proceed safely."""


def _require_text(value: Any, *, field: str, maximum: int = MAX_TEXT_LENGTH) -> str:
    if not isinstance(value, str):
        raise WorldTimelineError(f"{field} must be a string")
    normalized = value.strip()
    if not normalized or len(normalized) > maximum:
        raise WorldTimelineError(f"{field} must be non-empty and bounded")
    return normalized


def _require_revision(value: Any, *, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise WorldTimelineError(f"{field} must be a non-negative integer")
    return value


def _require_object(value: Any, *, field: str, expected: set[str]) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != expected:
        raise WorldTimelineError(f"{field} must have exactly the required fields")
    return value


def _parse_timestamp(value: Any, *, field: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise WorldTimelineError(f"{field} must be a timezone-aware ISO datetime")
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise WorldTimelineError(f"{field} must be a valid ISO datetime") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise WorldTimelineError(f"{field} must include a timezone")
    return parsed


def _canonical_json(value: Any, *, field: str) -> Any:
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
        raise WorldTimelineError(f"{field} must be bounded JSON") from exc


def _forbidden_key(value: str) -> bool:
    normalized = value.strip().lower()
    root = normalized.split("_", 1)[0]
    return normalized in _FORBIDDEN_FIELDS or root in _FORBIDDEN_FIELDS


def _bounded_metadata(value: Any, *, field: str, depth: int = 0) -> Any:
    if depth > MAX_METADATA_DEPTH:
        raise WorldTimelineError(f"{field} exceeds metadata depth bound")
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, str):
        if len(value) > MAX_TEXT_LENGTH:
            raise WorldTimelineError(f"{field} contains an overlong string")
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise WorldTimelineError(f"{field} contains a non-finite number")
        return value
    if isinstance(value, Mapping):
        if len(value) > MAX_METADATA_KEYS:
            raise WorldTimelineError(f"{field} exceeds metadata key bound")
        result: dict[str, Any] = {}
        for key, nested in value.items():
            if not isinstance(key, str) or not key.strip():
                raise WorldTimelineError(f"{field} keys must be bounded strings")
            normalized_key = key.strip()
            if _forbidden_key(normalized_key):
                raise WorldTimelineError(f"{field} contains forbidden semantic field")
            result[normalized_key] = _bounded_metadata(
                nested,
                field=f"{field}.{normalized_key}",
                depth=depth + 1,
            )
        encoded = json.dumps(
            result,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        if len(encoded) > MAX_METADATA_BYTES:
            raise WorldTimelineError(f"{field} exceeds metadata byte bound")
        return result
    if isinstance(value, list):
        if len(value) > MAX_METADATA_LIST_ITEMS:
            raise WorldTimelineError(f"{field} exceeds metadata list bound")
        result = [
            _bounded_metadata(item, field=f"{field}[{index}]", depth=depth + 1)
            for index, item in enumerate(value)
        ]
        encoded = json.dumps(
            result,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        if len(encoded) > MAX_METADATA_BYTES:
            raise WorldTimelineError(f"{field} exceeds metadata byte bound")
        return result
    raise WorldTimelineError(f"{field} contains unsupported JSON")


def validate_process(process: Mapping[str, Any]) -> dict[str, Any]:
    """Validate one Temporal Process and return a detached normalized copy."""

    if not isinstance(process, Mapping) or set(process) != _PROCESS_KEYS:
        raise WorldTimelineError("Temporal Process has an invalid strict shape")
    process_id = _require_text(process["process_id"], field="process_id", maximum=MAX_ID_LENGTH)
    kind = _require_text(process["kind"], field="kind", maximum=80)
    if _forbidden_key(kind):
        raise WorldTimelineError("kind is not a temporal process kind")
    starts_at = _parse_timestamp(process["starts_at"], field="starts_at")
    expected_end_at = _parse_timestamp(process["expected_end_at"], field="expected_end_at")
    if expected_end_at <= starts_at:
        raise WorldTimelineError("expected_end_at must be later than starts_at")
    location = _require_object(process["location"], field="location", expected=_LOCATION_KEYS)
    status_flags = _require_object(
        process["status_flags"], field="status_flags", expected=_STATUS_FLAG_KEYS
    )
    if not isinstance(status_flags["cancelled"], bool):
        raise WorldTimelineError("status_flags.cancelled must be boolean")
    metadata = _bounded_metadata(process["metadata"], field="metadata")
    return _canonical_json(
        {
            "process_id": process_id,
            "kind": kind,
            "starts_at": starts_at.isoformat(),
            "expected_end_at": expected_end_at.isoformat(),
            "location": {
                "place_id": _require_text(
                    location["place_id"], field="location.place_id", maximum=MAX_ID_LENGTH
                ),
                "area_id": _require_text(
                    location["area_id"], field="location.area_id", maximum=MAX_ID_LENGTH
                ),
            },
            "status_flags": {"cancelled": status_flags["cancelled"]},
            "metadata": metadata,
        },
        field="process",
    )


def validate_timeline(timeline: Mapping[str, Any]) -> dict[str, Any]:
    """Validate and normalize a strict World Timeline V1 document."""

    if not isinstance(timeline, Mapping) or set(timeline) != _TIMELINE_KEYS:
        raise WorldTimelineError("Timeline has an invalid strict shape")
    schema_version = _require_text(timeline["schema_version"], field="schema_version", maximum=80)
    if schema_version != TIMELINE_SCHEMA_VERSION:
        raise WorldTimelineError("unsupported Timeline schema_version")
    revision = _require_revision(timeline["revision"], field="revision")
    world_id = _require_text(timeline["world_id"], field="world_id", maximum=MAX_ID_LENGTH)
    processes = timeline["processes"]
    if not isinstance(processes, list) or len(processes) > MAX_PROCESSES:
        raise WorldTimelineError("processes must be a bounded list")
    normalized_processes: list[dict[str, Any]] = []
    process_ids: set[str] = set()
    for process in processes:
        normalized = validate_process(process)
        if normalized["process_id"] in process_ids:
            raise WorldTimelineError("process_id must be unique within a Timeline")
        process_ids.add(normalized["process_id"])
        normalized_processes.append(normalized)
    return _canonical_json(
        {
            "schema_version": schema_version,
            "revision": revision,
            "world_id": world_id,
            "processes": normalized_processes,
        },
        field="timeline",
    )


def _require_aware_now(value: Optional[datetime]) -> datetime:
    instant = datetime.now(timezone.utc) if value is None else value
    if not isinstance(instant, datetime) or instant.tzinfo is None or instant.utcoffset() is None:
        raise WorldTimelineError("now must be a timezone-aware datetime")
    return instant


def resolve_phase(process: Mapping[str, Any], now: Optional[datetime] = None) -> str:
    """Derive a process phase from wall-clock time without writing the Timeline."""

    normalized = validate_process(process)
    instant = _require_aware_now(now)
    starts_at = _parse_timestamp(normalized["starts_at"], field="starts_at")
    expected_end_at = _parse_timestamp(normalized["expected_end_at"], field="expected_end_at")
    if normalized["status_flags"]["cancelled"]:
        return PHASE_CANCELLED
    if instant < starts_at:
        return PHASE_SCHEDULED
    if instant < expected_end_at:
        return PHASE_ACTIVE
    return PHASE_ELAPSED


class WorldTimelineStore:
    """Injected-path Timeline store with strict read and atomic write semantics."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        if not self.path.is_absolute():
            raise WorldTimelineError("Timeline path must be absolute and injected")
        if self.path.exists() and self.path.is_dir():
            raise WorldTimelineError("Timeline path must be a file")

    def load(self) -> Optional[dict[str, Any]]:
        """Return a valid Timeline or None for missing/malformed data."""

        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            return validate_timeline(raw)
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            return None

    def save(
        self,
        timeline: Mapping[str, Any],
        *,
        expected_revision: Optional[int] = None,
    ) -> dict[str, Any]:
        normalized = validate_timeline(timeline)
        if expected_revision is not None:
            if isinstance(expected_revision, bool) or not isinstance(expected_revision, int):
                raise WorldTimelineError("expected_revision must be an integer")
            if normalized["revision"] != expected_revision:
                raise WorldTimelineError("Timeline revision mismatch")
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
                prefix=f".{self.path.name}.", dir=str(parent), text=True
            )
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_path, self.path)
            temporary_path = None
        except OSError as exc:
            raise WorldTimelineError("Timeline could not be written atomically") from exc
        finally:
            if temporary_path is not None:
                try:
                    os.unlink(temporary_path)
                except OSError:
                    pass
        return normalized


def is_supported_world_id(value: Any) -> bool:
    return isinstance(value, str) and value in SUPPORTED_WORLD_IDS


def build_initial_timeline(world_id: str = WORLD_ID) -> dict[str, Any]:
    """Return the deliberately empty World Timeline V1 genesis document."""

    if not is_supported_world_id(world_id):
        raise WorldTimelineError("world_id is not supported")
    return validate_timeline(
        {
            "schema_version": TIMELINE_SCHEMA_VERSION,
            "revision": 1,
            "world_id": _require_text(world_id, field="world_id", maximum=MAX_ID_LENGTH),
            "processes": [],
        }
    )


def _path_exists(path: Path) -> bool:
    return os.path.lexists(path)


def _claim_path(path: Path) -> Path:
    return path.with_name(f".{path.name}.genesis-claim")


def create_genesis(path: Path | str, world_state_path: Path | str) -> dict[str, Any]:
    """Create one empty Timeline only after validating the existing World State."""

    target = Path(path)
    world_path = Path(world_state_path)
    if not target.is_absolute() or not world_path.is_absolute():
        raise WorldTimelineGenesisError(
            "Timeline and World State paths must be absolute and explicitly injected"
        )
    if _path_exists(target):
        raise WorldTimelineGenesisError("Timeline already exists; refusing overwrite")
    world_state = WorldStateStore(world_path).load()
    if world_state is None:
        raise WorldTimelineGenesisError("World State is missing or malformed")
    if not is_supported_world_id(world_state["world_id"]):
        raise WorldTimelineGenesisError("World State world_id is not supported")

    target.parent.mkdir(parents=True, exist_ok=True)
    claim = _claim_path(target)
    claim_created = False
    try:
        try:
            claim.mkdir(mode=0o700)
            claim_created = True
        except FileExistsError as exc:
            raise WorldTimelineGenesisError(
                "Timeline genesis claim already exists; refusing concurrent creation"
            ) from exc
        if _path_exists(target):
            raise WorldTimelineGenesisError("Timeline appeared during genesis; refusing overwrite")
        expected = build_initial_timeline(WORLD_ID)
        WorldTimelineStore(target).save(expected, expected_revision=1)
        loaded = WorldTimelineStore(target).load()
        if loaded is None or loaded != expected:
            raise WorldTimelineGenesisError("created Timeline failed validation")
        return {
            "status": "created",
            "path": str(target),
            "schema_version": loaded["schema_version"],
            "world_id": loaded["world_id"],
            "revision": loaded["revision"],
            "processes_count": len(loaded["processes"]),
            "validated": True,
        }
    finally:
        if claim_created:
            try:
                claim.rmdir()
            except OSError:
                pass


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Create one empty World Timeline V1 state")
    parser.add_argument("--path", required=True, help="absolute Timeline path")
    parser.add_argument("--world-state-path", required=True, help="absolute World State path")
    args = parser.parse_args(argv)
    try:
        receipt = create_genesis(args.path, args.world_state_path)
    except (WorldTimelineError, OSError) as exc:
        print(json.dumps({"status": "not_created", "reason": str(exc)}, ensure_ascii=False))
        return 2
    print(json.dumps(receipt, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
