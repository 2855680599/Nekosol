"""Runs the fixture-transport cognition reliability suite against the exported code.

The suite exercises crash windows, unknown send states, late results, budget accounting and
journal retention, using an in-process fixture transport: no socket is opened, no credential is
read and no paid call is made.
"""
import io
import json
import runpy
from contextlib import redirect_stdout
from pathlib import Path

SUITE = Path(__file__).resolve().parent / "cognition_reliability_suite.py"


def _run():
    buf = io.StringIO()
    rc = 0
    with redirect_stdout(buf):
        try:
            runpy.run_path(str(SUITE), run_name="__main__")
        except SystemExit as exc:
            rc = exc.code or 0
    return buf.getvalue(), rc


def test_cognition_reliability_suite_reports_no_failed_checks():
    out, rc = _run()
    marker = "================ SUMMARY ================"
    assert marker in out, out[-4000:]
    tail = out.split(marker)[-1]
    payload = json.loads(tail[tail.index("{"):tail.rindex("}") + 1])
    assert payload["checks"]["failed"] == 0, out[-6000:]
    assert payload["provider_calls_made"] == 0
    assert payload["gates"]["DUPLICATE_PAID_CALL"] == 0
    assert payload["gates"]["UNKNOWN_SEND_STATE_AUTO_RETRY"] == 0
    assert payload["gates"]["LATE_RESULT_PROMOTION"] == 0
    assert payload["gates"]["canonical_activity_revision"] == 0
    assert rc == 0

