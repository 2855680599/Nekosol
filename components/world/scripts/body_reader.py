#!/usr/bin/env python3
"""BodyReader adapter + shadow comparison (M9 / M7B prep).

A single consumer entry point with three modes:

    legacy    -> HeartMind                       (production default)
    shadow    -> return HeartMind, silently compare M8
    canonical -> M8                              (test only, never production)

Plus a read-only shadow comparison between the legacy HeartMind projection and
the M8 compatibility projection.

Hard rules
----------
* **Default is legacy.**  Canonical mode is refused unless a test-only
  environment is explicitly set; production must never reach it.
* **Read-only.**  The comparison never writes either source and never
  synchronises them.
* **Scale conversion is centralised here.**  Consumers must not sprinkle
  ``/100`` / ``*100`` and grow a second implicit semantic.
* **Semantic equivalence is NOT expected** and every report says so.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Optional

SCHEMA_READER = "body.reader.v1"
SCHEMA_COMPARISON = "body.shadow_comparison.v1"

MODE_ENV = "CHIYO_BODY_READER_MODE"
TEST_ONLY_ENV = "CHIYO_BODY_READER_TEST_ONLY"

#: M13A production authorisation.  Both must be set deliberately for canonical
#: reads outside tests; neither is a test flag.
PRODUCTION_AUTHORITY_ENV = "CHIYO_WORLD_BODY_AUTHORITY"
PRODUCTION_AUTHORITY_VALUE = "production"
RUNTIME_MODE_ENV = "BODY_RUNTIME_MODE"
RUNTIME_MODE_CANONICAL = "canonical"

MODE_LEGACY = "legacy"
MODE_SHADOW = "shadow"
MODE_CANONICAL = "canonical"
MODES = (MODE_LEGACY, MODE_SHADOW, MODE_CANONICAL)

#: The legacy body state, read-only.
LEGACY_STATE_RELATIVE = ("xinxi", "current_state.json")

#: HeartMind stores 0..100; M8 stores 0..1.  All conversion lives here.
LEGACY_SCALE = 100.0

COMPARED_FIELDS = ("fatigue", "energy", "reserve")

SEMANTIC_NOTE = "semantic equivalence NOT expected (legacy mixes mood/drive/cycle/weather)"

#: Canonical freshness.  A canonical reader must never present a frozen state as
#: "now" -- that is exactly the failure mode the legacy HeartMind had (its last
#: tick was 19.4 days before cutover).
FRESHNESS_MAX_AGE_HOURS = 12.0

FRESHNESS_FRESH = "fresh"
FRESHNESS_STALE = "stale"
FRESHNESS_UNAVAILABLE = "unavailable"
FRESHNESS_UNVERIFIED = "unverified"

STATUS_OK = "OK"
STATUS_STALE = "BODY_STATE_STALE"
STATUS_UNAVAILABLE = "BODY_STATE_UNAVAILABLE"
STATUS_UNVERIFIED = "OK_UNVERIFIED"
STATUS_LEGACY = "LEGACY_READ"


class BodyReaderError(ValueError):
    """The reader contract was violated."""


class BodyStateStaleError(BodyReaderError):
    """Canonical body state exceeded its freshness bound."""


class BodyStateUnavailableError(BodyReaderError):
    """Canonical body state is not available.  Never falls back to HeartMind."""


def _flag(environment: Mapping[str, str], name: str) -> bool:
    raw = environment.get(name)
    return str(raw).strip().lower() in {"1", "true", "yes", "on"} if raw else False


def production_canonical_configured(
    environment: Optional[Mapping[str, str]] = None,
) -> bool:
    """Is canonical explicitly authorised for production?

    Added by M13A (ticket sections 11/12).  The shadow-era gate only allowed
    canonical behind ``CHIYO_BODY_READER_TEST_ONLY``, so production had no legal
    way in, and faking it with TEST_ONLY was the thing we refused to do.

    This is the deliberate production switch instead.  It is **not** a test flag
    and it does not weaken fail-closed behaviour: when it is absent, canonical
    is still downgraded to legacy rather than silently taken.
    """

    env = environment if environment is not None else os.environ
    authority = str(env.get(PRODUCTION_AUTHORITY_ENV) or "").strip().lower()
    runtime_mode = str(env.get(RUNTIME_MODE_ENV) or "").strip().lower()
    return (
        authority == PRODUCTION_AUTHORITY_VALUE
        and runtime_mode == RUNTIME_MODE_CANONICAL
    )


def resolve_mode(environment: Optional[Mapping[str, str]] = None) -> str:
    """Return the effective reader mode.

    Canonical is allowed when either an explicit production authorisation is
    configured (M13A) or the test-only flag is set.  Otherwise it is downgraded
    to legacy.
    """

    env = environment if environment is not None else os.environ
    requested = str(env.get(MODE_ENV) or MODE_LEGACY).strip().lower()
    if requested not in MODES:
        raise BodyReaderError(f"{MODE_ENV} must be one of {list(MODES)}")
    if requested == MODE_CANONICAL:
        if production_canonical_configured(env):
            return MODE_CANONICAL
        if not _flag(env, TEST_ONLY_ENV):
            # production must never silently become canonical
            return MODE_LEGACY
    return requested


def legacy_state_path(hermes_home: Optional[Path | str] = None) -> Path:
    home = Path(
        hermes_home
        if hermes_home is not None
        else os.environ.get("HERMES_HOME") or (Path.home() / ".hermes")
    )
    return home.joinpath(*LEGACY_STATE_RELATIVE)


def load_legacy_body(hermes_home: Optional[Path | str] = None) -> Optional[dict[str, Any]]:
    """Read the legacy body values.  Read-only; never writes."""

    path = legacy_state_path(hermes_home)
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, Mapping):
        return None

    def _number(key: str) -> Optional[float]:
        value = raw.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        return float(value)

    return {
        "source": "heartmind_legacy",
        "scale": "0..100",
        "fatigue": _number("fatigue"),
        "energy": _number("energy"),
        "reserve": _number("reserve"),
        "present_fields": sorted(
            key for key in COMPARED_FIELDS if raw.get(key) is not None
        ),
    }


# --------------------------------------------------------------------------
# scale conversion (centralised)
# --------------------------------------------------------------------------


def legacy_to_canonical(value: Optional[float]) -> Optional[float]:
    """0..100 -> 0..1.  The only place this conversion is allowed to happen."""

    if value is None:
        return None
    return min(1.0, max(0.0, float(value) / LEGACY_SCALE))


def canonical_to_legacy(value: Optional[float]) -> Optional[float]:
    """0..1 -> 0..100.  The only place this conversion is allowed to happen."""

    if value is None:
        return None
    return min(LEGACY_SCALE, max(0.0, float(value) * LEGACY_SCALE))


# --------------------------------------------------------------------------
# the reader
# --------------------------------------------------------------------------


def assess_freshness(
    runtime_state: Mapping[str, Any],
    *,
    now: Any = None,
    max_age_hours: float = FRESHNESS_MAX_AGE_HOURS,
) -> dict[str, Any]:
    """Is the canonical body state still current?

    The legacy HeartMind read a JSON file that had not been updated for 19.4
    days and every consumer treated it as "now".  Canonical mode must never do
    that: if the caller injects a reference instant and the state is older than
    the bound, the body is reported ``stale`` -- not silently returned.

    ``now`` is injected by the caller.  With no reference instant the reader
    reports ``unverified`` rather than pretending the state is fresh.
    """

    import body_runtime as br

    state = br.validate_runtime(runtime_state)
    last = state.get("last_advanced_at")
    result: dict[str, Any] = {
        "last_advanced_at": last,
        "state_version": state.get("version"),
        "runtime_version": state.get("runtime_version"),
        "max_age_hours": max_age_hours,
        "reference_now": None,
        "age_hours": None,
    }
    if last is None:
        result["freshness"] = FRESHNESS_UNAVAILABLE
        result["reason"] = "canonical state has never been advanced"
        return result
    if now is None:
        result["freshness"] = FRESHNESS_UNVERIFIED
        result["reason"] = "no reference instant injected; freshness not verifiable"
        return result

    reference = br._instant(now, "now")
    advanced = br._instant(last, "last_advanced_at")
    age_hours = (reference - advanced).total_seconds() / 3600.0
    result["reference_now"] = br._iso(reference)
    result["age_hours"] = round(age_hours, 6)
    if age_hours > max_age_hours:
        result["freshness"] = FRESHNESS_STALE
        result["reason"] = (
            f"canonical state is {age_hours:.2f}h old, above the {max_age_hours}h bound"
        )
    else:
        result["freshness"] = FRESHNESS_FRESH
        result["reason"] = "within the freshness bound"
    return result


def read_body(
    *,
    hermes_home: Optional[Path | str] = None,
    runtime_state: Optional[Mapping[str, Any]] = None,
    environment: Optional[Mapping[str, str]] = None,
    now: Any = None,
    require_canonical: bool = False,
) -> dict[str, Any]:
    """Read the body through the adapter, honouring the mode.

    Canonical mode **fails closed**: if the M8 runtime state is unavailable or
    stale it returns no body data and never falls back to HeartMind.
    """

    env = environment if environment is not None else os.environ
    mode = resolve_mode(env)

    legacy = load_legacy_body(hermes_home)
    projection = None
    if runtime_state is not None:
        import body_compatibility as bc

        projection = bc.project_legacy_body(runtime_state)

    payload: dict[str, Any] = {
        "kind": "body_reader_result",
        "schema_version": SCHEMA_READER,
        "mode": mode,
        "default_mode_is_legacy": True,
        "scale_of_returned_values": "0..100",
        "source": None,
        "body": None,
        "comparison": None,
        "readable": False,
        "freshness": None,
        "status": None,
        "fell_back_to_heartmind": False,
    }

    if mode == MODE_CANONICAL:
        payload["note"] = "canonical mode: canonical body authority only"
        if runtime_state is None or projection is None:
            payload["freshness"] = FRESHNESS_UNAVAILABLE
            payload["status"] = STATUS_UNAVAILABLE
            payload["reason"] = "canonical body runtime state is unavailable"
            payload["fail_closed"] = True
            if require_canonical:
                raise BodyStateUnavailableError(
                    "canonical body state unavailable; refusing to fall back to HeartMind"
                )
            return payload

        freshness = assess_freshness(runtime_state, now=now)
        payload["freshness"] = freshness["freshness"]
        payload["freshness_detail"] = freshness

        if freshness["freshness"] in (FRESHNESS_STALE, FRESHNESS_UNAVAILABLE):
            payload["status"] = STATUS_STALE
            payload["reason"] = freshness["reason"]
            payload["fail_closed"] = True
            if require_canonical:
                raise BodyStateStaleError(freshness["reason"])
            return payload

        payload["source"] = "m8_body_runtime"
        payload["body"] = {key: projection[f"{key}_0_100"] for key in COMPARED_FIELDS}
        payload["readable"] = True
        payload["status"] = (
            STATUS_OK
            if freshness["freshness"] == FRESHNESS_FRESH
            else STATUS_UNVERIFIED
        )
        payload["fail_closed"] = False
        return payload

    # legacy and shadow both RETURN legacy
    payload["source"] = "heartmind_legacy"
    payload["body"] = {key: legacy[key] for key in COMPARED_FIELDS} if legacy else None
    payload["readable"] = legacy is not None
    payload["status"] = STATUS_LEGACY if legacy else STATUS_UNAVAILABLE
    payload["freshness"] = FRESHNESS_UNVERIFIED
    if mode == MODE_SHADOW:
        payload["comparison"] = compare_shadow(legacy, projection)
    return payload


# --------------------------------------------------------------------------
# shadow comparison  (read-only)
# --------------------------------------------------------------------------


def compare_shadow(
    legacy: Optional[Mapping[str, Any]],
    projection: Optional[Mapping[str, Any]],
    *,
    tolerance_0_100: float = 15.0,
) -> dict[str, Any]:
    """Compare the two sides for *consumer analysis*, not for fitting."""

    report: dict[str, Any] = {
        "kind": "body_shadow_comparison",
        "schema_version": SCHEMA_COMPARISON,
        "semantic_note": SEMANTIC_NOTE,
        "read_only": True,
        "writes_legacy_state": False,
        "writes_body_state": False,
        "legacy_available": legacy is not None,
        "projection_available": projection is not None,
        "fields": {},
        "consumer_classification": {},
    }

    if legacy is None or projection is None:
        report["status"] = "insufficient_data"
        report["missing"] = [
            name for name, value in (("legacy", legacy), ("projection", projection))
            if value is None
        ]
        return report

    deltas: dict[str, Optional[float]] = {}
    for key in COMPARED_FIELDS:
        left = legacy.get(key)
        right = projection.get(f"{key}_0_100")
        if left is None or right is None:
            report["fields"][key] = {"legacy": left, "projection": right, "delta": None}
            deltas[key] = None
            continue
        delta = float(right) - float(left)
        deltas[key] = delta
        report["fields"][key] = {
            "legacy_0_100": round(float(left), 6),
            "projection_0_100": round(float(right), 6),
            "delta": round(delta, 6),
            "within_tolerance": abs(delta) <= tolerance_0_100,
        }

    report["status"] = "compared"
    report["max_abs_delta"] = (
        round(max(abs(v) for v in deltas.values() if v is not None), 6)
        if any(v is not None for v in deltas.values())
        else None
    )
    report["interpretation"] = {
        "close_match": "the consumer probably only needs clean body semantics",
        "large_delta": (
            "the consumer likely depends on legacy mixed semantics "
            "(mood/drive/sensitivity/cycle/weather) that M8 does not own"
        ),
        "tolerance_used": tolerance_0_100,
    }

    # Which future consumers need which treatment.
    report["consumer_classification"] = {
        "clean_body_only": [
            {"consumer": "dashboard_api.py:248-260", "evidence": "display of raw values"},
        ],
        "needs_scale_fix": [
            {"consumer": "life_loop.py:405-407,774-777", "evidence": "0..100 thresholds"},
            {"consumer": "life_choice.py:515-517", "evidence": "0..100 cost metric"},
            {"consumer": "trust_report.py:56-68", "evidence": "display + heat (desire)"},
        ],
        "depends_on_mixed_semantics": [
            {"consumer": "xinxi_tick.py:270-272", "evidence": "sensitivity -> mood coordinates"},
            {"consumer": "plugin/__init__.py:730,834-845", "evidence": "legacy body prose"},
            {"consumer": "pending_self_reply.py:253,355-373", "evidence": "raw body JSON"},
            {"consumer": "pending_reattention.py:177", "evidence": "raw body JSON"},
            {"consumer": "communication_autonomy.py:429", "evidence": "raw body JSON"},
        ],
    }
    return report


def legacy_reader_is_default(environment: Optional[Mapping[str, str]] = None) -> bool:
    return resolve_mode(environment) == MODE_LEGACY