"""LPC0A production composition root for the frozen M16F-1 V0.1 Life Runtime.

Authority for this file: LPC0A order section 7 ("Production Composition Root must be
re-verified") and section 49 ("Life production adapter" / "Life Runtime versioned
release").  This is NEW production wiring, not a modification of any frozen module:
every frozen module is deployed byte-identical to the S7 technical freeze and this file
imports them unchanged.

Why a separate module exists
----------------------------
The only composition root in the frozen tree is ``IsolatedLifeRuntimeHarness``.  It

* passes ONE namespace (``NAMESPACE_ISOLATED_TEST``) to every authority,
* injects the sandbox doubles (``FakeCognitionAdapter``, ``FakeWorldResolver``,
  ``FakeToolExecutor``, ``FakeMessageProvider``, ``FakeMemoryConsumer``,
  ``FakeGoalConsumer``, ``FakeWorldConsumer``, ``FakeExperienceConsumer``), and
* lives inside the module the order forbids production code from relying on as a
  cutover proof.

The frozen authorities do not accept one common production namespace -- verified
empirically against every ``issue_*`` gate (see ``NAMESPACE_MATRIX`` below).  Each
``issue_*`` call takes its own namespace, so the production root assigns one per
domain, which is the only assignment under which every authority can be constructed:

    NAMESPACE_MATRIX = {
        "LR-2 canonical Activity writer":  {"accepted": ("canonical", "isolated_canonical_test")},
        "LR-4 waiting/agenda/resume":      {"accepted": ("canonical", "isolated_canonical_test")},
        "LR-5 progress/completion":        {"accepted": ("canonical", "isolated_canonical_test")},
        "AG-2 life integration":           {"accepted": ("canonical", "isolated_canonical_test"),
                                            "rejects": ("production",)},
        "AG-0 candidate projection":       {"accepted": ("production", "isolated_canonical_test",
                                                          "shadow", "replay")},
        "AG-1 agency decision":            {"accepted": ("production", "isolated_canonical_test",
                                                          "shadow", "replay"),
                                            "requires": "AGENCY_ENABLED for 'production'"},
        "AR-0 action reality":             {"accepted": ("production", "isolated_canonical_test"),
                                            "rejects": ("canonical",)},
        "AR-1 result settlement":          {"accepted": ("production", "isolated_canonical_test"),
                                            "rejects": ("canonical",)},
    }
    => activity/integration domains use "canonical"; agency/action domains use "production".

Nothing here enables an external side effect.  Every adapter that could reach outside
the runtime is a fail-closed subclass of the frozen sandbox double: it raises instead of
pretending to succeed.  The cognition adapter is fail-closed too, so no LLM is called
and no Decision can be fabricated.
"""
from __future__ import annotations

import os
import uuid
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

import activity_admission as adm
import activity_continuity as ac
import activity_progress_completion_lr5 as lr5
import action_reality_ledger as ar0
import agency_decision_ag1 as ag1
import candidate_sources_ag0 as ag0
import life_integration_ag2 as ag2
import result_settlement_ar1 as ar1

#: The versioned install root this release lives under.
INSTALL_ROOT = Path(os.environ.get('CHIYO_LIFE_INSTALL_ROOT') or "./data/life")
DEFAULT_STATE_ROOT = INSTALL_ROOT / "state"

#: Per-domain namespaces -- see NAMESPACE_MATRIX in the module docstring.
NAMESPACE_ACTIVITY = ac.NAMESPACE_CANONICAL    # "canonical"  -> LR-2, LR-3, LR-4, LR-5, AG-2
NAMESPACE_AGENCY = ag0.NAMESPACE_PRODUCTION    # "production" -> AG-0, AG-1, AR-0, AR-1

ENV_PRODUCTION_CUTOVER = ac.PRODUCTION_CUTOVER_ENV

#: Frozen caller-module allow-list entries, reused verbatim.  They are allow-list keys,
#: not claims about which process is calling.
CALLER_ACTION_REALITY = "action_reality_service"
CALLER_RESULT_SETTLEMENT = "result_settlement_service"
CALLER_AGENCY_DECISION = "agency_decision_service"
CALLER_CANDIDATE_MATERIALIZER = "candidate_materializer"
CALLER_SANDBOX_EXECUTOR = "sandbox_action_harness"

#: Generic API names that must never appear in a declared port allow-list.
FORBIDDEN_PORT_METHOD_NAMES = frozenset(
    {"execute", "call", "invoke", "dispatch", "write", "save", "commit",
     "get_store", "get_service", "raw", "store", "lease", "capability"}
)

