#!/usr/bin/env python3
"""Acceptance C - the contact demo.

1. runs ``chiyo demo contact`` and requires exit 0 plus ``CONTACT DEMO: PASS``,
2. asserts the id chain produced by the real owners is present,
3. asserts that no real send happened: the only transport is the in-tree
   recording ``FakeMessageProvider`` (1 invocation), the serialized Telegram
   request never reached a transport, ``network_used`` is false and the real
   side effect switch is off,
4. keeps the driver's declared-test-double tags (``canonical_owner_exists=false``)
   in the report, so a reader is never misled about capacity ownership.
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

REQUIRED_CHAIN = (
    "contact_intent_ref",
    "agent0_candidate_set_ref",
    "agency_decision_ref",
    "expressive_draft_ref",
    "canonical_action_ref",
    "action_submission_key",
    "receipt_ref",
    "ar1_settlement_ref",
    "handoff_ref",
)


def main(argv: list[str] | None = None) -> int:
    parser = add_common_arguments(argparse.ArgumentParser(description=__doc__))
    args = parser.parse_args(argv)
    work = work_dir(args, "c_contact_demo")
    env = clean_environment(work / "tmp")
    data_dir = work / "data"
    data_dir.mkdir(parents=True, exist_ok=True)

    checks: list[dict] = []
    demo = run_cli("demo", "contact", "--data-dir", str(data_dir), "--json", env=env)
    payload = last_json_object(demo["stdout"]) or {}

    ok = demo["exit_code"] == 0 and payload.get("verdict") == "CONTACT DEMO: PASS"
    checks.append(
        check(
            "chiyo demo contact exits 0 with CONTACT DEMO: PASS",
            ok,
            {
                "exit_code": demo["exit_code"],
                "verdict": payload.get("verdict"),
                "blocked": payload.get("blocked"),
                "process": fail_messages(demo) if not ok else "",
            },
        )
    )

    chain = payload.get("id_chain") or {}
    missing = [name for name in REQUIRED_CHAIN if not chain.get(name)]
    checks.append(
        check(
            "the id chain is present",
            not missing,
            {"missing": missing, "id_chain": chain},
        )
    )

    runs = payload.get("runs") or []
    by_mode = {(row["mode"], row["profile"]): row for row in runs}
    checks.append(
        check(
            "driver profiles behaved as required",
            by_mode.get(("chain", "ISOLATED_TEST_OPEN"), {}).get("verdict")
            == "CANONICAL_CHAIN_COMPLETE"
            and by_mode.get(("chain", "PRODUCTION_OFF"), {}).get("verdict")
            == "BLOCKED_AT_CAPACITY"
            and by_mode.get(("replay", "ISOLATED_TEST_OPEN"), {}).get("result") == "PASS",
            {
                f"{row['mode']}/{row['profile']}": {
                    "exit_code": row["exit_code"],
                    "result": row["result"],
                    "verdict": row["verdict"],
                }
                for row in runs
            },
        )
    )

    send = payload.get("no_proactive_send") or {}
    sends_zero = (
        send.get("serialized_request_transport_reached") is False
        and send.get("transport_not_reached_check") is True
        and send.get("network_used") is False
        and send.get("provider_kind") == "FakeMessageProvider"
        and send.get("provider_invocations", 0) <= 1
        and send.get("real_proactive_telegram_side_effect_enabled") is False
        and send.get("telegram_dispatch_status") == "BLOCKED_BY_GATE"
    )
    checks.append(
        check(
            "no real send: transport never reached, real sends = 0",
            sends_zero,
            send,
        )
    )

    doubles = payload.get("declared_test_doubles") or []
    checks.append(
        check(
            "declared test doubles are kept and tagged canonical_owner_exists=false",
            doubles
            and all(row.get("canonical_owner_exists") is False for row in doubles)
            and all(row.get("declared_test_double") is True for row in doubles),
            [
                {
                    "port": row.get("port"),
                    "canonical_owner_exists": row.get("canonical_owner_exists"),
                    "disposition": row.get("disposition"),
                }
                for row in doubles
            ],
        )
    )

    restart = payload.get("restart_recovery") or {}
    checks.append(
        check(
            "restart recovery did not resend",
            restart.get("no_new_action") is True
            and restart.get("new_actions") == 0
            and restart.get("no_telegram_submit") is True
            and restart.get("new_telegram_submits") == 0,
            restart,
        )
    )

    if not args.json:
        print(json.dumps(checks, indent=1, default=str)[:2000], file=sys.stderr)
    return emit("c_contact_demo.py", checks, extra={"contact_ids": chain})


if __name__ == "__main__":
    raise SystemExit(main())
