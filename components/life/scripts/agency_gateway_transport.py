"""LPC0B-R1 sections 3A/5/6/7: reuse the gateway's own provider transport.

Authority: LPC0B-R1 order sections 2-7.

Why this file exists
--------------------
LPC0B proved that the raw ``urllib`` transport cannot reach the configured provider: on the
same host and the same URL, ``curl`` gets ``401`` (Cloudflare lets it through) while
``urllib`` gets ``403 error code: 1010`` -- a client-signature ban, so the API key was never
the problem. LPC0B-R1 forbids fighting that (section 4: no UA spoofing, no TLS-fingerprint
impersonation, no header piling, no "try requests/httpx"). The correct answer is section 3A:
use the host's own, already-working, officially supported plugin LLM seam.

That seam is ``ctx.llm`` -- ``agent/plugin_llm.py`` in the Hermes package, backed by
``agent/auxiliary_client.call_llm``, i.e. the same transport the main chat uses. Its own
documentation describes exactly this use case ("one LLM call, a typed answer, and to be
done").

What this class is, and is not (order sections 5 and 6)
-------------------------------------------------------
It is a **narrow** transport: request in, text/finish/usage/latency out. It knows nothing
about Decision, Adoption, Activity, LR-2, grants or permits, and it cannot become an Agency
owner -- the response still has to pass the adapter's contract mapping and then AG-1's own
``DecisionOutputValidator`` before any DecisionRecord can exist.

The host owns routing, authentication, timeouts and fallback, so no key, token or endpoint
ever passes through the Life Runtime (order sections 3A/11).
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Mapping, Optional

#: Identifier recorded in every metric so a reader can prove which backend was used.
TRANSPORT_BACKEND_GATEWAY = "gateway_ctx_llm"
TRANSPORT_BACKEND_RAW_HTTP = "raw_http_offline_only"

#: Purpose string; the host echoes it into ``agent.log`` and ``result.audit`` (section 9).
PURPOSE_AGENCY_COGNITION = "agency_cognition"


@dataclass
class TransportResult:
    """Provider-agnostic completion result. No domain meaning whatsoever."""

    text: str
    provider: str = "host"
    model: str = "host"
    finish_reason: Optional[str] = None
    provider_request_id: Optional[str] = None
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    total_tokens: Optional[int] = None
    cost_usd: Optional[float] = None
    usage_source: str = "unavailable"
    latency_ms: int = 0
    audit: Mapping[str, Any] = None  # type: ignore[assignment]


class HostTransportUnavailable(RuntimeError):
    """No host LLM seam was injected. Fail closed rather than silently falling back to raw HTTP."""


def _map_host_error(exc: BaseException) -> str:
    """Map a host-side failure onto one of the LPC0B transport failure classes."""
    name = type(exc).__name__
    text = f"{name}: {exc}".lower()
    if "trust" in name.lower() or "permission" in name.lower():
        return "CONFIG_MISSING"
    if "timeout" in text or "timed out" in text:
        return "TRANSPORT_TIMEOUT"
    if "429" in text or "rate" in text:
        return "PROVIDER_RATE_LIMITED"
    if "401" in text or "403" in text or "auth" in text or "credential" in text:
        return "PROVIDER_AUTH_ERROR"
    if "connect" in text or "unreachable" in text or "dns" in text:
        return "TRANSPORT_CONNECTION"
    if "json" in text or "decode" in text:
        return "PROVIDER_MALFORMED_RESPONSE"
    if "5" in text[:4] and "error" in text:
        return "PROVIDER_SERVER_ERROR"
    return "PROVIDER_UNAVAILABLE"


class GatewayAgencyModelTransport:
    """Thin adapter over ``ctx.llm`` -- the host's own provider transport.

    Only ``complete`` is used: one bounded chat completion with the plugin supplying its own
    messages, so no main-chat history is assembled and no chat turn is created (sections
    3A, 8, 10).
    """

    backend = TRANSPORT_BACKEND_GATEWAY

    def __init__(self, plugin_llm: Any, *, purpose: str = PURPOSE_AGENCY_COGNITION,
                 default_timeout_s: float = 8.0) -> None:
        if plugin_llm is None:
            raise HostTransportUnavailable("ctx.llm is None: the host LLM seam is unavailable")
        for attr in ("complete",):
            if not callable(getattr(plugin_llm, attr, None)):
                raise HostTransportUnavailable(f"host LLM seam has no callable {attr}()")
        self._llm = plugin_llm
        self.purpose = purpose
        self.default_timeout_s = float(default_timeout_s)
        self.call_count = 0
        self.provider = "host"
        self.model = "host"
        self.last_audit: Optional[Mapping[str, Any]] = None
        self.last_finish_reason: Optional[str] = None

    def __repr__(self) -> str:  # pragma: no cover - diagnostics only
        return f"<GatewayAgencyModelTransport purpose={self.purpose!r} provider={self.provider}>"

    def complete(self, *, system_prompt: str, user_prompt: str,
                 timeout_s: Optional[float] = None,
                 max_output_tokens: Optional[int] = None) -> Any:
        """One bounded host completion. Returns a `transport_adapter.ModelCallResult`.

        Raises `transport_adapter.AgencyModelClientError`-compatible errors, i.e. it never
        returns a value that a caller could mistake for a model *choice* (section 16).
        """
        # Imported lazily so this module can be imported without the adapter module present.
        from agency_model_client import AgencyModelClientError, ModelCallResult

        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": user_prompt})

        kwargs: dict[str, Any] = {
            "messages": messages,
            "purpose": self.purpose,
            "temperature": 0.0,
        }
        if max_output_tokens:
            kwargs["max_tokens"] = int(max_output_tokens)
        kwargs["timeout"] = float(timeout_s or self.default_timeout_s)

        started = time.monotonic()
        try:
            result = self._llm.complete(**kwargs)
        except Exception as exc:  # noqa: BLE001 - mapped, never leaked as a "choice"
            raise AgencyModelClientError(_map_host_error(exc), f"host transport: {exc}"[:300]) from exc
        latency_ms = int((time.monotonic() - started) * 1000)

        usage = getattr(result, "usage", None)
        self.call_count += 1
        self.provider = str(getattr(result, "provider", "host") or "host")
        self.model = str(getattr(result, "model", "host") or "host")
        self.last_audit = getattr(result, "audit", None)
        self.last_finish_reason = getattr(result, "finish_reason", None)

        def _usage_int(name: str) -> Optional[int]:
            value = getattr(usage, name, None)
            return int(value) if isinstance(value, (int, float)) else None

        in_tok, out_tok, tot_tok = _usage_int("input_tokens"), _usage_int("output_tokens"), _usage_int("total_tokens")
        cost = getattr(usage, "cost_usd", None)
        text = getattr(result, "text", None)
        if not isinstance(text, str):
            text = "" if text is None else str(text)
        return ModelCallResult(
            purpose=self.purpose, provider=self.provider, model=self.model, text=text,
            latency_ms=latency_ms, status=200,
            input_tokens=in_tok, output_tokens=out_tok, total_tokens=tot_tok,
            reported_cost_usd=float(cost) if isinstance(cost, (int, float)) else None,
            usage_source="host_reported" if (tot_tok or in_tok or out_tok) else "unavailable",
            raw_usage={"audit": dict(self.last_audit or {})},
        )

    def describe(self) -> dict[str, Any]:
        return {"backend": self.backend, "purpose": self.purpose,
                "default_timeout_s": self.default_timeout_s,
                "provider": self.provider, "model": self.model,
                "calls": self.call_count,
                "note": "host owns routing, auth, timeout and retry; the plugin never sees a key"}
