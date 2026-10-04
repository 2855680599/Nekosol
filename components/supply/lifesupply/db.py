"""C7 PHASE 01/02 -- one SQLite file, three logical owners, one unit of work.

Why this module exists
----------------------
C6 kept Governance, Workspace and ManagedArtifact in **three separate SQLite files**.  An
operation that spans owners (authorise -> owner effect -> receipt) could therefore commit in
one file and not the other, and a crash in that window had no atomic answer.  The C6 handoff
recorded exactly that as its Gate A deployment blocker:

    "Governance ``EXECUTING`` reservation and Workspace/Artifact canonical write are in
     separate SQLite DBs.  ... Gate A remains blocked until an owner-receipt join/recovery
     design is independently approved and fault-injection tested."

C7 removes the gap **by construction**: one canonical file keeps all three owners' dedicated
tables, and a composite command runs inside a single ``BEGIN IMMEDIATE`` ... ``COMMIT``.

One database is NOT one universal owner
--------------------------------------
This is the boundary the C7 order insists on, and it is enforced here:

* every owner keeps its **own tables** (``OWNER_TABLES``);
* every owner keeps its **own writer fence** and its **own mutation guards**
  (``install_owner_write_guards`` -- no table may be mutated by a connection that is not
  registered for *its* owner);
* only five explicitly listed commands may span owners (``CROSS_OWNER_COMMANDS``);
* the unit of work owns **the transaction boundary only** -- it performs no domain judgement
  and writes no business fact of its own beyond the command journal.

Layout
------
``./data/supply/life_supply.sqlite``   the single canonical database
``./data/supply/projection/``          derived artifact Markdown (never canonical)

Logical vs physical names
-------------------------
The C7 order names the logical tables ``grants``/``grant_operations``,
``workspaces``/``inquiries``/``inquiry_operations``,
``artifacts``/``artifact_versions``/``artifact_operations``/``artifact_receipts``.
The C6 physical names are kept verbatim -- renaming them would mean rewriting every SQL
statement in three stores and their tests for zero semantic gain -- and the logical names are
exposed as **read-only views** over the physical tables (``LOGICAL_VIEWS``).  Canonical writes
always go through the owning store's physical table.
"""
from __future__ import annotations

import hashlib
import json
from lifesupply.sqlite_safety import configure_journal
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, NamedTuple, Optional, Sequence

__all__ = [
    "OWNERS",
    "OWNER_TABLES",
    "TABLE_OWNER",
    "CROSS_OWNER_COMMANDS",
    "LOGICAL_VIEWS",
    "SCHEMA_LAYOUT",
    "LifeSupplyDB",
    "LifeSupplyUnitOfWork",
    "CrossOwnerCommandRefused",
    "NestedUnitOfWorkRefused",
    "OwnerTableMismatch",
    "UnitOfWorkDoomed",
    "CommandContractViolation",
    "JoinedConnectionExpired",
    "CommandContract",
    "Postcondition",
    "COMMAND_CONTRACTS",
    "register_owner_writer_connection",
    "install_owner_write_guards",
    "activate_owner_epoch",
]


# ---------------------------------------------------------------------------------
# the three logical owners, and only these
# ---------------------------------------------------------------------------------

OWNERS: tuple[str, ...] = ("Governance", "Workspace", "ManagedArtifact")

#: Every canonical table belongs to exactly one owner.  Physical names are the C6 ones.
OWNER_TABLES: dict[str, tuple[str, ...]] = {
    "Governance": ("permission_grants", "permission_reservations", "authority_operations"),
    "Workspace": ("workspaces", "inquiry_proposals", "open_inquiries", "admission_results",
                  "operation_keys"),
    "ManagedArtifact": ("managed_artifacts", "artifact_versions", "artifact_operations",
                        "artifact_effect_receipts", "artifact_projection_bindings",
                        "artifact_projection_outbox"),
}

TABLE_OWNER: dict[str, str] = {
    table: owner for owner, tables in OWNER_TABLES.items() for table in tables
}

#: The only commands allowed to touch more than one owner inside one transaction.
#: Everything else must stay inside a single owner.
CROSS_OWNER_COMMANDS: frozenset[str] = frozenset({
    "ISSUE_GRANT",
    "REVOKE_GRANT",
    "PROVISION_WORKSPACE",
    "ADMIT_INQUIRY",
    "COMMIT_MARKDOWN",
})

#: C7 logical schema names -> C6 physical tables, exposed as read-only views.
LOGICAL_VIEWS: dict[str, str] = {
    "grants": "permission_grants",
    "grant_operations": "authority_operations",
    "inquiries": "open_inquiries",
    "inquiry_operations": "operation_keys",
    "artifacts": "managed_artifacts",
    "artifact_receipts": "artifact_effect_receipts",
    "writer_epoch": "c6_writer_epoch",
}

SCHEMA_LAYOUT = "life-supply.single-db.v1"

CANONICAL_FILENAME = "life_supply.sqlite"

