"""CT0-10 canonical READ-ONLY ports: P7 (authorization/boundary), P8 (pending-outbound /
UNKNOWN), P9 (activity waiting / resume + progress).

WHY THIS MODULE EXISTS
=====================
Order sections 21-31 demand a capacity resolver that reads REAL canonical state and
writes nothing (order 21, 105).  Every function below is a thin, READ-ONLY wrapper
around one real owner read path; each wrapper states the exact real function it calls
as ``"<relative path>:<line> <symbol>"`` so the call site is auditable without reading
this module's logic.

THE READ-ONLY FINDING THAT SHAPES THIS MODULE (verified by reading the real source)
===================================================================================
The real owners' *documented* read services are NOT side-effect free:

* ``scripts/activity_continuity.py`` ``ActivityReadService`` docstring says
  "Possesses ZERO write capability" (:1545-1548) but ``get_current_life_view`` (:1553)
  goes through ``CanonicalActivityStore.load_verified_state_and_journal`` (:1372) which
  takes ``_store_io_lock(shared=False)`` -> an EXCLUSIVE flock, ``run_dir.mkdir`` and a
  ``.store_io.lock`` create (:996-1002); the same chain calls
  ``_read_and_verify_state_unlocked`` (:1206) which calls
  ``_reconcile_commit_intent_unlocked`` (:1031) -- and that path can DELETE
  ``commit_intent.json`` (:1062) and REWRITE ``canonical_life_state.json`` (:1088-1093).
  So "read" through the real ActivityReadService can mutate the canonical store.
* ``scripts/action_reality_ledger.py`` ``ActionStore.load_state`` (:1138) can flush a
  dirty snapshot (:1142) and run WAL recovery (:1143-1144 -> :1077) which appends to the
  journal and rewrites state.
* ``scripts/activity_waiting_agenda.py`` ``WALAggregateStore.load_state`` (:1389) takes
  the same exclusive lock (:1384 -> :1184-1185 mkdir) and its state read
  (:1362 -> :1309) deletes stray temp files.
* ``scripts/activity_progress_completion_lr5.py`` ``ProgressStore.load_state`` (:2066)
  calls ``self.store_root.mkdir(parents=True, exist_ok=True)`` (:2067), recovers from WAL
  (:2068-2071) and can WRITE a rebuilt snapshot (:2091-2092, :2107-2110).
* ``scripts/activity_interruption.py`` ``InterruptionStateStore.load_state`` (:1617)
  unlinks stray ``.*.tmp.*`` files (:1613) -- a bounded housekeeping delete.

THEREFORE: these ports do NOT use the lock-taking read services.  They use the owners'
narrowest verified-side-effect-free readers (the ``*_unlocked`` readers, which take no
lock, create no directory and delete nothing) plus the owner's own pure projection
functions, and they FAIL CLOSED with a named reason when a store is in a state where the
only real read path would write (a WAL / commit-intent present) or when the owner read
would delete files.

READ-ONLY CONTRACT (enforced by the test, not just asserted)
===========================================================
* No lease is acquired, no writer capability is requested, no command service is built.
* No intent, record, journal entry, lock or directory is created by any call below.
* No sockets, no network, no credential values, no production roots
  (``spine.assert_isolated_store_root`` is applied to every store root first).
"""
from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

# --------------------------------------------------------------------------
# real runtime modules (LINUX-ONLY: POSIX fcntl) -- imported lazily by callers
# --------------------------------------------------------------------------
DEFAULT_RUNTIME_ROOT = Path(__file__).resolve().parents[2] / "life_runtime"
#: insert order matters: the LAST insertion wins, so ``service`` shadows
#: ``service/bridge`` for modules that exist in both (e.g. world_action_authorization).
_RUNTIME_SUBDIRS = ("scripts", "service/bridge", "service")


def ensure_real_runtime_on_path(runtime_root: Path | str | None = None) -> Path:
    """Put the real runtime modules on ``sys.path``; return the resolved root."""
    root = Path(runtime_root or os.environ.get("CT0_10_RUNTIME_ROOT", DEFAULT_RUNTIME_ROOT))
    for sub in _RUNTIME_SUBDIRS:
        candidate = root / sub
        if candidate.is_dir():
            text = str(candidate)
            if text in sys.path:
                sys.path.remove(text)
            sys.path.insert(0, text)
    return root


def _spine():
    import ct0_10.canonical_spine as spine  # local import: avoids a hard import cycle

    return spine


# --------------------------------------------------------------------------
# result shape
# --------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class PortRead:
    """One read-only port result.

    ``owner_function`` names the exact real function that produced ``data``.
    ``no_data_behaviour`` states what this port does when the real owner has no data.
    """

    port: str
    read_only: bool = True
    available: bool = False
    owner_function: str = ""
    reason: str | None = None
    data: Any = None
    no_data_behaviour: str = ""
    notes: tuple[str, ...] = ()

    def to_mapping(self) -> dict[str, Any]:
        return {
            "port": self.port,
            "read_only": self.read_only,
            "available": self.available,
            "owner_function": self.owner_function,
            "reason": self.reason,
            "data": self.data,
            "no_data_behaviour": self.no_data_behaviour,
            "notes": list(self.notes),
        }


