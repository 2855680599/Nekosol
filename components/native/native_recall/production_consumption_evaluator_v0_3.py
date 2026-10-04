"""CANDIDATE_CONSUMPTION_POLICY_V0.3 -- runtime-grounded production policy.

V0.1 and V0.2 are FROZEN and are never modified by this module.

Design principle
----------------
The policy may depend only on information the production runtime already possesses or
can deterministically derive from persisted evidence.  It therefore does NOT depend on:

    sealed canonical relation verdicts        offline reviewer judgement
    GT labels                                 Phase-N final decision
    Phase-P bundle state                      unpersisted semantic judgement
    a claim splitter that does not exist

The production unit of consumption is an ``EvidenceBundle``: one retrieved Memory
Episode plus its persisted evidence events, their provenance and their temporal
metadata.  Nothing is forced into claims.
"""
from __future__ import annotations

import dataclasses
import hashlib
import pathlib
from typing import Any, Dict, List, Optional, Sequence, Tuple

from production_consumption_evaluator_v0 import normalize, utf16_length

POLICY_ID = "CANDIDATE_CONSUMPTION_POLICY_V0_3"
POLICY_VERSION = "0.3"
POLICY_FILE = "CANDIDATE_CONSUMPTION_POLICY_V0_3.json"
V0_1_POLICY_SHA256 = "5c6ba70a160cbe4fa880a91caab47bf548138750bc1257d7daa9764a6a4679b7"
V0_2_POLICY_SHA256 = "33eaa9c690d68ded77283f23cd21282802919424f12b6cabb88b82a8fcb5fe62"
V0_2_STATUS = "CANONICAL-RECONCILIATION / FROZEN / NOT DEPLOYABLE"
V0_1_STATUS = "HISTORICAL / FROZEN / NOT PRODUCTION"

DECISION_ENUM = ("CONSUME", "ABSTAIN", "SKIP_REDUNDANT", "REJECT_INTEGRITY")
RUNTIME_DEFAULT = "ABSTAIN"

# --- authority -------------------------------------------------------------- #
AUTHORITY_CLASSES = (
    "DIRECT_USER_EVIDENCE",
    "TOOL_OBSERVED_EVIDENCE",
    "VERIFIED_EXECUTION_OUTCOME",
    "ASSISTANT_SELF_HISTORY",
    "DERIVED_MEMORY",
    "SUMMARY_OR_INTERPRETATION",
    "UNKNOWN",
)
AUTHORITY_MAY_BE_PROMOTED = False
AUTHORITY_RANK = {  # used only to DETECT an attempted promotion, never to grant one
    "DIRECT_USER_EVIDENCE": 6, "VERIFIED_EXECUTION_OUTCOME": 5, "TOOL_OBSERVED_EVIDENCE": 4,
    "ASSISTANT_SELF_HISTORY": 3, "DERIVED_MEMORY": 2, "SUMMARY_OR_INTERPRETATION": 1,
    "UNKNOWN": 0,
}
SOURCE_ROLES = ("user", "assistant", "tool", "system", "unknown")
ROLE_TO_AUTHORITY = {
    "user": "DIRECT_USER_EVIDENCE",
    "assistant": "ASSISTANT_SELF_HISTORY",
    "tool": "TOOL_OBSERVED_EVIDENCE",
    "system": "SUMMARY_OR_INTERPRETATION",
    "unknown": "UNKNOWN",
}
# The three permanent firewalls.
FIREWALLS = {
    "past_evidence_is_current_instruction": False,
    "historical_wording_is_style_authority": False,
    "assistant_self_history_is_user_truth": False,
}
STRIPPED_VALUES = ("NONE", "", None)

# --- bundle safety ---------------------------------------------------------- #
BUNDLE_SAFETY_STATUSES = (
    "SAFE_EVIDENCE_BUNDLE",
    "MIXED_AUTHORITY",
    "MISSING_PROVENANCE",
    "UNSAFE_INSTRUCTIONAL_CONTENT",
    "UNKNOWN",
)
SEMANTIC_RELATION_VALUES = ("UNKNOWN",)   # V0.3 has no semantic relation layer

# --- gates ------------------------------------------------------------------ #
GATE_ORDER = (
    "HARD_INTEGRITY_GATE",
    "SOURCE_TRACE_GATE",
    "AUTHORITY_PROVENANCE_SAFETY_GATE",
    "CURRENT_INSTRUCTION_FIREWALL_GATE",
    "KNOWN_SUPERSESSION_INVALIDATION_GATE",
    "EXACT_IDENTITY_REDUNDANCY_GATE",
    "BUNDLE_SAFETY_GATE",
    "CONSUME_GATE",
    "FAIL_CLOSED_DEFAULT",
)

