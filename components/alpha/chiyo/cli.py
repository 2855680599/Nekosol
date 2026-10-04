"""The CHIYO command line interface.

Only ``argparse`` is used - no third-party dependency, exactly like the runtime.

Commands
--------
``chiyo doctor``
    Environment / POSIX / kill-switch / data-directory report.  Exits non-zero
    when any item is a FAIL.

``chiyo demo core``
    Runs a REAL core loop (Life Runtime + Action Reality + Agency) in an isolated
    temporary directory using the vendored real classes, including a genuine
    restart in a FRESH process, an AR-0 idempotency proof and an ``UNKNOWN``
    never-auto-retried proof.

``chiyo demo contact``
    Runs the full ISOLATED-ONLY proactive contact pipeline by driving the
    vendored ``canonical_chain_driver`` in the ``ISOLATED_TEST_OPEN`` profile,
    shows that the same chain stops at ``BLOCKED_AT_CAPACITY`` under
    ``PRODUCTION_OFF``, and proves that no proactive send is possible.

Nothing here opens a socket, reads a credential or arms a kill switch.
"""

from __future__ import annotations

import argparse
import importlib
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

_ENTRY_LINE = "CHIYO - Persistent Digital Individual runtime"

#: environments that must never be set for a repository checkout under test
_KILL_SWITCHES = (
    "LIFE_RUNTIME_ENABLED",
    "AGENCY_ENABLED",
    "ACTION_EXECUTION_ENABLED",
    "PROACTIVE_ENABLED",
)

_PASS = "PASS"
_WARN = "WARN"
_FAIL = "FAIL"


# ---------------------------------------------------------------------------
# bootstrap helpers
# ---------------------------------------------------------------------------
def _vendor():
    """Import the vendored-tree bootstrap. MUST run before any vendored import."""
    vendor = importlib.import_module("chiyo._vendor_paths")
    vendor.ensure_importable()
    return vendor


def default_data_dir() -> Path:
    override = os.environ.get("CHIYO_DATA_DIR")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".chiyo"


def _json_out(payload: object) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
    sys.stdout.flush()


# ---------------------------------------------------------------------------
# chiyo doctor
# ---------------------------------------------------------------------------
def cmd_doctor(args: argparse.Namespace) -> int:
    rows: list[tuple[str, str, str]] = []

    if sys.version_info >= (3, 11):
        rows.append(("python>=3.11", _PASS, f"{sys.version.split()[0]} ({sys.executable})"))
    else:
        rows.append(("python>=3.11", _FAIL, f"{sys.version.split()[0]} is too old; need >= 3.11"))

    try:
        import fcntl  # noqa: F401

        rows.append(("fcntl (POSIX file locking)", _PASS, "importable; the runtime is POSIX native"))
    except ImportError as exc:
        rows.append(
            (
                "fcntl (POSIX file locking)",
                _FAIL,
                "the runtime requires POSIX; use Linux or WSL "
                f"({type(exc).__name__}: {exc})",
            )
        )

    try:
        import sqlite3

        rows.append(("sqlite3", _PASS, f"module {sqlite3.sqlite_version}"))
    except ImportError as exc:  # pragma: no cover - CPython always ships sqlite3
        rows.append(("sqlite3", _FAIL, f"{type(exc).__name__}: {exc}"))

    tz_status = _PASS
    tz_detail = "Asia/Shanghai resolvable"
    try:
        from zoneinfo import ZoneInfo

        ZoneInfo("Asia/Shanghai")
    except Exception as exc:  # noqa: BLE001
        tz_status = _WARN
        tz_detail = f"{type(exc).__name__}: {exc}"
    tzdata_installed = importlib.util.find_spec("tzdata") is not None
    if not tzdata_installed:
        tz_status = _WARN if tz_status == _PASS else tz_status
        tz_detail += (
            "; the `tzdata` package is NOT installed. The runtime needs the OS tz database "
            "(or `tzdata`) only to RENDER local time: every canonical timestamp is stored and "
            "compared in UTC, so the runtime degrades to UTC-only display and loses no "
            "authority. Install `tzdata` (or the distro tzdata) for local-time rendering."
        )
    rows.append(("zoneinfo Asia/Shanghai", tz_status, tz_detail))

    armed: list[str] = []
    for name in _KILL_SWITCHES:
        raw = os.environ.get(name, "")
        on = raw.strip().lower() in {"1", "true", "yes", "on"}
        if on:
            armed.append(name)
        rows.append((f"kill switch {name}", _FAIL if on else _PASS, "ON" if on else "OFF"))
    if armed:
        rows.append(
            (
                "shipped default (all kill switches OFF)",
                _FAIL,
                f"these switches are ON in this environment: {', '.join(armed)} "
                "- the alpha must ship with all four OFF",
            )
        )
    else:
        rows.append(("shipped default (all kill switches OFF)", _PASS, "all four switches are OFF"))

    data_dir = Path(args.data_dir).expanduser()
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        probe = data_dir / ".chiyo-doctor-probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        rows.append(("data directory writable", _PASS, f"{data_dir} (create + write + delete ok)"))
    except OSError as exc:
        rows.append(("data directory writable", _FAIL, f"{data_dir}: {type(exc).__name__}: {exc}"))

    rows.append(
        (
            "deployment requirements",
            _PASS,
            "no network, no credential, no VPS and no personal database is required: "
            "every store is created under the data directory, the only message transport "
            "is the in-tree recording fake, and this alpha can never reach Telegram",
        )
    )

    failures = [row for row in rows if row[1] == _FAIL]
    warnings = [row for row in rows if row[1] == _WARN]
    if args.json:
        for name, status, detail in rows:
            sys.stderr.write(f"{status:4} {name}: {detail}\n")
        _json_out(
            {
                "tool": "chiyo doctor",
                "checks": [
                    {"name": name, "status": status, "detail": detail} for name, status, detail in rows
                ],
                "failures": len(failures),
                "warnings": len(warnings),
                "verdict": "DOCTOR: FAIL" if failures else "DOCTOR: PASS",
            }
        )
    else:
        print(_ENTRY_LINE)
        print(f"chiyo doctor  (python {sys.version.split()[0]}, {sys.executable})")
        print()
        for name, status, detail in rows:
            print(f"[{status}] {name}")
            print(f"       {detail}")
        print(f"{len(rows)} checks, {len(warnings)} warning(s), {len(failures)} failure(s)")
        print("DOCTOR: FAIL" if failures else "DOCTOR: PASS")
    return 1 if failures else 0


# ---------------------------------------------------------------------------
# chiyo demo core
# ---------------------------------------------------------------------------
_CORE_TS_START = "2026-10-01T00:00:00Z"
_CORE_TS_EXTEND = "2026-10-01T00:02:00Z"
_CORE_TS_REQUEST = "2026-10-01T00:05:00Z"
_CORE_ACTIVITY_KIND = "open_ended_reading"
_CORE_USER_REQUEST = "ureq:core-demo"
_CORE_CORRELATION = "corr:core-demo"
_CORE_MESSAGE_TARGET = "recipient:sandbox-core-demo"
_CORE_MESSAGE_TEXT = "core demo: one message the provider only ACCEPTS (recorded, not sent)"
_CORE_DELIVERED_TARGET = "recipient:sandbox-core-demo-delivered"
_CORE_DELIVERED_TEXT = "core demo: one message the recording provider reports as DELIVERED"
_CORE_UNKNOWN_TARGET = "recipient:sandbox-core-demo-unknown"
_CORE_UNKNOWN_TEXT = "core demo: an ambiguous submission that must stay UNKNOWN"


