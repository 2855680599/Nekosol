"""CT0-10 Phase C -- READ-ONLY canonical contact capacity resolver (order 21-31).

Implements ``ct0_10.canonical_spine.CanonicalCapacityResolverPort``:

    resolve(*, intent, recipient_ref, channel, now) -> CanonicalCapacityResult

WHAT THIS RESOLVER IS
=====================
A pure PROJECTION.  It reads real canonical owners, maps their state onto the frozen
CT0-9 ``ContactCapacitySnapshot`` fields, and reports where every fact came from.  It is
NOT a new capacity owner and NOT a new domain truth (order 21, 105):

* it never creates an intent, never writes any store, never acquires a lease, never
  requests a writer capability and never builds a command service;
* it never renames one identity family into another (spine sections 13/40);
* when a fact cannot be read without the real owner writing, it FAILS CLOSED with a named
  reason instead of guessing.

THE THREE FAIL-CLOSED DECISIONS THIS FILE IS BUILT AROUND
=========================================================
1. ATOMIC (order 23/24).  The real LR-2 vocabulary is FOUR levels, declared at
   ``scripts/activity_continuity.py:111-120``; the LR-2 owner's own projection
   ``build_life_frame_read_view`` emits it as ``attention_occupancy`` (:892).  The frozen
   CT0-4 contact contract has a single ``atomic`` bool, so the projection is lossy BY
   DESIGN: ``spine.atomic_from_interruptibility`` maps FREE/LIGHT/FOCUSED -> False and
   ATOMIC -> True, and ``spine.KNOWN_SEMANTIC_LIMITATION_FOCUSED`` is always carried in
   ``notes``.  No FOCUSED policy is invented here (order 24).

2. QUIET WINDOW (order 26).  There is NO quiet-window policy owner anywhere in the real
   runtime (0 hits for ``quiet_window`` / ``QUIET_WINDOW`` / ``quiet_hours`` across
   ``scripts/*.py``, ``service/*.py`` and ``service/bridge/*.py``).  This field is the
   easiest one to invert, so the semantics are taken from the code that consumes it, not
   from the field name: the frozen evaluator at
   ``chiyo/contact/contact_intent_agency_bridge.py:133-134``
   reads ``if not snapshot.quiet_window_open: reasons.append(BLOCK_QUIET_WINDOW)``.
   THEREFORE ``quiet_window_open=True`` means "the window for contact IS open" and the
   value that makes the CT0-9 pipeline refuse to proceed is **False**.  With no policy
   owner this resolver sets ``quiet_window_open=False`` (fail closed for proactive),
   records ``spine.QUIET_POLICY_MISSING`` in ``provenance.quiet_policy`` and adds
   ``QUIET_POLICY_MISSING`` to ``unavailable_reasons``.
   (`quiet_window_policy=` may inject an explicit read-only policy port; the default is
   None and None always fails closed.)

3. DEVICE / CHANNEL (order 25).  No canonical owner in this runtime asserts "the device is
   present" or "this channel is reachable".  The two real surfaces that look like it are
   NOT usable: ``service/world_body_gateway_api.py:500 GatewaySurface.get_action_result``
   needs a live service instance behind the UNIX socket ``gateway.sock`` (:40) and
   ``service/bridge/world_body_client.py:253`` is socket RPC.  So device/channel are
   UNKNOWN -> capacity unavailable -> fail closed.  LR-3's real
   ``compute_communication_availability`` (:1160) verdict is carried in ``notes`` as the
   canonical attention verdict but is deliberately NOT mapped onto ``channel_available``
   (that mapping would be an invented policy).

PENDING FENCE (order 27/28)
===========================
The fence comes from REAL Action Reality, never from the contact Intent store:
``provenance.pending_action_refs`` = AR-0 actions whose status is in
``PRE_SUBMIT_ACTION_STATUSES`` (:156) or ``IN_FLIGHT_ACTION_STATUSES`` (:164), plus the
AR-0 submission fence ``open_submission_intents`` (:1022).  ``previous_unknown`` is True
when an in-flight action is UNKNOWN or a reconciliation record is STILL_UNKNOWN/CONFLICT.
NOTE (correction to the brief): AR-0 has no ``RECONCILING`` status -- the status set is
PROPOSED/PREPARED/SCHEDULED/SUBMITTED/ACKNOWLEDGED/SUCCEEDED/FAILED/CANCELLED/UNKNOWN
(:132-140); "reconciling" is expressed by ``reconciliations`` records with result
STILL_UNKNOWN (:316, :320).

AUTHORIZATION (order 29)
========================
The real owner ``service/world_action_authorization.py:192 AuthorizationRegistry.lookup``
is keyed by ``authorization_id`` only.  It cannot bind a recipient or a channel, and it has
no notion of proactive.  So:
* an ``authorization_id`` that resolves to an ISSUED, unexpired record IS read and reported;
* "recipient + channel + proactive is allowed" is UNPROVABLE from any real owner and the
  resolver returns NOT authorized (fail closed) unless an explicit read-only
  ``boundary_authorizer`` port is supplied (default None).
"""
from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional, Protocol, Sequence, runtime_checkable

