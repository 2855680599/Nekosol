"""LPC0A section 14 -- Life Runtime <-> Hermes gateway adapter.

Authority: LPC0A order sections 14-17 (gateway wiring, shadow only first), 20 (writer
handoff), 21-22 (what "Agency ON" means), 33-35 (chat/continuity acceptance), 37
(production observability), 38 (auto-degrade).

Design rules enforced here, each traceable to the order
------------------------------------------------------
* ``CHIYO_LIFE_INTEGRATION_MODE`` mirrors the house convention used by the existing
  ``WORLD_BODY_INTEGRATION_MODE``: ``off`` (default) / ``shadow`` / ``live``.
* The hook **always returns ``None``**.  The dispatch layer only injects when a hook
  returns ``{"context": ...}`` or a ``str`` (``hermes_cli/plugins_dispatch.py:173``), so
  no runtime metadata can ever reach the model (section 15) and ordinary chat does not
  turn into a status line (section 33).
* **Zero LLM calls, structurally.**  The frozen AG-1 needs an ``AgencyCognitionAdapter``
  to produce a Decision and V0.1 ships with cognition disabled, so a cycle can never
  reach a provider.  ``llm_calls_invoked`` is therefore 0 by construction (section 16).
* **Shadow writes nothing but its own observation record.**  In shadow the runtime is not
  even assembled, so no canonical lease is taken and ``activity_writes = 0`` holds
  trivially (sections 14, 17, 19).
* **No private text is ever recorded** (section 37): only hashed correlation ids, counts
  and enum outcomes.  ``ObservedUserRequestEvent`` has no free-text field by design, so
  raw chat cannot enter the Life runtime either.
* Every failure is swallowed and logged; the hook never breaks a chat turn.
"""
from __future__ import annotations

import atexit
import hashlib
import json
import logging
import os
import sys
import threading
import time
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Mapping, Optional

logger = logging.getLogger("chiyo_life_runtime")

INSTALL_ROOT = Path(os.environ.get("CHIYO_LIFE_INSTALL_ROOT") or "./data/life")
RELEASE_SCRIPTS = Path(os.environ.get('CHIYO_LIFE_RELEASE_SCRIPTS') or INSTALL_ROOT / "current" / "scripts")
STATE_ROOT = INSTALL_ROOT / "state"
LOG_DIR = INSTALL_ROOT / "logs"
SHADOW_DIR = LOG_DIR / "shadow"
METRICS_PATH = LOG_DIR / "life_metrics.json"
EVENTS_PATH = LOG_DIR / "life_events.jsonl"
SHADOW_PATH = SHADOW_DIR / "gateway-shadow.jsonl"
HEARTBEAT_PATH = LOG_DIR / "life_adapter_heartbeat.json"
DEGRADED_PATH = LOG_DIR / "READ_ONLY_DEGRADED.json"
CONTROL_PATH = LOG_DIR / "LIFE_CONTROL.json"
IDENTITY_PATH = LOG_DIR / "life_runtime_identity.json"


MODE_ENV = "CHIYO_LIFE_INTEGRATION_MODE"
MODE_OFF = "off"
MODE_SHADOW = "shadow"
MODE_LIVE = "live"
#: Operational control file. When present it wins over the unit environment, so the
#: shadow -> live handoff needs no second gateway restart (order sections 31 and 35).
MODE_FILE = LOG_DIR / "INTEGRATION_MODE"


BJT = timezone(timedelta(hours=8))

_LOCK = threading.RLock()
_RUNTIME: Any = None
_RUNTIME_ERROR: Optional[str] = None

#: LPC0B-R2: the host LLM facade. Read once to build the transport, then dropped (section 11).
_CTX_LLM: Any = None
_COGNITION: Any = None
_COGNITION_ERROR: Optional[str] = None
_DEGRADED: Optional[str] = None

