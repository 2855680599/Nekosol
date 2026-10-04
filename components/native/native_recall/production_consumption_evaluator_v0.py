"""ProductionConsumptionEvaluatorV0 -- the single canonical executable realization of
CANDIDATE_CONSUMPTION_POLICY_V0.1 (frozen).

ONE POLICY / ONE SEMANTICS / ONE PRODUCTION IMPLEMENTATION.

The decision procedure in `evaluate()` is a line-for-line realization of the frozen
policy's `ordered_rules` (orders 1..7) and of the sealed canonical generator's
decision cascade.  The feature derivation in `extract_features()` is the sealed
canonical generator's `ExtractFeatures`, reproduced with exact .NET string
semantics (UTF-16 code units, .NET `\\s`/`\\p{P}`/`\\p{S}` character classes).

The frozen policy JSON is NOT modified by this module and is never rewritten.
"""
from __future__ import annotations

import dataclasses
import datetime as _dt
import hashlib
import json
import re
import unicodedata
from typing import Any, Dict, List, Optional, Sequence

POLICY_SHA256 = "5c6ba70a160cbe4fa880a91caab47bf548138750bc1257d7daa9764a6a4679b7"
POLICY_VERSION = "CANDIDATE_CONSUMPTION_POLICY_V0"
SELECTED_CANDIDATE_VERSION = "v0.1"

DECISION_ENUM = ("CONSUME", "SKIP_REDUNDANT", "ABSTAIN", "REJECT_INTEGRITY")
RUNTIME_DEFAULT = "ABSTAIN"

# reason codes actually emitted by this implementation (subset of the frozen enum)
REASON_SAFE = "SAFE_USEFUL_PAST_BACKGROUND"
REASON_REDUNDANT = "FULLY_REDUNDANT_VISIBLE_HISTORY"
REASON_RISK = "HISTORICAL_CURRENT_COLLAPSE_RISK"
REASON_INSUFFICIENT = "INSUFFICIENT_SAFE_EVIDENCE"
REASON_INTEGRITY = "INTEGRITY_FAILURE"
REASON_SOURCE_TRACE = "SOURCE_TRACE_INCOMPLETE"

ALLOWED_FEATURES = (
    "integrity_lineage_valid", "source_trace_complete", "evidence_character_count",
    "visible_history_message_count", "visible_history_available", "speaker_scope",
    "subject_scope", "provenance_basis", "epistemic_state", "historical_current_risk",
    "style_authority_risk", "behavior_authority_risk", "instruction_authority_risk",
    "relationship_state_risk", "assistant_to_user_promotion_risk", "contradiction_risk",
    "semantic_overlap_score", "exact_duplicate", "semantic_duplicate",
    "semantic_delta_available",
)

FORBIDDEN_FEATURES = (
    "gt_label", "reviewer_a_label", "reviewer_b_label", "reviewer_c_label", "gt_confidence",
    "gt_reason_code", "gt_adjudication", "phase_b_evaluation_label",
    "phase_n_final_consumption_decision", "phase_p_bundle_state", "fresh_holdout_label",
    "fresh_holdout_metric", "pair_specific_override", "memory_specific_override",
    "event_specific_override", "replay_status", "phase_o_replay_status", "bundle_state",
    "expected_label", "fixture_answer", "ground_truth",
)

RISK_KEYS = (
    "historical_current_risk", "style_authority_risk", "behavior_authority_risk",
    "instruction_authority_risk", "relationship_state_risk",
    "assistant_to_user_promotion_risk", "contradiction_risk",
)

SEMANTIC_OVERLAP_THRESHOLD = 0.56      # frozen: ordered_rules order 5
INSUFFICIENT_EVIDENCE_THRESHOLD = 12   # frozen: ordered_rules order 3


# --------------------------------------------------------------------------- #
# exact .NET character-class semantics ([\\s\\p{P}\\p{S}] over UTF-16 code units)
# --------------------------------------------------------------------------- #
_WS = {"\f", "\n", "\r", "\t", "\v", "\u0085",           # .NET \s literal members
       "\u0020", "\u00a0", "\u1680", "\u2028", "\u2029", "\u202f", "\u205f", "\u3000"}
