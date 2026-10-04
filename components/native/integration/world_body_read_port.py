"""World/Body read port — the approved read-only surface (CHIYO-PROD-FINAL Phase 4 §19).

The World/Body runtime already publishes a read surface over its gateway socket; the
client that speaks it ships with the core
(``chiyo/life_runtime/service/bridge/world_body_client.py``) and exposes exactly:

    get_status()            readiness and revision of the service
    get_snapshot()          world observation snapshot (revision-bearing)
    get_observation()       world observation
    get_body_signals()      body capacity / interruptibility signals
    get_available_actions() currently available capabilities
    get_action_result(id)   the result of one previously submitted action

This module is the typed port over that surface. It is **read-only by construction**:
it holds a client and only ever calls read commands. ``submit_action`` is deliberately
not reachable from here, so a caller cannot mutate World/Body through this object.

Fail-safe rules (work order §19, §21):

* a non-OK outcome never becomes a fabricated state — ``data`` stays ``None`` and the
  record is marked ``degraded``;
* ``NOT_READY`` and transport faults are reported with their real vocabulary, not
  collapsed into a boolean;
* the port never retries a mutation, because it never performs one.
"""
from __future__ import annotations

from typing import Any, Mapping, Optional

__all__ = [
    "OUTCOME_OK",
    "OUTCOME_NOT_READY",
    "OUTCOME_UNAVAILABLE",
    "OUTCOME_TIMEOUT",
    "OUTCOME_FAILURE",
    "OUTCOME_INVALID",
    "READ_FIELDS",
    "WorldBodyReadPort",
    "WorldBodyReadError",
]

#: Mirrors the client's closed outcome vocabulary.
OUTCOME_OK = "OK"
OUTCOME_NOT_READY = "NOT_READY"
OUTCOME_UNAVAILABLE = "UNAVAILABLE"
OUTCOME_TIMEOUT = "TIMEOUT"
OUTCOME_FAILURE = "FAILURE"
OUTCOME_INVALID = "INVALID"

#: The fields this port exposes, matching the work order's minimum scope.
READ_FIELDS = ("observation", "body", "capability", "revision", "status")

_DEGRADED_OUTCOMES = frozenset(
    {OUTCOME_NOT_READY, OUTCOME_UNAVAILABLE, OUTCOME_TIMEOUT, OUTCOME_FAILURE, OUTCOME_INVALID}
)


class WorldBodyReadError(RuntimeError):
    """Raised only for programmer error (a missing read method on the client)."""


class WorldBodyReadPort:
    """Typed, read-only view of the live World/Body runtime."""

    def __init__(self, client: Any) -> None:
        for method in ("get_status", "get_snapshot", "get_observation",
                       "get_body_signals", "get_available_actions"):
            if not callable(getattr(client, method, None)):
                raise WorldBodyReadError(f"client is missing the read method {method!r}")
        self._client = client

    # ---- individual reads -------------------------------------------------
    def observation(self) -> dict[str, Any]:
        return self._read("observation", "get_observation")

    def body(self) -> dict[str, Any]:
        """Body capacity and interruptibility signals."""
        return self._read("body", "get_body_signals")

    def capability(self) -> dict[str, Any]:
        """Currently available actions (the Current Capability surface)."""
        return self._read("capability", "get_available_actions")

    def revision(self) -> dict[str, Any]:
        """The revision-bearing snapshot."""
        return self._read("revision", "get_snapshot")

    def status(self) -> dict[str, Any]:
        return self._read("status", "get_status")

    # ---- combined ---------------------------------------------------------
    def context(self) -> dict[str, Any]:
        """Everything an Activity needs, with one aggregate degraded flag.

        A single unavailable source degrades that field only; the rest still return
        their real values. Nothing is invented to fill a gap.
        """
        fields = {name: getattr(self, name)() for name in READ_FIELDS}
        degraded_fields = [n for n, f in fields.items() if f["degraded"]]
        return {
            "schema": "chiyo.integration.world_body_context.v1",
            "fields": fields,
            "degraded": bool(degraded_fields),
            "degraded_fields": degraded_fields,
            "read_only": True,
            "mutations_attempted": 0,
        }

    # ---- internals --------------------------------------------------------
    def _read(self, field: str, method: str) -> dict[str, Any]:
        try:
            result = getattr(self._client, method)()
        except Exception as exc:  # noqa: BLE001 - a read fault degrades, never raises upward
            return {"field": field, "outcome": OUTCOME_UNAVAILABLE, "data": None,
                    "reason": f"{type(exc).__name__}: {exc}", "degraded": True}
        outcome = str(getattr(result, "outcome", OUTCOME_UNAVAILABLE))
        data = getattr(result, "data", None)
        reason = getattr(result, "reason", None)
        # NOT_READY is a real answer from the service and carries its own readiness
        # payload, so that payload is preserved (still degraded). Every other non-OK
        # outcome carries no usable state and must not be presented as if it did.
        usable = outcome in (OUTCOME_OK, OUTCOME_NOT_READY) and isinstance(data, Mapping)
        return {
            "field": field,
            "outcome": outcome,
            "data": dict(data) if usable else None,
            "reason": reason,
            "degraded": outcome != OUTCOME_OK,
        }
