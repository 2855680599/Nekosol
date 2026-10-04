"""LPC0B sections 23/24/25/51: the real cognition budget.

Authority: the order requires an actual budget system (calls/hour, calls/day, input tokens,
output tokens, provider-reported usage, and cost only when the provider reports it), an
emergency hard cap that guards against code bugs, and caps that are *derived from measured
shadow traffic* rather than invented up front.

Cost policy: if the provider reports a cost we record it; if it does not we record
`UNKNOWN`.  No dollar figure is ever fabricated (order section 23).

The ledger is append-only JSONL plus an atomically replaced snapshot, under the Life
Runtime's own state root, so it survives a restart and can be read by the soak monitor.
Crash honesty (LPC0B-R2)
------------------------
A reservation whose process died between "reserved" and a known outcome must not be silently
closed as OK or as PROVIDER_ERROR: it is recorded as ``SEND_STATE_UNKNOWN``, so the ledger shows
the call as *possibly billed but unknowable*.  A result that arrived after its own deadline is
closed as ``LATE_DISCARDED`` -- it was paid for, and it was thrown away.
"""
from __future__ import annotations

import json
import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Optional

BJT = timezone(timedelta(hours=8))

COST_UNKNOWN = "UNKNOWN"

#: Statuses a reservation can end in.
OUTCOME_OK = "OK"
OUTCOME_PROVIDER_ERROR = "PROVIDER_ERROR"
OUTCOME_SCHEMA_REJECT = "SCHEMA_REJECT"
OUTCOME_STALE = "STALE"
OUTCOME_BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"
OUTCOME_NOT_CONFIGURED = "NOT_CONFIGURED"
OUTCOME_NOT_CONFIGURED = "NOT_CONFIGURED"
#: LPC0B-R2 Gap 2: the transport returned after its deadline; paid for, discarded.
OUTCOME_LATE_DISCARDED = "LATE_DISCARDED"
#: LPC0B-R2 Gap 1: reserved, then the process died before any outcome was knowable.
OUTCOME_SEND_STATE_UNKNOWN = "SEND_STATE_UNKNOWN"

class BudgetExhausted(RuntimeError):
    """Refused before any provider call.  Not a model choice."""

    def __init__(self, scope: str, detail: str) -> None:
        super().__init__(f"BUDGET_EXHAUSTED:{scope}: {detail}")
        self.scope = scope


@dataclass
class Reservation:
    reservation_id: str
    request_key: str
    reserved_at: str
    hour_bucket: str
    day_bucket: str
    granted: bool = True
    outcome: Optional[str] = None
    settled_at: Optional[str] = None
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    total_tokens: Optional[int] = None
    cost_usd: Any = None
    latency_ms: Optional[int] = None
    detail: Optional[str] = None


def _now_iso() -> str:
    return datetime.now(BJT).isoformat(timespec="seconds")


def _bucket_hour() -> str:
    return datetime.now(BJT).strftime("%Y-%m-%dT%H")


def _bucket_day() -> str:
    return datetime.now(BJT).strftime("%Y-%m-%d")


