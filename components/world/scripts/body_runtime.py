#!/usr/bin/env python3
"""Minimal Body Runtime (M8) — the Embodiment owner of somatic state.

Scope
-----
This module is the single candidate **Embodiment** owner for:

* ``BodyProfile``        stable embodiment identity and somatic contracts
* ``BodyRuntimeState``   the 0..1 somatic variables
* ``BodyRecoveryState``  a first-class recovery state machine
* ``BodyTransition``     one record per real state change
* deterministic ``advance()`` with event-driven time and bounded catch-up
* persistence, restart, replay and a migration harness

It deliberately does **not** own ``position`` / ``pose`` / ``orientation`` /
``placement`` / hand occupancy — those stay with the World substrate
(``world_body_substrate``).  There is no copy of them here.

Firewalls (hard)
----------------
* No LLM, no network, no ``random``, no ``datetime.now()`` in any computation.
  Every instant is injected by the caller.
* No import of memory / recall / affect / emotion / desire / relationship /
  self / expression / telegram / attention.
* Inputs are limited to elapsed time, a World environment snapshot, an actual
  successful physical result, and an explicit recovery mode.  An activity label
  is **not** a body fact and is rejected.
* No Body -> Affect / Desire / Memory / Relationship / Expression write exists.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
import tempfile
from collections.abc import Mapping, Sequence
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator, Optional

SCHEMA_PROFILE = "body.profile.v1"
SCHEMA_RUNTIME = "body.runtime.v1"
SCHEMA_TRANSITION = "body.transition.v1"
RUNTIME_VERSION = "embodiment.runtime.v1"
SUPPORTED_RUNTIME_SCHEMAS = (SCHEMA_RUNTIME,)
SUPPORTED_PROFILE_SCHEMAS = (SCHEMA_PROFILE,)

KIND_PROFILE = "body_profile"
KIND_RUNTIME = "body_runtime_state"
KIND_TRANSITION = "body_transition"

BODY_ID = "chiyo_body"
MODE = "virtual_topological"
FEATURE_ENV = "CHIYO_BODY_RUNTIME_ENABLED"

DATA_RELATIVE = ("data", "body")
PROFILE_NAME = "profile.json"
RUNTIME_NAME = "runtime.json"
TRANSITIONS_NAME = "transitions.jsonl"

MAX_TRANSITIONS = 4096
MAX_INPUTS = 64
MAX_HOURS_PER_ADVANCE = 24.0 * 14
CHANGE_EPSILON = 1e-9

# --------------------------------------------------------------------------
# somatic variable contracts  (ticket section 6)
# --------------------------------------------------------------------------
#
# Every variable answers, in one place: domain, default, owner, persistence,
# legal inputs, advance rule, recovery rule, bounds, signal thresholds and
# migration semantics.  A variable that cannot answer these does not belong.
#
# All domains are 0.0..1.0, clamped, finite and non-NaN.  HeartMind's 0..100
# scale is handled by the compatibility adapter, never here.

SOMATIC_VARIABLES = (
    "fatigue",
    "activation",
    "comfort",
    "sensory_load",
    "tension",
    "warmth",
)

SOMATIC_CONTRACTS: dict[str, dict[str, Any]] = {
    "fatigue": {
        "domain": (0.0, 1.0),
        "default": 0.15,
        "owner": "embodiment",
        "persistence": "runtime.json",
        "legal_inputs": ("PHYSICAL_RESULT.load", "RECOVERY.effect", "elapsed time"),
        "advance_rule": (
            "rises by load_units * FATIGUE_PER_LOAD_HOUR, then decays toward "
            "FATIGUE_BASELINE at the current decay rate"
        ),
        "recovery_rule": "recovery active multiplies the decay rate",
        "bounds": "clamped to [0,1]",
        "signal_thresholds": {"fatigue_noticeable": (0.45, 0.55), "fatigue_high": (0.70, 0.80)},
        "migration": "v1 value carried through unchanged",
    },
    "activation": {
        "domain": (0.0, 1.0),
        "default": 0.50,
        "owner": "embodiment",
        "persistence": "runtime.json",
        "legal_inputs": ("PHYSICAL_RESULT.load", "elapsed time"),
        "advance_rule": "rises with load, then decays toward ACTIVATION_BASELINE",
        "recovery_rule": "recovery active pulls activation toward its resting floor",
        "bounds": "clamped to [0,1]",
        "signal_thresholds": {
            "activation_low": (0.45, 0.35),
            "activation_recovering": None,
        },
        "migration": "v1 value carried through unchanged",
    },
    "comfort": {
        "domain": (0.0, 1.0),
        "default": 0.70,
        "owner": "embodiment",
        "persistence": "runtime.json",
        "legal_inputs": (
            "PHYSICAL_RESULT.posture_seconds",
            "PHYSICAL_RESULT.pose_strain",
            "ENVIRONMENT.temperature_tendency",
            "elapsed time",
        ),
        "advance_rule": "falls with sustained posture strain, otherwise returns to baseline",
        "recovery_rule": "recovery active accelerates the return to baseline",
        "bounds": "clamped to [0,1]",
        "signal_thresholds": {
            "discomfort_mild": (0.62, 0.55),
            "discomfort_noticeable": (0.45, 0.35),
        },
        "migration": "v1 value carried through unchanged",
    },
    "sensory_load": {
        "domain": (0.0, 1.0),
        "default": 0.10,
        "owner": "embodiment",
        "persistence": "runtime.json",
        "legal_inputs": ("ENVIRONMENT.sensory_intensity", "elapsed time"),
        "advance_rule": "moves toward the World sensory intensity, then decays to baseline",
        "recovery_rule": "recovery active accelerates the decay",
        "bounds": "clamped to [0,1]",
        "signal_thresholds": {"sensory_load_elevated": (0.50, 0.60)},
        "migration": "v1 value carried through unchanged",
    },
    "tension": {
        "domain": (0.0, 1.0),
        "default": 0.15,
        "owner": "embodiment",
        "persistence": "runtime.json",
        "legal_inputs": ("PHYSICAL_RESULT.load", "PHYSICAL_RESULT.posture_seconds", "elapsed time"),
        "advance_rule": "rises with load and posture strain, then decays toward baseline",
        "recovery_rule": "recovery active accelerates the decay",
        "bounds": "clamped to [0,1]",
        "signal_thresholds": {
            "tension_elevated": (0.50, 0.60),
            "tension_releasing": None,
        },
        "migration": "v1 value carried through unchanged",
    },
    "warmth": {
        "domain": (0.0, 1.0),
        "default": 0.50,
        "owner": "embodiment",
        "persistence": "runtime.json",
        "legal_inputs": ("ENVIRONMENT.temperature_tendency", "elapsed time"),
        "advance_rule": "approaches a virtual warmth target derived from the environment tendency",
        "recovery_rule": "unaffected by recovery",
        "bounds": "clamped to [0,1]",
        "semantics": (
            "virtual body warmth/cold tendency — NOT a server temperature and NOT a "
            "medical body temperature; no degree-Celsius representation exists"
        ),
        "signal_thresholds": {"warmth_high": (0.68, 0.75), "cold_tendency": (0.32, 0.25)},
        "migration": "v1 value carried through unchanged",
    },
}

# Rates are per hour.  They are constants of the runtime version.
FATIGUE_BASELINE = SOMATIC_CONTRACTS["fatigue"]["default"]
FATIGUE_PER_LOAD_HOUR = 0.35
FATIGUE_DECAY_PER_HOUR = 0.90
FATIGUE_RECOVERY_MULTIPLIER = 3.0

ACTIVATION_BASELINE = SOMATIC_CONTRACTS["activation"]["default"]
ACTIVATION_PER_LOAD_HOUR = 0.40
ACTIVATION_DECAY_PER_HOUR = 1.20
ACTIVATION_RECOVERY_TARGET = 0.30

COMFORT_BASELINE = SOMATIC_CONTRACTS["comfort"]["default"]
COMFORT_POSTURE_LOAD_PER_HOUR = 0.45
COMFORT_DECAY_PER_HOUR = 0.70
COMFORT_RECOVERY_MULTIPLIER = 2.5

SENSORY_BASELINE = SOMATIC_CONTRACTS["sensory_load"]["default"]
SENSORY_RISE_PER_HOUR = 1.60
SENSORY_DECAY_PER_HOUR = 1.10
SENSORY_RECOVERY_MULTIPLIER = 2.0

TENSION_BASELINE = SOMATIC_CONTRACTS["tension"]["default"]
TENSION_PER_LOAD_HOUR = 0.30
TENSION_POSTURE_PER_HOUR = 0.25
TENSION_DECAY_PER_HOUR = 0.80
TENSION_RECOVERY_MULTIPLIER = 2.5

WARMTH_BASELINE = SOMATIC_CONTRACTS["warmth"]["default"]
WARMTH_APPROACH_PER_HOUR = 0.90

# Recovery
RECOVERY_STATES = ("inactive", "active", "interrupted", "completed")
RECOVERY_MODES = ("rest", "sleep_like")
RECOVERY_TRENDS = ("improving", "stalled", "worsening")
#: fatigue must be at least this high before a recovery session may be opened
RECOVERY_OPEN_FATIGUE = 0.30

CAUSE_TIME_ADVANCE = "TIME_ADVANCE"
CAUSE_RECOVERY = "RECOVERY"
CAUSE_ENVIRONMENT = "ENVIRONMENT_INPUT"
CAUSE_PHYSICAL = "PHYSICAL_RESULT"
CAUSE_MIGRATION = "MIGRATION"
CAUSE_RECONCILIATION = "RECONCILIATION"
CAUSE_CLASSES = (
    CAUSE_TIME_ADVANCE,
    CAUSE_RECOVERY,
    CAUSE_ENVIRONMENT,
    CAUSE_PHYSICAL,
    CAUSE_MIGRATION,
    CAUSE_RECONCILIATION,
)

INPUT_CLASS_ENVIRONMENT = "ENVIRONMENT"
INPUT_CLASS_PHYSICAL = "PHYSICAL_RESULT"
INPUT_CLASS_RECOVERY = "RECOVERY"
INPUT_CLASSES = (INPUT_CLASS_ENVIRONMENT, INPUT_CLASS_PHYSICAL, INPUT_CLASS_RECOVERY)

EFFORT_CLASSES = {"light": 0.5, "moderate": 1.0, "heavy": 1.8}
POSE_STRAIN = {
    "standing": 0.25,
    "seated": 0.45,
    "lying": 0.10,
    "transitioning": 0.30,
}

_RUNTIME_KEYS = frozenset(
    {
        "schema_version",
        "kind",
        "runtime_version",
        "body_id",
        "version",
        "valid_from",
        "last_advanced_at",
        "somatic",
        "recovery",
        "signal_latch",
    }
)
_PROFILE_KEYS = frozenset(
    {
        "schema_version",
        "kind",
        "embodiment_version",
        "body_id",
        "mode",
        "runtime_profile",
        "somatic_config",
        "signal_profile",
        "sensor_profile_ref",
        "actuator_profile_ref",
    }
)
_RECOVERY_KEYS = frozenset(
    {"state", "started_at", "last_advanced_at", "mode", "progress", "trend"}
)


class BodyRuntimeError(ValueError):
    """The body runtime contract was violated."""


class BodySchemaError(BodyRuntimeError):
    """An unsupported schema was supplied; the runtime fails closed."""


# --------------------------------------------------------------------------
# primitives
# --------------------------------------------------------------------------


def _clamp(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise BodyRuntimeError(f"{label} must be a number")
    number = float(value)
    if math.isnan(number) or math.isinf(number):
        raise BodyRuntimeError(f"{label} must be finite")
    return min(1.0, max(0.0, number))


def _bounded_number(value: Any, label: str, *, low: float, high: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise BodyRuntimeError(f"{label} must be a number")
    number = float(value)
    if math.isnan(number) or math.isinf(number):
        raise BodyRuntimeError(f"{label} must be finite")
    return min(high, max(low, number))


def _strict_number(value: Any, label: str, *, low: float, high: float) -> float:
    """Validate an *input*.  Out-of-domain inputs are rejected, not clamped.

    State values are clamped defensively, but an input that violates its
    declared domain is a caller contract violation and must fail closed --
    silently clamping it would make the body wrong without saying so.
    """

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise BodyRuntimeError(f"{label} must be a number")
    number = float(value)
    if math.isnan(number) or math.isinf(number):
        raise BodyRuntimeError(f"{label} must be finite")
    if number < low or number > high:
        raise BodyRuntimeError(f"{label} must be within [{low}, {high}]")
    return number


def _text(value: Any, label: str, *, maximum: int = 128) -> str:
    if not isinstance(value, str):
        raise BodyRuntimeError(f"{label} must be a string")
    text = value.strip()
    if not text or len(text) > maximum:
        raise BodyRuntimeError(f"{label} must be non-empty and bounded")
    return text


def _instant(value: Any, label: str) -> datetime:
    if isinstance(value, datetime):
        moment = value
    elif isinstance(value, str):
        try:
            moment = datetime.fromisoformat(value)
        except ValueError as exc:
            raise BodyRuntimeError(f"{label} must be an ISO-8601 instant") from exc
    else:
        raise BodyRuntimeError(f"{label} must be a datetime or ISO-8601 string")
    if moment.tzinfo is None:
        raise BodyRuntimeError(f"{label} must be timezone-aware")
    return moment.astimezone(timezone.utc)


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat()


def _digest(value: Any) -> str:
    payload = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _detach(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False))


def _approach(value: float, target: float, rate_per_hour: float, hours: float) -> float:
    """Deterministic exponential approach.  Pure; no clock, no randomness."""

    if hours <= 0.0 or rate_per_hour <= 0.0:
        return value
    factor = 1.0 - math.exp(-rate_per_hour * hours)
    return value + (target - value) * factor


def feature_enabled(environment: Optional[Mapping[str, str]] = None) -> bool:
    raw = (environment or os.environ).get(FEATURE_ENV)
    return str(raw).strip().lower() in {"1", "true", "yes", "on"} if raw else False


def body_root(hermes_home: Optional[Path | str] = None) -> Path:
    home = (
        Path(hermes_home).expanduser()
        if hermes_home is not None
        else Path(os.environ.get("HERMES_HOME") or (Path.home() / ".hermes"))
    )
    return home.joinpath(*DATA_RELATIVE)


# --------------------------------------------------------------------------
# BodyProfile  (ticket section 4)
# --------------------------------------------------------------------------


def build_profile(
    *,
    body_id: str = BODY_ID,
    sensor_profile_ref: Optional[str] = None,
    actuator_profile_ref: Optional[str] = None,
) -> dict[str, Any]:
    """Deterministic BodyProfile.  No time, no randomness."""

    return {
        "schema_version": SCHEMA_PROFILE,
        "kind": KIND_PROFILE,
        "embodiment_version": RUNTIME_VERSION,
        "body_id": _text(body_id, "body_id"),
        "mode": MODE,
        "runtime_profile": {
            "schema_version": SCHEMA_RUNTIME,
            "runtime_version": RUNTIME_VERSION,
            "max_hours_per_advance": MAX_HOURS_PER_ADVANCE,
            "change_epsilon": CHANGE_EPSILON,
        },
        "somatic_config": {
            "domain": [0.0, 1.0],
            "variables": {
                name: {
                    "default": SOMATIC_CONTRACTS[name]["default"],
                    "legal_inputs": list(SOMATIC_CONTRACTS[name]["legal_inputs"]),
                    "advance_rule": SOMATIC_CONTRACTS[name]["advance_rule"],
                    "recovery_rule": SOMATIC_CONTRACTS[name]["recovery_rule"],
                }
                for name in SOMATIC_VARIABLES
            },
            "recovery_states": list(RECOVERY_STATES),
            "recovery_modes": list(RECOVERY_MODES),
        },
        "signal_profile": {
            "producer": "body_signal",
            "carries_raw_values": False,
            "hysteresis": True,
        },
        "sensor_profile_ref": sensor_profile_ref,
        "actuator_profile_ref": actuator_profile_ref,
    }


def validate_profile(profile: Any) -> dict[str, Any]:
    if not isinstance(profile, Mapping) or set(profile) != _PROFILE_KEYS:
        raise BodyRuntimeError("profile must have exactly the v1 fields")
    if profile["schema_version"] not in SUPPORTED_PROFILE_SCHEMAS:
        raise BodySchemaError(
            f"profile schema {profile['schema_version']!r} is not supported"
        )
    if profile["kind"] != KIND_PROFILE:
        raise BodyRuntimeError("profile kind is not supported")
    result = _detach(dict(profile))
    result["body_id"] = _text(result["body_id"], "body_id")
    result["mode"] = _text(result["mode"], "mode", maximum=64)
    result["embodiment_version"] = _text(
        result["embodiment_version"], "embodiment_version", maximum=64
    )
    if not isinstance(result["runtime_profile"], dict):
        raise BodyRuntimeError("runtime_profile must be an object")
    if not isinstance(result["somatic_config"], dict):
        raise BodyRuntimeError("somatic_config must be an object")
    return result


# --------------------------------------------------------------------------
# Recovery  (ticket section 18)
# --------------------------------------------------------------------------


def _idle_recovery() -> dict[str, Any]:
    return {
        "state": "inactive",
        "started_at": None,
        "last_advanced_at": None,
        "mode": None,
        "progress": 0.0,
        "trend": "stalled",
    }


def validate_recovery(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != _RECOVERY_KEYS:
        raise BodyRuntimeError("recovery must have exactly the v1 fields")
    state = _text(value["state"], "recovery.state", maximum=32)
    if state not in RECOVERY_STATES:
        raise BodyRuntimeError(f"recovery.state must be one of {list(RECOVERY_STATES)}")
    trend = _text(value["trend"], "recovery.trend", maximum=32)
    if trend not in RECOVERY_TRENDS:
        raise BodyRuntimeError(f"recovery.trend must be one of {list(RECOVERY_TRENDS)}")
    mode = value["mode"]
    if mode is not None:
        mode = _text(mode, "recovery.mode", maximum=32)
        if mode not in RECOVERY_MODES:
            raise BodyRuntimeError(f"recovery.mode must be one of {list(RECOVERY_MODES)}")
    started = value["started_at"]
    if started is not None:
        started = _iso(_instant(started, "recovery.started_at"))
    advanced = value["last_advanced_at"]
    if advanced is not None:
        advanced = _iso(_instant(advanced, "recovery.last_advanced_at"))
    return {
        "state": state,
        "started_at": started,
        "last_advanced_at": advanced,
        "mode": mode,
        "progress": _clamp(value["progress"], "recovery.progress"),
        "trend": trend,
    }


# --------------------------------------------------------------------------
# BodyRuntimeState  (ticket sections 5-7, 25-29)
# --------------------------------------------------------------------------


def initial_runtime(
    *, body_id: str = BODY_ID, at: Optional[Any] = None
) -> dict[str, Any]:
    moment = _instant(at, "at") if at is not None else None
    return {
        "schema_version": SCHEMA_RUNTIME,
        "kind": KIND_RUNTIME,
        "runtime_version": RUNTIME_VERSION,
        "body_id": _text(body_id, "body_id"),
        "version": 0,
        "valid_from": _iso(moment) if moment else None,
        "last_advanced_at": _iso(moment) if moment else None,
        "somatic": {name: SOMATIC_CONTRACTS[name]["default"] for name in SOMATIC_VARIABLES},
        "recovery": _idle_recovery(),
        "signal_latch": {},
    }


def validate_runtime(state: Any) -> dict[str, Any]:
    if not isinstance(state, Mapping) or set(state) != _RUNTIME_KEYS:
        raise BodyRuntimeError("runtime state must have exactly the v1 fields")
    if state["schema_version"] not in SUPPORTED_RUNTIME_SCHEMAS:
        raise BodySchemaError(
            f"runtime schema {state['schema_version']!r} is not supported"
        )
    if state["kind"] != KIND_RUNTIME:
        raise BodyRuntimeError("runtime kind is not supported")
    version = state["version"]
    if isinstance(version, bool) or not isinstance(version, int) or version < 0:
        raise BodyRuntimeError("version must be a non-negative integer")

    somatic_raw = state["somatic"]
    if not isinstance(somatic_raw, Mapping) or set(somatic_raw) != set(SOMATIC_VARIABLES):
        raise BodyRuntimeError(
            f"somatic must have exactly {list(SOMATIC_VARIABLES)}"
        )

    latch_raw = state["signal_latch"]
    if not isinstance(latch_raw, Mapping):
        raise BodyRuntimeError("signal_latch must be an object")

    def _optional_iso(value: Any, label: str) -> Optional[str]:
        return None if value is None else _iso(_instant(value, label))

    return {
        "schema_version": SCHEMA_RUNTIME,
        "kind": KIND_RUNTIME,
        "runtime_version": _text(state["runtime_version"], "runtime_version", maximum=64),
        "body_id": _text(state["body_id"], "body_id"),
        "version": version,
        "valid_from": _optional_iso(state["valid_from"], "valid_from"),
        "last_advanced_at": _optional_iso(state["last_advanced_at"], "last_advanced_at"),
        "somatic": {name: _clamp(somatic_raw[name], f"somatic.{name}") for name in SOMATIC_VARIABLES},
        "recovery": validate_recovery(state["recovery"]),
        "signal_latch": {str(k): bool(v) for k, v in sorted(latch_raw.items())},
    }


def migrate_runtime(raw: Any) -> tuple[dict[str, Any], bool]:
    """The only sanctioned upgrade path.  Unknown schemas fail closed."""

    if not isinstance(raw, Mapping):
        raise BodyRuntimeError("runtime state must be an object")
    schema = raw.get("schema_version")
    if schema in SUPPORTED_RUNTIME_SCHEMAS:
        return validate_runtime(raw), False
    # No earlier version has ever shipped.  A future v1 -> v2 step is added
    # here rather than by coercing the payload.
    raise BodySchemaError(f"runtime schema {schema!r} is not supported")


# --------------------------------------------------------------------------
# inputs  (ticket sections 10-11)
# --------------------------------------------------------------------------


_FORBIDDEN_INPUT_CLASSES = {
    "LLM", "EXPRESSION", "MOOD", "AFFECT", "EMOTION", "DESIRE", "GOAL",
    "RELATIONSHIP", "MEMORY", "ACTIVITY", "RELATION", "USER_AFFECTION",
}


def normalize_inputs(inputs: Any) -> list[dict[str, Any]]:
    """Validate the legal input set.  An activity label is not a body fact."""

    if inputs is None:
        return []
    if isinstance(inputs, Mapping):
        inputs = [inputs]
    if not isinstance(inputs, Sequence) or isinstance(inputs, (str, bytes)):
        raise BodyRuntimeError("inputs must be a sequence of input objects")
    if len(inputs) > MAX_INPUTS:
        raise BodyRuntimeError("too many inputs")

    normalized: list[dict[str, Any]] = []
    for index, entry in enumerate(inputs):
        if not isinstance(entry, Mapping):
            raise BodyRuntimeError(f"inputs[{index}] must be an object")
        label = entry.get("class")
        if not isinstance(label, str) or not label.strip():
            raise BodyRuntimeError(f"inputs[{index}].class is required")
        kind = label.strip().upper()
        if kind in _FORBIDDEN_INPUT_CLASSES:
            raise BodyRuntimeError(
                f"inputs[{index}].class {label!r} is not a legal body input"
            )
        if kind not in INPUT_CLASSES:
            raise BodyRuntimeError(
                f"inputs[{index}].class must be one of {list(INPUT_CLASSES)}"
            )

        if kind == INPUT_CLASS_ENVIRONMENT:
            allowed = {"class", "temperature_tendency", "sensory_intensity"}
            unknown = set(entry) - allowed
            if unknown:
                raise BodyRuntimeError(f"ENVIRONMENT input has unknown fields {sorted(unknown)}")
            item: dict[str, Any] = {"class": kind}
            if "temperature_tendency" in entry:
                item["temperature_tendency"] = _strict_number(
                    entry["temperature_tendency"], "temperature_tendency", low=-1.0, high=1.0
                )
            if "sensory_intensity" in entry:
                item["sensory_intensity"] = _strict_number(
                    entry["sensory_intensity"], "sensory_intensity", low=0.0, high=1.0
                )
            normalized.append(item)

        elif kind == INPUT_CLASS_PHYSICAL:
            allowed = {
                "class", "movement_duration_seconds", "effort", "posture_seconds", "pose",
                "execution_id",
            }
            unknown = set(entry) - allowed
            if unknown:
                raise BodyRuntimeError(
                    f"PHYSICAL_RESULT input has unknown fields {sorted(unknown)}"
                )
            item = {"class": kind}
            seconds = entry.get("movement_duration_seconds", 0)
            item["movement_duration_seconds"] = _strict_number(
                seconds, "movement_duration_seconds", low=0.0, high=86400.0
            )
            effort = entry.get("effort", "moderate")
            effort_key = _text(effort, "effort", maximum=32).lower()
            if effort_key not in EFFORT_CLASSES:
                raise BodyRuntimeError(f"effort must be one of {sorted(EFFORT_CLASSES)}")
            item["effort"] = effort_key
            item["posture_seconds"] = _strict_number(
                entry.get("posture_seconds", 0), "posture_seconds", low=0.0, high=86400.0
            )
            pose = entry.get("pose")
            if pose is not None:
                pose_key = _text(pose, "pose", maximum=32).lower()
                if pose_key not in POSE_STRAIN:
                    raise BodyRuntimeError(f"pose must be one of {sorted(POSE_STRAIN)}")
                item["pose"] = pose_key
            if entry.get("execution_id") is not None:
                item["execution_id"] = _text(
                    entry["execution_id"], "execution_id", maximum=256
                )
            normalized.append(item)

        else:  # RECOVERY
            allowed = {"class", "mode", "state"}
            unknown = set(entry) - allowed
            if unknown:
                raise BodyRuntimeError(f"RECOVERY input has unknown fields {sorted(unknown)}")
            item = {"class": kind}
            mode = entry.get("mode", "rest")
            mode_key = _text(mode, "mode", maximum=32).lower()
            if mode_key not in RECOVERY_MODES:
                raise BodyRuntimeError(f"mode must be one of {list(RECOVERY_MODES)}")
            item["mode"] = mode_key
            desired = entry.get("state", "start")
            desired_key = _text(desired, "state", maximum=32).lower()
            if desired_key not in {"start", "stop"}:
                raise BodyRuntimeError("RECOVERY.state must be 'start' or 'stop'")
            item["state"] = desired_key
            normalized.append(item)

    return normalized


def _load_units(physical: Mapping[str, Any]) -> float:
    """Hours-equivalent of physical effort.  Not an activity label."""

    hours = float(physical.get("movement_duration_seconds", 0.0)) / 3600.0
    effort = EFFORT_CLASSES[physical["effort"]]
    posture_hours = float(physical.get("posture_seconds", 0.0)) / 3600.0
    pose = physical.get("pose")
    strain = POSE_STRAIN.get(pose, 0.0) if pose else 0.0
    return hours * effort + posture_hours * strain


def _posture_strain_hours(physical: Mapping[str, Any]) -> float:
    posture_hours = float(physical.get("posture_seconds", 0.0)) / 3600.0
    pose = physical.get("pose")
    strain = POSE_STRAIN.get(pose, 0.25) if pose else 0.25
    return posture_hours * strain


def _cause_classes(inputs: Sequence[Mapping[str, Any]]) -> list[str]:
    causes: list[str] = []
    kinds = {item["class"] for item in inputs}
    if INPUT_CLASS_RECOVERY in kinds:
        causes.append(CAUSE_RECOVERY)
    if INPUT_CLASS_ENVIRONMENT in kinds:
        causes.append(CAUSE_ENVIRONMENT)
    if INPUT_CLASS_PHYSICAL in kinds:
        causes.append(CAUSE_PHYSICAL)
    causes.append(CAUSE_TIME_ADVANCE)
    return causes


# --------------------------------------------------------------------------
# advance  (ticket sections 8-19)
# --------------------------------------------------------------------------


def advance(
    previous_state: Mapping[str, Any],
    from_time: Any,
    to_time: Any,
    inputs: Any = None,
    runtime_version: Optional[str] = None,
) -> dict[str, Any]:
    """Deterministically advance the body from ``from_time`` to ``to_time``.

    Pure with respect to the outside world: the caller supplies both instants
    and every input.  No wall clock, no randomness, no LLM, no I/O.
    """

    state = validate_runtime(previous_state)
    version = _text(runtime_version or RUNTIME_VERSION, "runtime_version", maximum=64)
    if version != state["runtime_version"]:
        raise BodyRuntimeError(
            f"runtime_version mismatch: state={state['runtime_version']!r} call={version!r}"
        )

    start = _instant(from_time, "from_time")
    end = _instant(to_time, "to_time")
    if end < start:
        raise BodyRuntimeError("to_time must not precede from_time")
    total_hours = (end - start).total_seconds() / 3600.0
    if total_hours > MAX_HOURS_PER_ADVANCE:
        raise BodyRuntimeError(
            f"interval exceeds the bounded advance of {MAX_HOURS_PER_ADVANCE}h"
        )

    normalized = normalize_inputs(inputs)
    causes = _cause_classes(normalized)

    physical = [i for i in normalized if i["class"] == INPUT_CLASS_PHYSICAL]
    environment = [i for i in normalized if i["class"] == INPUT_CLASS_ENVIRONMENT]
    recovery = [i for i in normalized if i["class"] == INPUT_CLASS_RECOVERY]

    load_units = sum(_load_units(item) for item in physical)
    posture_hours = sum(_posture_strain_hours(item) for item in physical)

    temperature_tendency = None
    sensory_intensity = None
    if environment:
        # later inputs win; each is already bounded
        temperature_tendency = environment[-1].get("temperature_tendency")
        sensory_intensity = environment[-1].get("sensory_intensity")

    # ---- recovery state machine ----------------------------------------
    recovery_state = _detach(state["recovery"])
    recovery_open = recovery_state["state"] == "active"
    notes: list[str] = []

    for item in recovery:
        if item["state"] == "start":
            if state["somatic"]["fatigue"] < RECOVERY_OPEN_FATIGUE:
                notes.append("recovery_not_opened_below_threshold")
                continue
            recovery_open = True
            recovery_state["state"] = "active"
            recovery_state["mode"] = item["mode"]
            recovery_state["started_at"] = recovery_state["started_at"] or _iso(start)
        else:  # stop
            if recovery_open:
                recovery_state["state"] = "completed"
                recovery_state["trend"] = "improving"
            recovery_open = False

    # a real physical load interrupts an open recovery session
    if recovery_open and load_units > 0.0:
        recovery_state["state"] = "interrupted"
        recovery_state["trend"] = "worsening"
        recovery_open = False
        notes.append("recovery_interrupted_by_physical_load")

    multiplier = 1.0
    if recovery_state["state"] == "active":
        multiplier = {
            "rest": FATIGUE_RECOVERY_MULTIPLIER,
            "sleep_like": FATIGUE_RECOVERY_MULTIPLIER * 1.6,
        }[recovery_state["mode"] or "rest"]

    before = _detach(state["somatic"])
    after: dict[str, float] = {}

    # fatigue: accumulate real load first, then decay toward baseline
    fatigue = before["fatigue"] + load_units * FATIGUE_PER_LOAD_HOUR
    fatigue = _approach(
        fatigue, FATIGUE_BASELINE, FATIGUE_DECAY_PER_HOUR * multiplier, total_hours
    )
    after["fatigue"] = _clamp(fatigue, "fatigue")

    # activation
    activation = before["activation"] + load_units * ACTIVATION_PER_LOAD_HOUR
    activation_target = (
        ACTIVATION_RECOVERY_TARGET if recovery_state["state"] == "active" else ACTIVATION_BASELINE
    )
    activation = _approach(
        activation, activation_target, ACTIVATION_DECAY_PER_HOUR, total_hours
    )
    after["activation"] = _clamp(activation, "activation")

    # comfort: posture strain pushes it down
    comfort_multiplier = (
        COMFORT_RECOVERY_MULTIPLIER if recovery_state["state"] == "active" else 1.0
    )
    comfort = before["comfort"] - posture_hours * COMFORT_POSTURE_LOAD_PER_HOUR
    comfort = _approach(
        comfort, COMFORT_BASELINE, COMFORT_DECAY_PER_HOUR * comfort_multiplier, total_hours
    )
    after["comfort"] = _clamp(comfort, "comfort")

    # sensory load: responds ONLY to an explicit sensory intensity
    sensory = before["sensory_load"]
    if sensory_intensity is not None:
        sensory = _approach(sensory, sensory_intensity, SENSORY_RISE_PER_HOUR, total_hours)
    sensory_multiplier = (
        SENSORY_RECOVERY_MULTIPLIER if recovery_state["state"] == "active" else 1.0
    )
    sensory = _approach(
        sensory, SENSORY_BASELINE, SENSORY_DECAY_PER_HOUR * sensory_multiplier, total_hours
    )
    after["sensory_load"] = _clamp(sensory, "sensory_load")

    # tension
    tension = before["tension"] + (load_units + posture_hours) * TENSION_PER_LOAD_HOUR
    tension_multiplier = (
        TENSION_RECOVERY_MULTIPLIER if recovery_state["state"] == "active" else 1.0
    )
    tension = _approach(
        tension, TENSION_BASELINE, TENSION_DECAY_PER_HOUR * tension_multiplier, total_hours
    )
    after["tension"] = _clamp(tension, "tension")

    # warmth: virtual tendency toward the environment
    warmth_target = WARMTH_BASELINE
    if temperature_tendency is not None:
        warmth_target = _clamp(
            WARMTH_BASELINE + 0.40 * float(temperature_tendency), "warmth_target"
        )
    after["warmth"] = _clamp(
        _approach(before["warmth"], warmth_target, WARMTH_APPROACH_PER_HOUR, total_hours),
        "warmth",
    )

    # ---- recovery bookkeeping ------------------------------------------
    if recovery_state["state"] == "active":
        recovery_state["last_advanced_at"] = _iso(end)
        delta = before["fatigue"] - after["fatigue"]
        recovery_state["trend"] = (
            "improving" if delta > 1e-6 else "stalled" if delta > -1e-6 else "worsening"
        )
        recovery_state["progress"] = _clamp(
            recovery_state["progress"] + max(0.0, delta) * 2.0, "recovery.progress"
        )
        if after["fatigue"] <= FATIGUE_BASELINE + 1e-6:
            recovery_state["state"] = "completed"
            recovery_state["progress"] = 1.0
    elif recovery_state["state"] == "interrupted":
        recovery_state["last_advanced_at"] = _iso(end)
        recovery_state["trend"] = "worsening"

    changed = any(abs(after[name] - before[name]) > CHANGE_EPSILON for name in SOMATIC_VARIABLES)
    changed = changed or _detach(recovery_state) != _detach(state["recovery"])

    updated = {
        "schema_version": SCHEMA_RUNTIME,
        "kind": KIND_RUNTIME,
        "runtime_version": state["runtime_version"],
        "body_id": state["body_id"],
        "version": state["version"] + (1 if changed else 0),
        "valid_from": state["valid_from"] or _iso(start),
        "last_advanced_at": _iso(end),
        "somatic": after,
        "recovery": recovery_state,
        "signal_latch": _detach(state["signal_latch"]),
    }
    updated = validate_runtime(updated)

    return {
        "kind": "body_advance_result",
        "schema_version": SCHEMA_RUNTIME,
        "runtime_version": state["runtime_version"],
        "body_id": state["body_id"],
        "from_time": _iso(start),
        "to_time": _iso(end),
        "elapsed_seconds": (end - start).total_seconds(),
        "advanced": changed,
        "cause_class": causes[0],
        "cause_classes": causes,
        "inputs_summary": {
            "physical_events": len(physical),
            "environment_events": len(environment),
            "recovery_events": len(recovery),
            "load_units": round(load_units, 9),
            "posture_strain_hours": round(posture_hours, 9),
        },
        "notes": notes,
        "before": before,
        "after": after,
        "recovery_before": _detach(state["recovery"]),
        "recovery_after": _detach(recovery_state),
        "state": updated,
    }


def catch_up(
    previous_state: Mapping[str, Any],
    last_persisted_at: Any,
    now: Any,
    *,
    allow_recovery_continuation: bool = True,
) -> dict[str, Any]:
    """Bounded catch-up after downtime.

    Only deterministic time/recovery evolution is applied, and never more than
    ``MAX_HOURS_PER_ADVANCE`` in one step.  Anything longer is a *partial*
    advance and is reported as such rather than silently extrapolated.
    """

    start = _instant(last_persisted_at, "last_persisted_at")
    end = _instant(now, "now")
    if end < start:
        raise BodyRuntimeError("now must not precede last_persisted_at")

    hours = (end - start).total_seconds() / 3600.0
    bounded = min(hours, MAX_HOURS_PER_ADVANCE)
    step_end = start + timedelta(hours=bounded)

    state = validate_runtime(previous_state)
    inputs: list[dict[str, Any]] = []
    if allow_recovery_continuation and state["recovery"]["state"] == "active":
        inputs.append({"class": INPUT_CLASS_RECOVERY, "mode": state["recovery"]["mode"], "state": "start"})

    result = advance(state, start, step_end, inputs)
    result["catch_up"] = {
        "requested_hours": round(hours, 9),
        "applied_hours": round(bounded, 9),
        "bounded": bounded < hours,
        "partial": bounded < hours,
        "recovery_continued": state["recovery"]["state"] == "active",
    }
    return result


def replay(
    initial_state: Mapping[str, Any],
    steps: Sequence[Mapping[str, Any]],
    runtime_version: Optional[str] = None,
) -> dict[str, Any]:
    """Replay a transition sequence.  Same inputs must give the same state."""

    state = validate_runtime(initial_state)
    version = runtime_version or state["runtime_version"]
    produced: list[dict[str, Any]] = []
    for index, step in enumerate(steps):
        if not isinstance(step, Mapping):
            raise BodyRuntimeError(f"steps[{index}] must be an object")
        result = advance(
            state,
            step.get("from_time"),
            step.get("to_time"),
            step.get("inputs"),
            version,
        )
        state = result["state"]
        produced.append(result)
    return {"state": state, "steps": produced}


# --------------------------------------------------------------------------
# BodyTransition  (ticket sections 20-21)
# --------------------------------------------------------------------------


def build_transition(
    before_state: Mapping[str, Any],
    after_state: Mapping[str, Any],
    *,
    at: Any,
    cause_class: str,
    cause_classes: Optional[Sequence[str]] = None,
    input_summary: Optional[Mapping[str, Any]] = None,
    valid_to: Optional[Any] = None,
) -> dict[str, Any]:
    before = validate_runtime(before_state)
    after = validate_runtime(after_state)
    cause = _text(cause_class, "cause_class", maximum=64)
    if cause not in CAUSE_CLASSES:
        raise BodyRuntimeError(f"cause_class must be one of {list(CAUSE_CLASSES)}")
    moment = _instant(at, "at")
    causes = list(cause_classes or [cause])
    for item in causes:
        if item not in CAUSE_CLASSES:
            raise BodyRuntimeError(f"cause_classes contains an illegal value {item!r}")

    identity = {
        "body_id": after["body_id"],
        "from_version": before["version"],
        "to_version": after["version"],
        "valid_from": _iso(moment),
        "cause_class": cause,
        "before_state_ref": _digest(before),
        "after_state_ref": _digest(after),
    }
    return {
        "kind": KIND_TRANSITION,
        "schema_version": SCHEMA_TRANSITION,
        "transition_id": "body-transition:" + _digest(identity)[:32],
        "body_id": after["body_id"],
        "from_version": before["version"],
        "to_version": after["version"],
        "valid_from": _iso(moment),
        "valid_to": _iso(_instant(valid_to, "valid_to")) if valid_to is not None else None,
        "input_refs": list((input_summary or {}).get("input_refs") or []),
        "input_summary": _detach(dict(input_summary or {})),
        "before_state_ref": identity["before_state_ref"],
        "after_state_ref": identity["after_state_ref"],
        "runtime_version": after["runtime_version"],
        "cause_class": cause,
        "cause_classes": causes,
        "somatic_before": _detach(before["somatic"]),
        "somatic_after": _detach(after["somatic"]),
        "recovery_before": _detach(before["recovery"]),
        "recovery_after": _detach(after["recovery"]),
    }


def validate_transition(record: Any) -> dict[str, Any]:
    if not isinstance(record, Mapping):
        raise BodyRuntimeError("transition must be an object")
    if record.get("kind") != KIND_TRANSITION:
        raise BodyRuntimeError("transition kind is not supported")
    if record.get("schema_version") != SCHEMA_TRANSITION:
        raise BodySchemaError("transition schema is not supported")
    return _detach(dict(record))


# --------------------------------------------------------------------------
# persistence  (ticket section 25)
# --------------------------------------------------------------------------


@contextmanager
def _locked(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(str(path) + ".lock", "a+", encoding="utf-8")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def _atomic_write(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True)
    descriptor, temporary = tempfile.mkstemp(
        dir=str(path.parent), prefix=path.name + ".", suffix=".tmp"
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        if os.path.exists(temporary):
            os.unlink(temporary)
        raise


class BodyStore:
    """Atomic, lock-protected persistence for profile, runtime and transitions."""

    def __init__(self, root: Path | str):
        self.root = Path(root)
        self.profile_path = self.root / PROFILE_NAME
        self.runtime_path = self.root / RUNTIME_NAME
        self.transitions_path = self.root / TRANSITIONS_NAME

    # -- profile ---------------------------------------------------------

    def load_profile(self) -> Optional[dict[str, Any]]:
        if not self.profile_path.exists():
            return None
        try:
            raw = json.loads(self.profile_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise BodyRuntimeError("profile is not readable JSON") from exc
        return validate_profile(raw)

    def save_profile(self, profile: Mapping[str, Any]) -> dict[str, Any]:
        normalized = validate_profile(profile)
        with _locked(self.profile_path):
            _atomic_write(self.profile_path, normalized)
        return normalized

    # -- runtime ---------------------------------------------------------

    def load_runtime(self) -> Optional[dict[str, Any]]:
        state, _migrated = self.load_runtime_with_migration()
        return state

    def load_runtime_with_migration(self) -> tuple[Optional[dict[str, Any]], bool]:
        if not self.runtime_path.exists():
            return None, False
        try:
            raw = json.loads(self.runtime_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise BodyRuntimeError("runtime state is not readable JSON") from exc
        return migrate_runtime(raw)

    def save_runtime(
        self, state: Mapping[str, Any], *, expected_version: Optional[int] = None
    ) -> dict[str, Any]:
        normalized = validate_runtime(state)
        with _locked(self.runtime_path):
            if expected_version is not None:
                if isinstance(expected_version, bool) or not isinstance(
                    expected_version, int
                ):
                    raise BodyRuntimeError("expected_version must be an integer")
                current = None
                if self.runtime_path.exists():
                    try:
                        current = json.loads(self.runtime_path.read_text(encoding="utf-8"))
                    except (OSError, ValueError):
                        current = None
                live = current.get("version") if isinstance(current, dict) else None
                if live != expected_version:
                    raise BodyRuntimeError(
                        f"runtime version mismatch: expected {expected_version}, live {live}"
                    )
            _atomic_write(self.runtime_path, normalized)
        return normalized

    # -- transitions -----------------------------------------------------

    def append_transition(self, record: Mapping[str, Any]) -> dict[str, Any]:
        normalized = validate_transition(record)
        with _locked(self.transitions_path):
            self.transitions_path.parent.mkdir(parents=True, exist_ok=True)
            try:
                with self.transitions_path.open("a", encoding="utf-8") as handle:
                    handle.write(
                        json.dumps(normalized, ensure_ascii=False, sort_keys=True) + "\n"
                    )
                    handle.flush()
                    os.fsync(handle.fileno())
            except OSError as exc:
                raise BodyRuntimeError("transitions cannot be appended") from exc
        return normalized

    def read_transitions(self) -> list[dict[str, Any]]:
        if not self.transitions_path.exists():
            return []
        records: list[dict[str, Any]] = []
        for number, line in enumerate(
            self.transitions_path.read_text(encoding="utf-8").splitlines(), 1
        ):
            if not line.strip():
                continue
            try:
                records.append(validate_transition(json.loads(line)))
            except (TypeError, ValueError) as exc:
                raise BodyRuntimeError(
                    f"transitions malformed at line {number}"
                ) from exc
        return records