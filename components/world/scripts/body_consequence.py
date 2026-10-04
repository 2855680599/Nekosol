#!/usr/bin/env python3
"""Body Consequence Adapter (M9) — the only legal World-result -> Body path.

Chain
-----
    Action Proposal -> World Resolver -> Canonical Result -> Execution Ledger
      -> Body Consequence Adapter -> Body Runtime -> BodyTransition -> BodySignal

The adapter's whole job is:

    Canonical ActionResult + World/Substrate facts  ->  BodyRuntimeInput

The Resolver never learns ``fatigue`` / ``activation`` / ``comfort`` /
``tension``.  It emits facts only; the body interprets its own physical
consequences.

Hard rules
----------
* **SUCCESS only.**  A FAILURE / REJECTED result may not apply success
  consequences.  An UNKNOWN result is held in ``pending_body_consequence`` and
  is never guessed, never applied early, never auto-retried.
* **Exactly once.**  One ``execution_id`` maps to at most one canonical
  consequence outcome, survived across resolver retry, restart, and ledger
  replay.
* **Deterministic.**  Same ActionResult + same World facts + same previous body
  state + same runtime version -> same BodyRuntimeInput.  No LLM, no random, no
  wall clock.
* **No verb knowledge in the body.**  The adapter maps action facts to a load
  class; the runtime decides how that moves the variables.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
from collections.abc import Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Optional

SCHEMA_CONSEQUENCE = "body.consequence.v1"
KIND_CONSEQUENCE = "body_consequence"
KIND_PENDING = "pending_body_consequence"

#: Laws of the adapter: which action types may produce a body consequence.
SUPPORTED_ACTIONS = ("MOVE", "POSE", "REST", "PICK_UP", "PLACE")

#: Movement duration is not carried by the resolver result today, so a
#: deterministic per-action default is used and recorded as an assumption.
#: This is a load model, NOT an activity label.
MOVEMENT_SECONDS_BY_ACTION = {
    "MOVE": 600.0,
    "POSE": 5.0,
}

#: Effort classification per action type.  The resolver stays ignorant of it.
EFFORT_BY_ACTION = {
    "MOVE": "moderate",
    "POSE": "light",
    "PICK_UP": "light",
    "PLACE": "light",
    "REST": "light",
}

#: Actions that may open a recovery session when they SUCCEED.
RECOVERY_ACTIONS = ("REST",)

#: Actions whose posture consequence is worth recording.
POSTURE_ACTIONS = ("POSE", "MOVE")

STATUS_SUCCESS = frozenset({"applied", "already_applied"})
STATUS_FAILURE = frozenset({"rejected", "failed", "disabled", "noop"})
STATUS_UNKNOWN = frozenset({"prepared", "uncertain"})

CONSEQUENCE_STATE_PENDING = "pending"
CONSEQUENCE_STATE_APPLIED = "applied"
CONSEQUENCE_STATE_SKIPPED = "skipped"

MAX_RECORDS = 4096


class BodyConsequenceError(ValueError):
    """The consequence contract was violated."""


def _text(value: Any, label: str, *, maximum: int = 256) -> str:
    if not isinstance(value, str):
        raise BodyConsequenceError(f"{label} must be a string")
    text = value.strip()
    if not text or len(text) > maximum:
        raise BodyConsequenceError(f"{label} must be non-empty and bounded")
    return text


def _detach(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False))


def _digest(value: Any) -> str:
    payload = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def classify_status(result: Mapping[str, Any]) -> str:
    """Map a canonical ActionResult onto SUCCESS / FAILURE / UNKNOWN."""

    status = str(result.get("status") or "")
    if status in STATUS_SUCCESS:
        return "SUCCESS"
    if status in STATUS_UNKNOWN:
        return "UNKNOWN"
    if status in STATUS_FAILURE:
        return "FAILURE"
    # an unrecognised status is never treated as success
    return "UNKNOWN"


def consequence_id(execution_id: str, action_type: str, body_id: str) -> str:
    """Deterministic identity.  Same execution -> same id, across restarts."""

    identity = {
        "execution_id": _text(execution_id, "execution_id"),
        "action_type": _text(action_type, "action_type", maximum=64),
        "body_id": _text(body_id, "body_id"),
    }
    return "body-consequence:" + _digest(identity)[:32]


# --------------------------------------------------------------------------
# the mapping  (Resolver facts -> BodyRuntimeInput)
# --------------------------------------------------------------------------


def build_consequence(
    result: Mapping[str, Any],
    *,
    body_id: str,
    world_facts: Optional[Mapping[str, Any]] = None,
    pose: Optional[str] = None,
    object_id: Optional[str] = None,
    applied_at: Optional[str] = None,
) -> dict[str, Any]:
    """Derive the body input from a canonical result.  Facts in, facts out."""

    execution_id = _text(result.get("execution_id"), "execution_id")
    action_type = result.get("action") or result.get("action_type")
    action = _text(action_type, "action_type", maximum=64)
    verdict = classify_status(result)

    record: dict[str, Any] = {
        "kind": KIND_CONSEQUENCE,
        "schema_version": SCHEMA_CONSEQUENCE,
        "consequence_id": consequence_id(execution_id, action, body_id),
        "execution_id": execution_id,
        "body_id": _text(body_id, "body_id"),
        "action_type": action,
        "result_ref": _digest(_detach(dict(result))),
        "result_status": str(result.get("status") or ""),
        "verdict": verdict,
        "world_revision": (world_facts or {}).get("world_revision"),
        "world_ref": (world_facts or {}).get("world_id"),
        "applied_at": applied_at,
        "body_transition_ref": None,
        "state": CONSEQUENCE_STATE_PENDING,
        "inputs": [],
        "notes": [],
        "raw_body_values": False,
    }

    if verdict != "SUCCESS":
        # FAILURE applies nothing.  UNKNOWN waits for reconciliation.
        record["state"] = (
            CONSEQUENCE_STATE_SKIPPED if verdict == "FAILURE" else CONSEQUENCE_STATE_PENDING
        )
        record["notes"].append(
            "failure_applies_no_consequence" if verdict == "FAILURE" else "awaiting_reconciliation"
        )
        return record

    if action not in SUPPORTED_ACTIONS:
        record["state"] = CONSEQUENCE_STATE_SKIPPED
        record["notes"].append(f"unsupported_action:{action}")
        return record

    inputs: list[dict[str, Any]] = []

    if action in MOVEMENT_SECONDS_BY_ACTION:
        item: dict[str, Any] = {
            "class": "PHYSICAL_RESULT",
            "movement_duration_seconds": MOVEMENT_SECONDS_BY_ACTION[action],
            "effort": EFFORT_BY_ACTION[action],
            "execution_id": execution_id,
        }
        if pose:
            item["pose"] = pose
        inputs.append(item)
        record["notes"].append(f"movement_load_assumed:{MOVEMENT_SECONDS_BY_ACTION[action]}s")

    elif action in ("PICK_UP", "PLACE"):
        # a small manipulation: resource context, deliberately tiny load so the
        # runtime can decide whether it is even enough to form a transition
        inputs.append(
            {
                "class": "PHYSICAL_RESULT",
                "movement_duration_seconds": 2.0,
                "effort": EFFORT_BY_ACTION[action],
                "execution_id": execution_id,
            }
        )
        record["resource_ref"] = object_id
        record["notes"].append("manipulation_load_minimal")

    if action in POSTURE_ACTIONS and pose:
        inputs.append(
            {
                "class": "PHYSICAL_RESULT",
                "posture_seconds": 60.0,
                "pose": pose,
                "execution_id": execution_id,
            }
        )

    if action in RECOVERY_ACTIONS:
        mode = result.get("recovery_mode") or "rest"
        inputs.append({"class": "RECOVERY", "mode": str(mode), "state": "start"})

    if not inputs:
        record["state"] = CONSEQUENCE_STATE_SKIPPED
        record["notes"].append("no_body_input_for_action")
        return record

    latest = result.get("after")
    if isinstance(latest, Mapping) and isinstance(latest.get("pose"), str):
        for item in inputs:
            if item["class"] == "PHYSICAL_RESULT" and "pose" not in item:
                item["pose"] = latest["pose"]

    record["inputs"] = inputs
    record["input_summary"] = {
        "count": len(inputs),
        "classes": sorted({item["class"] for item in inputs}),
    }
    return record


def validate_consequence(record: Any) -> dict[str, Any]:
    if not isinstance(record, Mapping):
        raise BodyConsequenceError("consequence must be an object")
    if record.get("kind") != KIND_CONSEQUENCE:
        raise BodyConsequenceError("consequence kind is not supported")
    if record.get("schema_version") != SCHEMA_CONSEQUENCE:
        raise BodyConsequenceError("consequence schema is not supported")
    if record.get("state") not in (
        CONSEQUENCE_STATE_PENDING,
        CONSEQUENCE_STATE_APPLIED,
        CONSEQUENCE_STATE_SKIPPED,
    ):
        raise BodyConsequenceError("consequence state is not supported")
    return _detach(dict(record))


# --------------------------------------------------------------------------
# exactly-once journal + pending queue
# --------------------------------------------------------------------------


class ConsequenceLedger:
    """Append-only consequence records with deterministic replay to a state map.

    Recovery is by *reading history*, not by trusting a mutable pointer, so a
    crash between "body applied" and "reference finalised" cannot double-apply.
    """

    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.lock_path = self.path.with_name(self.path.name + ".lock")

    @contextmanager
    def locked(self) -> Any:
        if fcntl is None:
            raise BodyConsequenceError("consequence ledger lock unavailable")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.lock_path.open("a+") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield self.read_all()
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
                records.append(validate_consequence(json.loads(line)))
            except (TypeError, ValueError) as exc:
                raise BodyConsequenceError(
                    f"consequence ledger malformed at line {number}"
                ) from exc
        return records

    def append_unlocked(self, record: Mapping[str, Any]) -> dict[str, Any]:
        normalized = validate_consequence(record)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(
                    json.dumps(normalized, ensure_ascii=False, sort_keys=True) + "\n"
                )
                handle.flush()
                os.fsync(handle.fileno())
        except OSError as exc:
            raise BodyConsequenceError("consequence ledger cannot be appended") from exc
        return normalized

    def append(self, record: Mapping[str, Any]) -> dict[str, Any]:
        with self.locked():
            return self.append_unlocked(record)

    # -- deterministic views --------------------------------------------

    @staticmethod
    def current_state(records: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
        """Latest record per consequence_id, in first-seen order."""

        ordered: dict[str, dict[str, Any]] = {}
        for record in records:
            ordered[record["consequence_id"]] = dict(record)
        return ordered

    def state_of(self, cid: str) -> Optional[dict[str, Any]]:
        return self.current_state(self.read_all()).get(cid)

    def applied_execution_ids(self) -> set[str]:
        return {
            record["execution_id"]
            for record in self.current_state(self.read_all()).values()
            if record["state"] == CONSEQUENCE_STATE_APPLIED
        }

    def pending(self) -> list[dict[str, Any]]:
        return [
            record
            for record in self.current_state(self.read_all()).values()
            if record["state"] == CONSEQUENCE_STATE_PENDING
        ]

    def pending_body_consequence(self) -> list[dict[str, Any]]:
        """The ticket's ``pending_body_consequence`` queue (UNKNOWN results)."""

        return [
            record
            for record in self.pending()
            if record["verdict"] == "UNKNOWN"
        ]