# --------------------------------------------------------------------------
# shared helpers
# --------------------------------------------------------------------------
def _isolated(root: Path | str, *, namespace: str | None = None) -> Path:
    spine = _spine()
    return spine.assert_isolated_store_root(
        root, namespace=namespace or spine.NAMESPACE_ISOLATED_TEST
    )


def stray_temp_files(directory: Path | str) -> tuple[str, ...]:
    """Atomic-write leftovers (``.<file>.tmp.<pid>.<hex>``) present in ``directory``.

    These are exactly the names the real owners delete from their read paths, so their
    presence is what decides whether a lock-taking owner read can be called at all.
    """
    target = Path(directory)
    if not target.is_dir():
        return ()
    found: list[str] = []
    for item in sorted(target.iterdir()):
        if item.is_file() and item.name.startswith(".") and ".tmp." in item.name:
            found.append(item.name)
    return tuple(name for name in found)


@dataclass(frozen=True, slots=True)
class StoreFileFingerprint:
    """Cheap non-item-level fingerprint: file mtime_ns / size / sha256 per store file.

    Used only to prove that a read changed nothing (never for per-record hashing).
    """

    path: str
    exists: bool
    mtime_ns: int | None = None
    size: int | None = None
    sha256: str | None = None

    def to_mapping(self) -> dict[str, Any]:
        return {"path": self.path, "exists": self.exists, "mtime_ns": self.mtime_ns,
                "size": self.size, "sha256": self.sha256}


def fingerprint(paths: Sequence[Path | str]) -> tuple[StoreFileFingerprint, ...]:
    import hashlib

    out: list[StoreFileFingerprint] = []
    for raw in paths:
        p = Path(raw)
        if not p.is_file():
            out.append(StoreFileFingerprint(path=str(p), exists=False))
            continue
        st = p.stat()
        digest = hashlib.sha256(p.read_bytes()).hexdigest()
        out.append(StoreFileFingerprint(path=str(p), exists=True, mtime_ns=st.st_mtime_ns,
                                        size=st.st_size, sha256=digest))
    return tuple(out)


# ==========================================================================
# P7 -- authorization / boundary read
# ==========================================================================
AUTH_REGISTRY_NAME = "action_authorizations.json"


