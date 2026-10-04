#!/usr/bin/env python3
"""M15E: experience candidates derived from canonical observation."""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
import time
from typing import Any, Iterable, Mapping, Optional

SCHEMA = "world.experience.candidate.v1"
KIND_CANDIDATE = "world_experience_candidate"
KIND_CORRECTION = "world_experience_correction"

#: outcome vocabulary -> how the experience may truthfully be phrased
OUTCOME_PHRASING = {
    "APPLIED": "did",
    "REJECTED": "attempted_failed",
    "STALE": "attempted_failed_stale",
    "UNKNOWN": "attempted_uncertain",
    "ABORTED": "attempted_aborted",
    "CANCELLED": "cancelled",
}

#: the perceptual rendering: what the body can notice about a pose change
POSE_WORDS = {"lying": "躺下", "seated": "坐下", "standing": "站起"}

DEFAULT_SPOOL_ENV = "WORLD_EXPERIENCE_SPOOL"


def default_path(environment: Optional[Mapping[str, str]] = None) -> pathlib.Path:
    env = os.environ if environment is None else environment
    explicit = env.get(DEFAULT_SPOOL_ENV)
    if explicit:
        return pathlib.Path(str(explicit))
    for key in ("CHIYO_WORLD_HOME", "HERMES_HOME"):
        base = env.get(key)
        if base:
            run = pathlib.Path(str(base)) / "run"
            if run.is_dir():
                return run / "experience_candidates.jsonl"
    return pathlib.Path("./data/world/run/experience_candidates.jsonl")


def _key(decision_id: str, execution_id: str, identity: str) -> str:
    """Stable dedupe key: the *action identity*, never a post-state.

    ``identity`` is verb+target.  The observation taken after the action must
    not take part: an UNKNOWN action is re-observed later, and a key that moves
    with the observation would slip past dedupe and mint a twin.
    """

    payload = "%s|%s|%s" % (decision_id, execution_id, identity)
    return "experience:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


def _identity(verb: str, target: Any) -> str:
    """The action identity: which verb, against which target."""

    try:
        rendered = json.dumps(target or {}, sort_keys=True, ensure_ascii=False)
    except (TypeError, ValueError):
        rendered = str(target)
    return "%s:%s" % (verb or "", rendered)
def build_candidate(*, lifecycle_record: Mapping[str, Any],
                    observation: Mapping[str, Any]) -> dict[str, Any]:
    """Turn (lifecycle record, canonical observation) into one experience.

    ``observation`` must be the bounded observation the body actually has
    (location / pose / hands) — never a ledger row.
    """

    record = dict(lifecycle_record or {})
    state = str(record.get("state") or "")
    decision_id = str(record.get("decision_id") or "")
    execution_id = str(record.get("execution_id") or "")
    verb = str(record.get("verb") or "")
    target = record.get("target") or {}
    pose_after = str((observation or {}).get("pose") or "")

    phrasing = OUTCOME_PHRASING.get(state, "attempted_uncertain")
    # what the body can notice: the pose it ended up in, from observation only
    noticed = POSE_WORDS.get(pose_after, pose_after or "unknown")
    if phrasing == "did":
        summary = noticed
        assert_kind = "completed"
    else:
        summary = "尝试过(%s)，结果=%s" % (verb or "action", state)
        assert_kind = phrasing

    candidate = {
        "kind": KIND_CANDIDATE,
        "schema_version": SCHEMA,
        "experience_key": _key(decision_id, execution_id,
                               _identity(verb, target)),
        "provenance": {"decision_id": decision_id, "execution_id": execution_id,
                       "verb": verb, "target": target},
        "assertion": assert_kind,          # completed | attempted_* | cancelled
        "summary": summary,                # bounded, factual, no identifiers
        "observation": {"pose": pose_after,
                        "location": (observation or {}).get("location")},
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "correction_of": None,
    }
    return candidate


def build_correction(*, candidate: Mapping[str, Any], reconciled_state: str,
                     observation: Mapping[str, Any]) -> dict[str, Any]:
    """An UNKNOWN that later became APPLIED yields a correction, not a twin."""

    fixed = dict(candidate)
    fixed["kind"] = KIND_CORRECTION
    fixed["correction_of"] = candidate.get("experience_key")
    fixed["assertion"] = ("completed" if reconciled_state == "APPLIED"
                          else OUTCOME_PHRASING.get(reconciled_state,
                                                    "attempted_uncertain"))
    pose_after = str((observation or {}).get("pose") or "")
    fixed["summary"] = (POSE_WORDS.get(pose_after, pose_after)
                        if reconciled_state == "APPLIED"
                        else "尝试过，结果=%s" % reconciled_state)
    fixed["observation"] = {"pose": pose_after,
                            "location": (observation or {}).get("location")}
    fixed["created_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    return fixed


class ExperienceSpool:
    """Append-only spool + dedupe by canonical provenance.

    The spool is the ingress boundary: the native memory runtime (or its
    adapter) consumes these records.  Nothing here writes memory directly.
    """

    def __init__(self, path: Optional[pathlib.Path | str] = None) -> None:
        self.path = pathlib.Path(path) if path else default_path()

    def _read(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        out = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                try:
                    out.append(json.loads(line))
                except ValueError:
                    continue
        return out

    def keys(self) -> set[str]:
        keys = set()
        for rec in self._read():
            if rec.get("experience_key"):
                keys.add(str(rec["experience_key"]))
            if rec.get("correction_of"):
                keys.add(str(rec["correction_of"]))
        return keys

    def append(self, record: Mapping[str, Any]) -> dict[str, Any]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = dict(record)
        with open(self.path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False,
                                    sort_keys=True) + "\n")
        return payload

    def record(self, *, lifecycle_record: Mapping[str, Any],
               observation: Mapping[str, Any]) -> Optional[dict[str, Any]]:
        """Build + dedupe + append.  Returns None when already recorded."""

        candidate = build_candidate(lifecycle_record=lifecycle_record,
                                    observation=observation)
        if candidate["experience_key"] in self.keys():
            return None
        return self.append(candidate)

    def correct(self, *, lifecycle_record: Mapping[str, Any],
                reconciled_state: str, observation: Mapping[str, Any]
                ) -> Optional[dict[str, Any]]:
        """Emit a correction for a previously uncertain experience."""

        candidate = build_candidate(lifecycle_record=lifecycle_record,
                                    observation=observation)
        existing = [r for r in self._read()
                    if r.get("experience_key") == candidate["experience_key"]
                    and r.get("assertion") != "completed"]
        if not existing:
            return None
        correction = build_correction(candidate=existing[-1],
                                      reconciled_state=reconciled_state,
                                      observation=observation)
        return self.append(correction)