# --------------------------------------------------------------------------
# application
# --------------------------------------------------------------------------


def apply_consequence(
    record: Mapping[str, Any],
    runtime_state: Mapping[str, Any],
    *,
    at: Any,
    runtime_version: Optional[str] = None,
) -> dict[str, Any]:
    """Advance the body by this consequence.  Pure; the caller persists."""

    import body_runtime as br

    validated = validate_consequence(record)
    if validated["state"] == CONSEQUENCE_STATE_APPLIED:
        return {
            "applied": False,
            "reason": "already_applied",
            "consequence": validated,
            "state": runtime_state,
            "transition": None,
        }
    if validated["verdict"] != "SUCCESS":
        return {
            "applied": False,
            "reason": f"verdict_{validated['verdict'].lower()}",
            "consequence": validated,
            "state": runtime_state,
            "transition": None,
        }
    if not validated["inputs"]:
        return {
            "applied": False,
            "reason": "no_body_input",
            "consequence": validated,
            "state": runtime_state,
            "transition": None,
        }

    before = br.validate_runtime(runtime_state)
    from_time = before["last_advanced_at"] or at
    result = br.advance(
        before, from_time, at, validated["inputs"], runtime_version or before["runtime_version"]
    )

    transition = None
    if result["advanced"]:
        transition = br.build_transition(
            before,
            result["state"],
            at=at,
            cause_class=br.CAUSE_PHYSICAL,
            cause_classes=result["cause_classes"],
            input_summary={
                **result["inputs_summary"],
                "execution_id": validated["execution_id"],
                "consequence_id": validated["consequence_id"],
                "input_refs": [validated["consequence_id"]],
            },
        )

    updated = dict(validated)
    updated["state"] = CONSEQUENCE_STATE_APPLIED
    updated["applied_at"] = (
        transition["valid_from"] if transition else str(at)
    )
    updated["body_transition_ref"] = transition["transition_id"] if transition else None
    updated["body_version_before"] = before["version"]
    updated["body_version_after"] = result["state"]["version"]
    updated["notes"] = list(validated["notes"]) + (
        [] if result["advanced"] else ["advance_produced_no_change"]
    )

    return {
        "applied": True,
        "reason": "applied",
        "consequence": validate_consequence(updated),
        "state": result["state"],
        "transition": transition,
        "advance": result,
    }


