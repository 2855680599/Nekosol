#!/usr/bin/env python3
"""Unified World Resolver (M4) + real concurrency semantics (M5).

Generalizes ``life_execute.ProductionAdapter.execute_world_move`` -- which was
hard-wired to a single ``MOVE_TO`` branch -- into one execution skeleton:

    ActionProposal -> normalize -> validate -> collect dependencies
    -> check observed versions -> check world/body constraints
    -> prepare mutation -> atomic commit -> finalize ledger -> ActionResult

Nothing may bypass it: LLM, Life, Body and Expression all have to submit a
proposal here.

Key properties
--------------
* **MOVE stays compatible.**  The MOVE action *delegates* to the existing
  ``world_living_skeleton.plan_move_to`` / ``apply_move_to``.  Those functions
  are untouched, so the old result shape, the old failure contract and the old
  ``execution_id`` behaviour are preserved.  A thin
  :func:`execute_move_compat` wrapper keeps the old caller working.
* **Real stale protection (M5).**  A proposal carries the *dependency values its
  caller actually observed*.  The resolver re-reads them under the write lock and
  rejects with ``STALE_STATE`` if anything moved.  This is genuine optimistic
  concurrency -- unlike the legacy ``expected_revision`` argument, which writers
  filled in with a revision they had just read themselves.
* **Dependency-scoped, not one global lock.**  Dependencies are named and
  resolved individually (``world.revision``, ``body.revision``, ``body.pose``,
  ``body.slot.<slot>``), so the check is as narrow as the current schema allows.
* **Body is a constraint, not an authority.**  ``world_body_substrate`` supplies
  placement / pose / slot state.  The resolver alone commits.
* **UNKNOWN is preserved.**  An in-flight execution reconciles to ``UNKNOWN``
  with ``reconciliation_required``; it is never rewritten to FAILURE and never
  auto-retried.
"""

from __future__ import annotations

import fcntl
import json
import os
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import world_execution_ledger as ledger

RESULT_KIND = "world_action_result"
SCHEMA_VERSION = "world.action.resolver.v1"

AUTHORITY_VERIFIED_MAIN_SELF = "verified_main_self_tool"

# --------------------------------------------------------------------------
# Frozen error vocabulary + provenance (ticket section 10)
# --------------------------------------------------------------------------

LEGACY_CANONICAL = "LEGACY_CANONICAL"
NEW_M6_CANDIDATE = "NEW_M6_CANDIDATE"
NEW_RESOLVER_REQUIRED = "NEW_RESOLVER_REQUIRED"

CODE_OK = "OK"
CODE_NOOP = "NOOP"
CODE_FEATURE_DISABLED = "FEATURE_DISABLED"
CODE_INVALID_PROPOSAL = "INVALID_PROPOSAL"
CODE_UNKNOWN_ACTION = "UNKNOWN_ACTION"
CODE_UNKNOWN_TARGET = "UNKNOWN_TARGET"
CODE_NOT_REACHABLE = "NOT_REACHABLE"
CODE_OCCUPIED = "OCCUPIED"
CODE_STALE_STATE = "STALE_STATE"
CODE_NOT_TAKEABLE = "NOT_TAKEABLE"
CODE_NOT_ALLOWED = "NOT_ALLOWED"
CODE_ALREADY_APPLIED = "ALREADY_APPLIED"
CODE_POSTCONDITION_FAILED = "POSTCONDITION_FAILED"
CODE_LEDGER_CORRUPT = "LEDGER_CORRUPT"
CODE_RECONCILIATION_REQUIRED = "RECONCILIATION_REQUIRED"
CODE_WORLD_UNAVAILABLE = "WORLD_UNAVAILABLE"
CODE_AUTHORITY_REQUIRED = "AUTHORITY_REQUIRED"

#: code -> (provenance, literal spellings found in production, note)
ERROR_PROVENANCE: dict[str, tuple[str, list[str], str]] = {
    CODE_NOT_REACHABLE: (
        LEGACY_CANONICAL,
        ["LOCATION_NOT_REACHABLE", "ROUTE_NOT_REACHABLE"],
        "the bare code NOT_REACHABLE is NEW; only the two prefixed legacy spellings "
        "existed and both map onto this",
    ),
    CODE_OCCUPIED: (
        NEW_M6_CANDIDATE,
        [],
        "the bare code OCCUPIED is NEW; production had zero hits before the M6 substrate",
    ),
    CODE_STALE_STATE: (
        NEW_RESOLVER_REQUIRED,
        ["stale_affordance", "stale_activity_target", "stale_choice_context", "STALE_PRESENCE"],
        "no bare STALE_STATE existed; several module-local spellings were unified here, "
        "legacy text preserved in legacy_reason_code",
    ),
    CODE_UNKNOWN_TARGET: (
        LEGACY_CANONICAL,
        "UNKNOWN_LOCATION (legacy) maps here; the bare code is new",
        "unknown-target rejection existed for locations",
    ),
    CODE_NOT_ALLOWED: (
        LEGACY_CANONICAL,
        "LOCATION_CLOSED / TRAVEL_NOT_ALLOWED existed; note verified_main_self_required "
        "(lowercase, life_execute) case-diverges from the UPPER convention of 8 modules",
        "permission/availability refusals existed",
    ),
    CODE_ALREADY_APPLIED: (
        LEGACY_CANONICAL,
        ["already_applied"],
        "idempotent replay result existed",
    ),
    CODE_POSTCONDITION_FAILED: (
        LEGACY_CANONICAL,
        "postcondition verification existed under three spellings",
        "postcondition verification existed",
    ),
    CODE_RECONCILIATION_REQUIRED: (
        LEGACY_CANONICAL,
        ["reconciliation_required", "uncertain"],
        "UNKNOWN/reconciliation existed",
    ),
    CODE_FEATURE_DISABLED: (
        LEGACY_CANONICAL,
        ["FEATURE_DISABLED"],
        "feature gating existed",
    ),
    CODE_WORLD_UNAVAILABLE: (
        LEGACY_CANONICAL,
        ["WORLD_NOT_INITIALIZED"],
        "missing world state existed",
    ),
    CODE_NOT_TAKEABLE: (
        NEW_RESOLVER_REQUIRED,
        "the bare code NOT_TAKEABLE is NEW; no object-level takeability concept existed",
        "no prior object-level takeability concept",
    ),
}
#: Unified mapping from the two legacy vocabularies onto canonical codes.
LEGACY_CODE_MAP: dict[str, str] = {
    "LOCATION_NOT_REACHABLE": CODE_NOT_REACHABLE,
    "ROUTE_NOT_REACHABLE": CODE_NOT_REACHABLE,
    "UNKNOWN_LOCATION": CODE_UNKNOWN_TARGET,
    "INVALID_LOCATION_ID": CODE_INVALID_PROPOSAL,
    "LOCATION_CLOSED": CODE_NOT_ALLOWED,
    "TRAVEL_NOT_ALLOWED": CODE_NOT_ALLOWED,
    "DAILY_RHYTHM_UNAVAILABLE": CODE_WORLD_UNAVAILABLE,
    "WORLD_NOT_INITIALIZED": CODE_WORLD_UNAVAILABLE,
    "FEATURE_DISABLED": CODE_FEATURE_DISABLED,
    "ALREADY_AT_LOCATION": CODE_NOOP,
    "verified_main_self_required": CODE_AUTHORITY_REQUIRED,
    "confirmation_required": CODE_AUTHORITY_REQUIRED,
    "world_postcondition_failed": CODE_POSTCONDITION_FAILED,
    "WORLD_POSTCONDITION_FAILED": CODE_POSTCONDITION_FAILED,
    "postcondition_failed": CODE_POSTCONDITION_FAILED,
    "reconciliation_required": CODE_RECONCILIATION_REQUIRED,
    "journal_corrupt": CODE_LEDGER_CORRUPT,
    "already_applied": CODE_ALREADY_APPLIED,
    "invalid_location_id": CODE_INVALID_PROPOSAL,
    "invalid_execution_id": CODE_INVALID_PROPOSAL,
}