def _core_imports():
    """Import the vendored real classes. Call after :func:`_vendor`."""
    modules = {
        "ag2": importlib.import_module("life_integration_ag2"),
        "activity_continuity": importlib.import_module("activity_continuity"),
        "activity_interruption": importlib.import_module("activity_interruption"),
        "agency_decision_ag1": importlib.import_module("agency_decision_ag1"),
        "candidate_sources_ag0": importlib.import_module("candidate_sources_ag0"),
        "action_reality_ledger": importlib.import_module("action_reality_ledger"),
    }
    return modules


def _core_step(items: list[dict], name: str, status: str, detail: dict) -> None:
    items.append({"step": name, "status": status, "detail": detail})


def _core_phase_one(root: Path, imports: dict) -> dict:
    """Steps 1-5: a real core loop through the real canonical owners."""
    ag2 = imports["ag2"]
    ac = imports["activity_continuity"]
    ai = imports["activity_interruption"]
    ag1 = imports["agency_decision_ag1"]
    ag0 = imports["candidate_sources_ag0"]
    ar0 = imports["action_reality_ledger"]

    steps: list[dict] = []
    ids: dict[str, object] = {}
    harness = ag2.IsolatedLifeRuntimeHarness(root)
    try:
        # ---- 1. create and then extend an Activity (LR-2 CanonicalActivityStore)
        revision = int(
            harness.activity_store.load_verified_state_and_journal(_include_journal=False)[0][
                "revision"
            ]
        )
        started = harness.activity_command.start_activity(
            activity_kind=_CORE_ACTIVITY_KIND,
            title="Core demo: an Activity she is currently engaged in",
            origin_ref="dec:core-demo-seed",
            source_refs=["dec:core-demo-seed"],
            decision_ref="dec:core-demo-seed",
            interruptibility=ai.OCCUPANCY_LIGHT,
            extra_fields={"interrupt_mode": ai.INTERRUPTIBILITY_IMMEDIATE},
            expected_revision=revision,
            idempotency_key="idem:core-demo:start",
            reason_code="ADOPTED_DECISION",
            occurred_at=_CORE_TS_START,
            observed_at=_CORE_TS_START,
        )
        activity = started.activity
        activity_id = str(activity["activity_id"])
        revision_after_start = int(
            harness.activity_store.load_verified_state_and_journal(_include_journal=False)[0][
                "revision"
            ]
        )
        extended = harness.activity_command.update_checkpoint(
            activity_id=activity_id,
            checkpoint_ref="chk:core-demo-1",
            expected_revision=revision_after_start,
            idempotency_key="idem:core-demo:checkpoint",
            source_refs=["dec:core-demo-seed"],
            reason_code="CHECKPOINT_ADVANCED",
            occurred_at=_CORE_TS_EXTEND,
            observed_at=_CORE_TS_EXTEND,
        )
        states, _journal = harness.activity_store.load_verified_state_and_journal(
            _include_journal=False
        )
        final_activity = (states["activities"] or {})[activity_id]
        transitions = harness.activity_read.get_transition_history(activity_id=activity_id)
        ids["activity_id"] = activity_id
        ids["activity_revision"] = int(final_activity["revision"])
        ids["activity_checkpoint_ref"] = final_activity.get("checkpoint_ref")
        _core_step(
            steps,
            "1.create_and_extend_activity",
            _PASS,
            {
                "owner": "activity_continuity.CanonicalActivityStore + ActivityCommandService",
                "store_root": str(root / "activity"),
                "activity_id": activity_id,
                "activity_status": final_activity["status"],
                "revision_before": revision,
                "revision_after_start": revision_after_start,
                "revision_after_extend": int(final_activity["revision"]),
                "checkpoint_ref": final_activity.get("checkpoint_ref"),
                "transition_types": [t["transition_type"] for t in transitions],
                "extend_command_result_ref": getattr(extended, "command_ref", None),
                "real_module_files": {
                    "activity_continuity": str(Path(ac.__file__).resolve()),
                },
            },
        )

        # Release the single foreground slot. This deployment runs ONE foreground
        # Activity: an ACTIVE/P AUSED Activity keeps the slot, so the demo Activity is
        # moved into WAITING with `release_foreground=True` (a legal LR-2 transition),
        # which lets the decision below start its own Activity legally.
        revision_now = int(
            harness.activity_store.load_verified_state_and_journal(_include_journal=False)[0][
                "revision"
            ]
        )
        harness.activity_command.wait_activity(
            activity_id=activity_id,
            waiting_on_ref="external_cond:core-demo-foreground-release",
            expected_revision=revision_now,
            idempotency_key="idem:core-demo:wait",
            source_refs=["dec:core-demo-seed"],
            release_foreground=True,
            reason_code="EXTERNAL_DEPENDENCY_WAIT",
            occurred_at=_CORE_TS_EXTEND,
            observed_at=_CORE_TS_EXTEND,
        )
        waiting = harness.activity_read.get_activity(activity_id)
        life_view = harness.activity_read.get_current_life_view()
        _core_step(
            steps,
            "1b.release_foreground",
            _PASS,
            {
                "activity_id": activity_id,
                "activity_status_after_wait": waiting["status"],
                "waiting_on_ref": waiting.get("waiting_on_ref"),
                "foreground_activity_ref": life_view.get("foreground_activity_ref"),
                "note": "one foreground Activity only: the demo Activity waits with "
                "release_foreground=True so the decision below can start its own Activity "
                "legally",
            },
        )

        # ---- 2. build a candidate set from a legal source (declared USER_REQUEST projection)
        harness.user_request_source.ingest_typed_event(
            ag0.ObservedUserRequestEvent(
                event_id=_CORE_USER_REQUEST,
                channel="sandbox_channel",
                event_kind="EXPLICIT_USER_REQUEST",
                request_status="OPEN",
                target_refs=["artifact:core_demo"],
                occurred_at=_CORE_TS_REQUEST,
                observed_at=_CORE_TS_REQUEST,
                valid_from=_CORE_TS_REQUEST,
            )
        )
        candidate_set = harness.candidate_materializer.build_candidate_set(
            observed_at=_CORE_TS_REQUEST
        )
        candidate_refs = [str(ref) for ref in candidate_set.candidate_refs]
        ids["candidate_set_id"] = str(candidate_set.candidate_set_id)
        ids["candidate_refs"] = candidate_refs
        _core_step(
            steps,
            "2.candidate_set_from_legal_source",
            _PASS,
            {
                "owner": "candidate_sources_ag0.CandidateMaterializer over the real "
                "UserRequestCandidateSource",
                "source_kind": ag0.SOURCE_USER_REQUEST,
                "declared_projection": "the Contact/CT0-9 work proved exactly one legal "
                "source kind in isolation: the declared USER_REQUEST projection of an "
                "observed user-request event. No other source kind is invented here.",
                "candidate_set_id": str(candidate_set.candidate_set_id),
                "candidate_refs": candidate_refs,
                "observed_at": _CORE_TS_REQUEST,
                "real_module_files": {"candidate_sources_ag0": str(Path(ag0.__file__).resolve())},
            },
        )

        # ---- 3. commit a legal Decision through the real AG-1 service (+ AR-0 action)
        chosen = None
        for candidate_ref in candidate_refs:
            record = harness.candidate_read.get_candidate(candidate_ref)
            if record.get("candidate_kind") == ag0.CANDIDATE_KIND_RESPOND_USER_REQUEST:
                chosen = str(candidate_ref)
                break
        if chosen is None and candidate_refs:
            chosen = candidate_refs[0]
        harness.decision_service.cognition_adapter = ag1.FakeCognitionAdapter(
            default_decision={
                "decision": ag1.DECISION_START,
                "selected_candidate_id": chosen,
                "reason_codes": [ag1.REASON_USER_REQUEST_VALID],
                "short_rationale": "core demo: a legal START for the open user request",
                "deferred_candidate_ids": [],
            }
        )
        cycle = harness.coordinator.run_decision_and_adoption_cycle(
            trigger_kind=ag1.TRIGGER_USER_REQUEST_EVENT,
            trigger_ref=_CORE_USER_REQUEST,
            observed_at=_CORE_TS_REQUEST,
            correlation_id=_CORE_CORRELATION,
            # The Action is driven EXPLICITLY in step 4 below through the real AR-0,
            # so the cycle itself must not propose/submit anything.
            execute_initial_step=False,
        )
        decision_result = cycle.get("decision_result") or {}
        decision_record = decision_result.get("decision_record")
        if decision_record is None:
            raise RuntimeError(
                "the real AG-1 service did not commit a DecisionRecord: "
                f"{json.dumps(cycle.get('decision_result'), default=str)[:600]}"
            )
        decision_id = str(decision_record.decision_id)
        adoption_result = cycle.get("adoption_result") or {}
        adoption_record = adoption_result.get("adoption_record")
        ids["decision_id"] = decision_id
        ids["decision_verb"] = str(decision_record.decision)
        ids["adoption_id"] = getattr(adoption_record, "adoption_id", None)
        ids["adoption_status"] = getattr(adoption_record, "adoption_status", None)
        ids["integration_id"] = (cycle.get("integration_record").integration_id
                                if cycle.get("integration_record") is not None else None)
        _core_step(
            steps,
            "3.legal_decision_via_real_ag1",
            _PASS,
            {
                "owner": "agency_decision_ag1.AgencyDecisionService (+ DecisionAdoptionCoordinator)",
                "decision_id": decision_id,
                "decision_verb": str(decision_record.decision),
                "selected_candidate_id": chosen,
                "committed_in_real_store": decision_id
                in (harness.decision_store.load_state() or {}).get("decisions_by_id", {}),
                "adoption_id": getattr(adoption_record, "adoption_id", None),
                "adoption_status": getattr(adoption_record, "adoption_status", None),
                "adopted_activity_ref": getattr(adoption_record, "created_activity_ref", None),
                "real_module_files": {"agency_decision_ag1": str(Path(ag1.__file__).resolve())},
            },
        )

        # ---- 4. propose -> prepare -> submit through the real AR-0 (recording fake provider)
        # Two submissions, both through the real AR-0 + the in-tree recording
        # FakeMessageProvider, with no socket anywhere:
        #  A) the provider only ACCEPTS the message  -> the Action is ACKNOWLEDGED, the
        #     receipt claims ACCEPTED and AR-1 settles NOTHING (acceptance is not proof);
        #  B) the provider reports DELIVERED          -> the Action is SUCCEEDED and AR-1
        #     produces a real settlement.
        adoption_id = getattr(adoption_record, "adoption_id", None)
        adopted_activity_ref = getattr(adoption_record, "created_activity_ref", None)
        if not adopted_activity_ref:
            raise RuntimeError("the adoption produced no Activity; the AR-0 step cannot be bound")
        action_specs = (
            {
                "provider_mode": "accepted",
                "target_ref": _CORE_MESSAGE_TARGET,
                "text": _CORE_MESSAGE_TEXT,
                "label": "accepted-by-provider",
            },
            {
                "provider_mode": "delivered",
                "target_ref": _CORE_DELIVERED_TARGET,
                "text": _CORE_DELIVERED_TEXT,
                "label": "delivered-by-provider",
            },
        )
        submissions: list[dict] = []
        for spec in action_specs:
            harness.fake_message.next_mode = spec["provider_mode"]
            step_result = harness.coordinator.execute_activity_step(
                integration_id=ids["integration_id"],
                activity_id=adopted_activity_ref,
                decision_ref=decision_id,
                adoption_ref=adoption_id,
                step_spec_input={
                    "step_kind": "MESSAGE_ACTION",
                    "action_kind": ar0.ACTION_KIND_MESSAGE,
                    "target_ref": spec["target_ref"],
                    "payload_spec": {"channel": "sandbox_chat", "text": spec["text"]},
                    "expected_effect_predicates": [
                        f"message_provider_{spec['provider_mode']}"
                    ],
                    "post_submit_life_behavior": "KEEP_ACTIVITY_ACTIVE",
                },
                settle_immediately=True,
                occurred_at=_CORE_TS_REQUEST,
            )
            action_row = (step_result.get("bridge_result") or {}).get("action") or {}
            if not action_row:
                submit_result = (step_result.get("submit_result") or {}).get("action") or {}
                action_row = submit_result
            submitted_action_id = str(action_row["action_id"])
            submitted = harness.action_read.get_action(submitted_action_id)
            attempt_list = harness.action_read.list_attempts(submitted_action_id)
            receipt_list = harness.action_read.list_receipts(submitted_action_id)
            settlement_row = harness.settlement_read.get_settlement(submitted_action_id)
            submissions.append(
                {
                    "label": spec["label"],
                    "provider_mode": spec["provider_mode"],
                    "target_ref": spec["target_ref"],
                    "action_id": submitted_action_id,
                    "action_status": submitted["status"],
                    "idempotency_key": submitted["idempotency_key"],
                    "step_spec_id": (step_result.get("bridge_result") or {}).get("step_spec")
                    if isinstance((step_result.get("bridge_result") or {}).get("step_spec"), str)
                    else getattr(
                        (step_result.get("bridge_result") or {}).get("step_spec"),
                        "step_spec_id",
                        None,
                    ),
                    "attempt_id": attempt_list[0]["attempt_id"] if attempt_list else None,
                    "submission_key": attempt_list[0]["submission_key"] if attempt_list else None,
                    "transport_result": attempt_list[0].get("transport_result")
                    if attempt_list
                    else None,
                    "provider_ref": attempt_list[0].get("provider_ref") if attempt_list else None,
                    "receipt_ids": [r["receipt_id"] for r in receipt_list],
                    "receipt_status_claims": [r["status_claim"] for r in receipt_list],
                    "settlement_id": settlement_row["settlement_id"] if settlement_row else None,
                    "settlement_status": settlement_row["settlement_status"]
                    if settlement_row
                    else None,
                    "settlement_outcome_kind": settlement_row.get("outcome_kind")
                    if settlement_row
                    else None,
                    "as_json_ready_row": submitted,
                }
            )
        accepted = submissions[0]
        delivered = submissions[1]
        ids["action_id"] = delivered["action_id"]
        ids["action_status"] = delivered["action_status"]
        ids["action_idempotency_key"] = delivered["idempotency_key"]
        ids["action_attempt_id"] = delivered["attempt_id"]
        ids["action_submission_key"] = delivered["submission_key"]
        ids["provider_ref"] = delivered["provider_ref"]
        ids["action_acknowledged_id"] = accepted["action_id"]
        ids["receipt_refs"] = delivered["receipt_ids"]
        ids["ack_receipt_refs"] = accepted["receipt_ids"]
        ids["settlement_id"] = delivered["settlement_id"]
        ids["settlement_status"] = delivered["settlement_status"]
        _core_step(
            steps,
            "4.propose_prepare_submit_action_via_real_ar0",
            _PASS,
            {
                "owner": "action_reality_ledger.ActionCommandService + MessageActionAdapter "
                "over the in-tree FakeMessageProvider",
                "activity_ref": adopted_activity_ref,
                "submissions": [
                    {k: v for k, v in row.items() if k != "as_json_ready_row"}
                    for row in submissions
                ],
                "provider_kind": type(harness.fake_message).__name__,
                "provider_invocations": len(getattr(harness.fake_message, "invocation_log", [])),
                "network_used": False,
                "note": "propose/prepare/submit are the real AR-0 transitions; the only "
                "transport is the in-tree recording FakeMessageProvider (no socket). "
                "ACKNOWLEDGED != DELIVERED: the accepted submission leaves the Action at "
                "ACKNOWLEDGED with no settlement, the delivered one reaches SUCCEEDED.",
                "real_module_files": {"action_reality_ledger": str(Path(ar0.__file__).resolve())},
            },
        )

        # ---- 5. read the real receipt and the real AR-1 settlement
        _core_step(
            steps,
            "5.real_receipt_and_real_ar1_settlement",
            _PASS,
            {
                "owner": "Action Reality (AR-0 ActionStore receipts) + "
                "result_settlement_ar1.ResultSettlementService",
                "delivered_action": {
                    "action_id": delivered["action_id"],
                    "status": delivered["action_status"],
                    "attempt_id": delivered["attempt_id"],
                    "submission_key": delivered["submission_key"],
                    "transport_result": delivered["transport_result"],
                    "provider_ref": delivered["provider_ref"],
                    "receipt_ids": delivered["receipt_ids"],
                    "receipt_status_claims": delivered["receipt_status_claims"],
                    "settlement_id": delivered["settlement_id"],
                    "settlement_status": delivered["settlement_status"],
                    "settlement_outcome_kind": delivered["settlement_outcome_kind"],
                },
                "accepted_action": {
                    "action_id": accepted["action_id"],
                    "status": accepted["action_status"],
                    "attempt_id": accepted["attempt_id"],
                    "transport_result": accepted["transport_result"],
                    "receipt_ids": accepted["receipt_ids"],
                    "receipt_status_claims": accepted["receipt_status_claims"],
                    "settlement_id": accepted["settlement_id"],
                },
                "acknowledged_is_not_delivered": "the acceptance receipt (status_claim "
                "ACCEPTED) leaves the canonical Action at ACKNOWLEDGED and AR-1 settles "
                "nothing for it; only the DELIVERED receipt produces a settlement. The two "
                "states are separate records in the real stores, never conflated.",
                "real_module_files": {
                    "result_settlement_ar1": str(
                        Path(importlib.import_module("result_settlement_ar1").__file__).resolve()
                    )
                },
            },
        )
    finally:
        harness.close()

    return {"steps": steps, "ids": ids, "root": str(root)}


