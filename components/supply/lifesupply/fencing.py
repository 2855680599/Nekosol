"""Owner-local exclusive writer fences with database-checked epochs.

The lock excludes a second live service. The monotonic token is also compared inside every
canonical SQLite row mutation, so a stale process cannot write after a successor epoch commits.
"""
from __future__ import annotations

import fcntl
import json
import os
import socket
import time
import uuid
from pathlib import Path
from typing import Any


class FenceHeld(RuntimeError):
    pass


class StaleWriter(RuntimeError):
    pass


def require_schema_columns(con, owner: str, requirements: dict[str, set[str]]) -> None:
    """Reject prior schemas until an explicit, tested migration is supplied."""
    for table, required in requirements.items():
        actual = {row[1] for row in con.execute(f"PRAGMA table_info({table})").fetchall()}
        missing = sorted(required - actual)
        if missing:
            raise RuntimeError(f"C6_SCHEMA_MIGRATION_REQUIRED:{owner}:{table}:{','.join(missing)}")


def require_schema_columns_from_path(path: str | Path, owner: str, requirements: dict[str, set[str]]) -> None:
    import sqlite3
    uri = f"file:{Path(path)}?mode=ro"
    # ``with sqlite3.connect(...)`` is a TRANSACTION scope, not a closing one: relying on it
    # leaks the file handle and produces ``ResourceWarning: unclosed database``.
    con = sqlite3.connect(uri, uri=True)
    try:
        require_schema_columns(con, owner, requirements)
    finally:
        con.close()


def reject_incompatible_existing_schema(path: str | Path, owner: str,
        sentinel_table: str, requirements: dict[str, set[str]]) -> None:
    """Fail before DDL can partially mutate an existing prior-version database."""
    import sqlite3
    path = Path(path)
    if not path.exists() or path.stat().st_size == 0:
        return
    uri = f"file:{path}?mode=ro"
    con = sqlite3.connect(uri, uri=True)
    try:
        tables = {row[0] for row in
                  con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if sentinel_table in tables:
            require_schema_columns(con, owner, requirements)
    finally:
        con.close()


class WriterFence:
    def __init__(self, owner: str, directory: str | Path, *, instance_id: str | None = None,
                 read_only: bool = False):
        self.owner = owner
        self.directory = Path(directory)
        self.path = self.directory / f"{owner.lower()}.fence"
        self.instance_id = instance_id or f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex}"
        self.writer_token = uuid.uuid4().hex
        self.read_only = read_only
        self.epoch: int | None = None
        self._fd: int | None = None
        if not read_only:
            self.acquire()

    def acquire(self) -> int:
        if self._fd is not None:
            return int(self.epoch or 0)
        self.directory.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            os.close(fd)
            raise FenceHeld(f"WRITE_FENCE_HELD:{self.owner}") from exc
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            raw = os.read(fd, 65536)
            try:
                current = json.loads(raw.decode()) if raw else {}
                epoch = int(current.get("epoch", 0)) + 1
            except (ValueError, TypeError, AttributeError) as exc:
                raise RuntimeError(f"invalid fence state for {self.owner}") from exc
            payload = json.dumps({"owner": self.owner, "epoch": epoch,
                "instance_id": self.instance_id, "writer_token": self.writer_token, "pid": os.getpid(),
                "acquired_at": time.time()}, sort_keys=True).encode()
            os.ftruncate(fd, 0)
            os.lseek(fd, 0, os.SEEK_SET)
            os.write(fd, payload)
            os.fsync(fd)
            self._fd = fd
            self.epoch = epoch
            self.read_only = False
            return epoch
        except Exception:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
            raise

    def assert_current(self, epoch: int | None = None) -> int:
        if self.read_only or self._fd is None or self.epoch is None:
            raise StaleWriter(f"STALE_WRITER:{self.owner}:read-only")
        if epoch is not None and int(epoch) != self.epoch:
            raise StaleWriter(f"STALE_WRITER:{self.owner}:epoch")
        try:
            with open(self.path, "r", encoding="utf-8") as stream:
                current = json.load(stream)
        except (OSError, ValueError) as exc:
            raise StaleWriter(f"STALE_WRITER:{self.owner}:unavailable") from exc
        if (current.get("instance_id") != self.instance_id or int(current.get("epoch", -1)) != self.epoch
                or current.get("writer_token") != self.writer_token):
            raise StaleWriter(f"STALE_WRITER:{self.owner}:superseded")
        return self.epoch

    def status(self) -> dict[str, Any]:
        if self.read_only or self._fd is None:
            return {"owner": self.owner, "state": "READ_ONLY", "epoch": self.epoch}
        try:
            self.assert_current()
            state = "HELD"
        except StaleWriter:
            state = "FENCED"
        return {"owner": self.owner, "state": state, "epoch": self.epoch,
                "instance_id": self.instance_id}

    def release(self) -> None:
        if self._fd is not None:
            try:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
            finally:
                os.close(self._fd)
                self._fd = None

    def close(self) -> None:
        self.release()

    def __enter__(self) -> "WriterFence":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


