from __future__ import annotations

from pathlib import Path
from typing import Any
import fcntl
import json
import os
import re
import tempfile


_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


class PersistenceError(RuntimeError):
    pass


class ConversationStore:
    """Raw conversation storage owned only by Native Runtime."""

    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.conversations_dir = self.root / "conversations"
        self.conversations_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.root, 0o700)
        os.chmod(self.conversations_dir, 0o700)

    def _validate_id(self, conversation_id: str) -> str:
        if not isinstance(conversation_id, str) or not _ID_RE.fullmatch(conversation_id):
            raise ValueError("invalid conversation_id")
        return conversation_id

    def _path(self, conversation_id: str) -> Path:
        return self.conversations_dir / (self._validate_id(conversation_id) + ".json")

    def _lock_path(self, conversation_id: str) -> Path:
        return self.conversations_dir / (self._validate_id(conversation_id) + ".lock")

    def load(self, conversation_id: str) -> list[dict[str, Any]]:
        path = self._path(conversation_id)
        if not path.exists():
            return []
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, list):
            raise PersistenceError("conversation file is not a list")
        return data

    def persist_turn(
        self,
        conversation_id: str,
        turn_id: str,
        timestamp: str,
        user_text: str,
        assistant_text: str,
        model: str,
    ) -> None:
        self._validate_id(conversation_id)
        if not isinstance(user_text, str) or not isinstance(assistant_text, str):
            raise PersistenceError("turn content must be text")
        lock_path = self._lock_path(conversation_id)
        target = self._path(conversation_id)
        try:
            with lock_path.open("a+", encoding="utf-8") as lock:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
                current = self.load(conversation_id)
                records = current + [
                    {
                        "turn_id": turn_id,
                        "conversation_id": conversation_id,
                        "timestamp": timestamp,
                        "speaker": "user",
                        "raw_content": user_text,
                        "model": None,
                    },
                    {
                        "turn_id": turn_id,
                        "conversation_id": conversation_id,
                        "timestamp": timestamp,
                        "speaker": "assistant",
                        "raw_content": assistant_text,
                        "model": model,
                    },
                ]
                fd, temp_name = tempfile.mkstemp(
                    prefix="." + target.name + ".",
                    dir=str(target.parent),
                    text=True,
                )
                try:
                    with os.fdopen(fd, "w", encoding="utf-8", newline="") as temp:
                        json.dump(records, temp, ensure_ascii=False, indent=2)
                        temp.write("\n")
                        temp.flush()
                        os.fsync(temp.fileno())
                    os.chmod(temp_name, 0o600)
                    os.replace(temp_name, target)
                    dir_fd = os.open(target.parent, os.O_DIRECTORY)
                    try:
                        os.fsync(dir_fd)
                    finally:
                        os.close(dir_fd)
                finally:
                    if os.path.exists(temp_name):
                        os.unlink(temp_name)
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        except Exception as exc:
            if isinstance(exc, PersistenceError):
                raise
            raise PersistenceError(str(exc)) from exc
