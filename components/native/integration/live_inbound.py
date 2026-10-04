"""Typed inbound event + observation ports for the live chat path.

This module implements the contract required by CHIYO-PROD-03 section 14/15:

    real inbound turn -> typed event (event_id, event_type, source, subject_ref,
    conversation_ref, occurred_at, correlation_id) -> observation port

The identity is *derived from the identity the live adapter already uses*, so the
adapter's own dedup ledger and this event stream agree by construction:

* conversation: ``telegram_adapter.conversation_id_for(namespace, chat_id)``
  produces ``tg-<sha256(namespace:chat_id)[:24]>``
* message: the live adapter mints ``telegram:<chat_id>:<message_id>``

Raw chat identifiers are never copied into the event; subject and conversation
references are hashed, and message text is never carried (section 15 and the
LPC0A no-private-text rule).
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Optional, Protocol, runtime_checkable

SCHEMA_EVENT = "chiyo.integration.live_inbound_event.v1"

SOURCE_TELEGRAM = "telegram"
EVENT_TYPE_ORDINARY_COMMUNICATION = "ORDINARY_COMMUNICATION"

#: Outcome of the live adapter's own delivery ledger, mirrored verbatim.
OUTCOME_ACCEPTED = "ACCEPTED"
OUTCOME_DELIVERED = "DELIVERED"
OUTCOME_DELIVERY_UNKNOWN = "DELIVERY_UNKNOWN"
OUTCOME_FAILED = "FAILED"


def _sha(text: str, length: int = 24) -> str:
    return hashlib.sha256(str(text).encode("utf-8")).hexdigest()[:length]


def mint_event_id(*, source: str, chat_id: str, message_id: str) -> str:
    """Deterministic event identity.

    Deterministic means replaying the same inbound message can never mint a
    second event id, which is what makes the port-side dedup exact rather than
    heuristic (section 15).
    """
    return "linb:" + _sha(f"{source}|{chat_id}|{message_id}")


def subject_ref_for(source: str, transport_user_id: str) -> str:
    """Hashed subject reference -- never the raw transport id."""
    return f"{source}:subject:{_sha(transport_user_id, 16)}"


def correlation_ref_for(native_message_id: str) -> str:
    """Hashed correlation reference for a native message id.

    The raw ``telegram:<chat_id>:<message_id>`` form is never carried into the
    event; only its digest is, so the event stays correlatable without leaking a
    transport identifier.
    """
    return "lcorr:" + _sha(native_message_id)


def parse_native_message_id(native_message_id: str) -> Optional[tuple[str, str]]:
    """Split the live adapter's ``telegram:<chat_id>:<message_id>`` form.

    Returns ``None`` when the value does not have the live shape, so a caller can
    fail closed instead of inventing identity.
    """
    parts = str(native_message_id or "").split(":", 2)
    if len(parts) != 3 or parts[0] != SOURCE_TELEGRAM or not parts[1] or not parts[2]:
        return None
    return parts[1], parts[2]


@dataclass(frozen=True)
class LifeObservationEvent:
    """The typed event the core runtime consumes for one real inbound turn."""

    event_id: str
    event_type: str
    source: str
    subject_ref: str
    conversation_ref: str
    occurred_at: str
    correlation_id: Optional[str] = None
    outcome: str = OUTCOME_ACCEPTED
    schema: str = SCHEMA_EVENT

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "event_id": self.event_id,
            "event_type": self.event_type,
            "source": self.source,
            "subject_ref": self.subject_ref,
            "conversation_ref": self.conversation_ref,
            "occurred_at": self.occurred_at,
            "correlation_id": self.correlation_id,
            "outcome": self.outcome,
        }

    def validate(self) -> None:
        missing = [
            name
            for name, value in (
                ("event_id", self.event_id),
                ("event_type", self.event_type),
                ("source", self.source),
                ("subject_ref", self.subject_ref),
                ("conversation_ref", self.conversation_ref),
                ("occurred_at", self.occurred_at),
            )
            if not value
        ]
        if missing:
            raise ValueError(f"incomplete life observation event: missing {missing}")


@runtime_checkable
class LifeObservationPort(Protocol):
    """Where a typed inbound event is delivered."""

    def observe(self, event: LifeObservationEvent) -> Mapping[str, Any]:
        ...


class NullObservationPort:
    """Records nothing; used when observation is switched off."""

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def observe(self, event: LifeObservationEvent) -> Mapping[str, Any]:
        event.validate()
        self.events.append(event.as_dict())
        return {"accepted": True, "duplicate": False, "port": "null"}


class JsonlObservationPort:
    """Append-only, deduplicated observation sink.

    Dedup is by ``event_id`` and is seeded from the existing file, so a process
    restart cannot re-admit an already observed inbound message. A write failure
    is reported, never raised into the chat turn.
    """

    def __init__(self, path: Path | str, *, max_scan_lines: int = 200_000) -> None:
        self.path = Path(path)
        self._lock = threading.RLock()
        self._seen: set[str] = set()
        self._load(max_scan_lines)

    def _load(self, max_scan_lines: int) -> None:
        try:
            if not self.path.is_file():
                return
            with self.path.open("r", encoding="utf-8") as handle:
                for index, line in enumerate(handle):
                    if index >= max_scan_lines:
                        break
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        self._seen.add(str(json.loads(line)["event_id"]))
                    except (ValueError, KeyError, TypeError):
                        continue
        except OSError:
            return

    @property
    def seen_count(self) -> int:
        with self._lock:
            return len(self._seen)

    def observe(self, event: LifeObservationEvent) -> Mapping[str, Any]:
        event.validate()
        with self._lock:
            if event.event_id in self._seen:
                return {"accepted": True, "duplicate": True, "port": "jsonl"}
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open("a", encoding="utf-8") as handle:
                    handle.write(
                        json.dumps(event.as_dict(), ensure_ascii=False, separators=(",", ":")) + "\n"
                    )
            except OSError as exc:
                return {"accepted": False, "duplicate": False, "port": "jsonl",
                        "error": f"{type(exc).__name__}: {exc}"}
            self._seen.add(event.event_id)
            return {"accepted": True, "duplicate": False, "port": "jsonl"}


@dataclass
class ObservationMetrics:
    """Small, log-safe counters. No chat content is ever recorded."""

    submitted: int = 0
    delivered: int = 0
    duplicates: int = 0
    dropped_queue_full: int = 0
    port_errors: int = 0
    last_error: Optional[str] = None
    last_outcome: Optional[str] = None
    _lock: threading.RLock = field(default_factory=threading.RLock, repr=False)

    def as_dict(self) -> dict[str, Any]:
        with self._lock:
            return {
                "submitted": self.submitted,
                "delivered": self.delivered,
                "duplicates": self.duplicates,
                "dropped_queue_full": self.dropped_queue_full,
                "port_errors": self.port_errors,
                "last_error": self.last_error,
                "last_outcome": self.last_outcome,
            }


class ObservationDispatcher:
    """Bounded, non-blocking hand-off from the chat thread to the observation port.

    The chat thread must never wait for a domain store (CHIYO-PROD-03 section 16).
    ``submit`` therefore only enqueues; a single background thread drains the queue.
    When the queue is full the newest item is dropped and counted -- the chat turn
    is never blocked and the drop is visible in metrics rather than silent.
    """

    def __init__(
        self,
        port: LifeObservationPort,
        *,
        queue_size: int = 256,
        name: str = "chiyo-observation",
    ) -> None:
        self.port = port
        self.queue_size = max(1, int(queue_size))
        self.metrics = ObservationMetrics()
        self._queue: list[LifeObservationEvent] = []
        self._lock = threading.RLock()
        self._cv = threading.Condition(self._lock)
        self._stop = False
        #: True while the worker is inside port.observe() for an item it already popped.
        self._busy = False
        self._thread: Optional[threading.Thread] = None
        self._name = name

    # -- chat-thread side -------------------------------------------------
    def submit(self, event: LifeObservationEvent) -> None:
        """Never blocks, never raises."""
        try:
            event.validate()
        except ValueError as exc:
            with self.metrics._lock:
                self.metrics.port_errors += 1
                self.metrics.last_error = f"INVALID_EVENT: {exc}"
            return
        with self._cv:
            with self.metrics._lock:
                self.metrics.submitted += 1
            if self._stop:
                return
            if len(self._queue) >= self.queue_size:
                self._queue.pop(0)
                with self.metrics._lock:
                    self.metrics.dropped_queue_full += 1
            self._queue.append(event)
            self._ensure_thread()
            self._cv.notify()

    def _ensure_thread(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._drain, name=self._name, daemon=True)
        self._thread.start()

    # -- worker side ------------------------------------------------------
    def _drain(self) -> None:
        while True:
            with self._cv:
                while not self._queue and not self._stop:
                    self._cv.wait(timeout=0.05)
                if self._queue:
                    event = self._queue.pop(0)
                    self._busy = True
                elif self._stop:
                    return
                else:
                    continue
            try:
                try:
                    result = dict(self.port.observe(event))
                except Exception as exc:  # noqa: BLE001 - observation must never break a turn
                    with self.metrics._lock:
                        self.metrics.port_errors += 1
                        self.metrics.last_error = f"{type(exc).__name__}: {exc}"
                    continue
                with self.metrics._lock:
                    if result.get("duplicate"):
                        self.metrics.duplicates += 1
                    elif result.get("accepted"):
                        self.metrics.delivered += 1
                    else:
                        self.metrics.port_errors += 1
                        self.metrics.last_error = str(result.get("error") or "NOT_ACCEPTED")
                    self.metrics.last_outcome = "duplicate" if result.get("duplicate") else (
                        "accepted" if result.get("accepted") else "error"
                    )
            finally:
                # Cleared even when the port raised: an item is only "done" once the
                # worker has left it, which is what flush() must wait for.
                with self._lock:
                    self._busy = False

    def flush(self, timeout_s: float = 5.0) -> bool:
        """Wait until the queue is empty. Returns False on timeout."""
        deadline = time.monotonic() + float(timeout_s)
        while time.monotonic() < deadline:
            with self._lock:
                if not self._queue and not self._busy:
                    return True
            time.sleep(0.005)
        with self._lock:
            return not self._queue and not self._busy

    def close(self, timeout_s: float = 5.0) -> None:
        with self._cv:
            self._stop = True
            self._cv.notify_all()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=float(timeout_s))


def iso_now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def resolve_observation_path(environment: Optional[Mapping[str, str]] = None) -> Optional[Path]:
    """Observation sink path, or ``None`` when observation is switched off."""
    env = environment if environment is not None else os.environ
    raw = str(env.get("CHIYO_LIVE_OBSERVATION_PATH") or "").strip()
    if not raw:
        return None
    return Path(raw)