DEFAULT_PRINCIPAL = "service:chiyo-life-supply"

_FENCE_EPOCH_FN = "c6_writer_epoch_for"
_FENCE_INSTANCE_FN = "c6_writer_instance_for"
_FENCE_TOKEN_FN = "c6_writer_token_for"


class CrossOwnerCommandRefused(RuntimeError):
    """A coordinated (cross-owner) command that is not on the frozen allowlist."""


class NestedUnitOfWorkRefused(RuntimeError):
    """A unit of work was opened inside another unit of work on the same thread."""


class OwnerTableMismatch(ValueError):
    """A caller claimed a table for an owner that does not own it."""


class UnitOfWorkDoomed(RuntimeError):
    """A participating store rolled back, so the coordinated command cannot commit."""


class JoinedConnectionExpired(RuntimeError):
    """A store kept using its joined connection after the unit of work finished.

    The unit of work owns the connection lifetime.  A retained ``_JoinedConnection`` must fail
    loudly instead of silently executing against a finished transaction.
    """


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------------
# merged physical schema (C7 PHASE 05)
# ---------------------------------------------------------------------------------
#
# This is the union of the three C6 schema blocks, byte-for-byte in content, with three
# changes: (1) all tables now live in one file; (2) the guards become per-owner
# (``c7_fence_*`` using the owner-parameterised writer functions) instead of the 0-arg C6
# form; (3) the cross-owner command journal and the logical views are added.
#
# The C6 ``c6_writer_epoch`` table is kept, unchanged, as the single per-owner epoch table:
# its primary key is already the owner, so one file holds one row per owner.

MERGED_SCHEMA_DDL = """
    ---- shared infrastructure ------------------------------------------------
    CREATE TABLE IF NOT EXISTS schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS c6_writer_epoch(
        owner TEXT PRIMARY KEY,
        active_epoch INTEGER NOT NULL,
        instance_id TEXT NOT NULL,
        writer_token TEXT NOT NULL DEFAULT 'unfenced-test'
    );
    -- C7 PHASE 02: the cross-owner command journal.  A row only ever exists for a command
    -- that COMMITTED, because it is written inside the same transaction as its effects.
    CREATE TABLE IF NOT EXISTS life_supply_uow_journal(
        uow_id TEXT PRIMARY KEY,
        command TEXT NOT NULL,
        operation_id TEXT NOT NULL,
        subject_id TEXT,
        caller_principal TEXT NOT NULL,
        participants TEXT NOT NULL,
        facts TEXT NOT NULL,
        outcome TEXT NOT NULL CHECK(outcome IN ('COMMITTED')),
        started_at REAL NOT NULL,
        finished_at REAL NOT NULL,
        UNIQUE(command, operation_id)
    );

    ---- Governance ----------------------------------------------------------
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

    CREATE TABLE IF NOT EXISTS authority_operations (
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
    CREATE INDEX IF NOT EXISTS reservations_grant_state
        ON permission_reservations(grant_id, state);

    ---- Workspace -----------------------------------------------------------
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

    ---- ManagedArtifact -----------------------------------------------------
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
"""


# ---------------------------------------------------------------------------------
# per-owner writer registration and guards (C7 PHASE 01/08)
# ---------------------------------------------------------------------------------


def register_owner_writer_connection(
    con: sqlite3.Connection,
    owners: tuple[str, ...] = OWNERS,
    fences: Mapping[str, Any] | None = None,
) -> dict[str, int]:
    """Register the owner-parameterised writer functions on one shared connection.

    A single connection now serves three owners, so the guards cannot use the C6 0-arg
    ``c6_writer_epoch()`` form.  Instead three 1-arg functions take the owner name, and every
    guard trigger passes its own owner literal.  Returns the epoch per owner.
    """
    fences = dict(fences or {})
    epochs: dict[str, int] = {}
    instances: dict[str, str] = {}
    tokens: dict[str, str] = {}
    for owner in owners:
        fence = fences.get(owner)
        if fence is None:
            epochs[owner], instances[owner], tokens[owner] = 0, "unfenced-test", "unfenced-test"
            continue
        epochs[owner] = int(fence.assert_current())
        instances[owner] = fence.instance_id
        tokens[owner] = fence.writer_token
    con.create_function(_FENCE_EPOCH_FN, 1, lambda owner: int(epochs.get(owner, 0)),
                        deterministic=False)
    con.create_function(_FENCE_INSTANCE_FN, 1, lambda owner: instances.get(owner, "unfenced-test"),
                        deterministic=False)
    con.create_function(_FENCE_TOKEN_FN, 1, lambda owner: tokens.get(owner, "unfenced-test"),
                        deterministic=False)
    return epochs


