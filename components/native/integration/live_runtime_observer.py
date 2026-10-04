"""Live-runtime observer: the real inbound turn -> typed Life event seam.

CHIYO-PROD-03 section 13/14/16/17.

Why this seam
-------------
The live passive-chat application builds its Telegram adapter as::

    TelegramAdapter(runtime=<runtime object>, api=..., state=...)

and the adapter calls exactly three methods on that object
(``telegram_adapter.py:482-493`` and ``:548-549``):

* ``runtime.handle_turn(conversation_id, user_text, message_id=native_message_id[, participant=...])``
* ``runtime.commit_turn(result, user_text)``
* ``runtime.record_delivery_success(result)``

The runtime object is a documented injection point (the live runner passes its own
``OriginalNativeRuntime``; the shipped tests pass a ``FakeRuntime``). Wrapping it is
therefore the one integration seam that requires **no change to the live application
and no change to its behaviour**: every call is forwarded verbatim, and the wrapper
additionally emits one typed observation event per accepted inbound turn.

Guarantees
----------
* Behaviour-preserving: arguments and return values pass through untouched.
* Fail-open: any failure inside observation is swallowed and counted; the chat turn
  never breaks because of the integration (section 16).
* Non-blocking: the event is handed to a bounded queue drained by a background
  thread, so the chat thread never waits on a domain store.
* Identity-exact: the event id is minted from the same chat/message identity the
  live adapter already uses for its own dedup ledger, so a redelivered Telegram
  message cannot create a second event (section 15).
"""
from __future__ import annotations

from typing import Any, Optional

from .live_inbound import (
    EVENT_TYPE_ORDINARY_COMMUNICATION,
    OUTCOME_ACCEPTED,
    SOURCE_TELEGRAM,
    LifeObservationEvent,
    ObservationDispatcher,
    correlation_ref_for,
    iso_now,
    mint_event_id,
    parse_native_message_id,
    subject_ref_for,
)

__all__ = ["LifeObservingRuntime", "build_observing_runtime"]


def _participant_key(participant: Any) -> Optional[str]:
    """Best-effort stable subject key from the live participant object."""
    if participant is None:
        return None
    for attribute in ("participant_id", "id", "user_id", "subject_id"):
        value = getattr(participant, attribute, None)
        if isinstance(value, str) and value:
            return value
    if isinstance(participant, dict):
        for key in ("participant_id", "id", "user_id", "subject_id"):
            value = participant.get(key)
            if isinstance(value, str) and value:
                return value
    return None


def _correlation_id(result: Any, native_message_id: str) -> str:
    """Prefer the real turn identity; otherwise a digest of the native message id.

    A raw ``telegram:<chat_id>:<message_id>`` value is never emitted.
    """
    if isinstance(result, dict):
        for key in ("turn_id", "trace_id"):
            value = result.get(key)
            if isinstance(value, str) and value:
                return value
    return correlation_ref_for(native_message_id)


class LifeObservingRuntime:
    """Delegating wrapper that emits one typed Life event per accepted inbound turn."""

    def __init__(
        self,
        wrapped: Any,
        dispatcher: ObservationDispatcher,
        *,
        source: str = SOURCE_TELEGRAM,
        event_type: str = EVENT_TYPE_ORDINARY_COMMUNICATION,
    ) -> None:
        object.__setattr__(self, "_wrapped", wrapped)
        object.__setattr__(self, "_dispatcher", dispatcher)
        object.__setattr__(self, "_source", source)
        object.__setattr__(self, "_event_type", event_type)
        object.__setattr__(self, "_observe_errors", 0)
        object.__setattr__(self, "_observed", 0)
        object.__setattr__(self, "_skipped_identity", 0)

    # ---- diagnostics -----------------------------------------------------
    @property
    def observation_status(self) -> dict[str, Any]:
        return {
            "observed_events": self._observed,
            "skipped_for_missing_identity": self._skipped_identity,
            "observe_errors": self._observe_errors,
            "dispatcher": self._dispatcher.metrics.as_dict(),
        }

    # ---- the three methods the live adapter calls ------------------------
    def handle_turn(self, *args: Any, **kwargs: Any) -> Any:
        result = self._wrapped.handle_turn(*args, **kwargs)
        self._emit(args, kwargs, result)
        return result

    def commit_turn(self, *args: Any, **kwargs: Any) -> Any:
        return self._wrapped.commit_turn(*args, **kwargs)

    def record_delivery_success(self, *args: Any, **kwargs: Any) -> Any:
        return self._wrapped.record_delivery_success(*args, **kwargs)

    # ---- everything else is the wrapped runtime --------------------------
    def __getattr__(self, name: str) -> Any:
        return getattr(self._wrapped, name)

    # ---- observation -----------------------------------------------------
    def _emit(self, args: tuple[Any, ...], kwargs: dict[str, Any], result: Any = None) -> None:
        try:
            conversation_ref = kwargs.get("conversation_id") or (args[0] if args else None)
            native_message_id = kwargs.get("message_id") or (
                args[2] if len(args) > 2 else None
            )
            identity = parse_native_message_id(str(native_message_id or ""))
            if identity is None or not conversation_ref:
                self._skipped_identity += 1
                return
            chat_id, message_id = identity
            participant = kwargs.get("participant")
            subject_key = _participant_key(participant) or chat_id
            event = LifeObservationEvent(
                event_id=mint_event_id(
                    source=self._source, chat_id=chat_id, message_id=message_id
                ),
                event_type=self._event_type,
                source=self._source,
                subject_ref=subject_ref_for(self._source, subject_key),
                conversation_ref=str(conversation_ref),
                occurred_at=iso_now(),
                correlation_id=_correlation_id(result, str(native_message_id)),
                outcome=OUTCOME_ACCEPTED,
            )
            self._dispatcher.submit(event)
            self._observed += 1
        except Exception:  # noqa: BLE001 - a chat turn must never break because of us
            self._observe_errors += 1


def build_observing_runtime(
    runtime: Any,
    *,
    observation_path: Any = None,
    queue_size: int = 256,
) -> tuple[Any, Optional[ObservationDispatcher]]:
    """Wrap ``runtime`` for observation, or return it unchanged when switched off.

    ``observation_path`` is the sink file. When it is ``None`` the runtime is
    returned untouched, so the integration is off by default and enabling it is an
    explicit operator decision.
    """
    if observation_path is None:
        return runtime, None
    from .live_inbound import JsonlObservationPort

    port = JsonlObservationPort(observation_path)
    dispatcher = ObservationDispatcher(port, queue_size=queue_size)
    return LifeObservingRuntime(runtime, dispatcher), dispatcher
