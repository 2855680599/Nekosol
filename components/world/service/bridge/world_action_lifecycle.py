#!/usr/bin/env python3
"""M15B: world_action_lifecycle.py — the canonical Action Lifecycle.

One record per action attempt, keyed by ``execution_id`` (the idempotency key
the Resolver already uses).  States and the transitions between them are
explicit; illegal transitions fail closed.

    CREATED -> AUTHORIZED -> SUBMITTING -> SUBMITTED -> (APPLIED | REJECTED |
                                                          STALE | UNKNOWN)
    any non-terminal -> ABORTED | CANCELLED
    UNKNOWN -> reconciled into a terminal state (never terminal by itself)

Storage is append-only JSONL: every transition is one line.  The current state
is derived by replaying the file, so a crash can never leave a half-written
record, and no history is ever lost.

This module holds *infrastructure evidence*: none of these fields may enter
model-facing surfaces (M15E owns the perceptual projection).
"""

from __future__ import annotations

import fcntl
import json
import os
import pathlib
import time
from typing import Any, Mapping, Optional

SCHEMA = "world.action.lifecycle.v1"
KIND_TRANSITION = "world_action_lifecycle_transition"

#: lifecycle states
CREATED = "CREATED"
AUTHORIZED = "AUTHORIZED"
SUBMITTING = "SUBMITTING"
SUBMITTED = "SUBMITTED"
APPLIED = "APPLIED"
REJECTED = "REJECTED"
STALE = "STALE"
UNKNOWN = "UNKNOWN"
ABORTED = "ABORTED"
CANCELLED = "CANCELLED"

STATES = (CREATED, AUTHORIZED, SUBMITTING, SUBMITTED, APPLIED, REJECTED,
          STALE, UNKNOWN, ABORTED, CANCELLED)

#: terminal states.  UNKNOWN is deliberately absent: an unknown result must be
#: reconciled against the canonical ledger before it can become terminal.
TERMINAL = (APPLIED, REJECTED, STALE, ABORTED, CANCELLED)

#: the only legal transitions
ALLOWED = {
    CREATED: (AUTHORIZED, ABORTED, CANCELLED),
    AUTHORIZED: (SUBMITTING, ABORTED, CANCELLED),
    SUBMITTING: (SUBMITTED, UNKNOWN, ABORTED, CANCELLED),
    SUBMITTED: (APPLIED, REJECTED, STALE, UNKNOWN),
    UNKNOWN: (APPLIED, REJECTED, STALE, ABORTED, CANCELLED),
    APPLIED: (), REJECTED: (), STALE: (), ABORTED: (), CANCELLED: (),
}

DEFAULT_PATH_ENV = "WORLD_ACTION_LIFECYCLE_PATH"

#: provenance fields every record carries (infrastructure evidence)
PROVENANCE_FIELDS = (
    "decision_id", "authorization_id", "intent_id", "execution_id", "actor_id",
    "verb", "target", "failure_class", "world_event_ref", "body_consequence_ref",
    "origin", "detail",
)


class LifecycleError(ValueError):
    """Raised on an illegal transition, unknown record, or bad input."""


def default_path(environment: Optional[Mapping[str, str]] = None) -> pathlib.Path:
    env = os.environ if environment is None else environment
    explicit = env.get(DEFAULT_PATH_ENV)
    if explicit:
        return pathlib.Path(str(explicit))
    for key in ("CHIYO_WORLD_HOME", "HERMES_HOME"):
        base = env.get(key)
        if base:
            run = pathlib.Path(str(base)) / "run"
            if run.is_dir():
                return run / "action_lifecycle.jsonl"
    return pathlib.Path("./data/world/run/action_lifecycle.jsonl")


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + "Z"


