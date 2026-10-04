#!/usr/bin/env python3
"""Chiyo Life Runtime | LR-2 Activity Continuity Core (with LR-3 Preflight WAL Atomicity).

Implements the frozen LR-1 Authority + Contract invariants for canonical
Activity continuity, upgraded in LR-3 Preflight with a WAL Commit Intent +
Authoritative Journal Projection Rebuild protocol so that a crash at ANY commit
step recovers deterministically to either the previous committed state or the
new committed state (never a half-committed truth).
"""

from __future__ import annotations

import copy
import errno
import fcntl
import hashlib
import hmac
import json
import os
import re
import tempfile
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Optional, Sequence

from activity_admission import (
    AUTHORITY_AGENCY,
    AUTHORITY_CONSEQUENCE,
    ActivityCommitReceipt,
    AdmissionIssuer,
    AdmissionRejected,
)

from activity_compat import (
    BJT,
    ActivityError,
    ActivityLockError,
    ActivityTransitionError,
    CompletionEvidenceError,
    _compact_value,
    _iso,
    _parse,
    now,
)

# ---------------------------------------------------------------------------
# Frozen Constants & Schemas (LR-1 / LR-2 / LR-3 Preflight)
# ---------------------------------------------------------------------------

GLOBAL_SUBJECT_ID = "chiyo.global"
ACCEPTED_SUBJECT_ALIASES = frozenset({"chiyo.global", "chiyo", "Chiyo"})

SCHEMA_CANONICAL_LIFE_STATE = "chiyo.life.canonical_state.v1"
SCHEMA_ACTIVITY_INSTANCE = "chiyo.life.activity.v1"
SCHEMA_LIFE_TRANSITION = "chiyo.life.transition.v1"
SCHEMA_LIFE_FRAME_VIEW = "chiyo.life.frame.read_view.v1"
SCHEMA_LEGACY_MIGRATION_REPORT = "chiyo.life.legacy_v2_inspection.v1"
SCHEMA_AUTHORITY_MARKER = "chiyo.life.authority.v1"
SCHEMA_COMMIT_INTENT = "chiyo.life.commit_intent.v1"

LIFE_STATE_IDLE = "IDLE"
LIFE_STATE_ACTIVE = "ACTIVE"
LIFE_STATE_PAUSED = "PAUSED"
LIFE_STATE_WAITING = "WAITING"
LIFE_STATES = (
    LIFE_STATE_IDLE,
    LIFE_STATE_ACTIVE,
    LIFE_STATE_PAUSED,
    LIFE_STATE_WAITING,
)

STATUS_ACTIVE = "ACTIVE"
STATUS_PAUSED = "PAUSED"
STATUS_WAITING = "WAITING"
STATUS_COMPLETED = "COMPLETED"
STATUS_ABANDONED = "ABANDONED"
STATUS_CANCELLED = "CANCELLED"
STATUS_EXPIRED = "EXPIRED"

OPEN_ACTIVITY_STATUSES = (STATUS_ACTIVE, STATUS_PAUSED, STATUS_WAITING)
TERMINAL_ACTIVITY_STATUSES = (
    STATUS_COMPLETED,
    STATUS_ABANDONED,
    STATUS_CANCELLED,
    STATUS_EXPIRED,
)
ALL_ACTIVITY_STATUSES = OPEN_ACTIVITY_STATUSES + TERMINAL_ACTIVITY_STATUSES

TRANSITION_START = "START"
TRANSITION_PAUSE = "PAUSE"
TRANSITION_WAIT = "WAIT"
TRANSITION_RESUME = "RESUME"
TRANSITION_COMPLETE = "COMPLETE"
TRANSITION_ABANDON = "ABANDON"
TRANSITION_CANCEL = "CANCEL"
TRANSITION_EXPIRE = "EXPIRE"
TRANSITION_CHECKPOINT = "CHECKPOINT"
TRANSITION_PROGRESS = "PROGRESS"
TRANSITION_NOOP = "NOOP"

TRANSITION_TYPES = (
    TRANSITION_START,
    TRANSITION_PAUSE,
    TRANSITION_WAIT,
    TRANSITION_RESUME,
    TRANSITION_COMPLETE,
    TRANSITION_ABANDON,
    TRANSITION_CANCEL,
    TRANSITION_EXPIRE,
    TRANSITION_CHECKPOINT,
    TRANSITION_PROGRESS,
)

# Narrow Attention Occupancy (LR-3 Section 3)
INTERRUPTIBILITY_FREE = "FREE"
INTERRUPTIBILITY_LIGHT = "LIGHT"
INTERRUPTIBILITY_FOCUSED = "FOCUSED"
INTERRUPTIBILITY_ATOMIC = "ATOMIC"
INTERRUPTIBILITY_LEVELS = (
    INTERRUPTIBILITY_FREE,
    INTERRUPTIBILITY_LIGHT,
    INTERRUPTIBILITY_FOCUSED,
    INTERRUPTIBILITY_ATOMIC,
)

# Physical/Logical Interruptibility Mode (LR-3 Section 5)
INTERRUPT_MODE_IMMEDIATE = "IMMEDIATE"
INTERRUPT_MODE_SAFE_BOUNDARY = "SAFE_BOUNDARY"
INTERRUPT_MODE_ATOMIC = "ATOMIC"
INTERRUPT_MODES = (
    INTERRUPT_MODE_IMMEDIATE,
    INTERRUPT_MODE_SAFE_BOUNDARY,
    INTERRUPT_MODE_ATOMIC,
)

REASON_CODES = frozenset(
    {
        "ADOPTED_DECISION",
        "ADOPTED_COMMITMENT",
        "USER_INTERRUPTION",
        "VOLUNTARY_PAUSE",
        "SAFE_BOUNDARY_PAUSE",
        "EXTERNAL_DEPENDENCY_WAIT",
        "RESUMED_BY_DECISION",
        "CHECKPOINT_ADVANCED",
        "COMPLETED_WITH_EVIDENCE",
        "ABANDONED_BY_DECISION",
        "CANCELLED_BY_DECISION",
        "EXPIRED_BY_TIMEOUT",
        "SPONTANEOUS_RECONSIDERATION",
        "OPAQUE",
        "UNKNOWN",
    }
)

WRITER_DOMAIN_CANONICAL = "life_activity_authority"
FORBIDDEN_WRITER_DOMAINS = frozenset(
    {
        "world",
        "body",
        "memory",
        "expression",
        "grounding",
        "agenda",
        "life_agenda_authority",
        "resume_condition",
        "life_resume_condition_authority",
        "resume_eligibility",
        "life_resume_eligibility_authority",
        "life_progress_authority",
        "result_settlement_authority",
        "action_reality_authority",
        "cron",
        "perception",
        "telegram",
        "desktop",
        "qq",
        "web",
        "channel_adapter",
        "research",
        "replay",
        "shadow",
        "evaluator",
        "twin_chiyo",
        "migration_simulation",
        "recovery_rehearsal",
        "offline_experiment",
    }
)

NAMESPACE_CANONICAL = "canonical"
NAMESPACE_ISOLATED_TEST = "isolated_canonical_test"
NON_PRODUCTION_NAMESPACES = frozenset(
    {
        "shadow",
        "replay",
        "research",
        "evaluator",
        "twin_chiyo",
        "migration_simulation",
        "recovery_rehearsal",
        "offline_experiment",
    }
)

PRODUCTION_HOMES = (
    Path("./data/world").resolve(),
    Path("./data/persona").resolve(),
    # Retain denial-only compatibility guards; these paths are never read.
    Path("/root/.hermes-world-hk"),
    Path("/root/.hermes-stock"),
    Path("/root/.hermes"),
)
PRODUCTION_CUTOVER_ENV = "CHIYO_LIFE_PRODUCTION_CUTOVER"

LOCK_FILENAME = "life_activity_authority.lock"
MARKER_FILENAME = "life_authority.json"
STATE_FILENAME = "canonical_life_state.json"
JOURNAL_FILENAME = "life_transitions.jsonl"
COMMIT_INTENT_FILENAME = "commit_intent.json"
GENESIS_HASH = "0" * 64

_STRUCTURED_REF_RE = re.compile(r"^([a-z][a-z0-9_]{1,39}):([A-Za-z0-9][A-Za-z0-9_.:/-]{0,160})$")

FORBIDDEN_ADOPTION_PREFIXES = frozenset(
    {
        "world",
        "world_event",
        "body",
        "body_event",
        "memory",
        "memory_recall",
        "agenda",
        "agenda_due",
        "agnd",
        "rcond",
        "resume_cond",
        "relig",
        "resume_eligible",
        "eligibility",
        "wep",
        "cron",
        "timer",
        "tool",
        "tool_result",
        "npc",
        "npc_event",
        "sensor",
        "sensor_event",
        "perception",
        "perception_event",
        "channel",
        "telegram",
        "desktop",
        "llm_text",
    }
)
VALID_DECISION_PREFIXES = frozenset({"decision", "dec"})
VALID_ADOPTION_PREFIXES = frozenset({"adoption", "adp"})
VALID_EVIDENCE_PREFIXES = frozenset(
    {
        "action_receipt",
        "act_receipt",
        "artifact_rev",
        "art_rev",
        "artifact",
        "artifact_file",
        "file",
        "world_fact",
        "world",
        "tool_result",
        "tool",
        "game_state",
        "game",
        "doc_state",
        "document",
        "doc",
        "book",
        "external_result",
        "export",
        "evd_receipt",
        "evidence",
        "settlement",
        "rset",
        "srev",
        "settled_result",
        "aevid",
        "arcpt",
        "eclaim",
        "celig",
        "comp_elig",
        "completion_eligibility",
        "pevid",
        "progress_evidence",
    }
)
VALID_CHECKPOINT_PREFIXES = VALID_EVIDENCE_PREFIXES | frozenset(
    {
        "checkpoint_ext",
        "chk_ext",
        "checkpoint",
        "chk",
        "cursor",
        "page",
    }
)
VALID_ACTION_REF_PREFIXES = frozenset(
    {
        "action_receipt",
        "act_receipt",
        "world_exec",
        "tool_result",
        "action",
        "actn",
        "arcpt",
        "rset",
        "srev",
    }
)
# S7 Phase-F (order sections 12-13): LR-2 no longer keeps its own waiting-reference
# vocabulary. The waiting domain owns the schema (`waiting_ref_vocab`); this set is DERIVED
# from it, so a reference a real producer can emit is canonical for both layers -- or refused
# by both. This is what closes the `wait_dep:` hole: AG-2's own default wait reference was
# admissible as consequence evidence while this validator refused it as canonical state.
from waiting_ref_vocab import (  # noqa: E402
    VALID_WAITING_REF_PREFIXES as _CANONICAL_WAITING_REF_PREFIXES,
)

VALID_WAITING_REF_PREFIXES = _CANONICAL_WAITING_REF_PREFIXES

FORBIDDEN_SELF_TRUTH_KEYS = frozenset(
    {
        "goal",
        "progress",
        "checkpoint",
        "completed",
        "completion",
        "completion_criteria",
        "world_state",
        "body_state",
        "memory_text",
        "artifact_body",
    }
)


# ---------------------------------------------------------------------------
# Specific Exceptions
# ---------------------------------------------------------------------------


class ContractViolationError(ActivityError):
    """Raised when an input violates a frozen LR-1/LR-2/LR-3 contract rule."""


class SubjectIdentityError(ContractViolationError):
    """Raised when an operation attempts to partition or change One Chiyo identity."""


class WriterCapabilityError(ActivityError):
    """Raised when a caller lacks canonical Activity writer authority."""


class AdoptionProvenanceError(ActivityTransitionError):
    """Raised when START or RESUME lacks valid decision_ref or adoption_ref."""


class ForegroundConflictError(ActivityTransitionError):
    """Raised when START is attempted while a foreground activity already exists."""


class TerminalActivityImmutableError(ActivityTransitionError):
    """Raised when any lifecycle mutation targets a terminal activity."""


class RevisionConflictError(ActivityTransitionError):
    """Raised when expected_revision does not match the current canonical revision."""


class IdempotencyConflictError(ActivityTransitionError):
    """Raised when an idempotency_key is reused with different mutation parameters."""


class RecoveryRequiredError(ActivityError):
    """Raised when canonical state or journal is corrupt or inconsistent (Fail Closed)."""

    def __init__(self, code: str, message: str, *, diagnostic: Optional[dict[str, Any]] = None):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.diagnostic = diagnostic or {
            "error_code": code,
            "message": message,
            "repair_recommendation": "Inspect canonical_life_state.json and life_transitions.jsonl; do not auto-overwrite.",
        }


# ---------------------------------------------------------------------------
# Canonical Serialization & Digest Helpers
# ---------------------------------------------------------------------------


def canonical_json_line(value: Any) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ContractViolationError(f"value is not valid canonical JSON: {exc}") from exc


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def is_production_path(path: Path | str) -> bool:
    resolved = Path(path).expanduser().resolve()
    for prod in PRODUCTION_HOMES:
        prod_resolved = prod.resolve()
        if resolved == prod_resolved or prod_resolved in resolved.parents:
            return True
    return False


# ---------------------------------------------------------------------------
# Validation Helpers
# ---------------------------------------------------------------------------


def validate_subject_id(subject_id: Optional[str]) -> str:
    if subject_id is None:
        return GLOBAL_SUBJECT_ID
    if not isinstance(subject_id, str) or not subject_id.strip():
        raise SubjectIdentityError("subject_id must be a non-empty string")
    cleaned = subject_id.strip()
    if cleaned not in ACCEPTED_SUBJECT_ALIASES:
        raise SubjectIdentityError(
            f"One Chiyo violation: illegal subject_id={subject_id!r}; "
            f"canonical Life state belongs exclusively to {GLOBAL_SUBJECT_ID!r}"
        )
    return GLOBAL_SUBJECT_ID


