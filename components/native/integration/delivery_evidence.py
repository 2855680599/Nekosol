"""Correct interpretation of the live delivery-evidence contract (defect D3).

The defect
----------
``telegram_adapter.py`` (the live Alpha adapter) finalises a turn like this::

    event_id = self.runtime.record_delivery_success(confirmed_result)
    if not event_id:
        raise RuntimeError("M0 delivery evidence writer unavailable")
    ...
    self.state.update(update_key, "DELIVERED", error_class=None)

but the real ``EvidenceWriter.append`` returns a *status string*, not an id::

    inserted   - the evidence row was written
    duplicate  - the idempotency key (source_origin, source_refs[0].id) already existed
    queued     - the store raised; the event was spooled for retry, NOT yet durable
    failed     - ValueError, or the store raised and the spool also failed

Every one of those is a non-empty string, so ``if not event_id`` is always false and
the guard can never fire. A turn whose evidence is merely *spooled* or outright
*failed* is therefore recorded as ``DELIVERED`` - a full success claim the runtime has
not earned.

What this module does
---------------------
It maps the real status vocabulary onto an explicit disposition, and states which
dispositions are durable. Callers get three separate answers, never a truthiness test:

* is the evidence durable?
* which disposition is it (committed / already committed / pending / failed / unknown)?
* what may the adapter do next?

The invariant that matters: an evidence problem must **never** be read as "the message
was not sent", and must **never** trigger a resend. By the time evidence is written the
Telegram message has already been delivered; the only open question is whether the
*record* is durable.
"""
from __future__ import annotations

from typing import Any, Optional

__all__ = [
    "EVIDENCE_INSERTED",
    "EVIDENCE_DUPLICATE",
    "EVIDENCE_QUEUED",
    "EVIDENCE_FAILED",
    "DISPOSITION_COMMITTED",
    "DISPOSITION_ALREADY_COMMITTED",
    "DISPOSITION_PENDING",
    "DISPOSITION_FAILED",
    "DISPOSITION_UNKNOWN",
    "DURABLE_DISPOSITIONS",
    "MESSAGE_ALREADY_DELIVERED",
    "disposition_of",
    "is_durable",
    "evidence_state_name",
    "should_mark_delivered",
    "should_resend",
]

#: The exact strings ``app.evidence.EvidenceWriter.append`` can return.
EVIDENCE_INSERTED = "inserted"
EVIDENCE_DUPLICATE = "duplicate"
EVIDENCE_QUEUED = "queued"
EVIDENCE_FAILED = "failed"

DISPOSITION_COMMITTED = "EVIDENCE_COMMITTED"
DISPOSITION_ALREADY_COMMITTED = "EVIDENCE_ALREADY_COMMITTED"
DISPOSITION_PENDING = "EVIDENCE_PENDING"
DISPOSITION_FAILED = "EVIDENCE_FAILED"
DISPOSITION_UNKNOWN = "EVIDENCE_UNKNOWN"

#: Only these mean the evidence is on disk and will survive a restart.
DURABLE_DISPOSITIONS = frozenset({DISPOSITION_COMMITTED, DISPOSITION_ALREADY_COMMITTED})

#: Always true at the point evidence is written: the Telegram send already returned a
#: message id. Nothing downstream may reinterpret an evidence problem as a send problem.
MESSAGE_ALREADY_DELIVERED = True

_STATUS_TO_DISPOSITION = {
    EVIDENCE_INSERTED: DISPOSITION_COMMITTED,
    EVIDENCE_DUPLICATE: DISPOSITION_ALREADY_COMMITTED,
    EVIDENCE_QUEUED: DISPOSITION_PENDING,
    EVIDENCE_FAILED: DISPOSITION_FAILED,
}


def disposition_of(status: Any) -> str:
    """Map a raw evidence status onto a disposition. Unknown input is UNKNOWN.

    ``None``, ``""``, a non-string, or any unexpected string all land on
    ``DISPOSITION_UNKNOWN`` rather than being silently treated as success.
    """
    if isinstance(status, str) and status in _STATUS_TO_DISPOSITION:
        return _STATUS_TO_DISPOSITION[status]
    return DISPOSITION_UNKNOWN


def is_durable(status: Any) -> bool:
    return disposition_of(status) in DURABLE_DISPOSITIONS


def evidence_state_name(disposition: str) -> Optional[str]:
    """The adapter ledger state that matches a disposition.

    Durable evidence lets the turn reach ``DELIVERED``. Anything else must leave the
    row at ``TELEGRAM_CONFIRMED`` so the adapter's own ``recover()`` path can finalise
    it after a restart, without ever resending the message.
    """
    if disposition in DURABLE_DISPOSITIONS:
        return "DELIVERED"
    return "TELEGRAM_CONFIRMED"


def should_mark_delivered(status: Any) -> bool:
    return is_durable(status)


def should_resend(status: Any) -> bool:
    """Never. Evidence status says nothing about whether the message was sent."""
    return False