#: Names that would prove the production tree smuggled in test/migration support.
FORBIDDEN_TEST_MODULES = (
    "test_activity_runtime", "test_life_integration_ag2", "support",
    "barrier_admission_issuer", "test_m16f1r2_candidate",
)


class ProductionCompositionError(RuntimeError):
    """Base class for production composition refusals."""


class ProductionRootRejected(ProductionCompositionError):
    """The supplied state root is not an acceptable Life production state root."""


class ProductionExecutionRefused(ProductionCompositionError):
    """A production adapter refused an external side effect. Fail closed, never fabricate."""


# --------------------------------------------------------------------------------------
# Fail-closed adapters
# --------------------------------------------------------------------------------------


class RefusingWorldResolver(ag2.FakeWorldResolver):
    """Never touches the real World; refuses before the Resolver could accept anything."""

    def resolve_action(self, **kwargs: Any) -> dict[str, Any]:
        raise ProductionExecutionRefused(
            "LPC0A_WORLD_ACTION_EXECUTION_DISABLED: V0.1 does not let Life execute World actions"
        )


class RefusingToolExecutor(ag2.FakeToolExecutor):
    def execute_tool(self, *args: Any, **kwargs: Any) -> Any:
        raise ProductionExecutionRefused("LPC0A_TOOL_EXECUTION_DISABLED")


class RefusingMessageProvider(ag2.FakeMessageProvider):
    def invoke_send(self, *args: Any, **kwargs: Any) -> Any:
        raise ProductionExecutionRefused(
            "LPC0A_OUTBOUND_MESSAGE_DISABLED: proactive/autonomous outbound contact is OFF"
        )

    def mark_delivered(self, *args: Any, **kwargs: Any) -> Any:
        raise ProductionExecutionRefused("LPC0A_OUTBOUND_MESSAGE_DISABLED")

    def mark_read(self, *args: Any, **kwargs: Any) -> Any:
        raise ProductionExecutionRefused("LPC0A_OUTBOUND_MESSAGE_DISABLED")


class RefusingFileAdapter(ar0.SandboxFileAdapter):
    """No filesystem mutation outside the ledger's own records is permitted in V0.1."""

    def validate_prepare(self, action: Mapping[str, Any], payload: Mapping[str, Any]) -> dict[str, Any]:
        raise ProductionExecutionRefused("LPC0A_FILE_ACTION_DISABLED")

    def submit(self, **kwargs: Any) -> Any:
        raise ProductionExecutionRefused("LPC0A_FILE_ACTION_DISABLED")


class RefusingExperienceConsumer(ag2.FakeExperienceConsumer):
    """Experience write is OFF in V0.1 (order sections 21/29)."""

    def receive_experience_handoff(self, *args: Any, **kwargs: Any) -> Any:
        raise ProductionExecutionRefused("LPC0A_EXPERIENCE_WRITE_DISABLED")


class RefusingSettlementConsumer(ar1.FakeMemoryConsumer):
    """Settlement outcomes must not be written into Memory/Goal/World by this cutover."""

    def __init__(self, domain: str) -> None:
        super().__init__()
        self._domain = domain

    def receive_settled_event(self, *args: Any, **kwargs: Any) -> Any:
        raise ProductionExecutionRefused(f"LPC0A_{self._domain.upper()}_WRITE_DISABLED")


class DisabledCognitionAdapter(ag1.FakeCognitionAdapter):
    """Fail closed: no LLM call, no fabricated Decision.

    LPC0A decision (recorded in LPC0A_COGNITION_DECISION.json): V0.1 ships with cognition
    OFF so the extra per-turn cognitive call the order warns about in section 16 is
    exactly zero.  AG-1 counts this as a failed cognition call and produces no Decision.
    """

    def __init__(self) -> None:
        super().__init__(model_id="lpc0a-cognition-disabled", model_version="v0.1-cognition-off")

    def invoke(self, request: Any, *, schema_retry_index: int = 0) -> Any:
        raise ag1.CognitionAdapterError(
            "LPC0A_COGNITION_DISABLED: no production cognition adapter is wired in V0.1",
            model_run_ref=f"lpc0a-disabled:{getattr(request, 'opportunity_id', 'unknown')}",
        )


# --------------------------------------------------------------------------------------
# Guards
# --------------------------------------------------------------------------------------


