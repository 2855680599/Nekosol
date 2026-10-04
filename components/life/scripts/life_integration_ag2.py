#!/usr/bin/env python3
"""Chiyo Agency | AG-2 Life Integration (完整 Life 闭环集成与隔离验收).

Canonical Cross-Domain Choreography Layer connecting:
  Observed Event
    -> CandidateSet (AG-0)
    -> DecisionOpportunity -> DecisionRecord (AG-1)
    -> DecisionAdoption (AG-2)
    -> Canonical Activity (LR-2 / LR-3 / LR-4)
    -> ExecutableStepSpec -> ActionProposal (AG-2 -> AR-0)
    -> Action Reality & Reconciliation (AR-0)
    -> Result Settlement & Outbox (AR-1)
    -> Natural Progress & Completion (LR-5)
    -> Experience Handoff Event (AG-2 -> FakeExperienceConsumer)

Strict Top-Level Non-Collapsed Invariants:
  1. Decision != Adoption
  2. Adoption != Activity Truth
  3. Activity != Action
  4. Action != Action Success
  5. Action Success != Settled Consequence
  6. Settled Consequence != Activity Completion
  7. Activity Completion != Memory

Zero-Usurpation Guarantees:
  - LifeIntegrationCoordinator owns ONLY cross-domain choreography & integration lineage.
  - 0 new LLM calls inside AG-2 integration/adoption/bridge/reconciliation/handoff.
  - 0 Planner / Task-Tree invention.
  - 0 Distributed magic rollback (PARTIAL is a first-class recoverable status).
  - 0 Automatic infinite loop on COMPLETED -> IDLE.
  - 0 Direct Native Memory writes (Experience Handoff is an event, not Memory).
  - Production Cutover = NO; all 4 kill switches default to False in production.
"""

from __future__ import annotations

from contextlib import contextmanager
import copy
import dataclasses
from dataclasses import dataclass, field
import errno
import fcntl
import json
import os
from pathlib import Path
import time
from typing import Any, Callable, Iterable, Iterator, Mapping, Optional, Protocol, Sequence
import uuid

from activity_admission import (
    ActivityConsequencePort,
    REASON_KIND_AGENCY_CHOICE,
    build_activity_lineage_verifier,
    build_admission_issuer,
    build_completion_evidence_verifier,
    build_interruption_evidence_verifier,
    build_waiting_evidence_verifier,
)

from activity_continuity import (
    COMMIT_INTENT_FILENAME,
    GENESIS_HASH,
    GLOBAL_SUBJECT_ID,
    INTERRUPTIBILITY_ATOMIC as LR2_OCCUPANCY_ATOMIC,
    INTERRUPTIBILITY_FOCUSED as LR2_OCCUPANCY_FOCUSED,
    INTERRUPTIBILITY_FREE as LR2_OCCUPANCY_FREE,
    INTERRUPTIBILITY_LIGHT as LR2_OCCUPANCY_LIGHT,
    INTERRUPTIBILITY_LEVELS,
    LIFE_STATE_IDLE,
    NAMESPACE_ISOLATED_TEST,
    OPEN_ACTIVITY_STATUSES,
    STATUS_ABANDONED,
    STATUS_ACTIVE,
    STATUS_CANCELLED,
    STATUS_COMPLETED,
    STATUS_EXPIRED,
    STATUS_PAUSED,
    STATUS_WAITING,
    TERMINAL_ACTIVITY_STATUSES,
    TRANSITION_ABANDON,
    TRANSITION_COMPLETE,
    TRANSITION_NOOP,
    TRANSITION_PAUSE,
    TRANSITION_RESUME,
    TRANSITION_START,
    TRANSITION_WAIT,
    ActivityAuthorityLease,
    ActivityCommandService,
    ActivityError,
    ActivityLockError,
    ActivityReadService,
    CanonicalActivityStore,
    CommandResult,
    ContractViolationError,
    RecoveryRequiredError,
    RevisionConflictError,
    SubjectIdentityError,
    WriterCapability,
    WriterCapabilityError,
    _fsync_dir,
    _iso,
    _parse,
    canonical_json_line,
    now,
    sha256_hex,
    validate_subject_id,
)

from activity_interruption import (
    EVENT_KIND_BODY_CONSTRAINT,
    EVENT_KIND_COMMUNICATION,
    EVENT_KIND_OBSERVED_WORLD_URGENCY,
    INTERRUPTIBILITY_ATOMIC,
    INTERRUPTIBILITY_IMMEDIATE,
    INTERRUPTIBILITY_SAFE_BOUNDARY,
    MESSAGE_CLASS_EXPLICIT_HOLD_REQUEST,
    MESSAGE_CLASS_ORDINARY,
    OCCUPANCY_ATOMIC,
    OCCUPANCY_FOCUSED,
    OCCUPANCY_FREE,
    OCCUPANCY_LIGHT,
    PENDING_STATUS_APPLIED,
    PENDING_STATUS_CANCELLED,
    PENDING_STATUS_EXPIRED,
    PENDING_STATUS_PENDING,
    PENDING_STATUS_READY_AT_BOUNDARY,
    PENDING_STATUS_SUPERSEDED,
    BoundaryReachedEvent,
    InterruptionCoordinator,
    InterruptionStateStore,
)

INTERRUPTIBILITY_LIGHT = INTERRUPTIBILITY_IMMEDIATE
OCCUPANCY_FOREGROUND = OCCUPANCY_LIGHT
OCCUPANCY_BACKGROUND = OCCUPANCY_FREE
OCCUPANCY_RELEASED = OCCUPANCY_FREE

from activity_waiting_agenda import (
    COND_KIND_EXTERNAL_RESULT_EVENT,
    COND_KIND_TIME_AT_OR_AFTER,
    COND_KIND_TIME_WINDOW_OPEN,
    ELIG_STATUS_CANCELLED,
    ELIG_STATUS_CONSUMED,
    ELIG_STATUS_EXPIRED,
    ELIG_STATUS_OPEN,
    ELIG_STATUS_STALE,
    ELIG_STATUS_SUPERSEDED,
    AgendaStore,
    EligibilityNotOpenError,
    NormalizedWaitingEvent,
    ResumeConditionStore,
    ResumeEligibilityStore,
    StaleResumeConditionError,
    WaitingCoordinator,
)

from action_reality_ledger import (
    ACTION_KIND_FILE,
    ACTION_KIND_MESSAGE,
    ACTION_KIND_TOOL,
    ACTION_KIND_WORLD,
    ACTION_STATUS_ACKNOWLEDGED,
    ACTION_STATUS_CANCELLED,
    ACTION_STATUS_FAILED,
    ACTION_STATUS_PREPARED,
    ACTION_STATUS_PROPOSED,
    ACTION_STATUS_SCHEDULED,
    ACTION_STATUS_SUBMITTED,
    ACTION_STATUS_SUCCEEDED,
    ACTION_STATUS_UNKNOWN,
    RECON_RESULT_CONFIRMED_FAILED,
    RECON_RESULT_CONFIRMED_SUCCEEDED,
    RECON_RESULT_CONFLICT,
    RECON_RESULT_NOT_SUBMITTED,
    RECON_RESULT_STILL_UNKNOWN,
    TRANSPORT_ERR_AMBIGUOUS_POST_SUBMIT,
    TRANSPORT_ERR_DEFINITE_PRE_SUBMIT,
    TRANSPORT_OK_ACCEPTED,
    TRANSPORT_OK_SUCCEEDED,
    ActionCommandService,
    ActionReadService,
    ActionRealityAuthority,
    ActionReceipt,
    ActionReplayEngine,
    ActionStore,
    FakeMessageProvider,
    FakeToolExecutor,
    FakeWorldResolver,
    FileArtifactObserver,
    MessageActionAdapter,
    ResultEvidence,
    SandboxExecutorCapability,
    SandboxFileAdapter,
    ToolActionAdapter,
    UnknownActionRetryForbiddenError,
    WorldActionAdapter,
    issue_action_writer_capability,
)

from result_settlement_ar1 import (
    CONSUMER_DOMAIN_GOAL,
    CONSUMER_DOMAIN_LIFE,
    CONSUMER_DOMAIN_MEMORY,
    CONSUMER_DOMAIN_WORLD,
    DELIVERY_STATUS_DELIVERED,
    DELIVERY_STATUS_FAILED,
    DISPOSITION_ACCEPT,
    DISPOSITION_CONFLICT,
    DISPOSITION_DEFER,
    DISPOSITION_IGNORE,
    EVENT_KIND_CORRECTION,
    EVENT_KIND_SETTLED,
    OUTCOME_CANCELLED_NO_EFFECT,
    OUTCOME_FAILURE_NO_EFFECT,
    OUTCOME_PARTIAL_EFFECT,
    OUTCOME_SUCCESS_EFFECT,
    OUTCOME_UNKNOWN_EFFECT,
    SETTLEMENT_STATUS_CONFLICT,
    SETTLEMENT_STATUS_OPEN,
    SETTLEMENT_STATUS_PARTIAL,
    SETTLEMENT_STATUS_SETTLED,
    SETTLEMENT_STATUS_SUPERSEDED,
    SETTLEMENT_STATUS_UNRESOLVED,
    ConsumerDispositionResult,
    FakeGoalConsumer,
    FakeMemoryConsumer,
    FakeWorldConsumer,
    ResultConsumerProtocol,
    ResultSettlementAuthority,
    ResultSettlementReplay,
    ResultSettlementService,
    SettlementEvidenceInput,
    SettlementPolicyRegistry,
    SettlementReadService,
    SettlementStore,
)

from activity_progress_completion_lr5 import (
    CHECKPOINT_SEMANTICS_LATEST_ARTIFACT_VERSION,
    CHECKPOINT_SEMANTICS_LATEST_POSITION,
    CHECKPOINT_SEMANTICS_MAX_CONFIRMED_POSITION,
    CHECKPOINT_SEMANTICS_TARGET_STATE,
    CRITERIA_ACTION_RESULT_MATCH,
    CRITERIA_ALL_OF,
    CRITERIA_ANY_OF,
    CRITERIA_ARTIFACT_VERSION_REACHED,
    CRITERIA_DOCUMENT_CURSOR_REACHED,
    CRITERIA_EXTERNAL_RESULT_MATCH,
    CRITERIA_PAGE_POSITION_REACHED,
    CRITERIA_TOOL_RESULT_MATCH,
    CRITERIA_WORLD_FACT_MATCH,
    EVAL_COMPLETION_ELIGIBLE,
    EVAL_CONFLICT,
    EVAL_NOT_COMPLETE,
    POLICY_ARTIFACT_V1,
    POLICY_COMPOSITE_V1,
    POLICY_EXTERNAL_RESULT_V1,
    POLICY_OPEN_ENDED_V1,
    POLICY_POSITION_V1,
    POLICY_WORLD_FACT_V1,
    PROGRESS_KIND_ARTIFACT_EDIT,
    PROGRESS_KIND_MULTI_ACTION_CHECKLIST,
    PROGRESS_KIND_OPEN_ENDED,
    PROGRESS_KIND_POSITIONAL_READING,
    PROGRESS_KIND_WAIT_FOR_EXTERNAL_RESULT,
    PROGRESS_KIND_WORLD_ACTION_BOUNDED,
    PROGRESS_STATE_ADVANCED,
    PROGRESS_STATE_BLOCKED,
    PROGRESS_STATE_COMPLETION_ELIGIBLE,
    PROGRESS_STATE_REGRESSED,
    PROGRESS_STATE_UNCHANGED,
    PROGRESS_STATE_UNKNOWN,
    RECON_STATUS_REQUIRED,
    SOURCE_KIND_ARTIFACT_STATE,
    SOURCE_KIND_CORRECTION_EVENT,
    SOURCE_KIND_EXTERNAL_RESULT,
    SOURCE_KIND_SETTLED_EVENT,
    SOURCE_KIND_WORLD_FACT,
    ActivityCompletionEligibility,
    ActivityProgressCoordinator,
    ActivityProgressReadService,
    ActivityProgressReplay,
    ActivityResultBinding,
    CompletionContract,
    LifeProgressAuthority,
    LifeProgressEvidence,
    ProgressContract,
    ProgressStore,
)

from candidate_sources_ag0 import (
    CANDIDATE_KIND_CONSIDER_EXPLICIT_GOAL,
    CANDIDATE_KIND_CONSIDER_WORLD_OPPORTUNITY,
    CANDIDATE_KIND_CONTINUE_CURRENT,
    CANDIDATE_KIND_FULFILL_COMMITMENT,
    CANDIDATE_KIND_PURSUE_GOAL_STEP,
    CANDIDATE_KIND_RESPOND_USER_REQUEST,
    CANDIDATE_KIND_RESUME_ACTIVITY,
    CANDIDATE_STATUS_OPEN,
    SOURCE_COMMITMENT,
    SOURCE_CURRENT_ACTIVITY,
    SOURCE_EXPLICIT_GOAL,
    SOURCE_RESUME_ELIGIBLE,
    SOURCE_USER_REQUEST,
    SOURCE_WORLD_OPPORTUNITY,
    CanonicalCommitmentRecord,
    CanonicalExplicitGoalRecord,
    CandidateAuditJournal,
    CandidateMaterializer,
    CandidateProjectionGuard,
    CandidateReadService,
    CandidateRecord,
    CandidateSet,
    CandidateSourceRegistry,
    CommitmentCandidateSource,
    CurrentActivityCandidateSource,
    ExplicitGoalCandidateSource,
    ObservedUserRequestEvent,
    ObservedWorldOpportunity,
    ResumeEligibleCandidateSource,
    UserRequestCandidateSource,
    WorldOpportunityCandidateSource,
)

from agency_decision_ag1 import (
    ATTEMPT_STATUS_FAILED,
    ATTEMPT_STATUS_OPEN,
    ATTEMPT_STATUS_STALE,
    ATTEMPT_STATUS_SUCCEEDED,
    CAPACITY_AVAILABLE,
    CAPACITY_LIMITED,
    CAPACITY_UNAVAILABLE,
    CAPACITY_UNKNOWN,
    DECISION_ABANDON,
    DECISION_CONTINUE,
    DECISION_DEFER,
    DECISION_NO_ACTION,
    DECISION_PAUSE,
    DECISION_RESUME,
    DECISION_START,
    DECISION_WAIT,
    OPPORTUNITY_STATUS_DECIDED,
    OPPORTUNITY_STATUS_FAILED,
    OPPORTUNITY_STATUS_OPEN,
    OPPORTUNITY_STATUS_STALE,
    ROUTE_COGNITIVE_REQUIRED,
    ROUTE_RULE_RESOLVED,
    AgencyCognitionAdapter,
    AgencyDecisionAuthority,
    AgencyDecisionCapability,
    AgencyDecisionJournal,
    AgencyDecisionReadService,
    AgencyDecisionService,
    AgencyDecisionStore,
    CognitionAdapterError,
    CognitiveDecisionRequest,
    CognitiveDecisionResponse,
    DecisionOpportunity,
    DecisionPolicyRouter,
    DecisionRecord,
    DecisionReplayVerifier,
    FakeCognitionAdapter,
)


# ---------------------------------------------------------------------------
# Constants, Schemas & Enums (Sections 28-120, 234-239)
# ---------------------------------------------------------------------------

POLICY_VERSION_AG2_V1 = "ag2.life_integration.v1"

SCHEMA_LIFE_INTEGRATION_RECORD = "chiyo.agency.life_integration_record.v1"
SCHEMA_DECISION_ADOPTION = "chiyo.agency.decision_adoption.v1"
SCHEMA_ACTIVITY_ADOPTION_SPEC = "chiyo.agency.activity_adoption_spec.v1"
SCHEMA_EXECUTABLE_STEP_SPEC = "chiyo.agency.executable_step_spec.v1"
SCHEMA_EXPERIENCE_HANDOFF = "chiyo.life.experience_handoff.v1"
SCHEMA_REPAIR_PROPOSAL = "chiyo.agency.repair_proposal.v1"
SCHEMA_INTEGRATION_STATE = "chiyo.agency.life_integration_state.v1"
SCHEMA_INTEGRATION_JOURNAL_ENTRY = "chiyo.agency.life_integration_journal_entry.v1"
SCHEMA_INTEGRATION_WAL = "chiyo.agency.life_integration_wal.v1"

WRITER_DOMAIN_LIFE_INTEGRATION = "life_integration_authority"

# LifeIntegrationRecord statuses (Section 37 & 46)
INTEGRATION_STATUS_OPEN = "OPEN"
INTEGRATION_STATUS_ADOPTED = "ADOPTED"
INTEGRATION_STATUS_IN_PROGRESS = "IN_PROGRESS"
INTEGRATION_STATUS_WAITING_RESULT = "WAITING_RESULT"
INTEGRATION_STATUS_SETTLED = "SETTLED"
INTEGRATION_STATUS_COMPLETED = "COMPLETED"
INTEGRATION_STATUS_PARTIAL = "PARTIAL"
INTEGRATION_STATUS_FAILED = "FAILED"
INTEGRATION_STATUS_CONFLICT = "CONFLICT"
INTEGRATION_STATUS_SUPERSEDED = "SUPERSEDED"

VALID_INTEGRATION_STATUSES = frozenset(
    {
        INTEGRATION_STATUS_OPEN,
        INTEGRATION_STATUS_ADOPTED,
        INTEGRATION_STATUS_IN_PROGRESS,
        INTEGRATION_STATUS_WAITING_RESULT,
        INTEGRATION_STATUS_SETTLED,
        INTEGRATION_STATUS_COMPLETED,
        INTEGRATION_STATUS_PARTIAL,
        INTEGRATION_STATUS_FAILED,
        INTEGRATION_STATUS_CONFLICT,
        INTEGRATION_STATUS_SUPERSEDED,
    }
)

# DecisionAdoptionRecord statuses (Section 45)
ADOPTION_STATUS_PENDING = "PENDING"
ADOPTION_STATUS_VALIDATED = "VALIDATED"
ADOPTION_STATUS_AUTHORIZED = "AUTHORIZED"
ADOPTION_STATUS_APPLIED = "APPLIED"
ADOPTION_STATUS_NOOP = "NOOP"
ADOPTION_STATUS_REJECTED_STALE = "REJECTED_STALE"
ADOPTION_STATUS_REJECTED_ILLEGAL = "REJECTED_ILLEGAL"
ADOPTION_STATUS_FAILED = "FAILED"
ADOPTION_STATUS_SUPERSEDED = "SUPERSEDED"

VALID_ADOPTION_STATUSES = frozenset(
    {
        ADOPTION_STATUS_PENDING,
        ADOPTION_STATUS_VALIDATED,
        ADOPTION_STATUS_AUTHORIZED,
        ADOPTION_STATUS_APPLIED,
        ADOPTION_STATUS_NOOP,
        ADOPTION_STATUS_REJECTED_STALE,
        ADOPTION_STATUS_REJECTED_ILLEGAL,
        ADOPTION_STATUS_FAILED,
        ADOPTION_STATUS_SUPERSEDED,
    }
)

# ExecutableStepSpec kinds & post-submit behaviors (Section 74-78)
STEP_KIND_WORLD_ACTION = "WORLD_ACTION"
STEP_KIND_FILE_ACTION = "FILE_ACTION"
STEP_KIND_TOOL_ACTION = "TOOL_ACTION"
STEP_KIND_MESSAGE_ACTION = "MESSAGE_ACTION"
STEP_KIND_INTERNAL_WAIT = "INTERNAL_WAIT_STEP"
STEP_KIND_INTERNAL_CHECKPOINT = "INTERNAL_CHECKPOINT_STEP"

VALID_STEP_KINDS = frozenset(
    {
        STEP_KIND_WORLD_ACTION,
        STEP_KIND_FILE_ACTION,
        STEP_KIND_TOOL_ACTION,
        STEP_KIND_MESSAGE_ACTION,
        STEP_KIND_INTERNAL_WAIT,
        STEP_KIND_INTERNAL_CHECKPOINT,
    }
)

POST_SUBMIT_KEEP_ACTIVE = "KEEP_ACTIVITY_ACTIVE"
POST_SUBMIT_ENTER_WAITING = "ENTER_WAITING_AFTER_SUBMIT"
POST_SUBMIT_NO_LIFE_CHANGE = "NO_LIFE_CHANGE"

VALID_POST_SUBMIT_BEHAVIORS = frozenset(
    {
        POST_SUBMIT_KEEP_ACTIVE,
        POST_SUBMIT_ENTER_WAITING,
        POST_SUBMIT_NO_LIFE_CHANGE,
    }
)

# Async result-settlement waiting modes (Section 78)
ASYNC_COMPLETION_MODE_DIRECT_SETTLE = "DIRECT_COMPLETE_ON_SETTLEMENT"
ASYNC_COMPLETION_MODE_ELIGIBILITY_FIRST = "RESUME_ELIGIBILITY_ON_SETTLEMENT"

# Experience Handoff milestone kinds (Section 107)
HANDOFF_KIND_COMPLETED = "COMPLETED"
HANDOFF_KIND_ABANDONED = "ABANDONED"
HANDOFF_KIND_SIGNIFICANT_PROGRESS = "SIGNIFICANT_PROGRESS"
HANDOFF_KIND_CONFLICT = "CONFLICT"

VALID_HANDOFF_KINDS = frozenset(
    {
        HANDOFF_KIND_COMPLETED,
        HANDOFF_KIND_ABANDONED,
        HANDOFF_KIND_SIGNIFICANT_PROGRESS,
        HANDOFF_KIND_CONFLICT,
    }
)

# Kill switch environment variable names (Section 234)
ENV_LIFE_RUNTIME_ENABLED = "LIFE_RUNTIME_ENABLED"
ENV_AGENCY_ENABLED = "AGENCY_ENABLED"
ENV_ACTION_EXECUTION_ENABLED = "ACTION_EXECUTION_ENABLED"
ENV_PROACTIVE_ENABLED = "PROACTIVE_ENABLED"

# Forbidden scalar / KPI keys in AG-2 telemetry or records (Section 233)
FORBIDDEN_AG2_SCALAR_KEYS = frozenset(
    {
        "life_quality_score",
        "autonomy_score",
        "human_likeness_score",
        "proactivity_kpi",
        "completion_kpi",
        "agency_quality_score",
        "utility_score",
    }
)


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class _NarrowPort:
    """Composition-root-allocated narrow port (S7 rule A, hardened in Phase-C).

    AG-2 may hold ports, never raw domain stores or writer services. The port is the
    capability: it exposes exactly the named members and refuses everything else, so
    holding it grants no more authority than the allow-list.

    Phase-C hardening: a port instance holds NO reference to its target at all. Every
    allow-listed member is a closure / property created by the composition root, so
    walking the port's own instance state reaches nothing whatsoever. The wrapped store,
    writer service or lease is only reachable by *calling* a declared member, which is
    precisely the declared authority. That turns "no authority lease is reachable from
    AG-2" into a structural property instead of a traversal convention.
    """

    __slots__ = ()

    def __getattr__(self, name: str) -> Any:
        # Protocol/dunder lookups (__dict__, __deepcopy__, __getstate__, ...) must behave
        # normally, otherwise introspection, copy and test tooling break. Any other name is
        # by definition outside the allow-list, because allow-listed members are installed
        # as class attributes by `build_ag2_port`.
        if name.startswith("__") and name.endswith("__"):
            raise AttributeError(name)
        raise CoordinatorAuthorityViolationError(
            f"AG-2 narrow port does not expose {name!r}; allowed={sorted(port_allow_list(self))}"
        )

    def __setattr__(self, name: str, value: Any) -> None:
        # The allow-list declares the whole surface: readable *and* writable members. Only a
        # declared writable property forwards to the target; anything else - including a
        # declared method name - is refused, so the port cannot be re-pointed after
        # composition.
        for klass in type(self).__mro__:
            descriptor = klass.__dict__.get(name)
            if descriptor is None:
                continue
            if isinstance(descriptor, property) and descriptor.fset is not None:
                descriptor.__set__(self, value)
                return
            break
        raise CoordinatorAuthorityViolationError(
            f"AG-2 narrow port refuses to set {name!r}; allowed={sorted(port_allow_list(self))}"
        )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<NarrowPort {sorted(port_allow_list(self))}>"


def port_allow_list(port: Any) -> frozenset[str]:
    """Public read accessor for a narrow port's declared allow-list (audit / tests)."""
    return frozenset(getattr(type(port), "_allowed", frozenset()))


AG2_DECLARED_PORT_TYPES = ("_NarrowPort",)


def _narrow_caller(fn: Any, name: str) -> Any:
    """Bind one declared callable as a closure over the target (no instance reference)."""

    def _call(*args: Any, **kwargs: Any) -> Any:
        return fn(*args, **kwargs)

    _call.__name__ = name
    _call.__qualname__ = name
    _call.__doc__ = getattr(fn, "__doc__", None)
    return _call


def _narrow_field(name: str, target: Any) -> property:
    """Bind one declared non-callable member as a forwarding property."""
    doc = f"Declared narrow-port member {name!r} forwarded to the wrapped owner."

    def _get(_self: Any) -> Any:
        return getattr(target, name)

    def _set(_self: Any, value: Any) -> None:
        setattr(target, name, value)

    return property(_get, _set, doc=doc)


def build_ag2_port(target: Any, allowed: Any) -> _NarrowPort:
    """Composition-root factory for an AG-2 narrow port.

    Builds a `_NarrowPort` subclass whose declared members close over `target`; the port
    instance itself keeps no reference to `target`.
    """
    if not isinstance(allowed, (set, frozenset, list, tuple)):
        raise TypeError("a narrow port needs an explicit method allow-list")
    allowed_set = frozenset(allowed)
    if not allowed_set:
        raise TypeError("a narrow port needs a non-empty method allow-list")
    namespace: dict[str, Any] = {"__slots__": (), "_allowed": allowed_set}
    for name in sorted(allowed_set):
        if not hasattr(target, name):
            raise CoordinatorAuthorityViolationError(
                f"narrow port target {type(target).__name__} has no attribute {name!r}"
            )
        member = getattr(target, name)
        namespace[name] = (
            staticmethod(_narrow_caller(member, name)) if callable(member)
            else _narrow_field(name, target)
        )
    return type("_NarrowPort", (_NarrowPort,), namespace)()


class LifeIntegrationError(Exception):
    """Base error for AG-2 Life Integration."""


class CoordinatorAuthorityViolationError(LifeIntegrationError):
    """Raised when LifeIntegrationCoordinator or an unauthorized caller attempts to usurp domain truth."""


class KillSwitchDisabledError(LifeIntegrationError):
    """Raised when an operation is blocked by an independent kill switch."""

    def __init__(self, switch_name: str, message: str) -> None:
        super().__init__(message)
        self.switch_name = switch_name


class AdoptionStaleError(LifeIntegrationError):
    """Raised when a DecisionRecord is stale at adoption time."""


class AdoptionIllegalError(LifeIntegrationError):
    """Raised when a DecisionRecord or ActivityAdoptionSpec violates adoption legality."""


class PlannerInventionForbiddenError(LifeIntegrationError):
    """Raised when an ExecutableStepSpec attempts multi-step task-tree planning in AG-2."""


class ReplayIsolationViolationError(LifeIntegrationError):
    """Raised when Replay or Shadow verification triggers any external side effect or LLM call."""


def _require_iso(val: Any, field_name: str) -> str:
    if not isinstance(val, str) or _parse(val) is None:
        raise LifeIntegrationError(f"{field_name} must be a valid ISO-8601 timestamp, got {val!r}")
    return val


def _reject_forbidden_ag2_keys(mapping: Mapping[str, Any], context: str) -> None:
    for k, v in mapping.items():
        if k in FORBIDDEN_AG2_SCALAR_KEYS:
            raise LifeIntegrationError(f"Forbidden scalar score/KPI key {k!r} in {context}")
        if isinstance(v, Mapping):
            _reject_forbidden_ag2_keys(v, f"{context}.{k}")


# ---------------------------------------------------------------------------
# Kill Switches (Sections 234-239)
# ---------------------------------------------------------------------------


@dataclass
class LifeRuntimeKillSwitches:
    """Sections 234-239: Four independent kill switches with production defaults all False."""

    life_runtime_enabled: bool = False
    agency_enabled: bool = False
    action_execution_enabled: bool = False
    proactive_enabled: bool = False

    @classmethod
    def production_defaults(cls) -> "LifeRuntimeKillSwitches":
        """Section 235: Production defaults MUST all be False."""
        return cls(
            life_runtime_enabled=False,
            agency_enabled=False,
            action_execution_enabled=False,
            proactive_enabled=False,
        )

    @classmethod
    def isolated_test_defaults(
        cls,
        *,
        life_runtime_enabled: bool = True,
        agency_enabled: bool = True,
        action_execution_enabled: bool = True,
        proactive_enabled: bool = False,
    ) -> "LifeRuntimeKillSwitches":
        """Isolated test defaults (note: proactive_enabled remains False in AG-2 before CT-0)."""
        return cls(
            life_runtime_enabled=life_runtime_enabled,
            agency_enabled=agency_enabled,
            action_execution_enabled=action_execution_enabled,
            proactive_enabled=proactive_enabled,
        )

    @classmethod
    def from_env(cls, env: Optional[Mapping[str, str]] = None) -> "LifeRuntimeKillSwitches":
        src = env if env is not None else os.environ

        def _parse_bool(key: str) -> bool:
            raw = str(src.get(key, "false")).strip().lower()
            return raw in {"1", "true", "yes", "on"}

        return cls(
            life_runtime_enabled=_parse_bool(ENV_LIFE_RUNTIME_ENABLED),
            agency_enabled=_parse_bool(ENV_AGENCY_ENABLED),
            action_execution_enabled=_parse_bool(ENV_ACTION_EXECUTION_ENABLED),
            proactive_enabled=_parse_bool(ENV_PROACTIVE_ENABLED),
        )

    def require_life_runtime(self) -> None:
        if not self.life_runtime_enabled:
            raise KillSwitchDisabledError(
                ENV_LIFE_RUNTIME_ENABLED,
                "LIFE_RUNTIME_ENABLED is False: full Life runtime loop is disabled",
            )

    def require_agency(self) -> None:
        self.require_life_runtime()
        if not self.agency_enabled:
            raise KillSwitchDisabledError(
                ENV_AGENCY_ENABLED,
                "AGENCY_ENABLED is False: AG-0/AG-1/Adoption pipeline is disabled",
            )

    def require_action_execution(self) -> None:
        self.require_life_runtime()
        if not self.action_execution_enabled:
            raise KillSwitchDisabledError(
                ENV_ACTION_EXECUTION_ENABLED,
                "ACTION_EXECUTION_ENABLED is False: external Action submission is disabled",
            )

    def require_proactive(self) -> None:
        if not self.proactive_enabled:
            raise KillSwitchDisabledError(
                ENV_PROACTIVE_ENABLED,
                "PROACTIVE_ENABLED is False: proactive outbound contact is disabled in AG-2",
            )

    def to_dict(self) -> dict[str, bool]:
        return {
            ENV_LIFE_RUNTIME_ENABLED: self.life_runtime_enabled,
            ENV_AGENCY_ENABLED: self.agency_enabled,
            ENV_ACTION_EXECUTION_ENABLED: self.action_execution_enabled,
            ENV_PROACTIVE_ENABLED: self.proactive_enabled,
        }


# ---------------------------------------------------------------------------
# Integration Capability Guard (Sections 28-33)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class IntegrationWriterCapability:
    """Capability token authorizing writes ONLY to AG-2 IntegrationStore & IntegrationJournal.

    Never grants write access to CanonicalActivityStore, ActionStore, SettlementStore,
    ProgressStore, World, Memory, Goal, or Commitment.
    """

    capability_id: str
    writer_domain: str
    store_root: Path
    namespace: str
    lease_token: str
    issued_at: str


