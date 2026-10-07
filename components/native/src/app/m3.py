from __future__ import annotations

"""Chiyo Native M3 Recall / Availability shadow layer.

The module owns only a derived, rebuildable shadow store.  M0, M1 and M2
are opened through read-only SQLite connections.  No result is connected to
Native Context or reply generation.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import logging
import re
from app.sqlite_safety import configure_journal
import sqlite3
from pathlib import Path
from typing import Any
from urllib.parse import quote

from app.m3_recall_support import FixtureCandidateProvider


M3_GENERATOR_VERSION = "m3-shadow-v0.1"
M3_AVAILABILITY_VERSION = "m3-e2-v0.1"
M3_SCHEMA_VERSION = 1
USER_ORIGIN = "USER_VISIBLE_INPUT"
P2_REFLECTION_CUES = (
    "后来怎么看", "回头看", "意味着", "对你有什么意义", "为什么那样说",
    "为什么会那样", "当时为什么", "那次对你", "反思", "怎么看那件事",
)
P1_FACTUAL_CUES = (
    "之前", "以前", "上次", "第一次", "昨天", "最近", "什么时候",
    "说过", "聊过", "回忆", "记得我", "到底怎么", "哪一次",
)
TEMPORAL_CUES = ("之前", "以前", "上次", "第一次", "昨天", "最近", "什么时候")
TOKEN_RE = re.compile(r"[A-Za-z0-9_]+|[\u4e00-\u9fff]")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def connect_ro(path: Path) -> sqlite3.Connection:
    encoded = quote(str(path), safe="/:")
    connection = sqlite3.connect("file:" + encoded + "?mode=ro", uri=True, timeout=5.0)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    connection.execute("PRAGMA busy_timeout=5000")
    return connection


def terms(text: str) -> list[str]:
    values: list[str] = []
    for match in TOKEN_RE.findall(text or ""):
        value = match.lower()
        if len(value) >= 2 or "\u4e00" <= value <= "\u9fff":
            values.append(value)
    return list(dict.fromkeys(values))


def classify_intent(text: str) -> tuple[str, str]:
    normalized = text or ""
    if any(cue in normalized for cue in P2_REFLECTION_CUES):
        return "P2", "EXPLICIT_REFLECTIVE_CUE"
    if any(cue in normalized for cue in P1_FACTUAL_CUES):
        return "P1", "EXPLICIT_FACTUAL_CUE"
    return "P0", "AMBIGUOUS_OR_ORDINARY_DEFAULT_P0"


@dataclass(frozen=True)
class Trigger:
    event: dict[str, Any]
    sequence: int
    intent: str
    intent_reason: str
    epoch_id: str | None
    boundary_id: str | None
    visible_event_ids: tuple[str, ...]
    visible_history_digest: str


class SourceReader:
    """Read-only causal view over M0/M1/M2."""

    def __init__(self, m0_path: str | Path, m1_path: str | Path, m2_path: str | Path, epoch_path: str | Path | None = None):
        self.m0_path = Path(m0_path)
        self.m1_path = Path(m1_path)
        self.m2_path = Path(m2_path)
        self.epoch_path = Path(epoch_path) if epoch_path else None

    @staticmethod
    def _row_event(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["source_refs"] = json.loads(result.pop("source_refs_json"))
        return result

    def events(self) -> list[dict[str, Any]]:
        connection = connect_ro(self.m0_path)
        try:
            rows = connection.execute(
                "SELECT * FROM evidence_events ORDER BY occurred_at, created_at, event_id"
            ).fetchall()
            return [self._row_event(row) for row in rows]
        finally:
            connection.close()

    #: Columns the trigger-discovery path needs. Projected explicitly so the
    #: steady state never pays for SELECT *; the evaluation context still uses
    #: events(), which is deliberately left unchanged.
    DISCOVERY_COLUMNS = (
        "event_id", "occurred_at", "created_at", "conversation_id", "turn_id",
        "source_origin", "delivery_status", "content", "source_refs_json",
    )

    def trigger_candidates_since(self, after_rowid: int, limit: int = 200):
        """One trigger-discovery batch, in M0 append (rowid) order.

        rowid is the consumption order; occurred_at remains the business time and
        is still handled by build_trigger() over the full ordered history. Using
        rowid here means a late-arriving event (larger rowid, earlier occurred_at)
        is still discovered, where an occurred_at watermark would skip it forever.
        """
        columns = ", ".join(self.DISCOVERY_COLUMNS)
        connection = connect_ro(self.m0_path)
        try:
            rows = connection.execute(
                "SELECT rowid AS m0_rowid, " + columns + " FROM evidence_events "
                "WHERE rowid > ? ORDER BY rowid ASC LIMIT ?",
                (int(after_rowid), int(limit)),
            ).fetchall()
            return [(int(row["m0_rowid"]), self._row_event(row)) for row in rows]
        finally:
            connection.close()

    def m0_rowid_for(self, event_id: str) -> int | None:
        """rowid of one M0 event, or None. Used to place the initial watermark."""
        connection = connect_ro(self.m0_path)
        try:
            row = connection.execute(
                "SELECT rowid FROM evidence_events WHERE event_id=?", (event_id,)
            ).fetchone()
            return int(row[0]) if row is not None else None
        finally:
            connection.close()

    def m1_data(self) -> tuple[list[dict[str, Any]], dict[str, list[dict[str, Any]]]]:
        connection = connect_ro(self.m1_path)
        try:
            episodes = [dict(row) for row in connection.execute("SELECT * FROM episodes ORDER BY started_at, episode_id")]
            membership: dict[str, list[dict[str, Any]]] = {}
            for row in connection.execute("SELECT * FROM episode_evidence ORDER BY episode_id, sequence_index"):
                membership.setdefault(str(row["episode_id"]), []).append(dict(row))
            return episodes, membership
        finally:
            connection.close()

    def m2_data(self) -> list[dict[str, Any]]:
        connection = connect_ro(self.m2_path)
        try:
            rows = connection.execute(
                "SELECT understanding_id, status, understanding_kind, statement, epistemic_basis, "
                "requires_review, review_flags_json, source_snapshot_digest, generator_version "
                "FROM understandings ORDER BY created_at, understanding_id"
            ).fetchall()
            result: list[dict[str, Any]] = []
            for row in rows:
                item = dict(row)
                try:
                    item["review_flags"] = json.loads(item.pop("review_flags_json") or "[]")
                except json.JSONDecodeError:
                    item["review_flags"] = ["malformed_review_flags"]
                result.append(item)
            refs = connect_ro(self.m2_path)
            try:
                for item in result:
                    uid = item["understanding_id"]
                    item["episode_refs"] = [str(x[0]) for x in refs.execute("SELECT episode_id FROM understanding_episodes WHERE understanding_id=? ORDER BY episode_id", (uid,))]
                    item["evidence_refs"] = [str(x[0]) for x in refs.execute("SELECT event_id FROM understanding_evidence WHERE understanding_id=? ORDER BY event_id", (uid,))]
            finally:
                refs.close()
            return result
        finally:
            connection.close()

    def epoch(self, conversation_id: str) -> tuple[str | None, str | None, set[str] | None]:
        if self.epoch_path is None or not self.epoch_path.exists():
            return None, None, None
        try:
            metadata = json.loads(self.epoch_path.read_text(encoding="utf-8"))
            boundary = metadata.get("boundaries", {}).get(conversation_id)
            if not isinstance(boundary, dict):
                return str(metadata.get("epoch_id")), None, None
            # M0 source_refs retain the Native turn id, so use the raw file
            # cursor only to determine the current visible turn set.
            raw_path = self.epoch_path.parent / "conversations" / f"{conversation_id}.json"
            if not raw_path.exists():
                return str(metadata.get("epoch_id")), str(boundary.get("boundary_id")), None
            records = json.loads(raw_path.read_text(encoding="utf-8"))
            count = int(boundary.get("record_count", 0))
            visible = records[count:] if isinstance(records, list) else []
            return (
                str(metadata.get("epoch_id")),
                str(boundary.get("boundary_id")),
                {str(item.get("turn_id")) for item in visible if isinstance(item, dict) and item.get("turn_id")},
            )
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return None, None, None

    def build_trigger(self, event: dict[str, Any], all_events: list[dict[str, Any]]) -> Trigger:
        ordered_ids = [str(item["event_id"]) for item in all_events]
        sequence = ordered_ids.index(str(event["event_id"])) + 1
        epoch_id, boundary_id, visible_turn_ids = self.epoch(str(event["conversation_id"]))
        visible_events = [
            item for item in all_events[:sequence]
            if str(item["conversation_id"]) == str(event["conversation_id"])
            and (visible_turn_ids is None or str(item["turn_id"]) in visible_turn_ids)
        ]
        visible_ids = tuple(str(item["event_id"]) for item in visible_events)
        visible_digest = digest([
            {"event_id": item["event_id"], "source_origin": item["source_origin"], "content_sha256": digest(item["content"])}
            for item in visible_events
        ])
        intent, reason = classify_intent(str(event.get("content", "")))
        return Trigger(event, sequence, intent, reason, epoch_id, boundary_id, visible_ids, visible_digest)

    def episode_candidates(self, trigger: Trigger, all_events: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], set[str]]:
        event_by_id = {str(item["event_id"]): item for item in all_events}
        sequence_by_id = {str(item["event_id"]): index + 1 for index, item in enumerate(all_events)}
        episodes, membership = self.m1_data()
        current_episode_ids = {
            episode_id for episode_id, rows in membership.items()
            if any(str(row["event_id"]) == str(trigger.event["event_id"]) for row in rows)
        }
        candidates: list[dict[str, Any]] = []
        for episode in episodes:
            episode_id = str(episode["episode_id"])
            rows = membership.get(episode_id, [])
            causal_rows = [row for row in rows if sequence_by_id.get(str(row["event_id"]), 10**12) <= trigger.sequence]
            if not causal_rows or episode_id in current_episode_ids:
                continue
            source_ids = [str(row["event_id"]) for row in causal_rows if str(row["event_id"]) in event_by_id]
            if not source_ids:
                continue
            contents = [str(event_by_id[event_id].get("content", "")) for event_id in source_ids]
            last_sequence = max(sequence_by_id[event_id] for event_id in source_ids)
            same_conversation = any(event_by_id[event_id].get("conversation_id") == trigger.event.get("conversation_id") for event_id in source_ids)
            visible_duplicate = bool(source_ids) and all(event_id in trigger.visible_event_ids for event_id in source_ids)
            candidates.append({
                "candidate_id": "episode:" + episode_id,
                "episode_id": episode_id,
                "candidate_type": "EPISODE",
                "content": " ".join(contents[-8:]),
                "cue_terms": terms(str(trigger.event.get("content", ""))),
                "source_event_ids": source_ids,
                "source_max_sequence": last_sequence,
                "temporal_relevance": "match" if any(cue in str(trigger.event.get("content", "")) for cue in TEMPORAL_CUES) else "none",
                "visible_duplicate": visible_duplicate,
                "adjacent_to_visible": same_conversation and last_sequence == trigger.sequence - 1,
                "surface_priority": last_sequence,
            })
        return candidates, current_episode_ids

    def linked_m2(self, episode_id: str, trigger: Trigger, sequence_by_id: dict[str, int], m2_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for row in m2_rows:
            if episode_id not in row.get("episode_refs", []):
                continue
            evidence_refs = [str(ref) for ref in row.get("evidence_refs", [])]
            if not evidence_refs or any(sequence_by_id.get(ref, 10**12) > trigger.sequence for ref in evidence_refs):
                continue
            item = dict(row)
            item["provenance_complete"] = bool(row.get("episode_refs") and evidence_refs)
            result.append(item)
        return result


class M3Store:
    def __init__(self, path: str | Path, initialize: bool = True):
        self.path = Path(path)
        if initialize:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._initialize()
            self.path.chmod(0o600)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=5000")
        return connection

    def _initialize(self) -> None:
        connection = self._connect()
        try:
            configure_journal(connection)
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS runtime_meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS evaluations(
                    evaluation_id TEXT PRIMARY KEY,
                    trigger_evidence_id TEXT NOT NULL UNIQUE,
                    trigger_sequence INTEGER NOT NULL,
                    trigger_occurred_at TEXT NOT NULL,
                    conversation_id TEXT NOT NULL,
                    cognitive_intent TEXT NOT NULL,
                    intent_reason TEXT NOT NULL,
                    epoch_id TEXT,
                    boundary_id TEXT,
                    visible_history_digest TEXT NOT NULL,
                    context_snapshot_digest TEXT NOT NULL,
                    generator_version TEXT NOT NULL,
                    availability_version TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS candidates(
                    candidate_id TEXT NOT NULL,
                    evaluation_id TEXT NOT NULL,
                    candidate_type TEXT NOT NULL,
                    episode_id TEXT NOT NULL,
                    source_event_ids_json TEXT NOT NULL,
                    source_max_sequence INTEGER NOT NULL,
                    generator_signals_json TEXT NOT NULL,
                    provenance_json TEXT NOT NULL,
                    candidate_digest TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(evaluation_id, candidate_id)
                );
                CREATE TABLE IF NOT EXISTS eligibility_decisions(
                    evaluation_id TEXT NOT NULL,
                    candidate_ref TEXT NOT NULL,
                    candidate_type TEXT NOT NULL,
                    eligible INTEGER NOT NULL,
                    reason_code TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(evaluation_id, candidate_ref)
                );
                CREATE TABLE IF NOT EXISTS availability_decisions(
                    evaluation_id TEXT PRIMARY KEY,
                    outcome TEXT NOT NULL,
                    selected_refs_json TEXT NOT NULL,
                    reason_code TEXT NOT NULL,
                    surface_payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS secondary_expansions(
                    evaluation_id TEXT NOT NULL,
                    understanding_id TEXT NOT NULL,
                    episode_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    eligible INTEGER NOT NULL,
                    surfaceable INTEGER NOT NULL,
                    withheld_reason TEXT NOT NULL,
                    provenance_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(evaluation_id, understanding_id)
                );
                CREATE TABLE IF NOT EXISTS processed_triggers(
                    trigger_evidence_id TEXT PRIMARY KEY,
                    evaluation_id TEXT,
                    trigger_sequence INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    processed_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS eval_intent_idx ON evaluations(cognitive_intent);
                CREATE INDEX IF NOT EXISTS eval_status_idx ON evaluations(status);
                CREATE INDEX IF NOT EXISTS candidate_episode_idx ON candidates(episode_id);
                CREATE INDEX IF NOT EXISTS secondary_status_idx ON secondary_expansions(status);
                """
            )
            connection.execute("INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES(1, ?)", (utc_now(),))
            # v2 records that this runtime supports the consumed-rowid watermark.
            # No schema change is needed (runtime_meta already exists), so an old
            # database opens directly: nothing is dropped and no rebuild is asked
            # of the operator. Idempotent by INSERT OR IGNORE.
            connection.execute("INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES(2, ?)", (utc_now(),))
            connection.commit()
        finally:
            connection.close()

    def ensure_cursor(self, initial_user_event_id: str) -> None:
        connection = self._connect()
        try:
            row = connection.execute("SELECT value FROM runtime_meta WHERE key='initial_user_event_id'").fetchone()
            if row is None:
                connection.execute("INSERT INTO runtime_meta(key,value) VALUES('initial_user_event_id',?)", (initial_user_event_id,))
                connection.commit()
            elif str(row[0]) != initial_user_event_id:
                raise RuntimeError("m3 initial cursor differs from stored cursor")
        finally:
            connection.close()

    #: Trigger-discovery consumption watermark. Independent of the immutable
    #: initial_user_event_id anchor: the anchor is the business start, this is
    #: only "how far discovery has consumed". None means no watermark yet.
    WATERMARK_KEY = "m3_consumed_rowid"

    def consumed_rowid(self) -> int | None:
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT value FROM runtime_meta WHERE key=?", (self.WATERMARK_KEY,)
            ).fetchone()
            if row is None:
                return None
            try:
                return int(row[0])
            except (TypeError, ValueError):
                return None
        finally:
            connection.close()

    def set_consumed_rowid(self, rowid: int) -> None:
        connection = self._connect()
        try:
            connection.execute(
                "INSERT OR REPLACE INTO runtime_meta(key,value) VALUES(?,?)",
                (self.WATERMARK_KEY, str(int(rowid))),
            )
            connection.commit()
        finally:
            connection.close()

    def reset_consumed_rowid(self) -> None:
        """Clear the discovery watermark only; the anchor is never touched."""
        connection = self._connect()
        try:
            connection.execute(
                "DELETE FROM runtime_meta WHERE key=?", (self.WATERMARK_KEY,))
            connection.commit()
        finally:
            connection.close()

    def is_processed(self, trigger_id: str) -> bool:
        connection = self._connect()
        try:
            return connection.execute("SELECT 1 FROM processed_triggers WHERE trigger_evidence_id=?", (trigger_id,)).fetchone() is not None
        finally:
            connection.close()

    def record(self, evaluation: dict[str, Any], candidates: list[dict[str, Any]], eligibility: list[dict[str, Any]], availability: dict[str, Any], expansions: list[dict[str, Any]], status: str) -> str:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            trigger_id = evaluation["trigger_evidence_id"]
            existing = connection.execute("SELECT evaluation_id FROM processed_triggers WHERE trigger_evidence_id=?", (trigger_id,)).fetchone()
            if existing is not None:
                connection.commit()
                return "duplicate"
            evaluation_id = evaluation["evaluation_id"]
            connection.execute(
                "INSERT INTO evaluations VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (evaluation_id, trigger_id, evaluation["trigger_sequence"], evaluation["trigger_occurred_at"], evaluation["conversation_id"], evaluation["cognitive_intent"], evaluation["intent_reason"], evaluation.get("epoch_id"), evaluation.get("boundary_id"), evaluation["visible_history_digest"], evaluation["context_snapshot_digest"], evaluation["generator_version"], evaluation["availability_version"], status, utc_now()),
            )
            for candidate in candidates:
                connection.execute(
                    "INSERT INTO candidates VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (candidate["candidate_id"], evaluation_id, candidate["candidate_type"], candidate["episode_id"], json.dumps(candidate["source_event_ids"], ensure_ascii=False), candidate["source_max_sequence"], json.dumps(candidate["signals"], ensure_ascii=False, sort_keys=True), json.dumps(candidate["provenance"], ensure_ascii=False, sort_keys=True), candidate["candidate_digest"], utc_now()),
                )
            for item in eligibility:
                connection.execute(
                    "INSERT INTO eligibility_decisions VALUES(?,?,?,?,?,?)",
                    (evaluation_id, item["candidate_ref"], item["candidate_type"], int(item["eligible"]), item["reason_code"], utc_now()),
                )
            connection.execute(
                "INSERT INTO availability_decisions VALUES(?,?,?,?,?,?)",
                (evaluation_id, availability["outcome"], json.dumps(availability["selected_refs"], ensure_ascii=False), availability["reason_code"], json.dumps(availability["surface_payload"], ensure_ascii=False), utc_now()),
            )
            for item in expansions:
                connection.execute(
                    "INSERT INTO secondary_expansions VALUES(?,?,?,?,?,?,?,?,?)",
                    (evaluation_id, item["understanding_id"], item["episode_id"], item["status"], int(item["eligible"]), int(item["surfaceable"]), item["withheld_reason"], json.dumps(item["provenance"], ensure_ascii=False, sort_keys=True), utc_now()),
                )
            connection.execute("INSERT INTO processed_triggers VALUES(?,?,?,?,?)", (trigger_id, evaluation_id, evaluation["trigger_sequence"], status, utc_now()))
            connection.commit()
            return "inserted"
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def recent(self, limit: int = 50, intent: str | None = None) -> list[dict[str, Any]]:
        connection = self._connect()
        try:
            if intent:
                rows = connection.execute("SELECT * FROM evaluations WHERE cognitive_intent=? ORDER BY created_at DESC LIMIT ?", (intent, limit)).fetchall()
            else:
                rows = connection.execute("SELECT * FROM evaluations ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
            return [dict(row) for row in rows]
        finally:
            connection.close()

    def why(self, evaluation_id: str) -> dict[str, Any] | None:
        connection = self._connect()
        try:
            evaluation = connection.execute("SELECT * FROM evaluations WHERE evaluation_id=?", (evaluation_id,)).fetchone()
            if evaluation is None:
                return None
            result: dict[str, Any] = {"evaluation": dict(evaluation)}
            result["candidates"] = [dict(row) for row in connection.execute("SELECT * FROM candidates WHERE evaluation_id=? ORDER BY candidate_id", (evaluation_id,))]
            result["eligibility"] = [dict(row) for row in connection.execute("SELECT * FROM eligibility_decisions WHERE evaluation_id=? ORDER BY candidate_ref", (evaluation_id,))]
            availability = connection.execute("SELECT * FROM availability_decisions WHERE evaluation_id=?", (evaluation_id,)).fetchone()
            result["availability"] = dict(availability) if availability else None
            result["secondary_expansions"] = [dict(row) for row in connection.execute("SELECT * FROM secondary_expansions WHERE evaluation_id=? ORDER BY understanding_id", (evaluation_id,))]
            return result
        finally:
            connection.close()

    def verify(self) -> dict[str, Any]:
        connection = self._connect()
        issues: list[str] = []
        try:
            orphan_candidates = connection.execute("SELECT count(*) FROM candidates c LEFT JOIN evaluations e ON e.evaluation_id=c.evaluation_id WHERE e.evaluation_id IS NULL").fetchone()[0]
            orphan_decisions = connection.execute("SELECT count(*) FROM eligibility_decisions d LEFT JOIN evaluations e ON e.evaluation_id=d.evaluation_id WHERE e.evaluation_id IS NULL").fetchone()[0]
            future_rows = connection.execute("SELECT count(*) FROM candidates c JOIN evaluations e ON e.evaluation_id=c.evaluation_id WHERE c.source_max_sequence>e.trigger_sequence").fetchone()[0]
            m2_standalone = connection.execute("SELECT count(*) FROM eligibility_decisions WHERE candidate_type='M2_STANDALONE' AND eligible=1").fetchone()[0]
            review_leak = connection.execute("SELECT count(*) FROM secondary_expansions WHERE surfaceable=1 AND status IN ('REVIEW','REJECTED')").fetchone()[0]
            rejected_leak = connection.execute("SELECT count(*) FROM secondary_expansions WHERE surfaceable=1 AND status='REJECTED'").fetchone()[0]
            duplicate_triggers = connection.execute("SELECT COUNT(*)-COUNT(DISTINCT trigger_evidence_id) FROM processed_triggers").fetchone()[0]
            for name, count in (("orphan_candidate", orphan_candidates), ("orphan_eligibility", orphan_decisions), ("future_leakage", future_rows), ("m2_standalone_surface", m2_standalone), ("review_leakage", review_leak), ("rejected_leakage", rejected_leak), ("duplicate_trigger", duplicate_triggers)):
                if count:
                    issues.append(f"{name}:{count}")
            return {"schema_version": M3_SCHEMA_VERSION, "evaluation_count": connection.execute("SELECT count(*) FROM evaluations").fetchone()[0], "candidate_count": connection.execute("SELECT count(*) FROM candidates").fetchone()[0], "processed_trigger_count": connection.execute("SELECT count(*) FROM processed_triggers").fetchone()[0], "m2_standalone_surface_count": m2_standalone, "issues": issues, "ok": not issues}
        finally:
            connection.close()


def _surface_payload(candidate: dict[str, Any], m2_rows: list[dict[str, Any]], trigger: Trigger, selected_episode_id: str, source_ids: list[str]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    payload = {"source_type": "EPISODE", "episode_id": selected_episode_id, "supporting_evidence_refs": source_ids, "trigger_sequence": trigger.sequence, "reply_authority": False}
    expansions: list[dict[str, Any]] = []
    for row in m2_rows:
        if selected_episode_id not in row.get("episode_refs", []):
            continue
        provenance = {"episode_refs": row.get("episode_refs", []), "evidence_refs": row.get("evidence_refs", []), "epistemic_basis": row.get("epistemic_basis"), "status": row.get("status")}
        if row.get("status") == "ACTIVE" and row.get("episode_refs") and row.get("evidence_refs"):
            expansions.append({"understanding_id": row["understanding_id"], "episode_id": selected_episode_id, "status": row["status"], "eligible": True, "surfaceable": True, "withheld_reason": "", "provenance": provenance})
            payload.setdefault("secondary_understandings", []).append({"understanding_id": row["understanding_id"], "statement": row["statement"], "status": row["status"], "epistemic_basis": row["epistemic_basis"], "episode_refs": row["episode_refs"], "evidence_refs": row["evidence_refs"], "reply_authority": False})
        else:
            reason = {"UNCERTAIN": "UNCERTAIN_WITHHELD_V0", "REVIEW": "REVIEW_NEVER_ELIGIBLE", "REJECTED": "REJECTED_NEVER_ELIGIBLE"}.get(str(row.get("status")), "PROVENANCE_INCOMPLETE")
            expansions.append({"understanding_id": row["understanding_id"], "episode_id": selected_episode_id, "status": row.get("status", "UNKNOWN"), "eligible": False, "surfaceable": False, "withheld_reason": reason, "provenance": provenance})
    if payload.get("secondary_understandings"):
        payload["source_type"] = "EPISODE_WITH_SECONDARY_UNDERSTANDING"
    return payload, expansions


class M3Worker:
    def __init__(self, reader: SourceReader, store: M3Store, *, generator_version: str = M3_GENERATOR_VERSION, availability_version: str = M3_AVAILABILITY_VERSION, max_candidates: int = 200, logger: logging.Logger | None = None):
        self.reader = reader
        self.store = store
        self.generator_version = generator_version
        self.availability_version = availability_version
        self.max_candidates = max_candidates
        self.logger = logger or logging.getLogger("chiyo.m3")
        self.provider = FixtureCandidateProvider()

    def _evaluate(self, trigger: Trigger, all_events: list[dict[str, Any]]) -> str:
        trigger_id = str(trigger.event["event_id"])
        if self.store.is_processed(trigger_id):
            return "duplicate"
        episodes, _ = self.reader.episode_candidates(trigger, all_events)
        episodes = episodes[: self.max_candidates]
        raw_for_provider = []
        for candidate in episodes:
            raw_for_provider.append({
                "candidate_id": candidate["candidate_id"],
                "role": "direct_answer",
                "kind": "episode",
                "content": candidate["content"],
                "cue_terms": candidate["cue_terms"],
                "temporal_relevance": candidate["temporal_relevance"],
                "support_level": "supported",
                "availability_state": "available",
                "surface_priority": candidate["surface_priority"],
                "visible_duplicate": candidate["visible_duplicate"],
                "adjacent_to_visible": candidate["adjacent_to_visible"],
            })
        generated = self.provider.generate({"user_text": trigger.event.get("content", ""), "visible_history": []}, raw_for_provider)
        generated_by_id = {item["candidate_id"]: item for item in generated}
        sequence_by_id = {str(item["event_id"]): index + 1 for index, item in enumerate(all_events)}
        candidates: list[dict[str, Any]] = []
        eligibility: list[dict[str, Any]] = []
        eligible_episode_ids: list[str] = []
        for candidate in episodes:
            generated_item = generated_by_id[candidate["candidate_id"]]
            signals = generated_item["signals"]
            eligible = True
            reason = "ELIGIBLE"
            if candidate["source_max_sequence"] > trigger.sequence:
                eligible, reason = False, "FUTURE_SOURCE"
            elif candidate["visible_duplicate"]:
                eligible, reason = False, "VISIBLE_DUPLICATE"
            elif trigger.intent == "P0":
                eligible, reason = False, "P0_SELECTIVE_NON_RECALL"
            elif not (signals.get("query_match") or signals.get("temporal_match") or signals.get("adjacent_to_visible")):
                eligible, reason = False, "NO_CUE_MATCH"
            if eligible:
                eligible_episode_ids.append(candidate["episode_id"])
            eligibility.append({"candidate_ref": candidate["candidate_id"], "candidate_type": "EPISODE", "eligible": eligible, "reason_code": reason})
            candidates.append({"candidate_id": candidate["candidate_id"], "candidate_type": "EPISODE", "episode_id": candidate["episode_id"], "source_event_ids": candidate["source_event_ids"], "source_max_sequence": candidate["source_max_sequence"], "signals": signals, "provenance": {"episode_id": candidate["episode_id"], "evidence_refs": candidate["source_event_ids"]}, "candidate_digest": digest({"episode_id": candidate["episode_id"], "source_event_ids": candidate["source_event_ids"], "signals": signals})})

        selected_episode: dict[str, Any] | None = None
        if trigger.intent in {"P1", "P2"} and eligible_episode_ids:
            selected_id = eligible_episode_ids[0]
            selected_episode = next(item for item in candidates if item["episode_id"] == selected_id)
        m2_rows = self.reader.m2_data()
        sequence_by_id = {str(item["event_id"]): index + 1 for index, item in enumerate(all_events)}
        causal_m2_rows = [
            row for row in m2_rows
            if row.get("evidence_refs")
            and all(sequence_by_id.get(str(ref), 10**12) <= trigger.sequence for ref in row.get("evidence_refs", []))
        ]
        for row in causal_m2_rows:
            if trigger.intent == "P0":
                reason = "P0_M2_STANDALONE_NEVER"
            elif trigger.intent == "P1":
                reason = "P1_M2_STANDALONE_NEVER"
            else:
                reason = "P2_SECONDARY_ONLY_REQUIRES_SELECTED_EPISODE"
            eligibility.append({"candidate_ref": "m2:" + str(row["understanding_id"]), "candidate_type": "M2_STANDALONE", "eligible": False, "reason_code": reason})
        expansions: list[dict[str, Any]] = []
        if selected_episode is not None:
            linked_m2_rows = self.reader.linked_m2(selected_episode["episode_id"], trigger, sequence_by_id, causal_m2_rows)
            payload, expansions = _surface_payload(selected_episode, linked_m2_rows, trigger, selected_episode["episode_id"], selected_episode["source_event_ids"])
            outcome = "SURFACE_EPISODE_WITH_SECONDARY_UNDERSTANDING" if payload.get("secondary_understandings") else "SURFACE_EPISODE"
            selected_refs = [selected_episode["candidate_id"]] + ["m2:" + item["understanding_id"] for item in expansions if item["surfaceable"]]
            reason_code = "P2_E2_SECONDARY" if payload.get("secondary_understandings") else ("P2_EPISODE" if trigger.intent == "P2" else "P1_EPISODE")
        else:
            payload = {"reply_authority": False}
            outcome = "NO_SURFACE"
            selected_refs = []
            reason_code = "P0_SELECTIVE_NON_RECALL" if trigger.intent == "P0" else "NO_ELIGIBLE_EPISODE"
        evaluation_body = {"trigger_evidence_id": trigger_id, "trigger_sequence": trigger.sequence, "trigger_occurred_at": trigger.event["occurred_at"], "conversation_id": trigger.event["conversation_id"], "cognitive_intent": trigger.intent, "intent_reason": trigger.intent_reason, "epoch_id": trigger.epoch_id, "boundary_id": trigger.boundary_id, "visible_history_digest": trigger.visible_history_digest, "candidate_ids": [item["candidate_id"] for item in candidates], "selected_refs": selected_refs}
        evaluation = {"evaluation_id": digest(evaluation_body)[:32], "trigger_evidence_id": trigger_id, "trigger_sequence": trigger.sequence, "trigger_occurred_at": trigger.event["occurred_at"], "conversation_id": trigger.event["conversation_id"], "cognitive_intent": trigger.intent, "intent_reason": trigger.intent_reason, "epoch_id": trigger.epoch_id, "boundary_id": trigger.boundary_id, "visible_history_digest": trigger.visible_history_digest, "context_snapshot_digest": digest({"trigger": trigger_id, "sequence": trigger.sequence, "epoch_id": trigger.epoch_id, "boundary_id": trigger.boundary_id, "visible_history_digest": trigger.visible_history_digest}), "generator_version": self.generator_version, "availability_version": self.availability_version}
        availability = {"outcome": outcome, "selected_refs": selected_refs, "reason_code": reason_code, "surface_payload": {**payload, "trigger_evidence_id": trigger_id, "cognitive_intent": trigger.intent, "reply_authority": False}}
        status = "COMPLETED" if outcome else "FAILED_CLOSED"
        return self.store.record(evaluation, candidates, eligibility, availability, expansions, status)

    def _record_failed_closed(self, trigger: Trigger) -> str:
        body = {"trigger_evidence_id": trigger.event["event_id"], "trigger_sequence": trigger.sequence, "intent": trigger.intent}
        evaluation = {
            "evaluation_id": digest(body)[:32],
            "trigger_evidence_id": trigger.event["event_id"],
            "trigger_sequence": trigger.sequence,
            "trigger_occurred_at": trigger.event["occurred_at"],
            "conversation_id": trigger.event["conversation_id"],
            "cognitive_intent": trigger.intent,
            "intent_reason": trigger.intent_reason,
            "epoch_id": trigger.epoch_id,
            "boundary_id": trigger.boundary_id,
            "visible_history_digest": trigger.visible_history_digest,
            "context_snapshot_digest": digest(body),
            "generator_version": self.generator_version,
            "availability_version": self.availability_version,
        }
        availability = {"outcome": "NO_SURFACE", "selected_refs": [], "reason_code": "FAILED_CLOSED", "surface_payload": {"reply_authority": False}}
        return self.store.record(evaluation, [], [], availability, [], "FAILED_CLOSED")

    #: trigger-discovery batch size
    DISCOVERY_BATCH = 200

    def process_once(self, initial_user_event_id: str) -> dict[str, int]:
        """Discover new triggers incrementally; evaluate them with full context.

        Only discovery changed. The evaluation context is still the complete
        history in (occurred_at, created_at, event_id) order, because
        build_trigger / episode_candidates / _evaluate derive absolute global
        sequence numbers from it -- narrowing that would change the persisted
        trigger_sequence / source_max_sequence contract.
        """
        all_events = self.reader.events()
        ids = [str(item["event_id"]) for item in all_events]
        if initial_user_event_id not in ids:
            raise RuntimeError("m3 initial cursor event not found")
        sequence_by_id = {event_id: index + 1 for index, event_id in enumerate(ids)}
        anchor_sequence = sequence_by_id[initial_user_event_id]

        counts = {"evaluated": 0, "duplicate": 0, "skipped_non_user": 0,
                  "failed_closed": 0, "skipped_before_anchor": 0, "skipped_unknown": 0}
        watermark = self.store.consumed_rowid()
        if watermark is None:
            # First run on this database: start at the anchor's own M0 row rather
            # than assuming MAX(rowid), so history is discovered rather than
            # skipped. processed_triggers still decides what is already done.
            anchor_rowid = self.reader.m0_rowid_for(str(initial_user_event_id))
            watermark = int(anchor_rowid) if anchor_rowid is not None else 0
            # Persist the starting point immediately: a first run that discovers
            # nothing new must still establish the watermark, otherwise every
            # later call repeats the anchor lookup and the steady state never
            # becomes incremental.
            self.store.set_consumed_rowid(watermark)

        while True:
            batch = self.reader.trigger_candidates_since(watermark, self.DISCOVERY_BATCH)
            if not batch:
                return counts
            safe = watermark
            for rowid, event in batch:
                if (event.get("source_origin") != USER_ORIGIN
                        or event.get("delivery_status") != "RECEIVED"):
                    counts["skipped_non_user"] += 1
                    safe = rowid
                    continue
                position = sequence_by_id.get(str(event["event_id"]))
                if position is None:
                    counts["skipped_unknown"] += 1
                    safe = rowid
                    continue
                if position < anchor_sequence:
                    # business-anchor guard: an event ordering before the anchor is
                    # not a normal history trigger. (A late event lands here too;
                    # its sequence is still computed by build_trigger from the full
                    # ordering, so the sequence contract is untouched.)
                    counts["skipped_before_anchor"] += 1
                    safe = rowid
                    continue
                try:
                    trigger = self.reader.build_trigger(event, all_events)
                    result = self._evaluate(trigger, all_events)
                    counts["duplicate" if result == "duplicate" else "evaluated"] += 1
                    safe = rowid
                except Exception as exc:
                    self.logger.error("m3.evaluation.failed trigger=%s error_class=%s",
                                      event.get("event_id"), type(exc).__name__)
                    try:
                        trigger = self.reader.build_trigger(event, all_events)
                        self._record_failed_closed(trigger)
                    except Exception:
                        pass
                    counts["failed_closed"] += 1
                    # The watermark must never move past a failed row: persist the
                    # last safely-consumed one and stop this pass.
                    self.store.set_consumed_rowid(safe)
                    return counts
            watermark = safe
            self.store.set_consumed_rowid(watermark)
