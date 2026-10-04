from __future__ import annotations

from pathlib import Path
import json
import tempfile
import unittest

from app.participant import ParticipantConfigurationError, ParticipantResolver
from telegram_adapter import parse_private_text_update


class ParticipantResolverTests(unittest.TestCase):
    def make_mapping(self, temp: str, **entry_overrides):
        path = Path(temp) / "participants.json"
        entry = {
            "participant_id": "alex",
            "display_name": "Alex",
            "transport": "telegram",
            "transport_user_id": "42",
        }
        entry.update(entry_overrides)
        path.write_text(
            json.dumps({"version": 1, "participants": [entry]}, ensure_ascii=False),
            encoding="utf-8",
        )
        return path

    def test_allowlisted_id_resolves_to_rui(self):
        with tempfile.TemporaryDirectory() as temp:
            resolver = ParticipantResolver.load(self.make_mapping(temp))
            participant = resolver.resolve("telegram", "42")
            self.assertEqual(participant.participant_id, "alex")
            self.assertEqual(participant.display_name, "Alex")

    def test_unknown_id_does_not_resolve(self):
        with tempfile.TemporaryDirectory() as temp:
            resolver = ParticipantResolver.load(self.make_mapping(temp))
            self.assertIsNone(resolver.resolve("telegram", "99"))

    def test_telegram_display_or_username_is_not_identity_key(self):
        with tempfile.TemporaryDirectory() as temp:
            resolver = ParticipantResolver.load(
                self.make_mapping(temp, display_name="Alex的测试名")
            )
            update = {
                "update_id": 1,
                "message": {
                    "message_id": 10,
                    "from": {"id": 42, "username": "changed_username", "first_name": "changed"},
                    "chat": {"id": 42, "type": "private"},
                    "text": "hello",
                },
            }
            accepted = parse_private_text_update(update, "42")
            self.assertEqual(accepted.transport_user_id, "42")
            self.assertEqual(
                resolver.resolve("telegram", accepted.transport_user_id).participant_id,
                "alex",
            )

    def test_relationship_fields_are_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            path = self.make_mapping(temp, relationship="lover")
            with self.assertRaises(ParticipantConfigurationError):
                ParticipantResolver.load(path)


if __name__ == "__main__":
    unittest.main()
