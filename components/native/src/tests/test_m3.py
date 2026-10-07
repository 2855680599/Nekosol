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


class _BoomWorker(M3Worker):
    """Raises inside _evaluate for one nominated trigger event."""

    def __init__(self, reader, store, boom_event_id: str):
        super().__init__(reader, store)
        self.boom_event_id = boom_event_id

    def _evaluate(self, trigger, all_events):
        if str(trigger.event.get("event_id")) == self.boom_event_id:
            raise RuntimeError("simulated evaluation failure")
        return super()._evaluate(trigger, all_events)


class M3WatermarkTests(unittest.TestCase):
    """Trigger-discovery watermark, independent of the initial anchor."""

    def _tmp(self):
        return tempfile.TemporaryDirectory()

    @staticmethod
    def _migrations(store_path: Path) -> list[int]:
        with closing(sqlite3.connect(store_path)) as connection:
            return [int(row[0]) for row in connection.execute(
                "SELECT version FROM schema_migrations ORDER BY version")]

    def test_old_db_without_watermark_opens_normally(self) -> None:
        with self._tmp() as directory:
            tmp_path = Path(directory)
            _fixture(tmp_path)
            store = M3Store(tmp_path / "recall.sqlite")
            self.assertIsNone(store.consumed_rowid())
            self.assertTrue(store.verify()["ok"])

    def test_migration_v2_is_idempotent(self) -> None:
        with self._tmp() as directory:
            tmp_path = Path(directory)
            _fixture(tmp_path)
            path = tmp_path / "recall.sqlite"
            M3Store(path)
            M3Store(path)          # second construction must be safe and idempotent
            M3Store(path)
            self.assertEqual(self._migrations(path), [1, 2])

    def test_initial_anchor_is_unchanged_and_independent(self) -> None:
        with self._tmp() as directory:
            tmp_path = Path(directory)
            _fixture(tmp_path)
            store = M3Store(tmp_path / "recall.sqlite")
            store.ensure_cursor("e1")
            store.ensure_cursor("e1")          # idempotent
            with closing(sqlite3.connect(tmp_path / "recall.sqlite")) as connection:
                anchor = connection.execute(
                    "SELECT value FROM runtime_meta WHERE key='initial_user_event_id'"
                ).fetchone()[0]
            self.assertEqual(anchor, "e1")
            self.assertIsNone(store.consumed_rowid())
            store.set_consumed_rowid(7)
            self.assertEqual(store.consumed_rowid(), 7)
            # advancing the watermark never rewrites the anchor
            with closing(sqlite3.connect(tmp_path / "recall.sqlite")) as connection:
                anchor_after = connection.execute(
                    "SELECT value FROM runtime_meta WHERE key='initial_user_event_id'"
                ).fetchone()[0]
            self.assertEqual(anchor_after, "e1")

    def test_first_process_establishes_watermark(self) -> None:
        with self._tmp() as directory:
            tmp_path = Path(directory)
            reader, _, _ = _fixture(tmp_path)
            store = M3Store(tmp_path / "recall.sqlite")
            store.ensure_cursor("e1")
            self.assertIsNone(store.consumed_rowid())
            counts = M3Worker(reader, store).process_once("e1")
            self.assertEqual(counts["evaluated"], 1)          # e3 only
            self.assertGreater(counts["skipped_non_user"], 0)  # e2 is chiyo output
            watermark = store.consumed_rowid()
            self.assertIsNotNone(watermark)
            self.assertEqual(watermark, reader.m0_rowid_for("e3"))

    def test_no_new_events_keeps_watermark(self) -> None:
        with self._tmp() as directory:
            tmp_path = Path(directory)
            reader, _, _ = _fixture(tmp_path)
            store = M3Store(tmp_path / "recall.sqlite")
            store.ensure_cursor("e1")
            worker = M3Worker(reader, store)
            worker.process_once("e1")
            first = store.consumed_rowid()
            again = worker.process_once("e1")
            self.assertEqual(again["evaluated"], 0)
            self.assertEqual(store.consumed_rowid(), first)
            # the discovery query must return nothing to do
            self.assertEqual(reader.trigger_candidates_since(first, 200), [])

    def test_new_event_advances_watermark(self) -> None:
        with self._tmp() as directory:
            tmp_path = Path(directory)
            reader, _, m0 = _fixture(tmp_path)
            store = M3Store(tmp_path / "recall.sqlite")
            store.ensure_cursor("e1")
            worker = M3Worker(reader, store)
            worker.process_once("e1")
            before = store.consumed_rowid()
            _add_event(m0, "e4", 4, "USER_VISIBLE_INPUT", "再看看图书馆那件事")
            counts = worker.process_once("e1")
            self.assertEqual(counts["evaluated"], 1)
            after = store.consumed_rowid()
            self.assertGreater(after, before)
            self.assertEqual(after, reader.m0_rowid_for("e4"))

    def test_failure_does_not_advance_past_the_failed_row(self) -> None:
        with self._tmp() as directory:
            tmp_path = Path(directory)
            reader, _, m0 = _fixture(tmp_path)
            store = M3Store(tmp_path / "recall.sqlite")
            store.ensure_cursor("e1")
            # two fresh triggers; the first of them fails
            _add_event(m0, "e4", 4, "USER_VISIBLE_INPUT", "第一条新的用户事实")
            _add_event(m0, "e5", 5, "USER_VISIBLE_INPUT", "第二条新的用户事实")
            worker = _BoomWorker(reader, store, "e4")
            counts = worker.process_once("e1")
            self.assertEqual(counts["failed_closed"], 1)
            watermark = store.consumed_rowid()
            self.assertLess(watermark, reader.m0_rowid_for("e4"))
            self.assertEqual(watermark, reader.m0_rowid_for("e3"))
            # a retry on a healthy worker resumes from just after the watermark
            resumed = M3Worker(reader, store).process_once("e1")
            self.assertEqual(resumed["failed_closed"], 0)
            self.assertEqual(store.consumed_rowid(), reader.m0_rowid_for("e5"))

    def test_restart_preserves_watermark(self) -> None:
        with self._tmp() as directory:
            tmp_path = Path(directory)
            reader, _, _ = _fixture(tmp_path)
            path = tmp_path / "recall.sqlite"
            first = M3Store(path)
            first.ensure_cursor("e1")
            M3Worker(reader, first).process_once("e1")
            saved = first.consumed_rowid()
            reopened = M3Store(path)                     # simulates a restart
            self.assertEqual(reopened.consumed_rowid(), saved)

    def test_reset_watermark_allows_a_fresh_pass(self) -> None:
        with self._tmp() as directory:
            tmp_path = Path(directory)
            reader, _, _ = _fixture(tmp_path)
            store = M3Store(tmp_path / "recall.sqlite")
            store.ensure_cursor("e1")
            worker = M3Worker(reader, store)
            worker.process_once("e1")
            self.assertIsNotNone(store.consumed_rowid())
            store.reset_consumed_rowid()
            self.assertIsNone(store.consumed_rowid())
            # processed_triggers is still the second correctness layer: the history
            # is re-discovered but nothing is evaluated twice
            again = worker.process_once("e1")
            self.assertEqual(again["evaluated"], 0)
            self.assertEqual(again["duplicate"], 1)