_METRICS: dict[str, Any] = {
    "schema": "lpc0a.life_adapter_metrics.v1",
    "hook_invocations": 0,
    "hook_errors": 0,
    "llm_calls_invoked": 0,
    "activity_commits": 0,
    "activity_rejects": 0,
    "adoption_reconciliations": 0,
    "duplicate_or_stale_rejects": 0,
    "consequence_proposals": 0,
    "cycle_runs": 0,
    "cycle_failures": 0,
    "runtime_load_attempts": 0,
    "runtime_loaded": False,
    "writer_lease_held": False,
    "canonical_revision": None,
    "foreground_activity_ref": None,
    "mode": MODE_OFF,
    "first_hook_at": None,
    "last_hook_at": None,
    "last_error": None,
    "integrity_alarm": None,
}
def integration_mode() -> str:
    """Resolve the integration mode.

    The explicit operational control file wins over the static unit environment so the
    shadow -> live handoff does not need a second gateway restart (order sections 31 and
    35: one controlled restart is authorized and a second must be merged, not spent).
    Deleting the file falls back to the unit environment, which defaults to ``off``.
    """
    try:
        if MODE_FILE.is_file():
            raw = MODE_FILE.read_text(encoding="utf-8").strip().lower()
            if raw in {MODE_OFF, MODE_SHADOW, MODE_LIVE}:
                return raw
    except OSError:
        pass
    raw = str(os.environ.get(MODE_ENV) or MODE_OFF).strip().lower()
    return raw if raw in {MODE_OFF, MODE_SHADOW, MODE_LIVE} else MODE_OFF




# ---------------------------------------------------------------------------------
# LPC0B-R3-Lite order section 三A: non-blocking audit bridge
# ---------------------------------------------------------------------------------

_AUDIT: Any = None
_AUDIT_ERROR: Optional[str] = None


def _audit() -> Any:
    """The LPC0B audit bridge module, or ``None`` when it is unavailable.

    Imported lazily so a missing audit layer degrades to "no audit" instead of breaking
    plugin import.  Audit is optional observability, never a load-bearing dependency.
    """
    global _AUDIT, _AUDIT_ERROR
    if _AUDIT is not None or _AUDIT_ERROR is not None:
        return _AUDIT
    try:
        import lpc0b_audit_bridge as bridge  # noqa: WPS433 - plugin-local module
    except Exception as exc:  # noqa: BLE001
        _AUDIT_ERROR = f"{type(exc).__name__}: {exc}"
        logger.warning("LPC0B audit bridge unavailable; audit disabled: %s", _AUDIT_ERROR)
        return None
    _AUDIT = bridge
    return _AUDIT


def start_audit() -> bool:
    """Configure and start the audit subsystem.  Called once, from ``register(ctx)``.

    Startup recovery of the journals happens here -- on the gateway start path -- and never
    on a turn (order section 三A requirement 2).  Never raises.
    """
    bridge = _audit()
    if bridge is None:
        return False
    try:
        bridge.configure(install_root=INSTALL_ROOT,
                         audit_root=os.environ.get("CHIYO_LPC0B_AUDIT_ROOT") or None)
        started = bool(bridge.start())
    except Exception as exc:  # noqa: BLE001
        logger.warning("LPC0B audit start failed; audit disabled: %s", exc)
        return False
    with _LOCK:
        _METRICS["audit_started"] = started
    return started


def stop_audit(**kwargs: Any) -> dict[str, Any]:
    """Stop the audit writer, draining what is already queued.  Never raises."""
    bridge = _audit()
    if bridge is None:
        return {}
    try:
        return dict(bridge.stop(**kwargs))
    except Exception as exc:  # noqa: BLE001
        return {"stop_error": f"{type(exc).__name__}: {exc}"}


