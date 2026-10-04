#!/usr/bin/env python3
"""Bounded World-side Embodiment Substrate (Body / Embodiment V1 prerequisite).

This module is the **World authority** for physical placement plus the minimum
body substrate that ``Body / Embodiment V1`` needs to exist inside the world:

* exactly one physical placement per object instance (placement authority);
* body entity ``pose`` / ``orientation``;
* body physical resource slots (hands) with **derived** occupancy;
* exclusive body-resource reservations for in-flight actions;
* reachability inputs derived from pose + free slots + placement location.

Design constraints (frozen by ``B0B_CONTRACT_FREEZE.md`` / this ticket):

* **Additive only.**  It does not modify ``world_state.json``,
  ``world_property_economy.json``, ``world_timeline.json``, location
  reachability, travel, ownership, the Life executor, or the legacy HeartMind
  state.  Its own state lives in ``data/world/body_substrate.json``.
* **Importing has no runtime effect.**  A caller must inject ``hermes_home``
  and explicitly call a function.  There is no scheduler, no tick, no daemon,
  no gateway hook and no LLM call.
* **No second truth.**  There is no ``inventory`` list and no stored
  ``left_hand``/``right_hand`` value.  Hand occupancy is *always* derived by
  scanning the placement map, so a hand query can never diverge from the
  placement it came from.
* **Deterministic.**  No randomness, no wall-clock dependency in any
  computation; ``at`` timestamps are injected by the caller.

Ownership split
---------------
===========================  ==========================================
World owns                   placement, pose, orientation, slots,
                             reservations  (this module)
Embodiment owns              BodyProfile, somatic runtime, BodySignal,
                             recovery      (not this module)
===========================  ==========================================
"""

from __future__ import annotations

import fcntl
import json
import os
import tempfile
from collections.abc import Mapping
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Optional

SCHEMA_VERSION = "world.body.substrate.v1.1"
LEGACY_SCHEMA_VERSIONS = ("world.body.substrate.v1",)
KIND = "world_body_substrate"
WORLD_ENTITY_KIND = "body"
FEATURE_ENV = "CHIYO_WORLD_BODY_SUBSTRATE_ENABLED"
DEFAULT_WORLD_ID = "shiomi_city"
DEFAULT_BODY_ID = "chiyo_body"
STATE_RELATIVE_PATH = ("data", "world", "body_substrate.json")

MAX_ID_LENGTH = 128
MAX_PLACEMENTS = 512
MAX_RESERVATIONS = 32

#: M15: canonical surface declarations.  A surface exists only when the world
#: says so AND the object that owns it is present; the list is seeded with the
#: single surface the M13A.1 bootstrap already declares.  No surface is invented
#: here, and `surface_verdict` never assumes one.
#: M15: capabilities declared for objects that are NOT economy items (the
#: purpose-built smoke/fixture objects).  Production objects get their capability
#: from their catalog entry; this registry exists so a fixture never needs the
#: economy to be forged, and it is empty in production until a smoke object is
#: declared here.  It grants nothing by itself.
DECLARED_CAPABILITIES: dict[str, dict[str, Any]] = {}

SURFACES: dict[str, dict[str, str]] = {
    "bed_surface": {"surface_id": "bed_surface", "owner_catalog_item_id": "basic_bed"},
}

HAND_SLOTS = ("left_hand", "right_hand")
POSES = ("standing", "seated", "lying", "transitioning")
ORIENTATIONS = ("north", "south", "east", "west", "up", "down")

PLACEMENT_ROOM = "room"
PLACEMENT_SURFACE = "surface"
PLACEMENT_CONTAINER = "container"
PLACEMENT_HAND = "hand"
PLACEMENT_IN_TRANSIT = "in_transit"
PLACEMENT_KINDS = (
    PLACEMENT_ROOM,
    PLACEMENT_SURFACE,
    PLACEMENT_CONTAINER,
    PLACEMENT_HAND,
    PLACEMENT_IN_TRANSIT,
)

_STATE_KEYS = frozenset(
    {
        "schema_version",
        "kind",
        "revision",
        "world_id",
        "body_id",
        "world_entity_id",
        "pose",
        "orientation",
        "placements",
        "reservations",
    }
)

# Reachability is a property of the pose, not of the object.
_HANDS_USABLE_POSES = frozenset({"standing", "seated"})


class BodySubstrateError(ValueError):
    """The substrate contract was violated by the caller."""


