from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Protocol
from urllib.parse import quote
import hashlib
import json
import logging
import re
import secrets
from app.sqlite_safety import configure_journal
import sqlite3
import uuid

from .m1 import M0EvidenceReader
from .model import DirectProvider


M2_GENERATOR_VERSION = "m2-shadow-v0.3"
M2_SCHEMA_VERSION = 1
ALLOWED_KINDS = {
    "EPISODE_INTERPRETATION",
    "PATTERN_CANDIDATE",
    "CONTRADICTION",
    "UNCERTAINTY",
}
ALLOWED_BASIS = {
    "UTTERANCE_AS_EVENT",
    "UTTERANCE_CONTENT_ASSERTION",
    "CONFIRMED_REALITY",
    "CROSS_EPISODE_PATTERN",
}
CONFIRMED_ORIGINS = {
    "WORLD_EVENT",
    "CONFIRMED_TOOL_RESULT",
    "CONFIRMED_ACTION",
    "BODY_EVENT",
}
ACTIVE_STATUSES = {"ACTIVE", "UNCERTAIN", "DISPUTED", "REVIEW"}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_id() -> str:
    timestamp_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    value = (timestamp_ms & ((1 << 48) - 1)) << 80
    value |= 0x7 << 76
    value |= secrets.randbits(12) << 64
    value |= 0b10 << 62
    value |= secrets.randbits(62)
    return str(uuid.UUID(int=value))


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _secure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    path.chmod(0o700)