def audit_health() -> dict[str, Any]:
    """The published audit health, or an explicit UNAVAILABLE.  Never raises."""
    bridge = _audit()
    if bridge is None:
        return {"state": "UNAVAILABLE", "reasons": ["BRIDGE_UNAVAILABLE"],
                "cognition_effect_permitted": False}
    try:
        return dict(bridge.audit_health())
    except Exception as exc:  # noqa: BLE001
        return {"state": "UNAVAILABLE", "reasons": [f"HEALTH_FAILED:{type(exc).__name__}"],
                "cognition_effect_permitted": False}


def _cognition_effect_permitted() -> bool:
    """Fail-closed effect gate.  No bridge, or any unhealthy audit, means OFF."""
    bridge = _audit()
    if bridge is None:
        return False
    try:
        return bool(bridge.cognition_effect_permitted())
    except Exception:  # noqa: BLE001
        return False


def _enqueue_observation(record: Mapping[str, Any]) -> bool:
    """Non-blocking.  Never opens a file, never raises."""
    bridge = _audit()
    if bridge is None:
        return False
    try:
        return bool(bridge.enqueue_observation(record))
    except Exception:  # noqa: BLE001
        return False


def _enqueue_metrics() -> bool:
    """Non-blocking metrics snapshot.  Reads in-memory counters only."""
    bridge = _audit()
    if bridge is None:
        return False
    try:
        with _LOCK:
            snapshot = dict(_METRICS)
        return bool(bridge.enqueue_metrics(snapshot))
    except Exception:  # noqa: BLE001
        return False


def _enqueue_integrity(reason: str) -> bool:
    bridge = _audit()
    if bridge is None:
        return False
    try:
        return bool(bridge.enqueue_integrity(reason))
    except Exception:  # noqa: BLE001
        return False


# ---------------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(BJT).isoformat(timespec="seconds")


def _digest(value: Any) -> str:
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()[:16]





def _ensure_dirs() -> None:
    for d in (LOG_DIR, SHADOW_DIR):
        try:
            d.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass


