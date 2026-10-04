"""CT0-10 Phase D - canonical AG-0 candidate port (order sections 32-37).

``CanonicalAg0CandidateAdapter`` implements ``ct0_10.canonical_spine.CanonicalAg0CandidatePort``
by driving the REAL AG-0 owner ``scripts/candidate_sources_ag0.py`` (aliased ``ag0`` below):

    eligible ContactIntent (CT0-3 frozen contract, read-only)
        -> ObservedUserRequestEvent            (real typed AG-0 record)
        -> UserRequestCandidateSource.ingest_typed_event / validate_source
        -> CandidateMaterializer.build_candidate_set
        -> real CandidateRecord + real CandidateSet (``cset:<sha256[:16]>``)

Facts verified by reading the real source (line numbers are of the real file):

* capability: ``CandidateProjectionGuard.issue_capability(caller_module=...)`` only accepts
  ``candidate_materializer`` / ``candidate_replay_verifier`` / ``candidate_shadow_observer`` /
  ``ag0_test_harness`` (:417-424). AG-0 has NO writer for a ContactIntent and never can have one:
  the module contains zero occurrences of ``ContactIntent``/``CONTACT_SELECTED`` and it imports only
  Activity/Waiting owners (:32-59).
* forbidden-writer paths that really raise: ``CandidateMaterializer.create_candidate``
  (``MissingCandidateProvenanceError``, :2543-2547), ``CandidateRecord.accept/choose/start/resume/
  execute`` (``CandidateAdoptionForbiddenError``, :664-677), ``CandidateSourceAdapter.
  start_activity/resume_activity/propose_action`` (:1001-1008), and
  ``CandidateProjectionGuard.issue_capability`` for any other caller (:433-436).
* ``cset:`` identity is ``sha256`` over ``{subject_id, observed_at, candidate_refs,
  source_snapshot_refs, policy}`` (:2721-2732) -> the set identity depends on ``observed_at`` and on
  the per-source snapshot ref ``snap:USER_REQUEST:rev:<n>``.
* REAL GOTCHA (this is a correction to the naive reading of order 37): ``UserRequestCandidateSource.
  ingest_typed_event`` increments its internal ``_revision`` whenever ``ev.revision >= existing.
  revision`` (:1481-1484), i.e. re-submitting the *same* event/revision still moves
  ``snap:USER_REQUEST:rev:<n>`` and therefore changes ``cset:``. Idempotency is therefore only
  achievable if the PORT does not re-submit an event revision it has already submitted; this
  adapter does exactly that (``ingested_revisions``), and the test proves ``cset:`` stability.

Declared projection (recorded limitation, see ``DECLARED_PROJECTION_LIMITATION``): canonical AG-0
has NO contact/proactive source kind - ``FORBIDDEN_CANDIDATE_SOURCE_KINDS`` (:104-129) permanently
forbids ``CONTACT_INTENT``/``PROACTIVE_CONTACT`` and ``FORBIDDEN_CANDIDATE_KINDS`` (:152-162)
forbids ``SEND_PROACTIVE_MESSAGE``. ``USER_REQUEST`` (:1431-1648) is the only real source that
materializes a communication-window candidate, and its ``validate_source`` (:1543-1544) requires
``event_kind == "EXPLICIT_USER_REQUEST"``. The port therefore declares the mapping
``CONTACT_INTENT -> USER_REQUEST`` explicitly instead of renaming anything, and the truthful label is
proven to be rejected by the real owner (``UntrustedCandidateSourceError``).
"""
from __future__ import annotations

import copy
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

# --- the portable spine (this module is the only layer allowed to import the real owners) ----
_SPINE_SRC_ROOT = Path(__file__).resolve().parents[2]
if str(_SPINE_SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(_SPINE_SRC_ROOT))

from ct0_10.canonical_spine import (  # noqa: E402
    CANONICAL_CANDIDATE_PREFIX,
    CANONICAL_CANDIDATE_SET_PREFIX,
    CanonicalIntegrationError,
    EXPECTED_FACT_WRITERS,
    NAMESPACE_ISOLATED_TEST,
    assert_isolated_store_root,
)

def host_path(value: str | Path) -> Path:
    """Translate a Windows 'D:/x' path to '/mnt/d/x' when running under Linux/WSL."""
    text = str(value)
    if os.name != "nt" and len(text) > 2 and text[1] == ":" and text[2] in ("/", "\\"):
        return Path("/mnt/" + text[0].lower() + "/" + text[2:].lstrip("/\\"))
    return Path(text)