ACTIONS = ("MOVE", "PICK_UP", "PLACE", "POSE")

TERMINAL_SUCCESS = frozenset({"applied", "noop"})
TERMINAL_FAILURE = frozenset({"rejected", "failed", "disabled"})
IN_FLIGHT = frozenset({"prepared", "uncertain"})

CRASH_AFTER_PREPARED = "after_prepared"
CRASH_AFTER_MUTATION = "after_mutation"
CRASH_POINTS = (CRASH_AFTER_PREPARED, CRASH_AFTER_MUTATION)

MAX_ID_LENGTH = 128


class ResolverError(ValueError):
    """The proposal violated the resolver contract."""


class ResolverCrash(RuntimeError):
    """Test-only crash injection at a named step."""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _text(value: Any, field: str, *, maximum: int = MAX_ID_LENGTH) -> str:
    if not isinstance(value, str):
        raise ResolverError(f"{field} must be a string")
    text = value.strip()
    if not text or len(text) > maximum:
        raise ResolverError(f"{field} must be non-empty and bounded")
    return text


# --------------------------------------------------------------------------
# result construction
# --------------------------------------------------------------------------


def _result(
    proposal: Mapping[str, Any],
    *,
    status: str,
    code: str,
    reason_code: str,
    before: Any = None,
    after: Any = None,
    dependencies: Optional[Mapping[str, Any]] = None,
    stale_dependencies: Optional[list[str]] = None,
    ledger_status: Optional[str] = None,
    reconciliation_required: bool = False,
    body_signal_hint: Optional[list[str]] = None,
) -> dict[str, Any]:
    result = (
        ledger.RESULT_SUCCESS
        if status in TERMINAL_SUCCESS
        else ledger.RESULT_FAILURE
        if status in TERMINAL_FAILURE
        else ledger.RESULT_UNKNOWN
    )
    payload: dict[str, Any] = {
        "kind": RESULT_KIND,
        "schema_version": SCHEMA_VERSION,
        "execution_id": proposal.get("execution_id"),
        "action": proposal.get("action"),
        "actor_id": proposal.get("actor_id"),
        "body_id": proposal.get("body_id"),
        "status": status,
        "result": result,
        "reason_code": code,
        "legacy_reason_code": reason_code,
        "before": before,
        "after": after,
        "dependencies": dict(dependencies or {}),
        "ledger_status": ledger_status,
        "executed_at": _now_iso(),
    }
    if stale_dependencies:
        payload["stale_dependencies"] = sorted(stale_dependencies)
    if reconciliation_required:
        payload["reconciliation_required"] = True
    if body_signal_hint:
        payload["body_consequence_refs"] = sorted(body_signal_hint)
    return payload


def _reject(
    proposal: Mapping[str, Any],
    status: str,
    code: str,
    *,
    reason_code: Optional[str] = None,
    before: Any = None,
    after: Any = None,
    **extra: Any,
) -> dict[str, Any]:
    return _result(
        proposal,
        status=status,
        code=code,
        reason_code=reason_code or code,
        before=before,
        after=after,
        **extra,
    )


def map_legacy_code(code: Optional[str]) -> Optional[str]:
    """Canonical code for a legacy spelling, or ``None`` if unmapped."""

    if not code:
        return None
    return LEGACY_CODE_MAP.get(code)


# --------------------------------------------------------------------------
# proposal normalization / validation
# --------------------------------------------------------------------------


_PROPOSAL_KEYS = frozenset(
    {
        "action",
        "execution_id",
        "actor_id",
        "body_id",
        "authority",
        "confirm_apply",
        "observed_dependencies",
        "params",
    }
)


