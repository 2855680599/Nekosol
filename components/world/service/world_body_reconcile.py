#!/usr/bin/env python3
"""Startup reconciliation for the standalone World/Body service (M13A).

The service may be killed at any instant.  Three windows matter, and each is
treated differently on purpose:

===========  ==========================================  =====================
window       what the ledger shows                       what startup does
===========  ==========================================  =====================
Crash A      World committed, ledger terminal SUCCESS,   apply the missing
             body consequence absent                     consequence, once
Crash B      body applied, reference not finalised       nothing (the dedupe
                                                         says already applied)
Crash C      UNKNOWN                                     report only; never
                                                         auto-retry, never
                                                         auto-grade
===========  ==========================================  =====================

Two boundaries keep this honest:

* Only **body-era** executions are considered -- records with representation
  ``v2``.  The legacy journal rows are evidence of a period before the body
  existed; healing them would invent body history that never happened, so they
  are never candidates.
* Reconciliation only ever *fills a gap*.  A restart with nothing to heal writes
  nothing at all: no record per restart, no re-application, no grading of an
  unknown.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

SCHEMA_RECONCILE = "world.body.service.reconciliation.v1"
KIND_RECONCILE = "world_body_startup_reconciliation"

IN_FLIGHT_STATES = ("prepared", "uncertain")

#: Ledger terminal result -> the status string the consequence mapping expects.
RESULT_TO_STATUS = {
    "SUCCESS": "applied",
    "FAILURE": "rejected",
    "UNKNOWN": "uncertain",
}


class ReconciliationError(RuntimeError):
    """Reconciliation could not be completed; the service must not go ready."""


def body_era_execution_ids(ledger) -> list[str]:
    """Execution ids that have at least one v2 record, in first-seen order."""

    ids: list[str] = []
    seen: set[str] = set()
    for record in ledger.read_all():
        if record.get("representation") != "v2":
            continue
        execution_id = record.get("execution_id")
        if execution_id and execution_id not in seen:
            seen.add(execution_id)
            ids.append(execution_id)
    return ids


def body_era_frames(ledger) -> list[dict[str, Any]]:
    return [ledger.frame(execution_id) for execution_id in body_era_execution_ids(ledger)]


def reconcile_startup(
    *,
    ledger,
    resolver,
    consequence_ledger,
    body_store,
    body_id: str,
    runtime_state: Mapping[str, Any],
    at: Any,
    world_facts: Optional[Mapping[str, Any]] = None,
    pose: Optional[str] = None,
) -> dict[str, Any]:
    """Heal only the gaps.  Returns a report plus the caller's new body state.

    ``runtime_state`` is never mutated; the returned state must be persisted by
    the caller.
    """

    import body_consequence as bcon
    import body_runtime as br

    chain = ledger.verify()  # raises LedgerTamperError when the chain is broken

    state = br.validate_runtime(runtime_state)
    records = consequence_ledger.read_all()
    consequence_state = consequence_ledger.current_state(records)

    healed: list[dict[str, Any]] = []
    already: list[str] = []
    unresolved: list[dict[str, Any]] = []
    failures: list[str] = []
    candidates: list[dict[str, Any]] = []

    for frame in body_era_frames(ledger):
        execution_id = frame["execution_id"]
        action_type = frame.get("action_type") or ""
        result = frame.get("result")
        frame_state = frame.get("state")
        consequence_id = bcon.consequence_id(execution_id, action_type, body_id)
        existing = consequence_state.get(consequence_id)
        already_applied = existing is not None and existing.get("state") == "applied"

        candidates.append(
            {
                "execution_id": execution_id,
                "action": action_type,
                "frame_state": frame_state,
                "ledger_result": result,
                "consequence_recorded": already_applied,
            }
        )

        if frame_state in IN_FLIGHT_STATES or result == "UNKNOWN":
            # Crash C, or an outcome that is still genuinely unknown.  The
            # resolver's own view is reported so the path is provably available.
            view = resolver.reconcile(execution_id)
            unresolved.append(
                {
                    "execution_id": execution_id,
                    "resolver_verdict": view.get("verdict"),
                    "frame_state": frame_state,
                    "reconciliation_required": frame.get("reconciliation_required"),
                    "action": "none (never auto-retry, never auto-grade)",
                }
            )
            continue

        if result == "FAILURE":
            # Failure applies nothing, so there is no gap to fill.
            failures.append(execution_id)
            continue

        if already_applied:
            # Crash B, or an ordinary clean restart.  Nothing to do.
            already.append(execution_id)
            continue

        # Crash A: the world and the ledger say success, the body never heard.
        outcome = bcon.apply_for_execution(
            {
                "kind": "world_action_result",
                "execution_id": execution_id,
                "action": action_type,
                "status": RESULT_TO_STATUS.get(result or "", "uncertain"),
                "reason_code": frame.get("reason_code"),
            },
            body_id=body_id,
            ledger=consequence_ledger,
            runtime_state=state,
            at=at,
            world_facts=world_facts,
            pose=pose,
        )
        if outcome["applied"]:
            state = outcome["state"]
            body_store.save_runtime(state)
            healed.append(
                {
                    "execution_id": execution_id,
                    "consequence_id": consequence_id,
                    "transition": (outcome["transition"] or {}).get("transition_id"),
                }
            )
        else:
            already.append(execution_id)

    return {
        "kind": KIND_RECONCILE,
        "schema_version": SCHEMA_RECONCILE,
        "chain_valid": bool(chain.get("valid")),
        "chain_entries": (chain.get("legacy_entries") or 0) + (chain.get("v2_entries") or 0),
        "candidates": candidates,
        "candidate_count": len(candidates),
        "in_flight": len(unresolved),
        "healed": healed,
        "already_applied": already,
        "failures_needing_nothing": failures,
        "unresolved": unresolved,
        "wrote_anything": bool(healed),
        "state": state,
        "complete": True,
    }


def reconciliation_summary(report: Mapping[str, Any]) -> dict[str, Any]:
    """Small, log-safe view: no raw body values, no payloads."""

    return {
        "schema_version": report.get("schema_version"),
        "chain_valid": report.get("chain_valid"),
        "candidates": report.get("candidate_count"),
        "healed": len(report.get("healed") or []),
        "already_applied": len(report.get("already_applied") or []),
        "unresolved": len(report.get("unresolved") or []),
        "wrote_anything": report.get("wrote_anything"),
    }


def pending_execution_ids(ledger) -> Sequence[str]:
    return [
        frame["execution_id"]
        for frame in body_era_frames(ledger)
        if frame.get("state") in IN_FLIGHT_STATES or frame.get("result") == "UNKNOWN"
    ]