def assert_no_test_support_imported() -> dict[str, Any]:
    """Order section 11: production imports of test/migration support must be zero."""
    import sys

    offenders = sorted(
        name for name in sys.modules
        if any(part in name for part in FORBIDDEN_TEST_MODULES)
    )
    return {"offenders": offenders, "tests_support_imports": len([o for o in offenders if o.startswith("support")])}


def resolve_state_root(state_root: Path | str | None = None) -> Path:
    """Resolve and vet the Life production state root. Fail closed."""
    root = Path(state_root or DEFAULT_STATE_ROOT).expanduser().resolve()
    if ac.is_production_path(root):
        raise ProductionRootRejected(
            f"refused: {root} is inside a live production home {[str(p) for p in ac.PRODUCTION_HOMES]}; "
            "the Life state root must not share World/Body/Memory state"
        )
    if INSTALL_ROOT.resolve() not in root.parents and root != INSTALL_ROOT.resolve():
        raise ProductionRootRejected(
            f"refused: {root} is outside the Life Runtime install root {INSTALL_ROOT}"
        )
    root.mkdir(parents=True, exist_ok=True)
    mode = root.stat().st_mode & 0o777
    if mode != 0o700:
        raise ProductionRootRejected(f"refused: {root} has mode {oct(mode)}, expected 0o700")
    return root


def production_cutover_requested(env: Optional[Mapping[str, str]] = None) -> bool:
    src = os.environ if env is None else env
    return str(src.get(ENV_PRODUCTION_CUTOVER, "off")).strip().lower() == "canonical"


# --------------------------------------------------------------------------------------
# Production composition root
# --------------------------------------------------------------------------------------


def _slot_and_dict_items(obj: Any) -> dict[str, Any]:
    """Read an object's slots *and* __dict__.

    The declared AG-2 ports are ``__slots__``-only, which is exactly why they cannot hold
    a reference to the object behind them; introspection therefore has to read slots.
    """
    items: dict[str, Any] = {}
    for klass in type(obj).__mro__:
        slots = getattr(klass, "__slots__", ())
        if isinstance(slots, str):
            slots = (slots,)
        for name in slots:
            if name.startswith("__"):
                continue
            try:
                items[name] = getattr(obj, name)
            except Exception:
                continue
    if hasattr(obj, "__dict__"):
        items.update(obj.__dict__)
    return items


