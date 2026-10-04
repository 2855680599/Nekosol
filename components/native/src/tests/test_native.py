from __future__ import annotations

from pathlib import Path
import re
import tempfile
import unittest

from app.context import assemble_context
from app.model import ModelResult, ProviderError
from app.participant import Participant
from app.runtime import NativeRuntime
from app.session import ConversationStore
from app.trace import TraceStore


class FakeBackend:
    def __init__(self, content="raw-provider-content", failure=None):
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
    identity_text = "你是千代。"


class NativeV0Tests(unittest.TestCase):
    def test_context_has_only_identity_history_current_turn(self):
        assembly = assemble_context(
            "你是千代。",
            [{"speaker": "user", "raw_content": "旧问题"}],
            "当前问题",
        )
        self.assertEqual(
            assembly.messages,
            [
                {"role": "system", "content": "你是千代。"},
                {"role": "user", "content": "旧问题"},
                {"role": "user", "content": "当前问题"},
            ],
        )
        self.assertEqual(
            [item["source"] for item in assembly.sources],
            ["identity", "conversation", "current_turn"],
        )

    def test_context_has_participant_without_relationship(self):
        participant = Participant("alex", "Alex", "telegram", "42")
        assembly = assemble_context(
            "你是千代，18岁。\n你是持续存在的千代。",
            [],
            "我是谁？",
            participant,
        )
        self.assertEqual(
            assembly.messages,
            [
                {"role": "system", "content": "你是千代，18岁。\n你是持续存在的千代。"},
                {"role": "system", "content": "当前与你对话的人是Alex。"},
                {"role": "user", "content": "我是谁？"},
            ],
        )
        authority_text = "\n".join(item["content"] for item in assembly.messages[:2])
        for forbidden in ("恋人", "朋友", "陌生人", "情侣", "partner", "lover"):
            self.assertNotIn(forbidden, authority_text)
        self.assertEqual(
            [item["source"] for item in assembly.sources],
            ["identity", "participant_identity", "current_turn"],
        )

    def test_raw_output_and_restart_roundtrip(self):
        with tempfile.TemporaryDirectory() as temp:
            store = ConversationStore(Path(temp) / "data")
            traces = TraceStore(Path(temp) / "traces")
            runtime = NativeRuntime(
                FakeConfig,
                store,
                FakeBackend("raw\noutput"),
                traces,
            )
            result = runtime.handle_turn("restart-test", "你好")
            self.assertEqual(result["raw_content"], "raw\noutput")
            restarted_store = ConversationStore(Path(temp) / "data")
            records = restarted_store.load("restart-test")
            self.assertEqual(records[-1]["speaker"], "assistant")
            self.assertEqual(records[-1]["raw_content"], "raw\noutput")
            trace = traces.find(result["trace_id"])
            self.assertEqual(trace["raw_output"], "raw\noutput")
            self.assertEqual(trace["status"], "success")

    def test_provider_failure_does_not_write_transcript(self):
        with tempfile.TemporaryDirectory() as temp:
            store = ConversationStore(Path(temp) / "data")
            traces = TraceStore(Path(temp) / "traces")
            runtime = NativeRuntime(
                FakeConfig,
                store,
                FakeBackend(failure=ProviderError("simulated timeout")),
                traces,
            )
            with self.assertRaises(ProviderError):
                runtime.handle_turn("failed-test", "你好")
            self.assertEqual(store.load("failed-test"), [])
            trace_files = list((Path(temp) / "traces").glob("*.jsonl"))
            self.assertEqual(len(trace_files), 1)
            self.assertIn("provider_error", trace_files[0].read_text(encoding="utf-8"))

    def test_native_source_has_no_hermes_import(self):
        source_root = Path(__file__).parents[1]
        pattern = re.compile(r"^\s*(?:from|import)\s+hermes\b", re.MULTILINE)
        for path in source_root.rglob("*.py"):
            self.assertIsNone(pattern.search(path.read_text(encoding="utf-8")))


if __name__ == "__main__":
    unittest.main()
