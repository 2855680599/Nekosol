"""Shared helpers for the CHIYO acceptance scripts.

Every acceptance script:

* runs the shipped tree the way a user would (through ``python -B -m chiyo.cli``
  or the vendored modules), never by importing the tester's own convenience
  wrappers;
* prints ONE JSON object on stdout (``{"checks": [...], "status": ...}``);
* exits 0 only when every check is PASS.

Nothing here writes inside the release tree: every artifact goes to
``--work-dir``.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

TREE = Path(__file__).resolve().parents[2]
ACCEPTANCE_DIR = Path(__file__).resolve().parent


def add_common_arguments(parser: argparse.ArgumentParser) -> argparse.ArgumentParser:
    parser.add_argument("--work-dir", default=None, help="scratch directory (never the tree)")
    parser.add_argument("--json", action="store_true", help="print the JSON report only")
    return parser


def work_dir(args: argparse.Namespace, name: str) -> Path:
    base = Path(args.work_dir) if args.work_dir else Path(tempfile.mkdtemp(prefix=f"chiyo-accept-{name}-"))
    path = base if base.name == name else base / name
    path.mkdir(parents=True, exist_ok=True)
    return path


def clean_environment(tmp: Path, extra: dict | None = None) -> dict:
    """A deliberately minimal environment: no developer config, no kill switch.

    Every ``CHIYO_*`` / CT0-10 / kill-switch variable is dropped, so a script
    that uses this env sees the tree exactly as a fresh checkout does.
    """
    env = {
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "HOME": os.environ.get("HOME", str(tmp)),
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "PYTHONPATH": str(TREE),
        "PYTHONDONTWRITEBYTECODE": "1",
        "TMPDIR": str(tmp),
        "TEMP": str(tmp),
        "TMP": str(tmp),
    }
    if extra:
        env.update(extra)
    return env


def run_cli(*arguments: str, env: dict | None = None, cwd: Path | None = None, timeout: int = 1800) -> dict:
    """Run the shipped CLI in its own process and return code + streams."""
    argv = [sys.executable, "-B", "-m", "chiyo.cli", *map(str, arguments)]
    completed = subprocess.run(
        argv,
        cwd=str(cwd or TREE),
        env=env if env is not None else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=timeout,
    )
    return {
        "argv": argv,
        "exit_code": completed.returncode,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
    }


def run_python(*arguments: str, env: dict | None = None, cwd: Path | None = None, timeout: int = 1800) -> dict:
    argv = [sys.executable, "-B", *map(str, arguments)]
    completed = subprocess.run(
        argv,
        cwd=str(cwd or TREE),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=timeout,
    )
    return {
        "argv": argv,
        "exit_code": completed.returncode,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
    }


def last_json_object(text: str) -> dict | None:
    for line in reversed(text.splitlines()):
        line = line.strip()
        if line.startswith("{") and line.endswith("}"):
            try:
                return json.loads(line)
            except ValueError:
                continue
    return None


def check(name: str, ok: bool, detail: object, *, status: str | None = None) -> dict:
    return {
        "name": name,
        "status": status or ("PASS" if ok else "FAIL"),
        "detail": detail,
    }


def emit(script: str, checks: list[dict], *, extra: dict | None = None) -> int:
    counts = {"pass": 0, "warn": 0, "fail": 0, "partial": 0}
    for row in checks:
        counts[row["status"].lower()] = counts.get(row["status"].lower(), 0) + 1
    failed = counts["fail"] > 0
    report = {
        "script": script,
        "status": "FAIL" if failed else ("PARTIAL" if counts["partial"] else "PASS"),
        "counts": counts,
        "checks": checks,
        "tree": str(TREE),
    }
    if extra:
        report.update(extra)
    print(json.dumps(report, ensure_ascii=False, sort_keys=True, default=str))
    sys.stdout.flush()
    return 1 if failed else 0


def fail_messages(result: dict, limit: int = 2000) -> str:
    return (
        f"exit={result['exit_code']}\n--- stdout (tail) ---\n"
        f"{result['stdout'][-limit:]}\n--- stderr (tail) ---\n{result['stderr'][-limit:]}"
    )