def _add_event_with_delivery(m0: Path, event_id: str, index: int, delivery: str,
                            content: str) -> None:
    """USER_VISIBLE_INPUT with a delivery_status other than RECEIVED."""
    with closing(sqlite3.connect(m0)) as connection, connection:
        connection.execute(
            "INSERT INTO evidence_events VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (event_id, _stamp(index), "chiyo", "USER_VISIBLE_INPUT", delivery,
             "OBSERVED_EXTERNAL_EXPRESSION", "user", content, "c", f"t{index}",
             event_id, json.dumps([{"kind": "test", "id": event_id}]), _stamp(index)),
        )


class _SmallBatchWorker(M3Worker):
    """Same code path, tiny batch, so multi-batch can be proven with few rows."""

    DISCOVERY_BATCH = 2


class M3LateEventTests(unittest.TestCase):
    """Discovery must see a late event; the anchor guard still owns eligibility."""

    def _tmp(self):
        return tempfile.TemporaryDirectory()

    def test_late_event_is_discovered_but_not_a_trigger(self) -> None:
        with self._tmp() as directory:
            tmp_path = Path(directory)
            reader, _, m0 = _fixture(tmp_path)
            store = M3Store(tmp_path / "recall.sqlite")
            # anchor = e3, appended third and temporally latest so far
            store.ensure_cursor("e3")
            worker = M3Worker(reader, store)
            worker.process_once("e3")
            base = store.consumed_rowid()
            self.assertIsNotNone(base)

            # append a LATE event: larger rowid, occurred_at earlier than the anchor
            _add_event(m0, "late", 1, "USER_VISIBLE_INPUT", "迟到但更早发生的事实")

            # A. discovery sees it (a timestamp watermark would have missed it)
            batch = reader.trigger_candidates_since(base, 200)
            self.assertEqual([event["event_id"] for _, event in batch], ["late"])
            self.assertGreater(batch[0][0], base)

            # B. business ordering is still the full-history ordering, so its
            #    absolute sequence sits before the anchor and it is not evaluated
            all_events = reader.events()
            order = [str(item["event_id"]) for item in all_events]
            self.assertLess(order.index("late"), order.index("e3"))

            counts = worker.process_once("e3")
            self.assertEqual(counts["evaluated"], 0)
            self.assertEqual(counts["skipped_before_anchor"], 1)
            self.assertEqual(counts["failed_closed"], 0)

            # watermark may safely pass the discovered row
            self.assertEqual(store.consumed_rowid(), batch[0][0])
            self.assertGreater(store.consumed_rowid(), base)

            # anchor untouched, and the sequence contract is unchanged
            with closing(sqlite3.connect(tmp_path / "recall.sqlite")) as connection:
                anchor = connection.execute(
                    "SELECT value FROM runtime_meta WHERE key='initial_user_event_id'"
                ).fetchone()[0]
            self.assertEqual(anchor, "e3")
            late_sequence = reader.build_trigger(
                next(i for i in all_events if i["event_id"] == "late"), all_events).sequence
            self.assertEqual(late_sequence, order.index("late") + 1)
            self.assertLess(late_sequence, order.index("e3") + 1)


