"""CT0-10 Phase G -- canonical expression adapter (order sections 50-56).

WHAT THIS IS
------------
`CanonicalExpressionAdapter` implements `canonical_spine.CanonicalExpressionPort`. It
answers only HOW to say something, for a Decision that a canonical owner already made, and
it freezes the resulting text immediately as the frozen CT0-5 `ContactMessageDraft` (with
`decision_ref` / `intent_ref` / `recipient_ref` / `channel_scope` / grounding refs), so
`contact_draft_model.build_frozen_draft` and
`contact_draft_projection.validate_frozen_draft_projection` keep validating it unchanged.

IT IS AN ADAPTER, NEVER AN EXPRESSION AUTHORITY (order 52)
---------------------------------------------------------
`decide`, `select_contact`, `submit`, `send`, `authorize`, `rewrite_decision` and
`create_action` exist only to RAISE `ExpressionCapabilityForbiddenError` and to be counted.
The adapter holds no transport, no gateway handle and no decision store; it cannot change a
Decision, and it is structurally unable to act on one.

HONEST STATUS OF THE REAL EXPRESSION SURFACE (verified, not assumed)
-------------------------------------------------------------------
The real expression surface is `agent/expression_contract.py` (176 lines, three
byte-identical copies in the recon) with its single call site
`gateway/run_turn_runner.py:1556 _combined_ephemeral_prompt` -> `:1573-1578
contract_for_gateway(ctx.user_config)`, consumed at `:1632`. It is PROMPT INJECTION ONLY:
it renders a labelled runtime-context block around the canonical skill body
(`CURRENT_EXPRESSION_CONTRACT` ... `END_CURRENT_EXPRESSION_CONTRACT`), it produces no
message text, it is DISABLED BY DEFAULT (`HERMES_EXPRESSION_CONTRACT` env flag /
`expression.contract.enabled` config key), and real text needs the live model
(`cline-pass/deepseek-v4.1-flash`) through the gateway.

`agent/expression_contract.py` is NOT INSTALLED on this machine: a recursive filename
search over the legacy development trees and the local hermes install directories
returns ZERO hits. No read-only capture ships with this release (the original capture
candidates were local developer paths and were REDACTED for the open-source export),
so this module binds NO contract version/hash. It keeps the fail-closed path:

  * the LIVE MODEL IS NOT WIRED, and a real model evaluation is OUT OF SCOPE here;
  * the qualification runs use `DeterministicGroundedRenderer`, a deterministic
    expression-only renderer, so crash/replay behaviour stays reproducible (order 115);
  * the adapter still exposes the real contract binding fields (contract version,
    contract sha256, contract path, availability, default-enabled state) and never claims
    the surface is wired when it is not.

ORDER 55: on a renderer failure there is NO ACTION, and the Decision is NOT rewritten to
NO_ACTION. The adapter records the failure, returns `action_semaphore="NO_ACTION"` meaning
"nothing downstream may act", and leaves the Decision object byte-identical (it re-checks
that before returning).

ORDER 56: replay reads the frozen draft (revalidated through the frozen projection) and
NEVER calls the renderer. The renderer call counter is exposed so a replay's zero
additional calls is provable.
"""

from __future__ import annotations

import contextlib
import importlib.util
import json
import os
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

try:  # when <ct0-10>/src/ct0_10 is on sys.path
    from canonical_spine import CanonicalExpressionPort, CanonicalIntegrationError, payload_hash
except ImportError:  # pragma: no cover - when <ct0-10>/src is on sys.path
    from ct0_10.canonical_spine import CanonicalExpressionPort, CanonicalIntegrationError, payload_hash

# The frozen CT0-5 / CT0-9 contact contracts (read-only imports).
from contact_draft_model import (
    CONTACT_DRAFT_RENDER_SCHEMA,
    DRAFT_POLICY_VERSION,
    ContactDecisionReceipt,
    ContactDraftRender,
    ContactGroundingSource,
    ContactIntent,
    ContactMessageDraft,
    DECISION_CONTACT_SELECTED,
    GroundedExcerpt,
    InvalidContactDraftError,
    build_frozen_draft,
    sha256_text,
)
from contact_draft_projection import validate_frozen_draft_projection


MODULE_ID = "ct0_10.canonical_ports.expression_adapter"
ORDER_SECTIONS = "50-56"
ADAPTER_VERSION = "chiyo.ct0_10.expression_adapter.v1"

