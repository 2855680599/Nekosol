#!/usr/bin/env python3
"""Intent execution through the WorldBody bridge (this ticket, §1 chain).

This is the *only* place a Decision-produced intent becomes a Resolver
submission.  The gate (§7) is checked before any Resolver contact:

    off       -> refused, no Resolver call, no dependency read
    shadow    -> full validation + submission built; nothing committed
    canonical -> real submit_action through the bridge

Everything here is transport-level: this module never decides what Chiyo
wants, never invents a revision, and never writes the World itself.
"""

from __future__ import annotations

import os
import pathlib
from typing import Any, Mapping, Optional

SCHEMA_EXECUTOR = "world.action.intent.executor.v1"

try:  # loaded as a plugin package
    from . import world_body_client as wbc  # type: ignore
except ImportError:  # noqa: BLE001 - loaded directly (tests, CLI)
    import pathlib as _pathlib
    import sys as _sys

    _HERE = str(_pathlib.Path(__file__).resolve().parent)
    if _HERE not in _sys.path:
        _sys.path.insert(0, _HERE)
    import world_body_client as wbc  # type: ignore


def _lifecycle():
    """The action lifecycle recorder (M15B).  Fail-closed: a caller that
    cannot record provenance must not submit."""

    try:
        from . import world_action_lifecycle as _l  # type: ignore
    except ImportError:  # flat load (tests, CLI)
        import importlib
        _l = importlib.import_module("world_action_lifecycle")
    return _l.ActionLifecycle()


def intent_gate() -> str:
    """The intent mutation gate.  Distinct from the M13B tool gate."""

    import action_intent as ai

    return ai.resolve_gate()


def _result(intent: Mapping[str, Any], *, status: str, outcome: str,
            detail: str, reason: Optional[str] = None,
            authoritative: bool = False, **extra: Any) -> dict[str, Any]:
    payload = {
        "kind": "world_action_intent_result",
        "schema_version": ai_schema(),
        "intent_id": intent.get("intent_id"),
        "decision_ref": intent.get("decision_ref"),
        "execution_id": intent.get("execution_id"),
        "intent_status": status,
        "outcome": outcome,
        "reason": reason or outcome,
        "detail": detail,
        "authoritative": authoritative,
    }
    payload.update(extra)
    return payload


def ai_schema() -> str:
    import action_intent as ai

    return ai.SCHEMA_INTENT


def _lifecycle_path(current: str, target: str, allowed) -> Optional[list[str]]:
    """Shortest legal transition path current -> target (BFS over ALLOWED)."""

    if allowed is None:
        return None
    if current == target:
        return []
    seen = {current}
    queue = [(current, [])]
    while queue:
        node, path = queue.pop(0)
        for nxt in allowed.get(node, ()):  # type: ignore[union-attr]
            if nxt in seen:
                continue
            new_path = path + [nxt]
            if nxt == target:
                return new_path
            seen.add(nxt)
            queue.append((nxt, new_path))
    return None


def _record_lifecycle(intent: Mapping[str, Any], *, verb: Optional[str] = None,
                      stage: str, failure_class: Optional[str] = None,
                      **fields: Any) -> None:
    """Write the lifecycle evidence for one attempt.

    ``stage`` is one of: created, authorized, submitting, submitted, applied,
    rejected, stale, unknown, aborted, cancelled.  Raises LifecycleError /
    OSError so the caller can fail closed.
    """

    lifecycle = _lifecycle()
    execution_id = str(intent.get("execution_id") or "")
    if not execution_id:
        raise ValueError("intent has no execution_id")
    decision_id = str(intent.get("decision_ref") or "")
    intent_id = str(intent.get("intent_id") or "")
    if stage == "created":
        lifecycle.begin(execution_id=execution_id, decision_id=decision_id,
                        intent_id=intent_id, verb=verb or str(intent.get("verb") or ""),
                        origin=str(intent.get("origin") or "production"),
                        detail=str(intent.get("intent_summary") or "")[:200])
        return
    mapping = {"authorized": "AUTHORIZED", "submitting": "SUBMITTING",
               "submitted": "SUBMITTED", "applied": "APPLIED",
               "rejected": "REJECTED", "stale": "STALE", "unknown": "UNKNOWN",
               "aborted": "ABORTED", "cancelled": "CANCELLED"}
    state = mapping[stage]
    # make sure a record exists (restart after CREATED, or a caller that
    # entered midway): create it first, then walk forward legally.
    entry = lifecycle.get(execution_id)
    if entry is None:
        lifecycle.begin(execution_id=execution_id, decision_id=decision_id,
                        intent_id=intent_id, verb=verb or "",
                        origin=str(intent.get("origin") or "production"))
        entry = lifecycle.get(execution_id)
    current = entry["state"]
    path = _lifecycle_path(current, state, getattr(lifecycle, "ALLOWED", None))
    if path is None:
        raise ValueError("illegal lifecycle path %s -> %s" % (current, state))
    for step in path:
        kwargs = dict(fields)
        if step in ("ABORTED", "REJECTED", "STALE", "UNKNOWN"):
            kwargs.setdefault("failure_class", failure_class)
        lifecycle.transition(execution_id=execution_id, state=step, **kwargs)