_WS |= {chr(c) for c in range(0x2000, 0x200B)}           # \p{Zs} remainder


def _drop_unit(cu: int) -> bool:
    if 0xD800 <= cu <= 0xDFFF:      # surrogate -> Unicode category Cs -> NOT \\p{P}/\\p{S}
        return False
    ch = chr(cu)
    if ch in _WS:
        return True
    return unicodedata.category(ch)[0] in ("P", "S")


def _to_utf16(text: str) -> List[int]:
    b = text.encode("utf-16-le", errors="surrogatepass")
    return [b[i] | (b[i + 1] << 8) for i in range(0, len(b), 2)]


def _from_utf16(units: Sequence[int]) -> str:
    b = bytearray()
    for u in units:
        b.append(u & 0xFF)
        b.append((u >> 8) & 0xFF)
    return bytes(b).decode("utf-16-le", errors="surrogatepass")


def normalize(text: Optional[str]) -> str:
    if text is None:
        return ""
    units = _to_utf16(text.lower())
    return _from_utf16([u for u in units if not _drop_unit(u)])


def token_set(text: Optional[str]) -> List[int]:
    n = normalize(text)
    if n == "":
        return []
    seen, out = set(), []
    for u in _to_utf16(n):
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


def overlap(evidence: str, visible: str) -> float:
    a, b = token_set(evidence), token_set(visible)
    if not a:
        return 0.0
    bs = set(b)
    return sum(1 for u in a if u in bs) / float(len(a))


def utf16_length(text: str) -> int:
    return len(_to_utf16(text))


RE_CONFLICT = re.compile(r"(但是|不过|相反|其实不是|矛盾|contradict|冲突)", re.I)
RE_STYLE = re.compile(r"(语气|风格|嘴毒|傲娇|撒娇|颜文字|猫娘|roleplay|style|teasing|playful)", re.I)
RE_BEHAVIOR = re.compile(r"(下次|以后|记住|别忘|你得|你要|必须|always|must|promise)", re.I)
RE_INSTRUCTION = re.compile(r"(命令|指令|要求你|请你|应该|do not|dont)", re.I)
RE_RELATIONSHIP = re.compile(r"(同居|当前关系|我们是|你是我的女朋友|你是我的男朋友|我爱你|爱我)", re.I)
RE_ASSISTANT_TO_USER = re.compile(r"(你喜欢|你的偏好|你一直|你总是|按你的偏好)", re.I)
RE_CURRENT = re.compile(r"(现在|正在|今天|目前|刚刚|now|current|today)", re.I)


def _blank(v: Any) -> bool:
    return v is None or (isinstance(v, str) and v.strip() == "")


def _iso_cn(ts: Any) -> Any:
    if not isinstance(ts, str) or not ts:
        return ts
    s = ts[:-1] + "+00:00" if ts.endswith("Z") else ts
    try:
        d = _dt.datetime.fromisoformat(s)
    except ValueError:
        return ts
    return d.astimezone(_dt.timezone(_dt.timedelta(hours=8))).isoformat() if d.tzinfo else ts