def normalize_proposal(proposal: Any) -> dict[str, Any]:
    """Validate and canonicalize an ActionProposal."""

    if not isinstance(proposal, Mapping):
        raise ResolverError("proposal must be an object")

    unknown = set(proposal) - _PROPOSAL_KEYS
    if unknown:
        raise ResolverError(f"proposal has unknown fields: {sorted(unknown)}")
    missing = {"action", "execution_id"} - set(proposal)
    if missing:
        raise ResolverError(f"proposal is missing fields: {sorted(missing)}")

    action = _text(proposal["action"], "action", maximum=32)
    if action not in ACTIONS:
        raise ResolverError(f"action must be one of {list(ACTIONS)}")

    params = proposal.get("params") or {}
    if not isinstance(params, Mapping):
        raise ResolverError("params must be an object")

    observed = proposal.get("observed_dependencies") or {}
    if not isinstance(observed, Mapping):
        raise ResolverError("observed_dependencies must be an object")
    for key, value in observed.items():
        _text(key, "observed_dependencies key", maximum=64)
        if not isinstance(value, (str, int, bool)) and value is not None:
            raise ResolverError("observed dependency values must be scalar")

    return {
        "action": action,
        "execution_id": _text(proposal["execution_id"], "execution_id", maximum=256),
        "actor_id": _text(proposal.get("actor_id") or "chiyo", "actor_id"),
        "body_id": _text(proposal.get("body_id") or "chiyo_body", "body_id"),
        "authority": proposal.get("authority"),
        "confirm_apply": bool(proposal.get("confirm_apply")),
        "observed_dependencies": {k: observed[k] for k in sorted(observed)},
        "params": dict(params),
    }


def _require_params(params: Mapping[str, Any], expected: set[str]) -> None:
    unknown = set(params) - expected
    if unknown:
        raise ResolverError(f"params has unknown fields: {sorted(unknown)}")


# --------------------------------------------------------------------------
# the resolver
# --------------------------------------------------------------------------



# --------------------------------------------------------------------------
# M15: object capability & destination rules (Resolver-owned)
# --------------------------------------------------------------------------


def _m15_slot_token(capability, slot: str) -> str:
    """Translate a substrate slot id into the M15F vocabulary.

    M15F names the hands ``left`` / ``right``; the substrate (which owns the
    ids) names them ``left_hand`` / ``right_hand``.  Reconciling the two is a
    boundary translation, not a second truth: an unknown token is passed through
    untouched so M15F still refuses it.
    """

    tokens = tuple(getattr(capability, "HAND_SLOTS", ()) or ())
    if slot in tokens:
        return slot
    short = slot[:-5] if slot.endswith("_hand") else slot
    return short if short in tokens else slot


def _m15_target_kind_token(capability, kind: str) -> str:
    """Substrate placement kind -> M15F target kind (``hand`` -> ``slot``).

    ``room`` has no M15F spelling: it is returned unchanged and the caller
    treats it as a kind M15F does not model.
    """

    kinds = tuple(getattr(capability, "MODELLED_TARGET_KINDS",
                          ("surface", "container", "slot")))
    if kind == "hand" and "slot" in kinds:
        return "slot"
    return kind


def _m15_capability_module():
    """Load the M15F capability vocabulary (it ships with the bridge package).

    The Resolver runs inside the World/Body service, whose sys.path does not
    include ``service/bridge``; a plain import would silently disable the
    capability gate.  Load it by file path instead, once.
    """

    global _M15_CAPABILITY_MODULE
    if _M15_CAPABILITY_MODULE is not None:
        return _M15_CAPABILITY_MODULE
    try:
        import world_object_capability as module  # already importable
        _M15_CAPABILITY_MODULE = module
        return module
    except ImportError:
        pass
    import importlib.util
    import pathlib as _pathlib
    candidate = (_pathlib.Path(__file__).resolve().parent.parent
                 / "service" / "bridge" / "world_object_capability.py")
    if not candidate.is_file():  # pragma: no cover - bridge not deployed
        return None
    spec = importlib.util.spec_from_file_location("world_object_capability", str(candidate))
    if spec is None or spec.loader is None:  # pragma: no cover
        return None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    _M15_CAPABILITY_MODULE = module
    return module


_M15_CAPABILITY_MODULE = None


def _m15_capability_block(hermes_home, object_id: str) -> dict[str, Any]:
    """An object's capability, joined by the binding layer (M15).

    The Resolver never reads the economy itself -- ``world_body_binding`` owns
    that join.  An unknown object yields ``{}``; the M15F vocabulary then reads
    UNKNOWN and PICK_UP is denied.  Nothing is assumed.
    """

    try:
        import world_body_binding as binding
    except ImportError:  # pragma: no cover
        return {}
    try:
        return binding.object_capability(hermes_home, object_id)
    except Exception:  # pragma: no cover - unreadable join denies
        return {}


def _m15_destination_verdict(
    target: Mapping[str, Any], *, world_place: Optional[str], hermes_home
) -> tuple[Optional[str], Optional[str]]:
    """``(code, detail)`` -- ``(None, None)`` when the destination is legal."""

    kind = target.get("kind")
    location_id = target.get("location_id")

    try:
        import world_living_skeleton as skeleton
    except ImportError:  # pragma: no cover - no topology means no legal move
        return "UNKNOWN_DESTINATION", "world topology is unavailable"

    catalog = {}
    for entry in skeleton.location_catalog():
        if isinstance(entry, Mapping) and entry.get("location_id"):
            catalog[str(entry["location_id"])] = dict(entry)

    if location_id not in catalog:
        return "UNKNOWN_DESTINATION", f"location {location_id} is not part of the world"
    if world_place and location_id != world_place:
        here = catalog.get(str(world_place)) or {}
        reachable = [str(r) for r in (here.get("reachable") or [])]
        if location_id not in reachable:
            return "NOT_REACHABLE", f"{location_id} is not reachable from {world_place}"

    if kind == "room":
        return None, None

    if kind == "surface":
        import world_body_substrate as substrate

        surface_id = str(target.get("surface_id"))
        if not substrate.surface_declared(surface_id):
            return ("UNKNOWN_DESTINATION",
                    f"surface {surface_id} is not declared by the world")
        owner_catalog = substrate.surface_owner_catalog(surface_id)
        if owner_catalog:
            try:
                import world_body_binding as binding
                owned = {str(i.get("catalog_item_id"))
                         for i in (binding.load_economy(hermes_home) or {}).get("items") or []
                         if isinstance(i, Mapping) and str(i.get("disposition")) == "OWNED"}
            except Exception:  # pragma: no cover
                owned = set()
            if owner_catalog not in owned:
                return ("UNKNOWN_DESTINATION",
                        f"surface {surface_id} has no present owner object")
        return None, None

    if kind == "container":
        return "DESTINATION_NOT_ALLOWED", "container placement is not enabled in V1"
    if kind == "in_transit":
        return "DESTINATION_NOT_ALLOWED", "in_transit is not a PLACE destination"
    return "UNKNOWN_DESTINATION", f"unsupported destination kind {kind}"