class AuthorizationBoundaryReadPort:
    """P7: read the REAL one-shot authorization registry (read-only surface only).

    Real owner: ``AuthorizationRegistry``, ``service/world_action_authorization.py:56``.
    The ONLY read-only method it has is ``lookup`` (:192-197), which calls ``_load``
    (:62-72) -- that just reads/parses JSON, creates nothing, takes no lock.

    IMPORTANT SCOPE CORRECTION (the code wins over the brief): the registry is keyed by
    ``authorization_id`` ONLY.  There is NO recipient-keyed and NO channel-keyed query
    anywhere in it, and it carries no notion of "proactive".  ``verify_and_consume``
    (:145) does know verb/actor/args/decision, but it MUTATES (``state = CONSUMED`` at
    :187-189) so this port never calls it.  Consequently a request of the form
    "is recipient X on channel Y allowed to be contacted proactively?" CANNOT be proven
    from this owner and this port says so instead of guessing.
    """

    OWNER_LOOKUP = "service/world_action_authorization.py:192 AuthorizationRegistry.lookup"
    OWNER_LOAD = "service/world_action_authorization.py:62 AuthorizationRegistry._load"
    WRITE_PATH_AVOIDED = "service/world_action_authorization.py:145 verify_and_consume (mutates: state=CONSUMED, :187-189)"

    def __init__(self, run_dir: Path | str, *, namespace: str | None = None) -> None:
        self.run_dir = _isolated(run_dir, namespace=namespace)
        self._registry = None

    def _get_registry(self):
        if self._registry is None:
            ensure_real_runtime_on_path()
            import world_action_authorization as wauth  # service/

            self._registry = wauth.AuthorizationRegistry(self.run_dir)
        return self._registry

    def read_authorization(self, authorization_id: str) -> PortRead:
        """Read one authorization record by id.  Pure read (``_load`` at :62)."""
        if not isinstance(authorization_id, str) or not authorization_id.strip():
            return PortRead(
                port="P7.authorization",
                owner_function=self.OWNER_LOOKUP,
                reason="AUTHORIZATION_ID_REQUIRED",
                no_data_behaviour="no id supplied -> nothing read, not authorized",
            )
        registry_path = self.run_dir / AUTH_REGISTRY_NAME
        try:
            record = self._get_registry().lookup(authorization_id.strip())
        except Exception as exc:  # unreadable registry: fail closed, never repair
            return PortRead(
                port="P7.authorization",
                owner_function=self.OWNER_LOOKUP,
                reason=f"AUTHORIZATION_REGISTRY_UNREADABLE:{type(exc).__name__}",
                data={"registry_present": registry_path.is_file()},
                no_data_behaviour="unreadable registry -> fail closed; this port never repairs it",
            )
        if record is None:
            return PortRead(
                port="P7.authorization",
                owner_function=self.OWNER_LOOKUP,
                reason="AUTHORIZATION_NOT_FOUND",
                data={"registry_present": registry_path.is_file(),
                      "authorization_id": authorization_id.strip()},
                no_data_behaviour=(
                    "no record for this id -> not authorized; the port does not fall back to "
                    "any other authority"
                ),
            )
        return PortRead(
            port="P7.authorization",
            available=True,
            owner_function=self.OWNER_LOOKUP,
            data={
                "authorization_id": record.get("authorization_id"),
                "state": record.get("state"),
                "verb": record.get("verb"),
                "actor": record.get("actor"),
                "decision_id": record.get("decision_id"),
                "execution_id": record.get("execution_id"),
                "intent_id": record.get("intent_id"),
                "issued_at": record.get("issued_at"),
                "expires_at": record.get("expires_at"),
                # the registry is revision-less: it is a bounded list of at most 256
                # records (:139-141). Reporting a revision here would invent one.
                "revision": None,
            },
            no_data_behaviour="record present -> returned verbatim; no field is inferred",
            notes=(
                f"write path deliberately not used: {self.WRITE_PATH_AVOIDED}",
                "registry is keyed by authorization_id only; no recipient/channel index exists",
            ),
        )

    def read_boundary(self, recipient_ref: str, channel: str, *, proactive: bool = True) -> PortRead:
        """Prove "recipient+channel+proactive is allowed" -- there is no such owner."""
        return PortRead(
            port="P7.boundary",
            owner_function=self.OWNER_LOOKUP,
            reason="NO_RECIPIENT_CHANNEL_BOUNDARY_OWNER",
            data={"recipient_ref": recipient_ref, "channel": channel, "proactive": bool(proactive),
                  "registry_records_are_keyed_by": "authorization_id"},
            no_data_behaviour=(
                "the real registry answers only 'is this authorization_id ISSUED and not "
                "expired/consumed'; it cannot bind a recipient or a channel, so a proactive "
                "recipient+channel permission is UNPROVABLE -> caller must fail closed"
            ),
            notes=(
                "default_path for the lifecycle ledger is a different owner "
                "(service/bridge/world_action_lifecycle.py:76) and carries no recipient either",
                f"avoided mutating path: {self.WRITE_PATH_AVOIDED}",
            ),
        )


# ==========================================================================
# P8 -- pending-outbound / UNKNOWN query
# ==========================================================================
ACTION_STATE_FILENAME = "action_reality_state.json"
ACTION_JOURNAL_FILENAME = "action_reality_journal.jsonl"
ACTION_WAL_FILENAME = "action_reality.wal"