STATUS_DRAFT_FROZEN = "DRAFT_FROZEN"
STATUS_EXPRESSION_FAILED = "EXPRESSION_FAILED"
STATUS_DRAFT_REPLAYED = "DRAFT_REPLAYED"
STATUS_FAILURE_REPLAYED = "DRAFT_FAILURE_REPLAYED"
STATUS_NO_DRAFT = "NO_DRAFT"

ACTION_SEMAPHORE_NONE = "NONE"
ACTION_SEMAPHORE_NO_ACTION = "NO_ACTION"

# ==========================================================================
# 1. the real expression surface: what it is, and what is available here
# ==========================================================================
EXPRESSION_AUTHORITY_OWNER = (
    "canonical owner = agent/expression_contract.py (a prompt-injection contract) plus the "
    "gateway model renderer; this adapter owns NEITHER and adds no fourth authority"
)
REAL_EXPRESSION_CONTRACT_MODULE = "agent/expression_contract.py"
REAL_EXPRESSION_CONTRACT_CALL_SITE = (
    "gateway/run_turn_runner.py:1556 _combined_ephemeral_prompt -> :1573-1578 "
    "contract_for_gateway(ctx.user_config), consumed :1632"
)
REAL_EXPRESSION_CONTRACT_FLAGS = (
    "HERMES_EXPRESSION_CONTRACT",
    "HERMES_EXPRESSION_CONTRACT_SKILL",
    "HERMES_EXPRESSION_CONTRACT_SKILLS_DIR",
    "expression.contract.enabled",
)
REAL_MODEL_NOT_WIRED = (
    "the live expression model (cline-pass/deepseek-v4.1-flash) behind the gateway is NOT "
    "wired in CT0-10; real model evaluation is out of scope. Qualification runs use "
    "DeterministicGroundedRenderer so crash/replay evidence stays reproducible (order 115)."
)
REAL_SURFACE_IS_PROMPT_ONLY = (
    "agent/expression_contract.py is prompt injection only: contract_for_gateway() returns a "
    "labelled runtime-context block around the canonical skill body and renders no message "
    "text at all (expression_contract.py:1-20, :123-136, :155-172)"
)

REAL_EXPRESSION_CONTRACT_INSTALLED_CANDIDATES = (
    "<redacted: hermes install>/agent/expression_contract.py",
    "<redacted: local developer hermes checkout>/agent/expression_contract.py",
    "<redacted: local developer hermes checkout>/agent/expression_contract.py",
    "<redacted: local developer hermes install>/agent/expression_contract.py",
)
# No read-only capture of the real contract ships with this release. The
# original candidate list pointed at local developer capture directories and
# was REDACTED for the open-source export; the adapter therefore reports
# NOT_LOCALLY_AVAILABLE (it never invents a contract).
REAL_EXPRESSION_CONTRACT_CAPTURE_CANDIDATES: tuple[str, ...] = ()
REAL_EXPRESSION_SKILLS_DIR_CANDIDATES: tuple[str, ...] = ()

DEFAULT_CONTRACT_SKILL = "ruirui-chat"
CONTRACT_VERSION_UNBOUND = "UNBOUND:real_expression_contract_not_locally_available"

EXPRESSION_LOCAL_AVAILABILITY = {
    "real_expression_contract_module": REAL_EXPRESSION_CONTRACT_MODULE,
    "installed_locally": False,
    "classification": "ADAPTER_ONLY / NOT_LOCALLY_AVAILABLE",
    "verified_by": (
        "recursive filename search for expression_contract.py over the legacy "
        "development trees (runtime source, deliverables, migrated Windows trees and "
        "the local hermes install dirs) -> 0 hits; the only prior-session read-only "
        "captures lived under a local developer scratch directory and were REDACTED "
        "for the open-source export, so this release binds no capture"
    ),
    "live_model": "cline-pass/deepseek-v4.1-flash via the gateway (not wired, out of scope)",
    "recorded_recon": (
        "CT0_10_CANONICAL_PORT_MAP.json ports[P11_EXPRESSION_DRAFT] and "
        "CT0_10_EXPRESSION_TELEGRAM_RECON.json found[G2_EXPRESSION]"
    ),
}


# ==========================================================================
# 2. errors
# ==========================================================================
class ExpressionAdapterError(CanonicalIntegrationError):
    """Base error for CT0-10 expression adapter failures."""


