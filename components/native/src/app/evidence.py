from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
import fcntl
import hashlib
import json
import logging
import os
import secrets
from app.sqlite_safety import configure_journal
import sqlite3
import tempfile
import uuid


USER_ORIGIN = "USER_VISIBLE_INPUT"
CHIYO_ORIGIN = "CHIYO_VISIBLE_OUTPUT"
USER_ROLE = "OBSERVED_EXTERNAL_EXPRESSION"
CHIYO_ROLE = "SELF_EXPRESSION"
RECEIVED = "RECEIVED"
DELIVERED = "DELIVERED"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_event_id() -> str:
    """Return a UUIDv7-shaped time-ordered identifier without extra packages."""
    timestamp_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    value = (timestamp_ms & ((1 << 48) - 1)) << 80
    value |= 0x7 << 76
    value |= secrets.randbits(12) << 64
    value |= 0b10 << 62
    value |= secrets.randbits(62)
    return str(uuid.UUID(int=value))


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _secure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, 0o700)


@dataclass(frozen=True)
class EvidenceEvent:
    event_id: str
    occurred_at: str
    memory_owner: str
    source_origin: str
    delivery_status: str
    epistemic_role: str
    speaker: str
    content: str
    conversation_id: str
    turn_id: str
    source_refs: list[dict[str, str]]
    created_at: str

    def __post_init__(self) -> None:
        if not self.event_id or not self.memory_owner:
            raise ValueError("event_id and memory_owner are required")
        if not isinstance(self.content, str):
            raise ValueError("evidence content must be text")
        if not self.conversation_id or not self.turn_id:
            raise ValueError("conversation_id and turn_id are required")
        if not self.source_refs or any(
            not isinstance(item, dict) or not item.get("kind") or not item.get("id")
            for item in self.source_refs
        ):
            raise ValueError("source_refs must contain kind and id")
        _parse_time(self.occurred_at)
        _parse_time(self.created_at)
        if self.source_origin == USER_ORIGIN:
            expected = (RECEIVED, USER_ROLE, "user")
        elif self.source_origin == CHIYO_ORIGIN:
            expected = (DELIVERED, CHIYO_ROLE, "chiyo")
        else:
            raise ValueError("unsupported M0 source origin")
        if (self.delivery_status, self.epistemic_role, self.speaker) != expected:
            raise ValueError("invalid source origin/provenance combination")

    @property
    def primary_source_ref_id(self) -> str:
        return str(self.source_refs[0]["id"])

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "occurred_at": self.occurred_at,
            "memory_owner": self.memory_owner,
            "source_origin": self.source_origin,
            "delivery_status": self.delivery_status,
            "epistemic_role": self.epistemic_role,
            "speaker": self.speaker,
            "content": self.content,
            "conversation_id": self.conversation_id,
            "turn_id": self.turn_id,
            "source_refs": self.source_refs,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "EvidenceEvent":
        return cls(
            event_id=str(data["event_id"]),
            occurred_at=str(data["occurred_at"]),
            memory_owner=str(data["memory_owner"]),
            source_origin=str(data["source_origin"]),
            delivery_status=str(data["delivery_status"]),
            epistemic_role=str(data["epistemic_role"]),
            speaker=str(data["speaker"]),
            content=str(data["content"]),
            conversation_id=str(data["conversation_id"]),
            turn_id=str(data["turn_id"]),
            source_refs=list(data["source_refs"]),
            created_at=str(data["created_at"]),
        )


@dataclass(frozen=True)
class AppendResult:
    status: str
    event_id: str


