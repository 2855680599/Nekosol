#!/usr/bin/env python3
"""Production configuration for the standalone World/Body service (M13A).

One job: decide, explicitly and verifiably, *which* runtime and *which* home
this process owns.

M13 was blocked (B2) because the live Hermes gateway's ``HERMES_HOME`` carried
two meanings at once -- "the agent's home" and "the world's home".  This module
removes that conflation:

* the canonical switch is ``CHIYO_WORLD_HOME`` (explicit, absolute, validated);
* the live gateway home is a **refused** value, not a default;
* ``HERMES_HOME`` is only ever *echoed* into this process so that legacy World
  modules -- which know nothing but ``HERMES_HOME`` -- keep working, and it is
  never exported system-wide.

Nothing here writes to disk.  Loading a config is pure.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional

SCHEMA_CONFIG = "world.body.service.config.v1"

SERVICE_NAME = "chiyo-world-body.service"

WORLD_HOME_ENV = "CHIYO_WORLD_HOME"
RUNTIME_ROOT_ENV = "CHIYO_WORLD_RUNTIME"
RUNTIME_MODE_ENV = "BODY_RUNTIME_MODE"
AUTHORITY_ENV = "CHIYO_WORLD_BODY_AUTHORITY"
READER_MODE_ENV = "CHIYO_BODY_READER_MODE"
RUNTIME_ENABLED_ENV = "CHIYO_BODY_RUNTIME_ENABLED"
#: The World-side embodiment substrate gate.  The Resolver disables every World
#: action while this is off, so the service owns it: canonical opens it, shadow
#: leaves it closed.  (Ticket section 15.)
SUBSTRATE_ENABLED_ENV = "CHIYO_WORLD_BODY_SUBSTRATE_ENABLED"
#: The World engine's own gate.  The Resolver reports FEATURE_DISABLED while it
#: is off, so canonical must open it too -- "World authority = ON".
WORLD_ENABLED_ENV = "CHIYO_WORLD_LIVING_SKELETON_ENABLED"

DEFAULT_RUNTIME_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_WORLD_HOME = (Path(__file__).resolve().parents[2] / "data/world").resolve()

#: The home the live Hermes gateway owns.  Never a legal World/Body home.
LIVE_GATEWAY_HOME = (Path(__file__).resolve().parents[2] / "data/persona").resolve()
LIVE_GATEWAY_CODE = (Path(__file__).resolve().parents[2] / "native").resolve()

MODE_SHADOW = "shadow"
MODE_CANONICAL = "canonical"
MODES = (MODE_SHADOW, MODE_CANONICAL)

AUTHORITY_PRODUCTION = "production"
AUTHORITY_SHADOW = "shadow"
AUTHORITIES = (AUTHORITY_SHADOW, AUTHORITY_PRODUCTION)

#: The World this service is allowed to own.  A mismatch is a refusal, not a
#: silent adoption of whatever happens to be on disk.
WORLD_ID_ENV = "CHIYO_WORLD_ID"
DEFAULT_WORLD_ID = "shiomi_city"

WORLD_STATE_RELATIVE = ("data", "world", "world_state.json")
WORLD_TIMELINE_RELATIVE = ("data", "world", "world_timeline.json")
LEDGER_RELATIVE = ("data", "life_execution.jsonl")
BODY_RELATIVE = ("data", "body")
RUN_RELATIVE = ("run",)

#: Environment keys this process is allowed to set for itself.
OWNED_ENV_KEYS = (
    "HERMES_HOME",
    WORLD_HOME_ENV,
    RUNTIME_ROOT_ENV,
    RUNTIME_MODE_ENV,
    AUTHORITY_ENV,
    READER_MODE_ENV,
    RUNTIME_ENABLED_ENV,
    SUBSTRATE_ENABLED_ENV,
    WORLD_ENABLED_ENV,
)


class ConfigError(ValueError):
    """The production configuration is not usable.  Never silently repaired."""


def _text(value: Any, label: str, *, maximum: int = 512) -> str:
    if isinstance(value, os.PathLike):
        value = os.fspath(value)
    if not isinstance(value, str) or not value.strip():
        raise ConfigError("%s must be a non-empty string" % label)
    text = value.strip()
    if len(text) > maximum:
        raise ConfigError("%s is too long" % label)
    return text


def _absolute(value: Any, label: str) -> Path:
    text = _text(value, label)
    path = Path(text).expanduser()
    if not path.is_absolute():
        raise ConfigError("%s must be absolute, got %r" % (label, text))
    return path


def _require_directory(path: Path, label: str) -> None:
    if not path.exists():
        raise ConfigError("%s does not exist: %s" % (label, path))
    if not path.is_dir():
        raise ConfigError("%s is not a directory: %s" % (label, path))


@dataclass(frozen=True)
class ServiceConfig:
    """Resolved, validated production identity for one service instance."""

    runtime_root: Path
    world_home: Path
    runtime_mode: str
    runtime_mode: str
    authority: str
    expected_world_id: str
    environment: Mapping[str, str]

    # ---- derived paths ---------------------------------------------------
    @property
    def scripts_dir(self) -> Path:
        return self.runtime_root / "scripts"

    @property
    def venv_python(self) -> Path:
        return self.runtime_root / "venv" / "bin" / "python"

    @property
    def world_state_path(self) -> Path:
        return self.world_home.joinpath(*WORLD_STATE_RELATIVE)

    @property
    def world_timeline_path(self) -> Path:
        return self.world_home.joinpath(*WORLD_TIMELINE_RELATIVE)

    @property
    def ledger_path(self) -> Path:
        return self.world_home.joinpath(*LEDGER_RELATIVE)

    @property
    def body_dir(self) -> Path:
        return self.world_home.joinpath(*BODY_RELATIVE)

    @property
    def run_dir(self) -> Path:
        return self.world_home.joinpath(*RUN_RELATIVE)

    # ---- mode ------------------------------------------------------------
    @property
    def canonical(self) -> bool:
        return self.runtime_mode == MODE_CANONICAL

    @property
    def production_authority(self) -> bool:
        return self.authority == AUTHORITY_PRODUCTION

    # ---- environment -----------------------------------------------------
    def owned_environment(self) -> dict[str, str]:
        """The exact environment this process installs for itself.

        ``HERMES_HOME`` is included only so legacy World modules resolve; it
        points at the *World* home, never at the live gateway home.
        """

        return {
            "HERMES_HOME": str(self.world_home),
            WORLD_HOME_ENV: str(self.world_home),
            WORLD_ID_ENV: self.expected_world_id,
            RUNTIME_MODE_ENV: self.runtime_mode,
            AUTHORITY_ENV: self.authority,
            READER_MODE_ENV: "canonical" if self.canonical else "shadow",
            RUNTIME_ENABLED_ENV: "1",
            SUBSTRATE_ENABLED_ENV: "1" if self.canonical else "0",
            WORLD_ENABLED_ENV: "1" if self.canonical else "0",
        }

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_CONFIG,
            "service_name": SERVICE_NAME,
            "runtime_root": str(self.runtime_root),
            "scripts_dir": str(self.scripts_dir),
            "world_home": str(self.world_home),
            "runtime_mode": self.runtime_mode,
            "authority": self.authority,
            "authority": self.authority,
            "expected_world_id": self.expected_world_id,
            "world_state_path": str(self.world_state_path),
            "world_timeline_path": str(self.world_timeline_path),
            "ledger_path": str(self.ledger_path),
            "body_dir": str(self.body_dir),
            "run_dir": str(self.run_dir),
            "live_gateway_home": str(LIVE_GATEWAY_HOME),
            "live_gateway_home_used_as_world_home": self.world_home == LIVE_GATEWAY_HOME,
        }


def load_config(
    environment: Optional[Mapping[str, str]] = None,
    *,
    runtime_root: Optional[Path | str] = None,
    world_home: Optional[Path | str] = None,
    runtime_mode: Optional[str] = None,
    authority: Optional[str] = None,
    world_id: Optional[str] = None,
    require_existing: bool = True,
) -> ServiceConfig:
    """Resolve and validate the production configuration.

    Fails closed on anything ambiguous.  It never picks a default World home
    silently: the defaults below are *documented* production values, and any
    disagreement with the environment is an error rather than a winner.
    """

    env = dict(environment if environment is not None else os.environ)

    root_raw = runtime_root if runtime_root is not None else env.get(RUNTIME_ROOT_ENV)
    home_raw = world_home if world_home is not None else env.get(WORLD_HOME_ENV)
    mode_raw = runtime_mode if runtime_mode is not None else env.get(RUNTIME_MODE_ENV)

    root = _absolute(root_raw, RUNTIME_ROOT_ENV) if root_raw else DEFAULT_RUNTIME_ROOT
    home = _absolute(home_raw, WORLD_HOME_ENV) if home_raw else DEFAULT_WORLD_HOME

    mode = _text(mode_raw, RUNTIME_MODE_ENV, maximum=32).lower() if mode_raw else MODE_SHADOW
    if mode not in MODES:
        raise ConfigError("%s must be one of %s" % (RUNTIME_MODE_ENV, list(MODES)))

    authority_raw = authority if authority is not None else env.get(AUTHORITY_ENV)
    if authority_raw:
        authority_value = _text(authority_raw, AUTHORITY_ENV, maximum=32).lower()
        if authority_value not in AUTHORITIES:
            raise ConfigError("%s must be one of %s" % (AUTHORITY_ENV, list(AUTHORITIES)))
    else:
        authority_value = AUTHORITY_PRODUCTION if mode == MODE_CANONICAL else AUTHORITY_SHADOW

    world_id_raw = world_id if world_id is not None else env.get(WORLD_ID_ENV)
    world_id_value = (
        _text(world_id_raw, WORLD_ID_ENV, maximum=64) if world_id_raw else DEFAULT_WORLD_ID
    )

    # ---- refusals: the M13 blast radius must stay impossible -------------
    if home == LIVE_GATEWAY_HOME:
        raise ConfigError(
            "refusing to use the live gateway home as the World/Body home: %s "
            "(this is the B2 conflation)" % home
        )
    if home == LIVE_GATEWAY_CODE:
        raise ConfigError("refusing to use the live gateway code tree: %s" % home)
    for forbidden in (LIVE_GATEWAY_HOME, LIVE_GATEWAY_CODE):
        if forbidden in home.parents:
            raise ConfigError(
                "World/Body home must not live inside the live gateway tree: %s" % home
            )

    if mode == MODE_CANONICAL and authority_value != AUTHORITY_PRODUCTION:
        raise ConfigError(
            "canonical runtime requires %s=%s (got %r)"
            % (AUTHORITY_ENV, AUTHORITY_PRODUCTION, authority_value)
        )

    if require_existing:
        _require_directory(root, "runtime root")
        _require_directory(root / "scripts", "runtime scripts directory")
        _require_directory(home, "World/Body home")

    return ServiceConfig(
        runtime_root=root,
        world_home=home,
        runtime_mode=mode,
        authority=authority_value,
        expected_world_id=world_id_value,
        environment=env,
    )


def apply_process_environment(config: ServiceConfig, *, target: Optional[dict[str, str]] = None) -> dict[str, str]:
    """Install this process's environment.  Process-local by construction.

    Touches only ``OWNED_ENV_KEYS`` and refuses to point ``HERMES_HOME`` anywhere
    but the World/Body home, so a mistake here cannot reach the gateway.
    """

    sink = os.environ if target is None else target
    owned = config.owned_environment()
    if owned["HERMES_HOME"] != str(config.world_home):
        raise ConfigError("owned environment must point HERMES_HOME at the World/Body home")
    for key, value in owned.items():
        sink[key] = value
    return owned


def environment_leaks_into_gateway(environment: Mapping[str, str]) -> list[str]:
    """Which owned keys, if any, would redirect the live gateway tree.

    Used as a guard in tests: the answer must always be an empty list.
    """

    leaks: list[str] = []
    home = environment.get("HERMES_HOME")
    if home and Path(home).expanduser() in (LIVE_GATEWAY_HOME, LIVE_GATEWAY_CODE):
        leaks.append("HERMES_HOME=%s" % home)
    world = environment.get(WORLD_HOME_ENV)
    if world and Path(world).expanduser() in (LIVE_GATEWAY_HOME, LIVE_GATEWAY_CODE):
        leaks.append("%s=%s" % (WORLD_HOME_ENV, world))
    return leaks