def _validate_structured_ref(
    value: Any,
    field_name: str,
    *,
    allowed_prefixes: Optional[frozenset[str]] = None,
    forbidden_prefixes: Optional[frozenset[str]] = None,
    error_cls: type[ActivityError] = ContractViolationError,
) -> str:
    if not isinstance(value, str) or not value.strip():
        raise error_cls(f"{field_name} must be a non-empty structured reference string")
    text = value.strip()
    match = _STRUCTURED_REF_RE.fullmatch(text)
    if not match:
        raise error_cls(
            f"{field_name} must be a structured reference '<namespace>:<id>', got {value!r}"
        )
    prefix, ref_id = match.group(1), match.group(2)
    if not ref_id:
        raise error_cls(f"{field_name} reference id cannot be empty: {value!r}")
    if forbidden_prefixes and prefix in forbidden_prefixes:
        raise error_cls(
            f"{field_name} cannot use upstream/non-adoption prefix {prefix!r} ({value!r})"
        )
    if allowed_prefixes and prefix not in allowed_prefixes:
        raise error_cls(
            f"{field_name} prefix {prefix!r} is not in allowed prefixes {sorted(allowed_prefixes)}"
        )
    return text


def validate_adoption_provenance(
    *,
    decision_ref: Optional[str],
    adoption_ref: Optional[str],
    transition_type: str,
) -> tuple[Optional[str], Optional[str]]:
    if not decision_ref and not adoption_ref:
        raise AdoptionProvenanceError(
            f"{transition_type} requires at least one of decision_ref or adoption_ref"
        )
    norm_dec: Optional[str] = None
    norm_adp: Optional[str] = None
    if decision_ref is not None:
        norm_dec = _validate_structured_ref(
            decision_ref,
            "decision_ref",
            allowed_prefixes=VALID_DECISION_PREFIXES,
            forbidden_prefixes=FORBIDDEN_ADOPTION_PREFIXES,
            error_cls=AdoptionProvenanceError,
        )
    if adoption_ref is not None:
        norm_adp = _validate_structured_ref(
            adoption_ref,
            "adoption_ref",
            allowed_prefixes=VALID_ADOPTION_PREFIXES,
            forbidden_prefixes=FORBIDDEN_ADOPTION_PREFIXES,
            error_cls=AdoptionProvenanceError,
        )
    return norm_dec, norm_adp


def validate_completion_evidence_ref(completion_evidence_ref: Any) -> str:
    if completion_evidence_ref is None or completion_evidence_ref == "":
        raise CompletionEvidenceError(
            "COMPLETE requires a structured external completion_evidence_ref"
        )
    return _validate_structured_ref(
        completion_evidence_ref,
        "completion_evidence_ref",
        allowed_prefixes=VALID_EVIDENCE_PREFIXES,
        error_cls=CompletionEvidenceError,
    )


def validate_optional_external_refs(
    *,
    checkpoint_ref: Optional[str] = None,
    last_action_ref: Optional[str] = None,
    completion_evidence_ref: Optional[str] = None,
    waiting_on_ref: Optional[str] = None,
    resume_condition_ref: Optional[str] = None,
) -> dict[str, Optional[str]]:
    return {
        "checkpoint_ref": (
            _validate_structured_ref(
                checkpoint_ref,
                "checkpoint_ref",
                allowed_prefixes=VALID_CHECKPOINT_PREFIXES,
            )
            if checkpoint_ref is not None
            else None
        ),
        "last_action_ref": (
            _validate_structured_ref(
                last_action_ref,
                "last_action_ref",
                allowed_prefixes=VALID_ACTION_REF_PREFIXES,
            )
            if last_action_ref is not None
            else None
        ),
        "completion_evidence_ref": (
            validate_completion_evidence_ref(completion_evidence_ref)
            if completion_evidence_ref is not None
            else None
        ),
        "waiting_on_ref": (
            _validate_structured_ref(
                waiting_on_ref,
                "waiting_on_ref",
                allowed_prefixes=VALID_WAITING_REF_PREFIXES,
            )
            if waiting_on_ref is not None
            else None
        ),
        "resume_condition_ref": (
            _validate_structured_ref(
                resume_condition_ref,
                "resume_condition_ref",
                allowed_prefixes=VALID_WAITING_REF_PREFIXES,
            )
            if resume_condition_ref is not None
            else None
        ),
    }


def validate_no_forbidden_self_truth(payload: Optional[Mapping[str, Any]]) -> None:
    if not payload:
        return
    illegal = FORBIDDEN_SELF_TRUTH_KEYS.intersection(payload.keys())
    if illegal:
        raise ContractViolationError(
            f"forbidden self-asserted truth fields in Activity payload: {sorted(illegal)}"
        )


def normalize_ref_list(refs: Optional[Sequence[str]], field_name: str, *, allow_empty: bool = False) -> list[str]:
    if refs is None:
        if allow_empty:
            return []
        raise ContractViolationError(f"{field_name} cannot be None")
    if isinstance(refs, (str, bytes)) or not isinstance(refs, Sequence):
        raise ContractViolationError(f"{field_name} must be a sequence of structured reference strings")
    result: list[str] = []
    for item in refs:
        validated = _validate_structured_ref(item, field_name)
        if validated not in result:
            result.append(validated)
    if not allow_empty and not result:
        raise ContractViolationError(f"{field_name} must contain at least one structured reference")
    return result


def normalize_reason_code(reason_code: Optional[str], default: str = "UNKNOWN") -> str:
    code = (reason_code or default).strip().upper()
    if code not in REASON_CODES:
        raise ContractViolationError(
            f"invalid reason_code={reason_code!r}; allowed={sorted(REASON_CODES)}"
        )
    return code


# ---------------------------------------------------------------------------
# Authority Lease & Writer Capability Model (Sections 3, 23, 24, 25)
# ---------------------------------------------------------------------------


class ActivityAuthorityLease:
    """Kernel-enforced `flock` single-writer lease over a Life store root."""

    def __init__(
        self,
        store_root: Path | str,
        *,
        instance_id: Optional[str] = None,
        pid: Optional[int] = None,
        holder_pid: Optional[int] = None,
    ):
        self.store_root = Path(store_root).expanduser().resolve()
        self.run_dir = self.store_root / "run"
        self.lock_path = self.run_dir / LOCK_FILENAME
        self.marker_path = self.run_dir / MARKER_FILENAME
        eff_pid = pid if pid is not None else holder_pid
        self.pid = int(eff_pid if eff_pid is not None else os.getpid())
        self.instance_id = instance_id or f"life-authority:{self.pid}:{uuid.uuid4().hex[:8]}"
        self._handle: Optional[Any] = None
        self._secret = os.urandom(32)

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
                    raise ActivityLockError(f"life authority lock failed: {exc}") from exc
                if time.monotonic() >= deadline:
                    handle.close()
                    raise ActivityLockError(
                        f"another live instance holds life authority lock on {self.lock_path}"
                    ) from exc
                time.sleep(0.02)

        self._handle = handle
        marker = {
            "schema_version": SCHEMA_AUTHORITY_MARKER,
            "instance_id": self.instance_id,
            "pid": self.pid,
            "store_root": str(self.store_root),
            "acquired_at": _iso(now()),
            "marker_is_truth": False,
        }
        tmp = self.marker_path.with_name(f".{MARKER_FILENAME}.tmp.{uuid.uuid4().hex}")
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

    @contextmanager
    def held_lease(self, *, timeout_seconds: float = 5.0) -> Iterator["ActivityAuthorityLease"]:
        self.acquire(timeout_seconds=timeout_seconds)
        try:
            yield self
        finally:
            self.release()

    def sign_capability(self, writer_domain: str, namespace: str) -> str:
        msg = f"{self.instance_id}|{self.store_root}|{writer_domain}|{namespace}".encode("utf-8")
        return hmac.new(self._secret, msg, hashlib.sha256).hexdigest()

    def verify_capability(self, capability: "WriterCapability") -> bool:
        if not self.held:
            return False
        expected = self.sign_capability(capability.writer_domain, capability.namespace)
        return hmac.compare_digest(expected, capability._signature)

    def issue_capability(
        self,
        *,
        writer_domain: str = WRITER_DOMAIN_CANONICAL,
        namespace: str = NAMESPACE_ISOLATED_TEST,
        allow_production_cutover: bool = False,
    ) -> "WriterCapability":
        return issue_writer_capability(
            lease=self,
            writer_domain=writer_domain,
            namespace=namespace,
            allow_production_cutover=allow_production_cutover,
        )

    def status(self) -> dict[str, Any]:
        return {
            "instance_id": self.instance_id,
            "pid": self.pid,
            "store_root": str(self.store_root),
            "held": self.held,
            "lock_path": str(self.lock_path),
        }


@dataclass(frozen=True)
class WriterCapability:
    """Unforgeable capability token required by CanonicalActivityStore / CommandService."""

    writer_domain: str
    namespace: str
    store_root: Path
    lease_instance_id: str
    _signature: str = field(repr=False)


def issue_writer_capability(
    *,
    lease: ActivityAuthorityLease,
    writer_domain: str = WRITER_DOMAIN_CANONICAL,
    namespace: str = NAMESPACE_ISOLATED_TEST,
    allow_production_cutover: bool = False,
) -> WriterCapability:
    """Validate writer domain and namespace before issuing a WriterCapability."""

    if not isinstance(lease, ActivityAuthorityLease) or not lease.held:
        raise WriterCapabilityError("cannot issue WriterCapability without a held ActivityAuthorityLease")

    domain = str(writer_domain or "").strip().lower()
    if domain in FORBIDDEN_WRITER_DOMAINS or domain != WRITER_DOMAIN_CANONICAL:
        raise WriterCapabilityError(
            f"writer_domain={writer_domain!r} is DENIED canonical Activity write authority; "
            f"only {WRITER_DOMAIN_CANONICAL!r} may hold writer capability"
        )

    ns = str(namespace or "").strip().lower()
    if ns in NON_PRODUCTION_NAMESPACES:
        raise WriterCapabilityError(
            f"namespace={namespace!r} (Shadow/Replay/Research/Evaluator/Twin) is permanently DENIED "
            f"canonical Activity write capability"
        )
    if ns not in {NAMESPACE_CANONICAL, NAMESPACE_ISOLATED_TEST}:
        raise WriterCapabilityError(f"unsupported writer namespace={namespace!r}")

    if is_production_path(lease.store_root):
        cutover_env = os.environ.get(PRODUCTION_CUTOVER_ENV, "off").strip().lower()
        if not allow_production_cutover or cutover_env != "canonical":
            raise WriterCapabilityError(
                f"LR-2 production non-cutover guard: writing to production path {lease.store_root} "
                f"is blocked while {PRODUCTION_CUTOVER_ENV}={cutover_env!r}"
            )

    sig = lease.sign_capability(domain, ns)
    return WriterCapability(
        writer_domain=domain,
        namespace=ns,
        store_root=lease.store_root,
        lease_instance_id=lease.instance_id,
        _signature=sig,
    )


# ---------------------------------------------------------------------------
# Data Models: ActivityInstance, LifeTransition, LifeFrameReadView
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CommandResult:
    """Result returned by ActivityCommandService mutations."""

    committed: bool
    idempotent_replay: bool
    noop: bool
    revision: int
    life_state: str
    foreground_activity_ref: Optional[str]
    activity: Optional[dict[str, Any]]
    transition: Optional[dict[str, Any]]
    life_frame: dict[str, Any]
    commit_receipt: Optional[dict[str, Any]] = None