# --------------------------------------------------------------------------- #
# 4. INPUT CONTRACT
# --------------------------------------------------------------------------- #
@dataclasses.dataclass(frozen=True)
class ConsumptionCandidate:
    """The only input the production evaluator accepts.

    Every field is either supplied by the production pipeline or is an explicitly
    declared policy feature input.  Ground-truth / replay metadata is NEVER read.
    """
    memory_id: str
    current_user_turn: str
    visible_history: Sequence[Dict[str, Any]]
    candidate_historical_evidence: Sequence[Dict[str, Any]]
    speaker_provenance: Dict[str, Any]
    temporal_information: Optional[Dict[str, Any]] = None
    candidate_capsule: Optional[Dict[str, Any]] = None
    source_event_ids: Sequence[str] = ()
    lineage_status: str = "FULL"
    authority_class: str = "PAST_EVIDENCE"
    provenance_class: Optional[str] = None
    claims: Sequence[str] = ()
    surface_result: Optional[str] = None
    relevance_evidence: Sequence[Any] = ()
    supersession_evidence: Sequence[Any] = ()
    contradiction_evidence: Sequence[Any] = ()

    # ---- helpers ---------------------------------------------------------- #
    @property
    def evidence_text(self) -> str:
        return " ".join((e.get("exact_source_text") or "")
                        for e in self.candidate_historical_evidence)

    @property
    def visible_text(self) -> str:
        return (" ".join((m.get("text") or "") for m in self.visible_history)
                + " " + str(self.current_user_turn))

    @property
    def lineage_valid(self) -> bool:
        return str(self.lineage_status).upper() in ("FULL", "VALID", "PASS")

    def as_candidate_dict(self) -> Dict[str, Any]:
        return {
            "memory_id": self.memory_id,
            "source_event_ids": list(self.source_event_ids),
            "lineage_status": self.lineage_status,
            "authority_class": self.authority_class,
            "provenance_class": self.provenance_class,
            "claims": list(self.claims),
            "surface_result": self.surface_result,
            "relevance_evidence": list(self.relevance_evidence),
            "supersession_evidence": list(self.supersession_evidence),
            "contradiction_evidence": list(self.contradiction_evidence),
        }

    @classmethod
    def from_packet(cls, packet: Dict[str, Any], **overrides) -> "ConsumptionCandidate":
        """Build a candidate from the sealed blind-packet / fixture schema."""
        sp = packet.get("speaker_provenance") or {}
        cap = packet.get("candidate_capsule") or {}
        kw = dict(
            memory_id=packet.get("candidate_memory_id") or packet.get("memory_id") or "",
            current_user_turn=packet.get("current_user_turn") or "",
            visible_history=packet.get("visible_history") or [],
            candidate_historical_evidence=packet.get("candidate_historical_evidence") or [],
            speaker_provenance=sp,
            temporal_information=packet.get("temporal_information"),
            candidate_capsule=cap,
            source_event_ids=[packet["event_id"]] if packet.get("event_id") else [],
            lineage_status="FULL",
            authority_class=cap.get("authority_class", "PAST_EVIDENCE"),
            provenance_class=cap.get("provenance_basis") or sp.get("basis"),
            claims=[c.get("exact_source_text", "") for c in (packet.get("candidate_historical_evidence") or [])],
        )
        kw.update(overrides)
        return cls(**kw)


# --------------------------------------------------------------------------- #
# 5. OUTPUT CONTRACT
# --------------------------------------------------------------------------- #
@dataclasses.dataclass(frozen=True)
class ConsumptionDecision:
    memory_id: str
    decision: str
    reason_codes: Sequence[str]
    authority_class: str
    lineage_status: str
    policy_version: str
    policy_sha: str
    rule_trace: Sequence[str] = ()
    features: Optional[Dict[str, Any]] = None
    forbidden_features_used: Sequence[str] = ()

    def to_dict(self, include_internal: bool = True) -> Dict[str, Any]:
        d = {
            "memory_id": self.memory_id,
            "decision": self.decision,
            "reason_codes": list(self.reason_codes),
            "authority_class": self.authority_class,
            "lineage_status": self.lineage_status,
            "policy_version": self.policy_version,
            "policy_sha": self.policy_sha,
        }
        if not include_internal:
            # reason_codes / rule traces / scores must not reach a provider-visible capsule
            return {k: d[k] for k in ("memory_id", "decision", "policy_version", "policy_sha")}
        d["rule_trace"] = list(self.rule_trace)
        d["features"] = dict(self.features or {})
        d["forbidden_features_used"] = list(self.forbidden_features_used)
        return d


