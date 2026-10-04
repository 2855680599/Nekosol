"""Workspace semantic authority: PersonalWorkspaceCapability and OpenInquiry only."""
from __future__ import annotations

import hashlib
import json
from lifesupply.sqlite_safety import configure_journal
import sqlite3
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Sequence

from lifesupply.fencing import activate_writer_epoch, install_write_guards, register_writer_connection, require_schema_columns, reject_incompatible_existing_schema

OPEN_STATES = frozenset({"OPEN", "PAUSED", "ANSWERED", "ABANDONED", "EXPIRED"})
DRIVERS = frozenset({"SELF_INITIATED", "USER_SPARKED", "SHARED", "SYSTEM", "OTHER"})
ADMISSION_OUTCOMES = frozenset({"ACCEPT", "REJECT", "MERGE", "BLOCK"})
#: The one meaning version an OpenInquiry row may carry today.
INQUIRY_MEANING_VERSION = "workspace.open_inquiry.v1"


def _now() -> float:
    return time.time()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


class ProposalValidators:
    """C10 PHASE 05 -- the trusted validator port for OpenInquiry admission.

    C6 refused to trust a caller's ``source_exists`` / ``scope_allowed`` / ``within_quota``
    booleans, and it was right: a boolean in a request is not evidence.  The store consults an
    injected port instead.  With no port wired the store stays fail-closed, which is the
    production default (``INQUIRY_ADMISSION = OFF``); an isolated run injects an explicitly
    marked test source port.

    An implementation that cannot answer from evidence must return ``False``, never guess.
    """

    def source_exists(self, *, subject_id: str, topic: str, source_refs: Sequence[str]) -> bool:
        raise NotImplementedError

    def scope_allowed(self, *, subject_id: str, workspace_id: str, topic: str) -> bool:
        raise NotImplementedError

    def within_quota(self, *, subject_id: str, workspace_id: str) -> bool:
        raise NotImplementedError