class EvidenceStore:
    """Append-only M0 Evidence store. It has no prompt/read-recall path."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        _secure_dir(self.path.parent)
        self._initialize()
        os.chmod(self.path, 0o600)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=5000")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def _initialize(self) -> None:
        connection = self._connect()
        try:
            configure_journal(connection)
            connection.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations ("
                "version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
            )
            applied = connection.execute(
                "SELECT version FROM schema_migrations WHERE version=1"
            ).fetchone()
            if applied is None:
                connection.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS evidence_events (
                        event_id TEXT PRIMARY KEY,
                        occurred_at TEXT NOT NULL,
                        memory_owner TEXT NOT NULL,
                        source_origin TEXT NOT NULL,
                        delivery_status TEXT NOT NULL,
                        epistemic_role TEXT NOT NULL,
                        speaker TEXT NOT NULL,
                        content TEXT NOT NULL,
                        conversation_id TEXT NOT NULL,
                        turn_id TEXT NOT NULL,
                        primary_source_ref_id TEXT NOT NULL,
                        source_refs_json TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        CHECK (
                            (source_origin='USER_VISIBLE_INPUT'
                             AND delivery_status='RECEIVED'
                             AND epistemic_role='OBSERVED_EXTERNAL_EXPRESSION'
                             AND speaker='user')
                            OR
                            (source_origin='CHIYO_VISIBLE_OUTPUT'
                             AND delivery_status='DELIVERED'
                             AND epistemic_role='SELF_EXPRESSION'
                             AND speaker='chiyo')
                        )
                    );
                    CREATE UNIQUE INDEX IF NOT EXISTS
                        evidence_source_id_uq
                        ON evidence_events(source_origin, primary_source_ref_id);
                    CREATE INDEX IF NOT EXISTS evidence_occurred_idx
                        ON evidence_events(occurred_at);
                    CREATE INDEX IF NOT EXISTS evidence_conversation_idx
                        ON evidence_events(conversation_id);
                    CREATE INDEX IF NOT EXISTS evidence_turn_idx
                        ON evidence_events(turn_id);
                    CREATE INDEX IF NOT EXISTS evidence_speaker_idx
                        ON evidence_events(speaker);
                    CREATE INDEX IF NOT EXISTS evidence_origin_idx
                        ON evidence_events(source_origin);
                    CREATE TRIGGER IF NOT EXISTS evidence_no_update
                        BEFORE UPDATE ON evidence_events
                        BEGIN SELECT RAISE(ABORT, 'M0 Evidence is append-only'); END;
                    CREATE TRIGGER IF NOT EXISTS evidence_no_delete
                        BEFORE DELETE ON evidence_events
                        BEGIN SELECT RAISE(ABORT, 'M0 Evidence is append-only'); END;
                    """
                )
                connection.execute(
                    "INSERT INTO schema_migrations(version, applied_at) VALUES(1, ?)",
                    (utc_now(),),
                )
            connection.commit()
        finally:
            connection.close()

    @staticmethod
    def _same_event(row: sqlite3.Row, event: EvidenceEvent) -> bool:
        return all(
            row[field] == value
            for field, value in (
                ("memory_owner", event.memory_owner),
                ("source_origin", event.source_origin),
                ("delivery_status", event.delivery_status),
                ("epistemic_role", event.epistemic_role),
                ("speaker", event.speaker),
                ("content", event.content),
                ("conversation_id", event.conversation_id),
                ("primary_source_ref_id", event.primary_source_ref_id),
            )
        )

    def append(self, event: EvidenceEvent) -> AppendResult:
        encoded_refs = json.dumps(
            event.source_refs, ensure_ascii=False, sort_keys=True
        )
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM evidence_events WHERE source_origin=? "
                "AND primary_source_ref_id=?",
                (event.source_origin, event.primary_source_ref_id),
            ).fetchone()
            if existing is not None:
                if not self._same_event(existing, event):
                    raise ValueError("evidence idempotency key conflicts with existing event")
                connection.commit()
                return AppendResult("duplicate", str(existing["event_id"]))
            connection.execute(
                "INSERT INTO evidence_events ("
                "event_id, occurred_at, memory_owner, source_origin, delivery_status, "
                "epistemic_role, speaker, content, conversation_id, turn_id, "
                "primary_source_ref_id, source_refs_json, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    event.event_id,
                    event.occurred_at,
                    event.memory_owner,
                    event.source_origin,
                    event.delivery_status,
                    event.epistemic_role,
                    event.speaker,
                    event.content,
                    event.conversation_id,
                    event.turn_id,
                    event.primary_source_ref_id,
                    encoded_refs,
                    event.created_at,
                ),
            )
            connection.commit()
            return AppendResult("inserted", event.event_id)
        except sqlite3.IntegrityError:
            connection.rollback()
            existing = connection.execute(
                "SELECT * FROM evidence_events WHERE source_origin=? "
                "AND primary_source_ref_id=?",
                (event.source_origin, event.primary_source_ref_id),
            ).fetchone()
            if existing is not None and self._same_event(existing, event):
                return AppendResult("duplicate", str(existing["event_id"]))
            raise
        finally:
            connection.close()

    def get(self, event_id: str) -> dict[str, Any] | None:
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT * FROM evidence_events WHERE event_id=?", (event_id,)
            ).fetchone()
            return self._row_to_dict(row) if row is not None else None
        finally:
            connection.close()

    def list_events(
        self, conversation_id: str | None = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 1000))
        connection = self._connect()
        try:
            if conversation_id is None:
                rows = connection.execute(
                    "SELECT * FROM evidence_events ORDER BY occurred_at, created_at "
                    "LIMIT ?",
                    (limit,),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM evidence_events WHERE conversation_id=? "
                    "ORDER BY occurred_at, created_at LIMIT ?",
                    (conversation_id, limit),
                ).fetchall()
            return [self._row_to_dict(row) for row in rows]
        finally:
            connection.close()

    def verify(self) -> dict[str, Any]:
        issues: list[str] = []
        connection = self._connect()
        try:
            version = connection.execute(
                "SELECT MAX(version) AS version FROM schema_migrations"
            ).fetchone()["version"]
            if version != 1:
                issues.append("schema_version_not_1")
            rows = connection.execute(
                "SELECT * FROM evidence_events ORDER BY occurred_at, created_at"
            ).fetchall()
            seen: set[tuple[str, str]] = set()
            for row in rows:
                key = (row["source_origin"], row["primary_source_ref_id"])
                if key in seen:
                    issues.append("duplicate_source_ref:" + ":".join(key))
                seen.add(key)
                try:
                    refs = json.loads(row["source_refs_json"])
                    if not isinstance(refs, list) or not refs:
                        raise ValueError
                    for item in refs:
                        if not isinstance(item, dict) or not item.get("kind") or not item.get("id"):
                            raise ValueError
                    _parse_time(row["occurred_at"])
                    created = _parse_time(row["created_at"])
                    occurred = _parse_time(row["occurred_at"])
                    if created < occurred:
                        issues.append("created_before_occurred:" + row["event_id"])
                except (TypeError, ValueError, json.JSONDecodeError):
                    issues.append("invalid_event:" + row["event_id"])
                if not row["memory_owner"]:
                    issues.append("null_owner:" + row["event_id"])
                if row["source_origin"] == CHIYO_ORIGIN and row["delivery_status"] != DELIVERED:
                    issues.append("undelivered_chiyo:" + row["event_id"])
            return {
                "schema_version": version,
                "event_count": len(rows),
                "issues": issues,
                "ok": not issues,
            }
        finally:
            connection.close()

    def backup(self, destination: str | Path) -> Path:
        target = Path(destination)
        _secure_dir(target.parent)
        if target.exists():
            raise FileExistsError(str(target))
        source = self._connect()
        destination_connection = sqlite3.connect(target)
        try:
            source.backup(destination_connection)
            destination_connection.commit()
        finally:
            destination_connection.close()
            source.close()
        os.chmod(target, 0o600)
        return target

    @staticmethod
    def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["source_refs"] = json.loads(result.pop("source_refs_json"))
        return result