# --------------------------------------------------------------------------
# validation helpers
# --------------------------------------------------------------------------


def _require_text(value: Any, field: str, *, maximum: int = MAX_ID_LENGTH) -> str:
    if not isinstance(value, str):
        raise BodySubstrateError(f"{field} must be a string")
    text = value.strip()
    if not text or len(text) > maximum:
        raise BodySubstrateError(f"{field} must be non-empty and bounded")
    return text


def _require_revision(value: Any, field: str = "revision") -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise BodySubstrateError(f"{field} must be a non-negative integer")
    return value


def _require_choice(value: Any, field: str, allowed: tuple[str, ...]) -> str:
    text = _require_text(value, field, maximum=32)
    if text not in allowed:
        raise BodySubstrateError(f"{field} must be one of {sorted(allowed)}")
    return text


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def canonical_copy(value: Any) -> Any:
    """Return a detached, canonical, bounded JSON copy."""

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
        raise BodySubstrateError("value is not bounded JSON") from exc


def validate_placement_target(target: Any) -> dict[str, Any]:
    """Validate and normalize a single exclusive placement target."""

    if not isinstance(target, Mapping):
        raise BodySubstrateError("placement target must be an object")
    kind = _require_choice(target.get("kind"), "placement.kind", PLACEMENT_KINDS)

    if kind == PLACEMENT_HAND:
        if set(target) != {"kind", "slot_id"}:
            raise BodySubstrateError("hand placement must have exactly kind/slot_id")
        return {
            "kind": kind,
            "slot_id": _require_choice(target["slot_id"], "placement.slot_id", HAND_SLOTS),
        }

    if kind == PLACEMENT_IN_TRANSIT:
        if set(target) != {"kind", "from_location_id", "to_location_id"}:
            raise BodySubstrateError(
                "in_transit placement must have exactly kind/from/to"
            )
        return {
            "kind": kind,
            "from_location_id": _require_text(
                target["from_location_id"], "placement.from_location_id"
            ),
            "to_location_id": _require_text(
                target["to_location_id"], "placement.to_location_id"
            ),
        }

    expected = {"kind", "location_id", "area_id"} if kind == PLACEMENT_ROOM else {
        "kind",
        "location_id",
        f"{kind}_id",
    }
    if set(target) != expected:
        raise BodySubstrateError(f"{kind} placement must have exactly {sorted(expected)}")
    location_id = _require_text(target["location_id"], "placement.location_id")
    if kind == PLACEMENT_ROOM:
        return {
            "kind": kind,
            "location_id": location_id,
            "area_id": _require_text(target["area_id"], "placement.area_id"),
        }
    detail_field = f"{kind}_id"
    return {
        "kind": kind,
        "location_id": location_id,
        detail_field: _require_text(target[detail_field], f"placement.{detail_field}"),
    }


def validate_substrate(state: Any) -> dict[str, Any]:
    """Validate and return a detached, normalized substrate state."""

    if not isinstance(state, Mapping) or set(state) != _STATE_KEYS:
        raise BodySubstrateError("substrate state must have exactly the V1.1 fields")

    if state["schema_version"] != SCHEMA_VERSION:
        raise BodySubstrateError("substrate schema_version is not supported")
    if state["kind"] != KIND:
        raise BodySubstrateError("substrate kind is not supported")

    placements_raw = state["placements"]
    if not isinstance(placements_raw, Mapping) or len(placements_raw) > MAX_PLACEMENTS:
        raise BodySubstrateError("placements must be a bounded object")

    placements: dict[str, Any] = {}
    slot_owner: dict[str, str] = {}
    for object_id, target in placements_raw.items():
        name = _require_text(object_id, "placements key")
        normalized = validate_placement_target(target)
        if normalized["kind"] == PLACEMENT_HAND:
            slot_id = normalized["slot_id"]
            if slot_id in slot_owner:
                raise BodySubstrateError(
                    f"exclusive slot {slot_id} holds two objects: "
                    f"{slot_owner[slot_id]} and {name}"
                )
            slot_owner[slot_id] = name
        placements[name] = normalized

    reservations_raw = state["reservations"]
    if not isinstance(reservations_raw, Mapping) or len(reservations_raw) > MAX_RESERVATIONS:
        raise BodySubstrateError("reservations must be a bounded object")

    reservations: dict[str, Any] = {}
    for slot_id, record in reservations_raw.items():
        slot = _require_choice(slot_id, "reservations key", HAND_SLOTS)
        if not isinstance(record, Mapping) or set(record) != {"execution_id", "reserved_at"}:
            raise BodySubstrateError(
                "reservation must have exactly execution_id/reserved_at"
            )
        reservations[slot] = {
            "execution_id": _require_text(record["execution_id"], "reservation.execution_id"),
            "reserved_at": _require_text(record["reserved_at"], "reservation.reserved_at"),
        }

    return {
        "schema_version": SCHEMA_VERSION,
        "kind": KIND,
        "revision": _require_revision(state["revision"]),
        "world_id": _require_text(state["world_id"], "world_id"),
        "body_id": _require_text(state["body_id"], "body_id"),
        "world_entity_id": _require_text(state["world_entity_id"], "world_entity_id"),
        "pose": _require_choice(state["pose"], "pose", POSES),
        "orientation": _require_choice(state["orientation"], "orientation", ORIENTATIONS),
        "placements": placements,
        "reservations": reservations,
    }


