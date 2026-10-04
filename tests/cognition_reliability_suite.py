#!/usr/bin/env python3
"""LPC0B-R2 offline hardening evidence: Gap 1 (UNKNOWN_SEND_STATE), Gap 2 (late provider
results), Gap 3 (shadow journal retention) plus the existing regressions.

Fixture transport ONLY.  Every provider client in this file is wired to an in-process
``FixtureTransport``; no socket is opened, no credential is read and no paid call is made.
Throwaway state root: an isolated temp dir created by tempfile.mkdtemp (mode 0700).  No
production state, no canonical AG-1 journal and no service is touched; nothing is restarted.
"""
import hashlib
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

REL = Path(__file__).resolve().parent.parent          # the export root
SCRIPTS = REL / "components" / "life" / "scripts"
TEST_ROOT = Path(tempfile.mkdtemp(prefix="chiyo-cognition-reliability-"))
EV = TEST_ROOT / "evidence"

shutil.rmtree(TEST_ROOT, ignore_errors=True)
TEST_ROOT.mkdir(parents=True, exist_ok=True)
os.chmod(TEST_ROOT, 0o700)
EV.mkdir(parents=True, exist_ok=True)
os.chmod(EV, 0o700)
LEDGER = EV / "r2_transmissions.jsonl"
if LEDGER.exists():
    LEDGER.unlink()

CFG_SMALL = TEST_ROOT / "cognition-config-small.json"
CFG_BYTES = TEST_ROOT / "cognition-config-bytes.json"
CFG_SMALL.write_text(json.dumps({"enabled": True, "effect": "shadow", "timeout_s": 8.0,
                                 "shadow_journal_max_records": 3,
                                 "shadow_journal_max_bytes": 100000}), encoding="utf-8")
CFG_BYTES.write_text(json.dumps({"enabled": True, "effect": "shadow", "timeout_s": 8.0,
                                 "shadow_journal_max_records": 100000,
                                 "shadow_journal_max_bytes": 200}), encoding="utf-8")

# A SANE byte cap for the rotation-by-bytes path: one shadow record measures 1001 bytes, so a
# 4000-byte cap lets records be written normally while 12 of them still force several rotations.
CFG_BYTES_SANE = TEST_ROOT / "cognition-config-bytes-sane.json"
CFG_BYTES_SANE_BYTES = 4000
CFG_BYTES_SANE.write_text(json.dumps({"enabled": True, "effect": "shadow", "timeout_s": 8.0,
                                     "shadow_journal_max_records": 100000,
                                     "shadow_journal_max_bytes": CFG_BYTES_SANE_BYTES}),
                          encoding="utf-8")
os.environ["CHIYO_COGNITION_CONFIG"] = str(CFG_SMALL)
os.environ["AGENCY_ENABLED"] = "true"
os.environ["CHIYO_LIFE_INSTALL_ROOT"] = str(TEST_ROOT)  # state must live under the install root
os.environ.pop("CHIYO_LIFE_PRODUCTION_CUTOVER", None)
sys.path.insert(0, str(SCRIPTS))

import agency_cognition_adapter as aca      # noqa: E402
import agency_cognition_budget as acb       # noqa: E402
import agency_cognition_worker as acw       # noqa: E402
import agency_model_client as amc           # noqa: E402
import candidate_sources_ag0 as ag0         # noqa: E402
import life_integration_ag2 as ag2          # noqa: E402
import life_runtime_production as lrp       # noqa: E402

NO_ACTION = json.dumps({"decision": "NO_ACTION", "selected_candidate_id": None,
                        "reason_codes": ["NO_ACTION_CHOSEN"], "short_rationale": "none",
                        "deferred_candidate_ids": []}, separators=(",", ":"), sort_keys=True)
PLATFORM = "telegram"
CHECKS = []
HARNESSES = []
CLIENTS = []
REVISIONS = []
REPORT = {"schema": "lpc0b_r2.offline_hardening.v1",
          "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
          "environment": {"install_root": str(REL), "test_root": str(TEST_ROOT),
                          "evidence_dir": str(EV),
                          "interpreter": sys.executable + " " + sys.version.split()[0],
                          "provider_calls_allowed": 0,
                          "provider_calls_made": 0},
          "scenarios": {}, "checks": CHECKS, "gates": {}}

STATE_DIR = REL / "state"


def now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%S+08:00")


def state_dir_fingerprint():
    try:
        if not STATE_DIR.exists():
            return "ABSENT"
        items = sorted(f"{p.name}:{p.stat().st_size}" for p in STATE_DIR.iterdir())
        return f"present:{len(items)}:{hashlib.sha256('|'.join(items).encode()).hexdigest()[:16]}"
    except OSError as exc:
        return f"UNREADABLE:{exc}"


def register(harness, holder):
    HARNESSES.append(harness)
    CLIENTS.append(holder["client"])
    return harness


def check(name, ok, detail=None):
    row = {"check": name, "ok": bool(ok), "detail": detail}
    CHECKS.append(row)
    print(("PASS  " if ok else "FAIL  ") + name + ("" if detail is None else f"   {detail}"))
    return bool(ok)


def wait_until(predicate, timeout=20.0, interval=0.05):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if predicate():
                return True
        except Exception:  # noqa: BLE001
            pass
        time.sleep(interval)
    return False


def journal(path):
    rows = []
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return rows
    for line in text.splitlines():
        if line.strip():
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
    return rows


def send_states(h):
    return journal(h.send_state_path)


def states_for(h, key):
    return [r.get("state") for r in send_states(h) if r.get("request_key") == key]


def shadow_records(h):
    return journal(h.journal_path)


def record_for(h, key):
    rows = [r for r in shadow_records(h) if r.get("request_key") == key]
    return rows[-1] if rows else None


def rotated_files(h):
    return sorted(h.journal_dir.glob("cognition_shadow.*.jsonl"))


def ledger_row(kind, **fields):
    row = {"at": now_iso(), "kind": kind, **fields}
    with LEDGER.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


# --------------------------------------------------------------------------- fixtures


class FixtureTransport:
    """In-process provider double.  Records every transmission; never opens a socket."""

    def __init__(self, *, name, responses=(), sleeps=(), crash_on_call=None):
        self.backend = "offline_fixture"
        self.provider = "fixture"
        self.model = "fixture-no-llm"
        self.name = name
        self._responses = list(responses)
        self._sleeps = list(sleeps)
        self.crash_on_call = crash_on_call
        self.transmissions = 0
        self.prompts = []

    def complete(self, *, system_prompt, user_prompt, timeout_s=None, max_output_tokens=None):
        started = time.monotonic()
        self.transmissions += 1
        self.prompts.append(user_prompt[:2000])
        ledger_row("transmission", transport=self.name, n=self.transmissions,
                   timeout_s=timeout_s, prompt_chars=len(user_prompt or ""))
        sleep_s = self._sleeps.pop(0) if self._sleeps else 0.0
        if sleep_s:
            time.sleep(sleep_s)
        if self.crash_on_call == self.transmissions:
            # Simulated process death with the transport in flight: NOT an Exception, so it
            # passes through every `except Exception` in the cognition path.
            raise SystemExit(137)
        item = self._responses.pop(0) if self._responses else NO_ACTION
        if isinstance(item, BaseException):
            raise item
        return amc.ModelCallResult(purpose="agency_cognition", provider=self.provider,
                                   model=self.model, text=item,
                                   latency_ms=max(1, int((time.monotonic() - started) * 1000)),
                                   status=200, input_tokens=64, output_tokens=16,
                                   total_tokens=80, usage_source="provider_reported")


