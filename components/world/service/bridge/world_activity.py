#!/usr/bin/env python3
"""M15D: world_activity.py — Natural Activity V0.

The body's daily activity as an explicit state machine.  An *Activity* is not
a World pose: ``activity = REST`` and ``pose = lying`` are facts owned by
different authorities ("activity ≠ world pose").  This module owns only the
activity bookkeeping and the time-driven events it produces.  It never writes
the World, never selects a verb, and never calls the Resolver.

Anti-random firewall (M15D.4): there is no ``random`` import and no choice
function here.  Every event carries its trigger, the state evidence it was
derived from, and the timestamp; anything that cannot be justified produces
``NO_EVENT`` and the Decision then produces NO_ACTION.
"""

from __future__ import annotations

import json
import os
import pathlib
import time
from typing import Any, Mapping, Optional

SCHEMA = "world.activity.v1"

# --- activity types (first batch: safe, no external side effects) -----------
IDLE = "IDLE"
REST = "REST"
STAND = "STAND"
SIT = "SIT"
LIE = "LIE"
MOVE_WITHIN_HOME = "MOVE_WITHIN_HOME"
ALLOWED_TYPES = (IDLE, REST, STAND, SIT, LIE, MOVE_WITHIN_HOME)

#: explicitly NOT in V0 (recorded so nobody "helpfully" adds them)
FORBIDDEN_TYPES = ("GO_OUTSIDE", "SHOPPING", "CONTACT_USER", "FILE_EDIT",
                   "NETWORK", "NPC_SOCIAL")

# --- activity states --------------------------------------------------------
PLANNED = "PLANNED"
ACTIVE = "ACTIVE"
INTERRUPTED = "INTERRUPTED"
COMPLETED = "COMPLETED"
ABANDONED = "ABANDONED"
STATES = (PLANNED, ACTIVE, INTERRUPTED, COMPLETED, ABANDONED)
TERMINAL = (COMPLETED, ABANDONED)

# --- tick event kinds (the ONLY things a tick may propose) ------------------
TIME_ELAPSED = "TIME_ELAPSED"
ACTIVITY_EXPIRED = "ACTIVITY_EXPIRED"
IDLE_TOO_LONG = "IDLE_TOO_LONG"
RECOVERY_CHECK = "RECOVERY_CHECK"
NO_EVENT = "NO_EVENT"
TICK_EVENTS = (TIME_ELAPSED, ACTIVITY_EXPIRED, IDLE_TOO_LONG, RECOVERY_CHECK,
               NO_EVENT)

#: data-derived: the median committed-action gap is 105s (M15C_DATA.json), so
#: "idle too long" wants a much longer window than that before it speaks.
IDLE_TOO_LONG_SECONDS = 3600.0
DEFAULT_STATE_ENV = "WORLD_ACTIVITY_STATE"


def default_path(environment: Optional[Mapping[str, str]] = None) -> pathlib.Path:
    env = os.environ if environment is None else environment
    explicit = env.get(DEFAULT_STATE_ENV)
    if explicit:
        return pathlib.Path(str(explicit))
    for key in ("CHIYO_WORLD_HOME", "HERMES_HOME"):
        base = env.get(key)
        if base:
            run = pathlib.Path(str(base)) / "run"
            if run.is_dir():
                return run / "activity_state.json"
    return pathlib.Path("./data/world/run/activity_state.json")


class ActivityError(ValueError):
    """Raised on an illegal activity transition or bad input."""


def _now(now: Optional[float] = None) -> float:
    return float(now if now is not None else time.time())


