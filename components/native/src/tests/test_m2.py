from __future__ import annotations

from pathlib import Path
import hashlib
import json
import sqlite3
import tempfile
import unittest

from app.evidence import EvidenceEvent, EvidenceStore, RECEIVED, USER_ORIGIN, USER_ROLE
from app.m1 import EpisodeDecision, EpisodeStore, EpisodeWorker, M0EvidenceReader
from app.m2 import (
    M2_GENERATOR_VERSION,
    M1ReadOnlyStore,
    M2Store,
    M2Worker,
    ValidatedProposal,
)


def add_event(store: EvidenceStore, index: int, content: str, conversation: str = "c") -> dict:
    timestamp = f"2026-09-07T12:{index:02d}:00+00:00"
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


class StartNewJudge:
    def decide(self, current_event, open_episodes):
        return EpisodeDecision("START_NEW", [], start_new=True, reason="test boundary")


class FakeProposer:
    def __init__(self, episode_mode="safe", cross_mode="empty"):
        self.episode_mode = episode_mode
        self.cross_mode = cross_mode

    def propose_episode(self, snapshot):
        event_id = snapshot["evidence"][0]["event_id"]
        if callable(self.episode_mode):
            return self.episode_mode(snapshot)
        if self.episode_mode == "malformed":
            return "not json"
        if self.episode_mode == "swapped_schema":
            return json.dumps(
                {
                    "episode_interpretations": [
                        {
                            "statement": "Alex在该 Episode 中表达了一个具体话题。",
                            "understanding_kind": "UTTERANCE_AS_EVENT",
                            "epistemic_basis": "该标签说明这里只保留表达事件，不升级为现实事实。",
                            "supporting_evidence_refs": [event_id],
                        }
                    ],
                    "contradictions": [],
                    "uncertainties": [],
                },
                ensure_ascii=False,
            )
        if self.episode_mode == "prefixed_refs":
            return json.dumps(
                {
                    "episode_interpretations": [
                        {
                            "statement": "Alex在该 Episode 中表达了一个具体话题。",
                            "understanding_kind": "EPISODE_INTERPRETATION",
                            "epistemic_basis": "UTTERANCE_CONTENT_ASSERTION",
                            "supporting_evidence_refs": [f"event_id:{event_id}"],
                        }
                    ],
                    "contradictions": [],
                    "uncertainties": [],
                },
                ensure_ascii=False,
            )
        if self.episode_mode == "false_bio":
            statement = "该个体昨天去了海边。"
        elif self.episode_mode == "false_relationship":
            statement = "Alex和该个体是恋人关系。"
        elif self.episode_mode == "false_body":
            statement = "Alex发烧了。"
        elif self.episode_mode == "false_world":
            statement = "窗外的夕阳真的发生了。"
        elif self.episode_mode == "imperative":
            statement = "以后应该安慰Alex。"
        else:
            statement = "Alex在该 Episode 中表达了一个具体话题。"
        return json.dumps(
            {
                "episode_interpretations": [
                    {
                        "statement": statement,
                        "understanding_kind": "EPISODE_INTERPRETATION",
                        "epistemic_basis": "UTTERANCE_CONTENT_ASSERTION",
                        "supporting_evidence_refs": [event_id],
                    }
                ],
                "contradictions": [],
                "uncertainties": [],
            },
            ensure_ascii=False,
        )

    def propose_cross(self, snapshots):
        refs = [item["episode"]["episode_id"] for item in snapshots[:2]]
        evidence = [item["evidence"][0]["event_id"] for item in snapshots[:2]]
        if self.cross_mode == "contradiction":
            return json.dumps(
                {
                    "patterns": [],
                    "contradictions": [
                        {
                            "statement": "不同时间的 Episode 中，Alex表达了相反的看法。",
                            "understanding_kind": "CONTRADICTION",
                            "epistemic_basis": "CROSS_EPISODE_PATTERN",
                            "supporting_episode_refs": refs,
                            "supporting_evidence_refs": evidence,
                        }
                    ],
                    "uncertainties": [],
                },
                ensure_ascii=False,
            )
        if self.cross_mode == "pattern":
            return json.dumps(
                {
                    "patterns": [
                        {
                            "statement": "多个不同 Episode 中出现了相似的讨论结构。",
                            "understanding_kind": "PATTERN_CANDIDATE",
                            "epistemic_basis": "CROSS_EPISODE_PATTERN",
                            "supporting_episode_refs": refs,
                            "supporting_evidence_refs": evidence,
                        }
                    ],
                    "contradictions": [],
                    "uncertainties": [],
                },
                ensure_ascii=False,
            )
        return json.dumps({"patterns": [], "contradictions": [], "uncertainties": []})


