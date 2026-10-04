from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Protocol
import json
import logging
import secrets
from app.sqlite_safety import configure_journal
import sqlite3
import time
import urllib.parse
import uuid

from .evidence import EvidenceStore
from .model import DirectProvider


M1_ALGORITHM_VERSION = "m1-shadow-v0.1"
ALLOWED_DECISIONS = {
    "CONTINUE_EXISTING",
    "START_NEW",
    "SHARE_WITH_EXISTING",
    "CLOSE_EXISTING",
}


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


def _secure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    path.chmod(0o700)


class M0EvidenceReader:
    """Read-only view of M0. It has no write, update, or delete operation."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        if not self.path.exists():
            raise FileNotFoundError(str(self.path))

    def _connect(self) -> sqlite3.Connection:
        encoded = urllib.parse.quote(str(self.path), safe="/:")
        connection = sqlite3.connect(
            "file:" + encoded + "?mode=ro", uri=True, timeout=5.0
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        connection.execute("PRAGMA busy_timeout=5000")
        return connection

    @staticmethod
    def _row_to_event(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["source_refs"] = json.loads(result.pop("source_refs_json"))
        return result

    def list_events(self) -> list[dict[str, Any]]:
        connection = self._connect()
        try:
            rows = connection.execute(
                "SELECT * FROM evidence_events "
                "ORDER BY occurred_at, created_at, event_id"
            ).fetchall()
            return [self._row_to_event(row) for row in rows]
        finally:
            connection.close()

    def get(self, event_id: str) -> dict[str, Any] | None:
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT * FROM evidence_events WHERE event_id=?", (event_id,)
            ).fetchone()
            return self._row_to_event(row) if row is not None else None
        finally:
            connection.close()


@dataclass(frozen=True)
class EpisodeDecision:
    decision: str
    attach: list[str]
    start_new: bool = False
    shared: bool = False
    close: list[str] | None = None
    reason: str = ""
    error: str | None = None

    def __post_init__(self) -> None:
        if self.decision not in ALLOWED_DECISIONS:
            raise ValueError("invalid M1 boundary decision")
        if any(not isinstance(item, str) or not item for item in self.attach):
            raise ValueError("attach must contain episode ids")
        if self.close is not None and any(
            not isinstance(item, str) or not item for item in self.close
        ):
            raise ValueError("close must contain episode ids")
        if len(self.reason) > 160:
            raise ValueError("M1 reason is too long")

    @classmethod
    def from_mapping(cls, data: dict[str, Any]) -> "EpisodeDecision":
        decision = str(data.get("decision", ""))
        attach = data.get("attach") or []
        close = data.get("close")
        if not isinstance(attach, list) or (close is not None and not isinstance(close, list)):
            raise ValueError("attach/close must be arrays")
        return cls(
            decision=decision,
            attach=[str(item) for item in attach],
            start_new=bool(data.get("start_new", False)),
            shared=bool(data.get("shared", False)),
            close=[str(item) for item in close] if close is not None else None,
            reason=str(data.get("reason", ""))[:160],
        )

    def audit_json(self) -> dict[str, Any]:
        return {
            "decision": self.decision,
            "attach": list(self.attach),
            "start_new": self.start_new,
            "shared": self.shared,
            "close": list(self.close or []),
            "reason": self.reason,
        }


class BoundaryJudge(Protocol):
    def decide(
        self,
        current_event: dict[str, Any],
        open_episodes: list[dict[str, Any]],
    ) -> EpisodeDecision:
        ...


class ConservativeBoundaryJudge:
    """Fail-open fallback: preserve local continuity rather than inventing splits."""

    def decide(
        self,
        current_event: dict[str, Any],
        open_episodes: list[dict[str, Any]],
    ) -> EpisodeDecision:
        if not open_episodes:
            return EpisodeDecision("START_NEW", [], start_new=True, reason="first event")
        return EpisodeDecision(
            "CONTINUE_EXISTING",
            [str(open_episodes[-1]["episode_id"])],
            reason="conservative fallback",
        )


class ProviderBoundaryJudge:
    """Narrow structured judge; its response is never used as prompt context."""

    SYSTEM_TEXT = (
        "You are the Chiyo Native M1 episode boundary judge. "
        "Only decide whether the current observed event is part of the same "
        "local real-world episode as the open episodes. Return JSON only with "
        "keys decision, attach, start_new, shared, close, reason. decision "
        "must be one of CONTINUE_EXISTING, START_NEW, SHARE_WITH_EXISTING, "
        "CLOSE_EXISTING. attach and close contain only supplied episode ids. "
        "Do not infer importance, emotion, relationship meaning, personality, "
        "preference, memory value, future behavior, or recall. Do not write a "
        "summary. reason must be a short structural reason."
    )

    def __init__(self, config: dict[str, Any]):
        sampling = dict(config.get("sampling") or {})
        sampling.setdefault("temperature", 0.0)
        sampling.setdefault("top_p", 1.0)
        sampling.setdefault("frequency_penalty", 0)
        sampling.setdefault("presence_penalty", 0)
        provider_config = SimpleNamespace(
            model=str(config.get("model", "deepseek/deepseek-v4-flash")),
            endpoint=str(config["base_url"]).rstrip("/")
            + "/"
            + str(config.get("api_path", "/chat/completions")).lstrip("/"),
            api_key_env=str(config.get("api_key_env", "COMMANDCODE_API_KEY")),
            timeout_seconds=float(config.get("timeout_seconds", 60)),
            sampling=sampling,
        )
        self.provider = DirectProvider(provider_config)

    def decide(
        self,
        current_event: dict[str, Any],
        open_episodes: list[dict[str, Any]],
    ) -> EpisodeDecision:
        episode_ids = {str(item["episode_id"]) for item in open_episodes}
        safe_episodes = []
        for episode in open_episodes:
            safe_episodes.append(
                {
                    "episode_id": episode["episode_id"],
                    "started_at": episode["started_at"],
                    "last_event_at": episode.get("last_event_at"),
                    "recent_events": episode.get("recent_events", []),
                }
            )
        safe_event = {
            key: current_event.get(key)
            for key in (
                "event_id",
                "occurred_at",
                "source_origin",
                "speaker",
                "content",
                "conversation_id",
                "turn_id",
            )
        }
        user_text = json.dumps(
            {"current_event": safe_event, "open_episodes": safe_episodes},
            ensure_ascii=False,
            separators=(",", ":"),
        )
        result = self.provider.complete(
            [
                {"role": "system", "content": self.SYSTEM_TEXT},
                {"role": "user", "content": user_text},
            ]
        )
        raw = result.content.strip()
        start = raw.find("{")
        end = raw.rfind("}")
        if start < 0 or end <= start:
            raise ValueError("M1 judge did not return JSON")
        data = json.loads(raw[start : end + 1])
        decision = EpisodeDecision.from_mapping(data)
        invalid = set(decision.attach + list(decision.close or [])) - episode_ids
        if invalid:
            raise ValueError("M1 judge referenced an unknown episode")
        return decision


@dataclass(frozen=True)
class ApplyResult:
    status: str
    episode_ids: list[str]
    membership_kinds: dict[str, str]


class EpisodeStore:
    """Independent, rebuildable derived store. It never writes M0."""

    def __init__(self, path: str | Path, algorithm_version: str = M1_ALGORITHM_VERSION):
        self.path = Path(path)
        _secure_dir(self.path.parent)
        self.algorithm_version = algorithm_version
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
            connection.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations ("
                "version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
            )
            if connection.execute(
                "SELECT 1 FROM schema_migrations WHERE version=1"
            ).fetchone() is None:
                connection.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS episodes (
                        episode_id TEXT PRIMARY KEY,
                        memory_owner TEXT NOT NULL,
                        started_at TEXT NOT NULL,
                        ended_at TEXT,
                        status TEXT NOT NULL CHECK(status IN ('OPEN','CLOSED')),
                        algorithm_version TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS episode_evidence (
                        episode_id TEXT NOT NULL REFERENCES episodes(episode_id) ON DELETE CASCADE,
                        event_id TEXT NOT NULL,
                        membership_kind TEXT NOT NULL CHECK(membership_kind IN ('PRIMARY','SHARED')),
                        sequence_index INTEGER NOT NULL,
                        attached_at TEXT NOT NULL,
                        PRIMARY KEY(episode_id, event_id),
                        UNIQUE(episode_id, sequence_index)
                    );
                    CREATE TABLE IF NOT EXISTS processed_evidence (
                        event_id TEXT PRIMARY KEY,
                        processed_at TEXT NOT NULL,
                        algorithm_version TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS segment_decisions (
                        event_id TEXT PRIMARY KEY,
                        decision_json TEXT NOT NULL,
                        episode_ids_json TEXT NOT NULL,
                        algorithm_version TEXT NOT NULL,
                        latency_ms REAL,
                        error TEXT,
                        created_at TEXT NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS episode_started_idx ON episodes(started_at);
                    CREATE INDEX IF NOT EXISTS episode_status_idx ON episodes(status);
                    CREATE INDEX IF NOT EXISTS episode_event_idx ON episode_evidence(event_id);
                    CREATE INDEX IF NOT EXISTS episode_membership_kind_idx ON episode_evidence(membership_kind);
                    """
                )
                connection.execute(
                    "INSERT INTO schema_migrations(version, applied_at) VALUES(1, ?)",
                    (utc_now(),),
                )
            connection.commit()
        finally:
            connection.close()

    def processed_ids(self) -> set[str]:
        connection = self._connect()
        try:
            return {
                str(row[0])
                for row in connection.execute("SELECT event_id FROM processed_evidence")
            }
        finally:
            connection.close()

    def open_episodes(self) -> list[dict[str, Any]]:
        connection = self._connect()
        try:
            rows = connection.execute(
                "SELECT * FROM episodes WHERE status='OPEN' ORDER BY started_at, episode_id"
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            connection.close()

    def memberships_for_episode(self, episode_id: str, limit: int = 8) -> list[dict[str, Any]]:
        connection = self._connect()
        try:
            rows = connection.execute(
                "SELECT * FROM episode_evidence WHERE episode_id=? "
                "ORDER BY sequence_index DESC LIMIT ?",
                (episode_id, max(1, min(limit, 100))),
            ).fetchall()
            return [dict(row) for row in reversed(rows)]
        finally:
            connection.close()

    @staticmethod
    def _next_sequence(connection: sqlite3.Connection, episode_id: str) -> int:
        row = connection.execute(
            "SELECT COALESCE(MAX(sequence_index), -1) + 1 FROM episode_evidence "
            "WHERE episode_id=?",
            (episode_id,),
        ).fetchone()
        return int(row[0])

    @staticmethod
    def _create_episode(
        connection: sqlite3.Connection,
        event: dict[str, Any],
        algorithm_version: str,
    ) -> str:
        episode_id = new_id()
        now = utc_now()
        connection.execute(
            "INSERT INTO episodes(episode_id, memory_owner, started_at, ended_at, "
            "status, algorithm_version, created_at, updated_at) VALUES(?, ?, ?, NULL, 'OPEN', ?, ?, ?)",
            (
                episode_id,
                str(event["memory_owner"]),
                str(event["occurred_at"]),
                algorithm_version,
                now,
                now,
            ),
        )
        return episode_id

    def apply_event(
        self,
        event: dict[str, Any],
        decision: EpisodeDecision,
        latency_ms: float | None = None,
        error: str | None = None,
    ) -> ApplyResult:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            event_id = str(event["event_id"])
            if connection.execute(
                "SELECT 1 FROM processed_evidence WHERE event_id=?", (event_id,)
            ).fetchone() is not None:
                connection.commit()
                return ApplyResult("duplicate", [], {})

            open_rows = connection.execute(
                "SELECT episode_id FROM episodes WHERE status='OPEN' "
                "ORDER BY started_at, episode_id"
            ).fetchall()
            open_ids = [str(row[0]) for row in open_rows]
            valid_attach = [item for item in decision.attach if item in open_ids]
            close_ids = [item for item in (decision.close or []) if item in open_ids]
            if decision.decision in {"START_NEW", "CLOSE_EXISTING"}:
                close_ids = open_ids if not close_ids else close_ids
            for episode_id in close_ids:
                connection.execute(
                    "UPDATE episodes SET status='CLOSED', ended_at=?, updated_at=? "
                    "WHERE episode_id=? AND status='OPEN'",
                    (str(event["occurred_at"]), utc_now(), episode_id),
                )

            membership_specs: list[tuple[str, str]] = []
            if decision.decision == "SHARE_WITH_EXISTING" or decision.shared:
                if not valid_attach and open_ids:
                    valid_attach = [open_ids[-1]]
                for episode_id in valid_attach:
                    membership_specs.append((episode_id, "SHARED"))
                membership_specs.append(
                    (self._create_episode(connection, event, self.algorithm_version), "PRIMARY")
                )
            elif decision.decision in {"START_NEW", "CLOSE_EXISTING"} or decision.start_new:
                membership_specs.append(
                    (self._create_episode(connection, event, self.algorithm_version), "PRIMARY")
                )
            elif valid_attach:
                membership_specs.append((valid_attach[-1], "PRIMARY"))
            elif not open_ids:
                membership_specs.append(
                    (self._create_episode(connection, event, self.algorithm_version), "PRIMARY")
                )

            membership_kinds: dict[str, str] = {}
            for episode_id, membership_kind in membership_specs:
                sequence = self._next_sequence(connection, episode_id)
                connection.execute(
                    "INSERT OR IGNORE INTO episode_evidence(episode_id, event_id, "
                    "membership_kind, sequence_index, attached_at) VALUES(?, ?, ?, ?, ?)",
                    (episode_id, event_id, membership_kind, sequence, utc_now()),
                )
                membership_kinds[episode_id] = membership_kind
                connection.execute(
                    "UPDATE episodes SET updated_at=? WHERE episode_id=?",
                    (utc_now(), episode_id),
                )

            episode_ids = list(membership_kinds)
            connection.execute(
                "INSERT INTO processed_evidence(event_id, processed_at, algorithm_version) "
                "VALUES(?, ?, ?)",
                (event_id, utc_now(), self.algorithm_version),
            )
            connection.execute(
                "INSERT INTO segment_decisions(event_id, decision_json, episode_ids_json, "
                "algorithm_version, latency_ms, error, created_at) VALUES(?, ?, ?, ?, ?, ?, ?)",
                (
                    event_id,
                    json.dumps(decision.audit_json(), ensure_ascii=False, sort_keys=True),
                    json.dumps(episode_ids, ensure_ascii=False),
                    self.algorithm_version,
                    latency_ms,
                    error[:240] if error else None,
                    utc_now(),
                ),
            )
            connection.commit()
            return ApplyResult("inserted", episode_ids, membership_kinds)
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def clear_derived(self) -> None:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("DELETE FROM segment_decisions")
            connection.execute("DELETE FROM processed_evidence")
            connection.execute("DELETE FROM episode_evidence")
            connection.execute("DELETE FROM episodes")
            connection.commit()
        finally:
            connection.close()

    def list_episodes(self, limit: int = 1000) -> list[dict[str, Any]]:
        connection = self._connect()
        try:
            rows = connection.execute(
                "SELECT * FROM episodes ORDER BY started_at, episode_id LIMIT ?",
                (max(1, min(limit, 1000)),),
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

    def memberships_for_event(self, event_id: str) -> list[dict[str, Any]]:
        connection = self._connect()
        try:
            rows = connection.execute(
                "SELECT * FROM episode_evidence WHERE event_id=? ORDER BY episode_id",
                (event_id,),
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            connection.close()

    def verify(self, evidence: M0EvidenceReader | None = None) -> dict[str, Any]:
        issues: list[str] = []
        connection = self._connect()
        try:
            version = connection.execute(
                "SELECT MAX(version) FROM schema_migrations"
            ).fetchone()[0]
            if version != 1:
                issues.append("schema_version_not_1")
            episodes = connection.execute("SELECT * FROM episodes").fetchall()
            memberships = connection.execute("SELECT * FROM episode_evidence").fetchall()
            processed = connection.execute("SELECT * FROM processed_evidence").fetchall()
            decisions = connection.execute("SELECT * FROM segment_decisions").fetchall()
            episode_ids = {str(row["episode_id"]) for row in episodes}
            membership_event_ids = {str(row["event_id"]) for row in memberships}
            processed_ids = {str(row["event_id"]) for row in processed}
            if processed_ids != {str(row["event_id"]) for row in decisions}:
                issues.append("processed_decision_mismatch")
            if not membership_event_ids <= processed_ids:
                issues.append("unprocessed_membership")
            if any(str(row["episode_id"]) not in episode_ids for row in memberships):
                issues.append("orphan_episode_membership")
            if evidence is not None:
                source_events = {str(row["event_id"]) for row in evidence.list_events()}
                if not membership_event_ids <= source_events:
                    issues.append("membership_event_not_in_m0")
                if not processed_ids <= source_events:
                    issues.append("processed_event_not_in_m0")
                total_evidence = len(source_events)
            else:
                total_evidence = None
            counts = connection.execute(
                "SELECT event_id, COUNT(*) AS count FROM episode_evidence GROUP BY event_id"
            ).fetchall()
            multi = [row for row in counts if int(row["count"]) > 1]
            three_plus = [row for row in counts if int(row["count"]) >= 3]
            assigned = len(membership_event_ids)
            return {
                "schema_version": version,
                "episode_count": len(episodes),
                "membership_count": len(memberships),
                "processed_count": len(processed),
                "decision_count": len(decisions),
                "total_evidence": total_evidence,
                "assigned_evidence": assigned,
                "unassigned_evidence": (
                    max(0, total_evidence - assigned)
                    if total_evidence is not None
                    else None
                ),
                "single_membership_evidence": sum(
                    1 for row in counts if int(row["count"]) == 1
                ),
                "multi_membership_evidence": len(multi),
                "three_plus_membership_evidence": len(three_plus),
                "projection_tables": [],
                "issues": issues,
                "ok": not issues,
            }
        finally:
            connection.close()


class EpisodeWorker:
    def __init__(
        self,
        evidence: M0EvidenceReader,
        store: EpisodeStore,
        judge: BoundaryJudge,
        logger: logging.Logger | None = None,
    ):
        self.evidence = evidence
        self.store = store
        self.judge = judge
        self.logger = logger or logging.getLogger("chiyo-native-m1-shadow")

    def _open_context(self) -> list[dict[str, Any]]:
        result = []
        for episode in self.store.open_episodes():
            recent = []
            for membership in self.store.memberships_for_episode(episode["episode_id"]):
                event = self.evidence.get(str(membership["event_id"]))
                if event is not None:
                    recent.append(
                        {
                            "event_id": event["event_id"],
                            "occurred_at": event["occurred_at"],
                            "source_origin": event["source_origin"],
                            "speaker": event["speaker"],
                            "content": event["content"],
                            "membership_kind": membership["membership_kind"],
                        }
                    )
            copy = dict(episode)
            copy["last_event_at"] = recent[-1]["occurred_at"] if recent else None
            copy["recent_events"] = recent
            result.append(copy)
        return result

    def process_once(self) -> dict[str, int]:
        processed_ids = self.store.processed_ids()
        inserted = 0
        duplicate = 0
        failed = 0
        for event in self.evidence.list_events():
            event_id = str(event["event_id"])
            if event_id in processed_ids:
                continue
            open_context = self._open_context()
            started = time.perf_counter()
            error = None
            try:
                if not open_context:
                    decision = EpisodeDecision(
                        "START_NEW", [], start_new=True, reason="first local event"
                    )
                else:
                    decision = self.judge.decide(event, open_context)
            except Exception as exc:
                error = type(exc).__name__ + ": " + str(exc)
                fallback = ConservativeBoundaryJudge()
                decision = fallback.decide(event, open_context)
                self.logger.error(
                    "m1.segment.failed event_id=%s error_class=%s",
                    event_id,
                    type(exc).__name__,
                )
            latency_ms = round((time.perf_counter() - started) * 1000, 3)
            try:
                result = self.store.apply_event(event, decision, latency_ms, error)
                if result.status == "inserted":
                    inserted += 1
                    self.logger.info(
                        "m1.segment.ok event_id=%s decision=%s episode_ids=%s "
                        "algorithm_version=%s latency_ms=%s error=%s",
                        event_id,
                        decision.decision,
                        ",".join(result.episode_ids),
                        self.store.algorithm_version,
                        latency_ms,
                        bool(error),
                    )
                else:
                    duplicate += 1
            except Exception as exc:
                failed += 1
                self.logger.error(
                    "m1.segment.store_failed event_id=%s error_class=%s",
                    event_id,
                    type(exc).__name__,
                )
        return {"inserted": inserted, "duplicate": duplicate, "failed": failed}


def load_m1_config(path: str | Path) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def build_judge(config: dict[str, Any]) -> BoundaryJudge:
    mode = str(config.get("judge_mode", "provider"))
    if mode == "conservative":
        return ConservativeBoundaryJudge()
    if mode == "provider":
        return ProviderBoundaryJudge(config)
    raise ValueError("unsupported M1 judge_mode")