class M3WatermarkCoverageTests(unittest.TestCase):
    def _tmp(self):
        return tempfile.TemporaryDirectory()

    def test_multi_batch_advances_to_the_last_row(self) -> None:
        with self._tmp() as directory:
            tmp_path = Path(directory)
            reader, _, m0 = _fixture(tmp_path)
            for index in range(6, 12):                      # five more user events
                _add_event(m0, f"x{index}", 6, "USER_VISIBLE_INPUT", f"事实 {index}")
            store = M3Store(tmp_path / "recall.sqlite")
            store.ensure_cursor("e1")
            worker = _SmallBatchWorker(reader, store)       # batch = 2
            counts = worker.process_once("e1")
            self.assertEqual(counts["evaluated"], 7)        # e3 + x6..x11 = 7
            self.assertEqual(store.consumed_rowid(), reader.m0_rowid_for("x11"))

    def test_non_user_event_at_the_tail_advances_watermark(self) -> None:
        with self._tmp() as directory:
            tmp_path = Path(directory)
            reader, _, m0 = _fixture(tmp_path)
            _add_event(m0, "e4", 4, "CHIYO_VISIBLE_OUTPUT", "助手输出")
            store = M3Store(tmp_path / "recall.sqlite")
            store.ensure_cursor("e1")
            counts = M3Worker(reader, store).process_once("e1")
            self.assertEqual(counts["evaluated"], 1)              # e3 only
            self.assertGreaterEqual(counts["skipped_non_user"], 2)
            self.assertEqual(store.consumed_rowid(), reader.m0_rowid_for("e4"))

    def test_non_received_event_at_the_tail_advances_watermark(self) -> None:
        with self._tmp() as directory:
            tmp_path = Path(directory)
            reader, _, m0 = _fixture(tmp_path)
            _add_event_with_delivery(m0, "e9", 9, "SENT", "用户输入但未送达")
            store = M3Store(tmp_path / "recall.sqlite")
            store.ensure_cursor("e1")
            counts = M3Worker(reader, store).process_once("e1")
            self.assertEqual(counts["evaluated"], 1)
            self.assertEqual(store.consumed_rowid(), reader.m0_rowid_for("e9"))

    def test_already_processed_event_advances_watermark(self) -> None:
        with self._tmp() as directory:
            tmp_path = Path(directory)
            reader, _, _ = _fixture(tmp_path)
            store = M3Store(tmp_path / "recall.sqlite")
            store.ensure_cursor("e1")
            worker = M3Worker(reader, store)
            worker.process_once("e1")
            first = store.consumed_rowid()
            store.reset_consumed_rowid()                    # re-discover the same rows
            counts = worker.process_once("e1")
            self.assertEqual(counts["duplicate"], 1)        # processed_triggers caught it
            self.assertEqual(counts["evaluated"], 0)
            self.assertEqual(store.consumed_rowid(), first)

    def test_failure_boundary_holds_later_rows_back_then_resumes(self) -> None:
        with self._tmp() as directory:
            tmp_path = Path(directory)
            reader, _, m0 = _fixture(tmp_path)
            for index, name in ((4, "e4"), (5, "e5"), (6, "e6")):
                _add_event(m0, name, index, "USER_VISIBLE_INPUT", f"用户事实 {name}")
            store = M3Store(tmp_path / "recall.sqlite")
            store.ensure_cursor("e1")
            broken = _BoomWorker(reader, store, "e4")
            counts = broken.process_once("e1")
            self.assertEqual(counts["failed_closed"], 1)
            self.assertEqual(store.consumed_rowid(), reader.m0_rowid_for("e3"))
            # e5 / e6 were not consumed past the failure
            self.assertLess(store.consumed_rowid(), reader.m0_rowid_for("e5"))
            resumed = M3Worker(reader, store).process_once("e1")
            self.assertEqual(resumed["failed_closed"], 0)
            self.assertEqual(resumed["evaluated"], 2)       # e5, e6 (e4 is now a known duplicate)
            self.assertEqual(resumed["duplicate"], 1)       # e4 was recorded by the failed pass
            self.assertEqual(store.consumed_rowid(), reader.m0_rowid_for("e6"))

    def test_restart_then_new_rows_only(self) -> None:
        with self._tmp() as directory:
            tmp_path = Path(directory)
            reader, _, m0 = _fixture(tmp_path)
            path = tmp_path / "recall.sqlite"
            first = M3Store(path)
            first.ensure_cursor("e1")
            M3Worker(reader, first).process_once("e1")
            saved = first.consumed_rowid()
            _add_event(m0, "e7", 7, "USER_VISIBLE_INPUT", "重启后新增的事实")

            reopened = M3Store(path)                        # simulated restart
            self.assertEqual(reopened.consumed_rowid(), saved)
            counts = M3Worker(reader, reopened).process_once("e1")
            self.assertEqual(counts["evaluated"], 1)        # only the new row
            self.assertEqual(reopened.consumed_rowid(), reader.m0_rowid_for("e7"))

    def test_reset_clears_watermark_but_not_the_anchor(self) -> None:
        with self._tmp() as directory:
            tmp_path = Path(directory)
            reader, _, _ = _fixture(tmp_path)
            store = M3Store(tmp_path / "recall.sqlite")
            store.ensure_cursor("e1")
            M3Worker(reader, store).process_once("e1")
            self.assertIsNotNone(store.consumed_rowid())
            store.reset_consumed_rowid()
            self.assertIsNone(store.consumed_rowid())
            with closing(sqlite3.connect(tmp_path / "recall.sqlite")) as connection:
                anchor = connection.execute(
                    "SELECT value FROM runtime_meta WHERE key='initial_user_event_id'"
                ).fetchone()[0]
            self.assertEqual(anchor, "e1")


