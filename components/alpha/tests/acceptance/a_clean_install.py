#!/usr/bin/env python3
"""Acceptance A - clean install.

In a throw-away working directory, with a deliberately clean environment (no
developer configuration, no kill switch, no CHIYO_*/CT0_10_* variable), this
script:

1. runs ``python -m chiyo doctor`` and requires exit 0,
2. imports ``chiyo`` and every vendored module from a fresh process and reports
   the module count plus every import failure,
3. verifies that both spellings of the packaged CT0-10 modules resolve to the
   SAME module object (a duplicate module object would silently break the
   ``isinstance`` / class-identity contracts).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _harness import (  # noqa: E402
    TREE,
    add_common_arguments,
    check,
    clean_environment,
    emit,
    fail_messages,
    last_json_object,
    run_cli,
    work_dir,
)


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = add_common_arguments(argparse.ArgumentParser(description=__doc__))
    args = parser.parse_args(argv)
    work = work_dir(args, "a_clean_install")
    env = clean_environment(work / "tmp")
    env["HOME"] = str(work / "home")
    Path(env["HOME"]).mkdir(parents=True, exist_ok=True)
    cwd = work / "cwd"
    cwd.mkdir(parents=True, exist_ok=True)

    checks: list[dict] = []

    doctor = run_cli("doctor", "--data-dir", str(work / "data"), env=env, cwd=cwd)
    checks.append(
        check(
            "python -m chiyo doctor exits 0 from a clean cwd",
            doctor["exit_code"] == 0,
            {
                "exit_code": doctor["exit_code"],
                "cwd": str(cwd),
                "env": sorted(env),
                "stdout_tail": doctor["stdout"][-1200:] if doctor["exit_code"] else "DOCTOR: PASS",
                "stderr_tail": doctor["stderr"][-600:],
            },
        )
    )

    imports = run_cli("_import-check", "--json", env=env, cwd=cwd)
    payload = last_json_object(imports["stdout"]) or {}
    module_count = payload.get("module_count")
    failures = payload.get("failures") or []
    identity = payload.get("single_module_objects") or []
    checks.append(
        check(
            "import chiyo + every vendored module",
            imports["exit_code"] == 0 and module_count and not failures,
            {
                "module_count": module_count,
                "imported": payload.get("imported"),
                "failures": failures,
                "stderr_tail": imports["stderr"][-600:] if failures else "",
            },
        )
    )

    distinct_classes = {}
    duplicates: list[str] = []
    for row in identity:
        key = (row["class"], row["class_id"])
        distinct_classes.setdefault(row["class"], set()).add(row["class_id"])
    for symbol, ids in distinct_classes.items():
        if len(ids) != 1:
            duplicates.append(f"{symbol}: {sorted(ids)}")
    checks.append(
        check(
            "flat and packaged spellings are the SAME module object",
            bool(identity) and not duplicates,
            {
                "pairings_checked": len(identity),
                "distinct_classes": {k: sorted(v) for k, v in distinct_classes.items()},
                "duplicates": duplicates,
                "sample": identity[:4],
            },
        )
    )

    version = run_cli("--version", env=env, cwd=cwd)
    checks.append(
        check(
            "chiyo reports its version",
            "0.1.0a0" in version["stdout"],
            {"stdout": version["stdout"].strip(), "exit_code": version["exit_code"]},
        )
    )

    # no byte-code cache may be left inside the shipped tree
    pycache = sorted(str(p) for p in TREE.rglob("__pycache__"))
    checks.append(
        check(
            "no __pycache__ inside the release tree",
            not pycache,
            {"found": pycache[:10], "count": len(pycache)},
        )
    )

    if not args.json:
        print(f"A clean install: {json.dumps(checks, indent=1, default=str)[:2000]}", file=sys.stderr)
    return emit("a_clean_install.py", checks)


if __name__ == "__main__":
    raise SystemExit(main())