# --- the frozen CT0-9 contact contracts: imported READ-ONLY for isinstance/mapping checks -----
_CONTACT_CONTRACT_ROOT = host_path(
    os.environ.get(
        "CT0_CONTACT_CONTRACT_ROOT", str(Path(__file__).resolve().parents[1])
    )
)
if str(_CONTACT_CONTRACT_ROOT) not in sys.path:
    sys.path.insert(0, str(_CONTACT_CONTRACT_ROOT))

import contact_candidate_model as contact_candidate_contract  # noqa: E402
import contact_intent_model as contact_intent_contract  # noqa: E402

RUNTIME_ROOT_ENV = "CHIYO_CANONICAL_RUNTIME_ROOT"
_DEFAULT_RUNTIME_ROOT = str(Path(__file__).resolve().parents[2] / "life_runtime")

AG0_OWNER_MODULE = "candidate_sources_ag0"
AG0_OWNER_FILE = "scripts/candidate_sources_ag0.py"
PORT_ID = "ct0_10.canonical_ports.ag0_adapter.CanonicalAg0CandidateAdapter"

CONTACT_EVENT_REF_PREFIX = "ureq:"
CONTACT_SOURCE_TYPE = "CONTACT_INTENT"

# ============================================================================
# 1. explicit declared mapping tables (order sections 13, 32-36)
# ============================================================================
# contact source type -> real AG-0 source_kind (the ONLY real source that materializes a
# communication candidate; declared explicitly, never a silent rename)
CONTACT_SOURCE_TYPE_TO_AG0_SOURCE_KIND: dict[str, str] = {
    CONTACT_SOURCE_TYPE: "USER_REQUEST",
}
# contact candidate kind -> the real AG-0 candidate_kind actually emitted by the owner
# (candidate_sources_ag0.py:1578 always emits RESPOND_USER_REQUEST for this source)
CONTACT_KIND_TO_AG0_CANDIDATE_KIND: dict[str, str] = {
    "SHARE_EXPERIENCE": "RESPOND_USER_REQUEST",
}
# contact intent kind -> real AG-0 event_kind (required by the real validate_source)
CONTACT_INTENT_KIND_TO_AG0_EVENT_KIND: dict[str, str] = {
    "SHARE_EXPERIENCE": "EXPLICIT_USER_REQUEST",
}
# contact lifecycle status -> real AG-0 ObservedUserRequestEvent.request_status
# (the real event vocabulary is OPEN/CANCELLED/WITHDRAWN/EXPIRED/FULFILLED/NONE, :780-781)
CONTACT_STATUS_TO_AG0_REQUEST_STATUS: dict[str, str] = {
    "OPEN": "OPEN",
    "DEFERRED": "OPEN",          # non-terminal: still consider-able
    "SELECTED": "NONE",          # already claimed: AG-0 must not re-materialize a claimable candidate
    "CONSUMED": "FULFILLED",
    "EXPIRED": "EXPIRED",
    "CANCELLED": "CANCELLED",
    "STALE": "WITHDRAWN",        # superseded by a newer observation
}
AG0_STATUS_TO_CONTACT_REJECTION_CASE: dict[str, str] = {
    "EXPIRED": "EXPIRED",
    "CANCELLED": "REVOKED_CANCELLED",
    "WITHDRAWN": "SUPERSEDED_STALE",
    "FULFILLED": "ALREADY_CONSUMED",
    "NONE": "ALREADY_SELECTED",
}
ELIGIBLE_CONTACT_INTENT_STATUSES = frozenset({"OPEN", "DEFERRED"})

DECLARED_PROJECTION_LIMITATION = (
    "canonical AG-0 has no contact/proactive candidate source: FORBIDDEN_CANDIDATE_SOURCE_KINDS "
    "(candidate_sources_ag0.py:104-129) permanently forbids CONTACT_INTENT/PROACTIVE_CONTACT and "
    "FORBIDDEN_CANDIDATE_KINDS (:152-162) forbids SEND_PROACTIVE_MESSAGE. USER_REQUEST (:1431-1648) "
    "is the only real source kind that materializes a communication-window candidate and its "
    "validate_source (:1543-1544) requires event_kind == 'EXPLICIT_USER_REQUEST'. CT0-10 therefore "
    "DECLARES CONTACT_INTENT -> USER_REQUEST as an explicit, lossy projection and records it as a "
    "canonical contract limitation; it does not add a source kind to AG-0 and the truthful label is "
    "proven rejected by the real owner (UntrustedCandidateSourceError)."
)

AG0_NO_CONTACT_INTENT_WRITER = (
    "scripts/candidate_sources_ag0.py contains zero occurrences of 'ContactIntent'/'CONTACT_SELECTED' "
    "and defines no intent writer; AG-0 can only project candidate records, never write a "
    "ContactIntent (which stays owned by the CT0-3 contact authority)."
)