class CognitionBudget:
    """Token/call budget with an emergency hard cap that is always enforced."""

    def __init__(
        self,
        directory: Path | str,
        *,
        max_calls_per_hour: Optional[int] = None,
        max_calls_per_day: Optional[int] = None,
        max_input_tokens_per_day: Optional[int] = None,
        max_output_tokens_per_day: Optional[int] = None,
        emergency_max_calls_per_hour: int = 60,
        emergency_max_calls_per_day: int = 240,
        concurrency: int = 1,
    ) -> None:
        self.dir = Path(directory)
        self.ledger_path = self.dir / "cognition_budget_ledger.jsonl"
        self.snapshot_path = self.dir / "cognition_budget.json"
        self.max_calls_per_hour = max_calls_per_hour
        self.max_calls_per_day = max_calls_per_day
        self.max_input_tokens_per_day = max_input_tokens_per_day
        self.max_output_tokens_per_day = max_output_tokens_per_day
        self.emergency_max_calls_per_hour = int(emergency_max_calls_per_hour)
        self.emergency_max_calls_per_day = int(emergency_max_calls_per_day)
        self.concurrency = int(concurrency)
        self._counters: dict[str, dict[str, int]] = {}
        self._unknown_request_keys: set[str] = set()
        self._unknown_accounted: set[str] = set()
        self._reservation_by_key: dict[str, str] = {}
        self._settled_request_keys: set[str] = set()
        self._ledger_lock = threading.RLock()
        self._load()

    # -- persistence -----------------------------------------------------------------

    def _load(self) -> None:
        try:
            self._rebuild_counters()
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise RuntimeError("COGNITION_BUDGET_LEDGER_CORRUPT") from exc

    def _rebuild_counters(self) -> None:
        """Rebuild ceiling counters idempotently from reservations and unknown-only attempts."""
        self._counters.clear()
        reservations: list[Mapping[str, Any]] = []
        reserved_keys: set[str] = set()
        unknown: dict[str, Mapping[str, Any]] = {}
        try:
            lines = self.ledger_path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return
        for line in lines:
            if not line.strip():
                continue
            row = json.loads(line)
            event = row.get("event")
            if event == "reserve":
                reservations.append(row)
                reserved_keys.add(str(row.get("request_key") or ""))
            elif event == "unknown_send_state":
                unknown[str(row.get("request_key") or "")] = row
            elif event == "settle":
                request_key = str(row.get("request_key") or "")
                if row.get("outcome") not in (OUTCOME_SEND_STATE_UNKNOWN, OUTCOME_LATE_DISCARDED):
                    unknown.pop(request_key, None)
        self._reservation_by_key = {}
        for row in reservations:
            self._reservation_by_key.setdefault(str(row.get("request_key") or ""),
                                                str(row.get("reservation_id") or ""))
        settled_keys = {str(row.get("request_key") or "") for line in lines if line.strip()
                        for row in [json.loads(line)] if row.get("event") == "settle"
                        and row.get("outcome") not in (OUTCOME_SEND_STATE_UNKNOWN,
                                                        OUTCOME_LATE_DISCARDED)}
        self._settled_request_keys = settled_keys
        self._unknown_request_keys = set(unknown)
        self._unknown_accounted = {key for key, row in unknown.items()
                                   if not row.get("reservation_exists") and key not in reserved_keys}
        rows = [row for row in reservations
                if str(row.get("request_key") or "") not in settled_keys
                and str(row.get("request_key") or "") not in unknown]
        rows += [row for key, row in unknown.items() if key not in reserved_keys
                 and not row.get("reservation_exists")]
        for row in rows:
            hour = str(row.get("hour_bucket") or "")
            day = str(row.get("day_bucket") or "")
            for bucket in (hour, day):
                self._counters.setdefault(bucket, {"calls": 0, "input_tokens": 0, "output_tokens": 0})
            self._counters[hour]["calls"] += 1
            self._counters[day]["calls"] += 1
        for line in lines:
            if not line.strip():
                continue
            row = json.loads(line)
            if row.get("event") != "settle" or row.get("outcome") in (
                    OUTCOME_SEND_STATE_UNKNOWN, OUTCOME_LATE_DISCARDED):
                continue
            day = str(row.get("day_bucket") or "")
            self._counters.setdefault(day, {"calls": 0, "input_tokens": 0, "output_tokens": 0})
            for name in ("input_tokens", "output_tokens"):
                if isinstance(row.get(name), int):
                    self._counters[day][name] += row[name]

    def _apply(self, row: Mapping[str, Any]) -> None:
        hour = str(row.get("hour_bucket") or "")
        day = str(row.get("day_bucket") or "")
        for bucket in (hour, day):
            self._counters.setdefault(bucket, {"calls": 0, "input_tokens": 0, "output_tokens": 0})
        if row.get("event") == "unknown_send_state":
            request_key = str(row.get("request_key") or "")
            if request_key and request_key not in self._unknown_request_keys:
                self._unknown_request_keys.add(request_key)
                if (row.get("reservation_id") is None
                        and not row.get("reservation_exists")
                        and request_key not in self._unknown_accounted):
                    self._counters[hour]["calls"] += 1
                    self._counters[day]["calls"] += 1
                    self._unknown_accounted.add(request_key)
            return
        if row.get("event") == "reserve":
            reservation_id = str(row.get("reservation_id") or "")
            request_key = str(row.get("request_key") or "")
            if request_key and reservation_id:
                self._reservation_by_key[request_key] = reservation_id
        if row.get("event") == "settle":
            settled_key = str(row.get("request_key") or "")
            self._unknown_request_keys.discard(settled_key)
            if row.get("outcome") not in (OUTCOME_SEND_STATE_UNKNOWN, OUTCOME_LATE_DISCARDED):
                self._settled_request_keys.add(settled_key)
        if row.get("event") != "settle" and not row.get("granted"):
            return
        if row.get("event") == "reserve":
            # Reservations count against ceilings even before a provider result settles.
            self._counters[hour]["calls"] += 1
            self._counters[day]["calls"] += 1
            return
        if row.get("event") != "settle":
            return
        # Settlement updates token usage only; it must not count a second call.
        for name, key in (("input_tokens", "input_tokens"), ("output_tokens", "output_tokens")):
            value = row.get(key)
            if isinstance(value, int):
                self._counters[day][name] += value

    def _append(self, row: Mapping[str, Any]) -> None:
        with self._ledger_lock:
            self._append_locked(row)

    def _append_locked(self, row: Mapping[str, Any]) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        try:
            with self.ledger_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(dict(row), ensure_ascii=False, separators=(",", ":")) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
        except OSError as exc:
            raise RuntimeError("COGNITION_BUDGET_LEDGER_WRITE_FAILED") from exc
        self._apply(row)
        self._write_snapshot()

    def _write_snapshot(self) -> None:
        doc = json.dumps(self.snapshot(), indent=2, ensure_ascii=False) + "\n"
        tmp = self.snapshot_path.with_name(f".{self.snapshot_path.name}.tmp.{uuid.uuid4().hex}")
        try:
            with tmp.open("w", encoding="utf-8") as handle:
                handle.write(doc)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, self.snapshot_path)
        except OSError as exc:
            raise RuntimeError("COGNITION_BUDGET_SNAPSHOT_WRITE_FAILED") from exc

    # -- accounting ------------------------------------------------------------------

    def _count(self, bucket: str, key: str) -> int:
        return int(self._counters.get(bucket, {}).get(key, 0))

    def snapshot(self) -> dict[str, Any]:
        hour, day = _bucket_hour(), _bucket_day()
        h = self._counters.get(hour, {"calls": 0, "input_tokens": 0, "output_tokens": 0})
        d = self._counters.get(day, {"calls": 0, "input_tokens": 0, "output_tokens": 0})
        return {
            "schema": "lpc0b.cognition_budget.v1",
            "at": _now_iso(),
            "hour_bucket": hour, "day_bucket": day,
            "calls_this_hour": h["calls"], "calls_today": d["calls"],
            "input_tokens_today": d["input_tokens"], "output_tokens_today": d["output_tokens"],
            "limits": {
                "max_calls_per_hour": self.max_calls_per_hour,
                "max_calls_per_day": self.max_calls_per_day,
                "max_input_tokens_per_day": self.max_input_tokens_per_day,
                "max_output_tokens_per_day": self.max_output_tokens_per_day,
                "emergency_max_calls_per_hour": self.emergency_max_calls_per_hour,
                "emergency_max_calls_per_day": self.emergency_max_calls_per_day,
                "concurrency": self.concurrency,
            },
            "cost_policy": "provider-reported cost only; otherwise UNKNOWN",
        }

    def check(self) -> dict[str, Any]:
        """Raise BudgetExhausted if a provider call must not be made right now."""
        hour, day = _bucket_hour(), _bucket_day()
        ch, cd = self._count(hour, "calls"), self._count(day, "calls")
        if ch >= self.emergency_max_calls_per_hour:
            raise BudgetExhausted("emergency_hour", f"{ch} >= {self.emergency_max_calls_per_hour}")
        if cd >= self.emergency_max_calls_per_day:
            raise BudgetExhausted("emergency_day", f"{cd} >= {self.emergency_max_calls_per_day}")
        if self.max_calls_per_hour is not None and ch >= self.max_calls_per_hour:
            raise BudgetExhausted("calls_per_hour", f"{ch} >= {self.max_calls_per_hour}")
        if self.max_calls_per_day is not None and cd >= self.max_calls_per_day:
            raise BudgetExhausted("calls_per_day", f"{cd} >= {self.max_calls_per_day}")
        if self.max_input_tokens_per_day is not None and self._count(day, "input_tokens") >= self.max_input_tokens_per_day:
            raise BudgetExhausted("input_tokens_per_day", f"{self._count(day, 'input_tokens')}")
        if self.max_output_tokens_per_day is not None and self._count(day, "output_tokens") >= self.max_output_tokens_per_day:
            raise BudgetExhausted("output_tokens_per_day", f"{self._count(day, 'output_tokens')}")
        return {"granted": True, "calls_this_hour": ch, "calls_today": cd}

    def reserve(self, *, request_key: str) -> Reservation:
        with self._ledger_lock:
            if (self._reservation_by_key.get(request_key)
                    or request_key in self._settled_request_keys
                    or request_key in self._unknown_request_keys):
                raise RuntimeError("COGNITION_BUDGET_DUPLICATE_REQUEST_KEY")
            if any(row.get("event") == "reserve" and row.get("request_key") == request_key
                   for row in self._read_ledger_rows()):
                raise RuntimeError("COGNITION_BUDGET_DUPLICATE_REQUEST_KEY")
            self.check()
            reservation = Reservation(
                reservation_id=f"cres:{uuid.uuid4().hex[:16]}", request_key=request_key,
                reserved_at=_now_iso(), hour_bucket=_bucket_hour(), day_bucket=_bucket_day(),
            )
            self._append({
                "event": "reserve", "reservation_id": reservation.reservation_id,
                "request_key": request_key, "granted": True,
                "hour_bucket": reservation.hour_bucket, "day_bucket": reservation.day_bucket,
                "at": reservation.reserved_at,
            })
            return reservation

    def refuse(self, *, request_key: str, scope: str, detail: str) -> Reservation:
        reservation = Reservation(
            reservation_id=f"cres:{uuid.uuid4().hex[:16]}", request_key=request_key,
            reserved_at=_now_iso(), hour_bucket=_bucket_hour(), day_bucket=_bucket_day(),
            granted=False, outcome=OUTCOME_BUDGET_EXHAUSTED, settled_at=_now_iso(),
            detail=f"{scope}: {detail}",
        )
        self._append({
            "event": "refuse", "reservation_id": reservation.reservation_id,
            "request_key": request_key, "granted": False,
            "hour_bucket": reservation.hour_bucket, "day_bucket": reservation.day_bucket,
            "scope": scope, "detail": detail, "at": reservation.reserved_at,
        })
        return reservation


    def _read_ledger_rows(self) -> list[dict[str, Any]]:
        try:
            return [json.loads(line) for line in self.ledger_path.read_text(encoding="utf-8").splitlines()
                    if line.strip()]
        except FileNotFoundError:
            return []

    def mark_send_state_unknown(self, *, request_key: str, transport_attempt_id: Optional[str] = None,
                                detail: Optional[str] = None) -> dict[str, Any]:
        """LPC0B-R2 Gap 1: close a reservation whose outcome is unknowable.

        Written when a process died between SEND_STARTED and RESPONSE_RECEIVED.  This row is NOT
        a success and NOT an error: it records that the provider may have been paid for a call
        nobody can read back.  It never grants a retry.
        """
        with self._ledger_lock:
            if any(row.get("request_key") == request_key and row.get("event") == "unknown_send_state"
                   for row in self._read_ledger_rows()):
                return {"event": "unknown_send_state", "request_key": request_key,
                        "outcome": OUTCOME_SEND_STATE_UNKNOWN, "duplicate": True}
            reservation_id = self._reservation_by_key.get(request_key)
            reserved = next((item for item in self._read_ledger_rows()
                             if item.get("event") == "reserve" and item.get("request_key") == request_key), None)
            if reserved is not None:
                reservation_id = reserved.get("reservation_id")
            elif reservation_id is not None:
                reserved = {"reservation_id": reservation_id}
            row = {
                "event": "unknown_send_state", "reservation_id": reservation_id,
                "reservation_exists": bool(reserved),
                "request_key": request_key, "granted": False,
                "hour_bucket": _bucket_hour(), "day_bucket": _bucket_day(),
                "outcome": OUTCOME_SEND_STATE_UNKNOWN,
                "transport_attempt_id": transport_attempt_id,
                "detail": detail, "at": _now_iso(),
            }
            self._append(row)
            if reserved is None:
                bucket = row["hour_bucket"]
                day = row["day_bucket"]
                self._unknown_accounted.add(request_key)
                self._counters.setdefault(bucket, {"calls": 0, "input_tokens": 0, "output_tokens": 0})
                self._counters.setdefault(day, {"calls": 0, "input_tokens": 0, "output_tokens": 0})
                self._counters[bucket]["calls"] += 1
                self._counters[day]["calls"] += 1
                self._write_snapshot()
            return row

    def settle(self, reservation: Reservation, *, outcome: str, result: Any = None,
               detail: Optional[str] = None) -> Reservation:
        with self._ledger_lock:
            rows = self._read_ledger_rows()
            if any(row.get("event") == "settle" and
                   row.get("reservation_id") == reservation.reservation_id for row in rows):
                return reservation
            if not any(row.get("event") == "reserve" and
                       row.get("reservation_id") == reservation.reservation_id for row in rows):
                raise RuntimeError("COGNITION_BUDGET_RESERVATION_MISSING")
            reservation.outcome = outcome
            reservation.settled_at = _now_iso()
            reservation.detail = detail
            row: dict[str, Any] = {
                "event": "settle", "reservation_id": reservation.reservation_id,
                "request_key": reservation.request_key, "granted": False,
                "hour_bucket": reservation.hour_bucket, "day_bucket": reservation.day_bucket,
                "outcome": outcome, "detail": detail, "at": reservation.settled_at,
            }
            if result is not None:
                reservation.input_tokens = getattr(result, "input_tokens", None)
                reservation.output_tokens = getattr(result, "output_tokens", None)
                reservation.total_tokens = getattr(result, "total_tokens", None)
                reservation.latency_ms = getattr(result, "latency_ms", None)
                reported = getattr(result, "reported_cost_usd", None)
                reservation.cost_usd = reported if isinstance(reported, (int, float)) else COST_UNKNOWN
                row.update({"input_tokens": reservation.input_tokens,
                            "output_tokens": reservation.output_tokens,
                            "total_tokens": reservation.total_tokens,
                            "latency_ms": reservation.latency_ms,
                            "cost_usd": reservation.cost_usd,
                            "usage_source": getattr(result, "usage_source", None),
                            "provider": getattr(result, "provider", None),
                            "model": getattr(result, "model", None)})
            self._append(row)
            return reservation


