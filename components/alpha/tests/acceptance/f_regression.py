#!/usr/bin/env python3
"""Acceptance F - regression on the vendored contact chain driver.

Runs the vendored ``canonical_chain_driver`` for BOTH gate profiles in
``--mode chain`` and ``--mode replay``, plus ``--mode negatives``, and asserts
the observed results.  Counts are reported exactly as the driver produced them:
a ``PARTIAL`` result is recorded as PARTIAL, never rounded up to a pass.

The driver is invoked through a tiny runner that imports
``chiyo._vendor_paths`` first (the same bootstrap the CLI uses), so the flat
import graph and the ``ct0``/``ct0_10`` aliases are identical in every run.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _harness import (  # noqa: E402
    TREE,
    add_common_arguments,
    check,
    clean_environment,
    emit,
    run_python,
    work_dir,
)

RUNNER = '''\
"""Vendored-chain runner: identical import bootstrap to the chiyo CLI."""
import os, runpy, sys

TREE = {tree!r}
sys.path.insert(0, TREE)
import chiyo._vendor_paths as vendor  # noqa: F401

driver = os.path.join(TREE, "chiyo", "contact", "canonical_chain_driver.py")
sys.argv = [driver] + sys.argv[1:]
runpy.run_path(driver, run_name="__main__")
'''

PROFILES = ("ISOLATED_TEST_OPEN", "PRODUCTION_OFF")


def run_mode(runner: Path, work: Path, mode: str, profile: str, env: dict) -> dict:
    out_dir = work / "artifacts"
    out_dir.mkdir(parents=True, exist_ok=True)
    env = dict(env)
    env.update(
        {
            "CT0_10_ISO_ROOT": str(work / "iso" / mode / profile),
            "CT0_10_OUT": str(out_dir),
            "PYTHONDONTWRITEBYTECODE": "1",
        }
    )
    result = run_python(str(runner), "--mode", mode, "--profile", profile, env=env, cwd=work)
    artifact_path = out_dir / f"CT0_10_MODE_{mode.upper()}_{profile}.json"
    artifact = json.loads(artifact_path.read_text(encoding="utf-8")) if artifact_path.is_file() else None
    result.update({"mode": mode, "profile": profile, "artifact": artifact, "artifact_path": str(artifact_path)})
    return result


def main(argv: list[str] | None = None) -> int:
    parser = add_common_arguments(argparse.ArgumentParser(description=__doc__))
    args = parser.parse_args(argv)
    work = work_dir(args, "f_regression")
    env = clean_environment(work / "tmp")

    runner = work / "_driver_runner.py"
    runner.write_text(RUNNER.format(tree=str(TREE)), encoding="utf-8")

    checks: list[dict] = []
    observed: dict[str, dict] = {}

    runs = {}
    for mode in ("chain", "replay", "negatives"):
        for profile in PROFILES:
            result = run_mode(runner, work, mode, profile, env)
            runs[(mode, profile)] = result
            artifact = result["artifact"] or {}
            observed[f"{mode}/{profile}"] = {
                "exit_code": result["exit_code"],
                "result": artifact.get("result"),
                "verdict": (artifact.get("chain") or {}).get("verdict"),
                "counts": artifact.get("counts"),
                "seconds": artifact.get("seconds"),
                "kill_switches": artifact.get("kill_switches"),
            }

    chain_iso = (runs[("chain", "ISOLATED_TEST_OPEN")]["artifact"] or {})
    chain_off = (runs[("chain", "PRODUCTION_OFF")]["artifact"] or {})
    checks.append(
        check(
            "--mode chain: ISOLATED_TEST_OPEN completes, PRODUCTION_OFF blocks at capacity",
            chain_iso.get("result") == "PASS"
            and (chain_iso.get("chain") or {}).get("verdict") == "CANONICAL_CHAIN_COMPLETE"
            and chain_off.get("result") == "PASS"
            and (chain_off.get("chain") or {}).get("verdict") == "BLOCKED_AT_CAPACITY",
            {
                "ISOLATED_TEST_OPEN": {
                    "result": chain_iso.get("result"),
                    "verdict": (chain_iso.get("chain") or {}).get("verdict"),
                    "stage_status": chain_iso.get("stage_status"),
                },
                "PRODUCTION_OFF": {
                    "result": chain_off.get("result"),
                    "verdict": (chain_off.get("chain") or {}).get("verdict"),
                    "stage_status": chain_off.get("stage_status"),
                    "capacity_unavailable_reasons": (chain_off.get("chain") or {}).get(
                        "capacity_unavailable_reasons"
                    ),
                },
            },
        )
    )

    stage_status = chain_iso.get("stage_status") or {}
    checks.append(
        check(
            "--mode chain: no stage FAILED or was BLOCKED in the isolated profile",
            not [stage for stage, status in stage_status.items() if status in {"FAIL", "BLOCKED"}],
            {stage: status for stage, status in stage_status.items()},
        )
    )

    replay_results = {
        profile: (runs[("replay", profile)]["artifact"] or {}) for profile in PROFILES
    }
    checks.append(
        check(
            "--mode replay: PASS for both profiles and no resend on reconstruction",
            all(
                artifact.get("result") == "PASS"
                and artifact.get("no_new_action") is True
                and artifact.get("no_telegram_submit") is True
                for artifact in replay_results.values()
            ),
            {
                profile: {
                    "result": artifact.get("result"),
                    "no_new_action": artifact.get("no_new_action"),
                    "new_actions": artifact.get("new_actions"),
                    "no_telegram_submit": artifact.get("no_telegram_submit"),
                    "new_telegram_submits": artifact.get("new_telegram_submits"),
                    "store_state_stable_across_close_and_rebootstrap": artifact.get(
                        "store_state_stable_across_close_and_rebootstrap"
                    ),
                }
                for profile, artifact in replay_results.items()
            },
        )
    )

    negative_counts = {
        profile: (runs[("negatives", profile)]["artifact"] or {}).get("counts")
        for profile in PROFILES
    }
    negative_fail = sum(
        (counts or {}).get("fail", 0) for counts in negative_counts.values()
    )
    negative_partial = sum(
        (counts or {}).get("partial", 0) for counts in negative_counts.values()
    )
    partial_ids = []
    for profile in PROFILES:
        for case in ((runs[("negatives", profile)]["artifact"] or {}).get("cases") or []):
            if case.get("status") == "PARTIAL":
                partial_ids.append(
                    {
                        "profile": profile,
                        "id": case.get("id"),
                        "requirement": case.get("requirement"),
                        "limitation": case.get("limitation"),
                    }
                )
    checks.append(
        check(
            "--mode negatives: recorded exactly as observed (no invented pass)",
            negative_fail == 0,
            {
                "counts_per_profile": negative_counts,
                "fail_total": negative_fail,
                "partial_total": negative_partial,
                "partial_cases": partial_ids,
            },
            status="PASS" if negative_fail == 0 and negative_partial == 0 else "PARTIAL",
        )
    )

    armed = {
        f"{mode}/{profile}": (artifact.get("kill_switches") or {})
        for (mode, profile), result in runs.items()
        for artifact in [result["artifact"] or {}]
    }
    checks.append(
        check(
            "no kill switch was armed in any run",
            all(not any(flags.values()) for flags in armed.values()) and bool(armed),
            armed,
        )
    )

    return emit("f_regression.py", checks, extra={"observed": observed, "partial_cases": partial_ids})


if __name__ == "__main__":
    raise SystemExit(main())
