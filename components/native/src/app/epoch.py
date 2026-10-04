from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
from typing import Any


class ContextEpochError(RuntimeError):
    pass


def _sha256_json(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _read_conversation(path: Path) -> list[dict[str, Any]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ContextEpochError("conversation boundary cannot be read") from exc
    if not isinstance(data, list) or any(not isinstance(item, dict) for item in data):
        raise ContextEpochError("conversation boundary is not a record list")
    return data


def create_epoch_metadata(
    data_dir: str | Path,
    epoch_id: str,
    created_at: str | None = None,
) -> dict[str, Any]:
    """Create a non-destructive cursor at the end of every existing raw file."""
    if not isinstance(epoch_id, str) or not epoch_id.strip():
        raise ContextEpochError("epoch_id is required")
    root = Path(data_dir)
    conversation_dir = root / "conversations"
    boundaries: dict[str, dict[str, Any]] = {}
    for path in sorted(conversation_dir.glob("*.json")):
        conversation_id = path.stem
        records = _read_conversation(path)
        last = records[-1] if records else {}
        boundaries[conversation_id] = {
            "record_count": len(records),
            "last_turn_id": last.get("turn_id"),
            "last_timestamp": last.get("timestamp"),
            "prefix_sha256": _sha256_json(records),
            "boundary_id": epoch_id + ":" + conversation_id,
        }
    return {
        "version": 1,
        "epoch_id": epoch_id,
        "created_at": created_at or _utc_now(),
        "boundary_kind": "raw_conversation_record_cursor",
        "policy": "records after each cursor are eligible for visible context",
        "boundaries": boundaries,
    }


def write_epoch_metadata(data_dir: str | Path, metadata: dict[str, Any]) -> Path:
    root = Path(data_dir)
    root.mkdir(parents=True, exist_ok=True)
    target = root / "context_epoch.json"
    temporary = target.with_name("." + target.name + ".tmp")
    temporary.write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.chmod(temporary, 0o640)
    os.replace(temporary, target)
    os.chmod(target, 0o640)
    return target


@dataclass(frozen=True)
class EpochSelection:
    epoch_id: str | None
    boundary_id: str | None
    raw_count: int
    visible_history: list[dict[str, Any]]
    excluded_count: int
    status: str

    def trace_context(self) -> dict[str, Any]:
        return {
            "epoch_id": self.epoch_id,
            "boundary_id": self.boundary_id,
            "raw_history_count": self.raw_count,
            "visible_history_count": len(self.visible_history),
            "excluded_history_count": self.excluded_count,
            "status": self.status,
        }


class ContextEpochStore:
    """Read-only runtime visibility policy; it never edits raw conversation."""

    def __init__(self, data_dir: str | Path):
        self.path = Path(data_dir) / "context_epoch.json"

    def _load(self) -> dict[str, Any] | None:
        if not self.path.exists():
            return None
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ContextEpochError("context epoch metadata cannot be read") from exc
        if not isinstance(data, dict) or not isinstance(data.get("boundaries"), dict):
            raise ContextEpochError("context epoch metadata is invalid")
        if not isinstance(data.get("epoch_id"), str) or not data["epoch_id"].strip():
            raise ContextEpochError("context epoch id is invalid")
        return data

    def select(
        self,
        conversation_id: str,
        history: list[dict[str, Any]],
    ) -> EpochSelection:
        metadata = self._load()
        if metadata is None:
            return EpochSelection(
                epoch_id=None,
                boundary_id=None,
                raw_count=len(history),
                visible_history=history,
                excluded_count=0,
                status="no_epoch_metadata",
            )

        epoch_id = str(metadata["epoch_id"])
        boundary = metadata["boundaries"].get(conversation_id)
        if boundary is None:
            return EpochSelection(
                epoch_id=epoch_id,
                boundary_id=None,
                raw_count=len(history),
                visible_history=history,
                excluded_count=0,
                status="post_epoch_conversation",
            )
        if not isinstance(boundary, dict):
            raise ContextEpochError("conversation epoch boundary is invalid")
        try:
            record_count = int(boundary["record_count"])
            prefix_sha256 = str(boundary["prefix_sha256"])
            boundary_id = str(boundary["boundary_id"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ContextEpochError("conversation epoch cursor is invalid") from exc
        if record_count < 0 or record_count > len(history):
            return EpochSelection(
                epoch_id=epoch_id,
                boundary_id=boundary_id,
                raw_count=len(history),
                visible_history=[],
                excluded_count=len(history),
                status="boundary_ahead_or_incomplete",
            )
        if _sha256_json(history[:record_count]) != prefix_sha256:
            return EpochSelection(
                epoch_id=epoch_id,
                boundary_id=boundary_id,
                raw_count=len(history),
                visible_history=[],
                excluded_count=len(history),
                status="prefix_mismatch_fail_closed",
            )
        visible = history[record_count:]
        return EpochSelection(
            epoch_id=epoch_id,
            boundary_id=boundary_id,
            raw_count=len(history),
            visible_history=visible,
            excluded_count=record_count,
            status="current_epoch_only",
        )
