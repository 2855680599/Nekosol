"""M3 source reader resources: every connection is closed, on every path.

P3-D. ``app/m3.py`` opens M0/M1/M2 read-only and the derived shadow store
read-write. The methods already used ``try/finally``; these tests pin the two
guarantees that were missing and the one that was invisible:

* A. a normal ``SourceReader.events()`` call returns the ordered history and
  leaves nothing open;
* B. an exception while the rows are being processed -- injected, not simulated
  by hand -- still closes the connection, and so does a failure inside the
  connection's own setup (the fail-closed path);
* C. repeated calls do not accumulate connections;
* D. the observable M3 result of one deterministic evaluation is byte-for-byte
  what it was before the change (frozen digests).

Run under ``-W error::ResourceWarning``: any connection that reaches the garbage
collector without being closed fails the suite.
"""
from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

from app import m3
from app.m3 import M3Store, M3Worker, SourceReader, connect_ro


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
        json.dumps({"epoch_id": "epoch-test",
                    "boundaries": {"c": {"boundary_id": "boundary-test", "record_count": 2}}}),
        encoding="utf-8",
    )
    return m0, m1, m2, epoch


def _stamp(index: int) -> str:
    return f"2026-01-01T00:0{index}:00+00:00"


def _add_event(m0: Path, event_id: str, index: int, origin: str, content: str) -> None:
    if origin == "USER_VISIBLE_INPUT":
        delivery, role, speaker = "RECEIVED", "OBSERVED_EXTERNAL_EXPRESSION", "user"
    else:
        delivery, role, speaker = "DELIVERED", "SELF_EXPRESSION", "chiyo"
    with closing(sqlite3.connect(m0)) as connection, connection:
        connection.execute(
            "INSERT INTO evidence_events VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (event_id, _stamp(index), "chiyo", origin, delivery, role, speaker, content,
             "c", f"t{index}", event_id, json.dumps([{"kind": "test", "id": event_id}]), _stamp(index)),
        )


def _add_episode(m1: Path, episode_id: str, event_ids: list[str], status: str = "CLOSED") -> None:
    with closing(sqlite3.connect(m1)) as connection, connection:
        connection.execute(
            "INSERT INTO episodes VALUES(?,?,?,?,?,?,?,?)",
            (episode_id, "chiyo", _stamp(1), _stamp(3) if status == "CLOSED" else None,
             status, "test", _stamp(1), _stamp(3)),
        )
        for sequence, event_id in enumerate(event_ids):
            connection.execute("INSERT INTO episode_evidence VALUES(?,?,?,?)",
                               (episode_id, event_id, "PRIMARY", sequence))


def _add_understanding(m2: Path, uid: str, episode_id: str, event_id: str, status: str) -> None:
    with closing(sqlite3.connect(m2)) as connection, connection:
        connection.execute(
            "INSERT INTO understandings VALUES(?,?,?,?,?,?,?,?,?,?)",
            (uid, status, "EPISODE_INTERPRETATION", "已确认的过去经历", "USER_ASSERTED",
             0, "[]", "snapshot", "test", _stamp(2)),
        )
        connection.execute("INSERT INTO understanding_episodes VALUES(?,?)", (uid, episode_id))
        connection.execute("INSERT INTO understanding_evidence VALUES(?,?)", (uid, event_id))


def _fixture(tmp_path: Path) -> tuple[SourceReader, list[dict], Path]:
    m0, m1, m2, epoch = _create_source_dbs(tmp_path)
    _add_event(m0, "e1", 1, "USER_VISIBLE_INPUT", "我之前去了图书馆学习日语")
    _add_event(m0, "e2", 2, "CHIYO_VISIBLE_OUTPUT", "记住了")
    _add_event(m0, "e3", 3, "USER_VISIBLE_INPUT", "你之前说过图书馆吗")
    _add_episode(m1, "ep-old", ["e1", "e2"])
    _add_episode(m1, "ep-current", ["e3"], status="OPEN")
    _add_understanding(m2, "u-active", "ep-old", "e1", "ACTIVE")
    _add_understanding(m2, "u-uncertain", "ep-old", "e1", "UNCERTAIN")
    reader = SourceReader(m0, m1, m2, epoch)
    return reader, reader.events(), m0


