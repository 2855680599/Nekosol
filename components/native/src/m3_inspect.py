from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.m3 import M3Store  # noqa: E402


def decode(value: str | None, fallback):
    try:
        return json.loads(value) if value is not None else fallback
    except json.JSONDecodeError:
        return fallback


def sanitize_why(payload: dict) -> dict:
    evaluation = payload["evaluation"]
    availability = payload.get("availability") or {}
    surface = decode(availability.get("surface_payload_json"), {})
    safe_surface = {
        key: value for key, value in surface.items()
        if key in {"source_type", "episode_id", "supporting_evidence_refs", "trigger_sequence", "reply_authority", "cognitive_intent", "trigger_evidence_id"}
    }
    safe_surface["secondary_understanding_ids"] = [
        str(item.get("understanding_id")) for item in surface.get("secondary_understandings", []) if isinstance(item, dict)
    ]
    return {
        "evaluation": evaluation,
        "candidates": [
            {
                "candidate_id": item["candidate_id"],
                "episode_id": item["episode_id"],
                "source_event_ids": decode(item.get("source_event_ids_json"), []),
                "source_max_sequence": item["source_max_sequence"],
                "signals": decode(item.get("generator_signals_json"), {}),
                "provenance": decode(item.get("provenance_json"), {}),
            }
            for item in payload.get("candidates", [])
        ],
        "eligibility": payload.get("eligibility", []),
        "availability": {
            "outcome": availability.get("outcome"),
            "selected_refs": decode(availability.get("selected_refs_json"), []),
            "reason_code": availability.get("reason_code"),
            "surface": safe_surface,
        },
        "secondary_expansions": payload.get("secondary_expansions", []),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only M3 shadow inspector")
    parser.add_argument("--db", default="/var/lib/chiyo/memory/recall_shadow.sqlite")
    sub = parser.add_subparsers(dest="command", required=True)
    recent = sub.add_parser("recent")
    recent.add_argument("--limit", type=int, default=50)
    turn = sub.add_parser("turn")
    turn.add_argument("trigger_evidence_id")
    sub.add_parser("recalls").add_argument("--limit", type=int, default=50)
    sub.add_parser("silence").add_argument("--limit", type=int, default=50)
    p1 = sub.add_parser("p1")
    p1.add_argument("--limit", type=int, default=50)
    p2 = sub.add_parser("p2")
    p2.add_argument("--limit", type=int, default=50)
    episode = sub.add_parser("episode")
    episode.add_argument("episode_id")
    why = sub.add_parser("why")
    why.add_argument("evaluation_id")
    sub.add_parser("withheld-m2").add_argument("--limit", type=int, default=100)
    sub.add_parser("verify")
    args = parser.parse_args()
    if not Path(args.db).exists():
        print(json.dumps({"error": "m3_store_not_found", "path": args.db}, ensure_ascii=False))
        return 1
    store = M3Store(args.db, initialize=False)
    if args.command == "recent":
        result = store.recent(args.limit)
    elif args.command == "recalls":
        result = [row for row in store.recent(args.limit * 4) if row.get("status") == "COMPLETED"]
    elif args.command == "silence":
        result = [row for row in store.recent(args.limit * 4) if row.get("status") == "COMPLETED"]
        result = [row for row in result if store.why(row["evaluation_id"])["availability"]["outcome"] == "NO_SURFACE"][: args.limit]
    elif args.command == "p1":
        result = store.recent(args.limit, "P1")
    elif args.command == "p2":
        result = store.recent(args.limit, "P2")
    elif args.command == "turn":
        rows = store.recent(1000)
        result = next((row for row in rows if row["trigger_evidence_id"] == args.trigger_evidence_id), None)
        if result is not None:
            result = sanitize_why(store.why(result["evaluation_id"]))
    elif args.command == "why":
        result = sanitize_why(store.why(args.evaluation_id)) if store.why(args.evaluation_id) else None
    elif args.command == "episode":
        result = []
        for row in store.recent(1000):
            detail = store.why(row["evaluation_id"])
            if detail and any(item.get("episode_id") == args.episode_id for item in detail.get("candidates", [])):
                result.append(row)
    elif args.command == "withheld-m2":
        result = []
        for row in store.recent(1000):
            detail = store.why(row["evaluation_id"])
            if detail:
                result.extend(detail.get("secondary_expansions", []))
        result = result[: args.limit]
    else:
        result = store.verify()
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["ok"] else 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result is not None else 1


if __name__ == "__main__":
    raise SystemExit(main())