def install_owner_write_guards(con: sqlite3.Connection) -> None:
    """Guard every canonical table with its own owner's fence, inside the caller's txn.

    A connection registered for ``Workspace`` therefore cannot mutate a
    ``ManagedArtifact`` table, and a superseded writer cannot mutate anything.
    """
    for table, owner in TABLE_OWNER.items():
        sub_epoch = f"(SELECT active_epoch FROM c6_writer_epoch WHERE owner='{owner}')"
        sub_instance = f"(SELECT instance_id FROM c6_writer_epoch WHERE owner='{owner}')"
        sub_token = f"(SELECT writer_token FROM c6_writer_epoch WHERE owner='{owner}')"
        condition = (
            f"{_FENCE_EPOCH_FN}('{owner}') < 0"
            f" OR {sub_epoch} != {_FENCE_EPOCH_FN}('{owner}')"
            f" OR {sub_instance} != {_FENCE_INSTANCE_FN}('{owner}')"
            f" OR {sub_token} != {_FENCE_TOKEN_FN}('{owner}')"
        )
        for action in ("INSERT", "UPDATE", "DELETE"):
            name = f"c7_fence_{table}_{action.lower()}"
            con.execute(f"DROP TRIGGER IF EXISTS {name}")
            con.execute(
                f"CREATE TRIGGER {name} BEFORE {action} ON {table}\n"
                f"    WHEN {condition}\n"
                f"    BEGIN SELECT RAISE(ABORT,'STALE_WRITER'); END"
            )


def activate_owner_epoch(con: sqlite3.Connection, owner: str, fence: Any) -> None:
    """Persist the active epoch/instance/token for one owner at fenced startup.

    Mirrors the C6 ``activate_writer_epoch`` but does **not** re-register the 0-arg writer
    functions (this module uses the owner-parameterised form).
    """
    con.execute("CREATE TABLE IF NOT EXISTS c6_writer_epoch("
                "owner TEXT PRIMARY KEY, active_epoch INTEGER NOT NULL, "
                "instance_id TEXT NOT NULL, writer_token TEXT NOT NULL DEFAULT 'unfenced-test')")
    columns = {row[1] for row in con.execute("PRAGMA table_info(c6_writer_epoch)").fetchall()}
    if "writer_token" not in columns:
        con.execute("ALTER TABLE c6_writer_epoch ADD COLUMN writer_token TEXT NOT NULL "
                    "DEFAULT 'unfenced-test'")
    con.execute("INSERT OR IGNORE INTO c6_writer_epoch(owner,active_epoch,instance_id,writer_token) "
                "VALUES(?,0,'unfenced-test','unfenced-test')", (owner,))
    if fence is None:
        return
    epoch = int(fence.assert_current())
    con.execute("BEGIN IMMEDIATE")
    try:
        con.execute("UPDATE c6_writer_epoch SET active_epoch=?,instance_id=?,writer_token=? "
                    "WHERE owner=?", (epoch, fence.instance_id, fence.writer_token, owner))
        con.commit()
    except Exception:
        con.rollback()
        raise


# ---------------------------------------------------------------------------------
# the single database
# ---------------------------------------------------------------------------------


# ---------------------------------------------------------------------------------
# per-command step contracts (C9 PHASE 02)
# ---------------------------------------------------------------------------------
#
# One loose "accepted status" set is not enough.  ``ALLOW`` proves that an authorisation
# check passed; it does not prove that a Workspace row now exists.  ``NO_CHANGE`` is a legal
# Artifact outcome but says nothing about the other owners in the command.  So every
# coordinated command declares:
#
#   * the steps it *requires*, with the exact statuses each step may return;
#   * the steps it merely *permits* (creating the artifact before committing it);
#   * postconditions evaluated against the transaction *before* COMMIT.
#
# ``LifeSupplyUnitOfWork.commit()`` refuses to commit unless every required step was
# registered and every postcondition holds.  A command can therefore never be reported as
# successful on the strength of "no exception was raised".


class CommandContractViolation(UnitOfWorkDoomed):
    """A coordinated command did not satisfy its own step contract or its postconditions."""


class Postcondition(NamedTuple):
    name: str
    check: Callable[[sqlite3.Connection, Mapping[str, Any]], tuple[bool, str]]


class CommandContract(NamedTuple):
    command: str
    required_steps: Mapping[tuple[str, str], frozenset[str]]
    allowed_extra_steps: Mapping[tuple[str, str], frozenset[str]] = {}
    postconditions: tuple[Postcondition, ...] = ()

    def statuses_for(self, owner: str, action: str) -> frozenset[str] | None:
        key = (owner, action)
        if key in self.required_steps:
            return self.required_steps[key]
        return self.allowed_extra_steps.get(key)

    def missing_steps(self, steps: Sequence[Mapping[str, Any]]) -> set[tuple[str, str]]:
        seen = {(str(step.get("owner")), str(step.get("action"))) for step in steps}
        return {key for key in self.required_steps if key not in seen}


