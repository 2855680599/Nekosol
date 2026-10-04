from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
import hashlib
import json
import os
import re
import time
import uuid
from typing import Any, Iterable

from .canary import CanaryConfig, CanaryDecision, ScopedCanaryGate


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


class ContextType(str, Enum):
    STABLE_SYSTEM = "STABLE_SYSTEM"
    CURRENT_USER = "CURRENT_USER"
    RECENT_EXACT_HISTORY = "RECENT_EXACT_HISTORY"
    CONVERSATION_DIGEST = "CONVERSATION_DIGEST"
    EPHEMERAL_NATIVE_MEMORY = "EPHEMERAL_NATIVE_MEMORY"
    TOOL_RESULT = "TOOL_RESULT"
    RUNTIME_STATE = "RUNTIME_STATE"
    LEGACY_MEMORY = "LEGACY_MEMORY"
    INTERNAL_AUDIT_ONLY = "INTERNAL_AUDIT_ONLY"


class Authority(str, Enum):
    DIRECT_USER_EVIDENCE = "DIRECT_USER_EVIDENCE"
    TOOL_OBSERVED_EVIDENCE = "TOOL_OBSERVED_EVIDENCE"
    VERIFIED_EXECUTION_OUTCOME = "VERIFIED_EXECUTION_OUTCOME"
    ASSISTANT_SELF_HISTORY = "ASSISTANT_SELF_HISTORY"
    DERIVED_MEMORY = "DERIVED_MEMORY"
    SUMMARY_OR_INTERPRETATION = "SUMMARY_OR_INTERPRETATION"
    UNKNOWN = "UNKNOWN"


class Persistence(str, Enum):
    REQUEST_ONLY = "REQUEST_ONLY"
    DURABLE = "DURABLE"
    AUDIT_ONLY = "AUDIT_ONLY"


class Eligibility(str, Enum):
    ALLOWED = "ALLOWED"
    FORBIDDEN = "FORBIDDEN"


class Visibility(str, Enum):
    PROVIDER_VISIBLE = "PROVIDER_VISIBLE"
    PROVIDER_HIDDEN = "PROVIDER_HIDDEN"


@dataclass(frozen=True)
class Namespace:
    user: str
    session: str
    conversation: str
    turn: str

    def __post_init__(self) -> None:
        for value in (self.user, self.session, self.conversation, self.turn):
            if not isinstance(value, str) or not value or len(value) > 160:
                raise ValueError("namespace component is invalid")

    def as_dict(self) -> dict[str, str]:
        return asdict(self)


@dataclass(frozen=True)
class ContextItem:
    context_id: str
    context_type: ContextType
    content: str
    source_ids: tuple[str, ...]
    authority: Authority
    created_at: str
    persistence: Persistence
    history_eligibility: Eligibility
    formation_eligibility: Eligibility
    provider_visibility: Visibility
    request_scope: str
    namespace: Namespace
    ttl_seconds: int | None = None

    def __post_init__(self) -> None:
        if not self.context_id or not isinstance(self.content, str):
            raise ValueError("context item identity/content is invalid")
        if not self.source_ids:
            raise ValueError("context item needs source ids")
        if self.context_type is ContextType.CURRENT_USER:
            if self.authority is not Authority.DIRECT_USER_EVIDENCE:
                raise ValueError("current user must be direct user evidence")
        if self.context_type is ContextType.EPHEMERAL_NATIVE_MEMORY:
            if self.history_eligibility is not Eligibility.FORBIDDEN:
                raise ValueError("ephemeral memory cannot enter history")
            if self.formation_eligibility is not Eligibility.FORBIDDEN:
                raise ValueError("ephemeral memory cannot enter formation")
            if self.persistence is not Persistence.REQUEST_ONLY:
                raise ValueError("ephemeral memory is request-only")
        if self.context_type is ContextType.CONVERSATION_DIGEST:
            if self.authority is not Authority.SUMMARY_OR_INTERPRETATION:
                raise ValueError("digest authority is summary")
            if self.history_eligibility is not Eligibility.FORBIDDEN:
                raise ValueError("digest cannot become dialogue history")
            if self.formation_eligibility is not Eligibility.FORBIDDEN:
                raise ValueError("digest cannot enter formation")
        if self.context_type is ContextType.INTERNAL_AUDIT_ONLY:
            if self.provider_visibility is not Visibility.PROVIDER_HIDDEN:
                raise ValueError("audit context cannot be provider visible")


