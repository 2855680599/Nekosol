from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import argparse
import hashlib
import json
import logging
import os
from pathlib import Path
import signal
import sqlite3
import threading
import time
from typing import Any
import urllib.error
import urllib.parse
import urllib.request

from app.config import NativeConfig
from app.participant import ParticipantResolver
from integration.delivery_evidence import disposition_of as evidence_disposition
from integration.delivery_evidence import is_durable as evidence_is_durable


LOGGER = logging.getLogger("chiyo.telegram")

#: filled in at start-up purely so log messages never carry the token
_TOKEN_FOR_LOGGING: dict[str, str] = {"value": ""}
MAX_TELEGRAM_TEXT = 4096


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def chat_fingerprint(chat_id: str) -> str:
    return hashlib.sha256(chat_id.encode("utf-8")).hexdigest()[:12]


def conversation_id_for(namespace: str, chat_id: str) -> str:
    digest = hashlib.sha256(
        (namespace + ":" + chat_id).encode("utf-8")
    ).hexdigest()[:24]
    return "tg-" + digest


def _scrub(message: str) -> str:
    """Remove the bot token from anything that is about to be logged."""
    token = _TOKEN_FOR_LOGGING.get("value") or ""
    if token and token in message:
        return message.replace(token, "<telegram-token>")
    return message


class TelegramAdapterError(RuntimeError):
    pass


class TelegramConfigurationError(TelegramAdapterError):
    pass


class TelegramApiError(TelegramAdapterError):
    pass


class TelegramTransportError(TelegramAdapterError):
    pass


