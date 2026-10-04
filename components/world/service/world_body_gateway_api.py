#!/usr/bin/env python3
"""Gateway-facing World/Body surface (M13B).

Ticket sections 8-14, 18-22, 54-55.  This is deliberately a **separate, narrow**
interface from the M13A admin control surface:

============================  ==========================================
admin socket (``control.sock``)  gateway socket (``gateway.sock``)
============================  ==========================================
``act``  (raw ActionProposal)    ``submit_action`` (proposal minus
                                  authority/confirm -- service stamps
                                  authorisation itself)
``apply_result`` (**admin**;     absent -- the gateway must never be able
 bypasses action authority)      to fabricate an execution result
``reconcile``                    absent
``bootstrap`` / migration        absent
============================  ==========================================

Everything on this surface is bounded and read-only except ``submit_action``.
Observations are **qualitative and finite**: no raw somatic floats, no full
World JSON, no test fixture.

Object portability: the current World substrate contract has no canonical
"takeable" capability, so ``PICK_UP`` exposure for ordinary objects is **off**
and fail-closed here rather than fixed by inventing a rule (ticket 19-21).
"""

from __future__ import annotations

import json
import os
import pathlib
import socket
import stat
from typing import Any, Mapping, Optional, Sequence

SCHEMA_GATEWAY_API = "world.body.gateway.api.v1"
KIND_GATEWAY_API = "world_body_gateway"

GATEWAY_SOCKET_NAME = "gateway.sock"

#: Read-only surface.
GATEWAY_READ_COMMANDS = (
    "get_snapshot",
    "get_observation",
    "get_body_signals",
    "get_available_actions",
    "get_action_result",
    "get_status",
    "ping",
)
#: The one mutating command (ticket section 10).
GATEWAY_MUTATING_COMMANDS = ("submit_action",)
GATEWAY_COMMANDS = GATEWAY_READ_COMMANDS + GATEWAY_MUTATING_COMMANDS

#: Explicitly refused, so a probing caller gets a clear answer rather than a
#: generic "unknown command" (ticket section 9).
GATEWAY_FORBIDDEN_COMMANDS = (
    "apply_result",
    "set_body_state",
    "set_fatigue",
    "force_placement",
    "edit_world",
    "bootstrap",
    "migration",
    "reconcile",
    "act",
)

#: Authorisation the *service* stamps onto a gateway submission.  A caller may
#: not supply this (ticket section 55) -- it is derived from the request
#: envelope, which the socket permissions are what actually protect.
GATEWAY_AUTHORITY = "verified_main_self_tool"
GATEWAY_ACTOR = "chiyo"
GATEWAY_CALLER = "hermes_gateway"

# --- M14A: action socket authority firewall -------------------------------
try:
    import world_action_authorization as waa
except ImportError:  # pragma: no cover - direct-load contexts
    import pathlib as _pl
    import sys as _sys
    _HERE = str(_pl.Path(__file__).resolve().parent)
    if _HERE not in _sys.path:
        _sys.path.insert(0, _HERE)
    import world_action_authorization as waa

#: Unix credential of the only process allowed to call the gateway surface.
#: Captured via SO_PEERCRED by the service loop (transport hardening layer).
GATEWAY_ALLOWED_PEER_UID = 0  # root; the whole deployment runs as root

#: M15: poses that can manipulate (mirrors world_body_substrate._HANDS_USABLE_POSES;
#: the substrate remains the owner of the rule).
_HANDS_USABLE_POSES = ("standing", "seated")

#: World objects that are the M13A.1 acceptance fixture, not Chiyo's property.
TEST_OBJECT_PREFIXES = ("M13A1_TEST_OBJECT", "M13A1_")
TEST_OBJECT_MARKER = "M13A1 acceptance fixture"

#: Poses/orientations, mirrored for the action-proposal vocabulary.
POSES = ("standing", "seated", "lying", "transitioning")
ORIENTATIONS = ("north", "south", "east", "west", "up", "down")
HAND_SLOTS = ("left_hand", "right_hand")

MAX_TEXT = 240


class GatewayAPIError(ValueError):
    """The gateway asked for something the narrow interface does not offer."""