class M2Tests(unittest.TestCase):
    def setup_fixture(self, event_count=3):
        temp = tempfile.TemporaryDirectory()
        root = Path(temp.name)
        m0 = EvidenceStore(root / "memory" / "evidence.sqlite")
        reader = M0EvidenceReader(root / "memory" / "evidence.sqlite")
        m1 = EpisodeStore(root / "memory" / "episodes.sqlite")
        for index in range(event_count):
            add_event(m0, index, f"测试表达 {index}")
        EpisodeWorker(reader, m1, StartNewJudge()).process_once()
        m1_reader = M1ReadOnlyStore(m1.path)
        m2 = M2Store(root / "memory" / "understandings.sqlite")
        return temp, root, m0, reader, m1, m1_reader, m2

    def closed_snapshots(self, reader, evidence):
        return [reader.snapshot(item["episode_id"], evidence) for item in reader.list_episodes("CLOSED")]

    def test_m1_and_m0_readers_are_read_only(self):
        temp, root, m0, reader, m1, m1_reader, m2 = self.setup_fixture()
        try:
            with self.assertRaises(sqlite3.OperationalError):
                reader._connect().execute("CREATE TABLE forbidden_m0(x TEXT)")
            connection = m1_reader._connect()
            with self.assertRaises(sqlite3.OperationalError):
                connection.execute("CREATE TABLE forbidden_m1(x TEXT)")
            connection.close()
        finally:
            temp.cleanup()

    def test_false_biography_relationship_body_world_and_authority_are_not_active(self):
        for mode in ("false_bio", "false_relationship", "false_body", "false_world", "imperative"):
            temp, root, m0, reader, m1, m1_reader, m2 = self.setup_fixture(event_count=2)
            try:
                result = M2Worker(reader, m1_reader, m2, FakeProposer(mode), enable_cross=False).process_once()
                self.assertEqual(result["failed"], 0)
                self.assertEqual(result["accepted"], 1)
                active = m2.list_recent(20, "ACTIVE")
                self.assertEqual(active, [], mode)
                flagged = m2.list_recent(20)
                self.assertTrue(any(item["status"] == "REVIEW" for item in flagged), mode)
            finally:
                temp.cleanup()

    def test_utterance_assertion_is_preserved_without_reality_upgrade(self):
        temp, root, m0, reader, m1, m1_reader, m2 = self.setup_fixture(event_count=2)
        try:
            result = M2Worker(reader, m1_reader, m2, FakeProposer(), enable_cross=False).process_once()
            self.assertEqual(result["accepted"], 1)
            rows = m2.list_recent(20, "ACTIVE")
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["epistemic_basis"], "UTTERANCE_CONTENT_ASSERTION")
            self.assertEqual(rows[0]["review_flags"], [])
        finally:
            temp.cleanup()

    def test_unambiguous_swapped_schema_is_normalized(self):
        temp, root, m0, reader, m1, m1_reader, m2 = self.setup_fixture(event_count=2)
        try:
            result = M2Worker(reader, m1_reader, m2, FakeProposer("swapped_schema"), enable_cross=False).process_once()
            self.assertEqual(result["accepted"], 1)
            row = m2.list_recent(10, "ACTIVE")[0]
            self.assertEqual(row["understanding_kind"], "EPISODE_INTERPRETATION")
            self.assertEqual(row["epistemic_basis"], "UTTERANCE_AS_EVENT")
        finally:
            temp.cleanup()

    def test_unambiguous_event_id_prefix_is_normalized(self):
        temp, root, m0, reader, m1, m1_reader, m2 = self.setup_fixture(event_count=2)
        try:
            expected = self.closed_snapshots(m1_reader, reader)[0]["evidence"][0]["event_id"]
            result = M2Worker(reader, m1_reader, m2, FakeProposer("prefixed_refs"), enable_cross=False).process_once()
            self.assertEqual(result["accepted"], 1)
            row = m2.list_recent(10, "ACTIVE")[0]
            trace = m2.evidence_trace(row["understanding_id"])
            self.assertEqual([item["event_id"] for item in trace["evidence_refs"]], [expected])
        finally:
            temp.cleanup()

    def test_cross_episode_pattern_and_contradiction_require_two_episodes(self):
        temp, root, m0, reader, m1, m1_reader, m2 = self.setup_fixture(event_count=4)
        try:
            result = M2Worker(reader, m1_reader, m2, FakeProposer(cross_mode="contradiction"), enable_cross=True).process_once()
            self.assertEqual(result["cross_processed"], 1)
            rows = [item for item in m2.list_recent(50) if item["understanding_kind"] == "CONTRADICTION"]
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["epistemic_basis"], "CROSS_EPISODE_PATTERN")
            trace = m2.evidence_trace(rows[0]["understanding_id"])
            self.assertGreaterEqual(len(trace["episode_refs"]), 2)
        finally:
            temp.cleanup()

    def test_idempotency_same_snapshot_is_delta_zero(self):
        temp, root, m0, reader, m1, m1_reader, m2 = self.setup_fixture(event_count=2)
        try:
            worker = M2Worker(reader, m1_reader, m2, FakeProposer(), enable_cross=False)
            first = worker.process_once()
            second = worker.process_once()
            self.assertEqual(first["accepted"], 1)
            self.assertEqual(second["accepted"], 0)
            self.assertGreaterEqual(second["duplicate"], 1)
            self.assertEqual(m2.verify()["formation_run_count"], 1)
        finally:
            temp.cleanup()

    def test_supersession_keeps_old_understanding(self):
        temp, root, m0, reader, m1, m1_reader, m2 = self.setup_fixture(event_count=2)
        try:
            snapshots = self.closed_snapshots(m1_reader, reader)
            episode_id = snapshots[0]["episode"]["episode_id"]
            event_id = snapshots[0]["evidence"][0]["event_id"]
            first = ValidatedProposal(
                "第一次理解：Alex在该 Episode 中表达了一个话题。",
                "EPISODE_INTERPRETATION",
                "UTTERANCE_AS_EVENT",
                (event_id,),
                (episode_id,),
                "ACTIVE",
            )
            second = ValidatedProposal(
                "后来理解：该 Episode 中Alex表达了另一个角度。",
                "EPISODE_INTERPRETATION",
                "UTTERANCE_AS_EVENT",
                (event_id,),
                (episode_id,),
                "ACTIVE",
            )
            m2.record_result("EPISODE", episode_id, "snapshot-v1", [first], "r1")
            m2.record_result("EPISODE", episode_id, "snapshot-v2", [second], "r2")
            all_rows = m2.by_episode(episode_id)
            self.assertEqual(len(all_rows), 2)
            self.assertEqual(sum(row["status"] == "SUPERSEDED" for row in all_rows), 1)
            self.assertEqual(sum(row["status"] == "ACTIVE" for row in all_rows), 1)
        finally:
            temp.cleanup()

    def test_failure_does_not_change_m0_or_m1(self):
        temp, root, m0, reader, m1, m1_reader, m2 = self.setup_fixture(event_count=2)
        try:
            m0_before = hashlib.sha256((root / "memory" / "evidence.sqlite").read_bytes()).hexdigest()
            m1_before = hashlib.sha256((root / "memory" / "episodes.sqlite").read_bytes()).hexdigest()
            result = M2Worker(reader, m1_reader, m2, FakeProposer("malformed"), enable_cross=False).process_once()
            self.assertEqual(result["failed"], 1)
            self.assertEqual(m0_before, hashlib.sha256((root / "memory" / "evidence.sqlite").read_bytes()).hexdigest())
            self.assertEqual(m1_before, hashlib.sha256((root / "memory" / "episodes.sqlite").read_bytes()).hexdigest())
            self.assertEqual(m2.verify()["understanding_count"], 0)
        finally:
            temp.cleanup()

    def test_rebuild_from_same_m1_is_semantically_stable(self):
        temp, root, m0, reader, m1, m1_reader, m2 = self.setup_fixture(event_count=3)
        try:
            M2Worker(reader, m1_reader, m2, FakeProposer(), enable_cross=False).process_once()
            rebuilt = M2Store(root / "memory" / "rebuilt.sqlite")
            M2Worker(reader, m1_reader, rebuilt, FakeProposer(), enable_cross=False).process_once()
            left = [(row["statement"], row["understanding_kind"], row["epistemic_basis"], row["status"]) for row in m2.list_recent(50)]
            right = [(row["statement"], row["understanding_kind"], row["epistemic_basis"], row["status"]) for row in rebuilt.list_recent(50)]
            self.assertEqual(left, right)
            self.assertTrue(m2.verify()["ok"])
            self.assertTrue(rebuilt.verify()["ok"])
        finally:
            temp.cleanup()

    def test_m2_does_not_enter_context_or_runtime(self):
        root = Path(__file__).resolve().parents[1]
        for name in ("context.py", "runtime.py", "server.py"):
            text = (root / "app" / name).read_text(encoding="utf-8").lower()
            self.assertNotIn("understanding", text)
            self.assertNotIn("m2", text)


if __name__ == "__main__":
    unittest.main()
