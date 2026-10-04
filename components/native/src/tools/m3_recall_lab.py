#!/usr/bin/env python3
"""Offline/shadow M3 recall-availability evaluation harness.

This file deliberately has no import path into Chiyo runtime code, no network
client, no model client, and no memory-store writer.  It evaluates synthetic
fixtures through two replaceable interfaces:

  candidate_provider.generate(context, candidates)
  availability_policy.decide(context, generated_candidates)

The bundled gate is a conservative reference policy for preconstruction.  It
is not a production policy and does not establish a universal score or top-k.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple


JsonObject = Dict[str, Any]


def read_jsonl(path: Path) -> List[JsonObject]:
    records: List[JsonObject] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_number, raw in enumerate(handle, start=1):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{line_number}: invalid JSON: {exc}") from exc
            if not isinstance(value, dict):
                raise ValueError(f"{path}:{line_number}: expected a JSON object")
            records.append(value)
    return records


def write_json(path: Path, value: JsonObject) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")


def canonical_digest(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def normalise_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip().lower())


def contains_term(text: str, term: str) -> bool:
    normalised_term = normalise_text(term)
    return bool(normalised_term) and normalised_term in normalise_text(text)


def budget_limit(name: str) -> int:
    """Map qualitative research budgets to a bounded fixture-only limit."""

    return {"zero": 0, "one": 1, "few": 2}.get(name or "zero", 0)


class CandidateProvider:
    """Interface for candidate generation; implementations must be read-only."""

    def generate(self, context: Mapping[str, Any], candidates: Sequence[Mapping[str, Any]]) -> List[JsonObject]:
        raise NotImplementedError


class FixtureCandidateProvider(CandidateProvider):
    """Generate transparent signals from fixture metadata without embeddings."""

    def generate(self, context: Mapping[str, Any], candidates: Sequence[Mapping[str, Any]]) -> List[JsonObject]:
        query = str(context.get("user_text", ""))
        visible_history = [normalise_text(item) for item in context.get("visible_history", [])]
        generated: List[JsonObject] = []
        for index, original in enumerate(candidates):
            candidate = copy.deepcopy(dict(original))
            candidate.setdefault("candidate_id", f"fixture_{index + 1:03d}")
            cue_terms = [str(term) for term in candidate.get("cue_terms", [])]
            entities = [str(entity) for entity in candidate.get("entities", [])]
            content = str(candidate.get("content", ""))
            normalised_content = normalise_text(content)
            visible_duplicate = bool(candidate.get("visible_duplicate", False)) or any(
                normalised_content and (
                    normalised_content == item
                    or normalised_content in item
                    or item in normalised_content
                )
                for item in visible_history
            )
            lexical_terms = [term for term in cue_terms if contains_term(query, term)]
            entity_terms = [entity for entity in entities if contains_term(query, entity)]
            temporal_relevance = candidate.get("temporal_relevance", "none")
            generated.append(
                {
                    "candidate_id": candidate["candidate_id"],
                    "role": candidate.get("role", "unknown"),
                    "kind": candidate.get("kind", "episode"),
                    "content": content,
                    "metadata": {
                        "availability_state": candidate.get("availability_state", "available"),
                        "support_level": candidate.get("support_level", "supported"),
                        "surface_priority": candidate.get("surface_priority", index + 1),
                        "conflict_group": candidate.get("conflict_group"),
                        "surface_eligibility": candidate.get("surface_eligibility"),
                        "explicit_target": bool(candidate.get("explicit_target", False)),
                        "association_only": bool(candidate.get("association_only", False)),
                    },
                    "signals": {
                        "lexical_terms": lexical_terms,
                        "entity_terms": entity_terms,
                        "lexical_match": bool(lexical_terms),
                        "entity_match": bool(entity_terms),
                        "temporal_match": temporal_relevance == "match",
                        "temporal_mismatch": temporal_relevance == "mismatch",
                        "visible_duplicate": visible_duplicate,
                        "adjacent_to_visible": bool(candidate.get("adjacent_to_visible", False)),
                        "query_match": bool(lexical_terms or entity_terms),
                    },
                }
            )
        return generated


class AvailabilityPolicy:
    """Interface for the independent availability gate."""

    def decide(self, context: Mapping[str, Any], generated: Sequence[Mapping[str, Any]]) -> JsonObject:
        raise NotImplementedError


class ReferenceAvailabilityGate(AvailabilityPolicy):
    """Conservative, explainable, fixture-only reference gate.

    The policy makes a small number of categorical checks.  It intentionally
    does not compute a single universal recall score.  Candidate generation
    and availability are separate so another provider or policy can be
    substituted later.
    """

    SAFE_SUPPORT_LEVELS = {"supported", "strong"}
    DOMAIN_ROLES = {"relationship", "self"}
    CONTENT_ROLES = {"direct_answer", "evidence", "superseded", "interpretation", "raw_transcript"}

    def decide(self, context: Mapping[str, Any], generated: Sequence[Mapping[str, Any]]) -> JsonObject:
        active = bool(context.get("active_recall_intent", False))
        memory_necessary = bool(context.get("memory_necessary", False))
        target_domain = str(context.get("target_domain", "none"))
        temporal_cue = bool(context.get("temporal_cue"))
        deep_recall = bool(context.get("deep_recall_requested", False))
        allow_raw = deep_recall and bool(context.get("allow_raw_transcript", False))
        mode = "active" if active else "passive"
        limit = budget_limit(str(context.get("recall_budget", "zero")))
        trace: List[JsonObject] = []
        eligible: List[JsonObject] = []

        for candidate in generated:
            metadata = dict(candidate.get("metadata", {}))
            signals = dict(candidate.get("signals", {}))
            role = str(candidate.get("role", "unknown"))
            reason = "ELIGIBLE"
            allowed = True

            if signals.get("visible_duplicate"):
                allowed = False
                reason = "VISIBLE_DUPLICATE"
            elif role == "false_biography":
                allowed = False
                reason = "UNVERIFIED_BIOGRAPHY"
            elif metadata.get("support_level") not in self.SAFE_SUPPORT_LEVELS:
                allowed = False
                reason = "UNSUPPORTED_CANDIDATE"
            elif metadata.get("availability_state") == "archived" and not active:
                allowed = False
                reason = "ARCHIVE_NOT_PASSIVE_AVAILABLE"
            elif role == "raw_transcript" and not allow_raw:
                allowed = False
                reason = "RAW_REQUIRES_DEEP_RECALL"
            elif role in self.DOMAIN_ROLES and not (active and target_domain == role and metadata.get("explicit_target")):
                allowed = False
                reason = f"{role.upper()}_NOT_REQUESTED"
            elif role == "interpretation" and not (active and target_domain == "interpretation"):
                allowed = False
                reason = "INTERPRETATION_NOT_REQUESTED"
            elif role == "superseded" and temporal_cue:
                allowed = False
                reason = "TEMPORAL_MISMATCH"
            elif role == "superseded":
                allowed = False
                reason = "SUPERSEDED_NOT_DEFAULT"
            elif not active:
                necessary_pass = bool(
                    memory_necessary
                    and role == "direct_answer"
                    and metadata.get("surface_eligibility") == "necessary"
                    and signals.get("adjacent_to_visible")
                )
                if not necessary_pass:
                    allowed = False
                    reason = "PASSIVE_NO_NECESSITY"
            elif not (
                signals.get("query_match")
                or signals.get("entity_match")
                or signals.get("temporal_match")
                or signals.get("adjacent_to_visible")
                or metadata.get("explicit_target")
            ):
                allowed = False
                reason = "NO_CUE_MATCH"

            decision = {
                "candidate_id": candidate["candidate_id"],
                "role": role,
                "allowed": allowed,
                "reason_code": reason,
                "signals": signals,
            }
            trace.append(decision)
            if allowed:
                eligible.append(dict(candidate))

        eligible.sort(key=lambda item: (int(item.get("metadata", {}).get("surface_priority", 999999)), str(item["candidate_id"])))
        selected: List[JsonObject] = []
        if limit > 0 and eligible:
            conflict_groups = {
                item.get("metadata", {}).get("conflict_group")
                for item in eligible
                if item.get("metadata", {}).get("conflict_group")
            }
            if str(context.get("request_kind", "")) == "contradiction" and conflict_groups:
                selected = [
                    item
                    for item in eligible
                    if item.get("metadata", {}).get("conflict_group") in conflict_groups
                ][:limit]
            else:
                selected = eligible[:limit]

        retrieval_allowed = active or bool(
            memory_necessary
            and any(
                item.get("role") == "direct_answer"
                and item.get("metadata", {}).get("surface_eligibility") == "necessary"
                and not item.get("signals", {}).get("visible_duplicate")
                for item in generated
            )
        )
        recall_required = bool(
            selected
            and (
                memory_necessary
                or (active and str(context.get("request_kind", "")) in {"lookup", "active_recall", "deep_recall", "contradiction"})
            )
        )
        empty_allowed = not recall_required

        if selected and str(context.get("request_kind", "")) == "contradiction" and any(
            item.get("metadata", {}).get("conflict_group") for item in selected
        ):
            reason_code = "CONFLICT_SET"
        elif selected and len(eligible) > len(selected):
            reason_code = "BOUNDED_BUDGET"
        elif selected:
            reason_code = "ACTIVE_EXPLICIT_QUERY" if active else "PASSIVE_ADJACENT_NECESSITY"
        elif any(item.get("reason_code") == "VISIBLE_DUPLICATE" for item in trace):
            reason_code = "VISIBLE_DUPLICATE"
        elif any(item.get("reason_code") == "UNVERIFIED_BIOGRAPHY" for item in trace):
            reason_code = "UNVERIFIED_BIOGRAPHY"
        elif any(item.get("reason_code") == "RAW_REQUIRES_DEEP_RECALL" for item in trace):
            reason_code = "RAW_REQUIRES_DEEP_RECALL"
        elif any(item.get("reason_code") == "TEMPORAL_MISMATCH" for item in trace):
            reason_code = "TEMPORAL_MISMATCH"
        elif trace:
            reason_code = trace[0]["reason_code"]
        else:
            reason_code = "NO_CANDIDATES"

        return {
            "allowed": retrieval_allowed,
            "required": recall_required,
            "empty_allowed": empty_allowed,
            "mode": mode,
            "budget": context.get("recall_budget", "zero"),
            "reason_code": reason_code,
            "surface_count": len(selected),
            "selected_refs": [item["candidate_id"] for item in selected],
            "reply_authority": False,
            "gate_is_independent": True,
            "audit_trace": trace,
        }, selected


def policy_decide(policy: AvailabilityPolicy, context: Mapping[str, Any], generated: Sequence[Mapping[str, Any]]) -> Tuple[JsonObject, List[JsonObject]]:
    result = policy.decide(context, generated)
    if not isinstance(result, tuple) or len(result) != 2:
        raise TypeError("availability_policy.decide must return (availability, selected)")
    availability, selected = result
    return dict(availability), [dict(item) for item in selected]


def run_case(
    case: Mapping[str, Any],
    expected: Mapping[str, Any],
    candidate_provider: CandidateProvider,
    availability_policy: AvailabilityPolicy,
) -> JsonObject:
    context = copy.deepcopy(dict(case.get("context", {})))
    candidates = copy.deepcopy(list(case.get("candidates", [])))
    generated = candidate_provider.generate(context, candidates)
    availability, selected = policy_decide(availability_policy, context, generated)
    selected_ids = {item["candidate_id"] for item in selected}
    selected_roles = [str(item.get("role", "unknown")) for item in selected]
    selected_by_id = {item["candidate_id"]: item for item in selected}

    violations: List[str] = []
    must_surface_roles = set(expected.get("must_surface_roles", []))
    must_not_surface_roles = set(expected.get("must_not_surface_roles", []))
    dangerous_items = set(expected.get("dangerous_items", []))
    if availability["allowed"] != bool(expected.get("recall_allowed", False)):
        violations.append("recall_allowed_mismatch")
    if availability["required"] != bool(expected.get("recall_required", False)):
        violations.append("recall_required_mismatch")
    if availability["empty_allowed"] != bool(expected.get("empty_allowed", False)):
        violations.append("empty_allowed_mismatch")
    if len(selected) > int(expected.get("max_reasonable_items", 0)):
        violations.append("recall_flood")
    if must_surface_roles and not (must_surface_roles & set(selected_roles)):
        violations.append("required_role_missing")
    if must_not_surface_roles & set(selected_roles):
        violations.append("forbidden_role_surfaced")

    selected_conflict_groups = {
        item.get("metadata", {}).get("conflict_group")
        for item in selected
        if item.get("metadata", {}).get("conflict_group")
    }
    if expected.get("require_conflict_set") and len(selected_conflict_groups) != 1:
        violations.append("conflict_set_missing")
    if "visible_duplicate" in dangerous_items and any(
        item.get("signals", {}).get("visible_duplicate") for item in selected
    ):
        violations.append("duplicate_surface")
    if "raw_transcript" in dangerous_items and any(item.get("role") == "raw_transcript" for item in selected):
        violations.append("raw_transcript_intrusion")
    if "false_biography" in dangerous_items and any(item.get("role") == "false_biography" for item in selected):
        violations.append("false_biography_surface")

    actual_empty = not selected
    return {
        "case_id": case["case_id"],
        "case_type": case.get("case_type"),
        "layer_mode": case.get("layer_mode", "episode-only"),
        "retrieved_candidates": generated,
        "availability": availability,
        "surfaced": [item["candidate_id"] for item in selected],
        "surfaced_roles": selected_roles,
        "empty_recall": actual_empty,
        "audit": {
            "selected_refs": list(selected_ids),
            "selected_metadata": {
                item_id: {
                    "role": selected_by_id[item_id].get("role"),
                    "kind": selected_by_id[item_id].get("kind"),
                }
                for item_id in sorted(selected_by_id)
            },
            "no_reply_authority": availability.get("reply_authority") is False,
            "violations": violations,
        },
    }


def load_candidate_overrides(path: Optional[Path]) -> Dict[str, List[JsonObject]]:
    if path is None:
        return {}
    overrides: Dict[str, List[JsonObject]] = {}
    for record in read_jsonl(path):
        case_id = record.get("case_id")
        if not case_id:
            raise ValueError(f"{path}: candidate override missing case_id")
        if "candidates" in record:
            values = record["candidates"]
        else:
            values = [record]
        if not isinstance(values, list):
            raise ValueError(f"{path}: candidates for {case_id} must be a list")
        overrides[str(case_id)] = copy.deepcopy(values)
    return overrides


def load_context_overrides(path: Optional[Path]) -> Dict[str, JsonObject]:
    if path is None:
        return {}
    overrides: Dict[str, JsonObject] = {}
    for record in read_jsonl(path):
        case_id = record.get("case_id")
        if not case_id:
            raise ValueError(f"{path}: context override missing case_id")
        context = record.get("context", record)
        if not isinstance(context, dict):
            raise ValueError(f"{path}: context for {case_id} must be an object")
        overrides[str(case_id)] = copy.deepcopy(context)
    return overrides


def with_overrides(
    cases: Sequence[Mapping[str, Any]],
    context_overrides: Mapping[str, Mapping[str, Any]],
    candidate_overrides: Mapping[str, Sequence[Mapping[str, Any]]],
) -> List[JsonObject]:
    result: List[JsonObject] = []
    for original in cases:
        case = copy.deepcopy(dict(original))
        case_id = str(case["case_id"])
        if case_id in context_overrides:
            case["context"] = copy.deepcopy(dict(context_overrides[case_id]))
        if case_id in candidate_overrides:
            case["candidates"] = copy.deepcopy(list(candidate_overrides[case_id]))
        result.append(case)
    return result


def compute_metrics(results: Sequence[Mapping[str, Any]], expected_by_id: Mapping[str, Mapping[str, Any]]) -> JsonObject:
    total = len(results)
    required_cases = [item for item in results if expected_by_id[item["case_id"]].get("recall_required")]
    empty_cases = [item for item in results if expected_by_id[item["case_id"]].get("empty_allowed")]
    dangerous_cases = [
        item
        for item in results
        if expected_by_id[item["case_id"]].get("dangerous_items")
        or expected_by_id[item["case_id"]].get("must_not_surface_roles")
    ]
    duplicate_exposure = [
        item
        for item in results
        if any(candidate.get("signals", {}).get("visible_duplicate") for candidate in item["retrieved_candidates"])
    ]
    interpretation_exposure = [
        item
        for item in results
        if any(candidate.get("role") == "interpretation" for candidate in item["retrieved_candidates"])
        and "interpretation" in expected_by_id[item["case_id"]].get("dangerous_items", [])
    ]
    self_noise_exposure = [
        item
        for item in results
        if "self" in expected_by_id[item["case_id"]].get("dangerous_items", [])
    ]
    relationship_noise_exposure = [
        item
        for item in results
        if "relationship" in expected_by_id[item["case_id"]].get("dangerous_items", [])
    ]
    selective_non_recall = [
        item
        for item in results
        if not expected_by_id[item["case_id"]].get("recall_required")
        or expected_by_id[item["case_id"]].get("empty_allowed")
    ]

    def rate(numerator: int, denominator: int) -> Optional[float]:
        return round(numerator / denominator, 4) if denominator else None

    violation_count = sum(bool(item.get("audit", {}).get("violations")) for item in results)
    flood_count = sum(
        "recall_flood" in item.get("audit", {}).get("violations", []) for item in results
    )
    false_surface_count = sum(
        any(
            code in item.get("audit", {}).get("violations", [])
            for code in (
                "forbidden_role_surfaced",
                "false_biography_surface",
                "raw_transcript_intrusion",
                "duplicate_surface",
            )
        )
        for item in results
    )
    duplicate_surface_count = sum("duplicate_surface" in item.get("audit", {}).get("violations", []) for item in results)
    interpretation_intrusion_count = sum(
        any(role == "interpretation" for role in item.get("surfaced_roles", []))
        for item in interpretation_exposure
    )
    self_noise_count = sum(any(role == "self" for role in item.get("surfaced_roles", [])) for item in self_noise_exposure)
    relationship_noise_count = sum(
        any(role == "relationship" for role in item.get("surfaced_roles", []))
        for item in relationship_noise_exposure
    )
    empty_success_count = sum(item.get("empty_recall") is True for item in empty_cases)
    selective_success_count = sum(item.get("empty_recall") is True for item in selective_non_recall)
    required_success_count = sum(
        not any(code in item.get("audit", {}).get("violations", []) for code in ("required_role_missing", "recall_required_mismatch"))
        for item in required_cases
    )

    return {
        "cases": total,
        "required_recall_cases": len(required_cases),
        "empty_allowed_cases": len(empty_cases),
        "dangerous_surface_cases": len(dangerous_cases),
        "required_recall_success_rate": rate(required_success_count, len(required_cases)),
        "false_surface_rate": rate(false_surface_count, len(dangerous_cases)),
        "recall_flood_rate": rate(flood_count, total),
        "duplicate_surface_rate": rate(duplicate_surface_count, len(duplicate_exposure)),
        "interpretation_intrusion_rate": rate(interpretation_intrusion_count, len(interpretation_exposure)),
        "self_noise_rate": rate(self_noise_count, len(self_noise_exposure)),
        "relationship_noise_rate": rate(relationship_noise_count, len(relationship_noise_exposure)),
        "empty_recall_success_rate": rate(empty_success_count, len(empty_cases)),
        "selective_non_recall_rate": rate(selective_success_count, len(selective_non_recall)),
        "snrr_definition": "correct empty decisions / cases where recall is not required or empty is explicitly allowed",
        "case_violation_count": violation_count,
        "pass": violation_count == 0,
    }


def run_suite(
    cases: Sequence[Mapping[str, Any]],
    expected_by_id: Mapping[str, Mapping[str, Any]],
    candidate_provider: CandidateProvider,
    availability_policy: AvailabilityPolicy,
    repeat: int,
) -> JsonObject:
    if repeat < 1:
        raise ValueError("repeat must be at least 1")
    base_digest = canonical_digest([case.get("candidates", []) for case in cases])
    all_runs: List[List[JsonObject]] = []
    for _ in range(repeat):
        run_results: List[JsonObject] = []
        for case in cases:
            case_id = str(case["case_id"])
            if case_id not in expected_by_id:
                raise ValueError(f"missing expected behavior for {case_id}")
            run_results.append(run_case(case, expected_by_id[case_id], candidate_provider, availability_policy))
        all_runs.append(run_results)
    final_digest = canonical_digest([case.get("candidates", []) for case in cases])
    first_results = all_runs[0]
    last_results = all_runs[-1]
    stable_surface = [
        first["surfaced"] == last["surfaced"]
        for first, last in zip(first_results, last_results)
    ]
    metrics = compute_metrics(first_results, expected_by_id)
    repeat_ok = base_digest == final_digest and all(stable_surface)
    return {
        "schema": "m3-recall-lab.v0",
        "mode": "offline-shadow",
        "repeat": repeat,
        "read_does_not_reinforce": {
            "pass": repeat_ok,
            "before_candidate_digest": base_digest,
            "after_candidate_digest": final_digest,
            "surfaced_sets_stable": all(stable_surface),
            "writer_invoked": False,
        },
        "metrics": metrics,
        "results": first_results,
        "pass": bool(metrics["pass"] and repeat_ok),
    }


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the offline M3 recall/availability lab.")
    parser.add_argument("--cases", type=Path, default=Path("evals/m3/cases.jsonl"))
    parser.add_argument("--expected", type=Path, default=Path("evals/m3/expected_behavior.jsonl"))
    parser.add_argument("--context", type=Path, help="Optional JSONL context override input.")
    parser.add_argument("--candidates", type=Path, help="Optional JSONL candidate override input.")
    parser.add_argument("--output", type=Path, help="Write a JSON audit result to this path.")
    parser.add_argument("--repeat", type=int, default=1, help="Repeat read-only evaluation N times.")
    parser.add_argument("--case-id", help="Run only one fixture case.")
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    cases = read_jsonl(args.cases)
    expected_records = read_jsonl(args.expected)
    expected_by_id = {str(record["case_id"]): record for record in expected_records}
    if len(expected_by_id) != len(expected_records):
        raise ValueError("expected behavior contains duplicate case_id values")
    context_overrides = load_context_overrides(args.context)
    candidate_overrides = load_candidate_overrides(args.candidates)
    cases = with_overrides(cases, context_overrides, candidate_overrides)
    if args.case_id:
        cases = [case for case in cases if str(case["case_id"]) == args.case_id]
        if not cases:
            raise ValueError(f"unknown case_id: {args.case_id}")
    result = run_suite(cases, expected_by_id, FixtureCandidateProvider(), ReferenceAvailabilityGate(), args.repeat)
    if args.output:
        write_json(args.output, result)
    print(json.dumps({
        "pass": result["pass"],
        "cases": result["metrics"]["cases"],
        "repeat": result["repeat"],
        "metrics": result["metrics"],
        "read_does_not_reinforce": result["read_does_not_reinforce"],
    }, ensure_ascii=False, sort_keys=True))
    return 0 if result["pass"] else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, TypeError, ValueError) as exc:
        print(f"m3_recall_lab: ERROR: {exc}", file=sys.stderr)
        raise SystemExit(2)
