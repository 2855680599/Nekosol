#!/usr/bin/env python3
"""ProductionNativeMemoryResolver (V0) -- orchestration adapter only.

Declared single pipeline (§43):

    Hermes identity
      -> Native namespace
      -> ReadOnlyProductionRecallAdapter   (sealed read path over the real store)
      -> Surface V1                        (M3 `_surface_payload`, sealed)
      -> ProductionConsumptionEvaluatorV0_3(sealed policy + sealed implementation)
      -> Runtime V1-compatible MemoryObjects (per-unit authority preserved)

This module implements NO new Recall, NO new Surface, NO new Consumption, NO new
ranking and NO new policy.  It only orchestrates the sealed components and maps
their output onto Runtime V1 `MemoryObject`s.

Fail-closed rules:
  * sealed component bytes must match their recorded SHA256, else UNAVAILABLE;
  * an identity that cannot be mapped to exactly one Native namespace -> (),
  * no unambiguous current-turn Native event -> (), never a guess;
  * anything that is not a V0.3 CONSUME contributes nothing.
"""
from __future__ import annotations

import hashlib
import json
import os
import pathlib
import re
import sys
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable
from urllib.parse import quote

RUNTIME_ROOT = pathlib.Path(os.environ.get("CHIYO_NATIVE_MEMORY_RUNTIME_ROOT")
                            or pathlib.Path(__file__).resolve().parents[1])
SEALED_DIR = RUNTIME_ROOT / "native_recall"
NATIVE_ROOT = pathlib.Path(__file__).resolve().parents[1] / "src"

