#!/usr/bin/env python3
"""Acceptance runner.

Runs every acceptance script (A-F) in its own process and writes
``ACCEPTANCE.json`` in the tree root with the per-check status, then prints a
summary table.  Exit code is non-zero when any script reported a FAIL.

Usage:
    python3 -B tests/acceptance/run_all.py [--work-dir DIR] [--only a,b]
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
TREE = HERE.parents[1]
SCRIPTS = (
    "a_clean_install.py",
    "b_core_demo.py",
    "c_contact_demo.py",
    "d_semantics.py",
    "e_safety.py",
    "f_regression.py",
)
OUTPUT = TREE / "ACCEPTANCE.json"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work-dir", default=None)
    parser.add_argument("--only", default=None, help="comma separated script prefixes, e.g. a,c")
    parser.add_argument("--timeout", type=int, default=3600)
    args = parser.parse_args(argv)

    work = Path(args.work_dir) if args.work_dir else (Path.home() / ".chiyo-acceptance")
    work.mkdir(parents=True, exist_ok=True)
    selected = [
        name
        for name in SCRIPTS
        if not args.only or name.split("_")[0] in {part.strip() for part in args.only.split(",")}
    ]

    results: list[dict] = []
    for name in selected:
        started = time.time()
        completed = subprocess.run(
            [sys.executable, "-B", str(HERE / name), "--work-dir", str(work / name), "--json"],
            cwd=str(TREE),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=args.timeout,
        )
        payload = None
        for line in reversed(completed.stdout.splitlines()):
            line = line.strip()
            if line.startswith("{") and line.endswith("}"):
                try:
                    payload = json.loads(line)
                    break
                except ValueError:
                    continue
        payload = payload or {
            "script": name,
            "status": "FAIL",
            "checks": [
                {
                    "name": "the script produced no JSON report",
                    "status": "FAIL",
                    "detail": {
                        "stdout_tail": completed.stdout[-1500:],
                        "stderr_tail": completed.stderr[-1500:],
                    },
                }
            ],
        }
        payload["exit_code"] = completed.returncode
        payload["seconds"] = round(time.time() - started, 2)
        results.append(payload)
        print(
            f"[{payload['status']:7}] {name:22} exit={completed.returncode} "
            f"{payload.get('counts')} {payload.get('seconds', '')}s",
            file=sys.stderr,
            flush=True,
        )

    failed = [row["script"] for row in results if row["status"] == "FAIL"]
    partial = [row["script"] for row in results if row["status"] == "PARTIAL"]
    document = {
        "tool": "chiyo acceptance",
        "tree": str(TREE),
        "work_dir": str(work),
        "generated_at_unix": int(time.time()),
        "status": "FAIL" if failed else ("PARTIAL" if partial else "PASS"),
        "failed_scripts": failed,
        "partial_scripts": partial,
        "results": results,
    }
    OUTPUT.write_text(json.dumps(document, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")
    print(json.dumps({"status": document["status"], "failed": failed, "partial": partial, "out": str(OUTPUT)}))
    print(f"ACCEPTANCE: {document['status']}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