# --------------------------------------------------------------------------- #
# M3-A2.5 equivalence harness
# --------------------------------------------------------------------------- #
def _add_event_full(m0: Path, event_id: str, occurred_at: str, created_at: str,
                    origin: str, content: str, conversation: str = "c",
                    turn: str = "t1", delivery: str | None = None) -> None:
    """Insert one M0 row with explicit ordering keys (for tie-breaker fixtures)."""
    if origin == "USER_VISIBLE_INPUT":
        default_delivery, role, speaker = "RECEIVED", "OBSERVED_EXTERNAL_EXPRESSION", "user"
    else:
        default_delivery, role, speaker = "DELIVERED", "SELF_EXPRESSION", "chiyo"
    with closing(sqlite3.connect(m0)) as connection, connection:
        connection.execute(
            "INSERT INTO evidence_events VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (event_id, occurred_at, "chiyo", origin, delivery or default_delivery, role,
             speaker, content, conversation, turn, event_id,
             json.dumps([{"kind": "test", "id": event_id}]), created_at),
        )


def _old_discovery(reader, worker, anchor: str) -> dict[str, int]:
    """Faithful copy of 58c159a's discovery loop.

    It reimplements ONLY the scan/filter skeleton; build_trigger and _evaluate are
    the real production methods, so the comparator cannot introduce a second set
    of business rules.
    """
    all_events = reader.events()
    ids = [str(item["event_id"]) for item in all_events]
    if anchor not in ids:
        raise RuntimeError("m3 initial cursor event not found")
    cursor = ids.index(anchor)
    counts = {"evaluated": 0, "duplicate": 0, "skipped_non_user": 0, "failed_closed": 0}
    for event in all_events[cursor + 1:]:
        if (event.get("source_origin") != "USER_VISIBLE_INPUT"
                or event.get("delivery_status") != "RECEIVED"):
            counts["skipped_non_user"] += 1
            continue
        try:
            trigger = reader.build_trigger(event, all_events)
            result = worker._evaluate(trigger, all_events)
            counts["duplicate" if result == "duplicate" else "evaluated"] += 1
        except Exception:
            counts["failed_closed"] += 1
    return counts