class ExpressionCapabilityForbiddenError(ExpressionAdapterError):
    """Expression was asked to decide, authorize, submit or send. Always refused."""


class ExpressionRendererUnavailableError(ExpressionAdapterError):
    """No renderer is wired. The adapter never falls back to a template."""


class ExpressionBindingError(ExpressionAdapterError):
    """The expression request binding does not match its owners."""


class DeterministicRenderFailure(ExpressionAdapterError):
    """Deterministic, injectable expression failure (qualification fixture)."""


# ==========================================================================
# 3. binding + outcome records
# ==========================================================================
def grounding_refs_of(grounding_sources: Sequence[ContactGroundingSource]) -> tuple[str, ...]:
    return tuple(source.source_ref for source in grounding_sources)


def grounding_hash_of(grounding_sources: Sequence[ContactGroundingSource]) -> str:
    """One hash over the whole grounded input set (never a per-item digest ladder)."""
    return payload_hash(
        {
            "grounding": [
                {"source_ref": source.source_ref, "content_hash": source.content_hash}
                for source in sorted(grounding_sources, key=lambda item: item.source_ref)
            ]
        }
    )


@dataclass(frozen=True, slots=True)
class ExpressionContractBinding:
    """Order 53: the request must be bound to all of these before anything renders."""

    decision_ref: str
    intent_ref: str
    recipient_ref: str
    grounding_hash: str
    grounding_refs: tuple[str, ...]
    expression_contract_version: str
    expression_contract_sha256: str | None
    expression_contract_path: str | None
    expression_contract_availability: str
    expression_contract_enabled_by_default: bool | None
    adapter_version: str = ADAPTER_VERSION
    live_model_wired: bool = False

    def to_mapping(self) -> dict[str, Any]:
        return {
            "decision_ref": self.decision_ref,
            "intent_ref": self.intent_ref,
            "recipient_ref": self.recipient_ref,
            "grounding_hash": self.grounding_hash,
            "grounding_refs": list(self.grounding_refs),
            "expression_contract_version": self.expression_contract_version,
            "expression_contract_sha256": self.expression_contract_sha256,
            "expression_contract_path": self.expression_contract_path,
            "expression_contract_availability": self.expression_contract_availability,
            "expression_contract_enabled_by_default": self.expression_contract_enabled_by_default,
            "adapter_version": self.adapter_version,
            "live_model_wired": self.live_model_wired,
        }


@dataclass(frozen=True, slots=True)
class ExpressionRenderOutcome:
    """What Expression produced, and explicitly what it did NOT do."""

    status: str
    binding: ExpressionContractBinding
    draft: ContactMessageDraft | None
    failure_code: str | None
    action_semaphore: str
    no_downstream_action: bool
    decision_rewritten: bool
    decision_outcome: str
    renderer_calls: int
    replayed: bool

    def to_mapping(self) -> dict[str, Any]:
        draft = self.draft
        return {
            "status": self.status,
            "binding": self.binding.to_mapping(),
            "draft": None
            if draft is None
            else {
                "draft_id": draft.draft_id,
                "content": draft.content,
                "content_hash": draft.content_hash,
                "decision_ref": draft.decision_ref,
                "intent_ref": draft.intent_ref,
                "recipient_ref": draft.recipient_ref,
                "channel_scope": list(draft.channel_scope),
                "source_refs": list(draft.source_refs),
                "idempotency_key": draft.idempotency_key,
                "created_at": draft.created_at,
                "policy_version": draft.policy_version,
                "content_chars": len(draft.content),
            },
            "failure_code": self.failure_code,
            "action_semaphore": self.action_semaphore,
            "no_downstream_action": self.no_downstream_action,
            "decision_rewritten": self.decision_rewritten,
            "decision_outcome": self.decision_outcome,
            "renderer_calls": self.renderer_calls,
            "replayed": self.replayed,
        }


# ==========================================================================
# 4. deterministic qualification renderer (expression capability only)
# ==========================================================================
MAX_EXCERPT_CHARS = 3990  # frozen GroundedExcerpt bound is 4000; keep a margin for joins
MAX_DRAFT_CONTENT_CHARS = 12000  # frozen ContactMessageDraft bound


