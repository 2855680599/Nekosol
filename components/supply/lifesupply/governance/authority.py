"""Durable, narrowly scoped Governance authority.

C5 reconstruction from the user's explicit PermissionGrant v1 contract. This is a
standalone semantic owner and never borrows the Life Runtime writer lease. Unknown
and every non-ALLOW outcome are fail-closed.
"""
from __future__ import annotations

import hashlib
import json
from lifesupply.sqlite_safety import configure_journal
import sqlite3
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from lifesupply.fencing import activate_writer_epoch, install_write_guards, register_writer_connection, require_schema_columns, reject_incompatible_existing_schema

CAPABILITIES = frozenset({
    "PROVISION_PERSONAL_WORKSPACE",
    "WRITE_OPEN_INQUIRY",
    "COMMIT_MANAGED_ARTIFACT",
})
USE_MODES = frozenset({"ONE_SHOT", "REUSABLE"})
ALLOW = "ALLOW"
DENY = "DENY"
EXPIRED = "EXPIRED"
REVOKED = "REVOKED"
SCOPE_MISMATCH = "SCOPE_MISMATCH"
TARGET_MISMATCH = "TARGET_MISMATCH"
CAPABILITY_MISMATCH = "CAPABILITY_MISMATCH"
USE_EXHAUSTED = "USE_EXHAUSTED"
STALE_REVISION = "STALE_REVISION"
UNKNOWN = "UNKNOWN"
CONFLICT = "CONFLICT"
NO_CHANGE = "NO_CHANGE"
OK = "OK"


def _now() -> float:
    return time.time()


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class GrantDecision:
    status: str
    grant_id: str | None = None
    revision: int | None = None
    reservation_id: str | None = None
    detail: str = ""

    @property
    def allowed(self) -> bool:
        return self.status == ALLOW

    def to_dict(self) -> dict[str, Any]:
        return {"status": self.status, "grant_id": self.grant_id,
                "revision": self.revision, "reservation_id": self.reservation_id,
                "detail": self.detail}


