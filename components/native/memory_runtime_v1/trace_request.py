from __future__ import annotations

import argparse
import json
from pathlib import Path


def load_observations(root: Path) -> list[dict]:
    path = root / "PRODUCTION_SHADOW_OBSERVATIONS.json"
    observations: list[dict] = []
    if path.exists():
        data = json.loads(path.read_text(encoding="utf-8"))
        observations.extend(data.get("observations") or [])
    canary = root / "CANARY_TRACE_INDEX.json"
    if canary.exists():
        observations.extend(json.loads(canary.read_text(encoding="utf-8")))
    if not observations:
        raise SystemExit("trace observations not found")
    return observations


def main() -> int:
    parser = argparse.ArgumentParser(prog="chiyo-memory trace-request")
    parser.add_argument("request_id")
    parser.add_argument("--root", default="./native/artifacts/memory-runtime-v1")
    parser.add_argument("--hook-report", default=None, help="Optional sanitized Hermes hook report JSON")
    args = parser.parse_args()
    matches = [item for item in load_observations(Path(args.root)) if item.get("request_id") == args.request_id]
    if not matches:
        print(json.dumps({"error": "request_not_found", "request_id": args.request_id}, ensure_ascii=False))
        return 1
    item = matches[0]
    output = {
        "request_id": item.get("request_id"),
        "CURRENT_USER": {"event_id": item.get("current_user_event_id"), "exact": True},
        "EXACT_HISTORY": item.get("exact_history_round_ids", []),
        "DIGESTS": item.get("digest_ids", []),
        "MEMORIES_SURFACED": item.get("memory_ids", []),
        "MEMORIES_CONSUMED": item.get("memory_ids", []),
        "MEMORIES_DEFERRED": item.get("deferred_memory_ids", []),
        "EVIDENCE_AUTHORITY": item.get("evidence_authority", []),
        "SOURCE_LINEAGE": item.get("source_lineage", []),
        "CONTEXT_BUDGET": item.get("context_budget"),
        "FINAL_PROVIDER_VISIBLE_LAYERS": item.get("provider_layers", 0),
        "PERSISTENCE_POLICY": {"capsule": "REQUEST_ONLY", "history": 0, "cross_turn_reuse": 0},
        "CANARY MODE": item.get("canary_mode"),
        "MASTER STATE": item.get("canary_master_enabled"),
        "SCOPE IDS": {
            "user_id": item.get("canary_user_id"),
            "session_id": item.get("canary_session_id"),
            "conversation_id": item.get("canary_conversation_id"),
        },
        "SCOPE MATCH": item.get("canary_scope_match"),
        "MATCHED BY": item.get("canary_matched_by"),
        "FINAL ALLOW/DENY": item.get("canary_allowed"),
        "REASON": item.get("canary_reason"),
    }
    if args.hook_report:
        hook_path = Path(args.hook_report)
        if hook_path.exists():
            hook = json.loads(hook_path.read_text(encoding="utf-8"))
            output["HERMES HOOK"] = {
                "HOOK REACHED": True,
                "MASTER STATE": hook.get("master_state"),
                "CANARY DECISION": hook.get("canary_decision"),
                "LEGACY VISIBILITY": hook.get("legacy_visibility"),
                "NATIVE DECISION": hook.get("reason"),
                "RUNTIME PIPELINE": hook.get("runtime_pipeline"),
                "PROVIDER INJECTION": hook.get("provider_injection_count", 0),
                "PERSISTENCE ELIGIBILITY": hook.get("persistence_eligibility"),
                "FORMATION ELIGIBILITY": hook.get("formation_eligibility"),
            }
        else:
            output["HERMES HOOK"] = {"HOOK REACHED": False, "reason": "hook_report_not_found"}
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
