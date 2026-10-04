#!/usr/bin/env python3
"""Production Decision trigger (M13C §3).

Runs the bounded World action-space selector once per chat turn, producing a
**terminal Decision record** with full provenance.  On a concrete action choice
it builds the Structured Intent and hands it to the intent executor — the same
sealed chain validated by the ACTION_INTENT ticket.

Hard rules:

* bounded and fail-open: any failure yields ``NO_ACTION`` and never blocks a
  chat turn (section 18);
* natural conversation is expected to produce ``NO_ACTION`` most of the time
  (section 6) — the selector may return ``no_valid_choice``, which yields no
  intent at all;
* the mutation gate (off/shadow/canonical) is honoured exactly as stored in the
  gateway environment (section 4/7);
* every terminal Decision is persisted with a ``decision_id`` for provenance
  (section 11);
* the model never sees a revision and never returns one (M13B frozen rule);
* the environment override (``WORLD_BODY_DECISION_SELECTED_CANDIDATE``) is a
  **test-only** path (M13C-FINAL §B).  It executes only when the shared test
  key ``WORLD_BODY_DECISION_TEST_KEY`` equals ``M13C_TEST``; without the key
  the selection is refused as ``OVERRIDE_UNAUTHORIZED`` and recorded, never
  executed.  A production turn therefore cannot mutate through this path, and
  every refusal is visible in the decision log for soak accounting.
"""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
import time
from typing import Any, Mapping, Optional

try:  # loaded as a plugin package
    from . import intent_executor as iex  # type: ignore
    from . import world_body_client as wbc  # type: ignore
except ImportError:  # noqa: BLE001 - direct load
    import intent_executor as iex  # type: ignore
    import world_body_client as wbc  # type: ignore

logger_name = "chiyo_decision_trigger"

SCHEMA_DECISION = "world.action.decision.v1"
KIND_DECISION = "world_action_decision"

#: Outcomes of one Decision evaluation.
NO_ACTION = "NO_ACTION"
ACTION_SELECTED = "ACTION_SELECTED"
GATE_BLOCKED = "GATE_BLOCKED"
EVAL_ERROR = "EVAL_ERROR"

#: M13C-FINAL §B: the env override is the M13C_TEST harness path.  It must
#: never become a long-term production control back door, so executing an
#: override requires this shared key.  Production turns carry no such key.
OVERRIDE_TEST_KEY_ENV = "WORLD_BODY_DECISION_TEST_KEY"
OVERRIDE_TEST_VALUE = "M13C_TEST"
SELECTION_SOURCE_TEST = "environment_override:M13C_TEST"
SELECTION_SOURCE_UNAUTHORIZED = "environment_override:unauthorized"

#: PROD-CUTOVER §4/§5: production selection source.  A production Action comes
#: from a REAL runtime event (the body's own state machine), never from an
#: override, a keyword, or an LLM.  Gated by the production mutation gate plus
#: an explicit switch; both fail closed.
PRODUCTION_EVENTS_ENV = "WORLD_BODY_PRODUCTION_EVENTS"
PRODUCTION_EVENTS_ON = "on"
PRODUCTION_EVENTS_OFF = "off"
SELECTION_SOURCE_EVENT = "runtime_event:body_recovery_interrupted"
REST_POSE = "lying"
#: Note: the production signal projection carries recovery {state, mode,
#: trend} and the band latch only — raw progress/fatigue are intentionally not
#: exposed, so the event is expressed purely in those summary facts.
OVERRIDE_UNAUTHORIZED = "OVERRIDE_UNAUTHORIZED"

#: Persist terminal decisions here (gateway-side xinxi, same convention as
#: decision_gate's decision_log.jsonl).
DECISION_LOG_RELATIVE = ("xinxi", "world_decision_log.jsonl")

#: Deduplication: one Decision per (session, turn).
_LAST_SEEN: dict[str, float] = {}
_LAST_TTL = 3600.0


def _decision_log_path(environment: Optional[Mapping[str, str]] = None) -> pathlib.Path:
    env = environment if environment is not None else os.environ
    home = pathlib.Path(str(env.get("HERMES_HOME") or "./data/persona"))
    return home.joinpath(*DECISION_LOG_RELATIVE)


def _decision_id(payload: Mapping[str, Any]) -> str:
    return "decision:" + hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True,
                   separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:32]