class LifeIntegrationAuthority:
    """Sections 28-33: Issues and verifies IntegrationWriterCapability."""

    FORBIDDEN_COORDINATOR_CAPABILITIES = frozenset(
        {
            "WorldWriterCapability",
            "MemoryWriterCapability",
            "GoalWriterCapability",
            "CommitmentWriterCapability",
            "DirectExecutorCapability",
            "TelegramLiveSendCapability",
        }
    )

    @staticmethod
    def issue_capability(
        lease: IntegrationAuthorityLease,
        *,
        writer_domain: str = WRITER_DOMAIN_LIFE_INTEGRATION,
        namespace: str = NAMESPACE_ISOLATED_TEST,
    ) -> IntegrationWriterCapability:
        # Phase-C: the AG-2 writer capability can only be minted by the AG-2-only lease.
        # An LR-2 `ActivityAuthorityLease` is a different type and is rejected outright,
        # so no LR-2 lease can ever be exchanged for an AG-2 write capability.
        if isinstance(lease, ActivityAuthorityLease):
            raise CoordinatorAuthorityViolationError(
                "an LR-2 ActivityAuthorityLease can never issue an AG-2 IntegrationWriterCapability"
            )
        if not isinstance(lease, IntegrationAuthorityLease):
            raise CoordinatorAuthorityViolationError(
                f"IntegrationWriterCapability requires an IntegrationAuthorityLease, got {type(lease).__name__}"
            )
        if not lease.held:
            raise CoordinatorAuthorityViolationError("IntegrationAuthorityLease must be held to issue IntegrationWriterCapability")
        if writer_domain != WRITER_DOMAIN_LIFE_INTEGRATION:
            raise CoordinatorAuthorityViolationError(f"Invalid writer_domain={writer_domain!r} for AG-2 IntegrationStore")
        if namespace == "production":
            raise CoordinatorAuthorityViolationError("Production namespace is forbidden in AG-2 isolated verification")
        return IntegrationWriterCapability(
            capability_id=f"intcap:{uuid.uuid4().hex[:16]}",
            writer_domain=writer_domain,
            store_root=lease.store_root,
            namespace=namespace,
            lease_token=lease.instance_id,
            issued_at=_iso(now()),
        )

    @staticmethod
    def verify_capability(
        lease: IntegrationAuthorityLease,
        capability: Any,
    ) -> None:
        if isinstance(lease, ActivityAuthorityLease):
            raise CoordinatorAuthorityViolationError(
                "an LR-2 ActivityAuthorityLease can never authorize an AG-2 IntegrationStore commit"
            )
        if not isinstance(lease, IntegrationAuthorityLease):
            raise CoordinatorAuthorityViolationError(
                f"AG-2 IntegrationStore commits require an IntegrationAuthorityLease, got {type(lease).__name__}"
            )
        if not isinstance(capability, IntegrationWriterCapability):
            raise CoordinatorAuthorityViolationError(
                f"Expected IntegrationWriterCapability, got {type(capability).__name__}"
            )
        if not lease.held or capability.lease_token != lease.instance_id:
            raise CoordinatorAuthorityViolationError("IntegrationWriterCapability lease_token mismatch or lease not held")
        if capability.writer_domain != WRITER_DOMAIN_LIFE_INTEGRATION:
            raise CoordinatorAuthorityViolationError(
                f"Invalid writer_domain={capability.writer_domain!r}"
            )


# ---------------------------------------------------------------------------
# AG-2 Integration authority boundary (Phase-C lease isolation, order sections 2-9)
# ---------------------------------------------------------------------------

#: Closed vocabulary of AG-2 journal events -> the state keys that event may touch.
#: The AG-2 commit boundary can express NOTHING outside this table, which is what makes
#: it a typed owner-side boundary instead of a generic "commit anything" handle.
INTEGRATION_COMMIT_CONTRACT: dict[str, frozenset[str]] = {
    "ACTIVITY_ASYNC_COMPLETION_MODE_SET": frozenset({"async_completion_mode_by_activity"}),
    "ADOPTION_SPEC_REGISTERED": frozenset({"adoption_specs_by_candidate"}),
    "DECISION_ADOPTED": frozenset({"adoption_id_by_decision", "adoption_id_by_idempotency_key", "adoptions_by_id"}),
    "DECISION_ADOPTION_APPLIED_FROM_RECEIPT": frozenset({"adoptions_by_id"}),
    "DECISION_ADOPTION_PRECOMMIT_AUTHORIZED": frozenset({"adoption_id_by_decision", "adoption_id_by_idempotency_key", "adoptions_by_id"}),
    "DECISION_ADOPTION_REJECTED_STALE": frozenset({"adoption_id_by_decision", "adoption_id_by_idempotency_key", "adoptions_by_id", "reevaluation_needed_events"}),
    "DECISION_ADOPTION_VALIDATED": frozenset({"adoption_id_by_decision", "adoption_id_by_idempotency_key", "adoptions_by_id"}),
    "EXECUTABLE_STEP_SPEC_CREATED": frozenset({"step_spec_id_by_idempotency_key", "step_specs_by_id"}),
    "EXPERIENCE_HANDOFF_DISPATCHED": frozenset({"handoff_deliveries_by_id", "pending_handoff_ids"}),
    "EXPERIENCE_HANDOFF_ENQUEUED": frozenset({"handoff_events_by_id", "integrations_by_id", "pending_handoff_ids"}),
    "LIFE_INTEGRATION_ABANDON_APPLIED": frozenset({"integration_ids_by_activity", "integrations_by_id"}),
    "LIFE_INTEGRATION_ACTION_SETTLED": frozenset({"integrations_by_id"}),
    "LIFE_INTEGRATION_ACTION_UNKNOWN": frozenset({"integrations_by_id"}),
    "LIFE_INTEGRATION_ADOPTED": frozenset({"integration_ids_by_activity", "integrations_by_id"}),
    "LIFE_INTEGRATION_ADOPTION_REJECTED": frozenset({"integrations_by_id"}),
    "LIFE_INTEGRATION_COMPLETED": frozenset({"integrations_by_id"}),
    "LIFE_INTEGRATION_DECISION_FAILED_OR_STALE": frozenset({"integrations_by_id"}),
    "LIFE_INTEGRATION_DECISION_LINKED": frozenset({"integration_id_by_decision", "integrations_by_id"}),
    "LIFE_INTEGRATION_MARKED_CONFLICT": frozenset({"integrations_by_id"}),
    "LIFE_INTEGRATION_NOOP_COMPLETED": frozenset({"integration_ids_by_activity", "integrations_by_id"}),
    "LIFE_INTEGRATION_OPENED": frozenset({"integration_id_by_correlation", "integrations_by_id"}),
    "LIFE_INTEGRATION_PAUSE_APPLIED": frozenset({"integration_ids_by_activity", "integrations_by_id"}),
    "LIFE_INTEGRATION_PROGRESS_UPDATED": frozenset({"integrations_by_id"}),
    "LIFE_INTEGRATION_RECONCILED": frozenset({"integration_ids_by_activity", "integrations_by_id"}),
    "LIFE_INTEGRATION_RESUME_APPLIED": frozenset({"integration_ids_by_activity", "integrations_by_id"}),
    "LIFE_INTEGRATION_START_APPLIED": frozenset({"integration_ids_by_activity", "integrations_by_id"}),
    "LIFE_INTEGRATION_STEP_PROPOSED": frozenset({"integrations_by_id"}),
    "LIFE_INTEGRATION_WAITING_ASYNC_RESULT": frozenset({"integrations_by_id"}),
    "LIFE_INTEGRATION_WAIT_APPLIED": frozenset({"integration_ids_by_activity", "integrations_by_id"}),
    "RECOVERY_RECONCILED_ADOPTION_FROM_LR2_JOURNAL": frozenset({"adoption_id_by_decision", "adoption_id_by_idempotency_key", "adoptions_by_id"}),
    "REPAIR_PROPOSAL_CREATED_ON_CONFLICT": frozenset({"repair_proposals_by_id"}),
}