class M1ReadOnlyStore:
    """Read-only M1 projection. It never initializes or writes the M1 database."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        if not self.path.exists():
            raise FileNotFoundError(str(self.path))

    def _connect(self) -> sqlite3.Connection:
        encoded = quote(str(self.path), safe="/:")
        connection = sqlite3.connect(
            "file:" + encoded + "?mode=ro", uri=True, timeout=5.0
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        connection.execute("PRAGMA busy_timeout=5000")
        return connection

    def list_episodes(self, status: str = "CLOSED", limit: int = 1000) -> list[dict[str, Any]]:
        connection = self._connect()
        try:
            rows = connection.execute(
                "SELECT * FROM episodes WHERE status=? ORDER BY started_at, episode_id LIMIT ?",
                (status, max(1, min(int(limit), 1000))),
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            connection.close()

    def get_episode(self, episode_id: str) -> dict[str, Any] | None:
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT * FROM episodes WHERE episode_id=?", (episode_id,)
            ).fetchone()
            return dict(row) if row is not None else None
        finally:
            connection.close()

    def memberships_for_episode(self, episode_id: str) -> list[dict[str, Any]]:
        connection = self._connect()
        try:
            rows = connection.execute(
                "SELECT * FROM episode_evidence WHERE episode_id=? ORDER BY sequence_index, event_id",
                (episode_id,),
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            connection.close()

    def snapshot(self, episode_id: str, evidence: M0EvidenceReader) -> dict[str, Any]:
        episode = self.get_episode(episode_id)
        if episode is None:
            raise ValueError("episode_not_found")
        memberships = self.memberships_for_episode(episode_id)
        events: list[dict[str, Any]] = []
        membership_rows: list[dict[str, Any]] = []
        for membership in memberships:
            event = evidence.get(str(membership["event_id"]))
            if event is None:
                raise ValueError("m1_membership_event_missing")
            event_identity = {
                key: event.get(key)
                for key in (
                    "event_id",
                    "occurred_at",
                    "memory_owner",
                    "source_origin",
                    "delivery_status",
                    "epistemic_role",
                    "speaker",
                    "content",
                    "conversation_id",
                    "turn_id",
                    "source_refs",
                    "created_at",
                )
            }
            event_identity["content_identity_sha256"] = digest(event_identity)
            membership_rows.append(
                {
                    "episode_id": str(membership["episode_id"]),
                    "event_id": str(membership["event_id"]),
                    "membership_kind": str(membership["membership_kind"]),
                    "sequence_index": int(membership["sequence_index"]),
                    "event_identity_sha256": event_identity["content_identity_sha256"],
                }
            )
            events.append(event_identity)
        snapshot_body = {
            "episode": {
                key: episode.get(key)
                for key in (
                    "episode_id",
                    "memory_owner",
                    "started_at",
                    "ended_at",
                    "status",
                    "algorithm_version",
                    "created_at",
                    "updated_at",
                )
            },
            "memberships": membership_rows,
            "evidence": events,
        }
        return {**snapshot_body, "snapshot_digest": digest(snapshot_body)}


@dataclass(frozen=True)
class ValidatedProposal:
    statement: str
    understanding_kind: str
    epistemic_basis: str
    evidence_refs: tuple[str, ...]
    episode_refs: tuple[str, ...]
    status: str
    review_flags: tuple[str, ...] = ()


class M2Store:
    """Independent, rebuildable M2 store. It never opens M0 or M1 for writing."""

    def __init__(self, path: str | Path, initialize: bool = True):
        self.path = Path(path)
        if initialize:
            _secure_dir(self.path.parent)
            self._initialize()
            self.path.chmod(0o600)

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
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version INTEGER PRIMARY KEY,
                    applied_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS understandings (
                    understanding_id TEXT PRIMARY KEY,
                    scope_type TEXT NOT NULL,
                    scope_ref TEXT NOT NULL,
                    statement TEXT NOT NULL,
                    understanding_kind TEXT NOT NULL,
                    status TEXT NOT NULL,
                    epistemic_basis TEXT NOT NULL,
                    source_snapshot_digest TEXT NOT NULL,
                    generator_version TEXT NOT NULL,
                    supersedes_id TEXT,
                    superseded_by_id TEXT,
                    requires_review INTEGER NOT NULL DEFAULT 0,
                    review_flags_json TEXT NOT NULL,
                    formation_time TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS understandings_scope_idx
                    ON understandings(scope_type, scope_ref, created_at);
                CREATE INDEX IF NOT EXISTS understandings_status_idx
                    ON understandings(status, created_at);
                CREATE TABLE IF NOT EXISTS understanding_evidence (
                    understanding_id TEXT NOT NULL REFERENCES understandings(understanding_id),
                    event_id TEXT NOT NULL,
                    ref_role TEXT NOT NULL,
                    PRIMARY KEY(understanding_id, event_id)
                );
                CREATE TABLE IF NOT EXISTS understanding_episodes (
                    understanding_id TEXT NOT NULL REFERENCES understandings(understanding_id),
                    episode_id TEXT NOT NULL,
                    PRIMARY KEY(understanding_id, episode_id)
                );
                CREATE TABLE IF NOT EXISTS formation_runs (
                    run_id TEXT PRIMARY KEY,
                    scope_type TEXT NOT NULL,
                    scope_ref TEXT NOT NULL,
                    source_snapshot_digest TEXT NOT NULL,
                    generator_version TEXT NOT NULL,
                    status TEXT NOT NULL,
                    proposal_count INTEGER NOT NULL,
                    accepted_count INTEGER NOT NULL,
                    rejected_count INTEGER NOT NULL,
                    response_digest TEXT,
                    error TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS formation_scope_idx
                    ON formation_runs(scope_type, scope_ref, created_at);
                CREATE TABLE IF NOT EXISTS processed_snapshots (
                    scope_type TEXT NOT NULL,
                    scope_ref TEXT NOT NULL,
                    source_snapshot_digest TEXT NOT NULL,
                    generator_version TEXT NOT NULL,
                    status TEXT NOT NULL,
                    processed_at TEXT NOT NULL,
                    PRIMARY KEY(scope_type, scope_ref, source_snapshot_digest, generator_version)
                );
                """
            )
            row = connection.execute(
                "SELECT 1 FROM schema_migrations WHERE version=?", (M2_SCHEMA_VERSION,)
            ).fetchone()
            if row is None:
                connection.execute(
                    "INSERT INTO schema_migrations(version, applied_at) VALUES(?, ?)",
                    (M2_SCHEMA_VERSION, utc_now()),
                )
            connection.commit()
        finally:
            connection.close()

    def has_processed(
        self, scope_type: str, scope_ref: str, snapshot_digest: str,
        generator_version: str = M2_GENERATOR_VERSION,
    ) -> bool:
        connection = self._connect()
        try:
            return connection.execute(
                "SELECT 1 FROM processed_snapshots WHERE scope_type=? AND scope_ref=? "
                "AND source_snapshot_digest=? AND generator_version=?",
                (scope_type, scope_ref, snapshot_digest, generator_version),
            ).fetchone() is not None
        finally:
            connection.close()

    def record_failure(
        self, scope_type: str, scope_ref: str, snapshot_digest: str, error: str,
        generator_version: str = M2_GENERATOR_VERSION,
    ) -> None:
        connection = self._connect()
        try:
            connection.execute(
                "INSERT INTO formation_runs(run_id, scope_type, scope_ref, source_snapshot_digest, "
                "generator_version, status, proposal_count, accepted_count, rejected_count, error, created_at) "
                "VALUES(?, ?, ?, ?, ?, 'FAILED', 0, 0, 0, ?, ?)",
                (new_id(), scope_type, scope_ref, snapshot_digest, generator_version, error[:500], utc_now()),
            )
            connection.commit()
        finally:
            connection.close()

    def record_result(
        self,
        scope_type: str,
        scope_ref: str,
        snapshot_digest: str,
        proposals: list[ValidatedProposal],
        response_digest: str,
        generator_version: str = M2_GENERATOR_VERSION,
    ) -> dict[str, int | str]:
        accepted = [item for item in proposals if item.status != "REJECTED"]
        rejected = [item for item in proposals if item.status == "REJECTED"]
        run_status = "COMPLETED" if accepted else "NO_STABLE_UNDERSTANDING"
        if accepted and rejected:
            run_status = "COMPLETED_WITH_REJECTIONS"
        connection = self._connect()
        inserted_ids: list[str] = []
        try:
            connection.execute("BEGIN IMMEDIATE")
            if connection.execute(
                "SELECT 1 FROM processed_snapshots WHERE scope_type=? AND scope_ref=? "
                "AND source_snapshot_digest=? AND generator_version=?",
                (scope_type, scope_ref, snapshot_digest, generator_version),
            ).fetchone() is not None:
                connection.commit()
                return {"status": "duplicate", "accepted": 0, "rejected": 0}

            supersede_targets = [
                str(row[0])
                for row in connection.execute(
                    "SELECT understanding_id FROM understandings WHERE scope_type=? AND scope_ref=? "
                    "AND status IN ('ACTIVE','UNCERTAIN','DISPUTED','REVIEW')",
                    (scope_type, scope_ref),
                ).fetchall()
            ]
            for proposal in accepted + rejected:
                understanding_id = new_id()
                inserted_ids.append(understanding_id)
                supersedes_id = (
                    supersede_targets[0]
                    if supersede_targets and accepted and not inserted_ids[:-1]
                    else None
                )
                status = proposal.status
                connection.execute(
                    "INSERT INTO understandings(understanding_id, scope_type, scope_ref, statement, "
                    "understanding_kind, status, epistemic_basis, source_snapshot_digest, generator_version, "
                    "supersedes_id, superseded_by_id, requires_review, review_flags_json, formation_time, created_at) "
                    "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?)",
                    (
                        understanding_id,
                        scope_type,
                        scope_ref,
                        proposal.statement,
                        proposal.understanding_kind,
                        status,
                        proposal.epistemic_basis,
                        snapshot_digest,
                        generator_version,
                        supersedes_id,
                        int(bool(proposal.review_flags)),
                        json.dumps(list(proposal.review_flags), ensure_ascii=False),
                        utc_now(),
                        utc_now(),
                    ),
                )
                for event_id in proposal.evidence_refs:
                    connection.execute(
                        "INSERT INTO understanding_evidence(understanding_id, event_id, ref_role) VALUES(?, ?, ?)",
                        (understanding_id, event_id, "supporting"),
                    )
                for episode_id in proposal.episode_refs:
                    connection.execute(
                        "INSERT INTO understanding_episodes(understanding_id, episode_id) VALUES(?, ?)",
                        (understanding_id, episode_id),
                    )
            if accepted and supersede_targets and inserted_ids:
                replacement_id = inserted_ids[0]
                connection.executemany(
                    "UPDATE understandings SET status='SUPERSEDED', superseded_by_id=? "
                    "WHERE understanding_id=? AND status IN ('ACTIVE','UNCERTAIN','DISPUTED','REVIEW')",
                    [(replacement_id, old_id) for old_id in supersede_targets],
                )
            connection.execute(
                "INSERT INTO formation_runs(run_id, scope_type, scope_ref, source_snapshot_digest, "
                "generator_version, status, proposal_count, accepted_count, rejected_count, response_digest, created_at) "
                "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    new_id(), scope_type, scope_ref, snapshot_digest, generator_version, run_status,
                    len(proposals), len(accepted), len(rejected), response_digest, utc_now(),
                ),
            )
            connection.execute(
                "INSERT INTO processed_snapshots(scope_type, scope_ref, source_snapshot_digest, "
                "generator_version, status, processed_at) VALUES(?, ?, ?, ?, ?, ?)",
                (scope_type, scope_ref, snapshot_digest, generator_version, run_status, utc_now()),
            )
            connection.commit()
            return {"status": run_status, "accepted": len(accepted), "rejected": len(rejected)}
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def list_recent(self, limit: int = 50, status: str | None = None) -> list[dict[str, Any]]:
        connection = self._connect()
        try:
            if status is None:
                rows = connection.execute(
                    "SELECT * FROM understandings ORDER BY created_at DESC LIMIT ?",
                    (max(1, min(int(limit), 500)),),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM understandings WHERE status=? ORDER BY created_at DESC LIMIT ?",
                    (status, max(1, min(int(limit), 500))),
                ).fetchall()
            return [self._row(row) for row in rows]
        finally:
            connection.close()

    def by_episode(self, episode_id: str, limit: int = 100) -> list[dict[str, Any]]:
        connection = self._connect()
        try:
            rows = connection.execute(
                "SELECT u.* FROM understandings u JOIN understanding_episodes ue "
                "ON ue.understanding_id=u.understanding_id WHERE ue.episode_id=? "
                "ORDER BY u.created_at DESC LIMIT ?",
                (episode_id, max(1, min(int(limit), 500))),
            ).fetchall()
            return [self._row(row) for row in rows]
        finally:
            connection.close()

    def evidence_trace(self, understanding_id: str) -> dict[str, Any] | None:
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT * FROM understandings WHERE understanding_id=?", (understanding_id,)
            ).fetchone()
            if row is None:
                return None
            evidence = connection.execute(
                "SELECT event_id, ref_role FROM understanding_evidence WHERE understanding_id=? ORDER BY event_id",
                (understanding_id,),
            ).fetchall()
            episodes = connection.execute(
                "SELECT episode_id FROM understanding_episodes WHERE understanding_id=? ORDER BY episode_id",
                (understanding_id,),
            ).fetchall()
            result = self._row(row)
            result["evidence_refs"] = [dict(item) for item in evidence]
            result["episode_refs"] = [str(item[0]) for item in episodes]
            return result
        finally:
            connection.close()

    def formation_runs(self, limit: int = 100) -> list[dict[str, Any]]:
        connection = self._connect()
        try:
            rows = connection.execute(
                "SELECT * FROM formation_runs ORDER BY created_at DESC LIMIT ?",
                (max(1, min(int(limit), 500)),),
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            connection.close()

    def verify(self) -> dict[str, Any]:
        issues: list[str] = []
        connection = self._connect()
        try:
            version = connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
            if version != M2_SCHEMA_VERSION:
                issues.append("schema_version_mismatch")
            rows = connection.execute("SELECT * FROM understandings").fetchall()
            ids = {str(row["understanding_id"]) for row in rows}
            for row in rows:
                try:
                    flags = json.loads(row["review_flags_json"])
                    if not isinstance(flags, list):
                        issues.append("invalid_review_flags:" + str(row["understanding_id"]))
                except json.JSONDecodeError:
                    issues.append("invalid_review_flags:" + str(row["understanding_id"]))
                if row["understanding_kind"] not in ALLOWED_KINDS:
                    issues.append("invalid_kind:" + str(row["understanding_id"]))
                if row["epistemic_basis"] not in ALLOWED_BASIS:
                    issues.append("invalid_basis:" + str(row["understanding_id"]))
                if row["supersedes_id"] and row["supersedes_id"] not in ids:
                    issues.append("missing_supersedes:" + str(row["understanding_id"]))
                if row["superseded_by_id"] and row["superseded_by_id"] not in ids:
                    issues.append("missing_superseded_by:" + str(row["understanding_id"]))
            orphan_evidence = connection.execute(
                "SELECT COUNT(*) FROM understanding_evidence e LEFT JOIN understandings u "
                "ON u.understanding_id=e.understanding_id WHERE u.understanding_id IS NULL"
            ).fetchone()[0]
            orphan_episodes = connection.execute(
                "SELECT COUNT(*) FROM understanding_episodes e LEFT JOIN understandings u "
                "ON u.understanding_id=e.understanding_id WHERE u.understanding_id IS NULL"
            ).fetchone()[0]
            if orphan_evidence:
                issues.append("orphan_evidence_refs")
            if orphan_episodes:
                issues.append("orphan_episode_refs")
            return {
                "schema_version": version,
                "understanding_count": len(rows),
                "active_count": sum(row["status"] == "ACTIVE" for row in rows),
                "review_count": sum(row["status"] == "REVIEW" for row in rows),
                "rejected_count": sum(row["status"] == "REJECTED" for row in rows),
                "superseded_count": sum(row["status"] == "SUPERSEDED" for row in rows),
                "formation_run_count": connection.execute("SELECT COUNT(*) FROM formation_runs").fetchone()[0],
                "processed_snapshot_count": connection.execute("SELECT COUNT(*) FROM processed_snapshots").fetchone()[0],
                "issues": issues,
                "ok": not issues,
            }
        finally:
            connection.close()

    @staticmethod
    def _row(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        try:
            result["review_flags"] = json.loads(result.pop("review_flags_json"))
        except (KeyError, json.JSONDecodeError):
            pass
        return result


class M2ProposalProvider(Protocol):
    def propose_episode(self, snapshot: dict[str, Any]) -> str:
        ...

    def propose_cross(self, snapshots: list[dict[str, Any]]) -> str:
        ...


class DirectM2ProposalProvider:
    EPISODE_SYSTEM = (
        "You are the Chiyo Native M2 understanding proposer. Return JSON only. "
        "You may propose derived interpretations of the supplied M1 episode and its M0 evidence. "
        "An utterance proves that a speaker said or expressed something; it does not prove that "
        "the described world event happened. Use only UTTERANCE_AS_EVENT or "
        "UTTERANCE_CONTENT_ASSERTION for current evidence. Never output CONFIRMED_REALITY unless "
        "the supplied evidence has a confirmed producer. Do not create behavior advice, lessons, "
        "Self, relationship state, world state, body state, or response suggestions. Preserve uncertainty. "
        "Use keys episode_interpretations, contradictions, uncertainties. Each item must contain "
        "statement, understanding_kind, epistemic_basis, supporting_evidence_refs."
    )
    CROSS_SYSTEM = (
        "You are the Chiyo Native M2 cross-episode consolidation proposer. Return JSON only. "
        "Use only the supplied M1 episodes and referenced M0 evidence. A pattern or contradiction "
        "must cite at least two distinct episode ids and must preserve the episode details. "
        "Do not select a true Self, relationship, preference, world fact, body fact, or future behavior. "
        "Use keys patterns, contradictions, uncertainties. Each item must contain statement, "
        "understanding_kind, epistemic_basis, supporting_episode_refs, supporting_evidence_refs. "
        "Use CROSS_EPISODE_PATTERN for cross-episode items. Return empty arrays when no stable "
        "understanding is supported."
    )

    def __init__(self, config: dict[str, Any]):
        sampling = dict(config.get("sampling") or {})
        provider_config = SimpleNamespace(
            model=str(config["model"]),
            endpoint=str(config["base_url"]).rstrip("/") + "/" + str(config.get("api_path", "/chat/completions")).lstrip("/"),
            api_key_env=str(config.get("api_key_env", "COMMANDCODE_API_KEY")),
            timeout_seconds=float(config.get("timeout_seconds", 60)),
            sampling=sampling,
        )
        self.provider = DirectProvider(provider_config)

    def _complete(self, system: str, payload: dict[str, Any]) -> str:
        result = self.provider.complete(
            [
                {"role": "system", "content": system},
                {"role": "user", "content": canonical_json(payload)},
            ]
        )
        return result.content

    def propose_episode(self, snapshot: dict[str, Any]) -> str:
        return self._complete(self.EPISODE_SYSTEM, {"episode": snapshot})

    def propose_cross(self, snapshots: list[dict[str, Any]]) -> str:
        return self._complete(self.CROSS_SYSTEM, {"episodes": snapshots})


PROVENANCE_RE = re.compile(
    r"说|表示|表达|提到|描述|声称|对话中|回复中|在这次经历|谈到|否认|纠正|said|expressed|mentioned|described|in the episode|in the conversation",
    re.IGNORECASE,
)
REALITY_RE = re.compile(
    r"昨天|今天|前天|刚刚|现在在|去过|去了|吃过|吃了|海边|夕阳|窗外|冰箱|家里|额头|发烧|体温|摸|测|床上|厨房|昨天|today|yesterday|went to|ate|forehead|fever|fridge|home",
    re.IGNORECASE,
)
IMPERATIVE_RE = re.compile(r"应该|必须|下次|以后|建议|请|不要|对用户|need to|should|must|next time|in the future", re.IGNORECASE)
SELF_RELATION_RE = re.compile(r"我是一个|你是一个|我们是|你们是|关系是|intimate|girlfriend|lover|恋人", re.IGNORECASE)


def _parse_json_object(raw: str) -> dict[str, Any]:
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text)
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("m2 output is not a JSON object")
    data = json.loads(text[start : end + 1])
    if not isinstance(data, dict):
        raise ValueError("m2 output root must be an object")
    return data


def _item_list(data: dict[str, Any], keys: set[str]) -> list[tuple[str, dict[str, Any]]]:
    unknown = set(data) - keys
    if unknown:
        raise ValueError("m2 output contains unsupported keys")
    result: list[tuple[str, dict[str, Any]]] = []
    for key in keys:
        values = data.get(key, [])
        if values is None:
            values = []
        if not isinstance(values, list):
            raise ValueError("m2 proposal list is not an array")
        for value in values:
            if not isinstance(value, dict):
                raise ValueError("m2 proposal item is not an object")
            result.append((key, value))
    return result


def _refs(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise ValueError("supporting refs must be a non-empty array")
    result: list[str] = []
    for item in value:
        if isinstance(item, str):
            result.append(item)
        elif isinstance(item, dict) and item.get("id"):
            result.append(str(item["id"]))
        else:
            raise ValueError("supporting refs must contain ids")
    return tuple(dict.fromkeys(result))


def _normalize_event_ref(value: str, event_map: dict[str, dict[str, Any]]) -> str:
    """Normalize only an unambiguous provider event-id label."""
    if value in event_map:
        return value
    for prefix in ("event_id:", "event:"):
        if value.startswith(prefix):
            candidate = value[len(prefix):]
            if candidate in event_map:
                return candidate
    return value


def _review_flags(statement: str, basis: str) -> tuple[str, ...]:
    flags: list[str] = []
    if IMPERATIVE_RE.search(statement):
        flags.append("possible_authority_overreach")
    if SELF_RELATION_RE.search(statement) and not PROVENANCE_RE.search(statement):
        flags.append("possible_self_or_relationship_upgrade")
    if basis not in {"CONFIRMED_REALITY", "CROSS_EPISODE_PATTERN"}:
        if REALITY_RE.search(statement) and not PROVENANCE_RE.search(statement):
            flags.append("possible_unqualified_reality")
    return tuple(dict.fromkeys(flags))


def validate_proposals(
    raw: str,
    snapshot: dict[str, Any],
    *,
    cross: bool = False,
) -> tuple[list[ValidatedProposal], list[str]]:
    data = _parse_json_object(raw)
    keys = {"patterns", "contradictions", "uncertainties"} if cross else {
        "episode_interpretations", "contradictions", "uncertainties"
    }
    items = _item_list(data, keys)
    if cross:
        event_map = {
            str(event["event_id"]): event
            for episode_snapshot in snapshot.get("episodes", [])
            for event in episode_snapshot.get("evidence", [])
        }
    else:
        event_map = {str(event["event_id"]): event for event in snapshot.get("evidence", [])}
    episode_map = {
        str(item["episode"]["episode_id"]): item
        for item in snapshot.get("episodes", [])
    } if cross else {str(snapshot["episode"]["episode_id"]): snapshot}
    current_episode = str(snapshot["episode"]["episode_id"]) if not cross else None
    proposals: list[ValidatedProposal] = []
    issues: list[str] = []
    for key, item in items[:32]:
        try:
            statement = item.get("statement")
            if not isinstance(statement, str) or not statement.strip() or len(statement) > 2000:
                raise ValueError("invalid statement")
            statement = statement.strip()
            raw_kind = str(item.get("understanding_kind") or item.get("kind") or "")
            raw_basis = str(item.get("epistemic_basis") or "")
            default_kind = {
                "episode_interpretations": "EPISODE_INTERPRETATION",
                "patterns": "PATTERN_CANDIDATE",
                "contradictions": "CONTRADICTION",
                "uncertainties": "UNCERTAINTY",
            }[key]
            # Some provider responses place the discrete epistemic label in
            # understanding_kind and put an explanatory sentence in
            # epistemic_basis. Normalize only this unambiguous field swap;
            # never infer a missing reality authority.
            if raw_kind in ALLOWED_BASIS and raw_basis not in ALLOWED_BASIS:
                kind = default_kind
                basis = raw_kind
            else:
                kind = raw_kind or default_kind
                basis = raw_basis
            if kind not in ALLOWED_KINDS:
                raise ValueError("invalid understanding kind")
            if basis not in ALLOWED_BASIS:
                raise ValueError("invalid epistemic basis")
            raw_evidence_refs = _refs(item.get("supporting_evidence_refs"))
            evidence_refs = tuple(
                dict.fromkeys(_normalize_event_ref(ref, event_map) for ref in raw_evidence_refs)
            )
            if any(ref not in event_map for ref in evidence_refs):
                raise ValueError("unknown evidence reference")
            if cross:
                episode_refs = _refs(item.get("supporting_episode_refs"))
                if len(set(episode_refs)) < 2:
                    raise ValueError("cross-episode item needs two distinct episodes")
                if any(ref not in episode_map for ref in episode_refs):
                    raise ValueError("unknown episode reference")
                if basis != "CROSS_EPISODE_PATTERN":
                    raise ValueError("cross-episode item needs CROSS_EPISODE_PATTERN basis")
                if kind not in {"PATTERN_CANDIDATE", "CONTRADICTION", "UNCERTAINTY"}:
                    raise ValueError("invalid cross-episode kind")
            else:
                episode_refs = (current_episode,)
                if basis == "CROSS_EPISODE_PATTERN" or kind == "PATTERN_CANDIDATE":
                    raise ValueError("cross-episode meaning is not valid for one episode")
                if basis == "CONFIRMED_REALITY":
                    confirmed = any(
                        event_map[ref].get("source_origin") in CONFIRMED_ORIGINS
                        for ref in evidence_refs
                    )
                    if not confirmed:
                        raise ValueError("no confirmed reality producer in evidence")
            flags = _review_flags(statement, basis)
            status = "REVIEW" if flags else ("DISPUTED" if kind == "CONTRADICTION" else ("UNCERTAIN" if kind == "UNCERTAINTY" else "ACTIVE"))
            proposals.append(
                ValidatedProposal(statement, kind, basis, evidence_refs, episode_refs, status, flags)
            )
        except (TypeError, ValueError) as exc:
            issues.append(f"proposal_{len(issues) + 1}:{exc}")
            statement = str(item.get("statement", ""))[:2000]
            if statement:
                try:
                    raw_evidence_refs = _refs(item.get("supporting_evidence_refs"))
                    evidence_refs = tuple(
                        dict.fromkeys(_normalize_event_ref(ref, event_map) for ref in raw_evidence_refs)
                    )
                except (TypeError, ValueError):
                    evidence_refs = ()
                fallback_kind = str(item.get("understanding_kind") or item.get("kind") or "")
                if fallback_kind not in ALLOWED_KINDS:
                    fallback_kind = "UNCERTAINTY"
                fallback_basis = str(item.get("epistemic_basis") or "")
                if fallback_basis not in ALLOWED_BASIS:
                    fallback_basis = "CROSS_EPISODE_PATTERN" if cross else "UTTERANCE_CONTENT_ASSERTION"
                proposals.append(
                    ValidatedProposal(
                        statement=statement,
                        understanding_kind=fallback_kind,
                        epistemic_basis=fallback_basis,
                        evidence_refs=evidence_refs,
                        episode_refs=((current_episode,) if current_episode else ()),
                        status="REJECTED",
                        review_flags=("validation_rejected",),
                    )
                )
    if len(items) > 32:
        issues.append("proposal_limit_exceeded")
    return proposals, issues


class M2Worker:
    def __init__(
        self,
        evidence: M0EvidenceReader,
        episodes: M1ReadOnlyStore,
        store: M2Store,
        proposer: M2ProposalProvider,
        *,
        generator_version: str = M2_GENERATOR_VERSION,
        logger: logging.Logger | None = None,
        max_episodes: int = 40,
        enable_cross: bool = True,
    ):
        self.evidence = evidence
        self.episodes = episodes
        self.store = store
        self.proposer = proposer
        self.generator_version = generator_version
        self.logger = logger or logging.getLogger("chiyo-native-m2-shadow")
        self.max_episodes = max(2, min(int(max_episodes), 100))
        self.enable_cross = enable_cross

    @staticmethod
    def _cross_snapshot(snapshots: list[dict[str, Any]]) -> dict[str, Any]:
        body = {
            "episodes": snapshots,
            "episode_ids": sorted(str(item["episode"]["episode_id"]) for item in snapshots),
        }
        return {**body, "snapshot_digest": digest(body)}

    def _record(self, scope_type: str, scope_ref: str, snapshot: dict[str, Any], raw: str, cross: bool) -> dict[str, Any]:
        proposals, issues = validate_proposals(raw, snapshot, cross=cross)
        result = self.store.record_result(
            scope_type,
            scope_ref,
            str(snapshot["snapshot_digest"]),
            proposals,
            digest(raw),
            self.generator_version,
        )
        result["issues"] = len(issues)
        if issues:
            self.logger.warning(
                "m2.validation.rejections scope_type=%s scope_ref=%s issue_count=%s",
                scope_type, scope_ref, len(issues),
            )
        return result

    def process_once(self) -> dict[str, int]:
        snapshots: list[dict[str, Any]] = []
        counts = {"episode_processed": 0, "cross_processed": 0, "accepted": 0, "rejected": 0, "failed": 0, "duplicate": 0}
        for episode in self.episodes.list_episodes(status="CLOSED", limit=self.max_episodes):
            episode_id = str(episode["episode_id"])
            try:
                snapshot = self.episodes.snapshot(episode_id, self.evidence)
                snapshots.append(snapshot)
            except Exception as exc:
                counts["failed"] += 1
                self.logger.error("m2.snapshot.failed episode_id=%s error_class=%s", episode_id, type(exc).__name__)
                continue
            if self.store.has_processed("EPISODE", episode_id, snapshot["snapshot_digest"], self.generator_version):
                counts["duplicate"] += 1
                continue
            try:
                raw = self.proposer.propose_episode(snapshot)
                result = self._record("EPISODE", episode_id, snapshot, raw, False)
                counts["episode_processed"] += 1
                counts["accepted"] += int(result["accepted"])
                counts["rejected"] += int(result["rejected"])
            except Exception as exc:
                counts["failed"] += 1
                self.store.record_failure("EPISODE", episode_id, snapshot["snapshot_digest"], type(exc).__name__ + ": " + str(exc), self.generator_version)
                self.logger.error("m2.episode.failed episode_id=%s error_class=%s", episode_id, type(exc).__name__)

        if self.enable_cross and len(snapshots) >= 2:
            cross_snapshot = self._cross_snapshot(snapshots)
            cross_ref = "cross:" + str(cross_snapshot["snapshot_digest"])
            if self.store.has_processed("CROSS_EPISODE_PATTERN", cross_ref, cross_snapshot["snapshot_digest"], self.generator_version):
                counts["duplicate"] += 1
            else:
                try:
                    raw = self.proposer.propose_cross(snapshots)
                    result = self._record("CROSS_EPISODE_PATTERN", cross_ref, cross_snapshot, raw, True)
                    counts["cross_processed"] += 1
                    counts["accepted"] += int(result["accepted"])
                    counts["rejected"] += int(result["rejected"])
                except Exception as exc:
                    counts["failed"] += 1
                    self.store.record_failure("CROSS_EPISODE_PATTERN", cross_ref, cross_snapshot["snapshot_digest"], type(exc).__name__ + ": " + str(exc), self.generator_version)
                    self.logger.error("m2.cross.failed error_class=%s", type(exc).__name__)
        return counts
