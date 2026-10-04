"""Chiyo World/Body bridge — a thin gateway-side adapter (M13B).

What this plugin is NOT:

* it does **not** import a single World implementation module.  There is no
  ``world_*`` / ``body_*`` import anywhere in this file, by design: the gateway
  reaches the World only through a unix socket (ticket sections 1, 6, 31);
* it does **not** read or write ``world_state.json`` / body runtime directly;
* it is **not** the old ``shiomi-world`` plugin.  That one performs World actions
  through the legacy ``life_execute.ProductionAdapter`` /
  ``world_affordance_wiring`` path, so enabling it would create a second action
  authority.  Ticket section 6 requires either adapting it or writing a thin
  bridge; the thin bridge is smaller and safer.

What it does:

* one hook, ``pre_llm_call``, which appends a **bounded** World/Body observation
  to the model's user message — and appends nothing at all when the standalone
  runtime is off, down, or not ready (sections 13, 14, 26, 27, 37);
* one read tool, ``world_body``;
* one optional mutation tool, ``world_body_action``, which submits an
  ActionProposal to the standalone service and is **disabled unless**
  ``WORLD_BODY_MUTATION_MODE=tool`` (section 46).  It never commits anything
  itself: the Resolver is the sole commit authority (section 12).
"""

from __future__ import annotations

import json
import logging
import os

#: The gateway imports this plugin as ``hermes_plugins.chiyo_world_bridge``
#: and keeps the plugin directory OFF ``sys.path``, so absolute imports of
#: sibling modules fail there while working in every harness (where the
#: directory is on the path).  Register ourselves at import time — in BOTH
#: load modes — so absolute sibling imports always resolve.
import sys as _sys

_HERE_DIR = os.path.dirname(os.path.abspath(__file__))
if _HERE_DIR not in _sys.path:
    _sys.path.insert(0, _HERE_DIR)

try:  # loaded as a plugin package (the gateway loader execs __init__.py by path)
    from . import world_body_client as wbc  # type: ignore
except ImportError:  # noqa: BLE001 - loaded directly (tests, CLI)
    import pathlib as _pathlib
    import sys as _sys

    _HERE = str(_pathlib.Path(__file__).resolve().parent)
    if _HERE not in _sys.path:
        _sys.path.insert(0, _HERE)
    import world_body_client as wbc  # type: ignore

logger = logging.getLogger("chiyo_world_bridge")

TOOLSET = "chiyo_world"

MUTATION_MODE_ENV = "WORLD_BODY_MUTATION_MODE"
MUTATION_OFF = "off"
MUTATION_TOOL = "tool"

_CLIENT = None

WORLD_BODY_SCHEMA = {
    "name": "world_body",
    "description": (
        "Read the current grounded World/Body state: where she is, her pose, what is in her "
        "hands, the bounded set of visible objects, and her qualitative body signals (bands, "
        "never raw numbers). Read-only. Returns a small bounded JSON object; there is no way "
        "to see the whole world through this tool."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "view": {
                "type": "string",
                "enum": ["snapshot", "observation", "signals", "actions", "status"],
                "description": "Which bounded view to read. Default 'observation'.",
            },
            "execution_id": {
                "type": "string",
                "description": "With view='status': look up one previously submitted action result.",
            },
        },
        "required": [],
    },
}

WORLD_BODY_ACTION_SCHEMA = {
    "name": "world_body_action",
    "description": (
        "Submit ONE physical action to the World/Body runtime. The runtime's Resolver is the only "
        "thing that can commit a change: a natural-language claim is never treated as a result. "
        "Reuse the same execution_id to retry safely (exactly-once). Available verbs are reported "
        "by world_body(view='actions'); anything not listed there will be refused."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["MOVE", "POSE", "PLACE", "PICK_UP"]},
            "execution_id": {
                "type": "string",
                "description": "Stable id for this action attempt. Reuse it to retry the same attempt.",
            },
            "params": {
                "type": "object",
                "description": (
                    "MOVE: {destination_location_id}. "
                    "POSE: {pose, orientation}. "
                    "PLACE: {object_id, target:{kind,surface_id|container_id|slot_id,location_id}}. "
                    "PICK_UP: {object_id, slot_id} (only exposed when World takeability allows)."
                ),
            },
            "observed_dependencies": {
                "type": "object",
                "description": (
                    "Dependency values you observed, used for optimistic concurrency, e.g. "
                    "{'world.revision': 9, 'world.location': 'home', 'body.pose': 'standing'}. "
                    "A stale value is refused rather than applied."
                ),
            },
        },
        "required": ["action", "execution_id"],
    },
}


def _mutation_mode() -> str:
    raw = str(os.environ.get(MUTATION_MODE_ENV) or MUTATION_OFF).strip().lower()
    return raw if raw in (MUTATION_OFF, MUTATION_TOOL) else MUTATION_OFF


def _client() -> "wbc.WorldBodyClient":
    global _CLIENT
    if _CLIENT is None:
        _CLIENT = wbc.WorldBodyClient()
    return _CLIENT


def _integration_mode() -> str:
    try:
        return wbc.resolve_mode()
    except Exception:  # a malformed mode must never look enabled
        return wbc.MODE_OFF


