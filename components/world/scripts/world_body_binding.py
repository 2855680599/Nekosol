#!/usr/bin/env python3
"""World <-> Body binding and cross-authority consistency projection (M6 completion).

This module makes the body a *first-class World entity* without creating a
second truth for anything:

World owns
    ``world_state.json``            location, area, revision, world_id
    ``world_property_economy.json`` owned item instances + their coarse location
Body (embodiment substrate) owns
    ``body_substrate.json``         pose, orientation, placements, reservations

The binding is therefore *compositional*: the body entity is described by
joining the two owners, and this module never writes either one.  There is no
copy of the body's location here, and no copy of the hand contents.

Why this exists
---------------
Before M6 completion the substrate knew its own ``world_id`` but nothing proved
the two stores actually described the same world, and nothing checked that a
physically-placed object and the economy's coarse location agreed.  Both gaps
are closed here:

* :func:`verify_binding`      -- fail-closed proof the body is bound to the world
* :func:`body_entity`         -- the World-side projection of the body entity
* :func:`consistency_report`  -- READ-ONLY divergence audit across the owners

Nothing here mutates state, and nothing here is a scheduler or an LLM.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Optional

SCHEMA_VERSION = "world.body.binding.v1"
BODY_ENTITY_KIND = "body"

WORLD_STATE_RELATIVE = ("data", "world", "world_state.json")
ECONOMY_RELATIVE = ("state", "world_property_economy.json")

#: The economy stores coarse locations as "<place>/<area>" (and in-transit as
#: "in_transit/<place>/<area>").  Placement targets store "<place>" plus an
#: optional area, so the join is on the place component only.
ECONOMY_IN_TRANSIT_PREFIX = "in_transit/"


class BodyBindingError(ValueError):
    """The binding contract was violated."""


def _read_json(path: Path) -> Optional[dict[str, Any]]:
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def world_state_path(hermes_home: Optional[Path | str] = None) -> Path:
    return _home(hermes_home).joinpath(*WORLD_STATE_RELATIVE)


def economy_path(hermes_home: Optional[Path | str] = None) -> Path:
    return _home(hermes_home).joinpath(*ECONOMY_RELATIVE)


def _home(hermes_home: Optional[Path | str]) -> Path:
    return Path(
        hermes_home
        if hermes_home is not None
        else os.environ.get("HERMES_HOME") or (Path.home() / ".hermes")
    )


def load_world_state(hermes_home: Optional[Path | str] = None) -> Optional[dict[str, Any]]:
    return _read_json(world_state_path(hermes_home))


def load_economy(hermes_home: Optional[Path | str] = None) -> Optional[dict[str, Any]]:
    return _read_json(economy_path(hermes_home))


# --------------------------------------------------------------------------
# binding
# --------------------------------------------------------------------------


def verify_binding(
    substrate: Optional[Mapping[str, Any]],
    world_state: Optional[Mapping[str, Any]],
) -> dict[str, Any]:
    """Is this body actually bound to this World?  Fails closed."""

    import world_body_substrate as wbs

    if substrate is None:
        return {"bound": False, "reasons": ["body_substrate_missing"]}
    if world_state is None:
        return {"bound": False, "reasons": ["world_state_missing"]}
    return wbs.verify_binding(substrate, world_state)


def body_entity(
    hermes_home: Optional[Path | str] = None,
) -> dict[str, Any]:
    """World-side projection of the body entity.

    ``position`` and ``area`` come from World.  ``pose`` / ``orientation`` /
    ``hands`` come from Embodiment.  Each field is labelled with its owner so a
    consumer can never mistake the composite for a single writer's output.
    """

    import world_body_substrate as wbs

    world_state = load_world_state(hermes_home)
    substrate = wbs.SubstrateStore(wbs.substrate_path(hermes_home)).load()
    binding = verify_binding(substrate, world_state)

    entity: dict[str, Any] = {
        "kind": BODY_ENTITY_KIND,
        "schema_version": SCHEMA_VERSION,
        "bound": binding["bound"],
        "binding_reasons": binding.get("reasons", []),
        "owners": {
            "position": "world",
            "pose": "embodiment",
            "hands": "embodiment",
            "placements": "world",
        },
    }

    if world_state is not None:
        location = world_state.get("location") or {}
        entity["world"] = {
            "world_id": world_state.get("world_id"),
            "world_revision": world_state.get("revision"),
            "place_id": location.get("place_id"),
            "area_id": location.get("area_id"),
        }
    else:
        entity["world"] = None

    if substrate is not None:
        entity["embodiment"] = {
            "body_id": substrate["body_id"],
            "world_entity_id": substrate["world_entity_id"],
            "body_revision": substrate["revision"],
            "pose": substrate["pose"],
            "orientation": substrate["orientation"],
            "hands": wbs.hand_view(substrate),
            "held_objects": sorted(
                object_id
                for object_id, target in substrate["placements"].items()
                if target["kind"] == wbs.PLACEMENT_HAND
            ),
        }
    else:
        entity["embodiment"] = None

    return entity


# --------------------------------------------------------------------------
# cross-authority consistency (READ-ONLY)
# --------------------------------------------------------------------------


def _economy_items(economy: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Item instances from whatever shape the economy store uses."""

    items: list[dict[str, Any]] = []
    for key in ("items", "item_instances", "owned_items"):
        value = economy.get(key)
        if isinstance(value, list):
            items.extend(item for item in value if isinstance(item, Mapping))
        elif isinstance(value, Mapping):
            for name, item in value.items():
                if isinstance(item, Mapping):
                    merged = dict(item)
                    merged.setdefault("item_instance_id", name)
                    items.append(merged)
    return items


