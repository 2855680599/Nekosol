#!/usr/bin/env python3
"""M13A.1 canonical World instance bootstrap.

Recon result (see the report): there is no historical World instance with a
place graph anywhere on either host, and ``body_substrate.json`` has never
existed in production.  What *does* exist:

* the World state itself -- the real, migrated production instance
  (`world_state.json`, revision 5, `home`/`bedroom`);
* the canonical topology -- in code (`world_living_skeleton._LOCATIONS`,
  six places with a reachability graph), not in data;
* one real owned object -- `basic_bed` in the property economy, located at
  `home/bedroom` since 2026-08-30.

So this module does **not** invent a world.  It initialises the one missing
piece -- the body substrate -- through the existing canonical writer
(`initial_substrate` + `place_object` + `SubstrateStore.save`), and it binds the
substrate placement of the real economy object to that object's identity instead
of minting a second one.

Idempotent: a second run returns ``ALREADY_BOOTSTRAPPED`` and writes nothing.
"""

from __future__ import annotations

from __future__ import annotations

import json
import pathlib
import sys as _sys
from typing import Any, Mapping, Optional

# Runnable as a CLI: put the verified runtime core on the path the same way the
# service does, so `python world_instance_bootstrap.py` works outside pytest.
_HERE = pathlib.Path(__file__).resolve().parent
_SCRIPTS = _HERE.parent / "scripts"
if str(_SCRIPTS) not in _sys.path:
    _sys.path.insert(0, str(_SCRIPTS))

SCHEMA_BOOTSTRAP = "world.instance.bootstrap.v1"
KIND_BOOTSTRAP = "world_instance_bootstrap"

STATUS_CREATED = "BOOTSTRAPPED"
STATUS_ALREADY = "ALREADY_BOOTSTRAPPED"
STATUS_REFUSED = "REFUSED"

#: The real object this instance already owns.  Taken from the property economy
#: (`state/world_property_economy.json`), not invented here.
ECONOMY_STATE_RELATIVE = ("state", "world_property_economy.json")

#: The single purpose-built smoke object (ticket section 13).  Its provenance is
#: stated in its own id and in every receipt; it is not presented as an
#: heirloom, and it can be deleted without touching anything else.
TEST_OBJECT_ID = "M13A1_TEST_OBJECT"
TEST_OBJECT_PROVENANCE = "M13A1 acceptance fixture; not Chiyo's history"

#: The surface the test object rests on.  Named after the real fixture.
BED_SURFACE_ID = "bed_surface"

#: Safe neutral genesis placement: the canonical initial location of the
#: existing World (ticket sections 9/10).  Not "where she was yesterday".
GENESIS_POSE = "standing"
GENESIS_ORIENTATION = "south"


class BootstrapError(RuntimeError):
    """The bootstrap could not proceed safely."""


class BootstrapRefused(BootstrapError):
    """Ticket section 44: stop rather than invent a world."""


def _module():
    import world_body_substrate as wbs

    return wbs


def economy_path(world_home: pathlib.Path) -> pathlib.Path:
    return world_home.joinpath(*ECONOMY_STATE_RELATIVE)


def owned_economy_items(world_home: pathlib.Path) -> list[dict[str, Any]]:
    """Objects the property economy says Chiyo actually owns."""

    path = economy_path(world_home)
    if not path.exists():
        return []
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise BootstrapError("property economy is unreadable") from error
    return [
        item for item in (state.get("items") or [])
        if isinstance(item, Mapping) and item.get("disposition") == "OWNED"
    ]


def _room_target(location_id: str, area_id: str) -> dict[str, Any]:
    return {"kind": "room", "location_id": location_id, "area_id": area_id}


def _surface_target(location_id: str, surface_id: str) -> dict[str, Any]:
    return {"kind": "surface", "location_id": location_id, "surface_id": surface_id}