class PendingOutboundReadPort:
    """P8: pending / UNKNOWN outbound actions from REAL Action Reality, plus body-era
    pending executions and bridge lifecycle open records.

    Real owners:
      * ``scripts/action_reality_ledger.py:1138 ActionStore.load_state`` (guarded -- see
        ``FAIL-CLOSED GUARDS``)
      * ``scripts/action_reality_ledger.py:1025 ActionStore.read_journal``
      * ``scripts/action_reality_ledger.py:156/164/172`` PRE_SUBMIT / IN_FLIGHT /
        TERMINAL status sets, ``:1017 idempotency_index``, ``:1022 open_submission_intents``
      * ``service/world_body_reconcile.py:208 pending_execution_ids``
      * ``scripts/world_execution_ledger.py:250 ExecutionLedger.read_all`` (pure read)
      * ``service/bridge/world_action_lifecycle.py:249 ActionLifecycle.open_records``
        (-> ``states`` :150 -> ``_read_all`` :117; pure read, no lock)

    FAIL-CLOSED GUARDS
    ------------------
    ``ActionStore.load_state`` writes when a WAL exists (:1143-1144 -> :1077) or when a
    dirty snapshot is pending (:1142).  This port calls it only for a freshly constructed
    store (so ``_dirty_snapshot`` is None) and only when ``action_reality.wal`` is absent;
    otherwise it returns ``ACTION_WAL_PRESENT_READ_WOULD_RECOVER`` and reads nothing.
    """

    OWNER_AR_STATE = "scripts/action_reality_ledger.py:1138 ActionStore.load_state"
    OWNER_AR_JOURNAL = "scripts/action_reality_ledger.py:1025 ActionStore.read_journal"
    OWNER_AR_STATUSES = "scripts/action_reality_ledger.py:156 PRE_SUBMIT_ACTION_STATUSES + :164 IN_FLIGHT_ACTION_STATUSES"
    OWNER_BODY_RECONCILE = "service/world_body_reconcile.py:208 pending_execution_ids"
    OWNER_EXEC_LEDGER = "scripts/world_execution_ledger.py:250 ExecutionLedger.read_all"
    OWNER_LIFECYCLE = "service/bridge/world_action_lifecycle.py:249 ActionLifecycle.open_records"
    #: what the brief hoped for and what actually exists:
    RECIPIENT_KEYED_QUERY_ABSENT = (
        "NO recipient-keyed (or channel-keyed) pending query exists anywhere in the canonical "
        "runtime. scripts/action_reality_ledger.py indexes pending work by action_id "
        "(:1022 open_submission_intents), by idempotency key (:1017 idempotency_index) and by "
        "status; the bridge lifecycle indexes by execution_id "
        "(service/bridge/world_action_lifecycle.py:144 _key). The only 'recipient_id' in the "
        "runtime belongs to in-world NPC messaging (scripts/world_local_messaging.py:379-402), "
        "which is not a pending-outbound fence."
    )

    def __init__(self, action_store_root: Path | str, *, execution_ledger_path: Path | str | None = None,
                 lifecycle_path: Path | str | None = None, namespace: str | None = None) -> None:
        self.action_store_root = _isolated(action_store_root, namespace=namespace)
        self.execution_ledger_path = (
            _isolated(execution_ledger_path, namespace=namespace)
            if execution_ledger_path is not None else None
        )
        self.lifecycle_path = (
            _isolated(lifecycle_path, namespace=namespace) if lifecycle_path is not None else None
        )

    # -- action reality ----------------------------------------------------
    def action_reality(self) -> PortRead:
        """Pending / UNKNOWN actions straight from the AR-0 ActionStore."""
        wal = self.action_store_root / ACTION_WAL_FILENAME
        if wal.is_file():
            return PortRead(
                port="P8.action_reality",
                owner_function=self.OWNER_AR_STATE,
                reason="ACTION_WAL_PRESENT_READ_WOULD_RECOVER",
                data={"wal_present": True},
                no_data_behaviour=(
                    "AR-0 recovery writes during a read (:1143-1144 -> :1077), so this port "
                    "reads nothing and the fence must be treated as UNKNOWN -> fail closed"
                ),
            )
        ensure_real_runtime_on_path()
        try:
            import action_reality_ledger as ar0  # scripts/

            store = ar0.ActionStore(self.action_store_root)
            state = store.load_state()  # guarded above: no WAL, no dirty snapshot
        except Exception as exc:
            return PortRead(
                port="P8.action_reality",
                owner_function=self.OWNER_AR_STATE,
                reason=f"ACTION_REALITY_UNREADABLE:{type(exc).__name__}",
                data={"state_present": (self.action_store_root / ACTION_STATE_FILENAME).is_file()},
                no_data_behaviour="unreadable -> fence UNKNOWN; the port never repairs the store",
            )

        pre = set(getattr(ar0, "PRE_SUBMIT_ACTION_STATUSES", ()))
        in_flight = set(getattr(ar0, "IN_FLIGHT_ACTION_STATUSES", ()))
        pending_threshold = pre | in_flight

        actions = state.get("actions") or {}
        pending: list[dict[str, Any]] = []
        for action_id in sorted(actions):
            rec = actions[action_id] or {}
            if rec.get("status") in pending_threshold:
                pending.append({"action_id": action_id, "status": rec.get("status"),
                                "action_kind": rec.get("action_kind")})
        open_submission_intents = sorted((state.get("open_submission_intents") or {}).keys())
        idempotency_keys = sorted((state.get("idempotency_index") or {}).keys())
        unknown_actions = sorted(a["action_id"] for a in pending if a["status"] == "UNKNOWN")
        unresolved_recon: list[dict[str, Any]] = []
        for recon_id, rec in sorted((state.get("reconciliations") or {}).items()):
            if (rec or {}).get("result") in ("STILL_UNKNOWN", "CONFLICT"):
                unresolved_recon.append({"reconciliation_id": recon_id,
                                         "action_id": (rec or {}).get("action_id"),
                                         "result": (rec or {}).get("result")})
        pending_refs = sorted({a["action_id"] for a in pending} | set(open_submission_intents))
        message_refs = sorted(a["action_id"] for a in pending
                              if a["action_kind"] == "MESSAGE_ACTION")
        return PortRead(
            port="P8.action_reality",
            available=True,
            owner_function=self.OWNER_AR_STATE,
            data={
                "store_revision": state.get("revision"),
                "pending_action_refs": pending_refs,
                "pending_actions": pending,
                "pending_message_action_refs": message_refs,
                "open_submission_intents": open_submission_intents,
                "idempotency_index_size": len(idempotency_keys),
                "idempotency_index_sample": idempotency_keys[:5],
                "unknown_action_refs": unknown_actions,
                "unresolved_reconciliation_refs": unresolved_recon,
            },
            no_data_behaviour=(
                "no state file and no journal -> the owner reports an empty store; this port "
                "reports zero pending (empty is knowledge, absence of a reader is not)"
            ),
            notes=(f"pending threshold statuses: {self.OWNER_AR_STATUSES}",
                   "open_submission_intents (:1022) is the AR-0 submission fence and is "
                   "surfaced as pending refs even when the action status is PREPARED"),
        )

    # -- body-era pending executions ---------------------------------------
    def body_era_pending(self) -> PortRead:
        """Body-era (v2) executions in flight or UNKNOWN, via the real reconcile reader."""
        if self.execution_ledger_path is None:
            return PortRead(
                port="P8.body_era",
                owner_function=self.OWNER_BODY_RECONCILE,
                reason="EXECUTION_LEDGER_PATH_NOT_CONFIGURED",
                no_data_behaviour="not configured -> nothing read; caller must not assume empty",
            )
        ensure_real_runtime_on_path()
        try:
            import world_body_reconcile as wrec  # service/
            import world_execution_ledger as wled  # scripts/

            ledger = wled.ExecutionLedger(self.execution_ledger_path)
            pending_ids = list(wrec.pending_execution_ids(ledger))
            frames = {eid: ledger.frame(eid) for eid in pending_ids}
        except Exception as exc:
            return PortRead(
                port="P8.body_era",
                owner_function=self.OWNER_BODY_RECONCILE,
                reason=f"BODY_ERA_PENDING_UNREADABLE:{type(exc).__name__}",
                no_data_behaviour="unreadable -> pending body-era work is UNKNOWN; fail closed",
            )
        return PortRead(
            port="P8.body_era",
            available=True,
            owner_function=self.OWNER_BODY_RECONCILE,
            data={
                "pending_execution_ids": pending_ids,
                "frames": {eid: {k: frames[eid].get(k) for k in ("state", "result", "execution_id")}
                           for eid in pending_ids},
                "in_flight_states": list(getattr(wrec, "IN_FLIGHT_STATES", ())),
            },
            no_data_behaviour=(
                "ledger file absent -> ExecutionLedger.read_all() returns [] (:242-243) and "
                "pending_execution_ids reports none; the port reports an empty list, distinctly "
                "labelled as empty-not-unknown"
            ),
            notes=(f"pure read: {self.OWNER_EXEC_LEDGER}",),
        )

    # -- bridge lifecycle --------------------------------------------------
    def lifecycle_open_records(self) -> PortRead:
        """Non-terminal bridge lifecycle records (what could become an orphan)."""
        if self.lifecycle_path is None:
            return PortRead(
                port="P8.lifecycle",
                owner_function=self.OWNER_LIFECYCLE,
                reason="LIFECYCLE_PATH_NOT_CONFIGURED",
                no_data_behaviour="not configured -> nothing read",
            )
        ensure_real_runtime_on_path()
        try:
            import world_action_lifecycle as wlife  # service/bridge

            records = wlife.ActionLifecycle(self.lifecycle_path).open_records()
        except Exception as exc:
            return PortRead(
                port="P8.lifecycle",
                owner_function=self.OWNER_LIFECYCLE,
                reason=f"LIFECYCLE_UNREADABLE:{type(exc).__name__}",
                no_data_behaviour="unreadable journal -> open records UNKNOWN; fail closed",
            )
        return PortRead(
            port="P8.lifecycle",
            available=True,
            owner_function=self.OWNER_LIFECYCLE,
            data={
                "open_records": [
                    {"execution_id": r.get("execution_id"), "state": r.get("state"),
                     "intent_id": (r.get("record") or {}).get("intent_id"),
                     "decision_id": (r.get("record") or {}).get("decision_id")}
                    for r in records
                ],
                "terminal_states": list(getattr(wlife, "TERMINAL", ())),
            },
            no_data_behaviour=(
                "journal file absent -> ``_read_all`` returns [] (:118-119); the port reports an "
                "empty list, and an empty list is NOT evidence that nothing was ever submitted"
            ),
            notes=(f"pure read: {self.OWNER_LIFECYCLE} -> states :150 -> _read_all :117",),
        )

    # -- the honest negative -----------------------------------------------
    def recipient_keyed_pending(self, recipient_ref: str, channel: str | None = None) -> PortRead:
        """There is no recipient-keyed pending query. Stated plainly, with evidence."""
        return PortRead(
            port="P8.recipient_keyed",
            owner_function="",
            reason="RECIPIENT_KEYED_PENDING_QUERY_ABSENT",
            data={"recipient_ref": recipient_ref, "channel": channel,
                  "evidence": self.RECIPIENT_KEYED_QUERY_ABSENT},
            no_data_behaviour=(
                "not answerable by any real owner; a caller needing per-recipient pending state "
                "must treat it as UNKNOWN and fail closed"
            ),
        )


