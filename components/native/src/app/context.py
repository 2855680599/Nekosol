from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .participant import Participant


@dataclass(frozen=True)
class ContextAssembly:
    messages: list[dict[str, str]]
    sources: list[dict[str, Any]]


def assemble_context(
    identity_text: str,
    history: list[dict[str, Any]],
    current_user_text: str,
    participant: Participant | None = None,
) -> ContextAssembly:
    if not identity_text.strip():
        raise ValueError("identity is empty")
    if not current_user_text.strip():
        raise ValueError("current user text is empty")

    messages: list[dict[str, str]] = [
        {"role": "system", "content": identity_text},
    ]
    sources: list[dict[str, Any]] = [
        {"source": "identity", "message_indices": [0], "item_count": 1},
    ]

    if participant is not None:
        participant_index = len(messages)
        messages.append({"role": "system", "content": participant.context_text})
        sources.append(
            {
                "source": "participant_identity",
                "message_indices": [participant_index],
                "item_count": 1,
            }
        )

    history_indices: list[int] = []
    for item in history:
        speaker = str(item.get("speaker", ""))
        content = item.get("raw_content")
        if speaker not in {"user", "assistant"}:
            raise ValueError("conversation contains an invalid speaker")
        if not isinstance(content, str):
            raise ValueError("conversation contains non-text content")
        history_indices.append(len(messages))
        messages.append({"role": speaker, "content": content})
    if history_indices:
        sources.append(
            {
                "source": "conversation",
                "message_indices": history_indices,
                "item_count": len(history_indices),
            }
        )

    current_index = len(messages)
    messages.append({"role": "user", "content": current_user_text})
    sources.append(
        {"source": "current_turn", "message_indices": [current_index], "item_count": 1}
    )
    return ContextAssembly(messages=messages, sources=sources)
