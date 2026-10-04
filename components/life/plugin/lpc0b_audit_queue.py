"""LPC0B WP2 bounded audit queue -- zero-blocking producer side.

Authority
---------
Order ``LPC0B-R3-Lite`` section 三A (requirements 1, 3, 4, 5) and 三D.
Design record: ``LPC0B_WP2_R3_DESIGN_DECISION.md`` section 4.3.

Contract
--------
* ``put()`` never blocks and never raises.  There is no timeout parameter, because a
  timeout would be a way for the hook to wait for the audit -- exactly what section 三D
  forbids.
* The queue has a hard capacity.  Memory is therefore bounded by
  ``capacity * max_record_bytes``.
* When a normal record cannot be admitted the queue reports it; the caller (the bridge)
  records an observable gap.  A reserved slot guarantees the gap marker itself fits.

Drop policy: **the newest arriving record is refused**, not the oldest queued one.  Audit
is observational, so keeping the causal order of already-accepted records matters more than
keeping the newest record; refusing the arrival keeps the chain head continuous and turns
the hole into an explicit, countable event instead of an unexplainable middle gap.
"""
from __future__ import annotations

import collections
import threading
from typing import Any, Optional

#: Single-record cap.  Mirrors ``lpc0b_audit_events.MAX_RECORD_BYTES`` so the queue can
#: refuse an oversized record early.
_MAX_RECORD_BYTES = 8192

#: Result codes from ``put``.
ACCEPTED = "accepted"
REFUSED_OVERFLOW = "refused_overflow"
REFUSED_OVERSIZED = "refused_oversized"
REFUSED_NOT_STARTED = "refused_not_started"


class BoundedAuditQueue:
    """A bounded FIFO with a non-blocking producer and a timeout-bounded consumer."""

    def __init__(self, *, capacity: int = 1024, gap_reserve: int = 1) -> None:
        if int(capacity) < 2:
            raise ValueError("audit queue capacity must be at least 2")
        self.capacity = int(capacity)
        self.gap_reserve = max(1, int(gap_reserve))
        self._items: "collections.deque[Any]" = collections.deque()
        self._cv = threading.Condition(threading.Lock())
        self.counters: dict[str, int] = {
            "accepted": 0,
            "refused_overflow": 0,
            "refused_oversized": 0,
            "gap_events": 0,
            "depth": 0,
            "depth_max": 0,
            "capacity": self.capacity,
        }

    # -- producer ---------------------------------------------------------------------

    def put(self, record: Any, *, is_gap: bool = False) -> str:
        """Admit ``record`` without ever blocking.

        ``is_gap=True`` marks the explicit hole marker, which may use the reserved slot so
        the hole is always representable.
        """
        try:
            size = int(record.size_bytes())
        except Exception:  # noqa: BLE001 - any shapeless object counts as oversized
            return self._refuse_oversized()
        if size > _MAX_RECORD_BYTES:
            return self._refuse_oversized()

        with self._cv:
            limit = self.capacity if is_gap else self.capacity - self.gap_reserve
            if len(self._items) >= limit:
                self.counters["refused_overflow"] += 1
                return REFUSED_OVERFLOW
            self._items.append(record)
            self.counters["accepted"] += 1
            if is_gap:
                self.counters["gap_events"] += 1
            self.counters["depth"] = len(self._items)
            if len(self._items) > self.counters["depth_max"]:
                self.counters["depth_max"] = len(self._items)
            self._cv.notify()
        return ACCEPTED

    def _refuse_oversized(self) -> str:
        with self._cv:
            self.counters["refused_oversized"] += 1
        return REFUSED_OVERSIZED

    # -- consumer (Audit Writer only) --------------------------------------------------

    def get(self, timeout: float = 0.05) -> Optional[Any]:
        """Block the consumer for at most ``timeout`` seconds.  Never used by the hook."""
        with self._cv:
            if not self._items:
                self._cv.wait(timeout)
            if self._items:
                item = self._items.popleft()
                self.counters["depth"] = len(self._items)
                return item
        return None

    def drain(self, *, max_items: int = 512) -> list[Any]:
        """Take up to ``max_items`` without blocking."""
        out: list[Any] = []
        with self._cv:
            while self._items and len(out) < int(max_items):
                out.append(self._items.popleft())
            self.counters["depth"] = len(self._items)
        return out

    # -- introspection ------------------------------------------------------------------

    def __len__(self) -> int:
        with self._cv:
            return len(self._items)

    def snapshot(self) -> dict[str, int]:
        with self._cv:
            snap = dict(self.counters)
            snap["depth"] = len(self._items)
            snap["capacity"] = self.capacity
            snap["gap_reserve"] = self.gap_reserve
            return snap