class GovernanceAuthority:
    """SQLite-backed PermissionGrant and PermissionReservation owner."""

    def __init__(self, db_path: str | Path | None = None, *, clock=_now, writer_fence: Any = None,
                 read_only: bool = False, db: Any = None):
        if db is None and db_path is None:
            raise ValueError("GovernanceAuthority requires db_path or db")
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
            reject_incompatible_existing_schema(self.db_path, "Governance", "permission_grants", {
                "permission_grants": {"writer_owner", "writer_instance", "writer_epoch", "operation_id"},
                "permission_reservations": {"execution_started_at"},
                "authority_operations": {"operation_id", "request_digest", "result_json"},
                "c6_writer_epoch": {"active_epoch", "instance_id", "writer_token"}})
            self._init_db()
        else:
            self._verify_schema()

    def _verify_schema(self) -> None:
        with self._connection() as con:
            require_schema_columns(con, "Governance", {
                "permission_grants": {"writer_owner", "writer_instance", "writer_epoch", "operation_id"},
                "permission_reservations": {"execution_started_at"},
                "authority_operations": {"operation_id", "request_digest", "result_json"},
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
                CREATE TABLE IF NOT EXISTS schema_meta (
                    key TEXT PRIMARY KEY, value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS permission_grants (
                    grant_id TEXT PRIMARY KEY,
                    grantor TEXT NOT NULL,
                    subject TEXT NOT NULL,
                    capability TEXT NOT NULL,
                    scope TEXT NOT NULL,
                    target TEXT NOT NULL,
                    payload_binding TEXT,
                    valid_from REAL NOT NULL,
                    expires_at REAL,
                    revision INTEGER NOT NULL CHECK(revision > 0),
                    revocation_state TEXT NOT NULL CHECK(revocation_state IN ('ACTIVE','REVOKED','EXPIRED')),
                    revoked_at REAL,
                    use_mode TEXT NOT NULL CHECK(use_mode IN ('ONE_SHOT','REUSABLE')),
                    max_uses INTEGER,
                    uses INTEGER NOT NULL DEFAULT 0 CHECK(uses >= 0),
                    delegation_allowed INTEGER NOT NULL CHECK(delegation_allowed IN (0,1)),
                    authority_scope TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    meaning_version TEXT NOT NULL,
                    caller_principal TEXT NOT NULL DEFAULT 'service:chiyo-life-supply',
                    writer_owner TEXT NOT NULL DEFAULT 'Governance',
                    writer_instance TEXT NOT NULL DEFAULT 'legacy',
                    writer_epoch INTEGER NOT NULL DEFAULT 0,
                    operation_id TEXT NOT NULL DEFAULT '',
                    CHECK(max_uses IS NULL OR max_uses >= 0),
                    CHECK(use_mode != 'ONE_SHOT' OR max_uses = 1),
                    CHECK(revocation_state != 'REVOKED' OR revoked_at IS NOT NULL)
                );
                CREATE INDEX IF NOT EXISTS grants_subject_cap_target
                    ON permission_grants(subject, capability, target, revocation_state);
                CREATE TABLE IF NOT EXISTS authority_operations(
                    operation_id TEXT PRIMARY KEY, request_digest TEXT NOT NULL,
                    result_json TEXT NOT NULL, created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS permission_reservations (
                    reservation_id TEXT PRIMARY KEY,
                    grant_id TEXT NOT NULL REFERENCES permission_grants(grant_id),
                    action_key TEXT NOT NULL,
                    action_digest TEXT NOT NULL,
                    grant_revision INTEGER NOT NULL,
                    reservation_owner_uid INTEGER,
                    execution_started_at REAL,
                    state TEXT NOT NULL CHECK(state IN ('RESERVED','EXECUTING','CONSUMED','RELEASED','NO_EFFECT')),
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    UNIQUE(grant_id, action_key)
                );
                CREATE TABLE IF NOT EXISTS c6_writer_epoch(owner TEXT PRIMARY KEY, active_epoch INTEGER NOT NULL, instance_id TEXT NOT NULL, writer_token TEXT NOT NULL DEFAULT 'unfenced-test');
                INSERT OR IGNORE INTO c6_writer_epoch(owner,active_epoch,instance_id,writer_token) VALUES('Governance',0,'unfenced-test','unfenced-test');
                CREATE INDEX IF NOT EXISTS reservations_grant_state
                    ON permission_reservations(grant_id, state);
                INSERT OR IGNORE INTO schema_meta(key,value) VALUES('version','1');
            """)
            require_schema_columns(con, "Governance", {
                "permission_grants": {"writer_owner", "writer_instance", "writer_epoch", "operation_id"},
                "permission_reservations": {"execution_started_at"},
                "authority_operations": {"operation_id", "request_digest", "result_json"},
            })
            activate_writer_epoch(con, "Governance", self.writer_fence)
            install_write_guards(con, "Governance", ("permission_grants", "permission_reservations", "authority_operations"))

    @staticmethod
    def _check_row(row: sqlite3.Row, *, now: float, subject: str, capability: str,
                   scope: str, target: str, expected_revision: int | None,
                   payload: Any, con: sqlite3.Connection,
                   action_key: str | None = None,
                   authority_scope: str | None = None) -> GrantDecision:
        gid = row["grant_id"]
        rev = int(row["revision"])
        if expected_revision is not None and rev != int(expected_revision):
            return GrantDecision(STALE_REVISION, gid, rev)
        if row["revocation_state"] == "REVOKED":
            return GrantDecision(REVOKED, gid, rev)
        if row["revocation_state"] == "EXPIRED" or (row["expires_at"] is not None and now >= row["expires_at"]):
            return GrantDecision(EXPIRED, gid, rev)
        if now < row["valid_from"]:
            return GrantDecision(DENY, gid, rev, detail="grant is not yet valid")
        if row["subject"] != subject:
            return GrantDecision(DENY, gid, rev, detail="subject mismatch")
        if row["capability"] != capability:
            return GrantDecision(CAPABILITY_MISMATCH, gid, rev)
        if row["scope"] != scope:
            return GrantDecision(SCOPE_MISMATCH, gid, rev)
        if row["target"] != target:
            return GrantDecision(TARGET_MISMATCH, gid, rev)
        if authority_scope is not None and row["authority_scope"] != authority_scope:
            return GrantDecision(DENY, gid, rev, detail="authority scope mismatch")
        if row["payload_binding"] is not None and row["payload_binding"] != _canonical(payload):
            return GrantDecision(DENY, gid, rev, detail="payload binding mismatch")
        if action_key is not None:
            digest = _digest({"subject": subject, "capability": capability,
                              "scope": scope, "target": target, "payload": payload})
            prior = con.execute("SELECT * FROM permission_reservations WHERE grant_id=? AND action_key=?",
                                 (gid, action_key)).fetchone()
            if prior:
                if prior["action_digest"] != digest:
                    return GrantDecision(CONFLICT, gid, rev, detail="action key payload conflict")
                if int(prior["grant_revision"]) != rev:
                    return GrantDecision(STALE_REVISION, gid, rev, prior["reservation_id"], "grant revision changed")
                if prior["state"] == "RELEASED":
                    return GrantDecision(DENY, gid, rev, detail="released action cannot be replayed")
                if prior["state"] == "NO_EFFECT":
                    return GrantDecision(DENY, gid, rev, prior["reservation_id"], "action already finalized with no effect")
                return GrantDecision(ALLOW, gid, rev, prior["reservation_id"], "same logical action replay")
        active = con.execute(
            "SELECT count(*) FROM permission_reservations WHERE grant_id=? AND state IN ('RESERVED','EXECUTING','CONSUMED')",
            (gid,),
        ).fetchone()[0]
        if row["max_uses"] is not None and active >= int(row["max_uses"]):
            return GrantDecision(USE_EXHAUSTED, gid, rev)
        if row["use_mode"] == "ONE_SHOT" and int(row["uses"]) >= 1:
            return GrantDecision(USE_EXHAUSTED, gid, rev)
        return GrantDecision(ALLOW, gid, rev)

    def issue_grant(self, *, grantor: str, bootstrap_authorized: bool,
                    subject: str, capability: str, scope: str, target: str,
                    authority_scope: str, payload_binding: Any = None,
                    valid_from: float | None = None, expires_at: float | None = None,
                    use_mode: str = "REUSABLE", max_uses: int | None = None,
                    delegation_allowed: bool = False,
                    meaning_version: str = "governance.permission_grant.v1",
                    grant_id: str | None = None, operation_id: str | None = None,
                    caller_principal: str = "service:chiyo-life-supply",
                    writer_instance: str = "legacy", writer_epoch: int | None = None) -> GrantDecision:
        # bootstrap_authorized is derived by Unix socket SO_PEERCRED, never request JSON.
        self._assert_writer()
        if not bootstrap_authorized or grantor != "HOST_OPERATOR":
            return GrantDecision(DENY, detail="HOST_OPERATOR bootstrap required")
        if delegation_allowed:
            return GrantDecision(DENY, detail="delegation is disabled in v1")
        if not all(str(x or "").strip() for x in (subject, capability, scope, target, authority_scope, meaning_version)):
            return GrantDecision(DENY, detail="required grant field missing")
        if capability not in CAPABILITIES:
            return GrantDecision(DENY, detail="capability is not in the narrow allowlist")
        if use_mode not in USE_MODES:
            return GrantDecision(DENY, detail="invalid use mode")
        if use_mode == "ONE_SHOT":
            if max_uses not in (None, 1):
                return GrantDecision(DENY, detail="ONE_SHOT requires max_uses=1")
            max_uses = 1
        elif max_uses is not None and max_uses < 1:
            return GrantDecision(DENY, detail="max_uses must be positive")
        requested_valid_from = valid_from
        now = float(self.clock())
        valid_from = now if valid_from is None else float(valid_from)
        if expires_at is not None and float(expires_at) <= valid_from:
            return GrantDecision(DENY, detail="expiry must be later than valid_from")
        gid = grant_id or "grant:" + uuid.uuid4().hex
        operation_id = operation_id or gid
        try:
            with self._connection() as con:
                con.execute("BEGIN IMMEDIATE")
                prior = con.execute("SELECT request_digest,result_json FROM authority_operations WHERE operation_id=?", (operation_id,)).fetchone()
                if prior and requested_valid_from is None:
                    request_without_default = {"grantor": grantor, "subject": subject, "capability": capability, "scope": scope,
                        "target": target, "payload_binding": payload_binding, "valid_from": None,
                        "expires_at": expires_at, "use_mode": use_mode, "max_uses": max_uses,
                        "authority_scope": authority_scope, "meaning_version": meaning_version}
                    if prior["request_digest"] == _digest(request_without_default):
                        con.commit(); return GrantDecision(**json.loads(prior["result_json"]))
                request = {"grantor": grantor, "subject": subject, "capability": capability, "scope": scope,
                    "target": target, "payload_binding": payload_binding, "valid_from": requested_valid_from,
                    "expires_at": expires_at, "use_mode": use_mode, "max_uses": max_uses,
                    "authority_scope": authority_scope, "meaning_version": meaning_version}
                req_digest = _digest(request)
                if prior:
                    if prior["request_digest"] != req_digest:
                        con.rollback(); return GrantDecision(CONFLICT, gid, detail="operation_id payload conflict")
                    con.commit(); return GrantDecision(**json.loads(prior["result_json"]))
                con.execute("""INSERT INTO permission_grants(
                    grant_id,grantor,subject,capability,scope,target,payload_binding,
                    valid_from,expires_at,revision,revocation_state,revoked_at,use_mode,
                    max_uses,uses,delegation_allowed,authority_scope,created_at,meaning_version,
                    caller_principal,writer_owner,writer_instance,writer_epoch,operation_id
                ) VALUES(?,?,?,?,?,?,?,?,?,1,'ACTIVE',NULL,?,?,0,?,?,?,?,?,'Governance',?,?,?)""",
                    (gid, grantor, subject, capability, scope, target,
                     None if payload_binding is None else _canonical(payload_binding),
                     valid_from, expires_at, use_mode, max_uses, int(bool(delegation_allowed)),
                     authority_scope, now, meaning_version, caller_principal,
                     writer_instance, int(self.writer_fence.epoch) if self.writer_fence and self.writer_fence.epoch else (writer_epoch or 0), operation_id))
                decision = GrantDecision(ALLOW, gid, 1, detail="grant issued")
                con.execute("INSERT INTO authority_operations VALUES(?,?,?,?)",
                    (operation_id, req_digest, _canonical(decision.to_dict()), now))
                con.commit()
            return decision
        except sqlite3.IntegrityError as exc:
            return GrantDecision(CONFLICT, gid, detail=f"grant id conflict: {exc}")
        except sqlite3.Error:
            return GrantDecision(UNKNOWN, gid, detail="grant store unavailable")

    def get_grant(self, grant_id: str) -> dict[str, Any] | None:
        try:
            with self._connection() as con:
                row = con.execute("SELECT * FROM permission_grants WHERE grant_id=?", (grant_id,)).fetchone()
                return dict(row) if row else None
        except sqlite3.Error:
            return None

    def get_reservation(self, reservation_id: str) -> dict[str, Any] | None:
        try:
            with self._connection() as con:
                row = con.execute("SELECT * FROM permission_reservations WHERE reservation_id=?", (reservation_id,)).fetchone()
                return dict(row) if row else None
        except sqlite3.Error:
            return None

    def validate_grant(self, grant_id: str, *, subject: str, capability: str,
                       scope: str, target: str, expected_revision: int | None = None,
                       payload: Any = None, action_key: str | None = None) -> GrantDecision:
        try:
            with self._connection() as con:
                row = con.execute("SELECT * FROM permission_grants WHERE grant_id=?", (grant_id,)).fetchone()
                if row is None:
                    return GrantDecision(UNKNOWN, grant_id, detail="grant not found")
                return self._check_row(row, now=float(self.clock()), subject=subject,
                                       capability=capability, scope=scope, target=target,
                                       expected_revision=expected_revision, payload=payload,
                                       con=con, action_key=action_key)
        except (sqlite3.Error, TypeError, ValueError, KeyError):
            return GrantDecision(UNKNOWN, grant_id, detail="grant store unavailable or invalid")

    def lookup_operation(self, operation_id: str) -> dict[str, Any] | None:
        try:
            with self._connection() as con:
                row = con.execute("SELECT * FROM authority_operations WHERE operation_id=?", (operation_id,)).fetchone()
                return dict(row) if row else None
        except sqlite3.Error:
            return None

    def lookup_reservation(self, grant_id: str, action_key: str) -> dict[str, Any] | None:
        try:
            with self._connection() as con:
                row = con.execute("SELECT * FROM permission_reservations WHERE grant_id=? AND action_key=?", (grant_id, action_key)).fetchone()
                return dict(row) if row else None
        except sqlite3.Error:
            return None

    def reserve_use(self, grant_id: str, *, subject: str, capability: str,
                    scope: str, target: str, action_key: str,
                    expected_revision: int | None = None, payload: Any = None,
                    actor_uid: int | None = None, authority_scope: str | None = None) -> GrantDecision:
        self._assert_writer()
        if not action_key:
            return GrantDecision(DENY, grant_id, detail="action_key required")
        digest = _digest({"subject": subject, "capability": capability,
                          "scope": scope, "target": target, "payload": payload})
        now = float(self.clock())
        try:
            with self._connection() as con:
                con.execute("BEGIN IMMEDIATE")
                row = con.execute("SELECT * FROM permission_grants WHERE grant_id=?", (grant_id,)).fetchone()
                if row is None:
                    con.rollback(); return GrantDecision(UNKNOWN, grant_id, detail="grant not found")
                decision = self._check_row(row, now=now, subject=subject,
                                           capability=capability, scope=scope, target=target,
                                           expected_revision=expected_revision, payload=payload,
                                           con=con, action_key=action_key, authority_scope=authority_scope)
                if not decision.allowed:
                    con.rollback(); return decision
                if decision.reservation_id:
                    con.commit(); return decision
                rid = "reservation:" + uuid.uuid4().hex
                con.execute("INSERT INTO permission_reservations(reservation_id,grant_id,action_key,action_digest,grant_revision,reservation_owner_uid,state,created_at,updated_at) VALUES(?,?,?,?,?,?,'RESERVED',?,?)",
                            (rid, grant_id, action_key, digest, int(row["revision"]), actor_uid, now, now))
                con.commit()
                return GrantDecision(ALLOW, grant_id, int(row["revision"]), rid, "reserved")
        except sqlite3.IntegrityError:
            return GrantDecision(CONFLICT, grant_id, detail="reservation conflict")
        except sqlite3.Error:
            return GrantDecision(UNKNOWN, grant_id, detail="grant store unavailable")

    def begin_execution(self, reservation_id: str, *, actor_uid: int | None) -> GrantDecision:
        """Linearization point: after this returns ALLOW, the logical action has won against revocation."""
        self._assert_writer()
        now = float(self.clock())
        try:
            with self._connection() as con:
                con.execute("BEGIN IMMEDIATE")
                res = con.execute("SELECT * FROM permission_reservations WHERE reservation_id=?", (reservation_id,)).fetchone()
                if res is None:
                    con.rollback(); return GrantDecision(UNKNOWN, reservation_id, detail="reservation not found")
                grant = con.execute("SELECT * FROM permission_grants WHERE grant_id=?", (res["grant_id"],)).fetchone()
                if grant is None:
                    con.rollback(); return GrantDecision(UNKNOWN, detail="owning grant missing")
                if res["reservation_owner_uid"] != actor_uid:
                    con.rollback(); return GrantDecision(DENY, grant["grant_id"], int(grant["revision"]), reservation_id, "reservation actor mismatch")
                if res["state"] in {"EXECUTING", "CONSUMED"}:
                    con.commit(); return GrantDecision(ALLOW, grant["grant_id"], int(grant["revision"]), reservation_id, "execution receipt replay")
                if res["state"] == "NO_EFFECT":
                    con.commit(); return GrantDecision(DENY, grant["grant_id"], int(grant["revision"]), reservation_id, "action already finalized with no effect")
                if res["state"] != "RESERVED":
                    con.rollback(); return GrantDecision(DENY, grant["grant_id"], int(grant["revision"]), reservation_id, "reservation not executable")
                if int(grant["revision"]) != int(res["grant_revision"]):
                    con.rollback(); return GrantDecision(STALE_REVISION, grant["grant_id"], int(grant["revision"]), reservation_id)
                if grant["revocation_state"] != "ACTIVE":
                    status = REVOKED if grant["revocation_state"] == "REVOKED" else EXPIRED
                    con.rollback(); return GrantDecision(status, grant["grant_id"], int(grant["revision"]), reservation_id)
                if grant["expires_at"] is not None and now >= float(grant["expires_at"]):
                    con.rollback(); return GrantDecision(EXPIRED, grant["grant_id"], int(grant["revision"]), reservation_id)
                con.execute("UPDATE permission_reservations SET state='EXECUTING',execution_started_at=?,updated_at=? WHERE reservation_id=? AND state='RESERVED'", (now, now, reservation_id))
                con.commit()
                return GrantDecision(ALLOW, grant["grant_id"], int(grant["revision"]), reservation_id, "execution fenced")
        except sqlite3.Error:
            return GrantDecision(UNKNOWN, reservation_id, detail="grant store unavailable")

    def consume_use(self, reservation_id: str, *, actor_uid: int | None = None) -> GrantDecision:
        return self._finish_reservation(reservation_id, "CONSUMED", actor_uid=actor_uid)

    def release_reservation(self, reservation_id: str, *, actor_uid: int | None = None) -> GrantDecision:
        return self._finish_reservation(reservation_id, "RELEASED", actor_uid=actor_uid)

    def finalize_no_effect(self, reservation_id: str, *, actor_uid: int | None = None) -> GrantDecision:
        return self._finish_reservation(reservation_id, "NO_EFFECT", actor_uid=actor_uid)

    def _finish_reservation(self, reservation_id: str, new_state: str, *, actor_uid: int | None) -> GrantDecision:
        self._assert_writer()
        now = float(self.clock())
        try:
            with self._connection() as con:
                con.execute("BEGIN IMMEDIATE")
                res = con.execute("SELECT * FROM permission_reservations WHERE reservation_id=?", (reservation_id,)).fetchone()
                if res is None:
                    con.rollback(); return GrantDecision(UNKNOWN, reservation_id, detail="reservation not found")
                grant = con.execute("SELECT * FROM permission_grants WHERE grant_id=?", (res["grant_id"],)).fetchone()
                if grant is None:
                    con.rollback(); return GrantDecision(UNKNOWN, detail="owning grant missing")
                if res["reservation_owner_uid"] != actor_uid:
                    con.rollback(); return GrantDecision(DENY, grant["grant_id"], int(grant["revision"]), reservation_id, "reservation actor mismatch")
                if res["state"] == new_state:
                    if int(grant["revision"]) != int(res["grant_revision"]):
                        if new_state in {"CONSUMED", "NO_EFFECT"} and res["execution_started_at"] is not None:
                            con.commit(); return GrantDecision(ALLOW, grant["grant_id"], int(grant["revision"]), reservation_id, "historical execution receipt")
                        con.rollback(); return GrantDecision(STALE_REVISION, grant["grant_id"], int(grant["revision"]), reservation_id, "historical receipt; grant revision changed")
                    con.commit(); return GrantDecision(ALLOW, grant["grant_id"], int(grant["revision"]), reservation_id, "idempotent completion receipt")
                permitted_previous = "EXECUTING" if new_state == "CONSUMED" else "RESERVED"
                if new_state == "NO_EFFECT" and res["state"] == "EXECUTING":
                    permitted_previous = "EXECUTING"
                if res["state"] != permitted_previous:
                    con.rollback(); return GrantDecision(DENY, grant["grant_id"], int(grant["revision"]), reservation_id, "reservation already finalized or not reserved")
                # Authorization was linearized by begin_execution while the grant revision was
                # current. Finalization records that already-authorized effect; it does not mint
                # fresh authority after a concurrent revoke.
                if new_state == "CONSUMED":
                    con.execute("UPDATE permission_grants SET uses=uses+1 WHERE grant_id=?", (grant["grant_id"],))
                con.execute("UPDATE permission_reservations SET state=?,updated_at=? WHERE reservation_id=? AND state=?",
                            (new_state, now, reservation_id, permitted_previous))
                con.commit()
                return GrantDecision(ALLOW, grant["grant_id"], int(grant["revision"]), reservation_id, new_state.lower())
        except sqlite3.Error:
            return GrantDecision(UNKNOWN, reservation_id, detail="grant store unavailable")

    def revoke_grant(self, grant_id: str, *, expected_revision: int, operation_id: str | None = None,
                     writer_instance: str = "legacy", writer_epoch: int | None = None) -> GrantDecision:
        self._assert_writer()
        operation_id = operation_id or f"revoke:{grant_id}:{expected_revision}"
        now = float(self.clock())
        try:
            with self._connection() as con:
                con.execute("BEGIN IMMEDIATE")
                op_req = {"grant_id": grant_id, "expected_revision": expected_revision}
                op_digest = _digest(op_req)
                receipt = con.execute("SELECT request_digest,result_json FROM authority_operations WHERE operation_id=?", (operation_id,)).fetchone()
                if receipt:
                    if receipt["request_digest"] != op_digest:
                        con.rollback(); return GrantDecision(CONFLICT, grant_id, detail="operation_id payload conflict")
                    con.commit(); return GrantDecision(**json.loads(receipt["result_json"]))
                row = con.execute("SELECT * FROM permission_grants WHERE grant_id=?", (grant_id,)).fetchone()
                if row is None:
                    con.rollback(); return GrantDecision(UNKNOWN, grant_id, detail="grant not found")
                if int(row["revision"]) != int(expected_revision):
                    con.rollback(); return GrantDecision(STALE_REVISION, grant_id, int(row["revision"]))
                if row["revocation_state"] == "REVOKED":
                    con.commit(); return GrantDecision(NO_CHANGE, grant_id, int(row["revision"]), detail="already revoked")
                revision = int(row["revision"]) + 1
                con.execute("UPDATE permission_grants SET revocation_state='REVOKED',revoked_at=?,revision=?,writer_instance=?,writer_epoch=? WHERE grant_id=? AND revision=?",
                            (now, revision, writer_instance, writer_epoch or (self.writer_fence.epoch if self.writer_fence else 0), grant_id, expected_revision))
                decision = GrantDecision(REVOKED, grant_id, revision, detail="grant revoked")
                con.execute("INSERT INTO authority_operations VALUES(?,?,?,?)",
                    (operation_id, op_digest, _canonical(decision.to_dict()), now))
                con.commit()
                return decision
        except sqlite3.Error:
            return GrantDecision(UNKNOWN, grant_id, detail="grant store unavailable")

    def expire_grants(self) -> int | None:
        self._assert_writer()
        now = float(self.clock())
        try:
            with self._connection() as con:
                con.execute("BEGIN IMMEDIATE")
                count = con.execute("UPDATE permission_grants SET revocation_state='EXPIRED',revision=revision+1 WHERE revocation_state='ACTIVE' AND expires_at IS NOT NULL AND expires_at<=?", (now,)).rowcount
                con.commit()
                return count
        except sqlite3.Error:
            return None

    def unresolved_reservations(self) -> list[dict[str, Any]]:
        try:
            with self._connection() as con:
                rows = con.execute("SELECT reservation_id,grant_id,action_key,state,created_at,updated_at FROM permission_reservations WHERE state IN ('RESERVED','EXECUTING') ORDER BY created_at").fetchall()
                return [dict(row) for row in rows]
        except sqlite3.Error:
            return []

    def unresolved_executions(self) -> list[dict[str, Any]]:
        try:
            with self._connection() as con:
                rows = con.execute("SELECT reservation_id,grant_id,action_key,state,execution_started_at,updated_at FROM permission_reservations WHERE state='EXECUTING' ORDER BY execution_started_at").fetchall()
                return [dict(row) for row in rows]
        except sqlite3.Error:
            return []

    def list_active_grants(self, *, subject: str | None = None) -> list[dict[str, Any]] | None:
        now = float(self.clock())
        try:
            with self._connection() as con:
                if subject is None:
                    rows = con.execute("SELECT * FROM permission_grants WHERE revocation_state='ACTIVE' AND (expires_at IS NULL OR expires_at>?) ORDER BY created_at", (now,)).fetchall()
                else:
                    rows = con.execute("SELECT * FROM permission_grants WHERE subject=? AND revocation_state='ACTIVE' AND (expires_at IS NULL OR expires_at>?) ORDER BY created_at", (subject, now)).fetchall()
                return [dict(row) for row in rows]
        except sqlite3.Error:
            return None
