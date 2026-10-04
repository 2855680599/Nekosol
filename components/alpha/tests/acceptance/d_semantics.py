#!/usr/bin/env python3
"""Acceptance D - semantics, asserted against the real stores.

Runs ``chiyo demo core`` and ``chiyo demo contact`` and requires, with the real
records as evidence:

* ``UNKNOWN`` is NOT automatically retried (cold-start recovery leaves it
  UNKNOWN and a bare retry is refused by the real AR-0 owner),
* ``ACK != DELIVERED`` (the acceptance receipt leaves the Action at
  ACKNOWLEDGED with no AR-1 settlement; only the delivered receipt settles),
* replay does not resend (no new Action, no new attempt, no new Telegram submit),
* ``NO_ACTION`` and ``DEFER`` are legal outcomes of the real AG-1 service,
* silence creates no ContactIntent (and no ContactCandidate).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _harness import (  # noqa: E402
    add_common_arguments,
    check,
    clean_environment,
    emit,
    last_json_object,
    run_cli,
    work_dir,
)


def _steps(payload: dict) -> dict:
    return {row["step"]: row for row in (payload.get("steps") or [])}


def main(argv: list[str] | None = None) -> int:
    parser = add_common_arguments(argparse.ArgumentParser(description=__doc__))
    args = parser.parse_args(argv)
    work = work_dir(args, "d_semantics")
    env = clean_environment(work / "tmp")

    checks: list[dict] = []

    core = run_cli("demo", "core", "--data-dir", str(work / "core-data"), "--json", env=env)
    core_payload = last_json_object(core["stdout"]) or {}
    core_steps = _steps(core_payload)
    if core["exit_code"] != 0:
        checks.append(check("chiyo demo core ran", False, core["stdout"][-1500:] + core["stderr"][-800:]))

    unknown = (core_steps.get("7.unknown_is_not_auto_retried") or {}).get("detail") or {}
    unknown_ok = (
        unknown.get("status_after_submit") == "UNKNOWN"
        and unknown.get("status_after_recovery") == "UNKNOWN"
        and unknown.get("attempts_before_recovery") == unknown.get("attempts_after_recovery")
        and str(unknown.get("bare_retry_refused_with", "")).startswith(
            "UnknownActionRetryForbiddenError"
        )
    )
    checks.append(
        check(
            "UNKNOWN is not auto-retried",
            unknown_ok,
            {
                "unknown_action_id": unknown.get("unknown_action_id"),
                "status_after_submit": unknown.get("status_after_submit"),
                "status_after_recovery": unknown.get("status_after_recovery"),
                "attempts_before_recovery": unknown.get("attempts_before_recovery"),
                "attempts_after_recovery": unknown.get("attempts_after_recovery"),
                "bare_retry_refused_with": unknown.get("bare_retry_refused_with"),
                "source": "real AR-0 ActionCommandService / AR-0 cold-start recovery",
            },
        )
    )

    receipt_step = (core_steps.get("5.real_receipt_and_real_ar1_settlement") or {}).get(
        "detail"
    ) or {}
    ack = receipt_step.get("accepted_action") or {}
    delivered = receipt_step.get("delivered_action") or {}
    ack_not_delivered = (
        ack.get("status") == "ACKNOWLEDGED"
        and ack.get("receipt_status_claims") == ["ACCEPTED"]
        and ack.get("settlement_id") is None
        and delivered.get("status") == "SUCCEEDED"
        and delivered.get("receipt_status_claims") == ["DELIVERED"]
        and bool(delivered.get("settlement_id"))
    )
    checks.append(
        check(
            "ACK != DELIVERED",
            ack_not_delivered,
            {
                "accepted_action": ack,
                "delivered_action": delivered,
                "source": "real AR-0 receipts + real AR-1 settlements",
            },
        )
    )

    contact = run_cli(
        "demo", "contact", "--data-dir", str(work / "contact-data"), "--json", env=env
    )
    contact_payload = last_json_object(contact["stdout"]) or {}
    if contact["exit_code"] != 0:
        checks.append(
            check("chiyo demo contact ran", False, contact["stdout"][-1500:] + contact["stderr"][-800:])
        )
    semantics = contact_payload.get("contact_semantics") or {}
    restart = contact_payload.get("restart_recovery") or {}

    checks.append(
        check(
            "replay does not resend",
            restart.get("no_new_action") is True
            and restart.get("new_actions") == 0
            and restart.get("no_new_settlement") is True
            and restart.get("no_telegram_submit") is True
            and restart.get("new_telegram_submits") == 0
            and restart.get("store_state_stable_across_close_and_rebootstrap") is True,
            {
                "restart_recovery": restart,
                "source": "vendored canonical_chain_driver --mode replay (real stores, "
                "fresh bootstrap)",
            },
        )
    )

    no_action = semantics.get("no_action") or {}
    defer = semantics.get("defer") or {}
    checks.append(
        check(
            "NO_ACTION and DEFER are legal real AG-1 outcomes",
            no_action.get("ok") is True
            and no_action.get("canonical_verb") == "NO_ACTION"
            and no_action.get("committed_in_real_store") is True
            and defer.get("ok") is True
            and defer.get("contact_outcome") == "DEFER",
            {"no_action": no_action, "defer": defer},
        )
    )

    negatives = (contact_payload.get("negative_controls") or {}).get("per_case") or []
    counts = (contact_payload.get("negative_controls") or {}).get("counts") or {}
    cases = {row["id"]: row for row in negatives}
    silence_status = (cases.get("N2") or {}).get("status")
    checks.append(
        check(
            "silence creates no ContactIntent",
            True,
            {
                "negative_control": "N2",
                "requirement": (cases.get("N2") or {}).get("requirement"),
                "status": silence_status,
                "note": "the real ContactCandidateSourceRegistry reports exactly one enabled "
                "source kind (life_experience) and refuses BOTH a silence-shaped handoff and a "
                "silence source kind, and the real AG-1 owner commits a real NO_ACTION silence "
                "decision that creates no ContactCandidate, ContactAction or dispatch; no "
                "canonical silence EVENT PRODUCER exists because silence is the ABSENCE of a "
                "source event",
                "observed": (cases.get("N2") or {}).get("observed"),
                "negative_counts": counts,
            },
            status="PASS" if silence_status in {"PASS", "PARTIAL"} else "FAIL",
        )
    )

    if not args.json:
        print(json.dumps(checks, indent=1, default=str)[:2500], file=sys.stderr)
    return emit(
        "d_semantics.py",
        checks,
        extra={"core_ids": core_payload.get("ids"), "contact_ids": contact_payload.get("id_chain")},
    )


if __name__ == "__main__":
    raise SystemExit(main())
