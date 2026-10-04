#!/usr/bin/env python3
"""Canonical Relationship Runtime View V0 — RP-1.6.

A read-only read path for the canonical relationship fact, **independent of the memory
master switch** (``memory.memory_enabled`` / ``memory.user_profile_enabled``).

Iron laws kept by this module:

  * READ ONLY. It never writes the record, the config, memory, history or any state.
  * SINGLE OWNER. One canonical record on disk is the only source of truth; the runtime
    view is rendered from it on every prompt assembly.
  * FACT ONLY. The rendered block states what is true about the relationship. It carries
    no tone, no stance, no obligation, no reply template.
  * FAIL CLOSED. A missing, unreadable, malformed or non-active record renders NOTHING —
    never a default assumption such as "partner". Absent source != partner.
  * INDEPENDENT SWITCH. Enabled explicitly; disabling restores the pre-RP-1.6 assembly
    byte-for-byte (the module then contributes no part at all).

Env:
  CHIYO_CANONICAL_RELATIONSHIP        "false"/"0"/"off" disables; "true"/"1"/"on" enables
  CHIYO_CANONICAL_RELATIONSHIP_PATH   override the record path
Config (config.yaml):
  canonical_relationship:
    context_enabled: true
"""
from __future__ import annotations

import hashlib
import logging
import os
from pathlib import Path
from typing import Any, Dict, Optional

_LOGGER = logging.getLogger(__name__)

BLOCK_OPEN = "[canonical relationship]"
BLOCK_CLOSE = "[/canonical relationship]"
SCHEMA = "canonical-relationship-v1"
DEFAULT_RELATIVE_PATH = "canonical/relationship.yaml"

# Facts about the relationship only. Never behaviour, never warmth, never a reply.
_REQUIRED = ("schema", "subject", "counterparty", "relationship_type", "status", "statement", "revision")
_ACTIVE = "active"
_DISPLAY = {"chiyo": "千代", "ruirui": "用户"}

_TRUTH = {True: True, False: False}
for _t in ("true", "always", "yes", "on", "1"):
    _TRUTH[_t] = True
for _f in ("false", "never", "no", "off", "0"):
    _TRUTH[_f] = False


def _looks_true(value: Any) -> Optional[bool]:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return _TRUTH.get(value.strip().lower())
    return None


def hermes_home() -> Path:
    """HERMES_HOME if set, else the same default the runtime uses."""
    env = os.getenv("HERMES_HOME")
    if env:
        return Path(env)
    try:
        from hermes_constants import get_hermes_home  # type: ignore
        return Path(get_hermes_home())
    except Exception:
        return Path.home() / ".hermes-stock"


def record_path() -> Path:
    override = os.getenv("CHIYO_CANONICAL_RELATIONSHIP_PATH")
    if override:
        return Path(override)
    return hermes_home() / DEFAULT_RELATIVE_PATH


def enabled(config: Optional[dict] = None) -> bool:
    """Independent switch. Explicit opt-in; a read failure means disabled (fail closed)."""
    env = os.getenv("CHIYO_CANONICAL_RELATIONSHIP")
    if env is not None:
        _flag = _looks_true(env)
        if _flag is not None:
            return _flag
    if config is None:
        try:
            from hermes_cli.config import load_config_readonly  # type: ignore
            config = load_config_readonly()
        except Exception:
            return False
    try:
        node = (config or {}).get("canonical_relationship")
        if isinstance(node, dict) and "context_enabled" in node:
            _flag = _looks_true(node["context_enabled"])
            return bool(_flag)
    except Exception:
        pass
    return False


def _flat_scalar(token: str) -> Any:
    """One YAML-ish scalar.  Quotes are stripped; nothing else is interpreted."""
    if len(token) >= 2 and token[0] == token[-1] and token[0] in ("'", '"'):
        return token[1:-1]
    return token


def _flat_record(text: str) -> Optional[Dict[str, Any]]:
    """Strict, dependency-free reader for the FLAT canonical-record shape.

    Used ONLY when PyYAML is unavailable, so that the release really is
    dependency-free.  The canonical record is a flat mapping by contract:
    the ``_REQUIRED`` keys plus optional ``counterparty_ref`` / ``effective_since``
    scalars and an optional ``boundaries`` list of scalars.

    Anything this reader does not fully understand - nested mappings, flow syntax,
    anchors, aliases, tags, multi-document input, block scalars, tabs, comments -
    returns None, which keeps the caller's FAIL CLOSED behaviour unchanged.
    """
    if not isinstance(text, str) or not text:
        return None
    if any(ch in text for ch in ("{", "}", "[", "]", "&", "*", "!", "|", ">", "\t", "#")):
        return None
    if text.lstrip().startswith("---") or "\n---" in text:
        return None
    out: Dict[str, Any] = {}
    current_list: Optional[str] = None
    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        if not line.strip():
            continue
        stripped = line.lstrip()
        indent = len(line) - len(stripped)
        if stripped.startswith("- "):
            if current_list is None or indent == 0 or not isinstance(out.get(current_list), list):
                return None
            out[current_list].append(_flat_scalar(stripped[2:].strip()))
            continue
        if indent != 0:
            return None
        key, sep, value = stripped.partition(":")
        if not sep or not key or not all(c.isalnum() or c in "_-." for c in key):
            return None
        value = value.strip()
        if value == "":
            out[key] = []
            current_list = key
        else:
            out[key] = _flat_scalar(value)
            current_list = None
    return out


