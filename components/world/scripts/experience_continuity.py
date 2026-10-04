#!/usr/bin/env python3
"""Phase 2-I: explicit, bounded Experience Continuity foundation.

This module stores facts about bounded episodes.  It deliberately does not
turn an episode into a thought, desire, goal, action, or message.  The
existing ``experience_bus.py`` is an older pressure/injection path and is not
used here.

The default store is only read when callers explicitly ask for it.  PLAN and
SHADOW are read-only.  APPLY is explicit, revision-checked, locked, and
atomically persisted.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import sys
import tempfile
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, Mapping, Optional, Sequence

try:
    import fcntl
except ImportError:  # pragma: no cover - production is POSIX
    fcntl = None


SCHEMA_VERSION = 1
EPISODE_KIND = "life_loop_experience_episode"
EXPERIENCE_STATE_KIND = "life_loop_experience_state"
EXPERIENCE_CONTEXT_KIND = "life_loop_experience_context"
EXPERIENCE_PATCH_KIND = "life_loop_experience_patch"
SOURCE_EVENT_KIND = "life_loop_source_event"
CANONICAL_EVENT_KEY_PREFIX = "sha256:"
CONTENT_FINGERPRINT_PREFIX = "sha256:"
EVENT_EPISODE_ID_PREFIX = "episode-event-"

MODE_PLAN = "PLAN"
MODE_SHADOW = "SHADOW"
MODE_APPLY = "APPLY"

ADD = "ADD"
UPDATE = "UPDATE"
RESOLVE = "RESOLVE"
NO_CHANGE = "NO_CHANGE"
PATCH_OPS = frozenset({ADD, UPDATE, RESOLVE, NO_CHANGE})

OPEN = "OPEN"
RESOLVED = "RESOLVED"
EPISODE_STATES = frozenset({OPEN, RESOLVED})

MAX_EPISODES = 256
MAX_PATCH_OPS = 4
MAX_CONTEXT_EPISODES = 8
MAX_CONTEXT_BYTES = 8192
MAX_ID_LENGTH = 128
MAX_TEXT_LENGTH = 512
MAX_SOURCE_LENGTH = 96
MAX_PROVENANCE_FIELDS = 8
MAX_FACT_FIELDS = 24
MAX_FACT_BYTES = 4096
MAX_EPISODE_BYTES = 8192
MAX_EVENT_TYPE_LENGTH = 96
REALITY_DOMAINS = frozenset(
    {
        "conversation",
        "chiyo_world",
        "infrastructure",
        "external_tool",
        "public_social",
        "life_runtime",
    }
)

_BJT = timezone(timedelta(hours=8), name="Asia/Shanghai")
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_KEY_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.:-]{0,63}$")
_EPISODE_FIELDS = frozenset(
    {
        "kind",
        "schema_version",
        "episode_id",
        "state",
        "source",
        "summary",
        "started_at",
        "updated_at",
        "ended_at",
        "provenance",
        "facts",
    }
)
_STATE_FIELDS = frozenset({"kind", "schema_version", "revision", "updated_at", "episodes"})
_PATCH_FIELDS = frozenset({"kind", "schema_version", "base_revision", "ops"})
_CONTEXT_FIELDS = frozenset({"kind", "schema_version", "revision", "episodes"})
_SOURCE_EVENT_FIELDS = frozenset(
    {
        "kind",
        "schema_version",
        "source",
        "source_event_id",
        "occurred_at",
        "event_type",
        "summary",
        "facts",
        "provenance",
        "reality_domain",
    }
)

# These fields can carry prompts, hidden model material, ranking, or a
# behavioral conclusion.  Experience may contain bounded factual fields, but
# not hidden reasoning or a second agency layer.
_FORBIDDEN_KEYS = frozenset(
    {
        "prompt",
        "system_prompt",
        "transcript",
        "raw",
        "raw_output",
        "model_output",
        "model_response",
        "completion",
        "reasoning",
        "chain_of_thought",
        "cot",
        "hidden_state",
        "priority",
        "score",
        "ranking",
        "recommendation",
        "utility",
        "relationship_weight",
        "attention_score",
        "goal",
        "desire",
        "commitment",
        "next_action",
        "must_start",
        "must_send",
        "importance",
        "novelty",
        "relation",
        "emotionality",
        "unfinished",
        "share_impulse",
        "curiosity",
        "level",
        "state_impact",
        "relevance_now",
        "pressure",
        "salience",
        "should_share",
        "should_act",
        "candidate_action",
    }
)


class ExperienceContractError(ValueError):
    """An Experience state, episode, context, or patch is malformed."""


class ExperienceStoreError(RuntimeError):
    """The durable Experience store cannot be used safely."""


def _canonical_json(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ExperienceContractError("value is not JSON serializable") from exc


def _json_copy(value: Any) -> Any:
    return json.loads(_canonical_json(value))


def _text(value: Any, label: str, *, max_length: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ExperienceContractError(f"{label} must be a non-empty string")
    result = value.strip()
    if len(result) > max_length:
        raise ExperienceContractError(f"{label} is too long")
    return result


def _timestamp(value: Any, label: str, *, allow_none: bool = False) -> Optional[str]:
    if value is None and allow_none:
        return None
    text = _text(value, label, max_length=80)
    normalized = text[:-1] + "+00:00" if text.endswith(("Z", "z")) else text
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ExperienceContractError(f"{label} must be ISO datetime") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ExperienceContractError(f"{label} must include timezone")
    return parsed.astimezone(_BJT).isoformat(timespec="seconds")


def _parse_timestamp(value: str, label: str) -> datetime:
    normalized = value[:-1] + "+00:00" if value.endswith(("Z", "z")) else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ExperienceContractError(f"{label} must be ISO datetime") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ExperienceContractError(f"{label} must include timezone")
    return parsed


def _now_iso() -> str:
    return datetime.now(_BJT).isoformat(timespec="seconds")


def _new_id() -> str:
    return uuid.uuid4().hex


def _assert_mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ExperienceContractError(f"{label} must be an object")
    for key in value:
        if not isinstance(key, str):
            raise ExperienceContractError(f"{label} keys must be strings")
    return value


def _assert_exact_keys(value: Mapping[str, Any], allowed: frozenset[str], label: str) -> None:
    unknown = set(value) - allowed
    if unknown:
        raise ExperienceContractError(f"{label} contains unknown fields: {sorted(unknown)}")


def _walk_keys(value: Any) -> Iterator[str]:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if isinstance(key, str):
                yield key.strip().lower()
            yield from _walk_keys(item)
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for item in value:
            yield from _walk_keys(item)


def _reject_forbidden_keys(value: Any, label: str) -> None:
    for key in _walk_keys(value):
        if key in _FORBIDDEN_KEYS:
            raise ExperienceContractError(f"{label} contains forbidden field: {key}")


def _validate_id(value: Any, label: str) -> str:
    result = _text(value, label, max_length=MAX_ID_LENGTH)
    if not _ID_RE.fullmatch(result):
        raise ExperienceContractError(f"{label} has invalid characters")
    return result


def _validate_small_json(value: Any, label: str, *, depth: int = 0) -> Any:
    if depth > 2:
        raise ExperienceContractError(f"{label} is too deeply nested")
    if value is None or isinstance(value, (str, bool, int, float)):
        if isinstance(value, float) and (value != value or value in (float("inf"), float("-inf"))):
            raise ExperienceContractError(f"{label} contains a non-finite number")
        if isinstance(value, str) and len(value) > MAX_TEXT_LENGTH:
            raise ExperienceContractError(f"{label} text is too long")
        return value
    if isinstance(value, Mapping):
        result: Dict[str, Any] = {}
        if len(value) > MAX_FACT_FIELDS:
            raise ExperienceContractError(f"{label} has too many fields")
        for key, item in value.items():
            if not isinstance(key, str) or not _KEY_RE.fullmatch(key):
                raise ExperienceContractError(f"{label} contains an invalid key")
            result[key] = _validate_small_json(item, f"{label}.{key}", depth=depth + 1)
        _reject_forbidden_keys(result, label)
        return result
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        if len(value) > MAX_FACT_FIELDS:
            raise ExperienceContractError(f"{label} has too many items")
        return [_validate_small_json(item, label, depth=depth + 1) for item in value]
    raise ExperienceContractError(f"{label} contains an unsupported value")


def _validate_facts(value: Any) -> Dict[str, Any]:
    mapping = _assert_mapping(value, "facts")
    result = _validate_small_json(dict(mapping), "facts")
    if not isinstance(result, dict):  # pragma: no cover - guarded above
        raise ExperienceContractError("facts must be an object")
    if len(_canonical_json(result).encode("utf-8")) > MAX_FACT_BYTES:
        raise ExperienceContractError("facts are too large")
    return result


def _validate_provenance(value: Any) -> Dict[str, str]:
    mapping = _assert_mapping(value, "provenance")
    if not mapping:
        raise ExperienceContractError("provenance must not be empty")
    if len(mapping) > MAX_PROVENANCE_FIELDS:
        raise ExperienceContractError("provenance has too many fields")
    result: Dict[str, str] = {}
    allowed = {
        "created_by",
        "origin_activity_id",
        "origin_cycle_id",
        "origin_wake_id",
        "source_ref",
        "external_id",
        "recorded_at",
        "transition",
        "record_coordinate",
        "source_event_id",
        "canonical_event_key",
        "content_fingerprint",
        "reality_domain",
        "runtime_id",
        "ingest_id",
        "ingested_at",
    }
    for key, item in mapping.items():
        if key not in allowed:
            raise ExperienceContractError(f"provenance field is not allowed: {key}")
        result[key] = _text(item, f"provenance.{key}", max_length=MAX_ID_LENGTH)
    _reject_forbidden_keys(result, "provenance")
    if "created_by" not in result:
        raise ExperienceContractError("provenance.created_by is required")
    return result


def _validate_source_name(value: Any, label: str = "source") -> str:
    result = _text(value, label, max_length=MAX_SOURCE_LENGTH)
    if not _ID_RE.fullmatch(result):
        raise ExperienceContractError(f"{label} has invalid characters")
    return result


@dataclass(frozen=True)
class SourceEvent:
    """A bounded external fact before it becomes a canonical Episode."""

    kind: str
    schema_version: int
    source: str
    source_event_id: str
    occurred_at: str
    event_type: str
    summary: str
    facts: Mapping[str, Any]
    provenance: Mapping[str, str]
    reality_domain: str

    @classmethod
    def create(
        cls,
        *,
        source: str,
        source_event_id: str,
        occurred_at: str,
        event_type: str,
        summary: str,
        provenance: Mapping[str, str],
        reality_domain: str,
        facts: Optional[Mapping[str, Any]] = None,
    ) -> "SourceEvent":
        return cls.from_mapping(
            {
                "kind": SOURCE_EVENT_KIND,
                "schema_version": SCHEMA_VERSION,
                "source": source,
                "source_event_id": source_event_id,
                "occurred_at": occurred_at,
                "event_type": event_type,
                "summary": summary,
                "facts": dict(facts or {}),
                "provenance": dict(provenance),
                "reality_domain": reality_domain,
            }
        )

    @classmethod
    def from_mapping(cls, value: Any) -> "SourceEvent":
        if isinstance(value, SourceEvent):
            return value
        mapping = _assert_mapping(value, "source event")
        _assert_exact_keys(mapping, _SOURCE_EVENT_FIELDS, "source event")
        if mapping.get("kind") != SOURCE_EVENT_KIND:
            raise ExperienceContractError("source_event.kind is invalid")
        if mapping.get("schema_version") != SCHEMA_VERSION:
            raise ExperienceContractError("source_event.schema_version is unsupported")
        source = _validate_source_name(mapping.get("source"), "source_event.source")
        source_event_id = _validate_id(mapping.get("source_event_id"), "source_event_id")
        occurred_at = _timestamp(mapping.get("occurred_at"), "source_event.occurred_at")
        event_type = _text(mapping.get("event_type"), "source_event.event_type", max_length=MAX_EVENT_TYPE_LENGTH)
        if not _ID_RE.fullmatch(event_type):
            raise ExperienceContractError("source_event.event_type has invalid characters")
        summary = _text(mapping.get("summary"), "source_event.summary", max_length=MAX_TEXT_LENGTH)
        facts = _validate_facts(mapping.get("facts"))
        provenance = _validate_provenance(mapping.get("provenance"))
        reality_domain = _text(mapping.get("reality_domain"), "source_event.reality_domain", max_length=64)
        if reality_domain not in REALITY_DOMAINS:
            raise ExperienceContractError("source_event.reality_domain is unknown")
        if reality_domain != "life_runtime" and provenance.get("created_by") == "main_self":
            raise ExperienceContractError("external source event cannot be created_by main_self")
        result = cls(
            kind=SOURCE_EVENT_KIND,
            schema_version=SCHEMA_VERSION,
            source=source,
            source_event_id=source_event_id,
            occurred_at=occurred_at or "",
            event_type=event_type,
            summary=summary,
            facts=facts,
            provenance=provenance,
            reality_domain=reality_domain,
        )
        if len(_canonical_json(result.to_dict()).encode("utf-8")) > MAX_EPISODE_BYTES:
            raise ExperienceContractError("source event is too large")
        return result

    def to_dict(self) -> Dict[str, Any]:
        return {
            "kind": self.kind,
            "schema_version": self.schema_version,
            "source": self.source,
            "source_event_id": self.source_event_id,
            "occurred_at": self.occurred_at,
            "event_type": self.event_type,
            "summary": self.summary,
            "facts": _json_copy(dict(self.facts)),
            "provenance": dict(self.provenance),
            "reality_domain": self.reality_domain,
        }


def validate_source_event(value: Any) -> SourceEvent:
    return SourceEvent.from_mapping(value)


def canonical_event_key_for(event_or_source: SourceEvent | Mapping[str, Any] | str, source_event_id: Optional[str] = None) -> str:
    if isinstance(event_or_source, (SourceEvent, Mapping)):
        parsed = SourceEvent.from_mapping(event_or_source)
        source = parsed.source
        event_id = parsed.source_event_id
    else:
        source = _validate_source_name(event_or_source, "source")
        event_id = _validate_id(source_event_id, "source_event_id")
    canonical_source = source.strip().lower()
    payload = (canonical_source + "\x1f" + event_id).encode("utf-8")
    return CANONICAL_EVENT_KEY_PREFIX + hashlib.sha256(payload).hexdigest()


def canonical_event_key(event_or_source: SourceEvent | Mapping[str, Any] | str, source_event_id: Optional[str] = None) -> str:
    return canonical_event_key_for(event_or_source, source_event_id)


_FINGERPRINT_PROVENANCE_KEYS = frozenset(
    {"origin_activity_id", "origin_cycle_id", "origin_wake_id", "external_id", "transition"}
)


def content_fingerprint_for(event: SourceEvent | Mapping[str, Any]) -> str:
    parsed = SourceEvent.from_mapping(event)
    factual_provenance = {
        key: parsed.provenance[key]
        for key in sorted(_FINGERPRINT_PROVENANCE_KEYS)
        if key in parsed.provenance
    }
    payload = {
        "occurred_at": parsed.occurred_at,
        "event_type": parsed.event_type,
        "summary": parsed.summary,
        "facts": _json_copy(dict(parsed.facts)),
        "provenance": factual_provenance,
        "reality_domain": parsed.reality_domain,
    }
    return CONTENT_FINGERPRINT_PREFIX + hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def content_fingerprint(event: SourceEvent | Mapping[str, Any]) -> str:
    return content_fingerprint_for(event)


def episode_id_for(event_or_key: SourceEvent | Mapping[str, Any] | str) -> str:
    key = canonical_event_key_for(event_or_key) if isinstance(event_or_key, (SourceEvent, Mapping)) else _text(event_or_key, "canonical_event_key", max_length=128)
    digest = key.split(":", 1)[-1]
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ExperienceContractError("canonical_event_key is invalid")
    return EVENT_EPISODE_ID_PREFIX + digest


def episode_id_for_event(event: SourceEvent | Mapping[str, Any]) -> str:
    return episode_id_for(SourceEvent.from_mapping(event))


def _episode_from_source_event(event: SourceEvent) -> "Episode":
    key = canonical_event_key_for(event)
    fingerprint = content_fingerprint_for(event)
    provenance = dict(event.provenance)
    provenance.update(
        {
            "source_event_id": event.source_event_id,
            "canonical_event_key": key,
            "content_fingerprint": fingerprint,
            "reality_domain": event.reality_domain,
        }
    )
    # Ingestion deliberately creates OPEN only.  A past occurrence is not an
    # assertion that Self has resolved the experience.
    return Episode.create(
        source=event.source,
        summary=event.summary,
        provenance=provenance,
        facts=event.facts,
        episode_id=episode_id_for(key),
        state=OPEN,
        started_at=event.occurred_at,
        updated_at=event.occurred_at,
        ended_at=None,
    )


@dataclass(frozen=True)
class Episode:
    kind: str
    schema_version: int
    episode_id: str
    state: str
    source: str
    summary: str
    started_at: str
    updated_at: str
    ended_at: Optional[str]
    provenance: Mapping[str, str]
    facts: Mapping[str, Any]

    @classmethod
    def create(
        cls,
        *,
        source: str,
        summary: str,
        provenance: Mapping[str, str],
        facts: Optional[Mapping[str, Any]] = None,
        episode_id: Optional[str] = None,
        state: str = OPEN,
        started_at: Optional[str] = None,
        updated_at: Optional[str] = None,
        ended_at: Optional[str] = None,
        clock: Callable[[], str] = _now_iso,
    ) -> "Episode":
        now = clock()
        return cls.from_mapping(
            {
                "kind": EPISODE_KIND,
                "schema_version": SCHEMA_VERSION,
                "episode_id": episode_id or _new_id(),
                "state": state,
                "source": source,
                "summary": summary,
                "started_at": started_at or now,
                "updated_at": updated_at or now,
                "ended_at": ended_at,
                "provenance": dict(provenance),
                "facts": dict(facts or {}),
            }
        )

    @classmethod
    def from_mapping(cls, value: Any) -> "Episode":
        if isinstance(value, Episode):
            return value
        mapping = _assert_mapping(value, "episode")
        _assert_exact_keys(mapping, _EPISODE_FIELDS, "episode")
        if mapping.get("kind") != EPISODE_KIND:
            raise ExperienceContractError("episode.kind is invalid")
        if mapping.get("schema_version") != SCHEMA_VERSION:
            raise ExperienceContractError("episode.schema_version is unsupported")
        episode_id = _validate_id(mapping.get("episode_id"), "episode_id")
        state = _text(mapping.get("state"), "episode.state", max_length=32)
        if state not in EPISODE_STATES:
            raise ExperienceContractError("episode.state is invalid")
        source = _text(mapping.get("source"), "episode.source", max_length=MAX_SOURCE_LENGTH)
        summary = _text(mapping.get("summary"), "episode.summary", max_length=MAX_TEXT_LENGTH)
        started_at = _timestamp(mapping.get("started_at"), "episode.started_at")
        updated_at = _timestamp(mapping.get("updated_at"), "episode.updated_at")
        ended_at = _timestamp(mapping.get("ended_at"), "episode.ended_at", allow_none=True)
        assert started_at is not None and updated_at is not None
        started_dt = _parse_timestamp(started_at, "episode.started_at")
        if _parse_timestamp(updated_at, "episode.updated_at") < started_dt:
            raise ExperienceContractError("episode.updated_at precedes started_at")
        if ended_at is not None and _parse_timestamp(ended_at, "episode.ended_at") < started_dt:
            raise ExperienceContractError("episode.ended_at precedes started_at")
        if state == OPEN and ended_at is not None:
            raise ExperienceContractError("OPEN episode cannot have ended_at")
        if state == RESOLVED and ended_at is None:
            raise ExperienceContractError("RESOLVED episode requires ended_at")
        provenance = _validate_provenance(mapping.get("provenance"))
        facts = _validate_facts(mapping.get("facts"))
        raw_size = len(_canonical_json(dict(mapping)).encode("utf-8"))
        if raw_size > MAX_EPISODE_BYTES:
            raise ExperienceContractError("episode is too large")
        return cls(
            kind=EPISODE_KIND,
            schema_version=SCHEMA_VERSION,
            episode_id=episode_id,
            state=state,
            source=source,
            summary=summary,
            started_at=started_at,
            updated_at=updated_at,
            ended_at=ended_at,
            provenance=provenance,
            facts=facts,
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "kind": self.kind,
            "schema_version": self.schema_version,
            "episode_id": self.episode_id,
            "state": self.state,
            "source": self.source,
            "summary": self.summary,
            "started_at": self.started_at,
            "updated_at": self.updated_at,
            "ended_at": self.ended_at,
            "provenance": dict(self.provenance),
            "facts": _json_copy(dict(self.facts)),
        }


@dataclass(frozen=True)
class ExperienceState:
    kind: str
    schema_version: int
    revision: int
    updated_at: str
    episodes: Sequence[Episode]

    @classmethod
    def empty(cls, *, clock: Callable[[], str] = _now_iso) -> "ExperienceState":
        return cls(
            kind=EXPERIENCE_STATE_KIND,
            schema_version=SCHEMA_VERSION,
            revision=0,
            updated_at=_timestamp(clock(), "state.updated_at"),
            episodes=(),
        )

    @classmethod
    def from_mapping(cls, value: Any) -> "ExperienceState":
        if isinstance(value, ExperienceState):
            return value
        mapping = _assert_mapping(value, "experience state")
        _assert_exact_keys(mapping, _STATE_FIELDS, "experience state")
        if mapping.get("kind") != EXPERIENCE_STATE_KIND:
            raise ExperienceContractError("state.kind is invalid")
        if mapping.get("schema_version") != SCHEMA_VERSION:
            raise ExperienceContractError("state.schema_version is unsupported")
        revision = mapping.get("revision")
        if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
            raise ExperienceContractError("state.revision must be a non-negative integer")
        updated_at = _timestamp(mapping.get("updated_at"), "state.updated_at")
        assert updated_at is not None
        raw_episodes = mapping.get("episodes")
        if not isinstance(raw_episodes, Sequence) or isinstance(raw_episodes, (str, bytes, bytearray)):
            raise ExperienceContractError("state.episodes must be a list")
        if len(raw_episodes) > MAX_EPISODES:
            raise ExperienceContractError("state contains too many episodes")
        episodes = tuple(Episode.from_mapping(item) for item in raw_episodes)
        ids = [item.episode_id for item in episodes]
        if len(ids) != len(set(ids)):
            raise ExperienceContractError("state contains duplicate episode_id")
        return cls(
            kind=EXPERIENCE_STATE_KIND,
            schema_version=SCHEMA_VERSION,
            revision=revision,
            updated_at=updated_at,
            episodes=episodes,
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "kind": self.kind,
            "schema_version": self.schema_version,
            "revision": self.revision,
            "updated_at": self.updated_at,
            "episodes": [item.to_dict() for item in self.episodes],
        }


def _validate_op(value: Any) -> Dict[str, Any]:
    mapping = _assert_mapping(value, "patch operation")
    op = _text(mapping.get("op"), "patch.op", max_length=32)
    if op not in PATCH_OPS:
        raise ExperienceContractError("patch operation is unknown")
    if op == NO_CHANGE:
        if set(mapping) != {"op"}:
            raise ExperienceContractError("NO_CHANGE cannot carry fields")
        return {"op": NO_CHANGE}
    if op == ADD:
        _assert_exact_keys(mapping, frozenset({"op", "episode"}), "ADD operation")
        episode = Episode.from_mapping(mapping.get("episode"))
        return {"op": ADD, "episode": episode.to_dict()}
    if op == UPDATE:
        _assert_exact_keys(mapping, frozenset({"op", "episode_id", "summary", "facts"}), "UPDATE operation")
        if "summary" not in mapping and "facts" not in mapping:
            raise ExperienceContractError("UPDATE must change summary or facts")
        result: Dict[str, Any] = {"op": UPDATE, "episode_id": _validate_id(mapping.get("episode_id"), "update.episode_id")}
        if "summary" in mapping:
            result["summary"] = _text(mapping.get("summary"), "update.summary", max_length=MAX_TEXT_LENGTH)
        if "facts" in mapping:
            result["facts"] = _validate_facts(mapping.get("facts"))
        return result
    _assert_exact_keys(mapping, frozenset({"op", "episode_id", "ended_at", "facts"}), "RESOLVE operation")
    result = {
        "op": RESOLVE,
        "episode_id": _validate_id(mapping.get("episode_id"), "resolve.episode_id"),
        "ended_at": _timestamp(mapping.get("ended_at"), "resolve.ended_at"),
    }
    if "facts" in mapping:
        result["facts"] = _validate_facts(mapping.get("facts"))
    return result


@dataclass(frozen=True)
class ExperiencePatch:
    kind: str
    schema_version: int
    base_revision: int
    ops: Sequence[Mapping[str, Any]]

    @classmethod
    def from_mapping(cls, value: Any) -> "ExperiencePatch":
        if isinstance(value, ExperiencePatch):
            return value
        mapping = _assert_mapping(value, "experience patch")
        _assert_exact_keys(mapping, _PATCH_FIELDS, "experience patch")
        if mapping.get("kind") != EXPERIENCE_PATCH_KIND:
            raise ExperienceContractError("patch.kind is invalid")
        if mapping.get("schema_version") != SCHEMA_VERSION:
            raise ExperienceContractError("patch.schema_version is unsupported")
        base_revision = mapping.get("base_revision")
        if isinstance(base_revision, bool) or not isinstance(base_revision, int) or base_revision < 0:
            raise ExperienceContractError("patch.base_revision must be a non-negative integer")
        raw_ops = mapping.get("ops")
        if not isinstance(raw_ops, Sequence) or isinstance(raw_ops, (str, bytes, bytearray)):
            raise ExperienceContractError("patch.ops must be a list")
        if not raw_ops or len(raw_ops) > MAX_PATCH_OPS:
            raise ExperienceContractError("patch operation count is invalid")
        ops = tuple(_validate_op(item) for item in raw_ops)
        if any(item["op"] == NO_CHANGE for item in ops) and len(ops) != 1:
            raise ExperienceContractError("NO_CHANGE must be the only operation")
        if sum(1 for item in ops if item["op"] == ADD) > 3:
            raise ExperienceContractError("patch contains too many ADD operations")
        return cls(
            kind=EXPERIENCE_PATCH_KIND,
            schema_version=SCHEMA_VERSION,
            base_revision=base_revision,
            ops=ops,
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "kind": self.kind,
            "schema_version": self.schema_version,
            "base_revision": self.base_revision,
            "ops": [_json_copy(dict(item)) for item in self.ops],
        }


def _state_with_revision(state: ExperienceState, episodes: Sequence[Episode], *, clock: Callable[[], str]) -> ExperienceState:
    return ExperienceState(
        kind=EXPERIENCE_STATE_KIND,
        schema_version=SCHEMA_VERSION,
        revision=state.revision + 1,
        updated_at=_timestamp(clock(), "state.updated_at"),
        episodes=tuple(episodes),
    )


def _project_patch(state: ExperienceState, patch: ExperiencePatch, *, clock: Callable[[], str]) -> ExperienceState:
    if state.revision != patch.base_revision:
        raise ExperienceContractError("stale_patch")
    if patch.ops[0]["op"] == NO_CHANGE:
        return state
    episodes = list(state.episodes)
    by_id = {item.episode_id: index for index, item in enumerate(episodes)}
    for operation in patch.ops:
        op = operation["op"]
        if op == ADD:
            episode = Episode.from_mapping(operation["episode"])
            if episode.episode_id in by_id:
                raise ExperienceContractError("episode_id already exists")
            if len(episodes) >= MAX_EPISODES:
                raise ExperienceContractError("experience capacity is full")
            by_id[episode.episode_id] = len(episodes)
            episodes.append(episode)
        elif op == UPDATE:
            episode_id = operation["episode_id"]
            if episode_id not in by_id:
                raise ExperienceContractError("unknown episode_id")
            current = episodes[by_id[episode_id]]
            if current.state != OPEN:
                raise ExperienceContractError("only OPEN episodes can be updated")
            values = current.to_dict()
            if "summary" in operation:
                values["summary"] = operation["summary"]
            if "facts" in operation:
                values["facts"] = operation["facts"]
            values["updated_at"] = clock()
            episodes[by_id[episode_id]] = Episode.from_mapping(values)
        elif op == RESOLVE:
            episode_id = operation["episode_id"]
            if episode_id not in by_id:
                raise ExperienceContractError("unknown episode_id")
            current = episodes[by_id[episode_id]]
            if current.state != OPEN:
                raise ExperienceContractError("episode is already resolved")
            ended_at = operation["ended_at"]
            values = current.to_dict()
            values["state"] = RESOLVED
            values["ended_at"] = ended_at
            values["updated_at"] = clock()
            if "facts" in operation:
                merged = dict(current.facts)
                merged.update(operation["facts"])
                values["facts"] = merged
            episodes[by_id[episode_id]] = Episode.from_mapping(values)
        else:  # pragma: no cover - _validate_op rejects it
            raise ExperienceContractError("unknown patch operation")
    return _state_with_revision(state, episodes, clock=clock)


def _context_episode(episode: Episode) -> Dict[str, Any]:
    # Context intentionally excludes the complete Episode object and exposes
    # only bounded, factual fields needed for continuity.
    return {
        "episode_id": episode.episode_id,
        "state": episode.state,
        "source": episode.source,
        "summary": episode.summary,
        "started_at": episode.started_at,
        "updated_at": episode.updated_at,
        "ended_at": episode.ended_at,
        "provenance": dict(episode.provenance),
        "facts": _json_copy(dict(episode.facts)),
    }


def build_experience_context(
    state: ExperienceState | Mapping[str, Any],
    *,
    limit: int = MAX_CONTEXT_EPISODES,
    max_bytes: int = MAX_CONTEXT_BYTES,
) -> Dict[str, Any]:
    """Build a stable, bounded context without ranking or psychological inference."""

    parsed = ExperienceState.from_mapping(state)
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0 or limit > MAX_CONTEXT_EPISODES:
        raise ExperienceContractError("context limit is invalid")
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes <= 0 or max_bytes > MAX_CONTEXT_BYTES:
        raise ExperienceContractError("context byte bound is invalid")
    ordered = sorted(parsed.episodes, key=lambda item: (item.updated_at, item.episode_id), reverse=True)
    selected: list[Dict[str, Any]] = []
    for episode in ordered[:limit]:
        candidate = _context_episode(episode)
        trial = {
            "kind": EXPERIENCE_CONTEXT_KIND,
            "schema_version": SCHEMA_VERSION,
            "revision": parsed.revision,
            "episodes": selected + [candidate],
        }
        if len(_canonical_json(trial).encode("utf-8")) > max_bytes:
            break
        selected.append(candidate)
    result = {
        "kind": EXPERIENCE_CONTEXT_KIND,
        "schema_version": SCHEMA_VERSION,
        "revision": parsed.revision,
        "episodes": selected,
    }
    if len(_canonical_json(result).encode("utf-8")) > max_bytes:  # pragma: no cover - defensive
        raise ExperienceContractError("experience context exceeds byte bound")
    return result


def validate_experience_context(value: Any, *, max_bytes: int = MAX_CONTEXT_BYTES) -> Dict[str, Any]:
    mapping = _assert_mapping(value, "experience context")
    _assert_exact_keys(mapping, _CONTEXT_FIELDS, "experience context")
    if mapping.get("kind") != EXPERIENCE_CONTEXT_KIND:
        raise ExperienceContractError("context.kind is invalid")
    if mapping.get("schema_version") != SCHEMA_VERSION:
        raise ExperienceContractError("context.schema_version is unsupported")
    revision = mapping.get("revision")
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
        raise ExperienceContractError("context.revision is invalid")
    episodes = mapping.get("episodes")
    if not isinstance(episodes, Sequence) or isinstance(episodes, (str, bytes, bytearray)):
        raise ExperienceContractError("context.episodes must be a list")
    if len(episodes) > MAX_CONTEXT_EPISODES:
        raise ExperienceContractError("context has too many episodes")
    # Reconstruct enough of the contract to reject forged/hidden context.  A
    # context is not accepted as a full Episode state and cannot be persisted.
    seen: set[str] = set()
    normalized: list[Dict[str, Any]] = []
    for item in episodes:
        entry = _assert_mapping(item, "context episode")
        _assert_exact_keys(
            entry,
            frozenset({"episode_id", "state", "source", "summary", "started_at", "updated_at", "ended_at", "provenance", "facts"}),
            "context episode",
        )
        episode_id = _validate_id(entry.get("episode_id"), "context.episode_id")
        if episode_id in seen:
            raise ExperienceContractError("context contains duplicate episode_id")
        seen.add(episode_id)
        # A context entry must still be a valid bounded episode; adding the
        # full kind/schema only for validation keeps the public context small.
        episode = Episode.from_mapping(
            {
                "kind": EPISODE_KIND,
                "schema_version": SCHEMA_VERSION,
                "episode_id": episode_id,
                "state": entry.get("state"),
                "source": entry.get("source"),
                "summary": entry.get("summary"),
                "started_at": entry.get("started_at"),
                "updated_at": entry.get("updated_at"),
                "ended_at": entry.get("ended_at"),
                "provenance": entry.get("provenance"),
                "facts": entry.get("facts"),
            }
        )
        normalized.append(_context_episode(episode))
    result = {"kind": EXPERIENCE_CONTEXT_KIND, "schema_version": SCHEMA_VERSION, "revision": revision, "episodes": normalized}
    if len(_canonical_json(result).encode("utf-8")) > max_bytes:
        raise ExperienceContractError("experience context exceeds byte bound")
    return result


def episode_in_context(context: Any, episode_id: str) -> bool:
    normalized = validate_experience_context(context)
    target = _validate_id(episode_id, "episode_id")
    return any(item["episode_id"] == target for item in normalized["episodes"])


def _default_hermes_home() -> Path:
    return Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes")))


def default_store_path(hermes_home: Optional[Path | str] = None) -> Path:
    return Path(hermes_home or _default_hermes_home()) / "state" / "experience_continuity.json"


class ExperienceStore:
    """Durable state authority with explicit atomic APPLY."""

    def __init__(
        self,
        path: Path | str | None = None,
        *,
        lock_timeout: float = 2.0,
        lock_poll_interval: float = 0.02,
        clock: Callable[[], str] = _now_iso,
    ) -> None:
        self.path = Path(path) if path is not None else default_store_path()
        if isinstance(lock_timeout, bool) or not isinstance(lock_timeout, (int, float)) or lock_timeout <= 0:
            raise ExperienceStoreError("lock_timeout must be positive")
        if isinstance(lock_poll_interval, bool) or not isinstance(lock_poll_interval, (int, float)) or lock_poll_interval <= 0:
            raise ExperienceStoreError("lock_poll_interval must be positive")
        self.lock_timeout = float(lock_timeout)
        self.lock_poll_interval = float(lock_poll_interval)
        self.clock = clock
        self.lock_path = self.path.with_name(self.path.name + ".lock")

    def _read_unlocked(self) -> ExperienceState:
        if not self.path.exists():
            return ExperienceState.empty(clock=self.clock)
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, TypeError, ValueError) as exc:
            raise ExperienceStoreError("experience state cannot be read") from exc
        try:
            return ExperienceState.from_mapping(raw)
        except ExperienceContractError as exc:
            raise ExperienceStoreError("experience state is malformed") from exc

    def load(self) -> ExperienceState:
        """Read without creating a lock or state file."""

        return self._read_unlocked()

    @contextmanager
    def _locked(self) -> Iterator[None]:
        if fcntl is None:
            raise ExperienceStoreError("cross-process lock unavailable")
        try:
            self.lock_path.parent.mkdir(parents=True, exist_ok=True)
            handle = self.lock_path.open("a+")
        except OSError as exc:
            raise ExperienceStoreError("experience lock cannot be opened") from exc
        deadline = time.monotonic() + self.lock_timeout
        try:
            while True:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError as exc:
                    if time.monotonic() >= deadline:
                        raise ExperienceStoreError("experience lock timeout") from exc
                    time.sleep(min(self.lock_poll_interval, max(0.001, deadline - time.monotonic())))
            yield
        finally:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            finally:
                handle.close()

    def _write_unlocked(self, state: ExperienceState) -> None:
        parsed = ExperienceState.from_mapping(state.to_dict())
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = (_canonical_json(parsed.to_dict()) + "\n").encode("utf-8")
        temporary: Optional[str] = None
        try:
            descriptor, temporary = tempfile.mkstemp(prefix=self.path.name + ".", suffix=".tmp", dir=str(self.path.parent))
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
            temporary = None
            try:
                directory_fd = os.open(self.path.parent, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            except OSError:
                pass
        except OSError as exc:
            raise ExperienceStoreError("experience state cannot be atomically written") from exc
        finally:
            if temporary is not None:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass

    def plan_patch(self, patch: ExperiencePatch | Mapping[str, Any]) -> Dict[str, Any]:
        parsed_patch = ExperiencePatch.from_mapping(patch)
        current = self.load()
        if current.revision != parsed_patch.base_revision:
            return {"status": "stale_patch", "revision": current.revision}
        try:
            projected = _project_patch(current, parsed_patch, clock=self.clock)
        except ExperienceContractError as exc:
            if str(exc) == "stale_patch":
                return {"status": "stale_patch", "revision": current.revision}
            return {"status": "rejected", "reason": str(exc), "revision": current.revision}
        return {
            "status": "planned",
            "revision": current.revision,
            "projected_revision": projected.revision,
            "state_preview": projected.to_dict(),
        }

    def apply_patch(
        self,
        patch: ExperiencePatch | Mapping[str, Any],
        *,
        confirm_apply: bool = False,
    ) -> Dict[str, Any]:
        parsed_patch = ExperiencePatch.from_mapping(patch)
        if not confirm_apply:
            current = self.load()
            return {"status": "confirmation_required", "revision": current.revision}
        try:
            with self._locked():
                current = self._read_unlocked()
                if current.revision != parsed_patch.base_revision:
                    return {"status": "stale_patch", "revision": current.revision}
                try:
                    projected = _project_patch(current, parsed_patch, clock=self.clock)
                except ExperienceContractError as exc:
                    return {"status": "rejected", "reason": str(exc), "revision": current.revision}
                if projected.revision == current.revision:
                    return {"status": "no_change", "revision": current.revision, "state": current.to_dict()}
                self._write_unlocked(projected)
                return {"status": "applied", "revision": projected.revision, "state": projected.to_dict()}
        except ExperienceStoreError as exc:
            if "timeout" in str(exc):
                return {"status": "busy", "reason": str(exc)}
            raise

    @staticmethod
    def _source_event_match(state: ExperienceState, event: SourceEvent) -> Dict[str, Any]:
        key = canonical_event_key_for(event)
        fingerprint = content_fingerprint_for(event)
        deterministic_episode_id = episode_id_for(key)
        existing = next(
            (
                item
                for item in state.episodes
                if item.provenance.get("canonical_event_key") == key
            ),
            None,
        )
        if existing is None:
            return {
                "status": "planned",
                "decision": ADD,
                "canonical_event_key": key,
                "episode_id": deterministic_episode_id,
                "content_fingerprint": fingerprint,
                "episode_preview": _episode_from_source_event(event).to_dict(),
                "revision": state.revision,
            }
        existing_fingerprint = existing.provenance.get("content_fingerprint")
        if existing_fingerprint == fingerprint:
            return {
                "status": "duplicate_event",
                "decision": NO_CHANGE,
                "canonical_event_key": key,
                "episode_id": existing.episode_id,
                "content_fingerprint": fingerprint,
                "episode_preview": existing.to_dict(),
                "revision": state.revision,
            }
        return {
            "status": "event_identity_conflict",
            "decision": "CONFLICT",
            "canonical_event_key": key,
            "episode_id": existing.episode_id,
            "content_fingerprint": fingerprint,
            "existing_content_fingerprint": existing_fingerprint,
            "episode_preview": existing.to_dict(),
            "revision": state.revision,
        }

    def plan_source_event(self, event: SourceEvent | Mapping[str, Any]) -> Dict[str, Any]:
        parsed = SourceEvent.from_mapping(event)
        return self._source_event_match(self.load(), parsed)

    def apply_source_event(
        self,
        event: SourceEvent | Mapping[str, Any],
        *,
        confirm_apply: bool = False,
    ) -> Dict[str, Any]:
        parsed = SourceEvent.from_mapping(event)
        if not confirm_apply:
            result = self.plan_source_event(parsed)
            result["status"] = "confirmation_required"
            return result
        try:
            with self._locked():
                current = self._read_unlocked()
                result = self._source_event_match(current, parsed)
                if result["status"] != "planned":
                    return result
                if len(current.episodes) >= MAX_EPISODES:
                    return {
                        "status": "rejected",
                        "reason": "experience capacity is full",
                        "canonical_event_key": result["canonical_event_key"],
                        "episode_id": result["episode_id"],
                        "revision": current.revision,
                    }
                episode = _episode_from_source_event(parsed)
                projected = _state_with_revision(current, [*current.episodes, episode], clock=self.clock)
                self._write_unlocked(projected)
                result["status"] = "applied"
                result["revision"] = projected.revision
                result["episode_preview"] = episode.to_dict()
                return result
        except ExperienceStoreError as exc:
            if "timeout" in str(exc):
                return {"status": "busy", "reason": str(exc)}
            raise

    def ingest_source_event(
        self,
        event: SourceEvent | Mapping[str, Any],
        *,
        confirm_apply: bool = False,
    ) -> Dict[str, Any]:
        return self.apply_source_event(event, confirm_apply=confirm_apply)

    def append_episode(self, episode: Episode | Mapping[str, Any], *, confirm_apply: bool = False) -> Dict[str, Any]:
        parsed = Episode.from_mapping(episode)
        current = self.load()
        patch = ExperiencePatch.from_mapping(
            {"kind": EXPERIENCE_PATCH_KIND, "schema_version": SCHEMA_VERSION, "base_revision": current.revision, "ops": [{"op": ADD, "episode": parsed.to_dict()}]}
        )
        return self.apply_patch(patch, confirm_apply=confirm_apply)

    def update_episode(
        self,
        episode_id: str,
        *,
        summary: Optional[str] = None,
        facts: Optional[Mapping[str, Any]] = None,
        confirm_apply: bool = False,
    ) -> Dict[str, Any]:
        current = self.load()
        op: Dict[str, Any] = {"op": UPDATE, "episode_id": episode_id}
        if summary is not None:
            op["summary"] = summary
        if facts is not None:
            op["facts"] = dict(facts)
        patch = ExperiencePatch.from_mapping(
            {"kind": EXPERIENCE_PATCH_KIND, "schema_version": SCHEMA_VERSION, "base_revision": current.revision, "ops": [op]}
        )
        return self.apply_patch(patch, confirm_apply=confirm_apply)

    def resolve_episode(
        self,
        episode_id: str,
        *,
        ended_at: Optional[str] = None,
        facts: Optional[Mapping[str, Any]] = None,
        confirm_apply: bool = False,
    ) -> Dict[str, Any]:
        current = self.load()
        op: Dict[str, Any] = {"op": RESOLVE, "episode_id": episode_id, "ended_at": ended_at or self.clock()}
        if facts is not None:
            op["facts"] = dict(facts)
        patch = ExperiencePatch.from_mapping(
            {"kind": EXPERIENCE_PATCH_KIND, "schema_version": SCHEMA_VERSION, "base_revision": current.revision, "ops": [op]}
        )
        return self.apply_patch(patch, confirm_apply=confirm_apply)

    def build_context(self, *, limit: int = MAX_CONTEXT_EPISODES, max_bytes: int = MAX_CONTEXT_BYTES) -> Dict[str, Any]:
        return build_experience_context(self.load(), limit=limit, max_bytes=max_bytes)

    def get_episode(self, episode_id: str) -> Optional[Episode]:
        target = _validate_id(episode_id, "episode_id")
        return next((item for item in self.load().episodes if item.episode_id == target), None)

    def has_episode(self, episode_id: str) -> bool:
        return self.get_episode(episode_id) is not None


def plan_experience_patch(
    patch: ExperiencePatch | Mapping[str, Any],
    *,
    store: Optional[ExperienceStore] = None,
) -> Dict[str, Any]:
    return (store or ExperienceStore()).plan_patch(patch)


def apply_experience_patch(
    patch: ExperiencePatch | Mapping[str, Any],
    *,
    store: Optional[ExperienceStore] = None,
    confirm_apply: bool = False,
) -> Dict[str, Any]:
    return (store or ExperienceStore()).apply_patch(patch, confirm_apply=confirm_apply)


def plan_source_event(
    event: SourceEvent | Mapping[str, Any],
    *,
    store: Optional[ExperienceStore] = None,
) -> Dict[str, Any]:
    return (store or ExperienceStore()).plan_source_event(event)


def apply_source_event(
    event: SourceEvent | Mapping[str, Any],
    *,
    store: Optional[ExperienceStore] = None,
    confirm_apply: bool = False,
) -> Dict[str, Any]:
    return (store or ExperienceStore()).apply_source_event(event, confirm_apply=confirm_apply)


def ingest_source_event(
    event: SourceEvent | Mapping[str, Any],
    *,
    store: Optional[ExperienceStore] = None,
    confirm_apply: bool = False,
) -> Dict[str, Any]:
    return apply_source_event(event, store=store, confirm_apply=confirm_apply)


def shadow_experience(
    *,
    store: Optional[ExperienceStore] = None,
    limit: int = MAX_CONTEXT_EPISODES,
    max_bytes: int = MAX_CONTEXT_BYTES,
) -> Dict[str, Any]:
    """Read-only production-safe inspection; it never creates state or locks."""

    target = store or ExperienceStore()
    state = target.load()
    context = build_experience_context(state, limit=limit, max_bytes=max_bytes)
    return {
        "status": "shadow",
        "revision": state.revision,
        "episode_count": len(state.episodes),
        "context": context,
    }


def _load_json_mapping(path: Path, label: str) -> Mapping[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError) as exc:
        raise ExperienceContractError(f"{label} JSON cannot be read") from exc
    return _assert_mapping(value, label)


def _load_patch_json(path: Path) -> Mapping[str, Any]:
    return _load_json_mapping(path, "patch JSON")


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Phase 2-I Experience Continuity boundary")
    parser.add_argument("mode", choices=(MODE_SHADOW, MODE_PLAN, MODE_APPLY), nargs="?", default=MODE_SHADOW)
    parser.add_argument("--store", type=Path, default=None)
    parser.add_argument("--patch-json", type=Path, default=None)
    parser.add_argument("--event-json", type=Path, default=None)
    parser.add_argument("--confirm-apply", action="store_true")
    args = parser.parse_args(argv)
    store = ExperienceStore(args.store)
    try:
        if args.mode == MODE_SHADOW:
            result = shadow_experience(store=store)
        else:
            if args.event_json is not None and args.patch_json is not None:
                raise ExperienceContractError("choose only one of --event-json and --patch-json")
            if args.event_json is not None:
                event = _load_json_mapping(args.event_json, "source event JSON")
                result = (
                    store.plan_source_event(event)
                    if args.mode == MODE_PLAN
                    else store.apply_source_event(event, confirm_apply=args.confirm_apply)
                )
            else:
                if args.patch_json is None:
                    raise ExperienceContractError("--patch-json or --event-json is required for PLAN/APPLY")
                patch = _load_patch_json(args.patch_json)
                result = (
                    store.plan_patch(patch)
                    if args.mode == MODE_PLAN
                    else store.apply_patch(patch, confirm_apply=args.confirm_apply)
                )
    except (ExperienceContractError, ExperienceStoreError) as exc:
        print(json.dumps({"status": "rejected", "reason": str(exc)}, ensure_ascii=False))
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI smoke is covered remotely
    raise SystemExit(main())