@dataclass(frozen=True)
class ConversationRound:
    round_id: str
    namespace: Namespace
    user_event_id: str
    user_text: str
    assistant_event_id: str | None
    assistant_text: str | None
    tool_chains: tuple[tuple[dict[str, Any], ...], ...] = ()

    def validate(self) -> None:
        if not self.round_id or not self.user_event_id or not self.user_text:
            raise ValueError("conversation round must preserve an exact user event")
        for chain in self.tool_chains:
            if len(chain) < 2 or len(chain) % 2:
                raise ValueError("tool chain must contain call/result pairs")
            for index, item in enumerate(chain):
                expected = "tool_call" if index % 2 == 0 else "tool_result"
                if item.get("kind") != expected:
                    raise ValueError("tool chain is not atomic")

    def source_ids(self) -> tuple[str, ...]:
        ids = [self.user_event_id]
        if self.assistant_event_id:
            ids.append(self.assistant_event_id)
        for chain in self.tool_chains:
            ids.extend(str(item["id"]) for item in chain if item.get("id"))
        return tuple(ids)


@dataclass(frozen=True)
class ConversationDigest:
    digest_id: str
    source_round_ids: tuple[str, ...]
    source_event_ids: tuple[str, ...]
    source_hashes: tuple[str, ...]
    summary_text: str
    generated_at: str
    generator_model: str
    digest_version: str
    source_availability: str
    authority: Authority = Authority.SUMMARY_OR_INTERPRETATION
    history_role: str = "NONE"
    formation_eligible: Eligibility = Eligibility.FORBIDDEN
    memory_eligible: Eligibility = Eligibility.FORBIDDEN

    def validate(self, available_round_ids: set[str], available_event_ids: set[str]) -> None:
        if self.authority is not Authority.SUMMARY_OR_INTERPRETATION:
            raise ValueError("digest authority changed")
        if self.history_role != "NONE":
            raise ValueError("digest cannot be a dialogue role")
        if self.formation_eligible is not Eligibility.FORBIDDEN:
            raise ValueError("digest cannot enter formation")
        if self.memory_eligible is not Eligibility.FORBIDDEN:
            raise ValueError("digest cannot become memory")
        missing_rounds = set(self.source_round_ids) - available_round_ids
        missing_events = set(self.source_event_ids) - available_event_ids
        if missing_rounds or missing_events:
            if self.source_availability not in {"PARTIAL", "UNAVAILABLE"}:
                raise ValueError("digest has fabricated lineage")


@dataclass(frozen=True)
class MemoryObject:
    memory_id: str
    text: str
    authority: Authority
    source_event_ids: tuple[str, ...]
    namespace: Namespace
    uncertainty: str = "NONE"
    valid_from: str | None = None
    valid_until: str | None = None
    supersedes: tuple[str, ...] = ()

    def eligible(self) -> bool:
        return self.authority in {
            Authority.DIRECT_USER_EVIDENCE,
            Authority.TOOL_OBSERVED_EVIDENCE,
            Authority.VERIFIED_EXECUTION_OUTCOME,
        } and bool(self.source_event_ids)


@dataclass(frozen=True)
class EvidenceTrace:
    trace_id: str
    memory_id: str
    source_event_ids: tuple[str, ...]
    stages: tuple[str, ...]
    lineage_status: str = "COMPLETE"

    def validate(self) -> None:
        required = (
            "raw_event", "formation", "memory_object", "recall", "surface",
            "consumption", "evidence_envelope", "ephemeral_capsule",
            "request_manifest", "provider_request",
        )
        if self.lineage_status not in {"COMPLETE", "LINEAGE_PARTIAL"}:
            raise ValueError("invalid lineage status")
        if self.lineage_status == "COMPLETE" and any(stage not in self.stages for stage in required):
            raise ValueError("evidence trace is incomplete")


@dataclass
class RequestContextManifest:
    request_id: str
    session_id: str
    conversation_id: str
    turn_id: str
    namespace: Namespace
    current_user_event_id: str
    exact_history_round_ids: list[str] = field(default_factory=list)
    conversation_digest_ids: list[str] = field(default_factory=list)
    surfaced_memory_ids: list[str] = field(default_factory=list)
    consumed_memory_ids: list[str] = field(default_factory=list)
    deferred_memory_ids: list[str] = field(default_factory=list)
    capsule_ids: list[str] = field(default_factory=list)
    tool_chain_ids: list[str] = field(default_factory=list)
    context_budget: dict[str, int] = field(default_factory=dict)
    estimated_tokens: int = 0
    legacy_memory_state: str = "DISABLED"
    native_memory_state: str = "SHADOW"
    double_authority_detected: bool = False
    double_injection_detected: bool = False
    formation_eligible_source_ids: list[str] = field(default_factory=list)
    created_at: str = field(default_factory=now_iso)

    def provider_visible(self) -> dict[str, Any]:
        value = asdict(self)
        value.pop("namespace", None)
        value.pop("current_user_event_id", None)
        return {"manifest_id": self.request_id, "provider_visible": False}