class ActionLifecycle:
    """Append-only lifecycle recorder.

    ``STATES`` / ``TERMINAL`` / ``ALLOWED`` are also exposed as class
    attributes so callers can reason about the machine without importing the
    module-level names.
    """

    STATES = STATES
    TERMINAL = TERMINAL
    ALLOWED = ALLOWED

    def __init__(self, path: Optional[pathlib.Path | str] = None) -> None:
        self.path = pathlib.Path(path) if path else default_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)

    # ---- storage ---------------------------------------------------------
    def _locked(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(self.path, "a+", encoding="utf-8")
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        return handle

    def _read_all(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        records = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except ValueError:
                continue
        return records

    def _append(self, record: Mapping[str, Any]) -> dict[str, Any]:
        payload = dict(record)
        payload.setdefault("kind", KIND_TRANSITION)
        payload.setdefault("schema_version", SCHEMA)
        line = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        with self._locked() as handle:
            handle.write(line + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        return payload

    # ---- state derivation ------------------------------------------------
    @staticmethod
    def _key(record: Mapping[str, Any]) -> str:
        execution_id = str(record.get("execution_id") or "").strip()
        if not execution_id:
            raise LifecycleError("execution_id is required")
        return execution_id

    def states(self) -> dict[str, dict[str, Any]]:
        """execution_id -> {state, record fields, history}."""
        out: dict[str, dict[str, Any]] = {}
        for rec in self._read_all():
            try:
                key = self._key(rec)
            except LifecycleError:
                continue
            entry = out.setdefault(key, {"execution_id": key, "state": None,
                                         "history": [], "record": {}})
            if rec.get("annotation"):
                entry.setdefault("annotations", []).append(rec.get("annotation_code"))
                continue
            state = str(rec.get("state") or "")
            entry["state"] = state
            entry["history"].append(state)
            for field in PROVENANCE_FIELDS + ("created_at", "authorized_at",
                                              "submitted_at", "resolved_at",
                                              "transitioned_at"):
                if rec.get(field) is not None:
                    entry["record"][field] = rec[field]
        return out

    def get(self, execution_id: str) -> Optional[dict[str, Any]]:
        return self.states().get(str(execution_id))

    # ---- transitions -----------------------------------------------------
    def begin(self, *, execution_id: str, decision_id: str, intent_id: str,
              verb: str, actor_id: str = "chiyo", target: Any = None,
              origin: str = "production", detail: str = "") -> dict[str, Any]:
        key = str(execution_id).strip()
        if not key:
            raise LifecycleError("execution_id is required")
        existing = self.get(key)
        if existing is not None:
            # idempotent restart: same execution_id must not create a second
            # lifecycle record
            if existing["state"] in (CREATED, AUTHORIZED, SUBMITTING,
                                     SUBMITTED, UNKNOWN):
                return existing["record"]
            raise LifecycleError(
                "lifecycle record for %s already exists in terminal state %s"
                % (key, existing["state"]))
        return self._append({
            "execution_id": key, "state": CREATED,
            "decision_id": decision_id, "intent_id": intent_id,
            "actor_id": actor_id, "verb": verb, "target": target,
            "origin": origin, "detail": detail, "created_at": _now(),
            "transitioned_at": _now(),
        })

    def transition(self, *, execution_id: str, state: str,
                   **fields: Any) -> dict[str, Any]:
        key = str(execution_id).strip()
        if state not in STATES:
            raise LifecycleError("unknown lifecycle state %r" % state)
        current = self.get(key)
        if current is None:
            raise LifecycleError("no lifecycle record for %s" % key)
        previous = current["state"]
        if state not in ALLOWED.get(previous, ()):
            raise LifecycleError(
                "illegal transition %s -> %s for %s" % (previous, state, key))
        payload = {"execution_id": key, "state": state,
                   "previous_state": previous, "transitioned_at": _now()}
        stamp = {"AUTHORIZED": "authorized_at", "SUBMITTING": "submitted_at",
                 "SUBMITTED": "submitted_at"}
        if state in stamp:
            payload.setdefault(stamp[state], _now())
        if state in TERMINAL:
            payload["resolved_at"] = _now()
        for field in PROVENANCE_FIELDS:
            if fields.get(field) is not None:
                payload[field] = fields[field]
        return self._append(payload)

    def annotate(self, *, execution_id: str, code: str, note: str) -> dict[str, Any]:
        """Append an annotation to an existing record without changing state.

        History is never rewritten: the annotation is one more line, so a
        terminal record can be flagged (e.g. "this APPLIED came from a harness
        whose socket never committed anything") while its state stays honest.
        """

        key = str(execution_id).strip()
        entry = self.get(key)
        if entry is None:
            raise LifecycleError("no lifecycle record for %s" % key)
        return self._append({
            "execution_id": key, "state": entry["state"],
            "annotation": True, "annotation_code": code, "note": note,
            "transitioned_at": _now(),
        })

    def is_terminal(self, execution_id: str) -> bool:
        entry = self.get(execution_id)
        return bool(entry) and entry["state"] in TERMINAL

    # ---- reconciliation -------------------------------------------------
    def open_records(self) -> list[dict[str, Any]]:
        """Non-terminal records (i.e. what could become an orphan)."""
        return [entry for entry in self.states().values()
                if entry["state"] not in TERMINAL]

    def advance(self, *, execution_id: str, target: str, **fields: Any) -> dict[str, Any]:
        """Walk the legal path from the current state to ``target``."""

        key = str(execution_id).strip()
        entry = self.get(key)
        if entry is None:
            raise LifecycleError("no lifecycle record for %s" % key)
        path = self._path(entry["state"], target)
        if path is None:
            raise LifecycleError("illegal lifecycle path %s -> %s"
                                 % (entry["state"], target))
        record = entry["record"]
        for step in path:
            kwargs = {k: v for k, v in record.items()
                      if k not in ("execution_id", "state", "previous_state")}
            kwargs.update({k: v for k, v in fields.items() if v is not None})
            record = self.transition(execution_id=key, state=step, **kwargs)
        return self.get(key)  # type: ignore[return-value]

    @staticmethod
    def _path(current: str, target: str) -> Optional[list[str]]:
        if current == target:
            return []
        seen = {current}
        queue: list[tuple[str, list[str]]] = [(current, [])]
        while queue:
            node, path = queue.pop(0)
            for nxt in ALLOWED.get(node, ()):
                if nxt in seen:
                    continue
                new_path = path + [nxt]
                if nxt == target:
                    return new_path
                seen.add(nxt)
                queue.append((nxt, new_path))
        return None

    def reconcile(self, *, ledger_index, consequence_index=None) -> dict[str, Any]:
        """Terminalise every non-terminal record from canonical evidence.

        ``ledger_index`` maps execution_id -> "committed" | "prepared" | None.
        Rules (fail-closed, never assume success):
          committed                     -> APPLIED   (the world already moved)
          prepared but not committed     -> UNKNOWN then ABORTED after grace
          neither                        -> ABORTED_INTEGRATION_ERROR
        """

        outcome = {"applied": [], "aborted": [], "still_unknown": []}
        for entry in self.open_records():
            key = entry["execution_id"]
            state = entry["state"]
            ledger_state = ledger_index.get(key)
            if ledger_state == "committed":
                refs = {}
                if consequence_index:
                    refs = consequence_index.get(key, {}) or {}
                self.advance(execution_id=key, target=APPLIED,
                             origin=str(entry["record"].get("origin") or ""),
                             world_event_ref=refs.get("world_event_ref"),
                             body_consequence_ref=refs.get("consequence_ref"))
                outcome["applied"].append(key)
            elif ledger_state in (None, "prepared"):
                if state in (CREATED, AUTHORIZED):
                    self.advance(execution_id=key, target=ABORTED,
                                 failure_class="ABORTED_INTEGRATION_ERROR")
                    outcome["aborted"].append(key)
                else:
                    self.advance(execution_id=key, target=ABORTED,
                                 failure_class="ABORTED_NO_COMMIT_EVIDENCE")
                    outcome["aborted"].append(key)
            else:
                outcome["still_unknown"].append(key)
        return outcome


    def reconcile_from_evidence(self, *, ledger_index, decision_records,
                                consequence_index=None) -> dict[str, Any]:
        """Reconcile using BOTH the ledger and the decision log as evidence.

        A Decision record may name an execution that never reached the
        executor (crash point 1): without ingesting the decision log those
        intents would stay invisible and un-terminalised.  Records created
        here carry the real decision/intent provenance and are then resolved
        by the standard rules — never assumed successful.
        """

        ingested = []
        known = self.states()
        for rec in decision_records:
            if rec.get("terminal_decision") != "ACTION_SELECTED":
                continue
            execution_id = str(rec.get("execution_id") or "")
            if not execution_id or execution_id in known:
                continue
            self.begin(execution_id=execution_id,
                       decision_id=str(rec.get("decision_id") or ""),
                       intent_id=str(rec.get("intent_id") or ""),
                       verb=str(rec.get("verb") or "POSE"),
                       origin="decision_log_reconciliation",
                       detail="ingested from the decision log: no executor entry")
            ingested.append(execution_id)
        result = self.reconcile(ledger_index=ledger_index,
                               consequence_index=consequence_index)
        result["ingested_from_decision_log"] = ingested
        return result


def ledger_index(path: Optional[pathlib.Path | str] = None) -> dict[str, str]:
    """execution_id -> 'committed' | 'prepared' from the canonical ledger."""

    p = pathlib.Path(path) if path else pathlib.Path(
        "./data/world/data/life_execution.jsonl")
    index: dict[str, str] = {}
    if not p.exists():
        return index
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if rec.get("kind") != "world_execution_ledger_entry":
            continue
        key = str(rec.get("execution_id") or "")
        if not key:
            continue
        if rec.get("status") == "committed":
            index[key] = "committed"
        else:
            index.setdefault(key, "prepared")
    return index
