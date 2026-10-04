from __future__ import annotations

from dataclasses import dataclass
from typing import Any
import json
import os
import urllib.error
import urllib.request


class ProviderError(RuntimeError):
    pass


@dataclass(frozen=True)
class ModelResult:
    content: str
    usage: dict[str, Any]
    finish_reason: Any
    reasoning_content_present: bool


class DirectProvider:
    """Direct OpenAI-compatible HTTP client; no Hermes imports or calls."""

    def __init__(self, config, *, api_key=None):
        self.config = config
        self.api_key = os.environ.get(config.api_key_env, "") if api_key is None else api_key
        if not self.api_key:
            raise ProviderError("provider credential is missing")

    def complete(self, messages: list[dict[str, str]]) -> ModelResult:
        payload: dict[str, Any] = {
            "model": self.config.model,
            "messages": messages,
            "stream": False,
        }
        for key in (
            "temperature",
            "top_p",
            "frequency_penalty",
            "presence_penalty",
        ):
            if key in self.config.sampling:
                payload[key] = self.config.sampling[key]
        if self.config.sampling.get("reasoning_effort") is not None:
            payload["reasoning_effort"] = self.config.sampling["reasoning_effort"]
        if self.config.sampling.get('max_tokens') is not None:
            payload['max_tokens'] = int(self.config.sampling['max_tokens'])

        request = urllib.request.Request(
            self.config.endpoint,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": "Bearer " + self.api_key,
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "ChiyoNative/0.1",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(
                request, timeout=self.config.timeout_seconds
            ) as response:
                body = response.read()
        except urllib.error.HTTPError as exc:
            exc.close()
            raise ProviderError("provider HTTP " + str(exc.code)) from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise ProviderError("provider transport failure") from None

        try:
            decoded = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ProviderError("provider returned invalid JSON") from None
        # Some OpenAI-compatible gateways (e.g. api.cline.bot) wrap the standard
        # chat-completion body one level down:
        #   {"data": {"choices": [...], "usage": {...}}, "success": true}
        # Accept both shapes; the standard shape is untouched.
        if isinstance(decoded, dict) and not isinstance(decoded.get("choices"), list):
            _inner = decoded.get("data")
            if isinstance(_inner, dict) and isinstance(_inner.get("choices"), list):
                decoded = _inner
        try:
            choice = decoded["choices"][0]
            message = choice["message"]
            content = message["content"]
        except (KeyError, IndexError, TypeError):
            raise ProviderError("provider response has no chat content") from None
        if not isinstance(content, str):
            raise ProviderError("provider content is not text")
        return ModelResult(
            content=content,
            usage=dict(decoded.get("usage") or {}),
            finish_reason=choice.get("finish_reason"),
            reasoning_content_present=bool(message.get("reasoning_content")),
        )