class ActivityStore:
    """The activity state machine + the time-driven tick."""

    def __init__(self, path: Optional[pathlib.Path | str] = None, *,
                 idle_too_long: float = IDLE_TOO_LONG_SECONDS) -> None:
        self.path = pathlib.Path(path) if path else default_path()
        self.idle_too_long = float(idle_too_long)

    # ---- storage (atomic replace) -----------------------------------------
    def load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"activity": None, "history": []}
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except ValueError:
            return {"activity": None, "history": []}

    def save(self, state: Mapping[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(dict(state), sort_keys=True), encoding="utf-8")
        os.replace(tmp, self.path)

    # ---- transitions -------------------------------------------------------
    def plan(self, *, activity_id: str, activity_type: str, now=None,
             expected_duration: float = 0.0, interruptibility: str = "normal",
             origin: str = "life_tick", related_decision_id: str = "") -> dict:
        if activity_type not in ALLOWED_TYPES:
            raise ActivityError("activity type %r is not in the V0 allow-list"
                                % activity_type)
        state = self.load()
        current = state.get("activity") or {}
        if current.get("status") == ACTIVE:
            raise ActivityError("activity %s is still ACTIVE"
                                % current.get("activity_id"))
        record = {
            "activity_id": str(activity_id), "activity_type": activity_type,
            "started_at": _now(now), "expected_duration": float(expected_duration),
            "interruptibility": str(interruptibility), "origin": str(origin),
            "related_decision_id": str(related_decision_id), "status": PLANNED,
        }
        state["activity"] = record
        state.setdefault("history", []).append({"activity_id": record["activity_id"],
                                                "status": PLANNED,
                                                "at": record["started_at"]})
        self.save(state)
        return record

    def act(self, *, now=None) -> dict:
        return self._transition(ACTIVE, now=now)

    def interrupt(self, *, reason: str = "", now=None) -> dict:
        return self._transition(INTERRUPTED, now=now, note=reason)

    def complete(self, *, now=None) -> dict:
        return self._transition(COMPLETED, now=now)

    def abandon(self, *, reason: str = "", now=None) -> dict:
        return self._transition(ABANDONED, now=now, note=reason)

    def _transition(self, status: str, *, now=None, note: str = "") -> dict:
        state = self.load()
        record = state.get("activity")
        if not record:
            raise ActivityError("no activity to transition")
        current = record.get("status")
        if current in TERMINAL:
            raise ActivityError("activity %s is already %s"
                                % (record.get("activity_id"), current))
        allowed = {PLANNED: (ACTIVE, ABANDONED),
                   ACTIVE: (INTERRUPTED, COMPLETED, ABANDONED),
                   INTERRUPTED: (ACTIVE, COMPLETED, ABANDONED)}
        if status not in allowed.get(current, ()):
            raise ActivityError("illegal activity transition %s -> %s"
                                % (current, status))
        record["status"] = status
        record["last_transition_at"] = _now(now)
        if note:
            record["note"] = note
        state.setdefault("history", []).append({"activity_id": record.get("activity_id"),
                                                "status": status,
                                                "at": record["last_transition_at"]})
        self.save(state)
        return record

    # ---- the safe tick (events only) ---------------------------------------
    def tick(self, *, now=None, recovery: Optional[Mapping[str, Any]] = None
             ) -> dict[str, Any]:
        """Produce at most ONE time-driven event.  Never mutates the World.

        Priority: an expired activity first, then a long idle window, then a
        recovery check.  Everything carries its trigger and the state evidence
        it came from; nothing is invented.
        """

        now_ts = _now(now)
        state = self.load()
        record = state.get("activity") or {}
        evidence: dict[str, Any] = {"activity_id": record.get("activity_id"),
                                   "activity_status": record.get("status")}
        last_tick = state.get("last_tick_at")
        if record.get("status") == ACTIVE and record.get("expected_duration"):
            elapsed = now_ts - float(record.get("started_at") or now_ts)
            if elapsed >= float(record["expected_duration"]):
                evidence.update({"elapsed": round(elapsed, 3),
                                 "expected_duration": record["expected_duration"]})
                state["last_tick_at"] = now_ts
                state["last_event"] = ACTIVITY_EXPIRED
                self.save(state)
                return {"event": ACTIVITY_EXPIRED, "trigger": "expected_duration",
                        "evidence": evidence, "candidate_hint": STAND}

        since_tick = (now_ts - float(last_tick)) if last_tick else None
        if since_tick is not None and since_tick >= self.idle_too_long \
                and record.get("status") in (None, IDLE, REST, COMPLETED, ABANDONED):
            evidence.update({"idle_seconds": round(since_tick, 3)})
            state["last_tick_at"] = now_ts
            state["last_event"] = IDLE_TOO_LONG
            self.save(state)
            return {"event": IDLE_TOO_LONG, "trigger": "idle_too_long",
                    "evidence": evidence, "candidate_hint": None}

        if recovery is not None:
            state_ = str((recovery or {}).get("state") or "")
            if state_ in ("interrupted", "completed"):
                evidence.update({"recovery_state": state_})
                state["last_tick_at"] = now_ts
                state["last_event"] = RECOVERY_CHECK
                self.save(state)
                return {"event": RECOVERY_CHECK, "trigger": "recovery_state",
                        "evidence": evidence, "candidate_hint": None}

        if since_tick is None:
            state["last_tick_at"] = now_ts
            self.save(state)
        return {"event": NO_EVENT, "trigger": "nothing_due", "evidence": evidence,
                "candidate_hint": None}


def activity_from_environment(environment: Optional[Mapping[str, str]] = None
                              ) -> "ActivityStore":
    env = os.environ if environment is None else environment
    try:
        idle = float(str(env.get("WORLD_ACTIVITY_IDLE_SECONDS")).strip())
    except (TypeError, ValueError):
        idle = IDLE_TOO_LONG_SECONDS
    return ActivityStore(path=default_path(env), idle_too_long=idle)