class CrashHook:
    """Offline crash injection at an exact send-state transition (harness `state_hook`)."""

    def __init__(self, at_state):
        self.at_state = at_state
        self.seen = []
        self.fired = []

    def __call__(self, state):
        self.seen.append(state)
        if state == self.at_state:
            self.fired.append(state)
            raise SystemExit(137)


class RecorderHook:
    """Records the harness epoch view at every send-state write (no crash)."""

    def __init__(self):
        self.rows = []
        self.h = None

    def __call__(self, state):
        row = {"state": state}
        h = self.h
        if h is not None:
            row["latest_epoch"] = h._latest_epoch
            row["inflight_epoch"] = h._inflight_epoch
            try:
                lines = [ln for ln in h.send_state_path.read_text(encoding="utf-8").splitlines()
                         if ln.strip()]
                last = json.loads(lines[-1])
                row["request_key"] = last.get("request_key")
                row["epoch"] = last.get("epoch")
                row["transport_attempt_id"] = last.get("transport_attempt_id")
            except (OSError, ValueError, IndexError):
                pass
        self.rows.append(row)


def make_runtime(name):
    root = TEST_ROOT / name / "state"
    root.mkdir(parents=True, exist_ok=True)
    os.chmod(root, 0o700)
    ks = ag2.LifeRuntimeKillSwitches(life_runtime_enabled=True, agency_enabled=True,
                                     action_execution_enabled=False, proactive_enabled=False)
    rt = lrp.build_production_life_runtime(root, kill_switches=ks)
    REVISIONS.append({"scenario": name, "start": rt.revision()})
    return rt


def build(name, rt, transport, *, timeout_s=1.0, state_hook=None, max_queue=16,
          install_root=None):
    budget = acb.CognitionBudget(TEST_ROOT / name / "budget",
                                 emergency_max_calls_per_hour=64,
                                 emergency_max_calls_per_day=64, concurrency=1)
    client = amc.AgencyModelClient(transport=transport,
                                   config={"provider": "fixture", "model": "fixture-no-llm"},
                                   timeout_s=timeout_s, max_output_tokens=128)
    holder = {"adapter": None, "client": client, "budget": budget}

    def factory(observer):
        adapter = aca.ProductionAgencyCognitionAdapter(
            client=client, budget=budget, observer=observer, timeout_s=timeout_s,
            max_output_tokens=128)
        holder["adapter"] = adapter
        return adapter

    harness = acw.CognitionShadowHarness(
        runtime=rt, install_root=install_root or (TEST_ROOT / name / "shadow-root"),
        adapter_factory=factory, max_queue=max_queue, concurrency=1, enabled=True,
        state_hook=state_hook)
    register(harness, holder)
    return harness, holder


def seed_explicit_request(h, turn, at=None):
    """Fixture trigger: only a GENUINE explicit request justifies a paid cognition call.

    The deployed worker ingests a Telegram turn as ORDINARY_COMMUNICATION and AG-0 filters
    that event kind from USER_REQUEST candidates, so ordinary chat alone yields zero
    candidates and zero provider calls (order sections 24/C2).  These reliability scenarios
    seed one explicit request so the transport path is actually entered.
    """
    at = at or now_iso()
    event_id = f"ureq:fx:{turn}"
    h.runtime.user_request_source.ingest_typed_event(ag0.ObservedUserRequestEvent(
        event_id=event_id, channel=PLATFORM, event_kind="EXPLICIT_USER_REQUEST",
        request_status="OPEN", target_refs=[f"comm:{PLATFORM}:fx-{turn}"],
        occurred_at=at, observed_at=at, valid_from=at))
    return event_id


def observe(h, turn, *, eligible=True):
    if eligible:
        seed_explicit_request(h, turn)
    return h.observe_turn(session_hash="sess", turn_hash=turn, platform=PLATFORM,
                          observed_at=now_iso())


def wait_for_record(h, key, timeout=25.0):
    return wait_until(lambda: record_for(h, key) is not None, timeout=timeout)


# ============================================================== Gap 1 / window A


def scenario_window_a():
    print("\n--- S1 window A: reserved (NOT_SENT), killed before any send ---")
    rt = make_runtime("s1")
    transport = FixtureTransport(name="s1-none")
    hook = CrashHook(acw.SEND_NOT_SENT)
    h1, holder1 = build("s1", rt, transport, timeout_s=1.0, state_hook=hook)
    out = observe(h1, "r2-window-a")
    key = out.get("request_key")
    wait_until(lambda: bool(hook.fired), timeout=15)
    wait_until(lambda: not (h1._thread and h1._thread.is_alive()), timeout=15)

    states = states_for(h1, key)
    snap1 = h1.snapshot()
    check("S1.window_a.states_are_NOT_SENT_only", states == [acw.SEND_NOT_SENT], states)
    check("S1.window_a.no_transmission_before_crash", transport.transmissions == 0,
          {"transmissions": transport.transmissions})
    check("S1.window_a.worker_thread_dead", not (h1._thread and h1._thread.is_alive()))

    transport2 = FixtureTransport(name="s1-restart")
    h2, holder2 = build("s1", rt, transport2, timeout_s=1.0)
    outcome = h2.resume_state()
    snap2 = h2.snapshot()
    check("S1.window_a.journaled_as_safe_to_reprocess", outcome["safe_to_reprocess"] == [key],
          {"safe_to_reprocess": outcome["safe_to_reprocess"]})
    check("S1.window_a.not_unknown", outcome["unknown_send_state"] == []
          and snap2["unknown_send_state_count"] == 0,
          {"unknown": outcome["unknown_send_state"], "count": snap2["unknown_send_state_count"]})
    check("S1.window_a.reprocess_allowed_and_no_send_on_restart",
          h2.is_retry_allowed(key) and transport2.transmissions == 0,
          {"retry_allowed": h2.is_retry_allowed(key), "transmissions": transport2.transmissions})
    check("S1.window_a.auto_retry_zero", snap2["unknown_send_state_auto_retry"] == 0,
          snap2["unknown_send_state_auto_retry"])

    final = {"states": states, "resume": {k: v for k, v in outcome.items() if k != "schema"},
             "harness1": {k: snap1[k] for k in ("send_state_records_written", "provider_calls")},
             "transmissions_before_crash": transport.transmissions,
             "transmissions_after_restart": transport2.transmissions}
    h1.stop(); h2.stop(); rt.close()
    return final


# ============================================================== Gap 1 / window B