def build_life_frame_read_view(
    canonical_state: Mapping[str, Any],
    *,
    caller_context: Optional[Mapping[str, Any]] = None,
    interruption_overlay: Optional[Mapping[str, Any]] = None,
    waiting_overlay: Optional[Mapping[str, Any]] = None,
    progress_overlay: Optional[Mapping[str, Any]] = None,
) -> dict[str, Any]:
    """Compose the derived LifeFrameReadView from canonical state without duplicating truth.

    Per LR-1 Invariant 7 & 8, LR-3 Section 32, LR-4 Section 48, and LR-5 Section 100:
    - Does NOT store a second copy of Activity.status, World location, Body pose, or progress.
    - Channel/session in `caller_context` never changes `subject_id` or the derived view.
    - When `interruption_overlay` is supplied by LR-3 InterruptionCoordinator, composes
      `interruptibility`, `pending_interruption_ref`, and `communication_availability`.
    - When `waiting_overlay` is supplied by LR-4 WaitingCoordinator, composes derived
      `waiting_on_ref`, `resume_condition_ref`, `resume_eligible`, and `next_recheck_at`
      without copying full Condition or Agenda bodies.
    - When `progress_overlay` is supplied by LR-5 ActivityProgressCoordinator, exposes
      read-only `checkpoint_ref`, `completion_status`, and `progress_kind` without
      granting Grounding any write authority.
    """

    subject_id = validate_subject_id(canonical_state.get("subject_id"))
    fg_ref = canonical_state.get("foreground_activity_ref")
    activities = canonical_state.get("activities") or {}

    if fg_ref is None:
        life_state = LIFE_STATE_IDLE
        fg_kind = None
        fg_occupancy = INTERRUPTIBILITY_FREE
        action_reality_refs = {
            "checkpoint_ref": None,
            "last_action_ref": None,
            "completion_evidence_ref": None,
        }
        waiting_refs = {
            "waiting_on_ref": None,
            "resume_condition_ref": None,
        }
    else:
        fg_act = activities.get(fg_ref)
        if not isinstance(fg_act, Mapping):
            raise RecoveryRequiredError(
                "FOREGROUND_REF_MISSING_ACTIVITY",
                f"foreground_activity_ref={fg_ref!r} does not exist in canonical activities map",
            )
        act_status = fg_act.get("status")
        if act_status not in OPEN_ACTIVITY_STATUSES:
            raise RecoveryRequiredError(
                "FOREGROUND_REF_NOT_OPEN",
                f"foreground_activity_ref={fg_ref!r} has non-open status={act_status!r}",
            )
        life_state = act_status  # ACTIVE | PAUSED | WAITING
        fg_kind = fg_act.get("activity_kind")
        fg_occupancy = fg_act.get("interruptibility", INTERRUPTIBILITY_LIGHT)
        action_reality_refs = {
            "checkpoint_ref": fg_act.get("checkpoint_ref"),
            "last_action_ref": fg_act.get("last_action_ref"),
            "completion_evidence_ref": fg_act.get("completion_evidence_ref"),
        }
        waiting_refs = {
            "waiting_on_ref": fg_act.get("waiting_on_ref"),
            "resume_condition_ref": fg_act.get("resume_condition_ref"),
        }

    view = {
        "schema_version": SCHEMA_LIFE_FRAME_VIEW,
        "derived_read_model": True,
        "subject_id": subject_id,
        "life_state": life_state,
        "foreground_activity_ref": fg_ref,
        "foreground_activity_kind": fg_kind,
        "attention_occupancy": fg_occupancy if fg_ref else INTERRUPTIBILITY_FREE,
        "pending_transition_ref": canonical_state.get("pending_transition_ref"),
        "last_transition_ref": canonical_state.get("last_transition_ref"),
        "view_revision": int(canonical_state.get("revision", 0)),
        "action_reality_refs": action_reality_refs,
        "waiting_refs": waiting_refs,
    }

    if interruption_overlay is not None:
        view["attention_occupancy"] = interruption_overlay.get(
            "attention_occupancy", view["attention_occupancy"]
        )
        view["interruptibility"] = interruption_overlay.get("interruptibility")
        view["pending_interruption_ref"] = interruption_overlay.get("pending_interruption_ref")
        view["communication_availability"] = interruption_overlay.get("communication_availability")

    if waiting_overlay is not None:
        if waiting_overlay.get("waiting_on_ref") is not None:
            view["waiting_refs"]["waiting_on_ref"] = waiting_overlay.get("waiting_on_ref")
        if waiting_overlay.get("resume_condition_ref") is not None:
            view["waiting_refs"]["resume_condition_ref"] = waiting_overlay.get("resume_condition_ref")
        view["waiting_on_ref"] = view["waiting_refs"]["waiting_on_ref"]
        view["resume_condition_ref"] = view["waiting_refs"]["resume_condition_ref"]
        view["resume_eligible"] = bool(waiting_overlay.get("resume_eligible", False))
        view["next_recheck_at"] = waiting_overlay.get("next_recheck_at")
        if "open_eligibility_refs" in waiting_overlay:
            view["open_eligibility_refs"] = list(waiting_overlay["open_eligibility_refs"])
        if "waiting_activity_refs" in waiting_overlay:
            view["waiting_activity_refs"] = list(waiting_overlay["waiting_activity_refs"])

    if progress_overlay is not None:
        view["checkpoint_ref"] = progress_overlay.get(
            "checkpoint_ref", view["action_reality_refs"]["checkpoint_ref"]
        )
        view["completion_status"] = progress_overlay.get("completion_status")
        view["progress_kind"] = progress_overlay.get("progress_kind")

    return view


# ---------------------------------------------------------------------------
# Canonical Persistence & WAL Commit Intent Protocol (LR-2 + LR-3 Preflight)
# ---------------------------------------------------------------------------


def _empty_canonical_state(at_iso: Optional[str] = None) -> dict[str, Any]:
    """The revision-0 canonical state, before any successful mutation.

    ``updated_at`` means "the time of the last SUCCESSFUL canonical Activity mutation". A store
    that has never committed anything has no such time, so the default is ``None``: reading an
    uncommitted store must be a pure, repeatable observation and must never invent a wall-clock
    stamp. Rejected, invalid, stale and failed-admission commands consequently leave this field
    untouched, which is what makes `rejected command -> canonical state unchanged` hold byte for
    byte.

    ``at_iso`` is honoured when a caller has a real recorded timestamp (deterministic replay,
    recovery rebuild, restore), so those paths stay deterministic too.
    """
    return {
        "schema_version": SCHEMA_CANONICAL_LIFE_STATE,
        "subject_id": GLOBAL_SUBJECT_ID,
        "revision": 0,
        "foreground_activity_ref": None,
        "pending_transition_ref": None,
        "last_transition_ref": None,
        "last_transition_hash": GENESIS_HASH,
        "updated_at": at_iso,
        "activities": {},
    }