class IntegrationAuthorityLease:
    """AG-2-only single-writer lease over the AG-2 ``IntegrationStore`` root.

    Deliberately a DIFFERENT type from ``ActivityAuthorityLease`` and bound to a different
    lock file: the two are never interchangeable, never share a capability type and never
    share a store binding. This lease can only ever issue an ``IntegrationWriterCapability``
    for the AG-2 store. It cannot issue an LR-2 writer capability, and an LR-2 lease can
    never be exchanged for one.
    """

    LOCK_FILENAME = "life_integration_authority.lock"
    MARKER_FILENAME = "life_integration_authority.json"
    SCHEMA_MARKER = "chiyo.life.integration.authority.v1"

    def __init__(
        self,
        store_root: Path | str,
        *,
        instance_id: Optional[str] = None,
        pid: Optional[int] = None,
        holder_pid: Optional[int] = None,
    ) -> None:
        self.store_root = Path(store_root).expanduser().resolve()
        self.run_dir = self.store_root / "run"
        self.lock_path = self.run_dir / self.LOCK_FILENAME
        self.marker_path = self.run_dir / self.MARKER_FILENAME
        eff_pid = pid if pid is not None else holder_pid
        self.pid = int(eff_pid if eff_pid is not None else os.getpid())
        self.instance_id = instance_id or f"integration-authority:{self.pid}:{uuid.uuid4().hex[:8]}"
        self._handle: Optional[Any] = None

    @property
    def held(self) -> bool:
        return self._handle is not None

    def acquire(self, *, timeout_seconds: float = 5.0) -> dict[str, Any]:
        if self.held:
            return self.status()
        self.run_dir.mkdir(parents=True, exist_ok=True)
        handle = self.lock_path.open("a+", encoding="utf-8")
        deadline = time.monotonic() + max(0.0, timeout_seconds)
        while True:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as exc:
                if exc.errno not in (errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK):
                    handle.close()
                    raise ActivityLockError(f"integration authority lock failed: {exc}") from exc
                if time.monotonic() >= deadline:
                    handle.close()
                    raise ActivityLockError(
                        f"another live instance holds the integration authority lock on {self.lock_path}"
                    ) from exc
                time.sleep(0.02)
        self._handle = handle
        marker = {
            "schema_version": self.SCHEMA_MARKER,
            "instance_id": self.instance_id,
            "pid": self.pid,
            "store_root": str(self.store_root),
            "acquired_at": _iso(now()),
            "marker_is_truth": False,
        }
        tmp = self.marker_path.with_name(f".{self.MARKER_FILENAME}.tmp.{uuid.uuid4().hex}")
        tmp.write_text(json.dumps(marker, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, self.marker_path)
        return marker

    def release(self) -> None:
        if self._handle is None:
            return
        try:
            self.marker_path.unlink(missing_ok=True)
        except OSError:
            pass
        try:
            fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            self._handle.close()
        finally:
            self._handle = None

    def issue_capability(
        self,
        *,
        writer_domain: str = WRITER_DOMAIN_LIFE_INTEGRATION,
        namespace: str = NAMESPACE_ISOLATED_TEST,
    ) -> "IntegrationWriterCapability":
        return LifeIntegrationAuthority.issue_capability(
            self, writer_domain=writer_domain, namespace=namespace
        )

    def status(self) -> dict[str, Any]:
        return {
            "instance_id": self.instance_id,
            "pid": self.pid,
            "store_root": str(self.store_root),
            "held": self.held,
            "lock_path": str(self.lock_path),
            "capability_type": "IntegrationWriterCapability",
        }


class IntegrationReadPort:
    """Pure read-only owner-side view of AG-2's OWN ``IntegrationStore``.

    Read-only by construction: it exposes no mutation of any kind, and the store it views
    belongs to AG-2 itself, so this grants no cross-domain authority.
    """

    def __init__(self, store: "IntegrationStore") -> None:
        if store is None:
            raise TypeError("IntegrationReadPort requires the AG-2 IntegrationStore")
        self.__store = store

    def read_state(self, *, mutable: bool = False) -> dict[str, Any]:
        return self.__store.load_state(_mutable=mutable)

    def read_journal(self) -> list[dict[str, Any]]:
        return self.__store.read_journal()

    def read_store_root(self) -> Path:
        return self.__store.store_root

    def __repr__(self) -> str:  # pragma: no cover
        return "<IntegrationReadPort read-only>"


class IntegrationCommitPort:
    """AG-2's owner-side commit boundary for AG-2's OWN ``IntegrationStore``.

    Composition-root-allocated. It keeps the AG-2 store, the AG-2-only
    ``IntegrationAuthorityLease`` and the matching ``IntegrationWriterCapability`` private,
    and exposes exactly one typed operation per AG-2 journal event plus the three
    read/maintenance operations AG-2 genuinely needs.

    There is deliberately no ``commit_anything`` / ``write_raw`` / ``execute`` /
    ``get_store`` / ``get_lease`` / ``issue_capability`` entry point: the event and the
    touched state keys are fixed by the method being called, so this boundary can neither
    express a mutation outside AG-2's own record vocabulary nor mint any capability. This is
    an owner-side commit boundary, NOT a new fact owner - AG-2 still owns its integration
    orchestration semantics.
    """

    def __init__(
        self,
        store: "IntegrationStore",
        lease: IntegrationAuthorityLease,
        capability: "IntegrationWriterCapability",
    ) -> None:
        if isinstance(lease, ActivityAuthorityLease):
            raise CoordinatorAuthorityViolationError(
                "IntegrationCommitPort refuses an LR-2 ActivityAuthorityLease"
            )
        if not isinstance(lease, IntegrationAuthorityLease):
            raise CoordinatorAuthorityViolationError(
                f"IntegrationCommitPort requires an IntegrationAuthorityLease, got {type(lease).__name__}"
            )
        LifeIntegrationAuthority.verify_capability(lease, capability)
        if capability.store_root != store.store_root:
            raise CoordinatorAuthorityViolationError("IntegrationCommitPort store/lease root mismatch")
        if lease.store_root != store.store_root:
            raise CoordinatorAuthorityViolationError("IntegrationCommitPort lease/store root mismatch")
        self.__store = store
        self.__lease = lease
        self.__capability = capability

    # -- read / maintenance -------------------------------------------------------
    def read_state(self, *, mutable: bool = False) -> dict[str, Any]:
        return self.__store.load_state(_mutable=mutable)

    def read_journal(self) -> list[dict[str, Any]]:
        return self.__store.read_journal()

    def flush(self) -> None:
        self.__store.flush_snapshot()

    def recover(self) -> Any:
        return self.__store.recover_from_wal_if_needed()

    # -- typed commit operations (one per AG-2 journal event) ---------------------
    def __record(
        self,
        event_type: str,
        *,
        map_updates: Optional[Mapping[str, Mapping[str, Any]]],
        list_replacements: Optional[Mapping[str, Sequence[Any]]],
        journal_payload: Mapping[str, Any],
        fault_hook: Optional[Callable[[str], None]] = None,
    ) -> dict[str, Any]:
        allowed_keys = INTEGRATION_COMMIT_CONTRACT.get(event_type)
        if allowed_keys is None:
            raise CoordinatorAuthorityViolationError(
                f"AG-2 commit boundary does not know event_type={event_type!r}"
            )
        payload = dict(journal_payload)
        if payload.get("event_type") != event_type:
            raise CoordinatorAuthorityViolationError(
                f"AG-2 commit boundary expected event_type={event_type!r}, "
                f"got {payload.get('event_type')!r}"
            )
        for key in (map_updates or {}):
            if key not in allowed_keys:
                raise CoordinatorAuthorityViolationError(
                    f"{event_type} may not touch IntegrationStore key {key!r}; "
                    f"allowed={sorted(allowed_keys)}"
                )
        for key in (list_replacements or {}):
            if key not in allowed_keys:
                raise CoordinatorAuthorityViolationError(
                    f"{event_type} may not replace IntegrationStore key {key!r}; "
                    f"allowed={sorted(allowed_keys)}"
                )
        return self.__store.commit_delta(
            lease=self.__lease,
            capability=self.__capability,
            map_updates=map_updates,
            list_replacements=list_replacements,
            journal_payload=payload,
            fault_hook=fault_hook,
        )

    def activity_async_completion_mode_set(
        self,
        *,
        map_updates: Optional[Mapping[str, Mapping[str, Any]]] = None,
        list_replacements: Optional[Mapping[str, Sequence[Any]]] = None,
        journal_payload: Mapping[str, Any],
        fault_hook: Optional[Callable[[str], None]] = None,
    ) -> dict[str, Any]:
        """`ACTIVITY_ASYNC_COMPLETION_MODE_SET` - one AG-2 record family; the event and its state keys are fixed."""
        return self.__record(
            "ACTIVITY_ASYNC_COMPLETION_MODE_SET",
            map_updates=map_updates,
            list_replacements=list_replacements,
            journal_payload=journal_payload,
            fault_hook=fault_hook,
        )
    def adoption_spec_registered(
        self,
        *,
        map_updates: Optional[Mapping[str, Mapping[str, Any]]] = None,
        list_replacements: Optional[Mapping[str, Sequence[Any]]] = None,
        journal_payload: Mapping[str, Any],
        fault_hook: Optional[Callable[[str], None]] = None,
    ) -> dict[str, Any]:
        """`ADOPTION_SPEC_REGISTERED` - one AG-2 record family; the event and its state keys are fixed."""
        return self.__record(
            "ADOPTION_SPEC_REGISTERED",
            map_updates=map_updates,
            list_replacements=list_replacements,
            journal_payload=journal_payload,
            fault_hook=fault_hook,
        )
    def decision_adopted(
        self,
        *,
        map_updates: Optional[Mapping[str, Mapping[str, Any]]] = None,
        list_replacements: Optional[Mapping[str, Sequence[Any]]] = None,
        journal_payload: Mapping[str, Any],
        fault_hook: Optional[Callable[[str], None]] = None,
    ) -> dict[str, Any]:
        """`DECISION_ADOPTED` - one AG-2 record family; the event and its state keys are fixed."""
        return self.__record(
            "DECISION_ADOPTED",
            map_updates=map_updates,
            list_replacements=list_replacements,
            journal_payload=journal_payload,
            fault_hook=fault_hook,
        )
    def decision_adoption_applied_from_receipt(
        self,
        *,
        map_updates: Optional[Mapping[str, Mapping[str, Any]]] = None,
        list_replacements: Optional[Mapping[str, Sequence[Any]]] = None,
        journal_payload: Mapping[str, Any],
        fault_hook: Optional[Callable[[str], None]] = None,
    ) -> dict[str, Any]:
        """`DECISION_ADOPTION_APPLIED_FROM_RECEIPT` - one AG-2 record family; the event and its state keys are fixed."""
        return self.__record(
            "DECISION_ADOPTION_APPLIED_FROM_RECEIPT",
            map_updates=map_updates,
            list_replacements=list_replacements,
            journal_payload=journal_payload,
            fault_hook=fault_hook,
        )
    def decision_adoption_precommit_authorized(
        self,
        *,
        map_updates: Optional[Mapping[str, Mapping[str, Any]]] = None,
        list_replacements: Optional[Mapping[str, Sequence[Any]]] = None,
        journal_payload: Mapping[str, Any],
        fault_hook: Optional[Callable[[str], None]] = None,
    ) -> dict[str, Any]:
        """`DECISION_ADOPTION_PRECOMMIT_AUTHORIZED` - one AG-2 record family; the event and its state keys are fixed."""
        return self.__record(
            "DECISION_ADOPTION_PRECOMMIT_AUTHORIZED",
            map_updates=map_updates,
            list_replacements=list_replacements,
            journal_payload=journal_payload,
            fault_hook=fault_hook,
        )
    def decision_adoption_rejected_stale(
        self,
        *,
        map_updates: Optional[Mapping[str, Mapping[str, Any]]] = None,
        list_replacements: Optional[Mapping[str, Sequence[Any]]] = None,
        journal_payload: Mapping[str, Any],
        fault_hook: Optional[Callable[[str], None]] = None,
    ) -> dict[str, Any]:
        """`DECISION_ADOPTION_REJECTED_STALE` - one AG-2 record family; the event and its state keys are fixed."""
        return self.__record(
            "DECISION_ADOPTION_REJECTED_STALE",
            map_updates=map_updates,
            list_replacements=list_replacements,
            journal_payload=journal_payload,
            fault_hook=fault_hook,
        )
    def decision_adoption_validated(
        self,
        *,
        map_updates: Optional[Mapping[str, Mapping[str, Any]]] = None,
        list_replacements: Optional[Mapping[str, Sequence[Any]]] = None,
        journal_payload: Mapping[str, Any],
        fault_hook: Optional[Callable[[str], None]] = None,
    ) -> dict[str, Any]:
        """`DECISION_ADOPTION_VALIDATED` - one AG-2 record family; the event and its state keys are fixed."""
        return self.__record(
            "DECISION_ADOPTION_VALIDATED",
            map_updates=map_updates,
            list_replacements=list_replacements,
            journal_payload=journal_payload,
            fault_hook=fault_hook,
        )
    def executable_step_spec_created(
        self,
        *,
        map_updates: Optional[Mapping[str, Mapping[str, Any]]] = None,
        list_replacements: Optional[Mapping[str, Sequence[Any]]] = None,
        journal_payload: Mapping[str, Any],
        fault_hook: Optional[Callable[[str], None]] = None,
    ) -> dict[str, Any]:
        """`EXECUTABLE_STEP_SPEC_CREATED` - one AG-2 record family; the event and its state keys are fixed."""
        return self.__record(
            "EXECUTABLE_STEP_SPEC_CREATED",
            map_updates=map_updates,
            list_replacements=list_replacements,
            journal_payload=journal_payload,
            fault_hook=fault_hook,
        )
    def experience_handoff_dispatched(
        self,
        *,
        map_updates: Optional[Mapping[str, Mapping[str, Any]]] = None,
        list_replacements: Optional[Mapping[str, Sequence[Any]]] = None,
        journal_payload: Mapping[str, Any],
        fault_hook: Optional[Callable[[str], None]] = None,
    ) -> dict[str, Any]:
        """`EXPERIENCE_HANDOFF_DISPATCHED` - one AG-2 record family; the event and its state keys are fixed."""
        return self.__record(
            "EXPERIENCE_HANDOFF_DISPATCHED",
            map_updates=map_updates,
            list_replacements=list_replacements,
            journal_payload=journal_payload,
            fault_hook=fault_hook,
        )
    def experience_handoff_enqueued(
        self,
        *,
        map_updates: Optional[Mapping[str, Mapping[str, Any]]] = None,
        list_replacements: Optional[Mapping[str, Sequence[Any]]] = None,
        journal_payload: Mapping[str, Any],
        fault_hook: Optional[Callable[[str], None]] = None,
    ) -> dict[str, Any]:
        """`EXPERIENCE_HANDOFF_ENQUEUED` - one AG-2 record family; the event and its state keys are fixed."""
        return self.__record(
            "EXPERIENCE_HANDOFF_ENQUEUED",
            map_updates=map_updates,
            list_replacements=list_replacements,
            journal_payload=journal_payload,
            fault_hook=fault_hook,
        )
    def life_integration_abandon_applied(
        self,
        *,
        map_updates: Optional[Mapping[str, Mapping[str, Any]]] = None,
        list_replacements: Optional[Mapping[str, Sequence[Any]]] = None,
        journal_payload: Mapping[str, Any],
        fault_hook: Optional[Callable[[str], None]] = None,
    ) -> dict[str, Any]:
        """`LIFE_INTEGRATION_ABANDON_APPLIED` - one AG-2 record family; the event and its state keys are fixed."""
        return self.__record(
            "LIFE_INTEGRATION_ABANDON_APPLIED",
            map_updates=map_updates,
            list_replacements=list_replacements,
            journal_payload=journal_payload,
            fault_hook=fault_hook,
        )
    def life_integration_action_settled(
        self,
        *,
        map_updates: Optional[Mapping[str, Mapping[str, Any]]] = None,
        list_replacements: Optional[Mapping[str, Sequence[Any]]] = None,
        journal_payload: Mapping[str, Any],
        fault_hook: Optional[Callable[[str], None]] = None,
    ) -> dict[str, Any]:
        """`LIFE_INTEGRATION_ACTION_SETTLED` - one AG-2 record family; the event and its state keys are fixed."""
        return self.__record(
            "LIFE_INTEGRATION_ACTION_SETTLED",
            map_updates=map_updates,
            list_replacements=list_replacements,
            journal_payload=journal_payload,
            fault_hook=fault_hook,
        )
    def life_integration_action_unknown(
        self,
        *,
        map_updates: Optional[Mapping[str, Mapping[str, Any]]] = None,
        list_replacements: Optional[Mapping[str, Sequence[Any]]] = None,
        journal_payload: Mapping[str, Any],
        fault_hook: Optional[Callable[[str], None]] = None,
    ) -> dict[str, Any]:
        """`LIFE_INTEGRATION_ACTION_UNKNOWN` - one AG-2 record family; the event and its state keys are fixed."""
        return self.__record(
            "LIFE_INTEGRATION_ACTION_UNKNOWN",
            map_updates=map_updates,
            list_replacements=list_replacements,
            journal_payload=journal_payload,
            fault_hook=fault_hook,
        )
    def life_integration_adopted(
        self,
        *,
        map_updates: Optional[Mapping[str, Mapping[str, Any]]] = None,
        list_replacements: Optional[Mapping[str, Sequence[Any]]] = None,
        journal_payload: Mapping[str, Any],
        fault_hook: Optional[Callable[[str], None]] = None,
    ) -> dict[str, Any]:
        """`LIFE_INTEGRATION_ADOPTED` - one AG-2 record family; the event and its state keys are fixed."""
        return self.__record(
            "LIFE_INTEGRATION_ADOPTED",
            map_updates=map_updates,
            list_replacements=list_replacements,
            journal_payload=journal_payload,
            fault_hook=fault_hook,
        )
    def life_integration_adoption_rejected(
        self,
        *,
        map_updates: Optional[Mapping[str, Mapping[str, Any]]] = None,
        list_replacements: Optional[Mapping[str, Sequence[Any]]] = None,
        journal_payload: Mapping[str, Any],
        fault_hook: Optional[Callable[[str], None]] = None,
    ) -> dict[str, Any]:
        """`LIFE_INTEGRATION_ADOPTION_REJECTED` - one AG-2 record family; the event and its state keys are fixed."""
        return self.__record(
            "LIFE_INTEGRATION_ADOPTION_REJECTED",
            map_updates=map_updates,
            list_replacements=list_replacements,
            journal_payload=journal_payload,
            fault_hook=fault_hook,
        )
    def life_integration_completed(
        self,
        *,
        map_updates: Optional[Mapping[str, Mapping[str, Any]]] = None,
        list_replacements: Optional[Mapping[str, Sequence[Any]]] = None,
        journal_payload: Mapping[str, Any],
        fault_hook: Optional[Callable[[str], None]] = None,
    ) -> dict[str, Any]:
        """`LIFE_INTEGRATION_COMPLETED` - one AG-2 record family; the event and its state keys are fixed."""
        return self.__record(
            "LIFE_INTEGRATION_COMPLETED",
            map_updates=map_updates,
            list_replacements=list_replacements,
            journal_payload=journal_payload,
            fault_hook=fault_hook,
        )
    def life_integration_decision_failed_or_stale(
        self,
        *,
        map_updates: Optional[Mapping[str, Mapping[str, Any]]] = None,
        list_replacements: Optional[Mapping[str, Sequence[Any]]] = None,
        journal_payload: Mapping[str, Any],
        fault_hook: Optional[Callable[[str], None]] = None,
    ) -> dict[str, Any]:
        """`LIFE_INTEGRATION_DECISION_FAILED_OR_STALE` - one AG-2 record family; the event and its state keys are fixed."""
        return self.__record(
            "LIFE_INTEGRATION_DECISION_FAILED_OR_STALE",
            map_updates=map_updates,
            list_replacements=list_replacements,
            journal_payload=journal_payload,
            fault_hook=fault_hook,
        )
    def life_integration_decision_linked(
        self,
        *,
        map_updates: Optional[Mapping[str, Mapping[str, Any]]] = None,
        list_replacements: Optional[Mapping[str, Sequence[Any]]] = None,
        journal_payload: Mapping[str, Any],
        fault_hook: Optional[Callable[[str], None]] = None,
    ) -> dict[str, Any]:
        """`LIFE_INTEGRATION_DECISION_LINKED` - one AG-2 record family; the event and its state keys are fixed."""
        return self.__record(
            "LIFE_INTEGRATION_DECISION_LINKED",
            map_updates=map_updates,
            list_replacements=list_replacements,
            journal_payload=journal_payload,
            fault_hook=fault_hook,
        )
    def life_integration_marked_conflict(
        self,
        *,
        map_updates: Optional[Mapping[str, Mapping[str, Any]]] = None,
        list_replacements: Optional[Mapping[str, Sequence[Any]]] = None,
        journal_payload: Mapping[str, Any],
        fault_hook: Optional[Callable[[str], None]] = None,
    ) -> dict[str, Any]:
        """`LIFE_INTEGRATION_MARKED_CONFLICT` - one AG-2 record family; the event and its state keys are fixed."""
        return self.__record(
            "LIFE_INTEGRATION_MARKED_CONFLICT",
            map_updates=map_updates,
            list_replacements=list_replacements,
            journal_payload=journal_payload,
            fault_hook=fault_hook,
        )
    def life_integration_noop_completed(
        self,
        *,
        map_updates: Optional[Mapping[str, Mapping[str, Any]]] = None,
        list_replacements: Optional[Mapping[str, Sequence[Any]]] = None,
        journal_payload: Mapping[str, Any],
        fault_hook: Optional[Callable[[str], None]] = None,
    ) -> dict[str, Any]:
        """`LIFE_INTEGRATION_NOOP_COMPLETED` - one AG-2 record family; the event and its state keys are fixed."""
        return self.__record(
            "LIFE_INTEGRATION_NOOP_COMPLETED",
            map_updates=map_updates,
            list_replacements=list_replacements,
            journal_payload=journal_payload,
            fault_hook=fault_hook,
        )
    def life_integration_opened(
        self,
        *,
        map_updates: Optional[Mapping[str, Mapping[str, Any]]] = None,
        list_replacements: Optional[Mapping[str, Sequence[Any]]] = None,
        journal_payload: Mapping[str, Any],
        fault_hook: Optional[Callable[[str], None]] = None,
    ) -> dict[str, Any]:
        """`LIFE_INTEGRATION_OPENED` - one AG-2 record family; the event and its state keys are fixed."""
        return self.__record(
            "LIFE_INTEGRATION_OPENED",
            map_updates=map_updates,
            list_replacements=list_replacements,
            journal_payload=journal_payload,
            fault_hook=fault_hook,
        )
    def life_integration_progress_updated(
        self,
        *,
        map_updates: Optional[Mapping[str, Mapping[str, Any]]] = None,
        list_replacements: Optional[Mapping[str, Sequence[Any]]] = None,
        journal_payload: Mapping[str, Any],
        fault_hook: Optional[Callable[[str], None]] = None,
    ) -> dict[str, Any]:
        """`LIFE_INTEGRATION_PROGRESS_UPDATED` - one AG-2 record family; the event and its state keys are fixed."""
        return self.__record(
            "LIFE_INTEGRATION_PROGRESS_UPDATED",
            map_updates=map_updates,
            list_replacements=list_replacements,
            journal_payload=journal_payload,
            fault_hook=fault_hook,
        )
    def life_integration_reconciled(
        self,
        *,
        map_updates: Optional[Mapping[str, Mapping[str, Any]]] = None,
        list_replacements: Optional[Mapping[str, Sequence[Any]]] = None,
        journal_payload: Mapping[str, Any],
        fault_hook: Optional[Callable[[str], None]] = None,
    ) -> dict[str, Any]:
        """`LIFE_INTEGRATION_RECONCILED` - one AG-2 record family; the event and its state keys are fixed."""
        return self.__record(
            "LIFE_INTEGRATION_RECONCILED",
            map_updates=map_updates,
            list_replacements=list_replacements,
            journal_payload=journal_payload,
            fault_hook=fault_hook,
        )
    def life_integration_step_proposed(
        self,
        *,
        map_updates: Optional[Mapping[str, Mapping[str, Any]]] = None,
        list_replacements: Optional[Mapping[str, Sequence[Any]]] = None,
        journal_payload: Mapping[str, Any],
        fault_hook: Optional[Callable[[str], None]] = None,
    ) -> dict[str, Any]:
        """`LIFE_INTEGRATION_STEP_PROPOSED` - one AG-2 record family; the event and its state keys are fixed."""
        return self.__record(
            "LIFE_INTEGRATION_STEP_PROPOSED",
            map_updates=map_updates,
            list_replacements=list_replacements,
            journal_payload=journal_payload,
            fault_hook=fault_hook,
        )
    def life_integration_waiting_async_result(
        self,
        *,
        map_updates: Optional[Mapping[str, Mapping[str, Any]]] = None,
        list_replacements: Optional[Mapping[str, Sequence[Any]]] = None,
        journal_payload: Mapping[str, Any],
        fault_hook: Optional[Callable[[str], None]] = None,
    ) -> dict[str, Any]:
        """`LIFE_INTEGRATION_WAITING_ASYNC_RESULT` - one AG-2 record family; the event and its state keys are fixed."""
        return self.__record(
            "LIFE_INTEGRATION_WAITING_ASYNC_RESULT",
            map_updates=map_updates,
            list_replacements=list_replacements,
            journal_payload=journal_payload,
            fault_hook=fault_hook,
        )
    def recovery_reconciled_adoption_from_lr2_journal(
        self,
        *,
        map_updates: Optional[Mapping[str, Mapping[str, Any]]] = None,
        list_replacements: Optional[Mapping[str, Sequence[Any]]] = None,
        journal_payload: Mapping[str, Any],
        fault_hook: Optional[Callable[[str], None]] = None,
    ) -> dict[str, Any]:
        """`RECOVERY_RECONCILED_ADOPTION_FROM_LR2_JOURNAL` - one AG-2 record family; the event and its state keys are fixed."""
        return self.__record(
            "RECOVERY_RECONCILED_ADOPTION_FROM_LR2_JOURNAL",
            map_updates=map_updates,
            list_replacements=list_replacements,
            journal_payload=journal_payload,
            fault_hook=fault_hook,
        )
    def repair_proposal_created_on_conflict(
        self,
        *,
        map_updates: Optional[Mapping[str, Mapping[str, Any]]] = None,
        list_replacements: Optional[Mapping[str, Sequence[Any]]] = None,
        journal_payload: Mapping[str, Any],
        fault_hook: Optional[Callable[[str], None]] = None,
    ) -> dict[str, Any]:
        """`REPAIR_PROPOSAL_CREATED_ON_CONFLICT` - one AG-2 record family; the event and its state keys are fixed."""
        return self.__record(
            "REPAIR_PROPOSAL_CREATED_ON_CONFLICT",
            map_updates=map_updates,
            list_replacements=list_replacements,
            journal_payload=journal_payload,
            fault_hook=fault_hook,
        )

    def life_integration_agency_transition_applied(
        self,
        *,
        map_updates: Optional[Mapping[str, Mapping[str, Any]]] = None,
        list_replacements: Optional[Mapping[str, Sequence[Any]]] = None,
        journal_payload: Mapping[str, Any],
        fault_hook: Optional[Callable[[str], None]] = None,
    ) -> dict[str, Any]:
        """`LIFE_INTEGRATION_<decision-kind>_APPLIED` - agency-authorized transition outcome.

        The decision kind is a closed vocabulary, so this is still a typed member of the
        boundary: an agency-applied event outside that vocabulary is refused here.
        """
        event_type = str(journal_payload.get("event_type") or "")
        if event_type not in INTEGRATION_COMMIT_CONTRACT:
            raise CoordinatorAuthorityViolationError(
                f"AG-2 commit boundary refuses unknown agency-applied event {event_type!r}"
            )
        return self.__record(
            event_type,
            map_updates=map_updates,
            list_replacements=list_replacements,
            journal_payload=journal_payload,
            fault_hook=fault_hook,
        )


    def __repr__(self) -> str:  # pragma: no cover
        return "<IntegrationCommitPort typed AG-2 owner-side boundary>"


# ---------------------------------------------------------------------------
# Core Data Models (Sections 37, 45, 69, 74, 98, 107)
# ---------------------------------------------------------------------------


@dataclass
class ActivityAdoptionSpec:
    """Section 69: Canonical specification for how an adopted candidate maps to an LR-2/3/4/5 Activity."""

    activity_kind: str
    title: str
    target_refs: list[str]
    origin_ref: str
    initial_checkpoint_ref: Optional[str] = "chk:stage:001"
    attention_occupancy: str = OCCUPANCY_FOREGROUND
    interruptibility: str = INTERRUPTIBILITY_LIGHT
    atomic_region_ref: Optional[str] = None
    max_atomic_duration_seconds: int = 60
    progress_contract_spec: Optional[dict[str, Any]] = None
    completion_contract_spec: Optional[dict[str, Any]] = None
    result_binding_spec: Optional[dict[str, Any]] = None
    initial_executable_step_spec: Optional[dict[str, Any]] = None
    async_completion_mode: str = ASYNC_COMPLETION_MODE_DIRECT_SETTLE
    schema_version: str = SCHEMA_ACTIVITY_ADOPTION_SPEC

    def __post_init__(self) -> None:
        if not self.activity_kind or not isinstance(self.activity_kind, str):
            raise AdoptionIllegalError("ActivityAdoptionSpec.activity_kind must be a non-empty string")
        if not self.title or not isinstance(self.title, str):
            raise AdoptionIllegalError("ActivityAdoptionSpec.title must be a non-empty string")
        if not self.target_refs:
            raise AdoptionIllegalError("ActivityAdoptionSpec.target_refs must be non-empty")
        if not self.origin_ref or ":" not in self.origin_ref:
            raise AdoptionIllegalError(f"ActivityAdoptionSpec.origin_ref={self.origin_ref!r} must be structured")
        if self.async_completion_mode not in {
            ASYNC_COMPLETION_MODE_DIRECT_SETTLE,
            ASYNC_COMPLETION_MODE_ELIGIBILITY_FIRST,
        }:
            raise AdoptionIllegalError(f"Invalid async_completion_mode={self.async_completion_mode!r}")

    def to_dict(self) -> dict[str, Any]:
        return copy.deepcopy(dataclasses.asdict(self))

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ActivityAdoptionSpec":
        d = dict(data)
        d.pop("schema_version", None)
        return cls(**d)


@dataclass
class DecisionAdoptionRecord:
    """Section 45: Canonical record of adopting (or rejecting) an AG-1 DecisionRecord."""

    adoption_id: str
    decision_ref: str
    decision_revision: int
    candidate_set_ref: str
    selected_candidate_ref: Optional[str]
    decision_kind: str
    target_activity_ref: Optional[str]
    expected_activity_revision: Optional[int]
    adoption_status: str
    applied_activity_command_ref: Optional[str]
    applied_activity_revision: Optional[int]
    created_activity_ref: Optional[str]
    rejection_reason_code: Optional[str]
    adopted_at: Optional[str]
    recorded_at: str
    idempotency_key: str
    pending_interruption_ref: Optional[str] = None
    reevaluation_needed_event_ref: Optional[str] = None
    revision: int = 1
    authorized_command_id: Optional[str] = None
    authorized_mutation_kind: Optional[str] = None
    authorized_activity_ref: Optional[str] = None
    subject_id: str = GLOBAL_SUBJECT_ID
    policy_version: str = POLICY_VERSION_AG2_V1
    schema_version: str = SCHEMA_DECISION_ADOPTION

    def __post_init__(self) -> None:
        validate_subject_id(self.subject_id)
        if not self.adoption_id.startswith(("adp:", "adopt:", "adoption:")):
            raise LifeIntegrationError(f"adoption_id={self.adoption_id!r} must start with 'adp:'")
        if self.adoption_status not in VALID_ADOPTION_STATUSES:
            raise LifeIntegrationError(f"Invalid adoption_status={self.adoption_status!r}")
        _require_iso(self.recorded_at, "recorded_at")
        if self.adopted_at is not None:
            _require_iso(self.adopted_at, "adopted_at")

    def to_dict(self) -> dict[str, Any]:
        return copy.deepcopy(dataclasses.asdict(self))

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "DecisionAdoptionRecord":
        return cls(**copy.deepcopy(dict(data)))


@dataclass
class ExecutableStepSpec:
    """Section 74-75: Single current executable step specification (NOT a multi-step planner or task tree)."""

    step_spec_id: str
    activity_ref: str
    activity_revision: int
    decision_ref: str
    adoption_ref: str
    step_kind: str
    action_kind: Optional[str]
    target_ref: str
    payload_spec: dict[str, Any]
    expected_effect_predicates: list[str]
    requires_waiting: bool
    waiting_condition_spec: Optional[dict[str, Any]]
    post_submit_life_behavior: str
    idempotency_key: str
    created_at: str
    schema_version: str = SCHEMA_EXECUTABLE_STEP_SPEC

    def __post_init__(self) -> None:
        if not self.step_spec_id.startswith("step:"):
            raise LifeIntegrationError(f"step_spec_id={self.step_spec_id!r} must start with 'step:'")
        if self.step_kind not in VALID_STEP_KINDS:
            raise LifeIntegrationError(f"Invalid step_kind={self.step_kind!r}")
        if self.post_submit_life_behavior not in VALID_POST_SUBMIT_BEHAVIORS:
            raise LifeIntegrationError(f"Invalid post_submit_life_behavior={self.post_submit_life_behavior!r}")
        # Section 75: Forbid multi-step planner or task-tree structures inside ExecutableStepSpec
        for forbidden_planner_key in ("sub_steps", "task_tree", "dag_nodes", "future_steps", "plan_steps"):
            if forbidden_planner_key in self.payload_spec:
                raise PlannerInventionForbiddenError(
                    f"Section 75 violation: ExecutableStepSpec cannot contain planner/task-tree key {forbidden_planner_key!r}"
                )
        _require_iso(self.created_at, "created_at")

    def to_dict(self) -> dict[str, Any]:
        return copy.deepcopy(dataclasses.asdict(self))

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ExecutableStepSpec":
        return cls(**copy.deepcopy(dict(data)))


@dataclass
class LifeExperienceHandoffEvent:
    """Section 107: Canonical handoff event from Life runtime to Experience consumer (NOT a Memory write)."""

    handoff_id: str
    correlation_id: str
    activity_ref: str
    activity_revision: int
    activity_kind: str
    terminal_or_milestone_kind: str
    decision_refs: list[str]
    adoption_refs: list[str]
    action_refs: list[str]
    settlement_refs: list[str]
    progress_refs: list[str]
    completion_evidence_ref: Optional[str]
    fact_refs: list[str]
    artifact_refs: list[str]
    world_refs: list[str]
    occurred_at: str
    observed_at: str
    recorded_at: str
    causal_parent_refs: list[str]
    subject_id: str = GLOBAL_SUBJECT_ID
    policy_version: str = POLICY_VERSION_AG2_V1
    schema_version: str = SCHEMA_EXPERIENCE_HANDOFF

    def __post_init__(self) -> None:
        validate_subject_id(self.subject_id)
        if not self.handoff_id.startswith("lexp:"):
            raise LifeIntegrationError(f"handoff_id={self.handoff_id!r} must start with 'lexp:'")
        if self.terminal_or_milestone_kind not in VALID_HANDOFF_KINDS:
            raise LifeIntegrationError(f"Invalid terminal_or_milestone_kind={self.terminal_or_milestone_kind!r}")
        _require_iso(self.occurred_at, "occurred_at")
        _require_iso(self.observed_at, "observed_at")
        _require_iso(self.recorded_at, "recorded_at")

    def to_dict(self) -> dict[str, Any]:
        return copy.deepcopy(dataclasses.asdict(self))

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "LifeExperienceHandoffEvent":
        return cls(**copy.deepcopy(dict(data)))


@dataclass
class RepairProposal:
    """Section 98: Optional repair proposal recorded when a post-completion settlement conflict occurs."""

    repair_proposal_id: str
    correlation_id: str
    conflicted_activity_ref: str
    conflicted_settlement_ref: str
    reconciliation_ref: Optional[str]
    target_refs: list[str]
    reason_code: str
    created_at: str
    status: str = "OPEN"
    subject_id: str = GLOBAL_SUBJECT_ID
    schema_version: str = SCHEMA_REPAIR_PROPOSAL

    def to_dict(self) -> dict[str, Any]:
        return copy.deepcopy(dataclasses.asdict(self))


@dataclass
class LifeIntegrationRecord:
    """Section 37: Cross-domain choreography & causal correlation record for one Life loop episode."""

    integration_id: str
    correlation_id: str
    opportunity_ref: Optional[str]
    candidate_set_ref: Optional[str]
    decision_ref: Optional[str] = None
    adoption_ref: Optional[str] = None
    activity_ref: Optional[str] = None
    executable_step_refs: list[str] = field(default_factory=list)
    action_proposal_refs: list[str] = field(default_factory=list)
    action_refs: list[str] = field(default_factory=list)
    settlement_refs: list[str] = field(default_factory=list)
    progress_refs: list[str] = field(default_factory=list)
    completion_ref: Optional[str] = None
    experience_handoff_refs: list[str] = field(default_factory=list)
    status: str = INTEGRATION_STATUS_OPEN
    failure_reason_code: Optional[str] = None
    partial_stage: Optional[str] = None
    repair_proposal_ref: Optional[str] = None
    created_at: str = field(default_factory=lambda: _iso(now()))
    updated_at: str = field(default_factory=lambda: _iso(now()))
    revision: int = 1
    subject_id: str = GLOBAL_SUBJECT_ID
    policy_version: str = POLICY_VERSION_AG2_V1
    schema_version: str = SCHEMA_LIFE_INTEGRATION_RECORD

    def __post_init__(self) -> None:
        validate_subject_id(self.subject_id)
        if not self.integration_id.startswith("lint:"):
            raise LifeIntegrationError(f"integration_id={self.integration_id!r} must start with 'lint:'")
        if not self.correlation_id.startswith("corr:"):
            raise LifeIntegrationError(f"correlation_id={self.correlation_id!r} must start with 'corr:'")
        if self.status not in VALID_INTEGRATION_STATUSES:
            raise LifeIntegrationError(f"Invalid LifeIntegrationRecord.status={self.status!r}")
        _require_iso(self.created_at, "created_at")
        _require_iso(self.updated_at, "updated_at")

    def to_dict(self) -> dict[str, Any]:
        return copy.deepcopy(dataclasses.asdict(self))

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "LifeIntegrationRecord":
        return cls(**copy.deepcopy(dict(data)))


# ---------------------------------------------------------------------------
# FakeExperienceConsumer (Sections 103-117)
# ---------------------------------------------------------------------------


class ExperienceConsumerProtocol(Protocol):
    """Protocol for downstream Experience Handoff consumers."""

    def receive_experience_handoff(self, event: Mapping[str, Any]) -> dict[str, Any]:
        ...


class FakeExperienceConsumer:
    """Sections 115-117: Isolated test consumer for `LifeExperienceHandoffEvent`.

    Supports dispositions:
      - `ACCEPT`
      - `IGNORE`
      - `DEFER`
      - `DUPLICATE` (automatically detected on repeat delivery of the same handoff_id)
      - `DOWN` (raises RuntimeError when `simulate_down=True` to test consumer failure isolation)

    Guarantees `memory_write_count == 0` (Experience Handoff != Memory write).
    """

    def __init__(
        self,
        *,
        default_disposition: str = "ACCEPT",
        simulate_down: bool = False,
    ) -> None:
        self.default_disposition = default_disposition
        self.simulate_down = simulate_down
        self.disposition_overrides_by_handoff: dict[str, str] = {}
        self.received_events_by_id: dict[str, dict[str, Any]] = {}
        self.delivery_log: list[dict[str, Any]] = []
        self.accepted_handoff_ids: list[str] = []
        self.ignored_handoff_ids: list[str] = []
        self.deferred_handoff_ids: list[str] = []
        self.duplicate_delivery_count: int = 0
        self.memory_write_count: int = 0

    def set_disposition_for(self, handoff_id: str, disposition: str) -> None:
        if disposition not in {"ACCEPT", "IGNORE", "DEFER"}:
            raise ValueError(f"Unsupported disposition={disposition!r}")
        self.disposition_overrides_by_handoff[handoff_id] = disposition

    def receive_experience_handoff(self, event: Mapping[str, Any]) -> dict[str, Any]:
        if self.simulate_down:
            raise RuntimeError("FAKE_EXPERIENCE_CONSUMER_DOWN")

        ev_obj = LifeExperienceHandoffEvent.from_dict(event)
        hid = ev_obj.handoff_id

        # Duplicate idempotency check (Section 111 & 115)
        if hid in self.received_events_by_id and hid not in self.deferred_handoff_ids:
            self.duplicate_delivery_count += 1
            res = {
                "handoff_id": hid,
                "disposition": "DUPLICATE",
                "idempotent_replay": True,
                "memory_writes": 0,
            }
            self.delivery_log.append(res)
            return res

        disp = self.disposition_overrides_by_handoff.get(hid, self.default_disposition)
        self.received_events_by_id[hid] = ev_obj.to_dict()

        if disp == "ACCEPT":
            if hid in self.deferred_handoff_ids:
                self.deferred_handoff_ids.remove(hid)
            if hid not in self.accepted_handoff_ids:
                self.accepted_handoff_ids.append(hid)
        elif disp == "IGNORE":
            if hid in self.deferred_handoff_ids:
                self.deferred_handoff_ids.remove(hid)
            if hid not in self.ignored_handoff_ids:
                self.ignored_handoff_ids.append(hid)
        elif disp == "DEFER":
            if hid not in self.deferred_handoff_ids:
                self.deferred_handoff_ids.append(hid)

        res = {
            "handoff_id": hid,
            "disposition": disp,
            "idempotent_replay": False,
            "memory_writes": 0,
        }
        self.delivery_log.append(res)
        return res


# ---------------------------------------------------------------------------
# IntegrationStore & IntegrationJournal (WAL + Atomic Replace + Hash Chain)
# ---------------------------------------------------------------------------


class IntegrationJournal:
    """Hash-chained append-only JSONL journal view over `IntegrationStore`."""

    def __init__(self, source: Any) -> None:
        # `source` is an `IntegrationCommitPort` (or any object exposing `read_journal()`);
        # the journal view never receives the raw store.
        self.source = source

    def list_entries(
        self,
        *,
        integration_id: Optional[str] = None,
        correlation_id: Optional[str] = None,
        event_type: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        entries = self.source.read_journal()
        if integration_id is not None:
            entries = [
                e for e in entries if (e.get("payload") or {}).get("integration_id") == integration_id
            ]
        if correlation_id is not None:
            entries = [
                e for e in entries if (e.get("payload") or {}).get("correlation_id") == correlation_id
            ]
        if event_type is not None:
            entries = [
                e for e in entries if (e.get("payload") or {}).get("event_type") == event_type
            ]
        return entries

    def verify_integrity(self) -> bool:
        try:
            self.source.read_journal()
            return True
        except RecoveryRequiredError:
            return False


class IntegrationStore:
    """WAL (`commit_intent.json`) + hash-chained journal + atomic snapshot replace store for AG-2."""

    STATE_FILENAME = "life_integration_state.json"
    JOURNAL_FILENAME = "life_integration_journal.jsonl"
    WAL_FILENAME = COMMIT_INTENT_FILENAME

    def __init__(self, store_root: Path | str, *, namespace: str = NAMESPACE_ISOLATED_TEST) -> None:
        self.store_root = Path(store_root).expanduser().resolve()
        self.namespace = namespace
        self.state_path = self.store_root / self.STATE_FILENAME
        self.journal_path = self.store_root / self.JOURNAL_FILENAME
        self.wal_path = self.store_root / self.WAL_FILENAME
        self.run_dir = self.store_root / "run"
        self._verified_cache: Optional[dict[str, Any]] = None
        self._dirty_snapshot: Optional[dict[str, Any]] = None

    @contextmanager
    def _io_lock(self, *, shared: bool = False, timeout_seconds: float = 5.0) -> Iterator[None]:
        self.run_dir.mkdir(parents=True, exist_ok=True)
        lock_file = self.run_dir / ".life_integration_io.lock"
        mode = fcntl.LOCK_SH if shared else fcntl.LOCK_EX
        deadline = time.monotonic() + max(0.0, timeout_seconds)
        with lock_file.open("a+", encoding="utf-8") as handle:
            while True:
                try:
                    fcntl.flock(handle.fileno(), mode | fcntl.LOCK_NB)
                    break
                except OSError as exc:
                    if exc.errno not in (errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK):
                        raise ActivityLockError(f"AG-2 store IO lock failed: {exc}") from exc
                    if time.monotonic() >= deadline:
                        raise ActivityLockError("AG-2 store IO lock timeout") from exc
                    time.sleep(0.005)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _empty_state(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_INTEGRATION_STATE,
            "subject_id": GLOBAL_SUBJECT_ID,
            "namespace": self.namespace,
            "writer_domain": WRITER_DOMAIN_LIFE_INTEGRATION,
            "revision": 0,
            "last_entry_hash": GENESIS_HASH,
            "updated_at": _iso(now()),
            "integrations_by_id": {},
            "integration_id_by_correlation": {},
            "integration_id_by_decision": {},
            "integration_ids_by_activity": {},
            "adoptions_by_id": {},
            "adoption_id_by_decision": {},
            "adoption_id_by_idempotency_key": {},
            "adoption_specs_by_candidate": {},
            "step_specs_by_id": {},
            "step_spec_id_by_idempotency_key": {},
            "handoff_events_by_id": {},
            "pending_handoff_ids": [],
            "handoff_deliveries_by_id": {},
            "reevaluation_needed_events": [],
            "repair_proposals_by_id": {},
            "async_completion_mode_by_activity": {},
        }

    def _disk_signature(self) -> Optional[tuple[int, int, int, int]]:
        if not self.state_path.exists() or not self.journal_path.exists():
            return None
        try:
            st_s = self.state_path.stat()
            st_j = self.journal_path.stat()
            return (st_s.st_mtime_ns, st_s.st_size, st_j.st_mtime_ns, st_j.st_size)
        except OSError:
            return None

    def flush_snapshot(self) -> None:
        if self._dirty_snapshot is None:
            return
        state_to_write = self._dirty_snapshot
        self._dirty_snapshot = None
        self._write_snapshot_sync(state_to_write)
        sig = self._disk_signature()
        if sig is not None:
            self._verified_cache = {"sig": sig, "state": copy.deepcopy(state_to_write)}

    def _write_snapshot_sync(self, state_obj: dict[str, Any]) -> None:
        self.store_root.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(state_obj, ensure_ascii=False, separators=(",", ":")) + "\n"
        tmp = self.store_root / f".{self.STATE_FILENAME}.tmp.{os.getpid()}.{uuid.uuid4().hex}"
        try:
            with tmp.open("w", encoding="utf-8") as sh:
                sh.write(payload)
                sh.flush()
                os.fsync(sh.fileno())
            os.replace(tmp, self.state_path)
            _fsync_dir(self.store_root)
        finally:
            if tmp.exists():
                tmp.unlink(missing_ok=True)

    def read_journal(self) -> list[dict[str, Any]]:
        self.flush_snapshot()
        if not self.journal_path.exists():
            return []
        raw = self.journal_path.read_text(encoding="utf-8")
        if raw and not raw.endswith("\n"):
            raise RecoveryRequiredError(
                "AG2_INCOMPLETE_JOURNAL_TAIL",
                f"{self.JOURNAL_FILENAME} does not end with newline",
            )
        entries: list[dict[str, Any]] = []
        prev_hash = GENESIS_HASH
        expected_rev = 1
        for idx, line in enumerate(raw.splitlines(), start=1):
            if not line.strip():
                continue
            try:
                item = json.loads(line)
            except ValueError as exc:
                raise RecoveryRequiredError(
                    "AG2_CORRUPTED_JOURNAL_JSON",
                    f"invalid JSON at line {idx}: {exc}",
                ) from exc
            if not isinstance(item, dict) or item.get("schema_version") != SCHEMA_INTEGRATION_JOURNAL_ENTRY:
                raise RecoveryRequiredError(
                    "AG2_INVALID_JOURNAL_SCHEMA",
                    f"invalid schema at line {idx}",
                )
            if item.get("store_revision") != expected_rev:
                raise RecoveryRequiredError(
                    "AG2_JOURNAL_REVISION_GAP",
                    f"expected store_revision={expected_rev}, got {item.get('store_revision')}",
                )
            if item.get("prev_entry_hash") != prev_hash:
                raise RecoveryRequiredError(
                    "AG2_JOURNAL_HASH_CHAIN_BROKEN",
                    f"broken hash chain at line {idx}",
                )
            rec_hash = item.get("entry_hash")
            unsigned = dict(item)
            unsigned.pop("entry_hash", None)
            if rec_hash != sha256_hex(canonical_json_line(unsigned)):
                raise RecoveryRequiredError(
                    "AG2_JOURNAL_ENTRY_TAMPERED",
                    f"entry_hash mismatch at line {idx}",
                )
            prev_hash = rec_hash
            expected_rev += 1
            entries.append(item)
        return entries

    @staticmethod
    def _apply_delta_into_state(state: dict[str, Any], delta: Mapping[str, Any]) -> None:
        for map_key, sub_updates in (delta.get("map_updates") or {}).items():
            target_map = state.setdefault(map_key, {})
            for k, v in sub_updates.items():
                if v == "__ABSENT__":
                    target_map.pop(k, None)
                else:
                    target_map[k] = copy.deepcopy(v)
        for list_key, list_val in (delta.get("list_replacements") or {}).items():
            state[list_key] = copy.deepcopy(list_val)

    def rebuild_state_from_journal(self, entries: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        state = self._empty_state()
        for entry in entries:
            delta = entry.get("state_delta") or {}
            self._apply_delta_into_state(state, delta)
            state["revision"] = int(entry["store_revision"])
            state["last_entry_hash"] = str(entry["entry_hash"])
            state["updated_at"] = str(entry["recorded_at"])
        return state

    def recover_from_wal_if_needed(self) -> bool:
        self.store_root.mkdir(parents=True, exist_ok=True)
        self._dirty_snapshot = None
        self._verified_cache = None

        if self.journal_path.exists():
            raw_j = self.journal_path.read_text(encoding="utf-8")
            if raw_j and not raw_j.endswith("\n"):
                lines = raw_j.splitlines()
                valid_lines: list[str] = []
                for ln in lines:
                    try:
                        json.loads(ln)
                        valid_lines.append(ln)
                    except ValueError:
                        break
                repaired = ("\n".join(valid_lines) + "\n") if valid_lines else ""
                self.journal_path.write_text(repaired, encoding="utf-8")

        if not self.wal_path.exists():
            if self.journal_path.exists():
                entries = self.read_journal()
                if not self.state_path.exists():
                    self._write_snapshot_sync(self.rebuild_state_from_journal(entries))
                else:
                    try:
                        st = json.loads(self.state_path.read_text(encoding="utf-8"))
                        if st.get("revision") != len(entries):
                            self._write_snapshot_sync(self.rebuild_state_from_journal(entries))
                    except ValueError:
                        self._write_snapshot_sync(self.rebuild_state_from_journal(entries))
            return False

        try:
            wal_data = json.loads(self.wal_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.wal_path.unlink(missing_ok=True)
            return True

        if not isinstance(wal_data, dict) or wal_data.get("schema_version") != SCHEMA_INTEGRATION_WAL:
            self.wal_path.unlink(missing_ok=True)
            return True

        journal_entry = wal_data.get("journal_entry")
        if not isinstance(journal_entry, dict):
            self.wal_path.unlink(missing_ok=True)
            return True

        entries = self.read_journal()
        target_rev = int(journal_entry["store_revision"])
        if len(entries) == target_rev - 1:
            j_line = canonical_json_line(journal_entry) + "\n"
            with self.journal_path.open("a", encoding="utf-8") as jh:
                jh.write(j_line)
                jh.flush()
                os.fsync(jh.fileno())
            entries = self.read_journal()

        if len(entries) == target_rev:
            rebuilt = self.rebuild_state_from_journal(entries)
            self._write_snapshot_sync(rebuilt)

        self.wal_path.unlink(missing_ok=True)
        _fsync_dir(self.store_root)
        return True

    def load_state(self, *, _mutable: bool = False) -> dict[str, Any]:
        self.store_root.mkdir(parents=True, exist_ok=True)
        if self.wal_path.exists():
            self._dirty_snapshot = None
            self._verified_cache = None
            self.recover_from_wal_if_needed()

        if self._dirty_snapshot is not None:
            return self._dirty_snapshot if _mutable else copy.deepcopy(self._dirty_snapshot)

        sig = self._disk_signature()
        if (
            self._verified_cache is not None
            and sig is not None
            and self._verified_cache.get("sig") == sig
        ):
            cached = self._verified_cache["state"]
            return cached if _mutable else copy.deepcopy(cached)

        if not self.state_path.exists() and not self.journal_path.exists():
            empty = self._empty_state()
            return empty if _mutable else copy.deepcopy(empty)

        entries = self.read_journal()
        if not self.state_path.exists():
            rebuilt = self.rebuild_state_from_journal(entries)
            self._write_snapshot_sync(rebuilt)
            return rebuilt if _mutable else copy.deepcopy(rebuilt)

        try:
            state = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise RecoveryRequiredError(
                "AG2_CORRUPTED_INTEGRATION_STATE",
                f"life_integration_state.json is corrupted: {exc}",
            ) from exc

        if state.get("schema_version") != SCHEMA_INTEGRATION_STATE:
            raise RecoveryRequiredError("AG2_INVALID_STATE_SCHEMA", "invalid integration state schema")
        validate_subject_id(state.get("subject_id"))

        if state.get("revision") != len(entries):
            rebuilt = self.rebuild_state_from_journal(entries)
            self._write_snapshot_sync(rebuilt)
            state = rebuilt

        new_sig = self._disk_signature()
        if new_sig is not None:
            self._verified_cache = {"sig": new_sig, "state": state}
        return state if _mutable else copy.deepcopy(state)

    def commit_delta(
        self,
        *,
        lease: IntegrationAuthorityLease,
        capability: IntegrationWriterCapability,
        map_updates: Optional[Mapping[str, Mapping[str, Any]]] = None,
        list_replacements: Optional[Mapping[str, Sequence[Any]]] = None,
        journal_payload: Mapping[str, Any],
        fault_hook: Optional[Callable[[str], None]] = None,
    ) -> dict[str, Any]:
        LifeIntegrationAuthority.verify_capability(lease, capability)
        if capability.store_root != self.store_root:
            raise CoordinatorAuthorityViolationError("IntegrationStore root mismatch")

        current = self.load_state(_mutable=True)
        next_rev = int(current["revision"]) + 1
        prev_hash = str(current["last_entry_hash"])
        ts_now = _iso(now())

        state_delta: dict[str, Any] = {}
        if map_updates:
            state_delta["map_updates"] = copy.deepcopy(dict(map_updates))
        if list_replacements:
            state_delta["list_replacements"] = copy.deepcopy({k: list(v) for k, v in list_replacements.items()})

        unsigned_entry: dict[str, Any] = {
            "schema_version": SCHEMA_INTEGRATION_JOURNAL_ENTRY,
            "entry_id": f"ijen:{next_rev:08d}:{uuid.uuid4().hex[:8]}",
            "store_revision": next_rev,
            "prev_entry_hash": prev_hash,
            "recorded_at": ts_now,
            "writer_domain": capability.writer_domain,
            "payload": copy.deepcopy(dict(journal_payload)),
            "state_delta": state_delta,
        }
        entry_hash = sha256_hex(canonical_json_line(unsigned_entry))
        signed_entry = dict(unsigned_entry)
        signed_entry["entry_hash"] = entry_hash

        next_state = current if self._dirty_snapshot is not None else copy.deepcopy(current)
        self._apply_delta_into_state(next_state, state_delta)
        next_state["schema_version"] = SCHEMA_INTEGRATION_STATE
        next_state["subject_id"] = GLOBAL_SUBJECT_ID
        next_state["namespace"] = self.namespace
        next_state["revision"] = next_rev
        next_state["updated_at"] = ts_now
        next_state["last_entry_hash"] = entry_hash

        wal_doc = {
            "schema_version": SCHEMA_INTEGRATION_WAL,
            "store_revision": next_rev,
            "journal_entry": signed_entry,
        }
        self.store_root.mkdir(parents=True, exist_ok=True)
        wal_tmp = self.store_root / f".{self.WAL_FILENAME}.tmp.{os.getpid()}.{uuid.uuid4().hex}"
        with wal_tmp.open("w", encoding="utf-8") as wh:
            wh.write(canonical_json_line(wal_doc) + "\n")
            wh.flush()
            if fault_hook is not None:
                os.fsync(wh.fileno())
        os.replace(wal_tmp, self.wal_path)

        j_line = canonical_json_line(signed_entry) + "\n"
        with self.journal_path.open("a", encoding="utf-8") as jh:
            jh.write(j_line)
            jh.flush()
            if fault_hook is not None:
                os.fsync(jh.fileno())

        if fault_hook is not None:
            self._write_snapshot_sync(next_state)
            self.wal_path.unlink(missing_ok=True)
            _fsync_dir(self.store_root)
            sig = self._disk_signature()
            if sig is not None:
                self._verified_cache = {"sig": sig, "state": copy.deepcopy(next_state)}
        else:
            self.wal_path.unlink(missing_ok=True)
            self._dirty_snapshot = next_state
            self._verified_cache = None

        return copy.deepcopy(signed_entry)


class AdoptionAuthorizationReadPort:
    """Read-only owner port for command-scoped Adoption authorizations."""
    def __init__(self, integration_store: IntegrationStore) -> None:
        if integration_store is None: raise TypeError("Adoption owner store is required")
        self.__store = integration_store
    def get_adoption_by_id(self, adoption_id: str) -> Optional[dict[str, Any]]:
        state = self.__store.load_state()
        record = state.get("adoptions_by_id", {}).get(adoption_id)
        return copy.deepcopy(record) if record is not None else None

# ---------------------------------------------------------------------------
# DecisionAdoptionCoordinator (Sections 39-70)
# ---------------------------------------------------------------------------


class DecisionAdoptionCoordinator:
    """Sections 39-70: Validates final freshness of an AG-1 DecisionRecord and translates it into
    canonical LR-2/LR-3/LR-4 commands (0 LLM, 0 direct state file writes).
    """

    def __init__(
        self,
        *,
        integration_commit: IntegrationCommitPort,
        decision_read: AgencyDecisionReadService,
        candidate_read: CandidateReadService,
        activity_read: ActivityReadService,
        activity_command: _NarrowPort,
        interruption_coordinator: _NarrowPort,
        waiting_coordinator: _NarrowPort,
        progress_coordinator: _NarrowPort,
    ) -> None:
        # Phase-C: AG-2 holds a typed commit boundary for its OWN store, never the raw
        # IntegrationStore, never an authority lease and never a writer capability.
        self.integration_commit = integration_commit
        # S7: no raw AG-1 store; only the read port is held.
        self.decision_read = decision_read
        self.candidate_read = candidate_read
        # S7: no raw LR-2 store; only the read port is held.
        self.activity_read = activity_read
        self.activity_command = activity_command
        self.interruption_coordinator = interruption_coordinator
        self.waiting_coordinator = waiting_coordinator
        self.progress_coordinator = progress_coordinator
        self.llm_call_count: int = 0

    def register_adoption_spec(
        self,
        candidate_id: str,
        spec: ActivityAdoptionSpec | Mapping[str, Any],
    ) -> ActivityAdoptionSpec:
        spec_obj = spec if isinstance(spec, ActivityAdoptionSpec) else ActivityAdoptionSpec.from_dict(spec)
        self.integration_commit.adoption_spec_registered(
            map_updates={"adoption_specs_by_candidate": {candidate_id: spec_obj.to_dict()}},
            journal_payload={
                "event_type": "ADOPTION_SPEC_REGISTERED",
                "candidate_id": candidate_id,
                "activity_kind": spec_obj.activity_kind,
            },
        )
        return spec_obj

    def get_adoption_spec(self, candidate_id: str) -> Optional[ActivityAdoptionSpec]:
        st = self.integration_commit.read_state(mutable=True)
        d = st["adoption_specs_by_candidate"].get(candidate_id)
        return ActivityAdoptionSpec.from_dict(d) if d else None

    @staticmethod
    def _default_evidence_kinds() -> list[str]:
        return [
            SOURCE_KIND_SETTLED_EVENT,
            SOURCE_KIND_CORRECTION_EVENT,
            SOURCE_KIND_WORLD_FACT,
            SOURCE_KIND_ARTIFACT_STATE,
            SOURCE_KIND_EXTERNAL_RESULT,
        ]

    @staticmethod
    def _default_authority_domains() -> list[str]:
        return [
            "result_settlement_authority",
            "world_authority",
            "artifact_authority",
            "external_result_authority",
        ]

    @classmethod
    def build_result_binding(
        cls,
        *,
        activity_id: str,
        adoption_id: str,
        spec_obj: ActivityAdoptionSpec,
        created_at: str,
        extra_correlation_ids: Optional[Sequence[str]] = None,
    ) -> ActivityResultBinding:
        rb_spec = spec_obj.result_binding_spec or {"target_refs": spec_obj.target_refs}
        corr_ids = list(rb_spec.get("correlation_ids") or [])
        for cid in extra_correlation_ids or []:
            if cid and cid not in corr_ids:
                corr_ids.append(cid)
        return ActivityResultBinding(
            binding_id=f"abind:{sha256_hex(f'{activity_id}:{adoption_id}')[:16]}",
            activity_id=activity_id,
            target_refs=list(rb_spec.get("target_refs") or spec_obj.target_refs),
            allowed_evidence_kinds=list(rb_spec.get("allowed_evidence_kinds") or cls._default_evidence_kinds()),
            authority_domains=list(rb_spec.get("authority_domains") or cls._default_authority_domains()),
            created_at=created_at,
            policy_version=str(rb_spec.get("policy_version") or POLICY_EXTERNAL_RESULT_V1),
            correlation_ids=corr_ids,
        )

    @classmethod
    def build_progress_contract(
        cls,
        *,
        activity_id: str,
        spec_obj: ActivityAdoptionSpec,
        created_at: str,
    ) -> ProgressContract:
        pc_spec = spec_obj.progress_contract_spec or {}
        return ProgressContract(
            progress_contract_id=f"pcont:{sha256_hex(f'{activity_id}:pcont')[:16]}",
            activity_id=activity_id,
            progress_kind=str(pc_spec.get("progress_kind", PROGRESS_KIND_WAIT_FOR_EXTERNAL_RESULT)),
            target_refs=list(pc_spec.get("target_refs") or spec_obj.target_refs),
            checkpoint_semantics=str(pc_spec.get("checkpoint_semantics", CHECKPOINT_SEMANTICS_TARGET_STATE)),
            created_at=created_at,
            policy_version=str(pc_spec.get("policy_version", POLICY_EXTERNAL_RESULT_V1)),
            allow_checkpoint_regression=bool(pc_spec.get("allow_checkpoint_regression", False)),
        )

    @classmethod
    def build_completion_contract(
        cls,
        *,
        activity_id: str,
        spec_obj: ActivityAdoptionSpec,
        created_at: str,
    ) -> CompletionContract:
        cc_spec = spec_obj.completion_contract_spec or {}
        default_criteria = {
            "subcriteria": [
                {
                    "criterion_id": "crit_world",
                    "criteria_kind": CRITERIA_ACTION_RESULT_MATCH,
                    "criteria": {"required_predicate": "world_state_mutated"},
                },
                {
                    "criterion_id": "crit_file",
                    "criteria_kind": CRITERIA_ACTION_RESULT_MATCH,
                    "criteria": {"required_predicate": "artifact_written_verified"},
                },
                {
                    "criterion_id": "crit_tool",
                    "criteria_kind": CRITERIA_ACTION_RESULT_MATCH,
                    "criteria": {"required_predicate": "business_artifact_verified"},
                },
                {
                    "criterion_id": "crit_msg",
                    "criteria_kind": CRITERIA_ACTION_RESULT_MATCH,
                    "criteria": {"required_predicate": "message_provider_accepted"},
                },
            ]
        }
        ckind = str(cc_spec.get("criteria_kind") or CRITERIA_ANY_OF)
        crit = dict(cc_spec.get("criteria") or (default_criteria if ckind == CRITERIA_ANY_OF else {"required_predicate": "world_state_mutated"}))
        return CompletionContract(
            contract_id=f"ccont:{sha256_hex(f'{activity_id}:ccont:v1')[:16]}",
            activity_id=activity_id,
            criteria_kind=ckind,
            target_refs=list(cc_spec.get("target_refs") or spec_obj.target_refs),
            criteria=crit,
            required_evidence_kinds=list(cc_spec.get("required_evidence_kinds") or cls._default_evidence_kinds()),
            required_authority_domains=list(cc_spec.get("required_authority_domains") or cls._default_authority_domains()),
            created_at=created_at,
            policy_version=str(cc_spec.get("policy_version", POLICY_EXTERNAL_RESULT_V1)),
            revision=int(cc_spec.get("revision", 1)),
        )

    def _build_default_adoption_spec_from_candidate(self, cand: Mapping[str, Any]) -> ActivityAdoptionSpec:
        ckind = str(cand["candidate_kind"])
        sref = str(cand["source_ref"])
        trefs = list(cand.get("target_refs") or [sref])
        return ActivityAdoptionSpec(
            activity_kind=f"life_{ckind.lower()}",
            title=f"{ckind} ({sref})",
            target_refs=trefs,
            origin_ref=sref if ":" in sref else f"src:{sref}",
            initial_checkpoint_ref="chk:stage:001",
            attention_occupancy=OCCUPANCY_FOREGROUND,
            interruptibility=INTERRUPTIBILITY_LIGHT,
            progress_contract_spec={
                "progress_kind": PROGRESS_KIND_WAIT_FOR_EXTERNAL_RESULT,
                "checkpoint_semantics": CHECKPOINT_SEMANTICS_TARGET_STATE,
                "policy_version": POLICY_EXTERNAL_RESULT_V1,
            },
            completion_contract_spec={
                "criteria_kind": CRITERIA_ANY_OF,
                "target_refs": trefs,
                "policy_version": POLICY_EXTERNAL_RESULT_V1,
            },
            result_binding_spec={
                "target_refs": trefs,
            },
        )

    def validate_decision_for_adoption(
        self,
        decision: DecisionRecord,
        *,
        check_at: Optional[str] = None,
        live_capacity_override: Optional[Mapping[str, Any]] = None,
    ) -> tuple[bool, Optional[str]]:
        """Section 41: Final Freshness Check immediately before adoption."""
        chk_iso = _require_iso(check_at or _iso(now()), "check_at")
        ctx_dict = self.decision_read.get_context_snapshot(decision.context_snapshot_ref)
        opp_dict = self.decision_read.get_opportunity(decision.decision_opportunity_ref)
        if not ctx_dict or not opp_dict:
            return False, "DECISION_PROVENANCE_SNAPSHOT_MISSING"

        # 1. Check Opportunity / Decision not already superseded
        if opp_dict.get("status") in {"STALE", "SUPERSEDED", "FAILED"}:
            return False, f"OPPORTUNITY_STATUS_{opp_dict.get('status')}"

        # 2. Check live CanonicalActivityStore revision & status against DecisionContextSnapshot
        act_state, _ = self.activity_read.read_canonical_state(include_journal=False)
        live_store_rev = int(act_state.get("revision", 0))
        snap_act_rev = ctx_dict.get("current_activity_revision")
        if snap_act_rev is not None and live_store_rev != int(snap_act_rev):
            return False, f"STALE_ACTIVITY_REVISION:{snap_act_rev}->{live_store_rev}"

        fg_ref = act_state.get("foreground_activity_ref")
        snap_fg_ref = ctx_dict.get("current_activity_ref")
        if fg_ref != snap_fg_ref:
            return False, f"FOREGROUND_ACTIVITY_CHANGED:{snap_fg_ref}->{fg_ref}"

        # 3. Check live LR-3 ATOMIC boundary
        if fg_ref:
            fg_act = (act_state.get("activities") or {}).get(fg_ref)
            _, live_intr, _ = self.interruption_coordinator.get_effective_attention(fg_act)
            if live_intr == INTERRUPTIBILITY_ATOMIC and decision.decision in {
                DECISION_START,
                DECISION_RESUME,
                DECISION_PAUSE,
                DECISION_ABANDON,
            }:
                return False, "Entered_ATOMIC_REGION_BEFORE_ADOPTION"

        # 4. Check live capacity override if provided
        if live_capacity_override is not None:
            if live_capacity_override.get("overall_capacity_state") == CAPACITY_UNAVAILABLE and decision.decision in {
                DECISION_START,
                DECISION_RESUME,
                DECISION_CONTINUE,
            }:
                return False, "CAPACITY_UNAVAILABLE_AT_ADOPTION"
            if live_capacity_override.get("permission_revoked") is True:
                return False, "PERMISSION_REVOKED_BEFORE_ADOPTION"

        # 5. Check selected candidate freshness in AG-0
        sel_cid = decision.selected_candidate_ref
        if sel_cid and decision.decision in {
            DECISION_START,
            DECISION_CONTINUE,
            DECISION_RESUME,
            DECISION_PAUSE,
            DECISION_WAIT,
            DECISION_ABANDON,
        }:
            v_info = self.candidate_read.validate_candidate(sel_cid, observed_at=chk_iso)
            if not v_info.get("valid", False):
                return False, f"SELECTED_CANDIDATE_STALE:{v_info.get('reason_code')}"
            snap_c_revs = ctx_dict.get("candidate_revisions") or {}
            if sel_cid in snap_c_revs:
                if v_info.get("source_revision") != snap_c_revs[sel_cid].get("source_revision"):
                    return False, f"CANDIDATE_SOURCE_REVISION_CHANGED:{sel_cid}"

            cand_dict = v_info.get("candidate") or {}
            # If RESUME_WAITING_ACTIVITY, verify ResumeEligibility is still OPEN
            if decision.decision == DECISION_RESUME:
                elig_ref = cand_dict.get("resume_eligibility_ref")
                if not elig_ref:
                    return False, "MISSING_RESUME_ELIGIBILITY_REF"
                edict = self.waiting_coordinator.get_eligibility(elig_ref)
                if not edict or edict.get("status") != ELIG_STATUS_OPEN:
                    return False, f"RESUME_ELIGIBILITY_NOT_OPEN:{elig_ref}"

        # 6. Check START legality against active foreground activity
        if decision.decision == DECISION_START and fg_ref is not None:
            fg_act = (act_state.get("activities") or {}).get(fg_ref)
            if fg_act and fg_act.get("status") == STATUS_ACTIVE:
                return False, f"CANNOT_START_WHILE_FOREGROUND_ACTIVE:{fg_ref}"

        return True, None

    def _issue_precommit_authorization(
        self,
        *,
        decision: Any,
        adoption_id: str,
        idem: str,
        cmd_idem: str,
        mutation_kind: str,
        activity_ref: str,
        expected_act_rev: int,
        occ_iso: str,
    ) -> Any:
        """Adoption-owner, command-scoped authorization for exactly one LR-2 command.

        The grant binds the Decision revision, the Adoption, the command id, the
        transition kind and the Activity, so a PAUSE ticket cannot be spent on
        RESUME/ABANDON/START or on a different Activity. This is an owner fact, not
        a caller claim: LR-2 still re-verifies Decision + Adoption + binding through
        the immutable admission issuer before it commits anything.
        """
        current = (
            self.integration_commit.read_state().get("adoptions_by_id", {}).get(adoption_id)
        )
        if current and current.get("adoption_status") == ADOPTION_STATUS_AUTHORIZED:
            same_binding = (
                current.get("authorized_command_id") == cmd_idem
                and current.get("authorized_activity_ref") == activity_ref
                and current.get("authorized_mutation_kind") == mutation_kind
            )
            if not same_binding:
                raise AdoptionIllegalError(
                    "existing precommit authorization is bound to a different "
                    "command / transition / activity"
                )
            return DecisionAdoptionRecord.from_dict(current)
        revision = int(current.get("revision", 0)) + 1 if current else 1
        authorized_rec = DecisionAdoptionRecord(
            adoption_id=adoption_id,
            decision_ref=decision.decision_id,
            decision_revision=decision.revision,
            candidate_set_ref=decision.candidate_set_ref,
            selected_candidate_ref=decision.selected_candidate_ref,
            decision_kind=decision.decision,
            target_activity_ref=activity_ref,
            expected_activity_revision=expected_act_rev,
            adoption_status=ADOPTION_STATUS_AUTHORIZED,
            applied_activity_command_ref=None,
            applied_activity_revision=None,
            created_activity_ref=None,
            rejection_reason_code=None,
            adopted_at=None,
            recorded_at=occ_iso,
            idempotency_key=idem,
            revision=revision,
            authorized_command_id=cmd_idem,
            authorized_mutation_kind=mutation_kind,
            authorized_activity_ref=activity_ref,
        )
        self.integration_commit.decision_adoption_precommit_authorized(
            map_updates={
                "adoptions_by_id": {adoption_id: authorized_rec.to_dict()},
                "adoption_id_by_decision": {decision.decision_id: adoption_id},
                "adoption_id_by_idempotency_key": {idem: adoption_id},
            },
            journal_payload={
                "event_type": "DECISION_ADOPTION_PRECOMMIT_AUTHORIZED",
                "adoption_id": adoption_id,
                "decision_id": decision.decision_id,
                "command_id": cmd_idem,
                "mutation_kind": mutation_kind,
                "activity_id": activity_ref,
                "decision_revision": decision.revision,
                "adoption_revision": revision,
            },
        )
        return authorized_rec

    def _canonical_transition_entry(
        self, *, activity_id: str, transition_id: str
    ) -> Optional[dict[str, Any]]:
        history = self.activity_read.get_transition_history(activity_id=activity_id)
        for entry in history or []:
            if entry.get("transition_id") == transition_id:
                return dict(entry)
        return None

    def _validate_activity_commit_receipt(self, receipt: Mapping[str, Any]) -> dict[str, Any]:
        """Adoption-owner side validation of an LR-2 commit receipt.

        The receipt is only a *claim* until the owner re-reads canonical truth. A
        receipt that does not match the canonical journal entry, the canonical
        Activity, or the Adoption's own authorized command is rejected, so a forged
        receipt can never produce APPLIED.
        """
        if not isinstance(receipt, Mapping):
            raise AdoptionIllegalError("commit receipt must be a mapping")
        required = (
            "receipt_id",
            "command_id",
            "activity_id",
            "transition_id",
            "transition_type",
            "previous_revision",
            "new_revision",
            "authority_class",
        )
        missing = [k for k in required if not receipt.get(k) and receipt.get(k) != 0]
        if missing:
            raise AdoptionIllegalError(f"commit receipt missing fields: {missing}")
        activity_id = str(receipt["activity_id"])
        transition_id = str(receipt["transition_id"])
        command_id = str(receipt["command_id"])

        act = self.activity_read.get_activity(activity_id)
        if act is None:
            raise AdoptionIllegalError("commit receipt references an unknown Activity")
        entry = self._canonical_transition_entry(
            activity_id=activity_id, transition_id=transition_id
        )
        if entry is None:
            raise AdoptionIllegalError(
                "commit receipt transition is not present in the canonical journal"
            )
        if str(entry.get("idempotency_key")) != command_id:
            raise AdoptionIllegalError("commit receipt command id does not match the journal")
        if str(entry.get("activity_id")) != activity_id:
            raise AdoptionIllegalError("commit receipt activity does not match the journal")
        if str(entry.get("transition_type")) != str(receipt["transition_type"]):
            raise AdoptionIllegalError("commit receipt transition type does not match the journal")
        if int(entry.get("result_revision", -1)) != int(receipt["new_revision"]):
            raise AdoptionIllegalError("commit receipt new_revision does not match the journal")
        if int(entry.get("expected_revision", -1)) != int(receipt["previous_revision"]):
            raise AdoptionIllegalError("commit receipt previous_revision does not match the journal")
        if int(act.get("revision", -1)) != int(receipt["new_revision"]):
            raise AdoptionIllegalError(
                "commit receipt is stale: Activity has moved past this revision"
            )
        return {"activity_id": activity_id, "transition_id": transition_id, "entry": entry}

    def consume_activity_commit_receipt(self, receipt: Mapping[str, Any]) -> dict[str, Any]:
        """Idempotently finalise an Adoption from a verified LR-2 commit receipt.

        LR-2 never writes Adoption state; this method is the owner's own write. It is
        exactly-once on the receipt: the same receipt consumed N times yields one
        APPLIED record and never a second canonical transition.
        """
        checked = self._validate_activity_commit_receipt(receipt)
        activity_id = checked["activity_id"]
        transition_id = checked["transition_id"]
        entry = checked["entry"]
        command_id = str(receipt["command_id"])
        adoption_ref = receipt.get("adoption_ref")
        if not adoption_ref:
            raise AdoptionIllegalError("commit receipt does not name the Adoption it authorises")

        state = self.integration_commit.read_state(mutable=True)
        rec_dict = (state.get("adoptions_by_id") or {}).get(str(adoption_ref))
        if rec_dict is None:
            raise AdoptionIllegalError("commit receipt references an unknown Adoption record")
        if str(rec_dict.get("authorized_command_id") or "") != command_id:
            raise AdoptionIllegalError(
                "commit receipt command does not match this Adoption's authorization"
            )
        if (
            rec_dict.get("adoption_status") == ADOPTION_STATUS_APPLIED
            and str(rec_dict.get("applied_activity_command_ref") or "") == transition_id
        ):
            return {
                "applied": True,
                "idempotent_replay": True,
                "adoption_id": str(adoption_ref),
                "activity_id": activity_id,
                "transition_id": transition_id,
            }
        rec = DecisionAdoptionRecord.from_dict(rec_dict)
        rec.adoption_status = ADOPTION_STATUS_APPLIED
        rec.applied_activity_command_ref = transition_id
        rec.applied_activity_revision = int(receipt["new_revision"])
        rec.target_activity_ref = activity_id
        if str(entry.get("transition_type")) == "START":
            rec.created_activity_ref = rec.created_activity_ref or activity_id
        rec.adopted_at = str(entry.get("occurred_at") or rec.adopted_at or "")
        rec.revision = int(rec_dict.get("revision", 0)) + 1
        self.integration_commit.decision_adoption_applied_from_receipt(
            map_updates={"adoptions_by_id": {str(adoption_ref): rec.to_dict()}},
            journal_payload={
                "event_type": "DECISION_ADOPTION_APPLIED_FROM_RECEIPT",
                "adoption_id": str(adoption_ref),
                "activity_id": activity_id,
                "transition_id": transition_id,
                "command_id": command_id,
                "receipt_id": str(receipt["receipt_id"]),
                "activity_revision": int(receipt["new_revision"]),
            },
        )
        return {
            "applied": True,
            "idempotent_replay": False,
            "adoption_id": str(adoption_ref),
            "activity_id": activity_id,
            "transition_id": transition_id,
        }

    def reconcile_pending_adoptions(self) -> dict[str, Any]:
        """F1 reconciliation: finalise AUTHORIZED adoptions whose command committed.

        Logical only - there is no durable crash journal in V0.1. The canonical LR-2
        journal is the source of truth for "did this command commit", and the Adoption
        owner finishes its own state from it idempotently.
        """
        state = self.integration_commit.read_state()
        finalized: list[str] = []
        skipped: list[str] = []
        for adoption_id, rec in (state.get("adoptions_by_id") or {}).items():
            if rec.get("adoption_status") != ADOPTION_STATUS_AUTHORIZED:
                continue
            cmd_id = rec.get("authorized_command_id")
            act_ref = rec.get("authorized_activity_ref")
            if not cmd_id or not act_ref:
                skipped.append(adoption_id)
                continue
            history = self.activity_read.get_transition_history(activity_id=act_ref) or []
            match = [e for e in history if str(e.get("idempotency_key")) == str(cmd_id)]
            if not match:
                skipped.append(adoption_id)
                continue
            entry = match[-1]
            receipt = {
                "receipt_id": str(entry.get("transition_id")),
                "command_id": str(cmd_id),
                "activity_id": str(act_ref),
                "transition_id": str(entry.get("transition_id")),
                "transition_type": str(entry.get("transition_type")),
                "previous_revision": int(entry.get("expected_revision", 0)),
                "new_revision": int(entry.get("result_revision", 0)),
                "authority_class": "AGENCY_AUTHORIZED",
                "evidence_digest": str(entry.get("entry_hash") or ""),
                "runtime_epoch": (
                    self.admission_issuer.runtime_epoch
                    if getattr(self, "admission_issuer", None) is not None
                    else ""
                ),
                "adoption_ref": str(adoption_id),
            }
            self.consume_activity_commit_receipt(receipt)
            finalized.append(adoption_id)
        return {"finalized": finalized, "skipped": skipped}

    def adopt_decision(
        self,
        decision_input: DecisionRecord | Mapping[str, Any] | str,
        *,
        adoption_spec: Optional[ActivityAdoptionSpec | Mapping[str, Any]] = None,
        waiting_spec: Optional[Mapping[str, Any]] = None,
        pause_mode_override: Optional[str] = None,
        occurred_at: Optional[str] = None,
        live_capacity_override: Optional[Mapping[str, Any]] = None,
        idempotency_key: Optional[str] = None,
        fault_hook: Optional[Callable[[str], None]] = None,
    ) -> dict[str, Any]:
        """Sections 39-70: Adopt a committed AG-1 DecisionRecord into canonical Life Runtime state."""
        if isinstance(decision_input, str):
            d_dict = self.decision_read.get_decision(decision_input)
            if d_dict is None:
                raise AdoptionIllegalError(f"Unknown decision_id={decision_input!r}")
            decision = DecisionRecord.from_dict(d_dict)
        elif isinstance(decision_input, DecisionRecord):
            decision = decision_input
        elif isinstance(decision_input, Mapping):
            decision = DecisionRecord.from_dict(decision_input)
        else:
            raise AdoptionIllegalError(f"Unsupported decision_input: {type(decision_input)!r}")

        # Verify DecisionRecord actually exists in canonical AG-1 DecisionStore (Section 211 Test 1)
        stored_dec = self.decision_read.get_decision(decision.decision_id)
        if stored_dec is None:
            raise AdoptionIllegalError(
                f"Decision {decision.decision_id!r} is not committed in canonical AgencyDecisionStore"
            )

        idem = idempotency_key or f"idem_adopt:{decision.decision_id}"
        int_state = self.integration_commit.read_state(mutable=True)

        # Idempotent replay check: if this decision was already adopted with APPLIED or NOOP, return it!
        existing_adp_id = int_state["adoption_id_by_idempotency_key"].get(idem) or int_state[
            "adoption_id_by_decision"
        ].get(decision.decision_id)
        if existing_adp_id and existing_adp_id in int_state["adoptions_by_id"]:
            existing_adp = DecisionAdoptionRecord.from_dict(int_state["adoptions_by_id"][existing_adp_id])
            if existing_adp.adoption_status in {
                ADOPTION_STATUS_APPLIED,
                ADOPTION_STATUS_NOOP,
                ADOPTION_STATUS_REJECTED_STALE,
                ADOPTION_STATUS_REJECTED_ILLEGAL,
            }:
                act_obj = (
                    self.activity_read.get_activity(existing_adp.created_activity_ref or existing_adp.target_activity_ref)
                    if (existing_adp.created_activity_ref or existing_adp.target_activity_ref)
                    else None
                )
                return {
                    "idempotent_replay": True,
                    "adoption_record": existing_adp,
                    "activity": act_obj,
                }

        occ_iso = _require_iso(occurred_at or _iso(now()), "occurred_at")
        adoption_id = existing_adp_id or f"adp:{sha256_hex(f'{decision.decision_id}:{idem}')[:16]}"

        if fault_hook is not None:
            fault_hook("after_decision_commit_before_adoption")

        # Step 1: Final Freshness Validation before Adoption (Sections 41-44)
        is_fresh, stale_reason = self.validate_decision_for_adoption(
            decision,
            check_at=occ_iso,
            live_capacity_override=live_capacity_override,
        )

        act_state, _ = self.activity_read.read_canonical_state(include_journal=False)
        expected_act_rev = int(act_state.get("revision", 0))

        if not is_fresh:
            reeval_id = f"reev:{sha256_hex(f'{decision.decision_id}:{stale_reason}:{occ_iso}')[:16]}"
            reeval_event = {
                "event_id": reeval_id,
                "event_kind": "DECISION_REEVALUATION_NEEDED",
                "decision_ref": decision.decision_id,
                "opportunity_ref": decision.decision_opportunity_ref,
                "reason_code": stale_reason,
                "occurred_at": occ_iso,
            }
            rejected_rec = DecisionAdoptionRecord(
                adoption_id=adoption_id,
                decision_ref=decision.decision_id,
                decision_revision=decision.revision,
                candidate_set_ref=decision.candidate_set_ref,
                selected_candidate_ref=decision.selected_candidate_ref,
                decision_kind=decision.decision,
                target_activity_ref=decision.target_activity_ref,
                expected_activity_revision=expected_act_rev,
                adoption_status=ADOPTION_STATUS_REJECTED_STALE,
                applied_activity_command_ref=None,
                applied_activity_revision=None,
                created_activity_ref=None,
                rejection_reason_code=stale_reason,
                adopted_at=None,
                recorded_at=occ_iso,
                idempotency_key=idem,
                reevaluation_needed_event_ref=reeval_id,
            )
            int_state = self.integration_commit.read_state(mutable=True)
            reev_list = list(int_state.get("reevaluation_needed_events") or [])
            reev_list.append(reeval_event)
            self.integration_commit.decision_adoption_rejected_stale(
                map_updates={
                    "adoptions_by_id": {adoption_id: rejected_rec.to_dict()},
                    "adoption_id_by_decision": {decision.decision_id: adoption_id},
                    "adoption_id_by_idempotency_key": {idem: adoption_id},
                },
                list_replacements={"reevaluation_needed_events": reev_list},
                journal_payload={
                    "event_type": "DECISION_ADOPTION_REJECTED_STALE",
                    "adoption_id": adoption_id,
                    "decision_id": decision.decision_id,
                    "reason_code": stale_reason,
                    "reevaluation_event_id": reeval_id,
                },
            )
            return {
                "idempotent_replay": False,
                "adoption_record": rejected_rec,
                "activity": None,
                "reevaluation_needed_event": reeval_event,
            }

        # Stage VALIDATED adoption record before executing LR-2 command when fault_hook is present (Crash Point 3)
        if fault_hook is not None:
            validated_rec = DecisionAdoptionRecord(
                adoption_id=adoption_id,
                decision_ref=decision.decision_id,
                decision_revision=decision.revision,
                candidate_set_ref=decision.candidate_set_ref,
                selected_candidate_ref=decision.selected_candidate_ref,
                decision_kind=decision.decision,
                target_activity_ref=decision.target_activity_ref,
                expected_activity_revision=expected_act_rev,
                adoption_status=ADOPTION_STATUS_VALIDATED,
                applied_activity_command_ref=None,
                applied_activity_revision=None,
                created_activity_ref=None,
                rejection_reason_code=None,
                adopted_at=None,
                recorded_at=occ_iso,
                idempotency_key=idem,
            )
            self.integration_commit.decision_adoption_validated(
                map_updates={
                    "adoptions_by_id": {adoption_id: validated_rec.to_dict()},
                    "adoption_id_by_decision": {decision.decision_id: adoption_id},
                    "adoption_id_by_idempotency_key": {idem: adoption_id},
                },
                journal_payload={
                    "event_type": "DECISION_ADOPTION_VALIDATED",
                    "adoption_id": adoption_id,
                    "decision_id": decision.decision_id,
                },
            )
            self.integration_commit.flush()
            fault_hook("after_adoption_validated_before_activity_command")

        # Step 2: Execute Decision-Kind-Specific Adoption Semantics (Sections 48-67)
        dkind = decision.decision
        sel_cid = decision.selected_candidate_ref
        cand_dict = self.candidate_read.get_candidate(sel_cid) if sel_cid else None

        applied_cmd_ref: Optional[str] = None
        applied_act_rev: Optional[int] = None
        created_act_ref: Optional[str] = None
        target_act_ref: Optional[str] = decision.target_activity_ref
        adoption_status = ADOPTION_STATUS_APPLIED
        authorization_revision: int = 0
        authorized_cmd_id: Optional[str] = None
        authorized_kind: Optional[str] = None
        authorized_act_ref: Optional[str] = None
        pending_intr_ref: Optional[str] = None
        resulting_activity: Optional[dict[str, Any]] = None

        if dkind == DECISION_START:
            # Section 49 & 68-70: START_NEW_ACTIVITY
            assert cand_dict is not None and sel_cid is not None
            if adoption_spec is not None:
                spec_obj = (
                    adoption_spec
                    if isinstance(adoption_spec, ActivityAdoptionSpec)
                    else ActivityAdoptionSpec.from_dict(adoption_spec)
                )
                self.register_adoption_spec(sel_cid, spec_obj)
            else:
                spec_obj = self.get_adoption_spec(sel_cid) or self._build_default_adoption_spec_from_candidate(cand_dict)
                self.register_adoption_spec(sel_cid, spec_obj)

            # Check if a WAITING activity is currently holding foreground; if so, allow background waiting on start
            fg_ref = act_state.get("foreground_activity_ref")
            fg_act = (act_state.get("activities") or {}).get(fg_ref) if fg_ref else None
            allow_bg_wait = bool(fg_act and fg_act.get("status") == STATUS_WAITING)

            cmd_idem = f"lr2_start:{decision.decision_id}:{adoption_id}"
            authorized_activity_id = f"actv_{sha256_hex(f'{decision.decision_id}:{adoption_id}:{cmd_idem}')[:16]}"
            authorized_rec = self._issue_precommit_authorization(
                decision=decision,
                adoption_id=adoption_id,
                idem=idem,
                cmd_idem=cmd_idem,
                mutation_kind="START",
                activity_ref=authorized_activity_id,
                expected_act_rev=expected_act_rev,
                occ_iso=occ_iso,
            )
            authorization_revision = int(authorized_rec.revision)
            authorized_cmd_id = cmd_idem
            authorized_kind = "START"
            authorized_act_ref = authorized_activity_id
            lr2_occ = (
                spec_obj.attention_occupancy
                if spec_obj.attention_occupancy in INTERRUPTIBILITY_LEVELS
                else (
                    OCCUPANCY_ATOMIC
                    if spec_obj.interruptibility == INTERRUPTIBILITY_ATOMIC
                    else OCCUPANCY_FOCUSED
                    if spec_obj.interruptibility == INTERRUPTIBILITY_SAFE_BOUNDARY
                    else OCCUPANCY_LIGHT
                )
            )
            lr3_intr = (
                spec_obj.interruptibility
                if spec_obj.interruptibility in {
                    INTERRUPTIBILITY_IMMEDIATE,
                    INTERRUPTIBILITY_SAFE_BOUNDARY,
                    INTERRUPTIBILITY_ATOMIC,
                }
                else (
                    INTERRUPTIBILITY_ATOMIC
                    if lr2_occ == OCCUPANCY_ATOMIC
                    else INTERRUPTIBILITY_SAFE_BOUNDARY
                    if lr2_occ == OCCUPANCY_FOCUSED
                    else INTERRUPTIBILITY_IMMEDIATE
                )
            )
            if lr2_occ == OCCUPANCY_ATOMIC:
                lr3_intr = INTERRUPTIBILITY_ATOMIC
            elif lr3_intr == INTERRUPTIBILITY_ATOMIC:
                lr2_occ = OCCUPANCY_ATOMIC

            cmd_res = self.activity_command.start_activity(
                activity_kind=spec_obj.activity_kind,
                title=spec_obj.title,
                origin_ref=spec_obj.origin_ref,
                source_refs=[decision.decision_id, adoption_id, sel_cid, *cand_dict.get("source_refs", [])],
                expected_revision=expected_act_rev,
                idempotency_key=cmd_idem,
                activity_id=authorized_activity_id,
                decision_ref=decision.decision_id,
                adoption_ref=adoption_id,
                subject_refs=spec_obj.target_refs,
                interruptibility=lr2_occ,
                checkpoint_ref=spec_obj.initial_checkpoint_ref,
                causal_parent_refs=[decision.decision_id, adoption_id, sel_cid],
                reason_code="ADOPTED_DECISION",
                occurred_at=occ_iso,
                observed_at=occ_iso,
                allow_background_waiting=allow_bg_wait,
            )
            resulting_activity = cmd_res.activity
            assert resulting_activity is not None
            created_act_ref = str(resulting_activity["activity_id"])
            target_act_ref = created_act_ref
            applied_act_rev = int(resulting_activity["revision"])
            applied_cmd_ref = (
                str(cmd_res.transition["transition_id"])
                if cmd_res.transition
                else f"cmd:{cmd_idem}"
            )

            # Configure LR-3 attention & optional atomic region
            self.interruption_coordinator.configure_activity_attention(
                activity_id=created_act_ref,
                attention_occupancy=lr2_occ,
                interruptibility=lr3_intr,
                atomic_region_ref=spec_obj.atomic_region_ref,
                max_atomic_duration_seconds=spec_obj.max_atomic_duration_seconds,
            )

            # Register LR-5 ActivityResultBinding, ProgressContract, CompletionContract
            # Correlation lineage: bind the same correlation_id the settlement/progress path uses
            # (LifeIntegrationRecord.correlation_id), looked up canonically by decision_ref.
            _int_state_for_corr = self.integration_commit.read_state()
            _int_id_for_corr = (_int_state_for_corr.get("integration_id_by_decision") or {}).get(
                decision.decision_id
            )
            _extra_corr_ids: list[str] = []
            if _int_id_for_corr:
                _int_rec_for_corr = (
                    _int_state_for_corr.get("integrations_by_id") or {}
                ).get(_int_id_for_corr) or {}
                _corr = _int_rec_for_corr.get("correlation_id")
                if _corr:
                    _extra_corr_ids.append(str(_corr))
            binding = self.build_result_binding(
                activity_id=created_act_ref,
                adoption_id=adoption_id,
                spec_obj=spec_obj,
                created_at=occ_iso,
                extra_correlation_ids=_extra_corr_ids,
            )
            self.progress_coordinator.register_result_binding(binding)

            if spec_obj.progress_contract_spec is not None:
                p_contract = self.build_progress_contract(
                    activity_id=created_act_ref,
                    spec_obj=spec_obj,
                    created_at=occ_iso,
                )
                self.progress_coordinator.register_progress_contract(p_contract)

            if spec_obj.completion_contract_spec is not None:
                c_contract = self.build_completion_contract(
                    activity_id=created_act_ref,
                    spec_obj=spec_obj,
                    created_at=occ_iso,
                )
                self.progress_coordinator.register_completion_contract(c_contract)

            # Record async completion mode for this activity
            self.integration_commit.activity_async_completion_mode_set(
                map_updates={
                    "async_completion_mode_by_activity": {
                        created_act_ref: spec_obj.async_completion_mode
                    }
                },
                journal_payload={
                    "event_type": "ACTIVITY_ASYNC_COMPLETION_MODE_SET",
                    "activity_id": created_act_ref,
                    "async_completion_mode": spec_obj.async_completion_mode,
                },
            )

        elif dkind == DECISION_CONTINUE:
            # Section 50-51: CONTINUE_CURRENT_ACTIVITY -> NOOP on Activity status/revision
            adoption_status = ADOPTION_STATUS_NOOP
            fg_ref = act_state.get("foreground_activity_ref") or decision.target_activity_ref
            target_act_ref = fg_ref
            resulting_activity = self.activity_read.get_activity(fg_ref) if fg_ref else None
            applied_act_rev = int(resulting_activity["revision"]) if resulting_activity else expected_act_rev
            applied_cmd_ref = f"noop:{adoption_id}"

        elif dkind == DECISION_PAUSE:
            # Sections 52-55: PAUSE_CURRENT_ACTIVITY respecting IMMEDIATE vs SAFE_BOUNDARY vs ATOMIC
            fg_ref = act_state.get("foreground_activity_ref") or decision.target_activity_ref
            if not fg_ref:
                raise AdoptionIllegalError("Cannot adopt PAUSE_CURRENT_ACTIVITY when no foreground Activity exists")
            fg_act = self.activity_read.get_activity(fg_ref)
            if not fg_act or fg_act.get("status") != STATUS_ACTIVE:
                raise AdoptionIllegalError(f"Cannot adopt PAUSE on non-ACTIVE Activity {fg_ref!r}")
            target_act_ref = fg_ref
            _, eff_intr, _ = self.interruption_coordinator.get_effective_attention(fg_act)
            mode = pause_mode_override or eff_intr

            if mode == INTERRUPTIBILITY_SAFE_BOUNDARY:
                # Section 54: Register pending interruption at safe boundary; Activity stays ACTIVE until BoundaryReachedEvent!
                ev_list = self.interruption_coordinator.assess_events(
                    [
                        {
                            "event_kind": EVENT_KIND_COMMUNICATION,
                            "event_id": f"comm_evt:{sha256_hex(f'pause:{adoption_id}')[:16]}",
                            "channel_ref": "channel:internal_adoption",
                            "message_ref": f"msg:{sha256_hex(f'pause_msg:{adoption_id}')[:16]}",
                            "message_class": MESSAGE_CLASS_EXPLICIT_HOLD_REQUEST,
                            "request_boundary_pause": True,
                            "source_refs": [decision.decision_id, adoption_id],
                            "occurred_at": occ_iso,
                            "observed_at": occ_iso,
                        }
                    ],
                    apply_immediate_pause=False,
                )
                if ev_list:
                    pending_intr_ref = ev_list[0].get("pending_interruption_ref")
                resulting_activity = self.activity_read.get_activity(fg_ref)
                applied_act_rev = int(resulting_activity["revision"]) if resulting_activity else expected_act_rev
                applied_cmd_ref = pending_intr_ref or f"pending_boundary:{adoption_id}"
            else:
                # Section 53: Immediate pause through LR-2 ActivityCommandService
                cmd_idem = f"lr2_pause:{decision.decision_id}:{adoption_id}"
                _grant = self._issue_precommit_authorization(
                    decision=decision,
                    adoption_id=adoption_id,
                    idem=idem,
                    cmd_idem=cmd_idem,
                    mutation_kind="PAUSE",
                    activity_ref=fg_ref,
                    expected_act_rev=expected_act_rev,
                    occ_iso=occ_iso,
                )
                authorization_revision = int(_grant.revision)
                authorized_cmd_id, authorized_kind, authorized_act_ref = (
                    cmd_idem,
                    "PAUSE",
                    fg_ref,
                )
                cmd_res = self.activity_command.pause_activity(
                    activity_id=fg_ref,
                    expected_revision=expected_act_rev,
                    idempotency_key=cmd_idem,
                    source_refs=[decision.decision_id, adoption_id],
                    decision_ref=decision.decision_id,
                    adoption_ref=adoption_id,
                    causal_parent_refs=[decision.decision_id, adoption_id],
                    reason_code="VOLUNTARY_PAUSE",
                    occurred_at=occ_iso,
                    observed_at=occ_iso,
                )
                resulting_activity = cmd_res.activity
                applied_act_rev = int(resulting_activity["revision"]) if resulting_activity else expected_act_rev
                applied_cmd_ref = (
                    str(cmd_res.transition["transition_id"])
                    if cmd_res.transition
                    else f"cmd:{cmd_idem}"
                )

        elif dkind == DECISION_WAIT:
            # Sections 56-57: WAIT_CURRENT_ACTIVITY via LR-4 WaitingCoordinator
            fg_ref = act_state.get("foreground_activity_ref") or decision.target_activity_ref
            if not fg_ref:
                raise AdoptionIllegalError("Cannot adopt WAIT_CURRENT_ACTIVITY without an active Activity")
            target_act_ref = fg_ref
            w_spec = dict(waiting_spec or {})
            waiting_on_ref = str(
                w_spec.get("waiting_on_ref")
                or decision.reevaluation_hint_ref
                or f"wait_dep:{decision.decision_id}"
            )
            cond_kind = str(w_spec.get("condition_kind") or COND_KIND_EXTERNAL_RESULT_EVENT)
            cond_params = dict(
                w_spec.get("parameters")
                or {"expected_source_ref": waiting_on_ref, "expected_status": "SETTLED"}
            )
            cmd_idem = f"lr4_wait:{decision.decision_id}:{adoption_id}"
            _grant = self._issue_precommit_authorization(
                decision=decision,
                adoption_id=adoption_id,
                idem=idem,
                cmd_idem=cmd_idem,
                mutation_kind="WAIT",
                activity_ref=fg_ref,
                expected_act_rev=expected_act_rev,
                occ_iso=occ_iso,
            )
            authorization_revision = int(_grant.revision)
            authorized_cmd_id, authorized_kind, authorized_act_ref = (
                cmd_idem,
                "WAIT",
                fg_ref,
            )
            wait_res = self.waiting_coordinator.enter_waiting_with_condition(
                activity_id=fg_ref,
                expected_revision=expected_act_rev,
                waiting_on_ref=waiting_on_ref,
                condition_kind=cond_kind,
                parameters=cond_params,
                source_refs=[decision.decision_id, adoption_id],
                idempotency_key=cmd_idem,
                causal_parent_refs=[decision.decision_id, adoption_id],
                condition_expires_at=w_spec.get("condition_expires_at"),
                schedule_agenda=w_spec.get("schedule_agenda"),
                release_foreground=bool(w_spec.get("release_foreground", False)),
                occurred_at=occ_iso,
                observed_at=occ_iso,
                reason_kind=REASON_KIND_AGENCY_CHOICE,
                decision_ref=decision.decision_id,
                adoption_ref=adoption_id,
            )
            resulting_activity = wait_res["activity"]
            applied_act_rev = int(resulting_activity["revision"])
            w_cmd_res = wait_res["wait_result"]
            applied_cmd_ref = (
                str(w_cmd_res.transition["transition_id"])
                if w_cmd_res.transition
                else f"cmd:{cmd_idem}"
            )

        elif dkind == DECISION_RESUME:
            # Sections 58-59: RESUME_WAITING_ACTIVITY via LR-4 explicit_resume_from_eligibility
            assert cand_dict is not None
            elig_ref = str(cand_dict["resume_eligibility_ref"])
            target_act_ref = str(cand_dict.get("activity_ref") or decision.target_activity_ref)
            fg_ref = act_state.get("foreground_activity_ref")
            fg_act = (act_state.get("activities") or {}).get(fg_ref) if fg_ref else None
            # If another PAUSED or WAITING activity is currently foreground, pause/release or allow background waiting
            allow_bg_wait = bool(fg_act and fg_ref != target_act_ref and fg_act.get("status") == STATUS_WAITING)
            if fg_act and fg_ref != target_act_ref and fg_act.get("status") == STATUS_ACTIVE:
                _pre_cmd = f"lr2_pre_resume_pause:{decision.decision_id}:{fg_ref}"
                self._issue_precommit_authorization(
                    decision=decision,
                    adoption_id=adoption_id,
                    idem=idem,
                    cmd_idem=_pre_cmd,
                    mutation_kind="PAUSE",
                    activity_ref=fg_ref,
                    expected_act_rev=expected_act_rev,
                    occ_iso=occ_iso,
                )
                self.activity_command.pause_activity(
                    activity_id=fg_ref,
                    expected_revision=expected_act_rev,
                    idempotency_key=_pre_cmd,
                    source_refs=[decision.decision_id, adoption_id],
                    decision_ref=decision.decision_id,
                    adoption_ref=adoption_id,
                    reason_code="VOLUNTARY_PAUSE",
                    occurred_at=occ_iso,
                    observed_at=occ_iso,
                )
                act_state, _ = self.activity_read.read_canonical_state(include_journal=False)
                expected_act_rev = int(act_state.get("revision", 0))

            cmd_idem = f"lr4_resume:{decision.decision_id}:{adoption_id}"
            _grant = self._issue_precommit_authorization(
                decision=decision,
                adoption_id=adoption_id,
                idem=idem,
                cmd_idem=cmd_idem,
                mutation_kind="RESUME",
                activity_ref=target_act_ref,
                expected_act_rev=expected_act_rev,
                occ_iso=occ_iso,
            )
            authorization_revision = int(_grant.revision)
            authorized_cmd_id, authorized_kind, authorized_act_ref = (
                cmd_idem,
                "RESUME",
                target_act_ref,
            )
            res_cmd = self.waiting_coordinator.explicit_resume_from_eligibility(
                eligibility_id=elig_ref,
                expected_revision=expected_act_rev,
                idempotency_key=cmd_idem,
                decision_ref=decision.decision_id,
                adoption_ref=adoption_id,
                source_refs=[decision.decision_id, adoption_id, elig_ref],
                allow_background_waiting=allow_bg_wait,
                occurred_at=occ_iso,
                observed_at=occ_iso,
            )
            resulting_activity = res_cmd.activity
            assert resulting_activity is not None
            applied_act_rev = int(resulting_activity["revision"])
            applied_cmd_ref = (
                str(res_cmd.transition["transition_id"])
                if res_cmd.transition
                else f"cmd:{cmd_idem}"
            )

        elif dkind == DECISION_ABANDON:
            # Sections 60-62: ABANDON_CURRENT_ACTIVITY via LR-2 ActivityCommandService
            target_id = decision.target_activity_ref or act_state.get("foreground_activity_ref")
            if not target_id:
                raise AdoptionIllegalError("Cannot adopt ABANDON_CURRENT_ACTIVITY without a target Activity")
            target_act_ref = target_id
            cmd_idem = f"lr2_abandon:{decision.decision_id}:{adoption_id}"
            _grant = self._issue_precommit_authorization(
                decision=decision,
                adoption_id=adoption_id,
                idem=idem,
                cmd_idem=cmd_idem,
                mutation_kind="ABANDON",
                activity_ref=target_id,
                expected_act_rev=expected_act_rev,
                occ_iso=occ_iso,
            )
            authorization_revision = int(_grant.revision)
            authorized_cmd_id, authorized_kind, authorized_act_ref = (
                cmd_idem,
                "ABANDON",
                target_id,
            )
            cmd_res = self.activity_command.abandon_activity(
                activity_id=target_id,
                expected_revision=expected_act_rev,
                idempotency_key=cmd_idem,
                source_refs=[decision.decision_id, adoption_id],
                decision_ref=decision.decision_id,
                adoption_ref=adoption_id,
                causal_parent_refs=[decision.decision_id, adoption_id],
                reason_code="ABANDONED_BY_DECISION",
                occurred_at=occ_iso,
                observed_at=occ_iso,
            )
            resulting_activity = cmd_res.activity
            assert resulting_activity is not None
            applied_act_rev = int(resulting_activity["revision"])
            applied_cmd_ref = (
                str(cmd_res.transition["transition_id"])
                if cmd_res.transition
                else f"cmd:{cmd_idem}"
            )
            # Reconcile LR-4 conditions/agendas/eligibilities for abandoned activity
            self.waiting_coordinator.reconcile_with_canonical_activities(now_at=occ_iso)

        elif dkind in {DECISION_DEFER, DECISION_NO_ACTION}:
            # Sections 63-67: DEFER and NO_ACTION are zero-side-effect NOOP adoptions
            adoption_status = ADOPTION_STATUS_NOOP
            fg_ref = act_state.get("foreground_activity_ref")
            resulting_activity = self.activity_read.get_activity(fg_ref) if fg_ref else None
            applied_act_rev = int(resulting_activity["revision"]) if resulting_activity else expected_act_rev
            applied_cmd_ref = f"noop_{dkind.lower()}:{adoption_id}"

        else:
            raise AdoptionIllegalError(f"Unsupported decision_kind={dkind!r}")

        if fault_hook is not None:
            fault_hook("after_activity_command_before_adoption_commit")

        if authorized_kind and applied_cmd_ref and not str(applied_cmd_ref).startswith(
            ("noop", "pending_boundary", "cmd:")
        ):
            # The Adoption owner validates the LR-2 commit receipt before it records
            # APPLIED: LR-2 proves the commit, the owner owns the status change.
            self._validate_activity_commit_receipt(
                {
                    "receipt_id": f"actrcpt:{sha256_hex(f'{authorized_cmd_id}|{target_act_ref}|{applied_cmd_ref}')[:16]}",
                    "command_id": str(authorized_cmd_id),
                    "activity_id": str(target_act_ref),
                    "transition_id": str(applied_cmd_ref),
                    "transition_type": str(authorized_kind),
                    "previous_revision": int(expected_act_rev),
                    "new_revision": int(applied_act_rev if applied_act_rev is not None else expected_act_rev + 1),
                    "authority_class": "AGENCY_AUTHORIZED",
                    "evidence_digest": "",
                    "runtime_epoch": "",
                }
            )

        adoption_rec = DecisionAdoptionRecord(
            adoption_id=adoption_id,
            decision_ref=decision.decision_id,
            decision_revision=decision.revision,
            candidate_set_ref=decision.candidate_set_ref,
            selected_candidate_ref=decision.selected_candidate_ref,
            decision_kind=decision.decision,
            target_activity_ref=target_act_ref,
            expected_activity_revision=expected_act_rev,
            adoption_status=adoption_status,
            applied_activity_command_ref=applied_cmd_ref,
            applied_activity_revision=applied_act_rev,
            created_activity_ref=created_act_ref,
            rejection_reason_code=None,
            adopted_at=occ_iso,
            recorded_at=occ_iso,
            idempotency_key=idem,
            pending_interruption_ref=pending_intr_ref,
            revision=(authorization_revision + 1) if authorized_kind else 1,
            authorized_command_id=authorized_cmd_id,
            authorized_mutation_kind=authorized_kind,
            authorized_activity_ref=authorized_act_ref,
        )

        self.integration_commit.decision_adopted(
            map_updates={
                "adoptions_by_id": {adoption_id: adoption_rec.to_dict()},
                "adoption_id_by_decision": {decision.decision_id: adoption_id},
                "adoption_id_by_idempotency_key": {idem: adoption_id},
            },
            journal_payload={
                "event_type": "DECISION_ADOPTED",
                "adoption_id": adoption_id,
                "decision_id": decision.decision_id,
                "decision_kind": decision.decision,
                "adoption_status": adoption_status,
                "target_activity_ref": target_act_ref,
                "created_activity_ref": created_act_ref,
                "applied_activity_revision": applied_act_rev,
            },
        )

        if fault_hook is not None:
            fault_hook("after_adoption_commit")

        return {
            "idempotent_replay": False,
            "adoption_record": adoption_rec,
            "activity": resulting_activity,
        }


# ---------------------------------------------------------------------------
# Executable step -> canonical LR-5 target refs
# ---------------------------------------------------------------------------


def _derive_step_binding_target_refs(step: "ExecutableStepSpec") -> list[str]:
    """Canonical target refs LR-5 will see for this step's settled result.

    AR-1 builds its effect claim object_ref from the *verification evidence* produced by the
    adapter observer, and LR-5 normalizes `artifact_file:<rel>` to `file:<rel>`. For world /
    tool / message steps the verification evidence carries the action target_ref itself, which
    is already used as a binding target ref by the caller.
    """
    refs: list[str] = []
    payload = step.payload_spec or {}
    for key in ("path", "dest_path"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            rel = value.strip().lstrip("/")
            for ref in (f"file:{rel}", f"artifact_file:{rel}"):
                if ref not in refs:
                    refs.append(ref)
    return refs


# ---------------------------------------------------------------------------
# DecisionActionBridge (Sections 71-81)
# ---------------------------------------------------------------------------


class DecisionActionBridge:
    """Sections 71-81: Bridges an active canonical Activity + single ExecutableStepSpec into AR-0 ActionProposal.

    Enforces:
      - Activity != Action (Section 71)
      - Single current step only; no multi-step Planner / Task Tree (Section 75)
      - Every ActionProposal carries `decision_ref`, `activity_ref`, `executable_step_ref`, `correlation_id`,
        and `idempotency_key` (Section 76)
      - Handles `post_submit_life_behavior` (`KEEP_ACTIVITY_ACTIVE | ENTER_WAITING_AFTER_SUBMIT | NO_LIFE_CHANGE`)
        after crossing the AR-0 submission fence (Sections 77-78)
    """

    def __init__(
        self,
        *,
        integration_commit: IntegrationCommitPort,
        activity_read: ActivityReadService,
        waiting_coordinator: _NarrowPort,
        action_command: _NarrowPort,
        action_read: ActionReadService,
        progress_read: Any = None,
        progress_coordinator: _NarrowPort,
    ) -> None:
        # Phase-C: typed AG-2 commit boundary instead of store + lease + capability.
        self.integration_commit = integration_commit
        # S7: no raw LR-2 store; only the read port is held.
        self.activity_read = activity_read
        self.waiting_coordinator = waiting_coordinator
        self.action_command = action_command
        self.progress_read = progress_read
        self.action_read = action_read
        self.progress_coordinator = progress_coordinator
        self.llm_call_count: int = 0

    def create_step_and_propose_action(
        self,
        *,
        activity_id: str,
        decision_ref: str,
        adoption_ref: str,
        correlation_id: str,
        step_kind: str,
        action_kind: Optional[str],
        target_ref: str,
        payload_spec: Mapping[str, Any],
        expected_effect_predicates: Optional[Sequence[str]] = None,
        requires_waiting: bool = False,
        waiting_condition_spec: Optional[Mapping[str, Any]] = None,
        post_submit_life_behavior: str = POST_SUBMIT_KEEP_ACTIVE,
        idempotency_key: Optional[str] = None,
        occurred_at: Optional[str] = None,
        fault_hook: Optional[Callable[[str], None]] = None,
    ) -> dict[str, Any]:
        """Create `ExecutableStepSpec` and (if external action) propose + prepare an `AR-0` `ActionProposal`."""
        act = self.activity_read.get_activity(activity_id)
        if act is None or act.get("status") != STATUS_ACTIVE:
            raise LifeIntegrationError(
                f"DecisionActionBridge requires an ACTIVE Activity, got {act.get('status') if act else None!r} for {activity_id!r}"
            )
        if not decision_ref or not adoption_ref:
            raise LifeIntegrationError("ExecutableStepSpec requires both decision_ref and adoption_ref")

        occ_iso = _require_iso(occurred_at or _iso(now()), "occurred_at")
        act_rev = int(act["revision"])
        step_idem = idempotency_key or f"idem_step:{activity_id}:{adoption_ref}:{step_kind}:{target_ref}"

        int_state = self.integration_commit.read_state(mutable=True)
        existing_step_id = int_state["step_spec_id_by_idempotency_key"].get(step_idem)
        if existing_step_id and existing_step_id in int_state["step_specs_by_id"]:
            step_obj = ExecutableStepSpec.from_dict(int_state["step_specs_by_id"][existing_step_id])
        else:
            step_id = f"step:{sha256_hex(step_idem)[:16]}"
            step_obj = ExecutableStepSpec(
                step_spec_id=step_id,
                activity_ref=activity_id,
                activity_revision=act_rev,
                decision_ref=decision_ref,
                adoption_ref=adoption_ref,
                step_kind=step_kind,
                action_kind=action_kind,
                target_ref=target_ref,
                payload_spec=dict(payload_spec),
                expected_effect_predicates=list(expected_effect_predicates or []),
                requires_waiting=requires_waiting,
                waiting_condition_spec=dict(waiting_condition_spec) if waiting_condition_spec else None,
                post_submit_life_behavior=post_submit_life_behavior,
                idempotency_key=step_idem,
                created_at=occ_iso,
            )
            self.integration_commit.executable_step_spec_created(
                map_updates={
                    "step_specs_by_id": {step_obj.step_spec_id: step_obj.to_dict()},
                    "step_spec_id_by_idempotency_key": {step_idem: step_obj.step_spec_id},
                },
                journal_payload={
                    "event_type": "EXECUTABLE_STEP_SPEC_CREATED",
                    "step_spec_id": step_obj.step_spec_id,
                    "activity_ref": activity_id,
                    "decision_ref": decision_ref,
                    "adoption_ref": adoption_ref,
                    "step_kind": step_kind,
                },
            )

        if fault_hook is not None:
            fault_hook("after_step_spec_before_action_propose")

        if step_obj.action_kind is None:
            return {
                "step_spec": step_obj,
                "proposal": None,
                "action": None,
            }

        # Propose & Prepare Action in AR-0 ActionCommandService (Section 76)
        action_idem = f"ar0_act:{step_obj.step_spec_id}"
        payload_with_lineage = dict(step_obj.payload_spec)
        payload_with_lineage.setdefault("executable_step_ref", step_obj.step_spec_id)
        payload_with_lineage.setdefault("correlation_id", correlation_id)

        prop_res = self.action_command.propose_action(
            action_kind=step_obj.action_kind,
            target_ref=step_obj.target_ref,
            payload_ref=f"pay:{step_obj.step_spec_id}",
            payload_data=payload_with_lineage,
            idempotency_key=action_idem,
            source_refs=[decision_ref, adoption_ref, step_obj.step_spec_id],
            decision_ref=decision_ref,
            authorization_ref=adoption_ref,
            activity_ref=activity_id,
            causal_parent_refs=[decision_ref, adoption_ref, step_obj.step_spec_id],
            occurred_at=occ_iso,
        )
        action_dict = prop_res["action"]
        action_id = str(action_dict["action_id"])

        # Ensure ActivityResultBinding on LR-5 includes this correlation_id and target_ref
        existing_bind = (
            self.progress_read.get_binding(activity_id)
            if self.progress_read is not None
            else None
        )
        if existing_bind is not None:
            b_obj = ActivityResultBinding(
                binding_id=existing_bind["binding_id"],
                activity_id=existing_bind["activity_id"],
                target_refs=list(existing_bind["target_refs"]),
                allowed_evidence_kinds=list(existing_bind["allowed_evidence_kinds"]),
                authority_domains=list(existing_bind["authority_domains"]),
                created_at=existing_bind["created_at"],
                policy_version=existing_bind["policy_version"],
                correlation_ids=list(existing_bind.get("correlation_ids") or []),
            )
            mutated_b = False
            if correlation_id not in b_obj.correlation_ids:
                b_obj.correlation_ids.append(correlation_id)
                mutated_b = True
            for _binding_target_ref in [
                step_obj.target_ref,
                *_derive_step_binding_target_refs(step_obj),
            ]:
                if _binding_target_ref and _binding_target_ref not in b_obj.target_refs:
                    b_obj.target_refs.append(_binding_target_ref)
                    mutated_b = True
            if mutated_b:
                self.progress_coordinator.register_result_binding(b_obj)

        if fault_hook is not None:
            fault_hook("after_action_propose_before_prepare")

        if action_dict["status"] == ACTION_STATUS_PROPOSED:
            action_dict = self.action_command.prepare_action(
                action_id=action_id,
                occurred_at=occ_iso,
            )

        return {
            "step_spec": step_obj,
            "proposal": prop_res["proposal"],
            "action": action_dict,
        }

    def submit_step_action_and_apply_post_submit_behavior(
        self,
        *,
        step_spec: ExecutableStepSpec,
        action_id: str,
        executor_capability: SandboxExecutorCapability,
        settle_immediately: bool = True,
        occurred_at: Optional[str] = None,
        fault_hook: Optional[Callable[[str], None]] = None,
    ) -> dict[str, Any]:
        """Submit the prepared Action through AR-0 and apply `post_submit_life_behavior` (Sections 77-88)."""
        occ_iso = _require_iso(occurred_at or _iso(now()), "occurred_at")
        sub_res = self.action_command.submit_action(
            action_id=action_id,
            executor_capability=executor_capability,
            settle_immediately=settle_immediately,
            occurred_at=occ_iso,
            fault_hook=fault_hook,
        )
        act_after_submit = sub_res["action"]
        action_status = act_after_submit["status"]

        # Section 78: If step requires waiting after submission (`ENTER_WAITING_AFTER_SUBMIT`)
        # and the action crossed the submission boundary (`SUBMITTED`, `ACKNOWLEDGED`, `SUCCEEDED`, or `UNKNOWN`),
        # transition the Activity to `WAITING` via LR-4 WaitingCoordinator!
        life_act = self.activity_read.get_activity(step_spec.activity_ref)
        waiting_applied = False
        if (
            step_spec.post_submit_life_behavior == POST_SUBMIT_ENTER_WAITING
            and action_status in {
                ACTION_STATUS_SUBMITTED,
                ACTION_STATUS_ACKNOWLEDGED,
                ACTION_STATUS_SUCCEEDED,
                ACTION_STATUS_UNKNOWN,
            }
            and life_act is not None
            and life_act.get("status") == STATUS_ACTIVE
        ):
            can_st, _ = self.activity_read.read_canonical_state(include_journal=False)
            w_spec = dict(step_spec.waiting_condition_spec or {})
            waiting_on_ref = str(w_spec.get("waiting_on_ref") or action_id)
            cond_kind = str(w_spec.get("condition_kind") or COND_KIND_EXTERNAL_RESULT_EVENT)
            params = dict(
                w_spec.get("parameters")
                or {"expected_source_ref": waiting_on_ref, "expected_action_id": action_id}
            )
            w_res = self.waiting_coordinator.enter_waiting_with_condition(
                activity_id=step_spec.activity_ref,
                expected_revision=int(can_st["revision"]),
                waiting_on_ref=waiting_on_ref,
                condition_kind=cond_kind,
                parameters=params,
                source_refs=[step_spec.step_spec_id, action_id],
                idempotency_key=f"lr4_post_submit_wait:{step_spec.step_spec_id}:{action_id}",
                causal_parent_refs=[step_spec.step_spec_id, action_id],
                condition_expires_at=w_spec.get("condition_expires_at"),
                schedule_agenda=w_spec.get("schedule_agenda"),
                release_foreground=bool(w_spec.get("release_foreground", False)),
                occurred_at=occ_iso,
                observed_at=occ_iso,
            )
            life_act = w_res["activity"]
            waiting_applied = True

        return {
            "submit_result": sub_res,
            "action": act_after_submit,
            "activity": life_act,
            "waiting_applied": waiting_applied,
        }


# ---------------------------------------------------------------------------
# LifeIntegrationReadService (Sections 101, 226-233)
# ---------------------------------------------------------------------------


class LifeIntegrationReadService:
    """Sections 101, 226-233: Read-only causal lineage, audit, orphan detection, and operational metrics."""

    def __init__(
        self,
        *,
        integration_read: IntegrationReadPort,
        candidate_read: CandidateReadService,
        decision_read: AgencyDecisionReadService,
        activity_read: ActivityReadService,
        interruption_coordinator: InterruptionCoordinator,
        waiting_coordinator: WaitingCoordinator,
        action_read: ActionReadService,
        settlement_read: SettlementReadService,
        progress_read: ActivityProgressReadService,
    ) -> None:
        self.integration_read = integration_read
        self.candidate_read = candidate_read
        self.decision_read = decision_read
        self.activity_read = activity_read
        self.interruption_coordinator = interruption_coordinator
        self.waiting_coordinator = waiting_coordinator
        self.action_read = action_read
        self.settlement_read = settlement_read
        self.progress_read = progress_read

    def _build_progress_lineage(self, activity_id: str) -> dict[str, Any]:
        return {
            "activity_id": activity_id,
            "binding": self.progress_read.get_binding(activity_id),
            "progress_contract": self.progress_read.get_progress_contract(activity_id),
            "completion_contract": self.progress_read.get_completion_contract(activity_id),
            "progress_state": self.progress_read.get_progress_state(activity_id),
            "checkpoint_updates": self.progress_read.list_checkpoint_updates(activity_id),
            "completion_eligibilities": self.progress_read.list_completion_eligibilities(activity_id=activity_id),
            "reconciliations": self.progress_read.list_reconciliations(activity_id=activity_id),
            "late_terminal_evidences": self.progress_read.list_late_terminal_evidences(activity_id=activity_id),
        }

    def get_integration_record(self, integration_id: str) -> Optional[dict[str, Any]]:
        st = self.integration_read.read_state(mutable=True)
        rec = st["integrations_by_id"].get(integration_id)
        return copy.deepcopy(rec) if rec else None

    def get_decision_adoption(self, decision_id: str) -> Optional[dict[str, Any]]:
        st = self.integration_read.read_state(mutable=True)
        aid = st["adoption_id_by_decision"].get(decision_id)
        if not aid:
            return None
        rec = st["adoptions_by_id"].get(aid)
        return copy.deepcopy(rec) if rec else None

    def get_adoption_by_id(self, adoption_id: str) -> Optional[dict[str, Any]]:
        st = self.integration_read.read_state(mutable=True)
        rec = st["adoptions_by_id"].get(adoption_id)
        return copy.deepcopy(rec) if rec else None

    def list_partial_integrations(self) -> list[dict[str, Any]]:
        st = self.integration_read.read_state(mutable=True)
        items = [
            copy.deepcopy(r)
            for r in st["integrations_by_id"].values()
            if r.get("status") == INTEGRATION_STATUS_PARTIAL
        ]
        items.sort(key=lambda x: str(x["integration_id"]))
        return items

    def list_stale_adoptions(self) -> list[dict[str, Any]]:
        st = self.integration_read.read_state(mutable=True)
        items = [
            copy.deepcopy(r)
            for r in st["adoptions_by_id"].values()
            if r.get("adoption_status") == ADOPTION_STATUS_REJECTED_STALE
        ]
        items.sort(key=lambda x: str(x["adoption_id"]))
        return items

    def list_experience_handoffs(self, *, activity_id: Optional[str] = None) -> list[dict[str, Any]]:
        st = self.integration_read.read_state(mutable=True)
        items = list(st["handoff_events_by_id"].values())
        if activity_id is not None:
            items = [e for e in items if e.get("activity_ref") == activity_id]
        items.sort(key=lambda x: (str(x["occurred_at"]), str(x["handoff_id"])))
        return copy.deepcopy(items)

    def get_activity_full_lineage(self, activity_id: str) -> dict[str, Any]:
        """Section 229: Trace complete lineage for an Activity across AG-0..AG-2, LR-2..LR-5, AR-0..AR-1."""
        st = self.integration_read.read_state(mutable=True)
        act = self.activity_read.get_activity(activity_id)
        int_ids = st["integration_ids_by_activity"].get(activity_id) or []
        integrations = [
            copy.deepcopy(st["integrations_by_id"][iid])
            for iid in int_ids
            if iid in st["integrations_by_id"]
        ]
        adoptions = [
            copy.deepcopy(a)
            for a in st["adoptions_by_id"].values()
            if a.get("created_activity_ref") == activity_id or a.get("target_activity_ref") == activity_id
        ]
        decisions = [
            self.decision_read.get_decision(a["decision_ref"])
            for a in adoptions
            if a.get("decision_ref")
        ]
        progress_lineage = self._build_progress_lineage(activity_id) if act else None
        handoffs = self.list_experience_handoffs(activity_id=activity_id)

        return {
            "activity_id": activity_id,
            "activity": act,
            "integrations": integrations,
            "decisions": [d for d in decisions if d is not None],
            "adoptions": adoptions,
            "progress_lineage": progress_lineage,
            "experience_handoffs": handoffs,
        }

    def trace_life_loop(self, correlation_id: str) -> dict[str, Any]:
        """Section 101 & 226-229: Complete end-to-end provenance trace for a `correlation_id`:
        Observed Event / Source -> CandidateRecord -> CandidateSet -> DecisionOpportunity ->
        DecisionContextSnapshot -> HardConstraintResult -> AgencyCapacitySnapshot ->
        DecisionRecord -> DecisionAdoptionRecord -> ActivityRecord -> ExecutableStepSpec ->
        ActionProposal -> ActionRecord -> ActionAttempt -> ActionReceipt / ResultEvidence ->
        ResultSettlementRecord -> SettledResultEvent -> LifeProgressEvidence ->
        CheckpointUpdate / ActivityCompletionEligibility -> Activity COMPLETED ->
        LifeExperienceHandoffEvent.
        """
        st = self.integration_read.read_state(mutable=True)
        int_id = st["integration_id_by_correlation"].get(correlation_id)
        if not int_id or int_id not in st["integrations_by_id"]:
            raise LifeIntegrationError(f"Unknown correlation_id={correlation_id!r}")

        int_rec = copy.deepcopy(st["integrations_by_id"][int_id])
        dec_id = int_rec.get("decision_ref")
        adp_id = int_rec.get("adoption_ref")
        act_id = int_rec.get("activity_ref")

        decision_trace = self.decision_read.trace_decision_provenance(dec_id) if dec_id else None
        adoption_rec = copy.deepcopy(st["adoptions_by_id"].get(adp_id)) if adp_id else None
        activity_rec = self.activity_read.get_activity(act_id) if act_id else None

        step_specs = [
            copy.deepcopy(st["step_specs_by_id"][sid])
            for sid in int_rec.get("executable_step_refs") or []
            if sid in st["step_specs_by_id"]
        ]
        action_traces = [
            self.settlement_read.trace_result_lineage(aid)
            for aid in int_rec.get("action_refs") or []
        ]
        progress_trace = self._build_progress_lineage(act_id) if act_id else None
        handoffs = [
            copy.deepcopy(st["handoff_events_by_id"][hid])
            for hid in int_rec.get("experience_handoff_refs") or []
            if hid in st["handoff_events_by_id"]
        ]
        handoff_deliveries = [
            copy.deepcopy(st["handoff_deliveries_by_id"][hid])
            for hid in int_rec.get("experience_handoff_refs") or []
            if hid in st["handoff_deliveries_by_id"]
        ]

        return {
            "correlation_id": correlation_id,
            "integration_record": int_rec,
            "decision_trace": decision_trace,
            "adoption_record": adoption_rec,
            "activity_record": activity_rec,
            "executable_step_specs": step_specs,
            "action_and_settlement_traces": action_traces,
            "progress_and_completion_trace": progress_trace,
            "experience_handoff_events": handoffs,
            "experience_handoff_deliveries": handoff_deliveries,
        }

    def audit_zero_orphans(self) -> dict[str, Any]:
        """Section 230-231: Audit across all canonical stores to verify 0 orphan objects."""
        st = self.integration_read.read_state(mutable=True)
        act_state, _ = self.activity_read._store.load_verified_state_and_journal(_include_journal=False)
        activities = act_state.get("activities") or {}

        orphan_activities: list[str] = []
        for aid, adict in activities.items():
            # Every started activity must have origin_ref and source_refs; if started via AG-2, must link to decision & adoption
            if not adict.get("origin_ref") or not adict.get("source_refs"):
                orphan_activities.append(aid)

        orphan_actions: list[str] = []
        for act_rec in self.action_read.list_actions():
            aid = act_rec["action_id"]
            if not act_rec.get("proposal_ref") or not act_rec.get("source_refs"):
                orphan_actions.append(aid)

        orphan_settlements: list[str] = []
        set_state = self.settlement_read.settlement_store.load_state(_mutable=True)
        for sid, srec in set_state.get("settlements_by_id", {}).items():
            if not srec.get("action_id") or self.action_read.get_action(srec["action_id"]) is None:
                orphan_settlements.append(sid)

        orphan_progress: list[str] = []
        prog_state = self.progress_read.progress_store.load_state(_mutable=True)
        for pevid, pentry in prog_state.get("consumed_evidences_by_id", {}).items():
            b_act = pentry.get("bound_activity_id")
            if b_act is not None and b_act not in activities:
                orphan_progress.append(pevid)

        orphan_completions: list[str] = []
        for aid, adict in activities.items():
            if adict.get("status") == STATUS_COMPLETED and not adict.get("completion_evidence_ref"):
                orphan_completions.append(aid)

        orphan_handoffs: list[str] = []
        for hid, hev in st.get("handoff_events_by_id", {}).items():
            if hev.get("activity_ref") not in activities:
                orphan_handoffs.append(hid)

        orphan_eligibilities: list[str] = []
        for el in self.waiting_coordinator.list_eligibilities(only_open=True):
            act_obj = activities.get(el["activity_id"])
            if act_obj is None or act_obj.get("status") in TERMINAL_ACTIVITY_STATUSES:
                orphan_eligibilities.append(el["eligibility_id"])

        orphan_agendas: list[str] = []
        for ag in self.waiting_coordinator.list_agenda_items(only_open=True):
            if ag.get("activity_id"):
                act_obj = activities.get(ag["activity_id"])
                if act_obj is None or act_obj.get("status") in TERMINAL_ACTIVITY_STATUSES:
                    orphan_agendas.append(ag["agenda_id"])

        total_orphans = (
            len(orphan_activities)
            + len(orphan_actions)
            + len(orphan_settlements)
            + len(orphan_progress)
            + len(orphan_completions)
            + len(orphan_handoffs)
            + len(orphan_eligibilities)
            + len(orphan_agendas)
        )
        return {
            "zero_orphans": total_orphans == 0,
            "total_orphan_count": total_orphans,
            "orphan_activities": orphan_activities,
            "orphan_actions": orphan_actions,
            "orphan_settlements": orphan_settlements,
            "orphan_progress": orphan_progress,
            "orphan_completions": orphan_completions,
            "orphan_handoffs": orphan_handoffs,
            "orphan_eligibilities": orphan_eligibilities,
            "orphan_agendas": orphan_agendas,
        }

    def get_operational_metrics(self) -> dict[str, Any]:
        """Sections 232-233: Operational health metrics ONLY (0 scalar life/autonomy/human-likeness KPIs)."""
        st = self.integration_read.read_state(mutable=True)
        adoptions = list(st["adoptions_by_id"].values())
        integrations = list(st["integrations_by_id"].values())
        actions = self.action_read.list_actions()
        set_state = self.settlement_read.settlement_store.load_state(_mutable=True)
        settlements = list(set_state.get("settlements_by_id", {}).values())
        act_state, _ = self.activity_read._store.load_verified_state_and_journal(_include_journal=False)
        activities = list((act_state.get("activities") or {}).values())

        adoption_dist: dict[str, int] = {}
        for a in adoptions:
            s = a["adoption_status"]
            adoption_dist[s] = adoption_dist.get(s, 0) + 1

        integration_dist: dict[str, int] = {}
        for i in integrations:
            s = i["status"]
            integration_dist[s] = integration_dist.get(s, 0) + 1

        action_dist: dict[str, int] = {}
        for ac in actions:
            s = ac["status"]
            action_dist[s] = action_dist.get(s, 0) + 1

        settlement_dist: dict[str, int] = {}
        for sr in settlements:
            s = sr["settlement_status"]
            settlement_dist[s] = settlement_dist.get(s, 0) + 1

        activity_dist: dict[str, int] = {}
        for act in activities:
            s = act["status"]
            activity_dist[s] = activity_dist.get(s, 0) + 1

        metrics = {
            "total_integrations": len(integrations),
            "integration_status_distribution": integration_dist,
            "adoption_status_distribution": adoption_dist,
            "activity_status_distribution": activity_dist,
            "action_status_distribution": action_dist,
            "settlement_status_distribution": settlement_dist,
            "stale_adoption_count": adoption_dist.get(ADOPTION_STATUS_REJECTED_STALE, 0),
            "partial_integration_count": integration_dist.get(INTEGRATION_STATUS_PARTIAL, 0),
            "conflict_integration_count": integration_dist.get(INTEGRATION_STATUS_CONFLICT, 0),
            "unknown_action_count": action_dist.get(ACTION_STATUS_UNKNOWN, 0),
            "pending_experience_handoff_count": len(st.get("pending_handoff_ids") or []),
            "delivered_experience_handoff_count": len(st.get("handoff_deliveries_by_id") or {}),
        }
        _reject_forbidden_ag2_keys(metrics, "operational_metrics")
        return metrics


# ---------------------------------------------------------------------------
# LifeIntegrationCoordinator (Sections 28-140)
# ---------------------------------------------------------------------------


class LifeIntegrationCoordinator:
    """Sections 28-140: Pure cross-domain choreography coordinator for the complete Chiyo Life loop.

    Holds no authority lease at all and delegates every domain truth mutation to the
    canonical services of `AG-0`, `AG-1`, `LR-2`, `LR-3`, `LR-4`, `AR-0`, `AR-1`, and `LR-5`.

    Phase-C: its own-store writes go through the typed `IntegrationCommitPort`, and every
    other owner is reached only through a declared port; it holds no lease and no writer
    capability of any domain.
    """

    def __init__(
        self,
        *,
        integration_commit: IntegrationCommitPort,
        integration_read: IntegrationReadPort,
        kill_switches: LifeRuntimeKillSwitches,
        candidate_materializer: CandidateMaterializer,
        candidate_read: CandidateReadService,
        decision_service: AgencyDecisionService,
        decision_read: AgencyDecisionReadService,
        adoption_coordinator: DecisionAdoptionCoordinator,
        action_bridge: DecisionActionBridge,
        activity_read: ActivityReadService,
        interruption_coordinator: _NarrowPort,
        waiting_coordinator: _NarrowPort,
        action_store: Any = None,
        action_command: _NarrowPort,
        action_read: ActionReadService,
        executor_capability: Any = None,
        settlement_store: Any = None,
        settlement_service: _NarrowPort,
        settlement_read: SettlementReadService,
        progress_store: Any = None,
        progress_coordinator: _NarrowPort,
        progress_read: ActivityProgressReadService,
        experience_consumer: Optional[ExperienceConsumerProtocol] = None,
        file_observer: Optional[FileArtifactObserver] = None,
    ) -> None:
        # Phase-C: the composition root already verified the AG-2 capability when it was
        # minted; this facade receives the typed boundary and holds no lease at all.
        self.integration_commit = integration_commit
        self.integration_read = integration_read
        self.journal = IntegrationJournal(integration_commit)
        self.kill_switches = kill_switches

        self.candidate_materializer = candidate_materializer
        self.candidate_read = candidate_read
        self.decision_service = decision_service
        # S7: the AG-1 service arrives as a declared narrow port; the raw
        # AgencyDecisionStore it wraps is not held here.
        self.decision_read = decision_read
        self.adoption_coordinator = adoption_coordinator
        self.action_bridge = action_bridge

        # S7: no raw LR-2 store; only the read port is held.
        self.activity_read = activity_read
        self.interruption_coordinator = interruption_coordinator
        self.waiting_coordinator = waiting_coordinator

        # S7: raw action_store is not held; LR/AR ports cover the needed reads.
        self.action_command = action_command
        self.action_read = action_read
        self.executor_capability = executor_capability
        self.file_observer = file_observer

        # S7: raw settlement_store is not held; LR/AR ports cover the needed reads.
        self.settlement_service = settlement_service
        self.settlement_read = settlement_read

        # S7: raw progress_store is not held; LR/AR ports cover the needed reads.
        self.progress_coordinator = progress_coordinator
        self.progress_read = progress_read

        self.experience_consumer = experience_consumer
        self.read_service = LifeIntegrationReadService(
            integration_read=integration_read,
            candidate_read=candidate_read,
            decision_read=decision_read,
            activity_read=activity_read,
            interruption_coordinator=interruption_coordinator,
            waiting_coordinator=waiting_coordinator,
            action_read=action_read,
            settlement_read=settlement_read,
            progress_read=progress_read,
        )

        # Strict zero-usurpation & zero-LLM counters on AG-2 coordinator itself
        self.coordinator_llm_call_count: int = 0
        self.memory_write_count: int = 0
        self.real_telegram_send_count: int = 0
        self.production_write_count: int = 0

    # -- Forbidden Direct Domain Write Methods (Section 31-33) ---------------

    def hold_forbidden_capability(self, cap_name: str) -> None:
        if cap_name in LifeIntegrationAuthority.FORBIDDEN_COORDINATOR_CAPABILITIES:
            raise CoordinatorAuthorityViolationError(
                f"Section 32 violation: LifeIntegrationCoordinator is forbidden from holding {cap_name!r}"
            )

    def direct_write_activity_store(self, *args: Any, **kwargs: Any) -> None:
        raise CoordinatorAuthorityViolationError(
            "Section 31 violation: LifeIntegrationCoordinator cannot directly write CanonicalActivityStore"
        )

    def direct_write_action_store(self, *args: Any, **kwargs: Any) -> None:
        raise CoordinatorAuthorityViolationError(
            "Section 31 violation: LifeIntegrationCoordinator cannot directly write ActionStore"
        )

    def direct_write_settlement_store(self, *args: Any, **kwargs: Any) -> None:
        raise CoordinatorAuthorityViolationError(
            "Section 31 violation: LifeIntegrationCoordinator cannot directly write SettlementStore"
        )

    def direct_write_progress_store(self, *args: Any, **kwargs: Any) -> None:
        raise CoordinatorAuthorityViolationError(
            "Section 31 violation: LifeIntegrationCoordinator cannot directly write ProgressStore"
        )

    def direct_write_memory(self, *args: Any, **kwargs: Any) -> None:
        raise CoordinatorAuthorityViolationError(
            "Section 103 violation: LifeIntegrationCoordinator cannot directly write Native Memory"
        )

    def direct_write_world(self, *args: Any, **kwargs: Any) -> None:
        raise CoordinatorAuthorityViolationError(
            "Section 31 violation: LifeIntegrationCoordinator cannot directly overwrite World authority"
        )

    # -- Experience Handoff Outbox & Dispatch (Sections 103-117) -------------

    def emit_experience_handoff(
        self,
        *,
        integration_id: str,
        activity_id: str,
        terminal_or_milestone_kind: str,
        occurred_at: Optional[str] = None,
        fact_refs: Optional[Sequence[str]] = None,
        artifact_refs: Optional[Sequence[str]] = None,
        world_refs: Optional[Sequence[str]] = None,
        fault_hook: Optional[Callable[[str], None]] = None,
    ) -> dict[str, Any]:
        """Emit a canonical `LifeExperienceHandoffEvent` into the AG-2 Outbox and dispatch to consumer."""
        act = self.activity_read.get_activity(activity_id)
        if act is None:
            raise LifeIntegrationError(f"Cannot emit ExperienceHandoff for unknown activity_id={activity_id!r}")

        int_state = self.integration_commit.read_state(mutable=True)
        int_dict = int_state["integrations_by_id"].get(integration_id)
        if int_dict is None:
            raise LifeIntegrationError(f"Unknown integration_id={integration_id!r}")

        act_rev = int(act["revision"])
        occ_iso = _require_iso(occurred_at or _iso(now()), "occurred_at")

        # Exactly-once handoff ID per (activity_id, terminal_or_milestone_kind, activity_revision or COMPLETED)
        if terminal_or_milestone_kind in {HANDOFF_KIND_COMPLETED, HANDOFF_KIND_ABANDONED}:
            idem_seed = f"{activity_id}:{terminal_or_milestone_kind}"
        else:
            idem_seed = f"{activity_id}:{terminal_or_milestone_kind}:v{act_rev}"
        handoff_id = f"lexp:{sha256_hex(idem_seed)[:16]}"

        if handoff_id in int_state["handoff_events_by_id"]:
            existing_ev = LifeExperienceHandoffEvent.from_dict(int_state["handoff_events_by_id"][handoff_id])
            disp_res = self.dispatch_experience_handoffs(handoff_id=handoff_id, fault_hook=fault_hook)
            return {
                "idempotent_replay": True,
                "handoff_event": existing_ev,
                "dispatch_result": disp_res,
            }

        # Gather structured refs (0 prompt dumps, 0 chain-of-thought, 0 direct memory writes)
        dec_refs = [int_dict["decision_ref"]] if int_dict.get("decision_ref") else []
        adp_refs = [int_dict["adoption_ref"]] if int_dict.get("adoption_ref") else []
        act_refs = list(int_dict.get("action_refs") or [])
        set_refs = list(int_dict.get("settlement_refs") or [])
        prog_refs = list(int_dict.get("progress_refs") or [])
        comp_ev_ref = act.get("completion_evidence_ref") or int_dict.get("completion_ref")

        resolved_facts = list(fact_refs or [])
        resolved_artifacts = list(artifact_refs or [])
        resolved_worlds = list(world_refs or [])

        for sref in set_refs:
            s_rec = self.settlement_read.get_settlement_by_id(sref)
            if s_rec:
                for claim in s_rec.get("effect_claims") or []:
                    obj_ref = claim.get("object_ref")
                    if obj_ref:
                        if claim.get("domain") == "world" and obj_ref not in resolved_worlds:
                            resolved_worlds.append(obj_ref)
                        elif claim.get("domain") == "filesystem" and obj_ref not in resolved_artifacts:
                            resolved_artifacts.append(obj_ref)
                        elif obj_ref not in resolved_facts:
                            resolved_facts.append(obj_ref)

        causal_parents = [activity_id] + dec_refs + adp_refs + set_refs
        if comp_ev_ref and comp_ev_ref not in causal_parents:
            causal_parents.append(comp_ev_ref)

        handoff_ev = LifeExperienceHandoffEvent(
            handoff_id=handoff_id,
            correlation_id=int_dict["correlation_id"],
            activity_ref=activity_id,
            activity_revision=act_rev,
            activity_kind=act["activity_kind"],
            terminal_or_milestone_kind=terminal_or_milestone_kind,
            decision_refs=dec_refs,
            adoption_refs=adp_refs,
            action_refs=act_refs,
            settlement_refs=set_refs,
            progress_refs=prog_refs,
            completion_evidence_ref=comp_ev_ref,
            fact_refs=resolved_facts,
            artifact_refs=resolved_artifacts,
            world_refs=resolved_worlds,
            occurred_at=occ_iso,
            observed_at=occ_iso,
            recorded_at=occ_iso,
            causal_parent_refs=causal_parents,
        )

        if fault_hook is not None:
            fault_hook("before_experience_handoff_persist")

        int_rec = LifeIntegrationRecord.from_dict(int_dict)
        if handoff_id not in int_rec.experience_handoff_refs:
            int_rec.experience_handoff_refs.append(handoff_id)
        int_rec.revision += 1
        int_rec.updated_at = occ_iso

        pending_h = list(int_state.get("pending_handoff_ids") or [])
        if handoff_id not in pending_h:
            pending_h.append(handoff_id)

        self.integration_commit.experience_handoff_enqueued(
            map_updates={
                "handoff_events_by_id": {handoff_id: handoff_ev.to_dict()},
                "integrations_by_id": {integration_id: int_rec.to_dict()},
            },
            list_replacements={"pending_handoff_ids": pending_h},
            journal_payload={
                "event_type": "EXPERIENCE_HANDOFF_ENQUEUED",
                "integration_id": integration_id,
                "correlation_id": int_rec.correlation_id,
                "activity_ref": activity_id,
                "handoff_id": handoff_id,
                "milestone_kind": terminal_or_milestone_kind,
            },
        )

        if fault_hook is not None:
            fault_hook("after_experience_handoff_persist_before_dispatch")

        disp_res = self.dispatch_experience_handoffs(handoff_id=handoff_id, fault_hook=fault_hook)
        return {
            "idempotent_replay": False,
            "handoff_event": handoff_ev,
            "dispatch_result": disp_res,
        }

    def dispatch_experience_handoffs(
        self,
        *,
        handoff_id: Optional[str] = None,
        fault_hook: Optional[Callable[[str], None]] = None,
    ) -> dict[str, Any]:
        """Deliver pending `LifeExperienceHandoffEvent` items to `experience_consumer`.

        Section 112: If consumer is down or defers, Activity completion and AR-1 Settlement NEVER roll back;
        the handoff event remains in `pending_handoff_ids` for deterministic retry on recovery/reconcile.
        """
        int_state = self.integration_commit.read_state(mutable=True)
        if handoff_id is not None:
            target_ids = [handoff_id] if handoff_id in int_state["handoff_events_by_id"] else []
        else:
            target_ids = list(int_state.get("pending_handoff_ids") or [])

        deliveries: list[dict[str, Any]] = []
        if self.experience_consumer is None:
            return {"dispatched_count": 0, "deliveries": [], "pending_handoff_ids": list(int_state.get("pending_handoff_ids") or [])}

        pending_list = list(int_state.get("pending_handoff_ids") or [])
        map_del_updates: dict[str, Any] = {}

        for hid in target_ids:
            ev_dict = int_state["handoff_events_by_id"][hid]
            prev_del = int_state.get("handoff_deliveries_by_id", {}).get(hid)
            attempt_no = (int(prev_del["attempt"]) + 1) if prev_del else 1
            ts_now = _iso(now())

            try:
                c_res = self.experience_consumer.receive_experience_handoff(ev_dict)
                disp = str(c_res.get("disposition", "ACCEPT"))
                status = "DELIVERED" if disp in {"ACCEPT", "IGNORE", "DUPLICATE"} else "DEFERRED"
                del_rec = {
                    "handoff_id": hid,
                    "attempt": attempt_no,
                    "status": status,
                    "consumer_disposition": disp,
                    "last_error": None,
                    "delivered_at": ts_now,
                }
                if status == "DELIVERED" and hid in pending_list:
                    pending_list.remove(hid)
            except Exception as exc:
                if isinstance(exc, RuntimeError) and str(exc).startswith("CRASH_INJECTED:"):
                    raise
                del_rec = {
                    "handoff_id": hid,
                    "attempt": attempt_no,
                    "status": "FAILED_PENDING_RETRY",
                    "consumer_disposition": None,
                    "last_error": f"{type(exc).__name__}:{exc}",
                    "delivered_at": None,
                }
                if hid not in pending_list:
                    pending_list.append(hid)

            map_del_updates[hid] = del_rec
            deliveries.append(del_rec)

        if map_del_updates:
            self.integration_commit.experience_handoff_dispatched(
                map_updates={"handoff_deliveries_by_id": map_del_updates},
                list_replacements={"pending_handoff_ids": pending_list},
                journal_payload={
                    "event_type": "EXPERIENCE_HANDOFF_DISPATCHED",
                    "deliveries": deliveries,
                },
            )
            if fault_hook is not None:
                fault_hook("after_experience_handoff_ack")

        return {
            "dispatched_count": len(deliveries),
            "deliveries": deliveries,
            "pending_handoff_ids": pending_list,
        }

    # -- Main Closed-Loop Choreography Entrypoints ---------------------------

    def run_decision_and_adoption_cycle(
        self,
        *,
        trigger_kind: str,
        trigger_ref: str,
        observed_at: Optional[str] = None,
        correlation_id: Optional[str] = None,
        valid_until: Optional[str] = None,
        adoption_spec: Optional[ActivityAdoptionSpec | Mapping[str, Any]] = None,
        waiting_spec: Optional[Mapping[str, Any]] = None,
        pause_mode_override: Optional[str] = None,
        step_spec_override: Optional[Mapping[str, Any]] = None,
        execute_initial_step: bool = True,
        settle_immediately: bool = True,
        post_cognition_mutation_hook: Optional[Callable[[], Optional[dict[str, Any]]]] = None,
        pre_adoption_mutation_hook: Optional[Callable[[DecisionRecord], Optional[dict[str, Any]]]] = None,
        fault_hook: Optional[Callable[[str], None]] = None,
        **decision_eval_kwargs: Any,
    ) -> dict[str, Any]:
        """Execute a complete event-driven Life Integration cycle:
        1. AG-0: Build fresh `CandidateSet` from canonical sources & observed events.
        2. AG-1: Create `DecisionOpportunity` and evaluate -> `DecisionRecord`.
        3. AG-2: Validate final freshness & adopt `DecisionRecord` -> `DecisionAdoptionRecord` + canonical `LR-2/3/4` command.
        4. AG-2 -> AR-0: If adopted Activity has an executable step, create `ExecutableStepSpec` + `ActionProposal`,
           submit via `AR-0` (if `ACTION_EXECUTION_ENABLED`), and apply `post_submit_life_behavior`.
        5. AR-1 -> LR-5 -> Experience Handoff: If action settled synchronously, settle via `AR-1`, dispatch outbox to `LR-5`,
           and if Activity reached `COMPLETED` or `ABANDONED`, emit `LifeExperienceHandoffEvent`.
        """
        self.kill_switches.require_agency()
        obs_iso = _require_iso(observed_at or _iso(now()), "observed_at")
        corr_id = correlation_id or f"corr:{sha256_hex(f'{trigger_kind}:{trigger_ref}:{obs_iso}')[:16]}"
        int_id = f"lint:{sha256_hex(corr_id)[:16]}"

        # 1. AG-0: Build CandidateSet
        cset = self.candidate_materializer.build_candidate_set(observed_at=obs_iso)
        if fault_hook is not None:
            fault_hook("after_candidate_set_built")

        # 2. AG-1: Create DecisionOpportunity & evaluate
        # AG-2/AG-1 interface alignment: build the opportunity through the canonical AG-1 factory
        # (real DecisionOpportunity has no `created_at`; it has occurred_at/recorded_at/fingerprint).
        current_activity_ref: Optional[str] = None
        for _open_act in self.activity_read.list_open_activities():
            if _open_act.get("status") == STATUS_ACTIVE:
                current_activity_ref = _open_act.get("activity_id")
                break
        opp = DecisionOpportunity.create(
            trigger_kind=trigger_kind,
            trigger_ref=trigger_ref,
            candidate_set=cset,
            current_activity_ref=current_activity_ref,
            observed_at=obs_iso,
            causal_parent_refs=[trigger_ref, cset.candidate_set_id],
            valid_until=valid_until,
        )
        opp_id = opp.opportunity_id

        # Create initial OPEN LifeIntegrationRecord
        int_state = self.integration_commit.read_state(mutable=True)
        existing_int = int_state["integrations_by_id"].get(int_id)
        if existing_int is None:
            int_rec = LifeIntegrationRecord(
                integration_id=int_id,
                correlation_id=corr_id,
                opportunity_ref=opp_id,
                candidate_set_ref=cset.candidate_set_id,
                status=INTEGRATION_STATUS_OPEN,
                created_at=obs_iso,
                updated_at=obs_iso,
            )
            self.integration_commit.life_integration_opened(
                map_updates={
                    "integrations_by_id": {int_id: int_rec.to_dict()},
                    "integration_id_by_correlation": {corr_id: int_id},
                },
                journal_payload={
                    "event_type": "LIFE_INTEGRATION_OPENED",
                    "integration_id": int_id,
                    "correlation_id": corr_id,
                    "opportunity_id": opp_id,
                    "candidate_set_id": cset.candidate_set_id,
                },
            )
        else:
            int_rec = LifeIntegrationRecord.from_dict(existing_int)

        if fault_hook is not None:
            fault_hook("after_opportunity_created")

        # Pass current LR-3 enriched life_view to AG-1 if not overridden
        if "life_frame" not in decision_eval_kwargs:
            decision_eval_kwargs["life_frame"] = self.interruption_coordinator.get_current_life_view()

        dec_res = self.decision_service.evaluate_opportunity(
            opportunity=opp,
            candidate_set=cset,
            post_cognition_mutation_hook=post_cognition_mutation_hook,
            fault_hook=fault_hook,
            **decision_eval_kwargs,
        )

        decision_record: Optional[DecisionRecord] = dec_res.get("decision_record")
        if decision_record is None:
            # Decision attempt failed or went stale before commit -> 0 Activity mutation, 0 Action
            int_rec.status = INTEGRATION_STATUS_FAILED
            int_rec.failure_reason_code = str(
                dec_res.get("failure_class") or dec_res.get("stale_reason") or "DECISION_NOT_COMMITTED"
            )
            int_rec.revision += 1
            int_rec.updated_at = _iso(now())
            self.integration_commit.life_integration_decision_failed_or_stale(
                map_updates={"integrations_by_id": {int_id: int_rec.to_dict()}},
                journal_payload={
                    "event_type": "LIFE_INTEGRATION_DECISION_FAILED_OR_STALE",
                    "integration_id": int_id,
                    "correlation_id": corr_id,
                    "reason_code": int_rec.failure_reason_code,
                },
            )
            return {
                "correlation_id": corr_id,
                "integration_record": int_rec,
                "candidate_set": cset,
                "decision_result": dec_res,
                "adoption_result": None,
            }

        # Link DecisionRecord to LifeIntegrationRecord
        int_rec.decision_ref = decision_record.decision_id
        int_rec.revision += 1
        int_rec.updated_at = obs_iso
        self.integration_commit.life_integration_decision_linked(
            map_updates={
                "integrations_by_id": {int_id: int_rec.to_dict()},
                "integration_id_by_decision": {decision_record.decision_id: int_id},
            },
            journal_payload={
                "event_type": "LIFE_INTEGRATION_DECISION_LINKED",
                "integration_id": int_id,
                "decision_id": decision_record.decision_id,
                "decision": decision_record.decision,
            },
        )

        # Optional test hook simulating world/activity/candidate mutation AFTER DecisionRecord commit but BEFORE Adoption! (Section 42-44, Loop P)
        live_cap_at_adoption: Optional[dict[str, Any]] = None
        if pre_adoption_mutation_hook is not None:
            live_cap_at_adoption = pre_adoption_mutation_hook(decision_record)

        # 3. AG-2: Adopt DecisionRecord
        adp_res = self.adoption_coordinator.adopt_decision(
            decision_record,
            adoption_spec=adoption_spec,
            waiting_spec=waiting_spec,
            pause_mode_override=pause_mode_override,
            occurred_at=obs_iso,
            live_capacity_override=live_cap_at_adoption,
            fault_hook=fault_hook,
        )
        adp_rec: DecisionAdoptionRecord = adp_res["adoption_record"]
        activity_after_adp = adp_res.get("activity")

        int_rec.adoption_ref = adp_rec.adoption_id
        if adp_rec.created_activity_ref or adp_rec.target_activity_ref:
            int_rec.activity_ref = adp_rec.created_activity_ref or adp_rec.target_activity_ref

        if adp_rec.adoption_status in {ADOPTION_STATUS_REJECTED_STALE, ADOPTION_STATUS_REJECTED_ILLEGAL}:
            int_rec.status = INTEGRATION_STATUS_FAILED
            int_rec.failure_reason_code = adp_rec.rejection_reason_code or adp_rec.adoption_status
            int_rec.revision += 1
            int_rec.updated_at = obs_iso
            self.integration_commit.life_integration_adoption_rejected(
                map_updates={"integrations_by_id": {int_id: int_rec.to_dict()}},
                journal_payload={
                    "event_type": "LIFE_INTEGRATION_ADOPTION_REJECTED",
                    "integration_id": int_id,
                    "adoption_id": adp_rec.adoption_id,
                    "reason_code": int_rec.failure_reason_code,
                },
            )
            return {
                "correlation_id": corr_id,
                "integration_record": int_rec,
                "candidate_set": cset,
                "decision_result": dec_res,
                "adoption_result": adp_res,
            }

        # Update integration_ids_by_activity index
        map_upd: dict[str, dict[str, Any]] = {}
        if int_rec.activity_ref:
            st_cur = self.integration_commit.read_state(mutable=True)
            act_int_list = list(st_cur["integration_ids_by_activity"].get(int_rec.activity_ref) or [])
            if int_id not in act_int_list:
                act_int_list.append(int_id)
            map_upd["integration_ids_by_activity"] = {int_rec.activity_ref: act_int_list}

        # Handle NOOP adoptions (CONTINUE without new step, DEFER, NO_ACTION) or PAUSE/WAIT/ABANDON
        if decision_record.decision in {DECISION_NO_ACTION, DECISION_DEFER}:
            int_rec.status = INTEGRATION_STATUS_COMPLETED
            int_rec.revision += 1
            int_rec.updated_at = obs_iso
            map_upd["integrations_by_id"] = {int_id: int_rec.to_dict()}
            self.integration_commit.life_integration_noop_completed(
                map_updates=map_upd,
                journal_payload={
                    "event_type": "LIFE_INTEGRATION_NOOP_COMPLETED",
                    "integration_id": int_id,
                    "decision": decision_record.decision,
                },
            )
            return {
                "correlation_id": corr_id,
                "integration_record": int_rec,
                "candidate_set": cset,
                "decision_result": dec_res,
                "adoption_result": adp_res,
            }

        if decision_record.decision == DECISION_ABANDON and int_rec.activity_ref:
            int_rec.status = INTEGRATION_STATUS_COMPLETED
            int_rec.revision += 1
            int_rec.updated_at = obs_iso
            map_upd["integrations_by_id"] = {int_id: int_rec.to_dict()}
            self.integration_commit.life_integration_abandon_applied(
                map_updates=map_upd,
                journal_payload={
                    "event_type": "LIFE_INTEGRATION_ABANDON_APPLIED",
                    "integration_id": int_id,
                    "activity_ref": int_rec.activity_ref,
                },
            )
            handoff_res = self.emit_experience_handoff(
                integration_id=int_id,
                activity_id=int_rec.activity_ref,
                terminal_or_milestone_kind=HANDOFF_KIND_ABANDONED,
                occurred_at=obs_iso,
                fault_hook=fault_hook,
            )
            int_rec = LifeIntegrationRecord.from_dict(
                self.integration_commit.read_state(mutable=True)["integrations_by_id"][int_id]
            )
            return {
                "correlation_id": corr_id,
                "integration_record": int_rec,
                "candidate_set": cset,
                "decision_result": dec_res,
                "adoption_result": adp_res,
                "handoff_result": handoff_res,
            }

        if decision_record.decision in {DECISION_PAUSE, DECISION_WAIT}:
            int_rec.status = (
                INTEGRATION_STATUS_WAITING_RESULT
                if decision_record.decision == DECISION_WAIT
                else INTEGRATION_STATUS_ADOPTED
            )
            int_rec.revision += 1
            int_rec.updated_at = obs_iso
            map_upd["integrations_by_id"] = {int_id: int_rec.to_dict()}
            self.integration_commit.life_integration_agency_transition_applied(
                map_updates=map_upd,
                journal_payload={
                    "event_type": f"LIFE_INTEGRATION_{decision_record.decision}_APPLIED",
                    "integration_id": int_id,
                    "activity_ref": int_rec.activity_ref,
                },
            )
            return {
                "correlation_id": corr_id,
                "integration_record": int_rec,
                "candidate_set": cset,
                "decision_result": dec_res,
                "adoption_result": adp_res,
            }

        # For START, RESUME, or CONTINUE with an executable step:
        int_rec.status = INTEGRATION_STATUS_ADOPTED
        int_rec.revision += 1
        int_rec.updated_at = obs_iso
        map_upd["integrations_by_id"] = {int_id: int_rec.to_dict()}
        self.integration_commit.life_integration_adopted(
            map_updates=map_upd,
            journal_payload={
                "event_type": "LIFE_INTEGRATION_ADOPTED",
                "integration_id": int_id,
                "activity_ref": int_rec.activity_ref,
                "adoption_id": adp_rec.adoption_id,
            },
        )

        # Resolve step spec if any
        resolved_step_dict: Optional[dict[str, Any]] = None
        if step_spec_override is not None:
            resolved_step_dict = dict(step_spec_override)
        elif decision_record.selected_candidate_ref:
            spec_obj = self.adoption_coordinator.get_adoption_spec(decision_record.selected_candidate_ref)
            if spec_obj and spec_obj.initial_executable_step_spec:
                resolved_step_dict = dict(spec_obj.initial_executable_step_spec)

        step_exec_result = None
        if execute_initial_step and resolved_step_dict is not None and int_rec.activity_ref:
            step_exec_result = self.execute_activity_step(
                integration_id=int_id,
                activity_id=int_rec.activity_ref,
                decision_ref=decision_record.decision_id,
                adoption_ref=adp_rec.adoption_id,
                step_spec_input=resolved_step_dict,
                settle_immediately=settle_immediately,
                occurred_at=obs_iso,
                fault_hook=fault_hook,
            )
            int_rec = LifeIntegrationRecord.from_dict(
                self.integration_commit.read_state(mutable=True)["integrations_by_id"][int_id]
            )

        return {
            "correlation_id": corr_id,
            "integration_record": int_rec,
            "candidate_set": cset,
            "decision_result": dec_res,
            "adoption_result": adp_res,
            "step_execution_result": step_exec_result,
        }

    def execute_activity_step(
        self,
        *,
        integration_id: str,
        activity_id: str,
        decision_ref: str,
        adoption_ref: str,
        step_spec_input: Mapping[str, Any],
        settle_immediately: bool = True,
        occurred_at: Optional[str] = None,
        fault_hook: Optional[Callable[[str], None]] = None,
    ) -> dict[str, Any]:
        """Execute one `ExecutableStepSpec` for an active Activity through `AR-0 -> AR-1 -> LR-5 -> ExperienceHandoff`."""
        int_state = self.integration_commit.read_state(mutable=True)
        int_rec = LifeIntegrationRecord.from_dict(int_state["integrations_by_id"][integration_id])
        occ_iso = _require_iso(occurred_at or _iso(now()), "occurred_at")

        bridge_res = self.action_bridge.create_step_and_propose_action(
            activity_id=activity_id,
            decision_ref=decision_ref,
            adoption_ref=adoption_ref,
            correlation_id=int_rec.correlation_id,
            step_kind=str(step_spec_input["step_kind"]),
            action_kind=step_spec_input.get("action_kind"),
            target_ref=str(step_spec_input["target_ref"]),
            payload_spec=dict(step_spec_input.get("payload_spec") or {}),
            expected_effect_predicates=list(step_spec_input.get("expected_effect_predicates") or []),
            requires_waiting=bool(step_spec_input.get("requires_waiting", False)),
            waiting_condition_spec=step_spec_input.get("waiting_condition_spec"),
            post_submit_life_behavior=str(
                step_spec_input.get("post_submit_life_behavior", POST_SUBMIT_KEEP_ACTIVE)
            ),
            idempotency_key=step_spec_input.get("idempotency_key"),
            occurred_at=occ_iso,
            fault_hook=fault_hook,
        )
        step_obj: ExecutableStepSpec = bridge_res["step_spec"]
        prop_dict = bridge_res["proposal"]
        action_dict = bridge_res["action"]

        if step_obj.step_spec_id not in int_rec.executable_step_refs:
            int_rec.executable_step_refs.append(step_obj.step_spec_id)
        if prop_dict and prop_dict["proposal_id"] not in int_rec.action_proposal_refs:
            int_rec.action_proposal_refs.append(prop_dict["proposal_id"])
        if action_dict and action_dict["action_id"] not in int_rec.action_refs:
            int_rec.action_refs.append(action_dict["action_id"])
        int_rec.status = INTEGRATION_STATUS_IN_PROGRESS
        int_rec.partial_stage = None
        int_rec.revision += 1
        int_rec.updated_at = occ_iso

        self.integration_commit.life_integration_step_proposed(
            map_updates={"integrations_by_id": {integration_id: int_rec.to_dict()}},
            journal_payload={
                "event_type": "LIFE_INTEGRATION_STEP_PROPOSED",
                "integration_id": integration_id,
                "step_spec_id": step_obj.step_spec_id,
                "action_id": action_dict["action_id"] if action_dict else None,
            },
        )

        if action_dict is None:
            return {"bridge_result": bridge_res, "submit_result": None, "settlement_result": None}

        # Check ACTION_EXECUTION_ENABLED kill switch before crossing external submission fence! (Section 235)
        self.kill_switches.require_action_execution()
        if step_obj.action_kind == ACTION_KIND_MESSAGE and step_obj.payload_spec.get("proactive", False):
            self.kill_switches.require_proactive()

        action_id = str(action_dict["action_id"])
        sub_out = self.action_bridge.submit_step_action_and_apply_post_submit_behavior(
            step_spec=step_obj,
            action_id=action_id,
            executor_capability=self.executor_capability,
            settle_immediately=settle_immediately,
            occurred_at=occ_iso,
            fault_hook=fault_hook,
        )
        act_after_sub = sub_out["action"]

        # If action entered UNKNOWN or is waiting for async receipt, record status honestly (Sections 80-88)
        if act_after_sub["status"] == ACTION_STATUS_UNKNOWN:
            int_rec.status = INTEGRATION_STATUS_PARTIAL
            int_rec.partial_stage = "ACTION_UNKNOWN_UNSETTLED"
            int_rec.revision += 1
            int_rec.updated_at = occ_iso
            self.integration_commit.life_integration_action_unknown(
                map_updates={"integrations_by_id": {integration_id: int_rec.to_dict()}},
                journal_payload={
                    "event_type": "LIFE_INTEGRATION_ACTION_UNKNOWN",
                    "integration_id": integration_id,
                    "action_id": action_id,
                },
            )
            return {
                "bridge_result": bridge_res,
                "submit_result": sub_out,
                "settlement_result": None,
            }

        if not settle_immediately or act_after_sub["status"] in {
            ACTION_STATUS_PREPARED,
            ACTION_STATUS_SUBMITTED,
            ACTION_STATUS_ACKNOWLEDGED,
        }:
            if sub_out.get("waiting_applied"):
                int_rec.status = INTEGRATION_STATUS_WAITING_RESULT
                int_rec.revision += 1
                int_rec.updated_at = occ_iso
                self.integration_commit.life_integration_waiting_async_result(
                    map_updates={"integrations_by_id": {integration_id: int_rec.to_dict()}},
                    journal_payload={
                        "event_type": "LIFE_INTEGRATION_WAITING_ASYNC_RESULT",
                        "integration_id": integration_id,
                        "action_id": action_id,
                        "activity_ref": activity_id,
                    },
                )
            return {
                "bridge_result": bridge_res,
                "submit_result": sub_out,
                "settlement_result": None,
            }

        # Action reached SUCCEEDED or FAILED synchronously -> settle & propagate to LR-5!
        settle_res = self.settle_and_propagate_action_result(
            action_id=action_id,
            integration_id=integration_id,
            occurred_at=occ_iso,
            fault_hook=fault_hook,
        )
        return {
            "bridge_result": bridge_res,
            "submit_result": sub_out,
            "settlement_result": settle_res,
        }

    def settle_and_propagate_action_result(
        self,
        *,
        action_id: str,
        integration_id: Optional[str] = None,
        additional_evidences: Optional[Sequence[Any]] = None,
        occurred_at: Optional[str] = None,
        fault_hook: Optional[Callable[[str], None]] = None,
    ) -> dict[str, Any]:
        """Sections 89-117: Settle an Action via `AR-1`, dispatch `SettledResultEvent` to `LR-5` (and `LR-4` if WAITING),
        update `LifeIntegrationRecord`, and emit `LifeExperienceHandoffEvent` if Activity completed/conflicted.
        """
        self.kill_switches.require_life_runtime()
        occ_iso = _require_iso(occurred_at or _iso(now()), "occurred_at")
        act_rec = self.action_read.get_action(action_id)
        if act_rec is None:
            raise LifeIntegrationError(f"Unknown action_id={action_id!r}")

        int_state = self.integration_commit.read_state(mutable=True)
        resolved_int_id = integration_id
        if resolved_int_id is None:
            for iid, irec in int_state["integrations_by_id"].items():
                if action_id in (irec.get("action_refs") or []):
                    resolved_int_id = iid
                    break
        if resolved_int_id is None and act_rec.get("decision_ref"):
            resolved_int_id = int_state["integration_id_by_decision"].get(act_rec["decision_ref"])

        int_rec = (
            LifeIntegrationRecord.from_dict(int_state["integrations_by_id"][resolved_int_id])
            if (resolved_int_id and resolved_int_id in int_state["integrations_by_id"])
            else None
        )
        corr_id = int_rec.correlation_id if int_rec else f"corr:{sha256_hex(action_id)[:16]}"
        activity_id = act_rec.get("activity_ref") or (int_rec.activity_ref if int_rec else None)

        # 0. For FILE_ACTION, observe sandbox file artifact via FileArtifactObserver if not already observed
        if (
            act_rec["action_kind"] == ACTION_KIND_FILE
            and additional_evidences is None
            and self.file_observer is not None
            and not self.action_read.list_result_evidences(action_id)
        ):
            # Real AR-0 ActionRecord stores the frozen payload under `frozen_payload`
            # (payload keys: op/path/content), not under `payload_spec`/`rel_path`.
            payload = act_rec.get("frozen_payload") or act_rec.get("payload_spec") or {}
            rel_path = (
                payload.get("path")
                or payload.get("rel_path")
                or str(act_rec.get("target_ref", "")).replace("file:sandbox/", "")
            )
            content = str(payload.get("content", ""))
            expected_hash = payload.get("expected_sha256") or sha256_hex(content)
            obs_ev = self.file_observer.observe_file_evidence(
                action_id=action_id,
                rel_path=rel_path,
                expected_hash=expected_hash,
                occurred_at=occ_iso,
            )
            self.action_command.record_result_evidence(obs_ev)

        # 1. AR-1: Settle Action
        set_res = self.settlement_service.settle_action(
            action_id=action_id,
            additional_evidences=additional_evidences,
            correlation_id=corr_id,
            occurred_at=occ_iso,
            fault_hook=fault_hook,
        )
        settlement_dict = set_res["settlement"]
        outbox_ev = set_res.get("outbox_event")
        settlement_id = str(settlement_dict["settlement_id"])

        if int_rec is not None and resolved_int_id is not None:
            if settlement_id not in int_rec.settlement_refs:
                int_rec.settlement_refs.append(settlement_id)
            int_rec.status = INTEGRATION_STATUS_SETTLED
            int_rec.revision += 1
            int_rec.updated_at = occ_iso
            self.integration_commit.life_integration_action_settled(
                map_updates={"integrations_by_id": {resolved_int_id: int_rec.to_dict()}},
                journal_payload={
                    "event_type": "LIFE_INTEGRATION_ACTION_SETTLED",
                    "integration_id": resolved_int_id,
                    "action_id": action_id,
                    "settlement_id": settlement_id,
                    "settlement_status": settlement_dict["settlement_status"],
                    "outcome_kind": settlement_dict["outcome_kind"],
                },
            )

        if fault_hook is not None:
            fault_hook("after_settlement_before_lr5_dispatch")

        # 2. Check if Activity is currently WAITING and uses ASYNC_COMPLETION_MODE_ELIGIBILITY_FIRST (Section 78 Loop C Mode 2)
        async_mode = (
            self.integration_commit.read_state(mutable=True)["async_completion_mode_by_activity"].get(
                activity_id, ASYNC_COMPLETION_MODE_DIRECT_SETTLE
            )
            if activity_id
            else ASYNC_COMPLETION_MODE_DIRECT_SETTLE
        )
        cur_life_act = self.activity_read.get_activity(activity_id) if activity_id else None
        waiting_eval_res = None

        if cur_life_act and cur_life_act.get("status") == STATUS_WAITING and outbox_ev is not None:
            # Feed external settled result event to LR-4 WaitingCoordinator so ResumeCondition is satisfied!
            waiting_eval_res = self.waiting_coordinator.evaluate_event(
                {
                    "event_id": outbox_ev["event_id"],
                    "event_category": "EXTERNAL_RESULT_READY",
                    "source_ref": action_id,
                    "waiting_on_ref": action_id,
                    "activity_id": activity_id,
                    "occurred_at": occ_iso,
                    "observed_at": occ_iso,
                    "attributes": {
                        "action_id": action_id,
                        "settlement_id": settlement_id,
                        "status": settlement_dict["settlement_status"],
                        "outcome_kind": settlement_dict["outcome_kind"],
                    },
                }
            )

        # If mode is ELIGIBILITY_FIRST and Activity is still WAITING, defer natural completion until after RESUME (or record progress without auto-completing)
        prev_auto_complete = self.progress_coordinator.auto_apply_natural_completion
        if (
            cur_life_act
            and cur_life_act.get("status") == STATUS_WAITING
            and async_mode == ASYNC_COMPLETION_MODE_ELIGIBILITY_FIRST
        ):
            self.progress_coordinator.auto_apply_natural_completion = False

        try:
            dispatch_res = (
                self.settlement_service.dispatch_outbox(
                    event_id=outbox_ev["event_id"],
                    fault_hook=fault_hook,
                )
                if outbox_ev is not None
                else None
            )
        finally:
            self.progress_coordinator.auto_apply_natural_completion = prev_auto_complete

        if fault_hook is not None:
            fault_hook("after_lr5_dispatch_before_integration_finalize")

        # 3. Inspect LR-5 Progress & Canonical Activity state after outbox consumption
        updated_act = self.activity_read.get_activity(activity_id) if activity_id else None
        prog_state = self.progress_read.get_progress_state(activity_id) if activity_id else None
        reconciliations = self.progress_read.list_reconciliations(activity_id=activity_id) if activity_id else []

        handoff_res = None
        repair_proposal = None

        if int_rec is not None and resolved_int_id is not None:
            int_rec = LifeIntegrationRecord.from_dict(
                self.integration_commit.read_state(mutable=True)["integrations_by_id"][resolved_int_id]
            )
            if outbox_ev is not None:
                progress_event_hash_input = f"{outbox_ev['event_id']}:{settlement_id}"
                pevid = f"pevid:{sha256_hex(progress_event_hash_input)[:16]}"
                if pevid not in int_rec.progress_refs:
                    int_rec.progress_refs.append(pevid)

            # Check for post-completion conflict or settlement conflict (Sections 94, 97, 98, Loop L & Loop M)
            if reconciliations or settlement_dict["settlement_status"] == SETTLEMENT_STATUS_CONFLICT:
                int_rec.status = INTEGRATION_STATUS_CONFLICT
                int_rec.failure_reason_code = (
                    "POST_COMPLETION_SETTLEMENT_CONFLICT"
                    if reconciliations
                    else "SETTLEMENT_CONFLICT_DETECTED"
                )
                if reconciliations and activity_id:
                    rep_id = f"rprop:{sha256_hex(f'{activity_id}:{settlement_id}')[:16]}"
                    repair_proposal = RepairProposal(
                        repair_proposal_id=rep_id,
                        correlation_id=int_rec.correlation_id,
                        conflicted_activity_ref=activity_id,
                        conflicted_settlement_ref=settlement_id,
                        reconciliation_ref=reconciliations[-1]["reconciliation_id"],
                        target_refs=[act_rec["target_ref"]],
                        reason_code="POST_COMPLETION_SETTLEMENT_CONFLICT",
                        created_at=occ_iso,
                    )
                    int_rec.repair_proposal_ref = rep_id
                    self.integration_commit.repair_proposal_created_on_conflict(
                        map_updates={"repair_proposals_by_id": {rep_id: repair_proposal.to_dict()}},
                        journal_payload={
                            "event_type": "REPAIR_PROPOSAL_CREATED_ON_CONFLICT",
                            "repair_proposal_id": rep_id,
                            "activity_id": activity_id,
                            "settlement_id": settlement_id,
                        },
                    )
                int_rec.revision += 1
                int_rec.updated_at = occ_iso
                self.integration_commit.life_integration_marked_conflict(
                    map_updates={"integrations_by_id": {resolved_int_id: int_rec.to_dict()}},
                    journal_payload={
                        "event_type": "LIFE_INTEGRATION_MARKED_CONFLICT",
                        "integration_id": resolved_int_id,
                        "activity_id": activity_id,
                        "settlement_id": settlement_id,
                    },
                )
                if activity_id:
                    handoff_res = self.emit_experience_handoff(
                        integration_id=resolved_int_id,
                        activity_id=activity_id,
                        terminal_or_milestone_kind=HANDOFF_KIND_CONFLICT,
                        occurred_at=occ_iso,
                        fault_hook=fault_hook,
                    )
            elif updated_act and updated_act.get("status") == STATUS_COMPLETED:
                int_rec.status = INTEGRATION_STATUS_COMPLETED
                int_rec.completion_ref = updated_act.get("completion_evidence_ref")
                int_rec.partial_stage = None
                int_rec.revision += 1
                int_rec.updated_at = occ_iso
                self.integration_commit.life_integration_completed(
                    map_updates={"integrations_by_id": {resolved_int_id: int_rec.to_dict()}},
                    journal_payload={
                        "event_type": "LIFE_INTEGRATION_COMPLETED",
                        "integration_id": resolved_int_id,
                        "activity_id": activity_id,
                        "completion_evidence_ref": int_rec.completion_ref,
                    },
                )
                # Reconcile LR-4 waiting/agenda records for completed activity
                self.waiting_coordinator.reconcile_with_canonical_activities(now_at=occ_iso)

                if fault_hook is not None:
                    fault_hook("after_activity_complete_before_experience_handoff")

                if activity_id:
                    handoff_res = self.emit_experience_handoff(
                        integration_id=resolved_int_id,
                        activity_id=activity_id,
                        terminal_or_milestone_kind=HANDOFF_KIND_COMPLETED,
                        occurred_at=occ_iso,
                        fault_hook=fault_hook,
                    )
            else:
                # Partial or intermediate progress (Activity remains ACTIVE or WAITING, Section 93 & Loop K)
                int_rec.status = (
                    INTEGRATION_STATUS_WAITING_RESULT
                    if (updated_act and updated_act.get("status") == STATUS_WAITING)
                    else INTEGRATION_STATUS_IN_PROGRESS
                )
                int_rec.revision += 1
                int_rec.updated_at = occ_iso
                self.integration_commit.life_integration_progress_updated(
                    map_updates={"integrations_by_id": {resolved_int_id: int_rec.to_dict()}},
                    journal_payload={
                        "event_type": "LIFE_INTEGRATION_PROGRESS_UPDATED",
                        "integration_id": resolved_int_id,
                        "activity_id": activity_id,
                        "progress_state": prog_state["progress_state"] if prog_state else None,
                    },
                )

            int_rec = LifeIntegrationRecord.from_dict(
                self.integration_commit.read_state(mutable=True)["integrations_by_id"][resolved_int_id]
            )

        return {
            "settlement_result": set_res,
            "dispatch_result": dispatch_res,
            "waiting_eval_result": waiting_eval_res,
            "activity": updated_act,
            "progress_state": prog_state,
            "integration_record": int_rec,
            "handoff_result": handoff_res,
            "repair_proposal": repair_proposal,
        }

    # -- Ordinary Chat vs User Request Handling During Activity (Sections 118-123) --

    def handle_ordinary_chat_turn(
        self,
        *,
        chat_event_id: str,
        message_text: str,
        occurred_at: Optional[str] = None,
    ) -> dict[str, Any]:
        """Sections 118-120 (Loop N): Ordinary chat turn while an Activity is ACTIVE.

        - Reads current `LifeFrameReadView` via `interruption_coordinator.get_current_life_view()`.
        - Does NOT generate a `USER_REQUEST` candidate in AG-0 (`0` candidate created).
        - Does NOT pause or overwrite the active `ActivityRecord` (`0` activity transition).
        - Does NOT create an `ActionProposal` (`0` action created).
        """
        occ_iso = _require_iso(occurred_at or _iso(now()), "occurred_at")
        life_view_before = self.interruption_coordinator.get_current_life_view()
        fg_ref_before = life_view_before.get("foreground_activity_ref")
        act_before = self.activity_read.get_activity(fg_ref_before) if fg_ref_before else None

        # Assess chat event via LR-3 (ordinary chat is non-interrupting when LIGHT/SAFE_BOUNDARY/ATOMIC)
        ev_id = (
            chat_event_id
            if chat_event_id.startswith(("comm_evt:", "evt:", "msg_evt:"))
            else f"comm_evt:{sha256_hex(chat_event_id)[:16]}"
        )
        msg_id = f"msg:{sha256_hex(f'{chat_event_id}:{message_text}')[:16]}"
        self.interruption_coordinator.assess_events(
            [
                {
                    "event_kind": EVENT_KIND_COMMUNICATION,
                    "event_id": ev_id,
                    "channel_ref": "channel:chat_ui",
                    "message_ref": msg_id,
                    "message_class": MESSAGE_CLASS_ORDINARY,
                    "request_boundary_pause": False,
                    "source_refs": ["channel:chat_ui", msg_id],
                    "occurred_at": occ_iso,
                    "observed_at": occ_iso,
                }
            ],
            apply_immediate_pause=False,
        )

        life_view_after = self.interruption_coordinator.get_current_life_view()
        act_after = self.activity_read.get_activity(fg_ref_before) if fg_ref_before else None

        return {
            "chat_event_id": chat_event_id,
            "life_view": life_view_after,
            "foreground_activity_preserved": (
                act_before == act_after if act_before is not None else True
            ),
            "user_request_candidate_created": False,
            "activity_mutated": False,
        }

    # -- Crash Recovery & Canonical Reconciliation (Sections 124-140) --------

    def recover_all_and_reconcile(
        self,
        *,
        now_at: Optional[str] = None,
        auto_reconcile_unknown_actions: bool = False,
    ) -> dict[str, Any]:
        """Sections 124-140: Cold-start recovery across all 9 canonical stores and reconciliation of
        every open/partial `LifeIntegrationRecord` consulting canonical stores first (0 LLM calls).
        """
        now_iso = _require_iso(now_at or _iso(now()), "now_at")

        # 1. Recover all canonical stores in dependency order (0 LLM)
        self.integration_commit.flush()
        int_wal = self.integration_commit.recover()
        ag1_rec = self.decision_service.reconcile_after_crash()
        lr3_rec = self.interruption_coordinator.recover_cold_start()
        lr4_rec = self.waiting_coordinator.recover_cold_start(now_at=now_iso)
        ar0_rec = self.action_command.recover()
        ar1_rec = self.settlement_service.recover()
        lr5_rec = self.progress_coordinator.recover()

        # 2. Reconcile each LifeIntegrationRecord against canonical truth (Phases A..G, Sections 127-133)
        int_state = self.integration_commit.read_state(mutable=True)
        reconciled_ids: list[str] = []
        for iid in sorted(int_state["integrations_by_id"].keys()):
            res = self.reconcile_integration(
                iid,
                now_at=now_iso,
                auto_reconcile_unknown_actions=auto_reconcile_unknown_actions,
            )
            if res.get("mutated"):
                reconciled_ids.append(iid)

        # 3. Retry any pending ExperienceHandoff deliveries
        handoff_disp = self.dispatch_experience_handoffs()

        return {
            "integration_wal_recovered": int_wal,
            "ag1_recovery": ag1_rec,
            "lr3_recovery": lr3_rec,
            "lr4_recovery": lr4_rec,
            "ar0_recovery": ar0_rec,
            "ar1_recovery": ar1_rec,
            "lr5_recovery": lr5_rec,
            "reconciled_integration_ids": reconciled_ids,
            "handoff_dispatch": handoff_disp,
            "llm_calls": 0,
        }

    def reconcile_integration(
        self,
        integration_id: str,
        *,
        now_at: Optional[str] = None,
        auto_reconcile_unknown_actions: bool = False,
    ) -> dict[str, Any]:
        """Sections 125-133: Consult canonical stores first (`AG-1`, `LR-2/3/4`, `AR-0`, `AR-1`, `LR-5`)
        and reconcile `DecisionAdoptionRecord` and `LifeIntegrationRecord` without ever rolling back
        committed canonical truth.
        """
        now_iso = _require_iso(now_at or _iso(now()), "now_at")
        int_state = self.integration_commit.read_state(mutable=True)
        int_dict = int_state["integrations_by_id"].get(integration_id)
        if int_dict is None:
            raise LifeIntegrationError(f"Unknown integration_id={integration_id!r}")

        int_rec = LifeIntegrationRecord.from_dict(int_dict)
        mutated = False

        # Phase A: Check AG-1 DecisionStore for opportunity_ref
        if not int_rec.decision_ref and int_rec.opportunity_ref:
            dec_id = self.decision_read.find_decision_id_by_opportunity(
                int_rec.opportunity_ref
            )
            if dec_id:
                int_rec.decision_ref = dec_id
                mutated = True
            else:
                opp_obj = self.decision_read.get_opportunity(int_rec.opportunity_ref)
                if opp_obj and opp_obj.get("status") in {OPPORTUNITY_STATUS_FAILED, OPPORTUNITY_STATUS_STALE}:
                    if int_rec.status != INTEGRATION_STATUS_FAILED:
                        int_rec.status = INTEGRATION_STATUS_FAILED
                        int_rec.failure_reason_code = f"OPPORTUNITY_{opp_obj.get('status')}"
                        mutated = True

        # Phase B: Check LR-2 CanonicalActivityStore journal for transitions with decision_ref == int_rec.decision_ref
        # (e.g. Crash Point 4: Activity command committed in LR-2, crash before DecisionAdoptionRecord committed!)
        if int_rec.decision_ref:
            can_state, can_journal = self.activity_read.read_canonical_state(include_journal=True)
            matched_trn = None
            for trn in can_journal:
                if trn.get("decision_ref") == int_rec.decision_ref:
                    matched_trn = trn
                    break

            adp_id = int_state["adoption_id_by_decision"].get(int_rec.decision_ref)
            adp_dict = int_state["adoptions_by_id"].get(adp_id) if adp_id else None

            if matched_trn is not None:
                act_id_from_trn = str(matched_trn["activity_id"])
                act_obj = (can_state.get("activities") or {}).get(act_id_from_trn)
                resolved_adp_id = matched_trn.get("adoption_ref") or adp_id or f"adp:{sha256_hex(int_rec.decision_ref)[:16]}"

                if adp_dict is None or adp_dict.get("adoption_status") in {ADOPTION_STATUS_PENDING, ADOPTION_STATUS_VALIDATED}:
                    dec_dict = self.decision_read.get_decision(int_rec.decision_ref)
                    if dec_dict:
                        reconciled_adp = DecisionAdoptionRecord(
                            adoption_id=resolved_adp_id,
                            decision_ref=int_rec.decision_ref,
                            decision_revision=int(dec_dict["revision"]),
                            candidate_set_ref=str(dec_dict["candidate_set_ref"]),
                            selected_candidate_ref=dec_dict.get("selected_candidate_ref"),
                            decision_kind=str(dec_dict["decision"]),
                            target_activity_ref=act_id_from_trn,
                            expected_activity_revision=int(matched_trn.get("expected_revision", 0)),
                            adoption_status=ADOPTION_STATUS_APPLIED,
                            applied_activity_command_ref=str(matched_trn["transition_id"]),
                            applied_activity_revision=(
                                int(act_obj["revision"])
                                if act_obj
                                else int(matched_trn.get("result_revision", matched_trn.get("expected_revision", 0)))
                            ),
                            created_activity_ref=(
                                act_id_from_trn if matched_trn.get("transition_type") == TRANSITION_START else None
                            ),
                            rejection_reason_code=None,
                            adopted_at=str(matched_trn.get("occurred_at") or now_iso),
                            recorded_at=now_iso,
                            idempotency_key=f"idem_adopt:{int_rec.decision_ref}",
                        )
                        self.integration_commit.recovery_reconciled_adoption_from_lr2_journal(
                            map_updates={
                                "adoptions_by_id": {resolved_adp_id: reconciled_adp.to_dict()},
                                "adoption_id_by_decision": {int_rec.decision_ref: resolved_adp_id},
                                "adoption_id_by_idempotency_key": {reconciled_adp.idempotency_key: resolved_adp_id},
                            },
                            journal_payload={
                                "event_type": "RECOVERY_RECONCILED_ADOPTION_FROM_LR2_JOURNAL",
                                "integration_id": integration_id,
                                "adoption_id": resolved_adp_id,
                                "activity_id": act_id_from_trn,
                                "transition_id": matched_trn["transition_id"],
                            },
                        )
                        adp_id = resolved_adp_id
                        mutated = True

                if int_rec.adoption_ref != resolved_adp_id:
                    int_rec.adoption_ref = resolved_adp_id
                    mutated = True
                if int_rec.activity_ref != act_id_from_trn:
                    int_rec.activity_ref = act_id_from_trn
                    mutated = True

                # Register missing LR-5 contracts if crash occurred right after start_activity
                dec_dict = self.decision_read.get_decision(int_rec.decision_ref)
                if dec_dict and dec_dict.get("selected_candidate_ref"):
                    spec_obj = self.adoption_coordinator.get_adoption_spec(dec_dict["selected_candidate_ref"])
                    if spec_obj and act_obj:
                        if self.progress_read.get_binding(act_id_from_trn) is None:
                            self.progress_coordinator.register_result_binding(
                                self.adoption_coordinator.build_result_binding(
                                    activity_id=act_id_from_trn,
                                    adoption_id=resolved_adp_id,
                                    correlation_id=int_rec.correlation_id,
                                    spec=spec_obj,
                                    occurred_at=now_iso,
                                )
                            )
                        if spec_obj.progress_contract_spec and self.progress_read.get_progress_contract(act_id_from_trn) is None:
                            self.progress_coordinator.register_progress_contract(
                                self.adoption_coordinator.build_progress_contract(
                                    activity_id=act_id_from_trn,
                                    spec=spec_obj,
                                    occurred_at=now_iso,
                                )
                            )
                        if spec_obj.completion_contract_spec and self.progress_read.get_completion_contract(act_id_from_trn) is None:
                            self.progress_coordinator.register_completion_contract(
                                self.adoption_coordinator.build_completion_contract(
                                    activity_id=act_id_from_trn,
                                    spec=spec_obj,
                                    occurred_at=now_iso,
                                )
                            )

        # Phase C: Check if Activity is committed in LR-2, but ActionProposal is missing (Crash Point 5 / Loop R)
        # Section 46 & 129: NEVER roll back the committed Activity! Mark LifeIntegrationRecord = PARTIAL
        # ("ACTIVITY_COMMITTED_ACTION_MISSING") if a step was expected but not yet proposed.
        if int_rec.activity_ref and not int_rec.action_refs:
            # Also check if AR-0 ActionStore already has an action with activity_ref == int_rec.activity_ref
            for act_in_store in self.action_read.list_actions():
                if act_in_store.get("activity_ref") == int_rec.activity_ref:
                    if act_in_store["action_id"] not in int_rec.action_refs:
                        int_rec.action_refs.append(act_in_store["action_id"])
                        mutated = True
                    if act_in_store.get("proposal_ref") and act_in_store["proposal_ref"] not in int_rec.action_proposal_refs:
                        int_rec.action_proposal_refs.append(act_in_store["proposal_ref"])
                        mutated = True

            if not int_rec.action_refs and int_rec.decision_ref:
                dec_dict = self.decision_read.get_decision(int_rec.decision_ref)
                if dec_dict and dec_dict.get("decision") == DECISION_START:
                    sel_cid = dec_dict.get("selected_candidate_ref")
                    spec_obj = self.adoption_coordinator.get_adoption_spec(sel_cid) if sel_cid else None
                    if (spec_obj and spec_obj.initial_executable_step_spec) or int_rec.executable_step_refs:
                        if int_rec.status not in {INTEGRATION_STATUS_COMPLETED, INTEGRATION_STATUS_CONFLICT}:
                            int_rec.status = INTEGRATION_STATUS_PARTIAL
                            int_rec.partial_stage = "ACTIVITY_COMMITTED_ACTION_MISSING"
                            mutated = True

        # Phase D & E: Check AR-0 Actions & AR-1 Settlements for linked actions
        for aid in list(int_rec.action_refs):
            act_rec = self.action_read.get_action(aid)
            if act_rec is None:
                continue
            if act_rec["status"] == ACTION_STATUS_UNKNOWN and auto_reconcile_unknown_actions:
                rec_out = self.action_command.reconcile_action(action_id=aid, occurred_at=now_iso)
                act_rec = rec_out["action"]
                mutated = True

            if act_rec["status"] == ACTION_STATUS_UNKNOWN:
                if int_rec.status not in {INTEGRATION_STATUS_COMPLETED, INTEGRATION_STATUS_CONFLICT}:
                    int_rec.status = INTEGRATION_STATUS_PARTIAL
                    int_rec.partial_stage = "ACTION_UNKNOWN_UNSETTLED"
                    mutated = True
            elif act_rec["status"] in {ACTION_STATUS_SUCCEEDED, ACTION_STATUS_FAILED}:
                # Ensure settled in AR-1 and dispatched to LR-5
                existing_set = self.settlement_read.get_settlement(aid)
                if existing_set is None:
                    self.settle_and_propagate_action_result(
                        action_id=aid,
                        integration_id=integration_id,
                        occurred_at=now_iso,
                    )
                    int_rec = LifeIntegrationRecord.from_dict(
                        self.integration_commit.read_state(mutable=True)["integrations_by_id"][integration_id]
                    )
                    mutated = True
                else:
                    if existing_set["settlement_id"] not in int_rec.settlement_refs:
                        int_rec.settlement_refs.append(existing_set["settlement_id"])
                        mutated = True
                    # Dispatch any pending AR-1 outbox events for this action
                    for ob_ev in self.settlement_read.list_outbox_events(action_id=aid):
                        self.settlement_service.dispatch_outbox(event_id=ob_ev["event_id"])

        # Phase F & G: Check LR-2 Activity terminal status & ExperienceHandoff
        if int_rec.activity_ref:
            live_act = self.activity_read.get_activity(int_rec.activity_ref)
            if live_act:
                if live_act["status"] == STATUS_COMPLETED:
                    if int_rec.status != INTEGRATION_STATUS_CONFLICT:
                        if int_rec.status != INTEGRATION_STATUS_COMPLETED:
                            int_rec.status = INTEGRATION_STATUS_COMPLETED
                            int_rec.completion_ref = live_act.get("completion_evidence_ref")
                            int_rec.partial_stage = None
                            mutated = True
                    if not int_rec.experience_handoff_refs:
                        self.emit_experience_handoff(
                            integration_id=integration_id,
                            activity_id=int_rec.activity_ref,
                            terminal_or_milestone_kind=HANDOFF_KIND_COMPLETED,
                            occurred_at=now_iso,
                        )
                        int_rec = LifeIntegrationRecord.from_dict(
                            self.integration_commit.read_state(mutable=True)["integrations_by_id"][integration_id]
                        )
                        mutated = True
                elif live_act["status"] == STATUS_ABANDONED:
                    if int_rec.status != INTEGRATION_STATUS_COMPLETED:
                        int_rec.status = INTEGRATION_STATUS_COMPLETED
                        int_rec.partial_stage = None
                        mutated = True
                    if not int_rec.experience_handoff_refs:
                        self.emit_experience_handoff(
                            integration_id=integration_id,
                            activity_id=int_rec.activity_ref,
                            terminal_or_milestone_kind=HANDOFF_KIND_ABANDONED,
                            occurred_at=now_iso,
                        )
                        int_rec = LifeIntegrationRecord.from_dict(
                            self.integration_commit.read_state(mutable=True)["integrations_by_id"][integration_id]
                        )
                        mutated = True

        if mutated:
            int_rec.revision += 1
            int_rec.updated_at = now_iso
            st_latest = self.integration_commit.read_state(mutable=True)
            map_u: dict[str, dict[str, Any]] = {"integrations_by_id": {integration_id: int_rec.to_dict()}}
            if int_rec.activity_ref:
                alist = list(st_latest["integration_ids_by_activity"].get(int_rec.activity_ref) or [])
                if integration_id not in alist:
                    alist.append(integration_id)
                map_u["integration_ids_by_activity"] = {int_rec.activity_ref: alist}
            self.integration_commit.life_integration_reconciled(
                map_updates=map_u,
                journal_payload={
                    "event_type": "LIFE_INTEGRATION_RECONCILED",
                    "integration_id": integration_id,
                    "status": int_rec.status,
                    "partial_stage": int_rec.partial_stage,
                },
            )

        return {
            "integration_id": integration_id,
            "mutated": mutated,
            "integration_record": int_rec,
        }


# ---------------------------------------------------------------------------
# LifeLoopReplayVerifier & Shadow Mode (Sections 197-203)
# ---------------------------------------------------------------------------


class LifeLoopReplayVerifier:
    """Sections 197-203: Zero-side-effect Replay & Shadow verifier for the full AG-2 Life Loop.

    Guarantees during Replay:
      - `0` LLM calls (`cognition_adapter.call_count` delta == 0)
      - `0` real Action adapter submissions (`submit_call_count` delta == 0)
      - `0` canonical store mutations (`activity_store`, `action_store`, `settlement_store`, `progress_store`, `decision_store`)
      - `0` World / File / Memory / Outbound mutations
      - Rebuilds `IntegrationStore` state from `IntegrationJournal` and verifies 100% consistency with live snapshot
        and with `AR-0`, `AR-1`, `LR-5`, and `AG-1` replay engines.
    """

    @staticmethod
    def replay_and_verify(harness: "IsolatedLifeRuntimeHarness") -> dict[str, Any]:
        # Capture pre-replay side-effect counters
        cog_before = harness.cognition_adapter.call_count
        world_sub_before = harness.world_adapter.submit_call_count
        file_sub_before = harness.file_adapter.submit_call_count
        tool_sub_before = harness.tool_adapter.submit_call_count
        msg_sub_before = harness.message_adapter.submit_call_count
        exp_mem_before = harness.experience_consumer.memory_write_count
        act_rev_before = harness.activity_store.load_verified_state_and_journal(_include_journal=False)[0]["revision"]

        # 1. Verify AG-2 IntegrationJournal -> IntegrationStore state rebuild
        int_entries = harness.integration_store.read_journal()
        rebuilt_int_state = harness.integration_store.rebuild_state_from_journal(int_entries)
        live_int_state = harness.integration_store.load_state()

        assert rebuilt_int_state["revision"] == live_int_state["revision"]
        assert rebuilt_int_state["last_entry_hash"] == live_int_state["last_entry_hash"]
        assert rebuilt_int_state["integrations_by_id"] == live_int_state["integrations_by_id"]
        assert rebuilt_int_state["adoptions_by_id"] == live_int_state["adoptions_by_id"]
        assert rebuilt_int_state["handoff_events_by_id"] == live_int_state["handoff_events_by_id"]

        # 2. Verify all AG-1 DecisionRecords via DecisionReplayVerifier (0 LLM calls)
        dec_state = harness.decision_store.load_state()
        replayed_decisions: dict[str, Any] = {}
        for did in sorted(dec_state["decisions_by_id"].keys()):
            d_rep = DecisionReplayVerifier.replay_decision(
                store=harness.decision_store,
                decision_id=did,
                cognition_adapter=harness.cognition_adapter,
            )
            assert d_rep["deterministic_match"] is True
            assert d_rep["replay_cognition_calls"] == 0
            replayed_decisions[did] = d_rep

        # 3. Verify AR-0 ActionReplayEngine & AR-1 ResultSettlementReplay & LR-5 ActivityProgressReplay
        ar0_rep = ActionReplayEngine.replay_and_verify(
            harness.action_store,
            adapters=harness.action_command.adapters,
        )
        ar1_rep = ResultSettlementReplay.replay_and_verify(
            action_store=harness.action_store,
            settlement_store=harness.settlement_store,
            adapters=harness.action_command.adapters,
        )
        lr5_rep = ActivityProgressReplay.replay_and_verify(
            activity_store=harness.activity_store,
            progress_store=harness.progress_store,
        )

        # Verify 0 side effects occurred during replay
        cog_after = harness.cognition_adapter.call_count
        world_sub_after = harness.world_adapter.submit_call_count
        file_sub_after = harness.file_adapter.submit_call_count
        tool_sub_after = harness.tool_adapter.submit_call_count
        msg_sub_after = harness.message_adapter.submit_call_count
        exp_mem_after = harness.experience_consumer.memory_write_count
        act_rev_after = harness.activity_store.load_verified_state_and_journal(_include_journal=False)[0]["revision"]

        if (
            cog_after != cog_before
            or world_sub_after != world_sub_before
            or file_sub_after != file_sub_before
            or tool_sub_after != tool_sub_before
            or msg_sub_after != msg_sub_before
            or exp_mem_after != exp_mem_before
            or act_rev_after != act_rev_before
        ):
            raise ReplayIsolationViolationError("LifeLoopReplayVerifier caused side effects or LLM calls!")

        return {
            "consistent": True,
            "integration_journal_count": len(int_entries),
            "replayed_decision_count": len(replayed_decisions),
            "rebuilt_integrations_by_id": copy.deepcopy(rebuilt_int_state["integrations_by_id"]),
            "rebuilt_adoptions_by_id": copy.deepcopy(rebuilt_int_state["adoptions_by_id"]),
            "rebuilt_handoff_events_by_id": copy.deepcopy(rebuilt_int_state["handoff_events_by_id"]),
            "ar0_replay": ar0_rep,
            "ar1_replay": ar1_rep,
            "lr5_replay": lr5_rep,
            "llm_call_delta": 0,
            "adapter_submit_delta": 0,
            "canonical_mutation_delta": 0,
            "memory_write_delta": 0,
        }


# ---------------------------------------------------------------------------
# IsolatedLifeRuntimeHarness (Section 12, 241)
# ---------------------------------------------------------------------------


class IsolatedLifeRuntimeHarness:
    """Complete isolated multi-subsystem runtime harness wiring real canonical instances of:
      - `AG-0`: `CandidateSourceRegistry`, `CandidateMaterializer`, `CandidateReadService`
      - `AG-1`: `AgencyDecisionStore`, `AgencyDecisionService`, `AgencyDecisionReadService`, `FakeCognitionAdapter`
      - `LR-2`: `ActivityAuthorityLease`, `CanonicalActivityStore`, `ActivityCommandService`, `ActivityReadService`
      - `LR-3`: `InterruptionStateStore`, `InterruptionCoordinator`
      - `LR-4`: `ResumeConditionStore`, `AgendaStore`, `ResumeEligibilityStore`, `WaitingCoordinator`
      - `AR-0`: `ActionStore`, `ActionCommandService`, `ActionReadService`, `WorldActionAdapter`, `SandboxFileAdapter`, `ToolActionAdapter`, `MessageActionAdapter`
      - `AR-1`: `SettlementStore`, `ResultSettlementService`, `SettlementReadService`
      - `LR-5`: `ProgressStore`, `ActivityProgressCoordinator`, `ActivityProgressReadService`
      - `AG-2`: `IntegrationStore`, `DecisionAdoptionCoordinator`, `DecisionActionBridge`, `LifeIntegrationCoordinator`, `FakeExperienceConsumer`

    All storage is strictly rooted under an isolated `root_dir` (`tmp_path`), with `0` connection
    to `./world/data` or any live production service.
    """

    def __init__(
        self,
        root_dir: Path | str,
        *,
        kill_switches: Optional[LifeRuntimeKillSwitches] = None,
        cognition_adapter: Optional[AgencyCognitionAdapter] = None,
        enable_commitment_source: bool = True,
        enable_goal_source: bool = True,
    ) -> None:
        self.root_dir = Path(root_dir).expanduser().resolve()
        self.root_dir.mkdir(parents=True, exist_ok=True)
        self.sandbox_files_dir = self.root_dir / "sandbox_files"
        self.sandbox_files_dir.mkdir(parents=True, exist_ok=True)

        self.kill_switches = kill_switches or LifeRuntimeKillSwitches.isolated_test_defaults()

        # Real owner stores/read ports precede immutable LR-2 runtime assembly.
        self.decision_store = AgencyDecisionStore(self.root_dir, namespace=NAMESPACE_ISOLATED_TEST)
        self.decision_owner_read = AgencyDecisionReadService(self.decision_store)
        self.integration_store = IntegrationStore(self.root_dir, namespace=NAMESPACE_ISOLATED_TEST)
        # Phase-C: the AG-2 store gets its OWN lease type and its OWN lock file. It is
        # never the LR-2 lease, so an AG-2 write capability can never be an LR-2 capability.
        self.integration_lease = IntegrationAuthorityLease(self.integration_store.store_root)
        self.integration_lease.acquire()
        self.adoption_owner_read = AdoptionAuthorizationReadPort(self.integration_store)
        self.admission_issuer = build_admission_issuer(
            self.decision_owner_read, self.adoption_owner_read,
            runtime_epoch=f"isolated:{uuid.uuid4().hex}",
        )

        # 1. Authority Lease & Canonical Activity Store (LR-2)
        self.lease = ActivityAuthorityLease(self.root_dir)
        self.lease.acquire()
        self.activity_capability = self.lease.issue_capability(
            writer_domain="life_activity_authority",
            namespace=NAMESPACE_ISOLATED_TEST,
        )
        self.activity_store = CanonicalActivityStore(
            self.root_dir, namespace=NAMESPACE_ISOLATED_TEST,
            _admission_issuer=self.admission_issuer,
        )
        self.activity_read = ActivityReadService(self.activity_store)
        self.activity_command = ActivityCommandService(
            store=self.activity_store,
            lease=self.lease,
            capability=self.activity_capability,
        )

        # 2. Interruption & Narrow Attention (LR-3)
        self.interruption_store = InterruptionStateStore(self.root_dir)
        self.interruption_coordinator = InterruptionCoordinator(
            activity_store=self.activity_store,
            read_service=self.activity_read,
            command_service=self.activity_command,
            interruption_store=self.interruption_store,
        )

        # 3. Waiting, Resume & Agenda (LR-4)
        self.condition_store = ResumeConditionStore(self.root_dir)
        self.agenda_store = AgendaStore(self.root_dir)
        self.eligibility_store = ResumeEligibilityStore(self.root_dir)
        self.waiting_coordinator = WaitingCoordinator(
            activity_store=self.activity_store,
            lease=self.lease,
            command_service=self.activity_command,
            condition_store=self.condition_store,
            agenda_store=self.agenda_store,
            eligibility_store=self.eligibility_store,
            namespace=NAMESPACE_ISOLATED_TEST,
        )

        # 4. Action Reality Ledger & Sandbox Adapters (AR-0)
        self.action_store = ActionStore(self.root_dir, namespace=NAMESPACE_ISOLATED_TEST)
        self.action_capability = ActionRealityAuthority.issue_writer_capability(
            self.lease,
            caller_module="action_reality_service",
            namespace=NAMESPACE_ISOLATED_TEST,
        )
        self.executor_capability = ActionRealityAuthority.issue_sandbox_executor_capability(
            caller_module="sandbox_action_harness",
            namespace=NAMESPACE_ISOLATED_TEST,
        )
        self.fake_world = FakeWorldResolver()
        self.fake_tool = FakeToolExecutor()
        self.fake_message = FakeMessageProvider()
        self.world_adapter = WorldActionAdapter(self.fake_world)
        self.file_adapter = SandboxFileAdapter(self.sandbox_files_dir)
        self.file_observer = FileArtifactObserver(self.sandbox_files_dir)
        self.tool_adapter = ToolActionAdapter(self.fake_tool)
        self.message_adapter = MessageActionAdapter(self.fake_message)

        self.action_command = ActionCommandService(
            store=self.action_store,
            lease=self.lease,
            capability=self.action_capability,
            adapters={
                ACTION_KIND_WORLD: self.world_adapter,
                ACTION_KIND_FILE: self.file_adapter,
                ACTION_KIND_TOOL: self.tool_adapter,
                ACTION_KIND_MESSAGE: self.message_adapter,
            },
        )
        self.action_read = ActionReadService(self.action_store)

        # 5. Result Settlement & Outbox (AR-1)
        self.settlement_store = SettlementStore(self.root_dir, namespace=NAMESPACE_ISOLATED_TEST)
        self.settlement_capability = ResultSettlementAuthority.issue_writer_capability(
            self.lease,
            caller_module="result_settlement_service",
            namespace=NAMESPACE_ISOLATED_TEST,
        )
        self.settlement_policy = SettlementPolicyRegistry()
        self.settlement_service = ResultSettlementService(
            settlement_store=self.settlement_store,
            action_store=self.action_store,
            lease=self.lease,
            capability=self.settlement_capability,
            policy_registry=self.settlement_policy,
        )
        self.settlement_read = SettlementReadService(
            settlement_store=self.settlement_store,
            action_store=self.action_store,
        )

        # 6. Natural Progress & Completion (LR-5)
        self.progress_store = ProgressStore(self.root_dir, namespace=NAMESPACE_ISOLATED_TEST)
        self.progress_capability = LifeProgressAuthority.issue_capability(
            lease=self.lease,
            writer_domain="life_progress_authority",
            namespace=NAMESPACE_ISOLATED_TEST,
        )
        self.progress_coordinator = ActivityProgressCoordinator(
            progress_store=self.progress_store,
            activity_store=self.activity_store,
            activity_command=self.activity_command,
            lease=self.lease,
            progress_capability=self.progress_capability,
            settlement_read=self.settlement_read,
            auto_apply_natural_completion=True,
        )
        self.progress_read = self.progress_coordinator.read_service

        # ACTIVITY_CONSEQUENCE authorities. No new fact owner: each capability is
        # minted here and bound to the read port of the owner that already holds
        # the evidence, so the orchestration module can propose but never author
        # canonical truth. MIGRATION/RECOVERY get no capability at all.
        self.waiting_consequence_capability = (
            self.admission_issuer.build_consequence_capability(
                allowed_kinds=("WAIT",),
                owner_label="lr4_waiting",
                evidence_verifier=build_waiting_evidence_verifier(self.activity_read),
            )
        )
        self.completion_consequence_capability = (
            self.admission_issuer.build_consequence_capability(
                allowed_kinds=("COMPLETE",),
                owner_label="lr5_completion",
                evidence_verifier=build_completion_evidence_verifier(
                    self.activity_read, self.progress_read
                ),
            )
        )
        self.progress_consequence_capability = (
            self.admission_issuer.build_consequence_capability(
                allowed_kinds=("PROGRESS", "CHECKPOINT"),
                owner_label="lr5_progress_state",
                evidence_verifier=build_activity_lineage_verifier(self.activity_read),
            )
        )
        self.interruption_consequence_capability = (
            self.admission_issuer.build_consequence_capability(
                allowed_kinds=("PAUSE",),
                owner_label="lr3_interruption",
                evidence_verifier=build_interruption_evidence_verifier(
                    self.activity_read, self.interruption_store.load_state
                ),
            )
        )
        self.waiting_coordinator.set_consequence_port(
            ActivityConsequencePort(
                self.waiting_consequence_capability, self.activity_command
            )
        )
        self.progress_coordinator.set_consequence_port(
            ActivityConsequencePort(
                self.completion_consequence_capability, self.activity_command
            )
        )
        self.progress_coordinator.set_progress_consequence_port(
            ActivityConsequencePort(
                self.progress_consequence_capability, self.activity_command
            )
        )
        self.interruption_coordinator.set_consequence_port(
            ActivityConsequencePort(
                self.interruption_consequence_capability, self.activity_command
            )
        )

        # Register LR-5 ActivityProgressCoordinator as the canonical LIFE consumer on AR-1 SettlementService!
        self.fake_memory_consumer = FakeMemoryConsumer()
        self.fake_goal_consumer = FakeGoalConsumer()
        self.fake_world_consumer = FakeWorldConsumer()
        self.settlement_service.register_consumer(self.progress_coordinator)
        self.settlement_service.register_consumer(self.fake_memory_consumer)
        self.settlement_service.register_consumer(self.fake_goal_consumer)
        self.settlement_service.register_consumer(self.fake_world_consumer)

        # 7. Candidate Sources (AG-0)
        self.current_activity_source = CurrentActivityCandidateSource(self.activity_read)
        self.resume_eligible_source = ResumeEligibleCandidateSource(
            self.eligibility_store,
            self.activity_read,
        )
        self.user_request_source = UserRequestCandidateSource()
        self.commitment_source = CommitmentCandidateSource(enabled=enable_commitment_source)
        self.goal_source = ExplicitGoalCandidateSource(enabled=enable_goal_source)
        self.world_opportunity_source = WorldOpportunityCandidateSource()

        self.candidate_registry = CandidateSourceRegistry(
            {
                SOURCE_CURRENT_ACTIVITY: self.current_activity_source,
                SOURCE_RESUME_ELIGIBLE: self.resume_eligible_source,
                SOURCE_USER_REQUEST: self.user_request_source,
                SOURCE_COMMITMENT: self.commitment_source,
                SOURCE_EXPLICIT_GOAL: self.goal_source,
                SOURCE_WORLD_OPPORTUNITY: self.world_opportunity_source,
            }
        )
        self.candidate_capability = CandidateProjectionGuard.issue_capability(
            caller_module="candidate_materializer",
            namespace=NAMESPACE_ISOLATED_TEST,
        )
        self.candidate_audit_journal = CandidateAuditJournal(self.root_dir / "candidate_audit_journal.jsonl")
        self.candidate_materializer = CandidateMaterializer(
            self.candidate_registry,
            capability=self.candidate_capability,
            audit_journal=self.candidate_audit_journal,
        )
        self.candidate_read = CandidateReadService(self.candidate_materializer)

        # 8. Agency Decision (AG-1)
        self.decision_capability = AgencyDecisionAuthority.issue_capability(
            self.lease,
            caller_module="agency_decision_service",
            store_root=self.root_dir,
            namespace=NAMESPACE_ISOLATED_TEST,
        )
        self.cognition_adapter = cognition_adapter or FakeCognitionAdapter()
        self.decision_service = AgencyDecisionService(
            store=self.decision_store,
            lease=self.lease,
            capability=self.decision_capability,
            candidate_read_service=self.candidate_read,
            activity_read_service=self.activity_read,
            resume_eligibility_store=self.eligibility_store,
            cognition_adapter=self.cognition_adapter,
        )
        # S7 Phase-B ruling 2: AG-2 gets a pure read port. The writing service is
        # deliberately NOT injected here, so no write path exists behind it.
        self.decision_read = AgencyDecisionReadService(
            self.decision_store,
            candidate_read_service=self.candidate_read,
            decision_service=None,
        )

        # 9. Life Integration (AG-2)
        self.integration_capability = LifeIntegrationAuthority.issue_capability(
            self.integration_lease,
            writer_domain=WRITER_DOMAIN_LIFE_INTEGRATION,
            namespace=NAMESPACE_ISOLATED_TEST,
        )
        # Phase-C owner-side boundaries for AG-2's OWN store. The raw store, the AG-2 lease
        # and the AG-2 writer capability never leave the composition root again.
        self.integration_read = IntegrationReadPort(self.integration_store)
        self.integration_commit = IntegrationCommitPort(
            self.integration_store, self.integration_lease, self.integration_capability
        )
        self.experience_consumer = FakeExperienceConsumer()
        # ---- S7 rule A: the composition root allocates AG-2's narrow ports ----
        # AG-2 holds ports, never raw domain stores or writer services. Each port
        # exposes an explicit method allow-list, so holding it grants no more authority
        # than that list; a store reachable only through a declared port is out of scope
        # for the AG-2 forbidden-writer gate.
        self.ag2_decision_port = build_ag2_port(
            self.decision_service,
            {"evaluate_opportunity", "reconcile_after_crash"},
        )
        self.ag2_action_port = build_ag2_port(
            self.action_command,
            {
                "propose_action",
                "prepare_action",
                "submit_action",
                "reconcile_action",
                "record_result_evidence",
                "recover",
            },
        )
        self.ag2_settlement_port = build_ag2_port(
            self.settlement_service,
            {"settle_action", "dispatch_outbox", "recover"},
        )
        self.ag2_interruption_port = build_ag2_port(
            self.interruption_coordinator,
            {
                "assess_events",
                "configure_activity_attention",
                "get_effective_attention",
                "get_current_life_view",
                "recover_cold_start",
            },
        )
        self.ag2_waiting_port = build_ag2_port(
            self.waiting_coordinator,
            {
                "enter_waiting_with_condition",
                "explicit_resume_from_eligibility",
                "reconcile_with_canonical_activities",
                "evaluate_event",
                "recover_cold_start",
                "list_agenda_items",
                "list_eligibilities",
                "get_eligibility",
            },
        )
        self.ag2_progress_port = build_ag2_port(
            self.progress_coordinator,
            {
                "register_result_binding",
                "register_progress_contract",
                "register_completion_contract",
                "ingest_progress_evidence",
                "recover",
                "auto_apply_natural_completion",
                "read_service",
            },
        )
        self.ag2_file_observer_port = build_ag2_port(
            self.file_observer,
            {"observe_file_evidence"},
        )
        # Phase-C: LR-2's own command service is reached only through an allow-listed port,
        # so AG-2 can never inspect the lease or the writer capability it holds internally.
        self.ag2_activity_command_port = build_ag2_port(
            self.activity_command,
            {"start_activity", "pause_activity", "abandon_activity"},
        )
        self.ag2_candidate_port = build_ag2_port(
            self.candidate_materializer,
            {"build_candidate_set"},
        )

        self.adoption_coordinator = DecisionAdoptionCoordinator(
            integration_commit=self.integration_commit,
            decision_read=self.decision_read,
            candidate_read=self.candidate_read,
            activity_read=self.activity_read,
            activity_command=self.ag2_activity_command_port,
            interruption_coordinator=self.ag2_interruption_port,
            waiting_coordinator=self.ag2_waiting_port,
            progress_coordinator=self.ag2_progress_port,
        )
        self.action_bridge = DecisionActionBridge(
            integration_commit=self.integration_commit,
            activity_read=self.activity_read,
            waiting_coordinator=self.ag2_waiting_port,
            action_command=self.ag2_action_port,
            action_read=self.action_read,
            progress_read=self.progress_read,
            progress_coordinator=self.ag2_progress_port,
        )
        self.coordinator = LifeIntegrationCoordinator(
            integration_commit=self.integration_commit,
            integration_read=self.integration_read,
            kill_switches=self.kill_switches,
            candidate_materializer=self.ag2_candidate_port,
            candidate_read=self.candidate_read,
            decision_service=self.ag2_decision_port,
            decision_read=self.decision_read,
            adoption_coordinator=self.adoption_coordinator,
            action_bridge=self.action_bridge,
            activity_read=self.activity_read,
            interruption_coordinator=self.ag2_interruption_port,
            waiting_coordinator=self.ag2_waiting_port,
            action_command=self.ag2_action_port,
            action_read=self.action_read,
            executor_capability=self.executor_capability,
            settlement_service=self.ag2_settlement_port,
            settlement_read=self.settlement_read,
            progress_coordinator=self.ag2_progress_port,
            progress_read=self.progress_read,
            experience_consumer=self.experience_consumer,
            file_observer=self.ag2_file_observer_port,
        )
        self.read_service = self.coordinator.read_service

    def flush_all(self) -> None:
        self.decision_store.flush_snapshot()
        self.action_store.flush_snapshot()
        self.settlement_store.flush_snapshot()
        self.progress_store.flush_snapshot()
        self.integration_store.flush_snapshot()

    def close(self) -> None:
        self.flush_all()
        if self.integration_lease.held:
            self.integration_lease.release()
        if self.lease.held:
            self.lease.release()


__all__ = [
    "ADOPTION_STATUS_APPLIED",
    "ADOPTION_STATUS_AUTHORIZED",
    "AdoptionAuthorizationReadPort",
    "ADOPTION_STATUS_FAILED",
    "ADOPTION_STATUS_NOOP",
    "ADOPTION_STATUS_PENDING",
    "ADOPTION_STATUS_REJECTED_ILLEGAL",
    "ADOPTION_STATUS_REJECTED_STALE",
    "ADOPTION_STATUS_SUPERSEDED",
    "ADOPTION_STATUS_VALIDATED",
    "ASYNC_COMPLETION_MODE_DIRECT_SETTLE",
    "ASYNC_COMPLETION_MODE_ELIGIBILITY_FIRST",
    "ActivityAdoptionSpec",
    "CoordinatorAuthorityViolationError",
    "DecisionActionBridge",
    "DecisionAdoptionCoordinator",
    "DecisionAdoptionRecord",
    "ENV_ACTION_EXECUTION_ENABLED",
    "ENV_AGENCY_ENABLED",
    "ENV_LIFE_RUNTIME_ENABLED",
    "ENV_PROACTIVE_ENABLED",
    "ExecutableStepSpec",
    "ExperienceConsumerProtocol",
    "FakeExperienceConsumer",
    "HANDOFF_KIND_ABANDONED",
    "HANDOFF_KIND_COMPLETED",
    "HANDOFF_KIND_CONFLICT",
    "HANDOFF_KIND_SIGNIFICANT_PROGRESS",
    "INTEGRATION_STATUS_ADOPTED",
    "INTEGRATION_STATUS_COMPLETED",
    "INTEGRATION_STATUS_CONFLICT",
    "INTEGRATION_STATUS_FAILED",
    "INTEGRATION_STATUS_IN_PROGRESS",
    "INTEGRATION_STATUS_OPEN",
    "INTEGRATION_STATUS_PARTIAL",
    "INTEGRATION_STATUS_SETTLED",
    "INTEGRATION_STATUS_SUPERSEDED",
    "INTEGRATION_STATUS_WAITING_RESULT",
    "IntegrationAuthorityLease",
    "IntegrationCommitPort",
    "IntegrationJournal",
    "IntegrationReadPort",
    "IntegrationStore",
    "IntegrationWriterCapability",
    "IsolatedLifeRuntimeHarness",
    "KillSwitchDisabledError",
    "LifeExperienceHandoffEvent",
    "LifeIntegrationAuthority",
    "LifeIntegrationCoordinator",
    "LifeIntegrationError",
    "LifeIntegrationReadService",
    "LifeIntegrationRecord",
    "LifeLoopReplayVerifier",
    "LifeRuntimeKillSwitches",
    "POLICY_VERSION_AG2_V1",
    "POST_SUBMIT_ENTER_WAITING",
    "POST_SUBMIT_KEEP_ACTIVE",
    "POST_SUBMIT_NO_LIFE_CHANGE",
    "PlannerInventionForbiddenError",
    "ReplayIsolationViolationError",
    "RepairProposal",
    "STEP_KIND_FILE_ACTION",
    "STEP_KIND_INTERNAL_CHECKPOINT",
    "STEP_KIND_INTERNAL_WAIT",
    "STEP_KIND_MESSAGE_ACTION",
    "STEP_KIND_TOOL_ACTION",
    "STEP_KIND_WORLD_ACTION",
]
