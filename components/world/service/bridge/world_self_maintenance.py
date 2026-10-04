#!/usr/bin/env python3
"""M15C: the body's self-maintenance policy (event state machine).

    RECOVERY_NORMAL -> RECOVERY_INTERRUPTED -> RECOVERY_WORSENING
                                            -> RECOVERY_STABILIZED
                                            -> RECOVERY_COMPLETE

Each transition has an entry condition, an exit condition, a minimum dwell, a
cooldown and a repeat suppression, so the body cannot oscillate
standing -> lying -> standing -> lying.

The policy only *classifies* the body's own state and says whether the
Decision is allowed to consider a self-maintenance action.  It never selects a
verb and never touches the World.
"""

from __future__ import annotations

import fcntl
import json
import os
import pathlib
import time
from typing import Any, Mapping, Optional

SCHEMA = "world.self_maintenance.state.v1"

# --- event states -----------------------------------------------------------
RECOVERY_NORMAL = "RECOVERY_NORMAL"
RECOVERY_INTERRUPTED = "RECOVERY_INTERRUPTED"
RECOVERY_WORSENING = "RECOVERY_WORSENING"
RECOVERY_STABILIZED = "RECOVERY_STABILIZED"
RECOVERY_COMPLETE = "RECOVERY_COMPLETE"
EVENTS = (RECOVERY_NORMAL, RECOVERY_INTERRUPTED, RECOVERY_WORSENING,
          RECOVERY_STABILIZED, RECOVERY_COMPLETE)

# --- data-derived timings (see M15C_DATA.json) ------------------------------
# each is a *lower bound* on patience; nothing here makes her act sooner
MIN_DWELL_SECONDS = 300.0      # 3x the median committed-action gap (105s)
EVENT_DEDUPE_SECONDS = 300.0   # same event not re-emitted inside this window
DECISION_COOLDOWN_SECONDS = 120.0
ACTION_COOLDOWN_SECONDS = 600.0  # ~6x the median action gap

#: severity ladder, derived from the fatigue bands the runtime itself publishes
SEVERITY_NONE = 0
SEVERITY_DWELL = 1     # interrupted long enough to matter
SEVERITY_NOTICEABLE = 2  # fatigue_noticeable latched (band 0.45-0.55+)
SEVERITY_HIGH = 3      # fatigue_high latched (band 0.70-0.80)

DEFAULT_STATE_ENV = "WORLD_SELF_MAINTENANCE_STATE"


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
                return run / "self_maintenance_state.json"
    return pathlib.Path("./data/world/run/self_maintenance_state.json")


#: operational overrides (a revert is a config change, never a code change)
ENV_MIN_DWELL = "WORLD_SELF_MAINTENANCE_MIN_DWELL"
ENV_DEDUPE = "WORLD_SELF_MAINTENANCE_EVENT_DEDUPE"
ENV_DECISION_COOLDOWN = "WORLD_SELF_MAINTENANCE_DECISION_COOLDOWN"
ENV_ACTION_COOLDOWN = "WORLD_SELF_MAINTENANCE_ACTION_COOLDOWN"


def _number(env: Mapping[str, str], key: str, fallback: float) -> float:
    try:
        return float(str(env.get(key)).strip())
    except (TypeError, ValueError):
        return fallback


def policy_from_environment(environment: Optional[Mapping[str, str]] = None
                            ) -> "SelfMaintenancePolicy":
    """Build the policy with data-derived defaults plus env overrides."""

    env = os.environ if environment is None else environment
    return SelfMaintenancePolicy(
        path=default_path(env),
        min_dwell=_number(env, ENV_MIN_DWELL, MIN_DWELL_SECONDS),
        dedupe=_number(env, ENV_DEDUPE, EVENT_DEDUPE_SECONDS),
        decision_cooldown=_number(env, ENV_DECISION_COOLDOWN,
                                  DECISION_COOLDOWN_SECONDS),
        action_cooldown=_number(env, ENV_ACTION_COOLDOWN,
                                ACTION_COOLDOWN_SECONDS),
    )