# --------------------------------------------------------------------------
# persistence (same discipline as the existing World stores)
# --------------------------------------------------------------------------


def substrate_path(hermes_home: Optional[Path | str] = None) -> Path:
    home = (
        Path(hermes_home).expanduser()
        if hermes_home is not None
        else Path(os.environ.get("HERMES_HOME") or (Path.home() / ".hermes"))
    )
    return home.joinpath(*STATE_RELATIVE_PATH)


def feature_enabled(environment: Optional[Mapping[str, str]] = None) -> bool:
    raw = (environment or os.environ).get(FEATURE_ENV)
    return str(raw).strip().lower() in {"1", "true", "yes", "on"} if raw else False


def migrate_substrate(raw: Any) -> tuple[dict[str, Any], bool]:
    """Upgrade an older substrate representation to the current schema.

    Returns ``(state, migrated)``.  This is the *only* sanctioned upgrade path:
    a future ``v1.1 -> v2`` step is added here rather than overwriting JSON, so
    ``body_id``, the world binding, placements and reservations all survive.
    Unknown schemas fail closed.
    """

    if not isinstance(raw, Mapping):
        raise BodySubstrateError("substrate state must be an object")
    version = raw.get("schema_version")
    if version == SCHEMA_VERSION:
        return validate_substrate(raw), False
    if version not in LEGACY_SCHEMA_VERSIONS:
        raise BodySubstrateError(f"substrate schema_version {version!r} is not supported")

    # v1 -> v1.1: the body entity binding is introduced.  The body entity id is
    # derived from body_id; nothing else changes, nothing is recomputed.
    upgraded = {
        "schema_version": SCHEMA_VERSION,
        "kind": raw.get("kind"),
        "revision": raw.get("revision"),
        "world_id": raw.get("world_id"),
        "body_id": raw.get("body_id"),
        "world_entity_id": raw.get("world_entity_id") or raw.get("body_id"),
        "pose": raw.get("pose"),
        "orientation": raw.get("orientation"),
        "placements": raw.get("placements") or {},
        "reservations": raw.get("reservations") or {},
    }
    return validate_substrate(upgraded), True


def verify_binding(
    state: Mapping[str, Any], world_state: Optional[Mapping[str, Any]]
) -> dict[str, Any]:
    """Prove the body entity is bound to the World it claims.  Fails closed."""

    normalized = validate_substrate(state)
    reasons: list[str] = []

    if not isinstance(world_state, Mapping):
        return {
            "bound": False,
            "body_id": normalized["body_id"],
            "world_entity_id": normalized["world_entity_id"],
            "world_id": normalized["world_id"],
            "reasons": ["world_state_unavailable"],
        }

    world_id = world_state.get("world_id")
    if world_id != normalized["world_id"]:
        reasons.append(
            f"world_id mismatch: substrate={normalized['world_id']!r} world={world_id!r}"
        )
    if normalized["world_entity_id"] != normalized["body_id"]:
        reasons.append("world_entity_id does not match body_id")
    if world_state.get("schema_version") is None:
        reasons.append("world_state has no schema_version")

    return {
        "bound": not reasons,
        "body_id": normalized["body_id"],
        "world_entity_id": normalized["world_entity_id"],
        "world_id": normalized["world_id"],
        "world_revision": world_state.get("revision"),
        "body_revision": normalized["revision"],
        "reasons": reasons,
    }


