from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterable


class CanaryMode(str, Enum):
    OFF = "OFF"
    CANARY = "CANARY"
    GLOBAL = "GLOBAL"


class MatchBy(str, Enum):
    USER = "USER"
    SESSION = "SESSION"
    CONVERSATION = "CONVERSATION"
    STRICT_BINDING = "STRICT_BINDING"
    NONE = "NONE"


@dataclass(frozen=True)
class CanaryBinding:
    user_id: str
    session_id: str
    conversation_id: str | None = None


@dataclass(frozen=True)
class CanaryConfig:
    master_enabled: bool = False
    mode: CanaryMode = CanaryMode.OFF
    allowed_user_ids: frozenset[str] = frozenset()
    allowed_session_ids: frozenset[str] = frozenset()
    allowed_conversation_ids: frozenset[str] = frozenset()
    strict_canary_binding: bool = False
    bindings: tuple[CanaryBinding, ...] = ()

    @classmethod
    def from_dict(cls, data: Any) -> "CanaryConfig":
        if not isinstance(data, dict):
            raise ValueError("canary config must be an object")
        raw_mode = data.get("mode", "OFF")
        try:
            mode = CanaryMode(str(raw_mode).upper())
        except ValueError as exc:
            raise ValueError("unknown canary mode") from exc

        def ids(name: str) -> frozenset[str]:
            raw = data.get(name, [])
            if raw is None:
                raw = []
            if not isinstance(raw, list) or any(not isinstance(item, str) or not item.strip() for item in raw):
                raise ValueError(f"malformed {name}")
            values = [item.strip() for item in raw]
            if len(values) != len(set(values)):
                raise ValueError(f"duplicate {name}")
            return frozenset(values)

        raw_bindings = data.get("bindings", [])
        if not isinstance(raw_bindings, list):
            raise ValueError("malformed bindings")
        bindings: list[CanaryBinding] = []
        seen: set[tuple[str, str, str | None]] = set()
        for item in raw_bindings:
            if not isinstance(item, dict):
                raise ValueError("malformed binding")
            user_id = item.get("user_id")
            session_id = item.get("session_id")
            conversation_id = item.get("conversation_id")
            if not isinstance(user_id, str) or not user_id.strip() or not isinstance(session_id, str) or not session_id.strip():
                raise ValueError("binding requires user_id and session_id")
            if conversation_id is not None and (not isinstance(conversation_id, str) or not conversation_id.strip()):
                raise ValueError("malformed binding conversation_id")
            key = (user_id.strip(), session_id.strip(), conversation_id.strip() if conversation_id else None)
            if key in seen:
                raise ValueError("duplicate binding")
            seen.add(key)
            bindings.append(CanaryBinding(*key))
        return cls(
            master_enabled=data.get("master_enabled") is True,
            mode=mode,
            allowed_user_ids=ids("allowed_user_ids"),
            allowed_session_ids=ids("allowed_session_ids"),
            allowed_conversation_ids=ids("allowed_conversation_ids"),
            strict_canary_binding=data.get("strict_canary_binding") is True,
            bindings=tuple(bindings),
        )

    @classmethod
    def fail_closed(cls) -> "CanaryConfig":
        return cls()

    def to_dict(self) -> dict[str, Any]:
        return {
            "master_enabled": self.master_enabled,
            "mode": self.mode.value,
            "allowed_user_ids": sorted(self.allowed_user_ids),
            "allowed_session_ids": sorted(self.allowed_session_ids),
            "allowed_conversation_ids": sorted(self.allowed_conversation_ids),
            "strict_canary_binding": self.strict_canary_binding,
            "bindings": [
                {key: value for key, value in {
                    "user_id": item.user_id,
                    "session_id": item.session_id,
                    "conversation_id": item.conversation_id,
                }.items() if value is not None}
                for item in self.bindings
            ],
        }


@dataclass(frozen=True)
class CanaryDecision:
    request_id: str
    master_enabled: bool
    mode: str
    user_id: str | None
    session_id: str | None
    conversation_id: str | None
    scope_match: bool
    matched_by: MatchBy
    native_context_allowed: bool
    reason_code: str

    def as_dict(self) -> dict[str, Any]:
        result = dict(self.__dict__)
        result["matched_by"] = self.matched_by.value
        return result


class CanaryObservability:
    def __init__(self) -> None:
        self.counters = {
            "native_memory_canary_scope_checks_total": 0,
            "native_memory_canary_scope_allow_total": 0,
            "native_memory_canary_scope_deny_total": 0,
            "native_memory_canary_user_match_total": 0,
            "native_memory_canary_session_match_total": 0,
            "native_memory_canary_conversation_match_total": 0,
            "native_memory_canary_invalid_config_total": 0,
        }

    def inc(self, key: str) -> None:
        self.counters[key] += 1


