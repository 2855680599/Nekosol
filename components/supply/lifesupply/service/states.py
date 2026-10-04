"""PHASE 16 runtime state machine.

``STARTING -> RECOVERING -> (READ_ONLY | READY) -> (FENCED | FAILED)``

Writes are permitted **only** in ``READY``: schema compatibility, writer fencing and startup
recovery must all have completed first.  ``READ_ONLY`` serves reads and refuses writes,
``FENCED`` means a writer no longer holds its lease (or recovery could not be completed) so
writes are refused until an operator resolves it, and ``FAILED`` is terminal for the process.
"""
from __future__ import annotations

import time
from typing import Any, Callable

STARTING = "STARTING"
RECOVERING = "RECOVERING"
READ_ONLY = "READ_ONLY"
READY = "READY"
FENCED = "FENCED"
FAILED = "FAILED"

STATES: tuple[str, ...] = (STARTING, RECOVERING, READ_ONLY, READY, FENCED, FAILED)

#: The frozen transition graph.  Anything else raises ``IllegalStateTransition``.
ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    STARTING: frozenset({RECOVERING, READ_ONLY, FAILED}),
    RECOVERING: frozenset({READ_ONLY, READY, FENCED, FAILED}),
    READ_ONLY: frozenset({READY, FENCED, FAILED}),
    READY: frozenset({READ_ONLY, FENCED, FAILED, RECOVERING}),
    FENCED: frozenset({RECOVERING, FAILED}),
    FAILED: frozenset(),
}

WRITE_PERMITTING_STATES: frozenset[str] = frozenset({READY})
READ_PERMITTING_STATES: frozenset[str] = frozenset({READY, READ_ONLY, FENCED})

WRITE_REFUSAL_REASON: dict[str, str] = {
    STARTING: "SERVICE_STARTING",
    RECOVERING: "SERVICE_RECOVERING",
    READ_ONLY: "SERVICE_READ_ONLY",
    FENCED: "SERVICE_FENCED",
    FAILED: "SERVICE_FAILED",
}


class IllegalStateTransition(RuntimeError):
    """An illegal transition was attempted (e.g. FAILED -> READY)."""


class RuntimeStateMachine:
    """Explicit runtime state with an auditable transition history."""

    def __init__(self, state: str = STARTING, *, clock: Callable[[], float] = time.time) -> None:
        if state not in STATES:
            raise IllegalStateTransition(f"UNKNOWN_STATE:{state}")
        self._state = state
        self._clock = clock
        self._history: list[dict[str, Any]] = [
            {"from": None, "to": state, "reason": "INITIAL", "detail": None,
             "at": float(clock())}]

    @property
    def state(self) -> str:
        return self._state

    @property
    def history(self) -> list[dict[str, Any]]:
        return [dict(entry) for entry in self._history]

    def may_write(self) -> bool:
        return self._state in WRITE_PERMITTING_STATES

    def may_read(self) -> bool:
        return self._state in READ_PERMITTING_STATES

    def write_refusal_reason(self) -> str:
        return WRITE_REFUSAL_REASON.get(self._state, "SERVICE_NOT_READY")

    def transition(self, target: str, reason: str = "", detail: Any = None) -> str:
        if target not in STATES:
            raise IllegalStateTransition(f"UNKNOWN_STATE:{target}")
        if target == self._state:
            raise IllegalStateTransition(f"STATE_ALREADY_ACTIVE:{target}")
        if target not in ALLOWED_TRANSITIONS[self._state]:
            raise IllegalStateTransition(f"ILLEGAL_STATE_TRANSITION:{self._state}->{target}")
        previous = self._state
        self._state = target
        self._history.append({"from": previous, "to": target, "reason": reason,
                              "detail": detail, "at": float(self._clock())})
        return target

    def status(self) -> dict[str, Any]:
        return {"state": self._state, "writes_permitted": self.may_write(),
                "reads_permitted": self.may_read(),
                "write_refusal_reason": self.write_refusal_reason(),
                "transitions": self._history}
