"""LPC0B section 10/11: provider-agnostic Agency model client.

Authority: LPC0B order sections 9 (reuse the existing paid gateway), 10 (abstract the
transport), 11 (cognition gets its own config but reuses transport/auth/endpoint/error
conventions) and 46 (usage must be attributable to a purpose).

What this module is
-------------------
The only place in the Agency stack that speaks HTTP.  `ProductionAgencyCognitionAdapter`
owns the AG-1 contract mapping; this owns request/timeout/usage/provider-error/response
extraction.  There is deliberately no `if provider == "cline"` anywhere in AG-1.

Credentials
-----------
Read only from the existing official surfaces: `<HERMES_HOME>/config.yaml`
(`model.provider`, `providers.<name>.base_url`, `providers.<name>.key_env`) and the
matching key from the process environment or `<HERMES_HOME>/.env`.  No new secret file is
created, no key is ever written into a log or an evidence artifact.

Recursion
---------
The endpoint is the LLM relay (`api.example.invalid`), not the Hermes agent socket, and the call
is raw HTTP with no hermes plugin involvement, so a cognition call cannot re-enter
`pre_llm_call` (order section 26).
"""
from __future__ import annotations

import json
import os
import re
import socket
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Optional

#: Failure classes.  Order section 16 requires every one of these to stay distinguishable
#: from a model choosing NO_ACTION.
TRANSPORT_TIMEOUT = "TRANSPORT_TIMEOUT"
TRANSPORT_CONNECTION = "TRANSPORT_CONNECTION"
PROVIDER_RATE_LIMITED = "PROVIDER_RATE_LIMITED"
PROVIDER_SERVER_ERROR = "PROVIDER_SERVER_ERROR"
PROVIDER_MALFORMED_RESPONSE = "PROVIDER_MALFORMED_RESPONSE"
PROVIDER_AUTH_ERROR = "PROVIDER_AUTH_ERROR"
PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"
CONFIG_MISSING = "CONFIG_MISSING"

PURPOSE_AGENCY_COGNITION = "agency_cognition"
PURPOSE_CHAT = "chat"


class AgencyModelClientError(RuntimeError):
    """Transport-level failure.  Never represents a model choice."""

    def __init__(self, failure_class: str, message: str, *, status: Optional[int] = None) -> None:
        super().__init__(f"{failure_class}: {message}")
        self.failure_class = failure_class
        self.status = status


@dataclass
class ModelCallResult:
    """One provider round trip.  `text` is the model's raw text; nothing is interpreted here."""

    purpose: str
    provider: str
    model: str
    text: str
    latency_ms: int
    status: int = 0
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    total_tokens: Optional[int] = None
    reported_cost_usd: Optional[float] = None
    usage_source: str = "provider_reported"
    raw_usage: Mapping[str, Any] = field(default_factory=dict)

    def to_record(self) -> dict[str, Any]:
        return {
            "purpose": self.purpose, "provider": self.provider, "model": self.model,
            "latency_ms": self.latency_ms, "status": self.status,
            "input_tokens": self.input_tokens, "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens, "usage_source": self.usage_source,
            "reported_cost_usd": self.reported_cost_usd,
        }


# ---------------------------------------------------------------------------------
# existing config surfaces
# ---------------------------------------------------------------------------------