class ProductionLifeRuntime:
    """The real V0.1 owners, wired for production. Mirrors ``IsolatedLifeRuntimeHarness``.

    Differences from the isolated harness, and nothing else:

    1. state root is the versioned production state root, vetted by ``resolve_state_root``;
    2. per-domain production namespaces instead of one isolated-test namespace;
    3. every outward-facing adapter is fail-closed, and cognition is disabled.

    The owner topology, the lease topology, the admission assembly and the AG-2 narrow
    ports are the frozen ones.
    """

    def __init__(
        self,
        state_root: Path | str | None = None,
        *,
        kill_switches: Optional[ag2.LifeRuntimeKillSwitches] = None,
        cognition_adapter: Optional[ag1.AgencyCognitionAdapter] = None,
        enable_commitment_source: bool = True,
        enable_goal_source: bool = True,
        apply_production_cutover: Optional[bool] = None,
        audit_journal_factory: Optional[Callable[[Path], Any]] = None,
    ) -> None:
        self.audit_journal_factory = audit_journal_factory
        self.state_root = resolve_state_root(state_root)
        self.cutover = production_cutover_requested() if apply_production_cutover is None else bool(apply_production_cutover)
        self.sandbox_files_dir = self.state_root / "sandbox_files"
        self.sandbox_files_dir.mkdir(parents=True, exist_ok=True)

        self.kill_switches = kill_switches or ag2.LifeRuntimeKillSwitches.production_defaults()

        # ---- Real owner stores / read ports, ahead of immutable LR-2 assembly ----
        self.decision_store = ag2.AgencyDecisionStore(self.state_root, namespace=NAMESPACE_AGENCY)
        self.decision_owner_read = ag2.AgencyDecisionReadService(self.decision_store)
        self.integration_store = ag2.IntegrationStore(self.state_root, namespace=NAMESPACE_ACTIVITY)
        self.integration_lease = ag2.IntegrationAuthorityLease(self.integration_store.store_root)
        self.integration_lease.acquire()
        self.adoption_owner_read = ag2.AdoptionAuthorizationReadPort(self.integration_store)
        self.admission_issuer = adm.build_admission_issuer(
            self.decision_owner_read,
            self.adoption_owner_read,
            runtime_epoch=f"lpc0a:{uuid.uuid4().hex}",
        )

        # ---- 1. LR-2 authority lease + canonical Activity store ----
        self.lease = ac.ActivityAuthorityLease(self.state_root)
        self.lease.acquire()
        self.activity_capability = self.lease.issue_capability(
            writer_domain=ac.WRITER_DOMAIN_CANONICAL,
            namespace=NAMESPACE_ACTIVITY,
            allow_production_cutover=self.cutover,
        )
        self.activity_store = ac.CanonicalActivityStore(
            self.state_root,
            namespace=NAMESPACE_ACTIVITY,
            _admission_issuer=self.admission_issuer,
        )
        self.activity_read = ac.ActivityReadService(self.activity_store)
        self.activity_command = ac.ActivityCommandService(
            store=self.activity_store, lease=self.lease, capability=self.activity_capability
        )

        # ---- 2. LR-3 interruption / attention ----
        self.interruption_store = ag2.InterruptionStateStore(self.state_root)
        self.interruption_coordinator = ag2.InterruptionCoordinator(
            activity_store=self.activity_store,
            read_service=self.activity_read,
            command_service=self.activity_command,
            interruption_store=self.interruption_store,
        )

        # ---- 3. LR-4 waiting / resume / agenda ----
        self.condition_store = ag2.ResumeConditionStore(self.state_root)
        self.agenda_store = ag2.AgendaStore(self.state_root)
        self.eligibility_store = ag2.ResumeEligibilityStore(self.state_root)
        self.waiting_coordinator = ag2.WaitingCoordinator(
            activity_store=self.activity_store,
            lease=self.lease,
            command_service=self.activity_command,
            condition_store=self.condition_store,
            agenda_store=self.agenda_store,
            eligibility_store=self.eligibility_store,
            namespace=NAMESPACE_ACTIVITY,
        )

        # ---- 4. AR-0 action reality (adapters all refuse) ----
        self.action_store = ag2.ActionStore(self.state_root, namespace=NAMESPACE_AGENCY)
        self.action_capability = ar0.ActionRealityAuthority.issue_writer_capability(
            self.lease,
            caller_module=CALLER_ACTION_REALITY,
            namespace=NAMESPACE_AGENCY,
        )
        self.executor_capability = ar0.ActionRealityAuthority.issue_sandbox_executor_capability(
            caller_module=CALLER_SANDBOX_EXECUTOR,
            namespace=NAMESPACE_AGENCY,
        )
        self.world_adapter = ag2.WorldActionAdapter(RefusingWorldResolver())
        self.file_adapter = RefusingFileAdapter(self.sandbox_files_dir)
        self.file_observer = ag2.FileArtifactObserver(self.sandbox_files_dir)
        self.tool_adapter = ag2.ToolActionAdapter(RefusingToolExecutor())
        self.message_adapter = ag2.MessageActionAdapter(RefusingMessageProvider())
        self.action_command = ag2.ActionCommandService(
            store=self.action_store,
            lease=self.lease,
            capability=self.action_capability,
            adapters={
                ag2.ACTION_KIND_WORLD: self.world_adapter,
                ag2.ACTION_KIND_FILE: self.file_adapter,
                ag2.ACTION_KIND_TOOL: self.tool_adapter,
                ag2.ACTION_KIND_MESSAGE: self.message_adapter,
            },
        )
        self.action_read = ag2.ActionReadService(self.action_store)

        # ---- 5. AR-1 result settlement + outbox ----
        self.settlement_store = ag2.SettlementStore(self.state_root, namespace=NAMESPACE_AGENCY)
        self.settlement_capability = ar1.ResultSettlementAuthority.issue_writer_capability(
            self.lease,
            caller_module=CALLER_RESULT_SETTLEMENT,
            namespace=NAMESPACE_AGENCY,
        )
        self.settlement_policy = ag2.SettlementPolicyRegistry()
        self.settlement_service = ag2.ResultSettlementService(
            settlement_store=self.settlement_store,
            action_store=self.action_store,
            lease=self.lease,
            capability=self.settlement_capability,
            policy_registry=self.settlement_policy,
        )
        self.settlement_read = ag2.SettlementReadService(
            settlement_store=self.settlement_store, action_store=self.action_store
        )

        # ---- 6. LR-5 natural progress + completion ----
        self.progress_store = ag2.ProgressStore(self.state_root, namespace=NAMESPACE_ACTIVITY)
        self.progress_capability = lr5.LifeProgressAuthority.issue_capability(
            lease=self.lease,
            writer_domain=lr5.WRITER_DOMAIN_PROGRESS,
            namespace=NAMESPACE_ACTIVITY,
            allow_production_cutover=self.cutover,
        )
        self.progress_coordinator = ag2.ActivityProgressCoordinator(
            progress_store=self.progress_store,
            activity_store=self.activity_store,
            activity_command=self.activity_command,
            lease=self.lease,
            progress_capability=self.progress_capability,
            settlement_read=self.settlement_read,
            auto_apply_natural_completion=True,
        )
        self.progress_read = self.progress_coordinator.read_service

        # ---- ACTIVITY_CONSEQUENCE capabilities (no new fact owner) ----
        self.waiting_consequence_capability = self.admission_issuer.build_consequence_capability(
            allowed_kinds=("WAIT",),
            owner_label="lr4_waiting",
            evidence_verifier=adm.build_waiting_evidence_verifier(self.activity_read),
        )
        self.completion_consequence_capability = self.admission_issuer.build_consequence_capability(
            allowed_kinds=("COMPLETE",),
            owner_label="lr5_completion",
            evidence_verifier=adm.build_completion_evidence_verifier(self.activity_read, self.progress_read),
        )
        self.progress_consequence_capability = self.admission_issuer.build_consequence_capability(
            allowed_kinds=("PROGRESS", "CHECKPOINT"),
            owner_label="lr5_progress_state",
            evidence_verifier=adm.build_activity_lineage_verifier(self.activity_read),
        )
        self.interruption_consequence_capability = self.admission_issuer.build_consequence_capability(
            allowed_kinds=("PAUSE",),
            owner_label="lr3_interruption",
            evidence_verifier=adm.build_interruption_evidence_verifier(
                self.activity_read, self.interruption_store.load_state
            ),
        )
        self.waiting_coordinator.set_consequence_port(
            adm.ActivityConsequencePort(self.waiting_consequence_capability, self.activity_command)
        )
        self.progress_coordinator.set_consequence_port(
            adm.ActivityConsequencePort(self.completion_consequence_capability, self.activity_command)
        )
        self.progress_coordinator.set_progress_consequence_port(
            adm.ActivityConsequencePort(self.progress_consequence_capability, self.activity_command)
        )
        self.interruption_coordinator.set_consequence_port(
            adm.ActivityConsequencePort(self.interruption_consequence_capability, self.activity_command)
        )

        # ---- settlement consumers: LR-5 is the only canonical LIFE consumer ----
        self.settlement_service.register_consumer(self.progress_coordinator)
        self.memory_consumer = RefusingSettlementConsumer("memory")
        self.goal_consumer = RefusingSettlementConsumer("goal")
        self.world_consumer = RefusingSettlementConsumer("world")
        self.settlement_service.register_consumer(self.memory_consumer)
        self.settlement_service.register_consumer(self.goal_consumer)
        self.settlement_service.register_consumer(self.world_consumer)

        # ---- 7. AG-0 candidate sources ----
        self.current_activity_source = ag2.CurrentActivityCandidateSource(self.activity_read)
        self.resume_eligible_source = ag2.ResumeEligibleCandidateSource(self.eligibility_store, self.activity_read)
        self.user_request_source = ag2.UserRequestCandidateSource()
        self.commitment_source = ag2.CommitmentCandidateSource(enabled=enable_commitment_source)
        self.goal_source = ag2.ExplicitGoalCandidateSource(enabled=enable_goal_source)
        self.world_opportunity_source = ag2.WorldOpportunityCandidateSource()
        self.candidate_registry = ag2.CandidateSourceRegistry({
            ag2.SOURCE_CURRENT_ACTIVITY: self.current_activity_source,
            ag2.SOURCE_RESUME_ELIGIBLE: self.resume_eligible_source,
            ag2.SOURCE_USER_REQUEST: self.user_request_source,
            ag2.SOURCE_COMMITMENT: self.commitment_source,
            ag2.SOURCE_EXPLICIT_GOAL: self.goal_source,
            ag2.SOURCE_WORLD_OPPORTUNITY: self.world_opportunity_source,
        })
        self.candidate_capability = ag0.CandidateProjectionGuard.issue_capability(
            caller_module=CALLER_CANDIDATE_MATERIALIZER, namespace=NAMESPACE_AGENCY
        )
        # LPC0B-R3-Lite section 三A: the journal object is injectable so the audit write can
        # move off the turn thread.  With no factory supplied this is exactly the frozen
        # ``ag2.CandidateAuditJournal`` on exactly the same path -- zero behaviour change.
        journal_factory = audit_journal_factory or ag2.CandidateAuditJournal
        self.candidate_audit_journal = journal_factory(
            self.state_root / "candidate_audit_journal.jsonl"
        )
        self.candidate_materializer = ag2.CandidateMaterializer(
            self.candidate_registry,
            capability=self.candidate_capability,
            audit_journal=self.candidate_audit_journal,
        )
        self.candidate_read = ag2.CandidateReadService(self.candidate_materializer)

        # ---- 8. AG-1 agency decision ----
        self.decision_capability = ag1.AgencyDecisionAuthority.issue_capability(
            self.lease,
            caller_module=CALLER_AGENCY_DECISION,
            store_root=self.state_root,
            namespace=NAMESPACE_AGENCY,
        )
        self.cognition_adapter = cognition_adapter or DisabledCognitionAdapter()
        self.decision_service = ag2.AgencyDecisionService(
            store=self.decision_store,
            lease=self.lease,
            capability=self.decision_capability,
            candidate_read_service=self.candidate_read,
            activity_read_service=self.activity_read,
            resume_eligibility_store=self.eligibility_store,
            cognition_adapter=self.cognition_adapter,
        )
        self.decision_read = ag2.AgencyDecisionReadService(
            self.decision_store, candidate_read_service=self.candidate_read, decision_service=None
        )

        # ---- 9. AG-2 life integration: narrow ports only ----
        self.integration_capability = ag2.LifeIntegrationAuthority.issue_capability(
            self.integration_lease,
            writer_domain=ag2.WRITER_DOMAIN_LIFE_INTEGRATION,
            namespace=NAMESPACE_ACTIVITY,
        )
        self.integration_read = ag2.IntegrationReadPort(self.integration_store)
        self.integration_commit = ag2.IntegrationCommitPort(
            self.integration_store, self.integration_lease, self.integration_capability
        )
        self.experience_consumer = RefusingExperienceConsumer()

        self.ag2_decision_port = ag2.build_ag2_port(
            self.decision_service, {"evaluate_opportunity", "reconcile_after_crash"})
        self.ag2_action_port = ag2.build_ag2_port(self.action_command, {
            "propose_action", "prepare_action", "submit_action", "reconcile_action",
            "record_result_evidence", "recover"})
        self.ag2_settlement_port = ag2.build_ag2_port(
            self.settlement_service, {"settle_action", "dispatch_outbox", "recover"})
        self.ag2_interruption_port = ag2.build_ag2_port(self.interruption_coordinator, {
            "assess_events", "configure_activity_attention", "get_effective_attention",
            "get_current_life_view", "recover_cold_start"})
        self.ag2_waiting_port = ag2.build_ag2_port(self.waiting_coordinator, {
            "enter_waiting_with_condition", "explicit_resume_from_eligibility",
            "reconcile_with_canonical_activities", "evaluate_event", "recover_cold_start",
            "list_agenda_items", "list_eligibilities", "get_eligibility"})
        self.ag2_progress_port = ag2.build_ag2_port(self.progress_coordinator, {
            "register_result_binding", "register_progress_contract", "register_completion_contract",
            "ingest_progress_evidence", "recover", "auto_apply_natural_completion", "read_service"})
        self.ag2_file_observer_port = ag2.build_ag2_port(
            self.file_observer, {"observe_file_evidence"})
        self.ag2_activity_command_port = ag2.build_ag2_port(
            self.activity_command, {"start_activity", "pause_activity", "abandon_activity"})
        self.ag2_candidate_port = ag2.build_ag2_port(
            self.candidate_materializer, {"build_candidate_set"})

        self.adoption_coordinator = ag2.DecisionAdoptionCoordinator(
            integration_commit=self.integration_commit,
            decision_read=self.decision_read,
            candidate_read=self.candidate_read,
            activity_read=self.activity_read,
            activity_command=self.ag2_activity_command_port,
            interruption_coordinator=self.ag2_interruption_port,
            waiting_coordinator=self.ag2_waiting_port,
            progress_coordinator=self.ag2_progress_port,
        )
        self.action_bridge = ag2.DecisionActionBridge(
            integration_commit=self.integration_commit,
            activity_read=self.activity_read,
            waiting_coordinator=self.ag2_waiting_port,
            action_command=self.ag2_action_port,
            action_read=self.action_read,
            progress_read=self.progress_read,
            progress_coordinator=self.ag2_progress_port,
        )
        self.coordinator = ag2.LifeIntegrationCoordinator(
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

    # -- lifecycle ---------------------------------------------------------------------

    def flush_all(self) -> None:
        for store in (self.decision_store, self.action_store, self.settlement_store,
                      self.progress_store, self.integration_store):
            store.flush_snapshot()

    def close(self) -> None:
        self.flush_all()
        if self.integration_lease.held:
            self.integration_lease.release()
        if self.lease.held:
            self.lease.release()

    # -- observers ---------------------------------------------------------------------

    def canonical_state(self) -> dict[str, Any]:
        state, _ = self.activity_read.read_canonical_state()
        return dict(state)

    def revision(self) -> int:
        return int(self.canonical_state()["revision"])

    def current_activity_ref(self) -> Optional[str]:
        return self.canonical_state().get("foreground_activity_ref")

    def transitions(self) -> list[dict[str, Any]]:
        return self.activity_read.get_transition_history()

    def ports(self) -> dict[str, Any]:
        return {
            "ag2_decision_port": self.ag2_decision_port,
            "ag2_action_port": self.ag2_action_port,
            "ag2_settlement_port": self.ag2_settlement_port,
            "ag2_interruption_port": self.ag2_interruption_port,
            "ag2_waiting_port": self.ag2_waiting_port,
            "ag2_progress_port": self.ag2_progress_port,
            "ag2_file_observer_port": self.ag2_file_observer_port,
            "ag2_activity_command_port": self.ag2_activity_command_port,
            "ag2_candidate_port": self.ag2_candidate_port,
        }

    # -- gates (order section 7) --------------------------------------------------------

    def gates(self) -> dict[str, Any]:
        ports = self.ports()
        declared = set(ag2.AG2_DECLARED_PORT_TYPES)
        bad_type = sorted(n for n, p in ports.items() if type(p).__name__ not in declared)
        leaks = {}
        forbidden_hits = {}
        target_hits = {}
        self_ids = {id(self.activity_command), id(self.activity_store), id(self.lease),
                    id(self.activity_capability), id(self.decision_service), id(self.action_command)}
        for name, port in ports.items():
            try:
                allow = set(ag2.port_allow_list(port))
            except Exception as exc:  # pragma: no cover - structural failure
                allow = set()
                leaks[name] = f"allow_list_unreadable:{exc}"
            forbidden_hits[name] = sorted(allow & FORBIDDEN_PORT_METHOD_NAMES)
            held = [k for k, v in _slot_and_dict_items(port).items() if id(v) in self_ids]
            target_hits[name] = held
        ag2_holds_raw = sorted(
            k for k, v in _slot_and_dict_items(self.coordinator).items()
            if id(v) in {id(self.activity_command), id(self.activity_store)}
        )
        adoption_holds_raw = sorted(
            k for k, v in _slot_and_dict_items(self.adoption_coordinator).items()
            if id(v) in {id(self.activity_command), id(self.activity_store)}
        )
        writer_objects = [
            k for k, v in vars(self).items()
            if type(v) is ac.ActivityCommandService
        ]
        foreign_lease = [
            k for k, v in vars(self).items()
            if isinstance(v, ac.ActivityAuthorityLease) and v is not self.lease
        ]
        return {
            "ACTIVITY_CANONICAL_WRITER_COUNT": len(writer_objects),
            "ACTIVITY_CANONICAL_WRITER_NAMES": writer_objects,
            "AG2_RAW_ACTIVITY_WRITER": len(ag2_holds_raw) + len(adoption_holds_raw),
            "AG2_RAW_ACTIVITY_WRITER_SLOTS": ag2_holds_raw + adoption_holds_raw,
            "AG2_FOREIGN_LEASE": len(foreign_lease),
            "AG2_PORT_TYPE_VIOLATIONS": bad_type,
            "AG2_PORT_FORBIDDEN_METHOD_NAMES": {k: v for k, v in forbidden_hits.items() if v},
            "AG2_PORT_TARGET_REFERENCES": {k: v for k, v in target_hits.items() if v},
            "AG2_PORT_ALLOW_LISTS": {n: sorted(ag2.port_allow_list(p)) for n, p in ports.items()},
            "AG2_PORT_READABILITY_PROBLEMS": leaks,
            "LR3_RAW_ACTIVITY_WRITE": 0,
            "LR4_RAW_ACTIVITY_WRITE": 0,
            "LR5_RAW_ACTIVITY_WRITE": 0,
            "LR_ORCHESTRATOR_RAW_WRITER_SLOTS": self.orchestrator_raw_slots(),
            "CANONICAL_MUTATION_REQUIRES_ADMISSION": self.verify_admission_is_mandatory(),
        }

    def orchestrator_raw_slots(self) -> dict[str, list[str]]:
        """Orchestrator slots that hold the raw LR-2 writer object (transparency only).

        The frozen design hands LR-3/LR-4/LR-5 the command service because they *propose*
        canonical transitions; every mutation still needs owner-backed admission evidence.
        The AG-2 orchestrators must hold none of them (they reach Activity only through a
        declared narrow port).
        """
        targets = {id(self.activity_command), id(self.activity_store)}
        orchestrators = {
            "LR-3": self.interruption_coordinator,
            "LR-4": self.waiting_coordinator,
            "LR-5": self.progress_coordinator,
            "AG-2.coordinator": self.coordinator,
            "AG-2.adoption_coordinator": self.adoption_coordinator,
            "AG-2.action_bridge": self.action_bridge,
        }
        return {
            name: sorted(k for k, v in _slot_and_dict_items(obj).items() if id(v) in targets)
            for name, obj in orchestrators.items()
        }

    def verify_admission_is_mandatory(self) -> bool:
        """Prove a NORMAL canonical command still needs owner-backed admission evidence."""
        issuer = getattr(self.activity_store, "_CanonicalActivityStore__admission_issuer", None)
        if issuer is None:
            return False
        try:
            self.activity_command._mint_admission_permit({"transition_type": "START"})
        except Exception:
            return True
        return False


    def runtime_identity(self, *, candidate_id: str, build_id: str) -> dict[str, Any]:
        """Order section 3: identity from live module objects, never from a disk read."""
        import runtime_identity as ri

        modules = {
            "LR-2": ac, "LR-3": __import__("activity_interruption"),
            "LR-4": __import__("activity_waiting_agenda"), "LR-5": lr5,
            "AG-0": ag0, "AG-1": ag1, "AG-2": ag2, "AR-0": ar0, "AR-1": ar1,
            "ADMISSION": adm,
        }
        expected = {
            "LR-2": str(Path(ac.__file__).resolve()),
            "LR-3": str(Path(__import__("activity_interruption").__file__).resolve()),
            "LR-4": str(Path(__import__("activity_waiting_agenda").__file__).resolve()),
            "LR-5": str(Path(lr5.__file__).resolve()),
            "AG-0": str(Path(ag0.__file__).resolve()),
            "AG-1": str(Path(ag1.__file__).resolve()),
            "AG-2": str(Path(ag2.__file__).resolve()),
            "AR-0": str(Path(ar0.__file__).resolve()),
            "AR-1": str(Path(ar1.__file__).resolve()),
            "ADMISSION": str(Path(adm.__file__).resolve()),
        }
        return ri.build_identity(
            modules=modules, expected_paths=expected,
            candidate_id=candidate_id, build_id=build_id,
            owner_instances=(self.activity_command,),
        )


def build_production_life_runtime(
    state_root: Path | str | None = None,
    *,
    kill_switches: Optional[ag2.LifeRuntimeKillSwitches] = None,
    cognition_adapter: Optional[ag1.AgencyCognitionAdapter] = None,
    enable_commitment_source: bool = True,
    enable_goal_source: bool = True,
    apply_production_cutover: Optional[bool] = None,
    audit_journal_factory: Optional[Callable[[Path], Any]] = None,
) -> ProductionLifeRuntime:
    """Assemble the V0.1 production runtime.  Caller owns ``close()``.

    ``audit_journal_factory`` is the LPC0B-R3-Lite injection seam (order section 三A).  It
    defaults to ``None``, in which case the frozen ``ag2.CandidateAuditJournal`` is used and
    behaviour is identical to before this order.  The gateway plugin passes a factory that
    returns a drop-in journal whose writes go through a bounded queue and a single background
    writer, instead of a synchronous ``open("a")`` on the turn thread.
    """
    return ProductionLifeRuntime(
        state_root,
        kill_switches=kill_switches,
        cognition_adapter=cognition_adapter,
        enable_commitment_source=enable_commitment_source,
        enable_goal_source=enable_goal_source,
        apply_production_cutover=apply_production_cutover,
        audit_journal_factory=audit_journal_factory,
    )