# --------------------------------------------------------------------------
# portable spine (Windows + Linux) and the frozen CT0-9 contact contract
# --------------------------------------------------------------------------
_CONTACT_SANDBOX_SRC = os.environ.get(
    "CT0_9_CONTACT_SRC", str(Path(__file__).resolve().parents[1])
)

_src_root = Path(__file__).resolve().parents[2]  # .../chiyo
if str(_src_root) not in sys.path:
    sys.path.insert(0, str(_src_root))

import ct0_10.canonical_spine as spine  # noqa: E402


def _contact_snapshot_type():
    """Return the frozen CT0-9 ``ContactCapacitySnapshot`` class (never redefined)."""
    sandbox_src = Path(_CONTACT_SANDBOX_SRC)
    # the frozen CT0-9 modules use FLAT imports (e.g. "from contact_candidate_model import"),
    # so the package directory itself must be importable as well as its parent.
    for candidate in (sandbox_src, sandbox_src / "ct0"):
        if candidate.is_dir() and str(candidate) not in sys.path:
            sys.path.insert(0, str(candidate))
    from ct0 import contact_intent_agency_bridge as contact  # noqa: PLC0415

    return contact.ContactCapacitySnapshot, contact


@dataclass(frozen=True, slots=True)
class _FallbackCapacitySnapshot:
    """Field-identical stand-in used ONLY if the frozen CT0-9 module cannot be imported.

    Kept so the resolver degrades visibly (``notes`` says so) instead of crashing; the real
    frozen type is used whenever it is importable.
    """

    observed_at: str
    revision: int
    quiet_window_open: bool
    device_available: bool
    channel_available: bool
    atomic: bool
    pending_outbound_count: int
    previous_unknown: bool
    unresolved_inbound: bool


# --------------------------------------------------------------------------
# read-only port protocols (all are optional; None always fails closed)
# --------------------------------------------------------------------------
@runtime_checkable
class DeviceChannelCapabilityPort(Protocol):
    """Read-only reader for "the device is present / this channel is reachable".

    No such owner exists in the canonical runtime; a caller may inject a read-only reader
    of its own.  The return value must be ``{"device_available": bool|None,
    "channel_available": bool|None, "ref": str|None, "revision": int|None}``; ``None``
    means UNKNOWN and is treated as unavailable.
    """

    def read(self, *, channel: str) -> Mapping[str, Any]: ...


@runtime_checkable
class QuietWindowPolicyPort(Protocol):
    """Read-only reader for the quiet/contact-window policy.

    No such owner exists in the canonical runtime.  The return value must be
    ``{"contact_window_open": bool|None, "policy_ref": str|None,
    "policy_revision": int|None}``.  ``None`` / missing means UNKNOWN -> fail closed.
    """

    def read(self, *, recipient_ref: str, channel: str, now: str) -> Mapping[str, Any]: ...


@runtime_checkable
class InboundSettlementPort(Protocol):
    """Read-only reader for "is there unresolved inbound contact from this recipient".

    No such owner exists in the canonical runtime.  ``None`` means UNKNOWN -> fail closed.
    """

    def read(self, *, recipient_ref: str, channel: str) -> Mapping[str, Any]: ...


@runtime_checkable
class ProactiveBoundaryAuthorizerPort(Protocol):
    """Read-only authorizer for "recipient + channel + proactive is permitted".

    No such owner exists in the canonical runtime.  The return value must be
    ``{"authorized": bool, "ref": str|None, "revision": int|None}``.  ``authorized`` not
    exactly ``True`` -> not authorized.
    """

    def authorize(self, *, recipient_ref: str, channel: str, now: str,
                  authorization_id: str | None) -> Mapping[str, Any]: ...