#: M15: destination failure -> Resolver reason code
_M15_DESTINATION_CODES = {
    "UNKNOWN_DESTINATION": CODE_UNKNOWN_TARGET,
    "NOT_REACHABLE": CODE_NOT_REACHABLE,
    "DESTINATION_NOT_ALLOWED": CODE_NOT_ALLOWED,
}

class WorldResolver:
    """Single commit authority for World actions."""

    def __init__(
        self,
        hermes_home: Optional[Path | str] = None,
        *,
        ledger_path: Optional[Path | str] = None,
        environment: Optional[Mapping[str, str]] = None,
    ):
        home = Path(
            hermes_home
            if hermes_home is not None
            else os.environ.get("HERMES_HOME", Path.home() / ".hermes")
        )
        self.hermes_home = home
        self.environment: Mapping[str, str] = (
            environment if environment is not None else os.environ
        )
        self.ledger = ledger.ExecutionLedger(
            ledger_path if ledger_path is not None else ledger.ledger_path(home)
        )

    # -- lazy module access (import has no runtime effect) ---------------

    def _world_module(self):
        import world_living_skeleton

        return world_living_skeleton

    def _world_state(self) -> Optional[dict[str, Any]]:
        import world_foundation

        return world_foundation.WorldStateStore(
            self.hermes_home / "data" / "world" / "world_state.json"
        ).load()

    def _binding_module(self):
        import world_body_binding

        return world_body_binding

    def _cross_authority_refs(self, object_id: str) -> list[str]:
        """Economy bookkeeping the caller must reconcile after a physical move.

        The resolver deliberately does NOT write the economy store: a physical
        placement is not authority to rewrite ownership bookkeeping.
        """

        try:
            binding = self._binding_module()
        except Exception:
            return []
        return binding.cross_authority_refs(
            self._substrate() or {}, binding.load_economy(self.hermes_home), object_id
        )

    def _substrate_module(self):
        import world_body_substrate

        return world_body_substrate

    def _substrate(self) -> Optional[dict[str, Any]]:
        module = self._substrate_module()
        return module.SubstrateStore(module.substrate_path(self.hermes_home)).load()

    # -- locking ----------------------------------------------------------

    @contextmanager
    def _commit_lock(self) -> Iterator[None]:
        if fcntl is None:
            raise ResolverError("resolver lock unavailable")
        lock_path = self.hermes_home / "data" / ".world_resolver.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a+") as handle:
            deadline = time.monotonic() + 10.0
            while True:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError as exc:
                    if time.monotonic() >= deadline:
                        raise ResolverError("resolver lock timeout") from exc
                    time.sleep(0.02)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    # -- dependencies (M5) -----------------------------------------------

    def _dependencies(self) -> dict[str, Any]:
        """The live dependency values a caller would have observed."""

        deps: dict[str, Any] = {}
        state = self._world_state()
        if state is None:
            deps["world.revision"] = None
            deps["world.location"] = None
        else:
            deps["world.revision"] = state["revision"]
            deps["world.location"] = state["location"]["place_id"]

        substrate = self._substrate()
        if substrate is None:
            deps["body.revision"] = None
        else:
            module = self._substrate_module()
            deps["body.revision"] = substrate["revision"]
            deps["body.pose"] = substrate["pose"]
            hands = module.hand_view(substrate)
            for slot in module.HAND_SLOTS:
                deps[f"body.slot.{slot}"] = hands[slot]
        return deps

    @staticmethod
    def _stale_keys(
        observed: Mapping[str, Any], live: Mapping[str, Any]
    ) -> list[str]:
        stale: list[str] = []
        for key, value in observed.items():
            if key not in live:
                stale.append(key)
            elif live[key] != value:
                stale.append(key)
        return stale

    # -- execution --------------------------------------------------------

    def resolve(
        self,
        proposal: Mapping[str, Any],
        *,
        crash_after: Optional[str] = None,
    ) -> dict[str, Any]:
        """Resolve one proposal end to end.

        ``crash_after`` is test-only crash injection and must never be used in
        production; it exists so the crash windows of ticket section 14 can be
        tested deterministically.
        """

        if crash_after is not None and crash_after not in CRASH_POINTS:
            raise ResolverError(f"unknown crash point {crash_after!r}")

        try:
            normalized = normalize_proposal(proposal)
        except ResolverError as error:
            return _result(
                proposal if isinstance(proposal, Mapping) else {},
                status="rejected",
                code=CODE_INVALID_PROPOSAL,
                reason_code=str(error),
            )

        # authorization -- unchanged legacy contract
        if normalized["authority"] != AUTHORITY_VERIFIED_MAIN_SELF:
            return _reject(
                normalized, "rejected", CODE_AUTHORITY_REQUIRED,
                reason_code="verified_main_self_required",
            )
        if not normalized["confirm_apply"]:
            return _reject(
                normalized, "rejected", CODE_AUTHORITY_REQUIRED,
                reason_code="confirmation_required",
            )

        handler = {
            "MOVE": self._resolve_move,
            "PICK_UP": self._resolve_pick_up,
            "PLACE": self._resolve_place,
            "POSE": self._resolve_pose,
        }[normalized["action"]]

        try:
            with self._commit_lock():
                return handler(normalized, crash_after=crash_after)
        except (ledger.LedgerError, ledger.LedgerCorruptError):
            return _reject(normalized, "failed", CODE_LEDGER_CORRUPT)
        except ResolverCrash:
            raise
        except ResolverError as error:
            return _reject(
                normalized, "rejected", CODE_INVALID_PROPOSAL, reason_code=str(error)
            )
        except Exception:
            return _reject(normalized, "failed", CODE_WORLD_UNAVAILABLE)

    # -- shared idempotency gate -----------------------------------------

    def _replay_frame(
        self, normalized: Mapping[str, Any]
    ) -> tuple[Optional[dict[str, Any]], Optional[dict[str, Any]]]:
        """Return ``(already_finished_result, frame)`` for an execution_id."""

        frame = self.ledger.frame(normalized["execution_id"])
        if not frame["known"]:
            return None, frame

        state = frame["state"]
        if state == "committed":
            return (
                _result(
                    normalized,
                    status="already_applied",
                    code=CODE_ALREADY_APPLIED,
                    reason_code="already_applied",
                    before=frame["records"][-1]["raw"].get("before"),
                    after=frame["records"][-1]["raw"].get("after"),
                    ledger_status="committed",
                ),
                frame,
            )
        if state in IN_FLIGHT:
            # Never guess.  Flip a prepared run to uncertain and report UNKNOWN.
            if state == "prepared":
                self.ledger.append_unlocked(
                    self._entry(
                        normalized,
                        status="uncertain",
                        reason_code="reconciliation_required",
                        reconciliation_required=True,
                    )
                )
            return (
                _result(
                    normalized,
                    status="uncertain",
                    code=CODE_RECONCILIATION_REQUIRED,
                    reason_code="reconciliation_required",
                    before=frame["records"][0]["raw"].get("before"),
                    ledger_status="uncertain",
                    reconciliation_required=True,
                ),
                frame,
            )
        return (
            _reject(
                normalized,
                "failed" if state == "failed" else "rejected",
                CODE_NOT_ALLOWED,
                reason_code=frame["reason_code"] or "prior_attempt_failed",
                ledger_status=state,
            ),
            frame,
        )

    def _entry(
        self,
        normalized: Mapping[str, Any],
        *,
        status: str,
        reason_code: str,
        before: Any = None,
        after: Any = None,
        dependencies: Optional[Mapping[str, Any]] = None,
        world_mutation_refs: Optional[list[str]] = None,
        body_consequence_refs: Optional[list[str]] = None,
        cross_authority_refs: Optional[list[str]] = None,
        reconciliation_required: bool = False,
    ) -> dict[str, Any]:
        return {
            "execution_id": normalized["execution_id"],
            "action_type": normalized["action"],
            "actor_id": normalized["actor_id"],
            "status": status,
            "reason_code": reason_code,
            "timestamp": _now_iso(),
            "proposal_ref": normalized["action"],
            "observed_dependencies": dict(normalized["observed_dependencies"]),
            "expected_versions": dict(normalized["observed_dependencies"]),
            "world_mutation_refs": list(world_mutation_refs or []),
            "body_consequence_refs": list(body_consequence_refs or []),
            "cross_authority_refs": list(cross_authority_refs or []),
            "before_state_ref": self.ledger.state_ref(before),
            "after_state_ref": self.ledger.state_ref(after),
            "before": before,
            "after": after,
            "reconciliation_required": reconciliation_required,
            "dependencies": dict(dependencies or {}),
        }

    # -- MOVE (delegates to the untouched World core) ---------------------

    def _resolve_move(
        self, normalized: Mapping[str, Any], *, crash_after: Optional[str]
    ) -> dict[str, Any]:
        world = self._world_module()
        _require_params(normalized["params"], {"destination_location_id"})

        finished, _frame = self._replay_frame(normalized)
        if finished is not None:
            return finished

        target = normalized["params"].get("destination_location_id")
        try:
            target_text = _text(target, "destination_location_id", maximum=96)
        except ResolverError as error:
            return _reject(
                normalized, "rejected", CODE_INVALID_PROPOSAL, reason_code=str(error)
            )

        live = self._dependencies()
        stale = self._stale_keys(normalized["observed_dependencies"], live)
        if stale:
            return _reject(
                normalized, "rejected", CODE_STALE_STATE,
                reason_code="stale_dependency",
                before=None, dependencies=live, stale_dependencies=stale,
            )

        plan = world.plan_move_to(
            target_text, hermes_home=self.hermes_home, environment=self.environment
        )
        code = map_legacy_code(plan.get("reason_code"))
        if code == CODE_FEATURE_DISABLED:
            return _reject(
                normalized, "disabled", CODE_FEATURE_DISABLED,
                reason_code=plan.get("reason_code"),
            )
        if plan.get("status") == "noop":
            return _reject(
                normalized, "noop", CODE_NOOP,
                reason_code=plan.get("reason_code"), before=plan.get("before"),
                dependencies=live,
            )
        if plan.get("status") != "ready":
            return _reject(
                normalized, "rejected", code or CODE_NOT_ALLOWED,
                reason_code=plan.get("reason_code"), before=plan.get("before"),
                dependencies=live,
            )

        before = plan.get("before")
        self.ledger.append_unlocked(
            self._entry(
                normalized, status="prepared", reason_code="reachable",
                before=before, dependencies=live,
            )
        )
        if crash_after == CRASH_AFTER_PREPARED:
            raise ResolverCrash("crash after prepared (test)")

        applied = world.apply_move_to(
            target_text,
            hermes_home=self.hermes_home,
            environment=self.environment,
            execution_id=normalized["execution_id"],
        )
        after = applied.get("after") or world.read_move_state(
            hermes_home=self.hermes_home, environment=self.environment
        )
        if crash_after == CRASH_AFTER_MUTATION:
            raise ResolverCrash("crash after mutation (test)")

        if applied.get("status") != "applied":
            uncertain = applied.get("status") == "uncertain"
            legacy = applied.get("reason_code")
            self.ledger.append_unlocked(
                self._entry(
                    normalized, status="uncertain" if uncertain else "failed",
                    reason_code=str(legacy or "move_failed"), before=before, after=after,
                    dependencies=live, reconciliation_required=uncertain,
                )
            )
            return _reject(
                normalized, "uncertain" if uncertain else "failed",
                map_legacy_code(legacy) or CODE_WORLD_UNAVAILABLE,
                reason_code=legacy, before=before, after=after, dependencies=live,
                ledger_status="uncertain" if uncertain else "failed",
                reconciliation_required=uncertain,
            )

        verified = (
            isinstance(after, Mapping)
            and after.get("world_location_id") == target_text
        )
        if not verified:
            self.ledger.append_unlocked(
                self._entry(
                    normalized, status="uncertain", reason_code="world_postcondition_failed",
                    before=before, after=after, dependencies=live,
                    reconciliation_required=True,
                )
            )
            return _reject(
                normalized, "uncertain", CODE_POSTCONDITION_FAILED,
                reason_code="world_postcondition_failed", before=before, after=after,
                dependencies=live, ledger_status="uncertain", reconciliation_required=True,
            )

        self.ledger.append_unlocked(
            self._entry(
                normalized, status="committed", reason_code="move_committed",
                before=before, after=after, dependencies=live,
                world_mutation_refs=[f"world.location={target_text}"],
            )
        )
        return _result(
            normalized, status="applied", code=CODE_OK, reason_code="move_committed",
            before=before, after=after, dependencies=live, ledger_status="committed",
        )

    # -- PICK_UP / PLACE / POSE (M6 substrate integration) ---------------

    def _substrate_gate(
        self, normalized: Mapping[str, Any]
    ) -> tuple[Optional[Any], Optional[dict[str, Any]], Optional[dict[str, Any]]]:
        module = self._substrate_module()
        if not module.feature_enabled(self.environment):
            return module, None, _reject(
                normalized, "disabled", CODE_FEATURE_DISABLED,
                reason_code=module.FEATURE_ENV + " is off",
            )
        state = self._substrate()
        if state is None:
            return module, None, _reject(
                normalized, "rejected", CODE_WORLD_UNAVAILABLE,
                reason_code="body_substrate_not_initialized",
            )
        # M6 completion: the body must actually be bound to this World before it
        # is allowed to act on it.  A world_id mismatch fails closed.
        binding = module.verify_binding(state, self._world_state())
        if not binding["bound"]:
            return module, None, _reject(
                normalized, "rejected", CODE_WORLD_UNAVAILABLE,
                reason_code="body_world_binding_invalid: "
                + "; ".join(binding.get("reasons") or ["unknown"]),
            )
        return module, state, None

    def _resolve_pick_up(
        self, normalized: Mapping[str, Any], *, crash_after: Optional[str]
    ) -> dict[str, Any]:
        module, state, blocked = self._substrate_gate(normalized)
        if blocked is not None:
            return blocked

        _require_params(normalized["params"], {"object_id", "slot_id"})
        finished, _frame = self._replay_frame(normalized)
        if finished is not None:
            return finished

        try:
            object_id = _text(normalized["params"].get("object_id"), "object_id")
            slot = _text(normalized["params"].get("slot_id"), "slot_id", maximum=32)
            if slot not in module.HAND_SLOTS:
                raise ResolverError(f"slot_id must be one of {list(module.HAND_SLOTS)}")
        except ResolverError as error:
            return _reject(
                normalized, "rejected", CODE_INVALID_PROPOSAL, reason_code=str(error)
            )

        live = self._dependencies()
        stale = self._stale_keys(normalized["observed_dependencies"], live)
        if stale:
            return _reject(
                normalized, "rejected", CODE_STALE_STATE, reason_code="stale_dependency",
                dependencies=live, stale_dependencies=stale,
            )

        before = module.read_model(state)
        placement = module.placement_of(state, object_id)
        if placement is None:
            return _reject(
                normalized, "rejected", CODE_UNKNOWN_TARGET,
                reason_code=f"UNKNOWN: {object_id} has no placement",
                before=before, dependencies=live,
            )
        if placement["kind"] == module.PLACEMENT_HAND:
            return _reject(
                normalized, "rejected", CODE_NOT_TAKEABLE,
                reason_code=f"{object_id} is already held",
                before=before, dependencies=live,
            )

        # M15: capability pre-flight (single vocabulary: world_object_capability)
        capability = _m15_capability_module()
        cap = _m15_capability_block(self.hermes_home, object_id)
        if capability is not None:
            verdict = capability.pick_up_verdict(
                obj={"capability": cap}, hand=_m15_slot_token(capability, slot)
            )
            if verdict.get("verdict") != capability.ALLOWED:
                return _reject(
                    normalized, "rejected", CODE_NOT_TAKEABLE,
                    reason_code="NOT_PORTABLE: %s" % verdict.get("reason"),
                    before=before, dependencies=live,
                )

        world = self._world_state()
        here = world["location"]["place_id"] if world else None
        if placement.get("location_id") != here:
            return _reject(
                normalized, "rejected", CODE_NOT_REACHABLE,
                reason_code="LOCATION_NOT_REACHABLE",
                before=before, dependencies=live,
            )

        occupant = module.slot_occupant(state, slot)
        if occupant is not None and occupant != object_id:
            return _reject(
                normalized, "rejected", CODE_OCCUPIED,
                reason_code=f"OCCUPIED: {slot} already holds {occupant}",
                before=before, dependencies=live,
            )
        if not module.reachability_inputs(state)["hands_usable"]:
            return _reject(
                normalized, "rejected", CODE_NOT_REACHABLE,
                reason_code=f"pose {state['pose']} cannot manipulate",
                before=before, dependencies=live,
            )

        self.ledger.append_unlocked(
            self._entry(
                normalized, status="prepared", reason_code="pick_up_prepared",
                before=before, dependencies=live,
            )
        )
        if crash_after == CRASH_AFTER_PREPARED:
            raise ResolverCrash("crash after prepared (test)")

        try:
            # Hand occupancy is derived from placement alone.  We deliberately do
            # NOT also take a resource reservation here: PICK_UP is a single
            # synchronous commit, so a reservation would be a second occupancy
            # truth and could go stale against the placement.
            updated = module.place_object(
                state, object_id, {"kind": "hand", "slot_id": slot},
                expected_revision=state["revision"],
            )
            module.SubstrateStore(module.substrate_path(self.hermes_home)).save(
                updated, expected_revision=state["revision"]
            )
        except module.BodySubstrateError as error:
            self.ledger.append_unlocked(
                self._entry(
                    normalized, status="failed", reason_code="substrate_rejected",
                    before=before, dependencies=live,
                )
            )
            return _reject(
                normalized, "failed", CODE_NOT_ALLOWED,
                reason_code=str(error), before=before, dependencies=live,
                ledger_status="failed",
            )
        if crash_after == CRASH_AFTER_MUTATION:
            raise ResolverCrash("crash after mutation (test)")

        substrate = self._substrate()
        after = module.read_model(substrate)
        hints = ["hands_occupied"] if not module.free_slots(substrate) else []
        self.ledger.append_unlocked(
            self._entry(
                normalized, status="committed", reason_code="pick_up_committed",
                before=before, after=after, dependencies=live,
                world_mutation_refs=[f"placement.{object_id}=hand:{slot}"],
                cross_authority_refs=self._cross_authority_refs(object_id),
                body_consequence_refs=hints,
            )
        )
        return _result(
            normalized, status="applied", code=CODE_OK, reason_code="pick_up_committed",
            before=before, after=after, dependencies=live, ledger_status="committed",
            body_signal_hint=hints,
        )

    def _resolve_place(
        self, normalized: Mapping[str, Any], *, crash_after: Optional[str]
    ) -> dict[str, Any]:
        module, state, blocked = self._substrate_gate(normalized)
        if blocked is not None:
            return blocked

        _require_params(normalized["params"], {"object_id", "target"})
        finished, _frame = self._replay_frame(normalized)
        if finished is not None:
            return finished

        try:
            object_id = _text(normalized["params"].get("object_id"), "object_id")
            target = module.validate_placement_target(normalized["params"].get("target"))
        except (ResolverError, module.BodySubstrateError) as error:
            return _reject(
                normalized, "rejected", CODE_INVALID_PROPOSAL, reason_code=str(error)
            )

        live = self._dependencies()
        stale = self._stale_keys(normalized["observed_dependencies"], live)
        if stale:
            return _reject(
                normalized, "rejected", CODE_STALE_STATE, reason_code="stale_dependency",
                dependencies=live, stale_dependencies=stale,
            )

        before = module.read_model(state)
        placement = module.placement_of(state, object_id)
        if placement is None:
            return _reject(
                normalized, "rejected", CODE_UNKNOWN_TARGET,
                reason_code=f"UNKNOWN: {object_id} has no placement",
                before=before, dependencies=live,
            )
        if placement["kind"] != module.PLACEMENT_HAND:
            return _reject(
                normalized, "rejected", CODE_NOT_ALLOWED,
                reason_code=f"{object_id} is not held",
                before=before, dependencies=live,
            )

        # M15: capability pre-flight (the object must be placeable at all)
        capability = _m15_capability_module()
        cap = _m15_capability_block(self.hermes_home, object_id)
        modelled_kind = _m15_target_kind_token(capability, str(target.get("kind"))) \
            if capability is not None else None
        if capability is not None and cap and modelled_kind in getattr(
                capability, "MODELLED_TARGET_KINDS", ("surface", "container", "slot")):
            # a declared capability that says "not placeable" is honoured; an
            # undeclared one is not invented (the object is demonstrably held).
            # Kinds M15F does not model (room) are judged by the destination
            # rule below, which is the Resolver's own authority.
            verdict = capability.place_verdict(
                obj={"capability": cap}, held=True,
                target_kind=modelled_kind,
                target_permits=True, slot_available=True,
            )
            if verdict.get("verdict") != capability.ALLOWED:
                return _reject(
                    normalized, "rejected", CODE_NOT_ALLOWED,
                    reason_code="NOT_PLACEABLE: %s" % verdict.get("reason"),
                    before=before, dependencies=live,
                )

        # M15: destination rule (Resolver-owned, deterministic)
        world_state = self._world_state() or {}
        world_place = (world_state.get("location") or {}).get("place_id")
        dest_code, dest_detail = _m15_destination_verdict(
            target, world_place=world_place, hermes_home=self.hermes_home
        )
        if dest_code is not None:
            return _reject(
                normalized, "rejected", _M15_DESTINATION_CODES.get(dest_code, CODE_UNKNOWN_TARGET),
                reason_code=f"{dest_code}: {dest_detail}",
                before=before, dependencies=live,
            )
        if target["kind"] == module.PLACEMENT_HAND and target["slot_id"] != placement["slot_id"]:
            other = module.slot_occupant(state, target["slot_id"])
            if other is not None and other != object_id:
                return _reject(
                    normalized, "rejected", CODE_OCCUPIED,
                    reason_code=f"OCCUPIED: {target['slot_id']} already holds {other}",
                    before=before, dependencies=live,
                )

        self.ledger.append_unlocked(
            self._entry(
                normalized, status="prepared", reason_code="place_prepared",
                before=before, dependencies=live,
            )
        )
        if crash_after == CRASH_AFTER_PREPARED:
            raise ResolverCrash("crash after prepared (test)")

        try:
            updated = module.place_object(
                state, object_id, target, expected_revision=state["revision"]
            )
            module.SubstrateStore(module.substrate_path(self.hermes_home)).save(
                updated, expected_revision=state["revision"]
            )
        except module.BodySubstrateError as error:
            self.ledger.append_unlocked(
                self._entry(
                    normalized, status="failed", reason_code="substrate_rejected",
                    before=before, dependencies=live,
                )
            )
            return _reject(
                normalized, "failed", CODE_NOT_ALLOWED, reason_code=str(error),
                before=before, dependencies=live, ledger_status="failed",
            )
        if crash_after == CRASH_AFTER_MUTATION:
            raise ResolverCrash("crash after mutation (test)")

        after = module.read_model(self._substrate())
        self.ledger.append_unlocked(
            self._entry(
                normalized, status="committed", reason_code="place_committed",
                before=before, after=after, dependencies=live,
                world_mutation_refs=[f"placement.{object_id}={target['kind']}"],
            )
        )
        return _result(
            normalized, status="applied", code=CODE_OK, reason_code="place_committed",
            before=before, after=after, dependencies=live, ledger_status="committed",
        )

    def _resolve_pose(
        self, normalized: Mapping[str, Any], *, crash_after: Optional[str]
    ) -> dict[str, Any]:
        module, state, blocked = self._substrate_gate(normalized)
        if blocked is not None:
            return blocked

        _require_params(normalized["params"], {"pose", "orientation"})
        finished, _frame = self._replay_frame(normalized)
        if finished is not None:
            return finished

        try:
            pose = _text(normalized["params"].get("pose"), "pose", maximum=32)
            if pose not in module.POSES:
                raise ResolverError(f"pose must be one of {list(module.POSES)}")
            orientation = normalized["params"].get("orientation")
            if orientation is not None:
                orientation = _text(orientation, "orientation", maximum=32)
                if orientation not in module.ORIENTATIONS:
                    raise ResolverError(
                        f"orientation must be one of {list(module.ORIENTATIONS)}"
                    )
        except ResolverError as error:
            return _reject(
                normalized, "rejected", CODE_INVALID_PROPOSAL, reason_code=str(error)
            )

        live = self._dependencies()
        stale = self._stale_keys(normalized["observed_dependencies"], live)
        if stale:
            return _reject(
                normalized, "rejected", CODE_STALE_STATE, reason_code="stale_dependency",
                dependencies=live, stale_dependencies=stale,
            )

        before = module.read_model(state)
        if state["pose"] == pose and (orientation is None or state["orientation"] == orientation):
            return _reject(
                normalized, "noop", CODE_NOOP, reason_code="already_in_pose",
                before=before, after=before, dependencies=live,
            )

        self.ledger.append_unlocked(
            self._entry(
                normalized, status="prepared", reason_code="pose_prepared",
                before=before, dependencies=live,
            )
        )
        if crash_after == CRASH_AFTER_PREPARED:
            raise ResolverCrash("crash after prepared (test)")

        try:
            updated = module.set_pose(
                state, pose, orientation=orientation, expected_revision=state["revision"]
            )
            module.SubstrateStore(module.substrate_path(self.hermes_home)).save(
                updated, expected_revision=state["revision"]
            )
        except module.BodySubstrateError as error:
            self.ledger.append_unlocked(
                self._entry(
                    normalized, status="failed", reason_code="substrate_rejected",
                    before=before, dependencies=live,
                )
            )
            return _reject(
                normalized, "failed", CODE_NOT_ALLOWED, reason_code=str(error),
                before=before, dependencies=live, ledger_status="failed",
            )
        if crash_after == CRASH_AFTER_MUTATION:
            raise ResolverCrash("crash after mutation (test)")

        after = module.read_model(self._substrate())
        self.ledger.append_unlocked(
            self._entry(
                normalized, status="committed", reason_code="pose_committed",
                before=before, after=after, dependencies=live,
                world_mutation_refs=[f"body.pose={pose}"],
            )
        )
        return _result(
            normalized, status="applied", code=CODE_OK, reason_code="pose_committed",
            before=before, after=after, dependencies=live, ledger_status="committed",
        )

    # -- reconciliation ---------------------------------------------------

    def reconcile(self, execution_id: str) -> dict[str, Any]:
        """Report what is known about an execution.  Never invents an outcome."""

        frame = self.ledger.frame(execution_id)
        if not frame["known"]:
            return {
                "kind": "world_action_reconciliation",
                "schema_version": SCHEMA_VERSION,
                "execution_id": execution_id,
                "known": False,
                "result": None,
                "verdict": "NOT_FOUND",
            }
        if frame["reason_code"] == "reconciliation_required" or frame["reconciliation_required"]:
            verdict = "UNKNOWN"
        elif frame["result"] == ledger.RESULT_SUCCESS:
            verdict = "SUCCESS"
        elif frame["result"] == ledger.RESULT_FAILURE:
            verdict = "FAILURE"
        else:
            verdict = "UNKNOWN"
        return {
            "kind": "world_action_reconciliation",
            "schema_version": SCHEMA_VERSION,
            "execution_id": execution_id,
            "known": True,
            "state": frame["state"],
            "result": frame["result"],
            "verdict": verdict,
            "reconciliation_required": frame["reconciliation_required"],
            "action_type": frame["action_type"],
            "attempts": frame["attempts"],
        }


def execute_move_compat(
    target_location_id: Any,
    *,
    execution_id: Any,
    authority: str,
    confirm_apply: bool = False,
    hermes_home: Optional[Path | str] = None,
    environment: Optional[Mapping[str, str]] = None,
    observed_dependencies: Optional[Mapping[str, Any]] = None,
) -> dict[str, Any]:
    """Compatibility wrapper: the old ``execute_world_move`` signature.

    Behaviour is preserved for callers that do not supply observed dependency
    versions -- the dependency check is simply vacuous, exactly like before.
    """

    resolver = WorldResolver(hermes_home, environment=environment)
    return resolver.resolve(
        {
            "action": "MOVE",
            "execution_id": execution_id,
            "authority": authority,
            "confirm_apply": confirm_apply,
            "observed_dependencies": dict(observed_dependencies or {}),
            "params": {"destination_location_id": target_location_id},
        }
    )