class SelfMaintenancePolicy:
    """Classifies the body's maintenance state and gates the Decision."""

    def __init__(self, path: Optional[pathlib.Path | str] = None,
                 *, min_dwell: float = MIN_DWELL_SECONDS,
                 dedupe: float = EVENT_DEDUPE_SECONDS,
                 decision_cooldown: float = DECISION_COOLDOWN_SECONDS,
                 action_cooldown: float = ACTION_COOLDOWN_SECONDS) -> None:
        self.path = pathlib.Path(path) if path else default_path()
        self.min_dwell = float(min_dwell)
        self.dedupe = float(dedupe)
        self.decision_cooldown = float(decision_cooldown)
        self.action_cooldown = float(action_cooldown)

    # ---- tiny persistent store (atomic replace, never half-written) --------
    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {}
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except ValueError:
            return {}

    def _save(self, state: Mapping[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(dict(state), sort_keys=True), encoding="utf-8")
        os.replace(tmp, self.path)

    # ---- classification ---------------------------------------------------
    @staticmethod
    def _severity(recovery: Mapping[str, Any], latch: Mapping[str, Any],
                  dwell: float, min_dwell: float) -> int:
        if latch.get("fatigue_high"):
            return SEVERITY_HIGH
        if latch.get("fatigue_noticeable"):
            return SEVERITY_NOTICEABLE
        if dwell >= min_dwell:
            return SEVERITY_DWELL
        return SEVERITY_NONE

    def classify(self, signals: Mapping[str, Any], *, now: Optional[float] = None
                 ) -> dict[str, Any]:
        """Return the event, severity and whether the Decision may consider an
        action.  Pure w.r.t. the store (call ``observe`` to persist)."""

        now_ts = float(now if now is not None else time.time())
        recovery = dict((signals or {}).get("recovery") or {})
        latch = dict((signals or {}).get("latch") or {})
        state = str(recovery.get("state") or "")
        trend = str(recovery.get("trend") or "").strip().lower()

        store = self._load()
        episode_key = "recovery-interrupted"
        first_seen = store.get("episode_first_seen")

        if state == "completed":
            event = RECOVERY_COMPLETE
        elif state == "interrupted":
            dwell = (now_ts - float(first_seen)) if first_seen else 0.0
            event = (RECOVERY_WORSENING if trend == "worsening"
                     else RECOVERY_STABILIZED)
            severity = self._severity(recovery, latch, dwell, self.min_dwell)
            return self._decision(event, severity, dwell, store, now_ts,
                                  episode_key, recovery, latch)
        elif state == "active":
            event = RECOVERY_NORMAL
        else:
            event = RECOVERY_NORMAL
        return {"event": event, "severity": SEVERITY_NONE, "dwell_seconds": 0.0,
                "action_allowed": False, "reason": "no maintenance event",
                "episode": None}

    def _decision(self, event, severity, dwell, store, now_ts, episode_key,
                  recovery, latch) -> dict[str, Any]:
        last_action = float(store.get("last_action_at") or 0.0)
        last_decision = float(store.get("last_decision_at") or 0.0)
        last_event = store.get("last_event")
        last_event_at = float(store.get("last_event_at") or 0.0)

        if severity < SEVERITY_DWELL:
            return {"event": event, "severity": severity, "dwell_seconds": dwell,
                    "action_allowed": False,
                    "reason": "below minimum dwell (%.0fs < %.0fs)"
                              % (dwell, self.min_dwell),
                    "episode": episode_key}
        if event == RECOVERY_STABILIZED and severity < SEVERITY_NOTICEABLE:
            # a stabilized (non-worsening) interrupted rest only justifies an
            # action when the body's own fatigue band agrees
            return {"event": event, "severity": severity, "dwell_seconds": dwell,
                    "action_allowed": False,
                    "reason": "stabilized needs a fatigue band",
                    "episode": episode_key}
        since_action = now_ts - last_action
        if last_action and since_action < self.action_cooldown:
            return {"event": event, "severity": severity, "dwell_seconds": dwell,
                    "action_allowed": False,
                    "reason": "action cooldown (%.0fs < %.0fs)"
                              % (since_action, self.action_cooldown),
                    "episode": episode_key}
        since_decision = now_ts - last_decision
        if last_decision and since_decision < self.decision_cooldown:
            return {"event": event, "severity": severity, "dwell_seconds": dwell,
                    "action_allowed": False,
                    "reason": "decision cooldown (%.0fs < %.0fs)"
                              % (since_decision, self.decision_cooldown),
                    "episode": episode_key}
        if last_event == event and last_event_at and \
                (now_ts - last_event_at) < self.dedupe:
            return {"event": event, "severity": severity, "dwell_seconds": dwell,
                    "action_allowed": False,
                    "reason": "event dedupe (%s inside %.0fs)"
                              % (event, self.dedupe),
                    "episode": episode_key}
        return {"event": event, "severity": severity, "dwell_seconds": dwell,
                "action_allowed": True,
                "reason": "maintenance event at severity %d after %.0fs"
                          % (severity, dwell),
                "episode": episode_key}

    # ---- persistence of observation / decisions ---------------------------
    def observe(self, signals: Mapping[str, Any], *, now: Optional[float] = None
                ) -> dict[str, Any]:
        """Classify AND persist the episode bookkeeping (dedupe/cooldowns)."""

        now_ts = float(now if now is not None else time.time())
        verdict = self.classify(signals, now=now_ts)
        store = self._load()
        recovery = dict((signals or {}).get("recovery") or {})
        if str(recovery.get("state") or "") == "interrupted":
            store.setdefault("episode_first_seen", now_ts)
        else:
            store.pop("episode_first_seen", None)
        if verdict.get("action_allowed"):
            store["last_event"] = verdict["event"]
            store["last_event_at"] = now_ts
            store["last_decision_at"] = now_ts
        store["last_seen_at"] = now_ts
        self._save(store)
        return verdict

    def note_action(self, *, now: Optional[float] = None) -> None:
        """Record that a self-maintenance action was actually committed."""

        now_ts = float(now if now is not None else time.time())
        store = self._load()
        store["last_action_at"] = now_ts
        store.pop("episode_first_seen", None)   # the episode was served
        self._save(store)
