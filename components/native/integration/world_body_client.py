#!/usr/bin/env python3
"""WorldBodyClient -- the gateway's only door to the World/Body runtime (M13B).

Ticket sections 29-32:

* one client, one socket, every gateway-side World call goes through it;
* every call has an explicit timeout, so a stuck service can never hang a
  Telegram reply forever;
* outcomes are a closed vocabulary -- ``None`` is never used to hide a failure;
* reads may retry, a mutation may **only** retry with the *same* execution_id
  (which the exactly-once ledger makes safe).

This module holds no secrets and imports no World implementation.
"""

from __future__ import annotations

import json
import os
import pathlib
import socket
import time
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional

SCHEMA_CLIENT = "world.body.gateway.client.v1"

MODE_ENV = "WORLD_BODY_INTEGRATION_MODE"
SOCKET_ENV = "WORLD_BODY_SOCKET"
MODE_OFF = "off"
MODE_SHADOW = "shadow"
MODE_CANONICAL = "canonical"
MODES = (MODE_OFF, MODE_SHADOW, MODE_CANONICAL)
DEFAULT_SOCKET = "./data/world/run/gateway.sock"

OK = "OK"
NOT_READY = "NOT_READY"
UNAVAILABLE = "UNAVAILABLE"
TIMEOUT = "TIMEOUT"
DEPENDENCY_UNAVAILABLE = "DEPENDENCY_UNAVAILABLE"
READY = "READY"
SUCCESS = "SUCCESS"
FAILURE = "FAILURE"
UNKNOWN = "UNKNOWN"
STALE = "STALE"
CONFLICT = "CONFLICT"
INVALID = "INVALID"

READ_OUTCOMES = (OK, NOT_READY, UNAVAILABLE, TIMEOUT, DEPENDENCY_UNAVAILABLE)
ACTION_OUTCOMES = (SUCCESS, FAILURE, UNKNOWN, STALE, CONFLICT, INVALID,
                   NOT_READY, UNAVAILABLE, TIMEOUT, DEPENDENCY_UNAVAILABLE)


def resolve_mode(environment: Optional[Mapping[str, str]] = None) -> str:
    """The gateway-side integration gate.  Defaults to ``off``."""

    env = environment if environment is not None else os.environ
    raw = str(env.get(MODE_ENV) or MODE_OFF).strip().lower()
    if raw not in MODES:
        raise ValueError("%s must be one of %s" % (MODE_ENV, list(MODES)))
    return raw


def resolve_socket(environment: Optional[Mapping[str, str]] = None) -> pathlib.Path:
    env = environment if environment is not None else os.environ
    return pathlib.Path(str(env.get(SOCKET_ENV) or DEFAULT_SOCKET))


@dataclass
class ClientResult:
    """Never ``None``; always says what happened."""

    outcome: str
    data: Optional[dict[str, Any]] = None
    reason: Optional[str] = None
    latency_ms: Optional[float] = None
    attempts: int = 1

    @property
    def ok(self) -> bool:
        return self.outcome == OK

    def as_dict(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome,
            "ok": self.ok,
            "reason": self.reason,
            "latency_ms": self.latency_ms,
            "attempts": self.attempts,
        }