def aggregate_ledger(path: Path | str) -> dict[str, Any]:
    """Read the ledger into the metrics the shadow report needs (order section 44)."""
    rows: list[dict[str, Any]] = []
    try:
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            if line.strip():
                rows.append(json.loads(line))
    except (OSError, ValueError):
        pass
    settles = [r for r in rows if r.get("event") == "settle"]
    reserves = [r for r in rows if r.get("event") == "reserve"]
    refusals = [r for r in rows if r.get("event") == "refuse"]
    unknown_states = [r for r in rows if r.get("event") == "unknown_send_state"]
    latencies = sorted(int(r["latency_ms"]) for r in settles if isinstance(r.get("latency_ms"), int))
    in_tok = [int(r["input_tokens"]) for r in settles if isinstance(r.get("input_tokens"), int)]
    out_tok = [int(r["output_tokens"]) for r in settles if isinstance(r.get("output_tokens"), int)]

    def pct(values: list[int], q: float) -> Optional[int]:
        if not values:
            return None
        idx = min(len(values) - 1, max(0, int(round(q * (len(values) - 1)))))
        return values[idx]

    costs = [r.get("cost_usd") for r in settles]
    cost_known = [c for c in costs if isinstance(c, (int, float))]
    return {
        "provider_calls_reserved": len(reserves),
        "provider_calls_settled": len(settles),
        "provider_calls_refused": len(refusals),
        # LPC0B-R2: a reservation with no knowable outcome, and a result that arrived too late.
        "provider_calls_send_state_unknown": len(unknown_states),
        "provider_calls_late_discarded": sum(1 for r in settles
                                             if r.get("outcome") == OUTCOME_LATE_DISCARDED),
        "outcomes": {k: sum(1 for r in settles if r.get("outcome") == k)
                     for k in sorted({str(r.get("outcome")) for r in settles})},
        "input_tokens_total": sum(in_tok) or 0,
        "output_tokens_total": sum(out_tok) or 0,
        "input_tokens_p95": pct(sorted(in_tok), 0.95),
        "output_tokens_avg": (sum(out_tok) // len(out_tok)) if out_tok else None,
        "latency_ms_p50": pct(latencies, 0.50),
        "latency_ms_p95": pct(latencies, 0.95),
        "cost_usd_total": round(sum(cost_known), 6) if cost_known else COST_UNKNOWN,
        "cost_reporting": "provider_reported" if cost_known else "UNKNOWN",
    }