class ScopedCanaryGate:
    """Pure, stateless request gate. It never reads or mutates Memory Store."""

    def __init__(self, config: CanaryConfig, observability: CanaryObservability | None = None):
        self.config = config
        self.observability = observability or CanaryObservability()

    @classmethod
    def from_dict(cls, data: Any) -> "ScopedCanaryGate":
        try:
            config = CanaryConfig.from_dict(data)
        except Exception:
            obs = CanaryObservability()
            obs.inc("native_memory_canary_invalid_config_total")
            return cls(CanaryConfig.fail_closed(), obs)
        return cls(config)

    def decide(
        self,
        request_id: str,
        user_id: str | None,
        session_id: str | None,
        conversation_id: str | None,
        *,
        double_authority: bool = False,
        double_injection: bool = False,
        circuit_fault: bool = False,
    ) -> CanaryDecision:
        self.observability.inc("native_memory_canary_scope_checks_total")
        config = self.config
        base = dict(
            request_id=request_id,
            master_enabled=config.master_enabled,
            mode=config.mode.value,
            user_id=user_id,
            session_id=session_id,
            conversation_id=conversation_id,
        )
        if not config.master_enabled:
            return self._deny(base, MatchBy.NONE, "MASTER_OFF")
        if config.mode is CanaryMode.OFF:
            return self._deny(base, MatchBy.NONE, "MODE_OFF")
        if config.mode is CanaryMode.GLOBAL:
            if double_authority:
                return self._deny(base, MatchBy.NONE, "DOUBLE_AUTHORITY_BLOCKED")
            if double_injection:
                return self._deny(base, MatchBy.NONE, "DOUBLE_INJECTION_BLOCKED")
            if circuit_fault:
                return self._deny(base, MatchBy.NONE, "CIRCUIT_BREAKER_BYPASS")
            return self._allow(base, MatchBy.NONE, "GLOBAL_MODE")

        if config.strict_canary_binding or config.bindings:
            for binding in config.bindings:
                if user_id == binding.user_id and session_id == binding.session_id and (
                    binding.conversation_id is None or conversation_id == binding.conversation_id
                ):
                    if double_authority:
                        return self._deny(base, MatchBy.STRICT_BINDING, "DOUBLE_AUTHORITY_BLOCKED")
                    if double_injection:
                        return self._deny(base, MatchBy.STRICT_BINDING, "DOUBLE_INJECTION_BLOCKED")
                    if circuit_fault:
                        return self._deny(base, MatchBy.STRICT_BINDING, "CIRCUIT_BREAKER_BYPASS")
                    return self._allow(base, MatchBy.STRICT_BINDING, "STRICT_BINDING_MATCH")
            if not config.bindings:
                return self._deny(base, MatchBy.NONE, "EMPTY_ALLOWLIST")
            return self._deny(base, MatchBy.NONE, "NO_SCOPE_MATCH")

        if not (config.allowed_user_ids or config.allowed_session_ids or config.allowed_conversation_ids):
            return self._deny(base, MatchBy.NONE, "EMPTY_ALLOWLIST")
        if user_id is not None and user_id in config.allowed_user_ids:
            match = MatchBy.USER
        elif session_id is not None and session_id in config.allowed_session_ids:
            match = MatchBy.SESSION
        elif conversation_id is not None and conversation_id in config.allowed_conversation_ids:
            match = MatchBy.CONVERSATION
        else:
            return self._deny(base, MatchBy.NONE, "NO_SCOPE_MATCH")
        if double_authority:
            return self._deny(base, match, "DOUBLE_AUTHORITY_BLOCKED")
        if double_injection:
            return self._deny(base, match, "DOUBLE_INJECTION_BLOCKED")
        if circuit_fault:
            return self._deny(base, match, "CIRCUIT_BREAKER_BYPASS")
        return self._allow(base, match, {
            MatchBy.USER: "USER_MATCH",
            MatchBy.SESSION: "SESSION_MATCH",
            MatchBy.CONVERSATION: "CONVERSATION_MATCH",
        }[match])

    def _allow(self, base: dict[str, Any], matched_by: MatchBy, reason: str) -> CanaryDecision:
        self.observability.inc("native_memory_canary_scope_allow_total")
        if matched_by is MatchBy.USER:
            self.observability.inc("native_memory_canary_user_match_total")
        elif matched_by is MatchBy.SESSION:
            self.observability.inc("native_memory_canary_session_match_total")
        elif matched_by is MatchBy.CONVERSATION:
            self.observability.inc("native_memory_canary_conversation_match_total")
        return CanaryDecision(**base, scope_match=True, matched_by=matched_by, native_context_allowed=True, reason_code=reason)

    def _deny(self, base: dict[str, Any], matched_by: MatchBy, reason: str) -> CanaryDecision:
        self.observability.inc("native_memory_canary_scope_deny_total")
        return CanaryDecision(**base, scope_match=matched_by is not MatchBy.NONE, matched_by=matched_by, native_context_allowed=False, reason_code=reason)
