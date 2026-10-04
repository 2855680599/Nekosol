from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from app.m3 import M3Store, M3Worker, SourceReader, connect_ro


UTC = "+00:00"


def _stamp(index: int) -> str:
    return f"2026-01-01T00:0{index}:00{UTC}"


def _create_source_dbs(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    m0 = tmp_path / "evidence.sqlite"
    m1 = tmp_path / "episodes.sqlite"
    m2 = tmp_path / "understandings.sqlite"
    epoch = tmp_path / "context_epoch.json"
    with closing(sqlite3.connect(m0)) as connection, connection:
        connection.executescript(
            """
            CREATE TABLE evidence_events(
                event_id TEXT PRIMARY KEY, occurred_at TEXT NOT NULL,
                memory_owner TEXT NOT NULL, source_origin TEXT NOT NULL,
                delivery_status TEXT NOT NULL, epistemic_role TEXT NOT NULL,
                speaker TEXT NOT NULL, content TEXT NOT NULL,
                conversation_id TEXT NOT NULL, turn_id TEXT NOT NULL,
                primary_source_ref_id TEXT NOT NULL, source_refs_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            """
        )
    with closing(sqlite3.connect(m1)) as connection, connection:
        connection.executescript(
            """
            CREATE TABLE episodes(
                episode_id TEXT PRIMARY KEY, memory_owner TEXT NOT NULL,
                started_at TEXT NOT NULL, ended_at TEXT, status TEXT NOT NULL,
                algorithm_version TEXT NOT NULL, created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE episode_evidence(
                episode_id TEXT NOT NULL, event_id TEXT NOT NULL,
                membership_kind TEXT NOT NULL, sequence_index INTEGER NOT NULL
            );
            """
        )
    with closing(sqlite3.connect(m2)) as connection, connection:
        connection.executescript(
            """
            CREATE TABLE understandings(
                understanding_id TEXT PRIMARY KEY, status TEXT NOT NULL,
                understanding_kind TEXT NOT NULL, statement TEXT NOT NULL,
                epistemic_basis TEXT NOT NULL, requires_review INTEGER NOT NULL,
                review_flags_json TEXT NOT NULL, source_snapshot_digest TEXT NOT NULL,
                generator_version TEXT NOT NULL, created_at TEXT NOT NULL
            );
            CREATE TABLE understanding_episodes(
                understanding_id TEXT NOT NULL, episode_id TEXT NOT NULL
            );
            CREATE TABLE understanding_evidence(
                understanding_id TEXT NOT NULL, event_id TEXT NOT NULL
            );
            """
        )
    conversation_dir = tmp_path / "conversations"
    conversation_dir.mkdir()
    (conversation_dir / "c.json").write_text(
        json.dumps([{"turn_id": f"t{i}"} for i in range(1, 6)]), encoding="utf-8"
    )
    epoch.write_text(
        json.dumps(
            {
                "epoch_id": "epoch-test",
                "boundaries": {"c": {"boundary_id": "boundary-test", "record_count": 2}},
            }
        ),
        encoding="utf-8",
    )
    return m0, m1, m2, epoch


def _add_event(m0: Path, event_id: str, index: int, origin: str, content: str) -> None:
    if origin == "USER_VISIBLE_INPUT":
        delivery, role, speaker = "RECEIVED", "OBSERVED_EXTERNAL_EXPRESSION", "user"
    else:
        delivery, role, speaker = "DELIVERED", "SELF_EXPRESSION", "chiyo"
    with closing(sqlite3.connect(m0)) as connection, connection:
        connection.execute(
            "INSERT INTO evidence_events VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                event_id, _stamp(index), "chiyo", origin, delivery, role, speaker,
                content, "c", f"t{index}", event_id,
                json.dumps([{"kind": "test", "id": event_id}]), _stamp(index),
            ),
        )


def _add_episode(m1: Path, episode_id: str, event_ids: list[str], status: str = "CLOSED") -> None:
    with closing(sqlite3.connect(m1)) as connection, connection:
        connection.execute(
            "INSERT INTO episodes VALUES(?,?,?,?,?,?,?,?)",
            (episode_id, "chiyo", _stamp(1), _stamp(3) if status == "CLOSED" else None,
             status, "test", _stamp(1), _stamp(3)),
        )
        for sequence, event_id in enumerate(event_ids):
            connection.execute(
                "INSERT INTO episode_evidence VALUES(?,?,?,?)",
                (episode_id, event_id, "PRIMARY", sequence),
            )


def _add_understanding(m2: Path, uid: str, episode_id: str, event_id: str, status: str) -> None:
    with closing(sqlite3.connect(m2)) as connection, connection:
        connection.execute(
            "INSERT INTO understandings VALUES(?,?,?,?,?,?,?,?,?,?)",
            (uid, status, "EPISODE_INTERPRETATION", "已确认的过去经历", "USER_ASSERTED",
             0, "[]", "snapshot", "test", _stamp(2)),
        )
        connection.execute("INSERT INTO understanding_episodes VALUES(?,?)", (uid, episode_id))
        connection.execute("INSERT INTO understanding_evidence VALUES(?,?)", (uid, event_id))


def _fixture(tmp_path: Path, *, with_m2: bool = True) -> tuple[SourceReader, list[dict[str, object]], Path]:
    m0, m1, m2, epoch = _create_source_dbs(tmp_path)
    _add_event(m0, "e1", 1, "USER_VISIBLE_INPUT", "我之前去了图书馆学习日语")
    _add_event(m0, "e2", 2, "CHIYO_VISIBLE_OUTPUT", "记住了")
    _add_event(m0, "e3", 3, "USER_VISIBLE_INPUT", "你之前说过图书馆吗")
    _add_episode(m1, "ep-old", ["e1", "e2"])
    _add_episode(m1, "ep-current", ["e3"], status="OPEN")
    if with_m2:
        _add_understanding(m2, "u-active", "ep-old", "e1", "ACTIVE")
        _add_understanding(m2, "u-uncertain", "ep-old", "e1", "UNCERTAIN")
    reader = SourceReader(m0, m1, m2, epoch)
    return reader, reader.events(), m0