class MetricsStore:
    KEYS = (
        "evidence_user_total",
        "evidence_chiyo_total",
        "evidence_duplicate_total",
        "evidence_failed_total",
        "evidence_retry_pending",
    )

    def __init__(self, path: str | Path):
        self.path = Path(path)
        _secure_dir(self.path.parent)
        self.lock_path = self.path.with_suffix(self.path.suffix + ".lock")

    def _read(self) -> dict[str, int]:
        if not self.path.exists():
            return {key: 0 for key in self.KEYS}
        data = json.loads(self.path.read_text(encoding="utf-8"))
        return {key: int(data.get(key, 0)) for key in self.KEYS}

    def _write(self, data: dict[str, int]) -> None:
        fd, temp_name = tempfile.mkstemp(prefix=".metrics.", dir=str(self.path.parent), text=True)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(data, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(temp_name, 0o600)
            os.replace(temp_name, self.path)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)

    def increment(self, key: str, amount: int = 1) -> None:
        with self.lock_path.open("a+", encoding="utf-8") as lock:
            os.chmod(self.lock_path, 0o600)
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            data = self._read()
            data[key] = data.get(key, 0) + amount
            self._write(data)
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def set_value(self, key: str, value: int) -> None:
        with self.lock_path.open("a+", encoding="utf-8") as lock:
            os.chmod(self.lock_path, 0o600)
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            data = self._read()
            data[key] = int(value)
            self._write(data)
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def snapshot(self) -> dict[str, int]:
        with self.lock_path.open("a+", encoding="utf-8") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_SH)
            data = self._read()
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
            return data