def _fsync_dir(dir_path: Path) -> None:
    dir_fd = os.open(str(dir_path), os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


class CanonicalActivityStore:
    """Crash-safe, single-writer canonical store with WAL Commit Intent + Journal Atomicity.

    Commit Protocol (LR-3 Section 1 Preflight Upgrade):
    1. Write `.life_transitions.jsonl.tmp` (fsync) and `.canonical_life_state.json.tmp` (fsync).
    2. Write and atomically replace `commit_intent.json` (recording `target_revision`,
       `transition_id`, `entry_hash`, `state_sha256`).
    3. Atomically replace `life_transitions.jsonl` (SINGLE TRUE COMMIT POINT).
    4. Atomically replace `canonical_life_state.json`.
    5. Unlink `commit_intent.json`.

    Recovery Semantics:
    - If a crash occurs BEFORE Step 3 completes: `life_transitions.jsonl` is still at
      revision `N` (does not contain `commit_intent.entry_hash`). Recovery rolls back
      `commit_intent.json` and returns the exact previous committed state (`revision = N`).
    - If a crash occurs AT or AFTER Step 3 (journal has `N+1` matching `commit_intent.json`,
      even if `canonical_life_state.json` was not yet replaced or failed to write):
      Recovery deterministically rebuilds `canonical_life_state.json` at `revision = N+1`
      from the verified authoritative journal, verifies its digest against `commit_intent`,
      atomically replaces `canonical_life_state.json`, clears `commit_intent.json`, and
      returns the new committed state (`revision = N+1`).
    - External corruption or revision mismatch WITHOUT a valid matching `commit_intent.json`
      fails closed (`RecoveryRequiredError`).
    """

    def __init__(self, store_root: Path | str, *, namespace: str = NAMESPACE_ISOLATED_TEST, _admission_issuer: Optional[AdmissionIssuer] = None):
        self.store_root = Path(store_root).expanduser().resolve()
        if _admission_issuer is not None:
            if not isinstance(_admission_issuer, AdmissionIssuer):
                raise WriterCapabilityError("canonical runtime requires an AdmissionIssuer from assembly")
            _admission_issuer._bind_store_once(self)
        self.__admission_issuer = _admission_issuer
        self.namespace = namespace
        self.state_path = self.store_root / STATE_FILENAME
        self.journal_path = self.store_root / JOURNAL_FILENAME
        self.intent_path = self.store_root / COMMIT_INTENT_FILENAME
        self.run_dir = self.store_root / "run"
        self.lock_path = self.run_dir / LOCK_FILENAME
        self._verified_cache: Optional[dict[str, Any]] = None

    @contextmanager
    def _store_io_lock(self, *, shared: bool = False, timeout_seconds: float = 5.0) -> Iterator[None]:
        self.run_dir.mkdir(parents=True, exist_ok=True)
        io_lock_path = self.run_dir / ".store_io.lock"
        mode = fcntl.LOCK_SH if shared else fcntl.LOCK_EX
        deadline = time.monotonic() + max(0.0, timeout_seconds)
        with io_lock_path.open("a+", encoding="utf-8") as handle:
            while True:
                try:
                    fcntl.flock(handle.fileno(), mode | fcntl.LOCK_NB)
                    break
                except OSError as exc:
                    if exc.errno not in (errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK):
                        raise ActivityLockError(f"store IO lock failed: {exc}") from exc
                    if time.monotonic() >= deadline:
                        raise ActivityLockError("store IO lock timeout") from exc
                    time.sleep(0.01)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _cleanup_stray_temp_files_unlocked(self) -> None:
        if not self.store_root.exists():
            return
        for item in self.store_root.iterdir():
            if item.is_file() and (
                item.name.startswith(f".{JOURNAL_FILENAME}.tmp.")
                or item.name.startswith(f".{STATE_FILENAME}.tmp.")
                or item.name.startswith(f".{COMMIT_INTENT_FILENAME}.tmp.")
            ):
                try:
                    item.unlink(missing_ok=True)
                except OSError:
                    pass

    def _reconcile_commit_intent_unlocked(self, journal_entries: list[dict[str, Any]]) -> None:
        """Resolve an interrupted WAL commit_intent.json deterministically."""
        self._cleanup_stray_temp_files_unlocked()
        if not self.intent_path.exists():
            return

        try:
            intent = json.loads(self.intent_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise RecoveryRequiredError(
                "CORRUPTED_COMMIT_INTENT",
                f"commit_intent.json is unreadable or invalid JSON: {exc}",
            ) from exc

        if not isinstance(intent, dict) or intent.get("schema_version") != SCHEMA_COMMIT_INTENT:
            raise RecoveryRequiredError(
                "INVALID_COMMIT_INTENT_SCHEMA",
                f"invalid commit_intent.json schema: {intent!r}",
            )

        target_rev = intent.get("target_revision")
        expected_prev_rev = intent.get("expected_revision")
        intent_trn_id = intent.get("transition_id")
        intent_entry_hash = intent.get("entry_hash")
        intent_state_sha = intent.get("state_sha256")

        current_journal_rev = len(journal_entries)

        # Case A: Crash occurred AFTER commit_intent.json was written, BEFORE journal was replaced.
        # Journal is still at expected_prev_rev. Roll back commit_intent.json cleanly.
        if current_journal_rev == expected_prev_rev:
            self.intent_path.unlink(missing_ok=True)
            _fsync_dir(self.store_root)
            return

        # Case B: Crash occurred AT or AFTER journal was replaced (journal is at target_rev).
        if current_journal_rev == target_rev and current_journal_rev > 0:
            last_entry = journal_entries[-1]
            if (
                last_entry.get("transition_id") == intent_trn_id
                and last_entry.get("entry_hash") == intent_entry_hash
            ):
                # Reconstruct canonical state deterministically from authoritative journal
                rebuilt_state = ReplayRuntime.project_from_transitions(journal_entries)
                rebuilt_payload = (
                    json.dumps(rebuilt_state, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
                )
                if intent_state_sha and sha256_hex(rebuilt_payload) != intent_state_sha:
                    raise RecoveryRequiredError(
                        "COMMIT_INTENT_STATE_DIGEST_MISMATCH",
                        "rebuilt state from journal does not match commit_intent state_sha256",
                    )
                state_tmp = (
                    self.store_root
                    / f".{STATE_FILENAME}.tmp.{os.getpid()}.{uuid.uuid4().hex}"
                )
                try:
                    with state_tmp.open("w", encoding="utf-8") as sh:
                        sh.write(rebuilt_payload)
                        sh.flush()
                        os.fsync(sh.fileno())
                    os.replace(state_tmp, self.state_path)
                    self.intent_path.unlink(missing_ok=True)
                    _fsync_dir(self.store_root)
                finally:
                    if state_tmp.exists():
                        state_tmp.unlink(missing_ok=True)
                return

        raise RecoveryRequiredError(
            "UNRECONCILABLE_COMMIT_INTENT",
            f"commit_intent target_revision={target_rev} cannot be reconciled with "
            f"journal length={current_journal_rev}",
        )

    def _read_and_verify_journal_unlocked(self) -> list[dict[str, Any]]:
        if not self.journal_path.exists():
            return []
        try:
            raw_bytes = self.journal_path.read_bytes()
        except OSError as exc:
            raise RecoveryRequiredError("JOURNAL_UNREADABLE", f"cannot read journal: {exc}") from exc

        if not raw_bytes:
            return []

        try:
            text = raw_bytes.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise RecoveryRequiredError("JOURNAL_CORRUPT_UTF8", f"invalid UTF-8 in journal: {exc}") from exc

        if not text.endswith("\n"):
            raise RecoveryRequiredError(
                "INCOMPLETE_JOURNAL_TAIL",
                "life_transitions.jsonl does not end with a newline (truncated tail)",
            )

        entries: list[dict[str, Any]] = []
        seen_transition_ids: set[str] = set()
        seen_idempotency_keys: set[str] = set()
        prev_hash = GENESIS_HASH
        expected_rev = 1

        for line_no, raw_line in enumerate(text.splitlines(), start=1):
            if not raw_line.strip():
                raise RecoveryRequiredError(
                    "JOURNAL_BLANK_LINE",
                    f"blank line at journal line {line_no}",
                )
            try:
                entry = json.loads(raw_line)
            except ValueError as exc:
                raise RecoveryRequiredError(
                    "INCOMPLETE_JOURNAL_TAIL",
                    f"malformed JSON at journal line {line_no}: {exc}",
                ) from exc
            if not isinstance(entry, dict):
                raise RecoveryRequiredError(
                    "JOURNAL_LINE_NOT_OBJECT",
                    f"journal line {line_no} is not a JSON object",
                )
            if entry.get("schema_version") != SCHEMA_LIFE_TRANSITION:
                raise RecoveryRequiredError(
                    "UNKNOWN_JOURNAL_SCHEMA_VERSION",
                    f"journal line {line_no} has unsupported schema_version={entry.get('schema_version')!r}",
                )

            trn_id = entry.get("transition_id")
            if not isinstance(trn_id, str) or not trn_id:
                raise RecoveryRequiredError("MISSING_TRANSITION_ID", f"line {line_no} missing transition_id")
            if trn_id in seen_transition_ids:
                raise RecoveryRequiredError(
                    "DUPLICATE_TRANSITION_ID",
                    f"duplicate transition_id={trn_id!r} at line {line_no}",
                )
            seen_transition_ids.add(trn_id)

            idem_key = entry.get("idempotency_key")
            if not isinstance(idem_key, str) or not idem_key:
                raise RecoveryRequiredError("MISSING_IDEMPOTENCY_KEY", f"line {line_no} missing idempotency_key")
            if idem_key in seen_idempotency_keys:
                raise RecoveryRequiredError(
                    "DUPLICATE_IDEMPOTENCY_KEY",
                    f"duplicate idempotency_key={idem_key!r} at line {line_no}",
                )
            seen_idempotency_keys.add(idem_key)

            if entry.get("expected_revision") != expected_rev - 1 or entry.get("result_revision") != expected_rev:
                raise RecoveryRequiredError(
                    "BAD_REVISION_CHAIN",
                    f"journal line {line_no} revision mismatch: expected ({expected_rev - 1}->{expected_rev}), "
                    f"got ({entry.get('expected_revision')}->{entry.get('result_revision')})",
                )

            if entry.get("prev_hash") != prev_hash:
                raise RecoveryRequiredError(
                    "JOURNAL_HASH_CHAIN_BROKEN",
                    f"journal line {line_no} prev_hash mismatch",
                )
            recorded_hash = entry.get("entry_hash")
            check_copy = dict(entry)
            check_copy.pop("entry_hash", None)
            computed_hash = sha256_hex(canonical_json_line(check_copy))
            if recorded_hash != computed_hash:
                raise RecoveryRequiredError(
                    "JOURNAL_ENTRY_TAMPERED",
                    f"journal line {line_no} entry_hash mismatch",
                )

            prev_hash = recorded_hash
            expected_rev += 1
            entries.append(entry)

        return entries

    def _read_and_verify_state_unlocked(self, journal_entries: list[dict[str, Any]]) -> dict[str, Any]:
        self._reconcile_commit_intent_unlocked(journal_entries)

        if not self.state_path.exists():
            if journal_entries:
                raise RecoveryRequiredError(
                    "SNAPSHOT_MISSING_WITH_NONEMPTY_JOURNAL",
                    "canonical_life_state.json is missing while life_transitions.jsonl has entries",
                )
            return _empty_canonical_state()

        try:
            raw_text = self.state_path.read_text(encoding="utf-8")
            state = json.loads(raw_text)
        except (OSError, ValueError) as exc:
            raise RecoveryRequiredError(
                "CORRUPTED_SNAPSHOT",
                f"canonical_life_state.json is unreadable or invalid JSON: {exc}",
            ) from exc

        if not isinstance(state, dict):
            raise RecoveryRequiredError("CORRUPTED_SNAPSHOT", "snapshot root must be a JSON object")

        if state.get("schema_version") != SCHEMA_CANONICAL_LIFE_STATE:
            raise RecoveryRequiredError(
                "UNKNOWN_SCHEMA_VERSION",
                f"unsupported snapshot schema_version={state.get('schema_version')!r}",
            )

        validate_subject_id(state.get("subject_id"))

        revision = state.get("revision")
        if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
            raise RecoveryRequiredError("BAD_REVISION_CHAIN", f"invalid snapshot revision={revision!r}")

        if revision != len(journal_entries):
            raise RecoveryRequiredError(
                "BAD_REVISION_CHAIN",
                f"snapshot revision={revision} does not match journal length={len(journal_entries)}",
            )

        if revision == 0:
            if state.get("last_transition_ref") is not None:
                raise RecoveryRequiredError(
                    "MISSING_TRANSITION",
                    "revision 0 snapshot cannot reference a non-null last_transition_ref",
                )
        else:
            last_entry = journal_entries[-1]
            if state.get("last_transition_ref") != last_entry["transition_id"]:
                raise RecoveryRequiredError(
                    "MISSING_TRANSITION",
                    f"snapshot last_transition_ref={state.get('last_transition_ref')!r} "
                    f"!= journal tail transition_id={last_entry['transition_id']!r}",
                )
            if state.get("last_transition_hash") != last_entry["entry_hash"]:
                raise RecoveryRequiredError(
                    "BAD_REVISION_CHAIN",
                    "snapshot last_transition_hash does not match journal tail entry_hash",
                )

        activities = state.get("activities")
        if not isinstance(activities, dict):
            raise RecoveryRequiredError("CORRUPTED_SNAPSHOT", "activities must be a JSON object")

        fg_ref = state.get("foreground_activity_ref")
        fg_bound_open_ids: list[str] = []
        for act_id, act in activities.items():
            if not isinstance(act, dict) or act.get("activity_id") != act_id:
                raise RecoveryRequiredError("CORRUPTED_SNAPSHOT", f"invalid activity record for {act_id!r}")
            if act.get("schema_version") != SCHEMA_ACTIVITY_INSTANCE:
                raise RecoveryRequiredError(
                    "UNKNOWN_SCHEMA_VERSION",
                    f"activity {act_id!r} has unknown schema_version={act.get('schema_version')!r}",
                )
            validate_subject_id(act.get("subject_id"))
            validate_no_forbidden_self_truth(act)
            status = act.get("status")
            if status not in ALL_ACTIVITY_STATUSES:
                raise RecoveryRequiredError("CORRUPTED_SNAPSHOT", f"activity {act_id!r} has invalid status={status!r}")
            if status in OPEN_ACTIVITY_STATUSES:
                if status == STATUS_WAITING and act.get("foreground_released") is True:
                    continue
                fg_bound_open_ids.append(act_id)

        if len(fg_bound_open_ids) > 1:
            raise RecoveryRequiredError(
                "MULTIPLE_FOREGROUND_ACTIVITIES",
                f"more than 1 foreground-bound open activity found in canonical store: {fg_bound_open_ids}",
            )

        if fg_ref is None and fg_bound_open_ids:
            raise RecoveryRequiredError(
                "FOREGROUND_MISMATCH",
                f"foreground_activity_ref is None but foreground-bound open activity exists: {fg_bound_open_ids}",
            )

        if fg_ref is not None:
            if fg_ref not in activities:
                raise RecoveryRequiredError(
                    "MISSING_ACTIVITY_TARGET",
                    f"foreground_activity_ref={fg_ref!r} points to missing activity",
                )
            if fg_bound_open_ids != [fg_ref]:
                raise RecoveryRequiredError(
                    "FOREGROUND_MISMATCH",
                    f"foreground_activity_ref={fg_ref!r} does not match fg_bound_open_ids={fg_bound_open_ids}",
                )

        replayed = ReplayRuntime.project_from_transitions(journal_entries)
        if not _projections_equivalent(state, replayed):
            raise RecoveryRequiredError(
                "REPLAY_PROJECTION_MISMATCH",
                "snapshot state diverges from deterministic replay of life_transitions.jsonl",
            )

        return state

    def _disk_byte_signature_unlocked(self) -> Optional[tuple[bytes, bytes]]:
        if self.intent_path.exists() or not self.state_path.exists() or not self.journal_path.exists():
            return None
        try:
            s_hash = hashlib.sha256(self.state_path.read_bytes()).digest()
            j_hash = hashlib.sha256(self.journal_path.read_bytes()).digest()
            return (s_hash, j_hash)
        except OSError:
            return None

    def _load_verified_unlocked(
        self,
        *,
        _include_journal: bool = True,
        _copy_journal: bool = True,
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        self._cleanup_stray_temp_files_unlocked()
        sig = self._disk_byte_signature_unlocked()
        if (
            self._verified_cache is not None
            and sig is not None
            and self._verified_cache.get("sig") == sig
        ):
            cached_state = copy.deepcopy(self._verified_cache["state"])
            if not _include_journal:
                return cached_state, []
            cached_journal = (
                copy.deepcopy(self._verified_cache["journal"])
                if _copy_journal
                else self._verified_cache["journal"]
            )
            return cached_state, cached_journal

        journal = self._read_and_verify_journal_unlocked()
        state = self._read_and_verify_state_unlocked(journal)
        new_sig = self._disk_byte_signature_unlocked()
        if new_sig is not None:
            raw_j = self.journal_path.read_text(encoding="utf-8") if self.journal_path.exists() else ""
            self._verified_cache = {
                "sig": new_sig,
                "state": copy.deepcopy(state),
                "journal": copy.deepcopy(journal),
                "raw_journal": raw_j,
            }
        if not _include_journal:
            return copy.deepcopy(state), []
        return copy.deepcopy(state), (copy.deepcopy(journal) if _copy_journal else journal)

    def load_verified_state_and_journal(
        self,
        *,
        _include_journal: bool = True,
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        with self._store_io_lock(shared=False):
            return self._load_verified_unlocked(_include_journal=_include_journal, _copy_journal=True)

    def commit_transition(
        self,
        *,
        lease: ActivityAuthorityLease,
        capability: WriterCapability,
        new_state: dict[str, Any],
        new_transition_unsigned: dict[str, Any],
        journal_entries: list[dict[str, Any]],
        permit: Any,
        fault_hook: Optional[Callable[[str], None]] = None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Atomically commit transition + canonical state using WAL commit_intent.json."""

        if not isinstance(lease, ActivityAuthorityLease) or not lease.held:
            raise WriterCapabilityError("canonical commit requires a held ActivityAuthorityLease")
        if lease.store_root != self.store_root or capability.store_root != self.store_root:
            raise WriterCapabilityError("WriterCapability store_root mismatch")
        if not lease.verify_capability(capability):
            raise WriterCapabilityError("WriterCapability signature verification failed")
        issuer = self.__admission_issuer
        if issuer is None:
            raise WriterCapabilityError("canonical commit rejected: runtime has no owner-backed admission assembly")
        try:
            owner_snapshot = issuer.consume(permit, new_transition_unsigned)
        except AdmissionRejected as exc:
            raise WriterCapabilityError(f"canonical commit rejected by opaque admission permit: {exc}") from exc
        new_transition_unsigned = dict(new_transition_unsigned)
        if isinstance(owner_snapshot, dict):
            new_transition_unsigned["authority_evidence"] = dict(owner_snapshot)
            new_transition_unsigned["provenance_owner_snapshot"] = {
                "authority_class": owner_snapshot.get("authority_class"),
                "owner_label": owner_snapshot.get("owner_label"),
                "activity_id": owner_snapshot.get("activity_id"),
                "activity_revision": owner_snapshot.get("activity_revision"),
                "runtime_epoch": owner_snapshot.get("runtime_epoch"),
            }
        else:
            new_transition_unsigned["provenance_owner_snapshot"] = {
                "decision_ref": owner_snapshot[0], "decision_revision": owner_snapshot[1],
                "adoption_ref": owner_snapshot[2], "adoption_revision": owner_snapshot[3],
                "adoption_status": owner_snapshot[4], "runtime_epoch": owner_snapshot[5],
            }

        prev_hash = journal_entries[-1]["entry_hash"] if journal_entries else GENESIS_HASH
        signed_transition = dict(new_transition_unsigned)
        signed_transition["prev_hash"] = prev_hash
        signed_transition.pop("entry_hash", None)
        entry_hash = sha256_hex(canonical_json_line(signed_transition))
        signed_transition["entry_hash"] = entry_hash

        committed_state = copy.deepcopy(new_state)
        committed_state["last_transition_ref"] = signed_transition["transition_id"]
        committed_state["last_transition_hash"] = entry_hash
        committed_state["pending_transition_ref"] = None

        self.store_root.mkdir(parents=True, exist_ok=True)
        journal_tmp = self.store_root / f".{JOURNAL_FILENAME}.tmp.{os.getpid()}.{uuid.uuid4().hex}"
        state_tmp = self.store_root / f".{STATE_FILENAME}.tmp.{os.getpid()}.{uuid.uuid4().hex}"
        intent_tmp = self.store_root / f".{COMMIT_INTENT_FILENAME}.tmp.{os.getpid()}.{uuid.uuid4().hex}"

        do_fsync = fault_hook is not None or self.namespace != NAMESPACE_ISOLATED_TEST

        try:
            new_line = canonical_json_line(signed_transition)
            if (
                self._verified_cache is not None
                and len(self._verified_cache.get("journal", [])) == len(journal_entries)
                and self._verified_cache.get("raw_journal") is not None
            ):
                journal_payload = self._verified_cache["raw_journal"] + new_line + "\n"
            else:
                all_lines = [canonical_json_line(e) for e in journal_entries] + [new_line]
                journal_payload = "\n".join(all_lines) + "\n"

            with journal_tmp.open("w", encoding="utf-8") as jh:
                jh.write(journal_payload)
                jh.flush()
                if do_fsync:
                    os.fsync(jh.fileno())

            if fault_hook is not None:
                fault_hook("after_journal_tmp_write")

            state_payload = json.dumps(committed_state, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
            with state_tmp.open("w", encoding="utf-8") as sh:
                sh.write(state_payload)
                sh.flush()
                if do_fsync:
                    os.fsync(sh.fileno())

            if fault_hook is not None:
                fault_hook("before_atomic_replace")

            intent_payload = (
                json.dumps(
                    {
                        "schema_version": SCHEMA_COMMIT_INTENT,
                        "expected_revision": signed_transition["expected_revision"],
                        "target_revision": signed_transition["result_revision"],
                        "transition_id": signed_transition["transition_id"],
                        "entry_hash": entry_hash,
                        "state_sha256": sha256_hex(state_payload),
                        "created_at": signed_transition["recorded_at"],
                    },
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                )
                + "\n"
            )
            with intent_tmp.open("w", encoding="utf-8") as ih:
                ih.write(intent_payload)
                ih.flush()
                if do_fsync:
                    os.fsync(ih.fileno())

            if fault_hook is not None:
                fault_hook("before_intent_replace")

            os.replace(intent_tmp, self.intent_path)
            if do_fsync:
                _fsync_dir(self.store_root)

            if fault_hook is not None:
                fault_hook("after_intent_replace_before_journal_replace")

            # SINGLE TRUE ATOMIC COMMIT POINT: authoritative journal replace
            os.replace(journal_tmp, self.journal_path)
            if do_fsync:
                _fsync_dir(self.store_root)

            if fault_hook is not None:
                fault_hook("after_journal_replace_before_state_replace")

            os.replace(state_tmp, self.state_path)
            if do_fsync:
                _fsync_dir(self.store_root)

            if fault_hook is not None:
                fault_hook("after_state_replace_before_intent_unlink")

            self.intent_path.unlink(missing_ok=True)
            if do_fsync:
                _fsync_dir(self.store_root)

            if fault_hook is None:
                s_hash = hashlib.sha256(state_payload.encode("utf-8")).digest()
                j_hash = hashlib.sha256(journal_payload.encode("utf-8")).digest()
                self._verified_cache = {
                    "sig": (s_hash, j_hash),
                    "state": copy.deepcopy(committed_state),
                    "journal": journal_entries + [copy.deepcopy(signed_transition)],
                    "raw_journal": journal_payload,
                }
            else:
                self._verified_cache = None
        except Exception:
            self._verified_cache = None
            raise
        finally:
            for tmp_file in (journal_tmp, state_tmp, intent_tmp):
                if tmp_file.exists():
                    tmp_file.unlink(missing_ok=True)

        return committed_state, signed_transition


def _projections_equivalent(state_a: Mapping[str, Any], state_b: Mapping[str, Any]) -> bool:
    keys = (
        "schema_version",
        "subject_id",
        "revision",
        "foreground_activity_ref",
        "last_transition_ref",
        "last_transition_hash",
        "activities",
    )
    for k in keys:
        if state_a.get(k) != state_b.get(k):
            return False
    return True


# ---------------------------------------------------------------------------
# Read Service vs. Command Service (Sections 36, 37)
# ---------------------------------------------------------------------------


class ActivityReadService:
    """Read-only interface for Grounding, Expression, and Diagnostics.

    Possesses ZERO write capability.
    """

    def __init__(self, store: CanonicalActivityStore):
        self._store = store

    def get_current_life_view(
        self,
        *,
        caller_context: Optional[Mapping[str, Any]] = None,
        interruption_overlay: Optional[Mapping[str, Any]] = None,
        waiting_overlay: Optional[Mapping[str, Any]] = None,
        progress_overlay: Optional[Mapping[str, Any]] = None,
    ) -> dict[str, Any]:
        state, _ = self._store.load_verified_state_and_journal(_include_journal=False)
        return build_life_frame_read_view(
            state,
            caller_context=caller_context,
            interruption_overlay=interruption_overlay,
            waiting_overlay=waiting_overlay,
            progress_overlay=progress_overlay,
        )

    def get_activity(self, activity_id: str) -> Optional[dict[str, Any]]:
        state, _ = self._store.load_verified_state_and_journal(_include_journal=False)
        act = state.get("activities", {}).get(activity_id)
        return copy.deepcopy(act) if act is not None else None

    def list_open_activities(self) -> list[dict[str, Any]]:
        state, _ = self._store.load_verified_state_and_journal(_include_journal=False)
        return [
            copy.deepcopy(act)
            for act in state.get("activities", {}).values()
            if act.get("status") in OPEN_ACTIVITY_STATUSES
        ]

    def list_waiting_activities(self) -> list[dict[str, Any]]:
        state, _ = self._store.load_verified_state_and_journal(_include_journal=False)
        return [
            copy.deepcopy(act)
            for act in state.get("activities", {}).values()
            if act.get("status") == STATUS_WAITING
        ]

    def get_transition_history(self, *, activity_id: Optional[str] = None) -> list[dict[str, Any]]:
        _, journal = self._store.load_verified_state_and_journal()
        if activity_id is None:
            return journal
        return [entry for entry in journal if entry.get("activity_id") == activity_id]

    def read_canonical_state(
        self, *, include_journal: bool = False
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """Read-only canonical snapshot for cross-domain read ports.

        Deep copies: a holder of this port cannot mutate canonical state, and this port
        is not a write capability.
        """
        state, journal = self._store.load_verified_state_and_journal(
            _include_journal=include_journal
        )
        return (
            copy.deepcopy(state),
            copy.deepcopy(journal) if include_journal else [],
        )

    def recover_cold_start(self) -> dict[str, Any]:
        """Cold restart recovery verification (0 LLM, 0 actions, 0 side effects)."""
        state, journal = self._store.load_verified_state_and_journal()
        life_frame = build_life_frame_read_view(state)
        return {
            "recovered": True,
            "revision": state["revision"],
            "life_state": life_frame["life_state"],
            "foreground_activity_ref": state["foreground_activity_ref"],
            "last_transition_ref": state["last_transition_ref"],
            "journal_count": len(journal),
            "life_frame": life_frame,
        }


class ActivityCommandService:
    """Single Canonical Activity Writer & Deterministic Transition Engine."""

    def __init__(
        self,
        store: CanonicalActivityStore,
        *,
        lease: ActivityAuthorityLease,
        capability: WriterCapability,
    ):
        if not isinstance(capability, WriterCapability):
            raise WriterCapabilityError("ActivityCommandService requires a valid WriterCapability")
        if not isinstance(lease, ActivityAuthorityLease) or not lease.held:
            raise WriterCapabilityError("ActivityCommandService requires a held ActivityAuthorityLease")
        if not lease.verify_capability(capability) or lease.store_root != store.store_root:
            raise WriterCapabilityError("WriterCapability does not match store and held lease")
        self._store = store
        self._admission_issuer = store._CanonicalActivityStore__admission_issuer
        self._lease = lease
        self._capability = capability
        self._reader = ActivityReadService(store)

    @property
    def read_service(self) -> ActivityReadService:
        return self._reader

    def _mint_admission_permit(
        self,
        transition: Mapping[str, Any],
        *,
        consequence_capability: Optional[Any] = None,
        canonical_state: Optional[Mapping[str, Any]] = None,
    ) -> Any:
        """Mint exactly one opaque permit for one command.

        Authority class is decided by *what evidence the caller can actually
        present*, never by a caller-supplied class string:

        * both an AG-1 Decision ref and an AG-2 Adoption ref -> AGENCY_AUTHORIZED;
        * otherwise -> an ACTIVITY_CONSEQUENCE capability minted only by the
          composition root;
        * neither -> fail closed.
        """
        issuer = self._admission_issuer
        if issuer is None:
            raise WriterCapabilityError(
                "NORMAL Activity command runtime lacks immutable owner-backed admission assembly"
            )
        agency_evidence = bool(transition.get("decision_ref")) and bool(
            transition.get("adoption_ref")
        )
        try:
            if agency_evidence:
                return issuer.issue(transition)
            if consequence_capability is not None:
                return issuer.issue_consequence(
                    consequence_capability, transition, canonical_state=canonical_state
                )
            raise AdmissionRejected("NO_AUTHORITY_CAPABILITY_FOR_TRANSITION")
        except AdmissionRejected as exc:
            raise WriterCapabilityError(
                f"NORMAL Activity command rejected by owner-backed admission: {exc}"
            ) from exc

    def _build_commit_receipt(
        self,
        *,
        transition: Mapping[str, Any],
        previous_revision: int,
        new_revision: int,
    ) -> dict[str, Any]:
        """Build the immutable LR-2 commit receipt. LR-2 never writes Adoption state."""
        owner_snapshot = dict(transition.get("provenance_owner_snapshot") or {})
        authority_evidence = transition.get("authority_evidence") or {}
        authority_class = (
            AUTHORITY_CONSEQUENCE if authority_evidence else AUTHORITY_AGENCY
        )
        digest_source = authority_evidence if authority_evidence else owner_snapshot
        receipt = ActivityCommitReceipt(
            receipt_id=ActivityCommitReceipt.build_receipt_id(
                command_id=str(transition.get("idempotency_key")),
                activity_id=str(transition.get("activity_id")),
                transition_id=str(transition.get("transition_id")),
            ),
            command_id=str(transition.get("idempotency_key")),
            activity_id=str(transition.get("activity_id")),
            transition_id=str(transition.get("transition_id")),
            transition_type=str(transition.get("transition_type")),
            previous_revision=int(previous_revision),
            new_revision=int(new_revision),
            authority_class=authority_class,
            evidence_digest=sha256_hex(canonical_json_line(digest_source)),
            runtime_epoch=(
                self._admission_issuer.runtime_epoch
                if self._admission_issuer is not None
                else ""
            ),
            decision_ref=owner_snapshot.get("decision_ref"),
            decision_revision=owner_snapshot.get("decision_revision"),
            adoption_ref=owner_snapshot.get("adoption_ref"),
            adoption_revision=owner_snapshot.get("adoption_revision"),
            committed_at=transition.get("occurred_at"),
        )
        return receipt.to_dict()

    def _check_idempotency(
        self,
        journal: list[dict[str, Any]],
        state: dict[str, Any],
        *,
        idempotency_key: str,
        transition_type: str,
        target_activity_id: Optional[str],
        activity_kind: Optional[str] = None,
    ) -> Optional[CommandResult]:
        if not isinstance(idempotency_key, str) or not idempotency_key.strip():
            raise ContractViolationError("idempotency_key must be a non-empty string")
        key = idempotency_key.strip()
        for entry in journal:
            if entry.get("idempotency_key") == key:
                if entry.get("transition_type") != transition_type:
                    raise IdempotencyConflictError(
                        f"idempotency_key={key!r} already used for {entry.get('transition_type')!r}, "
                        f"cannot reuse for {transition_type!r}"
                    )
                if target_activity_id is not None and entry.get("activity_id") != target_activity_id:
                    raise IdempotencyConflictError(
                        f"idempotency_key={key!r} already used for activity_id={entry.get('activity_id')!r}"
                    )
                if transition_type == TRANSITION_START and activity_kind is not None:
                    snap = entry.get("activity_snapshot") or {}
                    if snap.get("activity_kind") != activity_kind:
                        raise IdempotencyConflictError(
                            f"idempotency_key={key!r} reused with different activity_kind"
                        )
                act_id = entry["activity_id"]
                act = state.get("activities", {}).get(act_id)
                life_frame = build_life_frame_read_view(state)
                return CommandResult(
                    committed=False,
                    idempotent_replay=True,
                    noop=False,
                    revision=state["revision"],
                    life_state=life_frame["life_state"],
                    foreground_activity_ref=state["foreground_activity_ref"],
                    activity=copy.deepcopy(act),
                    transition=copy.deepcopy(entry),
                    life_frame=life_frame,
                    commit_receipt=self._build_commit_receipt(
                        transition=entry,
                        previous_revision=int(entry.get("expected_revision", 0)),
                        new_revision=int(entry.get("result_revision", 0)),
                    ),
                )
        return None

    def noop(self, *, expected_revision: Optional[int] = None) -> CommandResult:
        """Explicit NOOP: returns current state without forging a transition."""
        state, _ = self._store.load_verified_state_and_journal()
        if expected_revision is not None and expected_revision != state["revision"]:
            raise RevisionConflictError(
                f"revision_conflict: expected={expected_revision}, actual={state['revision']}"
            )
        life_frame = build_life_frame_read_view(state)
        fg_ref = state.get("foreground_activity_ref")
        act = state["activities"].get(fg_ref) if fg_ref else None
        return CommandResult(
            committed=False,
            idempotent_replay=False,
            noop=True,
            revision=state["revision"],
            life_state=life_frame["life_state"],
            foreground_activity_ref=fg_ref,
            activity=copy.deepcopy(act),
            transition=None,
            life_frame=life_frame,
        )

    def start_activity(
        self,
        *,
        activity_kind: str,
        title: str,
        origin_ref: str,
        source_refs: Sequence[str],
        expected_revision: int,
        idempotency_key: str,
        decision_ref: Optional[str] = None,
        adoption_ref: Optional[str] = None,
        subject_id: str = GLOBAL_SUBJECT_ID,
        subject_refs: Optional[Sequence[str]] = None,
        interruptibility: str = INTERRUPTIBILITY_LIGHT,
        checkpoint_ref: Optional[str] = None,
        last_action_ref: Optional[str] = None,
        causal_parent_refs: Optional[Sequence[str]] = None,
        reason_code: str = "ADOPTED_DECISION",
        occurred_at: Optional[str] = None,
        observed_at: Optional[str] = None,
        enqueued_at: Optional[str] = None,
        activity_id: Optional[str] = None,
        allow_background_waiting: bool = False,
        extra_fields: Optional[Mapping[str, Any]] = None,
        fault_hook: Optional[Callable[[str], None]] = None,
    ) -> CommandResult:
        validate_no_forbidden_self_truth(extra_fields)
        canon_subject = validate_subject_id(subject_id)
        if not isinstance(activity_kind, str) or not activity_kind.strip():
            raise ContractViolationError("activity_kind must be a non-empty string")
        if not isinstance(title, str) or not title.strip():
            raise ContractViolationError("title must be a non-empty string")
        if interruptibility not in INTERRUPTIBILITY_LEVELS:
            raise ContractViolationError(f"invalid interruptibility={interruptibility!r}")

        norm_dec, norm_adp = validate_adoption_provenance(
            decision_ref=decision_ref,
            adoption_ref=adoption_ref,
            transition_type=TRANSITION_START,
        )
        norm_origin = _validate_structured_ref(origin_ref, "origin_ref")
        norm_sources = normalize_ref_list(source_refs, "source_refs", allow_empty=False)
        norm_subject_refs = normalize_ref_list(subject_refs or [], "subject_refs", allow_empty=True)
        norm_parents = normalize_ref_list(causal_parent_refs or [], "causal_parent_refs", allow_empty=True)
        ext_refs = validate_optional_external_refs(
            checkpoint_ref=checkpoint_ref,
            last_action_ref=last_action_ref,
        )
        norm_reason = normalize_reason_code(reason_code, default="ADOPTED_DECISION")

        with self._store._store_io_lock(shared=False):
            state, journal = self._store._load_verified_unlocked(_include_journal=True, _copy_journal=False)

            replay_hit = self._check_idempotency(
                journal,
                state,
                idempotency_key=idempotency_key,
                transition_type=TRANSITION_START,
                target_activity_id=activity_id,
                activity_kind=activity_kind.strip(),
            )
            if replay_hit is not None:
                return replay_hit

            if expected_revision != state["revision"]:
                raise RevisionConflictError(
                    f"revision_conflict on START: expected={expected_revision}, actual={state['revision']}"
                )

            current_fg_id = state.get("foreground_activity_ref")
            if current_fg_id is not None:
                current_fg_act = state["activities"].get(current_fg_id)
                if (
                    not allow_background_waiting
                    or current_fg_act is None
                    or current_fg_act.get("status") != STATUS_WAITING
                ):
                    raise ForegroundConflictError(
                        f"foreground_conflict: cannot START while foreground_activity_ref="
                        f"{current_fg_id!r} is active/paused/waiting"
                    )

            new_act_id = (activity_id or f"actv_{uuid.uuid4().hex[:16]}").strip()
            if new_act_id in state["activities"]:
                raise ActivityTransitionError(f"activity_id={new_act_id!r} already exists")

            rec_stamp = _iso(now())
            occ_stamp = _iso(_parse(occurred_at) or now()) if occurred_at else rec_stamp
            obs_stamp = _iso(_parse(observed_at)) if observed_at else occ_stamp
            enq_stamp = _iso(_parse(enqueued_at)) if enqueued_at else obs_stamp

            result_rev = state["revision"] + 1
            trn_id = f"trn_{uuid.uuid4().hex[:16]}"

            instance: dict[str, Any] = {
                "activity_id": new_act_id,
                "schema_version": SCHEMA_ACTIVITY_INSTANCE,
                "revision": result_rev,
                "subject_id": canon_subject,
                "activity_kind": activity_kind.strip()[:80],
                "title": _compact_value(title.strip(), 200),
                "subject_refs": norm_subject_refs,
                "status": STATUS_ACTIVE,
                "interruptibility": interruptibility,
                "origin_ref": norm_origin,
                "decision_ref": norm_dec,
                "adoption_ref": norm_adp,
                "started_at": occ_stamp,
                "updated_at": occ_stamp,
                "paused_at": None,
                "waiting_since": None,
                "ended_at": None,
                "waiting_on_ref": None,
                "resume_condition_ref": None,
                "checkpoint_ref": ext_refs["checkpoint_ref"],
                "last_action_ref": ext_refs["last_action_ref"],
                "completion_evidence_ref": None,
                "last_transition_ref": trn_id,
                "terminal_reason_code": None,
                "terminal_source_ref": None,
                "source_refs": norm_sources,
            }

            transition_unsigned: dict[str, Any] = {
                "schema_version": SCHEMA_LIFE_TRANSITION,
                "transition_id": trn_id,
                "activity_id": new_act_id,
                "subject_id": canon_subject,
                "transition_type": TRANSITION_START,
                "from_status": LIFE_STATE_IDLE,
                "to_status": STATUS_ACTIVE,
                "expected_revision": expected_revision,
                "result_revision": result_rev,
                "source_refs": norm_sources,
                "causal_parent_refs": norm_parents,
                "decision_ref": norm_dec,
                "adoption_ref": norm_adp,
                "occurred_at": occ_stamp,
                "observed_at": obs_stamp,
                "recorded_at": rec_stamp,
                "enqueued_at": enq_stamp,
                "reason_code": norm_reason,
                "writer_domain": self._capability.writer_domain,
                "idempotency_key": idempotency_key.strip(),
                "activity_snapshot": copy.deepcopy(instance),
            }

            new_state = copy.deepcopy(state)
            if current_fg_id is not None and current_fg_id in new_state["activities"]:
                new_state["activities"][current_fg_id]["foreground_released"] = True
            new_state["revision"] = result_rev
            new_state["foreground_activity_ref"] = new_act_id
            new_state["updated_at"] = occ_stamp
            new_state["activities"][new_act_id] = instance

            permit = self._mint_admission_permit(transition_unsigned)
            committed_state, committed_trn = self._store.commit_transition(
                lease=self._lease,
                capability=self._capability,
                new_state=new_state,
                new_transition_unsigned=transition_unsigned,
                journal_entries=journal,
                permit=permit,
                fault_hook=fault_hook,
            )
            life_frame = build_life_frame_read_view(committed_state)
            return CommandResult(
                committed=True,
                idempotent_replay=False,
                noop=False,
                revision=committed_state["revision"],
                life_state=life_frame["life_state"],
                foreground_activity_ref=committed_state["foreground_activity_ref"],
                activity=copy.deepcopy(instance),
                transition=committed_trn,
                life_frame=life_frame,
                commit_receipt=self._build_commit_receipt(
                    transition=committed_trn,
                    previous_revision=expected_revision,
                    new_revision=int(committed_state["revision"]),
                ),
            )

    def _mutate_existing_activity(
        self,
        *,
        activity_id: str,
        transition_type: str,
        allowed_from_statuses: Sequence[str],
        to_status: str,
        expected_revision: int,
        idempotency_key: str,
        source_refs: Sequence[str],
        causal_parent_refs: Optional[Sequence[str]] = None,
        decision_ref: Optional[str] = None,
        adoption_ref: Optional[str] = None,
        waiting_on_ref: Optional[str] = None,
        resume_condition_ref: Optional[str] = None,
        waiting_episode_id: Optional[str] = None,
        release_foreground: bool = False,
        allow_background_waiting: bool = False,
        checkpoint_ref: Optional[str] = None,
        last_action_ref: Optional[str] = None,
        completion_evidence_ref: Optional[str] = None,
        reason_code: str = "UNKNOWN",
        occurred_at: Optional[str] = None,
        observed_at: Optional[str] = None,
        enqueued_at: Optional[str] = None,
        extra_fields: Optional[Mapping[str, Any]] = None,
        fault_hook: Optional[Callable[[str], None]] = None,
        consequence_capability: Optional[Any] = None,
        proposal_ref: Optional[str] = None,
        authority_reason_kind: Optional[str] = None,
    ) -> CommandResult:
        validate_no_forbidden_self_truth(extra_fields)
        if not isinstance(activity_id, str) or not activity_id.strip():
            raise ContractViolationError("activity_id must be a non-empty string")
        act_id = activity_id.strip()

        norm_dec: Optional[str] = None
        norm_adp: Optional[str] = None
        if transition_type == TRANSITION_RESUME:
            norm_dec, norm_adp = validate_adoption_provenance(
                decision_ref=decision_ref,
                adoption_ref=adoption_ref,
                transition_type=TRANSITION_RESUME,
            )
        else:
            if decision_ref is not None:
                norm_dec = _validate_structured_ref(
                    decision_ref,
                    "decision_ref",
                    allowed_prefixes=VALID_DECISION_PREFIXES,
                )
            if adoption_ref is not None:
                norm_adp = _validate_structured_ref(
                    adoption_ref,
                    "adoption_ref",
                    allowed_prefixes=VALID_ADOPTION_PREFIXES,
                )

        if transition_type == TRANSITION_COMPLETE:
            validate_completion_evidence_ref(completion_evidence_ref)

        if transition_type == TRANSITION_WAIT and not waiting_on_ref:
            raise ContractViolationError("WAIT transition requires a structured waiting_on_ref")

        norm_episode_id: Optional[str] = None
        if waiting_episode_id is not None:
            norm_episode_id = _validate_structured_ref(
                waiting_episode_id,
                "waiting_episode_id",
                allowed_prefixes=frozenset({"wep", "wait_ep"}),
            )

        ext_refs = validate_optional_external_refs(
            checkpoint_ref=checkpoint_ref,
            last_action_ref=last_action_ref,
            completion_evidence_ref=completion_evidence_ref,
            waiting_on_ref=waiting_on_ref,
            resume_condition_ref=resume_condition_ref,
        )
        norm_sources = normalize_ref_list(source_refs, "source_refs", allow_empty=False)
        norm_parents = normalize_ref_list(causal_parent_refs or [], "causal_parent_refs", allow_empty=True)
        norm_reason = normalize_reason_code(reason_code)

        with self._store._store_io_lock(shared=False):
            state, journal = self._store._load_verified_unlocked(_include_journal=True, _copy_journal=False)

            replay_hit = self._check_idempotency(
                journal,
                state,
                idempotency_key=idempotency_key,
                transition_type=transition_type,
                target_activity_id=act_id,
            )
            if replay_hit is not None:
                return replay_hit

            if expected_revision != state["revision"]:
                raise RevisionConflictError(
                    f"revision_conflict on {transition_type}: "
                    f"expected={expected_revision}, actual={state['revision']}"
                )

            current_act = state["activities"].get(act_id)
            if current_act is None:
                raise ActivityTransitionError(f"activity_id={act_id!r} not found")

            from_status = current_act["status"]
            if from_status in TERMINAL_ACTIVITY_STATUSES:
                raise TerminalActivityImmutableError(
                    f"terminal_activity_immutable: activity {act_id!r} is in terminal status "
                    f"{from_status!r} and cannot execute {transition_type}"
                )

            if from_status not in allowed_from_statuses:
                raise ActivityTransitionError(
                    f"illegal transition {transition_type} from status={from_status!r}; "
                    f"allowed from={list(allowed_from_statuses)}"
                )

            fg_id = state.get("foreground_activity_ref")
            if fg_id != act_id:
                if transition_type == TRANSITION_RESUME:
                    if fg_id is not None:
                        fg_act = state["activities"].get(fg_id)
                        if (
                            not allow_background_waiting
                            or fg_act is None
                            or fg_act.get("status") != STATUS_WAITING
                        ):
                            raise ForegroundConflictError(
                                f"foreground_conflict: cannot RESUME {act_id!r} while foreground_activity_ref="
                                f"{fg_id!r} is active or paused"
                            )
                elif transition_type in (
                    TRANSITION_COMPLETE,
                    TRANSITION_ABANDON,
                    TRANSITION_CANCEL,
                    TRANSITION_EXPIRE,
                    TRANSITION_CHECKPOINT,
                    TRANSITION_PROGRESS,
                ):
                    if not (
                        from_status == STATUS_WAITING
                        and current_act.get("foreground_released") is True
                    ):
                        raise ForegroundConflictError(
                            f"activity {act_id!r} is not the current foreground activity ({fg_id!r})"
                        )
                else:
                    raise ForegroundConflictError(
                        f"activity {act_id!r} is not the current foreground activity ({fg_id!r})"
                    )

            rec_stamp = _iso(now())
            occ_stamp = _iso(_parse(occurred_at) or now()) if occurred_at else rec_stamp
            obs_stamp = _iso(_parse(observed_at)) if observed_at else occ_stamp
            enq_stamp = _iso(_parse(enqueued_at)) if enqueued_at else obs_stamp

            effective_to_status = (
                from_status
                if transition_type in (TRANSITION_CHECKPOINT, TRANSITION_PROGRESS)
                else to_status
            )

            result_rev = state["revision"] + 1
            trn_id = f"trn_{uuid.uuid4().hex[:16]}"

            updated_act = copy.deepcopy(current_act)
            updated_act["revision"] = result_rev
            updated_act["status"] = effective_to_status
            updated_act["updated_at"] = occ_stamp
            updated_act["last_transition_ref"] = trn_id

            for src in norm_sources:
                if src not in updated_act["source_refs"]:
                    updated_act["source_refs"].append(src)

            if norm_dec is not None:
                updated_act["decision_ref"] = norm_dec
            if norm_adp is not None:
                updated_act["adoption_ref"] = norm_adp
            if ext_refs["checkpoint_ref"] is not None:
                updated_act["checkpoint_ref"] = ext_refs["checkpoint_ref"]
            if ext_refs["last_action_ref"] is not None:
                updated_act["last_action_ref"] = ext_refs["last_action_ref"]

            if transition_type in (TRANSITION_CHECKPOINT, TRANSITION_PROGRESS):
                # Non-lifecycle progress/checkpoint update: preserve existing status and waiting/paused fields
                pass
            elif effective_to_status == STATUS_PAUSED:
                updated_act["paused_at"] = occ_stamp
                updated_act["waiting_since"] = None
                updated_act["waiting_on_ref"] = None
                updated_act["resume_condition_ref"] = None
                updated_act.pop("waiting_episode_id", None)
                updated_act.pop("foreground_released", None)
            elif effective_to_status == STATUS_WAITING:
                updated_act["waiting_since"] = occ_stamp
                updated_act["waiting_on_ref"] = ext_refs["waiting_on_ref"]
                updated_act["resume_condition_ref"] = ext_refs["resume_condition_ref"]
                if norm_episode_id is not None:
                    updated_act["waiting_episode_id"] = norm_episode_id
                if release_foreground:
                    updated_act["foreground_released"] = True
                else:
                    updated_act.pop("foreground_released", None)
            elif effective_to_status == STATUS_ACTIVE:
                updated_act["paused_at"] = None
                updated_act["waiting_since"] = None
                updated_act["waiting_on_ref"] = None
                updated_act["resume_condition_ref"] = None
                updated_act.pop("waiting_episode_id", None)
                updated_act.pop("foreground_released", None)
            elif effective_to_status in TERMINAL_ACTIVITY_STATUSES:
                updated_act["ended_at"] = occ_stamp
                updated_act["waiting_on_ref"] = None
                updated_act["resume_condition_ref"] = None
                updated_act.pop("waiting_episode_id", None)
                updated_act.pop("foreground_released", None)
                updated_act["terminal_reason_code"] = norm_reason
                updated_act["terminal_source_ref"] = norm_sources[0]
                if effective_to_status == STATUS_COMPLETED:
                    updated_act["completion_evidence_ref"] = ext_refs["completion_evidence_ref"]

            transition_unsigned: dict[str, Any] = {
                "schema_version": SCHEMA_LIFE_TRANSITION,
                "transition_id": trn_id,
                "activity_id": act_id,
                "subject_id": GLOBAL_SUBJECT_ID,
                "transition_type": transition_type,
                "from_status": from_status,
                "to_status": effective_to_status,
                "expected_revision": expected_revision,
                "result_revision": result_rev,
                "source_refs": norm_sources,
                "causal_parent_refs": norm_parents,
                "decision_ref": norm_dec,
                "adoption_ref": norm_adp,
                "occurred_at": occ_stamp,
                "observed_at": obs_stamp,
                "recorded_at": rec_stamp,
                "enqueued_at": enq_stamp,
                "reason_code": norm_reason,
                "writer_domain": self._capability.writer_domain,
                "idempotency_key": idempotency_key.strip(),
                "activity_snapshot": copy.deepcopy(updated_act),
            }
            if proposal_ref is not None:
                transition_unsigned["proposal_ref"] = proposal_ref
            if authority_reason_kind is not None:
                transition_unsigned["authority_reason_kind"] = authority_reason_kind

            new_state = copy.deepcopy(state)
            new_state["revision"] = result_rev
            new_state["updated_at"] = occ_stamp
            new_state["activities"][act_id] = updated_act

            if transition_type in (TRANSITION_CHECKPOINT, TRANSITION_PROGRESS):
                new_state["foreground_activity_ref"] = fg_id
            elif effective_to_status in TERMINAL_ACTIVITY_STATUSES:
                if fg_id == act_id:
                    new_state["foreground_activity_ref"] = None
            elif effective_to_status == STATUS_WAITING and release_foreground:
                if fg_id == act_id:
                    new_state["foreground_activity_ref"] = None
            else:
                if fg_id is not None and fg_id != act_id and fg_id in new_state["activities"]:
                    new_state["activities"][fg_id]["foreground_released"] = True
                new_state["foreground_activity_ref"] = act_id

            permit = self._mint_admission_permit(
                transition_unsigned,
                consequence_capability=consequence_capability,
                canonical_state=state,
            )
            committed_state, committed_trn = self._store.commit_transition(
                lease=self._lease,
                capability=self._capability,
                new_state=new_state,
                new_transition_unsigned=transition_unsigned,
                journal_entries=journal,
                permit=permit,
                fault_hook=fault_hook,
            )
            life_frame = build_life_frame_read_view(committed_state)
            return CommandResult(
                committed=True,
                idempotent_replay=False,
                noop=False,
                revision=committed_state["revision"],
                life_state=life_frame["life_state"],
                foreground_activity_ref=committed_state["foreground_activity_ref"],
                activity=copy.deepcopy(updated_act),
                transition=committed_trn,
                life_frame=life_frame,
                commit_receipt=self._build_commit_receipt(
                    transition=committed_trn,
                    previous_revision=expected_revision,
                    new_revision=int(committed_state["revision"]),
                ),
            )

    def pause_activity(
        self,
        *,
        activity_id: str,
        expected_revision: int,
        idempotency_key: str,
        source_refs: Sequence[str],
        decision_ref: Optional[str] = None,
        adoption_ref: Optional[str] = None,
        checkpoint_ref: Optional[str] = None,
        last_action_ref: Optional[str] = None,
        causal_parent_refs: Optional[Sequence[str]] = None,
        reason_code: str = "VOLUNTARY_PAUSE",
        occurred_at: Optional[str] = None,
        observed_at: Optional[str] = None,
        enqueued_at: Optional[str] = None,
        extra_fields: Optional[Mapping[str, Any]] = None,
        fault_hook: Optional[Callable[[str], None]] = None,
        consequence_capability: Optional[Any] = None,
        proposal_ref: Optional[str] = None,
        authority_reason_kind: Optional[str] = None,
    ) -> CommandResult:
        return self._mutate_existing_activity(
            activity_id=activity_id,
            transition_type=TRANSITION_PAUSE,
            allowed_from_statuses=(STATUS_ACTIVE,),
            to_status=STATUS_PAUSED,
            expected_revision=expected_revision,
            idempotency_key=idempotency_key,
            source_refs=source_refs,
            causal_parent_refs=causal_parent_refs,
            decision_ref=decision_ref,
            adoption_ref=adoption_ref,
            checkpoint_ref=checkpoint_ref,
            last_action_ref=last_action_ref,
            reason_code=reason_code,
            occurred_at=occurred_at,
            observed_at=observed_at,
            enqueued_at=enqueued_at,
            extra_fields=extra_fields,
            fault_hook=fault_hook,
            consequence_capability=consequence_capability,
            proposal_ref=proposal_ref,
            authority_reason_kind=authority_reason_kind,
        )

    def wait_activity(
        self,
        *,
        activity_id: str,
        waiting_on_ref: str,
        expected_revision: int,
        idempotency_key: str,
        source_refs: Sequence[str],
        resume_condition_ref: Optional[str] = None,
        waiting_episode_id: Optional[str] = None,
        release_foreground: bool = False,
        decision_ref: Optional[str] = None,
        adoption_ref: Optional[str] = None,
        checkpoint_ref: Optional[str] = None,
        last_action_ref: Optional[str] = None,
        causal_parent_refs: Optional[Sequence[str]] = None,
        reason_code: str = "EXTERNAL_DEPENDENCY_WAIT",
        occurred_at: Optional[str] = None,
        observed_at: Optional[str] = None,
        enqueued_at: Optional[str] = None,
        extra_fields: Optional[Mapping[str, Any]] = None,
        fault_hook: Optional[Callable[[str], None]] = None,
        consequence_capability: Optional[Any] = None,
        proposal_ref: Optional[str] = None,
        authority_reason_kind: Optional[str] = None,
    ) -> CommandResult:
        return self._mutate_existing_activity(
            activity_id=activity_id,
            transition_type=TRANSITION_WAIT,
            allowed_from_statuses=(STATUS_ACTIVE, STATUS_PAUSED),
            to_status=STATUS_WAITING,
            expected_revision=expected_revision,
            idempotency_key=idempotency_key,
            source_refs=source_refs,
            causal_parent_refs=causal_parent_refs,
            decision_ref=decision_ref,
            adoption_ref=adoption_ref,
            waiting_on_ref=waiting_on_ref,
            resume_condition_ref=resume_condition_ref,
            waiting_episode_id=waiting_episode_id,
            release_foreground=release_foreground,
            checkpoint_ref=checkpoint_ref,
            last_action_ref=last_action_ref,
            reason_code=reason_code,
            occurred_at=occurred_at,
            observed_at=observed_at,
            enqueued_at=enqueued_at,
            extra_fields=extra_fields,
            fault_hook=fault_hook,
            consequence_capability=consequence_capability,
            proposal_ref=proposal_ref,
            authority_reason_kind=authority_reason_kind,
        )

    def resume_activity(
        self,
        *,
        activity_id: str,
        expected_revision: int,
        idempotency_key: str,
        source_refs: Sequence[str],
        decision_ref: Optional[str] = None,
        adoption_ref: Optional[str] = None,
        allow_background_waiting: bool = False,
        checkpoint_ref: Optional[str] = None,
        last_action_ref: Optional[str] = None,
        causal_parent_refs: Optional[Sequence[str]] = None,
        reason_code: str = "RESUMED_BY_DECISION",
        occurred_at: Optional[str] = None,
        observed_at: Optional[str] = None,
        enqueued_at: Optional[str] = None,
        extra_fields: Optional[Mapping[str, Any]] = None,
        fault_hook: Optional[Callable[[str], None]] = None,
        consequence_capability: Optional[Any] = None,
        proposal_ref: Optional[str] = None,
        authority_reason_kind: Optional[str] = None,
    ) -> CommandResult:
        return self._mutate_existing_activity(
            activity_id=activity_id,
            transition_type=TRANSITION_RESUME,
            allowed_from_statuses=(STATUS_PAUSED, STATUS_WAITING),
            to_status=STATUS_ACTIVE,
            expected_revision=expected_revision,
            idempotency_key=idempotency_key,
            source_refs=source_refs,
            causal_parent_refs=causal_parent_refs,
            decision_ref=decision_ref,
            adoption_ref=adoption_ref,
            allow_background_waiting=allow_background_waiting,
            checkpoint_ref=checkpoint_ref,
            last_action_ref=last_action_ref,
            reason_code=reason_code,
            occurred_at=occurred_at,
            observed_at=observed_at,
            enqueued_at=enqueued_at,
            extra_fields=extra_fields,
            fault_hook=fault_hook,
            consequence_capability=consequence_capability,
            proposal_ref=proposal_ref,
            authority_reason_kind=authority_reason_kind,
        )

    def complete_activity(
        self,
        *,
        activity_id: str,
        completion_evidence_ref: str,
        expected_revision: int,
        idempotency_key: str,
        source_refs: Sequence[str],
        decision_ref: Optional[str] = None,
        adoption_ref: Optional[str] = None,
        checkpoint_ref: Optional[str] = None,
        last_action_ref: Optional[str] = None,
        causal_parent_refs: Optional[Sequence[str]] = None,
        reason_code: str = "COMPLETED_WITH_EVIDENCE",
        occurred_at: Optional[str] = None,
        observed_at: Optional[str] = None,
        enqueued_at: Optional[str] = None,
        extra_fields: Optional[Mapping[str, Any]] = None,
        fault_hook: Optional[Callable[[str], None]] = None,
        consequence_capability: Optional[Any] = None,
        proposal_ref: Optional[str] = None,
        authority_reason_kind: Optional[str] = None,
    ) -> CommandResult:
        return self._mutate_existing_activity(
            activity_id=activity_id,
            transition_type=TRANSITION_COMPLETE,
            allowed_from_statuses=(STATUS_ACTIVE, STATUS_PAUSED, STATUS_WAITING),
            to_status=STATUS_COMPLETED,
            expected_revision=expected_revision,
            idempotency_key=idempotency_key,
            source_refs=source_refs,
            causal_parent_refs=causal_parent_refs,
            decision_ref=decision_ref,
            adoption_ref=adoption_ref,
            checkpoint_ref=checkpoint_ref,
            last_action_ref=last_action_ref,
            completion_evidence_ref=completion_evidence_ref,
            reason_code=reason_code,
            occurred_at=occurred_at,
            observed_at=observed_at,
            enqueued_at=enqueued_at,
            extra_fields=extra_fields,
            fault_hook=fault_hook,
            consequence_capability=consequence_capability,
            proposal_ref=proposal_ref,
            authority_reason_kind=authority_reason_kind,
        )

    def abandon_activity(
        self,
        *,
        activity_id: str,
        expected_revision: int,
        idempotency_key: str,
        source_refs: Sequence[str],
        decision_ref: Optional[str] = None,
        adoption_ref: Optional[str] = None,
        checkpoint_ref: Optional[str] = None,
        last_action_ref: Optional[str] = None,
        causal_parent_refs: Optional[Sequence[str]] = None,
        reason_code: str = "SPONTANEOUS_RECONSIDERATION",
        occurred_at: Optional[str] = None,
        observed_at: Optional[str] = None,
        enqueued_at: Optional[str] = None,
        extra_fields: Optional[Mapping[str, Any]] = None,
        fault_hook: Optional[Callable[[str], None]] = None,
        consequence_capability: Optional[Any] = None,
        proposal_ref: Optional[str] = None,
        authority_reason_kind: Optional[str] = None,
    ) -> CommandResult:
        return self._mutate_existing_activity(
            activity_id=activity_id,
            transition_type=TRANSITION_ABANDON,
            allowed_from_statuses=(STATUS_ACTIVE, STATUS_PAUSED, STATUS_WAITING),
            to_status=STATUS_ABANDONED,
            expected_revision=expected_revision,
            idempotency_key=idempotency_key,
            source_refs=source_refs,
            causal_parent_refs=causal_parent_refs,
            decision_ref=decision_ref,
            adoption_ref=adoption_ref,
            checkpoint_ref=checkpoint_ref,
            last_action_ref=last_action_ref,
            reason_code=reason_code,
            occurred_at=occurred_at,
            observed_at=observed_at,
            enqueued_at=enqueued_at,
            extra_fields=extra_fields,
            fault_hook=fault_hook,
            consequence_capability=consequence_capability,
            proposal_ref=proposal_ref,
            authority_reason_kind=authority_reason_kind,
        )

    def cancel_activity(
        self,
        *,
        activity_id: str,
        expected_revision: int,
        idempotency_key: str,
        source_refs: Sequence[str],
        decision_ref: Optional[str] = None,
        adoption_ref: Optional[str] = None,
        checkpoint_ref: Optional[str] = None,
        last_action_ref: Optional[str] = None,
        causal_parent_refs: Optional[Sequence[str]] = None,
        reason_code: str = "CANCELLED_BY_DECISION",
        occurred_at: Optional[str] = None,
        observed_at: Optional[str] = None,
        enqueued_at: Optional[str] = None,
        extra_fields: Optional[Mapping[str, Any]] = None,
        fault_hook: Optional[Callable[[str], None]] = None,
        consequence_capability: Optional[Any] = None,
        proposal_ref: Optional[str] = None,
        authority_reason_kind: Optional[str] = None,
    ) -> CommandResult:
        return self._mutate_existing_activity(
            activity_id=activity_id,
            transition_type=TRANSITION_CANCEL,
            allowed_from_statuses=(STATUS_ACTIVE, STATUS_PAUSED, STATUS_WAITING),
            to_status=STATUS_CANCELLED,
            expected_revision=expected_revision,
            idempotency_key=idempotency_key,
            source_refs=source_refs,
            causal_parent_refs=causal_parent_refs,
            decision_ref=decision_ref,
            adoption_ref=adoption_ref,
            checkpoint_ref=checkpoint_ref,
            last_action_ref=last_action_ref,
            reason_code=reason_code,
            occurred_at=occurred_at,
            observed_at=observed_at,
            enqueued_at=enqueued_at,
            extra_fields=extra_fields,
            fault_hook=fault_hook,
            consequence_capability=consequence_capability,
            proposal_ref=proposal_ref,
            authority_reason_kind=authority_reason_kind,
        )

    def expire_activity(
        self,
        *,
        activity_id: str,
        expected_revision: int,
        idempotency_key: str,
        source_refs: Sequence[str],
        decision_ref: Optional[str] = None,
        adoption_ref: Optional[str] = None,
        checkpoint_ref: Optional[str] = None,
        last_action_ref: Optional[str] = None,
        causal_parent_refs: Optional[Sequence[str]] = None,
        reason_code: str = "EXPIRED_BY_TIMEOUT",
        occurred_at: Optional[str] = None,
        observed_at: Optional[str] = None,
        enqueued_at: Optional[str] = None,
        extra_fields: Optional[Mapping[str, Any]] = None,
        fault_hook: Optional[Callable[[str], None]] = None,
        consequence_capability: Optional[Any] = None,
        proposal_ref: Optional[str] = None,
        authority_reason_kind: Optional[str] = None,
    ) -> CommandResult:
        return self._mutate_existing_activity(
            activity_id=activity_id,
            transition_type=TRANSITION_EXPIRE,
            allowed_from_statuses=(STATUS_ACTIVE, STATUS_PAUSED, STATUS_WAITING),
            to_status=STATUS_EXPIRED,
            expected_revision=expected_revision,
            idempotency_key=idempotency_key,
            source_refs=source_refs,
            causal_parent_refs=causal_parent_refs,
            decision_ref=decision_ref,
            adoption_ref=adoption_ref,
            checkpoint_ref=checkpoint_ref,
            last_action_ref=last_action_ref,
            reason_code=reason_code,
            occurred_at=occurred_at,
            observed_at=observed_at,
            enqueued_at=enqueued_at,
            extra_fields=extra_fields,
            fault_hook=fault_hook,
            consequence_capability=consequence_capability,
            proposal_ref=proposal_ref,
            authority_reason_kind=authority_reason_kind,
        )

    def update_checkpoint(
        self,
        *,
        activity_id: str,
        checkpoint_ref: str,
        expected_revision: int,
        idempotency_key: str,
        source_refs: Sequence[str],
        last_action_ref: Optional[str] = None,
        causal_parent_refs: Optional[Sequence[str]] = None,
        reason_code: str = "CHECKPOINT_ADVANCED",
        occurred_at: Optional[str] = None,
        observed_at: Optional[str] = None,
        enqueued_at: Optional[str] = None,
        extra_fields: Optional[Mapping[str, Any]] = None,
        fault_hook: Optional[Callable[[str], None]] = None,
        consequence_capability: Optional[Any] = None,
        proposal_ref: Optional[str] = None,
        authority_reason_kind: Optional[str] = None,
    ) -> CommandResult:
        """Advance or update Activity.checkpoint_ref (and last_action_ref) under Life Authority without changing lifecycle status."""
        if checkpoint_ref is None or (isinstance(checkpoint_ref, str) and not checkpoint_ref.strip()):
            raise ContractViolationError("update_checkpoint requires a non-empty structured checkpoint_ref")
        return self._mutate_existing_activity(
            activity_id=activity_id,
            transition_type=TRANSITION_CHECKPOINT,
            allowed_from_statuses=(STATUS_ACTIVE, STATUS_PAUSED, STATUS_WAITING),
            to_status=STATUS_ACTIVE,  # Dynamically resolved to from_status in _mutate_existing_activity
            expected_revision=expected_revision,
            idempotency_key=idempotency_key,
            source_refs=source_refs,
            causal_parent_refs=causal_parent_refs,
            checkpoint_ref=checkpoint_ref,
            last_action_ref=last_action_ref,
            reason_code=reason_code,
            occurred_at=occurred_at,
            observed_at=observed_at,
            enqueued_at=enqueued_at,
            extra_fields=extra_fields,
            fault_hook=fault_hook,
            consequence_capability=consequence_capability,
            proposal_ref=proposal_ref,
            authority_reason_kind=authority_reason_kind,
        )


# ---------------------------------------------------------------------------
# Deterministic Replay Runtime & Shadow Sandbox (Sections 24, 51, 55)
# ---------------------------------------------------------------------------


class ReplayRuntime:
    """Deterministic, zero-side-effect transition replay engine.

    Never writes to canonical production storage.
    """

    @staticmethod
    def project_from_transitions(transitions: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        state = _empty_canonical_state()
        expected_rev = 1
        prev_hash = GENESIS_HASH

        for entry in transitions:
            if entry.get("expected_revision") != expected_rev - 1 or entry.get("result_revision") != expected_rev:
                raise RecoveryRequiredError("REPLAY_BAD_REVISION", "invalid revision sequence during replay")
            if entry.get("prev_hash") != prev_hash:
                raise RecoveryRequiredError("REPLAY_BAD_HASH_CHAIN", "invalid hash chain during replay")

            act_id = entry["activity_id"]
            to_status = entry["to_status"]
            trn_type = entry.get("transition_type")
            snap = copy.deepcopy(dict(entry["activity_snapshot"]))
            state["activities"][act_id] = snap
            state["revision"] = expected_rev
            state["last_transition_ref"] = entry["transition_id"]
            state["last_transition_hash"] = entry["entry_hash"]
            state["updated_at"] = entry["occurred_at"]

            prev_fg = state["foreground_activity_ref"]
            if trn_type in (TRANSITION_CHECKPOINT, TRANSITION_PROGRESS):
                state["foreground_activity_ref"] = prev_fg
            elif to_status in TERMINAL_ACTIVITY_STATUSES:
                if prev_fg == act_id:
                    state["foreground_activity_ref"] = None
            elif to_status == STATUS_WAITING and snap.get("foreground_released") is True:
                if prev_fg == act_id:
                    state["foreground_activity_ref"] = None
            elif to_status in OPEN_ACTIVITY_STATUSES:
                if prev_fg is not None and prev_fg != act_id and prev_fg in state["activities"]:
                    state["activities"][prev_fg]["foreground_released"] = True
                state["foreground_activity_ref"] = act_id

            prev_hash = entry["entry_hash"]
            expected_rev += 1

        return state

    @classmethod
    def verify_store_replay_consistency(cls, store: CanonicalActivityStore) -> dict[str, Any]:
        canonical_state, journal = store.load_verified_state_and_journal()
        replayed_state = cls.project_from_transitions(journal)
        matches = _projections_equivalent(canonical_state, replayed_state)
        replayed_view = build_life_frame_read_view(replayed_state)
        canonical_view = build_life_frame_read_view(canonical_state)
        return {
            "consistent": matches and (replayed_view == canonical_view),
            "revision": canonical_state["revision"],
            "journal_count": len(journal),
            "replayed_state": replayed_state,
            "replayed_life_frame": replayed_view,
        }


class IsolatedSandboxRuntime:
    """Shadow / Replay / Twin sandbox runtime that is physically isolated from canonical store."""

    def __init__(self, sandbox_root: Path | str, *, mode: str = "shadow"):
        if mode not in NON_PRODUCTION_NAMESPACES:
            raise ContractViolationError(f"invalid sandbox mode={mode!r}")
        resolved = Path(sandbox_root).expanduser().resolve()
        if is_production_path(resolved):
            raise WriterCapabilityError("sandbox_root cannot reside inside a production home")
        self.mode = mode
        self.sandbox_root = resolved
        self._state = _empty_canonical_state()
        self._transitions: list[dict[str, Any]] = []

    def simulate_transition_in_memory(
        self,
        *,
        activity_id: str,
        transition_type: str,
        to_status: str,
    ) -> dict[str, Any]:
        """Run an isolated in-memory shadow/replay step without touching canonical files."""
        rev = self._state["revision"] + 1
        self._state["revision"] = rev
        self._state["foreground_activity_ref"] = (
            activity_id if to_status in OPEN_ACTIVITY_STATUSES else None
        )
        self._transitions.append(
            {
                "sandbox_mode": self.mode,
                "activity_id": activity_id,
                "transition_type": transition_type,
                "to_status": to_status,
                "revision": rev,
            }
        )
        return copy.deepcopy(self._state)


# ---------------------------------------------------------------------------
# Read-Only Legacy State Importer / Migration Inspector (Sections 33, 34)
# ---------------------------------------------------------------------------


class LegacyActivityInspector:
    """Read-only inspector for legacy `current_activity.py` Schema v2 records."""

    STATUS_MAP = {
        "idle": (LIFE_STATE_IDLE, None),
        "active": (LIFE_STATE_ACTIVE, STATUS_ACTIVE),
        "paused": (LIFE_STATE_PAUSED, STATUS_PAUSED),
        "completed": (LIFE_STATE_IDLE, STATUS_COMPLETED),
        "abandoned": (LIFE_STATE_IDLE, STATUS_ABANDONED),
    }

    @classmethod
    def inspect_legacy_v2_dict(cls, data: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(data, Mapping):
            raise ContractViolationError("legacy v2 payload must be a mapping")

        raw_status = str(data.get("status") or "idle").strip().lower()
        if raw_status not in cls.STATUS_MAP:
            raise ContractViolationError(f"unsupported legacy v2 status={raw_status!r}")

        proposed_life_state, proposed_activity_status = cls.STATUS_MAP[raw_status]
        warnings: list[str] = []
        unsupported_fields: dict[str, str] = {}
        provenance_gaps: list[str] = []

        if data.get("goal"):
            unsupported_fields["goal"] = "EXTERNAL_UNTRUSTED_METADATA_CANDIDATE"
            warnings.append("legacy.goal cannot be promoted to canonical Activity truth")

        if "progress" in data or "active_seconds" in data or "duration_seconds" in data:
            unsupported_fields["progress_or_duration"] = "NOT_CANONICAL"
            warnings.append("legacy progress/active_seconds/duration is NOT_CANONICAL; progress = UNKNOWN")

        if data.get("note") or data.get("last_transition_detail"):
            unsupported_fields["free_text_checkpoint_or_note"] = "NOT_EVIDENCE"
            warnings.append("legacy free-text note/checkpoint is NOT_EVIDENCE; checkpoint_ref = null")

        if data.get("completion_criteria"):
            unsupported_fields["completion_criteria"] = "NOT_CANONICAL_IN_ACTIVITY"
            warnings.append("legacy.completion_criteria excluded from canonical ActivityInstance")

        if raw_status != "idle":
            provenance_gaps.append("missing_decision_or_adoption_ref")
            provenance_gaps.append("missing_structured_origin_ref")
            if raw_status == "completed":
                provenance_gaps.append("legacy_inline_evidence_requires_external_completion_evidence_ref")

        proposal = {
            "subject_id": GLOBAL_SUBJECT_ID,
            "proposed_life_state": proposed_life_state,
            "proposed_activity_status": proposed_activity_status,
            "legacy_activity_id": data.get("activity_id"),
            "activity_kind": str(data.get("kind") or "legacy_imported")[:80] if raw_status != "idle" else None,
            "title": str(data.get("activity") or "")[:200] if raw_status != "idle" else None,
            "checkpoint_ref": None,
            "last_action_ref": None,
            "completion_evidence_ref": None,
            "progress": "UNKNOWN",
            "can_auto_commit_to_canonical": False,
        }

        return {
            "schema_version": SCHEMA_LEGACY_MIGRATION_REPORT,
            "read_only_inspection": True,
            "legacy_schema_version": data.get("schema_version", 2),
            "legacy_status": raw_status,
            "migration_proposal": proposal,
            "warnings": warnings,
            "unsupported_fields": unsupported_fields,
            "required_provenance_gaps": provenance_gaps,
        }

    @classmethod
    def inspect_legacy_v2_file(cls, path: Path | str) -> dict[str, Any]:
        file_path = Path(path).expanduser().resolve()
        raw = json.loads(file_path.read_text(encoding="utf-8"))
        report = cls.inspect_legacy_v2_dict(raw)
        report["inspected_path"] = str(file_path)
        return report
