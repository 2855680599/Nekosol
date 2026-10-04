#!/usr/bin/env python3
"""Structured Action Intent V0 — Decision output to World action (this ticket).

This module ports the **sealed intent contract** of the legacy Life path
(``life_choice`` + ``life_execute.build_execution_ticket``) onto the WorldBody
bridge, without reviving the legacy write path (ticket section 8).

Authorities (ticket section 2) — each layer does exactly one thing:

===========  =====================================================
layer        authority
===========  =====================================================
Decision     what is wanted (``life_choice.select_and_validate``)
Intent       an existing decision, as a structured, fingerprint-
             bound request (this module)
Bridge       transport + validation + fresh dependency read
Resolver     whether reality allows it (sole commit)
World        canonical facts
Expression   describing results only
===========  =====================================================

Hard rules enforced here:

* an intent can only be built from a **validated** choice bound to an action
  space snapshot (no orphan actions, ticket section 4);
* dependency evidence is produced **here**, by a fresh authority-side read —
  never by the model (section 5 / the M13B frozen rule);
* PICK_UP / PLACE affordances are never emitted (section 6);
* the mutation gate (off / shadow / canonical) is checked **before** any
  Resolver contact, and shadow never commits (section 7);
* nothing in this module writes the World directly.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping, Optional, Sequence

SCHEMA_INTENT = "world.action.intent.v1"
KIND_INTENT = "world_action_intent"
KIND_SUBMISSION = "world_action_intent_submission"

#: Intent lifecycle (ticket section 4).
STATUS_PROPOSED = "PROPOSED"
STATUS_AUTHORIZED = "AUTHORIZED"
STATUS_SUBMITTED = "SUBMITTED"
STATUS_APPLIED = "APPLIED"
STATUS_REJECTED = "REJECTED"
STATUS_STALE = "STALE"
STATUS_UNKNOWN = "UNKNOWN"
STATUS_CANCELLED = "CANCELLED"
INTENT_STATUSES = (
    STATUS_PROPOSED, STATUS_AUTHORIZED, STATUS_SUBMITTED, STATUS_APPLIED,
    STATUS_REJECTED, STATUS_STALE, STATUS_UNKNOWN, STATUS_CANCELLED,
)

#: Verbs this ticket exposes.  M15 enabled PICK_UP / PLACE once the World could
#: declare an object's capability (see world_object_capability + the economy
#: catalog), so the gate is no longer "the verb is unknown" but "the object
#: says takeable/placeable".
EXPOSED_VERBS = ("MOVE", "POSE", "PICK_UP", "PLACE")

#: M15 §19: the production allowlist is explicit.  A future verb does NOT gain
#: permission by being added to the resolver; it must be added here on purpose.
ACTION_ALLOWLIST = ("MOVE", "POSE", "PICK_UP", "PLACE")

#: kept for compatibility with older callers/tests; M15 emptied it.
BLOCKED_VERBS: tuple[str, ...] = ()

#: Mutation gate (section 7).
GATE_ENV = "WORLD_BODY_INTENT_MUTATION_GATE"
GATE_OFF = "off"
GATE_SHADOW = "shadow"
GATE_CANONICAL = "canonical"
GATES = (GATE_OFF, GATE_SHADOW, GATE_CANONICAL)

SOURCE = "action_intent_v0"

#: Bounded text.
MAX_TEXT = 240


class ActionIntentError(ValueError):
    """The intent contract was violated by the caller."""


class IntentRefused(ActionIntentError):
    """A gate or exposure policy refused the intent before the Resolver."""


def _text(value: Any, label: str, *, maximum: int = MAX_TEXT) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ActionIntentError("%s must be a non-empty string" % label)
    text = value.strip()
    if len(text) > maximum:
        raise ActionIntentError("%s is too long" % label)
    return text


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------
# the World affordance space (bounded; the Decision selector's input)
# --------------------------------------------------------------------------


SUPPORTED_POSES = ("standing", "seated", "lying")


def build_action_space(
    snapshot: Mapping[str, Any],
    *,
    poses: Sequence[str] = SUPPORTED_POSES,
    available_actions: Optional[Mapping[str, Any]] = None,
) -> dict[str, Any]:
    """Build the bounded action space a Decision selector may choose from.

    ``snapshot`` is the gateway snapshot (``get_snapshot``) — location, pose,
    visible objects.  Candidates are **derived from the code topology and the
    live substrate**, never invented here.  PICK_UP/PLACE are not emitted at
    all (section 6): without a canonical takeable capability there is nothing
    to offer, and an object-name blacklist is exactly what section 6 forbids.
    """

    location = snapshot.get("location") or {}
    place_id = _text(location.get("place_id"), "location.place_id", maximum=128)
    area_id = _text(location.get("area_id"), "location.area_id", maximum=128)
    pose = _text(snapshot.get("pose"), "snapshot.pose", maximum=32)

    # visible objects, by label only (the model never sees raw ids)
    objects = []
    for obj in snapshot.get("visible_objects") or []:
        if isinstance(obj, Mapping) and obj.get("label"):
            objects.append({"label": str(obj["label"]), "kind": str(obj.get("kind"))})

    candidates: list[dict[str, Any]] = []
    # M15: object candidates come from the gateway's availability truth.  The
    # candidate_id is what a selector may choose; the raw object id stays here,
    # on the authority side, and is never put in front of the model.
    offer_pick_up = False
    offer_place = False
    # M15 §17: the gate owns the model-facing verdict.  With the gate off the
    # space must say RUNTIME_DISABLED instead of offering a fake AVAILABLE.
    gate_state = resolve_gate()
    gate_open = gate_state == GATE_CANONICAL
    if not gate_open and isinstance(available_actions, Mapping):
        available_actions = None
    if isinstance(available_actions, Mapping):
        for entry in available_actions.get("actions") or []:
            if not isinstance(entry, Mapping) or not entry.get("available"):
                continue
            if entry.get("verb") == "PICK_UP":
                offer_pick_up = True
                for offered in entry.get("candidates") or []:
                    object_id = str((offered or {}).get("object_id") or "")
                    if not object_id:
                        continue
                    slots = list((offered or {}).get("slots") or []) or ["right_hand"]
                    candidates.append({
                        "candidate_id": "pickup:%s" % object_id,
                        "verb": "PICK_UP",
                        "summary": "拿起一个物体",
                        "params": {"object_id": object_id, "slot_id": slots[0]},
                    })
            if entry.get("verb") == "PLACE":
                offer_place = True

    for target_pose in poses:
        if target_pose == pose:
            continue
        candidates.append({
            "candidate_id": "pose:%s" % target_pose,
            "verb": "POSE",
            "summary": "变为%s" % target_pose,
            "params": {"pose": target_pose},
        })

    return {
        "kind": "world_action_space",
        "schema_version": SCHEMA_INTENT,
        "snapshot_at": snapshot.get("captured_at"),
        "location": {"place_id": place_id, "area_id": area_id},
        "current_pose": pose,
        "world_revision": snapshot.get("world_revision"),  # internal, never model-facing
        "candidates": candidates,
        "visible_objects": objects,
        "exposed_verbs": sorted({c["verb"] for c in candidates}) or [],
        "blocked_verbs": sorted(BLOCKED_VERBS),
        "action_allowlist": list(ACTION_ALLOWLIST),
        "intent_gate": gate_state,
        "object_actions": "RUNTIME_DISABLED" if not gate_open else "GATE_OPEN",
        "blocked_reason": "WORLD_OBJECT_PORTABILITY_GAP",
    }


def redact_action_space_for_selector(action_space: Mapping[str, Any]) -> dict[str, Any]:
    """The selector must not see reconciliation metadata (M13B frozen rule)."""

    redacted = {k: v for k, v in action_space.items()
                if k not in ("world_revision", "kind", "schema_version")}
    return redacted


# --------------------------------------------------------------------------
# Decision → Intent (fingerprint-bound, like the sealed life_choice contract)
# --------------------------------------------------------------------------


def fingerprint_action_space(action_space: Mapping[str, Any]) -> str:
    return digest(action_space)[:32]


def build_intent(
    choice: Mapping[str, Any],
    action_space: Mapping[str, Any],
    *,
    decision_ref: Optional[str] = None,
    created_at: Any = None,
) -> dict[str, Any]:
    """Turn one **validated** choice into a structured World Action Intent.

    ``choice`` is the selector's raw choice; it is validated here against the
    same fingerprint of the action space it was captured from.  A fingerprint
    mismatch is a stale intent and is refused, never silently refreshed.
    """

    if not isinstance(choice, Mapping):
        raise ActionIntentError("choice must be an object")
    if str(choice.get("status") or "") != "validated":
        raise ActionIntentError(
            "intent requires a validated choice (got %r)" % choice.get("status")
        )

    intent_fingerprint = str(choice.get("snapshot_fingerprint") or "")
    if not intent_fingerprint:
        raise ActionIntentError("choice is missing snapshot_fingerprint")
    live_fingerprint = fingerprint_action_space(action_space)
    if intent_fingerprint != live_fingerprint:
        raise ActionIntentError(
            "stale intent: choice fingerprint %s does not match the live action "
            "space %s" % (intent_fingerprint[:12], live_fingerprint[:12])
        )

    # `action` is the selector's framing (e.g. "SELECT"); the World verb comes
    # from the candidate, resolved below.
    action = _text(choice.get("action") or "SELECT", "choice.action", maximum=32)
    verb, params = _intent_to_verb(choice, action_space)

    summary = _text(choice.get("intent_summary"), "choice.intent_summary")
    created = _text(created_at or choice.get("validated_at"), "created_at", maximum=64)

    intent = {
        "kind": KIND_INTENT,
        "schema_version": SCHEMA_INTENT,
        "intent_id": "",           # filled below from content
        "decision_ref": _text(decision_ref, "decision_ref", maximum=128)
        if decision_ref else None,
        "actor_id": "chiyo",
        "action": action,
        "verb": verb,
        "params": params,
        "target_summary": summary,
        "created_at": created,
        "source_provenance": SOURCE,
        "authorization_ref": "pending",   # stamped at submission, not by the model
        "execution_id": "",               # derived below, same rule as life_execute
        "intent_status": STATUS_AUTHORIZED,
        "snapshot_fingerprint": intent_fingerprint,
    }
    # the id is derived from the content, exactly like the sealed
    # life_execute.derive_execution_id — an intent is a fact about a decision,
    # not a random row
    intent["intent_id"] = "world-intent:" + digest(
        {k: intent[k] for k in ("action", "verb", "params", "target_summary",
                                "snapshot_fingerprint", "created_at")}
    )[:32]
    intent["execution_id"] = "world-exec:" + digest(
        {"intent_id": intent["intent_id"]}
    )[:32]
    return intent


def _intent_to_verb(
    choice: Mapping[str, Any], action_space: Mapping[str, Any]
) -> tuple[str, dict[str, Any]]:
    """Map one candidate choice onto the exposed Resolver verb + params.

    The choice may only SELECT from the captured action space.  There is no
    free-form fallback: an intent that names a candidate which is not in the
    space it was validated against is an orphan and is refused (section 4).
    """

    candidate_id = choice.get("candidate_id")
    for candidate in action_space.get("candidates") or []:
        if not isinstance(candidate, Mapping):
            continue
        if str(candidate.get("candidate_id")) == str(candidate_id):
            verb = _text(candidate.get("verb"), "candidate.verb", maximum=32)
            if verb not in EXPOSED_VERBS:
                raise IntentRefused(
                    "verb %s is not exposed (blocked: %s)"
                    % (verb, sorted(BLOCKED_VERBS))
                )
            raw_params = candidate.get("params") or {}
            if not isinstance(raw_params, Mapping):
                raise ActionIntentError("candidate params must be an object")
            return verb, {str(k): v for k, v in raw_params.items()}

    raise ActionIntentError(
        "unknown candidate %r: intents may only select from the captured action space"
        % candidate_id
    )
# --------------------------------------------------------------------------
# Mutation gate (section 7)
# --------------------------------------------------------------------------


def resolve_gate(environment: Optional[Mapping[str, str]] = None) -> str:
    import os

    env = environment if environment is not None else os.environ
    raw = str(env.get(GATE_ENV) or GATE_OFF).strip().lower()
    if raw not in GATES:
        raise ActionIntentError("%s must be one of %s" % (GATE_ENV, list(GATES)))
    return raw


# --------------------------------------------------------------------------
# Intent → submission (the bridge calls this, never the model)
# --------------------------------------------------------------------------


def build_submission(
    intent: Mapping[str, Any],
    *,
    fresh_snapshot: Mapping[str, Any],
) -> dict[str, Any]:
    """Build the Resolver submission, filling dependencies from a FRESH read.

    Section 5: concurrency evidence is produced here, by the authority side,
    at submission time.  The model never sees or returns a revision.
    """

    if str(intent.get("intent_status")) not in (STATUS_AUTHORIZED, STATUS_PROPOSED):
        raise ActionIntentError(
            "intent %s is %s, not submittable"
            % (intent.get("intent_id"), intent.get("intent_status"))
        )
    execution_id = _text(intent.get("execution_id"), "intent.execution_id", maximum=256)
    # M15 §19: only explicitly allowed verbs may reach the Resolver.
    if str(intent.get("verb")) not in ACTION_ALLOWLIST:
        raise ActionIntentError(
            "verb %s is not in the production allowlist %s"
            % (intent.get("verb"), list(ACTION_ALLOWLIST))
        )

    location = (fresh_snapshot.get("location") or {})
    dependencies = {
        "world.revision": fresh_snapshot.get("world_revision"),
        "world.location": location.get("place_id"),
        "body.pose": fresh_snapshot.get("pose"),
    }
    if any(v is None for v in dependencies.values()):
        raise ActionIntentError(
            "fresh snapshot is incomplete: %s" % canonical_json(dependencies)
        )

    return {
        "kind": KIND_SUBMISSION,
        "schema_version": SCHEMA_INTENT,
        "intent_id": intent.get("intent_id"),
        "decision_ref": intent.get("decision_ref"),
        "action": intent.get("verb"),
        "execution_id": execution_id,
        "params": dict(intent.get("params") or {}),
        "observed_dependencies": dependencies,
        "source_provenance": intent.get("source_provenance"),
        "authorization_ref": GATEWAY_AUTHORIZATION,
    }


#: Authorisation the *service* stamps onto gateway submissions (M13B contract).
GATEWAY_AUTHORIZATION = "verified_main_self_tool"


def map_result(intent: Mapping[str, Any], outcome: Mapping[str, Any]) -> dict[str, Any]:
    """Map a bridge outcome onto the intent lifecycle.  Never invents success.

    ``submitted`` is not success (section 9); ``UNKNOWN`` is never retried here.
    """

    outcome_name = str(outcome.get("outcome") or "")
    mapping = {
        "SUCCESS": (STATUS_APPLIED, "applied"),
        "FAILURE": (STATUS_REJECTED, "rejected"),
        "STALE": (STATUS_STALE, "stale"),
        "UNKNOWN": (STATUS_UNKNOWN, "unknown"),
        "INVALID": (STATUS_REJECTED, "invalid"),
        # gate/transport refusals: the intent was never committed, so the
        # factual claim is "not done" — REJECTED, never UNKNOWN (which would
        # suggest the World might have been touched).
        "GATE_OFF": (STATUS_REJECTED, "gate_off"),
        "SHADOW_NO_COMMIT": (STATUS_REJECTED, "shadow_no_commit"),
    }
    status, reason = mapping.get(outcome_name, (STATUS_UNKNOWN, outcome_name))
    return {
        "kind": "world_action_intent_result",
        "schema_version": SCHEMA_INTENT,
        "intent_id": intent.get("intent_id"),
        "decision_ref": intent.get("decision_ref"),
        "execution_id": intent.get("execution_id"),
        "intent_status": status,
        "outcome": outcome_name,
        "reason": reason,
        "detail": outcome.get("reason"),
        "authoritative": outcome_name in ("SUCCESS", "FAILURE", "STALE", "UNKNOWN"),
    }