def _pc_workspace_active(con, params):
    """PROVISION_WORKSPACE: the ACTIVE workspace must be the one THIS operation created.

    "exactly one active workspace exists" is not enough: an older workspace would satisfy that
    while this command created nothing.  The row must carry this command's operation key, and
    it must have been created by this command's grant.
    """
    subject = params.get("subject_id")
    operation_key = params.get("operation_key") or params.get("operation_id")
    if not subject or not operation_key:
        return False, "subject_id/operation_key parameter missing"
    row = con.execute("SELECT workspace_id,lifecycle,operation_id,created_by_grant "
                      "FROM workspaces WHERE subject_id=?", (subject,)).fetchone()
    if row is None:
        return False, "no workspace row for subject"
    if row["lifecycle"] != "ACTIVE":
        return False, f"lifecycle = {row['lifecycle']}"
    if row["operation_id"] != operation_key:
        return False, ("the workspace was not created by this operation "
                       f"(row operation_id={row['operation_id']!r})")
    grant_id = params.get("grant_id")
    if grant_id is not None and row["created_by_grant"] != grant_id:
        return False, "the workspace was created by another grant"
    return True, f"workspace {row['workspace_id']} created by {operation_key}"


def _pc_reservation_consumed(con, params):
    """The reservation consumed must be THIS command's reservation, for THIS command's grant."""
    reservation_id = params.get("reservation_id")
    if not reservation_id:
        return False, "reservation_id parameter missing"
    row = con.execute("SELECT state,grant_id FROM permission_reservations "
                      "WHERE reservation_id=?", (reservation_id,)).fetchone()
    if row is None:
        return False, "reservation row missing"
    if row["state"] != "CONSUMED":
        return False, f"reservation state = {row['state']}"
    grant_id = params.get("grant_id")
    if grant_id is not None and row["grant_id"] != grant_id:
        return False, "the reservation belongs to another grant"
    return True, f"reservation {reservation_id} consumed for grant {row['grant_id']}"


def _pc_grant_active(con, params):
    grant_id = params.get("grant_id")
    if not grant_id:
        return False, "grant_id parameter missing"
    row = con.execute("SELECT revocation_state FROM permission_grants WHERE grant_id=?",
                      (grant_id,)).fetchone()
    state = None if row is None else row["revocation_state"]
    return (state == "ACTIVE", f"grant revocation_state = {state}")


def _pc_grant_revoked(con, params):
    grant_id = params.get("grant_id")
    if not grant_id:
        return False, "grant_id parameter missing"
    row = con.execute("SELECT revocation_state FROM permission_grants WHERE grant_id=?",
                      (grant_id,)).fetchone()
    state = None if row is None else row["revocation_state"]
    return (state == "REVOKED", f"grant revocation_state = {state}")


def _pc_artifact_head_committed(con, params):
    """COMMIT_MARKDOWN: the canonical head must BE the version THIS operation created."""
    artifact_id = params.get("artifact_id")
    operation_key = params.get("operation_key") or params.get("operation_id")
    if not artifact_id or not operation_key:
        return False, "artifact_id/operation_key parameter missing"
    version = con.execute("SELECT version_id,artifact_id FROM artifact_versions "
                          "WHERE operation_key=?", (operation_key,)).fetchone()
    if version is None:
        return False, "no artifact version was created by this operation"
    if version["artifact_id"] != artifact_id:
        return False, "the version created by this operation belongs to another artifact"
    head = con.execute("SELECT head_version_id FROM managed_artifacts WHERE artifact_id=?",
                       (artifact_id,)).fetchone()
    if head is None:
        return False, "artifact row missing"
    if head["head_version_id"] != version["version_id"]:
        return False, ("the artifact head is not the version this operation created "
                       "(a later operation moved it, or this was only a replayed receipt)")
    return True, f"head {version['version_id']} created by {operation_key}"


def _pc_effect_receipt(con, params):
    """COMMIT_MARKDOWN: exactly one receipt, for THIS operation, THIS artifact, THIS version."""
    operation_key = params.get("operation_key") or params.get("operation_id")
    artifact_id = params.get("artifact_id")
    if not operation_key:
        return False, "operation_key parameter missing"
    rows = con.execute("SELECT artifact_id,version_id FROM artifact_effect_receipts "
                       "WHERE operation_key=?", (operation_key,)).fetchall()
    if len(rows) != 1:
        return False, f"effect receipts for this operation = {len(rows)}"
    receipt = rows[0]
    if artifact_id is not None and receipt["artifact_id"] != artifact_id:
        return False, "the receipt belongs to another artifact"
    version = con.execute("SELECT version_id FROM artifact_versions WHERE operation_key=?",
                          (operation_key,)).fetchone()
    if version is not None and receipt["version_id"] != version["version_id"]:
        return False, "the receipt does not reference the version this operation created"
    return True, f"receipt for {operation_key} references {receipt['version_id']}"