def _text(value: Any, label: str, *, maximum: int = MAX_TEXT) -> str:
    if not isinstance(value, str) or not value.strip():
        raise GatewayAPIError("%s must be a non-empty string" % label)
    text = value.strip()
    if len(text) > maximum:
        raise GatewayAPIError("%s is too long" % label)
    return text


def is_test_object(object_id: Any) -> bool:
    """True for the acceptance fixture, which production observation hides."""

    if not isinstance(object_id, str):
        return False
    return object_id.startswith(TEST_OBJECT_PREFIXES) or TEST_OBJECT_MARKER in object_id


def socket_permissions(path: pathlib.Path) -> dict[str, Any]:
    try:
        info = path.stat()
    except OSError as error:
        return {"exists": False, "error": str(error)}
    return {
        "exists": True,
        "mode": oct(stat.S_IMODE(info.st_mode)),
        "uid": info.st_uid,
        "gid": info.st_gid,
    }


# --------------------------------------------------------------------------
# the surface
# --------------------------------------------------------------------------


class GatewaySurface:
    """Read + submit_action over a bounded view of the running service."""

    def __init__(self, service: Any, *, include_test_objects: bool = False):
        self.service = service
        self.include_test_objects = bool(include_test_objects)

        self.include_test_objects = bool(include_test_objects)
        # M14A: registry of one-shot action authorizations (issued by the
        # Intent Executor side, consumed here; the surface never issues).
        # Defensive: a bare/stub service (admin-surface tests, direct use)
        # may have no config; the firewall then rejects every submission
        # instead of making the surface unconstructible.
        _run_dir = None
        try:
            _run_dir = getattr(service.config, "run_dir")
        except Exception:  # pragma: no cover - stub services
            _run_dir = None
        self.authorizations = waa.AuthorizationRegistry(_run_dir) if _run_dir else None

    # ---- helpers ---------------------------------------------------------
    def _substrate(self) -> Optional[dict[str, Any]]:
        service = self.service
        try:
            store = service.wbs.SubstrateStore(service.wbs.substrate_path(service.config.world_home))
            return store.load_with_migration()[0]
        except Exception:
            return None
    def _world_live(self) -> dict[str, Any]:
        """The World state as it is on disk *now*.

        M13A.1 created the substrate, but before that nothing could move the
        World, so the service cached `world_state` at startup.  Now that MOVE
        works through the Resolver the cache goes stale on the first successful
        move -- and a gateway that reports a stale location is a gateway that
        lies to the model.  Always read the live file; fall back to the cache
        only if the file is momentarily unreadable.
        """

        service = self.service
        try:
            import json as _json

            return _json.loads(service.config.world_state_path.read_text(encoding="utf-8"))
        except Exception:
            return dict(service.world_state or {})

    def _visible_object_ids(self) -> list[str]:
        substrate = self._substrate() or {}
        ids = sorted((substrate.get("placements") or {}).keys())
        if self.include_test_objects:
            return ids
        return [i for i in ids if not is_test_object(i)]

    def _takeability(self, object_id: str) -> dict[str, Any]:
        """M15: object capability + live placement verdict (per object).

        Capability comes from the object's canonical identity (economy catalog
        entry, or a declared smoke/fixture object) via the binding join.  An
        object with no declared capability reads UNKNOWN and is NOT takeable --
        never assumed.
        """

        substrate = self._substrate() or {}
        placement = (substrate.get("placements") or {}).get(object_id)
        capability = self._object_capability(object_id)
        takeable = str(capability.get("takeable") or "UNKNOWN").lower() == "true"

        verdict = {
            "object_id": object_id,
            "known": placement is not None,
            "capability": capability,
            "takeable": takeable,
            "placement_kind": (placement or {}).get("kind"),
            "reason": None,
        }
        if placement is None:
            verdict["reason"] = "OBJECT_NOT_FOUND"
        elif placement.get("kind") == "hand":
            verdict["takeable"] = False
            verdict["reason"] = "ALREADY_HELD"
        elif not takeable:
            verdict["reason"] = "NOT_PORTABLE"
        return verdict

    # ---- M15: real availability truth -------------------------------------
    def _object_capability(self, object_id: str) -> dict[str, Any]:
        """The object's canonical capability block (binding join, never invented)."""

        try:
            import world_body_binding as binding
            return binding.object_capability(self.service.config.world_home, object_id)
        except Exception:
            return {}

    def _pick_up_availability(self, *, held: list[str]) -> dict[str, Any]:
        """M15 §17: the physical truth about PICK_UP right now.

        The intent mutation gate is a *gateway* concept; this service owns
        placements and capabilities, so it answers with what it can see:
        capability / hands / reachability.  The bridge labels the model-facing
        space RUNTIME_DISABLED when its own gate is off.
        """

        substrate = self._substrate() or {}
        placements = substrate.get("placements") or {}
        hands = {slot: None for slot in HAND_SLOTS}
        for object_id, target in placements.items():
            if str(target.get("kind")) == "hand" and target.get("slot_id") in hands:
                hands[str(target["slot_id"])] = object_id
        free = [slot for slot, occupant in hands.items() if occupant is None]
        if not free:
            return {"available": False, "blocked_reason": "HAND_OCCUPIED",
                    "detail": "every hand slot is occupied", "candidates": []}

        # M15: the pose rule -- the same one the Resolver enforces.  A body that
        # cannot manipulate must never be told PICK_UP is available.
        pose = (substrate.get("pose") or "").strip()
        if pose not in _HANDS_USABLE_POSES:
            return {"available": False, "blocked_reason": "NOT_REACHABLE",
                    "detail": "pose %s cannot manipulate" % pose, "candidates": []}

        here = (self._world_live() or {}).get("location", {}).get("place_id") \
            if isinstance(self._world_live(), Mapping) else None
        takeable_ids, elsewhere_ids = [], []
        for object_id, target in sorted(placements.items()):
            if str(target.get("kind")) == "hand":
                continue
            if is_test_object(object_id) and not self.include_test_objects:
                continue
            if not self._takeability(object_id).get("takeable"):
                continue
            if target.get("location_id") == here:
                takeable_ids.append(object_id)
            else:
                elsewhere_ids.append(object_id)

        if not takeable_ids:
            reason = "NOT_REACHABLE" if elsewhere_ids else "NO_TAKEABLE_OBJECT"
            return {"available": False, "blocked_reason": reason,
                    "detail": ("takeable objects exist but not in %s" % here)
                    if elsewhere_ids else "no object in this world declares takeable=true",
                    "candidates": []}

        return {"available": True, "blocked_reason": None, "detail": None,
                "candidates": [{"object_id": o, "slots": free} for o in takeable_ids]}

    # ---- read ------------------------------------------------------------
    def get_status(self) -> dict[str, Any]:
        service = self.service
        readiness = service.readiness()
        return {
            "kind": KIND_GATEWAY_API,
            "schema_version": SCHEMA_GATEWAY_API,
            "ready": readiness["status"] == "READY",
            "readiness": readiness["status"],
            "readiness_checks": readiness["checks"],
            "world_id": self._world_live().get("world_id"),
            "world_revision": self._world_live().get("revision"),
            "body_id": (service.body_state or {}).get("body_id"),
            "socket_permissions": socket_permissions(service.gateway_socket_path)
            if getattr(service, "gateway_socket_path", None) else None,
        }

    def get_snapshot(self) -> dict[str, Any]:
        """Finite, grounded world+body snapshot (ticket sections 37-38)."""

        service = self.service
        world = self._world_live()
        substrate = self._substrate() or {}
        location = world.get("location") or {}

        placements = substrate.get("placements") or {}
        hands: dict[str, Optional[str]] = {slot: None for slot in HAND_SLOTS}
        for object_id, placement in placements.items():
            if placement.get("kind") == "hand" and placement.get("slot_id") in hands:
                hands[placement["slot_id"]] = object_id
        if not self.include_test_objects:
            hands = {slot: (None if is_test_object(held) else held)
                     for slot, held in hands.items()}
        # A readable label instead of a 70-char instance id: the chat model
        # should see "basic_bed", not `item-8ad09561...`.  The join is with the
        # property economy in this same home, so it is bounded and local.
        labels: dict[str, str] = {}
        try:
            economy = json.loads(
                (service.config.world_home / "state" / "world_property_economy.json")
                .read_text(encoding="utf-8")
            )
            for item in economy.get("items") or []:
                if isinstance(item, dict) and item.get("item_instance_id"):
                    labels[item["item_instance_id"]] = str(
                        item.get("catalog_item_id") or "item"
                    )
        except Exception:
            labels = {}

        visible: list[dict[str, Any]] = []
        for object_id in self._visible_object_ids():
            placement = placements.get(object_id) or {}
            visible.append({
                "object_id": object_id,
                "label": labels.get(object_id) or object_id,
                "kind": placement.get("kind"),
                "surface_id": placement.get("surface_id"),
                "container_id": placement.get("container_id"),
                "slot_id": placement.get("slot_id"),
                "in_hand": placement.get("kind") == "hand",
            })
        return {
            "kind": KIND_GATEWAY_API,
            "schema_version": SCHEMA_GATEWAY_API,
            "world_id": world.get("world_id"),
            "world_revision": world.get("revision"),
            "location": {"place_id": location.get("place_id"), "area_id": location.get("area_id")},
            "scene_id": (world.get("scene") or {}).get("scene_id"),
            "pose": substrate.get("pose"),
            "orientation": substrate.get("orientation"),
            "hands": hands,
            "visible_objects": visible,
            "visible_object_count": len(visible),
            "observation_scope": "finite",
            "includes_test_objects": self.include_test_objects,
            "full_world_json_exposed": False,
        }

    def get_observation(self) -> dict[str, Any]:
        """The bounded observation the chat may be grounded on (section 13)."""

        snapshot = self.get_snapshot()
        return {
            "kind": KIND_GATEWAY_API,
            "schema_version": SCHEMA_GATEWAY_API,
            "world_id": snapshot["world_id"],
            "location": snapshot["location"],
            "pose": snapshot["pose"],
            "hands_occupied": sorted(
                slot for slot, held in (snapshot.get("hands") or {}).items() if held
            ),
            "visible_object_count": snapshot["visible_object_count"],
            "note": "bounded observation only; no raw body values, no full world JSON",
        }

    def get_body_signals(self) -> dict[str, Any]:
        """Bounded BodySignal -- never raw floats (ticket section 14)."""

        service = self.service
        state = service.body_state
        if not state:
            return {
                "kind": KIND_GATEWAY_API,
                "schema_version": SCHEMA_GATEWAY_API,
                "available": False,
                "signals": [],
                "raw_values_exposed": False,
            }
        import body_signal as bsi

        produced = bsi.produce_signals(state, occurred_at=service._moment())
        # body_summary() already proves the qualitative shape; re-derive here
        # through the service so the gateway never touches body_runtime.
        summary = service.body_summary()
        return {
            "kind": KIND_GATEWAY_API,
            "schema_version": SCHEMA_GATEWAY_API,
            "available": True,
            "recovery": summary.get("recovery"),
            "latch": produced.get("latch"),
            "signals": produced.get("signals"),
            "bands_only": True,
            "raw_values_exposed": False,
        }

    def _labels(self) -> dict[str, str]:
        """Economy-joined readable names, so the model never sees a raw 70-char id."""

        labels: dict[str, str] = {}
        try:
            economy = json.loads(
                (self.service.config.world_home / "state" / "world_property_economy.json")
                .read_text(encoding="utf-8")
            )
            for item in economy.get("items") or []:
                if isinstance(item, dict) and item.get("item_instance_id"):
                    labels[item["item_instance_id"]] = str(
                        item.get("catalog_item_id") or "item"
                    )
        except Exception:
            labels = {}
        return labels

    def get_available_actions(self) -> dict[str, Any]:
        """What the gateway is allowed to ask for (ticket sections 18-20)."""

        substrate = self._substrate() or {}
        labels = self._labels()
        held = sorted(
            object_id for object_id, placement in (substrate.get("placements") or {}).items()
            if placement.get("kind") == "hand"
        )
        held = [] if self.include_test_objects else [h for h in held if not is_test_object(h)]
        visible = [
            {"object_id": object_id, "label": labels.get(object_id) or object_id}
            for object_id in self._visible_object_ids()
        ]

        pick_up = self._pick_up_availability(held=held)

        actions: list[dict[str, Any]] = [
            {
                "verb": "MOVE",
                "available": True,
                "params": {"destination_location_id": "string"},
                "requires": ["world.revision", "world.location"],
            },
            {
                "verb": "POSE",
                "available": True,
                "params": {"pose": list(POSES), "orientation": list(ORIENTATIONS)},
                "requires": ["body.revision", "body.pose"],
            },
            {
                "verb": "PLACE",
                "available": bool(held),
                "params": {"object_id": "string", "target": "placement_target"},
                "requires": ["body.revision", "body.pose"],
                "blocked_reason": None if held else "nothing_held",
            },
            {
                "verb": "PICK_UP",
                "available": bool(pick_up["available"]),
                "params": {"object_id": "string", "slot_id": list(HAND_SLOTS)},
                "requires": ["body.revision", "body.pose", "capability.takeable"],
                "blocked_reason": pick_up["blocked_reason"],
                "detail": pick_up["detail"],
                "candidates": pick_up["candidates"],
            },
        ]
        return {
            "kind": KIND_GATEWAY_API,
            "schema_version": SCHEMA_GATEWAY_API,
            "actions": actions,
            "visible_objects": visible,
            "portability": {
                "canonical_takeability_rule": True,
                "rule": "capability.takeable from the object's canonical identity "
                        "(economy catalog entry, or a declared fixture object)",
                "pick_up_blocked_reason": pick_up["blocked_reason"],
                "takeable_candidates": pick_up["candidates"],
                "fail_closed": True,
            },
        }

    def get_action_result(self, execution_id: Any) -> dict[str, Any]:
        """Canonical truth for one execution.  Never invents an outcome."""

        execution_id = _text(execution_id, "execution_id", maximum=256)
        view = self.service.resolver.reconcile(execution_id)
        return {
            "kind": KIND_GATEWAY_API,
            "schema_version": SCHEMA_GATEWAY_API,
            "execution_id": execution_id,
            "known": view.get("known"),
            "verdict": view.get("verdict"),
            "state": view.get("state"),
            "result": view.get("result"),
            "action_type": view.get("action_type"),
            "authoritative": True,
            "source": "resolver_ledger",
        }

    # ---- the one mutation ------------------------------------------------
    def submit_action(self, request: Any) -> dict[str, Any]:
        """Gateway -> service -> Resolver.  The gateway never commits.

        M14A: the request MUST carry a one-shot ``authorization_id`` issued by
        the Intent Executor for this exact execution (id + verb + args).  The
        surface consumes it atomically; anything else is rejected before the
        Resolver is contacted (fail-closed, zero mutation).
        """

        if not isinstance(request, Mapping):
            raise GatewayAPIError("submit_action requires an object")

        action = _text(request.get("action") or request.get("verb"), "action", maximum=32)
        execution_id = _text(request.get("execution_id"), "execution_id", maximum=256)
        params = request.get("params") or {}
        if not isinstance(params, Mapping):
            raise GatewayAPIError("params must be an object")
        observed = request.get("observed_dependencies") or {}
        if not isinstance(observed, Mapping):
            raise GatewayAPIError("observed_dependencies must be an object")

        # ticket 55: the caller cannot supply authority.  The service stamps it.
        if "authority" in request or "confirm_apply" in request:
            raise GatewayAPIError(
                "authority/confirm_apply are stamped by the service, not supplied by the gateway"
            )

        # M14A: consume the one-shot authorization BEFORE any resolver contact.
        if self.authorizations is None:
            return self._reject_authorization(
                execution_id, action, "AUTHORIZATION_REGISTRY_UNAVAILABLE",
                "no authorization registry is wired to this surface",
            )
        authorization_id = request.get("authorization_id")
        if not isinstance(authorization_id, str) or not authorization_id.strip():
            return self._reject_authorization(
                execution_id, action, "AUTHORIZATION_REQUIRED",
                "submit_action requires an Intent-Executor-issued authorization_id",
            )
        try:
            record = self.authorizations.verify_and_consume(
                authorization_id=authorization_id,
                execution_id=execution_id,
                verb=action,
                params=dict(params),
                actor=GATEWAY_ACTOR,
                # M15G: cross-decision replay guard.  The submission names the
                # Decision it belongs to; a mismatched capability is refused.
                decision_id=request.get("decision_ref"),
            )
        except waa.AuthorizationError as error:
            return self._reject_authorization(
                execution_id, action, str(error),
                "authorization verify/consume failed (fail-closed)",
            )

        proposal = {
            "action": action,
            "execution_id": execution_id,
            "actor_id": GATEWAY_ACTOR,
            "body_id": (self.service.body_state or {}).get("body_id"),
            "authority": GATEWAY_AUTHORITY,
            "confirm_apply": True,
            "observed_dependencies": {k: observed[k] for k in sorted(observed)},
            "params": dict(params),
        }

        # Exposure policy: refuse anything the surface does not expose, so a
        # caller cannot route around get_available_actions (ticket 18-20).
        exposed = {a["verb"] for a in self.get_available_actions()["actions"] if a["available"]}
        if action not in exposed:
            return {
                "kind": KIND_GATEWAY_API,
                "schema_version": SCHEMA_GATEWAY_API,
                "execution_id": execution_id,
                "action": action,
                "status": "rejected",
                "reason_code": "NOT_EXPOSED",
                "detail": "verb %s is not exposed to the gateway right now" % action,
                "world_unchanged": True,
                "body_unchanged": True,
            }

        result = self.service.act(proposal)
        return {
            "kind": KIND_GATEWAY_API,
            "schema_version": SCHEMA_GATEWAY_API,
            "execution_id": result.get("execution_id"),
            "action": result.get("action"),
            "status": result.get("status"),
            "reason_code": result.get("reason_code"),
            "applied_to_body": result.get("applied_to_body"),
            "authoritative": True,
            "source": "resolver",
            "authorization": {
                "caller": GATEWAY_CALLER,
                "authority": GATEWAY_AUTHORITY,
                "authorization_id": record.get("authorization_id"),
                "decision_ref": record.get("decision_id"),
                "intent_id": record.get("intent_id"),
                "state": "CONSUMED",
            },
        }

    def _reject_authorization(
        self, execution_id: str, action: str, reason_code: str, detail: str
    ) -> dict[str, Any]:
        """Fail-closed rejection: zero mutation, zero ledger SUCCESS."""
        return {
            "kind": KIND_GATEWAY_API,
            "schema_version": SCHEMA_GATEWAY_API,
            "execution_id": execution_id,
            "action": action,
            "status": "rejected",
            "reason_code": reason_code,
            "detail": detail,
            "world_unchanged": True,
            "body_unchanged": True,
            "authoritative": True,
            "source": "authorization_firewall",
        }

    # ---- dispatch --------------------------------------------------------
    def handle(self, command: Any, argument: Any = None) -> dict[str, Any]:
        name = str(command or "").strip().lower()
        if name in GATEWAY_FORBIDDEN_COMMANDS:
            return {
                "ok": False,
                "error": "forbidden_on_gateway_surface",
                "command": name,
                "detail": "this command belongs to the admin control surface",
            }
        if name not in GATEWAY_COMMANDS:
            return {
                "ok": False,
                "error": "unknown_command",
                "command": name,
                "allowed": list(GATEWAY_COMMANDS),
            }
        try:
            handler = {
                "ping": lambda: {"pong": True},
                "get_status": self.get_status,
                "get_snapshot": self.get_snapshot,
                "get_observation": self.get_observation,
                "get_body_signals": self.get_body_signals,
                "get_available_actions": self.get_available_actions,
                "get_action_result": lambda: self.get_action_result(
                    (argument or {}).get("execution_id") if isinstance(argument, Mapping) else argument
                ),
                "submit_action": lambda: self.submit_action(argument),
            }[name]
            payload = handler()
        except GatewayAPIError as error:
            return {"ok": False, "error": "invalid_request", "command": name, "detail": str(error)}
        except Exception as error:  # never take the service down for a gateway call
            return {"ok": False, "error": type(error).__name__, "command": name,
                    "detail": str(error)[:400]}
        return {"ok": True, "command": name, "result": payload}


def peer_credentials(connection: socket.socket) -> Optional[dict[str, int]]:
    """Return {pid, uid, gid} for the peer, or None when unavailable (Linux)."""

    import struct
    cr = getattr(socket, "SO_PEERCRED", None)
    if cr is None:
        return None
    try:
        raw = connection.getsockopt(socket.SOL_SOCKET, cr, struct.calcsize("3i"))
        pid, uid, gid = struct.unpack("3i", raw)
        return {"pid": pid, "uid": uid, "gid": gid}
    except OSError:
        return None
