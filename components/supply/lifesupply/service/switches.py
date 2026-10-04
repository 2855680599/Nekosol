"""PHASE 17 runtime feature switches.

Six switches exist, all defaulting to **OFF**, and nothing in this module ever deletes or
rewrites canonical data.  A switch only decides whether a *new* write is attempted; the
canonical stores, the read API and every existing row are untouched by a switch being OFF.

Environment names accepted (first match wins), all optional:

``LIFE_SUPPLY_SWITCH_<NAME>``, ``LIFE_SUPPLY_<NAME>``, ``<NAME>``

A value is ON only if it is exactly ``ON`` (case/whitespace insensitive).  Anything else --
including an unparsable value -- is OFF, so a broken configuration fails closed.
"""
from __future__ import annotations

import os
from typing import Any, Mapping

ON = "ON"
OFF = "OFF"

SWITCH_NAMES: tuple[str, ...] = (
    "WORKSPACE_PROVISION",
    "INQUIRY_ADMISSION",
    "ARTIFACT_EXTERNAL_ACTION",
    "PERSONAL_CANDIDATE_CONSUMPTION",
    "AUTONOMOUS_WAKE",
    "PROACTIVE_CONTACT",
)

SHIPPED_DEFAULTS: dict[str, str] = {name: OFF for name in SWITCH_NAMES}

#: Which service operations each switch gates.  A switch with an empty tuple exists, is OFF,
#: and gates nothing yet because its owner operation is not staged in this service.
SWITCH_GATES: dict[str, tuple[str, ...]] = {
    "WORKSPACE_PROVISION": ("provision_workspace",),
    "INQUIRY_ADMISSION": ("admit_inquiry",),
    "ARTIFACT_EXTERNAL_ACTION": ("create_artifact", "commit_markdown",
                                 "archive_artifact", "tombstone"),
    "PERSONAL_CANDIDATE_CONSUMPTION": (),
    "AUTONOMOUS_WAKE": (),
    "PROACTIVE_CONTACT": (),
}

_ENV_PREFIXES = ("LIFE_SUPPLY_SWITCH_", "LIFE_SUPPLY_", "")


class UnknownSwitch(KeyError):
    """A switch name outside the frozen six."""


def _normalise(value: Any) -> str:
    if isinstance(value, bool):
        return ON if value else OFF
    text = "" if value is None else str(value).strip().upper()
    return ON if text == ON else OFF


class FeatureSwitches:
    """The loaded switch state.  Immutable in practice: build one, hand it to the service."""

    def __init__(self, values: Mapping[str, Any] | None = None) -> None:
        unknown = sorted(set(values or {}) - set(SWITCH_NAMES))
        if unknown:
            raise UnknownSwitch(f"UNKNOWN_SWITCH:{','.join(unknown)}")
        self._values = dict(SHIPPED_DEFAULTS)
        for name, value in (values or {}).items():
            self._values[name] = _normalise(value)

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "FeatureSwitches":
        source = os.environ if env is None else env
        values: dict[str, Any] = {}
        for name in SWITCH_NAMES:
            for prefix in _ENV_PREFIXES:
                key = f"{prefix}{name}"
                if key in source:
                    values[name] = source[key]
                    break
        return cls(values)

    @classmethod
    def all_on(cls) -> "FeatureSwitches":
        return cls({name: ON for name in SWITCH_NAMES})

    @classmethod
    def all_off(cls) -> "FeatureSwitches":
        return cls(SHIPPED_DEFAULTS)

    def __repr__(self) -> str:  # pragma: no cover - diagnostics only
        return f"FeatureSwitches({self._values!r})"

    def state(self, name: str) -> str:
        if name not in SWITCH_NAMES:
            raise UnknownSwitch(f"UNKNOWN_SWITCH:{name}")
        return self._values[name]

    def enabled(self, name: str) -> bool:
        return self.state(name) == ON

    def as_dict(self) -> dict[str, str]:
        return dict(self._values)

    def is_all_off(self) -> bool:
        return all(state == OFF for state in self._values.values())

    # -- refusal helpers -------------------------------------------------------

    def reason_code(self, name: str) -> str:
        return f"{name}_OFF"

    def refusal(self, name: str) -> dict[str, Any]:
        """The stable refusal every gated operation returns while its switch is OFF."""
        return {"status": "BLOCKED", "reason": self.reason_code(name),
                "switch": name, "detail": f"feature switch {name} is OFF"}

    def require(self, name: str) -> dict[str, Any] | None:
        return None if self.enabled(name) else self.refusal(name)


def load_switches(env: Mapping[str, str] | None = None) -> FeatureSwitches:
    return FeatureSwitches.from_env(env)


def assert_switch_table_complete() -> None:
    """Every shipped switch is described by the gate table and ships OFF."""
    if sorted(SWITCH_GATES) != sorted(SWITCH_NAMES):
        raise UnknownSwitch(f"SWITCH_GATE_TABLE_INCOMPLETE:{sorted(SWITCH_GATES)}")
    enabled = sorted(name for name, state in SHIPPED_DEFAULTS.items() if state != OFF)
    if enabled:
        raise RuntimeError(f"SWITCH_DEFAULTS_NOT_OFF:{','.join(enabled)}")