def register_writer_connection(con, fence: WriterFence | None) -> None:
    token = 0 if fence is None else int(fence.assert_current())
    instance_id = "unfenced-test" if fence is None else fence.instance_id
    writer_token = "unfenced-test" if fence is None else fence.writer_token
    con.create_function("c6_writer_epoch", 0, lambda: token, deterministic=False)
    con.create_function("c6_writer_instance", 0, lambda: instance_id, deterministic=False)
    con.create_function("c6_writer_token", 0, lambda: writer_token, deterministic=False)


def activate_writer_epoch(con, owner: str, fence: WriterFence | None) -> None:
    """Persist the active token during fenced service startup, before serving requests."""
    con.execute("CREATE TABLE IF NOT EXISTS c6_writer_epoch(owner TEXT PRIMARY KEY, active_epoch INTEGER NOT NULL, instance_id TEXT NOT NULL, writer_token TEXT NOT NULL DEFAULT 'unfenced-test')")
    cols = {row[1] for row in con.execute("PRAGMA table_info(c6_writer_epoch)").fetchall()}
    if "writer_token" not in cols:
        con.execute("ALTER TABLE c6_writer_epoch ADD COLUMN writer_token TEXT NOT NULL DEFAULT 'unfenced-test'")
    con.execute("INSERT OR IGNORE INTO c6_writer_epoch(owner,active_epoch,instance_id) VALUES(?,0,'unfenced-test')", (owner,))
    if fence is not None:
        epoch = fence.assert_current()
        con.execute("BEGIN IMMEDIATE")
        con.execute("UPDATE c6_writer_epoch SET active_epoch=?,instance_id=?,writer_token=? WHERE owner=?",
                    (epoch, fence.instance_id, fence.writer_token, owner))
        con.commit()
    register_writer_connection(con, fence)


def install_write_guards(con, owner: str, tables: tuple[str, ...]) -> None:
    """Guard canonical tables at SQLite statement time, within the caller's transaction."""
    for table in tables:
        for action in ("INSERT", "UPDATE", "DELETE"):
            name = f"c6_fence_{table}_{action.lower()}"
            condition = f"c6_writer_epoch() < 0 OR (SELECT active_epoch FROM c6_writer_epoch WHERE owner='{owner}') != c6_writer_epoch() OR (SELECT instance_id FROM c6_writer_epoch WHERE owner='{owner}') != c6_writer_instance() OR (SELECT writer_token FROM c6_writer_epoch WHERE owner='{owner}') != c6_writer_token()"
            con.execute(f"DROP TRIGGER IF EXISTS {name}")
            con.execute(f"""CREATE TRIGGER {name} BEFORE {action} ON {table}
                WHEN {condition}
                BEGIN SELECT RAISE(ABORT,'STALE_WRITER'); END""")