# --- AG-OBS-2 observability additions ---------------------------------------------------
#: §20/§21: stable, read-only version of the *decision semantics* in force on this path.
#: Bump ONLY when decision semantics change; telemetry-only edits must not bump it (§22).
POLICY_VERSION = "bridge-decision-policy-v0"
#: §29 typed reason codes, audited from the constants above (not invented).
RECEIPT_REASON_CODES = {
    "default_no_action": "DEFAULT_NO_ACTION",
    SELECTION_SOURCE_TEST: "TEST_OVERRIDE",
    SELECTION_SOURCE_UNAUTHORIZED: "ENVIRONMENT_OVERRIDE",
    SELECTION_SOURCE_EVENT: "MAINTENANCE_EVENT",
}


def _receipt_emitter():
    """Lazily import the observability emitter. Returns None when it is unavailable (§33)."""
    for name in ("observability.choice_receipt_emitter", "choice_receipt_emitter"):
        try:
            return __import__(name, fromlist=["emit"])
        except Exception:  # noqa: BLE001 - observation must never break a decision
            continue
    # AG-OBS-2C import fix (2026-09-30, revision 2): the emitter ships at ``<runtime>/observability``,
    # which is NOT on the decision process's sys.path — the gateway loads this module from its PLUGIN
    # directory, so walking up from __file__ alone never reaches it.  Probe, in order: the runtime root
    # from the environment, the walk-up candidates, then the declared deploy location.  Appended (never
    # inserted) so nothing can be shadowed; the first candidate that really contains
    # ``observability/__init__.py`` wins, and the import is retried exactly once.
    try:
        import sys as _sys
        _candidates = []
        _env_root = os.environ.get("CHIYO_WORLD_RUNTIME")
        if _env_root:
            _candidates.append(_env_root)
        _probe = os.path.dirname(os.path.abspath(__file__))
        for _ in range(4):
            _probe = os.path.dirname(_probe)
            if _probe and _probe not in _candidates:
                _candidates.append(_probe)
        _candidates.append("./world")
        for _candidate in _candidates:
            if os.path.isfile(os.path.join(_candidate, "observability", "__init__.py")):
                if _candidate not in _sys.path:
                    _sys.path.append(_candidate)
                return __import__("observability.choice_receipt_emitter", fromlist=["emit"])
    except Exception:  # noqa: BLE001 - observation must never break a decision
        pass
    return None


def _receipt_reason_code(selection_source: str) -> str:
    code = RECEIPT_REASON_CODES.get(selection_source)
    if code:
        return code
    if selection_source.startswith("runtime_event:"):
        return "RUNTIME_EVENT"
    if selection_source.startswith("environment_override:"):
        return "ENVIRONMENT_OVERRIDE"
    return "UNKNOWN"


def _emit_receipt(decision_record: Mapping[str, Any]) -> None:
    """Emit a Decision Receipt V1 for this decision. Fail-closed: never raises (§14/§33).

    Reads the record, writes only the observability shadow store, and returns nothing that the
    caller could act on.
    """
    emitter = _receipt_emitter()
    if emitter is None:
        return
    try:
        emitter.emit(dict(decision_record))
    except Exception:  # noqa: BLE001 - telemetry failure != decision failure (§33/§34)
        pass


def _persist_decision(record: Mapping[str, Any]) -> None:
    """Append one terminal Decision to the gateway-side decision log."""

    try:
        path = _decision_log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False,
                                    sort_keys=True) + "\n")
    except OSError as error:  # logging failure must never break the turn
        import logging

        logging.getLogger(logger_name).warning(
            "decision log write failed: %s", error)


def production_events_enabled() -> bool:
    """Explicit switch for acting on the body's own runtime event.

    Anything other than the literal ``on`` is off (fail-closed).
    """

    return str(os.environ.get(PRODUCTION_EVENTS_ENV) or PRODUCTION_EVENTS_OFF) \
        .strip().lower() == PRODUCTION_EVENTS_ON


def self_maintenance_policy():
    """The self-maintenance policy (M15C).  Reading it must never raise."""

    try:
        from . import world_self_maintenance as _sm  # type: ignore
    except ImportError:  # flat load (tests, CLI)
        import importlib
        _sm = importlib.import_module("world_self_maintenance")
    return _sm.policy_from_environment()


