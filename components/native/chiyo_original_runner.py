"""Original Instance Native Telegram Runner.
Integrates Native Runtime with:
1. Canonical Persona & Relationship (USER.md, MEMORY.md, relationship.yaml)
2. History reader (Hermes state.db bootstrap)
3. M37 Memory Resolver
4. Telegram long polling with update deduplication and delivery ledger
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
from pathlib import Path
import signal
import sqlite3
import sys
import threading
import time
import uuid
from typing import Any

INTEG_ROOT = Path(__file__).resolve().parent
SRC = INTEG_ROOT / 'src'
ADAPTERS = INTEG_ROOT / 'adapters'

sys.path.insert(0, str(INTEG_ROOT))
sys.path.insert(0, str(ADAPTERS))
sys.path.insert(0, str(SRC))
sys.path.append('./native')
sys.path.append('./native')
sys.path.append('./native')

from continuity_bridge import CanonicalPersonaProvider, HermesHistoryReader
from telegram_adapter import (
    TelegramAdapter,
    TelegramApi,
    TelegramState,
    _TOKEN_FOR_LOGGING,
    utc_now,
    chat_fingerprint,
    conversation_id_for,
    MAX_TELEGRAM_TEXT
)
from app.model import DirectProvider
from app.session import ConversationStore
from app.trace import TraceStore
from app.evidence import (
    EvidenceWriter,
    EvidenceEvent,
    new_event_id,
    USER_ORIGIN,
    CHIYO_ORIGIN,
    USER_ROLE,
    CHIYO_ROLE,
    RECEIVED,
    DELIVERED,
)
from types import SimpleNamespace
from memory_control import MemoryControlLedger, MemoryProjection

LOGGER = logging.getLogger("chiyo.original_runner")

class OriginalNativeRuntime:
    def __init__(self, data_dir: Path, trace_dir: Path, model_key: str):
        self.data_dir = data_dir
        self.trace_dir = trace_dir
        self.store = ConversationStore(str(data_dir))
        self.traces = TraceStore(str(trace_dir))
        # No local M0 evidence store: the canonical M37 evidence is written by the
        # Native->M37 bridge, so the runtime does not open a second one.
        self.persona_provider = CanonicalPersonaProvider()
        self.history_reader = HermesHistoryReader()
        self.world_body = None
        if os.environ.get('CHIYO_NATIVE_WORLD_BODY_ENABLED', 'false').lower() == 'true':
            from integration.world_body_client import WorldBodyClient
            self.world_body = WorldBodyClient(timeout=0.3, read_retries=0)
        # The canonical M37 M0 store is written through the bridge module, so the
        # runtime and the delivery poller share one derivation of the store path,
        # the owner binding and the conversation id.
        self.m37_bridge = None
        self.m37_binding: dict = {}
        try:
            import m37_m0_bridge as _bridge
            self.m37_bridge = _bridge
            self.m37_binding = _bridge.load_binding()
            LOGGER.info("m37.bridge.ready conversation_id=%s",
                        self.m37_binding.get("conversation_id"))
        except (Exception, SystemExit) as exc:
            LOGGER.warning("m37.bridge.unavailable error_class=%s", type(exc).__name__)

        self.memory_controls = None
        control_path = os.environ.get('CHIYO_MEMORY_CONTROL_DB') or str(data_dir / 'memory-controls.sqlite')
        if self.m37_bridge is not None and self.m37_binding:
            self.memory_controls = MemoryControlLedger(
                control_path, self.m37_bridge.load_m0_path(),
                owner=self.m37_binding['chat_id'], conversation=self.m37_binding['conversation_id'])
        self.memory_control_unavailable = self.memory_controls is None and (
            bool(os.environ.get('CHIYO_MEMORY_CONTROL_DB')) or Path(control_path).exists())
        
        # M37 resolver setup
        os.environ.setdefault('CHIYO_NATIVE_MEMORY_CONTEXT_ENABLED', 'false')
        os.environ.setdefault('CHIYO_NATIVE_MEMORY_CONTEXT_MODE', 'OFF')
        os.environ.setdefault('CHIYO_NATIVE_MEMORY_CONTEXT_CONFIG', './config/native/memory-context-wiring.json')
        os.environ.setdefault('CHIYO_NATIVE_MEMORY_RUNTIME_ROOT', str(INTEG_ROOT))
        os.environ.setdefault('CHIYO_NATIVE_MEMORY_OWNER_USER_IDS', self.m37_binding.get('chat_id', ''))
        
        self.m37_resolver = None
        try:
            from memory_runtime_v1.production_resolver import ProductionNativeMemoryResolver
            self.m37_resolver = ProductionNativeMemoryResolver()
            LOGGER.info("M37 production resolver initialized")
        except Exception as exc:
            LOGGER.warning("M37 resolver init skipped: %s", exc)

        os.environ["CHIYO_ORIG_KEY"] = model_key
        self.model_cfg = SimpleNamespace(
            model=os.environ.get("CHIYO_MODEL", "configure-model"),
            provider="gateway",
            base_url=os.environ.get("CHIYO_MODEL_BASE_URL", "https://api.example.invalid/v1"),
            endpoint=os.environ.get("CHIYO_MODEL_ENDPOINT", "https://api.example.invalid/v1/chat/completions"),
            api_path="/chat/completions",
            api_key_env="CHIYO_ORIG_KEY",
            timeout_seconds=60,
            sampling={"temperature": 1.0, "top_p": 0.9, "stream": False}
        )
        self.provider = DirectProvider(self.model_cfg)

    def m37_owner_user_id(self) -> str:
        """The user id the M37 resolver itself is bound to.

        Read from the resolver's own binding so this runtime can never request
        memory under an identity the resolver was not configured for.
        """
        identity = getattr(self.m37_resolver, "_identity", None) or {}
        return str(identity.get("transport_user_id") or "")

    def handle_turn(self, conversation_id: str, user_text: str, message_id: str | None = None, participant: Any = None) -> dict[str, Any]:
        turn_id = str(uuid.uuid4())
        trace_id = str(uuid.uuid4())
        received_at = utc_now()
        if message_id is None:
            message_id = turn_id

        # 0. Record the inbound turn in the canonical M37 M0 store *before* the
        #    reply is generated: the resolver anchors recall on the newest
        #    persisted user turn, so a post-delivery write would anchor on the
        #    previous turn.  The M0 unique index makes the delivery cycle's later
        #    offer of the same source ref a no-op.
        #
        #    The conversation id is the M37 binding's, not the adapter's: the
        #    resolver looks the anchor up by the id it derives from the native
        #    binding, and the delivery cycle offers that same id.  The adapter
        #    already composes message_id as "telegram:<chat>:<message id>", which
        #    IS the source ref, so it must not be re-prefixed.
        inbound_evidence_id = None
        if self.m37_bridge is not None and self.m37_binding:
            try:
                source_ref = str(message_id)
                if not source_ref.startswith("telegram:"):
                    source_ref = "telegram:%s:%s" % (self.m37_binding["chat_id"], source_ref)
                receipt = self.m37_bridge.write_user_event(
                    conversation_id=self.m37_binding["conversation_id"],
                    user_text=user_text,
                    source_ref=source_ref,
                    turn_id=turn_id,
                    occurred_at=received_at,
                )
                if receipt.get('status') in ('inserted', 'duplicate'):
                    inbound_evidence_id = receipt.get('event_id')
            except Exception as exc:
                # Memory must never break the conversation.
                LOGGER.warning("m0.user_event.failed error_class=%s", type(exc).__name__)

        if self.memory_controls is not None and user_text.startswith('/memory '):
            try:
                answer = self.memory_controls.command(user_text, source_ref)
                status = 'memory_control'
            except Exception as exc:
                LOGGER.warning('memory.control.failed error_class=%s', type(exc).__name__)
                answer = '这次记忆修改没有完成，请检查记忆ID后重试；我没有把失败当作成功。'
                status = 'memory_control_failed'
            self.traces.write({'trace_id': trace_id, 'turn_id': turn_id,
                'conversation_id': conversation_id, 'timestamp': utc_now(), 'status': status,
                'm37_present': False, 'legacy_memory_present': False})
            return {'turn_id': turn_id, 'trace_id': trace_id, 'conversation_id': conversation_id,
                'input_message_id': message_id, 'outbound_message_id': turn_id,
                'raw_content': answer, 'model': self.model_cfg.model}

        # 1. Base system prompt
        sys_prompt = self.persona_provider.load_system_prompt()
        world_text = ''
        if getattr(self, 'world_body', None) is not None:
            from integration.world_body_client import render_grounded_context
            try:
                world_text = render_grounded_context(self.world_body)
            except Exception as exc:
                LOGGER.warning('world.read.failed error_class=%s', type(exc).__name__)
            if world_text:
                sys_prompt += '\n\n【当前身体与环境观察】\n' + world_text

        projection = self.memory_controls.projection() if self.memory_controls is not None else (
            MemoryProjection(healthy=False) if getattr(self, 'memory_control_unavailable', False) else None)
        
        visible_history = self.store.load(conversation_id)
        if projection is not None:visible_history = projection.history(visible_history)
        visible_turn_ids={str(r['turn_id']) for r in visible_history if r.get('turn_id')}

        # 2. M37 recall injection -- owner-scoped, fail-closed.
        #    The resolver refuses an identity it cannot bind, so the four ids below are
        #    required: calling it without them always returns () (measured no-op).
        m37_text = ""
        m37_ids = {
            "user_id": str(self.m37_owner_user_id() or ""),
            "session_id": conversation_id,
            "conversation_id": conversation_id,
            "turn_id": turn_id,
        }
        if self.m37_resolver is not None and m37_ids["user_id"] and inbound_evidence_id:
            try:
                recalled = self.m37_resolver(
                    ids=m37_ids, current_user_text=user_text, current_user_event_id=inbound_evidence_id,
                    visible_turn_ids=visible_turn_ids)
                corrections = []
                if projection is not None:
                    recalled, corrections = projection.recalls(recalled)
                    # A valid correction remains direct user evidence even when the
                    # old episode is no longer selected. Ordinary P0 turns stay silent.
                    from app.m3 import classify_intent
                    if getattr(self.m37_resolver,'state',None)=='READY' and classify_intent(user_text)[0] in ('P1','P2'):
                        corrections=list(dict.fromkeys(corrections+projection.active_corrections()))
                rendered = [str(getattr(obj, "text", "") or "").strip()
                            for obj in (recalled or ())]
                rendered.extend('【用户纠正】' + text for text in corrections)
                m37_text = "\n".join(text for text in rendered if text)
            except Exception as exc:
                # Memory is never allowed to break the conversation.
                LOGGER.warning("m37.recall.failed error_class=%s", type(exc).__name__)
        if m37_text:
            sys_prompt += "\n\n【M37长期记忆】\n" + m37_text

        # 3. Assemble history: native turns first, or bootstrap from Hermes
        native_history = self.store.load(conversation_id)
        if projection is not None:
            native_history = projection.history(native_history)
        allow_bootstrap = projection is None or (projection.healthy and projection.controls == 0)
        if len(native_history) < 2 and allow_bootstrap:
            hermes_turns = self.history_reader.get_recent_turns(self.m37_binding.get("chat_id", ""), limit=6)
            history_messages = [{"role": t["role"], "content": t["content"]} for t in hermes_turns]
        else:
            history_messages = []
            for t in native_history[-40:]:
                role = t.get('speaker') or t.get('role')
                text = t.get('raw_content') if 'raw_content' in t else t.get('content')
                if role in ('user', 'assistant') and isinstance(text, str) and text:
                    history_messages.append({'role': role, 'content': text})

        messages = [{"role": "system", "content": sys_prompt}]
        messages.extend(history_messages)
        messages.append({"role": "user", "content": user_text})

        # No M0 write here.  Canonical M37 evidence has exactly one writer: the
        # Native->M37 bridge (m37_m0_bridge.py), which records a turn only after
        # the Telegram send is confirmed.  Writing a second copy from the runtime
        # would mint a second authority for the same real event.

        started = time.monotonic()
        result = self.provider.complete(messages)
        latency = round((time.monotonic() - started) * 1000, 3)

        # Telemetry trace
        trace = {
            "trace_id": trace_id,
            "turn_id": turn_id,
            "conversation_id": conversation_id,
            "timestamp": utc_now(),
            "model": self.model_cfg.model,
            "latency_ms": latency,
            "status": "success",
            "messages_count": len(messages),
            "m37_present": bool(m37_text),
            "world_body_present": bool(world_text),
            "legacy_memory_present": bool(self.persona_provider.include_legacy),
            "history_messages": len(history_messages),
            "memory_controls_healthy": projection.healthy if projection else None,
            "memory_controls_count": projection.controls if projection else 0,
        }
        self.traces.write(trace)

        return {
            "turn_id": turn_id,
            "trace_id": trace_id,
            "conversation_id": conversation_id,
            "input_message_id": message_id,
            "outbound_message_id": turn_id,
            "raw_content": result.content,
            "model": self.model_cfg.model,
        }

    def commit_turn(self, result: dict[str, Any], user_text: str) -> None:
        """Persist the confirmed turn into the conversation store, exactly once.

        The transport ledger can re-finalize a row after a crash, so this is
        guarded by turn_id: a turn already in the store is never appended twice.
        """
        conversation_id = str(result["conversation_id"])
        turn_id = str(result["turn_id"])
        existing = self.store.load(conversation_id)
        if any(str(record.get("turn_id")) == turn_id for record in existing):
            LOGGER.info("turn.commit.skipped_duplicate turn_id=%s", turn_id)
            return
        self.store.persist_turn(
            conversation_id,
            turn_id,
            utc_now(),
            user_text,
            str(result["raw_content"]),
            self.model_cfg.model,
        )

    def record_delivery_success(self, result: dict[str, Any]) -> str | None:
        """Confirm the delivery to the transport ledger.

        The canonical M0 evidence for this turn is written by the Native->M37
        bridge, from the DELIVERED row this returns -- one authority, one writer.
        """
        if self.m37_bridge is None or not self.m37_binding:
            return 'failed'
        try:
            receipt = self.m37_bridge.write_assistant_event(
                conversation_id=self.m37_binding['conversation_id'],
                content=str(result['raw_content']), source_ref=str(result['outbound_message_id']),
                turn_id=str(result['turn_id']), occurred_at=utc_now())
            return receipt.get('status') if receipt.get('status') in ('inserted', 'duplicate') else 'failed'
        except Exception as exc:
            LOGGER.warning('m0.delivery.failed error_class=%s', type(exc).__name__)
            return 'failed'


def main() -> None:
    parser = argparse.ArgumentParser(description="Chiyo Original Instance Native Telegram Runner")
    parser.add_argument("--data-dir", default="./data/native/data")
    parser.add_argument("--trace-dir", default="./data/native/traces")
    parser.add_argument("--state-db", default="./data/native/telegram/state.sqlite")
    parser.add_argument("--allowed-user-id", required=True)
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    data_dir = Path(args.data_dir)
    trace_dir = Path(args.trace_dir)
    state_path = Path(args.state_db)
    data_dir.mkdir(parents=True, exist_ok=True)
    trace_dir.mkdir(parents=True, exist_ok=True)
    state_path.parent.mkdir(parents=True, exist_ok=True)

    # 1. Read token
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if not token:
        env_file = Path("./data/persona/.env")
        if env_file.exists():
            for line in env_file.read_text().splitlines():
                if line.startswith("TELEGRAM_BOT_TOKEN="):
                    token = line.split("=", 1)[1].strip().strip('"').strip("'")
                    break
    if not token:
        raise SystemExit("TELEGRAM_BOT_TOKEN is required")
    _TOKEN_FOR_LOGGING["value"] = token

    # 2. Read model API key
    key = os.environ.get("CHIYO_MODEL_API_KEY", "")
    if not key:
        alpha_env = Path("./config/native/alpha.env")
        if alpha_env.exists():
            for line in alpha_env.read_text().splitlines():
                if line.startswith("CHIYO_MODEL_API_KEY="):
                    key = line.split("=", 1)[1].strip().strip('"').strip("'")
                    break
    if not key:
        raise SystemExit("CHIYO_MODEL_API_KEY is required")

    if not os.environ.get("CHIYO_MODEL_ENDPOINT") or not os.environ.get("CHIYO_MODEL"):
        raise SystemExit("set CHIYO_MODEL and CHIYO_MODEL_ENDPOINT before starting chat")
    runtime = OriginalNativeRuntime(data_dir, trace_dir, key)
    runtime, native_life = wire_runtime(runtime)
    from native_runtime_identity import write_identity
    write_identity(runtime, native_life, data_dir)
    api = TelegramApi(token, timeout_seconds=40)
    state = TelegramState(state_path)
    adapter = TelegramAdapter(
        runtime=runtime,
        api=api,
        state=state,
        allowed_user_id=args.allowed_user_id,
        namespace="chiyo-original-v0",
        participant_resolver=None,
    )

    stop_event = threading.Event()
    def stop(*_args):
        LOGGER.info("Shutdown requested")
        stop_event.set()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    LOGGER.info("Starting Chiyo Original Native Runner for configured Telegram bot (allowed_user_id=%s)...", args.allowed_user_id)
    try:
        adapter.run_forever(stop_event)
    finally:
        if native_life is not None:
            native_life.close()


def wire_runtime(runtime: Any, *, environment: Any = None) -> tuple[Any, Any]:
    env = environment if environment is not None else os.environ
    if str(env.get('CHIYO_NATIVE_LIFE_ENABLED', 'false')).lower() != 'true':
        return runtime, None
    from native_life import NativeLifeRuntime
    wrapper = NativeLifeRuntime(runtime, environment=env)
    return wrapper, wrapper


if __name__ == "__main__":
    main()