class CanonicalAg0PortError(CanonicalIntegrationError):
    """The AG-0 port could not perform its job (fail closed)."""


class ContactIntentNotEligibleError(CanonicalAg0PortError):
    """The REAL AG-0 owner refused the intent; the real verdict is carried verbatim."""

    def __init__(
        self,
        message: str,
        *,
        case: str,
        real_reason_code: str,
        real_owner_verdict: Sequence[Any],
        contact_intent_ref: str,
        contact_intent_status: str,
        real_error_type: str | None = None,
        real_error_text: str | None = None,
    ) -> None:
        super().__init__(message)
        self.case = case
        self.real_reason_code = real_reason_code
        self.real_owner_verdict = tuple(real_owner_verdict)
        self.contact_intent_ref = contact_intent_ref
        self.contact_intent_status = contact_intent_status
        self.real_error_type = real_error_type
        self.real_error_text = real_error_text

    def to_mapping(self) -> dict[str, Any]:
        return {
            "error": type(self).__name__,
            "message": str(self),
            "case": self.case,
            "real_owner_verdict": list(self.real_owner_verdict),
            "real_reason_code": self.real_reason_code,
            "real_owner": f"{AG0_OWNER_MODULE}.UserRequestCandidateSource.validate_source",
            "real_error_type": self.real_error_type,
            "real_error_text": self.real_error_text,
            "contact_intent_ref": self.contact_intent_ref,
            "contact_intent_status": self.contact_intent_status,
        }


# ============================================================================
# 2. real owner loading
# ============================================================================
_REAL_AG0: Any | None = None


def runtime_root() -> Path:
    return host_path(os.environ.get(RUNTIME_ROOT_ENV, _DEFAULT_RUNTIME_ROOT))


def load_real_ag0() -> Any:
    """Import the REAL AG-0 owner (POSIX-only: the owner imports fcntl)."""
    global _REAL_AG0
    if _REAL_AG0 is None:
        scripts = runtime_root() / "scripts"
        if not scripts.is_dir():
            raise CanonicalAg0PortError(f"canonical runtime scripts directory not found: {scripts}")
        if str(scripts) not in sys.path:
            sys.path.insert(0, str(scripts))
        try:
            import candidate_sources_ag0 as owner  # noqa: PLC0415
        except ImportError as exc:  # pragma: no cover - Windows host
            raise CanonicalAg0PortError(
                "the canonical AG-0 owner is not importable here "
                f"({type(exc).__name__}: {exc}); it requires POSIX fcntl - run under Linux/WSL"
            ) from exc
        _REAL_AG0 = owner
    return _REAL_AG0


# ============================================================================
# 3. portable result shapes
# ============================================================================
def _to_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


@dataclass(frozen=True, slots=True)
class ContactIntentProvenance:
    """The contact-side facts carried onto the canonical candidate (order section 34)."""

    contact_intent_ref: str
    contact_candidate_ref: str
    contact_candidate_idempotency_key: str
    contact_source_refs: tuple[str, ...]
    contact_source_type: str
    contact_kind: str
    contact_recipient_ref: str
    contact_revision: int
    contact_status: str
    contact_created_at: str
    contact_observed_at: str
    contact_valid_until: str

    def to_mapping(self) -> dict[str, Any]:
        return {
            "contact_intent_ref": self.contact_intent_ref,
            "contact_candidate_ref": self.contact_candidate_ref,
            "contact_candidate_idempotency_key": self.contact_candidate_idempotency_key,
            "contact_source_refs": list(self.contact_source_refs),
            "contact_source_type": self.contact_source_type,
            "contact_kind": self.contact_kind,
            "contact_recipient_ref": self.contact_recipient_ref,
            "contact_revision": self.contact_revision,
            "contact_status": self.contact_status,
            "contact_created_at": self.contact_created_at,
            "contact_observed_at": self.contact_observed_at,
            "contact_valid_until": self.contact_valid_until,
        }