@dataclass
class ClientMetrics:
    """Small, log-safe counters (ticket section 58).  No chat content."""

    calls: int = 0
    ok: int = 0
    not_ready: int = 0
    unavailable: int = 0
    timeouts: int = 0
    errors: int = 0
    submitted: int = 0
    action_success: int = 0
    action_failure: int = 0
    action_unknown: int = 0
    latencies_ms: list[float] = field(default_factory=list)

    def observe(self, result: ClientResult) -> None:
        self.calls += 1
        if result.latency_ms is not None:
            self.latencies_ms.append(result.latency_ms)
            del self.latencies_ms[:-200]
        {"OK": "ok", "NOT_READY": "not_ready", "UNAVAILABLE": "unavailable",
         "TIMEOUT": "timeouts"}.get(result.outcome, None)
        if result.outcome == OK:
            self.ok += 1
        elif result.outcome == NOT_READY:
            self.not_ready += 1
        elif result.outcome == TIMEOUT:
            self.timeouts += 1
        elif result.outcome in (UNAVAILABLE, DEPENDENCY_UNAVAILABLE):
            self.unavailable += 1
        else:
            self.errors += 1

    def as_dict(self) -> dict[str, Any]:
        lat = sorted(self.latencies_ms)
        return {
            "calls": self.calls,
            "ok": self.ok,
            "not_ready": self.not_ready,
            "unavailable": self.unavailable,
            "timeouts": self.timeouts,
            "errors": self.errors,
            "submitted": self.submitted,
            "action_success": self.action_success,
            "action_failure": self.action_failure,
            "action_unknown": self.action_unknown,
            "latency_ms_p50": lat[len(lat) // 2] if lat else None,
            "latency_ms_max": lat[-1] if lat else None,
        }


class WorldBodyClient:
    """One client, one socket, explicit outcomes."""

    def __init__(
        self,
        *,
        socket_path: Optional[pathlib.Path | str] = None,
        timeout: float = 2.0,
        read_retries: int = 2,
        mode: Optional[str] = None,
        environment: Optional[Mapping[str, str]] = None,
    ):
        env = environment if environment is not None else os.environ
        self.environment = dict(env)
        self.mode = mode or resolve_mode(env)
        self.socket_path = pathlib.Path(socket_path) if socket_path else resolve_socket(env)
        self.timeout = float(timeout)
        self.read_retries = max(0, int(read_retries))
        self.metrics = ClientMetrics()
        #: Outcome of the most recent read; lets a caller report readiness
        #: without issuing a second round trip.
        self.last_status: Optional[str] = None

    # ---- transport -------------------------------------------------------
    @property
    def enabled(self) -> bool:
        return self.mode != MODE_OFF

    def _call_once(self, command: str, argument: Any = None) -> ClientResult:
        started = time.monotonic()
        request = json.dumps({"command": command, "argument": argument}) + "\n"
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.settimeout(self.timeout)
                client.connect(str(self.socket_path))
                client.sendall(request.encode())
                buffer = b""
                while b"\n" not in buffer:
                    chunk = client.recv(65536)
                    if not chunk:
                        break
                    buffer += chunk
        except socket.timeout:
            return ClientResult(TIMEOUT, reason="socket_timeout",
                                latency_ms=(time.monotonic() - started) * 1000)
        except FileNotFoundError:
            return ClientResult(UNAVAILABLE, reason="socket_missing",
                                latency_ms=(time.monotonic() - started) * 1000)
        except (ConnectionRefusedError, ConnectionResetError, BrokenPipeError, OSError) as error:
            return ClientResult(UNAVAILABLE, reason="socket_error:%s" % type(error).__name__,
                                latency_ms=(time.monotonic() - started) * 1000)
        latency = (time.monotonic() - started) * 1000
        if not buffer:
            return ClientResult(UNAVAILABLE, reason="empty_response", latency_ms=latency)
        try:
            payload = json.loads(buffer.decode("utf-8").strip().splitlines()[0])
        except (ValueError, IndexError):
            return ClientResult(UNAVAILABLE, reason="malformed_response", latency_ms=latency)
        if not payload.get("ok"):
            return ClientResult(FAILURE, data=payload,
                                reason=str(payload.get("error")), latency_ms=latency)
        return ClientResult(OK, data=payload.get("result"), latency_ms=latency)

    def call(self, command: str, argument: Any = None, *, retries: int = 0) -> ClientResult:
        """One call, with bounded retries.  Never raises for transport faults."""

        result = self._call_once(command, argument)
        attempts = 1
        # Only transport-level faults are retried, and only for reads.
        while (attempts <= retries
               and result.outcome in (TIMEOUT, UNAVAILABLE)
               and command != "submit_action"):
            time.sleep(min(0.1 * attempts, 0.5))
            result = self._call_once(command, argument)
            attempts += 1
        result.attempts = attempts
        self.metrics.observe(result)
        return result

    # ---- read ------------------------------------------------------------
    def _read(self, command: str, argument: Any = None) -> ClientResult:
        if not self.enabled:
            return ClientResult(UNAVAILABLE, reason="integration_off")
        result = self.call(command, argument, retries=self.read_retries)
        if command == "get_status":
            self.last_status = result.outcome
        if result.outcome == OK and isinstance(result.data, dict):
            readiness = result.data.get("readiness")
            if readiness == NOT_READY:
                return ClientResult(NOT_READY, data=result.data, reason="not_ready",
                                    latency_ms=result.latency_ms, attempts=result.attempts)
        return result

    def get_status(self) -> ClientResult:
        return self._read("get_status")

    def get_snapshot(self) -> ClientResult:
        return self._read("get_snapshot")

    def get_observation(self) -> ClientResult:
        return self._read("get_observation")

    def get_body_signals(self) -> ClientResult:
        return self._read("get_body_signals")

    def get_available_actions(self) -> ClientResult:
        return self._read("get_available_actions")

    def get_action_result(self, execution_id: str) -> ClientResult:
        if not isinstance(execution_id, str) or not execution_id.strip():
            return ClientResult(INVALID, reason="execution_id_required")
        return self._read("get_action_result", {"execution_id": execution_id})

    def ready(self) -> bool:
        if not self.enabled:
            return False
        result = self._read("get_status")
        return result.outcome == OK and bool((result.data or {}).get("ready"))

    # ---- the one mutation ------------------------------------------------
    def submit_action(
        self,
        *,
        action: str,
        execution_id: str,
        params: Mapping[str, Any],
        observed_dependencies: Optional[Mapping[str, Any]] = None,
        authorization_id: Optional[str] = None,
        decision_ref: Optional[str] = None,
    ) -> ClientResult:
        """Submit one action.  Retry only ever reuses the SAME execution_id.

        M14A: ``authorization_id`` is issued by the Intent Executor (the
        authority side) and consumed by the service.  Without it the service
        rejects the submission (fail-closed firewall).
        """

        if not self.enabled:
            return ClientResult(UNAVAILABLE, reason="integration_off")
        if not isinstance(execution_id, str) or not execution_id.strip():
            return ClientResult(INVALID, reason="execution_id_required")

        payload = {
            "action": action,
            "execution_id": execution_id,
            "params": dict(params or {}),
            "observed_dependencies": dict(observed_dependencies or {}),
        }
        if authorization_id:
            payload["authorization_id"] = authorization_id
        # M15G: carry the Decision reference so the service can bind the
        # capability to the Decision that minted it.
        if decision_ref:
            payload["decision_ref"] = decision_ref
        result = self.call("submit_action", payload, retries=0)
        self.metrics.submitted += 1

        if result.outcome != OK or not isinstance(result.data, dict):
            if result.outcome in (TIMEOUT, UNAVAILABLE):
                # Transport fault.  The mutation may or may not have committed;
                # we say UNKNOWN and let the caller re-submit the SAME id or ask
                # for the canonical result.  Never auto-retry a new id.
                self.metrics.action_unknown += 1
                return ClientResult(UNKNOWN, data=result.data,
                                    reason="transport_fault:%s" % result.outcome,
                                    latency_ms=result.latency_ms, attempts=result.attempts)
            self.metrics.action_failure += 1
            return result

        status = str(result.data.get("status") or "").lower()
        code = result.data.get("reason_code")
        if status in ("applied", "noop", "already_applied"):
            self.metrics.action_success += 1
            outcome = SUCCESS
        elif code == "STALE_STATE":
            self.metrics.action_failure += 1
            outcome = STALE
        elif status in ("rejected", "failed", "disabled"):
            self.metrics.action_failure += 1
            outcome = FAILURE
        else:
            # uncertain / prepared / anything unrecognised is never success.
            self.metrics.action_unknown += 1
            outcome = UNKNOWN
        return ClientResult(outcome, data=result.data, reason=code,
                            latency_ms=result.latency_ms, attempts=result.attempts)


# --------------------------------------------------------------------------
# rendering (bounded, and off by default)
# --------------------------------------------------------------------------


def render_grounded_context(client: WorldBodyClient, *, max_objects: int = 8) -> str:
    """A short bounded line for the model's *user* message.  Empty when unsure.

    Ticket sections 13/14/26/27/37/38: finite observation, bounded body signals,
    no raw values, no full world JSON, and nothing at all when the runtime is not
    ready.  Never falls back to anything.

    **Context projection (M13B-R1).**  ``world_revision`` is reconciliation /
    concurrency metadata, not something Chiyo perceives with her body or her
    eyes.  The existing Perception contract is explicit that global metadata
    used for reconciliation must not be auto-injected into cognition, because a
    global version counter leaks activity that happened outside her view.  So it
    stays in the service, the client, the ledger and the logs — and never reaches
    the model.  Same reason ``world_id`` is dropped: she does not experience the
    name of her own world, she experiences being somewhere.
    """

    if not client.enabled:
        return ""
    status = client.get_status()
    if status.outcome != OK:
        return ""
    # Ticket section 26: canonical mode requires the runtime to be READY, not
    # merely reachable.  A NOT_READY runtime yields no capsule at all --
    # injecting a stale or half-initialised view would be a fake world.
    if not (status.data or {}).get("ready"):
        return ""
    snapshot = client.get_snapshot()
    if snapshot.outcome != OK or not isinstance(snapshot.data, dict):
        return ""
    signals = client.get_body_signals()

    snap = snapshot.data
    location = snap.get("location") or {}
    place = location.get("place_id")
    area = location.get("area_id")
    where = "/".join(str(x) for x in (place, area) if x) or "unknown"
    parts = ["[world] location=%s | pose=%s" % (where, snap.get("pose"))]
    objects = (snap.get("visible_objects") or [])[:max_objects]
    if objects:
        parts.append("visible: " + ", ".join(
            str(o.get("label") or o.get("object_id")) for o in objects
        ))
    hands = snap.get("hands") or {}
    occupied = sorted(slot for slot, held in hands.items() if held)
    parts.append("hands=%s" % (",".join(occupied) if occupied else "free"))

    if signals.outcome == OK and isinstance(signals.data, dict):
        bands = signals.data.get("signals") or []
        if bands:
            parts.append("body: " + ", ".join(
                str(s.get("signal_type") or s.get("type")) for s in bands[:6]
            ))
        recovery = (signals.data.get("recovery") or {}).get("state")
        if recovery and recovery != "inactive":
            parts.append("recovery=%s" % recovery)
    return " | ".join(parts)