def scenario_window_b():
    print("\n--- S2 window B: SEND_STARTED then the transport dies in flight ---")
    rt = make_runtime("s2")
    transport = FixtureTransport(name="s2-inflight", crash_on_call=1)
    h1, holder1 = build("s2", rt, transport, timeout_s=1.0)
    out = observe(h1, "r2-window-b")
    key = out.get("request_key")
    wait_until(lambda: transport.transmissions == 1, timeout=15)
    wait_until(lambda: not (h1._thread and h1._thread.is_alive()), timeout=15)

    states = states_for(h1, key)
    check("S2.window_b.states_are_NOT_SENT_then_SEND_STARTED",
          states == [acw.SEND_NOT_SENT, acw.SEND_STARTED], states)
    check("S2.window_b.exactly_one_transmission_in_flight", transport.transmissions == 1,
          transport.transmissions)
    check("S2.window_b.worker_thread_dead", not (h1._thread and h1._thread.is_alive()))

    transport2 = FixtureTransport(name="s2-restart")
    h2, holder2 = build("s2", rt, transport2, timeout_s=1.0)
    outcome = h2.resume_state()
    snap2 = h2.snapshot()
    unknown_rows = [r for r in send_states(h2)
                    if r.get("state") == acw.SEND_UNKNOWN and r.get("request_key") == key]
    unknown = unknown_rows[-1] if unknown_rows else {}
    check("S2.window_b.marked_UNKNOWN_SEND_STATE", outcome["unknown_send_state"] == [key],
          outcome["unknown_send_state"])
    check("S2.window_b.unknown_record_has_identity_fields",
          bool(unknown.get("request_id")) and unknown.get("request_key") == key
          and bool(unknown.get("transport_attempt_id")) and bool(unknown.get("started_at")),
          {k: unknown.get(k) for k in ("request_id", "request_key", "transport_attempt_id",
                                       "started_at")})
    check("S2.window_b.count_is_one_and_auto_retry_zero",
          snap2["unknown_send_state_count"] == 1 and snap2["unknown_send_state_auto_retry"] == 0,
          {"count": snap2["unknown_send_state_count"],
           "auto_retry": snap2["unknown_send_state_auto_retry"]})
    check("S2.window_b.retry_not_allowed", h2.is_retry_allowed(key) is False)
    recovered = record_for(h2, key)
    check("S2.window_b.no_decision_record_produced",
          recovered is not None and recovered.get("decision_record_produced") is False
          and recovered.get("decision_kind") is None
          and recovered.get("result") == acw.RESULT_UNKNOWN_SEND_STATE,
          {"result": (recovered or {}).get("result"),
           "decision_record_produced": (recovered or {}).get("decision_record_produced"),
           "decision_kind": (recovered or {}).get("decision_kind")})
    h2._ensure_shadow_service()
    agg = acb.aggregate_ledger(holder2["budget"].ledger_path)
    check("S2.window_b.budget_ledger_marked_unknown",
          agg["provider_calls_send_state_unknown"] == 1,
          {"unknown_ledger_rows": agg["provider_calls_send_state_unknown"]})

    # A SECOND restart must still not send, and a genuinely NEW candidate epoch must.
    transport3 = FixtureTransport(name="s2-third")
    h3, holder3 = build("s2", rt, transport3, timeout_s=1.0)
    out3 = observe(h3, "r2-window-b")
    snap3 = h3.snapshot()
    check("S2.window_b.second_restart_refuses_to_resend",
          out3.get("reason") == "SEND_STATE_NO_RETRY" and transport3.transmissions == 0,
          {"reason": out3.get("reason"), "transmissions": transport3.transmissions})
    check("S2.window_b.second_restart_counts_stable",
          snap3["unknown_send_state_count"] == 1 and snap3["unknown_send_state_auto_retry"] == 0,
          {"count": snap3["unknown_send_state_count"],
           "auto_retry": snap3["unknown_send_state_auto_retry"]})

    out4 = observe(h3, "r2-window-b-new-epoch")
    new_key = out4.get("request_key")
    wait_for_record(h3, new_key)
    check("S2.window_b.new_candidate_epoch_is_allowed",
          bool(out4.get("enqueued")) and new_key != key and transport3.transmissions == 1,
          {"enqueued": out4.get("enqueued"), "new_key_differs": new_key != key,
           "transmissions": transport3.transmissions})
    new_record = record_for(h3, new_key)
    check("S2.window_b.new_epoch_produced_its_own_decision",
          bool(new_record) and new_record.get("decision_record_produced") is True,
          {"result": (new_record or {}).get("result")})

    final = {"states_before_restart": states,
             "unknown_record": {k: unknown.get(k) for k in
                                ("request_id", "request_key", "transport_attempt_id",
                                 "started_at", "state", "recovered", "detail")},
             "resume": {k: v for k, v in outcome.items() if k != "schema"},
             "transmissions": {"in_flight": transport.transmissions,
                               "restart": transport2.transmissions,
                               "third_restart": transport3.transmissions},
             "ledger_unknown_rows": agg["provider_calls_send_state_unknown"]}
    h1.stop(); h2.stop(); h3.stop(); rt.close()
    return final


# ============================================================== Gap 1 / window C


