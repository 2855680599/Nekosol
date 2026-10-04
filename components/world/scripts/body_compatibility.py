#!/usr/bin/env python3
"""HeartMind compatibility projection (M8) — READ-ONLY, shadow only.

Purpose
-------
Prove *in advance* whether the M8 runtime could cover the legacy consumer
contract that M7B will have to replace.  This module answers that question and
nothing else.

It does **not** write HeartMind, does not import xinxi, does not feed Life, and
does not inject anything into Main Self.  Life keeps reading the legacy
HeartMind values until M7B (ticket section 33).

Semantics
---------
The legacy ``fatigue`` / ``energy`` / ``reserve`` numbers are a *mixed* value:
they were produced by a state machine that also owns mood, drives, sensitivity,
menstrual cycle and weather (see ``M7A_HEARTMIND_CONSUMER_INVENTORY.md``).
They are therefore **not** clean body semantics, and the projection is
explicitly labelled:

    approximate | legacy compatibility only | not canonical semantics

The mixed legacy meaning is never copied back into the M8 runtime.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Optional

SCHEMA_PROJECTION = "body.legacy_projection.v1"
SCHEMA_READINESS = "body.m7b_readiness.v1"
KIND_PROJECTION = "legacy_body_projection"
KIND_READINESS = "m7b_readiness"

LABEL = "approximate | legacy compatibility only | not canonical semantics"
CONSUMER = "m7b_shadow_comparison_only"

#: The legacy scale this projection emulates.  The conversion lives HERE and
#: nowhere else, so the M8 runtime never adopts the 0..100 scale.
LEGACY_SCALE = 100.0

#: Explicitly not projected: these legacy fields carry mixed non-body meaning.
NOT_PROJECTED = {
    "heartbeat": "legacy vitals were rendered as prose, not a body fact",
    "temperature": "legacy temperature had no clean body semantics",
    "mood": "affect domain, not body",
    "daily_mood": "affect domain, not body",
    "heat": "desire domain, not body",
    "pressure": "desire domain, not body",
    "possessiveness": "relationship domain, not body",
    "sensitivity": "mixed drive/sensitivity semantics",
    "body_reserve": "sexual-state reserve, not somatic reserve",
    "menses_*": "cycle domain, not body",
}


class BodyCompatibilityError(ValueError):
    """The compatibility contract was violated."""


def _clamp_legacy(value: float) -> float:
    return min(LEGACY_SCALE, max(0.0, value))


def project_legacy_body(
    runtime_state: Mapping[str, Any],
    *,
    include_provenance: bool = True,
) -> dict[str, Any]:
    """Project a BodyRuntimeState onto the legacy 0..100 body labels.

    Read-only: touches no file, writes no legacy state.  The formulas are a
    deliberate approximation whose only job is contract comparison.
    """

    import body_runtime as br

    state = br.validate_runtime(runtime_state)
    somatic = state["somatic"]
    fatigue = float(somatic["fatigue"])
    activation = float(somatic["activation"])
    comfort = float(somatic["comfort"])

    # Approximate legacy formulas.  energy leans on the fatigue complement and
    # the activation level; reserve leans on the fatigue complement and comfort.
    energy = 0.65 * (1.0 - fatigue) + 0.35 * activation
    reserve = (1.0 - fatigue) * comfort

    projection: dict[str, Any] = {
        "kind": KIND_PROJECTION,
        "schema_version": SCHEMA_PROJECTION,
        "body_id": state["body_id"],
        "source_body_version": state["version"],
        "runtime_version": state["runtime_version"],
        "label": LABEL,
        "consumer": CONSUMER,
        "writes_legacy_state": False,
        "canonical": False,
        "fatigue_0_100": round(_clamp_legacy(fatigue * LEGACY_SCALE), 6),
        "energy_0_100": round(_clamp_legacy(energy * LEGACY_SCALE), 6),
        "reserve_0_100": round(_clamp_legacy(reserve * LEGACY_SCALE), 6),
    }
    if include_provenance:
        projection["provenance"] = {
            "fatigue_0_100": "fatigue * 100",
            "energy_0_100": "(0.65 * (1 - fatigue) + 0.35 * activation) * 100",
            "reserve_0_100": "((1 - fatigue) * comfort) * 100",
            "not_projected": dict(NOT_PROJECTED),
            "note": (
                "legacy values are a mixed body/mood/drive/cycle state; this "
                "projection is for contract comparison, not semantic equality"
            ),
        }
    return projection


def assert_readonly(reference_path: Optional[Path | str]) -> dict[str, Any]:
    """Prove the projection does not touch a legacy state file.

    Returns a small evidence record.  Never writes.
    """

    if reference_path is None:
        return {"checked": False}
    path = Path(reference_path)
    if not path.exists():
        return {"checked": True, "exists": False, "sha256": None}
    import hashlib

    return {
        "checked": True,
        "exists": True,
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }


# --------------------------------------------------------------------------
# M7B readiness  (ticket section 32)
# --------------------------------------------------------------------------

#: Every legacy body consumer found by the M7A inventory, with a verdict on
#: whether the M8 projection can already stand in for it.
_MUST_REMOVE_RAW_INJECTION = (
    "this is a Main Self injection path, not a body read; M7B must remove the "
    "raw body JSON (see m7b_required)"
)

_CONSUMERS: tuple[dict[str, Any], ...] = (
    {
        "consumer": "life_loop.py:405-407,774-777",
        "reads": ["fatigue", "energy", "reserve"],
        "use": "Life body pressure metric",
        "m8_can_replace": "partial",
        "why": (
            "it reads a body pressure number; M8 can supply a clean fatigue "
            "value, but the current consumer compares against legacy 0..100 "
            "thresholds and must be changed to the new scale in M7B"
        ),
    },
    {
        "consumer": "life_choice.py:515-517",
        "reads": ["fatigue", "energy"],
        "use": "activity cost metric",
        "m8_can_replace": "partial",
        "why": "same scale mismatch; also coupled to drive-derived cost terms",
    },
    {
        "consumer": "pending_reattention.py:177",
        "reads": ["fatigue", "energy", "reserve"],
        "use": "raw body JSON into Main Self",
        "m8_can_replace": "no",
        "why": _MUST_REMOVE_RAW_INJECTION,
    },
    {
        "consumer": "communication_autonomy.py:429",
        "reads": ["fatigue", "energy", "reserve"],
        "use": "raw body JSON into Main Self",
        "m8_can_replace": "no",
        "why": _MUST_REMOVE_RAW_INJECTION,
    },
    {
        "consumer": "pending_self_reply.py:253,355-373",
        "reads": ["fatigue", "energy", "reserve"],
        "use": "raw body JSON into api_messages",
        "m8_can_replace": "no",
        "why": _MUST_REMOVE_RAW_INJECTION,
    },
    {
        "consumer": "plugin/__init__.py:730,834-845",
        "reads": ["legacy body prose"],
        "use": "highest-priority Main Self injection",
        "m8_can_replace": "no",
        "why": _MUST_REMOVE_RAW_INJECTION,
    },
    {
        "consumer": "dashboard_api.py:248-260,267-289",
        "reads": ["all raw legacy keys"],
        "use": "operator display",
        "m8_can_replace": "yes",
        "why": "display only; can read the M8 debug surface instead",
    },
    {
        "consumer": "trust_report.py:56-68",
        "reads": ["energy", "fatigue", "heat"],
        "use": "display only (self-described as non-interfering)",
        "m8_can_replace": "partial",
        "why": "heat is a desire field and stays out of Body",
    },
    {
        "consumer": "xinxi_tick.py:270-272",
        "reads": ["fatigue", "energy", "sensitivity"],
        "use": "derives mood coordinates",
        "m8_can_replace": "no",
        "why": "feeds the affect domain; must stay on the affect line, not Body",
    },
)


def m7b_readiness(runtime_state: Optional[Mapping[str, Any]] = None) -> dict[str, Any]:
    """Report what M7B would still have to do.  Reports only — implements nothing."""

    reasons = {
        "partial": (
            "value is cleanly derivable, but the consumer compares against the "
            "legacy 0..100 scale and legacy thresholds"
        ),
        "no": "the consumer is an injection or cross-domain path, not a body read",
        "yes": "read-only display surface",
    }
    consumers = []
    for entry in _CONSUMERS:
        consumers.append({**entry, "verdict_reason": reasons[entry["m8_can_replace"]]})

    return {
        "kind": KIND_READINESS,
        "schema_version": SCHEMA_READINESS,
        "m8_status": "SHADOW / isolated",
        "m7b_status": "READY (not started)",
        "heartmind_authority": "STILL ACTIVE",
        "projection_available": runtime_state is not None,
        "consumers": consumers,
        "summary": {
            "replaceable_now": sum(1 for c in _CONSUMERS if c["m8_can_replace"] == "yes"),
            "replaceable_after_scale_fix": sum(
                1 for c in _CONSUMERS if c["m8_can_replace"] == "partial"
            ),
            "requires_m7b_consumer_change": sum(
                1 for c in _CONSUMERS if c["m8_can_replace"] == "no"
            ),
        },
        "still_depends_on_mixed_semantics": [
            "mood / daily_mood (affect domain)",
            "heat / pressure / possessiveness (desire + relationship domain)",
            "sensitivity (drive-coupled)",
            "menses_* / ev_* (cycle domain)",
            "weather-coupled energy/mood update (xinxi_tick.py:296-309)",
        ],
        "requires_m7b_consumer_change": [
            "life_loop.py body pressure metric (scale + thresholds)",
            "life_choice.py cost metric",
            "pending_self_reply.py raw body JSON into api_messages",
            "pending_reattention.py raw body JSON into Main Self",
            "communication_autonomy.py raw body JSON",
            "plugin/__init__.py body injection precedence chain",
        ],
        "m7b_required": [
            {
                "item": "pending_self_reply raw JSON",
                "how": (
                    "stop serialising body.fatigue/energy/reserve into "
                    "api_messages; if the Main Self needs the body, give it a "
                    "bounded BodySignal vocabulary instead of raw numbers"
                ),
                "blocking": True,
            },
            {
                "item": "plugin body injection",
                "how": (
                    "the precedence chain (somatic_output.json > feeling_cat.txt "
                    "> feeling.txt) has no kill switch; M7B must add an explicit "
                    "switch, repoint it at BodySignal, and only then retire the "
                    "legacy prose"
                ),
                "blocking": True,
            },
            {
                "item": "life_loop body pressure",
                "how": (
                    "switch the reader to the new body authority and convert the "
                    "threshold comparison from 0..100 to 0..1"
                ),
                "blocking": True,
            },
            {
                "item": "HeartMind split",
                "how": (
                    "untangle _tick_drives (:611-619), weather->body "
                    "(xinxi_tick.py:296-309) and _tick_menses (:436-447) before "
                    "HeartMind can be marked LEGACY READ-ONLY"
                ),
                "blocking": True,
            },
            {
                "item": "scheduler freshness",
                "how": (
                    "no crontab/systemd/timer reference to xinxi_tick was found; "
                    "resolve who advances HeartMind before depending on either "
                    "side's freshness"
                ),
                "blocking": True,
            },
        ],
        "implements_nothing": True,
        "writes_legacy_state": False,
        "feeds_life": False,
        "injects_main_self": False,
    }


def readiness_json(runtime_state: Optional[Mapping[str, Any]] = None) -> str:
    return json.dumps(m7b_readiness(runtime_state), ensure_ascii=False, sort_keys=True, indent=2)