def apply_for_execution(
    result: Mapping[str, Any],
    *,
    body_id: str,
    ledger: ConsequenceLedger,
    runtime_state: Mapping[str, Any],
    at: Any,
    world_facts: Optional[Mapping[str, Any]] = None,
    pose: Optional[str] = None,
    object_id: Optional[str] = None,
    runtime_version: Optional[str] = None,
) -> dict[str, Any]:
    """End-to-end, exactly-once application for one execution.

    Safe against resolver retry, restart and ledger replay: a repeated
    ``execution_id`` returns the already-recorded outcome instead of applying a
    second time.
    """

    import body_runtime as br

    execution_id = _text(result.get("execution_id"), "execution_id")
    action = str(result.get("action") or result.get("action_type") or "")
    cid = consequence_id(execution_id, action, body_id)

    with ledger.locked() as records:
        existing = ledger.current_state(records).get(cid)
        if existing is not None and existing["state"] == CONSEQUENCE_STATE_APPLIED:
            return {
                "applied": False,
                "reason": "duplicate_execution_id",
                "consequence": existing,
                "state": runtime_state,
                "transition": None,
            }

        record = build_consequence(
            result,
            body_id=body_id,
            world_facts=world_facts,
            pose=pose,
            object_id=object_id,
            applied_at=str(at),
        )
        if record["state"] == CONSEQUENCE_STATE_SKIPPED:
            ledger.append_unlocked(record)
            return {
                "applied": False,
                "reason": f"skipped:{','.join(record['notes'])}",
                "consequence": record,
                "state": runtime_state,
                "transition": None,
            }
        if record["verdict"] == "UNKNOWN":
            ledger.append_unlocked(record)
            return {
                "applied": False,
                "reason": "pending_reconciliation",
                "consequence": record,
                "state": runtime_state,
                "transition": None,
            }

        outcome = apply_consequence(
            record, runtime_state, at=at, runtime_version=runtime_version
        )
        ledger.append_unlocked(outcome["consequence"])
        return {
            **outcome,
            "reason": "applied" if outcome["applied"] else outcome["reason"],
        }


def reconcile_pending(
    ledger: ConsequenceLedger,
    *,
    execution_id: str,
    confirmed_result: Mapping[str, Any],
    body_id: str,
    runtime_state: Mapping[str, Any],
    at: Any,
    world_facts: Optional[Mapping[str, Any]] = None,
    pose: Optional[str] = None,
    runtime_version: Optional[str] = None,
) -> dict[str, Any]:
    """Apply a previously UNKNOWN consequence, but only once SUCCESS is confirmed."""

    verdict = classify_status(confirmed_result)
    if verdict != "SUCCESS":
        return {
            "applied": False,
            "reason": f"not_confirmed_success:{verdict.lower()}",
            "state": runtime_state,
            "transition": None,
        }
    return apply_for_execution(
        confirmed_result,
        body_id=body_id,
        ledger=ledger,
        runtime_state=runtime_state,
        at=at,
        world_facts=world_facts,
        pose=pose,
        runtime_version=runtime_version,
    )