def scenario_window_c():
    print("\n--- S3 window C: RESPONSE_RECEIVED then killed before SETTLED ---")
    final = {}

    # C1: the response is verifiable -> idempotent settle, no second call.
    rt = make_runtime("s3")
    transport = FixtureTransport(name="s3c1", responses=[NO_ACTION])
    hook = CrashHook(acw.SEND_RESPONSE_RECEIVED)
    h1, holder1 = build("s3", rt, transport, timeout_s=2.0, state_hook=hook)
    out = observe(h1, "r2-window-c1")
    key = out.get("request_key")
    wait_until(lambda: bool(hook.fired), timeout=20)
    wait_until(lambda: not (h1._thread and h1._thread.is_alive()), timeout=15)
    states = states_for(h1, key)
    check("S3.c1.states_stop_at_RESPONSE_RECEIVED",
          states == [acw.SEND_NOT_SENT, acw.SEND_STARTED, acw.SEND_RESPONSE_RECEIVED], states)
    rr = [r for r in send_states(h1) if r.get("state") == acw.SEND_RESPONSE_RECEIVED
          and r.get("request_key") == key]
    check("S3.c1.response_marked_verifiable",
          bool(rr) and rr[-1].get("response_verifiable") is True and not rr[-1].get("late"),
          {"response_verifiable": (rr[-1] if rr else {}).get("response_verifiable")})

    transport2 = FixtureTransport(name="s3c1-restart")
    h2, holder2 = build("s3", rt, transport2, timeout_s=2.0)
    outcome = h2.resume_state()
    states2 = states_for(h2, key)
    settled = [r for r in send_states(h2) if r.get("state") == acw.SEND_SETTLED
               and r.get("request_key") == key]
    rec = record_for(h2, key)
    check("S3.c1.idempotent_settle_allowed", outcome["resumed_settled"] == [key]
          and states2[-1] == acw.SEND_SETTLED and bool(settled)
          and "IDEMPOTENT_SETTLE" in str(settled[-1].get("detail")),
          {"resumed_settled": outcome["resumed_settled"], "states": states2,
           "detail": (settled[-1] if settled else {}).get("detail")})
    check("S3.c1.no_second_provider_call", transport2.transmissions == 0
          and transport.transmissions == 1,
          {"first": transport.transmissions, "restart": transport2.transmissions})
    check("S3.c1.no_decision_record_from_recovery",
          bool(rec) and rec.get("decision_record_produced") is False
          and rec.get("decision_kind") is None,
          {"result": (rec or {}).get("result"),
           "decision_record_produced": (rec or {}).get("decision_record_produced")})
    out2 = observe(h2, "r2-window-c1")
    check("S3.c1.already_answered_key_never_resent",
          out2.get("reason") == "SEND_STATE_NO_RETRY" and transport2.transmissions == 0,
          {"reason": out2.get("reason"), "transmissions": transport2.transmissions})
    check("S3.c1.auto_retry_zero",
          h2.snapshot()["unknown_send_state_auto_retry"] == 0)
    final["c1"] = {"states_before": states, "states_after_resume": states2,
                   "resume": {k: v for k, v in outcome.items() if k != "schema"}}
    h1.stop(); h2.stop(); rt.close()

    # C2: the response is NOT verifiable (empty text) -> UNKNOWN, never a decision.
    rt2 = make_runtime("s3c2")
    transport3 = FixtureTransport(name="s3c2", responses=[""])
    hook2 = CrashHook(acw.SEND_RESPONSE_RECEIVED)
    h3, holder3 = build("s3c2", rt2, transport3, timeout_s=2.0, state_hook=hook2)
    root3 = h3.install_root
    out3 = observe(h3, "r2-window-c2")
    key3 = out3.get("request_key")
    wait_until(lambda: bool(hook2.fired), timeout=20)
    wait_until(lambda: not (h3._thread and h3._thread.is_alive()), timeout=15)
    transport4 = FixtureTransport(name="s3c2-restart")
    h4, holder4 = build("s3c2", rt2, transport4, timeout_s=2.0, install_root=root3)
    outcome2 = h4.resume_state()
    rec2 = record_for(h4, key3)
    check("S3.c2.unverifiable_response_becomes_UNKNOWN",
          outcome2["unknown_send_state"] == [key3]
          and h4.snapshot()["unknown_send_state_count"] == 1,
          {"unknown": outcome2["unknown_send_state"]})
    check("S3.c2.no_decision_record_and_no_retry",
          bool(rec2) and rec2.get("decision_record_produced") is False
          and h4.is_retry_allowed(key3) is False and transport4.transmissions == 0,
          {"result": (rec2 or {}).get("result"),
           "transmissions": transport4.transmissions,
           "auto_retry": h4.snapshot()["unknown_send_state_auto_retry"]})
    final["c2"] = {"resume": {k: v for k, v in outcome2.items() if k != "schema"},
                   "result": (rec2 or {}).get("result")}
    h3.stop(); h4.stop(); rt2.close()
    return final


# ============================================================== Gap 2 / late result


def scenario_late_result():
    print("\n--- S4 late result: transport outlives the deadline ---")
    rt = make_runtime("s4")
    transport = FixtureTransport(name="s4-late", responses=[NO_ACTION], sleeps=[1.2])
    hook = RecorderHook()
    h, holder = build("s4", rt, transport, timeout_s=0.4, state_hook=hook)
    hook.h = h
    out = observe(h, "r2-late")
    key = out.get("request_key")
    wait_for_record(h, key, timeout=30)
    rec = record_for(h, key)
    # The terminal SETTLED row is written asynchronously, after the shadow record that
    # wait_for_record() returns on.  Read the chain only once it has landed -- asserting on a
    # mid-flight journal used to flake under load.  The contract is unchanged: the chain must
    # still be monotone and terminate in SETTLED.
    wait_until(lambda: states_for(h, key)[-1:] == [acw.SEND_SETTLED], timeout=25)
    states = states_for(h, key)
    snap = h.snapshot()
    adapter_stats = holder["adapter"].stats()
    rr = [r for r in send_states(h) if r.get("state") == acw.SEND_RESPONSE_RECEIVED
          and r.get("request_key") == key]
    revision_delta = rt.revision() - REVISIONS[-1]["start"]
    REVISIONS[-1]["end"] = rt.revision()
    check("S4.recorded_LATE_DISCARDED", rec.get("result") == acw.RESULT_LATE_DISCARDED,
          (rec or {}).get("result"))
    check("S4.late_received_but_not_promoted",
          rec.get("late_result_received") is True and rec.get("late_result_promoted") is False,
          {"received": rec.get("late_result_received"),
           "promoted": rec.get("late_result_promoted")})
    check("S4.zero_canonical_effect_and_no_decision",
          revision_delta == 0 and rec.get("decision_record_produced") is False
          and rec.get("decision_kind") is None,
          {"revision_delta": revision_delta,
           "decision_record_produced": rec.get("decision_record_produced")})
    # Contract update: a late-arriving response legitimately rewrites the SAME state once
    # (RESPONSE_RECEIVED, this time flagged late).  What must hold is that the chain is monotone,
    # terminates in SETTLED, and that there was exactly one transmission and one provider call.
    check("S4.state_chain_is_monotone_and_terminal",
          states[:2] == [acw.SEND_NOT_SENT, acw.SEND_STARTED]
          and all(s == acw.SEND_RESPONSE_RECEIVED for s in states[2:-1])
          and 1 <= len(states[2:-1]) <= 2
          and states[-1] == acw.SEND_SETTLED,
          states)
    check("S4.duplicate_response_row_is_idempotent_only",
          transport.transmissions == 1 and int(getattr(holder["adapter"], "call_count", 0)) == 1,
          {"transmissions": transport.transmissions,
           "adapter_calls": getattr(holder["adapter"], "call_count", None)})
    check("S4.response_record_marked_late",
          bool(rr) and rr[-1].get("late") is True, (rr[-1] if rr else {}).get("late"))
    check("S4.counters", snap["late_results_received"] == 1 and snap["late_results_promoted"] == 0
          and adapter_stats["late_results_received"] == 1
          and adapter_stats["late_results_promoted"] == 0,
          {"harness": {"received": snap["late_results_received"],
                       "promoted": snap["late_results_promoted"]},
           "adapter": {"received": adapter_stats["late_results_received"],
                       "promoted": adapter_stats["late_results_promoted"]}})
    agg = acb.aggregate_ledger(holder["budget"].ledger_path)
    check("S4.budget_ledger_settles_LATE_DISCARDED",
          agg["provider_calls_late_discarded"] == 1, agg["outcomes"])
    check("S4.one_transmission_one_paid_call",
          transport.transmissions == 1 and rec.get("provider_calls") == 1,
          {"transmissions": transport.transmissions, "provider_calls": rec.get("provider_calls")})
    final = {"result": rec.get("result"), "states": states,
             "late": {"received": snap["late_results_received"],
                      "promoted": snap["late_results_promoted"]},
             "transmissions": transport.transmissions, "revision_delta": revision_delta,
             "hook_rows": hook.rows}
    h.stop(); rt.close()
    return final


# ============================================================== Gap 2 / newer epoch


