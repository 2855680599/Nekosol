"""Public ``NYAIRO_*`` configuration names with ``CHIYO_*`` compatibility.

Why this exists
---------------
The project's public name is nyairo; ``CHIYO_`` is the previous one. Users are told
to set ``NYAIRO_*``, while every reader in this tree still asks for ``CHIYO_*``.
Renaming ~70 read sites would break every existing configuration, deployed service
file and older guide for no functional gain, so the public name is resolved once,
at the configuration boundary instead:

    NYAIRO_XXX   ->   CHIYO_XXX   ->   default

``NYAIRO_XXX`` wins when both are set, a legacy ``CHIYO_XXX`` keeps working
unchanged, and no reader has to know that two spellings exist. Nothing is removed.

How it is applied
-----------------
* :func:`apply` returns a *copy* of a mapping with the legacy names filled in; the
  runtime passes that private mapping down (``Instance``), so ``os.environ`` is
  never mutated -- the same rule the rest of the runtime follows.
* :func:`resolve` is for a module that owns a documented public knob and reads the
  process environment itself.
* The installed launcher (``scripts/hermes.sh``) exports the legacy spelling of any
  ``NYAIRO_*`` variable it was given, which covers the whole process tree including
  components that read ``os.environ`` directly.

Precedence is by name, never by value: an empty ``NYAIRO_XXX`` does not shadow a
set ``CHIYO_XXX``, and a conflict between the two is reported (names only -- never
values, which may be credentials).
"""
from __future__ import annotations

import os
from typing import Mapping

PUBLIC_PREFIX = "NYAIRO_"
LEGACY_PREFIX = "CHIYO_"


def public_name(legacy_name: str) -> str | None:
    """``CHIYO_X`` -> ``NYAIRO_X``; None when the name is not a legacy name."""
    if legacy_name.startswith(LEGACY_PREFIX):
        return PUBLIC_PREFIX + legacy_name[len(LEGACY_PREFIX):]
    return None


def legacy_name(name: str) -> str | None:
    """``NYAIRO_X`` -> ``CHIYO_X``; None when the name is not a public name."""
    if name.startswith(PUBLIC_PREFIX):
        return LEGACY_PREFIX + name[len(PUBLIC_PREFIX):]
    return None


def resolve(legacy: str, env: Mapping[str, str] | None = None) -> str | None:
    """The value for a legacy name, honouring its public twin first.

    ``NYAIRO_X`` is used when it is set to a non-empty value, whether or not
    ``CHIYO_X`` also exists; otherwise ``CHIYO_X`` is returned as-is.
    """
    source = os.environ if env is None else env
    twin = public_name(legacy)
    if twin is not None:
        value = source.get(twin)
        if value is not None and str(value).strip() != "":
            return value
    return source.get(legacy)


def conflicts(env: Mapping[str, str] | None = None) -> list[tuple[str, str]]:
    """``(legacy_name, public_name)`` pairs that are both set to different values."""
    source = os.environ if env is None else env
    found = []
    for name, value in source.items():
        twin = legacy_name(name)
        if twin is None or twin not in source:
            continue
        if str(value).strip() != "" and str(source[twin]).strip() != "" and source[twin] != value:
            found.append((twin, name))
    return sorted(found)


def apply(env: Mapping[str, str] | None = None) -> dict:
    """A copy of *env* in which every legacy name reflects its public twin.

    Pure: ``os.environ`` (or whatever mapping was passed) is not modified. Keys
    that only exist in the legacy spelling are left exactly as they were, and a
    public key is never dropped.
    """
    source = os.environ if env is None else env
    resolved = dict(source)
    for name, value in source.items():
        twin = legacy_name(name)
        if twin is not None:
            resolved[twin] = value
    return resolved