class WorkspaceStore:
    """SQLite-backed Workspace authority. Access control is checked by service boundary."""

    def __init__(self, db_path: str | Path | None = None, *, clock=_now, writer_fence: Any = None,
                 read_only: bool = False, db: Any = None, validators: Any = None):
        if db is None and db_path is None:
            raise ValueError("WorkspaceStore requires db_path or db")
        self.validators = validators
        self.db = db
        self.db_path = str(db_path) if db_path is not None else str(db.path)
        self.clock = clock
        self.writer_fence = writer_fence
        self.read_only = read_only
        if db is not None:
            # C7 single database: the DB owns the schema, the epochs and the guards.
            self._verify_schema()
        elif not read_only:
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
            reject_incompatible_existing_schema(self.db_path, "Workspace", "workspaces", {
                "workspaces": {"writer_owner", "writer_instance", "writer_epoch", "operation_id"},
                "operation_keys": {"operation_key", "request_digest", "result_json"},
                "c6_writer_epoch": {"active_epoch", "instance_id", "writer_token"}})
            self._init_db()
        else:
            self._verify_schema()

    def _verify_schema(self) -> None:
        with self._connection() as con:
            require_schema_columns(con, "Workspace", {
                "workspaces": {"writer_owner", "writer_instance", "writer_epoch", "operation_id"},
                "operation_keys": {"operation_key", "request_digest", "result_json"},
                "c6_writer_epoch": {"active_epoch", "instance_id", "writer_token"},
            })

    def _assert_writer(self) -> None:
        if self.writer_fence is not None:
            self.writer_fence.assert_current()

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

    def _init_db(self) -> None:
        if self.db is not None:
            raise RuntimeError(
                "schema DDL belongs to LifeSupplyDB on the single-database layout")
        with self._connection() as con:
            configure_journal(con)
            con.execute("PRAGMA synchronous=FULL")
            con.executescript("""
                CREATE TABLE IF NOT EXISTS workspaces (
                    workspace_id TEXT PRIMARY KEY,
                    subject_id TEXT NOT NULL UNIQUE,
                    lifecycle TEXT NOT NULL CHECK(lifecycle IN ('ACTIVE','SUSPENDED','REVOKED')),
                    visibility TEXT NOT NULL,
                    artifact_scope TEXT NOT NULL,
                    created_by_grant TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    meaning_version TEXT NOT NULL,
                    revision INTEGER NOT NULL DEFAULT 1,
                    caller_principal TEXT NOT NULL DEFAULT 'service:chiyo-life-supply',
                    writer_owner TEXT NOT NULL DEFAULT 'Workspace',
                    writer_instance TEXT NOT NULL DEFAULT 'legacy',
                    writer_epoch INTEGER NOT NULL DEFAULT 0,
                    operation_id TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS inquiry_proposals (
                    proposal_id TEXT PRIMARY KEY,
                    workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id),
                    subject_id TEXT NOT NULL,
                    topic TEXT NOT NULL,
                    source_refs TEXT NOT NULL,
                    driver TEXT NOT NULL,
                    review_after REAL,
                    expires_at REAL,
                    admission_status TEXT NOT NULL CHECK(admission_status IN ('PENDING','ACCEPT','REJECT','MERGE','BLOCK')),
                    admission_reason TEXT,
                    created_at REAL NOT NULL,
                    UNIQUE(workspace_id, topic)
                );
                CREATE TABLE IF NOT EXISTS open_inquiries (
                    inquiry_id TEXT PRIMARY KEY,
                    workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id),
                    subject_id TEXT NOT NULL,
                    topic TEXT NOT NULL,
                    source_refs TEXT NOT NULL,
                    driver TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('OPEN','PAUSED','ANSWERED','ABANDONED','EXPIRED')),
                    review_after REAL,
                    expires_at REAL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    revision INTEGER NOT NULL DEFAULT 1,
                    meaning_version TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS inquiries_subject_status ON open_inquiries(subject_id,status);
                CREATE TABLE IF NOT EXISTS admission_results (
                    result_id TEXT PRIMARY KEY,
                    proposal_id TEXT NOT NULL UNIQUE REFERENCES inquiry_proposals(proposal_id),
                    outcome TEXT NOT NULL CHECK(outcome IN ('ACCEPT','REJECT','MERGE','BLOCK')),
                    reason TEXT NOT NULL,
                    inquiry_id TEXT,
                    created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS operation_keys (
                    operation_key TEXT PRIMARY KEY,
                    request_digest TEXT NOT NULL,
                    status TEXT NOT NULL,
                    result_json TEXT NOT NULL,
                    created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS c6_writer_epoch(owner TEXT PRIMARY KEY, active_epoch INTEGER NOT NULL, instance_id TEXT NOT NULL, writer_token TEXT NOT NULL DEFAULT 'unfenced-test');
                INSERT OR IGNORE INTO c6_writer_epoch(owner,active_epoch,instance_id,writer_token) VALUES('Workspace',0,'unfenced-test','unfenced-test');
            """)
            require_schema_columns(con, "Workspace", {
                "workspaces": {"writer_owner", "writer_instance", "writer_epoch", "operation_id"},
                "operation_keys": {"operation_key", "request_digest", "result_json"},
            })
            activate_writer_epoch(con, "Workspace", self.writer_fence)
            install_write_guards(con, "Workspace", ("workspaces", "inquiry_proposals", "open_inquiries", "admission_results", "operation_keys"))

    @staticmethod
    def _replay(con: sqlite3.Connection, key: str, digest: str) -> dict[str, Any] | None:
        row = con.execute("SELECT * FROM operation_keys WHERE operation_key=?", (key,)).fetchone()
        if row is None:
            return None
        if row["request_digest"] != digest:
            return {"status": "CONFLICT", "detail": "operation key payload mismatch"}
        return json.loads(row["result_json"])

    def provision_workspace(self, *, subject_id: str, grant_id: str,
                            operation_key: str, visibility: str = "PRIVATE",
                            artifact_scope: str = "PERSONAL",
                            meaning_version: str = "workspace.capability.v1") -> dict[str, Any]:
        """Persist capability fact. Caller must have reserved its exact Governance grant."""
        self._assert_writer()
        if not all((subject_id, grant_id, operation_key)):
            return {"status": "DENY", "detail": "subject, grant, operation key required"}
        request = {"op": "PROVISION", "subject": subject_id, "grant": grant_id,
                   "visibility": visibility, "artifact_scope": artifact_scope}
        digest, now = _digest(request), float(self.clock())
        try:
            with self._connection() as con:
                con.execute("BEGIN IMMEDIATE")
                replay = self._replay(con, operation_key, digest)
                if replay is not None:
                    con.commit(); return replay
                row = con.execute("SELECT * FROM workspaces WHERE subject_id=?", (subject_id,)).fetchone()
                if row:
                    result = {"status": "NO_CHANGE", "workspace": dict(row)}
                else:
                    wid = "workspace:" + uuid.uuid4().hex
                    con.execute("""INSERT INTO workspaces(
                        workspace_id,subject_id,lifecycle,visibility,artifact_scope,
                        created_by_grant,created_at,updated_at,meaning_version,revision,
                        caller_principal,writer_owner,writer_instance,writer_epoch,operation_id
                    ) VALUES(?,?,'ACTIVE',?,?,?,?,?,?,1,?,?,?,?,?)""",
                        (wid, subject_id, visibility, artifact_scope, grant_id,
                         now, now, meaning_version, "service:chiyo-life-supply", "Workspace",
                         self.writer_fence.instance_id if self.writer_fence else "legacy",
                         int(self.writer_fence.epoch) if self.writer_fence else 0, operation_key))
                    row = con.execute("SELECT * FROM workspaces WHERE workspace_id=?", (wid,)).fetchone()
                    result = {"status": "OK", "workspace": dict(row)}
                if row and self.writer_fence:
                    writer_epoch = int(self.writer_fence.epoch)
                    writer_instance = self.writer_fence.instance_id
                    # C10 PHASE 03/04: refresh the writer stamp only.  `operation_id` is
                    # creation provenance, not a writer stamp: the NO_CHANGE path must not
                    # rewrite the workspace's creator operation, or a duplicate provision could
                    # satisfy a postcondition that has to be bound to THIS operation.
                    con.execute("UPDATE workspaces SET writer_epoch=?,writer_instance=? WHERE workspace_id=?",
                        (writer_epoch, writer_instance, row["workspace_id"]))
                    row = con.execute("SELECT * FROM workspaces WHERE workspace_id=?", (row["workspace_id"],)).fetchone()
                    result["workspace"] = dict(row)
                elif row:
                    result["workspace"] = dict(row)
                con.execute("INSERT INTO operation_keys VALUES(?,?,?,?,?)",
                            (operation_key, digest, result["status"], _json(result), now))
                con.commit(); return result
        except sqlite3.IntegrityError:
            return {"status": "CONFLICT", "detail": "workspace identity or operation key conflict"}
        except sqlite3.Error:
            return {"status": "UNKNOWN", "detail": "workspace store unavailable"}

    def lookup_operation(self, operation_id: str) -> dict[str, Any] | None:
        try:
            with self._connection() as con:
                row = con.execute("SELECT * FROM operation_keys WHERE operation_key=?", (operation_id,)).fetchone()
                return dict(row) if row else None
        except sqlite3.Error:
            return None

    def get_workspace(self, subject_id: str) -> dict[str, Any] | None:
        try:
            with self._connection() as con:
                row = con.execute("SELECT * FROM workspaces WHERE subject_id=?", (subject_id,)).fetchone()
                return dict(row) if row else None
        except sqlite3.Error:
            return None

    def get_workspace_by_id(self, workspace_id: str) -> dict[str, Any] | None:
        try:
            with self._connection() as con:
                row = con.execute("SELECT * FROM workspaces WHERE workspace_id=?", (workspace_id,)).fetchone()
                return dict(row) if row else None
        except sqlite3.Error:
            return None

    def get_proposal(self, proposal_id: str) -> dict[str, Any] | None:
        try:
            with self._connection() as con:
                row = con.execute("SELECT * FROM inquiry_proposals WHERE proposal_id=?", (proposal_id,)).fetchone()
                return dict(row) if row else None
        except sqlite3.Error:
            return None

    def submit_proposal(self, *, subject_id: str, topic: str, source_refs: Sequence[str],
                        driver: str, review_after: float | None, expires_at: float | None,
                        operation_key: str) -> dict[str, Any]:
        """Stage one OpenInquiry proposal.  A proposal is not an inquiry until it is admitted.

        Fail-closed order: identity, then the workspace, then provenance.  The caller may not
        assert that its own sources exist -- only the injected validator port may.
        """
        self._assert_writer()
        if not all((subject_id, (topic or "").strip(), operation_key)):
            return {"status": "DENY", "detail": "subject, topic and operation key required"}
        if driver not in DRIVERS:
            return {"status": "DENY", "detail": "unknown driver"}
        refs = [str(ref).strip() for ref in (source_refs or [])]
        if not refs or not all(refs):
            return {"status": "DENY", "detail": "source refs required"}
        if self.validators is None:
            return {"status": "BLOCKED",
                    "detail": "trusted proposal validator not staged (INQUIRY_ADMISSION=OFF)"}
        if not self.validators.source_exists(subject_id=subject_id, topic=topic, source_refs=refs):
            return {"status": "BLOCKED", "detail": "source provenance not established"}
        request = {"op": "SUBMIT_PROPOSAL", "subject": subject_id, "topic": topic,
                   "source_refs": refs, "driver": driver, "review_after": review_after,
                   "expires_at": expires_at}
        digest, now = _digest(request), float(self.clock())
        try:
            with self._connection() as con:
                con.execute("BEGIN IMMEDIATE")
                replay = self._replay(con, operation_key, digest)
                if replay is not None:
                    con.commit(); return replay
                workspace = con.execute("SELECT * FROM workspaces WHERE subject_id=?",
                                        (subject_id,)).fetchone()
                if workspace is None:
                    con.rollback(); return {"status": "DENY", "detail": "no workspace for subject"}
                if workspace["lifecycle"] != "ACTIVE":
                    con.rollback(); return {"status": "BLOCKED", "detail": "workspace is not ACTIVE"}
                existing = con.execute(
                    "SELECT * FROM inquiry_proposals WHERE workspace_id=? AND topic=?",
                    (workspace["workspace_id"], topic)).fetchone()
                if existing is not None:
                    # dedup is the workspace's own uniqueness rule, not a caller claim
                    result = {"status": "NO_CHANGE", "proposal": dict(existing)}
                else:
                    proposal_id = "proposal:" + uuid.uuid4().hex
                    con.execute(
                        "INSERT INTO inquiry_proposals(proposal_id,workspace_id,subject_id,topic,"
                        "source_refs,driver,review_after,expires_at,admission_status,"
                        "admission_reason,created_at) VALUES(?,?,?,?,?,?,?,?,'PENDING',NULL,?)",
                        (proposal_id, workspace["workspace_id"], subject_id, topic, _json(refs),
                         driver, review_after, expires_at, now))
                    row = con.execute("SELECT * FROM inquiry_proposals WHERE proposal_id=?",
                                      (proposal_id,)).fetchone()
                    result = {"status": "OK", "proposal": dict(row)}
                con.execute("INSERT INTO operation_keys VALUES(?,?,?,?,?)",
                            (operation_key, digest, result["status"], _json(result), now))
                con.commit(); return result
        except sqlite3.IntegrityError:
            return {"status": "CONFLICT", "detail": "proposal identity conflict"}
        except sqlite3.Error:
            return {"status": "UNKNOWN", "detail": "workspace store unavailable"}

    def admit_proposal(self, *, proposal_id: str, authorized_subject_id: str,
                       authorized_workspace_id: str, governance_reservation_id: str | None,
                       operation_key: str | None = None,
                       source_exists: bool | None = None, scope_allowed: bool | None = None,
                       within_quota: bool | None = None) -> dict[str, Any]:
        """Admit a staged proposal into a real OpenInquiry.

        The legacy ``source_exists`` / ``scope_allowed`` / ``within_quota`` arguments are kept
        for call compatibility but never consulted: C10 PHASE 05 replaces caller claims with the
        injected validator port, and the grant must be a CONSUMED Governance reservation read
        back out of the store, not a string the caller supplied.
        """
        del source_exists, scope_allowed, within_quota
        self._assert_writer()
        if not all((proposal_id, authorized_subject_id, authorized_workspace_id)):
            return {"status": "DENY", "detail": "proposal, subject and workspace required"}
        if self.validators is None:
            return {"status": "BLOCKED",
                    "detail": "trusted admission validators not staged (INQUIRY_ADMISSION=OFF)"}
        if not governance_reservation_id:
            return {"status": "DENY", "detail": "consumed governance reservation required"}
        now = float(self.clock())
        try:
            with self._connection() as con:
                con.execute("BEGIN IMMEDIATE")
                reservation = con.execute(
                    "SELECT state,grant_id FROM permission_reservations WHERE reservation_id=?",
                    (governance_reservation_id,)).fetchone()
                if reservation is None or reservation["state"] != "CONSUMED":
                    con.rollback()
                    return {"status": "DENY", "detail": "reservation is not a consumed grant"}
                proposal = con.execute("SELECT * FROM inquiry_proposals WHERE proposal_id=?",
                                       (proposal_id,)).fetchone()
                if proposal is None:
                    con.rollback(); return {"status": "NOT_FOUND", "detail": "proposal missing"}
                if proposal["subject_id"] != authorized_subject_id:
                    con.rollback(); return {"status": "DENY", "detail": "proposal subject mismatch"}
                if proposal["workspace_id"] != authorized_workspace_id:
                    con.rollback(); return {"status": "DENY", "detail": "proposal workspace mismatch"}
                if proposal["admission_status"] != "PENDING":
                    con.rollback()
                    return {"status": "NO_CHANGE",
                            "detail": f"proposal already {proposal['admission_status']}",
                            "proposal": dict(proposal)}
                workspace = con.execute("SELECT * FROM workspaces WHERE workspace_id=?",
                                        (authorized_workspace_id,)).fetchone()
                if workspace is None or workspace["lifecycle"] != "ACTIVE":
                    con.rollback()
                    return {"status": "BLOCKED", "detail": "workspace is not ACTIVE"}
                refs = json.loads(proposal["source_refs"])
                if not self.validators.source_exists(subject_id=authorized_subject_id,
                                                     topic=proposal["topic"], source_refs=refs):
                    con.rollback()
                    return {"status": "BLOCKED", "detail": "source provenance not established"}
                if not self.validators.scope_allowed(subject_id=authorized_subject_id,
                                                     workspace_id=authorized_workspace_id,
                                                     topic=proposal["topic"]):
                    con.rollback(); return {"status": "BLOCKED", "detail": "scope not allowed"}
                if not self.validators.within_quota(subject_id=authorized_subject_id,
                                                    workspace_id=authorized_workspace_id):
                    con.rollback()
                    return {"status": "BLOCKED", "detail": "inquiry quota exhausted"}
                already_open = con.execute(
                    "SELECT * FROM open_inquiries WHERE workspace_id=? AND topic=?"
                    " AND status IN ('OPEN','PAUSED')",
                    (authorized_workspace_id, proposal["topic"])).fetchone()
                if already_open is not None:
                    # dedup: this topic is already open, so the proposal MERGES instead of
                    # opening a second inquiry for the same subject
                    con.execute("UPDATE inquiry_proposals SET admission_status='MERGE',"
                                "admission_reason=? WHERE proposal_id=?",
                                (f"existing inquiry {already_open['inquiry_id']}", proposal_id))
                    result = {"status": "NO_CHANGE", "outcome": "MERGE",
                              "proposal_id": proposal_id, "inquiry": dict(already_open)}
                else:
                    inquiry_id = "inquiry:" + uuid.uuid4().hex
                    con.execute(
                        "INSERT INTO open_inquiries(inquiry_id,workspace_id,subject_id,topic,"
                        "source_refs,driver,status,review_after,expires_at,created_at,updated_at,"
                        "revision,meaning_version) VALUES(?,?,?,?,?,?,'OPEN',?,?,?,?,1,?)",
                        (inquiry_id, authorized_workspace_id, authorized_subject_id,
                         proposal["topic"], proposal["source_refs"], proposal["driver"],
                         proposal["review_after"], proposal["expires_at"], now, now,
                         INQUIRY_MEANING_VERSION))
                    con.execute("UPDATE inquiry_proposals SET admission_status='ACCEPT',"
                                "admission_reason='admitted' WHERE proposal_id=?", (proposal_id,))
                    row = con.execute("SELECT * FROM open_inquiries WHERE inquiry_id=?",
                                      (inquiry_id,)).fetchone()
                    result = {"status": "OK", "outcome": "ACCEPT", "proposal_id": proposal_id,
                              "inquiry": dict(row)}
                if operation_key:
                    con.execute("INSERT INTO operation_keys VALUES(?,?,?,?,?)",
                                (operation_key,
                                 _digest({"op": "ADMIT_INQUIRY", "proposal": proposal_id}),
                                 result["status"], _json(result), now))
                con.commit(); return result
        except sqlite3.IntegrityError:
            return {"status": "CONFLICT", "detail": "inquiry identity conflict"}
        except sqlite3.Error:
            return {"status": "UNKNOWN", "detail": "workspace store unavailable"}

    def list_active_inquiries_view(self, subject_id: str) -> list[dict[str, Any]] | None:
        """Read expiry as eligibility, without changing canonical lifecycle state."""
        try:
            with self._connection() as con:
                rows = con.execute("SELECT * FROM open_inquiries WHERE subject_id=? AND status='OPEN' "
                    "AND (expires_at IS NULL OR expires_at>?) ORDER BY created_at,inquiry_id",
                    (subject_id, float(self.clock()))).fetchall()
                return [dict(row) for row in rows]
        except sqlite3.Error:
            return None

    def personal_opportunities_view(self, subject_id: str) -> list[dict[str, Any]] | None:
        rows = self.list_active_inquiries_view(subject_id)
        workspace = self.get_workspace(subject_id)
        if rows is None:
            return None
        if not workspace or workspace.get('lifecycle') != 'ACTIVE' or workspace.get('visibility') != 'PRIVATE':
            return []
        iso = lambda value: datetime.fromtimestamp(float(value), timezone.utc).isoformat()
        return [{"kind": "OPEN_INQUIRY", "inquiry": row,
                 "opportunity_id": row['inquiry_id'], "subject_id": row['subject_id'],
                 "source_ref": row['inquiry_id'], "source_kind": "PERSONAL_OPPORTUNITY",
                 "availability": "OPEN", "visibility": workspace['visibility'],
                 "valid_from": iso(row['created_at']),
                 "valid_until": iso(row['expires_at']) if row['expires_at'] is not None else None,
                 "source_revision": row['revision']}
                for row in rows if row['workspace_id'] == workspace['workspace_id']]

    def list_open_inquiries(self, subject_id: str) -> list[dict[str, Any]] | None:
        try:
            self._assert_writer()
            with self._connection() as con:
                now = float(self.clock())
                con.execute("UPDATE open_inquiries SET status='EXPIRED',revision=revision+1,updated_at=? WHERE subject_id=? AND status IN ('OPEN','PAUSED') AND expires_at IS NOT NULL AND expires_at<=?", (now, subject_id, now))
                rows = con.execute("SELECT * FROM open_inquiries WHERE subject_id=? AND status='OPEN' ORDER BY created_at,inquiry_id", (subject_id,)).fetchall()
                return [dict(row) for row in rows]
        except sqlite3.Error:
            return None

    def _transition(self, inquiry_id: str, expected_revision: int, status: str) -> dict[str, Any]:
        self._assert_writer()
        if status not in OPEN_STATES:
            return {"status": "DENY", "detail": "invalid state"}
        now = float(self.clock())
        try:
            with self._connection() as con:
                con.execute("BEGIN IMMEDIATE")
                row = con.execute("SELECT * FROM open_inquiries WHERE inquiry_id=?", (inquiry_id,)).fetchone()
                if not row:
                    con.rollback(); return {"status": "NOT_FOUND"}
                if int(row["revision"]) != int(expected_revision):
                    con.rollback(); return {"status": "CONFLICT", "revision": int(row["revision"])}
                if row["status"] in {"ANSWERED", "ABANDONED", "EXPIRED"}:
                    con.rollback(); return {"status": "BLOCKED", "detail": "terminal inquiry"}
                con.execute("UPDATE open_inquiries SET status=?,revision=revision+1,updated_at=? WHERE inquiry_id=? AND revision=?",
                            (status, now, inquiry_id, expected_revision))
                updated = con.execute("SELECT * FROM open_inquiries WHERE inquiry_id=?", (inquiry_id,)).fetchone()
                con.commit(); return {"status": "OK", "inquiry": dict(updated)}
        except sqlite3.Error:
            return {"status": "UNKNOWN", "detail": "workspace store unavailable"}

    def pause_inquiry(self, inquiry_id: str, expected_revision: int) -> dict[str, Any]:
        return self._transition(inquiry_id, expected_revision, "PAUSED")

    def answer_inquiry(self, inquiry_id: str, expected_revision: int) -> dict[str, Any]:
        return self._transition(inquiry_id, expected_revision, "ANSWERED")

    def abandon_inquiry(self, inquiry_id: str, expected_revision: int) -> dict[str, Any]:
        return self._transition(inquiry_id, expected_revision, "ABANDONED")

    def expire_inquiries(self) -> int | None:
        self._assert_writer()
        now = float(self.clock())
        try:
            with self._connection() as con:
                cur = con.execute("UPDATE open_inquiries SET status='EXPIRED',revision=revision+1,updated_at=? WHERE status IN ('OPEN','PAUSED') AND expires_at IS NOT NULL AND expires_at<=?", (now, now))
                return cur.rowcount
        except sqlite3.Error:
            return None

    def workspace_count(self) -> int | None:
        try:
            with self._connection() as con:
                return int(con.execute("SELECT count(*) FROM workspaces").fetchone()[0])
        except sqlite3.Error:
            return None