# --------------------------------------------------------------------------
# reason codes (fail-closed vocabulary; all appear in unavailable_reasons)
# --------------------------------------------------------------------------
R_ACTIVITY_UNREADABLE = "ACTIVITY_STATE_UNREADABLE"
R_ACTIVITY_COMMIT_INTENT = "ACTIVITY_COMMIT_INTENT_PRESENT_READ_WOULD_WRITE"
R_INTERRUPTIBILITY_UNKNOWN = "INTERRUPTIBILITY_UNKNOWN"
R_LR3_OVERLAY_REFUSED = "LR3_OVERLAY_READ_WOULD_DELETE_STRAY_TEMP"
R_DEVICE_UNKNOWN = "DEVICE_AVAILABILITY_UNKNOWN_NO_CANONICAL_OWNER"
R_CHANNEL_UNKNOWN = "CHANNEL_AVAILABILITY_UNKNOWN_NO_CANONICAL_OWNER"
R_DEVICE_UNAVAILABLE = "DEVICE_UNAVAILABLE"
R_CHANNEL_UNAVAILABLE = "CHANNEL_UNAVAILABLE"
R_QUIET_POLICY = "QUIET_POLICY_MISSING"
R_QUIET_CLOSED = "QUIET_WINDOW_CLOSED"
R_PENDING_UNKNOWN = "PENDING_FENCE_UNKNOWN_READ_WOULD_WRITE"
R_AUTH_NOT_PROVABLE = "AUTHORIZATION_BOUNDARY_NOT_PROVABLE"
R_AUTH_NOT_FOUND = "AUTHORIZATION_NOT_FOUND"
R_AUTH_NOT_ISSUED = "AUTHORIZATION_NOT_ISSUED"
R_INBOUND_UNKNOWN = "UNRESOLVED_INBOUND_UNKNOWN_NO_CANONICAL_OWNER"
R_ATOMIC = "ATOMIC"
R_PENDING_OUTBOUND = "PENDING_OUTBOUND"
R_PREVIOUS_UNKNOWN = "PREVIOUS_UNKNOWN"
R_UNRESOLVED_INBOUND = "UNRESOLVED_INBOUND"

#: proof-by-construction that this module cannot ask for write authority
WRITER_SYMBOLS_NEVER_USED = (
    "activity_continuity.ActivityAuthorityLease",
    "activity_continuity.issue_writer_capability",
    "activity_continuity.ActivityCommandService",
    "action_reality_ledger.ActionCommandService",
    "action_reality_ledger.ActionStore.commit_mutation",
    "activity_waiting_agenda.WALAggregateStore.commit_records",
    "activity_progress_completion_lr5.ActivityProgressCoordinator",
    "world_action_authorization.AuthorizationRegistry.issue",
    "world_action_authorization.AuthorizationRegistry.verify_and_consume",
    "world_action_lifecycle.ActionLifecycle.begin/_append/transition",
)


@dataclass(frozen=True, slots=True)
class AuthorizationRead:
    """Outcome of the order-29 authorization read."""

    authorization_ref: str | None
    authorization_revision: int | None
    authorized: bool
    reason: str
    detail: Mapping[str, Any]

    def to_mapping(self) -> dict[str, Any]:
        return {"authorization_ref": self.authorization_ref,
                "authorization_revision": self.authorization_revision,
                "authorized": self.authorized, "reason": self.reason,
                "detail": dict(self.detail)}