def _snapshot(store_path: Path) -> dict[str, list]:
    """Everything order-sensitive that must match between the two arms."""
    with closing(sqlite3.connect(store_path)) as connection:
        return {
            "evaluations": connection.execute(
                "SELECT trigger_evidence_id, trigger_sequence, trigger_occurred_at, "
                "conversation_id, visible_history_digest, status FROM evaluations "
                "ORDER BY trigger_sequence, trigger_evidence_id").fetchall(),
            "candidates": connection.execute(
                "SELECT candidate_id, source_max_sequence FROM candidates "
                "ORDER BY candidate_id, source_max_sequence").fetchall(),
            "eligibility": connection.execute(
                "SELECT candidate_ref, eligible FROM eligibility_decisions "
                "ORDER BY candidate_ref, eligible").fetchall(),
            "processed": connection.execute(
                "SELECT trigger_evidence_id FROM processed_triggers "
                "ORDER BY trigger_evidence_id").fetchall(),
        }


def _run_arm(builder, *, new_arm: bool, extra=None):
    with tempfile.TemporaryDirectory() as directory:
        tmp_path = Path(directory)
        reader, m0, anchor = builder(tmp_path)
        store_path = tmp_path / "recall.sqlite"
        store = M3Store(store_path)
        store.ensure_cursor(anchor)
        worker = M3Worker(reader, store)
        counts = worker.process_once(anchor) if new_arm else _old_discovery(reader, worker, anchor)
        if extra is not None:
            extra(m0)
            counts = worker.process_once(anchor) if new_arm else _old_discovery(reader, worker, anchor)
        return _snapshot(store_path), counts


def _fixture_plain(tmp_path: Path):
    reader, _, m0 = _fixture(tmp_path)
    return reader, m0, "e1"


def _fixture_two_conversations(tmp_path: Path):
    reader, _, m0 = _fixture(tmp_path)
    _add_event_full(m0, "c2a", _stamp(2), _stamp(2), "USER_VISIBLE_INPUT", "另一个会话的事实", "c2", "t2")
    _add_event_full(m0, "c2b", _stamp(3), _stamp(3), "CHIYO_VISIBLE_OUTPUT", "另一个会话的回复", "c2", "t3")
    _add_event_full(m0, "c2c", _stamp(4), _stamp(4), "USER_VISIBLE_INPUT", "另一个会话的追问", "c2", "t4")
    return reader, m0, "e1"


def _fixture_multi_turn(tmp_path: Path):
    reader, _, m0 = _fixture(tmp_path)
    for index in range(4, 10):
        _add_event(m0, f"m{index}", min(index, 9), "USER_VISIBLE_INPUT", f"第 {index} 轮用户输入")
        _add_event(m0, f"r{index}", min(index, 9), "CHIYO_VISIBLE_OUTPUT", f"第 {index} 轮回复")
    return reader, m0, "e1"


def _fixture_same_occurred(tmp_path: Path):
    """D: identical occurred_at, different created_at."""
    reader, _, m0 = _fixture(tmp_path)
    _add_event_full(m0, "d1", _stamp(5), _stamp(5), "USER_VISIBLE_INPUT", "同时刻较早写入")
    _add_event_full(m0, "d2", _stamp(5), _stamp(6), "USER_VISIBLE_INPUT", "同时刻较晚写入")
    return reader, m0, "e1"


