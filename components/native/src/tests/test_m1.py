from __future__ import annotations

from pathlib import Path
import hashlib
import sqlite3
import tempfile
import unittest

from app.evidence import (
    EvidenceEvent,
    EvidenceStore,
    RECEIVED,
    USER_ORIGIN,
    USER_ROLE,
)
from app.m1 import (
    M1_ALGORITHM_VERSION,
    EpisodeDecision,
    EpisodeStore,
    EpisodeWorker,
    M0EvidenceReader,
)


def add_event(store: EvidenceStore, index: int, content: str, conversation: str = "c") -> dict:
    minute = index % 60
    hour = 20 + (index // 60)
    timestamp = f"2026-09-07T{hour:02d}:{minute:02d}:00+00:00"
    event = EvidenceEvent(
        event_id=f"0199a000-0000-7000-8000-{index:012d}",
        occurred_at=timestamp,
        memory_owner="chiyo",
        source_origin=USER_ORIGIN,
        delivery_status=RECEIVED,
        epistemic_role=USER_ROLE,
        speaker="user",
        content=content,
        conversation_id=conversation,
        turn_id=f"turn-{index}",
        source_refs=[{"kind": "test-input", "id": f"input-{index}"}],
        created_at=timestamp,
    )
    store.append(event)
    return store.get(event.event_id)


class FunctionalJudge:
    def __init__(self, function):
        self.function = function

    def decide(self, current_event, open_episodes):
        return self.function(current_event, open_episodes)


class BrokenStore(EpisodeStore):
    def apply_event(self, *args, **kwargs):
        raise OSError("simulated episode store failure")


class M1Tests(unittest.TestCase):
    def setup_fixture(self):
        temp = tempfile.TemporaryDirectory()
        root = Path(temp.name)
        m0 = EvidenceStore(root / "memory" / "evidence.sqlite")
        reader = M0EvidenceReader(root / "memory" / "evidence.sqlite")
        m1 = EpisodeStore(root / "memory" / "episodes.sqlite")
        return temp, root, m0, reader, m1

    def test_m0_reader_is_read_only(self):
        temp, root, m0, reader, m1 = self.setup_fixture()
        try:
            add_event(m0, 1, "事实")
            connection = reader._connect()
            with self.assertRaises(sqlite3.OperationalError):
                connection.execute("CREATE TABLE forbidden_write(x TEXT)")
            connection.close()
            self.assertEqual(len(reader.list_events()), 1)
        finally:
            temp.cleanup()

    def test_case_a_continuous_activity_one_episode(self):
        temp, root, m0, reader, m1 = self.setup_fixture()
        try:
            for index, text in enumerate(["聊服务器", "排查问题", "找到原因", "修复"]):
                add_event(m0, index, text)

            def continue_last(current, open_episodes):
                return EpisodeDecision("CONTINUE_EXISTING", [open_episodes[-1]["episode_id"]], reason="same activity")

            result = EpisodeWorker(reader, m1, FunctionalJudge(continue_last)).process_once()
            self.assertEqual(result["inserted"], 4)
            self.assertEqual(len(m1.list_episodes()), 1)
            self.assertEqual(m1.verify(reader)["assigned_evidence"], 4)
        finally:
            temp.cleanup()

    def test_case_b_switch_creates_two_episodes(self):
        temp, root, m0, reader, m1 = self.setup_fixture()
        try:
            add_event(m0, 0, "聊服务器")
            add_event(m0, 1, "结束，开始聊明天考试")

            def start_new(current, open_episodes):
                return EpisodeDecision("START_NEW", [], start_new=True, reason="new independent activity")

            EpisodeWorker(reader, m1, FunctionalJudge(start_new)).process_once()
            episodes = m1.list_episodes()
            self.assertEqual(len(episodes), 2)
            self.assertEqual(episodes[0]["status"], "CLOSED")
        finally:
            temp.cleanup()

    def test_case_c_parallel_activity_allows_overlap(self):
        temp, root, m0, reader, m1 = self.setup_fixture()
        try:
            add_event(m0, 0, "聊考研")
            add_event(m0, 1, "继续考研，同时开始争执")

            def share(current, open_episodes):
                return EpisodeDecision(
                    "SHARE_WITH_EXISTING",
                    [open_episodes[-1]["episode_id"]],
                    shared=True,
                    reason="parallel local event line",
                )

            EpisodeWorker(reader, m1, FunctionalJudge(share)).process_once()
            rows = m1.memberships_for_event(reader.list_events()[-1]["event_id"])
            self.assertEqual(len(rows), 2)
            self.assertEqual({row["membership_kind"] for row in rows}, {"PRIMARY", "SHARED"})
        finally:
            temp.cleanup()

    def test_case_d_same_topic_does_not_merge_independent_events(self):
        temp, root, m0, reader, m1 = self.setup_fixture()
        try:
            add_event(m0, 0, "九月聊考研")
            add_event(m0, 1, "十月再次聊考研")

            def split(current, open_episodes):
                return EpisodeDecision("START_NEW", [], start_new=True, reason="separate local event")

            EpisodeWorker(reader, m1, FunctionalJudge(split)).process_once()
            self.assertEqual(len(m1.list_episodes()), 2)
        finally:
            temp.cleanup()

    def test_case_e_new_conversation_does_not_force_boundary(self):
        temp, root, m0, reader, m1 = self.setup_fixture()
        try:
            add_event(m0, 0, "服务器还没修好", "window-a")
            add_event(m0, 1, "继续刚才那个问题", "window-b")

            def continue_across_window(current, open_episodes):
                return EpisodeDecision("CONTINUE_EXISTING", [open_episodes[-1]["episode_id"]], reason="same event across transport")

            EpisodeWorker(reader, m1, FunctionalJudge(continue_across_window)).process_once()
            self.assertEqual(len(m1.list_episodes()), 1)
        finally:
            temp.cleanup()

    def test_case_g_fragments_can_remain_unassigned(self):
        temp, root, m0, reader, m1 = self.setup_fixture()
        try:
            add_event(m0, 0, "一个完整活动")
            add_event(m0, 1, "嗯")

            def leave_fragment(current, open_episodes):
                return EpisodeDecision("CONTINUE_EXISTING", [], reason="insufficient event continuity")

            EpisodeWorker(reader, m1, FunctionalJudge(leave_fragment)).process_once()
            result = m1.verify(reader)
            self.assertEqual(result["episode_count"], 1)
            self.assertEqual(result["unassigned_evidence"], 1)
        finally:
            temp.cleanup()

    def test_duplicate_event_is_idempotent(self):
        temp, root, m0, reader, m1 = self.setup_fixture()
        try:
            add_event(m0, 0, "一次事件")
            judge = FunctionalJudge(lambda current, open_episodes: EpisodeDecision("CONTINUE_EXISTING", [open_episodes[-1]["episode_id"]]))
            worker = EpisodeWorker(reader, m1, judge)
            first = worker.process_once()
            second = worker.process_once()
            self.assertEqual(first["inserted"], 1)
            self.assertEqual(second["inserted"], 0)
            self.assertEqual(len(m1.list_episodes()), 1)
            self.assertEqual(m1.verify(reader)["membership_count"], 1)
        finally:
            temp.cleanup()

    def test_rebuild_preserves_m0_and_memberships(self):
        temp, root, m0, reader, m1 = self.setup_fixture()
        try:
            add_event(m0, 0, "第一段")
            add_event(m0, 1, "第二段")
            before_rows = reader.list_events()
            before_hash = hashlib.sha256((root / "memory" / "evidence.sqlite").read_bytes()).hexdigest()
            judge = FunctionalJudge(lambda current, open_episodes: EpisodeDecision("CONTINUE_EXISTING", [open_episodes[-1]["episode_id"]]))
            worker = EpisodeWorker(reader, m1, judge)
            worker.process_once()
            m1.clear_derived()
            worker.process_once()
            after_rows = reader.list_events()
            after_hash = hashlib.sha256((root / "memory" / "evidence.sqlite").read_bytes()).hexdigest()
            self.assertEqual(before_rows, after_rows)
            self.assertEqual(before_hash, after_hash)
            self.assertTrue(m1.verify(reader)["ok"])
        finally:
            temp.cleanup()

    def test_store_failure_does_not_change_m0(self):
        temp, root, m0, reader, unused = self.setup_fixture()
        try:
            add_event(m0, 0, "上游事实")
            before = reader.list_events()
            broken = BrokenStore(root / "memory" / "broken.sqlite")
            worker = EpisodeWorker(reader, broken, FunctionalJudge(lambda current, open_episodes: EpisodeDecision("START_NEW", [], start_new=True)))
            result = worker.process_once()
            self.assertEqual(result["failed"], 1)
            self.assertEqual(before, reader.list_events())
        finally:
            temp.cleanup()

    def test_malformed_judge_falls_back_and_records_error(self):
        temp, root, m0, reader, m1 = self.setup_fixture()
        try:
            add_event(m0, 0, "第一条")
            add_event(m0, 1, "第二条")

            class BrokenJudge:
                def decide(self, current_event, open_episodes):
                    raise ValueError("malformed JSON")

            result = EpisodeWorker(reader, m1, BrokenJudge()).process_once()
            self.assertEqual(result["inserted"], 2)
            connection = sqlite3.connect(m1.path)
            error = connection.execute("SELECT error FROM segment_decisions WHERE event_id != (SELECT MIN(event_id) FROM segment_decisions)").fetchone()[0]
            connection.close()
            self.assertIn("ValueError", error)
        finally:
            temp.cleanup()

    def test_no_projection_table_and_verify_overlap_metrics(self):
        temp, root, m0, reader, m1 = self.setup_fixture()
        try:
            add_event(m0, 0, "并行事件")
            add_event(m0, 1, "共享证据")

            def share(current, open_episodes):
                return EpisodeDecision("SHARE_WITH_EXISTING", [open_episodes[-1]["episode_id"]], shared=True)

            EpisodeWorker(reader, m1, FunctionalJudge(share)).process_once()
            result = m1.verify(reader)
            self.assertEqual(result["projection_tables"], [])
            self.assertEqual(result["multi_membership_evidence"], 1)
            self.assertTrue(result["ok"])
        finally:
            temp.cleanup()


if __name__ == "__main__":
    unittest.main()