# --- reason codes ----------------------------------------------------------- #
REASON_SAFE = "SAFE_USEFUL_HISTORICAL_BUNDLE"
REASON_SAFE_MIXED = "SAFE_USEFUL_MIXED_AUTHORITY_BUNDLE"
REASON_SAFE_HISTORICAL_OVER_CURRENT = "HISTORICAL_EVIDENCE_DOES_NOT_OVERRIDE_CURRENT"
REASON_REDUNDANT_EVENT = "REDUNDANT_SAME_SOURCE_EVENT_ID"
REASON_REDUNDANT_EPISODE = "REDUNDANT_SAME_EPISODE_ALREADY_VISIBLE"
REASON_REDUNDANT_IDENTITY = "REDUNDANT_EXACT_NORMALIZED_EVIDENCE_IDENTITY"
REASON_INTEGRITY = "INTEGRITY_FAILURE"
REASON_SOURCE_TRACE = "SOURCE_TRACE_INCOMPLETE"
REASON_MISSING_PROVENANCE = "BUNDLE_MISSING_PROVENANCE"
REASON_AUTHORITY_CLASS = "AUTHORITY_CLASS_NOT_PERMITTED"
REASON_AUTHORITY_PROMOTION = "BUNDLE_AUTHORITY_PROMOTION_FORBIDDEN"
REASON_INSTRUCTION = "HISTORICAL_INSTRUCTION_FORCE_FORBIDDEN"
REASON_SUPERSEDED = "KNOWN_SUPERSEDED_OR_INVALIDATED"
REASON_UNSAFE_BUNDLE = "BUNDLE_UNSAFE_INSTRUCTIONAL_CONTENT"
REASON_MIXED_UNPRESERVABLE = "MIXED_AUTHORITY_BOUNDARY_NOT_PRESERVABLE"
REASON_BUNDLE_UNKNOWN = "BUNDLE_SAFETY_UNKNOWN"
REASON_NO_EVIDENCE = "INSUFFICIENT_SAFE_EVIDENCE"

REASON_CODE_ENUM = (
    REASON_SAFE, REASON_SAFE_MIXED, REASON_SAFE_HISTORICAL_OVER_CURRENT,
    REASON_REDUNDANT_EVENT, REASON_REDUNDANT_EPISODE, REASON_REDUNDANT_IDENTITY,
    REASON_INTEGRITY, REASON_SOURCE_TRACE, REASON_MISSING_PROVENANCE,
    REASON_AUTHORITY_CLASS, REASON_AUTHORITY_PROMOTION, REASON_INSTRUCTION,
    REASON_SUPERSEDED, REASON_UNSAFE_BUNDLE, REASON_MIXED_UNPRESERVABLE,
    REASON_BUNDLE_UNKNOWN, REASON_NO_EVIDENCE,
)

V0_3_FEATURES = (
    "episode_present",
    "evidence_unit_count",
    "multi_event_episode",
    "distinct_authority_class_count",
    "bundle_declared_authority_class",
    "bundle_max_unit_authority_rank",
    "bundle_authority_promotion_attempted",
    "bundle_safety_status",
    "all_units_have_provenance",
    "all_units_have_timestamp",
    "all_units_have_source_ref",
    "all_units_have_source_role",
    "any_unit_instructional_force",
    "any_unit_authority_promoted",
    "per_evidence_authority_preserved_by_provider",
    "authority_classes_permitted",
    "known_supersession",
    "known_invalidation",
    "known_retraction",
    "visible_exact_event_id_overlap",
    "episode_already_visible",
    "exact_normalized_identity_duplicate",
    "semantic_relation",
    "bundle_evidence_utf16_length",
    "current_direct_evidence_present",
    "current_user_evidence_would_be_overridden",
)