class RetrySpool:
    """Append-only local outbox; pending records are never rewritten."""

    def __init__(self, root: str | Path):
        self.root = Path(root)
        _secure_dir(self.root)
        self.pending_path = self.root / "pending.jsonl"
        self.committed_path = self.root / "committed.jsonl"
        self.lock_path = self.root / "spool.lock"

    @staticmethod
    def _append_line(path: Path, data: dict[str, Any]) -> None:
        with path.open("a", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(path, 0o600)

    def queue(self, event: EvidenceEvent, error: str) -> None:
        with self.lock_path.open("a+", encoding="utf-8") as lock:
            os.chmod(self.lock_path, 0o600)
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            self._append_line(
                self.pending_path,
                {"event": event.to_dict(), "queued_at": utc_now(), "error": error},
            )
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def _records(self) -> list[dict[str, Any]]:
        if not self.pending_path.exists():
            return []
        return [
            json.loads(line)
            for line in self.pending_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def _committed_keys(self) -> set[tuple[str, str]]:
        if not self.committed_path.exists():
            return set()
        keys: set[tuple[str, str]] = set()
        for line in self.committed_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            record = json.loads(line)
            keys.add((str(record["source_origin"]), str(record["source_ref_id"])))
        return keys

    def pending_records(self) -> list[dict[str, Any]]:
        committed = self._committed_keys()
        result = []
        for record in self._records():
            event = record["event"]
            key = (str(event["source_origin"]), str(event["source_refs"][0]["id"]))
            if key not in committed:
                result.append(record)
        return result

    def mark_committed(self, event: EvidenceEvent, status: str) -> None:
        with self.lock_path.open("a+", encoding="utf-8") as lock:
            os.chmod(self.lock_path, 0o600)
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            self._append_line(
                self.committed_path,
                {
                    "source_origin": event.source_origin,
                    "source_ref_id": event.primary_source_ref_id,
                    "event_id": event.event_id,
                    "status": status,
                    "committed_at": utc_now(),
                },
            )
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def pending_count(self) -> int:
        return len(self.pending_records())


class EvidenceWriter:
    """Thin append-only writer isolated from response/context authority."""

    def __init__(self, data_root: str | Path, logger: logging.Logger | None = None):
        root = Path(data_root)
        self.memory_root = root / "memory"
        _secure_dir(self.memory_root)
        self.logger = logger or logging.getLogger("chiyo.evidence")
        self.store: EvidenceStore | None = None
        self.spool = RetrySpool(self.memory_root / "evidence-retry")
        self.metrics = MetricsStore(self.memory_root / "metrics.json")
        try:
            self.store = EvidenceStore(self.memory_root / "evidence.sqlite")
        except Exception as exc:
            self.logger.error("evidence.store.init_failed error_class=%s", type(exc).__name__)
        self.retry_pending()

    def _get_store(self) -> EvidenceStore:
        if self.store is None:
            self.store = EvidenceStore(self.memory_root / "evidence.sqlite")
        return self.store

    def append(self, event: EvidenceEvent) -> str:
        try:
            result = self._get_store().append(event)
            if result.status == "inserted":
                key = "evidence_user_total" if event.source_origin == USER_ORIGIN else "evidence_chiyo_total"
                self.metrics.increment(key)
                self.logger.info(
                    "evidence.append.ok source_origin=%s event_id=%s conversation_id=%s "
                    "turn_id=%s source_ref_hash=%s",
                    event.source_origin,
                    result.event_id,
                    event.conversation_id,
                    event.turn_id,
                    hashlib.sha256(event.primary_source_ref_id.encode()).hexdigest()[:24],
                )
            else:
                self.metrics.increment("evidence_duplicate_total")
                self.logger.info(
                    "evidence.append.duplicate source_origin=%s event_id=%s "
                    "conversation_id=%s turn_id=%s source_ref_hash=%s",
                    event.source_origin,
                    result.event_id,
                    event.conversation_id,
                    event.turn_id,
                    hashlib.sha256(event.primary_source_ref_id.encode()).hexdigest()[:24],
                )
            return result.status
        except ValueError:
            self.metrics.increment("evidence_failed_total")
            self.logger.error(
                "evidence.append.failed error_class=ValueError source_origin=%s "
                "conversation_id=%s turn_id=%s source_ref_hash=%s",
                event.source_origin,
                event.conversation_id,
                event.turn_id,
                hashlib.sha256(event.primary_source_ref_id.encode()).hexdigest()[:24],
            )
            return "failed"
        except Exception as exc:
            self.metrics.increment("evidence_failed_total")
            try:
                self.spool.queue(event, type(exc).__name__)
                self.metrics.set_value("evidence_retry_pending", self.spool.pending_count())
                self.logger.error(
                    "evidence.retry.queued error_class=%s source_origin=%s "
                    "conversation_id=%s turn_id=%s source_ref_hash=%s",
                    type(exc).__name__,
                    event.source_origin,
                    event.conversation_id,
                    event.turn_id,
                    hashlib.sha256(event.primary_source_ref_id.encode()).hexdigest()[:24],
                )
                return "queued"
            except Exception as spool_exc:
                self.logger.critical(
                    "evidence.append.failed error_class=%s spool_error_class=%s",
                    type(exc).__name__,
                    type(spool_exc).__name__,
                )
                return "failed"

    def retry_pending(self) -> dict[str, int]:
        inserted = 0
        duplicate = 0
        failed = 0
        for record in self.spool.pending_records():
            event = EvidenceEvent.from_dict(record["event"])
            try:
                result = self._get_store().append(event)
                self.spool.mark_committed(event, result.status)
                if result.status == "inserted":
                    inserted += 1
                    key = "evidence_user_total" if event.source_origin == USER_ORIGIN else "evidence_chiyo_total"
                    self.metrics.increment(key)
                else:
                    duplicate += 1
                    self.metrics.increment("evidence_duplicate_total")
                self.logger.info(
                    "evidence.retry.ok source_origin=%s event_id=%s status=%s "
                    "conversation_id=%s turn_id=%s source_ref_hash=%s",
                    event.source_origin,
                    result.event_id,
                    result.status,
                    event.conversation_id,
                    event.turn_id,
                    hashlib.sha256(event.primary_source_ref_id.encode()).hexdigest()[:24],
                )
            except Exception as exc:
                failed += 1
                self.logger.error(
                    "evidence.retry.failed error_class=%s source_origin=%s "
                    "conversation_id=%s turn_id=%s source_ref_hash=%s",
                    type(exc).__name__,
                    event.source_origin,
                    event.conversation_id,
                    event.turn_id,
                    hashlib.sha256(event.primary_source_ref_id.encode()).hexdigest()[:24],
                )
        try:
            self.metrics.set_value("evidence_retry_pending", self.spool.pending_count())
        except Exception:
            pass
        return {"inserted": inserted, "duplicate": duplicate, "failed": failed}

    def status(self) -> dict[str, Any]:
        store = self._get_store()
        return {
            "metrics": self.metrics.snapshot(),
            "retry_pending": self.spool.pending_count(),
            "verify": store.verify(),
        }

    def backup(self, destination: str | Path) -> Path:
        return self._get_store().backup(destination)
