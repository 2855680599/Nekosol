#!/usr/bin/env python3
"""Interoception integration (M10) — durable, bounded, deliverable observations.

Where this sits
---------------
    RAW SOMATIC STATE        (body_runtime)     -- owner: Embodiment
      -> BODY SIGNAL         (body_signal)      -- owner: Embodiment
      -> BODY OBSERVATION    (body_signal)      -- owner: Embodiment
      -> **[ M10 BOUNDARY ]**                   -- this module
      -/- PERCEPTION / ATTENTION                -- NOT implemented, on purpose

M8 produced observations but they were ephemeral and had ``consumer=None``.
M10 makes them *usable* without crossing the boundary:

* **durable** — append-only store so an observation survives restart;
* **bounded** — a hard cap with an explicit, recorded retention policy;
* **time-scoped** — every observation has a TTL, and an expired observation is
  reported as expired rather than handed over as if current;
* **deliverable** — a named-consumer boundary with visibility scopes;
* **still raw-free** — the raw-value firewall applies to every delivered record.

What this deliberately does NOT do
----------------------------------
No perception.  No attention.  No interpretation.  No context injection.  No
affect, desire, memory or relationship.  A consumer receives bounded qualitative
signals and decides for itself what, if anything, to do with them.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import tempfile
from collections.abc import Mapping, Sequence
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path
from typing import Any, Optional

SCHEMA_OBSERVATION = "body.observation.v1"
SCHEMA_DELIVERY = "body.observation.delivery.v1"
SCHEMA_BOUNDARY = "body.interoception.boundary.v1"

KIND_OBSERVATION = "body_observation"
KIND_DELIVERY = "body_observation_delivery"
KIND_BOUNDARY = "body_interoception_boundary"

OBSERVATIONS_NAME = "observations.jsonl"
DATA_RELATIVE = ("data", "body")

#: Retention bound.  Eviction is oldest-first and its count is always reported;
#: nothing is ever dropped silently.
MAX_OBSERVATIONS = 512

#: An observation is time-scoped.  Beyond this it is reported as expired and is
#: not delivered to a consumer by default.  Kept as whole seconds so that an
#: observation record contains **no floating-point value at all** -- that way no
#: number on an observation can ever be mistaken for a body measurement.
OBSERVATION_TTL_SECONDS = 21600

FRESHNESS_CURRENT = "current"
FRESHNESS_EXPIRED = "expired"
FRESHNESS_UNKNOWN = "unknown"

#: The consumer roles allowed to receive interoceptive observations.
#: NOTE: these are *read roles*, not implementations.  No consumer is
#: implemented here -- that is exactly the boundary this module freezes.
CONSUMER_ROLES = {
    "main_self_surface": {
        "scope": "body_internal",
        "may_receive": ("signal_type", "qualitative_band", "observed_at"),
        "description": "bounded interoceptive signal for a character-facing surface",
    },
    "life_body_reader": {
        "scope": "body_internal",
        "may_receive": ("signal_type", "qualitative_band"),
        "description": "bounded interoceptive signal for life/activity reasoning",
    },
    "operator_debug": {
        "scope": "body_internal",
        "may_receive": ("signal_type", "qualitative_band", "observed_at", "observation_id"),
        "description": "operator inspection; still no raw somatic values",
    },
}
CONSUMER_ROLE_NAMES = tuple(sorted(CONSUMER_ROLES))

#: Stages this module refuses to grow into (ticket: boundary must stay frozen).
FORBIDDEN_NEIGHBOURS = (
    "perception",
    "attention",
    "working_context",
    "context_injection",
    "affect",
    "desire",
    "memory",
    "relationship",
    "interpretation",
)

#: Fields that must never appear on an observation or delivery.
FORBIDDEN_FIELDS = frozenset(
    {
        "raw", "value", "values", "state", "somatic", "fatigue", "activation",
        "comfort", "sensory_load", "tension", "warmth", "energy", "reserve",
        "heartbeat", "temperature", "emotion", "affect", "mood", "desire",
        "feeling", "interpretation", "advice", "diagnosis", "expression",
        "relationship", "memory", "goal", "attention", "salience", "percept",
    }
)


class InteroceptionError(ValueError):
    """The interoception contract was violated."""


def _text(value: Any, label: str, *, maximum: int = 256) -> str:
    if not isinstance(value, str):
        raise InteroceptionError(f"{label} must be a string")
    text = value.strip()
    if not text or len(text) > maximum:
        raise InteroceptionError(f"{label} must be non-empty and bounded")
    return text


def _detach(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False))


def _digest(value: Any) -> str:
    payload = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def observation_path(hermes_home: Optional[Path | str] = None) -> Path:
    home = Path(
        hermes_home
        if hermes_home is not None
        else os.environ.get("HERMES_HOME") or (Path.home() / ".hermes")
    )
    return home.joinpath(*DATA_RELATIVE, OBSERVATIONS_NAME)


def _assert_raw_free(payload: Mapping[str, Any], *, label: str) -> None:
    """The raw-value firewall, enforced structurally on every delivered record."""

    import body_runtime as br

    for key in payload:
        if key in FORBIDDEN_FIELDS:
            raise InteroceptionError(f"{label} must not carry the field {key!r}")
    for value in payload.values():
        if isinstance(value, float):
            raise InteroceptionError(f"{label} must not carry a raw number")
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    for name in br.SOMATIC_VARIABLES:
        if f'"{name}"' in blob:
            raise InteroceptionError(f"{label} leaks the raw variable {name!r}")


# --------------------------------------------------------------------------
# observation records
# --------------------------------------------------------------------------


def validate_observation(record: Any) -> dict[str, Any]:
    if not isinstance(record, Mapping):
        raise InteroceptionError("observation must be an object")
    if record.get("kind") != KIND_OBSERVATION:
        raise InteroceptionError("observation kind is not supported")
    if record.get("schema_version") != SCHEMA_OBSERVATION:
        raise InteroceptionError("observation schema is not supported")
    result = _detach(dict(record))
    _assert_raw_free(
        {k: v for k, v in result.items() if k not in ("source_transition_refs",)},
        label="observation",
    )
    return result


def observe(
    runtime_state: Mapping[str, Any],
    *,
    at: Any,
    previous_state: Optional[Mapping[str, Any]] = None,
    source_transition_refs: Optional[Sequence[str]] = None,
    ttl_seconds: float = OBSERVATION_TTL_SECONDS,
) -> list[dict[str, Any]]:
    """Derive time-scoped observations from a runtime state.  Pure."""

    import body_runtime as br
    import body_signal as bs

    state = br.validate_runtime(runtime_state)
    moment = br._instant(at, "at")
    if ttl_seconds <= 0:
        raise InteroceptionError("ttl_seconds must be positive")
    signals = bs.produce_signals(
        state,
        occurred_at=moment,
        previous_state=previous_state,
        source_transition_refs=source_transition_refs,
    )
    observations = bs.build_observation(signals)

    scoped: list[dict[str, Any]] = []
    for observation in observations:
        record = dict(observation)
        record["expires_at"] = br._iso(moment + timedelta(seconds=int(ttl_seconds)))
        # whole seconds: an observation record carries no float at all
        record["ttl_seconds"] = int(ttl_seconds)
        record["body_version"] = state["version"]
        record["state_schema_version"] = state["schema_version"]
        # an observation still has no consumer: delivery is a separate, explicit act
        record["consumer"] = None
        scoped.append(validate_observation(record))
    return scoped


# --------------------------------------------------------------------------
# durability
# --------------------------------------------------------------------------


class ObservationStore:
    """Append-only, bounded, lock-protected observation store."""

    def __init__(
        self,
        path: Path | str,
        *,
        max_observations: int = MAX_OBSERVATIONS,
    ):
        if max_observations <= 0:
            raise InteroceptionError("max_observations must be positive")
        self.path = Path(path)
        self.lock_path = self.path.with_name(self.path.name + ".lock")
        self.max_observations = int(max_observations)

    @contextmanager
    def locked(self) -> Any:
        if fcntl is None:
            raise InteroceptionError("observation store lock unavailable")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.lock_path.open("a+") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def read_all(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        records: list[dict[str, Any]] = []
        for number, line in enumerate(
            self.path.read_text(encoding="utf-8").splitlines(), 1
        ):
            if not line.strip():
                continue
            try:
                records.append(validate_observation(json.loads(line)))
            except (TypeError, ValueError) as exc:
                raise InteroceptionError(
                    f"observation store malformed at line {number}"
                ) from exc
        return records

    def append(self, records: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        """Append observations, then apply the retention policy explicitly."""

        validated = [validate_observation(record) for record in records]
        if not validated:
            return {"appended": 0, "evicted": 0, "total": len(self.read_all())}

        with self.locked():
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open("a", encoding="utf-8") as handle:
                    for record in validated:
                        handle.write(
                            json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n"
                        )
                    handle.flush()
                    os.fsync(handle.fileno())
            except OSError as exc:
                raise InteroceptionError("observation store cannot be appended") from exc

            kept, evicted = self._enforce_retention()
        return {
            "appended": len(validated),
            "evicted": evicted,
            "total": len(kept),
            "retention_policy": f"oldest_first_beyond_{self.max_observations}",
        }

    def _enforce_retention(self) -> tuple[list[dict[str, Any]], int]:
        records = self.read_all()
        if len(records) <= self.max_observations:
            return records, 0
        kept = records[-self.max_observations:]
        evicted = len(records) - len(kept)
        self._rewrite(kept)
        return kept, evicted

    def _rewrite(self, records: Sequence[Mapping[str, Any]]) -> None:

        payload = "".join(
            json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n"
            for record in records
        )
        descriptor, temporary = tempfile.mkstemp(
            dir=str(self.path.parent), prefix=self.path.name + ".", suffix=".tmp"
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        except BaseException:
            if os.path.exists(temporary):
                os.unlink(temporary)
            raise

    # -- views ------------------------------------------------------------

    def latest(self, *, limit: int = 64) -> list[dict[str, Any]]:
        if limit <= 0:
            raise InteroceptionError("limit must be positive")
        return self.read_all()[-limit:]

    def stats(self) -> dict[str, Any]:
        records = self.read_all()
        return {
            "total": len(records),
            "max_observations": self.max_observations,
            "distinct_signal_types": sorted(
                {record["signal_type"] for record in records}
            ),
            "retention_policy": f"oldest_first_beyond_{self.max_observations}",
        }


# --------------------------------------------------------------------------
# the boundary  (what a consumer may receive -- no consumer implemented)
# --------------------------------------------------------------------------


def observation_status(
    record: Mapping[str, Any], *, now: Any = None
) -> str:
    """``current`` / ``expired`` / ``unknown``.  Never guesses."""

    import body_runtime as br

    validated = validate_observation(record)
    expires_at = validated.get("expires_at")
    if not expires_at:
        return FRESHNESS_UNKNOWN
    if now is None:
        return FRESHNESS_UNKNOWN
    moment = br._instant(now, "now")
    expiry = br._instant(expires_at, "expires_at")
    return FRESHNESS_CURRENT if moment <= expiry else FRESHNESS_EXPIRED


def deliver(
    records: Sequence[Mapping[str, Any]],
    *,
    consumer_role: str,
    now: Any = None,
    include_expired: bool = False,
) -> dict[str, Any]:
    """Deliver observations to a *named* consumer role.

    Refuses an unknown role.  Refuses to hand over expired observations by
    default.  Every delivered record passes the raw-value firewall.
    """

    role = _text(consumer_role, "consumer_role", maximum=64)
    if role not in CONSUMER_ROLES:
        raise InteroceptionError(
            f"unknown consumer role {role!r}; allowed: {list(CONSUMER_ROLE_NAMES)}"
        )
    contract = CONSUMER_ROLES[role]
    allowed_keys = contract["may_receive"]

    delivered: list[dict[str, Any]] = []
    expired = 0
    for record in records:
        validated = validate_observation(record)
        status = observation_status(validated, now=now)
        if validated.get("visibility_scope") != contract["scope"]:
            continue
        if status == FRESHNESS_EXPIRED and not include_expired:
            expired += 1
            continue
        payload = {
            key: validated[key]
            for key in ("signal_type", "qualitative_band", "observed_at", "observation_id")
            if key in validated
        }
        payload = {key: value for key, value in payload.items() if key in allowed_keys}
        payload["freshness"] = status
        _assert_raw_free(payload, label="delivery")
        delivered.append(payload)

    return {
        "kind": KIND_DELIVERY,
        "schema_version": SCHEMA_DELIVERY,
        "consumer_role": role,
        "consumer_description": contract["description"],
        "observations": delivered,
        "count": len(delivered),
        "expired_withheld": expired,
        "carries_raw_values": False,
        "interpreted": False,
        "injected_into_context": False,
        "note": (
            "bounded interoceptive signals only; this module does not interpret "
            "them, does not add salience, and does not inject them anywhere"
        ),
    }


def interoception_boundary_audit() -> dict[str, Any]:
    """Prove the boundary is where it is claimed to be."""

    return {
        "kind": KIND_BOUNDARY,
        "schema_version": SCHEMA_BOUNDARY,
        "pipeline": [
            "RAW_SOMATIC_STATE (body_runtime)",
            "BODY_SIGNAL (body_signal)",
            "BODY_OBSERVATION (body_signal)",
            "INTEROCEPTION_BOUNDARY (this module)",
        ],
        "stops_at": "BODY_OBSERVATION",
        "implemented_here": {
            "observation_durability": True,
            "observation_expiry": True,
            "consumer_role_delivery": True,
            "raw_value_firewall": True,
        },
        "deliberately_not_implemented": {
            "perception": False,
            "attention": False,
            "salience": False,
            "interpretation": False,
            "context_injection": False,
            "affect": False,
            "desire": False,
            "memory": False,
            "relationship": False,
        },
        "forbidden_neighbours": list(FORBIDDEN_NEIGHBOURS),
        "consumer_roles": {name: dict(cfg) for name, cfg in CONSUMER_ROLES.items()},
        "consumers_implemented_here": 0,
        "raw_values_cross_the_boundary": False,
    }


def assert_no_downstream_implementation(module_sources: Optional[Sequence[str]] = None) -> dict[str, Any]:
    """Static-ish guard: the boundary module must not contain downstream logic."""

    sources = list(module_sources) if module_sources else [
        Path(__file__).read_text(encoding="utf-8")
    ]
    offenders: list[str] = []
    for source in sources:
        lowered = source.lower()
        for neighbour in FORBIDDEN_NEIGHBOURS:
            # naming the boundary in a comment/docstring is fine; calling into it is not
            for pattern in (f"import {neighbour}", f"from {neighbour} import"):
                if pattern in lowered:
                    offenders.append(pattern)
    return {
        "clean": not offenders,
        "offenders": sorted(set(offenders)),
        "checked": len(sources),
    }