def _pc_inquiry_admitted(con, params):
    """ADMIT_INQUIRY: the admitted inquiry must be the one THIS operation created.

    "some PENDING proposal is now ACCEPT" is not enough -- it must be THIS command's proposal,
    and the workspace's own operation ledger must carry THIS operation's admission result
    naming that proposal.
    """
    proposal_id = params.get("proposal_id")
    if not proposal_id:
        return False, "proposal_id parameter missing"
    row = con.execute("SELECT admission_status FROM inquiry_proposals WHERE proposal_id=?",
                      (proposal_id,)).fetchone()
    status = None if row is None else row["admission_status"]
    if status != "ACCEPT":
        return False, f"proposal admission_status = {status}"
    operation_key = params.get("operation_key")
    if not operation_key:
        # PHASE 07: an admission must be look-up-able under the operation id that asked for it,
        # otherwise a lost response can only be retried blindly with a fresh id.
        return False, "an admission must be recorded under an operation key"
    ledger = con.execute("SELECT result_json FROM operation_keys WHERE operation_key=?",
                         (operation_key,)).fetchone()
    if ledger is None:
        return False, "this operation left no workspace ledger row"
    try:
        recorded = json.loads(ledger["result_json"])
    except (TypeError, ValueError):
        return False, "the workspace ledger row is not readable"
    if recorded.get("proposal_id") != proposal_id:
        return False, "the ledger row names another proposal"
    if recorded.get("outcome") != "ACCEPT":
        return False, f"the ledger row records {recorded.get('outcome')}"
    return True, f"proposal {proposal_id} admitted by this operation"


COMMAND_CONTRACTS: Mapping[str, CommandContract] = {
    "ISSUE_GRANT": CommandContract(
        "ISSUE_GRANT",
        {("Governance", "issue_grant"): frozenset({"ALLOW", "NO_CHANGE"})},
        {},
        (Postcondition("grant_active", _pc_grant_active),),
    ),
    "REVOKE_GRANT": CommandContract(
        "REVOKE_GRANT",
        {("Governance", "revoke_grant"): frozenset({"ALLOW", "NO_CHANGE"})},
        {},
        (Postcondition("grant_revoked", _pc_grant_revoked),),
    ),
    "PROVISION_WORKSPACE": CommandContract(
        "PROVISION_WORKSPACE",
        {
            ("Governance", "validate_grant"): frozenset({"ALLOW"}),
            ("Governance", "reserve_use"): frozenset({"ALLOW"}),
            ("Governance", "begin_execution"): frozenset({"ALLOW"}),
            ("Governance", "consume_use"): frozenset({"ALLOW"}),
            ("Workspace", "provision_workspace"): frozenset({"OK", "NO_CHANGE"}),
        },
        {},
        (Postcondition("workspace_active", _pc_workspace_active),
         Postcondition("reservation_consumed", _pc_reservation_consumed)),
    ),
    "ADMIT_INQUIRY": CommandContract(
        "ADMIT_INQUIRY",
        {("Workspace", "admit_proposal"): frozenset({"OK", "NO_CHANGE"})},
        {},
        (Postcondition("inquiry_admitted", _pc_inquiry_admitted),),
    ),
    "COMMIT_MARKDOWN": CommandContract(
        "COMMIT_MARKDOWN",
        {
            ("Governance", "reserve_use"): frozenset({"ALLOW"}),
            ("Governance", "begin_execution"): frozenset({"ALLOW"}),
            ("Governance", "consume_use"): frozenset({"ALLOW"}),
            ("ManagedArtifact", "commit_markdown"): frozenset({"OK", "NO_CHANGE"}),
        },
        {("ManagedArtifact", "create_artifact"): frozenset({"OK", "NO_CHANGE"})},
        (Postcondition("artifact_head_committed", _pc_artifact_head_committed),
         Postcondition("effect_receipt", _pc_effect_receipt),
         Postcondition("reservation_consumed", _pc_reservation_consumed)),
    ),
}


class _JoinedConnection:
    """A participating store's view of the coordinated connection.

    A store written for the standalone case issues its own ``BEGIN IMMEDIATE`` /
    ``con.commit()`` / ``con.rollback()``.  Inside a coordinated command those statements are
    honoured as **intent**, not as real transaction control, so the store's business methods
    need no change when they take part in a unit of work:

    * ``BEGIN`` is a no-op -- the unit of work already began;
    * ``COMMIT`` only records that this store finished its part;
    * ``ROLLBACK`` dooms the whole coordinated command.

    Everything else is delegated to the real connection.  DDL is refused outright.
    """

    __slots__ = ("_uow",)

    def __init__(self, uow: "LifeSupplyUnitOfWork") -> None:
        self._uow = uow

    @property
    def raw(self) -> sqlite3.Connection:
        return self._uow._open_con

    def _assert_live(self) -> None:
        """A joined connection is usable only while its unit of work is still open."""
        if not self._uow.is_live:
            raise JoinedConnectionExpired(
                "JOINED_CONNECTION_EXPIRED: the unit of work that owns this connection has "
                "already finished; request a new one")


    def execute(self, sql: Any, *args: Any, **kwargs: Any) -> Any:
        self._assert_live()
        head = " ".join(str(sql).split()).lower()
        if head.startswith("begin"):
            return None
        if head == "commit" or head.startswith("commit "):
            self._uow.mark_part_committed()
            return None
        if head == "rollback" or head.startswith("rollback "):
            self._uow.doom("store issued ROLLBACK")
            return None
        return self.raw.execute(sql, *args, **kwargs)

    def executescript(self, script: str) -> Any:
        self._assert_live()
        self._uow.doom("DDL_INSIDE_COORDINATED_COMMAND")
        raise RuntimeError("DDL is not allowed inside a coordinated command")

    def commit(self) -> None:
        self._assert_live()
        self._uow.mark_part_committed()

    def rollback(self) -> None:
        self._assert_live()
        self._uow.doom("store issued rollback()")

    def close(self) -> None:
        """The unit of work owns the connection lifetime, not the store."""
        return None

    def __getattr__(self, name: str) -> Any:
        return getattr(self.raw, name)

    def __enter__(self) -> "_JoinedConnection":
        return self

    def __exit__(self, *exc: Any) -> bool:
        return False