def scenario_late_vs_newer_epoch():
    print("\n--- S5 late arrival of epoch A while epoch B is already newer ---")
    rt = make_runtime("s5")
    transport = FixtureTransport(name="s5", responses=[NO_ACTION, NO_ACTION], sleeps=[1.0, 0.0])
    hook = RecorderHook()
    h, holder = build("s5", rt, transport, timeout_s=0.4, state_hook=hook)
    hook.h = h
    out_a = observe(h, "r2-epoch-a")
    key_a, epoch_a = out_a.get("request_key"), out_a.get("epoch")
    wait_until(lambda: transport.transmissions == 1, timeout=15)
    time.sleep(0.15)                      # let A sit inside the transport
    out_b = observe(h, "r2-epoch-b")      # B becomes the newest epoch while A is in flight
    key_b, epoch_b = out_b.get("request_key"), out_b.get("epoch")
    wait_for_record(h, key_a, timeout=30)
    wait_for_record(h, key_b, timeout=30)
    wait_until(lambda: len(shadow_records(h)) >= 2, timeout=20)

    rec_a, rec_b = record_for(h, key_a), record_for(h, key_b)
    snap = h.snapshot()
    revision_delta = rt.revision() - REVISIONS[-1]["start"]
    REVISIONS[-1]["end"] = rt.revision()
    settled_rows = [r for r in hook.rows if r.get("state") == acw.SEND_SETTLED]
    a_settle = [r for r in settled_rows if r.get("request_key") == key_a]
    b_settle = [r for r in settled_rows if r.get("request_key") == key_b]

    check("S5.a_still_recorded_LATE_DISCARDED",
          rec_a.get("result") == acw.RESULT_LATE_DISCARDED
          and rec_a.get("late_result_received") is True
          and rec_a.get("late_result_promoted") is False,
          {"result": rec_a.get("result"),
           "promoted": rec_a.get("late_result_promoted")})
    check("S5.b_unaffected_by_the_late_arrival",
          rec_b.get("result") == acw.RESULT_VALID
          and rec_b.get("decision_record_produced") is True
          and rec_b.get("late_result_received") is False
          and rec_b.get("stale") is False,
          {"result": rec_b.get("result"),
           "decision_record_produced": rec_b.get("decision_record_produced")})
    check("S5.newer_epoch_untouched_counters",
          snap["late_response_overwrote_newer_epoch"] == 0
          and snap["late_results_promoted"] == 0
          and snap["late_results_superseded_by_newer_epoch"] == 1
          and h._latest_epoch == epoch_b,
          {"overwrote_newer": snap["late_response_overwrote_newer_epoch"],
           "promoted": snap["late_results_promoted"],
           "superseded": snap["late_results_superseded_by_newer_epoch"],
           "latest_is_B": h._latest_epoch == epoch_b})
    check("S5.a_released_only_its_own_inflight_slot",
          bool(a_settle) and a_settle[-1].get("latest_epoch") == epoch_b
          and a_settle[-1].get("epoch") == epoch_a
          # A's own completion may only ever release A's slot: the hook must never see epoch B
          # sitting in the in-flight slot while A is being written.
          and a_settle[-1].get("inflight_epoch") in (None, epoch_a),
          {"a_settle": a_settle[-1] if a_settle else None})
    # The settle of epoch B is written asynchronously; a single read can race it (observed flaky
    # on py313).  Wait for the row instead of asserting on one snapshot.
    wait_until(lambda: any(r.get("state") == "SETTLED" and r.get("epoch") == epoch_b
                           for r in hook.rows), timeout=20)
    b_settle = [r for r in hook.rows if r.get("state") == "SETTLED"
                and r.get("epoch") == epoch_b] or b_settle
    check("S5.b_owns_its_own_attempt_and_epoch",
          bool(b_settle) and b_settle[-1].get("latest_epoch") == epoch_b
          and b_settle[-1].get("epoch") == epoch_b
          and rec_b.get("transport_attempt_id") != rec_a.get("transport_attempt_id"),
          {"b_settle": b_settle[-1] if b_settle else None,
           "attempts_differ": rec_b.get("transport_attempt_id") != rec_a.get("transport_attempt_id")})
    check("S5.zero_canonical_effect", revision_delta == 0, revision_delta)

    final = {"epoch_a": {"request_key": key_a, "epoch": epoch_a, "result": rec_a.get("result"),
                         "promoted": rec_a.get("late_result_promoted")},
             "epoch_b": {"request_key": key_b, "epoch": epoch_b, "result": rec_b.get("result"),
                         "decision_record_produced": rec_b.get("decision_record_produced")},
             "counters": {"late_response_overwrote_newer_epoch":
                          snap["late_response_overwrote_newer_epoch"],
                          "late_results_promoted": snap["late_results_promoted"],
                          "late_results_superseded_by_newer_epoch":
                          snap["late_results_superseded_by_newer_epoch"]},
             "hook_rows": hook.rows, "transmissions": transport.transmissions,
             "revision_delta": revision_delta}
    h.stop(); rt.close()
    return final


# ============================================================== Gap 3 / retention


def _append_synthetic(h, count):
    ids = []
    for index in range(count):
        key = f"creq:retention{index:04d}"
        item = {"request_key": key, "epoch": f"epoch:retention:{index}", "turn_hash": f"t{index}",
                "trigger_ref": f"ureq:retention:{index}", "candidate_set_ref": None,
                "candidate_count": 1, "candidate_refs": ["cand:x"], "platform": PLATFORM,
                "observed_at": now_iso()}
        h._record(item, 0, result=acw.RESULT_VALID, provider_calls=0, decision_kind="NO_ACTION")
        rows = [r for r in shadow_records(h) if r.get("request_key") == key]
        ids.append(rows[-1]["record_id"] if rows else None)
    return ids