for _p in (str(RUNTIME_ROOT), str(SEALED_DIR), str(NATIVE_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from memory_runtime_v1.runtime import Authority, MemoryObject, Namespace  # noqa: E402

FAULT_ENV = "CHIYO_NATIVE_MEMORY_FAULT_INJECT"
RESOLVER_TYPE = "PRODUCTION_NATIVE"
RESOLVER_VERSION = "production-native-memory-resolver-v0"

SEALED_SHA256 = {
    "readonly_recall_adapter.py":
        "fe0f57efb43fee099fc449f580024f72e202bfeb704d78830c3d180dbd53fb26",
    "production_consumption_evaluator_v0_3.py":
        "141822d19049a8a5247b68885616cd0c156d616dea84d36ab099584605fc1b41",
    "production_consumption_evaluator_v0.py":
        "f7fdeccecea1c1a8377f4806d1d09b1a203367eb58bdc56505e021c264eb2400",
    "CANDIDATE_CONSUMPTION_POLICY_V0_3.json":
        "6228d24345249aed862f56484004c522e7573760d0288480ca3e87008a1514b2",
}
M3_CONFIG = os.environ.get("CHIYO_M3_CONFIG", "./native/config/m3-shadow.json")
CONFIG_ENV = "CHIYO_NATIVE_MEMORY_CONTEXT_CONFIG"
DEFAULT_WIRING = "./config/native/memory-context-wiring.json"
TELEGRAM_ENV = os.environ.get("CHIYO_IDENTITY_ENV", "./config/native/telegram.env")
PARTICIPANTS = os.environ.get("CHIYO_PARTICIPANTS_FILE", "./config/native/participants.json")

ROLE_TO_AUTHORITY = {
    "user": Authority.DIRECT_USER_EVIDENCE,
    "assistant": Authority.ASSISTANT_SELF_HISTORY,
    "tool": Authority.TOOL_OBSERVED_EVIDENCE,
    "system": Authority.SUMMARY_OR_INTERPRETATION,
    "unknown": Authority.UNKNOWN,
}
SPEAKER_TO_ROLE = {"user": "user", "chiyo": "assistant"}


def _sha256(path: pathlib.Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _read_env(path: str) -> dict[str, str]:
    out: dict[str, str] = {}
    try:
        for line in pathlib.Path(path).read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip().strip('"').strip("'")
    except OSError:
        return {}
    return out


def conversation_id_for(namespace: str, chat_id: str) -> str:
    """The Native adapter's own derivation -- reimplemented verbatim, not invented."""
    return "tg-" + hashlib.sha256((namespace + ":" + chat_id).encode("utf-8")).hexdigest()[:24]
_IDENTITY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


def _strict_id(value: Any) -> str | None:
    """Accept only a clean identifier: str/int, unpadded, whitespace-free, bounded.

    Deliberately stricter than ``str().strip()``: a malformed identity must fail
    closed rather than be normalised into a valid one.
    """
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return None
    text = str(value)
    return text if _IDENTITY_RE.fullmatch(text) else None


@dataclass
class ResolverStats:
    invocations: int = 0
    identity_denied: int = 0
    no_current_turn: int = 0
    no_surface: int = 0
    consumed_bundles: int = 0
    emitted_memory_objects: int = 0
    errors: int = 0
    fault_injections: int = 0

    def as_dict(self) -> dict[str, int]:
        return dict(self.__dict__)


def _ensure_dependency_aliases() -> None:
    """Make the sealed Native M3 import resolve inside the Hermes process.

    ``app/m3.py`` does ``from tools.m3_recall_lab import FixtureCandidateProvider``.
    Hermes ships its own *regular* ``tools`` package, which shadows the Native
    namespace package as soon as it is imported, so that import fails even though
    the Native file exists.  Load the Native module from its real path and register
    it under the exact expected name.  Nothing sealed is modified.
    """
    import importlib.util

    name = "tools.m3_recall_lab"
    if name in sys.modules:
        return
    path = NATIVE_ROOT / "tools" / "m3_recall_lab.py"
    if not path.is_file():
        return
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        return
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)


class ScopedVisibleReader:
    """Read-only projection of what the host actually put in short context."""
    def __init__(self,reader,visible_turn_ids):
        self.reader=reader;self.visible_turn_ids=frozenset(str(x) for x in visible_turn_ids)
    def __getattr__(self,name):return getattr(self.reader,name)
    def build_trigger(self,event,all_events):
        from dataclasses import replace
        from app.m3 import digest
        trigger=self.reader.build_trigger(event,all_events)
        visible=[e for e in all_events[:trigger.sequence]
            if e.get('conversation_id')==event.get('conversation_id') and
               (str(e.get('turn_id')) in self.visible_turn_ids or e.get('event_id')==event.get('event_id'))]
        return replace(trigger,visible_event_ids=tuple(str(e['event_id']) for e in visible),
            visible_history_digest=digest([{'event_id':e['event_id'],'source_origin':e['source_origin'],
                'content_sha256':digest(e['content'])} for e in visible]))


class ProductionNativeMemoryResolver:
    """The single production resolver.  Orchestration only."""

    resolver_type = RESOLVER_TYPE
    resolver_configured = True
    version = RESOLVER_VERSION

    def __init__(
        self,
        *,
        m3_config_path: str = M3_CONFIG,
        telegram_env_path: str = TELEGRAM_ENV,
        participants_path: str = PARTICIPANTS,
        sealed_dir: pathlib.Path = SEALED_DIR,
        max_memories: int = 8,
        recency_seconds: int | None = None,
        environment=None,
    ) -> None:
        self.environment = os.environ if environment is None else dict(environment)
        self.sealed_dir = pathlib.Path(sealed_dir)
        self.m3_config_path = m3_config_path
        self.telegram_env_path = telegram_env_path
        self.participants_path = participants_path
        self.max_memories = int(max_memories)
        # the anchor window is a runtime knob (wiring config), not a policy constant
        _wiring: dict[str, Any] = {}
        try:
            _wiring = json.loads(pathlib.Path(
                self.environment.get(CONFIG_ENV) or DEFAULT_WIRING).read_text(encoding="utf-8"))
        except Exception:
            _wiring = {}
        self.recency_seconds = int(
            recency_seconds if recency_seconds is not None
            else _wiring.get("resolver_anchor_window_seconds", 900))
        self.live_text_gate_enabled = bool(_wiring.get("resolver_live_text_gate", True))
        self.gate_denied_live_text = 0
        self.stats = ResolverStats()
        self._resolve_lock = threading.RLock()
        self.state = "UNAVAILABLE"
        self.reason = "not_initialised"
        self.component_sha256: dict[str, str | None] = {}
        self.m3_sha256: dict[str, str | None] = {}
        self._adapter = None
        self._evaluator = None
        self._classify_intent = None
        self._identity = {"participant_id": None, "transport": None,
                          "transport_user_id": None, "namespace": None,
                          "native_conversation_id": None}
        self._initialise()

    # ---------------------------------------------------------------- init --- #
    def _initialise(self) -> None:
        try:
            _ensure_dependency_aliases()
            for name, expected in SEALED_SHA256.items():
                path = self.sealed_dir / name
                actual = _sha256(path) if path.is_file() else None
                self.component_sha256[name] = actual
                if actual != expected:
                    self.reason = "sealed_component_mismatch:" + name
                    return
            self.m3_sha256 = {
                "app/m3.py": _sha256(NATIVE_ROOT / "app" / "m3.py"),
                "m3_worker.py": _sha256(NATIVE_ROOT / "m3_worker.py"),
            }
            self._identity = self._load_identity_binding()
            if not self._identity.get("transport_user_id"):
                self.reason = "native_participant_binding_missing"
                return

            from app.m3 import classify_intent as _sealed_classify_intent
            self._classify_intent = _sealed_classify_intent
            from readonly_recall_adapter import ReadOnlyProductionRecallAdapter
            from production_consumption_evaluator_v0_3 import (
                ProductionConsumptionEvaluatorV0_3, policy_sha256,
            )

            m3_cfg = json.loads(pathlib.Path(self.m3_config_path).read_text(encoding="utf-8"))
            self._adapter = ReadOnlyProductionRecallAdapter(m3_cfg)
            observed = policy_sha256(self.sealed_dir / "CANDIDATE_CONSUMPTION_POLICY_V0_3.json")
            if observed != SEALED_SHA256["CANDIDATE_CONSUMPTION_POLICY_V0_3.json"]:
                self._adapter.close()
                self._adapter = None
                self.reason = "policy_sha_mismatch"
                return
            self._evaluator = ProductionConsumptionEvaluatorV0_3(policy_sha=observed)
            self.state = "READY"
            self.reason = "ok"
        except Exception as exc:                       # pragma: no cover - fail closed
            self.state = "UNAVAILABLE"
            self.reason = "init_failure:" + type(exc).__name__

    def _load_identity_binding(self) -> dict[str, Any]:
        env = _read_env(self.telegram_env_path)
        participants: list[dict[str, Any]] = []
        try:
            participants = json.loads(
                pathlib.Path(self.participants_path).read_text(encoding="utf-8")
            ).get("participants", [])
        except Exception:
            participants = []
        match = next((p for p in participants if str(p.get("transport")) == "telegram"), None) or {}
        chat_id = str(match.get("transport_user_id") or env.get("TELEGRAM_ALLOWED_USER_ID") or "")
        namespace = str(env.get("TELEGRAM_CONVERSATION_NAMESPACE") or "native-test-v1")
        return {
            "participant_id": match.get("participant_id"),
            "transport": match.get("transport"),
            "transport_user_id": chat_id or None,
            "namespace": namespace,
            "native_conversation_id": conversation_id_for(namespace, chat_id) if chat_id else None,
        }

    # ------------------------------------------------------------ identity --- #
    def map_identity(self, ids: dict[str, Any] | None) -> dict[str, Any]:
        """§44 -- Hermes identity -> Native namespace.  Any uncertainty fails closed."""
        ids = dict(ids or {})
        out: dict[str, Any] = {"ok": False, "reason": None, "observed": {},
                               "native": dict(self._identity)}
        strict: dict[str, str | None] = {}
        for key in ("user_id", "session_id", "conversation_id", "turn_id"):
            value = ids.get(key)
            out["observed"][key] = None if value is None else hashlib.sha256(
                str(value).encode("utf-8")).hexdigest()[:16]
            strict[key] = _strict_id(value)
        if any(strict[k] is None for k in strict):
            # absent vs malformed are distinct outcomes: an absent identity is missing, a
            # present-but-dirty one (padded, whitespace, over-long, wrong type) is malformed.
            absent = any(ids.get(k) in (None, "") for k in strict)
            out["reason"] = "IDENTITY_MISSING" if absent else "IDENTITY_MALFORMED"
            return out
        bound = self._identity.get("transport_user_id")
        if not bound or strict["user_id"] != str(bound):
            # a session whose Hermes user does not equal the Native participant binding
            out["reason"] = "IDENTITY_USER_NOT_BOUND_TO_NATIVE_NAMESPACE"
            return out
        if not self._identity.get("native_conversation_id"):
            out["reason"] = "NATIVE_NAMESPACE_UNRESOLVED"
            return out
        out["ok"] = True
        out["reason"] = "OK"
        return out

    # -------------------------------------------------------- current turn --- #
    def resolve_current_turn(self, native_conversation_id: str) -> dict[str, Any] | None:
        """The newest persisted Native user turn in the mapped conversation.

        Fails closed when there is none, when it is not the newest persisted user
        turn, or when it is older than the recency window: an old turn is not
        'the current turn' and must never be replayed as if it were.
        """
        if self._adapter is None:
            return None
        events = self._adapter.reader.events()
        users = [e for e in events
                 if e.get("source_origin") == "USER_VISIBLE_INPUT"
                 and e.get("delivery_status") == "RECEIVED"
                 and str(e.get("conversation_id")) == str(native_conversation_id)]
        if not users:
            return None
        # the anchor must be a turn the *sealed* classifier itself considers recall-shaped,
        # otherwise the sealed M3 would (correctly) refuse to surface anything for it
        newest = users[-1]
        if self._classify_intent is not None:
            intent, _reason = self._classify_intent(str(newest.get('content') or ''))
            if intent not in ('P1', 'P2'):
                return None
        try:
            occurred = datetime.fromisoformat(str(newest.get("occurred_at")))
            if occurred.tzinfo is None:
                occurred = occurred.replace(tzinfo=timezone.utc)
            age = (datetime.now(timezone.utc) - occurred).total_seconds()
        except Exception:
            return None
        if age > self.recency_seconds:
            return None
        return {"event_id": str(newest["event_id"]), "age_seconds": round(age, 3),
                "conversation_id": str(native_conversation_id)}

    # ------------------------------------------------------------- resolve --- #
    def __call__(self, *, visible_turn_ids=None, **kwargs):
        lock=getattr(self,'_resolve_lock',None)
        if lock is None:self._resolve_lock=lock=threading.RLock()
        with lock:
            adapter=getattr(self,'_adapter',None)
            if visible_turn_ids is None or adapter is None:return self._resolve(**kwargs)
            old_reader=adapter.reader;old_worker_reader=adapter.worker.reader
            scoped=ScopedVisibleReader(old_reader,visible_turn_ids)
            adapter.reader=adapter.worker.reader=scoped
            try:return self._resolve(**kwargs)
            finally:adapter.reader=old_reader;adapter.worker.reader=old_worker_reader

    def _resolve(self, *, agent: Any = None, ids: dict[str, Any] | None = None,
                 current_user_event_id: Any = None, current_user_text: Any = None,
                 messages: Any = None) -> tuple[MemoryObject, ...]:
        self.stats.invocations += 1
        if self.state != "READY":
            return ()
        # test-only fault injection (default OFF): lets the canary exercise the hook's
        # NATIVE_ERROR_BYPASS / ordinary-request-survives path with a real failure
        _fault = self.environment.get(FAULT_ENV, "").strip().lower()
        if _fault in ("resolver_raise", "raise"):
            self.stats.fault_injections += 1
            raise RuntimeError("fault injection: resolver_raise")
        try:
            mapping = self.map_identity(ids)
            if not mapping["ok"]:
                self.stats.identity_denied += 1
                return ()
            if self.live_text_gate_enabled and self._classify_intent is not None:
                live_intent, live_reason = self._classify_intent(
                    str(current_user_text or ""))
                if live_intent not in ("P1", "P2"):
                    self.gate_denied_live_text += 1
                    return ()
            trigger = self.resolve_current_turn(self._identity["native_conversation_id"])
            if trigger is None or (current_user_event_id is not None and
                                   str(trigger['event_id']) != str(current_user_event_id)):
                self.stats.no_current_turn += 1
                return ()
            got = self._adapter.evaluate_trigger(trigger["event_id"])
            avail = got["availability"]
            if not str(avail.get("outcome", "")).startswith("SURFACE"):
                self.stats.no_surface += 1
                return ()
            objects = self._consume(got, trigger, mapping, ids)
            self.stats.emitted_memory_objects += len(objects)
            return tuple(objects)
        except Exception:
            self.stats.errors += 1
            return ()

    def _consume(self, got: dict[str, Any], trigger: dict[str, Any],
                 mapping: dict[str, Any], ids: dict[str, Any]) -> list[MemoryObject]:
        selected = {str(x) for x in (got["availability"].get("selected_refs") or [])}
        chosen = [c for c in got["candidates"] if c["candidate_id"] in selected]
        all_events = self._adapter.reader.events()
        by_id = {str(e["event_id"]): e for e in all_events}
        trigger_event = by_id[trigger["event_id"]]
        target = self._adapter.reader.build_trigger(trigger_event, all_events)
        visible_ids = {str(x) for x in target.visible_event_ids}
        episodes, membership = self._adapter.reader.m1_data()
        visible_episode_ids = []
        for ep in episodes:
            ids_ = [str(r["event_id"]) for r in membership.get(str(ep["episode_id"]), [])]
            if ids_ and all(i in visible_ids for i in ids_):
                visible_episode_ids.append(str(ep["episode_id"]))
        visible_contents = [str(by_id[i].get("content", "")) for i in visible_ids if i in by_id]

        # Runtime V1 must be able to compare the object against the request: _capsule()
        # accepts only memories whose namespace equals request.namespace.  The object is
        # therefore scoped to the REQUEST namespace (the Hermes identity), while the Native
        # conversation/trigger ids stay in telemetry and reports for traceability.
        namespace = Namespace(
            user=str(ids["user_id"]),
            session=str(ids["session_id"]),
            conversation=str(ids["conversation_id"]),
            turn=str(ids["turn_id"]),
        )
        out: list[MemoryObject] = []
        for candidate in chosen:
            units = []
            for eid in candidate["source_event_ids"]:
                row = by_id.get(str(eid))
                if row is None:
                    continue
                role = SPEAKER_TO_ROLE.get(str(row.get("speaker") or ""), "unknown")
                units.append({
                    "event_id": str(row["event_id"]),
                    "source_role": role,
                    "authority_class": ROLE_TO_AUTHORITY[role].value,
                    "timestamp": str(row.get("occurred_at") or ""),
                    "source_ref": str(row.get("primary_source_ref_id") or ""),
                    "lineage_status": "FULL" if row.get("primary_source_ref_id") else "PARTIAL",
                    "content": str(row.get("content") or ""),
                    "instructional_force": "NONE",
                    "authority_promoted": False,
                    "provenance_present": bool(row.get("conversation_id") and row.get("turn_id")
                                               and row.get("primary_source_ref_id")),
                })
            record = {
                "memory_id": candidate["candidate_id"],
                "episode_id": candidate["episode_id"],
                "evidence_units": units,
                "surface_payload": got["availability"].get("surface_payload") or {},
                "current_user_event_id": trigger["event_id"],
                "current_user_timestamp": str(trigger_event.get("occurred_at") or ""),
                "current_user_content": str(trigger_event.get("content") or ""),
                "visible_exact_event_ids": sorted(visible_ids),
                "visible_exact_episode_ids": visible_episode_ids,
                "visible_exact_contents": visible_contents,
                "supersession_metadata": None,
                "invalidation_metadata": None,
                # the runtime builds the bundle and keeps one segment per unit with its own
                # authority class (V0_3_RUNTIME_INPUT_AUDIT: the runtime constructs it)
                "provider_preserves_per_evidence_authority": True,
                "bundle_declared_authority_class": None,
            }
            decision = self._evaluator.evaluate_record(record)
            if decision.decision != "CONSUME":
                continue
            self.stats.consumed_bundles += 1
            # ONE MemoryObject per evidence unit: Runtime V1 carries a single authority per
            # object, so a bundle must never be collapsed into one promoted object.
            for unit in units:
                out.append(MemoryObject(
                    memory_id="%s#%s" % (candidate["candidate_id"], unit["event_id"]),
                    text=unit["content"],
                    authority=ROLE_TO_AUTHORITY[unit["source_role"]],
                    source_event_ids=(unit["event_id"],),
                    namespace=namespace,
                    uncertainty="NONE",
                    valid_from=unit["timestamp"] or None,
                ))
                if len(out) >= self.max_memories:
                    return out
        return out

    # ----------------------------------------------------------- reporting --- #
    def report(self) -> dict[str, Any]:
        """Sanitised runtime status for the hook report (no raw identifiers)."""
        return {
            "resolver_version": self.version,
            "resolver_type": RESOLVER_TYPE if self.state == "READY" else "UNAVAILABLE",
            "resolver_configured": True,
            "resolver_state": self.state,
            "resolver_reason": self.reason,
            "invocations": self.stats.invocations,
            "identity_denied": self.stats.identity_denied,
            "no_current_turn": self.stats.no_current_turn,
            "no_surface": self.stats.no_surface,
            "consumed_bundles": self.stats.consumed_bundles,
            "emitted_memory_objects": self.stats.emitted_memory_objects,
            "errors": self.stats.errors,
            "native_conversation_id": self._identity.get("native_conversation_id"),
            "anchor_window_seconds": self.recency_seconds,
            "live_text_gate_enabled": self.live_text_gate_enabled,
            "denied_by_live_text_gate": self.gate_denied_live_text,
            "fault_injections": self.stats.fault_injections,
        }
    def startup_contract(self) -> dict[str, Any]:
        """§46 startup contract."""
        healthy = self.state == "READY"
        return {
            "resolver_version": self.version,
            "resolver_type": RESOLVER_TYPE if healthy else "UNAVAILABLE",
            "resolver_configured": True,
            "resolver_state": self.state,
            "resolver_reason": self.reason,
            "resolver_invocations_at_startup": 0,
            "init_failure_behaviour": {
                "resolver_state": "UNAVAILABLE",
                "native_context": "FAIL_CLOSED",
                "ordinary_hermes": "HEALTHY",
                "silent_production_to_empty_fallback": False,
            },
            "identity_binding": {k: v for k, v in self._identity.items()},
            "sealed_components": self.component_sha256,
            "m3_components": self.m3_sha256,
        }

    def close(self) -> None:
        if self._adapter is not None:
            try:
                self._adapter.close()
            except Exception:
                pass


__all__ = ["ProductionNativeMemoryResolver", "RESOLVER_TYPE", "RESOLVER_VERSION",
           "SEALED_SHA256", "conversation_id_for"]