# ==========================================================================
# P9 -- activity waiting / resume + progress read
# ==========================================================================
LR4_JOURNAL_CONDITIONS = "resume_condition_transitions.jsonl"
LR4_STATE_CONDITIONS = "resume_conditions_state.json"
LR4_JOURNAL_AGENDA = "agenda_transitions.jsonl"
LR4_STATE_AGENDA = "agenda_state.json"
LR4_JOURNAL_ELIGIBILITY = "resume_eligibility_transitions.jsonl"
LR4_STATE_ELIGIBILITY = "resume_eligibility_state.json"
LR5_STATE_FILENAME = "activity_progress_state.json"
LR5_JOURNAL_FILENAME = "activity_progress_journal.jsonl"
LR5_WAL_FILENAME = "activity_progress.wal"


class ActivityWaitingProgressReadPort:
    """P9: waiting / resume conditions, agenda due items, resume eligibility, and
    progress / completion overlay -- all read-only.

    Real owners:
      * LR-4 ``scripts/activity_waiting_agenda.py:1271 WALAggregateStore._read_journal_unlocked``
        (verified pure: reads bytes, verifies the hash chain, writes nothing)
      * LR-4 open-status predicates the owners' own queries use:
        ``:140 OPEN_CONDITION_STATUSES``, ``:200 OPEN_AGENDA_STATUSES``,
        ``:227 OPEN_ELIGIBILITY_STATUSES``
      * LR-5 ``scripts/activity_progress_completion_lr5.py:2066 ProgressStore.load_state`` is
        DELIBERATELY NOT CALLED (mkdir :2067, WAL recovery :2068-2071, snapshot rebuild
        write :2091-2092 / :2107-2110).  This port reads the owner's canonical state file and
        projects exactly the fields ``ActivityProgressReadService.get_progress_state``
        (:2485-2496) projects, using the owner's own constants (:225, :355).

    WHY NOT THE REAL READ QUERIES (finding)
    ---------------------------------------
    ``ActivityWaitingAgendaCoordinator.list_conditions`` (:3144), ``list_agenda_items``
    (:3160) and ``list_eligibilities`` (:3176) are the real read queries, but they live on a
    coordinator whose ``__init__`` (:2021-2058) REQUIRES a held ``ActivityAuthorityLease``
    and ISSUES three LR-4 writer capabilities.  A write capability is therefore demanded
    just to reach the read queries; the narrowest read path is the store's own unlocked
    journal reader plus its state file.  (The same applies to LR-3's
    ``InterruptionCoordinator.get_effective_attention`` -- see the capacity resolver.)
    """

    OWNER_LR4_JOURNAL = "scripts/activity_waiting_agenda.py:1271 WALAggregateStore._read_journal_unlocked"
    OWNER_LR4_STATUSES = ("scripts/activity_waiting_agenda.py:140 OPEN_CONDITION_STATUSES, "
                          ":200 OPEN_AGENDA_STATUSES, :227 OPEN_ELIGIBILITY_STATUSES")
    OWNER_LR5_STATE = "scripts/activity_progress_completion_lr5.py:2066 ProgressStore.load_state"
    OWNER_LR5_PROJECTION = "scripts/activity_progress_completion_lr5.py:2485 ActivityProgressReadService.get_progress_state"
    OWNER_LR5_OVERLAY = "scripts/activity_progress_completion_lr5.py:2530 ActivityProgressReadService.get_progress_overlay"

    def __init__(self, activity_store_root: Path | str, *, namespace: str | None = None) -> None:
        self.root = _isolated(activity_store_root, namespace=namespace)

    # -- LR-4 --------------------------------------------------------------
    def waiting_resume(self, *, activity_id: str | None = None) -> PortRead:
        """Open resume conditions / agenda items / resume eligibilities (LR-4)."""
        ensure_real_runtime_on_path()
        import activity_waiting_agenda as lr4  # scripts/

        stores = (
            ("conditions", lr4.ResumeConditionStore(self.root), "conditions", LR4_STATE_CONDITIONS,
             LR4_JOURNAL_CONDITIONS, lr4.OPEN_CONDITION_STATUSES, "condition_id"),
            ("agenda_items", lr4.AgendaStore(self.root), "agenda_items", LR4_STATE_AGENDA,
             LR4_JOURNAL_AGENDA, lr4.OPEN_AGENDA_STATUSES, "agenda_id"),
            ("eligibilities", lr4.ResumeEligibilityStore(self.root), "eligibilities",
             LR4_STATE_ELIGIBILITY, LR4_JOURNAL_ELIGIBILITY, lr4.OPEN_ELIGIBILITY_STATUSES,
             "eligibility_id"),
        )
        out: dict[str, Any] = {}
        blocked: list[str] = []
        for name, store, collection_key, state_filename, journal_filename, open_statuses, id_field in stores:
            store_dir = Path(store.store_dir)
            stray = stray_temp_files(store_dir)
            if (store_dir / "commit_intent.json").is_file():
                blocked.append(f"{name}:LR4_COMMIT_INTENT_PRESENT_READ_WOULD_RECONCILE")
                continue
            if stray:
                blocked.append(f"{name}:LR4_STRAY_TEMP_WOULD_BE_DELETED_BY_OWNER_READ")
                continue
            state_file = store_dir / state_filename
            journal_file = store_dir / journal_filename
            if not state_file.is_file() and not journal_file.is_file():
                out[name] = {
                    "records": [],
                    "data_state": "EMPTY_STORE_NO_FILES",
                    "note": "owner store files absent -> genuinely empty, not unknown",
                }
                continue
            try:
                journal = store._read_journal_unlocked()  # :1271, verified side-effect free
                state = json.loads(state_file.read_text(encoding="utf-8")) if state_file.is_file() else {}
                revision = state.get("revision", 0)
                if state and revision != len(journal):
                    blocked.append(f"{name}:LR4_REVISION_MISMATCH_STATE_{revision}_JOURNAL_{len(journal)}")
                    continue
                records = [
                    rec for rec in (state.get(collection_key) or {}).values()
                    if activity_id is None or rec.get("activity_id") == activity_id
                ]
                open_records = [r for r in records if r.get("status") in open_statuses]
                out[name] = {
                    "records_considered": len(records),
                    "open_statuses": list(open_statuses),
                    "open_refs": sorted(r.get(id_field) for r in open_records if r.get(id_field)),
                    "open_records": [
                        {id_field: r.get(id_field), "activity_id": r.get("activity_id"),
                         "status": r.get("status")}
                        for r in sorted(open_records, key=lambda r: str(r.get(id_field)))
                    ],
                    "store_revision": revision,
                    "journal_entries": len(journal),
                    "data_state": "READ",
                }
            except Exception as exc:
                blocked.append(f"{name}:LR4_READ_FAILED_{type(exc).__name__}")
        if blocked and not out:
            return PortRead(port="P9.waiting_resume", owner_function=self.OWNER_LR4_JOURNAL,
                            reason=";".join(blocked),
                            no_data_behaviour="no LR-4 collection could be read without a writing read path",
                            notes=(f"open-status predicates: {self.OWNER_LR4_STATUSES}",))
        return PortRead(
            port="P9.waiting_resume",
            available=not blocked,
            owner_function=self.OWNER_LR4_JOURNAL,
            reason=";".join(blocked) if blocked else None,
            data=out,
            no_data_behaviour=(
                "an absent LR-4 store is reported as EMPTY_STORE_NO_FILES (empty is knowledge); "
                "a mid-commit store is reported as a blocker (unknown), never as empty"
            ),
            notes=(f"open-status predicates: {self.OWNER_LR4_STATUSES}",
                   "the coordinator's own read queries require a held lease + 3 writer "
                   "capabilities (activity_waiting_agenda.py:2021-2058) and are not reachable read-only"),
        )

    # -- LR-5 --------------------------------------------------------------
    def progress(self, *, activity_id: str | None = None) -> PortRead:
        """Progress state / completion status / checkpoint ref (LR-5 projection)."""
        ensure_real_runtime_on_path()
        import activity_progress_completion_lr5 as lr5  # scripts/

        state_file = self.root / LR5_STATE_FILENAME
        journal_file = self.root / LR5_JOURNAL_FILENAME
        wal_file = self.root / LR5_WAL_FILENAME
        if wal_file.is_file():
            return PortRead(
                port="P9.progress",
                owner_function=self.OWNER_LR5_STATE,
                reason="PROGRESS_WAL_PRESENT_READ_WOULD_RECOVER",
                data={"wal_present": True},
                no_data_behaviour=(
                    "ProgressStore.load_state recovers from WAL (:2068-2071, writes) -> read "
                    "nothing; progress is UNKNOWN -> fail closed"
                ),
            )
        stray = stray_temp_files(self.root)
        if stray:
            return PortRead(
                port="P9.progress",
                owner_function=self.OWNER_LR5_STATE,
                reason="PROGRESS_STRAY_TEMP_PRESENT",
                data={"stray_temp_files": list(stray)[:5]},
                no_data_behaviour="stray atomic-write temps present -> do not drive the store's read path",
            )
        if not state_file.is_file() and not journal_file.is_file():
            return PortRead(
                port="P9.progress",
                owner_function=self.OWNER_LR5_STATE,
                reason=None,
                available=True,
                data={"store_present": False, "data_state": "EMPTY_STORE_NO_FILES",
                      "activities": {}},
                no_data_behaviour=(
                    "no LR-5 store files -> EMPTY_STORE_NO_FILES; the caller decides whether an "
                    "empty progress store is acceptable (it is reported, not silently ignored)"
                ),
            )
        try:
            state = json.loads(state_file.read_text(encoding="utf-8")) if state_file.is_file() else {}
            if state and state.get("schema_version") != lr5.SCHEMA_PROGRESS_STATE:
                return PortRead(port="P9.progress", owner_function=self.OWNER_LR5_STATE,
                                reason="PROGRESS_STATE_INVALID_SCHEMA",
                                no_data_behaviour="invalid schema -> fail closed, never rebuild")
            ps_by_activity = state.get("progress_state_by_activity") or {}
            pc_by_activity = state.get("progress_contracts_by_activity") or {}
            known = sorted(set(ps_by_activity) | set(pc_by_activity))
            activities: dict[str, Any] = {}
            for aid in known:
                if activity_id is not None and aid != activity_id:
                    continue
                ps = ps_by_activity.get(aid)
                if ps is not None:
                    activities[aid] = {
                        "activity_id": aid,
                        "progress_state": ps.get("progress_state"),
                        "checkpoint_ref": ps.get("checkpoint_ref"),
                        "completion_status": ps.get("completion_status"),
                        "provable_percentage": ps.get("provable_percentage"),
                    }
                else:
                    pc = pc_by_activity.get(aid) or {}
                    activities[aid] = {
                        "activity_id": aid,
                        "progress_state": lr5.PROGRESS_STATE_UNKNOWN,
                        "checkpoint_ref": None,
                        "completion_status": lr5.EVAL_NOT_COMPLETE,
                        "provable_percentage": None,
                        "progress_kind": pc.get("progress_kind"),
                        "note": "no progress_state record; defaults are the owner's own (:2485-2496)",
                    }
        except Exception as exc:
            return PortRead(
                port="P9.progress",
                owner_function=self.OWNER_LR5_STATE,
                reason=f"PROGRESS_STATE_UNREADABLE:{type(exc).__name__}",
                no_data_behaviour="unreadable -> progress UNKNOWN; the port never rebuilds the store",
            )
        return PortRead(
            port="P9.progress",
            available=True,
            owner_function=self.OWNER_LR5_STATE,
            data={"store_present": True, "store_revision": state.get("revision"), "activities": activities},
            no_data_behaviour=(
                "store present but no record for the activity -> the owner's own defaults are "
                "projected (PROGRESS_STATE_UNKNOWN :225, NOT_COMPLETE :355), never a success"
            ),
            notes=(f"projection mirrors {self.OWNER_LR5_PROJECTION}",
                   f"overlay shape mirrors {self.OWNER_LR5_OVERLAY}",
                   "ProgressStore.load_state (:2066) is NOT called: it mkdirs (:2067) and can "
                   "rewrite the snapshot (:2091-2092, :2107-2110)"),
        )


