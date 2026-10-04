"""LPC0B section 11: cognition's own configuration.

Authority: the order allows cognition to have its own provider/model/timeout/token/budget/
concurrency/effect settings, but requires transport, authentication, endpoint and error
conventions to be reused from the existing provider client rather than duplicated.

So this module only decides *what* to call and *how much*; `AgencyModelClient` decides
*how* to call it, from the gateway's existing config and secret surface.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Mapping, Optional

COGNITION_CONFIG_ENV = "CHIYO_COGNITION_CONFIG"

DEFAULT_CONFIG_PATH = "./data/life/cognition-config.json"

EFFECT_SHADOW = "shadow"
EFFECT_OFF = "off"

DEFAULT_COGNITION_CONFIG: dict[str, Any] = {
    "enabled": False,
    "provider": None,
    "model": None,
    "timeout_s": 8.0,
    "max_output_tokens": 512,
    "concurrency": 1,
    "max_queue": 64,
    "effect": EFFECT_SHADOW,
    "budget": {
        "max_calls_per_hour": None,
        "max_calls_per_day": None,
        "max_input_tokens_per_day": None,
        "max_output_tokens_per_day": None,
        "emergency_max_calls_per_hour": 60,
        "emergency_max_calls_per_day": 240,
    },
}


def load_cognition_config(path: Optional[Path | str] = None, *, environment=None) -> dict[str, Any]:
    """Read the cognition config, falling back to safe defaults.  Never raises."""
    cfg: dict[str, Any] = json.loads(json.dumps(DEFAULT_COGNITION_CONFIG))
    env = os.environ if environment is None else environment
    candidate = Path(path) if path else Path(env.get(COGNITION_CONFIG_ENV) or DEFAULT_CONFIG_PATH)
    try:
        loaded = json.loads(candidate.read_text(encoding="utf-8"))
        if isinstance(loaded, Mapping):
            for key, value in loaded.items():
                if key == "budget" and isinstance(value, Mapping):
                    cfg["budget"].update(value)
                else:
                    cfg[key] = value
        cfg["config_loaded"] = True
    except (OSError, ValueError):
        cfg["config_loaded"] = False
    cfg["config_path"] = str(candidate)
    return cfg