class DeterministicGroundedRenderer:
    """Expression-only, deterministic renderer for the CT0-10 qualification runs.

    It can only quote text that is already present verbatim in the supplied grounding
    sources: one continuous excerpt per source, ordered by source_ref. It cannot decide,
    authorize, submit or send -- every such attempt is counted and raises.
    """

    renderer_kind = "DETERMINISTIC_QUALIFICATION_RENDERER"

    def __init__(self, *, mode: str = "verbatim") -> None:
        if mode not in ("verbatim", "fail"):
            raise ExpressionRendererUnavailableError("mode must be 'verbatim' or 'fail'")
        self.mode = mode
        self.calls = 0
        self.renders: list[dict[str, Any]] = []
        self.ownership_violations: list[str] = []
        self.submissions = 0
        self.decisions_made = 0

    # -- forbidden capabilities -------------------------------------------
    def _forbidden(self, capability: str) -> None:
        self.ownership_violations.append(capability)
        raise ExpressionCapabilityForbiddenError(
            f"expression renderer has no {capability} authority; refused"
        )

    def decide(self, *_args: Any, **_kwargs: Any) -> Any:
        self.decisions_made += 1
        self._forbidden("decide")

    def select_contact(self, *_args: Any, **_kwargs: Any) -> Any:
        self.decisions_made += 1
        self._forbidden("select_contact")

    def rewrite_decision(self, *_args: Any, **_kwargs: Any) -> Any:
        self.decisions_made += 1
        self._forbidden("rewrite_decision")

    def submit(self, *_args: Any, **_kwargs: Any) -> Any:
        self.submissions += 1
        self._forbidden("submit")

    def send(self, *_args: Any, **_kwargs: Any) -> Any:
        self.submissions += 1
        self._forbidden("send")

    # -- the one legitimate capability ------------------------------------
    @staticmethod
    def _window(text: str) -> str:
        window = text[:MAX_EXCERPT_CHARS].strip()
        if not window:
            raise DeterministicRenderFailure("grounding source is empty after trimming")
        return window

    def render(
        self,
        *,
        intent: Any,
        decision: Any,
        grounding_sources: Sequence[ContactGroundingSource],
    ) -> ContactDraftRender:
        self.calls += 1
        if not isinstance(decision, ContactDecisionReceipt):
            raise DeterministicRenderFailure("expression requires a typed Decision receipt")
        if not decision.committed or decision.outcome != DECISION_CONTACT_SELECTED:
            raise DeterministicRenderFailure(
                "expression may not act on a non-committed / non-CONTACT_SELECTED Decision"
            )
        if decision.intent_ref != intent.intent_id:
            raise DeterministicRenderFailure("the Decision is not bound to this Intent")
        if self.mode == "fail":
            raise DeterministicRenderFailure(
                "deterministic expression failure fixture (order 115 reproducibility)"
            )
        sources = tuple(grounding_sources)
        if not sources:
            raise DeterministicRenderFailure("expression requires grounded inputs")
        excerpts: list[GroundedExcerpt] = []
        for source in sorted(sources, key=lambda item: item.source_ref):
            if not isinstance(source, ContactGroundingSource):
                raise DeterministicRenderFailure("expression requires frozen grounding sources")
            window = self._window(source.canonical_text)
            if window not in source.canonical_text:
                raise DeterministicRenderFailure(
                    "renderer produced text that is not verbatim in its cited source"
                )
            excerpts.append(GroundedExcerpt(source_ref=source.source_ref, exact_text=window))
        render = ContactDraftRender(
            schema_version=CONTACT_DRAFT_RENDER_SCHEMA, excerpts=tuple(excerpts)
        )
        content = " ".join(excerpt.exact_text for excerpt in render.excerpts)
        if len(content) > MAX_DRAFT_CONTENT_CHARS:
            raise DeterministicRenderFailure(
                f"rendered content of {len(content)} chars exceeds the frozen Draft bound"
            )
        self.renders.append(
            {
                "intent_ref": intent.intent_id,
                "decision_ref": decision.decision_ref,
                "source_refs": [excerpt.source_ref for excerpt in render.excerpts],
                "content_hash": sha256_text(content),
                "call_index": self.calls,
            }
        )
        return render


# ==========================================================================
# 5. the adapter
# ==========================================================================
@contextlib.contextmanager
def _temporary_env(name: str, value: str | None):
    previous = os.environ.get(name)
    if value is None:
        os.environ.pop(name, None)
    else:
        os.environ[name] = value
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = previous


def first_existing(candidates: Sequence[str | Path]) -> Path | None:
    for candidate in candidates:
        try:
            path = Path(candidate)
        except (TypeError, ValueError):  # pragma: no cover - defensive
            continue
        if path.exists():
            return path
    return None


