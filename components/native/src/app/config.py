from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any
import json


@dataclass(frozen=True)
class NativeConfig:
    model: str
    provider: str
    base_url: str
    api_path: str
    api_key_env: str
    identity_path: Path
    participants_path: Path
    data_dir: Path
    trace_dir: Path
    host: str
    port: int
    timeout_seconds: float
    sampling: dict[str, Any]

    @classmethod
    def load(cls, path: str | Path) -> "NativeConfig":
        source = Path(path)
        data = json.loads(source.read_text(encoding="utf-8"))
        return cls(
            model=str(data["model"]),
            provider=str(data["provider"]),
            base_url=str(data["base_url"]).rstrip("/"),
            api_path=str(data.get("api_path", "/chat/completions")),
            api_key_env=str(data["api_key_env"]),
            identity_path=Path(data["identity_path"]).expanduser(),
            participants_path=Path(
                data.get("participants_path", "./config/native/participants.json")
            ),
            data_dir=Path(data["data_dir"]).expanduser(),
            trace_dir=Path(data["trace_dir"]).expanduser(),
            host=str(data.get("host", "127.0.0.1")),
            port=int(data.get("port", 18652)),
            timeout_seconds=float(data.get("timeout_seconds", 120)),
            sampling=dict(data.get("sampling") or {}),
        )

    @property
    def endpoint(self) -> str:
        return self.base_url + "/" + self.api_path.lstrip("/")

    @property
    def identity_text(self) -> str:
        text = self.identity_path.read_text(encoding="utf-8")
        if not text.strip():
            raise ValueError("identity is empty")
        return text.rstrip("\n")