def _open_handles(path: Path) -> int:
    """Open file descriptors pointing at *path*; /proc is Linux-only."""
    directory = Path("/proc/self/fd")
    if not directory.is_dir():
        raise unittest.SkipTest("requires /proc/self/fd")
    target = str(Path(path).resolve())
    count = 0
    for entry in directory.iterdir():
        try:
            if os.readlink(entry) == target:
                count += 1
        except OSError:
            continue
    return count


def _is_closed(connection: sqlite3.Connection) -> bool:
    try:
        connection.execute("SELECT 1")
    except sqlite3.ProgrammingError:
        return True
    return False


class _FailingSetupConnection:
    """A connection whose own setup raises once -- an injected fail-closed fault."""

    def __init__(self, real: sqlite3.Connection):
        self.real = real
        self.failures = 0

    def __getattr__(self, name):
        return getattr(self.real, name)

    def execute(self, sql, *args):
        if "PRAGMA" in sql and self.failures:
            self.failures -= 1
            raise sqlite3.OperationalError("injected PRAGMA failure")
        return self.real.execute(sql, *args)


class M3ReaderResourceTests(unittest.TestCase):
    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._temp.name)

    def tearDown(self):
        self._temp.cleanup()

    # ---- A: normal call -------------------------------------------------
    def test_normal_events_call_returns_ordered_history(self):
        reader, events, m0 = _fixture(self.tmp_path)
        again = reader.events()
        self.assertEqual([item["event_id"] for item in again], ["e1", "e2", "e3"])
        self.assertEqual(again, events)
        self.assertEqual(again[0]["source_refs"], [{"kind": "test", "id": "e1"}])
        self.assertEqual(_open_handles(m0), 0)

    def test_every_reader_method_closes_its_connection(self):
        reader, events, m0 = _fixture(self.tmp_path)
        self.assertEqual(reader.events(), events)
        episodes, membership = reader.m1_data()
        # ordered by started_at then episode_id, exactly as the query asks
        self.assertEqual([item["episode_id"] for item in episodes], ["ep-current", "ep-old"])
        self.assertEqual(sorted(membership), ["ep-current", "ep-old"])
        rows = reader.m2_data()
        self.assertEqual([row["understanding_id"] for row in rows], ["u-active", "u-uncertain"])
        self.assertEqual(rows[0]["episode_refs"], ["ep-old"])
        self.assertEqual(reader.m0_rowid_for("e2"), 2)
        self.assertEqual(reader.epoch("c")[0], "epoch-test")
        self.assertEqual(_open_handles(m0), 0)

    # ---- B: injected exceptions ----------------------------------------
    def test_exception_while_processing_rows_still_closes_the_connection(self):
        reader, events, m0 = _fixture(self.tmp_path)
        handed_out: list[sqlite3.Connection] = []
        real_connect_ro = m3.connect_ro

        def recording_connect_ro(path):
            connection = real_connect_ro(path)
            handed_out.append(connection)
            return connection

        with mock.patch.object(m3, "connect_ro", side_effect=recording_connect_ro), \
             mock.patch.object(SourceReader, "_row_event", side_effect=RuntimeError("injected")):
            with self.assertRaises(RuntimeError):
                reader.events()
        self.assertTrue(handed_out, "the injected failure must happen on a real connection")
        for connection in handed_out:
            self.assertTrue(_is_closed(connection), "an exception path left the connection open")
        self.assertEqual(_open_handles(m0), 0)

    def test_failing_connection_setup_closes_the_connection(self):
        reader, _events, m0 = _fixture(self.tmp_path)
        opened: list[_FailingSetupConnection] = []
        real_connect = sqlite3.connect

        def fake_connect(*args, **kwargs):
            wrapper = _FailingSetupConnection(real_connect(*args, **kwargs))
            wrapper.failures = 1
            opened.append(wrapper)
            return wrapper

        # read-only reader factory
        with mock.patch.object(m3.sqlite3, "connect", side_effect=fake_connect):
            with self.assertRaises(sqlite3.OperationalError):
                connect_ro(m0)
        self.assertTrue(opened)
        self.assertTrue(_is_closed(opened[-1].real))
        for connection in opened:
            self.assertTrue(_is_closed(connection.real))

        # shadow-store factory, reached through M3Store's own initialization
        opened.clear()
        with mock.patch.object(m3.sqlite3, "connect", side_effect=fake_connect):
            with self.assertRaises(sqlite3.OperationalError):
                M3Store(self.tmp_path / "recall.sqlite")
        self.assertTrue(opened)
        self.assertTrue(_is_closed(opened[-1].real))

    def test_missing_source_file_raises_without_leaving_a_connection(self):
        with self.assertRaises(sqlite3.OperationalError):
            connect_ro(self.tmp_path / "absent.sqlite")

    # ---- C: repeat calls ------------------------------------------------
    def test_repeated_calls_do_not_accumulate_connections(self):
        reader, _events, m0 = _fixture(self.tmp_path)
        before = _open_handles(m0)
        for _ in range(25):
            reader.events()
            reader.m0_rowid_for("e1")
            reader.m1_data()
            reader.m2_data()
        self.assertEqual(_open_handles(m0), before)
        store = M3Store(self.tmp_path / "recall.sqlite")
        for _ in range(25):
            store.verify()
            store.recent(1)
            store.consumed_rowid()
        self.assertEqual(_open_handles(self.tmp_path / "recall.sqlite"), 0)

    # ---- D: M3 semantics unchanged --------------------------------------
    def test_m3_result_is_byte_for_byte_unchanged(self):
        reader, _events, _m0 = _fixture(self.tmp_path)
        store = M3Store(self.tmp_path / "recall.sqlite")
        counts = M3Worker(reader, store).process_once("e1")
        row = store.recent(1)[0]
        detail = store.why(row["evaluation_id"])
        payload = json.loads(detail["availability"]["surface_payload_json"])
        self.assertEqual(counts, {"evaluated": 1, "skipped_non_user": 1, "skipped_unknown": 0,
                                  "skipped_before_anchor": 0, "duplicate": 0, "failed_closed": 0})
        # frozen before the change, on the same deterministic fixture
        self.assertEqual(row["evaluation_id"], "350877d28ca15d50ad1581d9c085c7bb")
        self.assertEqual(row["cognitive_intent"], "P1")
        self.assertEqual(row["status"], "COMPLETED")
        self.assertEqual(row["visible_history_digest"],
                         "b9a260bf770c11ffe6fbbb3e3e81f46b03ac7705977d89704b7b6c857d5a4bd2")
        self.assertEqual(row["context_snapshot_digest"],
                         "a91a1beedf61f75f72e9dceaf56eca830952b78ba47908e2e7a8ce28c325d62f")
        self.assertEqual(m3.digest(payload),
                         "14352f80a6eb7c9268d3482dfe10c5378fb4433dc78103ad3ef7b00a114c8634")
        self.assertEqual(detail["availability"]["outcome"], "SURFACE_EPISODE_WITH_SECONDARY_UNDERSTANDING")
        self.assertEqual(json.loads(detail["availability"]["selected_refs_json"]),
                         ["episode:ep-old", "m2:u-active"])
        self.assertEqual([(item["candidate_id"], item["candidate_digest"]) for item in detail["candidates"]],
                         [("episode:ep-old",
                           "d3eae19ffd05cd2dbe0f8640583a247bb6b737ea919c21c042320d6318517ba2")])
        self.assertEqual([(item["understanding_id"], item["status"]) for item in detail["secondary_expansions"]],
                         [("u-active", "ACTIVE"), ("u-uncertain", "UNCERTAIN")])
        self.assertEqual(store.verify()["ok"], True)


if __name__ == "__main__":
    unittest.main()