def _placement_place(
    placement: Mapping[str, Any], body_place: Optional[str]
) -> Optional[str]:
    """The place a placement physically sits in.

    A hand placement carries no location of its own: what is held is wherever
    the body currently is.  That is read from World, never duplicated here.
    """

    kind = placement.get("kind")
    if kind == "in_transit":
        return None
    if kind == "hand":
        return body_place
    return placement.get("location_id")


def consistency_report(hermes_home: Optional[Path | str] = None) -> dict[str, Any]:
    """Audit physical placement against the economy's coarse location.

    This is a *report*.  It never fixes anything, never writes, and never
    promotes one owner's value over the other's.  Divergences are returned for a
    human/operator or a later integration ticket to act on.
    """

    import world_body_substrate as wbs

    world_state = load_world_state(hermes_home)
    economy = load_economy(hermes_home)
    substrate = wbs.SubstrateStore(wbs.substrate_path(hermes_home)).load()

    body_place = None
    if world_state is not None:
        body_place = (world_state.get("location") or {}).get("place_id")

    divergences: list[dict[str, Any]] = []
    checked = 0
    untracked: list[str] = []

    if substrate is not None and economy is not None:
        items = _economy_items(economy)
        for item in items:
            object_id = item.get("item_instance_id")
            economy_location = item.get("current_location")
            if not isinstance(object_id, str) or not isinstance(economy_location, str):
                continue
            placement = substrate["placements"].get(object_id)
            if placement is None:
                untracked.append(object_id)
                continue
            checked += 1
            in_transit_placement = placement["kind"] == wbs.PLACEMENT_IN_TRANSIT
            economy_in_transit = economy_location.startswith(ECONOMY_IN_TRANSIT_PREFIX)

            if in_transit_placement != economy_in_transit:
                divergences.append(
                    {
                        "object_id": object_id,
                        "reason": "transit_state_disagrees",
                        "placement_kind": placement["kind"],
                        "economy_location": economy_location,
                    }
                )
                continue
            if in_transit_placement:
                continue

            place = _placement_place(placement, body_place)
            economy_place = economy_location.split("/")[0]
            if place != economy_place:
                divergences.append(
                    {
                        "object_id": object_id,
                        "reason": "place_disagrees",
                        "placement_location_id": place,
                        "economy_location": economy_location,
                    }
                )

    return {
        "kind": "world_body_consistency_report",
        "schema_version": SCHEMA_VERSION,
        "world_state_present": world_state is not None,
        "economy_present": economy is not None,
        "substrate_present": substrate is not None,
        "economy_items_seen": len(_economy_items(economy)) if economy else 0,
        "items_cross_checked": checked,
        "items_without_physical_tracking": sorted(untracked),
        "divergences": divergences,
        "consistent": not divergences,
    }


def object_capability(hermes_home: Optional[Path | str] = None,
                      object_id: str = "") -> dict[str, Any]:
    """M15: an object's canonical capability block, joined for the Resolver.

    Order (first hit wins, and nothing is invented):
      1. the economy item's catalog entry  -- production objects;
      2. the substrate's declared-capability registry -- purpose-built smoke
         and fixture objects;
      3. nothing -- an unknown object, which the capability vocabulary reads as
         UNKNOWN and which therefore cannot be taken.
    """

    name = str(object_id)
    economy = load_economy(hermes_home) or {}
    for item in economy.get("items") or []:
        if not isinstance(item, Mapping):
            continue
        if str(item.get("item_instance_id")) != name:
            continue
        if str(item.get("disposition")) != "OWNED":
            return {}
        try:
            import world_property_economy as economy_module
        except ImportError:  # pragma: no cover
            return {}
        return economy_module.object_capability(item.get("catalog_item_id"))

    try:
        import world_body_substrate as substrate
    except ImportError:  # pragma: no cover
        return {}
    return substrate.declared_capability(name, hermes_home)


def cross_authority_refs(
    substrate: Mapping[str, Any],
    economy: Optional[Mapping[str, Any]],
    object_id: str,
) -> list[str]:
    """Follow-up refs a caller must reconcile after moving ``object_id``.

    The resolver records these in the ledger.  It does **not** write the economy
    store: a physical move is not authority to rewrite ownership bookkeeping.
    """

    if economy is None:
        return []
    for item in _economy_items(economy):
        if item.get("item_instance_id") == object_id:
            return [f"economy.item_instance[{object_id}].current_location"]
    return []