def load_module_from_file(module_name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(module_name, str(path))
    if spec is None or spec.loader is None:  # pragma: no cover - defensive
        raise ExpressionRendererUnavailableError(f"cannot load {module_name} from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


class CanonicalExpressionAdapter:
    """Narrow expression port: HOW to say it, for an already-committed Decision."""

    def __init__(
        self,
        *,
        renderer: Any | None = None,
        contract_module_path: str | Path | None = None,
        contract_skills_dir: str | Path | None = None,
        contract_skill_name: str = DEFAULT_CONTRACT_SKILL,
        policy_version: str = DRAFT_POLICY_VERSION,
    ) -> None:
        self.renderer = renderer
        self.contract_module_path = (
            Path(contract_module_path) if contract_module_path is not None else None
        )
        self.contract_skills_dir = (
            Path(contract_skills_dir) if contract_skills_dir is not None else None
        )
        self.contract_skill_name = contract_skill_name
        self.policy_version = policy_version
        self._outcomes: dict[tuple[str, str], ExpressionRenderOutcome] = {}
        self.forbidden_capability_attempts: list[str] = []
        self._contract_cache: dict[str, Any] | None = None

    # -- declaration -------------------------------------------------------
    @property
    def renderer_calls(self) -> int:
        return int(getattr(self.renderer, "calls", 0))

    @staticmethod
    def declaration() -> dict[str, Any]:
        return {
            "adapter_version": ADAPTER_VERSION,
            "order_sections": ORDER_SECTIONS,
            "authority_owner": EXPRESSION_AUTHORITY_OWNER,
            "capabilities": ["render (HOW to say it, for an already-committed Decision)"],
            "forbidden_capabilities": [
                "decide",
                "select_contact",
                "rewrite_decision",
                "authorize",
                "submit",
                "send",
                "create_action",
            ],
            "real_surface": {
                "module": REAL_EXPRESSION_CONTRACT_MODULE,
                "call_site": REAL_EXPRESSION_CONTRACT_CALL_SITE,
                "flags": list(REAL_EXPRESSION_CONTRACT_FLAGS),
                "prompt_only": True,
                "statement": REAL_SURFACE_IS_PROMPT_ONLY,
            },
            "live_model_statement": REAL_MODEL_NOT_WIRED,
            "local_availability": EXPRESSION_LOCAL_AVAILABILITY,
        }

    # -- forbidden capabilities (order 50) --------------------------------
    def _forbidden(self, capability: str) -> None:
        self.forbidden_capability_attempts.append(capability)
        raise ExpressionCapabilityForbiddenError(
            f"the CT0-10 expression adapter has no {capability} capability; it answers only "
            "HOW to say it for a Decision a canonical owner already made (order 50)"
        )

    def decide(self, *_args: Any, **_kwargs: Any) -> Any:
        self._forbidden("decide")

    def select_contact(self, *_args: Any, **_kwargs: Any) -> Any:
        self._forbidden("select_contact")

    def rewrite_decision(self, *_args: Any, **_kwargs: Any) -> Any:
        self._forbidden("rewrite_decision")

    def authorize(self, *_args: Any, **_kwargs: Any) -> Any:
        self._forbidden("authorize")

    def submit(self, *_args: Any, **_kwargs: Any) -> Any:
        self._forbidden("submit")

    def send(self, *_args: Any, **_kwargs: Any) -> Any:
        self._forbidden("send")

    def create_action(self, *_args: Any, **_kwargs: Any) -> Any:
        self._forbidden("create_action")

    # -- real contract binding --------------------------------------------
    def _real_contract_module(self) -> tuple[Any, str, str]:
        if self.contract_module_path is not None:
            path = Path(self.contract_module_path)
            if not path.is_file():
                raise ExpressionRendererUnavailableError(
                    f"configured expression contract module not found at {path}"
                )
            return (
                load_module_from_file("ct0_10_real_expression_contract", path),
                "CAPTURE_NOT_INSTALLED",
                str(path),
            )
        installed = first_existing(REAL_EXPRESSION_CONTRACT_INSTALLED_CANDIDATES)
        if installed is not None:
            return (
                load_module_from_file("ct0_10_real_expression_contract", installed),
                "INSTALLED",
                str(installed),
            )
        capture = first_existing(REAL_EXPRESSION_CONTRACT_CAPTURE_CANDIDATES)
        if capture is not None:
            return (
                load_module_from_file("ct0_10_real_expression_contract", capture),
                "CAPTURE_NOT_INSTALLED",
                str(capture),
            )
        raise ExpressionRendererUnavailableError(
            "agent/expression_contract.py is not installed on this machine and no read-only "
            "capture is available; the expression contract binding is UNBOUND"
        )

    def real_expression_contract_binding(self) -> dict[str, Any]:
        """Read the REAL contract surface (never a message) and record its status.

        Returns the version/sha256/path/availability/enabled-by-default plus the rendered
        prompt block length when the contract is switched on. It never produces message
        text: the real surface is prompt injection only.
        """
        if self._contract_cache is not None:
            return self._contract_cache
        skills_dir = (
            Path(self.contract_skills_dir)
            if self.contract_skills_dir is not None
            else first_existing(REAL_EXPRESSION_SKILLS_DIR_CANDIDATES)
        )
        try:
            module, availability, module_path = self._real_contract_module()
        except ExpressionAdapterError as exc:
            record = {
                "available": False,
                "availability": "NOT_LOCALLY_AVAILABLE",
                "module": REAL_EXPRESSION_CONTRACT_MODULE,
                "error": str(exc),
                "contract_version": CONTRACT_VERSION_UNBOUND,
                "contract_sha256": None,
                "skill_path": None,
                "enabled_by_default": None,
                "prompt_only": True,
                "statement": REAL_SURFACE_IS_PROMPT_ONLY,
                "live_model_statement": REAL_MODEL_NOT_WIRED,
                "module_sha256": None,
            }
            self._contract_cache = record
            return record

        with _temporary_env("HERMES_EXPRESSION_CONTRACT", None):
            enabled_by_default = bool(module.contract_enabled({}))
        contract: Mapping[str, Any] | None = None
        block = ""
        if skills_dir is not None:
            with _temporary_env("HERMES_EXPRESSION_CONTRACT_SKILLS_DIR", str(skills_dir)):
                contract = module.read_contract(self.contract_skill_name)
                block = module.contract_for_gateway(
                    {"expression": {"contract": {"enabled": True}}}
                )
        record = {
            "available": True,
            "availability": availability,
            "module": REAL_EXPRESSION_CONTRACT_MODULE,
            "module_path": module_path,
            "module_sha256": sha256_text(Path(module_path).read_text(encoding="utf-8")),
            "block_open": getattr(module, "BLOCK_OPEN", None),
            "block_close": getattr(module, "BLOCK_CLOSE", None),
            "default_skill": getattr(module, "DEFAULT_SKILL", None),
            "enabled_by_default": enabled_by_default,
            "contract_version": (contract or {}).get("version") or CONTRACT_VERSION_UNBOUND,
            "contract_sha256": (contract or {}).get("sha256"),
            "skill_path": (contract or {}).get("path"),
            "skill_bytes": (contract or {}).get("bytes"),
            "rendered_block_chars": len(block),
            "prompt_only": True,
            "renders_message_text": False,
            "statement": REAL_SURFACE_IS_PROMPT_ONLY,
            "live_model_statement": REAL_MODEL_NOT_WIRED,
            "live_model_wired": False,
            "skills_dir": None if skills_dir is None else str(skills_dir),
        }
        self._contract_cache = record
        return record

    def contract_version_string(self) -> tuple[str, str | None]:
        record = self.real_expression_contract_binding()
        version = str(record.get("contract_version") or CONTRACT_VERSION_UNBOUND)
        digest = record.get("contract_sha256")
        if digest:
            return f"{record.get('default_skill') or self.contract_skill_name}@{version}+sha256:{str(digest)[:16]}", str(digest)
        return f"{ADAPTER_VERSION}:{CONTRACT_VERSION_UNBOUND}", None

    # -- binding -----------------------------------------------------------
    def binding_for(
        self,
        *,
        intent: ContactIntent,
        decision: ContactDecisionReceipt,
        grounding_sources: Sequence[ContactGroundingSource],
    ) -> ExpressionContractBinding:
        sources = tuple(grounding_sources)
        if not sources or any(not isinstance(source, ContactGroundingSource) for source in sources):
            raise ExpressionBindingError(
                "grounding_sources must be a non-empty tuple of frozen grounding sources"
            )
        if not isinstance(intent, ContactIntent) or not isinstance(decision, ContactDecisionReceipt):
            raise ExpressionBindingError("a typed ContactIntent and Decision receipt are required")
        if decision.intent_ref != intent.intent_id or decision.candidate_ref != intent.candidate_ref:
            raise ExpressionBindingError("the Decision receipt does not belong to this Intent")
        version, contract_sha = self.contract_version_string()
        record = self.real_expression_contract_binding()
        return ExpressionContractBinding(
            decision_ref=decision.decision_ref,
            intent_ref=intent.intent_id,
            recipient_ref=intent.recipient_ref,
            grounding_hash=grounding_hash_of(sources),
            grounding_refs=grounding_refs_of(sources),
            expression_contract_version=version,
            expression_contract_sha256=contract_sha,
            expression_contract_path=record.get("skill_path") or record.get("module_path"),
            expression_contract_availability=str(record.get("availability")),
            expression_contract_enabled_by_default=record.get("enabled_by_default"),
            live_model_wired=False,
        )

    def _check_binding(
        self, binding: ExpressionContractBinding, expected: ExpressionContractBinding
    ) -> None:
        for field in (
            "decision_ref",
            "intent_ref",
            "recipient_ref",
            "grounding_hash",
            "grounding_refs",
            "expression_contract_version",
        ):
            if getattr(binding, field) != getattr(expected, field):
                raise ExpressionBindingError(
                    f"supplied binding does not match its owners: {field} differs"
                )

    # -- render ------------------------------------------------------------
    def render(
        self,
        *,
        intent: Any,
        decision: Any,
        grounding_sources: Sequence[ContactGroundingSource],
        binding: ExpressionContractBinding | None = None,
    ) -> ExpressionRenderOutcome:
        if self.renderer is None:
            raise ExpressionRendererUnavailableError(
                "no renderer is wired; the adapter never substitutes a template"
            )
        if not isinstance(intent, ContactIntent) or not isinstance(decision, ContactDecisionReceipt):
            raise ExpressionBindingError("a typed ContactIntent and Decision receipt are required")
        sources = tuple(grounding_sources)
        expected = self.binding_for(intent=intent, decision=decision, grounding_sources=sources)
        if binding is not None:
            self._check_binding(binding, expected)
        decision_before = json.dumps(asdict(decision), sort_keys=True, separators=(",", ":"))

        key = (intent.intent_id, decision.decision_ref)
        prior = self._outcomes.get(key)
        if prior is not None:
            # Order 56: a repeated render for the same owners reads the stored outcome and
            # NEVER calls the renderer again.
            return self._replay_outcome(
                prior=prior, intent=intent, decision=decision, grounding_sources=sources
            )

        if not decision.committed or decision.outcome != DECISION_CONTACT_SELECTED:
            outcome = ExpressionRenderOutcome(
                status=STATUS_NO_DRAFT,
                binding=expected,
                draft=None,
                failure_code="DECISION_NOT_A_COMMITTED_SELECTION",
                action_semaphore=ACTION_SEMAPHORE_NO_ACTION,
                no_downstream_action=True,
                decision_rewritten=False,
                decision_outcome=decision.outcome,
                renderer_calls=self.renderer_calls,
                replayed=False,
            )
            self._outcomes[key] = outcome
            return outcome

        try:
            rendered = self.renderer.render(
                intent=intent, decision=decision, grounding_sources=sources
            )
            draft = build_frozen_draft(
                intent=intent,
                decision=decision,
                grounding_sources=sources,
                render=rendered,
                created_at=intent.updated_at,
                policy_version=self.policy_version,
            )
        except Exception as exc:  # order 55: NO ACTION, and the Decision is untouched
            code = f"RENDER_OR_FREEZE_FAILED:{type(exc).__name__}"
            outcome = ExpressionRenderOutcome(
                status=STATUS_EXPRESSION_FAILED,
                binding=expected,
                draft=None,
                failure_code=code,
                action_semaphore=ACTION_SEMAPHORE_NO_ACTION,
                no_downstream_action=True,
                decision_rewritten=False,
                decision_outcome=decision.outcome,
                renderer_calls=self.renderer_calls,
                replayed=False,
            )
            self._outcomes[key] = outcome
            if json.dumps(asdict(decision), sort_keys=True, separators=(",", ":")) != decision_before:
                raise ExpressionAdapterError(
                    "expression failure path modified the Decision; order 55 forbids it"
                )
            return outcome

        outcome = ExpressionRenderOutcome(
            status=STATUS_DRAFT_FROZEN,
            binding=expected,
            draft=draft,
            failure_code=None,
            action_semaphore=ACTION_SEMAPHORE_NONE,
            no_downstream_action=False,
            decision_rewritten=False,
            decision_outcome=decision.outcome,
            renderer_calls=self.renderer_calls,
            replayed=False,
        )
        self._outcomes[key] = outcome
        if json.dumps(asdict(decision), sort_keys=True, separators=(",", ":")) != decision_before:
            raise ExpressionAdapterError(
                "expression modified the Decision; expression has no decision authority"
            )
        return outcome

    # -- replay (order 56) -------------------------------------------------
    def replay(
        self,
        *,
        intent: Any,
        decision: Any,
        grounding_sources: Sequence[ContactGroundingSource],
    ) -> ExpressionRenderOutcome | None:
        if not isinstance(intent, ContactIntent) or not isinstance(decision, ContactDecisionReceipt):
            raise ExpressionBindingError("a typed ContactIntent and Decision receipt are required")
        prior = self._outcomes.get((intent.intent_id, decision.decision_ref))
        if prior is None:
            return None
        return self._replay_outcome(
            prior=prior,
            intent=intent,
            decision=decision,
            grounding_sources=tuple(grounding_sources),
        )

    def _replay_outcome(
        self,
        *,
        prior: ExpressionRenderOutcome,
        intent: ContactIntent,
        decision: ContactDecisionReceipt,
        grounding_sources: Sequence[ContactGroundingSource],
    ) -> ExpressionRenderOutcome:
        """Read the stored outcome. The renderer is never called here."""
        if prior.draft is None:
            return ExpressionRenderOutcome(
                status=STATUS_FAILURE_REPLAYED
                if prior.status == STATUS_EXPRESSION_FAILED
                else STATUS_NO_DRAFT,
                binding=prior.binding,
                draft=None,
                failure_code=prior.failure_code,
                action_semaphore=ACTION_SEMAPHORE_NO_ACTION,
                no_downstream_action=True,
                decision_rewritten=False,
                decision_outcome=decision.outcome,
                renderer_calls=self.renderer_calls,
                replayed=True,
            )
        try:
            frozen = validate_frozen_draft_projection(
                intent=intent,
                decision=decision,
                draft=prior.draft,
                grounding_sources=tuple(grounding_sources),
            )
        except Exception as exc:
            raise ExpressionAdapterError(
                f"stored draft failed frozen-projection revalidation on replay: {exc}"
            ) from exc
        return ExpressionRenderOutcome(
            status=STATUS_DRAFT_REPLAYED,
            binding=prior.binding,
            draft=frozen,
            failure_code=None,
            action_semaphore=ACTION_SEMAPHORE_NONE,
            no_downstream_action=False,
            decision_rewritten=False,
            decision_outcome=decision.outcome,
            renderer_calls=self.renderer_calls,
            replayed=True,
        )

    # -- introspection -----------------------------------------------------
    def stored_outcome_count(self) -> int:
        return len(self._outcomes)


__all__ = [
    "ACTION_SEMAPHORE_NO_ACTION",
    "ACTION_SEMAPHORE_NONE",
    "ADAPTER_VERSION",
    "CanonicalExpressionAdapter",
    "DEFAULT_CONTRACT_SKILL",
    "DeterministicGroundedRenderer",
    "DeterministicRenderFailure",
    "EXPRESSION_AUTHORITY_OWNER",
    "EXPRESSION_LOCAL_AVAILABILITY",
    "ExpressionAdapterError",
    "ExpressionBindingError",
    "ExpressionCapabilityForbiddenError",
    "ExpressionContractBinding",
    "ExpressionRenderOutcome",
    "ExpressionRendererUnavailableError",
    "MODULE_ID",
    "ORDER_SECTIONS",
    "REAL_EXPRESSION_CONTRACT_MODULE",
    "REAL_EXPRESSION_CONTRACT_CAPTURE_CANDIDATES",
    "REAL_MODEL_NOT_WIRED",
    "REAL_SURFACE_IS_PROMPT_ONLY",
    "STATUS_DRAFT_FROZEN",
    "STATUS_DRAFT_REPLAYED",
    "STATUS_EXPRESSION_FAILED",
    "STATUS_FAILURE_REPLAYED",
    "STATUS_NO_DRAFT",
    "grounding_hash_of",
    "grounding_refs_of",
]