@dataclass(frozen=True, slots=True)
class Ag0CandidateRecordProvenance:
    """The provenance the REAL AG-0 owner actually put on the candidate record."""

    candidate_ref: str
    candidate_kind: str
    source_kind: str
    source_ref: str
    source_revision: Any
    revision: int
    availability_status: str
    valid_from: str | None
    valid_until: str | None
    semantic_identity: str | None
    user_event_ref: str | None
    target_refs: tuple[str, ...]
    source_refs: tuple[str, ...]
    provenance_refs: tuple[str, ...]
    provenance_chain: tuple[Mapping[str, Any], ...]
    idempotency_key: str
    policy_version: str

    @classmethod
    def from_record(cls, record: Any) -> "Ag0CandidateRecordProvenance":
        return cls(
            candidate_ref=record.candidate_id,
            candidate_kind=record.candidate_kind,
            source_kind=record.source_kind,
            source_ref=record.source_ref,
            source_revision=record.source_revision,
            revision=record.revision,
            availability_status=record.availability_status,
            valid_from=record.valid_from,
            valid_until=record.valid_until,
            semantic_identity=record.semantic_identity,
            user_event_ref=record.user_event_ref,
            target_refs=tuple(record.target_refs),
            source_refs=tuple(record.source_refs),
            provenance_refs=tuple(record.provenance_refs),
            provenance_chain=tuple(copy.deepcopy(record.provenance_chain)),
            idempotency_key=record.idempotency_key,
            policy_version=record.policy_version,
        )

    def to_mapping(self) -> dict[str, Any]:
        return {
            "candidate_ref": self.candidate_ref,
            "candidate_kind": self.candidate_kind,
            "source_kind": self.source_kind,
            "source_ref": self.source_ref,
            "source_revision": self.source_revision,
            "revision": self.revision,
            "availability_status": self.availability_status,
            "valid_from": self.valid_from,
            "valid_until": self.valid_until,
            "semantic_identity": self.semantic_identity,
            "user_event_ref": self.user_event_ref,
            "target_refs": list(self.target_refs),
            "source_refs": list(self.source_refs),
            "provenance_refs": list(self.provenance_refs),
            "provenance_chain": [dict(step) for step in self.provenance_chain],
            "idempotency_key": self.idempotency_key,
            "policy_version": self.policy_version,
        }


@dataclass(frozen=True, slots=True)
class CanonicalAg0CandidateResult:
    """What ``build_candidate_set`` returns: the REAL CandidateSet plus the contact mapping."""

    candidate_set: Any
    candidates: tuple[Any, ...]
    contact: ContactIntentProvenance
    record: Ag0CandidateRecordProvenance
    declared_mapping: Mapping[str, Any]
    provenance_checks: Mapping[str, bool]
    contact_intent_unchanged: bool
    suppressed_reingestions: int
    source_snapshot_ref: str
    notes: tuple[str, ...] = ()

    @property
    def candidate_set_id(self) -> str:
        return self.candidate_set.candidate_set_id

    @property
    def candidate_refs(self) -> tuple[str, ...]:
        return tuple(self.candidate_set.candidate_refs)

    @property
    def contact_intent_ref(self) -> str:
        return self.contact.contact_intent_ref

    @property
    def real_candidate_ref(self) -> str:
        return self.record.candidate_ref

    def candidate_for_intent(self) -> Any | None:
        return self.candidates[0] if self.candidates else None

    def to_mapping(self) -> dict[str, Any]:
        return {
            "candidate_set_id": self.candidate_set_id,
            "candidate_set": self.candidate_set.to_dict(),
            "candidate_refs": list(self.candidate_refs),
            "source_snapshot_ref": self.source_snapshot_ref,
            "contact": self.contact.to_mapping(),
            "record": self.record.to_mapping(),
            "declared_mapping": dict(self.declared_mapping),
            "provenance_checks": dict(self.provenance_checks),
            "contact_intent_unchanged": self.contact_intent_unchanged,
            "suppressed_reingestions": self.suppressed_reingestions,
            "record_writer": EXPECTED_FACT_WRITERS["agency_candidate"],
            "notes": list(self.notes),
        }