class LifeSupplyDB:
    """The one canonical Life Supply database, holding three logical owners.

    Creating this object creates (or verifies) the merged schema and activates one epoch row
    per owner that has a fence.  It does not create any business fact.
    """

    def __init__(
        self,
        path: str | Path,
        *,
        owners: tuple[str, ...] = OWNERS,
        fences: Mapping[str, Any] | None = None,
        read_only: bool = False,
        clock=time.time,
    ) -> None:
        self.path = str(path)
        self.owners = tuple(owners)
        if set(self.owners) != set(OWNERS):
            raise ValueError(f"single-db layout requires exactly the owners {OWNERS!r}")
        self.fences = dict(fences or {})
        self.read_only = bool(read_only)
        self.clock = clock
        self.created_at = float(clock())
        self._local = threading.local()
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        if self.read_only:
            self._verify_schema()
        else:
            self._init_shared()

    # -- shared DDL ------------------------------------------------------------

    def _init_shared(self) -> None:
        con = self._raw_connection()
        try:
            configure_journal(con)
            con.execute("PRAGMA synchronous=FULL")
            con.executescript(MERGED_SCHEMA_DDL)
            for owner in self.owners:
                con.execute(
                    "INSERT OR IGNORE INTO c6_writer_epoch(owner,active_epoch,instance_id,writer_token) "
                    "VALUES(?,0,'unfenced-test','unfenced-test')", (owner,))
            con.execute("INSERT OR IGNORE INTO schema_meta(key,value) VALUES('version','1')")
            con.execute("INSERT OR IGNORE INTO schema_meta(key,value) VALUES('life_supply_layout',?)",
                        (SCHEMA_LAYOUT,))
            for view, table in LOGICAL_VIEWS.items():
                con.execute(f"CREATE VIEW IF NOT EXISTS {view} AS SELECT * FROM {table}")
            for owner in self.owners:
                activate_owner_epoch(con, owner, self.fences.get(owner))
            # install the per-owner mutation guards last, so every epoch row is already set
            install_owner_write_guards(con)
        finally:
            con.close()

    def _verify_schema(self) -> None:
        with self.connection() as con:
            tables = {row[0] for row in con.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table','view')").fetchall()}
            missing = sorted(set(TABLE_OWNER) - tables)
            if missing:
                raise RuntimeError(f"C7_SINGLE_DB_SCHEMA_REQUIRED:missing={','.join(missing)}")
            present = {row[0] for row in con.execute(
                "SELECT owner FROM c6_writer_epoch").fetchall()}
            absent = sorted(set(self.owners) - present)
            if absent:
                raise RuntimeError(f"C7_SINGLE_DB_OWNER_EPOCH_MISSING:{','.join(absent)}")

    # -- connections -----------------------------------------------------------

    def _raw_connection(self) -> sqlite3.Connection:
        uri = f"file:{self.path}?mode=ro" if self.read_only else self.path
        con = sqlite3.connect(uri, uri=self.read_only, timeout=10, isolation_level=None)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys=ON")
        con.execute("PRAGMA busy_timeout=10000")
        return con

    def _open(self) -> sqlite3.Connection:
        con = self._raw_connection()
        try:
            register_owner_writer_connection(con, self.owners, self.fences)
        except Exception:
            con.close()
            raise
        return con

    @property
    def active_unit_of_work(self) -> Optional["LifeSupplyUnitOfWork"]:
        return getattr(self._local, "uow", None)

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        """Yield the ambient unit-of-work connection, or a fresh one.

        Inside a ``unit_of_work()`` every owner store sees the *same* connection, which is what
        makes an authorise -> effect -> receipt sequence atomic.
        """
        active = self.active_unit_of_work
        if active is not None:
            yield active.con
            return
        con = self._open()
        try:
            yield con
        finally:
            con.close()

    def unit_of_work(self, *, command: str, operation_id: str, subject_id: str | None = None,
                     caller_principal: str = DEFAULT_PRINCIPAL) -> "LifeSupplyUnitOfWork":
        return LifeSupplyUnitOfWork(self, command=command, operation_id=operation_id,
                                    subject_id=subject_id, caller_principal=caller_principal)

    # -- observation -----------------------------------------------------------

    def layout_report(self) -> dict[str, Any]:
        """Read-only description of the physical layout: one file, three owners."""
        with self.connection() as con:
            tables = sorted(row[0] for row in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table'").fetchall())
            views = sorted(row[0] for row in con.execute(
                "SELECT name FROM sqlite_master WHERE type='view'").fetchall())
            epochs = {row["owner"]: dict(row) for row in con.execute(
                "SELECT owner,active_epoch,instance_id,writer_token FROM c6_writer_epoch").fetchall()}
            guards = sorted(row[0] for row in con.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger' AND name LIKE 'c7_fence_%'").fetchall())
            journal = con.execute("SELECT COUNT(*) FROM life_supply_uow_journal").fetchone()[0]
            layout = con.execute(
                "SELECT value FROM schema_meta WHERE key='life_supply_layout'").fetchone()
        return {
            "path": self.path,
            "single_file": True,
            "layout": None if layout is None else layout[0],
            "owners": list(self.owners),
            "owner_tables": {owner: list(tables_) for owner, tables_ in OWNER_TABLES.items()},
            "tables": tables,
            "logical_views": views,
            "writer_epoch_rows": epochs,
            "guard_triggers": guards,
            "uow_journal_rows": int(journal),
        }


# ---------------------------------------------------------------------------------
# the unit of work (C7 PHASE 02)
# ---------------------------------------------------------------------------------


class LifeSupplyUnitOfWork:
    """The transaction boundary for the five coordinated commands.

    It performs no domain judgement and owns no business fact.  Its guarantees:

    * the whole command is one ``BEGIN IMMEDIATE`` ... ``COMMIT`` on the single canonical file;
    * every fact it records is attributed to a table's real owner (``note`` refuses otherwise);
    * a journal row is written **inside** the transaction, so a committed command always leaves
      exactly one journal row and a rolled-back command leaves none -- there is no window in
      which the effects exist without their command record.
    """

    def __init__(self, db: LifeSupplyDB, *, command: str, operation_id: str,
                 subject_id: str | None = None,
                 caller_principal: str = DEFAULT_PRINCIPAL) -> None:
        if command not in CROSS_OWNER_COMMANDS:
            raise CrossOwnerCommandRefused(
                f"NOT_A_COORDINATED_COMMAND:{command}:"
                f"allowed={sorted(CROSS_OWNER_COMMANDS)}")
        if not operation_id:
            raise ValueError("operation_id is required for a coordinated command")
        self.contract = COMMAND_CONTRACTS.get(command) or CommandContract(command, {})
        self.db = db
        self.command = command
        self.operation_id = operation_id
        self.subject_id = subject_id
        self.caller_principal = caller_principal
        self.uow_id = f"uow:{uuid.uuid4().hex}"
        self.started_at = float(db.clock())
        self.facts: list[dict[str, Any]] = []
        self.con: sqlite3.Connection | None = None
        self._finished = False
        self._doomed: str | None = None
        self._parts_committed = 0
        self._steps: list[dict[str, Any]] = []
        self._undeclared_steps: list[str] = []
        self._params: dict[str, Any] = {}

    # -- store participation ---------------------------------------------------

    def doom(self, reason: str) -> None:
        """A participating store refused its part: the whole command must roll back."""
        if self._doomed is None:
            self._doomed = reason or "a participating store rolled back"

    def mark_part_committed(self) -> None:
        """A store finished its own part.  It does not end the coordinated transaction."""
        self._parts_committed += 1

    @property
    def doomed(self) -> str | None:
        return self._doomed

    def join(self) -> "_JoinedConnection":
        """Let a store use the coordinated connection without ending its transaction."""
        return _JoinedConnection(self)

    #: Fallback statuses for a step this command's contract does not declare at all.  A
    #: declared step is always judged by its own contract entry, never by this set: ``ALLOW``
    #: proves an authorisation check passed, it does not prove a Workspace row now exists.
    UNCONTRACTED_STEP_STATUSES = frozenset({"OK", "CREATED", "NO_CHANGE", "ALLOW"})

    def param(self, key: str, value: Any) -> None:
        """Record a command parameter that a contract postcondition may inspect."""
        self._params[key] = value

    def step(self, result: Any, *, owner: str, action: str) -> Any:
        """Record one participant's result against *this command's* contract.

        A store's refusal is a return value, not an exception, and several refusal paths do not
        roll back on their own, so every participant result must pass through here.  The
        allowed statuses come from the command's own contract (``COMMAND_CONTRACTS``) rather
        than from one loose global success set.
        """
        if isinstance(result, Mapping):
            status = result.get("status")
        else:
            status = getattr(result, "status", None)
        allowed = self.contract.statuses_for(owner, action)
        if allowed is None:
            allowed = self.UNCONTRACTED_STEP_STATUSES
            self._undeclared_steps.append(f"{owner}.{action}")
        self._steps.append({"owner": owner, "action": action, "status": status})
        if status not in allowed:
            self.doom(f"{owner}.{action} returned {status}; "
                      f"contract allows {sorted(allowed)}")
        return result

    # -- lifecycle -------------------------------------------------------------

    @property
    def is_live(self) -> bool:
        """True only between a successful ``__enter__`` and the end of the unit of work."""
        return self.con is not None and not self._finished

    def __enter__(self) -> "LifeSupplyUnitOfWork":
        if self.con is not None or self._finished:
            raise UnitOfWorkDoomed(f"UNIT_OF_WORK_ALREADY_USED:{self.uow_id}")
        if self.db.active_unit_of_work is not None:
            raise NestedUnitOfWorkRefused(
                f"NESTED_UNIT_OF_WORK:{self.db.active_unit_of_work.command}")
        con = self.db._open()
        con.execute("BEGIN IMMEDIATE")
        self.con = con
        # every command knows its own operation identity; the postconditions rely on it
        self._params.setdefault("operation_id", self.operation_id)
        self.db._local.uow = self
        return self

    @property
    def _open_con(self) -> sqlite3.Connection:
        if self.con is None:
            raise RuntimeError("unit of work is not open")
        return self.con

    def note(self, owner: str, table: str, fact: str, detail: Any = None) -> None:
        """Attribute one write to its owner.  Refuses a table the owner does not own."""
        actual = TABLE_OWNER.get(table)
        if actual is None:
            raise OwnerTableMismatch(f"UNKNOWN_OWNER_TABLE:{table}")
        if actual != owner:
            raise OwnerTableMismatch(f"TABLE_OWNED_BY_OTHERS:{table}:owner={actual}:claimed={owner}")
        self.facts.append({"owner": owner, "table": table, "fact": fact, "detail": detail})

    @property
    def participants(self) -> list[str]:
        owners = {fact["owner"] for fact in self.facts}
        owners |= {str(step["owner"]) for step in self._steps}
        return sorted(owners)

    def commit(self) -> dict[str, Any]:
        if self._doomed is not None:
            raise UnitOfWorkDoomed(f"COORDINATED_COMMAND_DOOMED:{self._doomed}")
        con = self._open_con
        # A command may not commit until *its own* contract is satisfied: every required step
        # registered, and every postcondition true against this very transaction.
        missing = self.contract.missing_steps(self._steps)
        if missing:
            reason = "CONTRACT_STEPS_MISSING:" + ",".join(
                sorted(f"{owner}:{action}" for owner, action in missing))
            self.doom(reason)
            raise CommandContractViolation(reason)
        results: list[dict[str, Any]] = []
        for postcondition in self.contract.postconditions:
            ok, detail = postcondition.check(con, self._params)
            results.append({"name": postcondition.name, "ok": bool(ok), "detail": detail})
            if not ok:
                reason = f"CONTRACT_POSTCONDITION_FAILED:{postcondition.name}:{detail}"
                self.doom(reason)
                raise CommandContractViolation(reason)
        finished = float(self.db.clock())
        con.execute(
            "INSERT INTO life_supply_uow_journal(uow_id,command,operation_id,subject_id,"
            "caller_principal,participants,facts,outcome,started_at,finished_at) "
            "VALUES(?,?,?,?,?,?,?,'COMMITTED',?,?)",
            (self.uow_id, self.command, self.operation_id, self.subject_id,
             self.caller_principal, _json(self.participants), _json(self.facts),
             self.started_at, finished))
        con.execute("COMMIT")
        self._finished = True
        return {"status": "COMMITTED", "uow_id": self.uow_id, "command": self.command,
                "operation_id": self.operation_id, "participants": self.participants,
                "facts": len(self.facts), "steps": list(self._steps),
                "undeclared_steps": list(self._undeclared_steps),
                "postconditions": results}

    def rollback(self) -> None:
        if self.con is not None and not self._finished:
            self.con.execute("ROLLBACK")
        self._finished = True

    def __exit__(self, exc_type, exc, tb) -> bool:
        doomed = False
        try:
            if exc_type is not None or self._doomed is not None:
                self.rollback()
                doomed = exc_type is None and self._doomed is not None
            elif not self._finished:
                try:
                    self.commit()
                except Exception:
                    # a failing commit (e.g. a duplicate journal key) must leave nothing behind
                    self.rollback()
                    raise
        finally:
            self.db._local.uow = None
            if self.con is not None:
                self.con.close()
                self.con = None
        if doomed:
            # a refusal inside a coordinated command must never be reported as success
            raise UnitOfWorkDoomed(f"COORDINATED_COMMAND_DOOMED:{self._doomed}")
        return False
