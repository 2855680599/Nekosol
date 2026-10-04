from __future__ import annotations

from pathlib import Path
from typing import Any
import json
import os
import time


class TraceStore:
    """Append-only local trace store for reconstruction of model input."""

    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        os.chmod(self.root, 0o700)

    def write(self, record: dict[str, Any]) -> None:
        day = time.strftime("%Y-%m-%d", time.gmtime())
        path = self.root / (day + ".jsonl")
        with path.open("a", encoding="utf-8") as stream:
            json.dump(record, stream, ensure_ascii=False, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(path, 0o600)

    def find(self, trace_id: str) -> dict[str, Any] | None:
        for path in sorted(self.root.glob("*.jsonl")):
            for line in path.read_text(encoding="utf-8").splitlines():
                if not line:
                    continue
                record = json.loads(line)
                if record.get("trace_id") == trace_id:
                    return record
        return None
