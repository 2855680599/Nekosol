#!/usr/bin/env python3
"""Bounded Shiomi City property and economy foundation V1.

This module is an explicit, verified-Main-Self surface.  It keeps the one-time
World genesis grant, durable JPY income and purchase transactions, and
owned-item projections in one small locked state file.  Baseline support is
the only lazy income source and can materialize only through the approved
wallet observation seam; no read creates genesis state.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import tempfile
from collections.abc import Mapping, Sequence
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator, Optional
from zoneinfo import ZoneInfo

try:
    import fcntl
except ImportError:  # pragma: no cover - production is POSIX
    fcntl = None


FEATURE_ENV = "CHIYO_WORLD_PROPERTY_ECONOMY_ENABLED"
WORLD_FEATURE_ENV = "CHIYO_WORLD_LIVING_SKELETON_ENABLED"
DAILY_FEATURE_ENV = "CHIYO_WORLD_DAILY_RHYTHM_ENABLED"
BASELINE_INCOME_FEATURE_ENV = "CHIYO_WORLD_BASELINE_INCOME_ENABLED"
DELIVERY_FEATURE_ENV = "CHIYO_WORLD_DELIVERY_TIME_ENABLED"
WORLD_ID = "shiomi_city"
OWNER = "chiyo"
CURRENCY = "JPY"
MOVE_IN_GRANT = 10_000
GENESIS_SOURCE = "owner_approved_world_genesis"
GENESIS_KIND = "move_in_grant"
BASELINE_SUPPORT_SOURCE = "shiomi_support_fund"
BASELINE_SUPPORT_INCOME_KIND = "baseline_living_support"
BASELINE_SUPPORT_PROGRAM_ID = "shiomi-baseline-support-v1"
BASELINE_SUPPORT_TRANSACTION_KIND = "BASELINE_SUPPORT"
BASELINE_SUPPORT_AMOUNT = 3_000
OWNER_DECISION_CORRECTION_TRANSACTION_KIND = "OWNER_DECISION_CORRECTION"
OWNER_DECISION_CORRECTION_DIRECTION = "ADJUSTMENT"
BASELINE_SUPPORT_CORRECTION_KIND = "baseline_support_revocation"
BASELINE_SUPPORT_CORRECTION_SOURCE = "owner_decision_phase10"
BASELINE_SUPPORT_CORRECTION_REASON = "recurring_baseline_support_disabled"
OWNER_FUNDING_TRANSACTION_KIND = "OWNER_FUNDING"
OWNER_FUNDING_SOURCE = "owner"
OWNER_FUNDING_INCOME_KIND = "owner_funding"
BASELINE_MAX_CATCH_UP_PERIODS = 12
WORLD_TIMEZONE = "Asia/Tokyo"
STORE_LOCATION_ID = "home_goods_store"
HOME_ITEM_LOCATION = "home/bedroom"
DELIVERY_PENDING_LOCATION = "in_transit/home/bedroom"
DELIVERY_STATUS_PENDING = "PENDING"
DELIVERY_STATUS_DELIVERED = "DELIVERED"
DELIVERY_PROVENANCE = "temporal_home_delivery"
DELIVERY_DURATION_SECONDS = 2 * 60 * 60
SCHEMA_VERSION = "world.property.economy.v1"
PROJECTION_KIND = "shiomi_property_economy_projection"
WALLET_KIND = "shiomi_wallet_projection"
SHOP_KIND = "shiomi_home_goods_store_projection"
PURCHASE_KIND = "shiomi_item_purchase"
STATE_FILENAME = "world_property_economy.json"
MAX_ID_LENGTH = 128
MAX_TEXT_LENGTH = 160
MAX_TRANSACTIONS = 256
MAX_ITEMS = 256
MAX_RECENT_TRANSACTIONS = 8
DISPOSITION_OWNED = "OWNED"
DISPOSITION_GIFTED_AWAY = "GIFTED_AWAY"
DISPOSITION_DISCARDED = "DISCARDED"
VALID_DISPOSITIONS = frozenset(
    {DISPOSITION_OWNED, DISPOSITION_GIFTED_AWAY, DISPOSITION_DISCARDED}
)

#: M15: canonical object capability declarations (vocabulary from M15F
#: ``world_object_capability``).  Anything not declared here stays UNKNOWN,
#: and UNKNOWN denies PICK_UP -- a capability is never assumed.
_CAPABILITY_FIXED: dict[str, Any] = {"takeable": False, "portable": False, "placeable": True, "fixed": True}
_CAPABILITY_HANDHELD: dict[str, Any] = {"takeable": True, "portable": True, "placeable": True, "fixed": False}


CATALOG: tuple[dict[str, Any], ...] = (
    {
        "catalog_item_id": "basic_bed",
        "name": "基本床",
        "description": "一张简单的床",
        "price": 5_800,
        "capability": _CAPABILITY_FIXED,
    },
    {
        "catalog_item_id": "simple_desk",
        "name": "简易书桌",
        "description": "一张简单的书桌",
        "price": 4_200,
        "capability": _CAPABILITY_FIXED,
    },
    {
        "catalog_item_id": "simple_chair",
        "name": "简易椅子",
        "description": "一把简单的椅子",
        "price": 1_800,
        "capability": _CAPABILITY_FIXED,
    },
    {
        "catalog_item_id": "bedside_lamp",
        "name": "床头灯",
        "description": "一盏放在床边的小灯",
        "price": 900,
        "capability": _CAPABILITY_FIXED,
    },
    {
        "catalog_item_id": "small_bookshelf",
        "name": "小书架",
        "description": "一个小型书架",
        "price": 3_000,
        "capability": _CAPABILITY_FIXED,
    },
    {
        "catalog_item_id": "floor_cushion",
        "name": "地垫坐垫",
        "description": "一个放在地上的坐垫",
        "price": 700,
        "capability": _CAPABILITY_FIXED,
    },
)
_CATALOG_BY_ID = {item["catalog_item_id"]: item for item in CATALOG}


def object_capability(catalog_item_id: Any) -> dict[str, Any]:
    """M15: an object's canonical capability block (M15F vocabulary).

    An unknown object yields ``{}`` -- every capability then reads UNKNOWN and
    PICK_UP is denied.  This function names no object and grants no capability.
    """

    entry = _CATALOG_BY_ID.get(str(catalog_item_id))
    if not isinstance(entry, Mapping):
        return {}
    capability = entry.get("capability")
    return dict(capability) if isinstance(capability, Mapping) else {}

_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_STATE_KEYS = frozenset(
    {
        "schema_version",
        "owner",
        "currency",
        "revision",
        "updated_at",
        "starting_grant",
        "transactions",
        "items",
        "baseline_support",
        "deliveries",
    }
)
_GRANT_KEYS = frozenset(
    {"grant_id", "amount", "currency", "source", "kind", "created_at"}
)
_TRANSACTION_KEYS = frozenset(
    {
        "transaction_id",
        "transaction_kind",
        "direction",
        "amount",
        "currency",
        "occurred_at",
        "catalog_item_id",
        "purchase_intent_id",
        "delivery_location",
        "effective_period",
        "provenance",
    }
)
_ITEM_KEYS = frozenset(
    {
        "item_instance_id",
        "catalog_item_id",
        "owner",
        "acquired_at",
        "acquisition_kind",
        "acquisition_provenance",
        "acquisition_price",
        "currency",
        "current_location",
        "disposition",
    }
)
_DELIVERY_KEYS = frozenset(
    {
        "delivery_id",
        "purchase_transaction_id",
        "purchase_intent_id",
        "item_instance_id",
        "destination_location",
        "purchased_at",
        "expected_delivery_at",
        "delivered_at",
        "status",
        "delivery_provenance",
    }
)
_PROVENANCE_KEYS = frozenset(
    {
        "source",
        "purchase_intent_id",
        "transaction_id",
        "delivery",
        "source_turn_id",
        "source_message_id",
    }
)
_INCOME_PROVENANCE_KEYS = frozenset(
    {
        "source",
        "income_kind",
        "program_id",
        "effective_period",
        "support_start_period",
        "transaction_id",
    }
)
_OWNER_FUNDING_PROVENANCE_KEYS = frozenset(
    {
        "source",
        "income_kind",
        "request_id",
        "owner_decision_id",
        "transaction_id",
    }
)
_CORRECTION_PROVENANCE_KEYS = frozenset(
    {
        "source",
        "correction_kind",
        "corrects_transaction_id",
        "reason",
        "phase",
        "transaction_id",
    }
)
_BASELINE_SUPPORT_KEYS = frozenset(
    {
        "program_id",
        "source",
        "income_kind",
        "amount",
        "currency",
        "support_start_period",
    }
)
_PERIOD_RE = re.compile(r"^[1-9][0-9]{3}-(0[1-9]|1[0-2])$")


class PropertyEconomyError(ValueError):
    """A property/economy contract is malformed or unavailable."""


def _copy_json(value: Any) -> Any:
    try:
        return json.loads(
            json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
        )
    except (TypeError, ValueError) as exc:
        raise PropertyEconomyError("value is not bounded JSON") from exc


def _truthy(value: Any) -> bool:
    return str(value or "").strip().lower() in _TRUE_VALUES


def feature_enabled(environment: Optional[Mapping[str, str]] = None) -> bool:
    return _truthy((environment or os.environ).get(FEATURE_ENV))


def baseline_income_enabled(
    environment: Optional[Mapping[str, str]] = None,
) -> bool:
    return _truthy((environment or os.environ).get(BASELINE_INCOME_FEATURE_ENV))


def delivery_time_enabled(
    environment: Optional[Mapping[str, str]] = None,
) -> bool:
    return _truthy((environment or os.environ).get(DELIVERY_FEATURE_ENV))


def _home(hermes_home: Optional[Path | str] = None) -> Path:
    raw = hermes_home if hermes_home is not None else os.environ.get("HERMES_HOME")
    path = Path(raw).expanduser() if raw else Path.home() / ".hermes"
    if not path.is_absolute():
        raise PropertyEconomyError("hermes_home must be absolute")
    return path


def state_path(hermes_home: Optional[Path | str] = None) -> Path:
    return _home(hermes_home) / "state" / STATE_FILENAME


def _text(value: Any, field: str, maximum: int = MAX_TEXT_LENGTH) -> str:
    if not isinstance(value, str):
        raise PropertyEconomyError(f"{field} must be a string")
    result = value.strip()
    if (
        not result
        or len(result) > maximum
        or any(char in result for char in "\r\n\x00")
    ):
        raise PropertyEconomyError(f"{field} is invalid")
    return result


def apply_baseline_support_revocation(
    *,
    baseline_transaction_id: Any,
    hermes_home: Optional[Path | str] = None,
    now: Any = None,
) -> dict[str, Any]:
    """Append one stable owner-decision correction for a baseline credit."""

    try:
        target_id = _id(baseline_transaction_id, "baseline_transaction_id")
    except PropertyEconomyError:
        return {
            "kind": WALLET_KIND,
            "status": "rejected",
            "reason_code": "INVALID_CORRECTION_TARGET",
        }
    path = state_path(hermes_home)
    try:
        with _lock(path):
            state = _load(path)
            if state is None:
                return {
                    "kind": WALLET_KIND,
                    "status": "unavailable",
                    "reason_code": "GENESIS_NOT_INITIALIZED",
                }
            target = next(
                (
                    transaction
                    for transaction in state["transactions"]
                    if transaction["transaction_id"] == target_id
                ),
                None,
            )
            if target is None:
                return {
                    "kind": WALLET_KIND,
                    "status": "rejected",
                    "reason_code": "CORRECTION_TARGET_NOT_FOUND",
                }
            if (
                target["transaction_kind"] != BASELINE_SUPPORT_TRANSACTION_KIND
                or target["direction"] != "CREDIT"
                or target["amount"] != BASELINE_SUPPORT_AMOUNT
            ):
                return {
                    "kind": WALLET_KIND,
                    "status": "rejected",
                    "reason_code": "CORRECTION_TARGET_NOT_BASELINE_SUPPORT",
                }
            correction_id = _baseline_support_correction_id(target_id)
            existing = next(
                (
                    transaction
                    for transaction in state["transactions"]
                    if transaction["transaction_id"] == correction_id
                ),
                None,
            )
            if existing is not None:
                return {
                    "kind": WALLET_KIND,
                    "status": "already_applied",
                    "transaction_id": correction_id,
                    "corrected_transaction_id": target_id,
                    "amount": existing["amount"],
                    "current_balance": _balance(state),
                    "transaction": dict(existing),
                    "corrected_transaction": dict(target),
                }
            corrected_balance = _balance(state) - target["amount"]
            if corrected_balance < 0:
                return {
                    "kind": WALLET_KIND,
                    "status": "rejected",
                    "reason_code": "CORRECTION_WOULD_NEGATIVE_BALANCE",
                    "current_balance": _balance(state),
                }
            occurred_at = _timestamp(now, "occurred_at")
            transaction = {
                "transaction_id": correction_id,
                "transaction_kind": OWNER_DECISION_CORRECTION_TRANSACTION_KIND,
                "direction": OWNER_DECISION_CORRECTION_DIRECTION,
                "amount": -target["amount"],
                "currency": CURRENCY,
                "occurred_at": occurred_at,
                "provenance": {
                    "source": BASELINE_SUPPORT_CORRECTION_SOURCE,
                    "correction_kind": BASELINE_SUPPORT_CORRECTION_KIND,
                    "corrects_transaction_id": target_id,
                    "reason": BASELINE_SUPPORT_CORRECTION_REASON,
                    "phase": "phase10",
                    "transaction_id": correction_id,
                },
            }
            updated = dict(state)
            updated["revision"] = state["revision"] + 1
            updated["updated_at"] = occurred_at
            updated["transactions"] = [*state["transactions"], transaction]
            saved = _save(path, updated)
            verified = _load(path)
            if verified != saved:
                raise PropertyEconomyError("correction postcondition failed")
            verified_transaction = next(
                item
                for item in verified["transactions"]
                if item["transaction_id"] == correction_id
            )
            return {
                "kind": WALLET_KIND,
                "status": "applied",
                "transaction_id": correction_id,
                "corrected_transaction_id": target_id,
                "amount": verified_transaction["amount"],
                "current_balance": _balance(verified),
                "transaction": dict(verified_transaction),
                "corrected_transaction": dict(target),
            }
    except PropertyEconomyError as exc:
        return {
            "kind": WALLET_KIND,
            "status": "failed",
            "reason_code": "ECONOMY_STATE_ERROR",
            "error_type": type(exc).__name__,
        }


def _id(value: Any, field: str) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise PropertyEconomyError(f"{field} must be a string or integer")
    result = _text(str(value), field, MAX_ID_LENGTH)
    if not _ID_RE.fullmatch(result):
        raise PropertyEconomyError(f"{field} has invalid characters")
    return result


def _positive_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise PropertyEconomyError(f"{field} must be a positive integer")
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
            raise PropertyEconomyError(f"{field} must be ISO datetime") from exc
    if instant.tzinfo is None or instant.utcoffset() is None:
        raise PropertyEconomyError(f"{field} must include timezone")
    return instant.astimezone(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def _assert_exact_keys(value: Mapping[str, Any], allowed: frozenset[str], field: str) -> None:
    unknown = set(value) - allowed
    if unknown:
        raise PropertyEconomyError(f"{field} contains unknown fields")


def _validate_provenance(value: Any, field: str) -> dict[str, str]:
    if not isinstance(value, Mapping) or not value:
        raise PropertyEconomyError(f"{field} must be a non-empty object")
    _assert_exact_keys(value, _PROVENANCE_KEYS, field)
    result: dict[str, str] = {}
    for key, item in value.items():
        result[key] = _text(item, f"{field}.{key}", MAX_ID_LENGTH)
    if result.get("source") != "verified_main_self_buy_item":
        raise PropertyEconomyError(f"{field}.source is invalid")
    if result.get("delivery") != "included_home_delivery":
        raise PropertyEconomyError(f"{field}.delivery is invalid")
    _id(result.get("purchase_intent_id"), f"{field}.purchase_intent_id")
    _id(result.get("transaction_id"), f"{field}.transaction_id")
    return result


def _period_id(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise PropertyEconomyError(f"{field} must be a string")
    result = _text(value, field, 7)
    if not _PERIOD_RE.fullmatch(result):
        raise PropertyEconomyError(f"{field} must be YYYY-MM")
    return result


def _baseline_transaction_id(effective_period: str) -> str:
    material = "|".join(
        (OWNER, CURRENCY, BASELINE_SUPPORT_INCOME_KIND, effective_period)
    )
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()
    return f"baseline-support-{digest}"


def _baseline_support_correction_id(transaction_id: str) -> str:
    material = "|".join(
        (OWNER, CURRENCY, BASELINE_SUPPORT_CORRECTION_KIND, transaction_id)
    )
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()
    return f"baseline-support-correction-{digest}"


def _owner_funding_transaction_id(request_id: str, owner_decision_id: str) -> str:
    material = f"{OWNER}|{CURRENCY}|{OWNER_FUNDING_INCOME_KIND}|{request_id}|{owner_decision_id}"
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()
    return f"owner-funding-{digest}"


def _owner_decision_id(source: str, session_id: str, message_id: str) -> str:
    material = f"{source}|{session_id}|{message_id}"
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()
    return f"owner-decision-{digest}"


def _validate_income_provenance(value: Any, field: str) -> dict[str, str]:
    if not isinstance(value, Mapping) or not value:
        raise PropertyEconomyError(f"{field} must be a non-empty object")
    _assert_exact_keys(value, _INCOME_PROVENANCE_KEYS, field)
    if set(value) != _INCOME_PROVENANCE_KEYS:
        raise PropertyEconomyError(f"{field} is incomplete")
    result: dict[str, str] = {}
    for key, item in value.items():
        result[key] = _text(item, f"{field}.{key}", MAX_ID_LENGTH)
    if result["source"] != BASELINE_SUPPORT_SOURCE:
        raise PropertyEconomyError(f"{field}.source is invalid")
    if result["income_kind"] != BASELINE_SUPPORT_INCOME_KIND:
        raise PropertyEconomyError(f"{field}.income_kind is invalid")
    if result["program_id"] != BASELINE_SUPPORT_PROGRAM_ID:
        raise PropertyEconomyError(f"{field}.program_id is invalid")
    _period_id(result["effective_period"], f"{field}.effective_period")
    _period_id(result["support_start_period"], f"{field}.support_start_period")
    _id(result["transaction_id"], f"{field}.transaction_id")
    return result


def _validate_owner_funding_provenance(value: Any, field: str) -> dict[str, str]:
    if not isinstance(value, Mapping) or not value:
        raise PropertyEconomyError(f"{field} must be a non-empty object")
    _assert_exact_keys(value, _OWNER_FUNDING_PROVENANCE_KEYS, field)
    if set(value) != _OWNER_FUNDING_PROVENANCE_KEYS:
        raise PropertyEconomyError(f"{field} is incomplete")
    result: dict[str, str] = {}
    for key, item in value.items():
        result[key] = _text(item, f"{field}.{key}", MAX_ID_LENGTH)
    if result["source"] != OWNER_FUNDING_SOURCE:
        raise PropertyEconomyError(f"{field}.source is invalid")
    if result["income_kind"] != OWNER_FUNDING_INCOME_KIND:
        raise PropertyEconomyError(f"{field}.income_kind is invalid")
    _id(result["request_id"], f"{field}.request_id")
    _id(result["owner_decision_id"], f"{field}.owner_decision_id")
    _id(result["transaction_id"], f"{field}.transaction_id")
    return result


def _validate_correction_provenance(value: Any, field: str) -> dict[str, str]:
    if not isinstance(value, Mapping) or not value:
        raise PropertyEconomyError(f"{field} must be a non-empty object")
    _assert_exact_keys(value, _CORRECTION_PROVENANCE_KEYS, field)
    if set(value) != _CORRECTION_PROVENANCE_KEYS:
        raise PropertyEconomyError(f"{field} is incomplete")
    result: dict[str, str] = {}
    for key, item in value.items():
        result[key] = _text(item, f"{field}.{key}", MAX_ID_LENGTH)
    if result["source"] != BASELINE_SUPPORT_CORRECTION_SOURCE:
        raise PropertyEconomyError(f"{field}.source is invalid")
    if result["correction_kind"] != BASELINE_SUPPORT_CORRECTION_KIND:
        raise PropertyEconomyError(f"{field}.correction_kind is invalid")
    if result["reason"] != BASELINE_SUPPORT_CORRECTION_REASON:
        raise PropertyEconomyError(f"{field}.reason is invalid")
    if result["phase"] != "phase10":
        raise PropertyEconomyError(f"{field}.phase is invalid")
    _id(result["corrects_transaction_id"], f"{field}.corrects_transaction_id")
    _id(result["transaction_id"], f"{field}.transaction_id")
    return result


def _validate_baseline_support(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise PropertyEconomyError("baseline_support must be an object")
    _assert_exact_keys(value, _BASELINE_SUPPORT_KEYS, "baseline_support")
    if set(value) != _BASELINE_SUPPORT_KEYS:
        raise PropertyEconomyError("baseline_support is incomplete")
    if value.get("program_id") != BASELINE_SUPPORT_PROGRAM_ID:
        raise PropertyEconomyError("baseline_support.program_id is invalid")
    if value.get("source") != BASELINE_SUPPORT_SOURCE:
        raise PropertyEconomyError("baseline_support.source is invalid")
    if value.get("income_kind") != BASELINE_SUPPORT_INCOME_KIND:
        raise PropertyEconomyError("baseline_support.income_kind is invalid")
    if _positive_int(value.get("amount"), "baseline_support.amount") != BASELINE_SUPPORT_AMOUNT:
        raise PropertyEconomyError("baseline_support.amount is invalid")
    if value.get("currency") != CURRENCY:
        raise PropertyEconomyError("baseline_support.currency is invalid")
    return {
        "program_id": BASELINE_SUPPORT_PROGRAM_ID,
        "source": BASELINE_SUPPORT_SOURCE,
        "income_kind": BASELINE_SUPPORT_INCOME_KIND,
        "amount": BASELINE_SUPPORT_AMOUNT,
        "currency": CURRENCY,
        "support_start_period": _period_id(
            value.get("support_start_period"),
            "baseline_support.support_start_period",
        ),
    }


def _validate_grant(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise PropertyEconomyError("starting_grant must be an object")
    _assert_exact_keys(value, _GRANT_KEYS, "starting_grant")
    if _id(value.get("grant_id"), "starting_grant.grant_id") != "move-in-grant":
        raise PropertyEconomyError("starting_grant.grant_id is invalid")
    if _positive_int(value.get("amount"), "starting_grant.amount") != MOVE_IN_GRANT:
        raise PropertyEconomyError("starting_grant.amount is invalid")
    if value.get("currency") != CURRENCY:
        raise PropertyEconomyError("starting_grant.currency is invalid")
    if value.get("source") != GENESIS_SOURCE:
        raise PropertyEconomyError("starting_grant.source is invalid")
    if value.get("kind") != GENESIS_KIND:
        raise PropertyEconomyError("starting_grant.kind is invalid")
    return {
        "grant_id": "move-in-grant",
        "amount": MOVE_IN_GRANT,
        "currency": CURRENCY,
        "source": GENESIS_SOURCE,
        "kind": GENESIS_KIND,
        "created_at": _timestamp(value.get("created_at"), "starting_grant.created_at"),
    }


def _validate_transaction(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise PropertyEconomyError("transaction must be an object")
    _assert_exact_keys(value, _TRANSACTION_KEYS, "transaction")
    transaction_id = _id(value.get("transaction_id"), "transaction.transaction_id")
    transaction_kind = value.get("transaction_kind")
    direction = value.get("direction")
    raw_amount = value.get("amount")
    if value.get("currency") != CURRENCY:
        raise PropertyEconomyError("transaction.currency is invalid")
    occurred_at = _timestamp(value.get("occurred_at"), "transaction.occurred_at")
    if (
        transaction_kind == OWNER_DECISION_CORRECTION_TRANSACTION_KIND
        and direction == OWNER_DECISION_CORRECTION_DIRECTION
    ):
        if any(
            field in value
            for field in (
                "catalog_item_id",
                "purchase_intent_id",
                "delivery_location",
                "effective_period",
            )
        ):
            raise PropertyEconomyError("correction transaction has income/purchase fields")
        if (
            isinstance(raw_amount, bool)
            or not isinstance(raw_amount, int)
            or raw_amount != -BASELINE_SUPPORT_AMOUNT
        ):
            raise PropertyEconomyError("baseline support correction amount is invalid")
        provenance = _validate_correction_provenance(
            value.get("provenance"), "transaction.provenance"
        )
        if provenance["transaction_id"] != transaction_id:
            raise PropertyEconomyError("correction transaction identity mismatch")
        if transaction_id != _baseline_support_correction_id(
            provenance["corrects_transaction_id"]
        ):
            raise PropertyEconomyError("correction transaction identity is not stable")
        return {
            "transaction_id": transaction_id,
            "transaction_kind": OWNER_DECISION_CORRECTION_TRANSACTION_KIND,
            "direction": OWNER_DECISION_CORRECTION_DIRECTION,
            "amount": raw_amount,
            "currency": CURRENCY,
            "occurred_at": occurred_at,
            "provenance": provenance,
        }

    amount = _positive_int(raw_amount, "transaction.amount")

    if transaction_kind == "PURCHASE" and direction == "DEBIT":
        if "effective_period" in value:
            raise PropertyEconomyError("purchase transaction has income fields")
        catalog_item_id = _id(
            value.get("catalog_item_id"), "transaction.catalog_item_id"
        )
        if catalog_item_id not in _CATALOG_BY_ID:
            raise PropertyEconomyError("transaction.catalog_item_id is unknown")
        if value.get("delivery_location") != HOME_ITEM_LOCATION:
            raise PropertyEconomyError("transaction.delivery_location is invalid")
        return {
            "transaction_id": transaction_id,
            "transaction_kind": "PURCHASE",
            "direction": "DEBIT",
            "amount": amount,
            "currency": CURRENCY,
            "occurred_at": occurred_at,
            "catalog_item_id": catalog_item_id,
            "purchase_intent_id": _id(
                value.get("purchase_intent_id"), "transaction.purchase_intent_id"
            ),
            "delivery_location": HOME_ITEM_LOCATION,
            "provenance": _validate_provenance(
                value.get("provenance"), "transaction.provenance"
            ),
        }

    if transaction_kind == OWNER_FUNDING_TRANSACTION_KIND and direction == "CREDIT":
        if any(
            field in value
            for field in (
                "catalog_item_id",
                "purchase_intent_id",
                "delivery_location",
                "effective_period",
            )
        ):
            raise PropertyEconomyError("owner funding transaction has purchase/period fields")
        provenance = _validate_owner_funding_provenance(
            value.get("provenance"), "transaction.provenance"
        )
        if provenance["transaction_id"] != transaction_id:
            raise PropertyEconomyError("owner funding transaction identity mismatch")
        if transaction_id != _owner_funding_transaction_id(
            provenance["request_id"], provenance["owner_decision_id"]
        ):
            raise PropertyEconomyError("owner funding transaction identity is not stable")
        return {
            "transaction_id": transaction_id,
            "transaction_kind": OWNER_FUNDING_TRANSACTION_KIND,
            "direction": "CREDIT",
            "amount": amount,
            "currency": CURRENCY,
            "occurred_at": occurred_at,
            "provenance": provenance,
        }

    if (
        transaction_kind == BASELINE_SUPPORT_TRANSACTION_KIND
        and direction == "CREDIT"
    ):
        if any(
            field in value
            for field in ("catalog_item_id", "purchase_intent_id", "delivery_location")
        ):
            raise PropertyEconomyError("income transaction has purchase fields")
        if amount != BASELINE_SUPPORT_AMOUNT:
            raise PropertyEconomyError("baseline support amount is invalid")
        effective_period = _period_id(
            value.get("effective_period"), "transaction.effective_period"
        )
        provenance = _validate_income_provenance(
            value.get("provenance"), "transaction.provenance"
        )
        if provenance["effective_period"] != effective_period:
            raise PropertyEconomyError("income period provenance mismatch")
        if provenance["transaction_id"] != transaction_id:
            raise PropertyEconomyError("income transaction identity mismatch")
        if transaction_id != _baseline_transaction_id(effective_period):
            raise PropertyEconomyError("income transaction identity is not stable")
        if provenance["support_start_period"] > effective_period:
            raise PropertyEconomyError("income period precedes support start")
        return {
            "transaction_id": transaction_id,
            "transaction_kind": BASELINE_SUPPORT_TRANSACTION_KIND,
            "direction": "CREDIT",
            "amount": amount,
            "currency": CURRENCY,
            "occurred_at": occurred_at,
            "effective_period": effective_period,
            "provenance": provenance,
        }

    raise PropertyEconomyError("transaction kind/direction is invalid")


def _validate_item(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise PropertyEconomyError("item must be an object")
    _assert_exact_keys(value, _ITEM_KEYS, "item")
    catalog_item_id = _id(value.get("catalog_item_id"), "item.catalog_item_id")
    if catalog_item_id not in _CATALOG_BY_ID:
        raise PropertyEconomyError("item.catalog_item_id is unknown")
    if value.get("owner") != OWNER:
        raise PropertyEconomyError("item.owner is invalid")
    if value.get("acquisition_kind") != "PURCHASE":
        raise PropertyEconomyError("item.acquisition_kind is invalid")
    if value.get("currency") != CURRENCY:
        raise PropertyEconomyError("item.currency is invalid")
    if value.get("current_location") not in {
        HOME_ITEM_LOCATION,
        DELIVERY_PENDING_LOCATION,
    }:
        raise PropertyEconomyError("item.current_location is invalid")
    disposition = value.get("disposition")
    if disposition not in VALID_DISPOSITIONS:
        raise PropertyEconomyError("item.disposition is invalid")
    return {
        "item_instance_id": _id(value.get("item_instance_id"), "item.item_instance_id"),
        "catalog_item_id": catalog_item_id,
        "owner": OWNER,
        "acquired_at": _timestamp(value.get("acquired_at"), "item.acquired_at"),
        "acquisition_kind": "PURCHASE",
        "acquisition_provenance": _validate_provenance(
            value.get("acquisition_provenance"), "item.acquisition_provenance"
        ),
        "acquisition_price": _positive_int(
            value.get("acquisition_price"), "item.acquisition_price"
        ),
        "currency": CURRENCY,
        "current_location": value["current_location"],
        "disposition": disposition,
    }


def _validate_delivery(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise PropertyEconomyError("delivery must be an object")
    _assert_exact_keys(value, _DELIVERY_KEYS, "delivery")
    delivery_id = _id(value.get("delivery_id"), "delivery.delivery_id")
    transaction_id = _id(
        value.get("purchase_transaction_id"),
        "delivery.purchase_transaction_id",
    )
    purchase_intent_id = _id(
        value.get("purchase_intent_id"),
        "delivery.purchase_intent_id",
    )
    item_instance_id = _id(
        value.get("item_instance_id"),
        "delivery.item_instance_id",
    )
    if value.get("destination_location") != HOME_ITEM_LOCATION:
        raise PropertyEconomyError("delivery.destination_location is invalid")
    purchased_at = _timestamp(value.get("purchased_at"), "delivery.purchased_at")
    expected = _timestamp(
        value.get("expected_delivery_at"),
        "delivery.expected_delivery_at",
    )
    purchased_instant = datetime.fromisoformat(purchased_at.replace("Z", "+00:00"))
    expected_instant = datetime.fromisoformat(expected.replace("Z", "+00:00"))
    if expected_instant <= purchased_instant:
        raise PropertyEconomyError("delivery ETA must be later than purchase")
    status = value.get("status")
    if status not in {DELIVERY_STATUS_PENDING, DELIVERY_STATUS_DELIVERED}:
        raise PropertyEconomyError("delivery.status is invalid")
    delivered_at = value.get("delivered_at")
    if status == DELIVERY_STATUS_PENDING:
        if delivered_at is not None:
            raise PropertyEconomyError("pending delivery cannot have delivered_at")
    else:
        if delivered_at is None:
            raise PropertyEconomyError("delivered delivery must have delivered_at")
        delivered_at = _timestamp(delivered_at, "delivery.delivered_at")
        delivered_instant = datetime.fromisoformat(
            delivered_at.replace("Z", "+00:00")
        )
        if delivered_instant < expected_instant:
            raise PropertyEconomyError("delivery completed before ETA")
    if value.get("delivery_provenance") != DELIVERY_PROVENANCE:
        raise PropertyEconomyError("delivery provenance is invalid")
    return {
        "delivery_id": delivery_id,
        "purchase_transaction_id": transaction_id,
        "purchase_intent_id": purchase_intent_id,
        "item_instance_id": item_instance_id,
        "destination_location": HOME_ITEM_LOCATION,
        "purchased_at": purchased_at,
        "expected_delivery_at": expected,
        "delivered_at": delivered_at,
        "status": status,
        "delivery_provenance": DELIVERY_PROVENANCE,
    }


def validate_state(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise PropertyEconomyError("economy state must be an object")
    _assert_exact_keys(value, _STATE_KEYS, "economy state")
    if value.get("schema_version") != SCHEMA_VERSION:
        raise PropertyEconomyError("economy schema is unsupported")
    if value.get("owner") != OWNER or value.get("currency") != CURRENCY:
        raise PropertyEconomyError("economy identity is invalid")
    revision = value.get("revision")
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
        raise PropertyEconomyError("economy revision is invalid")
    starting_grant = _validate_grant(value.get("starting_grant"))
    baseline_support = (
        _validate_baseline_support(value["baseline_support"])
        if "baseline_support" in value
        else None
    )
    raw_transactions = value.get("transactions")
    raw_items = value.get("items")
    raw_deliveries = value.get("deliveries", [])
    if (
        not isinstance(raw_transactions, Sequence)
        or isinstance(raw_transactions, (str, bytes, bytearray))
        or len(raw_transactions) > MAX_TRANSACTIONS
    ):
        raise PropertyEconomyError("transactions are invalid")
    if (
        not isinstance(raw_items, Sequence)
        or isinstance(raw_items, (str, bytes, bytearray))
        or len(raw_items) > MAX_ITEMS
    ):
        raise PropertyEconomyError("items are invalid")
    if (
        not isinstance(raw_deliveries, Sequence)
        or isinstance(raw_deliveries, (str, bytes, bytearray))
        or len(raw_deliveries) > MAX_ITEMS
    ):
        raise PropertyEconomyError("deliveries are invalid")
    transactions = [_validate_transaction(item) for item in raw_transactions]
    items = [_validate_item(item) for item in raw_items]
    deliveries = [_validate_delivery(item) for item in raw_deliveries]
    transaction_ids = [item["transaction_id"] for item in transactions]
    debit_transactions = [
        item for item in transactions if item["direction"] == "DEBIT"
    ]
    credit_transactions = [
        item for item in transactions if item["direction"] == "CREDIT"
    ]
    correction_transactions = [
        item
        for item in transactions
        if item["direction"] == OWNER_DECISION_CORRECTION_DIRECTION
    ]
    intent_ids = [item["purchase_intent_id"] for item in debit_transactions]
    baseline_credit_transactions = [
        item
        for item in credit_transactions
        if item["transaction_kind"] == BASELINE_SUPPORT_TRANSACTION_KIND
    ]
    owner_funding_transactions = [
        item
        for item in credit_transactions
        if item["transaction_kind"] == OWNER_FUNDING_TRANSACTION_KIND
    ]
    credit_periods = [item["effective_period"] for item in baseline_credit_transactions]
    owner_request_ids = [
        item["provenance"]["request_id"] for item in owner_funding_transactions
    ]
    item_ids = [item["item_instance_id"] for item in items]
    delivery_ids = [item["delivery_id"] for item in deliveries]
    delivery_intents = [item["purchase_intent_id"] for item in deliveries]
    delivery_items = [item["item_instance_id"] for item in deliveries]
    correction_target_ids = [
        item["provenance"]["corrects_transaction_id"]
        for item in correction_transactions
    ]
    if len(transaction_ids) != len(set(transaction_ids)):
        raise PropertyEconomyError("duplicate transaction_id")
    if len(correction_target_ids) != len(set(correction_target_ids)):
        raise PropertyEconomyError("duplicate corrected transaction")
    if len(intent_ids) != len(set(intent_ids)):
        raise PropertyEconomyError("duplicate purchase_intent_id")
    if len(credit_periods) != len(set(credit_periods)):
        raise PropertyEconomyError("duplicate effective_period")
    if len(owner_request_ids) != len(set(owner_request_ids)):
        raise PropertyEconomyError("duplicate owner funding request_id")
    if len(item_ids) != len(set(item_ids)):
        raise PropertyEconomyError("duplicate item_instance_id")
    if len(delivery_ids) != len(set(delivery_ids)):
        raise PropertyEconomyError("duplicate delivery_id")
    if len(delivery_intents) != len(set(delivery_intents)):
        raise PropertyEconomyError("duplicate delivery purchase_intent_id")
    if len(delivery_items) != len(set(delivery_items)):
        raise PropertyEconomyError("duplicate delivery item_instance_id")
    transaction_by_id = {item["transaction_id"]: item for item in transactions}
    item_by_id = {item["item_instance_id"]: item for item in items}
    for delivery in deliveries:
        transaction = transaction_by_id.get(delivery["purchase_transaction_id"])
        item = item_by_id.get(delivery["item_instance_id"])
        if (
            transaction is None
            or transaction["transaction_kind"] != "PURCHASE"
            or transaction["purchase_intent_id"] != delivery["purchase_intent_id"]
        ):
            raise PropertyEconomyError("delivery purchase transaction is invalid")
        if item is None or item["catalog_item_id"] != transaction["catalog_item_id"]:
            raise PropertyEconomyError("delivery item is invalid")
        expected_location = (
            DELIVERY_PENDING_LOCATION
            if delivery["status"] == DELIVERY_STATUS_PENDING
            else HOME_ITEM_LOCATION
        )
        if item["current_location"] != expected_location:
            raise PropertyEconomyError("delivery/item location mismatch")
    for correction in correction_transactions:
        target_id = correction["provenance"]["corrects_transaction_id"]
        target = transaction_by_id.get(target_id)
        if target is None:
            raise PropertyEconomyError("correction target is missing")
        if (
            target["transaction_kind"] != BASELINE_SUPPORT_TRANSACTION_KIND
            or target["direction"] != "CREDIT"
        ):
            raise PropertyEconomyError("correction target is not baseline support")
        if correction["amount"] != -target["amount"]:
            raise PropertyEconomyError("correction amount does not reverse target")
    if len(debit_transactions) != len(items):
        raise PropertyEconomyError("transaction/item cardinality is invalid")
    if baseline_credit_transactions and baseline_support is None:
        raise PropertyEconomyError("baseline income transactions require baseline_support")
    if baseline_support is not None:
        for transaction in baseline_credit_transactions:
            if (
                transaction["provenance"]["support_start_period"]
                != baseline_support["support_start_period"]
            ):
                raise PropertyEconomyError("income support start provenance mismatch")
    total_credits = sum(item["amount"] for item in credit_transactions)
    total_debits = sum(item["amount"] for item in debit_transactions)
    if starting_grant["amount"] + total_credits - total_debits < 0:
        raise PropertyEconomyError("economy balance is negative")
    normalized = {
        "schema_version": SCHEMA_VERSION,
        "owner": OWNER,
        "currency": CURRENCY,
        "revision": revision,
        "updated_at": _timestamp(value.get("updated_at"), "updated_at"),
        "starting_grant": starting_grant,
        "transactions": transactions,
        "items": items,
    }
    if baseline_support is not None:
        normalized["baseline_support"] = baseline_support
    if "deliveries" in value:
        normalized["deliveries"] = deliveries
    return normalized


def _load(path: Path) -> Optional[dict[str, Any]]:
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return validate_state(raw)
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise PropertyEconomyError("economy state cannot be read") from exc


def load_state(hermes_home: Optional[Path | str] = None) -> Optional[dict[str, Any]]:
    """Read state without creating a directory, lock, or grant."""

    return _load(state_path(hermes_home))


def _save(path: Path, state: Mapping[str, Any]) -> dict[str, Any]:
    normalized = validate_state(state)
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
        raise PropertyEconomyError("economy state could not be atomically written") from exc
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
        raise PropertyEconomyError("economy lock unavailable")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_name(path.name + ".lock").open("a+") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _empty_state(created_at: Any = None) -> dict[str, Any]:
    timestamp = _timestamp(created_at, "created_at")
    return {
        "schema_version": SCHEMA_VERSION,
        "owner": OWNER,
        "currency": CURRENCY,
        "revision": 1,
        "updated_at": timestamp,
        "starting_grant": {
            "grant_id": "move-in-grant",
            "amount": MOVE_IN_GRANT,
            "currency": CURRENCY,
            "source": GENESIS_SOURCE,
            "kind": GENESIS_KIND,
            "created_at": timestamp,
        },
        "transactions": [],
        "items": [],
    }


def initialize_genesis(
    *,
    hermes_home: Optional[Path | str] = None,
    environment: Optional[Mapping[str, str]] = None,
    created_at: Any = None,
) -> dict[str, Any]:
    """Materialize the owner-approved one-time grant, never on a read."""

    if not feature_enabled(environment):
        return {
            "kind": PROJECTION_KIND,
            "status": "disabled",
            "reason_code": "FEATURE_DISABLED",
        }
    path = state_path(hermes_home)
    with _lock(path):
        existing = _load(path)
        if existing is not None:
            return {
                "kind": PROJECTION_KIND,
                "status": "already_initialized",
                "reason_code": "GENESIS_ALREADY_PRESENT",
                "revision": existing["revision"],
                "starting_grant": MOVE_IN_GRANT,
            }
        saved = _save(path, _empty_state(created_at))
        verified = _load(path)
        if verified != saved:
            raise PropertyEconomyError("genesis postcondition failed")
    return {
        "kind": PROJECTION_KIND,
        "status": "created",
        "reason_code": "MOVE_IN_GRANT_CREATED",
        "revision": verified["revision"],
        "starting_grant": MOVE_IN_GRANT,
        "provenance": {
            "source": GENESIS_SOURCE,
            "kind": GENESIS_KIND,
        },
    }


def _world_local_period(now: Any = None) -> str:
    observed_at = _timestamp(now, "now")
    instant = datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
    return instant.astimezone(ZoneInfo(WORLD_TIMEZONE)).strftime("%Y-%m")


def _iter_periods(start: str, end: str) -> Iterator[str]:
    year, month = (int(part) for part in start.split("-"))
    end_year, end_month = (int(part) for part in end.split("-"))
    while (year, month) <= (end_year, end_month):
        yield f"{year:04d}-{month:02d}"
        month += 1
        if month == 13:
            year += 1
            month = 1


def _baseline_support_config(support_start_period: str) -> dict[str, Any]:
    return {
        "program_id": BASELINE_SUPPORT_PROGRAM_ID,
        "source": BASELINE_SUPPORT_SOURCE,
        "income_kind": BASELINE_SUPPORT_INCOME_KIND,
        "amount": BASELINE_SUPPORT_AMOUNT,
        "currency": CURRENCY,
        "support_start_period": support_start_period,
    }


def _realize_baseline_support(
    *,
    hermes_home: Optional[Path | str] = None,
    environment: Optional[Mapping[str, str]] = None,
    now: Any = None,
) -> list[dict[str, Any]]:
    if not baseline_income_enabled(environment):
        return []
    path = state_path(hermes_home)
    if not path.exists():
        return []
    observed_at = _timestamp(now, "now")
    current_period = _world_local_period(observed_at)
    realized: list[dict[str, Any]] = []
    with _lock(path):
        state = _load(path)
        if state is None:
            return []
        support = state.get("baseline_support")
        support_was_new = support is None
        if support is None:
            support = _baseline_support_config(current_period)
        support_start_period = support["support_start_period"]
        existing_periods = {
            transaction["effective_period"]
            for transaction in state["transactions"]
            if transaction["direction"] == "CREDIT"
        }
        for effective_period in _iter_periods(
            support_start_period,
            current_period,
        ):
            if effective_period in existing_periods:
                continue
            if len(realized) >= BASELINE_MAX_CATCH_UP_PERIODS:
                break
            transaction_id = _baseline_transaction_id(effective_period)
            transaction = {
                "transaction_id": transaction_id,
                "transaction_kind": BASELINE_SUPPORT_TRANSACTION_KIND,
                "direction": "CREDIT",
                "amount": BASELINE_SUPPORT_AMOUNT,
                "currency": CURRENCY,
                "occurred_at": observed_at,
                "effective_period": effective_period,
                "provenance": {
                    "source": BASELINE_SUPPORT_SOURCE,
                    "income_kind": BASELINE_SUPPORT_INCOME_KIND,
                    "program_id": BASELINE_SUPPORT_PROGRAM_ID,
                    "effective_period": effective_period,
                    "support_start_period": support_start_period,
                    "transaction_id": transaction_id,
                },
            }
            realized.append(transaction)
        if not support_was_new and not realized:
            return []
        updated = dict(state)
        updated["baseline_support"] = support
        if realized:
            updated["revision"] = state["revision"] + len(realized)
            updated["updated_at"] = observed_at
            updated["transactions"] = [*state["transactions"], *realized]
        else:
            updated["revision"] = state["revision"] + 1
            updated["updated_at"] = observed_at
        saved = _save(path, updated)
        verified = _load(path)
        if verified != saved:
            raise PropertyEconomyError("baseline support postcondition failed")
        if not realized:
            return []
        return verified["transactions"][-len(realized) :]


def _balance(state: Mapping[str, Any]) -> int:
    grant = int(state["starting_grant"]["amount"])
    return grant + sum(
        int(item["amount"])
        if item["direction"] == "CREDIT"
        else -int(item["amount"])
        if item["direction"] == "DEBIT"
        else int(item["amount"])
        for item in state["transactions"]
    )


def _transaction_projection(transaction: Mapping[str, Any]) -> dict[str, Any]:
    result = {
        "transaction_id": transaction["transaction_id"],
        "transaction_kind": transaction["transaction_kind"],
        "direction": transaction["direction"],
        "amount": transaction["amount"],
        "currency": transaction["currency"],
        "occurred_at": transaction["occurred_at"],
    }
    if transaction["direction"] == "DEBIT":
        result["catalog_item_id"] = transaction["catalog_item_id"]
        result["delivery_location"] = transaction["delivery_location"]
    elif transaction["transaction_kind"] == OWNER_FUNDING_TRANSACTION_KIND:
        result["request_id"] = transaction["provenance"]["request_id"]
        result["owner_decision_id"] = transaction["provenance"]["owner_decision_id"]
        result["income_kind"] = transaction["provenance"]["income_kind"]
    elif transaction["direction"] == "CREDIT":
        result["effective_period"] = transaction["effective_period"]
    else:
        result["corrects_transaction_id"] = transaction["provenance"][
            "corrects_transaction_id"
        ]
        result["correction_reason"] = transaction["provenance"]["reason"]
    return result


def _item_projection(item: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "item_instance_id": item["item_instance_id"],
        "catalog_item_id": item["catalog_item_id"],
        "owner": item["owner"],
        "acquired_at": item["acquired_at"],
        "acquisition_kind": item["acquisition_kind"],
        "acquisition_provenance": dict(item["acquisition_provenance"]),
        "acquisition_price": item["acquisition_price"],
        "currency": item["currency"],
        "current_location": item["current_location"],
        "disposition": item["disposition"],
    }


def _delivery_projection(delivery: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "delivery_id": delivery["delivery_id"],
        "purchase_intent_id": delivery["purchase_intent_id"],
        "item_instance_id": delivery["item_instance_id"],
        "destination_location": delivery["destination_location"],
        "purchased_at": delivery["purchased_at"],
        "expected_delivery_at": delivery["expected_delivery_at"],
        "delivered_at": delivery["delivered_at"],
        "status": delivery["status"],
        "delivery_provenance": delivery["delivery_provenance"],
    }


def _build_delivery_source_event(
    delivery: Mapping[str, Any],
    *,
    hermes_home: Optional[Path | str] = None,
) -> Any:
    normalized = _validate_delivery(delivery)
    if normalized["status"] != DELIVERY_STATUS_DELIVERED:
        raise PropertyEconomyError("delivery is not complete")
    import sys

    scripts_dir = str(_home(hermes_home) / "scripts")
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)
    import experience_continuity as ec

    return ec.SourceEvent.create(
        source="shiomi_world_property_economy",
        source_event_id=f"delivery-{normalized['delivery_id']}",
        occurred_at=normalized["delivered_at"],
        event_type="world_item_delivery",
        summary=f"物品 {normalized['item_instance_id']} 已送达家中",
        facts={
            "delivery_id": normalized["delivery_id"],
            "purchase_transaction_id": normalized["purchase_transaction_id"],
            "item_instance_id": normalized["item_instance_id"],
            "destination_location": normalized["destination_location"],
            "delivery_status": normalized["status"],
        },
        provenance={
            "created_by": "shiomi_world_property_economy",
            "source_ref": "temporal_home_delivery",
            "external_id": normalized["delivery_id"],
        },
        reality_domain="chiyo_world",
    )


def apply_delivery_experience(
    delivery: Mapping[str, Any],
    *,
    hermes_home: Optional[Path | str] = None,
) -> dict[str, Any]:
    """Project one committed delivery fact; it never changes the ledger."""

    try:
        import sys

        home = _home(hermes_home)
        scripts_dir = str(home / "scripts")
        if scripts_dir not in sys.path:
            sys.path.insert(0, scripts_dir)
        import experience_continuity as ec

        event = _build_delivery_source_event(delivery, hermes_home=home)
        store = ec.ExperienceStore(ec.default_store_path(home))
        applied = ec.ingest_source_event(event, store=store, confirm_apply=True)
        return {
            "status": applied.get("status"),
            "source_event_id": event.source_event_id,
            "canonical_event_key": ec.canonical_event_key_for(event),
        }
    except Exception as exc:
        return {
            "status": "failed",
            "reason_code": "EXPERIENCE_APPLY_FAILED",
            "error_type": type(exc).__name__,
        }


def _realize_due_deliveries(
    *,
    hermes_home: Optional[Path | str] = None,
    environment: Optional[Mapping[str, str]] = None,
    now: Any = None,
) -> list[dict[str, Any]]:
    if not delivery_time_enabled(environment):
        return []
    path = state_path(hermes_home)
    if not path.exists():
        return []
    observed_at = _timestamp(now, "now")
    instant = datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
    completed: list[dict[str, Any]] = []
    try:
        with _lock(path):
            state = _load(path)
            if state is None:
                return []
            deliveries = list(state.get("deliveries", []))
            due = [
                delivery
                for delivery in deliveries
                if (
                    delivery["status"] == DELIVERY_STATUS_PENDING
                    and datetime.fromisoformat(
                        delivery["expected_delivery_at"].replace("Z", "+00:00")
                    )
                    <= instant
                )
            ]
            if not due:
                return []
            due_ids = {delivery["delivery_id"] for delivery in due}
            updated = dict(state)
            updated["revision"] = state["revision"] + len(due)
            updated["updated_at"] = observed_at
            updated["deliveries"] = [
                {
                    **delivery,
                    "delivered_at": observed_at,
                    "status": DELIVERY_STATUS_DELIVERED,
                }
                if delivery["delivery_id"] in due_ids
                else delivery
                for delivery in deliveries
            ]
            updated["items"] = [
                {
                    **item,
                    "current_location": HOME_ITEM_LOCATION,
                }
                if item["item_instance_id"] in {
                    delivery["item_instance_id"] for delivery in due
                }
                else item
                for item in state["items"]
            ]
            saved = _save(path, updated)
            verified = _load(path)
            if verified != saved:
                raise PropertyEconomyError("delivery postcondition failed")
            completed = [
                dict(delivery)
                for delivery in verified.get("deliveries", [])
                if delivery["delivery_id"] in due_ids
            ]
    except PropertyEconomyError:
        return []
    for delivery in completed:
        try:
            apply_delivery_experience(delivery, hermes_home=hermes_home)
        except Exception:
            pass
    return completed


def wallet_projection(
    *,
    hermes_home: Optional[Path | str] = None,
    environment: Optional[Mapping[str, str]] = None,
    now: Any = None,
) -> dict[str, Any]:
    if not feature_enabled(environment):
        return {"kind": WALLET_KIND, "status": "disabled", "reason_code": "FEATURE_DISABLED"}
    if baseline_income_enabled(environment):
        _realize_baseline_support(
            hermes_home=hermes_home,
            environment=environment,
            now=now,
        )
    state = load_state(hermes_home)
    if state is None:
        return {"kind": WALLET_KIND, "status": "unavailable", "reason_code": "GENESIS_NOT_INITIALIZED"}
    if baseline_income_enabled(environment):
        for transaction in state["transactions"]:
            if (
                transaction["direction"] == "CREDIT"
                and transaction["transaction_kind"] == BASELINE_SUPPORT_TRANSACTION_KIND
            ):
                try:
                    apply_baseline_support_experience(
                        transaction,
                        hermes_home=hermes_home,
                    )
                except Exception:
                    pass
    result = {
        "kind": WALLET_KIND,
        "status": "available",
        "owner": OWNER,
        "currency": CURRENCY,
        "starting_grant": state["starting_grant"]["amount"],
        "grant_provenance": {
            "source": state["starting_grant"]["source"],
            "kind": state["starting_grant"]["kind"],
        },
        "current_balance": _balance(state),
        "recent_transactions": [
            _transaction_projection(item)
            for item in list(reversed(state["transactions"]))[:MAX_RECENT_TRANSACTIONS]
        ],
    }
    if baseline_income_enabled(environment) and state.get("baseline_support") is not None:
        result["baseline_support"] = dict(state["baseline_support"])
    return result


def home_items_projection(
    *,
    hermes_home: Optional[Path | str] = None,
    environment: Optional[Mapping[str, str]] = None,
    now: Any = None,
) -> dict[str, Any]:
    if not feature_enabled(environment):
        return {"kind": PROJECTION_KIND, "status": "disabled", "reason_code": "FEATURE_DISABLED"}
    if delivery_time_enabled(environment):
        _realize_due_deliveries(
            hermes_home=hermes_home,
            environment=environment,
            now=now,
        )
    state = load_state(hermes_home)
    if state is None:
        return {"kind": PROJECTION_KIND, "status": "unavailable", "reason_code": "GENESIS_NOT_INITIALIZED"}
    result: dict[str, Any] = {
        "kind": PROJECTION_KIND,
        "status": "available",
        "location": HOME_ITEM_LOCATION,
        "items": [
            _item_projection(item)
            for item in state["items"]
            if item["disposition"] == DISPOSITION_OWNED
            and item["current_location"] == HOME_ITEM_LOCATION
        ],
    }
    if delivery_time_enabled(environment):
        pending = [
            _delivery_projection(delivery)
            for delivery in state.get("deliveries", [])
            if delivery["status"] == DELIVERY_STATUS_PENDING
        ]
        result["pending_delivery_count"] = len(pending)
        result["pending_deliveries"] = pending[:MAX_RECENT_TRANSACTIONS]
    return result


def _current_world_location(
    *,
    hermes_home: Optional[Path | str],
    environment: Optional[Mapping[str, str]],
    now: Any = None,
) -> Optional[str]:
    if not _truthy((environment or os.environ).get(WORLD_FEATURE_ENV)):
        return None
    try:
        import sys

        scripts_dir = str(_home(hermes_home) / "scripts")
        if scripts_dir not in sys.path:
            sys.path.insert(0, scripts_dir)
        import world_living_skeleton

        snapshot = world_living_skeleton.read_move_state(
            hermes_home=_home(hermes_home),
            environment=environment,
            now=now,
        )
    except Exception:
        return None
    if not isinstance(snapshot, Mapping) or snapshot.get("world_id") != WORLD_ID:
        return None
    location = snapshot.get("world_location_id")
    return location if isinstance(location, str) else None


def _store_check(
    *,
    hermes_home: Optional[Path | str],
    environment: Optional[Mapping[str, str]],
    now: Any = None,
) -> dict[str, Any]:
    env = environment or os.environ
    if not _truthy(env.get(WORLD_FEATURE_ENV)):
        return {"status": "unavailable", "reason_code": "WORLD_FEATURE_REQUIRED"}
    if not _truthy(env.get(DAILY_FEATURE_ENV)):
        return {"status": "unavailable", "reason_code": "DAILY_RHYTHM_REQUIRED"}
    location = _current_world_location(
        hermes_home=hermes_home,
        environment=env,
        now=now,
    )
    if location is None:
        return {"status": "unavailable", "reason_code": "WORLD_LOCATION_UNAVAILABLE"}
    if location != STORE_LOCATION_ID:
        return {
            "status": "rejected",
            "reason_code": "STORE_LOCATION_REQUIRED",
            "current_location_id": location,
        }
    try:
        import sys

        scripts_dir = str(_home(hermes_home) / "scripts")
        if scripts_dir not in sys.path:
            sys.path.insert(0, scripts_dir)
        import world_daily_rhythm

        availability = world_daily_rhythm.location_availability(
            STORE_LOCATION_ID,
            now,
            timezone_name="Asia/Tokyo",
        )
    except Exception:
        return {"status": "unavailable", "reason_code": "DAILY_RHYTHM_UNAVAILABLE"}
    if availability.get("status") != "OPEN":
        return {
            "status": "rejected",
            "reason_code": "STORE_CLOSED",
            "availability": availability,
        }
    return {"status": "available", "availability": availability}


def shop_projection(
    *,
    hermes_home: Optional[Path | str] = None,
    environment: Optional[Mapping[str, str]] = None,
    now: Any = None,
) -> dict[str, Any]:
    if not feature_enabled(environment):
        return {"kind": SHOP_KIND, "status": "disabled", "reason_code": "FEATURE_DISABLED"}
    checked = _store_check(hermes_home=hermes_home, environment=environment, now=now)
    if checked["status"] != "available":
        return {"kind": SHOP_KIND, **checked}
    return {
        "kind": SHOP_KIND,
        "status": "available",
        "location_id": STORE_LOCATION_ID,
        "availability": checked["availability"],
        "currency": CURRENCY,
        "catalog": [
            {
                "catalog_item_id": item["catalog_item_id"],
                "name": item["name"],
                "description": item["description"],
                "price": item["price"],
                "currency": CURRENCY,
            }
            for item in CATALOG
        ],
    }


def _stable_id(prefix: str, purchase_intent_id: str) -> str:
    digest = hashlib.sha256(purchase_intent_id.encode("utf-8")).hexdigest()
    return f"{prefix}-{digest}"


def _source_provenance(
    *,
    purchase_intent_id: str,
    transaction_id: str,
    source_turn_id: Any = None,
    source_message_id: Any = None,
) -> dict[str, str]:
    provenance = {
        "source": "verified_main_self_buy_item",
        "purchase_intent_id": purchase_intent_id,
        "transaction_id": transaction_id,
        "delivery": "included_home_delivery",
    }
    if source_turn_id is not None:
        provenance["source_turn_id"] = _id(source_turn_id, "source_turn_id")
    if source_message_id is not None:
        provenance["source_message_id"] = _id(source_message_id, "source_message_id")
    return provenance


def _purchase_result(
    *,
    status: str,
    transaction: Mapping[str, Any],
    item: Mapping[str, Any],
    balance: int,
    reason_code: Optional[str] = None,
    delivery: Optional[Mapping[str, Any]] = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "kind": PURCHASE_KIND,
        "status": status,
        "transaction_id": transaction["transaction_id"],
        "purchase_intent_id": transaction["purchase_intent_id"],
        "catalog_item_id": transaction["catalog_item_id"],
        "item_instance_id": item["item_instance_id"],
        "price": transaction["amount"],
        "currency": CURRENCY,
        "occurred_at": transaction["occurred_at"],
        "delivery_location": HOME_ITEM_LOCATION,
        "delivery_provenance": "included_home_delivery",
        "current_balance": balance,
    }
    if reason_code is not None:
        result["reason_code"] = reason_code
    if isinstance(delivery, Mapping):
        result["delivery_status"] = delivery["status"]
        result["expected_delivery_at"] = delivery["expected_delivery_at"]
        result["delivered_at"] = delivery["delivered_at"]
        result["delivery_provenance"] = delivery["delivery_provenance"]
    return result


def purchase_item(
    *,
    catalog_item_id: Any,
    purchase_intent_id: Any,
    main_self_verified: bool,
    hermes_home: Optional[Path | str] = None,
    environment: Optional[Mapping[str, str]] = None,
    source_turn_id: Any = None,
    source_message_id: Any = None,
    now: Any = None,
) -> dict[str, Any]:
    if not feature_enabled(environment):
        return {"kind": PURCHASE_KIND, "status": "disabled", "reason_code": "FEATURE_DISABLED"}
    if main_self_verified is not True:
        return {
            "kind": PURCHASE_KIND,
            "status": "rejected",
            "reason_code": "VERIFIED_MAIN_SELF_REQUIRED",
        }
    try:
        item_id = _id(catalog_item_id, "catalog_item_id")
        intent_id = _id(purchase_intent_id, "purchase_intent_id")
    except PropertyEconomyError:
        return {"kind": PURCHASE_KIND, "status": "rejected", "reason_code": "INVALID_PURCHASE_INPUT"}
    catalog_item = _CATALOG_BY_ID.get(item_id)
    if catalog_item is None:
        return {"kind": PURCHASE_KIND, "status": "rejected", "reason_code": "UNKNOWN_CATALOG_ITEM"}

    path = state_path(hermes_home)
    try:
        with _lock(path):
            state = _load(path)
            if state is None:
                return {
                    "kind": PURCHASE_KIND,
                    "status": "unavailable",
                    "reason_code": "GENESIS_NOT_INITIALIZED",
                }
            existing = next(
                (
                    transaction
                    for transaction in state["transactions"]
                    if transaction.get("purchase_intent_id") == intent_id
                ),
                None,
            )
            if existing is not None:
                existing_item = next(
                    item
                    for item in state["items"]
                    if item["item_instance_id"]
                    == _stable_id("item", intent_id)
                )
                if existing["catalog_item_id"] != item_id:
                    return {
                        "kind": PURCHASE_KIND,
                        "status": "rejected",
                        "reason_code": "PURCHASE_INTENT_CONFLICT",
                        "purchase_intent_id": intent_id,
                    }
                existing_delivery = next(
                    (
                        delivery
                        for delivery in state.get("deliveries", [])
                        if delivery["purchase_intent_id"] == intent_id
                    ),
                    None,
                )
                return _purchase_result(
                    status="already_applied",
                    transaction=existing,
                    item=existing_item,
                    balance=_balance(state),
                    reason_code="IDEMPOTENT_REPLAY",
                    delivery=existing_delivery,
                )

            checked = _store_check(
                hermes_home=hermes_home,
                environment=environment,
                now=now,
            )
            if checked["status"] != "available":
                return {"kind": PURCHASE_KIND, **checked}
            amount = int(catalog_item["price"])
            balance = _balance(state)
            if balance < amount:
                return {
                    "kind": PURCHASE_KIND,
                    "status": "rejected",
                    "reason_code": "INSUFFICIENT_FUNDS",
                    "current_balance": balance,
                    "price": amount,
                    "currency": CURRENCY,
                }
            transaction_id = _stable_id("purchase", intent_id)
            item_instance_id = _stable_id("item", intent_id)
            provenance = _source_provenance(
                purchase_intent_id=intent_id,
                transaction_id=transaction_id,
                source_turn_id=source_turn_id,
                source_message_id=source_message_id,
            )
            occurred_at = _timestamp(now, "occurred_at")
            transaction = {
                "transaction_id": transaction_id,
                "transaction_kind": "PURCHASE",
                "direction": "DEBIT",
                "amount": amount,
                "currency": CURRENCY,
                "occurred_at": occurred_at,
                "catalog_item_id": item_id,
                "purchase_intent_id": intent_id,
                "delivery_location": HOME_ITEM_LOCATION,
                "provenance": provenance,
            }
            temporal_delivery = delivery_time_enabled(environment)
            delivery: Optional[dict[str, Any]] = None
            if temporal_delivery:
                purchased_instant = datetime.fromisoformat(
                    occurred_at.replace("Z", "+00:00")
                )
                delivery = {
                    "delivery_id": _stable_id("delivery", intent_id),
                    "purchase_transaction_id": transaction_id,
                    "purchase_intent_id": intent_id,
                    "item_instance_id": item_instance_id,
                    "destination_location": HOME_ITEM_LOCATION,
                    "purchased_at": occurred_at,
                    "expected_delivery_at": _timestamp(
                        purchased_instant
                        + timedelta(seconds=DELIVERY_DURATION_SECONDS),
                        "expected_delivery_at",
                    ),
                    "delivered_at": None,
                    "status": DELIVERY_STATUS_PENDING,
                    "delivery_provenance": DELIVERY_PROVENANCE,
                }
            item = {
                "item_instance_id": item_instance_id,
                "catalog_item_id": item_id,
                "owner": OWNER,
                "acquired_at": occurred_at,
                "acquisition_kind": "PURCHASE",
                "acquisition_provenance": provenance,
                "acquisition_price": amount,
                "currency": CURRENCY,
                "current_location": (
                    DELIVERY_PENDING_LOCATION
                    if temporal_delivery
                    else HOME_ITEM_LOCATION
                ),
                "disposition": DISPOSITION_OWNED,
            }
            updated = dict(state)
            updated["revision"] = state["revision"] + 1
            updated["updated_at"] = occurred_at
            updated["transactions"] = [*state["transactions"], transaction]
            updated["items"] = [*state["items"], item]
            if temporal_delivery and delivery is not None:
                updated["deliveries"] = [
                    *state.get("deliveries", []),
                    delivery,
                ]
            saved = _save(path, updated)
            verified = _load(path)
            if verified != saved:
                raise PropertyEconomyError("purchase postcondition failed")
            verified_transaction = verified["transactions"][-1]
            verified_item = verified["items"][-1]
            verified_delivery = (
                next(
                    item
                    for item in verified.get("deliveries", [])
                    if item["purchase_intent_id"] == intent_id
                )
                if temporal_delivery
                else None
            )
            delivery_timeline_status: Optional[str] = None
            if verified_delivery is not None:
                try:
                    import sys

                    scripts_dir = str(_home(hermes_home) / "scripts")
                    if scripts_dir not in sys.path:
                        sys.path.insert(0, scripts_dir)
                    import world_travel_delivery

                    timeline_result = world_travel_delivery.ensure_delivery_timeline_process(
                        verified_delivery,
                        hermes_home=hermes_home,
                    )
                    delivery_timeline_status = timeline_result.get("status")
                except Exception:
                    delivery_timeline_status = "unavailable"
            result = _purchase_result(
                status="applied",
                transaction=verified_transaction,
                item=verified_item,
                balance=_balance(verified),
                delivery=verified_delivery,
            )
            if delivery_timeline_status is not None:
                result["delivery_timeline_status"] = delivery_timeline_status
            return result
    except PropertyEconomyError as exc:
        return {
            "kind": PURCHASE_KIND,
            "status": "failed",
            "reason_code": "ECONOMY_STATE_ERROR",
            "error_type": type(exc).__name__,
        }


def find_owner_funding(
    request_id: Any,
    *,
    hermes_home: Optional[Path | str] = None,
) -> Optional[dict[str, Any]]:
    """Read the one owner-funding credit for a request, if it exists."""

    try:
        normalized_request_id = _id(request_id, "request_id")
        state = load_state(hermes_home)
        if state is None:
            return None
        for transaction in state["transactions"]:
            if (
                transaction["transaction_kind"] == OWNER_FUNDING_TRANSACTION_KIND
                and transaction["provenance"]["request_id"] == normalized_request_id
            ):
                return dict(transaction)
    except PropertyEconomyError:
        return None
    return None


def apply_owner_funding_credit(
    *,
    request_id: Any,
    owner_decision_id: Any,
    amount_jpy: Any,
    owner_verified: bool,
    authorization: Optional[Mapping[str, Any]],
    hermes_home: Optional[Path | str] = None,
    environment: Optional[Mapping[str, str]] = None,
    now: Any = None,
) -> dict[str, Any]:
    """Append one exactly-once owner credit after a delivered OPEN request.

    The funding module supplies the bounded authorization proof.  This keeps
    the finance ledger as the sole balance authority while rejecting direct
    unsolicited calls that do not carry a delivered request binding.
    """

    if not feature_enabled(environment):
        return {"kind": WALLET_KIND, "status": "disabled", "reason_code": "FEATURE_DISABLED"}
    if owner_verified is not True:
        return {
            "kind": WALLET_KIND,
            "status": "rejected",
            "reason_code": "VERIFIED_OWNER_REQUIRED",
        }
    try:
        normalized_request_id = _id(request_id, "request_id")
        normalized_decision_id = _id(owner_decision_id, "owner_decision_id")
        normalized_amount = _positive_int(amount_jpy, "amount_jpy")
        if not isinstance(authorization, Mapping):
            raise PropertyEconomyError("owner funding authorization is missing")
        owner_source = _id(authorization.get("owner_source"), "owner_source")
        owner_session_id = _id(
            authorization.get("owner_session_id"), "owner_session_id"
        )
        owner_turn_id = _id(authorization.get("owner_turn_id"), "owner_turn_id")
        owner_message_id = _id(
            authorization.get("owner_message_id"), "owner_message_id"
        )
        if (
            authorization.get("request_id") != normalized_request_id
            or authorization.get("request_status") != "OPEN"
            or authorization.get("delivery_status") != "DELIVERED"
            or authorization.get("owner_decision_id") != normalized_decision_id
            or _owner_decision_id(owner_source, owner_session_id, owner_message_id)
            != normalized_decision_id
        ):
            raise PropertyEconomyError("owner funding authorization is invalid")
        outbound_identity = _id(
            authorization.get("outbound_message_identity"),
            "outbound_message_identity",
        )
        import sys

        owner_funding_scripts = str(_home(hermes_home) / "scripts")
        if owner_funding_scripts not in sys.path:
            sys.path.insert(0, owner_funding_scripts)
        import world_owner_funding

        request = world_owner_funding.get_request(
            normalized_request_id,
            hermes_home=hermes_home,
        )
        if (
            request is None
            or request.get("status") != "OPEN"
            or request.get("delivery_status") != "DELIVERED"
            or request.get("outbound_message_identity") != outbound_identity
        ):
            raise PropertyEconomyError("owner funding request is not deliverable")
    except PropertyEconomyError:
        return {
            "kind": WALLET_KIND,
            "status": "rejected",
            "reason_code": "OWNER_FUNDING_REQUEST_NOT_AUTHORIZED",
        }

    path = state_path(hermes_home)
    try:
        with _lock(path):
            state = _load(path)
            if state is None:
                return {
                    "kind": WALLET_KIND,
                    "status": "unavailable",
                    "reason_code": "GENESIS_NOT_INITIALIZED",
                }
            existing = next(
                (
                    transaction
                    for transaction in state["transactions"]
                    if (
                        transaction["transaction_kind"] == OWNER_FUNDING_TRANSACTION_KIND
                        and transaction["provenance"]["request_id"] == normalized_request_id
                    )
                ),
                None,
            )
            if existing is not None:
                same_decision = (
                    existing["provenance"]["owner_decision_id"] == normalized_decision_id
                    and existing["amount"] == normalized_amount
                )
                return {
                    "kind": WALLET_KIND,
                    "status": "already_applied" if same_decision else "rejected",
                    "reason_code": "IDEMPOTENT_REPLAY" if same_decision else "REQUEST_ALREADY_RESOLVED",
                    "request_id": normalized_request_id,
                    "transaction_id": existing["transaction_id"],
                    "amount": existing["amount"],
                    "currency": CURRENCY,
                    "current_balance": _balance(state),
                    "transaction": dict(existing),
                }
            transaction_id = _owner_funding_transaction_id(
                normalized_request_id, normalized_decision_id
            )
            occurred_at = _timestamp(now, "occurred_at")
            transaction = {
                "transaction_id": transaction_id,
                "transaction_kind": OWNER_FUNDING_TRANSACTION_KIND,
                "direction": "CREDIT",
                "amount": normalized_amount,
                "currency": CURRENCY,
                "occurred_at": occurred_at,
                "provenance": {
                    "source": OWNER_FUNDING_SOURCE,
                    "income_kind": OWNER_FUNDING_INCOME_KIND,
                    "request_id": normalized_request_id,
                    "owner_decision_id": normalized_decision_id,
                    "transaction_id": transaction_id,
                },
            }
            updated = dict(state)
            updated["revision"] = state["revision"] + 1
            updated["updated_at"] = occurred_at
            updated["transactions"] = [*state["transactions"], transaction]
            saved = _save(path, updated)
            verified = _load(path)
            if verified != saved:
                raise PropertyEconomyError("owner funding postcondition failed")
            verified_transaction = verified["transactions"][-1]
            return {
                "kind": WALLET_KIND,
                "status": "applied",
                "request_id": normalized_request_id,
                "transaction_id": verified_transaction["transaction_id"],
                "amount": verified_transaction["amount"],
                "currency": CURRENCY,
                "current_balance": _balance(verified),
                "transaction": dict(verified_transaction),
            }
    except PropertyEconomyError as exc:
        return {
            "kind": WALLET_KIND,
            "status": "failed",
            "reason_code": "ECONOMY_STATE_ERROR",
            "error_type": type(exc).__name__,
        }


def build_baseline_support_source_event(
    transaction: Mapping[str, Any],
    *,
    occurred_at: Any = None,
) -> Any:
    normalized = _validate_transaction(transaction)
    if (
        normalized["direction"] != "CREDIT"
        or normalized["transaction_kind"] != BASELINE_SUPPORT_TRANSACTION_KIND
    ):
        raise PropertyEconomyError("transaction is not baseline support")
    import sys

    scripts_dir = str(_home(None) / "scripts")
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)
    import experience_continuity as ec

    transaction_id = normalized["transaction_id"]
    event_time = _timestamp(
        occurred_at if occurred_at is not None else normalized["occurred_at"],
        "baseline_support.occurred_at",
    )
    return ec.SourceEvent.create(
        source="shiomi_world_property_economy",
        source_event_id=f"baseline-support-event-{transaction_id}",
        occurred_at=event_time,
        event_type="world_baseline_living_support",
        summary=(
            f"收到 {normalized['effective_period']} 基础生活支持 "
            f"{normalized['amount']} JPY"
        ),
        facts={
            "income_kind": BASELINE_SUPPORT_INCOME_KIND,
            "program_id": BASELINE_SUPPORT_PROGRAM_ID,
            "effective_period": normalized["effective_period"],
            "support_start_period": normalized["provenance"][
                "support_start_period"
            ],
            "amount": normalized["amount"],
            "currency": CURRENCY,
            "transaction_id": transaction_id,
        },
        provenance={
            "created_by": "shiomi_world_property_economy",
            "source_ref": "world_baseline_living_support",
            "external_id": transaction_id,
        },
        reality_domain="chiyo_world",
    )


def apply_baseline_support_experience(
    transaction: Mapping[str, Any],
    *,
    hermes_home: Optional[Path | str] = None,
) -> dict[str, Any]:
    """Project one stable factual income event; the wallet remains authoritative."""

    try:
        import sys

        home = _home(hermes_home)
        scripts_dir = str(home / "scripts")
        if scripts_dir not in sys.path:
            sys.path.insert(0, scripts_dir)
        import experience_continuity as ec

        event = build_baseline_support_source_event(transaction)
        store = ec.ExperienceStore(ec.default_store_path(home))
        applied = ec.ingest_source_event(event, store=store, confirm_apply=True)
        return {
            "status": applied.get("status"),
            "source_event_id": event.source_event_id,
            "canonical_event_key": ec.canonical_event_key_for(event),
        }
    except Exception as exc:
        return {
            "status": "failed",
            "reason_code": "EXPERIENCE_APPLY_FAILED",
            "error_type": type(exc).__name__,
        }


def build_baseline_support_correction_source_event(
    correction_transaction: Mapping[str, Any],
    original_transaction: Mapping[str, Any],
    *,
    occurred_at: Any = None,
) -> Any:
    correction = _validate_transaction(correction_transaction)
    original = _validate_transaction(original_transaction)
    if (
        correction["transaction_kind"]
        != OWNER_DECISION_CORRECTION_TRANSACTION_KIND
        or correction["direction"] != OWNER_DECISION_CORRECTION_DIRECTION
    ):
        raise PropertyEconomyError("transaction is not a baseline correction")
    if (
        original["transaction_kind"] != BASELINE_SUPPORT_TRANSACTION_KIND
        or original["direction"] != "CREDIT"
        or correction["provenance"]["corrects_transaction_id"]
        != original["transaction_id"]
    ):
        raise PropertyEconomyError("correction target is invalid")
    import sys

    home = _home(None)
    scripts_dir = str(home / "scripts")
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)
    import experience_continuity as ec

    event_time = _timestamp(
        occurred_at if occurred_at is not None else correction["occurred_at"],
        "baseline_support_correction.occurred_at",
    )
    return ec.SourceEvent.create(
        source="shiomi_world_property_economy",
        source_event_id=(
            f"baseline-support-correction-event-{correction['transaction_id']}"
        ),
        occurred_at=event_time,
        event_type="world_baseline_living_support_correction",
        summary=(
            f"Phase10 owner decision corrected the {original['effective_period']} "
            f"baseline support ledger entry by {abs(correction['amount'])} JPY"
        ),
        facts={
            "correction_kind": BASELINE_SUPPORT_CORRECTION_KIND,
            "corrects_transaction_id": original["transaction_id"],
            "correction_transaction_id": correction["transaction_id"],
            "effective_period": original["effective_period"],
            "amount": correction["amount"],
            "currency": CURRENCY,
            "reason": BASELINE_SUPPORT_CORRECTION_REASON,
        },
        provenance={
            "created_by": "shiomi_world_property_economy",
            "source_ref": "world_baseline_living_support_correction",
            "external_id": correction["transaction_id"],
        },
        reality_domain="chiyo_world",
    )


def apply_baseline_support_correction_experience(
    correction_transaction: Mapping[str, Any],
    original_transaction: Mapping[str, Any],
    *,
    hermes_home: Optional[Path | str] = None,
) -> dict[str, Any]:
    """Append one factual correction event after the ledger commit."""

    try:
        import sys

        home = _home(hermes_home)
        scripts_dir = str(home / "scripts")
        if scripts_dir not in sys.path:
            sys.path.insert(0, scripts_dir)
        import experience_continuity as ec

        event = build_baseline_support_correction_source_event(
            correction_transaction,
            original_transaction,
        )
        store = ec.ExperienceStore(ec.default_store_path(home))
        applied = ec.ingest_source_event(event, store=store, confirm_apply=True)
        return {
            "status": applied.get("status"),
            "source_event_id": event.source_event_id,
            "canonical_event_key": ec.canonical_event_key_for(event),
        }
    except Exception as exc:
        return {
            "status": "failed",
            "reason_code": "EXPERIENCE_APPLY_FAILED",
            "error_type": type(exc).__name__,
        }


def build_purchase_source_event(
    purchase_result: Mapping[str, Any],
    *,
    occurred_at: Any = None,
) -> Any:
    if purchase_result.get("status") not in {"applied", "already_applied"}:
        raise PropertyEconomyError("purchase result is not successful")
    transaction_id = _id(purchase_result.get("transaction_id"), "transaction_id")
    item_id = _id(purchase_result.get("catalog_item_id"), "catalog_item_id")
    intent_id = _id(purchase_result.get("purchase_intent_id"), "purchase_intent_id")
    item_instance_id = _id(purchase_result.get("item_instance_id"), "item_instance_id")
    catalog_item = _CATALOG_BY_ID.get(item_id)
    if catalog_item is None:
        raise PropertyEconomyError("purchase result item is unknown")
    import sys

    scripts_dir = str(_home(None) / "scripts")
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)
    import experience_continuity as ec

    event_time = _timestamp(
        occurred_at if occurred_at is not None else purchase_result.get("occurred_at"),
        "purchase.occurred_at",
    )
    return ec.SourceEvent.create(
        source="shiomi_world_property_economy",
        source_event_id=f"purchase-event-{transaction_id}",
        occurred_at=event_time,
        event_type="world_item_purchase",
        summary=f"在 {STORE_LOCATION_ID} 购买 {catalog_item['name']}，支付 {catalog_item['price']} JPY",
        facts={
            "location_id": STORE_LOCATION_ID,
            "catalog_item_id": item_id,
            "item_instance_id": item_instance_id,
            "purchase_intent_id": intent_id,
            "transaction_id": transaction_id,
            "price": catalog_item["price"],
            "currency": CURRENCY,
            "delivery_location": HOME_ITEM_LOCATION,
        },
        provenance={
            "created_by": "shiomi_world_property_economy",
            "source_ref": "world_property_purchase",
            "external_id": transaction_id,
        },
        reality_domain="chiyo_world",
    )


def apply_purchase_experience(
    purchase_result: Mapping[str, Any],
    *,
    hermes_home: Optional[Path | str] = None,
) -> dict[str, Any]:
    """Apply one stable factual purchase event; economy remains authoritative."""

    try:
        import sys

        home = _home(hermes_home)
        scripts_dir = str(home / "scripts")
        if scripts_dir not in sys.path:
            sys.path.insert(0, scripts_dir)
        import experience_continuity as ec

        event = build_purchase_source_event(purchase_result)
        store = ec.ExperienceStore(ec.default_store_path(home))
        applied = ec.ingest_source_event(event, store=store, confirm_apply=True)
        return {
            "status": applied.get("status"),
            "source_event_id": event.source_event_id,
            "canonical_event_key": ec.canonical_event_key_for(event),
        }
    except Exception as exc:
        return {
            "status": "failed",
            "reason_code": "EXPERIENCE_APPLY_FAILED",
            "error_type": type(exc).__name__,
        }


def build_owner_funding_source_event(
    transaction: Mapping[str, Any],
    *,
    hermes_home: Optional[Path | str] = None,
) -> Any:
    """Build one bounded factual Experience event for a committed owner credit."""

    normalized = _validate_transaction(transaction)
    if (
        normalized["transaction_kind"] != OWNER_FUNDING_TRANSACTION_KIND
        or normalized["direction"] != "CREDIT"
    ):
        raise PropertyEconomyError("transaction is not owner funding")
    import sys

    scripts_dir = str(_home(hermes_home) / "scripts")
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)
    import experience_continuity as ec

    transaction_id = normalized["transaction_id"]
    provenance = normalized["provenance"]
    return ec.SourceEvent.create(
        source="shiomi_world_property_economy",
        source_event_id=f"owner-funding-event-{transaction_id}",
        occurred_at=normalized["occurred_at"],
        event_type="world_owner_funding_grant",
        summary=f"收到所有者转入 {normalized['amount']} JPY",
        facts={
            "income_kind": OWNER_FUNDING_INCOME_KIND,
            "request_id": provenance["request_id"],
            "owner_decision_id": provenance["owner_decision_id"],
            "amount": normalized["amount"],
            "currency": CURRENCY,
            "transaction_id": transaction_id,
        },
        provenance={
            "created_by": "shiomi_world_property_economy",
            "source_ref": "world_owner_funding_grant",
            "external_id": transaction_id,
        },
        reality_domain="chiyo_world",
    )


def apply_owner_funding_experience(
    transaction: Mapping[str, Any],
    *,
    hermes_home: Optional[Path | str] = None,
) -> dict[str, Any]:
    """Project a committed owner credit after ledger success; never credits."""

    try:
        import sys

        home = _home(hermes_home)
        scripts_dir = str(home / "scripts")
        if scripts_dir not in sys.path:
            sys.path.insert(0, scripts_dir)
        import experience_continuity as ec

        event = build_owner_funding_source_event(transaction, hermes_home=home)
        store = ec.ExperienceStore(ec.default_store_path(home))
        applied = ec.ingest_source_event(event, store=store, confirm_apply=True)
        return {
            "status": applied.get("status"),
            "source_event_id": event.source_event_id,
            "canonical_event_key": ec.canonical_event_key_for(event),
        }
    except Exception as exc:
        return {
            "status": "failed",
            "reason_code": "EXPERIENCE_APPLY_FAILED",
            "error_type": type(exc).__name__,
        }


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Shiomi property/economy genesis")
    parser.add_argument("--initialize-genesis", action="store_true")
    parser.add_argument("--hermes-home", type=Path, required=True)
    parser.add_argument("--created-at", default=None)
    args = parser.parse_args(argv)
    if not args.initialize_genesis:
        parser.error("--initialize-genesis is required")
    result = initialize_genesis(
        hermes_home=args.hermes_home,
        environment=os.environ,
        created_at=args.created_at,
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result.get("status") in {"created", "already_initialized"} else 2


__all__ = [
    "CATALOG",
    "CURRENCY",
    "BASELINE_SUPPORT_CORRECTION_KIND",
    "BASELINE_SUPPORT_CORRECTION_REASON",
    "BASELINE_SUPPORT_CORRECTION_SOURCE",
    "DAILY_FEATURE_ENV",
    "DELIVERY_DURATION_SECONDS",
    "DELIVERY_FEATURE_ENV",
    "DELIVERY_PENDING_LOCATION",
    "DELIVERY_PROVENANCE",
    "DELIVERY_STATUS_DELIVERED",
    "DELIVERY_STATUS_PENDING",
    "DISPOSITION_DISCARDED",
    "DISPOSITION_GIFTED_AWAY",
    "DISPOSITION_OWNED",
    "FEATURE_ENV",
    "GENESIS_KIND",
    "GENESIS_SOURCE",
    "HOME_ITEM_LOCATION",
    "MOVE_IN_GRANT",
    "OWNER",
    "OWNER_FUNDING_INCOME_KIND",
    "OWNER_FUNDING_SOURCE",
    "OWNER_FUNDING_TRANSACTION_KIND",
    "OWNER_DECISION_CORRECTION_DIRECTION",
    "OWNER_DECISION_CORRECTION_TRANSACTION_KIND",
    "PROJECTION_KIND",
    "PropertyEconomyError",
    "PURCHASE_KIND",
    "SHOP_KIND",
    "STORE_LOCATION_ID",
    "WALLET_KIND",
    "apply_baseline_support_correction_experience",
    "apply_baseline_support_revocation",
    "apply_purchase_experience",
    "apply_delivery_experience",
    "apply_owner_funding_credit",
    "apply_owner_funding_experience",
    "build_baseline_support_correction_source_event",
    "build_purchase_source_event",
    "feature_enabled",
    "delivery_time_enabled",
    "find_owner_funding",
    "build_owner_funding_source_event",
    "home_items_projection",
    "initialize_genesis",
    "load_state",
    "purchase_item",
    "shop_projection",
    "state_path",
    "validate_state",
    "wallet_projection",
]


if __name__ == "__main__":
    raise SystemExit(main())
