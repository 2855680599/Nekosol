"""CT0-10 canonical integration spine.

PORTABLE LAYER. This module must stay importable on BOTH the Windows host and the
Linux environment, because it is the shared vocabulary of the phase:

  * the versioned reference layer (order sections 11-19): a typed, version-aware
    reconciliation / settlement / action reference. `recon:` (v1) and `recon2:`
    (v2) are both accepted; an unknown scheme is REJECTED, never guessed. A prefix
    is only used for FORMAT DISPATCH - authority always comes from the canonical
    owner/store, never from the string.
  * the contact -> canonical identity mapping table (order sections 13, 40, 57, 59):
    the isolated contact contract and the real canonical runtime use DIFFERENT
    identity families. We translate explicitly and never rename one into the other.
  * the port protocols every adapter must satisfy, and the portable integration
    dataclasses.

It deliberately imports NO canonical runtime module: the real owners are Linux-only
(POSIX fcntl) and ship in this release under ``chiyo/life_runtime`` (they are copied
flat, and ``chiyo/life_runtime/scripts`` satisfies the loaders' layout contract).
Only the adapter modules under canonical_ports/ are allowed to import them.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence

SPINE_VERSION = "chiyo.ct0_10.canonical_spine.v1"

# ==========================================================================
# 1. isolated environment (never production)
# ==========================================================================
# Mirrors scripts/activity_continuity.py:187-207 (verified by reading the real
# source). Duplicated here ONLY so the portable layer can validate a store root
# without importing fcntl. The adapter layer must call the real
# activity_continuity.is_production_path() as the authoritative guard.
NAMESPACE_CANONICAL = "canonical"
NAMESPACE_ISOLATED_TEST = "isolated_canonical_test"
NON_PRODUCTION_NAMESPACES = (
    "shadow", "replay", "research", "evaluator", "twin_chiyo",
    "migration_simulation", "recovery_rehearsal", "offline_experiment",
    NAMESPACE_ISOLATED_TEST,
)
PRODUCTION_HOMES = (
    str(Path("./data/world").resolve()),
    str(Path("./data/persona").resolve()),
    "/root/.hermes-world-hk",
    "/root/.hermes-stock",
    "/root/.hermes",
)
PRODUCTION_CUTOVER_ENV = "CHIYO_LIFE_PRODUCTION_CUTOVER"


class CanonicalIntegrationError(Exception):
    """Base class for CT0-10 integration failures (fail-closed)."""


class ProductionPathRefusedError(CanonicalIntegrationError):
    """A canonical store/lease was pointed at a production home."""


class UnknownReferenceSchemeError(CanonicalIntegrationError):
    """A reference used a scheme we do not recognise. We never guess."""


class MalformedReferenceError(CanonicalIntegrationError):
    """A reference had a recognised scheme but an invalid body."""


def is_production_path(path: str | Path) -> bool:
    """Portable mirror of the real guard; the real one is authoritative."""
    resolved = Path(str(path)).expanduser().resolve()
    for home in PRODUCTION_HOMES:
        try:
            home_resolved = Path(home).resolve()
        except OSError:  # pragma: no cover - /root may not resolve on Windows
            home_resolved = Path(home)
        if resolved == home_resolved or home_resolved in resolved.parents:
            return True
    return False


def assert_isolated_store_root(root: str | Path, *, namespace: str = NAMESPACE_ISOLATED_TEST) -> Path:
    """Fail closed unless (root is not production) and (namespace is non-production)."""
    if namespace not in NON_PRODUCTION_NAMESPACES:
        raise CanonicalIntegrationError(
            f"CT0-10 may only run in a non-production namespace; got {namespace!r}"
        )
    resolved = Path(str(root)).expanduser()
    if is_production_path(resolved):
        raise ProductionPathRefusedError(
            f"refusing to use a production home as a CT0-10 store root: {resolved}"
        )
    resolved = resolved.resolve() if resolved.exists() else resolved
    return resolved


# ==========================================================================
# 2. versioned reference layer (order sections 11-19)
# ==========================================================================
RECON_V1_PREFIX = "recon:"
RECON_V2_PREFIX = "recon2:"
SETTLEMENT_V1_PREFIX = "csreq:"
SETTLEMENT_V2_PREFIX = "csreq2:"
RECON_V1_MEANING = "contact-reconciliation/v1"
RECON_V2_MEANING = "contact-reconciliation/v2"
SETTLEMENT_V1_MEANING = "contact-settlement/v1"
SETTLEMENT_V2_MEANING = "contact-settlement/v2"

# the real canonical runtime's own identity families (read from its source)
CANONICAL_ACTION_PREFIX = "actn:"          # action_reality_ledger.py:2439
CANONICAL_SUBMISSION_KEY_PREFIX = "subk:"  # action_reality_ledger.py:2734
CANONICAL_RECONCILIATION_PREFIX = "arec:"  # ActionStore state key reconciliations
CANONICAL_CANDIDATE_SET_PREFIX = "cset:"   # candidate_sources_ag0.py:2732
CANONICAL_CANDIDATE_PREFIX = "cand:"
CONTACT_SUBMISSION_KEY_PREFIX = "ar0_act:"  # frozen contact evidence contract

_HEX16 = re.compile(r"^[0-9a-f]{16}$")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True, slots=True)
class ReconciliationRef:
    """Version-aware reconciliation reference (order section 13).

    `scheme_version` selects the format; `reconciliation_id` is the body; the
    action and revision are carried so a reader can bind the reference to the
    canonical owner record instead of trusting the string.
    """

    scheme_version: str
    reconciliation_id: str
    action_ref: str | None = None
    revision: int | None = None

    def __post_init__(self) -> None:
        if self.scheme_version not in ("v1", "v2"):
            raise UnknownReferenceSchemeError(
                f"unknown reconciliation scheme_version {self.scheme_version!r}"
            )
        if not self.reconciliation_id:
            raise MalformedReferenceError("reconciliation_id must be non-empty")
        if self.scheme_version == "v2" and not _HEX64.match(self.reconciliation_id):
            raise MalformedReferenceError("a v2 reconciliation_id must be 64 lowercase hex chars")
        if self.scheme_version == "v1" and not self.reconciliation_id:
            raise MalformedReferenceError("a v1 reconciliation_id must be non-empty")
        if self.revision is not None and self.revision < 1:
            raise MalformedReferenceError("reconciliation revision must be >= 1")

    @property
    def meaning_version(self) -> str:
        return RECON_V2_MEANING if self.scheme_version == "v2" else RECON_V1_MEANING

    @property
    def prefix(self) -> str:
        return RECON_V2_PREFIX if self.scheme_version == "v2" else RECON_V1_PREFIX

    def format(self) -> str:
        return f"{self.prefix}{self.reconciliation_id}"

    def to_mapping(self) -> dict[str, Any]:
        return {"scheme_version": self.scheme_version, "reconciliation_id": self.reconciliation_id,
                "action_ref": self.action_ref, "revision": self.revision,
                "meaning_version": self.meaning_version, "ref": self.format()}

    # -- parsing ----------------------------------------------------------
    @classmethod
    def parse(cls, value: str, *, action_ref: str | None = None, revision: int | None = None) -> "ReconciliationRef":
        """Dispatch on the declared prefix. UNKNOWN PREFIX -> reject (section 14)."""
        if not isinstance(value, str) or not value:
            raise MalformedReferenceError("reconciliation reference must be a non-empty string")
        if value.startswith(RECON_V2_PREFIX):
            return cls("v2", value[len(RECON_V2_PREFIX):], action_ref, revision)
        if value.startswith(RECON_V1_PREFIX):
            return cls("v1", value[len(RECON_V1_PREFIX):], action_ref, revision)
        raise UnknownReferenceSchemeError(
            f"unrecognised reconciliation reference scheme in {value[:24]!r}; "
            "accepted schemes are recon: (v1) and recon2: (v2)"
        )

    @classmethod
    def from_contract(cls, *, meaning_version: str | None, reference: str,
                      action_ref: str | None = None, revision: int | None = None) -> "ReconciliationRef":
        """Declared meaning_version wins; otherwise fall back to the prefix."""
        if meaning_version == RECON_V2_MEANING:
            body = reference[len(RECON_V2_PREFIX):] if reference.startswith(RECON_V2_PREFIX) else reference
            return cls("v2", body, action_ref, revision)
        if meaning_version == RECON_V1_MEANING:
            body = reference[len(RECON_V1_PREFIX):] if reference.startswith(RECON_V1_PREFIX) else reference
            return cls("v1", body, action_ref, revision)
        if meaning_version is not None:
            raise UnknownReferenceSchemeError(
                f"unrecognised reconciliation meaning_version {meaning_version!r}"
            )
        return cls.parse(reference, action_ref=action_ref, revision=revision)


@dataclass(frozen=True, slots=True)
class SettlementRef:
    scheme_version: str
    request_id: str
    action_ref: str | None = None
    revision: int | None = None

    def __post_init__(self) -> None:
        if self.scheme_version not in ("v1", "v2"):
            raise UnknownReferenceSchemeError(f"unknown settlement scheme_version {self.scheme_version!r}")
        if not self.request_id:
            raise MalformedReferenceError("settlement request id must be non-empty")
        if self.scheme_version == "v2" and not _HEX64.match(self.request_id):
            raise MalformedReferenceError("a v2 settlement id must be 64 lowercase hex chars")

    @property
    def meaning_version(self) -> str:
        return SETTLEMENT_V2_MEANING if self.scheme_version == "v2" else SETTLEMENT_V1_MEANING

    @property
    def prefix(self) -> str:
        return SETTLEMENT_V2_PREFIX if self.scheme_version == "v2" else SETTLEMENT_V1_PREFIX

    def format(self) -> str:
        return f"{self.prefix}{self.request_id}"

    def to_mapping(self) -> dict[str, Any]:
        return {"scheme_version": self.scheme_version, "request_id": self.request_id,
                "action_ref": self.action_ref, "revision": self.revision,
                "meaning_version": self.meaning_version, "ref": self.format()}

    @classmethod
    def parse(cls, value: str, *, action_ref: str | None = None, revision: int | None = None) -> "SettlementRef":
        if not isinstance(value, str) or not value:
            raise MalformedReferenceError("settlement reference must be a non-empty string")
        if value.startswith(SETTLEMENT_V2_PREFIX):
            return cls("v2", value[len(SETTLEMENT_V2_PREFIX):], action_ref, revision)
        if value.startswith(SETTLEMENT_V1_PREFIX):
            return cls("v1", value[len(SETTLEMENT_V1_PREFIX):], action_ref, revision)
        raise UnknownReferenceSchemeError(
            f"unrecognised settlement reference scheme in {value[:24]!r}; "
            "accepted schemes are csreq: (v1) and csreq2: (v2)"
        )


@dataclass(frozen=True, slots=True)
class CanonicalActionRef:
    """The contact side and the real AR-0 side disagree on identity families.

    contact:  action `actn:<hex16>`  ·  submission key `ar0_act:<hex64>`
    real AR0: action `actn:<hex16>`  ·  submission key `subk:<...>`
              reconciliation `arec:<...>`

    `contact_submission_key` is what the frozen evidence contract demands;
    `canonical_submission_key` is what the real ActionStore indexes on. Both are
    carried so the mapping is explicit and auditable (order sections 57-60).
    """

    action_ref: str
    contact_submission_key: str | None = None
    canonical_submission_key: str | None = None
    canonical_reconciliation_ref: str | None = None
    canonical_action_id: str | None = None

    def __post_init__(self) -> None:
        if not self.action_ref.startswith(CANONICAL_ACTION_PREFIX):
            raise MalformedReferenceError("action_ref must use the actn: identity")
        if self.contact_submission_key is not None and not self.contact_submission_key.startswith(
            CONTACT_SUBMISSION_KEY_PREFIX
        ):
            raise MalformedReferenceError("the contact submission key must use ar0_act:")
        if self.canonical_submission_key is not None and not self.canonical_submission_key.startswith(
            CANONICAL_SUBMISSION_KEY_PREFIX
        ):
            raise MalformedReferenceError("the canonical submission key must use subk:")

    def to_mapping(self) -> dict[str, Any]:
        return {"action_ref": self.action_ref, "contact_submission_key": self.contact_submission_key,
                "canonical_submission_key": self.canonical_submission_key,
                "canonical_reconciliation_ref": self.canonical_reconciliation_ref,
                "canonical_action_id": self.canonical_action_id}


def payload_hash(payload: Mapping[str, Any]) -> str:
    """sha256 over canonical JSON - the same recipe the real AR-0 uses."""
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


# ==========================================================================
# 3. contact <-> canonical identity mapping (explicit, never a silent rename)
# ==========================================================================
# The real AG-1 vocabulary does not contain CONTACT_SELECTED (0 hits in the runtime).
# Its decision verbs are START/CONTINUE/PAUSE/WAIT/RESUME/ABANDON/DEFER/NO_ACTION and
# its runtime producer emits NO_ACTION/ACTION_SELECTED/GATE_BLOCKED/EVAL_ERROR.
# We ADAPT (order section 40) - we never add a verb to the real AG-1 contract.
CONTACT_DECISION_SEND = "CONTACT_SELECTED"
CONTACT_DECISION_NO_ACTION = "NO_ACTION"
CONTACT_DECISION_DEFER = "DEFER"

# CORRECTED by the real owner (CT0-10 Phase E): the canonical AG-1 validator REFUSES
# ACTION_SELECTED as a submitted decision verb -
#   DecisionValidationError: decision='ACTION_SELECTED' is not a valid AG-1 decision enum
# - so the contact SEND decision is submitted as the real verb START. ACTION_SELECTED
# remains the runtime OUTCOME projection only (see CANONICAL_AG1_RUNTIME_OUTCOMES).
CONTACT_DECISION_VERB: dict[str, str] = {
    CONTACT_DECISION_SEND: "START",
    CONTACT_DECISION_NO_ACTION: "NO_ACTION",
    CONTACT_DECISION_DEFER: "DEFER",
}
CONTACT_TO_CANONICAL_DECISION: dict[str, str] = dict(CONTACT_DECISION_VERB)
CANONICAL_TO_CONTACT_DECISION: dict[str, str] = {
    canonical: contact for contact, canonical in CONTACT_TO_CANONICAL_DECISION.items()
}
# runtime outcomes are a DIFFERENT vocabulary from submitted verbs: a decision the
# runtime observed as ACTION_SELECTED reads back as a contact SEND.
CANONICAL_OUTCOME_TO_CONTACT: dict[str, str] = {
    "ACTION_SELECTED": CONTACT_DECISION_SEND,
    "NO_ACTION": CONTACT_DECISION_NO_ACTION,
    "DEFER": CONTACT_DECISION_DEFER,
}
CANONICAL_AG1_VERBS = ("START", "CONTINUE", "PAUSE", "WAIT", "RESUME", "ABANDON", "DEFER", "NO_ACTION")
CANONICAL_AG1_RUNTIME_OUTCOMES = ("NO_ACTION", "ACTION_SELECTED", "GATE_BLOCKED", "EVAL_ERROR")


class UnmappableDecisionError(CanonicalIntegrationError):
    """A decision verb has no explicit mapping. We never invent one."""


def canonical_decision_for(contact_decision: str) -> str:
    try:
        return CONTACT_TO_CANONICAL_DECISION[contact_decision]
    except KeyError:
        raise UnmappableDecisionError(
            f"contact decision {contact_decision!r} has no canonical AG-1 mapping; "
            "CT0-10 does not introduce new AG-1 verbs"
        ) from None


def contact_decision_for(canonical_decision: str) -> str:
    try:
        return CANONICAL_TO_CONTACT_DECISION[canonical_decision]
    except KeyError:
        raise UnmappableDecisionError(
            f"canonical decision {canonical_decision!r} has no contact mapping; "
            "the contact pipeline ignores outcomes it does not understand"
        ) from None


# real LR-2/LR-3 interruptibility is FOUR levels; the frozen contact capacity
# contract only has a single `atomic` bool, so the projection is lossy by design
# and FOCUSED policy is a recorded limitation (order sections 23, 24, 118).
REAL_INTERRUPTIBILITY_LEVELS = ("FREE", "LIGHT", "FOCUSED", "ATOMIC")
CONTACT_ATOMIC_PROJECTION = {"FREE": False, "LIGHT": False, "FOCUSED": False, "ATOMIC": True}
KNOWN_SEMANTIC_LIMITATION_FOCUSED = (
    "FOCUSED is not a blocker in the frozen CT0-4 capacity contract (single atomic bit); "
    "whether canonical Activity should soften capacity for FOCUSED is a canonical contract "
    "decision that CT0-10 must NOT invent (order sections 24 and 118)"
)

QUIET_POLICY_MISSING = (
    "no quiet-window policy owner exists anywhere in the canonical runtime; per order "
    "section 26 this is recorded as QUIET_POLICY_MISSING and proactive dispatch fails closed"
)


def atomic_from_interruptibility(level: str) -> bool:
    try:
        return CONTACT_ATOMIC_PROJECTION[level]
    except KeyError:
        raise CanonicalIntegrationError(
            f"unknown interruptibility level {level!r}; canonical levels are "
            f"{REAL_INTERRUPTIBILITY_LEVELS}"
        ) from None


# ==========================================================================
# 4. portable integration data shapes
# ==========================================================================
@dataclass(frozen=True, slots=True)
class CapacityProvenance:
    """A capacity snapshot must say WHERE each fact came from (order section 30)."""

    activity_ref: str | None = None
    activity_revision: int | None = None
    authorization_ref: str | None = None
    authorization_revision: int | None = None
    pending_action_refs: tuple[str, ...] = ()
    policy_ref: str | None = None
    policy_revision: int | None = None
    observed_at: str = ""
    quiet_policy: str = QUIET_POLICY_MISSING
    source_modules: tuple[str, ...] = ()

    def to_mapping(self) -> dict[str, Any]:
        return {
            "activity_ref": self.activity_ref, "activity_revision": self.activity_revision,
            "authorization_ref": self.authorization_ref,
            "authorization_revision": self.authorization_revision,
            "pending_action_refs": list(self.pending_action_refs),
            "policy_ref": self.policy_ref, "policy_revision": self.policy_revision,
            "observed_at": self.observed_at, "quiet_policy": self.quiet_policy,
            "source_modules": list(self.source_modules),
        }


@dataclass(frozen=True, slots=True)
class CanonicalCapacityResult:
    """Result of a READ-ONLY capacity resolution (order sections 21-31)."""

    snapshot: Any | None
    provenance: CapacityProvenance
    unavailable_reasons: tuple[str, ...] = ()
    fail_closed: bool = False
    notes: tuple[str, ...] = ()

    @property
    def available(self) -> bool:
        return self.snapshot is not None and not self.unavailable_reasons and not self.fail_closed

    def to_mapping(self) -> dict[str, Any]:
        snapshot = self.snapshot
        return {
            "available": self.available,
            "unavailable_reasons": list(self.unavailable_reasons),
            "fail_closed": self.fail_closed,
            "provenance": self.provenance.to_mapping(),
            "snapshot": None if snapshot is None else {
                "observed_at": getattr(snapshot, "observed_at", None),
                "revision": getattr(snapshot, "revision", None),
                "quiet_window_open": getattr(snapshot, "quiet_window_open", None),
                "device_available": getattr(snapshot, "device_available", None),
                "channel_available": getattr(snapshot, "channel_available", None),
                "atomic": getattr(snapshot, "atomic", None),
                "pending_outbound_count": getattr(snapshot, "pending_outbound_count", None),
                "previous_unknown": getattr(snapshot, "previous_unknown", None),
                "unresolved_inbound": getattr(snapshot, "unresolved_inbound", None),
            },
            "notes": list(self.notes),
        }


@dataclass(frozen=True, slots=True)
class AuthorityFact:
    """One line of the end-to-end authority trace (order sections 79-80)."""

    fact: str
    owner: str
    writer: str
    ref: str
    revision: int | None = None
    extra: Mapping[str, Any] = field(default_factory=dict)

    def to_mapping(self) -> dict[str, Any]:
        return {"fact": self.fact, "owner": self.owner, "writer": self.writer, "ref": self.ref,
                "revision": self.revision, "extra": dict(self.extra)}


EXPECTED_FACT_WRITERS: dict[str, str] = {
    "contact_intent": "Contact owner (CT0-3 authority)",
    "agency_candidate": "canonical AG-0 (candidate_sources_ag0)",
    "decision": "canonical AG-1 (agency_decision_ag1)",
    "draft": "Expression (canonical expression adapter)",
    "action": "canonical AR-0 (action_reality_ledger)",
    "receipt": "Action Reality (AR-0 ActionStore)",
    "settlement": "canonical AR-1 (result_settlement_ar1)",
    "handoff": "Contact/Result integration owner",
}


# ==========================================================================
# 5. port protocols the adapters must satisfy
# ==========================================================================
class CanonicalCapacityResolverPort(Protocol):
    def resolve(self, *, intent: Any, recipient_ref: str, channel: str, now: str) -> CanonicalCapacityResult: ...


class CanonicalAg0CandidatePort(Protocol):
    def build_candidate_set(self, *, intent: Any, observed_at: str) -> Any: ...


class CanonicalAg1DecisionPort(Protocol):
    def decide(self, *, intent: Any, candidate_set: Any, capacity: CanonicalCapacityResult,
               observed_at: str) -> Any: ...


class CanonicalGroundingPort(Protocol):
    def resolve_grounding(self, *, intent: Any, decision: Any,
                          source_refs: Sequence[str]) -> tuple[Any, ...]: ...


class CanonicalExpressionPort(Protocol):
    def render(self, *, intent: Any, decision: Any, grounding_sources: Sequence[Any]) -> Any: ...


class CanonicalAr0Port(Protocol):
    def propose(self, *, request: Any, created_at: str) -> Any: ...


class CanonicalAr1Port(Protocol):
    def settle(self, *, action_ref: str, result_evidence: Any, observed_at: str) -> Any: ...


__all__ = [
    "AuthorityFact", "CANONICAL_ACTION_PREFIX", "CANONICAL_AG1_RUNTIME_OUTCOMES", "CANONICAL_AG1_VERBS",
    "CANONICAL_CANDIDATE_PREFIX", "CANONICAL_CANDIDATE_SET_PREFIX", "CANONICAL_RECONCILIATION_PREFIX",
    "CANONICAL_SUBMISSION_KEY_PREFIX", "CANONICAL_TO_CONTACT_DECISION", "CanonicalActionRef",
    "CanonicalAg0CandidatePort", "CanonicalAg1DecisionPort", "CanonicalAr0Port", "CanonicalAr1Port",
    "CanonicalCapacityResolverPort", "CanonicalCapacityResult", "CanonicalExpressionPort",
    "CanonicalGroundingPort", "CanonicalIntegrationError", "CapacityProvenance",
    "CONTACT_ATOMIC_PROJECTION", "CONTACT_DECISION_DEFER", "CONTACT_DECISION_NO_ACTION",
    "CONTACT_DECISION_SEND", "CONTACT_SUBMISSION_KEY_PREFIX", "CONTACT_TO_CANONICAL_DECISION",
    "EXPECTED_FACT_WRITERS", "KNOWN_SEMANTIC_LIMITATION_FOCUSED", "MalformedReferenceError",
    "NAMESPACE_CANONICAL", "NAMESPACE_ISOLATED_TEST", "NON_PRODUCTION_NAMESPACES",
    "PRODUCTION_CUTOVER_ENV", "PRODUCTION_HOMES", "ProductionPathRefusedError",
    "QUIET_POLICY_MISSING", "REAL_INTERRUPTIBILITY_LEVELS", "RECON_V1_MEANING", "RECON_V1_PREFIX",
    "RECON_V2_MEANING", "RECON_V2_PREFIX", "ReconciliationRef", "SettlementRef",
    "SETTLEMENT_V1_MEANING", "SETTLEMENT_V1_PREFIX", "SETTLEMENT_V2_MEANING",
    "SETTLEMENT_V2_PREFIX", "SPINE_VERSION", "UnknownReferenceSchemeError", "UnmappableDecisionError",
    "assert_isolated_store_root", "atomic_from_interruptibility", "canonical_decision_for",
    "contact_decision_for", "is_production_path", "payload_hash",
]