# --------------------------------------------------------------------------- #
# 3. SPEC -> CODE : the executable realization
# --------------------------------------------------------------------------- #
class ProductionConsumptionEvaluatorV0:
    """The single production implementation of the frozen consumption policy."""

    policy_version = POLICY_VERSION
    policy_sha = POLICY_SHA256

    # -- feature derivation (policy feature_lineage -> executable function) -- #
    def extract_features(self, c: ConsumptionCandidate) -> Dict[str, Any]:
        et, vt = c.evidence_text, c.visible_text
        sp = c.speaker_provenance or {}
        scope_mixed = str(sp.get("speaker_scope")) == "mixed"

        trace_complete = (
            len(c.candidate_historical_evidence) > 0
            and sum(1 for e in c.candidate_historical_evidence
                    if _blank(e.get("source_ref")) or e.get("line_number") is None
                    or _blank(e.get("exact_source_text")) or _blank(e.get("role"))
                    or _blank(e.get("timestamp"))) == 0
            and c.temporal_information is not None
        )
        ov = round(overlap(et, vt), 6)
        semantic_dup = ov >= SEMANTIC_OVERLAP_THRESHOLD
        ne, nv = normalize(et), normalize(vt)

        return {
            "integrity_lineage_valid": bool(c.lineage_valid),
            "source_trace_complete": bool(trace_complete),
            "evidence_character_count": utf16_length(et),
            "visible_history_message_count": len(c.visible_history),
            "visible_history_available": len(c.visible_history) > 0,
            "speaker_scope": sp.get("speaker_scope"),
            "subject_scope": sp.get("subject_scope"),
            "provenance_basis": sp.get("basis"),
            "epistemic_state": sp.get("epistemic_state"),
            "historical_current_risk": bool(RE_CURRENT.search(et)),
            "style_authority_risk": bool(RE_STYLE.search(et)) and scope_mixed,
            "behavior_authority_risk": bool(RE_BEHAVIOR.search(et)),
            "instruction_authority_risk": bool(RE_INSTRUCTION.search(et)),
            "relationship_state_risk": bool(RE_RELATIONSHIP.search(et)),
            "assistant_to_user_promotion_risk": bool(RE_ASSISTANT_TO_USER.search(et)) and scope_mixed,
            "contradiction_risk": bool(RE_CONFLICT.search(et)),
            "semantic_overlap_score": ov,
            "exact_duplicate": bool(len(ne) > 0 and ne in nv),
            "semantic_duplicate": bool(semantic_dup),
            "semantic_delta_available": (not semantic_dup),
        }

    # -- decision ----------------------------------------------------------- #
    def evaluate(self, c: ConsumptionCandidate) -> ConsumptionDecision:
        f = self.extract_features(c)
        trace: List[str] = []
        decision, reason = RUNTIME_DEFAULT, REASON_INSUFFICIENT     # rule 7 default

        # rule 1 HARD_INTEGRITY_GATE
        trace.append("order=1 HARD_INTEGRITY_GATE")
        if not f["integrity_lineage_valid"]:
            decision, reason = "REJECT_INTEGRITY", REASON_INTEGRITY
        else:
            # rule 2 SOURCE_TRACE_GATE
            trace.append("order=2 SOURCE_TRACE_GATE")
            if not f["source_trace_complete"]:
                decision, reason = "REJECT_INTEGRITY", REASON_SOURCE_TRACE
            else:
                # rule 3 INSUFFICIENT_EVIDENCE_GATE
                trace.append("order=3 INSUFFICIENT_EVIDENCE_GATE")
                if f["evidence_character_count"] < INSUFFICIENT_EVIDENCE_THRESHOLD:
                    decision, reason = "ABSTAIN", REASON_INSUFFICIENT
                else:
                    # rule 4 CLAIM_AND_AUTHORITY_GATE
                    trace.append("order=4 CLAIM_AND_AUTHORITY_GATE")
                    if any(f[k] for k in RISK_KEYS):
                        decision, reason = "ABSTAIN", REASON_RISK
                    else:
                        # rule 5 REDUNDANCY_GATE
                        trace.append("order=5 REDUNDANCY_GATE")
                        if f["exact_duplicate"] or f["semantic_duplicate"]:
                            decision, reason = "SKIP_REDUNDANT", REASON_REDUNDANT
                        else:
                            # rule 6 CONSUME_GATE
                            trace.append("order=6 CONSUME_GATE")
                            if f["semantic_delta_available"]:
                                decision, reason = "CONSUME", REASON_SAFE
                            else:
                                # rule 7 FAIL_CLOSED_DEFAULT
                                trace.append("order=7 FAIL_CLOSED_DEFAULT")
                                decision, reason = "ABSTAIN", REASON_INSUFFICIENT

        used = sorted(set(f) & set(FORBIDDEN_FEATURES))
        return ConsumptionDecision(
            memory_id=c.memory_id, decision=decision, reason_codes=[reason],
            authority_class=c.authority_class, lineage_status=c.lineage_status,
            policy_version=self.policy_version, policy_sha=self.policy_sha,
            rule_trace=trace, features=f, forbidden_features_used=used,
        )

    # -- convenience -------------------------------------------------------- #
    def evaluate_packet(self, packet: Dict[str, Any]) -> ConsumptionDecision:
        return self.evaluate(ConsumptionCandidate.from_packet(packet))

    def provider_visible(self, d: ConsumptionDecision) -> Dict[str, Any]:
        """Nothing but the allow-listed fields may reach a Capsule."""
        return d.to_dict(include_internal=False)