def _fixture_same_occurred_and_created(tmp_path: Path):
    """E: identical occurred_at AND created_at; only event_id breaks the tie."""
    reader, _, m0 = _fixture(tmp_path)
    _add_event_full(m0, "e-zz", _stamp(6), _stamp(6), "USER_VISIBLE_INPUT", "同键 zz")
    _add_event_full(m0, "e-aa", _stamp(6), _stamp(6), "USER_VISIBLE_INPUT", "同键 aa")
    return reader, m0, "e1"


def _fixture_mixed_origin(tmp_path: Path):
    reader, _, m0 = _fixture(tmp_path)
    _add_event(m0, "mix1", 4, "CHIYO_VISIBLE_OUTPUT", "助手输出一")
    _add_event(m0, "mix2", 5, "USER_VISIBLE_INPUT", "用户输入二")
    _add_event(m0, "mix3", 6, "CHIYO_VISIBLE_OUTPUT", "助手输出三")
    _add_event(m0, "mix4", 7, "USER_VISIBLE_INPUT", "用户输入四")
    return reader, m0, "e1"


def _fixture_mixed_delivery(tmp_path: Path):
    reader, _, m0 = _fixture(tmp_path)
    _add_event_with_delivery(m0, "nd1", 4, "SENT", "用户输入未送达")
    _add_event(m0, "ok1", 5, "USER_VISIBLE_INPUT", "用户输入已送达")
    _add_event_with_delivery(m0, "nd2", 6, "FAILED", "用户输入发送失败")
    return reader, m0, "e1"


class M3EquivalenceTests(unittest.TestCase):
    """Old discovery vs new watermark discovery, compared value by value."""

    def _equivalent(self, builder, extra=None):
        old_snapshot, _ = _run_arm(builder, new_arm=False, extra=extra)
        new_snapshot, _ = _run_arm(builder, new_arm=True, extra=extra)
        self.assertEqual(old_snapshot["evaluations"], new_snapshot["evaluations"],
                         "trigger ids / sequence / occurred_at / digest / status differ")
        self.assertEqual(old_snapshot["candidates"], new_snapshot["candidates"])
        self.assertEqual(old_snapshot["eligibility"], new_snapshot["eligibility"])
        self.assertEqual(old_snapshot["processed"], new_snapshot["processed"])
        return old_snapshot, new_snapshot

    def test_A_plain_sequential(self) -> None:
        self._equivalent(_fixture_plain)

    def test_B_two_conversations(self) -> None:
        self._equivalent(_fixture_two_conversations)

    def test_C_multi_turn(self) -> None:
        self._equivalent(_fixture_multi_turn)

    def test_D_same_occurred_at_different_created_at(self) -> None:
        self._equivalent(_fixture_same_occurred)

    def test_E_same_occurred_and_created_id_tiebreak(self) -> None:
        self._equivalent(_fixture_same_occurred_and_created)

    def test_F_user_assistant_mix(self) -> None:
        self._equivalent(_fixture_mixed_origin)

    def test_G_received_and_non_received_mix(self) -> None:
        self._equivalent(_fixture_mixed_delivery)

    def test_hard_flags(self) -> None:
        """The three named assertions, over every fixture."""
        for builder in (_fixture_plain, _fixture_two_conversations, _fixture_multi_turn,
                        _fixture_same_occurred, _fixture_same_occurred_and_created,
                        _fixture_mixed_origin, _fixture_mixed_delivery):
            old_snapshot, new_snapshot = self._equivalent(builder)
            self.assertEqual([row[1] for row in old_snapshot["evaluations"]],
                             [row[1] for row in new_snapshot["evaluations"]],
                             "TRIGGER_SEQUENCE_EQUIVALENT")
            self.assertEqual([row[4] for row in old_snapshot["evaluations"]],
                             [row[4] for row in new_snapshot["evaluations"]],
                             "VISIBLE_DIGEST_EQUIVALENT")
            self.assertEqual([row[1] for row in old_snapshot["candidates"]],
                             [row[1] for row in new_snapshot["candidates"]],
                             "SOURCE_MAX_SEQUENCE_EQUIVALENT")

    def test_H_large_history_then_increment(self) -> None:
        """1000+ events, ~990 consumed, then 10 new: same final business result."""
        def builder(tmp_path: Path):
            m0, m1, m2, epoch = _create_source_dbs(tmp_path)
            for index in range(1, 991):
                if index % 50 == 0:                       # sparse real triggers
                    _add_event_full(m0, f"u{index}", _stamp(index % 10),
                                    _stamp(index % 10), "USER_VISIBLE_INPUT",
                                    f"用户事实 {index}")
                else:
                    _add_event_full(m0, f"a{index}", _stamp(index % 10),
                                    _stamp(index % 10), "CHIYO_VISIBLE_OUTPUT",
                                    f"助手输出 {index}")
            _add_episode(m1, "ep-old", ["a1", "a2"])
            reader = SourceReader(m0, m1, m2, epoch)
            return reader, m0, "a1"

        def add_ten(m0: Path):
            for index in range(1001, 1011):
                _add_event_full(m0, f"n{index}", _stamp(index % 10), _stamp(index % 10),
                                "USER_VISIBLE_INPUT", f"新增用户事实 {index}")

        old_snapshot, _ = _run_arm(builder, new_arm=False, extra=add_ten)
        new_snapshot, _ = _run_arm(builder, new_arm=True, extra=add_ten)
        self.assertEqual(old_snapshot["evaluations"], new_snapshot["evaluations"])
        self.assertEqual(old_snapshot["candidates"], new_snapshot["candidates"])
        self.assertEqual(old_snapshot["processed"], new_snapshot["processed"])
        self.assertGreater(len(new_snapshot["processed"]), 0)

        # discovery must return exactly the new rows on a large history
        with tempfile.TemporaryDirectory() as directory:
            tmp_path = Path(directory)
            reader, m0, anchor = builder(tmp_path)
            store = M3Store(tmp_path / "recall.sqlite")
            store.ensure_cursor(anchor)
            M3Worker(reader, store).process_once(anchor)
            watermark = store.consumed_rowid()
            self.assertGreater(watermark, 900)                      # 990 already consumed
            self.assertEqual(reader.trigger_candidates_since(watermark, 200), [])
            add_ten(m0)
            fresh = reader.trigger_candidates_since(watermark, 200)
            self.assertEqual(len(fresh), 10)                        # only the new rows
            self.assertTrue(all(rowid > watermark for rowid, _ in fresh))

    def test_discovery_spy_returns_only_new_rows(self) -> None:
        """Discovery reads only rows beyond the watermark; [] when nothing is new."""
        with tempfile.TemporaryDirectory() as directory:
            tmp_path = Path(directory)
            reader, m0, anchor = _fixture_plain(tmp_path)
            store = M3Store(tmp_path / "recall.sqlite")
            store.ensure_cursor(anchor)
            worker = M3Worker(reader, store)
            worker.process_once(anchor)
            watermark = store.consumed_rowid()
            self.assertEqual(reader.trigger_candidates_since(watermark, 200), [])
            for index in (8, 9):
                _add_event(m0, f"s{index}", index, "USER_VISIBLE_INPUT", f"新增 {index}")
            fresh = reader.trigger_candidates_since(watermark, 200)
            self.assertEqual(len(fresh), 2)
            self.assertEqual([event["event_id"] for _, event in fresh], ["s8", "s9"])
            self.assertTrue(all(rowid > watermark for rowid, _ in fresh))