@dataclass
class EphemeralCapsule:
    capsule_id: str
    request_id: str
    memory_ids: list[str]
    contents: list[str]
    created_at: str = field(default_factory=now_iso)
    invalidated_at: str | None = None

    def invalidate(self) -> None:
        self.invalidated_at = now_iso()
        self.contents.clear()

    @property
    def valid(self) -> bool:
        return self.invalidated_at is None


@dataclass(frozen=True)
class RuntimeRequest:
    user: str
    session: str
    conversation: str
    turn: str
    user_event_id: str
    user_text: str
    history: tuple[ConversationRound, ...] = ()
    digests: tuple[ConversationDigest, ...] = ()
    memories: tuple[MemoryObject, ...] = ()
    tool_chain_ids: tuple[str, ...] = ()
    legacy_authoritative: bool = False
    legacy_visible: bool = False
    shadow: bool = True

    @property
    def namespace(self) -> Namespace:
        return Namespace(self.user, self.session, self.conversation, self.turn)


@dataclass
class ShadowResult:
    manifest: RequestContextManifest
    capsule: EphemeralCapsule | None
    hypothetical_context: list[dict[str, str]]
    provider_context: list[dict[str, str]]
    bypassed: bool
    bypass_reason: str | None
    traces: list[EvidenceTrace]
    metrics: dict[str, int]
    canary_decision: CanaryDecision | None = None


class KillSwitch:
    """The only live injection switch. Missing/invalid values fail closed."""

    ENV = "CHIYO_NATIVE_MEMORY_CONTEXT_ENABLED"

    @classmethod
    def enabled(cls, environ: dict[str, str] | None = None) -> bool:
        value = (environ or os.environ).get(cls.ENV)
        return value is not None and value.strip().lower() == "true"


class FormationFirewall:
    ALLOWED_SOURCES = {
        "real_current_user_event",
        "real_platform_event",
        "verified_tool_result",
        "explicit_authoritative_evidence",
    }

    def __init__(self) -> None:
        self.blocked = 0

    def admit(self, source_kind: str, source_ids: Iterable[str]) -> bool:
        if source_kind not in self.ALLOWED_SOURCES or not tuple(source_ids):
            self.blocked += 1
            return False
        return True

    def block_recall_writeback(self) -> bool:
        self.blocked += 1
        return False