def initial_substrate(
    *, world_id: str = DEFAULT_WORLD_ID, body_id: str = DEFAULT_BODY_ID
) -> dict[str, Any]:
    """Deterministic empty substrate.  No randomness, no time input."""

    resolved_body = _require_text(body_id, "body_id")
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": KIND,
        "revision": 0,
        "world_id": _require_text(world_id, "world_id"),
        "body_id": resolved_body,
        "world_entity_id": resolved_body,
        "pose": "standing",
        "orientation": "south",
        "placements": {},
        "reservations": {},
    }


class SubstrateStore:
    """Atomic, lock-protected store for the embodiment substrate."""

    def __init__(self, path: Path | str):
        self.path = Path(path)

    def load(self) -> Optional[dict[str, Any]]:
        state, _migrated = self.load_with_migration()
        return state

    def load_with_migration(self) -> tuple[Optional[dict[str, Any]], bool]:
        """Load and transparently upgrade an older representation.

        Read-only: the file on disk is never rewritten by a load.
        """

        if not self.path.exists():
            return None, False
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise BodySubstrateError("substrate state is not readable JSON") from exc
        return migrate_substrate(raw)

    def save(
        self, state: Mapping[str, Any], *, expected_revision: Optional[int] = None
    ) -> dict[str, Any]:
        """Atomically persist ``state``.

        ``expected_revision`` is a **real** optimistic-concurrency check: a
        caller that observed revision N must pass ``N`` and the write fails
        closed if the live file has moved on.  Unlike the legacy World store it
        is not silently skipped -- it is the documented write path.
        """
        # The lock file lives next to the state file, so the directory must
        # exist before the lock is taken (otherwise the very first save fails).
        self.path.parent.mkdir(parents=True, exist_ok=True)
        normalized = validate_substrate(state)
        with _locked(self.path):
            if expected_revision is not None:
                expected = _require_revision(expected_revision, "expected_revision")
                current = self.load()
                live = current["revision"] if current is not None else None
                if live != expected:
                    raise BodySubstrateError(
                        f"substrate revision mismatch: expected {expected}, live {live}"
                    )
            payload = json.dumps(normalized, ensure_ascii=False, sort_keys=True)
            descriptor, temporary = tempfile.mkstemp(
                dir=str(self.path.parent), prefix=self.path.name + ".", suffix=".tmp"
            )
            try:
                with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                    handle.write(payload)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, self.path)
            except BaseException:
                if os.path.exists(temporary):
                    os.unlink(temporary)
                raise
        return normalized


@contextmanager
def _locked(path: Path) -> Iterator[None]:
    handle = open(str(path) + ".lock", "a+", encoding="utf-8")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


class StaleSubstrateError(BodySubstrateError):
    """The caller acted on a substrate revision that is no longer current."""


# --------------------------------------------------------------------------
# pure state transitions (no I/O, no time unless injected)
# --------------------------------------------------------------------------


def _bump(state: Mapping[str, Any]) -> dict[str, Any]:
    normalized = validate_substrate(state)
    normalized["revision"] = normalized["revision"] + 1
    return normalized


def declare_capability(object_id: Any, capability: Mapping[str, Any]) -> None:
    """Declare the capability of a non-economy object (fixtures, smoke object)."""

    name = _require_text(object_id, "object_id")
    block = dict(capability or {})
    DECLARED_CAPABILITIES[name] = block


#: M15: the world-owned capability declaration file (non-economy objects).
CAPABILITIES_RELATIVE = ("state", "world_object_capabilities.json")
CAPABILITIES_SCHEMA = "world.object.capabilities.v1"


def capabilities_path(hermes_home: Optional[Path | str] = None) -> Path:
    home = (
        Path(hermes_home).expanduser()
        if hermes_home is not None
        else Path(os.environ.get("HERMES_HOME") or (Path.home() / ".hermes"))
    )
    return home.joinpath(*CAPABILITIES_RELATIVE)


def load_declared_capabilities(hermes_home: Optional[Path | str] = None) -> dict[str, dict[str, Any]]:
    """Read the world's capability declarations.  Fail-closed on any problem."""

    path = capabilities_path(hermes_home)
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, Mapping) or raw.get("schema_version") != CAPABILITIES_SCHEMA:
        return {}
    objects = raw.get("objects")
    if not isinstance(objects, Mapping):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for object_id, capability in objects.items():
        if isinstance(capability, Mapping):
            out[str(object_id)] = dict(capability)
    return out