ACTIVITY_ENV = "WORLD_BODY_ACTIVITY_ENABLED"


def activity_enabled() -> bool:
    """Explicit switch for the natural-activity source (default off)."""

    return str(os.environ.get(ACTIVITY_ENV) or "off").strip().lower() == "on"


def activity_source():
    """The activity store (M15D).  Reading it must never raise."""

    try:
        from . import world_activity as _wa  # type: ignore
    except ImportError:  # flat load (tests, CLI)
        import importlib
        _wa = importlib.import_module("world_activity")
    return _wa.activity_from_environment()


def evaluate_activity_event(*, client, action_space, gate):
    """One time-driven tick -> at most one candidate, or None."""

    import action_intent as ai

    if gate != ai.GATE_CANONICAL or not activity_enabled():
        return None
    store = activity_source()
    try:
        signals = client.get_body_signals()
        recovery = (signals.data or {}).get("recovery") if signals.outcome == wbc.OK \
            else None
    except Exception:  # fail-closed: no signal read, no event
        return None
    verdict = store.tick(recovery=recovery)
    hint = verdict.get("candidate_hint")
    if not hint:
        return None
    for candidate in action_space.get("candidates") or []:
        if candidate.get("candidate_id") == hint:
            return {"candidate_id": hint, "event": verdict["event"],
                    "severity": 1, "dwell_seconds": 0.0,
                    "trigger": verdict.get("trigger")}
    return None


def evaluate_production_event(*, client, action_space: Mapping[str, Any],
                              gate: str) -> Optional[str]:
    """Return a candidate_id when a REAL runtime event demands an action.

    Conditions (all required):
      * the production mutation gate is ``canonical`` (§6: gate canonical),
      * the production-events switch is explicitly ``on``,
      * the body's own state machine reports an interrupted recovery whose
        fatigue has reached the runtime's notice threshold (band facts only),
      * the bounded action space actually offers the rest candidate.

    Anything else returns ``None`` — a natural turn then stays NO_ACTION.
    """

    import action_intent as ai

    if gate != ai.GATE_CANONICAL:
        return None
    if not production_events_enabled():
        return None
    try:
        signals = client.get_body_signals()
    except Exception:  # fail-closed: no signal read means no event
        return None
    if signals.outcome != wbc.OK or not isinstance(signals.data, dict):
        return None

    # M15C: the self-maintenance policy owns the state machine (dwell,
    # hysteresis, cooldowns, event dedupe).  It only classifies the body's own
    # state; the Decision still has to choose, and the Resolver still commits.
    policy = self_maintenance_policy()
    verdict = policy.observe(signals.data)
    if not verdict.get("action_allowed"):
        # M15D: the time-driven activity tick is a second, independent source;
        # it is consulted only here, so the maintenance policy keeps priority.
        return evaluate_activity_event(client=client, action_space=action_space,
                                       gate=gate)
    want = "pose:%s" % REST_POSE
    for candidate in action_space.get("candidates") or []:
        if candidate.get("candidate_id") == want:
            return {"candidate_id": want, "event": verdict["event"],
                    "severity": verdict["severity"],
                    "dwell_seconds": verdict["dwell_seconds"]}
    return None


