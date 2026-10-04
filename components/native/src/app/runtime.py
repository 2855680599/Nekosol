from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
import time
import uuid

from .context import assemble_context
from .evidence import (
    CHIYO_ORIGIN,
    CHIYO_ROLE,
    DELIVERED,
    EvidenceEvent,
    EvidenceWriter,
    RECEIVED,
    USER_ORIGIN,
    USER_ROLE,
    new_event_id,
    utc_now,
)
from .epoch import ContextEpochStore
from .participant import Participant
from .session import ConversationStore, PersistenceError
from .trace import TraceStore


class NativeRuntime:
    def __init__(
        self,
        config,
        store: ConversationStore,
        backend,
        traces: TraceStore,
        evidence: EvidenceWriter | None = None,
        context_epoch: ContextEpochStore | None = None,
    ):
        self.config = config
        self.store = store
        self.backend = backend
        self.traces = traces
        self.evidence = evidence
        self.context_epoch = context_epoch

    def _base_trace(self, trace_id, turn_id, conversation_id, assembly):
        return {
            "trace_id": trace_id,
            "turn_id": turn_id,
            "conversation_id": conversation_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "model": self.config.model,
            "provider": self.config.provider,
            "endpoint": self.config.endpoint,
            "sampling": dict(self.config.sampling),
            "context_sources": assembly.sources,
            "messages": assembly.messages,
            "input_token_count": None,
            "output_token_count": None,
            "latency_ms": None,
            "raw_output": None,
            "finish_reason": None,
            "reasoning_content_present": False,
            "status": "started",
            "error": None,
        }

    def _append_user_evidence(
        self,
        conversation_id: str,
        turn_id: str,
        message_id: str,
        user_text: str,
        occurred_at: str,
    ) -> None:
        if self.evidence is None:
            return
        self.evidence.append(
            EvidenceEvent(
                event_id=new_event_id(),
                occurred_at=occurred_at,
                memory_owner="chiyo",
                source_origin=USER_ORIGIN,
                delivery_status=RECEIVED,
                epistemic_role=USER_ROLE,
                speaker="user",
                content=user_text,
                conversation_id=conversation_id,
                turn_id=turn_id,
                source_refs=[
                    {"kind": "native_http_input", "id": message_id},
                    {"kind": "native_turn", "id": turn_id},
                ],
                created_at=utc_now(),
            )
        )

    def handle_turn(
        self,
        conversation_id: str,
        user_text: str,
        message_id: str | None = None,
        participant: Participant | None = None,
    ) -> dict[str, Any]:
        turn_id = str(uuid.uuid4())
        trace_id = str(uuid.uuid4())
        received_at = utc_now()
        if message_id is None:
            # Compatibility for the original V0 caller. A retry-safe caller
            # should supply the same message_id/Idempotency-Key on retries.
            message_id = turn_id
        if not isinstance(message_id, str) or not message_id.strip():
            raise ValueError("message_id must be a non-empty string")
        raw_history = self.store.load(conversation_id)
        if self.context_epoch is None:
            history = raw_history
            epoch_trace = {
                "epoch_id": None,
                "boundary_id": None,
                "raw_history_count": len(raw_history),
                "visible_history_count": len(history),
                "excluded_history_count": 0,
                "status": "epoch_filter_disabled",
            }
        else:
            selection = self.context_epoch.select(conversation_id, raw_history)
            history = selection.visible_history
            epoch_trace = selection.trace_context()
        assembly = assemble_context(
            self.config.identity_text,
            history,
            user_text,
            participant,
        )
        trace = self._base_trace(trace_id, turn_id, conversation_id, assembly)
        trace["context_epoch"] = epoch_trace
        if participant is not None:
            trace["participant"] = participant.trace_context()
        trace["input_message_id"] = message_id
        trace["received_at"] = received_at
        self._append_user_evidence(
            conversation_id,
            turn_id,
            message_id,
            user_text,
            received_at,
        )
        started = time.monotonic()
        try:
            result = self.backend.complete(assembly.messages)
        except Exception as exc:
            trace["status"] = "provider_error"
            trace["error"] = type(exc).__name__ + ": " + str(exc)
            trace["latency_ms"] = round((time.monotonic() - started) * 1000, 3)
            self.traces.write(trace)
            raise

        trace["input_token_count"] = result.usage.get("prompt_tokens")
        trace["output_token_count"] = result.usage.get("completion_tokens")
        trace["latency_ms"] = round((time.monotonic() - started) * 1000, 3)
        trace["raw_output"] = result.content
        trace["finish_reason"] = result.finish_reason
        trace["reasoning_content_present"] = result.reasoning_content_present

        timestamp = datetime.now(timezone.utc).isoformat()
        try:
            self.store.persist_turn(
                conversation_id,
                turn_id,
                timestamp,
                user_text,
                result.content,
                self.config.model,
            )
        except PersistenceError as exc:
            trace["status"] = "persistence_error"
            trace["error"] = type(exc).__name__ + ": " + str(exc)
            self.traces.write(trace)
            raise

        trace["status"] = "success"
        self.traces.write(trace)
        return {
            "turn_id": turn_id,
            "trace_id": trace_id,
            "conversation_id": conversation_id,
            "input_message_id": message_id,
            "outbound_message_id": turn_id,
            "raw_content": result.content,
            "model": self.config.model,
        }

    def record_delivery_success(self, result: dict[str, Any]) -> str | None:
        """Record only the output whose HTTP response write completed."""
        if self.evidence is None:
            return None
        return self.evidence.append(
            EvidenceEvent(
                event_id=new_event_id(),
                occurred_at=utc_now(),
                memory_owner="chiyo",
                source_origin=CHIYO_ORIGIN,
                delivery_status=DELIVERED,
                epistemic_role=CHIYO_ROLE,
                speaker="chiyo",
                content=str(result["raw_content"]),
                conversation_id=str(result["conversation_id"]),
                turn_id=str(result["turn_id"]),
                source_refs=[
                    {"kind": "native_http_output", "id": str(result["outbound_message_id"])},
                    {"kind": "native_trace", "id": str(result["trace_id"])},
                ],
                created_at=utc_now(),
            )
        )