class TelegramApi:
    """Small long-polling client. It never logs the bot URL or token."""

    def __init__(self, token: str, timeout_seconds: float = 40.0):
        if not token or any(ch.isspace() for ch in token):
            raise TelegramConfigurationError("telegram token is missing or malformed")
        self._url = "https://api.telegram.org/bot" + token
        self.timeout_seconds = float(timeout_seconds)

    def call(self, method: str, payload: dict[str, Any] | None = None) -> Any:
        body = json.dumps(payload or {}, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            self._url + "/" + method,
            data=body,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "ChiyoNativeTelegram/0.1",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            # Do not copy response bodies into logs: Telegram error bodies are
            # not needed for the adapter's state machine.
            raise TelegramApiError("telegram HTTP error " + str(exc.code)) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise TelegramTransportError("telegram transport failure") from exc
        try:
            decoded = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise TelegramApiError("telegram returned invalid JSON") from exc
        if not isinstance(decoded, dict) or not decoded.get("ok"):
            raise TelegramApiError("telegram API rejected " + method)
        return decoded.get("result")

    def validate(self) -> None:
        result = self.call("getMe")
        if not isinstance(result, dict) or not result.get("id"):
            raise TelegramApiError("telegram getMe returned no bot identity")
        webhook = self.call("getWebhookInfo")
        if isinstance(webhook, dict) and webhook.get("url"):
            raise TelegramConfigurationError(
                "telegram webhook is configured; long polling refused"
            )

    def get_updates(self, offset: int | None, timeout_seconds: int) -> list[dict[str, Any]]:
        payload: dict[str, Any] = {
            "timeout": int(timeout_seconds),
            "allowed_updates": ["message"],
        }
        if offset is not None:
            payload["offset"] = int(offset)
        result = self.call("getUpdates", payload)
        if not isinstance(result, list):
            raise TelegramApiError("telegram getUpdates returned invalid result")
        return [item for item in result if isinstance(item, dict)]

    def send_message(self, chat_id: str, text: str) -> str:
        if not text or len(text) > MAX_TELEGRAM_TEXT:
            raise TelegramApiError("telegram message length is unsupported")
        result = self.call(
            "sendMessage",
            {"chat_id": chat_id, "text": text},
        )
        if not isinstance(result, dict) or not result.get("message_id"):
            raise TelegramApiError("telegram sendMessage returned no message id")
        return str(result["message_id"])


class TelegramState:
    """Durable update and delivery ledger, separate from M0 and raw history."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(self.path.parent, 0o700)
        self._initialize()
        os.chmod(self.path, 0o600)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=5000")
        return connection

    def _initialize(self) -> None:
        connection = self._connect()
        try:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS adapter_updates (
                    update_key TEXT PRIMARY KEY,
                    message_key TEXT NOT NULL UNIQUE,
                    update_id INTEGER NOT NULL,
                    chat_id TEXT NOT NULL,
                    message_id TEXT NOT NULL,
                    state TEXT NOT NULL,
                    turn_id TEXT,
                    trace_id TEXT,
                    conversation_id TEXT,
                    user_text TEXT,
                    raw_content TEXT,
                    telegram_message_id TEXT,
                    outbound_message_id TEXT,
                    error_class TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS adapter_updates_state_idx
                    ON adapter_updates(state);
                CREATE TABLE IF NOT EXISTS adapter_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                """
            )
            connection.commit()
        finally:
            connection.close()

    @staticmethod
    def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
        return dict(row) if row is not None else None

    def claim(
        self,
        update_key: str,
        message_key: str,
        update_id: int,
        chat_id: str,
        message_id: str,
    ) -> tuple[bool, dict[str, Any]]:
        now = utc_now()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM adapter_updates WHERE update_key=? OR message_key=?",
                (update_key, message_key),
            ).fetchone()
            if existing is not None:
                connection.commit()
                return False, dict(existing)
            connection.execute(
                "INSERT INTO adapter_updates ("
                "update_key, message_key, update_id, chat_id, message_id, state, "
                "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    update_key,
                    message_key,
                    int(update_id),
                    chat_id,
                    message_id,
                    "RECEIVED",
                    now,
                    now,
                ),
            )
            connection.commit()
            row = connection.execute(
                "SELECT * FROM adapter_updates WHERE update_key=?", (update_key,)
            ).fetchone()
            return True, dict(row)
        finally:
            connection.close()

    def update(self, update_key: str, state: str, **fields: Any) -> None:
        allowed = {
            "turn_id",
            "trace_id",
            "conversation_id",
            "user_text",
            "raw_content",
            "telegram_message_id",
            "outbound_message_id",
            "error_class",
        }
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError("unsupported adapter state field")
        assignments = ["state=?", "updated_at=?"]
        values: list[Any] = [state, utc_now()]
        for key, value in fields.items():
            assignments.append(key + "=?")
            values.append(value)
        values.append(update_key)
        connection = self._connect()
        try:
            connection.execute(
                "UPDATE adapter_updates SET "
                + ", ".join(assignments)
                + " WHERE update_key=?",
                values,
            )
            connection.commit()
        finally:
            connection.close()

    def get_offset(self) -> int | None:
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT value FROM adapter_meta WHERE key='update_offset'"
            ).fetchone()
            return int(row[0]) if row is not None else None
        finally:
            connection.close()

    def set_offset(self, offset: int) -> None:
        connection = self._connect()
        try:
            connection.execute(
                "INSERT INTO adapter_meta(key, value) VALUES('update_offset', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(int(offset)),),
            )
            connection.commit()
        finally:
            connection.close()

    def rows_in_state(self, state: str) -> list[dict[str, Any]]:
        connection = self._connect()
        try:
            rows = connection.execute(
                "SELECT * FROM adapter_updates WHERE state=? ORDER BY update_id",
                (state,),
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            connection.close()

    def count(self) -> int:
        connection = self._connect()
        try:
            return int(connection.execute("SELECT count(*) FROM adapter_updates").fetchone()[0])
        finally:
            connection.close()


@dataclass(frozen=True)
class AcceptedMessage:
    update_id: int
    chat_id: str
    transport_user_id: str
    message_id: str
    text: str


def parse_private_text_update(
    update: dict[str, Any], allowed_user_id: str
) -> AcceptedMessage | None:
    message = update.get("message")
    if not isinstance(message, dict):
        return None
    sender = message.get("from")
    chat = message.get("chat")
    text = message.get("text")
    if not isinstance(sender, dict) or not isinstance(chat, dict):
        return None
    if chat.get("type") != "private":
        return None
    if str(sender.get("id")) != allowed_user_id:
        return None
    if not isinstance(chat.get("id"), (str, int)):
        return None
    if not isinstance(message.get("message_id"), (str, int)):
        return None
    if not isinstance(text, str) or not text.strip():
        return None
    if not isinstance(sender.get("id"), (str, int)):
        return None
    if not isinstance(update.get("update_id"), int):
        return None
    return AcceptedMessage(
        update_id=int(update["update_id"]),
        chat_id=str(chat["id"]),
        transport_user_id=str(sender["id"]),
        message_id=str(message["message_id"]),
        text=text,
    )


class TelegramAdapter:
    TERMINAL_STATES = {"DELIVERED", "FAILED", "DELIVERY_UNKNOWN", "IGNORED"}

    def __init__(
        self,
        runtime: Any,
        api: TelegramApi,
        state: TelegramState,
        allowed_user_id: str,
        namespace: str = "native-test-v1",
        participant_resolver: ParticipantResolver | None = None,
    ):
        if not allowed_user_id or not allowed_user_id.isdigit():
            raise TelegramConfigurationError("one numeric Telegram allowlist user id is required")
        self.runtime = runtime
        self.api = api
        self.state = state
        self.allowed_user_id = allowed_user_id
        self.namespace = namespace
        self.participant_resolver = participant_resolver

    def _log_context(self, message: AcceptedMessage) -> dict[str, Any]:
        return {
            "update_id": message.update_id,
            "chat_hash": chat_fingerprint(message.chat_id),
            "message_id": message.message_id,
        }

    def _finalize_confirmed(self, row: dict[str, Any]) -> None:
        result = {
            "raw_content": row["raw_content"],
            "conversation_id": row["conversation_id"],
            "turn_id": row["turn_id"],
            "trace_id": row["trace_id"],
            "outbound_message_id": row["outbound_message_id"],
        }
        self.runtime.commit_turn(result, row["user_text"])
        evidence_status = self.runtime.record_delivery_success(result)
        disposition = evidence_disposition(evidence_status)
        if not evidence_is_durable(evidence_status):
            # Same rule as the live path: no DELIVERED claim, no resend, recoverable row.
            self.state.update(row["update_key"], "TELEGRAM_CONFIRMED",
                              error_class="evidence_" + disposition.lower())
            LOGGER.warning(
                "delivery.evidence_not_durable update_id=%s disposition=%s",
                row["update_id"],
                disposition,
            )
            return
        self.state.update(row["update_key"], "DELIVERED", error_class=None)
        LOGGER.info(
            "delivery.confirmed update_id=%s chat_hash=%s message_id=%s",
            row["update_id"],
            chat_fingerprint(row["chat_id"]),
            row["message_id"],
        )

    def recover(self) -> None:
        for row in self.state.rows_in_state("TELEGRAM_CONFIRMED"):
            try:
                self._finalize_confirmed(row)
            except Exception as exc:
                LOGGER.error(
                    "delivery.evidence_recovery_failed update_id=%s error_class=%s",
                    row["update_id"],
                    type(exc).__name__,
                )
        for state in ("PROCESSING", "NATIVE_GENERATED", "SEND_ATTEMPTED"):
            for row in self.state.rows_in_state(state):
                self.state.update(
                    row["update_key"],
                    "DELIVERY_UNKNOWN",
                    error_class="restart_uncertain",
                )
                LOGGER.warning(
                    "delivery.uncertain_after_restart update_id=%s state=%s",
                    row["update_id"],
                    state,
                )

    def process_update(self, update: dict[str, Any]) -> None:
        accepted = parse_private_text_update(update, self.allowed_user_id)
        if accepted is None:
            LOGGER.info(
                "update.ignored update_id=%s reason=not_allowed_private_text",
                update.get("update_id"),
            )
            return

        context = self._log_context(accepted)
        participant = None
        if self.participant_resolver is not None:
            participant = self.participant_resolver.resolve(
                "telegram", accepted.transport_user_id
            )
            if participant is None:
                LOGGER.info(
                    "update.ignored update_id=%s reason=participant_unresolved",
                    accepted.update_id,
                )
                return
        update_key = "update:" + str(accepted.update_id)
        message_key = "message:" + accepted.chat_id + ":" + accepted.message_id
        claimed, row = self.state.claim(
            update_key,
            message_key,
            accepted.update_id,
            accepted.chat_id,
            accepted.message_id,
        )
        if not claimed:
            if row.get("state") == "TELEGRAM_CONFIRMED":
                try:
                    self._finalize_confirmed(row)
                except Exception as exc:
                    LOGGER.error(
                        "delivery.evidence_recovery_failed update_id=%s error_class=%s",
                        accepted.update_id,
                        type(exc).__name__,
                    )
            else:
                LOGGER.info(
                    "update.duplicate state=%s update_id=%s chat_hash=%s message_id=%s",
                    row.get("state"),
                    accepted.update_id,
                    context["chat_hash"],
                    accepted.message_id,
                )
            return

        self.state.update(update_key, "PROCESSING")
        conversation_id = conversation_id_for(self.namespace, accepted.chat_id)
        native_message_id = "telegram:" + accepted.chat_id + ":" + accepted.message_id
        started = time.monotonic()
        try:
            if participant is None:
                result = self.runtime.handle_turn(
                    conversation_id,
                    accepted.text,
                    message_id=native_message_id,
                )
            else:
                result = self.runtime.handle_turn(
                    conversation_id,
                    accepted.text,
                    message_id=native_message_id,
                    participant=participant,
                )
        except Exception as exc:
            self.state.update(update_key, "FAILED", error_class=type(exc).__name__)
            LOGGER.error(
                "turn.failed update_id=%s chat_hash=%s message_id=%s error_class=%s",
                accepted.update_id,
                context["chat_hash"],
                accepted.message_id,
                type(exc).__name__,
            )
            return

        raw_content = result.get("raw_content")
        if not isinstance(raw_content, str) or not raw_content:
            self.state.update(update_key, "FAILED", error_class="EmptyReply")
            LOGGER.error("turn.failed update_id=%s error_class=EmptyReply", accepted.update_id)
            return

        self.state.update(
            update_key,
            "NATIVE_GENERATED",
            turn_id=str(result["turn_id"]),
            trace_id=str(result["trace_id"]),
            conversation_id=conversation_id,
            user_text=accepted.text,
            raw_content=raw_content,
        )
        self.state.update(update_key, "SEND_ATTEMPTED")
        try:
            telegram_message_id = self.api.send_message(accepted.chat_id, raw_content)
        except Exception as exc:
            self.state.update(
                update_key,
                "DELIVERY_UNKNOWN",
                error_class=type(exc).__name__,
            )
            LOGGER.error(
                "delivery.uncertain update_id=%s chat_hash=%s message_id=%s error_class=%s",
                accepted.update_id,
                context["chat_hash"],
                accepted.message_id,
                type(exc).__name__,
            )
            return

        outbound_message_id = "telegram:" + accepted.chat_id + ":" + telegram_message_id
        self.state.update(
            update_key,
            "TELEGRAM_CONFIRMED",
            telegram_message_id=telegram_message_id,
            outbound_message_id=outbound_message_id,
        )
        confirmed_result = dict(result)
        confirmed_result["outbound_message_id"] = outbound_message_id
        try:
            self.runtime.commit_turn(confirmed_result, accepted.text)
            evidence_status = self.runtime.record_delivery_success(confirmed_result)
        except Exception as exc:
            LOGGER.error(
                "delivery.evidence_pending update_id=%s chat_hash=%s error_class=%s",
                accepted.update_id,
                context["chat_hash"],
                type(exc).__name__,
            )
            return

        disposition = evidence_disposition(evidence_status)
        if not evidence_is_durable(evidence_status):
            # Telegram already confirmed the send. Only the evidence RECORD is not
            # durable, so the turn must not claim DELIVERED and must never resend.
            # Leaving the row at TELEGRAM_CONFIRMED lets recover() finalise it later.
            self.state.update(update_key, "TELEGRAM_CONFIRMED",
                              error_class="evidence_" + disposition.lower())
            LOGGER.warning(
                "delivery.evidence_not_durable update_id=%s chat_hash=%s disposition=%s",
                accepted.update_id,
                context["chat_hash"],
                disposition,
            )
            return

        self.state.update(update_key, "DELIVERED", error_class=None)
        LOGGER.info(
            "turn.delivered update_id=%s chat_hash=%s message_id=%s latency_ms=%s",
            accepted.update_id,
            context["chat_hash"],
            accepted.message_id,
            round((time.monotonic() - started) * 1000, 3),
        )

    def run_once(self) -> int:
        offset = self.state.get_offset()
        updates = self.api.get_updates(offset, 25)
        for update in updates:
            update_id = update.get("update_id")
            if isinstance(update_id, int):
                try:
                    self.process_update(update)
                except Exception as exc:
                    LOGGER.error(
                        "update.failed update_id=%s error_class=%s",
                        update_id,
                        type(exc).__name__,
                    )
                self.state.set_offset(update_id + 1)
        return len(updates)

    def run_forever(self, stop_event) -> None:
        self.api.validate()
        self.recover()
        LOGGER.info("telegram.polling_ready mode=long_polling")
        while not stop_event.is_set():
            try:
                self.run_once()
            except TelegramTransportError as exc:
                cause = exc.__cause__
                reason = getattr(cause, "reason", None) or cause
                if isinstance(reason, TimeoutError):
                    # Long polling: Telegram holds the connection until its own timeout and then
                    # returns an empty result. A socket timeout here is the normal end of a poll,
                    # not a fault, so it must not be logged as an error.
                    LOGGER.info("poll.long_poll_timeout")
                    continue
                LOGGER.error(
                    "poll.failed error_class=%s cause_class=%s cause=%s",
                    type(exc).__name__,
                    type(reason).__name__,
                    _scrub(str(reason))[:200],
                )
                stop_event.wait(5.0)
            except Exception as exc:
                LOGGER.error(
                    "poll.failed error_class=%s cause=%s",
                    type(exc).__name__,
                    _scrub(str(exc))[:200],
                )
                stop_event.wait(5.0)


class DeferredConversationStore:
    """Read through to Native history, deferring raw writes to transport success."""

    def __init__(self, source):
        self.source = source

    def load(self, conversation_id: str):
        return self.source.load(conversation_id)

    def persist_turn(self, *args, **kwargs):
        # NativeRuntime's normal HTTP path is unchanged. This store is used
        # only by the Telegram adapter until sendMessage is confirmed.
        return None


class DeferredNativeRuntime:
    """NativeRuntime with an external-transport commit point."""

    def __init__(self, runtime, raw_store, config):
        self.runtime = runtime
        self.raw_store = raw_store
        self.config = config

    def handle_turn(self, conversation_id, user_text, message_id=None, participant=None):
        return self.runtime.handle_turn(
            conversation_id,
            user_text,
            message_id,
            participant=participant,
        )

    def commit_turn(self, result, user_text):
        self.raw_store.persist_turn(
            str(result["conversation_id"]),
            str(result["turn_id"]),
            utc_now(),
            user_text,
            str(result["raw_content"]),
            self.config.model,
        )

    def record_delivery_success(self, result):
        return self.runtime.record_delivery_success(result)


def _build_runtime(config_path: str):
    from app.config import NativeConfig
    from app.epoch import ContextEpochStore
    from app.evidence import EvidenceWriter
    from app.model import DirectProvider
    from app.runtime import NativeRuntime
    from app.session import ConversationStore
    from app.trace import TraceStore

    config = NativeConfig.load(config_path)
    store = ConversationStore(config.data_dir)
    traces = TraceStore(config.trace_dir)
    backend = DirectProvider(config)
    evidence = EvidenceWriter(config.data_dir)
    context_epoch = ContextEpochStore(config.data_dir)
    deferred_store = DeferredConversationStore(store)
    runtime = NativeRuntime(
        config,
        deferred_store,
        backend,
        traces,
        evidence,
        context_epoch,
    )
    return DeferredNativeRuntime(runtime, store, config)


def main() -> None:
    parser = argparse.ArgumentParser(description="Chiyo Telegram adapter")
    parser.add_argument(
        "--config",
        default=os.environ.get("NATIVE_CONFIG_PATH", "/etc/chiyo/config.json"),
    )
    args = parser.parse_args()
    config = NativeConfig.load(args.config)
    allowed_user_id = os.environ.get("TELEGRAM_ALLOWED_USER_ID", "").strip()
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    _TOKEN_FOR_LOGGING["value"] = token
    state_path = os.environ.get(
        "TELEGRAM_STATE_DB", "/var/lib/chiyo/telegram/state.sqlite"
    )
    namespace = os.environ.get("TELEGRAM_CONVERSATION_NAMESPACE", "native-test-v1")
    if not allowed_user_id:
        raise SystemExit("TELEGRAM_ALLOWED_USER_ID is required")
    participant_resolver = ParticipantResolver.load(config.participants_path)
    if participant_resolver.resolve("telegram", allowed_user_id) is None:
        raise SystemExit("Telegram allowlist is not bound to a canonical participant")

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    runtime = _build_runtime(args.config)
    api = TelegramApi(token, timeout_seconds=40)
    state = TelegramState(state_path)
    adapter = TelegramAdapter(
        runtime,
        api,
        state,
        allowed_user_id,
        namespace,
        participant_resolver,
    )
    stop_event = threading.Event()

    def stop(*_args):
        stop_event.set()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    adapter.run_forever(stop_event)


if __name__ == "__main__":
    main()

