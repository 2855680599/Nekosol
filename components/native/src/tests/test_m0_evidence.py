from __future__ import annotations

from pathlib import Path
import json
import sqlite3
import tempfile
import unittest

from app.evidence import (
    CHIYO_ORIGIN,
    CHIYO_ROLE,
    DELIVERED,
    EvidenceEvent,
    EvidenceStore,
    EvidenceWriter,
    RECEIVED,
    USER_ORIGIN,
    USER_ROLE,
)
from app.model import ModelResult, ProviderError
from app.runtime import NativeRuntime
from app.session import ConversationStore
from app.trace import TraceStore


class FakeBackend:
    def __init__(self, content="visible reply", failure=None):
        self.content = content
        self.failure = failure

    def complete(self, messages):
        if self.failure is not None:
            raise self.failure
        return ModelResult(
            content=self.content,
            usage={"prompt_tokens": 3, "completion_tokens": 2},
            finish_reason="stop",
            reasoning_content_present=False,
        )


class FakeConfig:
    model = "deepseek/deepseek-v4-flash"
    provider = "test-provider"
    endpoint = "http://127.0.0.1/unused"
    sampling = {"temperature": 1.0, "top_p": 0.90}
    identity_text = "你是该个体。"


def event(origin=USER_ORIGIN, ref="message-1", content="123"):
    if origin == USER_ORIGIN:
        status, role, speaker = RECEIVED, USER_ROLE, "user"
    else:
        status, role, speaker = DELIVERED, CHIYO_ROLE, "chiyo"
    return EvidenceEvent(
        event_id="0199a000-0000-7000-8000-000000000001",
        occurred_at="2026-09-06T15:00:00+00:00",
        memory_owner="chiyo",
        source_origin=origin,
        delivery_status=status,
        epistemic_role=role,
        speaker=speaker,
        content=content,
        conversation_id="m0-test",
        turn_id="turn-1",
        source_refs=[{"kind": "test-source", "id": ref}],
        created_at="2026-09-06T15:00:01+00:00",
    )


class FailingStore:
    def append(self, event):
        raise OSError("simulated evidence db outage")


class M0EvidenceTests(unittest.TestCase):
    def test_schema_append_idempotency_and_append_only(self):
        with tempfile.TemporaryDirectory() as temp:
            store = EvidenceStore(Path(temp) / "memory" / "evidence.sqlite")
            first = event()
            self.assertEqual(store.append(first).status, "inserted")
            self.assertEqual(store.append(first).status, "duplicate")
            self.assertEqual(len(store.list_events()), 1)
            connection = sqlite3.connect(store.path)
            with self.assertRaises(sqlite3.DatabaseError):
                connection.execute(
                    "UPDATE evidence_events SET content='changed' WHERE event_id=?",
                    (first.event_id,),
                )
            with self.assertRaises(sqlite3.DatabaseError):
                connection.execute(
                    "DELETE FROM evidence_events WHERE event_id=?", (first.event_id,)
                )
            connection.close()
            self.assertTrue(store.verify()["ok"])

    def test_invalid_internal_origin_is_rejected(self):
        with self.assertRaises(ValueError):
            EvidenceEvent(
                event_id="internal",
                occurred_at="2026-09-06T15:00:00+00:00",
                memory_owner="chiyo",
                source_origin="SYSTEM_PROMPT",
                delivery_status=RECEIVED,
                epistemic_role=USER_ROLE,
                speaker="user",
                content="internal",
                conversation_id="m0-test",
                turn_id="turn-1",
                source_refs=[{"kind": "system", "id": "x"}],
                created_at="2026-09-06T15:00:01+00:00",
            )

    def test_runtime_provider_failure_keeps_user_evidence_only(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            store = ConversationStore(root / "data")
            traces = TraceStore(root / "traces")
            writer = EvidenceWriter(root / "data")
            runtime = NativeRuntime(
                FakeConfig,
                store,
                FakeBackend(failure=ProviderError("timeout")),
                traces,
                writer,
            )
            with self.assertRaises(ProviderError):
                runtime.handle_turn("m0-failure", "123", "input-1")
            rows = writer.store.list_events()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["source_origin"], USER_ORIGIN)
            self.assertEqual(store.load("m0-failure"), [])

    def test_success_and_delivery_retry_create_one_chiyo_event(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            writer = EvidenceWriter(root / "data")
            runtime = NativeRuntime(
                FakeConfig,
                ConversationStore(root / "data"),
                FakeBackend("visible reply"),
                TraceStore(root / "traces"),
                writer,
            )
            result = runtime.handle_turn("m0-success", "123", "input-2")
            self.assertEqual(runtime.record_delivery_success(result), "inserted")
            self.assertEqual(runtime.record_delivery_success(result), "duplicate")
            rows = writer.store.list_events()
            self.assertEqual(len(rows), 2)
            self.assertEqual(
                len([row for row in rows if row["source_origin"] == CHIYO_ORIGIN]), 1
            )
            self.assertTrue(all(row["delivery_status"] in {RECEIVED, DELIVERED} for row in rows))

    def test_delivery_failure_does_not_create_chiyo_event(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            writer = EvidenceWriter(root / "data")
            runtime = NativeRuntime(
                FakeConfig,
                ConversationStore(root / "data"),
                FakeBackend("generated but not delivered"),
                TraceStore(root / "traces"),
                writer,
            )
            runtime.handle_turn("m0-not-delivered", "123", "input-3")
            rows = writer.store.list_events()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["source_origin"], USER_ORIGIN)

    def test_evidence_failure_is_queued_without_changing_reply(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            writer = EvidenceWriter(root / "data")
            writer.store = FailingStore()
            runtime = NativeRuntime(
                FakeConfig,
                ConversationStore(root / "data"),
                FakeBackend("reply is unchanged"),
                TraceStore(root / "traces"),
                writer,
            )
            result = runtime.handle_turn("m0-db-failure", "123", "input-4")
            self.assertEqual(result["raw_content"], "reply is unchanged")
            runtime.record_delivery_success(result)
            self.assertEqual(writer.spool.pending_count(), 2)
            writer.store = EvidenceStore(root / "data" / "memory" / "evidence.sqlite")
            retry = writer.retry_pending()
            self.assertEqual(retry["inserted"], 2)
            self.assertEqual(writer.spool.pending_count(), 0)
            self.assertTrue(writer.store.verify()["ok"])

    def test_online_backup_is_readable(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            store = EvidenceStore(root / "memory" / "evidence.sqlite")
            store.append(event())
            destination = root / "backup" / "evidence.sqlite"
            store.backup(destination)
            backup = EvidenceStore(destination)
            self.assertEqual(len(backup.list_events()), 1)


if __name__ == "__main__":
    unittest.main()