def declared_capability(object_id: Any, hermes_home: Optional[Path | str] = None) -> dict[str, Any]:
    """The declared capability of a non-economy object ({} when none).

    Order: the world-owned declaration file first, then the in-process registry
    (tests/fixtures).  Both are declarations, never assumptions.
    """

    name = str(object_id)
    persisted = load_declared_capabilities(hermes_home).get(name)
    if isinstance(persisted, Mapping):
        return dict(persisted)
    entry = DECLARED_CAPABILITIES.get(name)
    return dict(entry) if isinstance(entry, Mapping) else {}


def surface_declared(surface_id: Any) -> bool:
    """M15: is this surface id a canonical surface of the world?"""

    return str(surface_id) in SURFACES


def surface_owner_catalog(surface_id: Any) -> Optional[str]:
    """M15: the catalog item that must exist for the surface to be real."""

    entry = SURFACES.get(str(surface_id))
    return entry.get("owner_catalog_item_id") if entry else None


def placement_of(state: Mapping[str, Any], object_id: str) -> Optional[dict[str, Any]]:
    """The single authoritative placement of ``object_id`` (or ``None``)."""

    normalized = validate_substrate(state)
    name = _require_text(object_id, "object_id")
    found = normalized["placements"].get(name)
    return canonical_copy(found) if found is not None else None


def slot_occupant(state: Mapping[str, Any], slot_id: str) -> Optional[str]:
    """DERIVED hand occupancy.  Never stored, so it cannot drift."""

    normalized = validate_substrate(state)
    slot = _require_choice(slot_id, "slot_id", HAND_SLOTS)
    for object_id, target in sorted(normalized["placements"].items()):
        if target["kind"] == PLACEMENT_HAND and target["slot_id"] == slot:
            return object_id
    return None


def hand_view(state: Mapping[str, Any]) -> dict[str, Optional[str]]:
    """Derived view: ``{"left_hand": ..., "right_hand": ...}``."""

    normalized = validate_substrate(state)
    return {slot: slot_occupant(normalized, slot) for slot in HAND_SLOTS}


def free_slots(state: Mapping[str, Any]) -> list[str]:
    normalized = validate_substrate(state)
    return [slot for slot in HAND_SLOTS if slot_occupant(normalized, slot) is None]


def place_object(
    state: Mapping[str, Any],
    object_id: str,
    target: Any,
    *,
    expected_revision: Optional[int] = None,
) -> dict[str, Any]:
    """Set the exclusive placement of ``object_id``.

    Raises :class:`StaleSubstrateError` if ``expected_revision`` is given and
    stale, and :class:`BodySubstrateError` if an exclusive hand slot already
    holds another object (``OCCUPIED``) or the object is unknown.
    """

    normalized = validate_substrate(state)
    if expected_revision is not None:
        expected = _require_revision(expected_revision, "expected_revision")
        if normalized["revision"] != expected:
            raise StaleSubstrateError(
                f"stale substrate: expected {expected}, live {normalized['revision']}"
            )

    name = _require_text(object_id, "object_id")
    normalized_target = validate_placement_target(target)

    if normalized_target["kind"] == PLACEMENT_HAND:
        slot = normalized_target["slot_id"]
        occupant = slot_occupant(normalized, slot)
        if occupant is not None and occupant != name:
            raise BodySubstrateError(f"OCCUPIED: {slot} already holds {occupant}")

    updated = canonical_copy(normalized)
    updated["placements"][name] = normalized_target
    return _bump(updated)


def remove_object(
    state: Mapping[str, Any], object_id: str, *, expected_revision: Optional[int] = None
) -> dict[str, Any]:
    normalized = validate_substrate(state)
    if expected_revision is not None:
        expected = _require_revision(expected_revision, "expected_revision")
        if normalized["revision"] != expected:
            raise StaleSubstrateError(
                f"stale substrate: expected {expected}, live {normalized['revision']}"
            )
    name = _require_text(object_id, "object_id")
    if name not in normalized["placements"]:
        raise BodySubstrateError(f"UNKNOWN: {name} has no placement")
    updated = canonical_copy(normalized)
    del updated["placements"][name]
    return _bump(updated)