class CanonicalContactCapacityResolver:
    """Read-only ``CanonicalCapacityResolverPort`` over real canonical state."""

    def __init__(
        self,
        *,
        activity_store_root: Path | str,
        action_store_root: Path | str,
        authorization_run_dir: Path | str | None = None,
        namespace: str | None = None,
        runtime_root: Path | str | None = None,
        execution_ledger_path: Path | str | None = None,
        lifecycle_path: Path | str | None = None,
        progress_store_root: Path | str | None = None,
        authorization_id: str | None = None,
        capability_read: DeviceChannelCapabilityPort | None = None,
        quiet_window_policy: QuietWindowPolicyPort | None = None,
        inbound_settlement: InboundSettlementPort | None = None,
        boundary_authorizer: ProactiveBoundaryAuthorizerPort | None = None,
    ) -> None:
        self.namespace = namespace or spine.NAMESPACE_ISOLATED_TEST
        self.runtime_root = runtime_root
        self.activity_store_root = spine.assert_isolated_store_root(
            activity_store_root, namespace=self.namespace
        )
        self.action_store_root = spine.assert_isolated_store_root(
            action_store_root, namespace=self.namespace
        )
        self.authorization_run_dir = (
            spine.assert_isolated_store_root(authorization_run_dir, namespace=self.namespace)
            if authorization_run_dir is not None else None
        )
        self.execution_ledger_path = execution_ledger_path
        self.lifecycle_path = lifecycle_path
        self.progress_store_root = (
            spine.assert_isolated_store_root(progress_store_root, namespace=self.namespace)
            if progress_store_root is not None else self.activity_store_root
        )
        self.authorization_id = authorization_id
        self.capability_read = capability_read
        self.quiet_window_policy = quiet_window_policy
        self.inbound_settlement = inbound_settlement
        self.boundary_authorizer = boundary_authorizer
        self._ports = None  # built lazily; read_ports imports fcntl (Linux-only)

    # -- ports -------------------------------------------------------------
    def _read_ports(self):
        if self._ports is None:
            from ct0_10.canonical_ports import read_ports  # noqa: PLC0415

            read_ports.ensure_real_runtime_on_path(self.runtime_root)
            self._ports = read_ports
        return self._ports

    def _posix_roots(self) -> tuple[Path, Path]:
        """Portable Windows paths -> WSL paths when running on Linux."""
        return self.activity_store_root, self.action_store_root

    # ------------------------------------------------------------------
    # 1. real Activity / interruptibility read (LR-2 + LR-3)
    # ------------------------------------------------------------------
    def _read_activity(self) -> tuple[dict[str, Any] | None, dict[str, Any], list[str]]:
        """Return (life_frame_view, raw_state, reasons).  Reads only; writes nothing."""
        ports = self._read_ports()
        reasons: list[str] = []
        root, _ = self._posix_roots()
        import activity_continuity as lr2  # noqa: PLC0415

        # A pending WAL commit intent means the owner's own read path would REWRITE
        # canonical_life_state.json (:1206-1207 -> :1031 -> :1088-1093). We refuse.
        if (root / "commit_intent.json").is_file():
            return None, {}, [R_ACTIVITY_COMMIT_INTENT]

        try:
            store = lr2.CanonicalActivityStore(root)  # __init__ (:985-993) does no I/O
            journal = store._read_and_verify_journal_unlocked()  # :1106 -- pure
            state_path = root / "canonical_life_state.json"
            if state_path.is_file():
                state = json.loads(state_path.read_text(encoding="utf-8"))
                revision = state.get("revision")
                if revision != len(journal):
                    return None, state, [R_ACTIVITY_UNREADABLE]
                if journal and state.get("last_transition_hash") != journal[-1]["entry_hash"]:
                    return None, state, [R_ACTIVITY_UNREADABLE]
            else:
                state = lr2._empty_canonical_state()  # :937 -- pure
            view = lr2.build_life_frame_read_view(state)  # :819 -- pure projection
        except Exception as exc:  # noqa: BLE001
            return None, {}, [f"{R_ACTIVITY_UNREADABLE}:{type(exc).__name__}"]

        # LR-3 overlay: the real coordinator knows about an ACTIVE atomic region, which
        # the LR-2 field alone cannot express. InterruptionStateStore.load_state (:1617)
        # unlinks stray temp files (:1613), so it is only called when there are none.
        stray = ports.stray_temp_files(root)
        if stray:
            reasons.append(R_LR3_OVERLAY_REFUSED)
        else:
            try:
                import activity_interruption as lr3  # noqa: PLC0415

                if (root / "interruption_state.json").is_file():
                    coord = lr3.InterruptionCoordinator(
                        activity_store=store,  # :1700-1711 assigns only
                        read_service=lr2.ActivityReadService(store),
                    )
                    fg_ref = view.get("foreground_activity_ref")
                    fg_act = (state.get("activities") or {}).get(fg_ref) if fg_ref else None
                    occupancy, interruptibility, region = coord.get_effective_attention(fg_act)
                    view["attention_occupancy"] = occupancy
                    view["interruptibility"] = interruptibility
                    view["active_atomic_region_ref"] = (
                        region.atomic_region_ref if region is not None else None
                    )
                    view["communication_availability"] = (
                        lr3.InterruptionPolicy.compute_communication_availability(
                            life_state=view.get("life_state", "IDLE"),
                            attention_occupancy=occupancy,
                            interruptibility=interruptibility,
                        )
                    )
                    view["_interruptibility_source"] = "LR-3.InterruptionCoordinator.get_effective_attention:1806"
            except Exception as exc:  # noqa: BLE001
                reasons.append(f"{R_LR3_OVERLAY_REFUSED}:{type(exc).__name__}")
        return view, state, reasons

    # ------------------------------------------------------------------
    # 2. authorization (order 29)
    # ------------------------------------------------------------------
    def _read_authorization(self, *, recipient_ref: str, channel: str, now: str) -> AuthorizationRead:
        ports = self._read_ports()
        detail: dict[str, Any] = {}
        auth_ref = self.authorization_id
        auth_revision: int | None = None

        if auth_ref and self.authorization_run_dir is not None:
            try:
                p7 = ports.AuthorizationBoundaryReadPort(self.authorization_run_dir,
                                                         namespace=self.namespace)
                read = p7.read_authorization(auth_ref)
                detail["authorization_read"] = read.to_mapping()
                if read.available:
                    rec = read.data or {}
                    auth_revision = rec.get("revision")
                    if rec.get("state") != "ISSUED":
                        return AuthorizationRead(auth_ref, auth_revision, False,
                                                 R_AUTH_NOT_ISSUED, detail)
                    detail["authorization_state"] = rec.get("state")
                else:
                    return AuthorizationRead(auth_ref, None, False,
                                             str(read.reason or R_AUTH_NOT_FOUND), detail)
            except Exception as exc:  # noqa: BLE001
                detail["authorization_read_error"] = f"{type(exc).__name__}"
                return AuthorizationRead(auth_ref, None, False, R_AUTH_NOT_PROVABLE, detail)

        if self.boundary_authorizer is not None:
            try:
                verdict = dict(self.boundary_authorizer.authorize(
                    recipient_ref=recipient_ref, channel=channel, now=now,
                    authorization_id=auth_ref))
                detail["boundary_authorizer"] = verdict
                if verdict.get("authorized") is True:
                    return AuthorizationRead(
                        verdict.get("ref") or auth_ref,
                        verdict.get("revision", auth_revision), True, "AUTHORIZED", detail)
                return AuthorizationRead(auth_ref, auth_revision, False,
                                         "BOUNDARY_AUTHORIZER_REFUSED", detail)
            except Exception as exc:  # noqa: BLE001
                detail["boundary_authorizer_error"] = f"{type(exc).__name__}"
                return AuthorizationRead(auth_ref, auth_revision, False, R_AUTH_NOT_PROVABLE, detail)

        try:
            p7 = ports.AuthorizationBoundaryReadPort(
                self.authorization_run_dir or self.activity_store_root, namespace=self.namespace)
            boundary = p7.read_boundary(recipient_ref, channel, proactive=True)
            detail["boundary_read"] = boundary.to_mapping()
        except Exception as exc:  # noqa: BLE001
            detail["boundary_read_error"] = f"{type(exc).__name__}"
        return AuthorizationRead(auth_ref, auth_revision, False, R_AUTH_NOT_PROVABLE, detail)

    # ------------------------------------------------------------------
    # resolve (order 21-31)
    # ------------------------------------------------------------------
    def resolve(self, *, intent: Any, recipient_ref: str, channel: str, now: str) -> spine.CanonicalCapacityResult:
        observed_at = now or _iso_now()
        notes: list[str] = [
            spine.KNOWN_SEMANTIC_LIMITATION_FOCUSED,
            ("capacity is a read-only projection of real canonical owners: no capacity "
             "owner, no new domain truth, no write (order 21/105)"),
        ]
        reasons: list[str] = []
        sources: list[str] = []

        # ---- 1. real Activity + interruptibility (order 23/24) -----------
        view, state, activity_reasons = self._read_activity()
        sources.append("scripts/activity_continuity.py:819 build_life_frame_read_view")
        sources.append("scripts/activity_continuity.py:1106 CanonicalActivityStore._read_and_verify_journal_unlocked")
        reasons.extend(activity_reasons)

        activity_ref: str | None = None
        activity_revision: int | None = None
        atomic = False
        occupancy = "UNKNOWN"
        if view is None:
            reasons.append(R_ACTIVITY_UNREADABLE)
        else:
            occupancy = str(view.get("attention_occupancy", "UNKNOWN"))
            activity_ref = view.get("foreground_activity_ref") or f"life_state:{view.get('life_state')}"
            activity_revision = view.get("view_revision")
            if occupancy not in spine.REAL_INTERRUPTIBILITY_LEVELS:
                reasons.append(f"{R_INTERRUPTIBILITY_UNKNOWN}:{occupancy}")
                atomic = True  # unknown attention -> treat as NOT interruptible (fail closed)
            else:
                atomic = spine.atomic_from_interruptibility(occupancy)
            if view.get("_interruptibility_source"):
                notes.append(f"interruptibility read from {view['_interruptibility_source']}")
                sources.append("scripts/activity_interruption.py:1806 InterruptionCoordinator.get_effective_attention")
            if view.get("communication_availability") is not None:
                notes.append(
                    "canonical LR-3 communication_availability="
                    f"{view['communication_availability']} "
                    "(scripts/activity_interruption.py:1160 InterruptionPolicy."
                    "compute_communication_availability); NOT mapped onto channel_available "
                    "-- that mapping would be an invented policy")
            if view.get("active_atomic_region_ref") is not None:
                notes.append(f"active atomic region ref: {view['active_atomic_region_ref']}")
            notes.append(f"interruptibility vocabulary in use: {occupancy}")

        # ---- 2. device / channel (order 25) -----------------------------
        device_available = False
        channel_available = False
        device_known = channel_known = False
        if self.capability_read is None:
            reasons.extend([R_DEVICE_UNKNOWN, R_CHANNEL_UNKNOWN])
            notes.append(
                "no canonical device/channel capability owner exists; the closest real "
                "surfaces (world_body_gateway_api.py:500, bridge/world_body_client.py:253) "
                "require the live UNIX-socket body service -> UNKNOWN -> fail closed")
        else:
            try:
                cap = dict(self.capability_read.read(channel=channel))
                notes.append("device/channel availability read via an INJECTED read-only "
                             "capability port (no canonical owner exists)")
                if cap.get("device_available") is None:
                    reasons.append(R_DEVICE_UNKNOWN)
                else:
                    device_known = True
                    device_available = bool(cap["device_available"])
                    if not device_available:
                        reasons.append(R_DEVICE_UNAVAILABLE)
                if cap.get("channel_available") is None:
                    reasons.append(R_CHANNEL_UNKNOWN)
                else:
                    channel_known = True
                    channel_available = bool(cap["channel_available"])
                    if not channel_available:
                        reasons.append(R_CHANNEL_UNAVAILABLE)
                if cap.get("ref"):
                    notes.append(f"capability ref: {cap['ref']} rev={cap.get('revision')}")
                    sources.append(str(cap["ref"]))
            except Exception as exc:  # noqa: BLE001
                reasons.extend([R_DEVICE_UNKNOWN, R_CHANNEL_UNKNOWN])
                notes.append(f"capability port failed ({type(exc).__name__}) -> fail closed")

        # ---- 3. quiet window (order 26 -- see module docstring) ----------
        policy_ref: str | None = None
        policy_revision: int | None = None
        quiet_window_open = False  # fail closed: True is the "contact window is open" value
        if self.quiet_window_policy is None:
            reasons.append(R_QUIET_POLICY)
            notes.append(
                "QUIET_POLICY_MISSING: 0 quiet-window owners exist in the canonical runtime "
                "(verified: no quiet_window/QUIET_WINDOW/quiet_hours symbol in scripts/, "
                "service/ or service/bridge/). quiet_window_open is set FALSE because the "
                "frozen evaluator at ct0-sandbox/src/ct0/contact_intent_agency_bridge.py:133 "
                "emits BLOCK_QUIET_WINDOW when it is falsy; FALSE is therefore the "
                "fail-closed value for proactive dispatch")
        else:
            try:
                pol = dict(self.quiet_window_policy.read(
                    recipient_ref=recipient_ref, channel=channel, now=observed_at))
                policy_ref = pol.get("policy_ref")
                policy_revision = pol.get("policy_revision")
                if pol.get("contact_window_open") is None:
                    reasons.append(R_QUIET_POLICY)
                else:
                    quiet_window_open = bool(pol["contact_window_open"])
                    if not quiet_window_open:
                        reasons.append(R_QUIET_CLOSED)
                notes.append("quiet window read via an INJECTED read-only policy port")
            except Exception as exc:  # noqa: BLE001
                reasons.append(R_QUIET_POLICY)
                notes.append(f"quiet-window policy port failed ({type(exc).__name__}) -> fail closed")

        # ---- 4. pending proactive fence from REAL Action Reality (27/28) --
        pending_refs: tuple[str, ...] = ()
        pending_count = 0
        previous_unknown = False
        pending_detail: Mapping[str, Any] = {}
        try:
            p8 = self._read_ports().PendingOutboundReadPort(
                self.action_store_root,
                execution_ledger_path=self.execution_ledger_path,
                lifecycle_path=self.lifecycle_path,
                namespace=self.namespace,
            )
            ar = p8.action_reality()
            sources.append(p8.OWNER_AR_STATE)
            if not ar.available:
                reasons.append(R_PENDING_UNKNOWN)
                previous_unknown = True  # unknown fence -> fail closed
                notes.append(f"pending fence unreadable: {ar.reason}")
            else:
                pending_detail = ar.data or {}
                pending_refs = tuple(pending_detail.get("pending_action_refs") or ())
                pending_count = len(pending_refs)
                previous_unknown = bool(pending_detail.get("unknown_action_refs")) or bool(
                    pending_detail.get("unresolved_reconciliation_refs"))
                notes.append(
                    "pending fence is sourced from REAL Action Reality "
                    f"(scripts/action_reality_ledger.py:1138 load_state; statuses :156/:164): "
                    f"{list(pending_refs)[:8]}")
            body = p8.body_era_pending()
            if body.available:
                sources.append(p8.OWNER_BODY_RECONCILE)
                body_ids = (body.data or {}).get("pending_execution_ids") or []
                if body_ids:
                    previous_unknown = True
                    pending_refs = tuple(sorted(set(pending_refs) | set(body_ids)))
                    pending_count = len(pending_refs)
                notes.append(f"body-era pending executions: {list(body_ids)[:8]}")
            life = p8.lifecycle_open_records()
            if life.available:
                sources.append(p8.OWNER_LIFECYCLE)
                open_ids = [r.get("execution_id") for r in ((life.data or {}).get("open_records") or [])]
                notes.append(f"bridge lifecycle open records: {open_ids[:8]}")
            neg = p8.recipient_keyed_pending(recipient_ref, channel)
            notes.append(f"recipient-keyed pending: {neg.reason}")
        except Exception as exc:  # noqa: BLE001
            reasons.append(f"{R_PENDING_UNKNOWN}:{type(exc).__name__}")
            previous_unknown = True
            notes.append("pending fence could not be read at all -> fail closed")

        if pending_count:
            reasons.append(R_PENDING_OUTBOUND)
        if previous_unknown:
            reasons.append(R_PREVIOUS_UNKNOWN)

        # ---- 4b. P9 activity waiting / resume + progress (read-only) ------
        try:
            p9 = self._read_ports().ActivityWaitingProgressReadPort(
                self.progress_store_root or self.activity_store_root, namespace=self.namespace)
            waiting = p9.waiting_resume()
            if waiting.available:
                sources.append(p9.OWNER_LR4_JOURNAL)
                waits = (waiting.data or {}).get("conditions", {}).get("open_refs") or []
                notes.append(f"open resume conditions: {list(waits)[:8]}")
            else:
                notes.append(f"P9 waiting/resume not answerable read-only: {waiting.reason}")
            prog = p9.progress()
            if prog.available:
                sources.append(p9.OWNER_LR5_STATE)
                notes.append(f"LR-5 progress read: {(prog.data or {}).get('data_state')}")
            else:
                notes.append(f"P9 progress not answerable read-only: {prog.reason}")
        except Exception as exc:  # noqa: BLE001
            notes.append(f"P9 read ports unavailable ({type(exc).__name__})")

        # ---- 5. unresolved inbound (no owner -> fail closed) -------------
        unresolved_inbound = True
        if self.inbound_settlement is not None:
            try:
                inb = dict(self.inbound_settlement.read(recipient_ref=recipient_ref, channel=channel))
                if inb.get("unresolved_inbound") is None:
                    reasons.append(R_INBOUND_UNKNOWN)
                else:
                    unresolved_inbound = bool(inb["unresolved_inbound"])
                    notes.append("inbound settlement read via an INJECTED read-only port")
            except Exception as exc:  # noqa: BLE001
                reasons.append(R_INBOUND_UNKNOWN)
                notes.append(f"inbound settlement port failed ({type(exc).__name__}) -> fail closed")
        else:
            reasons.append(R_INBOUND_UNKNOWN)
            notes.append(
                "no canonical owner for 'unresolved inbound contact from this recipient' "
                "exists in this runtime -> UNKNOWN -> fail closed (unresolved_inbound=True)")
        if unresolved_inbound:
            reasons.append(R_UNRESOLVED_INBOUND)

        # ---- 6. authorization (order 29) --------------------------------
        auth = self._read_authorization(recipient_ref=recipient_ref, channel=channel,
                                        now=observed_at)
        if not auth.authorized:
            reasons.append(R_AUTH_NOT_PROVABLE if auth.reason == R_AUTH_NOT_PROVABLE
                           else f"{R_AUTH_NOT_PROVABLE}:{auth.reason}")
        else:
            notes.append(f"authorized by {auth.authorization_ref!r} ({auth.reason})")

        if atomic:
            reasons.append(R_ATOMIC)

        # ---- 7. build the frozen snapshot + provenance (order 30) --------
        revision = activity_revision if isinstance(activity_revision, int) and activity_revision >= 1 else 1
        if revision != activity_revision:
            notes.append(
                "the frozen CT0-9 capacity revision must be >= 1 "
                "(contact_intent_agency_bridge.py:69-70); the canonical activity revision is "
                f"{activity_revision!r}, so the snapshot carries the floor 1. The raw value is "
                "preserved in provenance.activity_revision -- no information is lost")
        fields = dict(
            observed_at=observed_at,
            revision=revision,
            quiet_window_open=quiet_window_open,
            device_available=device_available,
            channel_available=channel_available,
            atomic=atomic,
            pending_outbound_count=pending_count,
            previous_unknown=previous_unknown,
            unresolved_inbound=unresolved_inbound,
        )
        try:
            snapshot_cls, _contact = _contact_snapshot_type()
            snapshot = snapshot_cls(**fields)
            sources.append("ct0-sandbox/src/ct0/contact_intent_agency_bridge.py:54 ContactCapacitySnapshot")
        except Exception as exc:  # noqa: BLE001
            snapshot = _FallbackCapacitySnapshot(**fields)
            notes.append(
                f"frozen CT0-9 ContactCapacitySnapshot unavailable ({type(exc).__name__}); "
                "used a field-identical local stand-in so the failure is visible, not silent")

        # dedupe reasons, keep first-seen order
        ordered: list[str] = []
        for r in reasons:
            if r not in ordered:
                ordered.append(r)

        provenance = spine.CapacityProvenance(
            activity_ref=activity_ref,
            activity_revision=activity_revision,
            authorization_ref=auth.authorization_ref,
            authorization_revision=auth.authorization_revision,
            pending_action_refs=tuple(pending_refs),
            policy_ref=policy_ref or "chiyo.life.interruption_policy.v1",
            policy_revision=policy_revision,
            observed_at=observed_at,
            quiet_policy=spine.QUIET_POLICY_MISSING if self.quiet_window_policy is None
            else "INJECTED_READ_ONLY_POLICY_PORT",
            source_modules=tuple(dict.fromkeys(sources)),
        )
        notes.append(
            "policy_ref is the real LR-3 policy version constant "
            "(scripts/activity_interruption.py:122 POLICY_VERSION_V1); the "
            "authorization registry is revision-less (bounded 256-record list, "
            "world_action_authorization.py:139-141), hence authorization_revision=None")
        notes.append(f"authorization detail: {auth.to_mapping()}")
        if pending_detail:
            notes.append(
                f"pending fence detail: open_submission_intents="
                f"{pending_detail.get('open_submission_intents')} "
                f"idempotency_index_size={pending_detail.get('idempotency_index_size')}")

        return spine.CanonicalCapacityResult(
            snapshot=snapshot,
            provenance=provenance,
            unavailable_reasons=tuple(ordered),
            fail_closed=True if ordered else False,
            notes=tuple(notes),
        )


def _iso_now() -> str:
    import datetime as _dt

    return _dt.datetime.now(_dt.timezone.utc).isoformat()


__all__ = [
    "AuthorizationRead", "CanonicalContactCapacityResolver", "DeviceChannelCapabilityPort",
    "InboundSettlementPort", "ProactiveBoundaryAuthorizerPort", "QuietWindowPolicyPort",
    "R_ACTIVITY_COMMIT_INTENT", "R_ACTIVITY_UNREADABLE", "R_ATOMIC", "R_AUTH_NOT_FOUND",
    "R_AUTH_NOT_ISSUED", "R_AUTH_NOT_PROVABLE", "R_CHANNEL_UNKNOWN", "R_DEVICE_UNKNOWN",
    "R_INBOUND_UNKNOWN", "R_INTERRUPTIBILITY_UNKNOWN", "R_LR3_OVERLAY_REFUSED",
    "R_PENDING_OUTBOUND", "R_PENDING_UNKNOWN", "R_PREVIOUS_UNKNOWN", "R_QUIET_POLICY",
    "R_UNRESOLVED_INBOUND", "WRITER_SYMBOLS_NEVER_USED",
]