# --------------------------------------------------------------------------- #
# input contract
# --------------------------------------------------------------------------- #
@dataclasses.dataclass(frozen=True)
class EvidenceUnit:
    """One persisted evidence event inside the bundle (a real store row)."""

    event_id: str = ""
    source_role: str = "unknown"
    authority_class: str = "UNKNOWN"
    timestamp: str = ""
    source_ref: str = ""
    lineage_status: str = "FULL"
    content: str = ""
    instructional_force: str = "NONE"
    authority_promoted: bool = False
    provenance_present: bool = True

    @property
    def present(self) -> bool:
        return bool(self.event_id or self.content or self.source_ref)

    @property
    def lineage_valid(self) -> bool:
        return str(self.lineage_status).upper() in ("FULL", "VALID", "PASS", "")

    def as_dict(self) -> Dict[str, Any]:
        return {
            "event_id": self.event_id,
            "source_role": self.source_role,
            "authority_class": self.authority_class,
            "timestamp": self.timestamp,
            "source_ref": self.source_ref,
            "lineage_status": self.lineage_status,
            "instructional_force": self.instructional_force,
        }


@dataclasses.dataclass(frozen=True)
class ProductionConsumptionCandidateV0_3:
    """Every field below is production-obtainable (see V0_3_RUNTIME_INPUT_AUDIT)."""

    memory_id: str = ""
    episode_id: str = ""
    evidence_units: Sequence[EvidenceUnit] = ()
    surface_payload: Optional[Dict[str, Any]] = None
    current_user_event_id: str = ""
    current_user_timestamp: str = ""
    current_user_content: str = ""
    visible_exact_event_ids: Sequence[str] = ()
    visible_exact_episode_ids: Sequence[str] = ()
    visible_exact_contents: Sequence[str] = ()
    supersession_metadata: Optional[Dict[str, Any]] = None
    invalidation_metadata: Optional[Dict[str, Any]] = None
    provider_preserves_per_evidence_authority: bool = True
    bundle_declared_authority_class: Optional[str] = None

    @property
    def units(self) -> List[EvidenceUnit]:
        return [u for u in self.evidence_units if u.present]

    @property
    def evidence_text(self) -> str:
        return " ".join(u.content for u in self.units if u.content)

    @property
    def current_direct_present(self) -> bool:
        return bool(self.current_user_event_id or self.current_user_content.strip())

    @classmethod
    def from_record(cls, r: Dict[str, Any]) -> "ProductionConsumptionCandidateV0_3":
        units = []
        for u in r.get("evidence_units") or []:
            if isinstance(u, EvidenceUnit):
                units.append(u)
                continue
            role = str(u.get("source_role") or u.get("speaker") or "unknown").lower()
            if role == "chiyo":
                role = "assistant"
            units.append(EvidenceUnit(
                event_id=str(u.get("event_id") or ""),
                source_role=role,
                authority_class=str(u.get("authority_class")
                                    or ROLE_TO_AUTHORITY.get(role, "UNKNOWN")),
                timestamp=str(u.get("timestamp") or ""),
                source_ref=str(u.get("source_ref") or ""),
                lineage_status=str(u.get("lineage_status") or "FULL"),
                content=str(u.get("content") or ""),
                instructional_force=str(u.get("instructional_force") or "NONE"),
                authority_promoted=bool(u.get("authority_promoted")),
                provenance_present=bool(u.get("provenance_present", True)),
            ))
        return cls(
            memory_id=str(r.get("memory_id") or ""),
            episode_id=str(r.get("episode_id") or ""),
            evidence_units=tuple(units),
            surface_payload=r.get("surface_payload"),
            current_user_event_id=str(r.get("current_user_event_id") or ""),
            current_user_timestamp=str(r.get("current_user_timestamp") or ""),
            current_user_content=str(r.get("current_user_content") or ""),
            visible_exact_event_ids=tuple(r.get("visible_exact_event_ids") or ()),
            visible_exact_episode_ids=tuple(r.get("visible_exact_episode_ids") or ()),
            visible_exact_contents=tuple(r.get("visible_exact_contents") or ()),
            supersession_metadata=r.get("supersession_metadata"),
            invalidation_metadata=r.get("invalidation_metadata"),
            provider_preserves_per_evidence_authority=bool(
                r.get("provider_preserves_per_evidence_authority", True)),
            bundle_declared_authority_class=r.get("bundle_declared_authority_class"),
        )