# ==========================================================================
# deliberately unused real "read" paths (kept here so the omission is explicit)
# ==========================================================================
UNAVAILABLE_READ_PATHS: dict[str, str] = {
    "service/world_body_gateway_api.py:500 GatewaySurface.get_action_result":
        "needs a live World/Body service instance (self.service.resolver, :504) reachable over "
        "the UNIX socket gateway.sock (:40) -> sockets are out of scope for CT0-10",
    "service/bridge/world_body_client.py:253 WorldBodyClient.get_action_result":
        "UNIX-socket RPC to the body service (:256 -> self._read) -> sockets out of scope",
    "scripts/approval_queue.py:95 list_pending":
        "reads ~/.hermes/rings/approval_queue.json (HOME = ~/.hermes, :23-24) which is a "
        "PRODUCTION home -> forbidden for CT0-10 reads, and the queue is an owner-approval "
        "list, not canonical pending-outbound state",
    "scripts/activity_interruption.py:1599 InterruptionStateStore":
        "its load_state (:1617) unlinks stray temp files (:1613); the resolver uses it only "
        "behind a stray-temp pre-flight (see capacity_resolver.py)",
    "scripts/activity_continuity.py:1553 ActivityReadService.get_current_life_view":
        "exclusive store lock + mkdir + possible commit-intent reconciliation write "
        "(:996-1002, :1206-1207 -> :1031): a read that can write",
}


__all__ = [
    "ACTION_JOURNAL_FILENAME", "ACTION_STATE_FILENAME", "ACTION_WAL_FILENAME",
    "AUTH_REGISTRY_NAME", "ActivityWaitingProgressReadPort", "AuthorizationBoundaryReadPort",
    "DEFAULT_RUNTIME_ROOT", "LR4_JOURNAL_AGENDA", "LR4_JOURNAL_CONDITIONS",
    "LR4_JOURNAL_ELIGIBILITY", "LR4_STATE_AGENDA", "LR4_STATE_CONDITIONS",
    "LR4_STATE_ELIGIBILITY", "LR5_JOURNAL_FILENAME", "LR5_STATE_FILENAME", "LR5_WAL_FILENAME",
    "PendingOutboundReadPort", "PortRead", "StoreFileFingerprint", "UNAVAILABLE_READ_PATHS",
    "ensure_real_runtime_on_path", "fingerprint", "stray_temp_files",
]
