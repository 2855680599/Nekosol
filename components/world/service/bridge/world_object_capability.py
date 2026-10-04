#!/usr/bin/env python3
"""M15F: object capability / portability semantics.

Ownership: capabilities belong to the World/Object owner, NOT to the gateway.
This module is the *contract* plus the enforce-side checks the Resolver needs
before any PICK_UP / PLACE.  It never invents a capability.

Fail-closed rules (ticket §M15F.2/.3):
  * an object with no canonical capability is ``takeable = UNKNOWN`` and
    PICK_UP is DENIED — never "assumable true";
  * furniture is refused because its capability says ``fixed / portable=false``,
    never because its label matches a known name -- this module names no object;
  * both hand slots are checked atomically before a PICK_UP is allowed.
"""

from __future__ import annotations

from typing import Any, Mapping, Optional

SCHEMA = "world.object.capability.v1"

#: capability states; UNKNOWN is the fail-closed default
KNOWN_TRUE = "true"
KNOWN_FALSE = "false"
UNKNOWN = "UNKNOWN"

HAND_SLOTS = ("left", "right")
DENIED = "DENIED"
ALLOWED = "ALLOWED"


def capability_of(obj: Mapping[str, Any]) -> dict[str, Any]:
    """Read an object's capability block, defaulting everything to UNKNOWN."""

    cap = dict((obj or {}).get("capability") or {})
    out = {"takeable": UNKNOWN, "portable": UNKNOWN, "placeable": UNKNOWN,
           "fixed": UNKNOWN, "hand_requirement": UNKNOWN, "weight_class": UNKNOWN}
    for key in out:
        value = cap.get(key)
        if isinstance(value, bool):
            out[key] = KNOWN_TRUE if value else KNOWN_FALSE
        elif isinstance(value, str) and value.lower() in (KNOWN_TRUE, KNOWN_FALSE,
                                                          "unknown"):
            out[key] = KNOWN_FALSE if value.lower() == "false" else (
                UNKNOWN if value.lower() == "unknown" else KNOWN_TRUE)
    return out


def pick_up_verdict(*, obj: Optional[Mapping[str, Any]], hand: str,
                    already_held: bool = False, visible: bool = True,
                    reachable: bool = True, dependencies_fresh: bool = True,
                    authorization_valid: bool = True) -> dict[str, Any]:
    """Atomic pre-flight for PICK_UP.  Every check must pass."""

    reasons = []
    if obj is None:
        return {"verdict": DENIED, "reason": "object_unknown"}
    if not visible:
        reasons.append("not_visible")
    if not reachable:
        reasons.append("not_reachable")
    cap = capability_of(obj)
    if cap["takeable"] != KNOWN_TRUE:
        # UNKNOWN and false both deny: never assume a capability
        reasons.append("takeable=%s" % cap["takeable"])
    if hand not in HAND_SLOTS:
        reasons.append("bad_hand_slot=%s" % hand)
    if already_held:
        reasons.append("hand_occupied")
    if not dependencies_fresh:
        reasons.append("stale_dependency")
    if not authorization_valid:
        reasons.append("authorization_invalid")
    return {"verdict": ALLOWED if not reasons else DENIED,
            "reason": "ok" if not reasons else ",".join(reasons),
            "capability": cap}


def place_verdict(*, obj: Optional[Mapping[str, Any]], held: bool,
                  target_kind: str, target_permits: bool,
                  slot_available: bool, authorization_valid: bool = True
                  ) -> dict[str, Any]:
    """Atomic pre-flight for PLACE."""

    reasons = []
    if obj is None:
        return {"verdict": DENIED, "reason": "object_unknown"}
    if not held:
        reasons.append("object_not_held")
    if target_kind not in ("surface", "container", "slot"):
        reasons.append("bad_target_kind=%s" % target_kind)
    if not target_permits:
        reasons.append("target_refuses_placement")
    if not slot_available:
        reasons.append("no_space")
    cap = capability_of(obj)
    if cap["placeable"] != KNOWN_TRUE:
        reasons.append("placeable=%s" % cap["placeable"])
    if not authorization_valid:
        reasons.append("authorization_invalid")
    return {"verdict": ALLOWED if not reasons else DENIED,
            "reason": "ok" if not reasons else ",".join(reasons),
            "capability": cap}


def hand_state(hands: Mapping[str, Any]) -> dict[str, Any]:
    """Normalise the hand slots into free/occupied."""

    slots = {}
    for slot in HAND_SLOTS:
        value = (hands or {}).get(slot)
        slots[slot] = "occupied" if value else "free"
    free = [s for s, v in slots.items() if v == "free"]
    return {"slots": slots, "free": free, "any_free": bool(free)}


def canonical_portable_objects(objects: Any) -> list[dict[str, Any]]:
    """Objects the WORLD declares portable.  Empty means the production verb
    stays blocked: ``BLOCKED_NO_CANONICAL_OBJECT`` is a legal state."""

    out = []
    for obj in objects or []:
        cap = capability_of(obj)
        if cap["portable"] == KNOWN_TRUE and cap["takeable"] == KNOWN_TRUE:
            out.append(dict(obj))
    return out


def production_verbs_available(objects: Any) -> dict[str, Any]:
    portable = canonical_portable_objects(objects)
    if portable:
        return {"verbs": ["PICK_UP", "PLACE"], "blocked_reason": None,
                "portable_count": len(portable)}
    return {"verbs": [], "blocked_reason": "BLOCKED_NO_CANONICAL_OBJECT",
            "portable_count": 0}