class M3LateEventEquivalenceTest(unittest.TestCase):
    """The one expected discovery difference, reported on its own."""

    def test_old_vs_new_discovery_of_a_late_event(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            tmp_path = Path(directory)
            reader, _, m0 = _fixture(tmp_path)
            store = M3Store(tmp_path / "recall.sqlite")
            store.ensure_cursor("e3")
            worker = M3Worker(reader, store)
            # consume everything up to the anchor first
            worker.process_once("e3")
            base = store.consumed_rowid()
            _add_event(m0, "late", 1, "USER_VISIBLE_INPUT", "迟到但更早发生")

            all_events = reader.events()
            ids = [str(item["event_id"]) for item in all_events]
            old_sees = "late" in ids[ids.index("e3") + 1:]
            new_batch = reader.trigger_candidates_since(base, 200)
            new_sees = [event["event_id"] for _, event in new_batch] == ["late"]
            self.assertFalse(old_sees)      # OLD: the tail scan cannot reach it
            self.assertTrue(new_sees)       # NEW: discovered

            # neither arm evaluates it: its global sequence precedes the anchor
            old_counts = _old_discovery(reader, worker, "e3")
            new_counts = worker.process_once("e3")
            self.assertEqual(old_counts["evaluated"], 0)
            self.assertEqual(new_counts["evaluated"], 0)
            self.assertEqual(new_counts["skipped_before_anchor"], 1)
            self.assertEqual(old_counts["evaluated"], new_counts["evaluated"])