def evaluate_turn_decision(**hook_kwargs: Any) -> dict[str, Any]:
    """Evaluate one chat turn and produce a terminal Decision record.

    Returns ``{"decision": record, "intent": …|None, "result": …|None}``.
    Never raises.
    """

    session_id = str(hook_kwargs.get("session_id") or "")
    turn_id = str(hook_kwargs.get("turn_id") or "")
    now = time.time()

    # --- deduplication: one Decision per (session, turn) -------------------
    dedup_key = "%s|%s" % (session_id, turn_id)
    last = _LAST_SEEN.get(dedup_key)
    if last is not None and now - last < _LAST_TTL:
        return {"decision": None, "intent": None, "result": None,
                "outcome": "DUPLICATE_TURN", "decision_id": None}
    _LAST_SEEN[dedup_key] = now
    stale = [k for k, ts in _LAST_SEEN.items() if now - ts > _LAST_TTL]
    for key in stale:
        _LAST_SEEN.pop(key, None)

    import action_intent as ai

    started = time.monotonic()
    try:
        # share the bridge's client so tests (and the gateway) see one socket
        # session per process, and so a NOT_READY runtime fails identically
        try:
            import bridge_init as _bi

            client = _bi._CLIENT or wbc.WorldBodyClient()
        except ImportError:  # noqa: BLE001
            client = wbc.WorldBodyClient()
        if not client.enabled:
            return {"decision": None, "intent": None, "result": None,
                    "outcome": "INTEGRATION_OFF", "decision_id": None}

        status = client.get_status()
        if status.outcome != wbc.OK or not (status.data or {}).get("ready"):
            return {"decision": None, "intent": None, "result": None,
                    "outcome": "RUNTIME_NOT_READY", "decision_id": None}

        snapshot = client.get_snapshot()
        if snapshot.outcome != wbc.OK or not isinstance(snapshot.data, dict):
            return {"decision": None, "intent": None, "result": None,
                    "outcome": "SNAPSHOT_FAILED", "decision_id": None}

        # M15: availability truth -- object candidates (PICK_UP / PLACE) are
        # emitted by the gateway from its capability verdict, never invented
        # here.  A client that cannot answer simply offers no object candidate.
        available_actions = None
        try:
            probe = client.get_available_actions()
            if getattr(probe, "outcome", None) == wbc.OK and isinstance(probe.data, dict):
                available_actions = probe.data
        except Exception:  # fail-closed: no truth means no object candidate
            available_actions = None
        action_space = ai.build_action_space(snapshot.data, available_actions=available_actions)

        # --- the bounded Decision selector --------------------------------
        # V0 policy: the World action space is offered but the default
        # terminal Decision for ordinary chat is NO_ACTION.  A concrete
        # selection is possible ONLY through the M13C_TEST harness path: the
        # override env var plus the shared test key.  Without the key the
        # selection is refused and recorded (M13C-FINAL §B) — a production
        # turn cannot mutate through this path.
        selected_candidate_id = os.environ.get(
            "WORLD_BODY_DECISION_SELECTED_CANDIDATE") or None
        # --- production runtime event (PROD-CUTOVER §4/§5) ----------------
        production_event = None
        production_event = None
        if not selected_candidate_id:
            production_event = evaluate_production_event(
                client=client, action_space=action_space, gate=ai.resolve_gate())

        decision_record: dict[str, Any] = {
            "kind": KIND_DECISION,
            "schema_version": SCHEMA_DECISION,
            "decision_id": "",
            "session_id": session_id,
            "turn_id": turn_id,
            "platform": str(hook_kwargs.get("platform") or ""),
            "captured_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "location": dict(snapshot.data.get("location") or {}),
            "pose": snapshot.data.get("pose"),
            "action_space_fingerprint": ai.fingerprint_action_space(action_space),
            "candidates_offered": [c.get("candidate_id")
                                   for c in action_space.get("candidates") or []],
            # --- AG-OBS-2 observability facts (recorded only; no decision logic touched) ----
            "policy_version": POLICY_VERSION,
            # AG-OBS-2C correction (2026-09-30): the V0 path evaluates NO eligibility stage, so any
            # per-candidate eligibility value here would be a claim the producer cannot support.  An
            # empty map means "no fact recorded": the emitter emits per-candidate UNKNOWN with this
            # basis, and the frozen DP8 contract accepts it.  (It refuses an ineligible candidate with
            # no rejection reason — which is exactly what this field used to fabricate.)
            "eligibility": {},
            "eligibility_basis": "no_eligibility_stage_in_v0_path",
            "rejections": {},
            "rejections_basis": "no_object_candidate_generated_when_gate_closed",
            "gate_state": str(ai.resolve_gate()),
            "seed_if_used": None,
            "disposition_refs_used": "UNKNOWN",
            "reason_codes": [_receipt_reason_code("default_no_action")],
            "terminal_decision": NO_ACTION,
            "selection_source": "default_no_action",
            "override_requested": bool(selected_candidate_id),
            "override_authorized": False,
            "production_event": bool(production_event),
            "production_events_enabled": production_events_enabled(),
        }

        if production_event:
            selected_candidate_id = production_event["candidate_id"]
            decision_record["selection_source"] = "runtime_event:%s" % (
                production_event.get("event") or "self_maintenance")
            decision_record["maintenance_event"] = production_event.get("event")
            decision_record["maintenance_severity"] = production_event.get("severity")
            decision_record["maintenance_dwell_seconds"] = round(
                float(production_event.get("dwell_seconds") or 0.0), 3)

        if not selected_candidate_id:
            decision_record["decision_id"] = _decision_id(decision_record)
            _persist_decision(decision_record)
            _emit_receipt(decision_record)
            return {"decision": decision_record, "intent": None, "result": None,
                    "outcome": NO_ACTION, "decision_id": decision_record["decision_id"]}

        # --- an override was requested: authorize it (M13C-FINAL §B) ------
        if not production_event and \
                os.environ.get(OVERRIDE_TEST_KEY_ENV) != OVERRIDE_TEST_VALUE:
            decision_record["terminal_decision"] = OVERRIDE_UNAUTHORIZED
            decision_record["selection_source"] = SELECTION_SOURCE_UNAUTHORIZED
            decision_record["selected_candidate_id"] = selected_candidate_id
            decision_record["decision_id"] = _decision_id(decision_record)
            _persist_decision(decision_record)
            _emit_receipt(decision_record)
            return {"decision": decision_record, "intent": None, "result": None,
                    "outcome": OVERRIDE_UNAUTHORIZED,
                    "decision_id": decision_record["decision_id"]}

        # --- a concrete selection: validate it as a terminal choice ---------
        candidate = next(
            (c for c in action_space.get("candidates") or []
             if c.get("candidate_id") == selected_candidate_id), None)
        if candidate is None:
            decision_record["terminal_decision"] = "INVALID_SELECTION"
            decision_record["selection_source"] = SELECTION_SOURCE_TEST
            decision_record["selected_candidate_id"] = selected_candidate_id
            decision_record["override_authorized"] = True
            decision_record["decision_id"] = _decision_id(decision_record)
            _persist_decision(decision_record)
            _emit_receipt(decision_record)
            return {"decision": decision_record, "intent": None, "result": None,
                    "outcome": "INVALID_SELECTION",
                    "decision_id": decision_record["decision_id"]}

        decision_record["terminal_decision"] = ACTION_SELECTED
        decision_record["selected_candidate_id"] = selected_candidate_id
        if production_event:
            decision_record["selection_source"] = "runtime_event:%s" % (
                production_event.get("event") or "self_maintenance")
            decision_record["production_event"] = True
        else:
            decision_record["selection_source"] = SELECTION_SOURCE_TEST
            decision_record["override_authorized"] = True
        decision_record["decision_id"] = _decision_id(decision_record)

        # §37/§38: the receipt is formed here - after the candidate set is final and BEFORE
        # any action side effect (execute_intent happens below).
        _emit_receipt(decision_record)

        choice = {
            "status": "validated",
            "snapshot_fingerprint": decision_record["action_space_fingerprint"],
            "action": "SELECT",
            "candidate_id": selected_candidate_id,
            "intent_summary": str(
                os.environ.get("WORLD_BODY_DECISION_INTENT_SUMMARY")
                or ("Decision %s" % decision_record["decision_id"][:18])),
            "validated_at": decision_record["captured_at"],
        }
        intent = ai.build_intent(choice, action_space,
                                 decision_ref=decision_record["decision_id"],
                                 created_at=decision_record["captured_at"])
        decision_record["intent_id"] = intent["intent_id"]
        decision_record["execution_id"] = intent["execution_id"]

        _persist_decision(decision_record)

        result = iex.execute_intent(intent, client=client)
        if production_event and str(result.get("intent_status")) == "APPLIED":
            try:
                self_maintenance_policy().note_action()
            except Exception:  # bookkeeping must never break the turn
                pass
        decision_record["intent_status"] = result.get("intent_status")
        decision_record["outcome"] = result.get("outcome")
        _persist_decision(decision_record)   # terminal update

        return {"decision": decision_record, "intent": intent,
                "result": result, "outcome": result.get("outcome"),
                "decision_id": decision_record["decision_id"]}
    except Exception as error:  # fail-open: a decision failure never blocks chat
        import logging

        logging.getLogger(logger_name).warning(
            "decision evaluation failed (turn=%s): %s", turn_id, error)
        return {"decision": None, "intent": None, "result": None,
                "outcome": EVAL_ERROR, "decision_id": None,
                "detail": "%s: %s" % (type(error).__name__, error)}
    finally:
        elapsed = time.monotonic() - started
        if elapsed > 1.0:
            import logging

            logging.getLogger(logger_name).info(
                "decision evaluation took %.2fs (turn=%s)", elapsed, turn_id)