def execute_intent(
    intent: Mapping[str, Any], *, client: Optional["wbc.WorldBodyClient"] = None
) -> dict[str, Any]:
    """Run one intent through the gate → fresh read → submit_action chain.

    Returns an intent result; never raises for gate or transport outcomes.
    """

    import action_intent as ai

    # M15B: evidence first.  A caller that cannot record provenance must not
    # submit, so a lifecycle failure here is fail-closed (UNKNOWN, no submit).
    try:
        _existing = _lifecycle().get(str(intent.get("execution_id") or ""))
        if _existing is not None and _existing["state"] in _lifecycle().TERMINAL:
            # defence in depth: a terminal record means this attempt is over;
            # never contact the Resolver again with the same execution id.
            return _result(intent, status=ai.STATUS_REJECTED,
                           outcome="DUPLICATE_EXECUTION",
                           detail="lifecycle already terminal: %s"
                                  % _existing["state"])
        _record_lifecycle(intent, stage="created")
    except Exception as _lc_error:  # noqa: BLE001
        return _result(intent, status=ai.STATUS_UNKNOWN, outcome="NO_PROVENANCE",
                       detail="action lifecycle unavailable: %s" % _lc_error)

    gate = intent_gate()

    if gate == ai.GATE_OFF:
        _record_lifecycle(intent, verb=intent.get("verb"),
                          stage="aborted", failure_class="GATE_OFF")
        return _result(
            intent, status=ai.STATUS_REJECTED, outcome="GATE_OFF",
            detail="intent mutation gate is off; nothing was validated or submitted",
        )

    client = client or wbc.WorldBodyClient()
    if not client.enabled:
        return _result(intent, status=ai.STATUS_UNKNOWN, outcome="UNAVAILABLE",
                       detail="World/Body integration is off")

    status = client.get_status()
    if status.outcome == wbc.NOT_READY:
        return _result(intent, status=ai.STATUS_UNKNOWN, outcome="NOT_READY",
                       detail="runtime is reachable but not READY (section 26)")
    if status.outcome != wbc.OK:
        return _result(intent, status=ai.STATUS_UNKNOWN, outcome=status.outcome,
                       detail="runtime status read failed")
    if not (status.data or {}).get("ready"):
        return _result(intent, status=ai.STATUS_UNKNOWN, outcome="NOT_READY",
                       detail="runtime is reachable but not READY (section 26)")

    snapshot = client.get_snapshot()
    if snapshot.outcome != wbc.OK or not isinstance(snapshot.data, dict):
        return _result(intent, status=ai.STATUS_UNKNOWN, outcome=snapshot.outcome,
                       detail="fresh snapshot read failed")

    try:
        submission = ai.build_submission(intent, fresh_snapshot=snapshot.data)
    except ai.ActionIntentError as error:
        return _result(intent, status=ai.STATUS_REJECTED, outcome="INVALID",
                       detail=str(error))

    if gate == ai.GATE_SHADOW:
        try:
            _record_lifecycle(intent, verb=submission["action"], stage="aborted",
                              failure_class="SHADOW_NO_COMMIT")
        except Exception:  # noqa: BLE001 - shadow never submits anyway
            pass
        # Everything up to and including Resolver-shape validation has run; the
        # only thing skipped is the commit itself (section 7).
        return _result(
            intent, status=ai.STATUS_PROPOSED, outcome="SHADOW_NO_COMMIT",
            detail="submission validated; nothing committed",
            reason="intent_gate=shadow",
            submission_preview={
                "action": submission["action"],
                "params": submission["params"],
                "observed_dependencies": submission["observed_dependencies"],
            },
        )

    # M14A: mint the one-shot action authorization.  This is the ONLY place
    # authorizations are created; the socket consumes them (firewall).
    try:
        import world_action_authorization as _waa
    except ImportError:  # pragma: no cover
        import pathlib as _pl
        import sys as _sys
        _HERE = str(_pl.Path(__file__).resolve().parent)
        if _HERE not in _sys.path:
            _sys.path.insert(0, _HERE)
        import world_action_authorization as _waa

    _explicit = os.environ.get("WORLD_ACTION_AUTH_REGISTRY_DIR")
    if _explicit:
        _run_dir = pathlib.Path(_explicit)
    else:
        _home = os.environ.get("CHIYO_WORLD_HOME") or os.environ.get("HERMES_HOME")
        _run_dir = (pathlib.Path(_home) / "run") if _home else None
    if _run_dir is None:
        return _result(intent, status=ai.STATUS_UNKNOWN, outcome="UNAVAILABLE",
                       detail="authorization registry dir not resolvable (M14A)")
    try:
        _run_dir.mkdir(parents=True, exist_ok=True)
    except OSError as _error:
        return _result(intent, status=ai.STATUS_UNKNOWN, outcome="UNAVAILABLE",
                       detail="authorization registry dir not writable: %s" % _error)

    _registry = _waa.AuthorizationRegistry(_run_dir)
    _auth = _registry.issue(
        decision_id=intent.get("decision_ref"),
        intent_id=str(intent.get("intent_id") or ""),
        intent_fingerprint=str(intent.get("snapshot_fingerprint") or ""),
        execution_id=submission["execution_id"],
        actor="chiyo",
        verb=submission["action"],
        params=submission["params"],
        authority_generation=(submission.get("observed_dependencies") or {}).get("world.revision"),
    )

    try:
        _record_lifecycle(intent, verb=submission["action"], stage="authorized",
                          authorization_id=_auth["authorization_id"],
                          target=submission.get("params"))
        _record_lifecycle(intent, verb=submission["action"], stage="submitting",
                          authorization_id=_auth["authorization_id"])
    except Exception as _lc_error:  # noqa: BLE001 - no provenance, no submit
        return _result(intent, status=ai.STATUS_UNKNOWN, outcome="NO_PROVENANCE",
                       detail="lifecycle write failed before submit: %s" % _lc_error)

    outcome = client.submit_action(
        action=submission["action"],
        execution_id=submission["execution_id"],
        params=submission["params"],
        observed_dependencies=submission["observed_dependencies"],
        authorization_id=_auth["authorization_id"],
        decision_ref=intent.get("decision_ref"),
    )
    mapped = ai.map_result(intent, {"outcome": outcome.outcome})
    _stage = {ai.STATUS_APPLIED: "applied", ai.STATUS_REJECTED: "rejected",
              ai.STATUS_STALE: "stale", ai.STATUS_UNKNOWN: "unknown"
              }.get(mapped["intent_status"], "unknown")
    try:
        _record_lifecycle(intent, verb=submission["action"], stage="submitted",
                          authorization_id=_auth["authorization_id"])
        _record_lifecycle(intent, verb=submission["action"], stage=_stage,
                          authorization_id=_auth["authorization_id"],
                          failure_class=(None if _stage == "applied"
                                         else str(outcome.reason or outcome.outcome)),
                          detail=str(outcome.reason or "")[:200])
    except Exception as _lc_error:  # noqa: BLE001 - the outcome already happened
        import logging as _logging
        _logging.getLogger("chiyo_intent_executor").warning(
            "lifecycle terminal write failed (execution=%s): %s",
            submission.get("execution_id"), _lc_error)
    return _result(
        intent, status=mapped["intent_status"], outcome=outcome.outcome,
        detail=outcome.reason or mapped["reason"],
        reason=outcome.reason, authoritative=True,
    )