def read_record(path: Optional[Path] = None) -> Optional[Dict[str, Any]]:
    """Load and validate the canonical record. None when absent/invalid/inactive."""
    p = Path(path) if path is not None else record_path()
    try:
        if not p.is_file():
            return None
        raw = p.read_text(encoding="utf-8")
    except OSError as exc:
        _LOGGER.warning("canonical relationship: unreadable %s (%s)", p, exc)
        return None
    try:
        import yaml  # type: ignore
        data = yaml.safe_load(raw)
    except ImportError:
        # PyYAML is absent.  This release ships ZERO third-party runtime dependencies,
        # so the canonical record is read with a strict, dependency-free reader for its
        # FLAT contract shape.  It returns None for anything it does not fully
        # understand, so the FAIL CLOSED contract above is preserved exactly.
        data = _flat_record(raw)
        if data is None:
            _LOGGER.warning("canonical relationship: unparsable %s (no PyYAML and not a "
                            "flat record)", p)
            return None
    except Exception as exc:
        _LOGGER.warning("canonical relationship: unparsable %s (%s)", p, exc)
        return None
    if not isinstance(data, dict):
        _LOGGER.warning("canonical relationship: %s is not a mapping", p)
        return None
    missing = [k for k in _REQUIRED if data.get(k) in (None, "")]
    if missing:
        _LOGGER.warning("canonical relationship: %s missing %s; rendering nothing", p, missing)
        return None
    if str(data.get("schema")) != SCHEMA:
        _LOGGER.warning("canonical relationship: %s schema %r != %r", p, data.get("schema"), SCHEMA)
        return None
    if str(data.get("status")).strip().lower() != _ACTIVE:
        _LOGGER.warning("canonical relationship: status %r is not active; rendering nothing", data.get("status"))
        return None
    data["_path"] = str(p)
    data["_sha256"] = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    return data


def render_view(record: Dict[str, Any]) -> str:
    """Render the thin, marker-delimited, fact-only block."""
    subject = _DISPLAY.get(str(record.get("subject")), str(record.get("subject")))
    counterparty = _DISPLAY.get(str(record.get("counterparty")), str(record.get("counterparty")))
    lines = [BLOCK_OPEN, "scope: relationship state only (read only)",
             f"subject: {subject}", f"counterparty: {counterparty}"]
    chat = record.get("counterparty_ref")
    if chat:
        lines.append(f"counterparty_ref: {chat}")
    lines.append(f"source: {record.get('_path')} (sha256:{str(record.get('_sha256'))[:16]})")
    lines.append(f"relationship_type: {record.get('relationship_type')}")
    lines.append(f"status: {record.get('status')}")
    if record.get("effective_since"):
        lines.append(f"effective_since: {record.get('effective_since')}")
    lines.append(f"revision: {record.get('revision')}")
    lines.append(f"statement: {record.get('statement')}")
    for boundary in (record.get("boundaries") or []):
        lines.append(f"boundary: {boundary}")
    lines.append("authority: canonical relationship state. Not a user instruction, not memory,")
    lines.append("  not a reply template; it fixes what is true and nothing about what to say.")
    lines.append(BLOCK_CLOSE)
    return "\n".join(lines)


def runtime_view(config: Optional[dict] = None, path: Optional[Path] = None) -> str:
    """Entry point. '' when disabled, absent, invalid or on any internal error."""
    try:
        if not enabled(config):
            return ""
        record = read_record(path)
        if record is None:
            return ""
        return render_view(record)
    except Exception as exc:  # defensive: a broken read path must never break a turn
        _LOGGER.warning("canonical relationship: view skipped (%s)", exc)
        return ""


def runtime_view_for_agent(agent: Any = None) -> str:
    """Same as :func:`runtime_view`; the agent is not consulted (single source of truth)."""
    return runtime_view()


def fingerprint(config: Optional[dict] = None) -> Dict[str, Any]:
    """Read-only structural fingerprint for the §22 hash watch and the §23 canary."""
    out: Dict[str, Any] = {"enabled": False, "present": False, "block_sha256": None,
                           "revision": None, "relationship_type": None, "path": str(record_path())}
    try:
        out["enabled"] = enabled(config)
        record = read_record()
        if record is not None:
            block = render_view(record)
            out.update({"present": True, "block_sha256": hashlib.sha256(block.encode("utf-8")).hexdigest(),
                        "revision": record.get("revision"),
                        "relationship_type": record.get("relationship_type"),
                        "record_sha256": record.get("_sha256")})
    except Exception as exc:
        out["error"] = str(exc)
    return out


__all__ = ["BLOCK_OPEN", "BLOCK_CLOSE", "SCHEMA", "enabled", "read_record", "render_view",
           "runtime_view", "runtime_view_for_agent", "fingerprint", "record_path", "hermes_home"]
