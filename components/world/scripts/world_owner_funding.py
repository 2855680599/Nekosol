#!/usr/bin/env python3
"""Owner-funding request and decision state for Shiomi City V1.

This module is deliberately a narrow communication/economy bridge.  Chiyo's
verified Main Self may create a request, but the request is not delivered by
this module and it never changes the wallet.  A verified owner message can
resolve a delivered request; only then does the existing property-economy
ledger append one owner CREDIT.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import tempfile
from collections.abc import Mapping
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Optional

try:
    import fcntl
except ImportError:  # pragma: no cover - production is POSIX
    fcntl = None


FEATURE_ENV = "CHIYO_WORLD_OWNER_FUNDING_ENABLED"
STATE_FILENAME = "world_owner_funding.json"
SCHEMA_VERSION = "world.owner.funding.v1"
REQUEST_KIND = "owner_funding_request"

STATUS_OPEN = "OPEN"
STATUS_GRANTED = "GRANTED"
STATUS_REFUSED = "REFUSED"
STATUS_FAILED_DELIVERY = "FAILED_DELIVERY"
DELIVERY_PENDING = "PENDING"
DELIVERY_DELIVERED = "DELIVERED"
DELIVERY_FAILED = "FAILED"
DECISION_GRANT = "GRANT"
DECISION_REFUSE = "REFUSE"
OWNER_FUNDING_SOURCE = "owner"
MAX_REQUESTS = 256
MAX_ID_LENGTH = 128
MAX_ERROR_LENGTH = 160
MAX_AMOUNT_JPY = 10_000_000

_TRUTHY = frozenset({"1", "true", "yes", "on"})
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_STATUSES = frozenset(
    {STATUS_OPEN, STATUS_GRANTED, STATUS_REFUSED, STATUS_FAILED_DELIVERY}
)
_DELIVERY_STATES = frozenset(
    {DELIVERY_PENDING, DELIVERY_DELIVERED, DELIVERY_FAILED}
)
_DECISIONS = frozenset({DECISION_GRANT, DECISION_REFUSE})
_STATE_KEYS = frozenset({"schema_version", "revision", "updated_at", "requests"})
_REQUEST_KEYS = frozenset(
    {
        "request_id",
        "request_intent_id",
        "source_session_id",
        "source_turn_id",
        "created_at",
        "status",
        "delivery_status",
        "requested_amount_jpy",
        "outbound_delivery_obligation_id",
        "outbound_message_identity",
        "delivered_at",
        "delivery_error",
        "owner_decision",
        "owner_decision_id",
        "owner_session_id",
        "owner_turn_id",
        "owner_message_id",
        "owner_source",
        "resolved_at",
        "granted_amount_jpy",
        "transaction_id",
    }
)


class OwnerFundingError(ValueError):
    """The bounded owner-funding state or authority is invalid."""


def _truthy(value: Any) -> bool:
    return str(value or "").strip().lower() in _TRUTHY


def feature_enabled(environment: Optional[Mapping[str, str]] = None) -> bool:
    values = os.environ if environment is None else environment
    return _truthy(values.get(FEATURE_ENV))


def _home(hermes_home: Optional[Path | str] = None) -> Path:
    raw = hermes_home if hermes_home is not None else os.environ.get("HERMES_HOME")
    path = Path(raw).expanduser() if raw else Path.home() / ".hermes"
    if not path.is_absolute():
        raise OwnerFundingError("hermes_home must be absolute")
    return path


def state_path(hermes_home: Optional[Path | str] = None) -> Path:
    return _home(hermes_home) / "state" / STATE_FILENAME


def _text(value: Any, field: str, maximum: int = MAX_ID_LENGTH) -> str:
    if not isinstance(value, str):
        raise OwnerFundingError(f"{field} must be a string")
    result = value.strip()
    if (
        not result
        or len(result) > maximum
        or any(char in result for char in "\r\n\x00")
    ):
        raise OwnerFundingError(f"{field} is invalid")
    return result


def _id(value: Any, field: str) -> str:
    result = _text(value, field)
    if not _ID_RE.fullmatch(result):
        raise OwnerFundingError(f"{field} has invalid characters")
    return result


def _positive_amount(value: Any, field: str) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value <= 0
        or value > MAX_AMOUNT_JPY
    ):
        raise OwnerFundingError(f"{field} must be a bounded positive integer")
    return value


def _timestamp(value: Any = None, field: str = "timestamp") -> str:
    if value is None:
        instant = datetime.now(timezone.utc)
    elif isinstance(value, datetime):
        instant = value
    else:
        text = _text(value, field, 96)
        normalized = text[:-1] + "+00:00" if text.endswith(("Z", "z")) else text
        try:
            instant = datetime.fromisoformat(normalized)
        except ValueError as exc:
            raise OwnerFundingError(f"{field} must be ISO datetime") from exc
    if instant.tzinfo is None or instant.utcoffset() is None:
        raise OwnerFundingError(f"{field} must include timezone")
    return instant.astimezone(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def _assert_exact_keys(value: Mapping[str, Any], allowed: frozenset[str], field: str) -> None:
    if set(value) - allowed:
        raise OwnerFundingError(f"{field} contains unknown fields")


def _validate_request(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise OwnerFundingError("request must be an object")
    _assert_exact_keys(value, _REQUEST_KEYS, "request")
    result: dict[str, Any] = {
        "request_id": _id(value.get("request_id"), "request.request_id"),
        "request_intent_id": _id(
            value.get("request_intent_id"), "request.request_intent_id"
        ),
        "source_session_id": _id(
            value.get("source_session_id"), "request.source_session_id"
        ),
        "source_turn_id": _id(value.get("source_turn_id"), "request.source_turn_id"),
        "created_at": _timestamp(value.get("created_at"), "request.created_at"),
        "status": value.get("status"),
        "delivery_status": value.get("delivery_status"),
    }
    if result["status"] not in _STATUSES:
        raise OwnerFundingError("request.status is invalid")
    if result["delivery_status"] not in _DELIVERY_STATES:
        raise OwnerFundingError("request.delivery_status is invalid")

    for field in (
        "outbound_delivery_obligation_id",
        "outbound_message_identity",
        "owner_decision_id",
        "owner_session_id",
        "owner_turn_id",
        "owner_message_id",
        "transaction_id",
    ):
        if field in value:
            result[field] = _id(value.get(field), f"request.{field}")
    for field in ("delivered_at", "resolved_at"):
        if field in value:
            result[field] = _timestamp(value.get(field), f"request.{field}")
    if "delivery_error" in value:
        result["delivery_error"] = _text(
            value.get("delivery_error"), "request.delivery_error", MAX_ERROR_LENGTH
        )
    if "requested_amount_jpy" in value:
        result["requested_amount_jpy"] = _positive_amount(
            value.get("requested_amount_jpy"), "request.requested_amount_jpy"
        )
    if "granted_amount_jpy" in value:
        result["granted_amount_jpy"] = _positive_amount(
            value.get("granted_amount_jpy"), "request.granted_amount_jpy"
        )
    if "owner_decision" in value:
        result["owner_decision"] = value.get("owner_decision")
        if result["owner_decision"] not in _DECISIONS:
            raise OwnerFundingError("request.owner_decision is invalid")
    if "owner_source" in value:
        result["owner_source"] = _id(value.get("owner_source"), "request.owner_source")

    if result["status"] == STATUS_OPEN:
        if result["delivery_status"] not in {DELIVERY_PENDING, DELIVERY_DELIVERED}:
            raise OwnerFundingError("OPEN request delivery state is invalid")
    elif result["status"] == STATUS_FAILED_DELIVERY:
        if result["delivery_status"] != DELIVERY_FAILED:
            raise OwnerFundingError("FAILED_DELIVERY request state is invalid")
    else:
        if result["delivery_status"] != DELIVERY_DELIVERED:
            raise OwnerFundingError("resolved request must be delivered")
        required = {
            "owner_decision",
            "owner_decision_id",
            "owner_session_id",
            "owner_turn_id",
            "owner_message_id",
            "owner_source",
            "resolved_at",
        }
        if not required.issubset(result):
            raise OwnerFundingError("resolved request authority is incomplete")
        if result["status"] == STATUS_GRANTED:
            if not {"granted_amount_jpy", "transaction_id"}.issubset(result):
                raise OwnerFundingError("GRANTED request settlement is incomplete")
            if result["owner_decision"] != DECISION_GRANT:
                raise OwnerFundingError("GRANTED request decision is invalid")
        elif result["owner_decision"] != DECISION_REFUSE:
            raise OwnerFundingError("REFUSED request decision is invalid")
    return result


def _validate_state(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise OwnerFundingError("owner funding state must be an object")
    _assert_exact_keys(value, _STATE_KEYS, "owner funding state")
    if value.get("schema_version") != SCHEMA_VERSION:
        raise OwnerFundingError("owner funding schema is unsupported")
    revision = value.get("revision")
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
        raise OwnerFundingError("owner funding revision is invalid")
    raw_requests = value.get("requests")
    if (
        not isinstance(raw_requests, list)
        or len(raw_requests) > MAX_REQUESTS
    ):
        raise OwnerFundingError("owner funding requests are invalid")
    requests = [_validate_request(item) for item in raw_requests]
    request_ids = [item["request_id"] for item in requests]
    intent_ids = [item["request_intent_id"] for item in requests]
    if len(request_ids) != len(set(request_ids)):
        raise OwnerFundingError("duplicate request_id")
    if len(intent_ids) != len(set(intent_ids)):
        raise OwnerFundingError("duplicate request_intent_id")
    return {
        "schema_version": SCHEMA_VERSION,
        "revision": revision,
        "updated_at": _timestamp(value.get("updated_at"), "updated_at"),
        "requests": requests,
    }


def _load(path: Path) -> Optional[dict[str, Any]]:
    if not path.exists():
        return None
    try:
        return _validate_state(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise OwnerFundingError("owner funding state cannot be read") from exc


def load_state(hermes_home: Optional[Path | str] = None) -> Optional[dict[str, Any]]:
    """Read request state without creating a file or changing any state."""

    return _load(state_path(hermes_home))


def _save(path: Path, state: Mapping[str, Any]) -> dict[str, Any]:
    normalized = _validate_state(state)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (
        json.dumps(
            normalized,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")
    temporary: Optional[str] = None
    try:
        descriptor, temporary = tempfile.mkstemp(
            prefix=f".{path.name}.", dir=str(path.parent), text=False
        )
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
        os.chmod(path, 0o600)
        try:
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError:
            pass
    except OSError as exc:
        raise OwnerFundingError("owner funding state could not be written") from exc
    finally:
        if temporary is not None:
            try:
                os.unlink(temporary)
            except OSError:
                pass
    return normalized


@contextmanager
def _lock(path: Path) -> Iterator[None]:
    if fcntl is None:
        raise OwnerFundingError("owner funding lock unavailable")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_name(path.name + ".lock").open("a+") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _empty_state(now: Any = None) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "revision": 0,
        "updated_at": _timestamp(now, "updated_at"),
        "requests": [],
    }


def _stable_request_id(request_intent_id: str) -> str:
    digest = hashlib.sha256(request_intent_id.encode("utf-8")).hexdigest()
    return f"owner-funding-request-{digest}"


def _stable_owner_decision_id(source: str, session_id: str, message_id: str) -> str:
    material = f"{source}|{session_id}|{message_id}"
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()
    return f"owner-decision-{digest}"


def _request_result(status: str, request: Optional[Mapping[str, Any]] = None, **extra: Any) -> dict[str, Any]:
    result: dict[str, Any] = {
        "kind": REQUEST_KIND,
        "status": status,
    }
    if request is not None:
        result["request"] = dict(request)
        result["request_id"] = request.get("request_id")
    result.update(extra)
    return result


def request_owner_funds(
    *,
    main_self_verified: bool,
    session_id: Any,
    turn_id: Any,
    request_intent_id: Any,
    requested_amount_jpy: Any = None,
    hermes_home: Optional[Path | str] = None,
    environment: Optional[Mapping[str, str]] = None,
    now: Any = None,
) -> dict[str, Any]:
    """Create one explicit request; this function never sends or credits."""

    if not feature_enabled(environment):
        return _request_result("disabled", reason_code="FEATURE_DISABLED")
    if main_self_verified is not True:
        return _request_result("rejected", reason_code="VERIFIED_MAIN_SELF_REQUIRED")
    try:
        normalized_session = _id(session_id, "session_id")
        normalized_turn = _id(turn_id, "turn_id")
        normalized_intent = _id(request_intent_id, "request_intent_id")
        normalized_amount = (
            _positive_amount(requested_amount_jpy, "requested_amount_jpy")
            if requested_amount_jpy is not None
            else None
        )
    except OwnerFundingError:
        return _request_result("rejected", reason_code="INVALID_REQUEST_INPUT")

    path = state_path(hermes_home)
    try:
        with _lock(path):
            state = _load(path) or _empty_state(now)
            existing = next(
                (
                    item
                    for item in state["requests"]
                    if item["request_intent_id"] == normalized_intent
                ),
                None,
            )
            if existing is not None:
                if existing.get("requested_amount_jpy") != normalized_amount:
                    return _request_result(
                        "rejected",
                        existing,
                        reason_code="REQUEST_INTENT_CONFLICT",
                    )
                return _request_result(
                    "already_exists",
                    existing,
                    reason_code="IDEMPOTENT_REPLAY",
                )
            same_turn = next(
                (
                    item
                    for item in state["requests"]
                    if item["source_session_id"] == normalized_session
                    and item["source_turn_id"] == normalized_turn
                ),
                None,
            )
            if same_turn is not None:
                return _request_result(
                    "rejected",
                    same_turn,
                    reason_code="ONE_REQUEST_PER_TURN",
                )
            if len(state["requests"]) >= MAX_REQUESTS:
                return _request_result("rejected", reason_code="REQUEST_CAPACITY_FULL")
            created_at = _timestamp(now, "created_at")
            record: dict[str, Any] = {
                "request_id": _stable_request_id(normalized_intent),
                "request_intent_id": normalized_intent,
                "source_session_id": normalized_session,
                "source_turn_id": normalized_turn,
                "created_at": created_at,
                "status": STATUS_OPEN,
                "delivery_status": DELIVERY_PENDING,
            }
            if normalized_amount is not None:
                record["requested_amount_jpy"] = normalized_amount
            updated = dict(state)
            updated["revision"] = state["revision"] + 1
            updated["updated_at"] = created_at
            updated["requests"] = [*state["requests"], record]
            saved = _save(path, updated)
            verified = _load(path)
            if verified != saved:
                raise OwnerFundingError("request postcondition failed")
            return _request_result("created", verified["requests"][-1])
    except OwnerFundingError as exc:
        return _request_result(
            "failed", reason_code="OWNER_FUNDING_STATE_ERROR", error_type=type(exc).__name__
        )


def _find_request(
    requests: list[dict[str, Any]],
    *,
    request_id: Optional[str] = None,
    session_id: Optional[str] = None,
    turn_id: Optional[str] = None,
) -> Optional[dict[str, Any]]:
    for item in requests:
        if request_id is not None and item["request_id"] != request_id:
            continue
        if session_id is not None and item["source_session_id"] != session_id:
            continue
        if turn_id is not None and item["source_turn_id"] != turn_id:
            continue
        return item
    return None


def _set_delivery_failed(
    request_id: str,
    *,
    error: str,
    hermes_home: Optional[Path | str],
    now: Any = None,
) -> bool:
    path = state_path(hermes_home)
    try:
        with _lock(path):
            state = _load(path)
            request = _find_request(state["requests"], request_id=request_id) if state else None
            if request is None or request["status"] != STATUS_OPEN:
                return False
            timestamp = _timestamp(now, "updated_at")
            request["status"] = STATUS_FAILED_DELIVERY
            request["delivery_status"] = DELIVERY_FAILED
            request["delivery_error"] = _text(error, "delivery_error", MAX_ERROR_LENGTH)
            updated = dict(state)
            updated["revision"] = state["revision"] + 1
            updated["updated_at"] = timestamp
            updated["requests"] = state["requests"]
            _save(path, updated)
            return True
    except OwnerFundingError:
        return False


def _ledger_row(obligation_id: str) -> Optional[Mapping[str, Any]]:
    try:
        from gateway import delivery_ledger

        return delivery_ledger.get_obligation(obligation_id)
    except Exception:
        return None


def _delivery_timestamp(row: Mapping[str, Any]) -> str:
    value = row.get("updated_at")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return _timestamp(datetime.fromtimestamp(float(value), timezone.utc), "delivered_at")
    return _timestamp(None, "delivered_at")


def _refresh_request_delivery_locked(request: dict[str, Any]) -> Optional[str]:
    obligation_id = request.get("outbound_delivery_obligation_id")
    if request["status"] != STATUS_OPEN or request["delivery_status"] != DELIVERY_PENDING:
        return None
    if not obligation_id:
        return None
    row = _ledger_row(obligation_id)
    if not isinstance(row, Mapping):
        return None
    state = str(row.get("state") or "").lower()
    if state == "delivered":
        request["delivery_status"] = DELIVERY_DELIVERED
        request["delivered_at"] = _delivery_timestamp(row)
        return DELIVERY_DELIVERED
    if state in {"failed", "abandoned"}:
        request["status"] = STATUS_FAILED_DELIVERY
        request["delivery_status"] = DELIVERY_FAILED
        request["delivery_error"] = f"delivery_{state}"
        return DELIVERY_FAILED
    return None


def finalize_delivery_binding(
    *,
    request_id: str,
    obligation_id: str,
    hermes_home: Optional[Path | str] = None,
    now: Any = None,
) -> dict[str, Any]:
    """Resolve a request only from the existing authoritative ledger row."""

    path = state_path(hermes_home)
    try:
        normalized_request_id = _id(request_id, "request_id")
        normalized_obligation_id = _id(obligation_id, "obligation_id")
    except OwnerFundingError:
        return _request_result("rejected", reason_code="INVALID_DELIVERY_BINDING")
    try:
        with _lock(path):
            state = _load(path)
            request = _find_request(
                state["requests"], request_id=normalized_request_id
            ) if state else None
            if request is None:
                return _request_result("rejected", reason_code="REQUEST_NOT_FOUND")
            if request.get("outbound_delivery_obligation_id") != normalized_obligation_id:
                return _request_result("rejected", reason_code="DELIVERY_IDENTITY_MISMATCH")
            outcome = _refresh_request_delivery_locked(request)
            if outcome is None:
                return _request_result("unconfirmed", request)
            timestamp = _timestamp(now, "updated_at")
            updated = dict(state)
            updated["revision"] = state["revision"] + 1
            updated["updated_at"] = timestamp
            updated["requests"] = state["requests"]
            saved = _save(path, updated)
            verified = _load(path)
            refreshed = _find_request(
                verified["requests"], request_id=normalized_request_id
            )
            return _request_result(
                "delivered" if outcome == DELIVERY_DELIVERED else "failed",
                refreshed or saved["requests"][0],
            )
    except OwnerFundingError as exc:
        return _request_result(
            "failed", reason_code="OWNER_FUNDING_STATE_ERROR", error_type=type(exc).__name__
        )


def prepare_delivery_binding(
    *,
    session_id: Any,
    turn_id: Any,
    session_key: Any,
    message_ref: Any,
    response: Any,
    adapter: Any,
    generation: Any = None,
    agent_failed: bool = False,
    already_sent: bool = False,
    intentional_silence: bool = False,
    hermes_home: Optional[Path | str] = None,
    environment: Optional[Mapping[str, str]] = None,
) -> bool:
    """Bind one explicit request to the normal response-delivery callback."""

    if not feature_enabled(environment):
        return False
    try:
        normalized_session = _id(session_id, "session_id")
        normalized_turn = _id(turn_id, "turn_id")
        normalized_session_key = _text(session_key, "session_key", 512)
        normalized_message_ref = _id(message_ref, "message_ref")
    except OwnerFundingError:
        return False

    path = state_path(hermes_home)
    try:
        with _lock(path):
            state = _load(path)
            request = _find_request(
                state["requests"], session_id=normalized_session, turn_id=normalized_turn
            ) if state else None
            if request is None or request["status"] != STATUS_OPEN:
                return False
            request_id = request["request_id"]
    except OwnerFundingError:
        return False

    if (
        agent_failed
        or already_sent
        or intentional_silence
        or not isinstance(response, str)
        or not response.strip()
        or "MEDIA:" in response
    ):
        _set_delivery_failed(
            request_id,
            error="response_not_boundable",
            hermes_home=hermes_home,
        )
        return False
    register = getattr(adapter, "register_post_delivery_callback", None)
    if not callable(register):
        _set_delivery_failed(
            request_id,
            error="delivery_callback_unavailable",
            hermes_home=hermes_home,
        )
        return False
    try:
        from gateway import delivery_ledger

        if not delivery_ledger.ledger_enabled():
            raise OwnerFundingError("delivery_ledger_disabled")
        obligation_id = delivery_ledger.compute_obligation_id(
            normalized_session_key, normalized_message_ref, response
        )
        with _lock(path):
            state = _load(path)
            request = _find_request(state["requests"], request_id=request_id) if state else None
            if request is None or request["status"] != STATUS_OPEN:
                return False
            if request.get("outbound_delivery_obligation_id") not in {None, obligation_id}:
                raise OwnerFundingError("request already has another delivery identity")
            request["outbound_delivery_obligation_id"] = obligation_id
            request["outbound_message_identity"] = obligation_id
            updated = dict(state)
            updated["revision"] = state["revision"] + 1
            updated["updated_at"] = _timestamp(None, "updated_at")
            updated["requests"] = state["requests"]
            _save(path, updated)

        def _after_delivery() -> dict[str, Any]:
            return finalize_delivery_binding(
                request_id=request_id,
                obligation_id=obligation_id,
                hermes_home=hermes_home,
            )

        register(
            normalized_session_key,
            _after_delivery,
            generation=generation,
        )
        return True
    except Exception as exc:
        _set_delivery_failed(
            request_id,
            error=f"delivery_binding_{type(exc).__name__}",
            hermes_home=hermes_home,
        )
        return False


def get_request(
    request_id: Any,
    *,
    hermes_home: Optional[Path | str] = None,
) -> Optional[dict[str, Any]]:
    try:
        normalized = _id(request_id, "request_id")
        state = load_state(hermes_home)
        request = _find_request(state["requests"], request_id=normalized) if state else None
        return dict(request) if request is not None else None
    except OwnerFundingError:
        return None


def list_requests(
    *,
    hermes_home: Optional[Path | str] = None,
    environment: Optional[Mapping[str, str]] = None,
) -> dict[str, Any]:
    if not feature_enabled(environment):
        return {"kind": REQUEST_KIND, "status": "disabled", "reason_code": "FEATURE_DISABLED"}
    state = load_state(hermes_home)
    return {
        "kind": REQUEST_KIND,
        "status": "available",
        "requests": [dict(item) for item in (state["requests"] if state else [])],
    }


def _economy_module(hermes_home: Path):
    scripts_dir = str(hermes_home / "scripts")
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)
    import world_property_economy

    return world_property_economy


def resolve_owner_fund_request(
    *,
    request_id: Any,
    decision: Any,
    amount_jpy: Any = None,
    owner_verified: bool,
    owner_session_id: Any,
    owner_turn_id: Any,
    owner_message_id: Any,
    owner_source: Any,
    hermes_home: Optional[Path | str] = None,
    environment: Optional[Mapping[str, str]] = None,
    now: Any = None,
) -> dict[str, Any]:
    """Resolve one delivered request from verified owner/Main Self context."""

    if not feature_enabled(environment):
        return _request_result("disabled", reason_code="FEATURE_DISABLED")
    if owner_verified is not True:
        return _request_result("rejected", reason_code="VERIFIED_OWNER_REQUIRED")
    try:
        normalized_request_id = _id(request_id, "request_id")
        normalized_decision = _text(decision, "decision", 16).upper()
        if normalized_decision not in _DECISIONS:
            raise OwnerFundingError("invalid decision")
        normalized_owner_session = _id(owner_session_id, "owner_session_id")
        normalized_owner_turn = _id(owner_turn_id, "owner_turn_id")
        normalized_owner_message = _id(owner_message_id, "owner_message_id")
        normalized_owner_source = _id(owner_source, "owner_source")
        if normalized_decision == DECISION_GRANT:
            normalized_amount = _positive_amount(amount_jpy, "amount_jpy")
        else:
            if amount_jpy is not None:
                raise OwnerFundingError("REFUSE cannot include amount")
            normalized_amount = None
    except OwnerFundingError:
        return _request_result("rejected", reason_code="INVALID_OWNER_DECISION_INPUT")

    decision_id = _stable_owner_decision_id(
        normalized_owner_source,
        normalized_owner_session,
        normalized_owner_message,
    )
    home = _home(hermes_home)
    path = state_path(home)
    granted_transaction: Optional[dict[str, Any]] = None
    already_granted = False
    try:
        with _lock(path):
            state = _load(path)
            request = _find_request(state["requests"], request_id=normalized_request_id) if state else None
            if request is None:
                return _request_result("rejected", reason_code="REQUEST_NOT_FOUND")
            delivery_outcome = _refresh_request_delivery_locked(request)
            if delivery_outcome is not None:
                updated = dict(state)
                updated["revision"] = state["revision"] + 1
                updated["updated_at"] = _timestamp(now, "updated_at")
                updated["requests"] = state["requests"]
                _save(path, updated)
            if request["status"] == STATUS_GRANTED:
                granted_transaction = dict(
                    _economy_module(home).find_owner_funding(
                        normalized_request_id, hermes_home=home
                    )
                    or {}
                )
                if (
                    not granted_transaction
                    or normalized_decision != DECISION_GRANT
                    or granted_transaction.get("amount") != normalized_amount
                    or granted_transaction.get("provenance", {}).get(
                        "owner_decision_id"
                    )
                    != decision_id
                ):
                    return _request_result(
                        "rejected",
                        request,
                        reason_code="REQUEST_ALREADY_RESOLVED",
                    )
                already_granted = True
            elif request["status"] == STATUS_REFUSED:
                return _request_result("already_refused", request)
            elif request["status"] == STATUS_FAILED_DELIVERY:
                return _request_result("rejected", request, reason_code="FAILED_DELIVERY")
            elif request["status"] != STATUS_OPEN or request["delivery_status"] != DELIVERY_DELIVERED:
                return _request_result("rejected", request, reason_code="REQUEST_NOT_DELIVERED")

            if granted_transaction is None:
                economy = _economy_module(home)
                existing = economy.find_owner_funding(
                    normalized_request_id, hermes_home=home
                )
                if existing is not None:
                    granted_transaction = dict(existing)
                    already_granted = True
                elif normalized_decision == DECISION_REFUSE:
                    timestamp = _timestamp(now, "resolved_at")
                    request.update(
                        {
                            "status": STATUS_REFUSED,
                            "owner_decision": DECISION_REFUSE,
                            "owner_decision_id": decision_id,
                            "owner_session_id": normalized_owner_session,
                            "owner_turn_id": normalized_owner_turn,
                            "owner_message_id": normalized_owner_message,
                            "owner_source": normalized_owner_source,
                            "resolved_at": timestamp,
                        }
                    )
                    updated = dict(state)
                    updated["revision"] = state["revision"] + 1
                    updated["updated_at"] = timestamp
                    updated["requests"] = state["requests"]
                    saved = _save(path, updated)
                    verified = _load(path)
                    final_request = _find_request(
                        verified["requests"], request_id=normalized_request_id
                    )
                    return _request_result("refused", final_request or saved["requests"][-1])
                else:
                    authorization = {
                        "request_id": normalized_request_id,
                        "request_status": STATUS_OPEN,
                        "delivery_status": DELIVERY_DELIVERED,
                        "owner_decision_id": decision_id,
                        "owner_source": normalized_owner_source,
                        "owner_session_id": normalized_owner_session,
                        "owner_turn_id": normalized_owner_turn,
                        "owner_message_id": normalized_owner_message,
                        "outbound_message_identity": request.get(
                            "outbound_message_identity"
                        ),
                    }
                    credit = economy.apply_owner_funding_credit(
                        request_id=normalized_request_id,
                        owner_decision_id=decision_id,
                        amount_jpy=normalized_amount,
                        owner_verified=True,
                        authorization=authorization,
                        hermes_home=home,
                        environment=environment,
                        now=now,
                    )
                    if credit.get("status") not in {"applied", "already_applied"}:
                        return _request_result(
                            "rejected",
                            request,
                            reason_code=credit.get("reason_code", "CREDIT_REJECTED"),
                        )
                    granted_transaction = dict(credit.get("transaction") or {})
                    already_granted = credit.get("status") == "already_applied"

            if not granted_transaction:
                return _request_result("rejected", request, reason_code="CREDIT_NOT_FOUND")
            provenance = granted_transaction.get("provenance", {})
            granted_amount = int(granted_transaction.get("amount"))
            settled_decision_id = str(provenance.get("owner_decision_id") or decision_id)
            timestamp = _timestamp(now, "resolved_at")
            request.update(
                {
                    "status": STATUS_GRANTED,
                    "owner_decision": DECISION_GRANT,
                    "owner_decision_id": settled_decision_id,
                    "owner_session_id": normalized_owner_session,
                    "owner_turn_id": normalized_owner_turn,
                    "owner_message_id": normalized_owner_message,
                    "owner_source": normalized_owner_source,
                    "resolved_at": timestamp,
                    "granted_amount_jpy": granted_amount,
                    "transaction_id": granted_transaction["transaction_id"],
                }
            )
            updated = dict(state)
            updated["revision"] = state["revision"] + 1
            updated["updated_at"] = timestamp
            updated["requests"] = state["requests"]
            saved = _save(path, updated)
            verified = _load(path)
            final_request = _find_request(
                verified["requests"], request_id=normalized_request_id
            )
    except OwnerFundingError as exc:
        return _request_result(
            "failed", reason_code="OWNER_FUNDING_STATE_ERROR", error_type=type(exc).__name__
        )

    experience_status: Optional[str] = None
    try:
        experience = _economy_module(home).apply_owner_funding_experience(
            granted_transaction,
            hermes_home=home,
        )
        experience_status = experience.get("status")
    except Exception:
        experience_status = "failed"
    return _request_result(
        "already_granted" if already_granted else "granted",
        final_request,
        amount_jpy=granted_transaction.get("amount"),
        transaction_id=granted_transaction.get("transaction_id"),
        current_balance=granted_transaction.get("current_balance"),
        experience_status=experience_status,
    )


__all__ = [
    "DECISION_GRANT",
    "DECISION_REFUSE",
    "DELIVERY_DELIVERED",
    "DELIVERY_FAILED",
    "DELIVERY_PENDING",
    "FEATURE_ENV",
    "OwnerFundingError",
    "STATUS_FAILED_DELIVERY",
    "STATUS_GRANTED",
    "STATUS_OPEN",
    "STATUS_REFUSED",
    "feature_enabled",
    "finalize_delivery_binding",
    "get_request",
    "list_requests",
    "load_state",
    "prepare_delivery_binding",
    "request_owner_funds",
    "resolve_owner_fund_request",
    "state_path",
]
