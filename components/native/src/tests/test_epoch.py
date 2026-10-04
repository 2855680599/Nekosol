from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from app.epoch import ContextEpochStore, create_epoch_metadata, write_epoch_metadata
from app.model import ModelResult
from app.participant import Participant
from app.runtime import NativeRuntime
from app.session import ConversationStore
from app.trace import TraceStore


class CaptureBackend:
    def __init__(self):
        self.calls = []

    def complete(self, messages):
        self.calls.append(messages)
        return ModelResult(
            content="新该个体回复",
            usage={},
            finish_reason="stop",
            reasoning_content_present=False,
        )


class Config:
    model = "deepseek/deepseek-v4-flash"
    provider = "test-provider"
    endpoint = "http://127.0.0.1/unused"
    sampling = {"temperature": 1.0, "top_p": 0.9}
    identity_text = "你是该个体。\n你是持续存在的该个体。"


class ContextEpochTests(unittest.TestCase):
    def test_old_records_are_preserved_but_excluded(self):
        with tempfile.TemporaryDirectory() as temp:
            data_dir = Path(temp) / "data"
            conversation_dir = data_dir / "conversations"
            conversation_dir.mkdir(parents=True)
            old = [
                {"turn_id": "t1", "timestamp": "2026-01-01T00:00:00Z", "speaker": "user", "raw_content": "旧问题", "model": None},
                {"turn_id": "t1", "timestamp": "2026-01-01T00:00:00Z", "speaker": "assistant", "raw_content": "旧回答", "model": "test"},
            ]
            path = conversation_dir / "conv.json"
            path.write_text(json.dumps(old, ensure_ascii=False), encoding="utf-8")
            metadata = create_epoch_metadata(data_dir, "baseline-test", "2026-01-02T00:00:00Z")
            write_epoch_metadata(data_dir, metadata)
            epoch = ContextEpochStore(data_dir)
            selection = epoch.select("conv", old)
            self.assertEqual(selection.visible_history, [])
            self.assertEqual(selection.excluded_count, 2)
            new = old + [
                {"turn_id": "t2", "timestamp": "2026-01-02T00:00:01Z", "speaker": "user", "raw_content": "新问题", "model": None},
                {"turn_id": "t2", "timestamp": "2026-01-02T00:00:01Z", "speaker": "assistant", "raw_content": "新回答", "model": "test"},
            ]
            selection = epoch.select("conv", new)
            self.assertEqual([item["raw_content"] for item in selection.visible_history], ["新问题", "新回答"])
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), old)

    def test_runtime_uses_only_current_epoch_and_keeps_two_systems(self):
        with tempfile.TemporaryDirectory() as temp:
            data_dir = Path(temp) / "data"
            store = ConversationStore(data_dir)
            store.persist_turn("conv", "old", "2026-01-01T00:00:00Z", "旧问题", "旧回答", "test")
            metadata = create_epoch_metadata(data_dir, "baseline-test", "2026-01-02T00:00:00Z")
            write_epoch_metadata(data_dir, metadata)
            backend = CaptureBackend()
            runtime = NativeRuntime(
                Config,
                store,
                backend,
                TraceStore(Path(temp) / "traces"),
                context_epoch=ContextEpochStore(data_dir),
            )
            participant = Participant("alex", "Alex", "telegram", "42")
            first = runtime.handle_turn("conv", "第一条", participant=participant)
            self.assertEqual(
                [item["role"] for item in backend.calls[0]],
                ["system", "system", "user"],
            )
            self.assertNotIn("旧回答", json.dumps(backend.calls[0], ensure_ascii=False))
            self.assertEqual(first["raw_content"], "新该个体回复")
            second = runtime.handle_turn("conv", "第二条", participant=participant)
            self.assertEqual(
                [item["role"] for item in backend.calls[1]],
                ["system", "system", "user", "assistant", "user"],
            )
            self.assertIn("新该个体回复", json.dumps(backend.calls[1], ensure_ascii=False))
            self.assertNotIn("旧回答", json.dumps(backend.calls[1], ensure_ascii=False))
            self.assertEqual(len(store.load("conv")), 6)


if __name__ == "__main__":
    unittest.main()