def _trigger(reader: SourceReader, events: list[dict[str, object]], event_id: str = "e3"):
    return reader.build_trigger(next(item for item in events if item["event_id"] == event_id), events)


class M3ShadowTests(unittest.TestCase):
    def _tmp(self):
        return tempfile.TemporaryDirectory()

    def test_p0_never_surfaces_m2_standalone(self) -> None:
        with self._tmp() as directory:
            tmp_path = Path(directory)
            reader, events, _ = _fixture(tmp_path)
            p0_event = dict(events[-1])
            p0_event.update({"content": "哈哈哈哈", "event_id": "p0", "turn_id": "t4", "occurred_at": _stamp(4), "created_at": _stamp(4)})
            events.append(p0_event)
            store = M3Store(tmp_path / "recall.sqlite")
            result = M3Worker(reader, store)._evaluate(reader.build_trigger(p0_event, events), events)
            self.assertEqual(result, "inserted")
            row = store.recent(1)[0]
            self.assertEqual(row["cognitive_intent"], "P0")
            detail = store.why(row["evaluation_id"])
            payload = json.loads(detail["availability"]["surface_payload_json"])
            self.assertFalse(payload["reply_authority"])
            self.assertEqual(store.verify()["m2_standalone_surface_count"], 0)
            self.assertTrue(store.verify()["ok"])

    def test_p2_surfaces_episode_and_only_active_secondary(self) -> None:
        with self._tmp() as directory:
            tmp_path = Path(directory)
            reader, events, _ = _fixture(tmp_path)
            base = _trigger(reader, events)
            trigger = base.__class__(
                event={**base.event, "content": "回头看你之前的图书馆那件事意味着什么"},
                sequence=base.sequence, intent="P2", intent_reason="EXPLICIT_REFLECTIVE_CUE",
                epoch_id=base.epoch_id, boundary_id=base.boundary_id,
                visible_event_ids=base.visible_event_ids,
                visible_history_digest=base.visible_history_digest,
            )
            store = M3Store(tmp_path / "recall.sqlite")
            self.assertEqual(M3Worker(reader, store)._evaluate(trigger, events), "inserted")
            evaluation = store.recent(1)[0]
            detail = store.why(evaluation["evaluation_id"])
            availability = json.loads(detail["availability"]["surface_payload_json"])
            self.assertEqual(availability["source_type"], "EPISODE_WITH_SECONDARY_UNDERSTANDING")
            self.assertEqual({item["understanding_id"] for item in detail["secondary_expansions"] if item["surfaceable"]}, {"u-active"})
            withheld = {item["understanding_id"]: item["withheld_reason"] for item in detail["secondary_expansions"] if not item["surfaceable"]}
            self.assertEqual(withheld, {"u-uncertain": "UNCERTAIN_WITHHELD_V0"})
            self.assertFalse(availability["reply_authority"])

    def test_future_events_are_not_candidates(self) -> None:
        with self._tmp() as directory:
            tmp_path = Path(directory)
            reader, _, m0 = _fixture(tmp_path, with_m2=False)
            _add_event(m0, "e4", 4, "CHIYO_VISIBLE_OUTPUT", "我刚从图书馆回来")
            all_events = reader.events()
            trigger = _trigger(reader, all_events)
            candidates, _ = reader.episode_candidates(trigger, all_events)
            self.assertEqual(trigger.sequence, 3)
            self.assertTrue(all(candidate["source_max_sequence"] <= trigger.sequence for candidate in candidates))
            self.assertTrue(all("e4" not in candidate["source_event_ids"] for candidate in candidates))

    def test_trigger_is_idempotent_and_rebuildable(self) -> None:
        with self._tmp() as directory:
            tmp_path = Path(directory)
            reader, events, _ = _fixture(tmp_path)
            trigger = _trigger(reader, events)
            first_store = M3Store(tmp_path / "first.sqlite")
            first_worker = M3Worker(reader, first_store)
            self.assertEqual(first_worker._evaluate(trigger, events), "inserted")
            self.assertEqual(first_worker._evaluate(trigger, events), "duplicate")
            first = first_store.recent(1)[0]
            first_detail = first_store.why(first["evaluation_id"])
            second_store = M3Store(tmp_path / "second.sqlite")
            self.assertEqual(M3Worker(reader, second_store)._evaluate(trigger, events), "inserted")
            second = second_store.recent(1)[0]
            second_detail = second_store.why(second["evaluation_id"])
            self.assertEqual(first_store.verify()["processed_trigger_count"], 1)
            self.assertEqual(first["context_snapshot_digest"], second["context_snapshot_digest"])
            self.assertEqual(first_detail["availability"]["outcome"], second_detail["availability"]["outcome"])

    def test_m0_m1_m2_are_read_only(self) -> None:
        with self._tmp() as directory:
            reader, _, _ = _fixture(Path(directory))
            for path in (reader.m0_path, reader.m1_path, reader.m2_path):
                connection = connect_ro(path)
                try:
                    with self.assertRaises(sqlite3.OperationalError):
                        connection.execute("CREATE TABLE should_not_exist(x TEXT)")
                finally:
                    connection.close()


if __name__ == "__main__":
    unittest.main()