def _append_jsonl(path: Path, record: Mapping[str, Any]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
    except OSError as exc:  # observability must never break the turn
        logger.warning("life adapter could not append %s: %s", path.name, exc)


def _write_metrics() -> None:
    try:
        _ensure_dirs()
        tmp = METRICS_PATH.with_name(f".{METRICS_PATH.name}.tmp.{uuid.uuid4().hex}")
        tmp.write_text(json.dumps(_METRICS, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        os.replace(tmp, METRICS_PATH)
    except OSError as exc:
        logger.warning("life adapter could not write metrics: %s", exc)


def _alarm(reason: str) -> None:
    """Order section 38: any integrity alarm drops straight to READ_ONLY_DEGRADED.

    The marker write below is now the only synchronous write left on the turn path.  It is
    deliberately *not* routed through the audit queue: it is a failure signal rather than
    optional audit, it must survive an unusable audit subsystem, and it happens at most once
    per process.  The alarm is also enqueued so the audit journal records it.  See the design
    record section 7.
    """
    _enqueue_integrity(reason)
    global _DEGRADED
    with _LOCK:
        if _DEGRADED is None:
            _DEGRADED = reason
            _METRICS["integrity_alarm"] = reason
            try:
                _ensure_dirs()
                DEGRADED_PATH.write_text(
                    json.dumps({"schema": "lpc0a.read_only_degraded.v1", "state": "READ_ONLY_DEGRADED",
                                "reason": reason, "at": _now_iso()}, indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8")
            except OSError:
                pass
            logger.error("LIFE RUNTIME: entering READ_ONLY_DEGRADED (%s)", reason)


def _close_runtime() -> None:
    global _RUNTIME
    with _LOCK:
        if _RUNTIME is not None:
            try:
                _RUNTIME.close()
            except Exception as exc:  # noqa: BLE001
                logger.warning("life runtime close failed: %s", exc)
            _RUNTIME = None


atexit.register(_close_runtime)


def _atexit_stop_audit() -> None:
    """Drain and stop the audit writer at process exit.  Never raises."""
    stop_audit(drain=True, timeout_s=2.0)


atexit.register(_atexit_stop_audit)


# ---------------------------------------------------------------------------------
# runtime assembly (live mode only)
# ---------------------------------------------------------------------------------


def _runtime() -> Any:
    global _RUNTIME, _RUNTIME_ERROR
    with _LOCK:
        if _RUNTIME is not None:
            return _RUNTIME
        if _RUNTIME_ERROR is not None:
            return None
        _METRICS["runtime_load_attempts"] += 1
        try:
            if str(RELEASE_SCRIPTS) not in sys.path:
                sys.path.insert(0, str(RELEASE_SCRIPTS))
            import life_integration_ag2 as ag2          # noqa: WPS433 - lazy by design
            import life_runtime_production as lrp       # noqa: WPS433

            switches = _kill_switches()
            # Order section 三A: the audit journal object is injected here so its writes go
            # through the bounded queue.  When the audit bridge is not running the factory
            # stays ``None`` and the frozen ``ag2.CandidateAuditJournal`` is used unchanged.
            bridge = _audit()
            factory = None
            if bridge is not None:
                try:
                    if bridge.is_running():
                        factory = bridge.candidate_journal_factory
                except Exception:  # noqa: BLE001
                    factory = None
            _RUNTIME = lrp.build_production_life_runtime(
                STATE_ROOT, kill_switches=switches, audit_journal_factory=factory,
            )
            _METRICS["runtime_loaded"] = True
            _METRICS["writer_lease_held"] = bool(_RUNTIME.lease.held)
            _METRICS["kill_switches"] = switches.to_dict()
            logger.info(
                "life runtime loaded: release=%s state=%s lease=%s switches=%s",
                RELEASE_SCRIPTS, STATE_ROOT, _RUNTIME.lease.instance_id, switches.to_dict(),
            )
        except Exception as exc:  # noqa: BLE001 - fail closed, never raise into the turn
            _RUNTIME_ERROR = f"{type(exc).__name__}: {exc}"
            _METRICS["last_error"] = _RUNTIME_ERROR
            logger.warning("life runtime assembly failed (fail closed): %s", _RUNTIME_ERROR)
            _alarm("RUNTIME_NOT_LOADED")
            return None
        return _RUNTIME



def runtime_status() -> dict[str, Any]:
    with _LOCK:
        rt = _RUNTIME
        out = {
            "mode": integration_mode(),
            "runtime_loaded": rt is not None,
            "runtime_error": _RUNTIME_ERROR,
            "degraded": _DEGRADED,
            "metrics": dict(_METRICS),
        }
        if rt is not None:
            out["lease"] = rt.lease.status()
            out["integration_lease"] = rt.integration_lease.status()
            try:
                out["canonical_revision"] = rt.revision()
                out["foreground_activity_ref"] = rt.current_activity_ref()
            except Exception as exc:  # noqa: BLE001
                out["observe_error"] = f"{type(exc).__name__}: {exc}"
        return out


# ---------------------------------------------------------------------------------
# the hook
# ---------------------------------------------------------------------------------


def _correlation(kwargs: Mapping[str, Any]) -> dict[str, Any]:
    """Hashed turn correlation only -- never raw ids, never message text (section 37)."""
    messages = kwargs.get("messages")
    return {
        "session_hash": _digest(kwargs.get("session_id")),
        "turn_hash": _digest(kwargs.get("turn_id")),
        "task_hash": _digest(kwargs.get("task_id")),
        "platform": str(kwargs.get("platform") or kwargs.get("channel") or "unknown"),
        "inbound_message_count": len(messages) if isinstance(messages, (list, tuple)) else None,
    }


def life_hook(**kwargs: Any) -> None:
    """``pre_llm_call`` hook.  Always returns ``None``: it never injects context."""
    started = time.monotonic()
    try:
        _hook_impl(dict(kwargs), started)
    except Exception as exc:  # noqa: BLE001 - a chat turn must never break because of us
        with _LOCK:
            _METRICS["hook_errors"] += 1
            _METRICS["last_error"] = f"{type(exc).__name__}: {exc}"
        logger.warning("life adapter hook error (swallowed): %s", exc)
    return None


def _hook_impl(kwargs: Mapping[str, Any], started: float) -> None:
    """Build a minimal observation record and hand it to the audit queue.

    Order section 三A: this function must not touch the disk.  Every write that used to be
    here was a synchronous ``open("a")`` (the shadow journal, the events journal, the metrics
    snapshot) plus, through AG-0, a synchronous candidate-audit append on the same thread.
    They are now non-blocking enqueues onto a bounded queue drained by a single background
    Audit Writer.

    Three things remain synchronous on purpose, and each is declared in the design record
    ``LPC0B_WP2_R3_DESIGN_DECISION.md`` section 7: the in-memory ``_METRICS`` update, the
    ``READ_ONLY_DEGRADED`` marker (a failure signal, written at most once per process), and --
    in live mode only -- the canonical runtime assembly, which is not optional audit.
    """
    mode = integration_mode()
    with _LOCK:
        _METRICS["mode"] = mode
        _METRICS["hook_invocations"] += 1
        _METRICS["first_hook_at"] = _METRICS["first_hook_at"] or _now_iso()
        _METRICS["last_hook_at"] = _now_iso()
        degraded = _DEGRADED
    if mode == MODE_OFF or degraded is not None:
        if mode != MODE_OFF:
            _enqueue_metrics()
        return

    ts = _now_iso()
    corr = _correlation(kwargs)
    record: dict[str, Any] = {
        "schema": "lpc0a.gateway_life_observation.v1",
        "at": ts, "mode": mode, **corr,
        "activity_writes": 0,
        "world_writes_from_life": 0,
        "body_writes_from_life": 0,
        "memory_writes_from_life": 0,
        "telegram_proactive_sends": 0,
        "llm_calls": 0,
        "prompt_injection": "none",
        "durability": "queued",   # never claim a record is committed at enqueue time
    }

    if mode == MODE_SHADOW:
        # The runtime is deliberately NOT assembled in shadow: no lease, no store, no writes.
        record.update({
            "runtime_loaded_in_gateway": False,
            "would_route": "AG0_user_request_candidate_from_real_inbound",
            "would_candidate_kind": "RESPOND_USER_REQUEST",
            "would_decision": "REQUIRES_COGNITION",
            "would_adopt": None,
            "would_transition": None,
            "derivation": "LPC0A_COGNITION_DISABLED: AG-1 cannot produce a Decision without a cognition adapter",
            "rejection_reason": "LPC0A_COGNITION_DISABLED",
            "shadow_only": True,
        })
        _enqueue_observation(record)
        with _LOCK:
            _METRICS["shadow_records"] = int(_METRICS.get("shadow_records", 0)) + 1
        _enqueue_metrics()
        return

    # ---- live -------------------------------------------------------------------
    rt = _runtime()
    record["runtime_loaded_in_gateway"] = rt is not None
    if rt is None:
        record.update({"would_decision": None, "rejection_reason": _RUNTIME_ERROR or "RUNTIME_NOT_LOADED"})
        _enqueue_observation(record)
        _enqueue_metrics()
        return

    # LPC0B-R2 sections 13/14: the canonical EFFECT path is deliberately NOT invoked here.
    # Cognition effect is OFF, and section 13 requires structural isolation -- so the turn
    # path reaches no canonical Decision writer at all. The production shadow observes,
    # enqueues, and writes only its own journal; the real provider call happens on the
    # worker thread and the hook still returns None.
    revision_before = rt.revision()
    wiring = _cognition_wiring(rt)
    if wiring is None:
        reason = _COGNITION_ERROR or "COGNITION_WIRING_UNAVAILABLE"
        record.update({"cognition_shadow": {"enqueued": False, "reason": reason},
                       "rejection_reason": reason})
        _enqueue_observation(record)
        _enqueue_metrics()
        return

    # Order section 三D: the Cognition Effect may not run when its required audit evidence is
    # missing.  The gate reads the published audit health and is fail-closed -- no bridge, a
    # corrupt journal, an unrecovered gap or a queue overflow all close it.
    gate_open = _cognition_effect_permitted()
    with _LOCK:
        _METRICS["audit_effect_gate_open"] = gate_open
    if not gate_open:
        reason = "AUDIT_HEALTH_NOT_HEALTHY"
        record.update({"cognition_shadow": {"enqueued": False, "reason": reason},
                       "rejection_reason": reason,
                       "cognition_effect_permitted": False})
        _enqueue_observation(record)
        _enqueue_metrics()
        return

    cog = (wiring.on_turn(session_hash=corr["session_hash"], turn_hash=corr["turn_hash"],
                          platform=corr["platform"], observed_at=ts) or {}).get("cognition") or {}
    with _LOCK:
        _METRICS["cycle_runs"] += 1
        _key = "cognition_enqueued" if cog.get("enqueued") else "cognition_skipped"
        _METRICS[_key] = int(_METRICS.get(_key, 0)) + 1
    record.update({"cognition_shadow": cog, "would_decision": "SHADOW_ONLY",
                   "rejection_reason": cog.get("reason"),
                   "cognition_effect_permitted": True})

    revision_after = rt.revision()
    commits = max(0, revision_after - revision_before)
    record["writer_lease_instance_id"] = rt.lease.instance_id
    record["activity_writes"] = commits
    record["canonical_revision_before"] = revision_before
    record["canonical_revision_after"] = revision_after
    record["elapsed_ms"] = round((time.monotonic() - started) * 1000, 1)
    with _LOCK:
        _METRICS["activity_commits"] += commits
        if commits == 0:
            _METRICS["activity_rejects"] += 1
        _METRICS["canonical_revision"] = revision_after
        try:
            _METRICS["foreground_activity_ref"] = rt.current_activity_ref()
        except Exception:  # noqa: BLE001
            pass
    _integrity_check(rt, revision_before, revision_after, commits)
    _enqueue_observation(record)
    _enqueue_metrics()


def _integrity_check(rt: Any, before: int, after: int, commits: int) -> None:
    """Order section 38 integrity alarms."""
    if after < before or commits > 1:
        _alarm(f"REVISION_FORK_OR_SKIP:{before}->{after}")
        return
    try:
        if not rt.lease.held or not rt.lease.verify_capability(rt.activity_capability):
            _alarm("WRITER_LEASE_MISMATCH")
    except Exception:  # noqa: BLE001
        _alarm("WRITER_LEASE_MISMATCH")


def healthcheck() -> dict[str, Any]:
    """Write a heartbeat and return the adapter status. Used by the soak monitor."""
    status = runtime_status()
    status["audit"] = audit_health()
    status["at"] = _now_iso()
    try:
        _ensure_dirs()
        tmp = HEARTBEAT_PATH.with_name(f".{HEARTBEAT_PATH.name}.tmp.{uuid.uuid4().hex}")
        tmp.write_text(json.dumps(status, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        os.replace(tmp, HEARTBEAT_PATH)
    except OSError as exc:
        status["heartbeat_error"] = str(exc)
    return status


# ---------------------------------------------------------------------------------
# operational control + in-process evidence
# ---------------------------------------------------------------------------------

#: Modules whose presence in ``sys.modules`` constitutes loaded-module evidence.
LIFE_MODULE_NAMES = (
    "life_runtime_production", "life_integration_ag2", "activity_continuity", "activity_admission",
    "activity_interruption", "activity_waiting_agenda", "activity_progress_completion_lr5",
    "agency_decision_ag1", "candidate_sources_ag0", "action_reality_ledger",
    "result_settlement_ar1", "provenance_verifier", "runtime_identity", "waiting_ref_vocab",
)


def _kill_switches() -> Any:
    """Resolve the four frozen kill switches.

    The unit environment is the declarative source; ``logs/LIFE_CONTROL.json`` is the
    operational override, so an operator can move between the prescribed profiles without
    spending a second gateway restart (order sections 21, 31, 35).  The keys are the
    frozen flag names, not new synonyms.
    """
    import life_integration_ag2 as ag2  # noqa: WPS433

    base = ag2.LifeRuntimeKillSwitches.from_env()
    try:
        if CONTROL_PATH.is_file():
            data = json.loads(CONTROL_PATH.read_text(encoding="utf-8"))
            ks = dict(data.get("kill_switches") or {})
            if ks:
                base = ag2.LifeRuntimeKillSwitches(
                    life_runtime_enabled=bool(ks.get("LIFE_RUNTIME_ENABLED", base.life_runtime_enabled)),
                    agency_enabled=bool(ks.get("AGENCY_ENABLED", base.agency_enabled)),
                    action_execution_enabled=bool(ks.get("ACTION_EXECUTION_ENABLED", base.action_execution_enabled)),
                    proactive_enabled=bool(ks.get("PROACTIVE_ENABLED", base.proactive_enabled)),
                )
    except (OSError, ValueError, KeyError, TypeError) as exc:
        logger.warning("life control file ignored: %s", exc)
    return base


def loaded_module_evidence(reason: str) -> dict[str, Any]:
    """In-process loaded-module evidence.

    ``sys.modules`` inside the gateway process is the only truthful source for what that
    process imported.  ``/proc/<pid>/maps`` cannot see CPython source modules because
    CPython reads them rather than memory-mapping them, so a mapping-table count of 0 is
    not evidence of absence and is deliberately not used as proof here (order section 3).
    """
    life: dict[str, Any] = {}
    for name in LIFE_MODULE_NAMES:
        module = sys.modules.get(name)
        if module is not None:
            path = getattr(module, "__file__", None)
            digest = None
            try:
                if path:
                    digest = hashlib.sha256(Path(path).read_bytes()).hexdigest()
            except OSError:
                digest = None
            life[name] = {"file": path, "disk_sha256_observed_separately": digest, "module_object_id": id(module)}
    return {
        "schema": "lpc0a.life_gateway_identity.v1",
        "reason": reason,
        "at": _now_iso(),
        "process_id": os.getpid(),
        "python_version": sys.version,
        "install_root": str(INSTALL_ROOT),
        "state_root": str(STATE_ROOT),
        "integration_mode": integration_mode(),
        "hook_registered": bool(_METRICS.get("hook_registered")),
        "hook_invocations": _METRICS.get("hook_invocations"),
        "runtime_loaded_in_process": _RUNTIME is not None,
        "writer_lease_held_in_process": bool(_RUNTIME is not None and _RUNTIME.lease.held),
        "life_modules_in_sys_modules": life,
        "life_module_count": len(life),
        "method": ("in-process sys.modules read; a disk read is NOT treated as proof of what "
                   "the process imported"),
    }


def write_identity(reason: str) -> dict[str, Any]:
    doc = loaded_module_evidence(reason)
    rt = _RUNTIME
    if rt is not None:
        try:
            doc["runtime_identity"] = rt.runtime_identity(
                candidate_id="m16f1r71s7-20260929T090939Z", build_id="lpc0a-v0.1.0")
            doc["writer_lease"] = rt.lease.status()
            doc["integration_lease"] = rt.integration_lease.status()
            doc["leaked_lease_can_be_reacquired_elsewhere"] = False
        except Exception as exc:  # noqa: BLE001
            doc["runtime_identity_error"] = f"{type(exc).__name__}: {exc}"
    try:
        _ensure_dirs()
        tmp = IDENTITY_PATH.with_name(f".{IDENTITY_PATH.name}.tmp.{uuid.uuid4().hex}")
        tmp.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        os.replace(tmp, IDENTITY_PATH)
    except OSError as exc:
        logger.warning("life identity write failed: %s", exc)
    return doc


def note_registration() -> None:
    """Called from the plugin's ``register(ctx)`` so the gateway records the registration.

    In live mode the production runtime is assembled here, at gateway start, rather than
    lazily on the first turn: the writer lease is then acquired deterministically at load
    time and the in-process identity document carries the real loaded-code digests even
    when no traffic has arrived yet (order sections 20, 32). The cognition shadow is built
    here too, so a wiring failure shows up in the journal at load time instead of on the
    first real turn.
    """
    with _LOCK:
        _METRICS["hook_registered"] = True
        _METRICS["hook_registered_at"] = _now_iso()
    mode = integration_mode()
    runtime = None
    if mode == MODE_LIVE:
        runtime = _runtime()
    wiring = _cognition_wiring(runtime) if runtime is not None else None
    write_identity("register")
    logger.warning(
        "LIFE ADAPTER: register() ran; pre_llm_call hook registered (pid=%s mode=%s "
        "runtime_loaded=%s cognition_shadow=%s cognition_error=%s)",
        os.getpid(), mode, _RUNTIME is not None, wiring is not None, _COGNITION_ERROR)


# Import-time evidence: this runs inside whatever process loads the plugin, which is the
# only place a loaded-module claim can honestly be made.
write_identity("import")


# ---------------------------------------------------------------------------------
# LPC0B-R2 sections 9-13: production cognition shadow wiring
# ---------------------------------------------------------------------------------


def register_context(ctx: Any) -> None:
    """Hand the plugin context to the composition root exactly once.

    Order section 11: only the transport may see the host LLM API, so the context is read
    here to build that transport and is then dropped -- it is never stored below this layer.
    """
    global _CTX_LLM
    try:
        _CTX_LLM = getattr(ctx, "llm", None)
    except Exception:  # noqa: BLE001
        _CTX_LLM = None
    try:
        on_unload = getattr(ctx, "on_unload", None)
        if callable(on_unload):
            on_unload(_stop_cognition)
    except Exception as exc:  # noqa: BLE001
        logger.warning("life adapter could not register on_unload: %s", exc)


def _stop_cognition() -> None:
    """Order section 10: the cognition worker must stop when the plugin unloads."""
    wiring = _COGNITION
    if wiring is not None:
        try:
            wiring.stop()
        except Exception as exc:  # noqa: BLE001
            logger.warning("cognition stop on unload failed: %s", exc)


def _cognition_wiring(rt: Any = None) -> Any:
    """Build the single cognition wiring for this process, or return None (fail closed)."""
    global _COGNITION, _COGNITION_ERROR
    with _LOCK:
        if _COGNITION is not None:
            return _COGNITION
        if _COGNITION_ERROR is not None:
            return None
        runtime = rt if rt is not None else _RUNTIME
        if runtime is None:
            _COGNITION_ERROR = "RUNTIME_NOT_LOADED"
            return None
        if _CTX_LLM is None:
            _COGNITION_ERROR = "CTX_LLM_UNAVAILABLE"
            logger.warning("LIFE COGNITION: ctx.llm unavailable; cognition shadow stays OFF")
            return None
        try:
            import life_cognition_wiring as lcw  # noqa: WPS433 - plugin-local module

            _COGNITION = lcw.wire(_CTX_LLM, install_root=INSTALL_ROOT, runtime=runtime)
            if _COGNITION is None:
                _COGNITION_ERROR = "WIRING_UNAVAILABLE"
        except Exception as exc:  # noqa: BLE001
            _COGNITION_ERROR = f"{type(exc).__name__}: {exc}"
            logger.warning("LIFE COGNITION: wiring failed (fail closed): %s", _COGNITION_ERROR)
        return _COGNITION