@dataclasses.dataclass(frozen=True)
class ConsumptionDecisionV0_3:
    memory_id: str
    episode_id: str
    decision: str
    reason_codes: Sequence[str]
    bundle_authority_classes: Sequence[str]
    bundle_safety_status: str
    policy_version: str
    policy_sha: str
    rule_trace: Sequence[str] = ()
    features: Optional[Dict[str, Any]] = None
    bundle_representation: Sequence[Dict[str, Any]] = ()
    authority_promoted: bool = False
    semantic_relation: str = "UNKNOWN"

    def to_dict(self, include_internal: bool = True) -> Dict[str, Any]:
        d = {
            "memory_id": self.memory_id,
            "episode_id": self.episode_id,
            "decision": self.decision,
            "policy_version": self.policy_version,
            "policy_sha": self.policy_sha,
        }
        if not include_internal:
            return dict(d)
        d["reason_codes"] = list(self.reason_codes)
        d["bundle_authority_classes"] = list(self.bundle_authority_classes)
        d["bundle_safety_status"] = self.bundle_safety_status
        d["rule_trace"] = list(self.rule_trace)
        d["features"] = dict(self.features or {})
        d["bundle_representation"] = [dict(u) for u in self.bundle_representation]
        d["authority_promoted"] = bool(self.authority_promoted)
        d["semantic_relation"] = self.semantic_relation
        return d


# --------------------------------------------------------------------------- #
# bundle safety (requirement 3 / 4)
# --------------------------------------------------------------------------- #
def evaluate_bundle_safety(c: ProductionConsumptionCandidateV0_3) -> Tuple[str, List[str]]:
    """Deterministic bundle_safety_status from persisted metadata only."""
    units = c.units
    if not units:
        return "UNKNOWN", ["no_evidence_units"]
    reasons: List[str] = []
    if any(not u.provenance_present or not u.source_ref or not u.event_id for u in units):
        return "MISSING_PROVENANCE", ["missing_event_id_or_source_ref"]
    if any(str(u.instructional_force) not in ("NONE", "") for u in units):
        return "UNSAFE_INSTRUCTIONAL_CONTENT", ["unit_declares_instructional_force"]
    if any(u.authority_promoted for u in units):
        return "UNSAFE_INSTRUCTIONAL_CONTENT", ["unit_authority_promoted"]
    classes = {u.authority_class for u in units}
    if len(classes) > 1:
        return "MIXED_AUTHORITY", ["distinct_authority_classes=" + str(len(classes))]
    return "SAFE_EVIDENCE_BUNDLE", reasons


