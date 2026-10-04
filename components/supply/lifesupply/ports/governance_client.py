"""Fail-closed local Governance socket client used by Life Supply writers."""
from __future__ import annotations

import json
import socket
from dataclasses import dataclass

@dataclass(frozen=True)
class PortResult:
    status: str
    data: dict

class GovernanceSocketPort:
    def __init__(self, socket_path: str, *, timeout: float = 2.0, actor_uid: int | None = None):
        self.socket_path = socket_path
        self.timeout = timeout
        self.actor_uid = actor_uid

    def call(self, request: dict) -> PortResult:
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.settimeout(self.timeout)
                client.connect(self.socket_path)
                request = dict(request)
                if request.get("op") == "reserve_use":
                    payload = dict(request.get("request") or {})
                    if self.actor_uid is not None:
                        payload["actor_uid"] = self.actor_uid
                    request["request"] = payload
                client.sendall(json.dumps(request, ensure_ascii=False, separators=(",", ":")).encode() + b"\n")
                stream = client.makefile("rb")
                line = stream.readline(1_000_001)
                if not line or len(line) > 1_000_000:
                    return PortResult("UNKNOWN", {"detail": "invalid Governance response"})
                result = json.loads(line.decode("utf-8"))
                if not isinstance(result, dict) or not isinstance(result.get("status"), str):
                    return PortResult("UNKNOWN", {"detail": "untyped Governance response"})
                return PortResult(result["status"], result)
        except (OSError, ValueError, UnicodeError, TimeoutError):
            return PortResult("UNKNOWN", {"detail": "Governance unavailable"})
