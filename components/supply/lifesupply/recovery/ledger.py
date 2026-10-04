"""Minimal per-owner inbox/outbox receipt ledger and conservative reconciliation."""
from __future__ import annotations

import hashlib
import json
from lifesupply.sqlite_safety import configure_journal
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value: Any) -> str:
    return hashlib.sha256(_json(value).encode()).hexdigest()


class OperationLedger:
    def __init__(self, path: str | Path, owner: str, *, clock=time.time, writer_fence=None):
        self.path = str(path)
        self.owner = owner
        self.clock = clock
        self.writer_fence = writer_fence
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as con:
            configure_journal(con)
            con.executescript("""
                PRAGMA synchronous=FULL;
                CREATE TABLE IF NOT EXISTS owner_inbox(
                    operation_id TEXT PRIMARY KEY, owner TEXT NOT NULL,
                    request_digest TEXT NOT NULL, state TEXT NOT NULL,
                    writer_epoch INTEGER NOT NULL, accepted_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS owner_outbox(
                    outbox_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    operation_id TEXT NOT NULL UNIQUE REFERENCES owner_inbox(operation_id),
                    owner TEXT NOT NULL, receipt_json TEXT NOT NULL,
                    delivery_state TEXT NOT NULL CHECK(delivery_state IN ('PENDING','DELIVERED')),
                    created_at REAL NOT NULL, delivered_at REAL
                );
            """)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        """A connection scope that also CLOSES.

        ``with sqlite3.connect(...) as con:`` is a *transaction* scope, not a closing one, so
        using it as a scope leaked one file handle per call (measured: 21 unclosed connections
        in a single test run).  Every caller already treats ``_connect()`` as a scope, so the
        fix belongs here instead of at each call site.

        Exit semantics match the old ``with con:`` exactly -- commit on success, roll back on
        an exception -- and the connection is then closed either way.
        """
        con = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        try:
            con.row_factory = sqlite3.Row
            con.execute("PRAGMA foreign_keys=ON")
            con.execute("PRAGMA busy_timeout=10000")
            yield con
            con.commit()
        except Exception:
            con.rollback()
            raise
        finally:
            con.close()

    def _epoch(self) -> int:
        if self.writer_fence is None:
            raise RuntimeError("writer fence required")
        return int(self.writer_fence.assert_current())

    def accept(self, operation_id: str, request: Any) -> dict[str, Any]:
        if not operation_id:
            return {"status": "DENY", "detail": "operation_id required"}
        epoch = self._epoch()
        digest, now = _digest(request), float(self.clock())
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute("SELECT * FROM owner_inbox WHERE operation_id=?", (operation_id,)).fetchone()
            if row:
                con.commit()
                if row["request_digest"] != digest:
                    return {"status": "CONFLICT", "detail": "operation_id request mismatch"}
                return {"status": "NO_CHANGE", "operation_id": operation_id, "state": row["state"]}
            con.execute("INSERT INTO owner_inbox VALUES(?,?,?,?,?,?,?)",
                        (operation_id, self.owner, digest, "ACCEPTED", epoch, now, now))
            con.commit()
        return {"status": "ACCEPTED", "operation_id": operation_id, "writer_epoch": epoch}

    def finish(self, operation_id: str, request: Any, result: dict[str, Any], *, delivered: bool = False) -> dict[str, Any]:
        epoch = self._epoch()
        digest, now = _digest(request), float(self.clock())
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute("SELECT * FROM owner_inbox WHERE operation_id=?", (operation_id,)).fetchone()
            if not row:
                con.rollback()
                return {"status": "NOT_FOUND", "operation_id": operation_id}
            if row["request_digest"] != digest:
                con.rollback()
                return {"status": "CONFLICT", "operation_id": operation_id}
            previous = con.execute("SELECT * FROM owner_outbox WHERE operation_id=?", (operation_id,)).fetchone()
            if previous:
                con.commit()
                return json.loads(previous["receipt_json"])
            con.execute("UPDATE owner_inbox SET state='COMMITTED',updated_at=? WHERE operation_id=?", (now, operation_id))
            con.execute("INSERT INTO owner_outbox(operation_id,owner,receipt_json,delivery_state,created_at,delivered_at) VALUES(?,?,?,?,?,?)",
                        (operation_id, self.owner, _json(result), "DELIVERED" if delivered else "PENDING", now, now if delivered else None))
            con.commit()
        return result

    def lookup(self, operation_id: str) -> dict[str, Any]:
        with self._connect() as con:
            row = con.execute("SELECT i.*,o.receipt_json,o.delivery_state FROM owner_inbox i LEFT JOIN owner_outbox o USING(operation_id) WHERE i.operation_id=?", (operation_id,)).fetchone()
        if not row:
            return {"status": "NOT_FOUND", "operation_id": operation_id}
        if row["receipt_json"]:
            return {"status": "KNOWN", "operation_id": operation_id,
                    "state": row["state"], "receipt": json.loads(row["receipt_json"]),
                    "delivery_state": row["delivery_state"], "writer_epoch": row["writer_epoch"]}
        return {"status": "UNKNOWN", "operation_id": operation_id, "state": row["state"],
                "writer_epoch": row["writer_epoch"]}

    def unresolved(self) -> list[dict[str, Any]]:
        with self._connect() as con:
            rows = con.execute("SELECT operation_id,state,writer_epoch,accepted_at,updated_at FROM owner_inbox WHERE state!='COMMITTED' ORDER BY accepted_at").fetchall()
            return [dict(row) for row in rows]

    def mark_delivered(self, operation_id: str) -> dict[str, Any]:
        self._epoch()
        now = float(self.clock())
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            cur = con.execute("UPDATE owner_outbox SET delivery_state='DELIVERED',delivered_at=? WHERE operation_id=?", (now, operation_id))
            con.commit()
            return {"status": "OK" if cur.rowcount else "NOT_FOUND", "operation_id": operation_id}
