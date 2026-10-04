from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from telegram_adapter import (
    TelegramAdapter,
    TelegramState,
    TelegramTransportError,
    parse_private_text_update,
)
from app.participant import ParticipantResolver


def update(update_id: int = 1, message_id: int = 10, user_id: int = 42, text: str = "hello") -> dict:
    return {
        "update_id": update_id,
        "message": {
            "message_id": message_id,
            "from": {"id": user_id},
            "chat": {"id": user_id, "type": "private"},
            "text": text,
        },
    }


class FakeRuntime:
    def __init__(self):
        self.handle_calls = []
        self.delivery_calls = []

    def handle_turn(
        self,
        conversation_id,
        user_text,
        message_id=None,
        persist_raw=True,
        participant=None,
    ):
        turn_id = "turn-" + str(len(self.handle_calls) + 1)
        result = {
            "turn_id": turn_id,
            "trace_id": "trace-" + turn_id,
            "conversation_id": conversation_id,
            "input_message_id": message_id,
            "outbound_message_id": turn_id,
            "raw_content": "reply",
        }
        result["participant"] = participant
        self.handle_calls.append(result)
        return result

    def commit_turn(self, result, user_text):
        return None

    def record_delivery_success(self, result):
        self.delivery_calls.append(dict(result))
        return "inserted"


class FakeApi:
    def __init__(self, failure=None):
        self.failure = failure
        self.send_calls = []

    def send_message(self, chat_id, text):
        self.send_calls.append((chat_id, text))
        if self.failure is not None:
            raise self.failure
        return "99"


class TelegramAdapterTests(unittest.TestCase):
    def make_adapter(self, api=None, participant_resolver=None):
        temp = tempfile.TemporaryDirectory()
        state = TelegramState(Path(temp.name) / "state.sqlite")
        runtime = FakeRuntime()
        api = api or FakeApi()
        adapter = TelegramAdapter(
            runtime,
            api,
            state,
            "42",
            participant_resolver=participant_resolver,
        )
        return temp, adapter, runtime, api, state

    def test_private_allowlist_parser(self):
        self.assertIsNotNone(parse_private_text_update(update(), "42"))
        self.assertIsNone(parse_private_text_update(update(user_id=99), "42"))
        group = update()
        group["message"]["chat"]["type"] = "group"
        self.assertIsNone(parse_private_text_update(group, "42"))

    def test_participant_binding_uses_transport_id(self):
        temp = tempfile.TemporaryDirectory()
        try:
            path = Path(temp.name) / "participants.json"
            path.write_text(
                '{"version":1,"participants":[{"participant_id":"alex","display_name":"Alex","transport":"telegram","transport_user_id":"42"}]}',
                encoding="utf-8",
            )
            resolver = ParticipantResolver.load(path)
            temp2, adapter, runtime, api, state = self.make_adapter(
                participant_resolver=resolver
            )
            try:
                adapter.process_update(update(text="hello"))
                self.assertEqual(runtime.handle_calls[0]["participant"].participant_id, "alex")
                self.assertEqual(runtime.handle_calls[0]["participant"].display_name, "Alex")
            finally:
                temp2.cleanup()
        finally:
            temp.cleanup()

    def test_duplicate_update_generates_one_reply_and_one_delivery(self):
        temp, adapter, runtime, api, state = self.make_adapter()
        try:
            item = update()
            adapter.process_update(item)
            adapter.process_update(item)
            self.assertEqual(len(runtime.handle_calls), 1)
            self.assertEqual(len(api.send_calls), 1)
            self.assertEqual(len(runtime.delivery_calls), 1)
            self.assertEqual(state.rows_in_state("DELIVERED")[0]["update_id"], 1)
        finally:
            temp.cleanup()

    def test_same_message_different_update_is_deduplicated(self):
        temp, adapter, runtime, api, state = self.make_adapter()
        try:
            adapter.process_update(update(update_id=1, message_id=10))
            adapter.process_update(update(update_id=2, message_id=10))
            self.assertEqual(len(runtime.handle_calls), 1)
            self.assertEqual(len(api.send_calls), 1)
            self.assertEqual(state.count(), 1)
        finally:
            temp.cleanup()

    def test_delivery_failure_never_records_chiyo_evidence(self):
        temp, adapter, runtime, api, state = self.make_adapter(
            FakeApi(TelegramTransportError("network"))
        )
        try:
            adapter.process_update(update())
            self.assertEqual(len(runtime.handle_calls), 1)
            self.assertEqual(len(runtime.delivery_calls), 0)
            self.assertEqual(len(state.rows_in_state("DELIVERY_UNKNOWN")), 1)
        finally:
            temp.cleanup()

    def test_confirmed_delivery_can_be_recovered_without_resend(self):
        temp, adapter, runtime, api, state = self.make_adapter()
        try:
            original = runtime.record_delivery_success

            def fail_once(result):
                runtime.record_delivery_success = original
                raise RuntimeError("temporary evidence failure")

            runtime.record_delivery_success = fail_once
            item = update()
            adapter.process_update(item)
            self.assertEqual(len(api.send_calls), 1)
            self.assertEqual(len(state.rows_in_state("TELEGRAM_CONFIRMED")), 1)
            adapter.process_update(item)
            self.assertEqual(len(api.send_calls), 1)
            self.assertEqual(len(runtime.delivery_calls), 1)
            self.assertEqual(len(state.rows_in_state("DELIVERED")), 1)
        finally:
            temp.cleanup()


if __name__ == "__main__":
    unittest.main()
