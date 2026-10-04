#!/usr/bin/env python3
"""BodySignal producer (M8) — bounded qualitative interoceptive projection.

Turns a ``BodyRuntimeState`` into a *small, versioned, machine-consumable*
vocabulary of body signals.  It is the producer only: no Perception, no
Attention, no Memory, no Affect, no Desire consumer exists here (that is M10).

Hard rules
----------
* **No raw state.**  A signal never carries ``fatigue = 0.637281`` or any other
  floating-point body value.  Severity is a qualitative band.  Debug surfaces
  may read raw values; signals may not.
* **Hysteresis.**  A signal turns on above its on-threshold and only turns off
  below its off-threshold, so a value hovering at the boundary does not flap.
  The latch lives in the runtime state, so it survives restart and replay.
* **No interpretation.**  Signals describe the body.  They never say anxious,
  sad, excited, wanting, or in love.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from typing import Any, Optional

SCHEMA_SIGNAL = "body.signal.v1"
SCHEMA_OBSERVATION = "body.observation.v1"
KIND_SIGNAL = "body_signal"
KIND_OBSERVATION = "body_observation"

BAND_MILD = "mild"
BAND_NOTICEABLE = "noticeable"
BAND_STRONG = "strong"
BANDS = (BAND_MILD, BAND_NOTICEABLE, BAND_STRONG)

#: severity -> representative scalar used ONLY inside the signal, and only ever
#: emitted as a band.  It is not a body measurement.
_BAND_RANK = {BAND_MILD: 1, BAND_NOTICEABLE: 2, BAND_STRONG: 3}

VISIBILITY_SCOPE = "body_internal"

# Each rule: name -> (variable, direction, (off, on), band_fn)
# direction "high" means the signal fires when the variable is high.
SIGNAL_RULES: dict[str, dict[str, Any]] = {
    "fatigue_noticeable": {
        "variable": "fatigue", "direction": "high", "thresholds": (0.45, 0.55),
        "bands": ((0.55, BAND_MILD), (0.75, BAND_NOTICEABLE), (0.90, BAND_STRONG)),
    },
    "fatigue_high": {
        "variable": "fatigue", "direction": "high", "thresholds": (0.70, 0.80),
        "bands": ((0.80, BAND_NOTICEABLE), (0.92, BAND_STRONG)),
    },
    "activation_low": {
        "variable": "activation", "direction": "low", "thresholds": (0.45, 0.35),
        "bands": ((0.35, BAND_MILD), (0.20, BAND_NOTICEABLE), (0.10, BAND_STRONG)),
    },
    "discomfort_mild": {
        "variable": "comfort", "direction": "low", "thresholds": (0.62, 0.55),
        "bands": ((0.55, BAND_MILD), (0.35, BAND_NOTICEABLE), (0.18, BAND_STRONG)),
    },
    "discomfort_noticeable": {
        "variable": "comfort", "direction": "low", "thresholds": (0.45, 0.35),
        "bands": ((0.35, BAND_NOTICEABLE), (0.18, BAND_STRONG)),
    },
    "sensory_load_elevated": {
        "variable": "sensory_load", "direction": "high", "thresholds": (0.50, 0.60),
        "bands": ((0.60, BAND_MILD), (0.78, BAND_NOTICEABLE), (0.90, BAND_STRONG)),
    },
    "tension_elevated": {
        "variable": "tension", "direction": "high", "thresholds": (0.50, 0.60),
        "bands": ((0.60, BAND_MILD), (0.78, BAND_NOTICEABLE), (0.90, BAND_STRONG)),
    },
    "warmth_high": {
        "variable": "warmth", "direction": "high", "thresholds": (0.68, 0.75),
        "bands": ((0.75, BAND_MILD), (0.88, BAND_NOTICEABLE)),
    },
    "cold_tendency": {
        "variable": "warmth", "direction": "low", "thresholds": (0.32, 0.25),
        "bands": ((0.25, BAND_MILD), (0.12, BAND_NOTICEABLE)),
    },
}

#: Trend-based signals are emitted only when the advance actually moved that way.
TREND_RULES: dict[str, dict[str, Any]] = {
    "recovery_underway": {"recovery_state": "active"},
    "recovery_interrupted": {"recovery_state": "interrupted"},
    "tension_releasing": {"variable": "tension", "direction": "down"},
    "activation_recovering": {"variable": "activation", "direction": "up"},
}

#: Fields that must never appear on a signal or observation.
FORBIDDEN_SIGNAL_FIELDS = frozenset(
    {
        "raw", "value", "values", "state", "somatic", "fatigue", "activation",
        "comfort", "sensory_load", "tension", "warmth", "emotion", "affect",
        "mood", "desire", "feeling", "interpretation", "advice", "expression",
        "relationship", "memory", "goal",
    }
)


class BodySignalError(ValueError):
    """A signal contract was violated."""


def _digest(value: Any) -> str:
    payload = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _band_for(rule: Mapping[str, Any], value: float) -> str:
    band = BAND_MILD
    for threshold, name in rule["bands"]:
        if rule["direction"] == "high" and value >= threshold:
            band = name
        elif rule["direction"] == "low" and value <= threshold:
            band = name
    return band


def compute_latch(
    somatic: Mapping[str, Any],
    previous_latch: Optional[Mapping[str, Any]] = None,
) -> dict[str, bool]:
    """Hysteretic on/off for level-based signals.  Pure, deterministic."""

    previous = dict(previous_latch or {})
    latch: dict[str, bool] = {}
    for name, rule in SIGNAL_RULES.items():
        value = float(somatic[rule["variable"]])
        off, on = rule["thresholds"]
        was_on = bool(previous.get(name, False))
        if rule["direction"] == "high":
            latch[name] = value >= (off if was_on else on)
        else:
            latch[name] = value <= (off if was_on else on)
    return latch


def produce_signals(
    runtime_state: Mapping[str, Any],
    *,
    occurred_at: Any,
    previous_state: Optional[Mapping[str, Any]] = None,
    source_transition_refs: Optional[Sequence[str]] = None,
    previous_latch: Optional[Mapping[str, Any]] = None,
) -> dict[str, Any]:
    """Derive the bounded signal set from a runtime state.

    ``previous_state`` (optional) enables the trend signals; without it, only
    level-based signals and recovery-state signals are produced.
    """

    import body_runtime as br

    state = br.validate_runtime(runtime_state)
    moment = br._instant(occurred_at, "occurred_at")
    refs = [br._text(item, "source_transition_ref", maximum=256) for item in (source_transition_refs or [])]

    latch = compute_latch(state["somatic"], previous_latch or state.get("signal_latch"))

    signals: list[dict[str, Any]] = []

    def _emit(signal_type: str, band: str) -> None:
        if band not in BANDS:
            raise BodySignalError(f"band {band!r} is not in the bounded vocabulary")
        identity = {
            "body_id": state["body_id"],
            "signal_type": signal_type,
            "band": band,
            "occurred_at": br._iso(moment),
            "from_version": state["version"],
            "source_transition_refs": sorted(refs),
        }
        signals.append(
            {
                "kind": KIND_SIGNAL,
                "schema_version": SCHEMA_SIGNAL,
                "signal_id": "body-signal:" + _digest(identity)[:32],
                "body_id": state["body_id"],
                "signal_type": signal_type,
                "qualitative_band": band,
                "occurred_at": br._iso(moment),
                "source_transition_refs": sorted(refs),
                "source_body_version": state["version"],
                "runtime_version": state["runtime_version"],
                "visibility_scope": VISIBILITY_SCOPE,
                "carries_raw_values": False,
            }
        )

    # level signals, gated by the hysteretic latch
    for name, rule in SIGNAL_RULES.items():
        if latch.get(name):
            _emit(name, _band_for(rule, float(state["somatic"][rule["variable"]])))

    # recovery-state signals
    recovery_state = state["recovery"]["state"]
    for name, rule in TREND_RULES.items():
        if rule.get("recovery_state") and recovery_state == rule["recovery_state"]:
            _emit(name, BAND_NOTICEABLE if name == "recovery_interrupted" else BAND_MILD)

    # trend signals need the previous state
    if previous_state is not None:
        before = br.validate_runtime(previous_state)
        for name, rule in TREND_RULES.items():
            variable = rule.get("variable")
            if not variable:
                continue
            delta = float(state["somatic"][variable]) - float(before["somatic"][variable])
            if rule["direction"] == "down" and delta < -br.CHANGE_EPSILON:
                _emit(name, BAND_MILD if abs(delta) < 0.05 else BAND_NOTICEABLE)
            elif rule["direction"] == "up" and delta > br.CHANGE_EPSILON:
                _emit(name, BAND_MILD if abs(delta) < 0.05 else BAND_NOTICEABLE)

    for signal in signals:
        assert_no_forbidden_fields(signal)

    return {
        "kind": "body_signal_set",
        "schema_version": SCHEMA_SIGNAL,
        "body_id": state["body_id"],
        "occurred_at": br._iso(moment),
        "runtime_version": state["runtime_version"],
        "latch": latch,
        "signals": signals,
    }


def assert_no_forbidden_fields(payload: Mapping[str, Any]) -> None:
    """The raw-value firewall, enforced structurally."""

    for key in payload:
        if key in FORBIDDEN_SIGNAL_FIELDS:
            raise BodySignalError(
                f"signal payload must not carry the field {key!r}"
            )
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    for name in br_variables():
        if f'"{name}"' in blob:
            raise BodySignalError(f"signal payload leaks the raw variable {name!r}")


def br_variables() -> tuple[str, ...]:
    import body_runtime as br

    return br.SOMATIC_VARIABLES


def build_observation(
    signals: Mapping[str, Any],
    *,
    observation_id_source: Optional[str] = None,
) -> list[dict[str, Any]]:
    """Project signals into ``BodyObservation`` records.

    The observation stops here.  It is not Perception, not Attention, and it is
    not injected into any model context.
    """

    if not isinstance(signals, Mapping) or signals.get("kind") != "body_signal_set":
        raise BodySignalError("expected a body_signal_set")
    observations: list[dict[str, Any]] = []
    for signal in signals["signals"]:
        signal = dict(signal)
        identity = {
            "signal_id": signal["signal_id"],
            "observed_at": signal["occurred_at"],
            "source": observation_id_source or "body_runtime",
        }
        observation = {
            "kind": KIND_OBSERVATION,
            "schema_version": SCHEMA_OBSERVATION,
            "observation_id": "body-observation:" + _digest(identity)[:32],
            "body_id": signal["body_id"],
            "observed_at": signal["occurred_at"],
            "signal_type": signal["signal_type"],
            "qualitative_band": signal["qualitative_band"],
            "source_transition_refs": list(signal["source_transition_refs"]),
            "visibility_scope": signal["visibility_scope"],
            "schema_source": SCHEMA_SIGNAL,
            "runtime_version": signal["runtime_version"],
            "freshness": "current",
            "consumer": None,
        }
        assert_no_forbidden_fields(observation)
        observations.append(observation)
    return observations