# --------------------------------------------------------------------------- #
# the executable realization
# --------------------------------------------------------------------------- #
class ProductionConsumptionEvaluatorV0_3:
    """The single production implementation of CANDIDATE_CONSUMPTION_POLICY_V0.3."""

    policy_version = POLICY_VERSION
    policy_id = POLICY_ID

    def __init__(self, policy_sha: str = ""):
        self.policy_sha = policy_sha

    # -- features ----------------------------------------------------------- #
    def extract_features(self, c: ProductionConsumptionCandidateV0_3) -> Dict[str, Any]:
        units = c.units
        classes = [u.authority_class for u in units]
        distinct = sorted(set(classes))
        status, status_reasons = evaluate_bundle_safety(c)

        vis_ev = {str(x) for x in c.visible_exact_event_ids}
        ev_ids = {u.event_id for u in units if u.event_id}
        overlap = sorted(vis_ev & ev_ids)

        vis_norm = {normalize(x) for x in c.visible_exact_contents if x}
        dup_identity = sorted({normalize(u.content) for u in units if u.content} & vis_norm)

        declared = c.bundle_declared_authority_class
        max_unit_rank = max((AUTHORITY_RANK.get(a, 0) for a in classes), default=0)
        # declaring an authority the units do not justify is a promotion attempt, whether or
        # not the bundle happens to mix classes
        promotion = bool(
            declared
            and declared not in set(classes)
            and declared in AUTHORITY_RANK
            and AUTHORITY_RANK[declared] > max_unit_rank
        )
        override = bool(
            c.current_direct_present
            and any(a == "ASSISTANT_SELF_HISTORY" for a in classes)
            and not c.provider_preserves_per_evidence_authority
        )

        return {
            "episode_present": bool(c.episode_id),
            "evidence_unit_count": len(units),
            "multi_event_episode": len(units) > 1,
            "distinct_authority_class_count": len(distinct),
            "bundle_declared_authority_class": declared,
            "bundle_max_unit_authority_rank": max_unit_rank,
            "bundle_authority_promotion_attempted": promotion,
            "bundle_safety_status": status,
            "bundle_safety_reasons": status_reasons,
            "all_units_have_provenance": all(u.provenance_present for u in units) and bool(units),
            "all_units_have_timestamp": all(bool(u.timestamp) for u in units) and bool(units),
            "all_units_have_source_ref": all(bool(u.source_ref) for u in units) and bool(units),
            "all_units_have_source_role": all(
                u.source_role in SOURCE_ROLES and u.source_role != "unknown"
                for u in units) and bool(units),
            "any_unit_instructional_force": any(
                str(u.instructional_force) not in ("NONE", "") for u in units),
            "any_unit_authority_promoted": any(u.authority_promoted for u in units),
            "per_evidence_authority_preserved_by_provider":
                bool(c.provider_preserves_per_evidence_authority),
            "authority_classes_permitted": all(a in AUTHORITY_CLASSES for a in classes) and bool(units),
            "known_supersession": bool((c.supersession_metadata or {}).get("superseded")),
            "known_invalidation": bool((c.invalidation_metadata or {}).get("invalidated")),
            "known_retraction": bool((c.invalidation_metadata or {}).get("retracted")
                                     or (c.supersession_metadata or {}).get("retracted")),
            "visible_exact_event_id_overlap": overlap,
            "episode_already_visible": bool(
                c.episode_id and c.episode_id in {str(x) for x in c.visible_exact_episode_ids}),
            "exact_normalized_identity_duplicate": bool(dup_identity),
            "semantic_relation": "UNKNOWN",
            "bundle_evidence_utf16_length": utf16_length(c.evidence_text),
            "current_direct_evidence_present": bool(c.current_direct_present),
            "current_user_evidence_would_be_overridden": override,
        }

    # -- decision ----------------------------------------------------------- #
    def evaluate(self, c: ProductionConsumptionCandidateV0_3) -> ConsumptionDecisionV0_3:
        f = self.extract_features(c)
        trace: List[str] = []
        decision, reason = RUNTIME_DEFAULT, REASON_NO_EVIDENCE      # gate 9 default
        units = c.units

        trace.append("gate=1 HARD_INTEGRITY_GATE")
        if not units or not all(u.lineage_valid for u in units) or not c.memory_id:
            decision, reason = "REJECT_INTEGRITY", REASON_INTEGRITY
        else:
            trace.append("gate=2 SOURCE_TRACE_GATE")
            if not (f["all_units_have_provenance"] and f["all_units_have_source_ref"]
                    and f["all_units_have_timestamp"] and f["all_units_have_source_role"]):
                decision, reason = "REJECT_INTEGRITY", REASON_SOURCE_TRACE
            else:
                trace.append("gate=3 AUTHORITY_PROVENANCE_SAFETY_GATE")
                if not f["authority_classes_permitted"]:
                    decision, reason = "ABSTAIN", REASON_AUTHORITY_CLASS
                elif f["bundle_authority_promotion_attempted"]:
                    decision, reason = "ABSTAIN", REASON_AUTHORITY_PROMOTION
                else:
                    trace.append("gate=4 CURRENT_INSTRUCTION_FIREWALL_GATE")
                    if f["any_unit_instructional_force"] or f["any_unit_authority_promoted"]:
                        decision, reason = "ABSTAIN", REASON_INSTRUCTION
                    else:
                        trace.append("gate=5 KNOWN_SUPERSESSION_INVALIDATION_GATE")
                        if f["known_supersession"] or f["known_invalidation"] or f["known_retraction"]:
                            decision, reason = "ABSTAIN", REASON_SUPERSEDED
                        else:
                            trace.append("gate=6 EXACT_IDENTITY_REDUNDANCY_GATE")
                            if f["visible_exact_event_id_overlap"]:
                                decision, reason = "SKIP_REDUNDANT", REASON_REDUNDANT_EVENT
                            elif f["episode_already_visible"]:
                                decision, reason = "SKIP_REDUNDANT", REASON_REDUNDANT_EPISODE
                            elif f["exact_normalized_identity_duplicate"]:
                                decision, reason = "SKIP_REDUNDANT", REASON_REDUNDANT_IDENTITY
                            else:
                                trace.append("gate=7 BUNDLE_SAFETY_GATE")
                                st = f["bundle_safety_status"]
                                if st == "MISSING_PROVENANCE":
                                    decision, reason = "REJECT_INTEGRITY", REASON_MISSING_PROVENANCE
                                elif st == "UNSAFE_INSTRUCTIONAL_CONTENT":
                                    decision, reason = "ABSTAIN", REASON_UNSAFE_BUNDLE
                                elif st == "UNKNOWN":
                                    decision, reason = "ABSTAIN", REASON_BUNDLE_UNKNOWN
                                elif (st == "MIXED_AUTHORITY"
                                      and not f["per_evidence_authority_preserved_by_provider"]):
                                    decision, reason = "ABSTAIN", REASON_MIXED_UNPRESERVABLE
                                else:
                                    trace.append("gate=8 CONSUME_GATE")
                                    if st == "MIXED_AUTHORITY":
                                        decision, reason = "CONSUME", REASON_SAFE_MIXED
                                    elif f["current_user_evidence_would_be_overridden"]:
                                        decision, reason = "CONSUME", REASON_SAFE_HISTORICAL_OVER_CURRENT
                                    else:
                                        decision, reason = "CONSUME", REASON_SAFE

        return ConsumptionDecisionV0_3(
            memory_id=c.memory_id, episode_id=c.episode_id, decision=decision,
            reason_codes=[reason], bundle_authority_classes=sorted({u.authority_class for u in units}),
            bundle_safety_status=f["bundle_safety_status"],
            policy_version=self.policy_version, policy_sha=self.policy_sha,
            rule_trace=trace, features=f,
            bundle_representation=[u.as_dict() for u in units],
            authority_promoted=False, semantic_relation="UNKNOWN",
        )

    def evaluate_record(self, r: Dict[str, Any]) -> ConsumptionDecisionV0_3:
        return self.evaluate(ProductionConsumptionCandidateV0_3.from_record(r))

    def provider_visible(self, d: ConsumptionDecisionV0_3) -> Dict[str, Any]:
        return d.to_dict(include_internal=False)

    def bundle_representation(self, c: ProductionConsumptionCandidateV0_3) -> List[Dict[str, Any]]:
        """The provider-visible Historical Evidence Bundle (per-unit authority kept)."""
        return [u.as_dict() for u in c.units]