def _read_env_file(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            out[key.strip()] = value.strip().strip('"').strip("'")
    except OSError:
        pass
    return out


def load_provider_config(*, hermes_home: Optional[str] = None,
                         provider_override: Optional[str] = None,
                         model_override: Optional[str] = None) -> dict[str, Any]:
    """Resolve provider/model/base_url/key from the EXISTING gateway configuration."""
    home = Path(hermes_home or os.environ.get("HERMES_HOME") or "./data/persona")
    config_path = home / "config.yaml"
    text = ""
    try:
        text = config_path.read_text(encoding="utf-8")
    except OSError:
        text = ""

    def _scalar(pattern: str) -> Optional[str]:
        match = re.search(pattern, text, re.MULTILINE)
        return match.group(1).strip() if match else None

    provider = provider_override or _scalar(r"^\s*provider:\s*([A-Za-z0-9_.-]+)\s*$") or "cline"
    model = model_override or _scalar(r"^\s*default:\s*([A-Za-z0-9_.:-]+)\s*$")

    block = re.search(rf"^\s{{2}}{re.escape(provider)}:\s*$(.*?)(?=^\s{{2}}\S|\Z)", text,
                      re.MULTILINE | re.DOTALL)
    base_url = None
    key_env = None
    if block:
        body = block.group(1)
        m = re.search(r"^\s*base_url:\s*(\S+)\s*$", body, re.MULTILINE)
        base_url = m.group(1).strip() if m else None
        m = re.search(r"^\s*key_env:\s*(\S+)\s*$", body, re.MULTILINE)
        key_env = m.group(1).strip() if m else None

    env_file = _read_env_file(home / ".env")
    api_key = None
    if key_env:
        api_key = os.environ.get(key_env) or env_file.get(key_env)
    return {
        "hermes_home": str(home), "provider": provider, "model": model,
        "base_url": base_url, "key_env": key_env,
        "api_key_present": bool(api_key), "api_key": api_key,
        "config_path": str(config_path),
    }


# ---------------------------------------------------------------------------------
# client
# ---------------------------------------------------------------------------------


class AgencyModelClient:
    """Bounded, provider-agnostic chat-completions transport for Agency cognition."""

    def __init__(
        self,
        *,
        config: Optional[Mapping[str, Any]] = None,
        transport: Any = None,
        timeout_s: float = 8.0,
        max_output_tokens: int = 512,
        temperature: float = 0.0,
        purpose: str = PURPOSE_AGENCY_COGNITION,
        request_json_object: bool = True,
    ) -> None:
        cfg = dict(config or load_provider_config())
        self.provider = str(cfg.get("provider") or "cline")
        self.model = str(cfg.get("model") or "")
        self.base_url = (cfg.get("base_url") or "").rstrip("/")
        self._api_key = cfg.get("api_key")
        self.key_env = cfg.get("key_env")
        self.timeout_s = float(timeout_s)
        self.max_output_tokens = int(max_output_tokens)
        self.temperature = float(temperature)
        self.purpose = purpose
        self.request_json_object = bool(request_json_object)
        # LPC0B-R1: when a host transport is injected it IS the provider path.
        self._transport = transport
        self.transport_backend = str(getattr(transport, "backend", "raw_http_offline_only")) if transport is not None else "raw_http_offline_only"
        if transport is not None:
            self.provider = str(getattr(transport, "provider", "host") or "host")
            self.model = str(getattr(transport, "model", "host") or "host")
        self.call_count = 0

    @property
    def configured(self) -> bool:
        if self._transport is not None:
            return True
        return bool(self.base_url and self._api_key and self.model)

    def describe(self) -> dict[str, Any]:
        return {"provider": self.provider, "model": self.model, "base_url": self.base_url,
                "key_env": self.key_env, "api_key_present": bool(self._api_key),
                "timeout_s": self.timeout_s, "max_output_tokens": self.max_output_tokens,
                "purpose": self.purpose, "configured": self.configured,
                "transport_backend": self.transport_backend,
                "host_transport": (self._transport.describe() if self._transport is not None
                                   and hasattr(self._transport, "describe") else None)}

    def complete(self, *, system_prompt: str, user_prompt: str,
                 timeout_s: Optional[float] = None,
                 max_output_tokens: Optional[int] = None) -> ModelCallResult:
        if self._transport is not None:
            # Section 3A/52: the production path delegates to the host's own transport.
            return self._transport.complete(
                system_prompt=system_prompt, user_prompt=user_prompt,
                timeout_s=timeout_s or self.timeout_s,
                max_output_tokens=max_output_tokens or self.max_output_tokens)
        if not self.configured:
            raise AgencyModelClientError(CONFIG_MISSING, "provider/model/base_url/key incomplete")

        body: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "max_tokens": int(max_output_tokens or self.max_output_tokens),
            "temperature": self.temperature,
            "stream": False,
        }
        if self.request_json_object:
            body["response_format"] = {"type": "json_object"}
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            f"{self.base_url}/chat/completions", data=payload, method="POST",
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {self._api_key}",
                     "X-Chiyo-Purpose": self.purpose},
        )
        started = time.monotonic()
        try:
            with urllib.request.urlopen(request, timeout=float(timeout_s or self.timeout_s)) as resp:
                status = int(getattr(resp, "status", 200) or 200)
                raw = resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            status = int(exc.code)
            detail = ""
            try:
                detail = exc.read().decode("utf-8", "replace")[:300]
            except Exception:  # noqa: BLE001
                pass
            if status == 429:
                raise AgencyModelClientError(PROVIDER_RATE_LIMITED, detail or "rate limited", status=status) from exc
            if status in (401, 403):
                raise AgencyModelClientError(PROVIDER_AUTH_ERROR, detail or "auth rejected", status=status) from exc
            if status >= 500:
                raise AgencyModelClientError(PROVIDER_SERVER_ERROR, detail or "server error", status=status) from exc
            raise AgencyModelClientError(PROVIDER_UNAVAILABLE, detail or f"http {status}", status=status) from exc
        except socket.timeout as exc:
            raise AgencyModelClientError(TRANSPORT_TIMEOUT, "socket timeout") from exc
        except urllib.error.URLError as exc:
            reason = str(getattr(exc, "reason", exc))
            if "timed out" in reason.lower():
                raise AgencyModelClientError(TRANSPORT_TIMEOUT, reason) from exc
            raise AgencyModelClientError(TRANSPORT_CONNECTION, reason) from exc
        except TimeoutError as exc:
            raise AgencyModelClientError(TRANSPORT_TIMEOUT, "timeout") from exc
        except OSError as exc:
            raise AgencyModelClientError(TRANSPORT_CONNECTION, str(exc)) from exc

        latency_ms = int((time.monotonic() - started) * 1000)
        self.call_count += 1
        try:
            doc = json.loads(raw)
        except ValueError as exc:
            raise AgencyModelClientError(PROVIDER_MALFORMED_RESPONSE, "non-JSON body") from exc
        if not isinstance(doc, dict):
            raise AgencyModelClientError(PROVIDER_MALFORMED_RESPONSE, "non-object body")

        choices = doc.get("choices")
        text = ""
        if isinstance(choices, list) and choices:
            first = choices[0] or {}
            message = first.get("message") or {}
            text = message.get("content") or first.get("text") or ""
        if not isinstance(text, str):
            text = json.dumps(text, ensure_ascii=False)

        usage = doc.get("usage") if isinstance(doc.get("usage"), Mapping) else {}
        cost = None
        for key in ("cost", "cost_usd", "total_cost"):
            if isinstance(usage.get(key), (int, float)):
                cost = float(usage[key]); break
        return ModelCallResult(
            purpose=self.purpose, provider=self.provider, model=self.model, text=text,
            latency_ms=latency_ms, status=status,
            input_tokens=usage.get("prompt_tokens", usage.get("input_tokens")),
            output_tokens=usage.get("completion_tokens", usage.get("output_tokens")),
            total_tokens=usage.get("total_tokens"),
            reported_cost_usd=cost,
            usage_source="provider_reported" if usage else "unavailable",
            raw_usage=dict(usage),
        )