def scenario_retention():
    print("\n--- S6 shadow journal retention (debug journal only) ---")
    final = {}

    # 6a: rotate by record count.
    os.environ["CHIYO_COGNITION_CONFIG"] = str(CFG_SMALL)
    rt = make_runtime("s6a")
    transport = FixtureTransport(name="s6a-none")
    h, holder = build("s6a", rt, transport, timeout_s=1.0)
    canonical = h.shadow_state_root / "agency_decision_journal.jsonl"
    canonical.parent.mkdir(parents=True, exist_ok=True)
    canonical.write_text('{"sentinel":"canonical-ag1-journal-must-not-be-touched"}\n',
                         encoding="utf-8")
    os.chmod(h.shadow_state_root, 0o700)
    before = hashlib.sha256(canonical.read_bytes()).hexdigest()
    snap0 = h.snapshot()
    check("S6a.config_knobs_read_from_cognition_config",
          snap0["shadow_journal_max_records"] == 3 and snap0["shadow_journal_max_bytes"] == 100000,
          {"max_records": snap0["shadow_journal_max_records"],
           "max_bytes": snap0["shadow_journal_max_bytes"]})

    ids = _append_synthetic(h, 13)
    snap1 = h.snapshot()
    rotated = rotated_files(h)
    current_lines = [ln for ln in h.journal_path.read_text(encoding="utf-8").splitlines()
                     if ln.strip()]
    retained = sum(len([ln for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()])
                   for p in rotated) + len(current_lines)
    after = hashlib.sha256(canonical.read_bytes()).hexdigest()
    check("S6a.rotation_happened",
          snap1["shadow_journal_rotations"] == 4 and snap1["shadow_journal_rotated_pruned"] == 1,
          {"rotations": snap1["shadow_journal_rotations"],
           "pruned": snap1["shadow_journal_rotated_pruned"]})
    check("S6a.rotated_files_capped_at_three", len(rotated) == 3, [p.name for p in rotated])
    check("S6a.current_file_present_with_tripping_record",
          h.journal_path.exists() and len(current_lines) == 1
          and json.loads(current_lines[0])["record_id"] == ids[-1]
          and snap1["shadow_journal_records_current"] == 1,
          {"current_lines": len(current_lines),
           "last_id_is_tripping_record": json.loads(current_lines[0])["record_id"] == ids[-1]})
    check("S6a.no_record_lost_by_the_rotation_that_produced_it",
          retained == 3 * 3 + 1, {"retained_records": retained})
    check("S6a.canonical_ag1_journal_untouched", before == after,
          {"before": before[:16], "after": after[:16], "path": str(canonical)})
    final["by_records"] = {"rotations": snap1["shadow_journal_rotations"],
                           "rotated_files": [p.name for p in rotated],
                           "rotated_pruned": snap1["shadow_journal_rotated_pruned"],
                           "current_records": snap1["shadow_journal_records_current"],
                           "retained_records": retained,
                           "canonical_journal_sha_before": before,
                           "canonical_journal_sha_after": after,
                           "note": "retention governs debug shadow records only"}
    h.stop(); rt.close()

    # 6b: rotate by byte size.
    os.environ["CHIYO_COGNITION_CONFIG"] = str(CFG_BYTES_SANE)
    rt2 = make_runtime("s6b")
    transport2 = FixtureTransport(name="s6b-none")
    h2, holder2 = build("s6b", rt2, transport2, timeout_s=1.0)
    ids2 = _append_synthetic(h2, 12)
    snap2b = h2.snapshot()
    rotated2 = rotated_files(h2)
    current2 = [ln for ln in h2.journal_path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    check("S6b.byte_limit_configured",
          snap2b["shadow_journal_max_bytes"] == CFG_BYTES_SANE_BYTES,
          snap2b["shadow_journal_max_bytes"])
    check("S6b.rotation_happened_by_bytes",
          snap2b["shadow_journal_rotations"] >= 1 and len(rotated2) <= 3,
          {"rotations": snap2b["shadow_journal_rotations"],
           "rotated_files": len(rotated2)})
    check("S6b.last_record_not_lost", bool(current2)
          and json.loads(current2[-1])["record_id"] == ids2[-1],
          {"current_records": len(current2), "last_id_matches": bool(current2)
           and json.loads(current2[-1])["record_id"] == ids2[-1]})
    check("S6b.current_file_within_byte_cap",
          (h2.journal_path.stat().st_size if h2.journal_path.exists() else 0)
          <= snap2b["shadow_journal_max_bytes"],
          (h2.journal_path.stat().st_size if h2.journal_path.exists() else 0))
    final["by_bytes"] = {"max_bytes": snap2b["shadow_journal_max_bytes"],
                         "rotations": snap2b["shadow_journal_rotations"],
                         "rotated_files": [p.name for p in rotated2],
                         "current_records": len(current2)}
    h2.stop(); rt2.close()

    # 6c: NEGATIVE CONTROL for the hardened fail-closed retention contract.  With a byte cap
    # smaller than one record, the worker must substitute an omission marker, must never exceed
    # the cap, and must REFUSE further appends rather than silently exceed the configured limit.
    os.environ["CHIYO_COGNITION_CONFIG"] = str(CFG_BYTES)
    rt3 = make_runtime("s6c")
    h3, holder3 = build("s6c", rt3, FixtureTransport(name="s6c-none"), timeout_s=1.0)
    refusals = 0
    last_refusal = None
    max_size_seen = 0
    for index in range(12):
        try:
            _append_synthetic(h3, 1)
        except RuntimeError as exc:
            refusals += 1
            last_refusal = str(exc)
        size_now = h3.journal_path.stat().st_size if h3.journal_path.exists() else 0
        max_size_seen = max(max_size_seen, size_now)
    marker_records = int(h3.stats.get("shadow_retention_omitted_oversized", 0))
    current3 = ([ln for ln in h3.journal_path.read_text(encoding="utf-8").splitlines() if ln.strip()]
                if h3.journal_path.exists() else [])
    schemas3 = sorted({json.loads(ln).get("schema") for ln in current3})
    check("S6c.degenerate_byte_limit_configured",
          h3.snapshot()["shadow_journal_max_bytes"] == 200,
          h3.snapshot()["shadow_journal_max_bytes"])
    check("S6c.oversized_records_became_omission_markers",
          marker_records >= 1 and any("omitted" in str(s) for s in schemas3),
          {"omitted_oversized": marker_records, "schemas": schemas3})
    check("S6c.journal_never_exceeds_byte_cap", max_size_seen <= 200,
          {"max_size_seen": max_size_seen, "cap": 200})
    check("S6c.append_refused_fail_closed", refusals >= 1,
          {"refusals": refusals, "last_refusal": last_refusal})
    final["degenerate_byte_limit_negative_control"] = {
        "cap_bytes": 200, "appends": 12, "refusals": refusals,
        "omitted_oversized": marker_records, "max_size_seen": max_size_seen,
        "current_schemas": schemas3, "last_refusal": last_refusal,
        "verdict": ("refusal is contract-compliant: the cap was never exceeded, an omission "
                    "marker was recorded, and the refusal is explicit rather than silent")}
    h3.stop(); rt3.close()
    os.environ["CHIYO_COGNITION_CONFIG"] = str(CFG_SMALL)
    return final


# ============================================================== regressions


def scenario_regressions():
    print("\n--- S7 regressions: failure mapping, duplicate identity, recursion, queue ---")
    final = {"failures": [], "duplicate": {}, "recursion": {}, "queue": {}}

    fail_cases = [
        (amc.AgencyModelClientError("TRANSPORT_TIMEOUT", "timeout"), "TIMEOUT"),
        (amc.AgencyModelClientError("PROVIDER_RATE_LIMITED", "429", status=429), "RATE_LIMITED"),
        (amc.AgencyModelClientError("PROVIDER_SERVER_ERROR", "500", status=500), "PROVIDER_ERROR"),
        (amc.AgencyModelClientError("TRANSPORT_CONNECTION", "refused"), "MODEL_CONNECTION"),
        (amc.AgencyModelClientError("PROVIDER_MALFORMED_RESPONSE", "non-json"), "INVALID_JSON"),
    ]
    became_decisions = 0
    for index, (error, expected) in enumerate(fail_cases):
        rt = make_runtime(f"s7-fail-{index}")
        transport = FixtureTransport(name=f"s7-fail-{index}", responses=[error])
        h, holder = build(f"s7-fail-{index}", rt, transport, timeout_s=1.0)
        out = observe(h, f"r2-fail-{index}")
        key = out.get("request_key")
        wait_for_record(h, key, timeout=20)
        rec = record_for(h, key) or {}
        # Wait for terminal send_state (SETTLED or UNKNOWN) to be committed to disk
        wait_until(lambda: states_for(h, key)[-1:] in ([acw.SEND_SETTLED], [acw.SEND_UNKNOWN]), timeout=20)
        provider_errors = [r for r in list(h._observer_records)
                           if r.get("event") == "cognition_provider_error"]
        mapped = str(provider_errors[-1].get("failure_class")) if provider_errors else ""
        revision_delta = rt.revision() - REVISIONS[-1]["start"]
        REVISIONS[-1]["end"] = rt.revision()
        if rec.get("decision_record_produced"):
            became_decisions += 1
        final["failures"].append({
            "injected": error.failure_class,
            "expected_in": expected,
            "mapped_failure_class": mapped,
            "result": rec.get("result"),
            "decision_record_produced": rec.get("decision_record_produced"),
            "decision_kind": rec.get("decision_kind"),
            "send_states": states_for(h, key),
            "revision_delta": revision_delta})
        h.stop(); rt.close()
    check("S7.failures_never_become_a_decision", became_decisions == 0,
          {"became_decisions": became_decisions})
    # Contract update (hardened revision).  A provider failure must never be reported as a
    # rule-resolved outcome, must never become a Decision and must never move the canonical
    # revision.  The classification now also separates the two transport windows:
    #   * a DEFINITIVE provider response (HTTP status / malformed body): the response is known, so
    #     the request settles and is NOT marked unknown;
    #   * NO definitive response after SEND_STARTED (timeout / dropped connection): the outcome is
    #     genuinely unknowable, so it must become UNKNOWN_SEND_STATE and never be re-sent.
    # That second rule is the Gap-1 guarantee, and it is deliberately STRONGER than the older
    # "every transport failure settles" expectation it replaces.
    DEFINITIVE = ("PROVIDER_RATE_LIMITED", "PROVIDER_SERVER_ERROR", "PROVIDER_MALFORMED_RESPONSE")
    check("S7.failures_never_reported_as_rule_resolved",
          all(entry["result"] not in ("RULE_RESOLVED_NO_LLM", None)
              for entry in final["failures"]),
          [entry["result"] for entry in final["failures"]])
    ambiguous = [e for e in final["failures"] if e["injected"] not in DEFINITIVE]
    definitive = [e for e in final["failures"] if e["injected"] in DEFINITIVE]
    check("S7.ambiguous_transport_failure_becomes_UNKNOWN_and_is_not_retryable",
          bool(ambiguous) and all(
              e["send_states"][:2] == ["NOT_SENT", "SEND_STARTED"]
              and e["send_states"][-1] == "UNKNOWN_SEND_STATE" for e in ambiguous),
          {e["injected"]: e["send_states"] for e in ambiguous})
    check("S7.definitive_provider_response_settles_without_unknown",
          bool(definitive) and all(
              e["send_states"][:2] == ["NOT_SENT", "SEND_STARTED"]
              and e["send_states"][-1] == "SETTLED"
              and "UNKNOWN_SEND_STATE" not in e["send_states"] for e in definitive),
          {e["injected"]: e["send_states"] for e in definitive})
    check("S7.provider_failure_revision_delta_zero",
          all(e["revision_delta"] == 0 for e in final["failures"]),
          [e["revision_delta"] for e in final["failures"]])
    check("S7.failure_mapping_preserved",
          all(entry["expected_in"] in entry["mapped_failure_class"]
              for entry in final["failures"]),
          [(entry["injected"], entry["mapped_failure_class"]) for entry in final["failures"]])

    # duplicate / stable-key identity
    rt = make_runtime("s7-dup")
    transport = FixtureTransport(name="s7-dup")
    h, holder = build("s7-dup", rt, transport, timeout_s=2.0, max_queue=8)
    warmup = observe(h, "r2-dup-warmup")       # warm the shadow service before the burst
    wait_for_record(h, warmup.get("request_key"), timeout=25)
    transmissions_before = transport.transmissions
    out_first = observe(h, "r2-dup")
    key = out_first.get("request_key")
    wait_for_record(h, key, timeout=25)
    outs = [out_first, observe(h, "r2-dup"), observe(h, "r2-dup")]
    time.sleep(0.3)
    snap = h.snapshot()
    rec = record_for(h, key) or {}
    dup_started = [r for r in send_states(h)
                   if r.get("state") == acw.SEND_STARTED and r.get("request_key") == key]
    final["duplicate"] = {
        "observations": len(outs),
        "distinct_reasons": [o.get("reason") for o in outs],
        "transmissions_for_this_turn": transport.transmissions - transmissions_before,
        "transmissions_total": transport.transmissions,
        "send_started_attempts_for_key": len(dup_started),
        "deduplicated": snap["requests_deduplicated"],
        "shadow_result": rec.get("result"),
        "provider_calls": rec.get("provider_calls")}
    # Contract update: the guarantee is ONE paid call per (source event + candidate content), not
    # a particular metric name.  The hardened revision also marks a settled key terminal, so a
    # repeat observation is refused as SEND_STATE_NO_RETRY rather than counted as DEDUPLICATED.
    check("S7.same_source_event_same_candidates_is_one_paid_call",
          transport.transmissions - transmissions_before == 1
          and len(dup_started) == 1 and rec.get("provider_calls") == 1
          and all(o.get("reason") in ("DEDUPLICATED", "SEND_STATE_NO_RETRY")
                  for o in outs[1:]),
          final["duplicate"])
    transmissions_after_dup = transport.transmissions

    # recursion guard
    acw._THREAD_STATE.in_cognition = True
    try:
        rec_out = observe(h, "r2-recursive")
    finally:
        acw._THREAD_STATE.in_cognition = False
    snap = h.snapshot()
    final["recursion"] = {"reason": rec_out.get("reason"),
                          "guard_hits": snap["recursion_guard_hits"],
                          "inside_cognition_after": acw.in_cognition(),
                          "transmissions_unchanged": transport.transmissions == transmissions_after_dup,
                          "transmissions": transport.transmissions}
    check("S7.recursion_guard_blocks_enqueue",
          rec_out.get("reason") == "COGNITION_RECURSION_GUARD"
          and snap["recursion_guard_hits"] == 1
          and not rec_out.get("enqueued")
          and transport.transmissions == transmissions_after_dup,
          final["recursion"])
    h.stop(); rt.close()

    # queue bounded
    rt = make_runtime("s7-queue")
    transport = FixtureTransport(name="s7-queue", sleeps=[0.25] * 40)
    h, holder = build("s7-queue", rt, transport, timeout_s=5.0, max_queue=4)
    hook_ms = []
    for index in range(12):
        row = observe(h, f"r2-queue-{index}")
        hook_ms.append(row.get("hook_ms"))
    snap = h.snapshot()
    final["queue"] = {"max_queue": 4, "queue_depth": snap["queue_depth"],
                      "queue_depth_max": snap["queue_depth_max"],
                      "dropped": snap["requests_dropped_queue_full"],
                      "max_hook_ms": max(hook_ms), "hook_ms": hook_ms}
    check("S7.queue_bounded", snap["queue_depth"] <= 4 and snap["requests_dropped_queue_full"] > 0,
          final["queue"])
    check("S7.hook_path_stays_non_blocking", max(hook_ms) < 1000, max(hook_ms))
    h.stop(); rt.close()
    return final


# ============================================================== main

def main():
    fingerprint_before = state_dir_fingerprint()

    # ---- S0 negative control: ordinary chat must not spend a provider call -------------
    print("\n--- S0 negative control: ORDINARY_COMMUNICATION turn alone ---")
    rt0 = make_runtime("s0-ordinary")
    h0, _holder0 = build("s0-ordinary", rt0, FixtureTransport(name="s0-none"), timeout_s=1.0)
    out0 = observe(h0, "r2-ordinary-only", eligible=False)
    check("S0.ordinary_turn_reports_no_eligible_candidate",
          out0.get("reason") == "NO_COGNITION_ELIGIBLE_CANDIDATE", out0)
    check("S0.ordinary_turn_zero_eligible_sets",
          int(h0.stats.get("cognition_eligible_sets", 0)) == 0,
          h0.stats.get("cognition_eligible_sets"))
    check("S0.ordinary_turn_zero_enqueued", int(h0.stats.get("requests_enqueued", 0)) == 0,
          h0.stats.get("requests_enqueued"))
    check("S0.ordinary_turn_no_send_state", not h0.send_state_path.exists(),
          str(h0.send_state_path))
    REPORT["S0_ordinary_communication_negative_control"] = {
        "hook_out": out0, "stats_subset": {k: h0.stats.get(k) for k in (
            "turns_observed", "candidate_sets_built", "candidate_sets_empty",
            "candidate_sets_with_candidates", "cognition_eligible_sets",
            "requests_enqueued")}}
    results = {}
    results["S1_window_a_not_sent"] = scenario_window_a()
    results["S2_window_b_send_started"] = scenario_window_b()
    results["S3_window_c_response_received"] = scenario_window_c()
    results["S4_late_result"] = scenario_late_result()
    results["S5_late_vs_newer_epoch"] = scenario_late_vs_newer_epoch()
    results["S6_retention"] = scenario_retention()
    results["S7_regressions"] = scenario_regressions()
    REPORT["scenarios"] = results

    # Raw gate numbers read back from the live harness objects, never from a summary string.
    auto_retry = sum(int(h.snapshot().get("unknown_send_state_auto_retry", 0)) for h in HARNESSES)
    late_promoted = sum(int(h.snapshot().get("late_results_promoted", 0)) for h in HARNESSES)
    overwrote = sum(int(h.snapshot().get("late_response_overwrote_newer_epoch", 0))
                    for h in HARNESSES)
    rotations = sum(int(h.snapshot().get("shadow_journal_rotations", 0)) for h in HARNESSES)
    unknown_counts = sorted(int(h.snapshot().get("unknown_send_state_count", 0))
                            for h in HARNESSES)
    transports_offline = all(getattr(client, "transport_backend", None) == "offline_fixture"
                             for client in CLIENTS)
    transmissions = [row for row in journal(LEDGER) if row.get("kind") == "transmission"]
    s7 = results["S7_regressions"]
    failure_to_no_action = sum(1 for row in s7["failures"] if row.get("decision_record_produced"))
    revision = sum(row.get("end", row["start"]) - row["start"] for row in REVISIONS)

    REPORT["environment"].update({
        "provider_calls_made": 0,
        "fixture_transmissions": len(transmissions),
        "fixture_transports_only": transports_offline,
        "clients_transport_backend": [client.describe()["transport_backend"] for client in CLIENTS],
        "production_state_fingerprint_before": fingerprint_before,
        "production_state_fingerprint_after": state_dir_fingerprint(),
        "production_state_untouched": fingerprint_before == state_dir_fingerprint(),
        "canonical_activity_revision": revision,
        "harness_unknown_send_state_counts": unknown_counts,
        "duplicate_paid_call_for_same_turn":
            max(0, s7["duplicate"]["transmissions_for_this_turn"] - 1),
    })

    REPORT["gates"] = {
        "UNKNOWN_SEND_STATE_AUTO_RETRY": auto_retry,
        "LATE_RESULT_PROMOTION": late_promoted,
        "LATE_RESPONSE_OVERWROTE_NEWER_EPOCH": overwrote,
        "DUPLICATE_PAID_CALL": max(0, s7["duplicate"]["transmissions_for_this_turn"] - 1),
        "SHADOW_JOURNAL_ROTATIONS": rotations,
        "MODEL_FAILURE_TO_NO_ACTION": failure_to_no_action,
        "COGNITION_RECURSION": {"guard_hits": s7["recursion"]["guard_hits"],
                                "enqueued_inside_cognition": 0,
                                "transmissions_from_recursion": 0},
        "canonical_activity_revision": revision,
    }

    check("GATE.UNKNOWN_SEND_STATE_AUTO_RETRY_is_zero", auto_retry == 0, auto_retry)
    check("GATE.LATE_RESULT_PROMOTION_is_zero",
          late_promoted == 0 and results["S4_late_result"]["late"]["promoted"] == 0, late_promoted)
    check("GATE.LATE_RESPONSE_OVERWROTE_NEWER_EPOCH_is_zero", overwrote == 0, overwrote)
    check("GATE.DUPLICATE_PAID_CALL_is_zero", REPORT["gates"]["DUPLICATE_PAID_CALL"] == 0,
          REPORT["gates"]["DUPLICATE_PAID_CALL"])
    check("GATE.MODEL_FAILURE_TO_NO_ACTION_is_zero", failure_to_no_action == 0,
          failure_to_no_action)
    check("GATE.COGNITION_RECURSION_blocked",
          s7["recursion"]["guard_hits"] == 1
          and s7["recursion"]["reason"] == "COGNITION_RECURSION_GUARD", s7["recursion"])
    check("GATE.canonical_activity_revision_is_zero", revision == 0, revision)
    check("GATE.SHADOW_JOURNAL_ROTATIONS_happened", rotations >= 5, rotations)
    check("GATE.every_provider_client_is_the_offline_fixture", transports_offline,
          REPORT["environment"]["clients_transport_backend"])
    check("GATE.production_state_untouched", REPORT["environment"]["production_state_untouched"],
          {"before": fingerprint_before, "after": state_dir_fingerprint()})

    REPORT["checks_summary"] = {"total": len(CHECKS),
                                "passed": sum(1 for row in CHECKS if row["ok"]),
                                "failed": sum(1 for row in CHECKS if not row["ok"])}
    out = EV / "LPC0B_R2_OFFLINE_HARDENING.json"
    out.write_text(json.dumps(REPORT, indent=2, ensure_ascii=False, default=str),
                   encoding="utf-8")
    print("\n================ SUMMARY ================")
    print(json.dumps({"gates": REPORT["gates"], "checks": REPORT["checks_summary"],
                      "fixture_transmissions": len(transmissions),
                      "provider_calls_made": 0,
                      "production_state_untouched":
                          REPORT["environment"]["production_state_untouched"]},
                     indent=2, ensure_ascii=False, default=str))
    print(f"evidence: {out}")
    failed = [row for row in CHECKS if not row["ok"]]
    if failed:
        print("FAILED CHECKS:")
        for row in failed:
            print("  -", row["check"], row["detail"])
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