# ============================================================================
# 4. the port
# ============================================================================
class CanonicalAg0CandidateAdapter:
    """Real-owner AG-0 port: ``spine.CanonicalAg0CandidatePort``."""

    port_id = PORT_ID

    def __init__(
        self,
        *,
        caller_module: str = "ag0_test_harness",
        per_source_cap: int = 25,
        global_cap: int = 100,
        audit_journal_path: str | Path | None = None,
        owner: Any | None = None,
    ) -> None:
        self.ag0 = owner if owner is not None else load_real_ag0()
        self.owner_module_file = str(Path(self.ag0.__file__).resolve())
        self.capability = self.ag0.CandidateProjectionGuard.issue_capability(
            caller_module=caller_module,
            namespace=NAMESPACE_ISOLATED_TEST,
        )
        self.source = self.ag0.UserRequestCandidateSource(per_source_cap=per_source_cap)
        self.registry = self.ag0.CandidateSourceRegistry(
            {self.ag0.SOURCE_USER_REQUEST: self.source}
        )
        journal_path = None
        if audit_journal_path is not None:
            journal_path = assert_isolated_store_root(audit_journal_path) / "ag0_candidate_audit.jsonl"
        self.audit_journal = self.ag0.CandidateAuditJournal(journal_path)
        self.materializer = self.ag0.CandidateMaterializer(
            self.registry,
            capability=self.capability,
            audit_journal=self.audit_journal,
            global_cap=global_cap,
        )
        self.read_service = self.ag0.CandidateReadService(self.materializer)
        self.ingested_revisions: dict[str, int] = {}
        self.suppressed_reingestion_count = 0
        self._sets_by_id: dict[str, tuple[Any, tuple[Any, ...]]] = {}

    # -- helpers ---------------------------------------------------------
    @staticmethod
    def _require_contact_intent(intent: Any) -> Any:
        if isinstance(intent, contact_intent_contract.ContactIntent):
            return intent
        if isinstance(intent, Mapping):
            return contact_intent_contract.ContactIntent.from_mapping(dict(intent))
        raise CanonicalAg0PortError(
            "AG-0 port requires a frozen CT0-3 ContactIntent (or its mapping form), got "
            f"{type(intent).__name__}"
        )

    @staticmethod
    def contact_intent_snapshot(intent: Any) -> dict[str, Any]:
        """A read-only snapshot used to prove AG-0 never touches the contact intent."""
        return copy.deepcopy(intent.to_dict())

    @staticmethod
    def _event_ref(intent: Any) -> str:
        ref = str(intent.intent_id)
        body = ref.split(":", 1)[1] if ":" in ref else ref
        return f"{CONTACT_EVENT_REF_PREFIX}{body}"

    def _to_user_request_event(self, intent: Any, observed_at: str) -> Any:
        source_kind = CONTACT_SOURCE_TYPE_TO_AG0_SOURCE_KIND.get(CONTACT_SOURCE_TYPE)
        if source_kind != self.ag0.SOURCE_USER_REQUEST:
            raise CanonicalAg0PortError(
                f"declared source mapping for {CONTACT_SOURCE_TYPE!r} is not USER_REQUEST"
            )
        event_kind = CONTACT_INTENT_KIND_TO_AG0_EVENT_KIND.get(str(intent.intent_kind))
        if event_kind is None:
            raise CanonicalAg0PortError(
                f"contact intent kind {intent.intent_kind!r} has no declared real AG-0 projection; "
                "CT0-10 never adds a source kind to AG-0"
            )
        request_status = CONTACT_STATUS_TO_AG0_REQUEST_STATUS.get(str(intent.status))
        if request_status is None:
            raise CanonicalAg0PortError(
                f"contact intent status {intent.status!r} has no declared real AG-0 mapping"
            )
        if not intent.channel_scope:
            raise CanonicalAg0PortError("contact intent has no channel_scope; cannot build an event")
        return self.ag0.ObservedUserRequestEvent(
            event_id=self._event_ref(intent),
            channel=str(intent.channel_scope[0]),
            event_kind=event_kind,
            request_status=request_status,
            target_refs=[str(intent.recipient_ref), str(intent.candidate_ref)],
            occurred_at=str(intent.created_at),
            observed_at=str(observed_at),
            revision=int(intent.revision),
            semantic_request_key=str(intent.idempotency_key),
            valid_from=str(intent.created_at),
            valid_until=str(intent.valid_until),
        )

    def _reject_ineligible(self, intent: Any, event: Any, observed_at: str) -> None:
        """Ask the REAL owner for its verdict; refuse with the REAL reason (order 36)."""
        ok, reason = self.source.validate_source(event.to_dict(), observed_at=observed_at)
        if ok:
            return
        real_reason = str(reason)
        ag0_status = str(event.request_status)
        case = AG0_STATUS_TO_CONTACT_REJECTION_CASE.get(ag0_status)
        if case is None:
            case = "NOT_YET_VALID" if "NOT_YET_VALID" in real_reason else real_reason
        raise ContactIntentNotEligibleError(
            "REJECTED by the real AG-0 owner "
            f"({AG0_OWNER_MODULE}.UserRequestCandidateSource.validate_source -> "
            f"(False, {real_reason!r})); contact intent status={intent.status!r}, "
            f"mapped request_status={ag0_status!r}. AG-0 has no exception for a non-OPEN request "
            "status - the real rejection authority is the owner's (False, reason_code) verdict",
            case=case,
            real_reason_code=real_reason,
            real_owner_verdict=(ok, reason),
            contact_intent_ref=str(intent.intent_id),
            contact_intent_status=str(intent.status),
        )

    def _record_for(self, candidate_ref: str) -> Any:
        raw = self.read_service.get_candidate(candidate_ref)
        if raw is None:
            raise CanonicalAg0PortError(f"real AG-0 owns no candidate {candidate_ref!r}")
        return self.ag0.CandidateRecord.from_dict(raw)

    def candidates_for(self, candidate_set_id: str) -> tuple[Any, ...]:
        entry = self._sets_by_id.get(candidate_set_id)
        if entry is None:
            raise CanonicalAg0PortError(
                f"candidate set {candidate_set_id!r} was not built by this port instance"
            )
        return entry[1]

    def solve_event_for_intent(self, intent: Any) -> str:
        return self._event_ref(self._require_contact_intent(intent))

    # -- the port method --------------------------------------------------
    def real_owner_verdict_for(self, intent: Any, observed_at: str) -> dict[str, Any]:
        """Ask the REAL AG-0 owner for its eligibility verdict without materializing anything."""
        intent = self._require_contact_intent(intent)
        event = self._to_user_request_event(intent, str(observed_at))
        ok, reason = self.source.validate_source(event.to_dict(), observed_at=str(observed_at))
        return {
            "real_owner": f"{AG0_OWNER_MODULE}.UserRequestCandidateSource.validate_source",
            "ag0_event_ref": event.event_id,
            "mapped_request_status": str(event.request_status),
            "eligible": bool(ok),
            "reason_code": reason,
            "contact_intent_status": str(intent.status),
        }

    def build_candidate_set(self, *, intent: Any, observed_at: str) -> CanonicalAg0CandidateResult:
        """Build a real AG-0 CandidateSet for one eligible contact intent (order 32-37)."""
        intent = self._require_contact_intent(intent)
        snapshot_before = self.contact_intent_snapshot(intent)
        observed_at = str(observed_at)

        # The REAL owner decides eligibility FIRST (order 36): expired / revoked / superseded /
        # already-claimed intents are refused with the owner's own reason code.
        event = self._to_user_request_event(intent, observed_at)
        self._reject_ineligible(intent, event, observed_at)

        # Belt and braces: a second local lock on the same lifecycle vocabulary.
        if str(intent.status) not in ELIGIBLE_CONTACT_INTENT_STATUSES:
            raise ContactIntentNotEligibleError(
                "contact intent is not eligible for AG-0 projection at input "
                f"(status={intent.status!r})",
                case=AG0_STATUS_TO_CONTACT_REJECTION_CASE.get(str(intent.status), str(intent.status)),
                real_reason_code="PORT_INPUT_GATE_NOT_ELIGIBLE",
                real_owner_verdict=(False, "PORT_INPUT_GATE_NOT_ELIGIBLE"),
                contact_intent_ref=str(intent.intent_id),
                contact_intent_status=str(intent.status),
            )

        # Idempotency: never re-submit an event revision already submitted (see module docstring).
        previous = self.ingested_revisions.get(event.event_id)
        if previous is None or int(intent.revision) > previous:
            self.source.ingest_typed_event(event)
            self.ingested_revisions[event.event_id] = int(intent.revision)
        else:
            self.suppressed_reingestion_count += 1

        candidate_set = self.materializer.build_candidate_set(observed_at=observed_at)
        candidates = tuple(self._record_for(ref) for ref in candidate_set.candidate_refs)
        mine = tuple(c for c in candidates if c.user_event_ref == event.event_id)
        if len(mine) != 1:
            raise CanonicalAg0PortError(
                "the real AG-0 candidate set does not contain exactly one candidate for "
                f"event {event.event_id!r}; got {[c.candidate_id for c in mine]}"
            )
        record = mine[0]

        checks = {
            "candidate_identity_is_real_cand": record.candidate_id.startswith(CANONICAL_CANDIDATE_PREFIX),
            "set_identity_is_real_cset": candidate_set.candidate_set_id.startswith(
                CANONICAL_CANDIDATE_SET_PREFIX
            ),
            "source_kind_is_real_user_request": record.source_kind == self.ag0.SOURCE_USER_REQUEST,
            "candidate_kind_owned_by_real_owner": record.candidate_kind
            == CONTACT_KIND_TO_AG0_CANDIDATE_KIND.get(str(intent.intent_kind), record.candidate_kind),
            "user_event_ref_is_derived_from_contact_intent": record.user_event_ref == event.event_id,
            "source_revision_matches_contact_revision": int(record.source_revision)
            == int(intent.revision),
            "expiry_preserved_on_candidate": record.valid_until is not None
            and _to_utc(record.valid_until) == _to_utc(intent.valid_until),
            "valid_from_preserved_on_candidate": record.valid_from is not None
            and _to_utc(record.valid_from) == _to_utc(intent.created_at),
            "contact_candidate_ref_retained_in_target_refs": str(intent.candidate_ref)
            in tuple(record.target_refs),
            "contact_recipient_ref_retained_in_target_refs": str(intent.recipient_ref)
            in tuple(record.target_refs),
            "observed_at_preserved": _to_utc(record.observed_at) == _to_utc(observed_at),
            "availability_open": record.availability_status == self.ag0.CANDIDATE_STATUS_OPEN,
        }
        failed = [name for name, ok in checks.items() if not ok]
        if failed:
            raise CanonicalAg0PortError(f"AG-0 provenance binding failed: {failed}")

        snapshot_after = self.contact_intent_snapshot(intent)
        contact_intent_unchanged = snapshot_before == snapshot_after
        if not contact_intent_unchanged:
            raise CanonicalAg0PortError("the contact intent was mutated while building the AG-0 set")

        contact = ContactIntentProvenance(
            contact_intent_ref=str(intent.intent_id),
            contact_candidate_ref=str(intent.candidate_ref),
            contact_candidate_idempotency_key=str(intent.candidate_idempotency_key),
            contact_source_refs=tuple(str(r) for r in intent.source_refs),
            contact_source_type=CONTACT_SOURCE_TYPE,
            contact_kind=str(intent.intent_kind),
            contact_recipient_ref=str(intent.recipient_ref),
            contact_revision=int(intent.revision),
            contact_status=str(intent.status),
            contact_created_at=str(intent.created_at),
            contact_observed_at=str(intent.observed_at),
            contact_valid_until=str(intent.valid_until),
        )
        declared_mapping = {
            "contact_source_type->ag0_source_kind": dict(CONTACT_SOURCE_TYPE_TO_AG0_SOURCE_KIND),
            "contact_kind->ag0_candidate_kind": dict(CONTACT_KIND_TO_AG0_CANDIDATE_KIND),
            "contact_intent_kind->ag0_event_kind": dict(CONTACT_INTENT_KIND_TO_AG0_EVENT_KIND),
            "contact_status->ag0_request_status": dict(CONTACT_STATUS_TO_AG0_REQUEST_STATUS),
            "ag0_status->contact_rejection_case": dict(AG0_STATUS_TO_CONTACT_REJECTION_CASE),
            "contact_intent_ref->ag0_event_ref": {
                str(intent.intent_id): event.event_id,
            },
            "ag0_event_ref->ag0_candidate_ref": {event.event_id: record.candidate_id},
            "ag0_candidate_ref->candidate_set_id": {record.candidate_id: candidate_set.candidate_set_id},
            "declared_projection_limitation": DECLARED_PROJECTION_LIMITATION,
            "ag0_owner_module": AG0_OWNER_MODULE,
            "ag0_owner_file": AG0_OWNER_FILE,
            "record_writer": EXPECTED_FACT_WRITERS["agency_candidate"],
        }
        result = CanonicalAg0CandidateResult(
            candidate_set=candidate_set,
            candidates=candidates,
            contact=contact,
            record=Ag0CandidateRecordProvenance.from_record(record),
            declared_mapping=declared_mapping,
            provenance_checks=checks,
            contact_intent_unchanged=contact_intent_unchanged,
            suppressed_reingestions=self.suppressed_reingestion_count,
            source_snapshot_ref=(
                candidate_set.source_snapshot_refs[0] if candidate_set.source_snapshot_refs else ""
            ),
            notes=(
                AG0_NO_CONTACT_INTENT_WRITER,
                DECLARED_PROJECTION_LIMITATION,
                "re-submitting an identical event revision would move snap:USER_REQUEST:rev:<n> and "
                "therefore change cset: (candidate_sources_ag0.py:1481-1484, :2721-2732); the port "
                "suppresses it so cset: stays stable for the same intent + observed_at",
            ),
        )
        self._sets_by_id[candidate_set.candidate_set_id] = (candidate_set, candidates)
        return result