def implementation_sha256() -> str:
    return hashlib.sha256(pathlib.Path(__file__).read_bytes()).hexdigest()


def policy_sha256(path: Optional[pathlib.Path] = None) -> str:
    p = path or (pathlib.Path(__file__).resolve().parent.parent / "artifacts" / POLICY_FILE)
    return hashlib.sha256(pathlib.Path(p).read_bytes()).hexdigest()


def evaluator_contract() -> Dict[str, Any]:
    return {
        "schema": "chiyo-production-consumption-evaluator-contract-v0-3",
        "policy_id": POLICY_ID, "policy_version": POLICY_VERSION,
        "predecessors": [
            {"policy_id": "CANDIDATE_CONSUMPTION_POLICY_V0", "sha256": V0_1_POLICY_SHA256,
             "status": V0_1_STATUS},
            {"policy_id": "CANDIDATE_CONSUMPTION_POLICY_V0_2", "sha256": V0_2_POLICY_SHA256,
             "status": V0_2_STATUS},
        ],
        "implementation": "ProductionConsumptionEvaluatorV0_3",
        "input_contract": "ProductionConsumptionCandidateV0_3",
        "output_contract": "ConsumptionDecisionV0_3",
        "policy_unit": "EvidenceBundle (one Episode + its persisted evidence events)",
        "claim_as_required_unit": False,
        "decision_enum": list(DECISION_ENUM), "runtime_default": RUNTIME_DEFAULT,
        "gate_order": list(GATE_ORDER),
        "authority_classes": list(AUTHORITY_CLASSES),
        "authority_may_be_promoted": AUTHORITY_MAY_BE_PROMOTED,
        "bundle_safety_statuses": list(BUNDLE_SAFETY_STATUSES),
        "semantic_relation_values": list(SEMANTIC_RELATION_VALUES),
        "reason_code_enum": list(REASON_CODE_ENUM),
        "features": list(V0_3_FEATURES),
        "firewalls": FIREWALLS,
        "forbidden_inputs": [
            "canonical_label", "gt_label", "phase_n_final_consumption_decision",
            "phase_p_bundle_state", "reviewer_answer", "replay_status",
            "offline_relation_verdict", "sealed_relation_class", "case_id", "expected_result",
            "pair_specific_override", "memory_specific_override", "event_specific_override",
        ],
        "offline_dependency_counts": {
            "offline_relation_verdict": 0, "claim_splitter": 0, "gt_metadata": 0,
            "phase_n_decision": 0, "phase_p_bundle_state": 0,
        },
        "invariants": {
            "provider_visible_fields": ["memory_id", "episode_id", "decision",
                                        "policy_version", "policy_sha"],
            "semantic_ambiguity_is_not_authority_ambiguity": True,
            "model_sees_it_does_not_mean_model_must_mention_it": True,
            "memory_to_context_enabled": False,
        },
    }
