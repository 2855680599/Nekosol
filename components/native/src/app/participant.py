from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
from typing import Any


class ParticipantConfigurationError(ValueError):
    pass


@dataclass(frozen=True)
class Participant:
    participant_id: str
    display_name: str
    transport: str
    transport_user_id: str = field(repr=False)

    @property
    def context_text(self) -> str:
        return f"当前与你对话的人是{self.display_name}。"

    def trace_context(self) -> dict[str, str]:
        return {
            "participant_id": self.participant_id,
            "display_name": self.display_name,
            "transport": self.transport,
        }


class ParticipantResolver:
    _FORBIDDEN_FIELDS = {
        "relationship",
        "relationship_status",
        "affection",
        "trust",
        "intimacy",
        "importance",
        "priority",
        "lover",
        "friend",
        "owner",
        "master",
    }

    def __init__(self, participants: list[Participant]):
        self._participants = tuple(participants)
        keys = [(item.transport, item.transport_user_id) for item in participants]
        if len(keys) != len(set(keys)):
            raise ParticipantConfigurationError("duplicate transport participant mapping")

    @classmethod
    def load(cls, path: str | Path) -> "ParticipantResolver":
        source = Path(path)
        try:
            data: Any = json.loads(source.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ParticipantConfigurationError(
                "participant mapping cannot be loaded"
            ) from exc
        if not isinstance(data, dict) or not isinstance(data.get("participants"), list):
            raise ParticipantConfigurationError("participants must be a list")

        participants: list[Participant] = []
        for item in data["participants"]:
            if not isinstance(item, dict):
                raise ParticipantConfigurationError("participant entry must be an object")
            if set(item).intersection(cls._FORBIDDEN_FIELDS):
                raise ParticipantConfigurationError(
                    "relationship fields are not allowed in participant mapping"
                )
            required = ("participant_id", "display_name", "transport", "transport_user_id")
            if any(not isinstance(item.get(key), str) or not item[key].strip() for key in required):
                raise ParticipantConfigurationError("participant identity fields are required")
            transport_user_id = item["transport_user_id"].strip()
            if any(ch.isspace() for ch in transport_user_id):
                raise ParticipantConfigurationError("transport user id must be one token")
            display_name = item["display_name"].strip()
            if any(ord(ch) in (10, 13) for ch in display_name) or len(display_name) > 64:
                raise ParticipantConfigurationError("display name is invalid")
            participants.append(
                Participant(
                    participant_id=item["participant_id"].strip(),
                    display_name=display_name,
                    transport=item["transport"].strip(),
                    transport_user_id=transport_user_id,
                )
            )
        return cls(participants)

    def resolve(self, transport: str, transport_user_id: str | int) -> Participant | None:
        normalized = str(transport_user_id)
        for participant in self._participants:
            if participant.transport == transport and participant.transport_user_id == normalized:
                return participant
        return None