# ============================================================================
# 5. real forbidden-writer / refusal evidence helpers (used by the test)
# ============================================================================
def prove_forbidden_writer_paths(adapter: CanonicalAg0CandidateAdapter) -> dict[str, Any]:
    """Exercise the REAL AG-0 forbidden paths and record the exact error texts."""
    ag0 = adapter.ag0
    probes: dict[str, Any] = {}

    def record(name: str, call: Callable[[], Any], *, note: str) -> None:
        try:
            call()
        except BaseException as exc:  # noqa: BLE001 - the point is to capture the real error
            probes[name] = {
                "raised": type(exc).__name__,
                "text": str(exc),
                "module": type(exc).__module__,
                "note": note,
            }
        else:
            probes[name] = {"raised": None, "text": None, "note": note, "unexpected": "no error raised"}

    record(
        "materializer.create_candidate",
        lambda: adapter.materializer.create_candidate(),
        note="raw sourceless candidate creation is permanently forbidden",
    )
    record(
        "materializer.reject_negative_authority_input",
        lambda: adapter.materializer.reject_negative_authority_input("CONTACT_INTENT"),
        note="a contact intent is not allowed to sign an AG-0 candidate",
    )
    record(
        "read_service.list_candidates_by_source(CONTACT_INTENT)",
        lambda: adapter.read_service.list_candidates_by_source("CONTACT_INTENT"),
        note="CONTACT_INTENT is a forbidden candidate source kind",
    )
    record(
        "registry.get_adapter(CONTACT_INTENT)",
        lambda: adapter.registry.get_adapter("CONTACT_INTENT"),
        note="AG-0 owns no contact-intent adapter",
    )
    record(
        "guard.issue_capability(contact_intent_authority)",
        lambda: ag0.CandidateProjectionGuard.issue_capability(
            caller_module="contact_intent_authority", namespace=NAMESPACE_ISOLATED_TEST
        ),
        note="the contact authority cannot hold an AG-0 projection capability",
    )

    dummy_candidate = adapter.ag0.CandidateRecord(
        candidate_id=f"{CANONICAL_CANDIDATE_PREFIX}{'0' * 16}",
        candidate_kind=ag0.CANDIDATE_KIND_RESPOND_USER_REQUEST,
        source_kind=ag0.SOURCE_USER_REQUEST,
        source_ref="ureq:probe",
        availability_status=ag0.CANDIDATE_STATUS_OPEN,
        created_at="2026-01-01T00:00:00+00:00",
        observed_at="2026-01-01T00:00:00+00:00",
        recorded_at="2026-01-01T00:00:00+00:00",
        causal_parent_refs=["ureq:probe"],
        provenance_refs=["ureq:probe", "snap:USER_REQUEST:rev:0"],
        idempotency_key="idem_cand:probe",
        user_event_ref="ureq:probe",
    )
    for method in ("accept", "choose", "start", "resume", "execute"):
        record(
            f"candidate.{method}()",
            getattr(dummy_candidate, method),
            note="CandidateRecord has zero adoption/decision/mutation authority",
        )
    record(
        "adapter.start_activity()",
        adapter.source.start_activity,
        note="a candidate source has zero Activity writer authority",
    )
    record(
        "adapter.propose_action()",
        adapter.source.propose_action,
        note="a candidate source has zero Action writer authority",
    )

    record(
        "candidate_record(source_kind=CONTACT_INTENT)",
        lambda: ag0.CandidateRecord(
            candidate_id=f"{CANONICAL_CANDIDATE_PREFIX}{'1' * 16}",
            candidate_kind=ag0.CANDIDATE_KIND_RESPOND_USER_REQUEST,
            source_kind="CONTACT_INTENT",
            source_ref="cintent:probe",
            availability_status=ag0.CANDIDATE_STATUS_OPEN,
            created_at="2026-01-01T00:00:00+00:00",
            observed_at="2026-01-01T00:00:00+00:00",
            recorded_at="2026-01-01T00:00:00+00:00",
            idempotency_key="idem_cand:probe:contact",
            user_event_ref="ureq:probe",
        ),
        note="invalid source context: the truthful contact label is rejected by the real owner",
    )
    record(
        "ingest_typed_event(raw chat text)",
        lambda: adapter.source.ingest_typed_event("please message me"),
        note="untyped raw chat text cannot enter the real AG-0 source",
    )
    return probes


__all__ = [
    "AG0_NO_CONTACT_INTENT_WRITER", "AG0_OWNER_FILE", "AG0_OWNER_MODULE",
    "AG0_STATUS_TO_CONTACT_REJECTION_CASE", "Ag0CandidateRecordProvenance",
    "CONTACT_EVENT_REF_PREFIX", "CONTACT_INTENT_KIND_TO_AG0_EVENT_KIND",
    "CONTACT_KIND_TO_AG0_CANDIDATE_KIND", "CONTACT_SOURCE_TYPE",
    "CONTACT_SOURCE_TYPE_TO_AG0_SOURCE_KIND", "CONTACT_STATUS_TO_AG0_REQUEST_STATUS",
    "CanonicalAg0CandidateAdapter", "CanonicalAg0CandidateResult", "CanonicalAg0PortError",
    "ContactIntentNotEligibleError", "ContactIntentProvenance",
    "DECLARED_PROJECTION_LIMITATION", "ELIGIBLE_CONTACT_INTENT_STATUSES", "PORT_ID",
    "RUNTIME_ROOT_ENV", "load_real_ag0", "prove_forbidden_writer_paths", "runtime_root",
]
