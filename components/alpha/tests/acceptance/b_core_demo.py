#!/usr/bin/env python3
"""Acceptance B - the core demo.

1. runs ``chiyo demo core`` and requires exit 0 plus ``CORE DEMO: PASS``,
2. asserts the required ids are present (activity, candidate set, decision,
   action, receipt, AR-1 settlement, and the UNKNOWN action),
3. re-reads the receipt of the delivered Action in a FRESH process through the
   real AR-0 read services and requires the same receipt ids to come back.
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
    fail_messages,
    last_json_object,
    run_cli,
    work_dir,
)

REQUIRED_IDS = (
    "activity_id",
    "candidate_set_id",
    "decision_id",
    "action_id",
    "action_attempt_id",
    "settlement_id",
    "unknown_action_id",
)


def main(argv: list[str] | None = None) -> int:
    parser = add_common_arguments(argparse.ArgumentParser(description=__doc__))
    args = parser.parse_args(argv)
    work = work_dir(args, "b_core_demo")
    env = clean_environment(work / "tmp")
    data_dir = work / "data"
    data_dir.mkdir(parents=True, exist_ok=True)

    checks: list[dict] = []
    demo = run_cli("demo", "core", "--data-dir", str(data_dir), "--json", env=env)
    payload = last_json_object(demo["stdout"]) or {}

    ok = demo["exit_code"] == 0 and payload.get("verdict") == "CORE DEMO: PASS"
    checks.append(
        check(
            "chiyo demo core exits 0 with CORE DEMO: PASS",
            ok,
            {
                "exit_code": demo["exit_code"],
                "verdict": payload.get("verdict"),
                "blocked": payload.get("blocked"),
                "process": fail_messages(demo) if not ok else "",
            },
        )
    )

    ids = payload.get("ids") or {}
    missing = [name for name in REQUIRED_IDS if not ids.get(name)]
    checks.append(
        check(
            "required core ids present",
            not missing,
            {"missing": missing, "ids": {k: ids.get(k) for k in REQUIRED_IDS}},
        )
    )
    checks.append(
        check(
            "receipt + AR-1 settlement are real records",
            bool(ids.get("receipt_refs")) and bool(ids.get("settlement_id")),
            {
                "receipt_refs": ids.get("receipt_refs"),
                "ack_receipt_refs": ids.get("ack_receipt_refs"),
                "settlement_id": ids.get("settlement_id"),
                "settlement_status": ids.get("settlement_status"),
            },
        )
    )

    step_status = {
        row["step"]: row["status"] for row in (payload.get("steps") or [])
    }
    checks.append(
        check(
            "all seven core steps reported PASS",
            step_status
            and all(status == "PASS" for status in step_status.values())
            and len(step_status) >= 8,
            step_status,
        )
    )

    run_dir = payload.get("run_dir")
    action_id = ids.get("action_id")
    if not run_dir or not action_id:
        checks.append(check("fresh-process receipt re-read", False, "demo output had no run_dir/action"))
        return emit("b_core_demo.py", checks)

    reread = run_cli(
        "_core-receipt",
        "--run-dir",
        str(Path(run_dir) / "runtime"),
        "--action-id",
        action_id,
        "--json",
        env=env,
    )
    reread_payload = last_json_object(reread["stdout"]) or {}
    fresh_receipt_ids = sorted(
        row["receipt_id"] for row in (reread_payload.get("receipts") or [])
    )
    same_receipts = fresh_receipt_ids == sorted(ids.get("receipt_refs") or [])
    checks.append(
        check(
            "the receipt re-reads identically in a fresh process",
            reread["exit_code"] == 0
            and same_receipts
            and (reread_payload.get("settlement") or {}).get("settlement_id")
            == ids.get("settlement_id"),
            {
                "exit_code": reread["exit_code"],
                "fresh_process_receipts": fresh_receipt_ids,
                "fresh_process_action_status": reread_payload.get("action_status"),
                "fresh_process_settlement": (reread_payload.get("settlement") or {}).get(
                    "settlement_id"
                ),
                "same_receipt_ids": same_receipts,
                "stderr_tail": reread["stderr"][-600:] if reread["exit_code"] else "",
            },
        )
    )

    checks.append(
        check(
            "the core demo used only the in-tree recording transport",
            all(
                row["status"] == "PASS"
                for row in (payload.get("steps") or [])
                if row["step"].startswith("4.")
            )
            and "FakeMessageProvider"
            in json.dumps(
                next(
                    (row["detail"] for row in payload.get("steps") or [] if row["step"].startswith("4.")),
                    {},
                )
            ),
            {
                "provider_kind": next(
                    (row["detail"].get("provider_kind") for row in payload.get("steps") or []
                     if row["step"].startswith("4.")),
                    None,
                ),
                "network_used": next(
                    (row["detail"].get("network_used") for row in payload.get("steps") or []
                     if row["step"].startswith("4.")),
                    None,
                ),
            },
        )
    )

    if not args.json:
        print(json.dumps(checks, indent=1, default=str)[:2000], file=sys.stderr)
    return emit("b_core_demo.py", checks, extra={"core_ids": ids, "run_dir": run_dir})


if __name__ == "__main__":
    raise SystemExit(main())