class ContextBudgetManager:
    PRIORITY = (
        ContextType.STABLE_SYSTEM,
        ContextType.CURRENT_USER,
        ContextType.TOOL_RESULT,
        ContextType.RECENT_EXACT_HISTORY,
        ContextType.EPHEMERAL_NATIVE_MEMORY,
        ContextType.CONVERSATION_DIGEST,
        ContextType.RUNTIME_STATE,
    )

    def __init__(self, max_tokens: int = 4096) -> None:
        self.max_tokens = max(64, int(max_tokens))

    @staticmethod
    def estimate(messages: Iterable[dict[str, str]]) -> int:
        return max(1, sum(len(str(item.get("content", ""))) for item in messages) // 4)

    def fit(self, layers: list[ContextItem]) -> tuple[list[ContextItem], dict[str, Any]]:
        kept = list(layers)
        dropped: list[str] = []
        while self.estimate([{"content": item.content} for item in kept]) > self.max_tokens:
            candidate = next(
                (item for item in reversed(kept)
                 if item.context_type not in {ContextType.STABLE_SYSTEM, ContextType.CURRENT_USER, ContextType.TOOL_RESULT}),
                None,
            )
            if candidate is None:
                break
            kept.remove(candidate)
            dropped.append(candidate.context_id)
        if any(item.context_type is ContextType.CURRENT_USER for item in kept) is False:
            raise ValueError("budget manager dropped current user")
        return kept, {
            "estimated_tokens": self.estimate([{"content": item.content} for item in kept]),
            "dropped_context_ids": dropped,
            "reason_code": "SAFE_TRUNCATION" if dropped else "WITHIN_BUDGET",
        }


class Observability:
    NAMES = (
        "native_memory_context_requests_total",
        "native_memory_surface_total",
        "native_memory_consumed_total",
        "native_memory_deferred_total",
        "native_memory_capsule_total",
        "native_memory_context_bypass_total",
        "native_memory_double_authority_block_total",
        "native_memory_double_injection_block_total",
        "native_memory_history_leak_total",
        "native_memory_writeback_block_total",
        "context_compaction_total",
        "context_compaction_failure_total",
        "conversation_digest_total",
    )

    def __init__(self) -> None:
        self.counters = {name: 0 for name in self.NAMES}
        self.latency_ms: dict[str, list[float]] = {}

    def inc(self, name: str, amount: int = 1) -> None:
        if name not in self.counters:
            self.counters[name] = 0
        self.counters[name] += amount

    def observe(self, name: str, elapsed_ms: float) -> None:
        self.latency_ms.setdefault(name, []).append(round(elapsed_ms, 3))

    def snapshot(self) -> dict[str, Any]:
        return {"counters": dict(self.counters), "latency_ms": dict(self.latency_ms)}


class NativeMemoryRuntime:
    """Request-scoped memory composer. It never mutates MemoryObject inputs."""

    def __init__(self, max_tokens: int = 4096, live_switch: dict[str, str] | None = None, canary_gate: ScopedCanaryGate | None = None):
        self.budget = ContextBudgetManager(max_tokens)
        self.obs = Observability()
        self.firewall = FormationFirewall()
        self.live_switch = live_switch or {}
        self.canary_gate = canary_gate or ScopedCanaryGate(CanaryConfig.fail_closed())
        self.write_audit = {"writes": 0, "touch": 0, "reinforce": 0, "importance_mutation": 0, "accessibility_mutation": 0}

    def _memory_relevance(self, memory: MemoryObject, query: str) -> int:
        ascii_terms = {term.lower() for term in re.findall(r"[A-Za-z0-9]{2,}", query)}
        han = "".join(re.findall(r"[\u4e00-\u9fff]", query))
        han_terms = {han[index:index + 2] for index in range(max(0, len(han) - 1))}
        terms = ascii_terms | han_terms
        text = memory.text.lower()
        return sum(1 for term in terms if term in text)

    def _capsule(self, request: RuntimeRequest, manifest: RequestContextManifest) -> EphemeralCapsule:
        eligible = [m for m in request.memories if m.namespace == request.namespace and m.eligible()]
        ranked = sorted(eligible, key=lambda item: self._memory_relevance(item, request.user_text), reverse=True)
        surfaced = [item for item in ranked if self._memory_relevance(item, request.user_text) > 0]
        deferred = [item for item in ranked if item not in surfaced]
        manifest.surfaced_memory_ids.extend(item.memory_id for item in surfaced)
        manifest.consumed_memory_ids.extend(item.memory_id for item in surfaced)
        manifest.deferred_memory_ids.extend(item.memory_id for item in deferred)
        self.obs.inc("native_memory_surface_total", len(surfaced))
        self.obs.inc("native_memory_consumed_total", len(surfaced))
        self.obs.inc("native_memory_deferred_total", len(deferred))
        capsule = EphemeralCapsule(
            capsule_id="capsule-" + uuid.uuid4().hex,
            request_id=manifest.request_id,
            memory_ids=[item.memory_id for item in surfaced],
            contents=[item.text for item in surfaced],
        )
        manifest.capsule_ids.append(capsule.capsule_id)
        self.obs.inc("native_memory_capsule_total")
        return capsule

    def compose(self, request: RuntimeRequest) -> ShadowResult:
        started = time.monotonic()
        self.obs.inc("native_memory_context_requests_total")
        namespace = request.namespace
        for round_item in request.history:
            round_item.validate()
        available_rounds = {item.round_id for item in request.history}
        available_events = {event_id for item in request.history for event_id in item.source_ids()}
        for item in request.digests:
            item.validate(available_rounds, available_events)
            self.obs.inc("conversation_digest_total")

        manifest = RequestContextManifest(
            request_id="request-" + uuid.uuid4().hex,
            session_id=request.session,
            conversation_id=request.conversation,
            turn_id=request.turn,
            namespace=namespace,
            current_user_event_id=request.user_event_id,
            exact_history_round_ids=[item.round_id for item in request.history],
            conversation_digest_ids=[item.digest_id for item in request.digests],
            tool_chain_ids=list(request.tool_chain_ids),
            legacy_memory_state="AUTHORITATIVE" if request.legacy_authoritative else "DISABLED",
            native_memory_state="SHADOW" if request.shadow else "LIVE",
            formation_eligible_source_ids=[request.user_event_id],
        )

        if request.legacy_authoritative and request.legacy_visible and request.memories:
            manifest.double_authority_detected = True
            manifest.double_injection_detected = True
            self.obs.inc("native_memory_double_authority_block_total")
            self.obs.inc("native_memory_double_injection_block_total")
            self.obs.inc("native_memory_context_bypass_total")
            decision = self.canary_gate.decide(
                manifest.request_id, request.user, request.session, request.conversation,
                double_authority=True,
            )
            return ShadowResult(manifest, None, [], [], True, "DOUBLE_AUTHORITY", [], self.obs.snapshot(), decision)

        layers: list[ContextItem] = [
            ContextItem(
                context_id="current-user:" + request.user_event_id,
                context_type=ContextType.CURRENT_USER,
                content=request.user_text,
                source_ids=(request.user_event_id,),
                authority=Authority.DIRECT_USER_EVIDENCE,
                created_at=now_iso(),
                persistence=Persistence.DURABLE,
                history_eligibility=Eligibility.ALLOWED,
                formation_eligibility=Eligibility.ALLOWED,
                provider_visibility=Visibility.PROVIDER_VISIBLE,
                request_scope="CURRENT_REQUEST",
                namespace=namespace,
            )
        ]
        for round_item in request.history[-4:]:
            content = "user: " + round_item.user_text
            if round_item.assistant_text:
                content += "\nassistant: " + round_item.assistant_text
            layers.append(ContextItem(
                context_id="round:" + round_item.round_id,
                context_type=ContextType.RECENT_EXACT_HISTORY,
                content=content,
                source_ids=round_item.source_ids(),
                authority=Authority.DIRECT_USER_EVIDENCE,
                created_at=now_iso(),
                persistence=Persistence.DURABLE,
                history_eligibility=Eligibility.ALLOWED,
                formation_eligibility=Eligibility.FORBIDDEN,
                provider_visibility=Visibility.PROVIDER_VISIBLE,
                request_scope="CURRENT_REQUEST",
                namespace=namespace,
            ))

        capsule: EphemeralCapsule | None = None
        traces: list[EvidenceTrace] = []
        if request.memories:
            capsule = self._capsule(request, manifest)
            for memory_id in capsule.memory_ids:
                item = next(m for m in request.memories if m.memory_id == memory_id)
                trace = EvidenceTrace(
                    trace_id="trace-" + uuid.uuid4().hex,
                    memory_id=item.memory_id,
                    source_event_ids=item.source_event_ids,
                    stages=("raw_event", "formation", "memory_object", "recall", "surface", "consumption", "evidence_envelope", "ephemeral_capsule", "request_manifest", "provider_request"),
                )
                trace.validate()
                traces.append(trace)
                layers.append(ContextItem(
                    context_id="memory:" + item.memory_id,
                    context_type=ContextType.EPHEMERAL_NATIVE_MEMORY,
                    content=item.text,
                    source_ids=item.source_event_ids,
                    authority=item.authority,
                    created_at=now_iso(),
                    persistence=Persistence.REQUEST_ONLY,
                    history_eligibility=Eligibility.FORBIDDEN,
                    formation_eligibility=Eligibility.FORBIDDEN,
                    provider_visibility=Visibility.PROVIDER_VISIBLE,
                    request_scope="CURRENT_REQUEST",
                    namespace=namespace,
                ))

        fitted, budget_report = self.budget.fit(list(reversed(layers)))
        fitted = list(reversed(fitted))
        manifest.context_budget = budget_report
        manifest.estimated_tokens = budget_report["estimated_tokens"]
        if budget_report["dropped_context_ids"]:
            self.obs.inc("context_compaction_total")

        hypothetical = [
            {"role": "user", "content": item.content}
            for item in fitted
            if item.provider_visibility is Visibility.PROVIDER_VISIBLE
        ]
        decision = self.canary_gate.decide(
            manifest.request_id,
            request.user,
            request.session,
            request.conversation,
            double_authority=request.legacy_authoritative and request.legacy_visible and bool(request.memories),
            double_injection=request.legacy_visible and bool(request.memories),
        )
        live_allowed = KillSwitch.enabled(self.live_switch) and decision.native_context_allowed and not request.shadow
        provider_context = hypothetical if live_allowed else []
        if not live_allowed:
            self.obs.inc("native_memory_context_bypass_total")
        if capsule is not None:
            capsule.invalidate()
        self.obs.observe("context_compose", (time.monotonic() - started) * 1000)
        return ShadowResult(
            manifest=manifest,
            capsule=capsule,
            hypothetical_context=hypothetical,
            provider_context=provider_context,
            bypassed=not live_allowed,
            bypass_reason="SHADOW_ONLY_OR_KILL_SWITCH_OFF" if not live_allowed else None,
            traces=traces,
            metrics=self.obs.snapshot(),
            canary_decision=decision,
        )

    def snapshot_writes(self) -> dict[str, int]:
        return dict(self.write_audit)