def decision_hook(**hook_kwargs: Any) -> Optional[dict[str, Any]]:
    """``pre_llm_call`` hook: evaluate the Decision, then run the capsule hook.

    Returns the capsule context (or None).  The Decision itself is evaluated
    for provenance and (when selected) executed — its result is NOT put in
    front of the model as text; Expression grounding happens through the
    bounded capsule, and mutation results are visible to the next turn's
    snapshot, not as an injected narrative.
    """

    import logging

    log = logging.getLogger(logger_name)
    _agop2_production_decision = None  # AG-OP-2B shadow input, never a decision input
    try:
        outcome = evaluate_turn_decision(**hook_kwargs)
        _agop2_production_decision = (outcome.get("decision")
                                     if isinstance(outcome, dict) else None)
        log.info(
            "world decision evaluated (outcome=%s decision=%s intent_status=%s "
            "session=%s turn=%s)",
            outcome.get("outcome"),
            (outcome.get("decision_id") or "-")[:26],
            (outcome.get("result") or {}).get("intent_status", "-"),
            hook_kwargs.get("session_id"), hook_kwargs.get("turn_id"),
        )
    except Exception as error:  # never break the chat turn
        log.warning("decision trigger failed: %s", error)

    # AG-OP-2B: the decision is already made and persisted above; observing it cannot change
    # it.  Bounded, fail-open, and written only into the shadow observability directory.
    _agop2_shadow_observe(_agop2_production_decision)

    # the capsule hook lives in this plugin's __init__ (canonical source
    # file: bridge_init.py).  When loaded as a package the relative import
    # works; when the gateway execs __init__.py directly, decision_trigger
    # is imported as a sibling module inside the package, so reach the
    # capsule through the same package import used above.
    try:
        from . import bridge_init as _bi  # type: ignore
    except ImportError:  # noqa: BLE001 - direct-load (tests, CLI)
        import importlib.util as _ilu
        _me = pathlib.Path(__file__).resolve()
        _capsule = _me.with_name("bridge_init.py")
        if not _capsule.exists():  # live install maps it to __init__.py
            _capsule = _me.with_name("__init__.py")
        _spec = _ilu.spec_from_file_location("bridge_init", str(_capsule))
        _bi = _ilu.module_from_spec(_spec)
        _spec.loader.exec_module(_bi)
    return _bi.world_context_hook(**hook_kwargs)


