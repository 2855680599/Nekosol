"""ManagedArtifact C5 v1 store reconstructed from the user's explicit handoff contract.

This is NOT recovered B3 source. Contract definition lives in contract.py. Writes must
be invoked through a service that reserves COMMIT_MANAGED_ARTIFACT in Governance first.
"""
from __future__ import annotations

import hashlib
import json
from lifesupply.sqlite_safety import configure_journal
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from lifesupply.fencing import activate_writer_epoch, install_write_guards, register_writer_connection, require_schema_columns, reject_incompatible_existing_schema

OUTCOMES = frozenset({"OK", "NO_CHANGE", "DENY", "CONFLICT", "BLOCKED", "NOT_FOUND", "UNKNOWN"})


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _now() -> float:
    return time.time()


class ManagedArtifactStore:
    """SQLite authority for identities, immutable versions, operations, receipts and bindings."""

    def __init__(self, db_path: str | Path | None = None, *, projection_root: str | Path | None = None,
                 workspace_authority: Any = None, clock=_now, writer_fence: Any = None,
                 read_only: bool = False, db: Any = None):
        if db is None and db_path is None:
            raise ValueError("ManagedArtifactStore requires db_path or db")
        self.db = db
        self.db_path = str(db_path) if db_path is not None else str(db.path)
        self.projection_root = Path(projection_root) if projection_root else None
        self.workspace_authority = workspace_authority
        self.clock = clock
        self.writer_fence = writer_fence
        self.read_only = read_only
        if db is not None:
            # C7 single database: the DB owns the schema, the epochs and the guards.
            if not read_only and self.projection_root:
                self.projection_root.mkdir(parents=True, exist_ok=True)
            self._verify_schema()
        elif not read_only:
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
            reject_incompatible_existing_schema(self.db_path, "ManagedArtifact", "managed_artifacts", {
                "managed_artifacts": {"writer_owner", "writer_instance", "writer_epoch"},
                "artifact_versions": {"version_id", "operation_key", "content_sha256"},
                "artifact_operations": {"operation_key", "request_digest", "result_json"},
                "artifact_projection_outbox": {"artifact_id", "expected_sha256", "state"},
                "c6_writer_epoch": {"active_epoch", "instance_id", "writer_token"}})
            if self.projection_root:
                self.projection_root.mkdir(parents=True, exist_ok=True)
            self._init_db()
        else:
            self._verify_schema()

    def _verify_schema(self) -> None:
        with self._connection() as con:
            require_schema_columns(con, "ManagedArtifact", {
                "managed_artifacts": {"writer_owner", "writer_instance", "writer_epoch"},
                "artifact_versions": {"version_id", "operation_key", "content_sha256"},
                "artifact_operations": {"operation_key", "request_digest", "result_json"},
                "artifact_projection_outbox": {"artifact_id", "expected_sha256", "state"},
                "c6_writer_epoch": {"active_epoch", "instance_id", "writer_token"},
            })

    @contextmanager
    def _connection(self) -> Iterator[Any]:
        if self.db is not None:
            active = self.db.active_unit_of_work
            if active is not None:
                # C7: join the coordinated transaction instead of opening our own.
                yield active.join()
                return
            con = self.db._open()
            try:
                yield con
            finally:
                con.close()
            return
        uri = f"file:{self.db_path}?mode=ro" if self.read_only else self.db_path
        con = sqlite3.connect(uri, uri=self.read_only, timeout=10, isolation_level=None)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys=ON")
        con.execute("PRAGMA busy_timeout=10000")
        register_writer_connection(con, self.writer_fence)
        try:
            yield con
        finally:
            con.close()

    def _assert_writer(self) -> None:
        if self.writer_fence is not None:
            self.writer_fence.assert_current()

    def _init_db(self) -> None:
        if self.db is not None:
            raise RuntimeError(
                "schema DDL belongs to LifeSupplyDB on the single-database layout")
        with self._connection() as con:
            configure_journal(con)
            con.execute("PRAGMA synchronous=FULL")
            con.executescript("""
                CREATE TABLE IF NOT EXISTS schema_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
                INSERT OR IGNORE INTO schema_meta VALUES('version','1');
                CREATE TABLE IF NOT EXISTS c6_writer_epoch(owner TEXT PRIMARY KEY, active_epoch INTEGER NOT NULL, instance_id TEXT NOT NULL, writer_token TEXT NOT NULL DEFAULT 'unfenced-test');
                INSERT OR IGNORE INTO c6_writer_epoch(owner,active_epoch,instance_id,writer_token) VALUES('ManagedArtifact',0,'unfenced-test','unfenced-test');
                CREATE TABLE IF NOT EXISTS managed_artifacts(
                    artifact_id TEXT PRIMARY KEY,
                    subject_id TEXT NOT NULL,
                    workspace_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    lifecycle TEXT NOT NULL CHECK(lifecycle IN ('ACTIVE','ARCHIVED','TOMBSTONED')),
                    head_version_id TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    revision INTEGER NOT NULL DEFAULT 1,
                    meaning_version TEXT NOT NULL,
                    caller_principal TEXT NOT NULL DEFAULT 'service:chiyo-life-supply',
                    writer_owner TEXT NOT NULL DEFAULT 'ManagedArtifact',
                    writer_instance TEXT NOT NULL DEFAULT 'legacy',
                    writer_epoch INTEGER NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS artifacts_subject ON managed_artifacts(subject_id,lifecycle);
                CREATE TABLE IF NOT EXISTS artifact_versions(
                    version_id TEXT PRIMARY KEY,
                    artifact_id TEXT NOT NULL REFERENCES managed_artifacts(artifact_id),
                    version_number INTEGER NOT NULL,
                    parent_version_id TEXT,
                    content TEXT NOT NULL,
                    content_sha256 TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    operation_key TEXT NOT NULL UNIQUE,
                    UNIQUE(artifact_id,version_number)
                );
                CREATE TRIGGER IF NOT EXISTS artifact_versions_no_update
                    BEFORE UPDATE ON artifact_versions BEGIN SELECT RAISE(ABORT,'artifact versions are immutable'); END;
                CREATE TRIGGER IF NOT EXISTS artifact_versions_no_delete
                    BEFORE DELETE ON artifact_versions BEGIN SELECT RAISE(ABORT,'artifact versions are immutable'); END;
                CREATE TABLE IF NOT EXISTS artifact_operations(
                    operation_key TEXT PRIMARY KEY,
                    operation_kind TEXT NOT NULL,
                    artifact_id TEXT NOT NULL,
                    request_digest TEXT NOT NULL,
                    outcome TEXT NOT NULL,
                    result_json TEXT NOT NULL,
                    created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS artifact_effect_receipts(
                    receipt_id TEXT PRIMARY KEY,
                    operation_key TEXT NOT NULL UNIQUE REFERENCES artifact_operations(operation_key),
                    artifact_id TEXT NOT NULL,
                    version_id TEXT,
                    effect_kind TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS artifact_projection_bindings(
                    artifact_id TEXT PRIMARY KEY REFERENCES managed_artifacts(artifact_id),
                    relative_path TEXT NOT NULL UNIQUE,
                    bound_at REAL NOT NULL,
                    projection_sha256 TEXT,
                    projection_state TEXT NOT NULL DEFAULT 'CLEAN'
                );
                CREATE TABLE IF NOT EXISTS artifact_projection_outbox(
                    artifact_id TEXT PRIMARY KEY REFERENCES managed_artifacts(artifact_id),
                    expected_sha256 TEXT NOT NULL,
                    state TEXT NOT NULL CHECK(state IN ('DIRTY','CLEAN')),
                    updated_at REAL NOT NULL
                );
            """)
            require_schema_columns(con, "ManagedArtifact", {
                "managed_artifacts": {"writer_owner", "writer_instance", "writer_epoch"},
                "artifact_versions": {"version_id", "operation_key", "content_sha256"},
                "artifact_operations": {"operation_key", "request_digest", "result_json"},
                "artifact_projection_outbox": {"artifact_id", "expected_sha256", "state"},
            })
            activate_writer_epoch(con, "ManagedArtifact", self.writer_fence)
            install_write_guards(con, "ManagedArtifact", ("managed_artifacts", "artifact_versions", "artifact_operations", "artifact_effect_receipts", "artifact_projection_bindings", "artifact_projection_outbox"))

    def create_artifact(self, *, subject_id: str, workspace_id: str, title: str,
                        operation_key: str, grant_reservation_id: str,
                        operation_payload: Any = None) -> dict[str, Any]:
        self._assert_writer()
        if not grant_reservation_id or self.workspace_authority is None:
            return {"status": "DENY", "detail": "COMMIT_MANAGED_ARTIFACT reservation and Workspace authority required"}
        workspace = self.workspace_authority.get_workspace_by_id(workspace_id)
        if not workspace or workspace["subject_id"] != subject_id or workspace["lifecycle"] != "ACTIVE":
            return {"status": "DENY", "detail": "workspace owner/lifecycle mismatch"}
        expected_payload = {"operation": "create_artifact", "subject_id": subject_id,
            "workspace_id": workspace_id, "title": title.strip()}
        if operation_payload is not None and operation_payload != expected_payload:
            return {"status": "DENY", "detail": "artifact operation payload mismatch"}
        if not all((subject_id, workspace_id, title.strip(), operation_key)):
            return {"status": "DENY", "detail": "required field missing"}
        req = {"subject": subject_id, "workspace": workspace_id, "title": title.strip(),
               "operation_payload": operation_payload}
        digest, now = _digest(req), float(self.clock())
        try:
            with self._connection() as con:
                con.execute("BEGIN IMMEDIATE")
                replay = self._lookup(con, operation_key, digest, "CREATE")
                if replay is not None:
                    con.commit(); return replay
                aid = "artifact:" + uuid.uuid4().hex
                con.execute("""INSERT INTO managed_artifacts(
                    artifact_id,subject_id,workspace_id,title,lifecycle,head_version_id,
                    created_at,updated_at,revision,meaning_version,caller_principal,
                    writer_owner,writer_instance,writer_epoch
                ) VALUES(?,?,?,?,'ACTIVE',NULL,?,?,1,'managed_artifact.v1',?,'ManagedArtifact',?,?)""",
                    (aid, subject_id, workspace_id, title.strip(), now, now,
                     "service:chiyo-life-supply", self.writer_fence.instance_id if self.writer_fence else "legacy",
                     int(self.writer_fence.epoch) if self.writer_fence else 0))
                result = {"status": "OK", "artifact_id": aid, "revision": 1}
                con.execute("INSERT INTO artifact_operations VALUES(?,?,?,?,?,?,?)",
                            (operation_key, "CREATE", aid, digest, result["status"], _json(result), now))
                con.execute("INSERT INTO artifact_effect_receipts VALUES(?,?,?,?,?,?,?)",
                            ("receipt:" + uuid.uuid4().hex, operation_key, aid, None, "CREATE", "APPLIED", now))
                con.commit(); return result
        except sqlite3.IntegrityError:
            return {"status": "CONFLICT", "detail": "artifact operation conflict"}
        except sqlite3.Error:
            return {"status": "UNKNOWN", "detail": "artifact store unavailable"}

    @staticmethod
    def _lookup(con: sqlite3.Connection, operation_key: str, digest: str,
                operation_kind: str | None = None) -> dict[str, Any] | None:
        row = con.execute("SELECT * FROM artifact_operations WHERE operation_key=?", (operation_key,)).fetchone()
        if not row:
            return None
        if row["request_digest"] != digest or (operation_kind and row["operation_kind"] != operation_kind):
            return {"status": "CONFLICT", "detail": "operation key reused with different request"}
        return json.loads(row["result_json"])

    def read_artifact(self, artifact_id: str) -> dict[str, Any] | None:
        try:
            with self._connection() as con:
                row = con.execute("SELECT * FROM managed_artifacts WHERE artifact_id=? AND lifecycle!='TOMBSTONED'", (artifact_id,)).fetchone()
                return dict(row) if row else None
        except sqlite3.Error:
            return None

    def get_artifact_for_subject(self, artifact_id: str, subject_id: str) -> dict[str, Any] | None:
        try:
            with self._connection() as con:
                row = con.execute("SELECT * FROM managed_artifacts WHERE artifact_id=? AND subject_id=?",
                                  (artifact_id, subject_id)).fetchone()
                return dict(row) if row else None
        except sqlite3.Error:
            return None

    def read_head(self, artifact_id: str) -> dict[str, Any] | None:
        try:
            with self._connection() as con:
                row = con.execute("""SELECT v.* FROM managed_artifacts a
                    JOIN artifact_versions v ON v.version_id=a.head_version_id
                    WHERE a.artifact_id=? AND a.lifecycle!='TOMBSTONED'""", (artifact_id,)).fetchone()
                return dict(row) if row else None
        except sqlite3.Error:
            return None

    def read_version(self, version_id: str) -> dict[str, Any] | None:
        try:
            with self._connection() as con:
                row = con.execute("SELECT v.* FROM artifact_versions v JOIN managed_artifacts a ON a.artifact_id=v.artifact_id WHERE v.version_id=? AND a.lifecycle!='TOMBSTONED'", (version_id,)).fetchone()
                return dict(row) if row else None
        except sqlite3.Error:
            return None

    def read_effect_receipt(self, operation_key: str) -> dict[str, Any] | None:
        try:
            with self._connection() as con:
                row = con.execute("SELECT * FROM artifact_effect_receipts WHERE operation_key=?", (operation_key,)).fetchone()
                return dict(row) if row else None
        except sqlite3.Error:
            return None

    def lookup_operation(self, operation_key: str) -> dict[str, Any] | None:
        try:
            with self._connection() as con:
                row = con.execute("SELECT * FROM artifact_operations WHERE operation_key=?", (operation_key,)).fetchone()
                return dict(row) if row else None
        except sqlite3.Error:
            return None

    def commit_markdown(self, *, artifact_id: str, authorized_subject_id: str,
                        expected_head: str | None, content: str, operation_key: str,
                        grant_reservation_id: str, operation_payload: Any = None,
                        operation_kind: str = "COMMIT_MARKDOWN") -> dict[str, Any]:
        self._assert_writer()
        if not grant_reservation_id:
            return {"status": "DENY", "detail": "COMMIT_MANAGED_ARTIFACT grant reservation required"}
        if not operation_key or not isinstance(content, str) or operation_kind != "COMMIT_MARKDOWN":
            return {"status": "DENY", "detail": "operation key/content/kind invalid"}
        expected_payload = {"operation": "commit_markdown", "subject_id": authorized_subject_id,
            "artifact_id": artifact_id, "expected_head": expected_head, "content": content}
        if operation_payload is not None and operation_payload != expected_payload:
            return {"status": "DENY", "detail": "route/action payload mismatch"}
        request = {"artifact_id": artifact_id, "expected_head": expected_head,
                   "content": content, "operation_kind": operation_kind,
                   "operation_payload": operation_payload}
        digest, now = _digest(request), float(self.clock())
        try:
            with self._connection() as con:
                con.execute("BEGIN IMMEDIATE")
                # Frozen rule: operation replay/conflict is decided before any CAS check.
                replay = self._lookup(con, operation_key, digest, "COMMIT_MARKDOWN")
                if replay is not None:
                    # A historical operation replay returns its original receipt, but its old
                    # version must never be projected over a newer canonical head.
                    con.commit()
                    self.rebuild_projection(artifact_id)
                    return replay
                artifact = con.execute("SELECT * FROM managed_artifacts WHERE artifact_id=?", (artifact_id,)).fetchone()
                if artifact is None or artifact["subject_id"] != authorized_subject_id:
                    con.rollback(); return {"status": "NOT_FOUND", "detail": "artifact subject mismatch"}
                if artifact["lifecycle"] != "ACTIVE":
                    con.rollback(); return {"status": "BLOCKED", "detail": "artifact is not active"}
                current_head = artifact["head_version_id"]
                if current_head != expected_head:
                    con.rollback(); return {"status": "CONFLICT", "detail": "stale expected_head", "current_head": current_head}
                if current_head is not None:
                    old = con.execute("SELECT content FROM artifact_versions WHERE version_id=?", (current_head,)).fetchone()
                    if old and old["content"] == content:
                        result = {"status": "NO_CHANGE", "artifact_id": artifact_id,
                                  "head_version_id": current_head, "version_created": False}
                        con.execute("INSERT INTO artifact_operations VALUES(?,?,?,?,?,?,?)",
                                    (operation_key, "COMMIT_MARKDOWN", artifact_id, digest,
                                     result["status"], _json(result), now))
                        con.commit(); return result
                number = int(con.execute("SELECT count(*) FROM artifact_versions WHERE artifact_id=?", (artifact_id,)).fetchone()[0]) + 1
                version_id = "artifact-version:" + uuid.uuid4().hex
                con.execute("INSERT INTO artifact_versions VALUES(?,?,?,?,?,?,?,?)",
                            (version_id, artifact_id, number, current_head, content,
                             hashlib.sha256(content.encode()).hexdigest(), now, operation_key))
                con.execute("UPDATE managed_artifacts SET head_version_id=?,updated_at=?,revision=revision+1 WHERE artifact_id=? AND head_version_id IS ?",
                            (version_id, now, artifact_id, current_head))
                result = {"status": "OK", "artifact_id": artifact_id,
                          "version_id": version_id, "version_number": number,
                          "parent_version_id": current_head, "version_created": True}
                con.execute("INSERT INTO artifact_operations VALUES(?,?,?,?,?,?,?)",
                            (operation_key, "COMMIT_MARKDOWN", artifact_id, digest,
                             result["status"], _json(result), now))
                con.execute("INSERT INTO artifact_effect_receipts VALUES(?,?,?,?,?,?,?)",
                            ("receipt:" + uuid.uuid4().hex, operation_key, artifact_id,
                             version_id, "COMMIT_MARKDOWN", "APPLIED", now))
                con.execute("INSERT INTO artifact_projection_outbox VALUES(?,?, 'DIRTY', ?) ON CONFLICT(artifact_id) DO UPDATE SET expected_sha256=excluded.expected_sha256,state='DIRTY',updated_at=excluded.updated_at",
                            (artifact_id, hashlib.sha256(content.encode()).hexdigest(), now))
                con.commit()
                self._update_projection(artifact_id, content, now)
                return result
        except sqlite3.IntegrityError:
            return {"status": "CONFLICT", "detail": "artifact operation or version conflict"}
        except (sqlite3.Error, OSError):
            return {"status": "UNKNOWN", "detail": "artifact store unavailable"}

    def _update_projection(self, artifact_id: str, content: str, now: float) -> None:
        self._assert_writer()
        if not self.projection_root:
            return
        rel = f"{hashlib.sha256(artifact_id.encode()).hexdigest()[:2]}/{hashlib.sha256(artifact_id.encode()).hexdigest()}.md"
        path = self.projection_root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
        tmp.write_text(content, encoding="utf-8")
        tmp.replace(path)
        # Determine head/version before marking the projection clean. If canonical state moved
        # during I/O, preserve DIRTY so recovery rebuilds the latest head.
        digest = hashlib.sha256(content.encode()).hexdigest()
        with self._connection() as con:
            con.execute("BEGIN IMMEDIATE")
            head = con.execute("SELECT v.content,v.content_sha256 FROM managed_artifacts a JOIN artifact_versions v ON v.version_id=a.head_version_id WHERE a.artifact_id=?", (artifact_id,)).fetchone()
            if head is None or head["content_sha256"] != digest or head["content"] != content:
                con.execute("UPDATE artifact_projection_bindings SET projection_state='DIRTY' WHERE artifact_id=?", (artifact_id,))
                con.execute("UPDATE artifact_projection_outbox SET state='DIRTY',updated_at=? WHERE artifact_id=?", (now, artifact_id))
                con.commit()
                return
            con.execute("INSERT INTO artifact_projection_bindings(artifact_id,relative_path,bound_at,projection_sha256,projection_state) VALUES(?,?,?,?, 'CLEAN') ON CONFLICT(artifact_id) DO UPDATE SET relative_path=excluded.relative_path,bound_at=excluded.bound_at,projection_sha256=excluded.projection_sha256,projection_state='CLEAN'",
                        (artifact_id, rel, now, digest))
            con.execute("INSERT INTO artifact_projection_outbox VALUES(?,?, 'CLEAN', ?) ON CONFLICT(artifact_id) DO UPDATE SET expected_sha256=excluded.expected_sha256,state='CLEAN',updated_at=excluded.updated_at",
                        (artifact_id, digest, now))
            con.commit()

    def check_projection(self, artifact_id: str) -> dict[str, Any]:
        if not self.projection_root:
            return {"status": "UNKNOWN", "detail": "projection root not configured"}
        try:
            with self._connection() as con:
                binding = con.execute("SELECT * FROM artifact_projection_bindings WHERE artifact_id=?", (artifact_id,)).fetchone()
                head = con.execute("SELECT v.* FROM managed_artifacts a JOIN artifact_versions v ON v.version_id=a.head_version_id WHERE a.artifact_id=?", (artifact_id,)).fetchone()
            if not binding or not head:
                return {"status": "NOT_FOUND"}
            path = self.projection_root / binding["relative_path"]
            if not path.is_file():
                return {"status": "CONFLICT", "detail": "projection missing"}
            actual = hashlib.sha256(path.read_bytes()).hexdigest()
            expected = head["content_sha256"]
            return {"status": "OK" if actual == expected else "CONFLICT",
                    "expected_sha256": expected, "actual_sha256": actual}
        except (OSError, sqlite3.Error):
            return {"status": "UNKNOWN", "detail": "projection check unavailable"}

    def recover_dirty_projections(self) -> dict[str, Any]:
        try:
            with self._connection() as con:
                rows = con.execute("SELECT o.artifact_id,v.content FROM artifact_projection_outbox o JOIN managed_artifacts a ON a.artifact_id=o.artifact_id JOIN artifact_versions v ON v.version_id=a.head_version_id WHERE o.state='DIRTY' AND a.lifecycle!='TOMBSTONED'").fetchall()
            recovered = []
            for row in rows:
                self._update_projection(row["artifact_id"], row["content"], float(self.clock()))
                recovered.append(row["artifact_id"])
            return {"status": "OK", "recovered": recovered}
        except (sqlite3.Error, OSError):
            return {"status": "UNKNOWN", "detail": "projection recovery incomplete"}

    def rebuild_projection(self, artifact_id: str) -> dict[str, Any]:
        if not self.projection_root:
            return {"status": "UNKNOWN", "detail": "projection root not configured"}
        try:
            with self._connection() as con:
                row = con.execute("SELECT v.content FROM managed_artifacts a JOIN artifact_versions v ON v.version_id=a.head_version_id WHERE a.artifact_id=? AND a.lifecycle!='TOMBSTONED'", (artifact_id,)).fetchone()
            if row is None:
                return {"status": "NOT_FOUND"}
            self._update_projection(artifact_id, row["content"], float(self.clock()))
            return self.check_projection(artifact_id)
        except sqlite3.Error:
            return {"status": "UNKNOWN", "detail": "artifact store unavailable"}

    def archive(self, artifact_id: str, *, authorized_subject_id: str, expected_revision: int,
                operation_key: str, grant_reservation_id: str, operation_payload: Any = None) -> dict[str, Any]:
        return self._lifecycle(artifact_id, "ARCHIVED", authorized_subject_id, expected_revision, operation_key, grant_reservation_id, operation_payload)

    def tombstone(self, artifact_id: str, *, authorized_subject_id: str, expected_revision: int,
                  operation_key: str, grant_reservation_id: str, operation_payload: Any = None) -> dict[str, Any]:
        return self._lifecycle(artifact_id, "TOMBSTONED", authorized_subject_id, expected_revision, operation_key, grant_reservation_id, operation_payload)

    def _lifecycle(self, artifact_id: str, lifecycle: str, authorized_subject_id: str, expected_revision: int,
                   operation_key: str, grant_reservation_id: str,
                   operation_payload: Any = None) -> dict[str, Any]:
        self._assert_writer()
        if not grant_reservation_id:
            return {"status": "DENY", "detail": "COMMIT_MANAGED_ARTIFACT grant reservation required"}
        expected_operation = "archive_artifact" if lifecycle == "ARCHIVED" else "tombstone_artifact"
        expected_payload = {"operation": expected_operation, "subject_id": authorized_subject_id,
            "artifact_id": artifact_id, "expected_revision": expected_revision}
        if operation_payload is not None and operation_payload != expected_payload:
            return {"status": "DENY", "detail": "route/action payload mismatch"}
        body = {"artifact_id": artifact_id, "lifecycle": lifecycle,
                "expected_revision": expected_revision, "operation_payload": operation_payload,
                "authorized_subject_id": authorized_subject_id}
        digest, now = _digest(body), float(self.clock())
        try:
            with self._connection() as con:
                con.execute("BEGIN IMMEDIATE")
                replay = self._lookup(con, operation_key, digest, lifecycle)
                if replay is not None:
                    con.commit(); return replay
                row = con.execute("SELECT * FROM managed_artifacts WHERE artifact_id=?", (artifact_id,)).fetchone()
                if not row or row["subject_id"] != authorized_subject_id:
                    con.rollback(); return {"status": "NOT_FOUND", "detail": "artifact subject mismatch"}
                if row["revision"] != expected_revision:
                    con.rollback(); return {"status": "CONFLICT", "revision": row["revision"]}
                if row["lifecycle"] == lifecycle:
                    result = {"status": "NO_CHANGE", "artifact_id": artifact_id,
                              "lifecycle": lifecycle, "revision": expected_revision}
                    con.execute("INSERT INTO artifact_operations VALUES(?,?,?,?,?,?,?)",
                                (operation_key, lifecycle, artifact_id, digest, result["status"], _json(result), now))
                    con.commit(); return result
                if row["lifecycle"] == "TOMBSTONED":
                    con.rollback(); return {"status": "BLOCKED", "detail": "terminal tombstone"}
                con.execute("UPDATE managed_artifacts SET lifecycle=?,revision=revision+1,updated_at=? WHERE artifact_id=? AND revision=?",
                            (lifecycle, now, artifact_id, expected_revision))
                result = {"status": "OK", "artifact_id": artifact_id, "lifecycle": lifecycle,
                          "revision": expected_revision + 1}
                con.execute("INSERT INTO artifact_operations VALUES(?,?,?,?,?,?,?)",
                            (operation_key, lifecycle, artifact_id, digest, result["status"], _json(result), now))
                con.execute("INSERT INTO artifact_effect_receipts VALUES(?,?,?,?,?,?,?)",
                            ("receipt:" + uuid.uuid4().hex, operation_key, artifact_id,
                             row["head_version_id"], lifecycle, "APPLIED", now))
                con.commit(); return result
        except sqlite3.IntegrityError:
            return {"status": "CONFLICT", "detail": "operation key conflict"}
        except sqlite3.Error:
            return {"status": "UNKNOWN", "detail": "artifact store unavailable"}

    def list_artifacts(self, subject_id: str) -> list[dict[str, Any]] | None:
        try:
            with self._connection() as con:
                rows = con.execute("SELECT * FROM managed_artifacts WHERE subject_id=? AND lifecycle!='TOMBSTONED' ORDER BY created_at", (subject_id,)).fetchall()
                return [dict(row) for row in rows]
        except sqlite3.Error:
            return None

    def artifact_count(self) -> int | None:
        try:
            with self._connection() as con:
                return int(con.execute("SELECT count(*) FROM managed_artifacts WHERE lifecycle!='TOMBSTONED'").fetchone()[0])
        except sqlite3.Error:
            return None