def set_pose(
    state: Mapping[str, Any],
    pose: str,
    *,
    orientation: Optional[str] = None,
    expected_revision: Optional[int] = None,
) -> dict[str, Any]:
    normalized = validate_substrate(state)
    if expected_revision is not None:
        expected = _require_revision(expected_revision, "expected_revision")
        if normalized["revision"] != expected:
            raise StaleSubstrateError(
                f"stale substrate: expected {expected}, live {normalized['revision']}"
            )
    updated = canonical_copy(normalized)
    updated["pose"] = _require_choice(pose, "pose", POSES)
    if orientation is not None:
        updated["orientation"] = _require_choice(orientation, "orientation", ORIENTATIONS)
    return _bump(updated)


def reserve_slot(
    state: Mapping[str, Any],
    slot_id: str,
    execution_id: str,
    *,
    at: Optional[str] = None,
    expected_revision: Optional[int] = None,
) -> dict[str, Any]:
    """Exclusive body-resource reservation for one in-flight action."""

    normalized = validate_substrate(state)
    if expected_revision is not None:
        expected = _require_revision(expected_revision, "expected_revision")
        if normalized["revision"] != expected:
            raise StaleSubstrateError(
                f"stale substrate: expected {expected}, live {normalized['revision']}"
            )
    slot = _require_choice(slot_id, "slot_id", HAND_SLOTS)
    holder = _require_text(execution_id, "execution_id")
    existing = normalized["reservations"].get(slot)
    if existing is not None and existing["execution_id"] != holder:
        raise BodySubstrateError(
            f"OCCUPIED: {slot} is reserved by {existing['execution_id']}"
        )
    updated = canonical_copy(normalized)
    updated["reservations"][slot] = {
        "execution_id": holder,
        "reserved_at": _require_text(at, "at") if at is not None else _now_iso(),
    }
    return _bump(updated)


def release_slot(
    state: Mapping[str, Any],
    slot_id: str,
    execution_id: str,
    *,
    expected_revision: Optional[int] = None,
) -> dict[str, Any]:
    normalized = validate_substrate(state)
    if expected_revision is not None:
        expected = _require_revision(expected_revision, "expected_revision")
        if normalized["revision"] != expected:
            raise StaleSubstrateError(
                f"stale substrate: expected {expected}, live {normalized['revision']}"
            )
    slot = _require_choice(slot_id, "slot_id", HAND_SLOTS)
    holder = _require_text(execution_id, "execution_id")
    existing = normalized["reservations"].get(slot)
    if existing is None or existing["execution_id"] != holder:
        raise BodySubstrateError(f"UNKNOWN: {slot} is not reserved by {holder}")
    updated = canonical_copy(normalized)
    del updated["reservations"][slot]
    return _bump(updated)


# --------------------------------------------------------------------------
# derived capability view -- never persisted, never authoritative
# --------------------------------------------------------------------------


def reachability_inputs(state: Mapping[str, Any]) -> dict[str, Any]:
    """Deterministic body-side inputs for the World resolver.

    This is a *derived view*.  It has no authority: the resolver still decides.
    """

    normalized = validate_substrate(state)
    pose = normalized["pose"]
    usable = pose in _HANDS_USABLE_POSES
    hands = hand_view(normalized)
    return {
        "body_id": normalized["body_id"],
        "pose": pose,
        "orientation": normalized["orientation"],
        "body_revision": normalized["revision"],
        "hands_usable": usable,
        "left_hand": hands["left_hand"],
        "right_hand": hands["right_hand"],
        "free_slots": free_slots(normalized) if usable else [],
        "reserved_slots": sorted(normalized["reservations"]),
        "can_manipulate": bool(usable and free_slots(normalized)),
    }


def read_model(state: Mapping[str, Any]) -> dict[str, Any]:
    """Composite read model.  Combines World-owned facts only."""

    normalized = validate_substrate(state)
    return {
        "kind": KIND,
        "schema_version": SCHEMA_VERSION,
        "world_id": normalized["world_id"],
        "body_id": normalized["body_id"],
        "world_entity_id": normalized["world_entity_id"],
        "revision": normalized["revision"],
        "pose": normalized["pose"],
        "orientation": normalized["orientation"],
        "hands": hand_view(normalized),
        "placements": canonical_copy(normalized["placements"]),
        "reservations": canonical_copy(normalized["reservations"]),
        "derived": reachability_inputs(normalized),
    }