class LegacyReplayLabelEvaluator:
    """HISTORICAL REPLAY COMPATIBILITY ONLY -- NOT a production evaluator.

    This is the drifted `CandidatePolicy.decision_for(replay label)` behaviour that
    consumed `phase_o_replay_status`.  It is retained solely to replay historical
    artifacts and is explicitly excluded from the production decision path.
    """

    production = False

    @staticmethod
    def decision_for(replay_label: str) -> str:
        return {"PAIRED_REPLAY_COMPLETE": "CONSUME",
                "DEFERRED_NOT_INJECTED": "ABSTAIN"}.get(replay_label, "ABSTAIN")


def implementation_sha256() -> str:
    return hashlib.sha256(open(__file__, "rb").read()).hexdigest()


def policy_sha256() -> str:
    return POLICY_SHA256


def evaluator_contract() -> Dict[str, Any]:
    return {
        "schema": "chiyo-production-consumption-evaluator-contract-v0",
        "policy_version": POLICY_VERSION,
        "policy_sha256": POLICY_SHA256,
        "selected_candidate_version": SELECTED_CANDIDATE_VERSION,
        "implementation": "ProductionConsumptionEvaluatorV0",
        "input_contract": "ConsumptionCandidate",
        "output_contract": "ConsumptionDecision",
        "decision_enum": list(DECISION_ENUM),
        "runtime_default": RUNTIME_DEFAULT,
        "allowed_features": list(ALLOWED_FEATURES),
        "forbidden_features": list(FORBIDDEN_FEATURES),
        "ordered_rules": [
            {"order": 1, "rule": "HARD_INTEGRITY_GATE", "effect": "REJECT_INTEGRITY/INTEGRITY_FAILURE",
             "function": "ProductionConsumptionEvaluatorV0.extract_features.integrity_lineage_valid"},
            {"order": 2, "rule": "SOURCE_TRACE_GATE", "effect": "REJECT_INTEGRITY/SOURCE_TRACE_INCOMPLETE",
             "function": "ProductionConsumptionEvaluatorV0.extract_features.source_trace_complete"},
            {"order": 3, "rule": "INSUFFICIENT_EVIDENCE_GATE", "effect": "ABSTAIN/INSUFFICIENT_SAFE_EVIDENCE",
             "function": "evidence_character_count < 12 (UTF-16 code units)"},
            {"order": 4, "rule": "CLAIM_AND_AUTHORITY_GATE", "effect": "ABSTAIN/HISTORICAL_CURRENT_COLLAPSE_RISK",
             "function": "any(authority_risks)"},
            {"order": 5, "rule": "REDUNDANCY_GATE", "effect": "SKIP_REDUNDANT/FULLY_REDUNDANT_VISIBLE_HISTORY",
             "function": "exact_duplicate or semantic_duplicate (overlap >= 0.56)"},
            {"order": 6, "rule": "CONSUME_GATE", "effect": "CONSUME/SAFE_USEFUL_PAST_BACKGROUND",
             "function": "semantic_delta_available"},
            {"order": 7, "rule": "FAIL_CLOSED_DEFAULT", "effect": "ABSTAIN/INSUFFICIENT_SAFE_EVIDENCE",
             "function": "fallthrough"},
        ],
        "invariants": {
            "GROUND_TRUTH_METADATA_IN_PRODUCTION_DECISION": 0,
            "REPLAY_LABEL_READS": 0,
            "provider_visible_fields": ["memory_id", "decision", "policy_version", "policy_sha"],
        },
    }