def plan(world_home: pathlib.Path) -> dict[str, Any]:
    """Everything the bootstrap would create, without touching disk."""

    wbs = _module()
    world_state_path = world_home / "data" / "world" / "world_state.json"
    if not world_state_path.exists():
        raise BootstrapRefused("no World state to bind to: %s" % world_state_path)
    world = json.loads(world_state_path.read_text(encoding="utf-8"))
    world_id = world.get("world_id")
    if not world_id:
        raise BootstrapRefused("World state has no world_id")
    if str(world.get("schema_version")) != "world.foundation.v0":
        raise BootstrapRefused("unexpected World schema %r" % world.get("schema_version"))

    location = world.get("location") or {}
    place_id = location.get("place_id")
    area_id = location.get("area_id")
    if not place_id or not area_id:
        raise BootstrapRefused("World state has no location")

    substrate = wbs.initial_substrate(world_id=world_id, body_id="chiyo_body")
    substrate["pose"] = GENESIS_POSE
    substrate["orientation"] = GENESIS_ORIENTATION
    substrate = wbs.validate_substrate(substrate)

    placements: list[dict[str, Any]] = []

    # 1. the real owned object, aligned with its economy identity
    for item in owned_economy_items(world_home):
        instance_id = item.get("item_instance_id")
        if not instance_id:
            continue
        substrate = wbs.place_object(
            substrate, instance_id, _room_target(place_id, area_id)
        )
        placements.append({
            "object_id": instance_id,
            "catalog_item_id": item.get("catalog_item_id"),
            "kind": "room",
            "location_id": place_id,
            "area_id": area_id,
            "identity_source": "property_economy",
            "economy_current_location": item.get("current_location"),
        })

    # 2. the one purpose-built smoke object, resting on the real fixture
    substrate = wbs.place_object(
        substrate, TEST_OBJECT_ID, _surface_target(place_id, BED_SURFACE_ID)
    )
    placements.append({
        "object_id": TEST_OBJECT_ID,
        "catalog_item_id": None,
        "kind": "surface",
        "location_id": place_id,
        "surface_id": BED_SURFACE_ID,
        "identity_source": "m13a1_fixture",
        "provenance": TEST_OBJECT_PROVENANCE,
    })

    return {
        "world_id": world_id,
        "world_revision": world.get("revision"),
        "location": {"place_id": place_id, "area_id": area_id},
        "substrate": substrate,
        "placements": placements,
    }


def bootstrap(world_home: pathlib.Path, *, apply: bool = False) -> dict[str, Any]:
    """Create the missing substrate.  Idempotent and schema-guarded."""

    wbs = _module()
    home = pathlib.Path(world_home)
    target = wbs.substrate_path(home)
    store = wbs.SubstrateStore(target)

    receipt: dict[str, Any] = {
        "kind": KIND_BOOTSTRAP,
        "schema_version": SCHEMA_BOOTSTRAP,
        "world_home": str(home),
        "substrate_path": str(target),
        "apply": bool(apply),
    }

    if target.exists():
        existing = store.load_with_migration()[0]
        receipt.update({
            "status": STATUS_ALREADY,
            "reason": "body_substrate.json already exists",
            "existing_revision": (existing or {}).get("revision"),
            "existing_placements": sorted((existing or {}).get("placements") or {}),
            "wrote_anything": False,
        })
        return receipt

    planned = plan(home)
    receipt.update({
        "world_id": planned["world_id"],
        "world_revision": planned["world_revision"],
        "location": planned["location"],
        "placements": planned["placements"],
        "test_object": {
            "object_id": TEST_OBJECT_ID,
            "provenance": TEST_OBJECT_PROVENANCE,
            "removable": True,
        },
    })

    if not apply:
        receipt.update({
            "status": STATUS_CREATED,
            "planned_only": True,
            "wrote_anything": False,
            "planned_substrate": planned["substrate"],
        })
        return receipt

    # Canonical writer: atomic (tmp + rename), schema-validated, versioned.
    # No expected_revision on a first creation -- there is no live revision to be
    # stale against.  The claim directory is what keeps two concurrent
    # bootstraps from racing (same discipline as world_genesis.create_genesis).
    claim = target.with_name("." + target.name + ".bootstrap-claim")
    try:
        claim.mkdir(mode=0o700)
    except FileExistsError as error:
        raise BootstrapError(
            "another bootstrap is in progress (claim %s exists)" % claim
        ) from error
    try:
        if target.exists():
            receipt.update({
                "status": STATUS_ALREADY,
                "reason": "body_substrate.json appeared during bootstrap",
                "wrote_anything": False,
            })
            return receipt
        saved = store.save(planned["substrate"])
    finally:
        try:
            claim.rmdir()
        except OSError:
            pass

    binding = wbs.verify_binding(saved, json.loads(
        (home / "data" / "world" / "world_state.json").read_text(encoding="utf-8")
    ))
    if not binding["bound"]:
        raise BootstrapError(
            "bootstrap produced an unbound substrate: %s" % binding.get("reasons")
        )

    receipt.update({
        "status": STATUS_CREATED,
        "planned_only": False,
        "wrote_anything": True,
        "written_revision": saved["revision"],
        "binding": {
            "bound": binding["bound"],
            "body_id": binding["body_id"],
            "world_id": binding["world_id"],
            "reasons": binding["reasons"],
        },
    })
    return receipt


def main(argv: Optional[list[str]] = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description="M13A.1 canonical World instance bootstrap (idempotent)"
    )
    parser.add_argument("--world-home", required=True)
    parser.add_argument("--apply", action="store_true",
                        help="actually write (default is a dry-run plan)")
    args = parser.parse_args(argv)

    receipt = bootstrap(pathlib.Path(args.world_home), apply=args.apply)
    print(json.dumps(receipt, ensure_ascii=False, sort_keys=True, indent=2))
    return 0 if receipt["status"] in (STATUS_CREATED, STATUS_ALREADY) else 1


if __name__ == "__main__":
    import sys

    sys.exit(main())