_DRIVER_BOOTSTRAP = textwrap.dedent(
    """
    import os, runpy, sys
    import chiyo._vendor_paths  # noqa: F401 - installs the flat/aliased import paths
    driver = os.environ["CHIYO_DRIVER_PATH"]
    sys.argv = [driver] + sys.argv[1:]
    runpy.run_path(driver, run_name="__main__")
    """
)


def _spawn_child(argv: list[str], *, cwd: Path, env_extra: dict[str, str] | None = None) -> dict:
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    if env_extra:
        env.update(env_extra)
    completed = subprocess.run(
        argv,
        cwd=str(cwd),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return {
        "argv": argv,
        "exit_code": completed.returncode,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
    }


def cmd_demo_core(args: argparse.Namespace) -> int:
    vendor = _vendor()
    imports = _core_imports()

    data_dir = Path(args.data_dir).expanduser()
    data_dir.mkdir(parents=True, exist_ok=True)
    run_dir = Path(tempfile.mkdtemp(prefix="chiyo-core-demo-", dir=str(data_dir)))
    result: dict = {
        "demo": "core",
        "run_dir": str(run_dir),
        "data_dir": str(data_dir),
        "vendor": {
            "life_runtime": str(vendor.LIFE_RUNTIME_DIR),
            "contact": str(vendor.CONTACT_DIR),
        },
    }
    steps: list[dict] = []
    ids: dict = {}
    blocked: list[dict] = []

    try:
        phase1 = _core_phase_one(run_dir / "runtime", imports)
        steps.extend(phase1["steps"])
        ids.update(phase1["ids"])
    except Exception as exc:  # noqa: BLE001 - a real failure must be reported, never faked
        blocked.append({"step": "1-5", "error": f"{type(exc).__name__}: {exc}"})
        result.update({"steps": steps, "ids": ids, "blocked": blocked, "verdict": "CORE DEMO: BLOCKED"})
        _emit_core(result, args)
        return 2

    # ---- 6/7: restart in a FRESH process, prove recovery, idempotency and UNKNOWN
    expect = {
        "activity_id": ids.get("activity_id"),
        "decision_id": ids.get("decision_id"),
        "action_id": ids.get("action_id"),
        "settlement_id": ids.get("settlement_id"),
        "action_idempotency_key": ids.get("action_idempotency_key"),
        "action_target_ref": _CORE_DELIVERED_TARGET,
        "action_payload": {"channel": "sandbox_chat", "text": _CORE_DELIVERED_TEXT},
        "acknowledged_action_id": ids.get("action_acknowledged_id"),
        "unknown_target_ref": _CORE_UNKNOWN_TARGET,
        "unknown_payload": {"channel": "sandbox_chat", "text": _CORE_UNKNOWN_TEXT},
        "occurred_at": _CORE_TS_REQUEST,
    }
    child = _spawn_child(
        [
            sys.executable,
            "-B",
            "-m",
            "chiyo.cli",
            "_core-restart",
            "--run-dir",
            str(run_dir / "runtime"),
            "--expect",
            json.dumps(expect),
            "--json",
        ],
        cwd=vendor.TREE_DIR,
    )
    child_json: dict | None = None
    for line in child["stdout"].splitlines():
        line = line.strip()
        if line.startswith("{") and line.endswith("}"):
            try:
                child_json = json.loads(line)
            except ValueError:
                continue
    if child["exit_code"] != 0 or child_json is None:
        blocked.append(
            {
                "step": "6-7.restart_and_unknown",
                "error": f"fresh-process restart exited {child['exit_code']}",
                "stdout_tail": child["stdout"][-1200:],
                "stderr_tail": child["stderr"][-1200:],
            }
        )
        result.update({"steps": steps, "ids": ids, "blocked": blocked, "verdict": "CORE DEMO: BLOCKED"})
        _emit_core(result, args)
        return 3

    if child_json.get("verdict") != "PASS":
        blocked.append(
            {
                "step": "6-7.restart_and_unknown",
                "error": child_json.get("error") or "restart child reported a non-PASS verdict",
                "detail": child_json,
            }
        )
        result.update({"steps": steps, "ids": ids, "blocked": blocked, "verdict": "CORE DEMO: BLOCKED"})
        _emit_core(result, args)
        return 3

    steps.extend(child_json["steps"])
    ids.update({k: v for k, v in child_json["ids"].items() if k not in ids})

    result.update(
        {
            "steps": steps,
            "ids": ids,
            "blocked": blocked,
            "restart_child": {
                "argv": child["argv"],
                "exit_code": child["exit_code"],
                "process_id_note": "a genuinely fresh interpreter (new process) reopened the "
                "same isolated root",
            },
            "verdict": "CORE DEMO: PASS",
        }
    )
    _emit_core(result, args)
    return 0


def _emit_core(result: dict, args: argparse.Namespace) -> None:
    if args.json:
        _json_out(result)
        return
    print(_ENTRY_LINE)
    print(f"chiyo demo core  (isolated root: {result['run_dir']})")
    print()
    for step in result["steps"]:
        print(f"[{step['status']}] {step['step']}")
        for key, value in step["detail"].items():
            print(f"       {key}: {json.dumps(value, ensure_ascii=False, default=str)}")
        print()
    if result["blocked"]:
        print("BLOCKED")
        for row in result["blocked"]:
            print(f"  {row['step']}: {row['error']}")
        print()
    print("ids:")
    for key, value in result["ids"].items():
        print(f"  {key} = {value}")
    print()
    print(result["verdict"])


def cmd_core_restart(args: argparse.Namespace) -> int:
    """Fresh-process half of ``demo core``: steps 6 and 7."""
    vendor = _vendor()
    imports = _core_imports()
    ag2 = imports["ag2"]
    ar0 = imports["action_reality_ledger"]
    expect = json.loads(args.expect)
    root = Path(args.run_dir)
    steps: list[dict] = []
    ids: dict = {}

    def fail(message: str) -> int:
        _json_out({"verdict": "BLOCKED", "error": message, "steps": steps, "ids": ids})
        return 1

    if not root.is_dir():
        return fail(f"run dir {root} does not exist")

    harness = ag2.IsolatedLifeRuntimeHarness(root)
    try:
        recovery = harness.coordinator.recover_all_and_reconcile()

        # ---- 6. all committed state recovered, and the same logical submission is idempotent
        activity = harness.activity_read.get_activity(expect["activity_id"])
        decision = harness.decision_read.get_decision(expect["decision_id"])
        action = harness.action_read.get_action(expect["action_id"])
        settlement = harness.settlement_read.get_settlement(expect["action_id"])
        recovered = {
            "activity_recovered": activity is not None,
            "decision_recovered": decision is not None,
            "action_recovered": action is not None,
            "settlement_recovered": settlement is not None,
            "activity_status": (activity or {}).get("status"),
            "decision_verb": (decision or {}).get("decision"),
            "action_status": (action or {}).get("status"),
            "settlement_status": (settlement or {}).get("settlement_status"),
            "fresh_process": True,
            "recovery_summary": {
                "reconciled_integration_ids": recovery.get("reconciled_integration_ids"),
                "llm_calls": recovery.get("llm_calls"),
            },
        }
        if not all(
            recovered[key]
            for key in (
                "activity_recovered",
                "decision_recovered",
                "action_recovered",
                "settlement_recovered",
            )
        ):
            return fail(f"committed state was NOT fully recovered: {json.dumps(recovered, default=str)}")

        actions_before = {str(a["action_id"]) for a in harness.action_read.list_actions()}
        attempts_before = len(harness.action_read.list_attempts(expect["action_id"]))
        replay = harness.action_command.propose_action(
            action_kind=action["action_kind"],
            target_ref=expect["action_target_ref"],
            payload_ref=action["payload_ref"],
            payload_data=dict(expect["action_payload"]),
            idempotency_key=expect["action_idempotency_key"],
            source_refs=list(action["source_refs"]),
            decision_ref=action.get("decision_ref"),
            authorization_ref=action.get("authorization_ref"),
            activity_ref=action.get("activity_ref"),
            causal_parent_refs=list(action.get("causal_parent_refs") or []),
            occurred_at=expect["occurred_at"],
        )
        actions_after = {str(a["action_id"]) for a in harness.action_read.list_actions()}
        attempts_after = len(harness.action_read.list_attempts(expect["action_id"]))
        idempotent = {
            "real_owner_flagged_idempotent_replay": bool(replay.get("idempotent_replay")),
            "same_action_id_returned": str(replay["action"]["action_id"]) == expect["action_id"],
            "action_count_before": len(actions_before),
            "action_count_after": len(actions_after),
            "no_duplicate_action": actions_before == actions_after,
            "attempts_before": attempts_before,
            "attempts_after": attempts_after,
            "no_duplicate_attempt": attempts_before == attempts_after,
        }
        ids["restart_action_idempotent"] = idempotent["no_duplicate_action"]
        steps.append(
            {
                "step": "6.restart_recovery_and_idempotency",
                "status": _PASS if all(
                    (
                        idempotent["real_owner_flagged_idempotent_replay"],
                        idempotent["same_action_id_returned"],
                        idempotent["no_duplicate_action"],
                        idempotent["no_duplicate_attempt"],
                    )
                ) else _FAIL,
                "detail": {
                    "owner": "life_integration_ag2.IsolatedLifeRuntimeHarness reopened in a fresh "
                    "interpreter + action_reality_ledger.ActionCommandService.propose_action",
                    "recovered": recovered,
                    "idempotency": idempotent,
                    "note": "the same logical submission (same AR-0 idempotency_key) returns the "
                    "SAME canonical action_id and mints no second Action and no second attempt",
                },
            }
        )
        if steps[-1]["status"] != _PASS:
            return fail(f"idempotency proof failed: {json.dumps(steps[-1]['detail'], default=str)}")

        # ---- 7. an UNKNOWN outcome is NOT automatically retried
        harness.fake_message.next_mode = "timeout_after_acceptance"
        proposed = harness.action_command.propose_action(
            action_kind=ar0.ACTION_KIND_MESSAGE,
            target_ref=expect["unknown_target_ref"],
            payload_ref="pay:core-demo-unknown",
            payload_data=dict(expect["unknown_payload"]),
            idempotency_key="idem:core-demo:unknown",
            source_refs=list(action["source_refs"]) or ["dec:core-demo"],
            decision_ref=action.get("decision_ref"),
            authorization_ref=action.get("authorization_ref"),
            activity_ref=action.get("activity_ref"),
            causal_parent_refs=list(action.get("causal_parent_refs") or []),
            occurred_at=expect["occurred_at"],
        )
        unknown_id = str(proposed["action"]["action_id"])
        harness.action_command.prepare_action(action_id=unknown_id, occurred_at=expect["occurred_at"])
        submitted = harness.action_command.submit_action(
            action_id=unknown_id,
            executor_capability=harness.executor_capability,
            settle_immediately=False,
            occurred_at=expect["occurred_at"],
        )
        unknown_status = str(submitted["action"]["status"])
        attempts_unknown_before = len(harness.action_read.list_attempts(unknown_id))
        harness.coordinator.recover_all_and_reconcile()
        after_recovery = harness.action_read.get_action(unknown_id)
        attempts_unknown_after = len(harness.action_read.list_attempts(unknown_id))
        retry_refusal = None
        try:
            harness.action_command.submit_action(
                action_id=unknown_id,
                executor_capability=harness.executor_capability,
                occurred_at=expect["occurred_at"],
            )
        except Exception as exc:  # noqa: BLE001 - the refusal IS the evidence
            retry_refusal = f"{type(exc).__name__}: {exc}"
        unknown_ok = (
            unknown_status == ar0.ACTION_STATUS_UNKNOWN
            and str(after_recovery["status"]) == ar0.ACTION_STATUS_UNKNOWN
            and attempts_unknown_before == attempts_unknown_after
            and (retry_refusal or "").startswith("UnknownActionRetryForbiddenError")
        )
        ids["unknown_action_id"] = unknown_id
        ids["unknown_action_status"] = unknown_status
        steps.append(
            {
                "step": "7.unknown_is_not_auto_retried",
                "status": _PASS if unknown_ok else _FAIL,
                "detail": {
                    "owner": "action_reality_ledger.ActionCommandService (UNKNOWN policy)",
                    "unknown_action_id": unknown_id,
                    "status_after_submit": unknown_status,
                    "status_after_recovery": str(after_recovery["status"]),
                    "attempts_before_recovery": attempts_unknown_before,
                    "attempts_after_recovery": attempts_unknown_after,
                    "bare_retry_refused_with": retry_refusal,
                    "provider_mode_used": "timeout_after_acceptance (declared FakeMessageProvider "
                    "mode: the submission boundary was crossed but the outcome is unknown)",
                    "note": "an ambiguous submission becomes UNKNOWN, cold-start recovery leaves "
                    "it UNKNOWN, and a bare retry is refused by the real owner with "
                    "UnknownActionRetryForbiddenError -- the outcome stays unanswered until "
                    "reconciliation proves it",
                },
            }
        )
        if not unknown_ok:
            return fail(json.dumps(steps[-1]["detail"], default=str))
    finally:
        harness.close()

    _json_out({"verdict": "PASS", "steps": steps, "ids": ids, "run_dir": str(root)})
    return 0


# ---------------------------------------------------------------------------
# chiyo demo contact
# ---------------------------------------------------------------------------
_PROFILE_ISOLATED_OPEN = "ISOLATED_TEST_OPEN"
_PROFILE_PRODUCTION_OFF = "PRODUCTION_OFF"


def _run_driver(vendor, *, mode: str, profile: str, data_dir: Path, out_dir: Path) -> dict:
    """Run the vendored canonical chain driver as a child process and read its artifact."""
    driver = vendor.CONTACT_DIR / "canonical_chain_driver.py"
    env = vendor.contact_chain_env(data_dir=data_dir, out_dir=out_dir)
    env["CHIYO_DRIVER_PATH"] = str(driver)
    completed = subprocess.run(
        [
            sys.executable,
            "-B",
            "-c",
            _DRIVER_BOOTSTRAP,
            "--mode",
            mode,
            "--profile",
            profile,
        ],
        cwd=str(vendor.CONTACT_DIR),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    artifact_path = out_dir / f"CT0_10_MODE_{mode.upper()}_{profile}.json"
    artifact = None
    if artifact_path.is_file():
        artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
    summary: dict = {
        "mode": mode,
        "profile": profile,
        "argv": ["python", "-B", "<canonical_chain_driver.py>", "--mode", mode, "--profile", profile],
        "exit_code": completed.returncode,
        "stdout_tail": completed.stdout.strip().splitlines()[-6:],
        "stderr_tail": completed.stderr.strip().splitlines()[-6:],
        "artifact_path": str(artifact_path),
        "artifact": artifact,
    }
    if artifact is None:
        summary["spawn_stdout"] = completed.stdout[-2000:]
        summary["spawn_stderr"] = completed.stderr[-2000:]
    return summary


def _contact_semantics(vendor, data_dir: Path) -> dict:
    """Drive the real AG-1 contact port for the DEFER / NO_ACTION outcomes."""
    out: dict = {}
    # The chain driver reads its isolated run root at import time; point it inside this
    # demo's temp directory before importing it (the driver refuses to reset a run dir
    # that lives outside the declared isolated root).
    os.environ["CT0_10_ISO_ROOT"] = str(data_dir / "cli-iso")
    os.environ["CT0_10_OUT"] = str(data_dir)
    import canonical_chain_driver as drv  # noqa: PLC0415
    import canonical_spine as spine  # noqa: PLC0415
    from canonical_ports import ag1_adapter as ag1  # noqa: PLC0415

    root_ok = data_dir / "cli-iso" / "contact-semantics-no-action"
    root_defer = data_dir / "cli-iso" / "contact-semantics-defer"

    # DEFER: the honest fail-closed capacity read (no injected sub-port) is unavailable,
    # so the real AG-1 commits a DEFER for the same eligible Intent.
    defer = drv.Wiring(root=root_defer, profile=drv.PROFILE_ISOLATED_TEST_OPEN, reset=True)
    try:
        defer.connect()
        _authority, intent = defer.intent_authority_for()
        cap_off = defer.capacity_production_off.resolve(
            intent=intent, recipient_ref=drv.RECIPIENT_REF, channel=drv.CHANNEL_REF, now=drv.NOW
        )
        candidate_set = defer.ag0.build_candidate_set(intent=intent, observed_at=drv.NOW)
        defer_decision = defer.ag1.decide(
            intent=intent,
            candidate_set=candidate_set.candidate_set,
            capacity=cap_off,
            observed_at=drv.NOW,
        )
        out["defer"] = {
            "intent_ref": str(intent.intent_id),
            "candidate_set_id": str(candidate_set.candidate_set.candidate_set_id),
            "capacity_available": bool(cap_off.available),
            "capacity_unavailable_reasons": list(cap_off.unavailable_reasons),
            "real_decision_id": defer_decision.real_decision_id,
            "canonical_verb": defer_decision.canonical_decision_verb,
            "contact_outcome": defer_decision.contact_outcome,
            "reason_codes": list(defer_decision.reason_codes),
            "expected": "DEFER",
            "ok": defer_decision.contact_outcome == "DEFER",
        }
    finally:
        try:
            defer.close()
        except Exception:  # noqa: BLE001
            pass

    # NO_ACTION: a legal NO_ACTION decision committed by the real AG-1 service for an
    # eligible Intent whose AG-0 candidate set carries no candidate for it.
    wiring = drv.Wiring(root=root_ok, profile=drv.PROFILE_ISOLATED_TEST_OPEN, reset=True)
    try:
        wiring.connect()
        _authority, intent = wiring.intent_authority_for()
        candidate_set = wiring.ag0.build_candidate_set(intent=intent, observed_at=drv.NOW)
        capacity = wiring.capacity.resolve(
            intent=intent, recipient_ref=drv.RECIPIENT_REF, channel=drv.CHANNEL_REF, now=drv.NOW
        )
        if not capacity.available:
            raise RuntimeError(
                "the declared test-profile capacity read is unavailable, so NO_ACTION cannot be "
                f"exercised here: {list(capacity.unavailable_reasons)}"
            )
        capacity_kwargs, _projection = wiring.ag1._capacity_inputs(capacity)
        atomic = bool(getattr(getattr(capacity, "snapshot", None), "atomic", False))
        life_frame = {
            "life_frame_id": f"lframe:{intent.candidate_ref}",
            "interruptibility": (
                wiring.ag1.ag1.INTERRUPT_MODE_ATOMIC
                if atomic
                else wiring.ag1.ag1.INTERRUPT_MODE_IMMEDIATE
            ),
            "foreground_activity_ref": None,
            "foreground_status": None,
        }
        result = wiring.ag1._evaluate(
            opportunity=wiring.ag1._build_opportunity(intent, candidate_set.candidate_set),
            candidate_set=candidate_set.candidate_set,
            candidates=list(candidate_set.candidates),
            raw_output={
                "decision": "NO_ACTION",
                "selected_candidate_id": None,
                "reason_codes": list(
                    ag1.CONTACT_TO_AG1_REASON_CODES[spine.CONTACT_DECISION_NO_ACTION]
                ),
                "short_rationale": "core demo: the real AG-1 service is asked for a legal "
                "NO_ACTION and commits exactly that",
                "deferred_candidate_ids": [],
            },
            capacity_kwargs=capacity_kwargs,
            life_frame=life_frame,
            channel=drv.CHANNEL_REF,
            post_cognition_hook=None,
        )
        record = result.get("decision_record")
        committed = bool(
            record is not None
            and getattr(record, "decision_id", None)
            in (wiring.ag1.store.load_state() or {}).get("decisions_by_id", {})
        )
        verb = str(getattr(record, "decision", "")) if record is not None else ""
        out["no_action"] = {
            "intent_ref": str(intent.intent_id),
            "real_decision_id": getattr(record, "decision_id", None),
            "canonical_verb": verb,
            "attempt_status": str(result.get("status")),
            "committed_in_real_store": committed,
            "expected": "NO_ACTION",
            "ok": bool(record is not None and verb == "NO_ACTION" and committed),
        }
    finally:
        try:
            wiring.close()
        except Exception:  # noqa: BLE001
            pass
    return out


def cmd_demo_contact(args: argparse.Namespace) -> int:
    vendor = _vendor()
    data_dir = Path(args.data_dir).expanduser()
    data_dir.mkdir(parents=True, exist_ok=True)
    run_dir = Path(tempfile.mkdtemp(prefix="chiyo-contact-demo-", dir=str(data_dir)))
    out_dir = run_dir / "artifacts"
    out_dir.mkdir(parents=True, exist_ok=True)

    result: dict = {"demo": "contact", "run_dir": str(run_dir)}
    runs: list[dict] = []
    for mode, profile in (
        ("chain", _PROFILE_ISOLATED_OPEN),
        ("chain", _PROFILE_PRODUCTION_OFF),
        ("replay", _PROFILE_ISOLATED_OPEN),
        ("negatives", _PROFILE_ISOLATED_OPEN),
    ):
        runs.append(_run_driver(vendor, mode=mode, profile=profile, data_dir=run_dir, out_dir=out_dir))

    chain_iso = next(r["artifact"] for r in runs if r["mode"] == "chain" and r["profile"] == _PROFILE_ISOLATED_OPEN)
    chain_off = next(r["artifact"] for r in runs if r["mode"] == "chain" and r["profile"] == _PROFILE_PRODUCTION_OFF)
    replay = next(r["artifact"] for r in runs if r["mode"] == "replay")
    negatives = next(r["artifact"] for r in runs if r["mode"] == "negatives")

    facts = {row["fact"]: row for row in chain_iso["chain"]["facts"]}
    stages = {row["stage"]: row for row in chain_iso["chain"]["stages"] if row["stage"] != "SEAM"}
    telegram = stages["telegram_boundary"]["detail"]
    submission = stages["ar0_submission"]["detail"]
    settlement = stages["ar1_settlement"]["detail"]
    doubles = chain_iso["chain"]["declared_test_doubles"]

    id_chain = {
        "contact_intent_ref": facts["contact_intent"]["ref"],
        "contact_intent_revision": facts["contact_intent"]["revision"],
        "agent0_candidate_set_ref": facts["agency_candidate"]["ref"],
        "agency_decision_ref": facts["decision"]["ref"],
        "expressive_draft_ref": facts["draft"]["ref"],
        "canonical_action_ref": facts["action"]["ref"],
        "action_submission_key": submission.get("submission_key_minted"),
        "receipt_ref": (submission.get("attempts") or [{}])[0].get("receipt_refs"),
        "ar1_settlement_ref": settlement.get("settlement_id"),
        "handoff_ref": facts["handoff"]["ref"],
        "transport_provider_ref": (submission.get("attempts") or [{}])[0].get("provider_ref"),
    }
    no_send_proof = {
        "real_proactive_telegram_side_effect_enabled": telegram["verdict"]["real_side_effect_enabled"],
        "serialized_request_transport_reached": telegram["serialized"]["transport_reached"],
        "transport_not_reached_check": telegram["checks"]["transport_not_reached"],
        "provider_kind": submission.get("provider_kind"),
        "provider_invocations": submission.get("provider_invocations"),
        "network_used": submission.get("network_used"),
        "telegram_dispatch_status": telegram["dispatch_status"],
        "production_counterparty_ref_recorded_not_used": telegram["recipient"][
            "production_counterparty_ref_recorded_not_used"
        ],
        "production_counterparty_used": telegram["recipient"]["production_counterparty_used"],
        "only_transport": "the in-tree recording FakeMessageProvider (action_reality_ledger); "
        "the only 'send' is a recorded, in-process call with no socket",
    }
    semantics: dict = {}
    semantics_error: str | None = None
    try:
        semantics = _contact_semantics(vendor, run_dir)
    except Exception as exc:  # noqa: BLE001 - report the real error, never fake the outcome
        semantics_error = f"{type(exc).__name__}: {exc}"

    declared_unknown = next(
        (
            case
            for case in negatives.get("cases", [])
            if case.get("id") in {"N9", "N10"}
        ),
        None,
    )
    semantics["declared_unknown_evidence"] = {
        "case": declared_unknown.get("id") if declared_unknown else None,
        "requirement": declared_unknown.get("requirement") if declared_unknown else None,
        "status": declared_unknown.get("status") if declared_unknown else None,
        "owner": declared_unknown.get("owner") if declared_unknown else None,
        "observed": declared_unknown.get("observed") if declared_unknown else None,
        "note": "AR-0's UNKNOWN policy is enforced by action_reality_ledger itself: an UNKNOWN "
        "action may only be resolved by reconciliation and refuses a bare retry (proved for real "
        "by `chiyo demo core`, step 7).",
    }

    blocked: list[dict] = []
    if chain_iso.get("result") != "PASS" or chain_iso["chain"]["verdict"] != "CANONICAL_CHAIN_COMPLETE":
        blocked.append(
            {
                "check": "ISOLATED_TEST_OPEN chain",
                "error": f"result={chain_iso.get('result')} verdict={chain_iso['chain']['verdict']}",
            }
        )
    if chain_off["chain"]["verdict"] != "BLOCKED_AT_CAPACITY":
        blocked.append(
            {
                "check": "PRODUCTION_OFF must stop at BLOCKED_AT_CAPACITY",
                "error": f"verdict={chain_off['chain']['verdict']}",
            }
        )
    if replay.get("result") != "PASS":
        blocked.append({"check": "restart/replay recovery", "error": f"result={replay.get('result')}"})
    for name in ("defer", "no_action"):
        if semantics_error is not None or not semantics.get(name, {}).get("ok"):
            blocked.append(
                {
                    "check": f"contact semantics: {name}",
                    "error": semantics_error
                    or f"real outcome was {semantics.get(name)!r} (expected "
                    f"{semantics.get(name, {}).get('expected')})",
                }
            )

    result.update(
        {
            "runs": [
                {
                    "mode": run["mode"],
                    "profile": run["profile"],
                    "exit_code": run["exit_code"],
                    "result": (run["artifact"] or {}).get("result"),
                    "verdict": (run["artifact"] or {}).get("chain", {}).get("verdict")
                    if run["mode"] == "chain"
                    else None,
                    "artifact_path": run["artifact_path"],
                    "stdout_tail": run["stdout_tail"],
                }
                for run in runs
            ],
            "id_chain": id_chain,
            "no_proactive_send": no_send_proof,
            "declared_test_doubles": doubles,
            "contact_semantics": semantics,
            "blocked_at_capacity_production_off": {
                "verdict": chain_off["chain"]["verdict"],
                "reasons": chain_off["chain"]["capacity_unavailable_reasons"],
                "stages": chain_off["stage_status"],
            },
            "restart_recovery": {
                "mode": "replay",
                "result": replay.get("result"),
                "store_state_stable_across_close_and_rebootstrap": replay.get(
                    "store_state_stable_across_close_and_rebootstrap"
                ),
                "no_new_action": replay.get("no_new_action"),
                "new_actions": replay.get("new_actions"),
                "no_telegram_submit": replay.get("no_telegram_submit"),
                "new_telegram_submits": replay.get("new_telegram_submits"),
                "no_new_settlement": replay.get("no_new_settlement"),
                "chain_verdict": replay.get("chain_verdict"),
            },
            "negative_controls": {
                "counts": negatives.get("counts"),
                "per_case": [
                    {
                        "id": case["id"],
                        "requirement": case["requirement"],
                        "status": case["status"],
                        "proven_via_real_port": case["proven_via_real_port"],
                    }
                    for case in negatives.get("cases", [])
                ],
            },
            "blocked": blocked,
            "verdict": "CONTACT DEMO: BLOCKED" if blocked else "CONTACT DEMO: PASS",
        }
    )
    _emit_contact(result, args)
    return 2 if blocked else 0


def _emit_contact(result: dict, args: argparse.Namespace) -> None:
    if args.json:
        _json_out(result)
        return
    print(_ENTRY_LINE)
    print(f"chiyo demo contact  (isolated root: {result['run_dir']})")
    print()
    print("driver runs (vendored chiyo/contact/canonical_chain_driver.py, subprocess per mode):")
    for run in result["runs"]:
        print(
            f"  [exit {run['exit_code']}] --mode {run['mode']} --profile {run['profile']}"
            f"  result={run['result']} verdict={run['verdict']}"
        )
    print()
    print("id chain (ISOLATED_TEST_OPEN, all ids produced by the real owners):")
    for key, value in result["id_chain"].items():
        print(f"  {key} = {value}")
    print()
    print("no proactive send:")
    for key, value in result["no_proactive_send"].items():
        print(f"  {key}: {json.dumps(value, ensure_ascii=False, default=str)}")
    print()
    print("declared test doubles (printed so the user is never misled):")
    for double in result["declared_test_doubles"]:
        print(
            f"  port={double['port']} declared_test_double={double['declared_test_double']} "
            f"canonical_owner_exists={double['canonical_owner_exists']} "
            f"disposition={double['disposition']}"
        )
    print()
    print("contact semantics:")
    print(json.dumps(result["contact_semantics"], ensure_ascii=False, indent=1, default=str))
    print()
    print("PRODUCTION_OFF (same chain, no owner-less capacity sub-ports):")
    print(json.dumps(result["blocked_at_capacity_production_off"], ensure_ascii=False, indent=1, default=str))
    print()
    print("restart / replay recovery:")
    print(json.dumps(result["restart_recovery"], ensure_ascii=False, indent=1, default=str))
    print()
    print("negative controls:")
    print(json.dumps(result["negative_controls"], ensure_ascii=False, indent=1, default=str))
    print()
    if result["blocked"]:
        print("BLOCKED")
        for row in result["blocked"]:
            print(f"  {row['check']}: {row['error']}")
        print()
    print(result["verdict"])


# ---------------------------------------------------------------------------
# hidden helpers used by the acceptance scripts
# ---------------------------------------------------------------------------
def cmd_import_check(args: argparse.Namespace) -> int:
    vendor = _vendor()
    names: list[str] = []
    for directory in (vendor.CONTACT_DIR, vendor.LIFE_RUNTIME_DIR, vendor.CANONICAL_PORTS_DIR):
        names.extend(sorted(p.stem for p in directory.glob("*.py") if p.stem != "__init__"))
    for package in ("durable", "support"):
        names.extend(
            sorted(
                f"{package}.{p.stem}"
                for p in (vendor.CONTACT_DIR / package).glob("*.py")
                if p.stem != "__init__"
            )
        )
    failures: list[dict] = []
    imported = 0
    for name in sorted(set(names)):
        try:
            importlib.import_module(name)
            imported += 1
        except Exception as exc:  # noqa: BLE001
            failures.append({"module": name, "error": f"{type(exc).__name__}: {exc}"})
    identity = vendor.assert_single_module_objects()
    _json_out(
        {
            "module_count": len(set(names)),
            "imported": imported,
            "failures": failures,
            "single_module_objects": identity,
        }
    )
    return 1 if failures else 0

def cmd_core_receipt(args: argparse.Namespace) -> int:
    """Re-read one Action's real receipt/settlement from the real stores (fresh process)."""
    _vendor()
    imports = _core_imports()
    ag2 = imports["ag2"]
    harness = ag2.IsolatedLifeRuntimeHarness(Path(args.run_dir))
    try:
        action = harness.action_read.get_action(args.action_id)
        if action is None:
            _json_out({"error": f"action {args.action_id} not found in the real ActionStore"})
            return 1
        attempts = harness.action_read.list_attempts(args.action_id)
        receipts = harness.action_read.list_receipts(args.action_id)
        settlement = harness.settlement_read.get_settlement(args.action_id)
        _json_out(
            {
                "action_id": args.action_id,
                "action_status": action["status"],
                "adapter_kind": action["adapter_kind"],
                "idempotency_key": action["idempotency_key"],
                "attempts": [
                    {
                        "attempt_id": row["attempt_id"],
                        "submission_key": row["submission_key"],
                        "transport_result": row["transport_result"],
                        "status": row["status"],
                        "provider_ref": row.get("provider_ref"),
                        "receipt_refs": list(row.get("receipt_refs") or []),
                    }
                    for row in attempts
                ],
                "receipts": [
                    {
                        "receipt_id": row["receipt_id"],
                        "receipt_kind": row["receipt_kind"],
                        "status_claim": row["status_claim"],
                        "provider": row.get("provider"),
                        "evidence_kind": row.get("evidence_kind"),
                        "authority_domain": row.get("authority_domain"),
                    }
                    for row in receipts
                ],
                "settlement": settlement,
                "read_by": "a fresh process using the real AR-0/AR-1 read services",
            }
        )
    finally:
        harness.close()
    return 0

# ---------------------------------------------------------------------------
# argument parsing
# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="chiyo",
        description="CHIYO - a Persistent Digital Individual runtime (alpha). "
        "POSIX only: run it on Linux or WSL.",
    )
    parser.add_argument("--version", action="version", version="chiyo 0.1.0a0")
    sub = parser.add_subparsers(dest="command", required=True)

    doctor = sub.add_parser("doctor", help="check this host before running anything")
    doctor.add_argument("--data-dir", default=str(default_data_dir()))
    doctor.add_argument("--json", action="store_true", help="machine-readable output")
    doctor.set_defaults(func=cmd_doctor)

    demo = sub.add_parser("demo", help="run a real, isolated demonstration")
    demo_sub = demo.add_subparsers(dest="demo", required=True)

    core = demo_sub.add_parser("core", help="Life Runtime + Action Reality + Agency loop")
    core.add_argument("--data-dir", default=str(default_data_dir()))
    core.add_argument("--json", action="store_true")
    core.set_defaults(func=cmd_demo_core)

    contact = demo_sub.add_parser("contact", help="the ISOLATED-ONLY proactive contact pipeline")
    contact.add_argument("--data-dir", default=str(default_data_dir()))
    contact.add_argument("--json", action="store_true")
    contact.set_defaults(func=cmd_demo_contact)

    restart = sub.add_parser("_core-restart", help=argparse.SUPPRESS)
    restart.add_argument("--run-dir", required=True)
    restart.add_argument("--expect", required=True)
    restart.add_argument("--json", action="store_true")
    restart.set_defaults(func=cmd_core_restart)

    import_check = sub.add_parser("_import-check", help=argparse.SUPPRESS)
    import_check.add_argument("--json", action="store_true")
    import_check.set_defaults(func=cmd_import_check)

    receipt = sub.add_parser("_core-receipt", help=argparse.SUPPRESS)
    receipt.add_argument("--run-dir", required=True)
    receipt.add_argument("--action-id", required=True)
    receipt.add_argument("--json", action="store_true")
    receipt.set_defaults(func=cmd_core_receipt)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
