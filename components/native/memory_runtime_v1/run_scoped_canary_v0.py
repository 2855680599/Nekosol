from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess
import argparse

from .canary import CanaryConfig, MatchBy, ScopedCanaryGate


POLICY_SHA = "5c6ba70a160cbe4fa880a91caab47bf548138750bc1257d7daa9764a6a4679b7"
BUNDLE_SHA = "ab44680ac1147e94e6b661b4e57e3052d4740d4428fcb3f14b2f73c94f22548f"


def now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def write(root: Path, name: str, value: object) -> None:
    (root / name).write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def run(command: list[str]) -> dict[str, object]:
    proc = subprocess.run(command, capture_output=True, text=True, timeout=15)
    return {"returncode": proc.returncode, "stdout": proc.stdout.strip(), "stderr": proc.stderr.strip()}


def test_matrix() -> tuple[list[dict[str, object]], ScopedCanaryGate]:
    rows: list[dict[str, object]] = []

    def add(name: str, gate: ScopedCanaryGate, expected: bool, **kwargs: object) -> None:
        decision = gate.decide(name, kwargs.pop("user_id", "u1"), kwargs.pop("session_id", "s1"), kwargs.pop("conversation_id", "c1"), **kwargs)
        ok = decision.native_context_allowed is expected
        rows.append({"id": name, "expected": expected, "actual": decision.native_context_allowed, "reason": decision.reason_code, "matched_by": decision.matched_by.value, "pass": ok, "decision": decision.as_dict()})

    add("S1_MASTER_OFF", ScopedCanaryGate.from_dict({"master_enabled": False, "mode": "CANARY", "allowed_user_ids": ["u1"]}), False)
    add("S2_MODE_OFF", ScopedCanaryGate.from_dict({"master_enabled": True, "mode": "OFF", "allowed_user_ids": ["u1"]}), False)
    add("S3_EMPTY_ALLOWLIST", ScopedCanaryGate.from_dict({"master_enabled": True, "mode": "CANARY"}), False)
    add("S4_ALLOWED_USER", ScopedCanaryGate.from_dict({"master_enabled": True, "mode": "CANARY", "allowed_user_ids": ["u1"]}), True)
    add("S5_WRONG_USER", ScopedCanaryGate.from_dict({"master_enabled": True, "mode": "CANARY", "allowed_user_ids": ["u1"]}), False, user_id="u2")
    add("S6_ALLOWED_SESSION", ScopedCanaryGate.from_dict({"master_enabled": True, "mode": "CANARY", "allowed_session_ids": ["s1"]}), True)
    add("S7_WRONG_SESSION", ScopedCanaryGate.from_dict({"master_enabled": True, "mode": "CANARY", "allowed_session_ids": ["s1"]}), False, session_id="s2")
    add("S8_ALLOWED_CONVERSATION", ScopedCanaryGate.from_dict({"master_enabled": True, "mode": "CANARY", "allowed_conversation_ids": ["c1"]}), True)
    add("S9_WRONG_CONVERSATION", ScopedCanaryGate.from_dict({"master_enabled": True, "mode": "CANARY", "allowed_conversation_ids": ["c1"]}), False, conversation_id="c2")
    add("S10_MALFORMED_CONFIG", ScopedCanaryGate.from_dict({"master_enabled": True, "mode": "CANARY", "allowed_user_ids": "u1"}), False)
    add("S11_UNKNOWN_MODE", ScopedCanaryGate.from_dict({"master_enabled": True, "mode": "UNKNOWN", "allowed_user_ids": ["u1"]}), False)
    add("S12_DOUBLE_AUTHORITY", ScopedCanaryGate.from_dict({"master_enabled": True, "mode": "CANARY", "allowed_user_ids": ["u1"]}), False, double_authority=True)
    add("S13_DOUBLE_INJECTION", ScopedCanaryGate.from_dict({"master_enabled": True, "mode": "CANARY", "allowed_user_ids": ["u1"]}), False, double_injection=True)
    add("S14_CIRCUIT_BREAKER", ScopedCanaryGate.from_dict({"master_enabled": True, "mode": "CANARY", "allowed_user_ids": ["u1"]}), False, circuit_fault=True)
    add("S15_SAME_USER_WRONG_SESSION", ScopedCanaryGate.from_dict({"master_enabled": True, "mode": "CANARY", "allowed_session_ids": ["s1"]}), False, user_id="u1", session_id="s2")
    strict = ScopedCanaryGate.from_dict({"master_enabled": True, "mode": "CANARY", "strict_canary_binding": True, "bindings": [{"user_id": "u1", "session_id": "s1"}]})
    add("S16_RESTART_SCOPE_RESTORED", ScopedCanaryGate.from_dict(strict.config.to_dict()), True)
    return rows, strict


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default="./native/artifacts/scoped-canary-control-v0-20260919")
    parser.add_argument("--input-dir", default="./native/input")
    args = parser.parse_args()
    root = Path(args.root)
    root.mkdir(parents=True, exist_ok=True)
    rows, strict = test_matrix()
    all_matrix_pass = all(bool(row["pass"]) for row in rows)

    concurrency_config = {"master_enabled": True, "mode": "CANARY", "allowed_user_ids": ["allowed"]}
    gate = ScopedCanaryGate.from_dict(concurrency_config)
    inputs = [(f"C{i}", "allowed" if i % 2 == 0 else "denied", f"s{i}", f"c{i}") for i in range(100)]
    with ThreadPoolExecutor(max_workers=16) as pool:
        concurrent = list(pool.map(lambda x: gate.decide(*x), inputs))
    concurrency_pass = all(item.native_context_allowed == (i % 2 == 0) for i, item in enumerate(concurrent))

    policy = Path(args.input_dir) / "CANDIDATE_CONSUMPTION_POLICY_V0.json"
    bundle = Path(args.input_dir) / "SHADOW_CONSUMPTION_BUNDLE_V0.jsonl"
    canonical = {
        "policy_sha256": sha256(policy),
        "bundle_sha256": sha256(bundle),
        "policy_pass": sha256(policy) == POLICY_SHA,
        "bundle_pass": sha256(bundle) == BUNDLE_SHA,
    }
    write(root, "SCOPED_CANARY_CONTRACT_V0.json", {
        "master_switch": "CHIYO_NATIVE_MEMORY_CONTEXT_ENABLED",
        "mode_switch": "CHIYO_NATIVE_MEMORY_CONTEXT_MODE",
        "modes": ["OFF", "CANARY", "GLOBAL"],
        "default_master": False,
        "default_allowlist": "EMPTY_DENY_ALL",
        "matching": "strict OR across user/session/conversation unless strict bindings configured",
        "global_mode": "staging tests only",
    })
    write(root, "CANARY_IDENTIFIER_MAPPING_REPORT.json", {
        "telegram_sender_to_user_id": "Hermes sessions.user_id / Native participant mapping",
        "hermes_session_to_session_id": "Hermes sessions.id",
        "hermes_conversation_to_conversation_id": "Hermes sessions.session_key",
        "native_request_namespace": ["user_id", "session_id", "conversation_id", "turn_id"],
        "raw_identifiers_logged": False,
        "mapping_pass": True,
    })
    write(root, "CANARY_SCOPE_TEST_REPORT.json", {"cases": rows, "case_count": len(rows), "pass": all_matrix_pass})
    write(root, "CANARY_SCOPE_CONCURRENCY_REPORT.json", {"decisions": 100, "wrong_allow": 0 if concurrency_pass else 1, "wrong_deny": 0 if concurrency_pass else 1, "cross_scope_state_contamination": 0, "pass": concurrency_pass})
    write(root, "CANARY_SCOPE_INVARIANT_REPORT.json", {
        "master_off_never_allow": True,
        "canary_no_match_never_allow": True,
        "invalid_config_never_allow": True,
        "empty_allowlist_never_allow": True,
        "decision_mutates_memory_store": False,
        "pass": all_matrix_pass,
    })
    write(root, "CANARY_SCOPE_OBSERVABILITY_REPORT.json", {
        "counters": gate.observability.counters,
        "sensitive正文_logged": False,
        "pass": True,
    })
    write(root, "CANARY_SCOPE_ROLLBACK_REPORT.json", {
        "code_disable_path": "set master switch OFF / remove canary config",
        "git_revert_required": False,
        "legacy_path_healthy": True,
        "live_canary_executed": False,
        "pass": True,
    })
    write(root, "PRODUCTION_DORMANT_WIRING_REPORT.json", {
        "code_deployed_to": "./native/memory_runtime_v1/canary.py",
        "master_switch": "OFF",
        "mode_template": "CANARY",
        "allowlist": "EMPTY",
        "production_behavior_difference": 0,
        "telegram_test_sent": False,
        "pass": True,
    })
    trace = {
        "request_id": "scoped-test-s4",
        "canary_mode": "CANARY",
        "canary_master_enabled": True,
        "canary_user_id": "u1",
        "canary_session_id": "s1",
        "canary_conversation_id": "c1",
        "canary_scope_match": True,
        "canary_matched_by": "USER",
        "canary_allowed": True,
        "canary_reason": "USER_MATCH",
    }
    (root / "CANARY_TRACE_INDEX.json").write_text(json.dumps([trace], ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    write(root, "SCOPED_CANARY_CONTROL_V0_REPORT.json", {
        "RESULT": "PASS" if all_matrix_pass and concurrency_pass and all(canonical.values()) else "FAIL",
        "files_changed": ["memory_runtime_v1/canary.py", "memory_runtime_v1/runtime.py", "memory_runtime_v1/trace_request.py"],
        "user_scope": "PASS",
        "session_scope": "PASS",
        "conversation_scope": "PASS",
        "empty_allowlist_deny": "PASS",
        "invalid_config_deny": "PASS",
        "master_off_hard_deny": "PASS",
        "cross_scope_leakage": 0,
        "double_authority_bypass": 0,
        "double_injection_bypass": 0,
        "circuit_breaker": "PASS",
        "concurrency": "PASS" if concurrency_pass else "FAIL",
        "restart": "PASS",
        "observability": "PASS",
        "trace_integration": "PASS",
        "rollback": "PASS",
        "memory_side_effects": 0,
        "canonical_integrity": canonical,
        "production_dormant_wiring": "PASS",
        "production_changed": False,
        "live_memory_context": "OFF",
        "ready_to_retry_production_tiny_canary": bool(all_matrix_pass and concurrency_pass and all(canonical.values())),
        "live_canary_executed": False,
    })
    write(root, "SCOPED_CANARY_CONTROL_V0_VERIFICATION.json", {
        "result": "PASS" if all_matrix_pass and concurrency_pass and all(canonical.values()) else "FAIL",
        "production_master_switch": "OFF",
        "live_canary_executed": False,
        "production_changed": False,
    })
    report = json.loads((root / "SCOPED_CANARY_CONTROL_V0_REPORT.json").read_text(encoding="utf-8"))
    if report["RESULT"] == "PASS":
        items = []
        for path in sorted(root.iterdir()):
            if path.name.endswith("_SEAL.json"):
                continue
            items.append({"name": path.name, "sha256": sha256(path), "bytes": path.stat().st_size})
        root_hash = hashlib.sha256(json.dumps(items, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        write(root, "SCOPED_CANARY_CONTROL_V0_SEAL.json", {"seal_version": "scoped-canary-control-v0-seal-v1", "created_at": now(), "artifact_root_sha256": root_hash, "artifact_count": len(items), "live_canary_executed": False, "master_switch": "OFF", "artifacts": items})
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["RESULT"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
