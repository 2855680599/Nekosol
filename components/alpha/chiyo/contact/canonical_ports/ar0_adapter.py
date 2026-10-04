"""CT0-10 Phase H - `CanonicalAr0Adapter` (order sections 57-61).

Satisfies the spine's `CanonicalAr0Port` (`propose(*, request, created_at)`) by
driving the REAL canonical AR-0 owner (`action_reality_ledger`) in an ISOLATED
store root. Nothing here re-implements AR-0: the adapter only translates the
frozen CT0-9 contact request into the owner's own call and translates the
owner's own answer back into a typed result.

Hard rules honoured by this module
----------------------------------
* The real owner is Linux-only (it imports POSIX `fcntl`). On a host without
  `fcntl` this adapter fails closed with `CanonicalRuntimeUnavailableError`
  instead of degrading into a fake.
* The real `ActionStore` refuses production homes itself
  (`action_reality_ledger.py:965-971`). The adapter constructs the REAL store
  FIRST so that guard fires, and then applies the portable spine guard
  (`canonical_spine.assert_isolated_store_root`, which also pins the namespace).
  A production root is therefore refused twice by two different owners; the real
  owner's verbatim error text is preserved in the raised exception.
* No network, no sockets, no real Telegram. The only transport is the in-tree
  `ActionAdapterProtocol` + `FakeMessageProvider` seam, and only when the caller
  explicitly asks for `submit()`.
* `propose()` performs zero external side effects: AR-0 `propose_action` creates
  a PROPOSED record only.

Identity handling (order sections 57-60) - never a rename
--------------------------------------------------------
Three DIFFERENT identities are carried side by side and never conflated:

  1. contact submission key   `ar0_act:<sha256[:64]>`  - minted by the frozen
     CT0-9 contract (`contact_message_action_model.derive_message_action_request_identity`).
     The frozen contract states the AR-0 owner receives it as *its own*
     `idempotency_key` (contact_message_action_model.py, docstring of
     `derive_message_action_request_identity`), so the adapter passes it through
     verbatim. That pass-through is the contract, not a rename.
  2. canonical action identity `actn:<sha256[:16]>` - minted by AR-0 at
     `action_reality_ledger.py:2439` as `sha256("actn:" + idempotency_key)[:16]`.
     Deterministic, so it is stable across process restart, writer epoch and
     wall-clock time.
  3. canonical submission key  `subk:<sha256[:16]>` - minted by AR-0 inside
     `submit_action` at `action_reality_ledger.py:2734` as
     `sha256(f"{idempotency_key}:att:{attempt_no}")[:16]`. It does NOT exist
     before the first submit, so `propose()` reports it as *derived*
     (`submission_key_minted=False`) and `submit()` reports the real minted
     value. The Linux test asserts the derived value equals the minted value.

Conflict semantics (order section 61)
-------------------------------------
The REAL AR-0 owner raises NO error when the same `idempotency_key` arrives with
different content: `propose_action` returns `{"idempotent_replay": True, ...}`
with the ORIGINAL action and mutates nothing (action_reality_ledger.py:2426-2435).
That is verified at runtime by `_probe_real_owner_replay` whose raw answer is
attached to the conflict it raises - we record what the owner actually does
instead of inventing an owner error.

CT0-10 therefore fails closed ITSELF: `CanonicalLogicalActionConflictError`
carries `failure_code = "LOGICAL_ACTION_CONFLICT"` (the CT0-6 vocabulary, see
`contact_message_action_bridge.RESULT_CONFLICT`). The owner's nearest canonical
payload-conflict error is `PayloadTamperError`, raised by `prepare_action` when a
PREPARED action is offered a different frozen payload
(`action_reality_ledger.py:2519-2527`); `prepare_and_probe_tamper_guard()`
obtains that message verbatim from the real owner.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

# ---------------------------------------------------------------------------
# imports: the portable spine is mandatory, the contact contract is mandatory,
# the real owner is loaded lazily and only on a POSIX host.
# ---------------------------------------------------------------------------
_THIS_DIR = Path(__file__).resolve().parent
_SRC_ROOT = _THIS_DIR.parents[1]
if str(_SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(_SRC_ROOT))

from ct0_10.canonical_spine import (  # noqa: E402
    CanonicalActionRef,
    CanonicalIntegrationError,
    CANONICAL_SUBMISSION_KEY_PREFIX,
    NAMESPACE_ISOLATED_TEST,
    ProductionPathRefusedError,
    assert_isolated_store_root,
    payload_hash,
)

# The canonical owners are vendored in this release: chiyo/life_runtime holds
# the flat modules and chiyo/life_runtime/scripts satisfies the "<root>/scripts"
# contract the loaders check.
_CHIYO_DIR = Path(__file__).resolve().parents[2]
DEFAULT_RUNTIME_SCRIPTS = _CHIYO_DIR / "life_runtime" / "scripts"
DEFAULT_CONTACT_CONTRACT_DIR = _CHIYO_DIR / "contact"
FAILURE_CODE_LOGICAL_ACTION_CONFLICT = "LOGICAL_ACTION_CONFLICT"


class CanonicalAr0AdapterError(CanonicalIntegrationError):
    """Base error for the canonical AR-0 adapter."""


class CanonicalRuntimeUnavailableError(CanonicalAr0AdapterError):
    """The real canonical owner cannot be loaded on this host."""


class CanonicalAr0ContractError(CanonicalAr0AdapterError):
    """The adapter was handed something that is not a frozen CT0-9 request."""


class CanonicalLogicalActionConflictError(CanonicalAr0AdapterError):
    """One logical action, two different contents (order section 61).

    `failure_code` uses the CT0-6 vocabulary because this is the contact-side
    logical-action conflict; `real_owner_*` records what the real AR-0 owner did
    when the same canonical idempotency key arrived with different content, and
    `real_owner_error_verbatim` carries the owner's own payload-conflict text
    when the caller has obtained it (`prepare_and_probe_tamper_guard`).
    """

    failure_code = FAILURE_CODE_LOGICAL_ACTION_CONFLICT

    def __init__(self, message: str, *, contact_submission_key: str, ar0_idempotency_key: str,
                 existing_action_ref: str | None, existing_status: str | None,
                 divergences: Sequence[str], real_owner_replay_probe: Mapping[str, Any],
                 canonical_submission_key: str | None,
                 real_owner_error_verbatim: str | None = None) -> None:
        super().__init__(message)
        self.contact_submission_key = contact_submission_key
        self.ar0_idempotency_key = ar0_idempotency_key
        self.existing_action_ref = existing_action_ref
        self.existing_status = existing_status
        self.divergences = tuple(divergences)
        self.real_owner_replay_probe = dict(real_owner_replay_probe)
        self.canonical_submission_key = canonical_submission_key
        self.real_owner_error_verbatim = real_owner_error_verbatim

    def to_mapping(self) -> dict[str, Any]:
        return {
            "error": type(self).__name__,
            "failure_code": self.failure_code,
            "message": str(self),
            "contact_submission_key": self.contact_submission_key,
            "ar0_idempotency_key": self.ar0_idempotency_key,
            "canonical_submission_key": self.canonical_submission_key,
            "existing_action_ref": self.existing_action_ref,
            "existing_status": self.existing_status,
            "divergences": list(self.divergences),
            "real_owner_raises_on_replay": self.real_owner_replay_probe.get("raised"),
            "real_owner_replay_probe": dict(self.real_owner_replay_probe),
            "real_owner_error_verbatim": self.real_owner_error_verbatim,
        }


# ---------------------------------------------------------------------------
# owner loaders
# ---------------------------------------------------------------------------
def load_ar0_modules(runtime_scripts: Path | str | None = None) -> tuple[Any, Any]:
    """Import the REAL `action_reality_ledger` + `activity_continuity` modules."""
    try:
        import fcntl  # noqa: F401
    except ImportError as exc:  # pragma: no cover - Windows host
        raise CanonicalRuntimeUnavailableError(
            "the canonical AR-0 owner is Linux-only (it imports POSIX fcntl); run this adapter under WSL"
        ) from exc
    scripts = Path(runtime_scripts or DEFAULT_RUNTIME_SCRIPTS)
    if str(scripts) not in sys.path:
        sys.path.insert(0, str(scripts))
    try:
        import action_reality_ledger as ar0
        import activity_continuity as ac
    except ImportError as exc:  # pragma: no cover
        raise CanonicalRuntimeUnavailableError(
            f"cannot import the real AR-0 owner from {scripts}: {exc}"
        ) from exc
    return ar0, ac


def load_contact_request_class(contact_contract_dir: Path | str | None = None) -> type:
    """Import the frozen CT0-9 `ContactMessageActionRequest` contract."""
    contract_dir = Path(contact_contract_dir or DEFAULT_CONTACT_CONTRACT_DIR)
    if str(contract_dir) not in sys.path:
        sys.path.insert(0, str(contract_dir))
    try:
        import contact_message_action_model as contact_model
    except ImportError as exc:
        raise CanonicalAr0ContractError(
            f"cannot import the frozen CT0-9 contact contract from {contract_dir}: {exc}"
        ) from exc
    return contact_model.ContactMessageActionRequest


# ---------------------------------------------------------------------------
# typed results
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class CanonicalAr0ProposalResult:
    """Typed projection of the REAL AR-0 answer to one contact proposal."""

    action_ref: str
    canonical_action_id: str
    status: str
    revision: int
    proposal_ref: str
    contact_submission_key: str
    ar0_idempotency_key: str
    canonical_submission_key: str | None
    canonical_submission_key_minted: bool
    canonical_submission_key_source: str
    declared_payload_hash: str
    ar0_payload_hash: str | None
    canonical_reconciliation_ref: str | None
    canonical_reconciliation_refs: tuple[str, ...]
    idempotent_replay: bool
    created_at: str
    action_kind: str
    target_ref: str
    payload_ref: str
    decision_ref: str | None
    authorization_ref: str | None
    identity: CanonicalActionRef
    notes: tuple[str, ...] = ()

    def to_mapping(self) -> dict[str, Any]:
        return {
            "action_ref": self.action_ref, "canonical_action_id": self.canonical_action_id,
            "status": self.status, "revision": self.revision, "proposal_ref": self.proposal_ref,
            "contact_submission_key": self.contact_submission_key,
            "ar0_idempotency_key": self.ar0_idempotency_key,
            "canonical_submission_key": self.canonical_submission_key,
            "canonical_submission_key_minted": self.canonical_submission_key_minted,
            "canonical_submission_key_source": self.canonical_submission_key_source,
            "declared_payload_hash": self.declared_payload_hash,
            "ar0_payload_hash": self.ar0_payload_hash,
            "canonical_reconciliation_ref": self.canonical_reconciliation_ref,
            "canonical_reconciliation_refs": list(self.canonical_reconciliation_refs),
            "idempotent_replay": self.idempotent_replay, "created_at": self.created_at,
            "action_kind": self.action_kind, "target_ref": self.target_ref,
            "payload_ref": self.payload_ref, "decision_ref": self.decision_ref,
            "authorization_ref": self.authorization_ref,
            "identity": self.identity.to_mapping(), "notes": list(self.notes),
        }


@dataclass(frozen=True, slots=True)
class CanonicalAr0Snapshot:
    """Read-only view of the REAL ActionRecord (order: later phases bind to it)."""

    action_ref: str
    status: str
    revision: int
    action_record: Mapping[str, Any]

    @property
    def terminal(self) -> bool:
        return self.status in {"SUCCEEDED", "FAILED", "CANCELLED"}

    @property
    def submission_key(self) -> str | None:
        return self.action_record.get("submission_key")

    @property
    def last_reconciliation_ref(self) -> str | None:
        return self.action_record.get("last_reconciliation_ref")

    @property
    def reconciliation_refs(self) -> tuple[str, ...]:
        return tuple(self.action_record.get("reconciliation_refs") or ())

    @property
    def receipt_refs(self) -> tuple[str, ...]:
        return tuple(self.action_record.get("receipt_refs") or ())

    @property
    def result_evidence_refs(self) -> tuple[str, ...]:
        return tuple(self.action_record.get("result_evidence_refs") or ())

    def to_mapping(self) -> dict[str, Any]:
        return {"action_ref": self.action_ref, "status": self.status, "revision": self.revision,
                "submission_key": self.submission_key,
                "last_reconciliation_ref": self.last_reconciliation_ref,
                "reconciliation_refs": list(self.reconciliation_refs),
                "receipt_refs": list(self.receipt_refs),
                "action_record": dict(self.action_record)}


def ar0_submission_key_for(idempotency_key: str, *, attempt_no: int = 1) -> str:
    """The key AR-0 WILL mint for attempt `attempt_no` (`action_reality_ledger.py:2734`).

    `subk:<sha256(f"{idempotency_key}:att:{attempt_no}")[:16]>` - a deterministic
    function of the frozen idempotency key and the attempt number only, so it is
    stable across process restart, writer epoch and current time (order 59).
    """
    digest = hashlib.sha256(f"{idempotency_key}:att:{int(attempt_no)}".encode("utf-8")).hexdigest()
    return f"{CANONICAL_SUBMISSION_KEY_PREFIX}{digest[:16]}"


# ---------------------------------------------------------------------------
# adapter
# ---------------------------------------------------------------------------
class CanonicalAr0Adapter:
    """`CanonicalAr0Port` over the REAL AR-0 owner in an isolated store root."""

    def __init__(
        self,
        *,
        store_root: Path | str,
        namespace: str = NAMESPACE_ISOLATED_TEST,
        lease_root: Path | str | None = None,
        runtime_scripts: Path | str | None = None,
        contact_contract_dir: Path | str | None = None,
        caller_module: str = "ct0_10_canonical_ar0_adapter",
        executor_module: str = "sandbox_action_harness",
        provider: Any | None = None,
    ) -> None:
        self.store_root = Path(store_root)
        self.namespace = namespace
        self.lease_root = Path(lease_root) if lease_root is not None else self.store_root.parent / "authority_lease"
        self.caller_module = caller_module
        self.executor_module = executor_module

        self._ar0, self._ac = load_ar0_modules(runtime_scripts)
        self._request_class = load_contact_request_class(contact_contract_dir)

        # 1. the REAL owner's own guard fires first (production homes are refused)
        try:
            self._store = self._ar0.ActionStore(self.store_root, namespace=namespace)
        except Exception as exc:
            if _is_real_production_path(self._ac, self.store_root):
                raise ProductionPathRefusedError(
                    "the real AR-0 owner refused to initialize an ActionStore on a production home: "
                    f"{exc}"
                ) from exc
            raise
        # 2. the portable spine guard: non-production namespace + non-production root
        assert_isolated_store_root(self.store_root, namespace=namespace)

        self._lease: Any | None = None
        self._writer_capability: Any | None = None
        self._executor_capability: Any | None = None
        self._command: Any | None = None
        self._read: Any | None = None
        self._provider = provider
        self._message_adapter: Any | None = None

    # -- authority / services (lazy, isolated only) --------------------------
    def _authority(self) -> tuple[Any, Any]:
        if self._command is None:
            lease = self._ac.ActivityAuthorityLease(self.lease_root)
            lease.acquire()
            writer = self._ar0.ActionRealityAuthority.issue_writer_capability(
                lease, caller_module=self.caller_module, namespace=self.namespace
            )
            executor = self._ar0.ActionRealityAuthority.issue_sandbox_executor_capability(
                caller_module=self.executor_module, namespace=self.namespace
            )
            if self._provider is None:
                self._provider = self._ar0.FakeMessageProvider(supports_status_query=True)
            self._message_adapter = self._ar0.MessageActionAdapter(self._provider)
            self._lease = lease
            self._writer_capability = writer
            self._executor_capability = executor
            self._command = self._ar0.ActionCommandService(
                store=self._store, lease=lease, capability=writer,
                adapters={self._ar0.ACTION_KIND_MESSAGE: self._message_adapter},
            )
            self._read = self._ar0.ActionReadService(self._store)
        return self._command, self._read

    @property
    def provider(self) -> Any | None:
        """The `FakeMessageProvider` transport seam (never a network client)."""
        return self._provider

    def close(self) -> None:
        if self._lease is not None:
            self._lease.release()
            self._lease = None
            self._command = None
            self._read = None

    def __enter__(self) -> "CanonicalAr0Adapter":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # -- request contract ----------------------------------------------------
    def _assert_request(self, request: Any) -> Any:
        if not isinstance(request, self._request_class):
            raise CanonicalAr0ContractError(
                "the canonical AR-0 port consumes a frozen CT0-9 ContactMessageActionRequest; "
                f"got {type(request).__name__!r}"
            )
        return request

    # -- order 57-61: propose ------------------------------------------------
    def propose(self, *, request: Any, created_at: str) -> CanonicalAr0ProposalResult:
        request = self._assert_request(request)
        cmd, read = self._authority()

        prior_state = self._store.load_state()
        prior_action_id = (prior_state.get("idempotency_index") or {}).get(request.idempotency_key)
        if prior_action_id:
            prior_action = (prior_state.get("actions") or {}).get(prior_action_id) or {}
            prior_proposal = (prior_state.get("proposals") or {}).get(prior_action.get("proposal_ref")) or {}
            divergences = self._divergences(prior_action, prior_proposal, request)
            if divergences:
                probe = self._probe_real_owner_replay(cmd, request, created_at=created_at)
                raise CanonicalLogicalActionConflictError(
                    f"{FAILURE_CODE_LOGICAL_ACTION_CONFLICT}: canonical idempotency key "
                    f"{request.idempotency_key!r} already owns {prior_action_id!r} with different content "
                    f"({'; '.join(divergences)})",
                    contact_submission_key=request.idempotency_key,
                    ar0_idempotency_key=request.idempotency_key,
                    existing_action_ref=prior_action_id,
                    existing_status=prior_action.get("status"),
                    divergences=divergences,
                    real_owner_replay_probe=probe,
                    canonical_submission_key=prior_action.get("submission_key"),
                )

        response = cmd.propose_action(
            action_kind=request.action_kind,
            target_ref=request.target_ref,
            payload_ref=request.payload_ref,
            payload_data=dict(request.payload_data),
            idempotency_key=request.idempotency_key,
            source_refs=list(request.source_refs),
            decision_ref=request.decision_ref,
            authorization_ref=request.authorization_ref,
            causal_parent_refs=list(request.causal_parent_refs),
            occurred_at=created_at,
        )
        record = dict(response["action"])
        proposal = dict(response["proposal"])

        minted = record.get("submission_key")
        if minted:
            submission_key, is_minted, source = minted, True, "real AR-0 ActionRecord.submission_key"
        else:
            submission_key = ar0_submission_key_for(request.idempotency_key, attempt_no=1)
            is_minted = False
            source = (f"derived, not yet minted: subk = sha256(f'{{idempotency_key}}:att:1')[:16] "
                      f"(action_reality_ledger.py:2734); submit() mints the real value")

        reconciliation_refs = tuple(record.get("reconciliation_refs") or ())
        identity = CanonicalActionRef(
            action_ref=str(record["action_id"]),
            contact_submission_key=request.idempotency_key,
            canonical_submission_key=submission_key,
            canonical_reconciliation_ref=record.get("last_reconciliation_ref"),
            canonical_action_id=str(record["action_id"]),
        )
        return CanonicalAr0ProposalResult(
            action_ref=str(record["action_id"]),
            canonical_action_id=str(record["action_id"]),
            status=str(record["status"]),
            revision=int(record["revision"]),
            proposal_ref=str(proposal.get("proposal_id") or record.get("proposal_ref")),
            contact_submission_key=request.idempotency_key,
            ar0_idempotency_key=str(record["idempotency_key"]),
            canonical_submission_key=submission_key,
            canonical_submission_key_minted=is_minted,
            canonical_submission_key_source=source,
            declared_payload_hash=payload_hash(request.payload_data),
            ar0_payload_hash=record.get("payload_hash"),
            canonical_reconciliation_ref=record.get("last_reconciliation_ref"),
            canonical_reconciliation_refs=reconciliation_refs,
            idempotent_replay=bool(response["idempotent_replay"]),
            created_at=str(record["created_at"]),
            action_kind=str(record["action_kind"]),
            target_ref=str(record["target_ref"]),
            payload_ref=str(record["payload_ref"]),
            decision_ref=record.get("decision_ref"),
            authorization_ref=record.get("authorization_ref"),
            identity=identity,
            notes=(
                "AR-0 stores the proposed payload_data verbatim; ActionRecord.payload_hash stays None "
                "until prepare_action freezes the payload, and MessageActionAdapter.validate_prepare then "
                "projects the payload to {channel,text} (action_reality_ledger.py:1473-1480), so the frozen "
                "payload hash is NOT expected to equal declared_payload_hash.",
                "the contact key is passed through as AR-0's own idempotency_key because the frozen CT0-6 "
                "contract states the AR-0 owner receives it that way; the canonical submission key is a "
                "separate, AR-0-minted identity and is never a rename of it.",
            ),
        )

    # -- transport seam (sandbox only, explicit opt-in) ----------------------
    def prepare(self, action_ref: str, *, occurred_at: str | None = None) -> Mapping[str, Any]:
        """Advance PROPOSED -> PREPARED through the REAL owner (no side effect)."""
        cmd, _ = self._authority()
        return dict(cmd.prepare_action(action_id=action_ref, occurred_at=occurred_at))

    def schedule(self, action_ref: str, *, scheduled_for: str,
                 occurred_at: str | None = None) -> Mapping[str, Any]:
        """Advance PREPARED -> SCHEDULED through the REAL owner.

        The real owner's signature is
        `ActionCommandService.schedule_action(*, action_id, scheduled_for, occurred_at=None)`
        (scripts/action_reality_ledger.py:2574), so `scheduled_for` is REQUIRED: the AR-0
        owner decides the schedule instant, not this adapter, and CT0-10 will not invent a
        default. `CT0_10_INTEGRATION_DEBT_AR0_SCHEDULE_ARG` was raised by the canonical
        chain runner and is closed here.
        """
        cmd, _ = self._authority()
        return dict(cmd.schedule_action(action_id=action_ref, scheduled_for=scheduled_for,
                                       occurred_at=occurred_at))

    def submit(self, action_ref: str, *, occurred_at: str | None = None) -> Mapping[str, Any]:
        """Cross the AR-0 submission fence through the in-tree sandbox transport.

        The adapter is registered with `MessageActionAdapter` over
        `FakeMessageProvider`: no network, no sockets, no real provider. This is
        the ONLY call in this module with an external side effect (in-memory),
        and it is never triggered by `propose()`.
        """
        cmd, _ = self._authority()
        return dict(cmd.submit_action(action_id=action_ref,
                                      executor_capability=self._executor_capability,
                                      occurred_at=occurred_at))

    def prepare_and_probe_tamper_guard(self, *, tampered_payload: Mapping[str, Any],
                                       action_ref: str | None = None) -> dict[str, Any]:
        """Obtain the REAL owner's payload-conflict error verbatim.

        `prepare_action` on a PREPARED action with a different frozen payload
        raises `PayloadTamperError` (action_reality_ledger.py:2519-2527). If the
        action is still PROPOSED this call first advances it to PREPARED, i.e. it
        DOES mutate the isolated ledger - callers are told so explicitly.
        """
        cmd, read = self._authority()
        results: dict[str, Any] = {"advanced_to_prepared": False, "verbatim_error": None,
                                   "error_type": None, "action_ref": action_ref}
        if action_ref is None:
            return results
        record = read.get_action(action_ref)
        if record is None:
            return results
        if record["status"] == self._ar0.ACTION_STATUS_PROPOSED:
            cmd.prepare_action(action_id=action_ref)
            results["advanced_to_prepared"] = True
        try:
            cmd.prepare_action(action_id=action_ref, payload_override=dict(tampered_payload))
            results["verbatim_error"] = None
            results["error_type"] = None
        except Exception as exc:  # the real owner's own error, recorded verbatim
            results["verbatim_error"] = str(exc)
            results["error_type"] = type(exc).__name__
        return results

    def reconcile(self, action_ref: str, *, explicit_result: str,
                  evidence_refs: Sequence[str] = ("evidence:ct0_10_probe",),
                  occurred_at: str | None = None) -> dict[str, Any]:
        """Deterministic 0-LLM reconciliation through the REAL owner.

        AR-0 only reconciles an UNKNOWN action (`action_reality_ledger.py:3161-3164`)
        and mints the reconciliation identity `arec:<sha256(f"{action_id}:{index}:{result}")[:16]>`
        (`action_reality_ledger.py:3217`). This pass-through exists so a caller can
        observe a REAL `arec:` identity in the adapter's typed result.
        """
        cmd, _ = self._authority()
        return dict(cmd.reconcile_action(action_id=action_ref, explicit_evidence_result=explicit_result,
                                         explicit_evidence_refs=list(evidence_refs),
                                         occurred_at=occurred_at))

    # -- read-only snapshot (order: later phases bind receipts to it) --------
    def snapshot(self, action_ref: str) -> CanonicalAr0Snapshot | None:
        _, read = self._authority()
        record = read.get_action(action_ref)
        if record is None:
            return None
        return CanonicalAr0Snapshot(action_ref=str(record["action_id"]), status=str(record["status"]),
                                    revision=int(record["revision"]), action_record=dict(record))

    def receipts(self, action_ref: str) -> tuple[Mapping[str, Any], ...]:
        """The real AR-0 receipts for an action (evidence input for AR-1)."""
        _, read = self._authority()
        return tuple(dict(item) for item in read.list_receipts(action_ref))

    def result_evidences(self, action_ref: str) -> tuple[Mapping[str, Any], ...]:
        """The real AR-0 ResultEvidence rows for an action (evidence input for AR-1)."""
        _, read = self._authority()
        return tuple(dict(item) for item in read.list_result_evidences(action_ref))

    def attempts(self, action_ref: str) -> tuple[Mapping[str, Any], ...]:
        _, read = self._authority()
        return tuple(dict(item) for item in read.list_attempts(action_ref))

    # -- internals -----------------------------------------------------------
    @staticmethod
    def _canonical_json(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    def _divergences(self, prior_action: Mapping[str, Any], prior_proposal: Mapping[str, Any],
                     request: Any) -> list[str]:
        """Every way the incoming request differs from the already-owned logical action."""
        out: list[str] = []
        if prior_action.get("action_kind") != request.action_kind:
            out.append(f"action_kind {prior_action.get('action_kind')!r} != {request.action_kind!r}")
        if prior_action.get("target_ref") != request.target_ref:
            out.append(f"recipient/target_ref {prior_action.get('target_ref')!r} != {request.target_ref!r}")
        if prior_action.get("payload_ref") != request.payload_ref:
            out.append(f"payload_ref {prior_action.get('payload_ref')!r} != {request.payload_ref!r}")
        if prior_action.get("decision_ref") != request.decision_ref:
            out.append(f"decision_ref {prior_action.get('decision_ref')!r} != {request.decision_ref!r}")
        if prior_action.get("authorization_ref") != request.authorization_ref:
            out.append(f"authorization_ref {prior_action.get('authorization_ref')!r} != "
                       f"{request.authorization_ref!r}")
        prior_payload = prior_proposal.get("payload_data")
        if prior_payload is not None and self._canonical_json(prior_payload) != self._canonical_json(
            request.payload_data
        ):
            out.append("content payload_data differs (channel/text/content_hash/correlation differ)")
        return out

    def _probe_real_owner_replay(self, cmd: Any, request: Any, *, created_at: str) -> dict[str, Any]:
        """Ask the REAL owner again with the same key and the divergent content.

        Zero-mutation probe: `propose_action` returns the existing record and
        commits nothing when the idempotency key is already indexed
        (action_reality_ledger.py:2426-2435). The raw answer is returned so the
        caller can see exactly what the real owner does instead of raising.
        """
        probe: dict[str, Any] = {"raised": None, "response": None}
        try:
            response = cmd.propose_action(
                action_kind=request.action_kind, target_ref=request.target_ref,
                payload_ref=request.payload_ref, payload_data=dict(request.payload_data),
                idempotency_key=request.idempotency_key, source_refs=list(request.source_refs),
                decision_ref=request.decision_ref, authorization_ref=request.authorization_ref,
                causal_parent_refs=list(request.causal_parent_refs), occurred_at=created_at,
            )
            probe["response"] = {
                "idempotent_replay": bool(response["idempotent_replay"]),
                "action_id": response["action"]["action_id"],
                "status": response["action"]["status"],
                "proposal_ref": response["action"].get("proposal_ref"),
                "stored_payload_ref": response["action"].get("payload_ref"),
                "stored_target_ref": response["action"].get("target_ref"),
                "stored_payload_data": response["proposal"].get("payload_data"),
            }
        except Exception as exc:
            probe["raised"] = f"{type(exc).__name__}: {exc}"
        return probe


def _is_real_production_path(activity_continuity: Any, path: Path) -> bool:
    try:
        return bool(activity_continuity.is_production_path(path))
    except Exception:  # pragma: no cover
        return False


__all__ = [
    "CanonicalAr0Adapter", "CanonicalAr0AdapterError", "CanonicalAr0ContractError",
    "CanonicalAr0ProposalResult", "CanonicalAr0Snapshot", "CanonicalLogicalActionConflictError",
    "CanonicalRuntimeUnavailableError", "FAILURE_CODE_LOGICAL_ACTION_CONFLICT",
    "ar0_submission_key_for", "load_ar0_modules", "load_contact_request_class",
]