def register(ctx) -> None:
    """Replace the plain bridge hook with the decision-triggering hook."""

    ctx.register_hook("pre_llm_call", decision_hook)


# --- AG-OP-2B read-only shadow (observability only) -------------------------
AGOP2_SHADOW_ENV = "AGOP2_SHADOW"
AGOP2_SHADOW_TIMEOUT_SECONDS = 2.0


def _agop2_shadow_observe(production_decision=None) -> None:
    """Observe the opportunity space beside the decision that was already made.

    Reads owner state through the frozen read-only ports and writes one observation record into
    ``<observability>/agop2_shadow/``.  It cannot change the decision, it cannot raise into the
    caller, and it has a hard time bound, so observability can never delay or alter the turn.
    """
    if str(os.environ.get(AGOP2_SHADOW_ENV) or "on").strip().lower() == "off":
        return None
    try:
        from agop2 import (
            ShadowStore,
            build_production_backend,
            load_plugin_modules,
            observe_with_timeout,
            resolve_root,
        )
        from agop2.production_ports import ProductionSourcePorts

        _here = pathlib.Path(__file__).resolve().parent
        _modules = load_plugin_modules(str(_here))
        _backend = build_production_backend(modules=_modules)
        _store = ShadowStore(resolve_root())
        observe_with_timeout(
            ProductionSourcePorts(_backend),
            timeout_seconds=AGOP2_SHADOW_TIMEOUT_SECONDS,
            store=_store,
            seed=0,
            modules=_modules,
            production_decision=production_decision,
        )
    except Exception:  # noqa: BLE001 - an observation failure is never a decision failure
        return None
    return None