def _json(payload) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def world_context_hook(**kwargs):
    """Append bounded World/Body context to the user message.

    Returns ``{"context": ...}`` or ``None``.  In shadow mode it reads and logs
    but injects nothing (ticket section 25).  It never raises into the turn.

    The INFO line records turn-correlation identifiers only -- session / turn /
    task / platform -- never message text, never the capsule.  That is what lets
    an operator tie "this chat turn" to "this world read" from the logs alone
    (ticket section 57).
    """

    turn_id = str(kwargs.get("turn_id") or "")
    try:
        mode = _integration_mode()
        if mode == wbc.MODE_OFF:
            return None
        client = _client()
        status = client.get_status()
        readiness = str((status.data or {}).get("readiness") or status.outcome)
        context = wbc.render_grounded_context(client)
        if not context:
            logger.info(
                "world-body context skipped (readiness=%s outcome=%s session=%s "
                "turn=%s platform=%s)",
                readiness, status.outcome, kwargs.get("session_id"),
                turn_id, kwargs.get("platform"),
            )
            return None
        if mode == wbc.MODE_SHADOW:
            logger.info("world-body shadow read: %s", context)
            return None
        logger.info(
            "world-body context injected (readiness=%s chars=%d session=%s turn=%s "
            "task=%s platform=%s first_turn=%s)",
            readiness, len(context),
            kwargs.get("session_id"), turn_id, kwargs.get("task_id"),
            kwargs.get("platform"), kwargs.get("is_first_turn"),
        )
        return {"context": context}
    except Exception as error:  # a bridge failure must never break a chat turn
        logger.warning("world-body hook failed (turn=%s): %s", turn_id, error)
        return None


def _model_view(view: str, data):
    """Project a gateway payload down to what Chiyo can actually perceive.

    M13B-R1: the model-facing surfaces carry *lived facts only*.  Version
    counters, world ids, canonical object ids and engine vocabulary are
    reconciliation metadata and are removed here.  Kept: where she is, her pose,
    what she can see and hold, and the qualitative band of what she feels.
    """

    if not isinstance(data, dict):
        return data

    engine_keys = frozenset({
        "kind", "schema_version", "world_id", "world_revision",
        "source_body_version", "body_id", "signal_id", "occurred_at",
        "visibility_scope", "source_transition_refs", "runtime_version",
        "object_id",
    })
    internal_deps = ("world.revision", "body.revision")

    def scrub(value):
        if isinstance(value, dict):
            return {k: scrub(v) for k, v in value.items() if k not in engine_keys}
        if isinstance(value, list):
            return [scrub(v) for v in value]
        return value

    scrubbed = scrub(data)

    for action in scrubbed.get("actions") or []:
        if isinstance(action, dict) and isinstance(action.get("requires"), list):
            action["requires"] = [r for r in action["requires"] if r not in internal_deps]

    if isinstance(scrubbed.get("visible_objects"), list):
        kept = []
        for obj in scrubbed["visible_objects"]:
            if not isinstance(obj, dict):
                continue
            minimal = {k: v for k, v in obj.items() if k in ("label", "kind", "in_hand")}
            if minimal:
                kept.append(minimal)
        scrubbed["visible_objects"] = kept
    return scrubbed


def _world_body_tool(args, **kwargs) -> str:
    client = _client()
    if not client.enabled:
        return _json({"outcome": wbc.UNAVAILABLE, "reason": "integration_off",
                      "note": "World/Body integration is off; no world state is available"})
    view = str((args or {}).get("view") or "observation")
    if view == "snapshot":
        result = client.get_snapshot()
    elif view == "signals":
        result = client.get_body_signals()
    elif view == "actions":
        result = client.get_available_actions()
    elif view == "status":
        execution_id = (args or {}).get("execution_id")
        result = (client.get_action_result(execution_id) if execution_id
                  else client.get_status())
    else:
        result = client.get_observation()
    return _json({"outcome": result.outcome, "reason": result.reason, "view": view,
                  "data": _model_view(view, result.data)})


def _check_world_body() -> bool:
    """Tool visibility gate: the runtime must be reachable and READY."""

    if _integration_mode() == wbc.MODE_OFF:
        return False
    return _client().ready()


def _world_body_action_tool(args, **kwargs) -> str:
    client = _client()
    if _mutation_mode() != MUTATION_TOOL:
        return _json({"outcome": wbc.INVALID, "reason": "mutation_disabled",
                      "note": "World/Body mutation exposure is off "
                              "(WORLD_BODY_MUTATION_MODE != tool)"})
    if not client.enabled:
        return _json({"outcome": wbc.UNAVAILABLE, "reason": "integration_off"})
    payload = args or {}
    result = client.submit_action(
        action=str(payload.get("action") or ""),
        execution_id=str(payload.get("execution_id") or ""),
        params=payload.get("params") or {},
        observed_dependencies=payload.get("observed_dependencies") or {},
    )
    return _json({"outcome": result.outcome, "reason": result.reason, "data": result.data})


def _check_world_body_action() -> bool:
    if _mutation_mode() != MUTATION_TOOL:
        return False
    if _integration_mode() != wbc.MODE_CANONICAL:
        return False
    return _client().ready()


def register(ctx) -> None:
    # M13C cutover: the pre_llm_call hook is the decision-triggering hook
    # (evaluates the terminal Decision, then runs the bounded capsule).  The
    # plain world_context_hook remains available for off/shadow tests but is
    # not what production registers.
    try:
        from . import decision_trigger as _dt  # type: ignore
    except ImportError:  # direct-load (tests)
        import decision_trigger as _dt  # type: ignore
    ctx.register_hook("pre_llm_call", _dt.decision_hook)
    ctx.register_tool(
        name="world_body", toolset=TOOLSET, schema=WORLD_BODY_SCHEMA,
        handler=_world_body_tool, check_fn=_check_world_body,
        description="Read the bounded World/Body state", emoji="🌏",
    )
    ctx.register_tool(
        name="world_body_action", toolset=TOOLSET, schema=WORLD_BODY_ACTION_SCHEMA,
        handler=_world_body_action_tool, check_fn=_check_world_body_action,
        description="Submit one physical action to the World/Body runtime", emoji="🖐",
    )
