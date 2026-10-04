"""Canonical ``WaitingRef`` vocabulary, owned by the waiting domain (LR-4).

S7 Phase-F, order sections 10-13. Before this module the LR-2 domain validator and the
ACTIVITY_CONSEQUENCE admission verifier each kept a hand-written prefix set:

* LR-2 ``activity_continuity.VALID_WAITING_REF_PREFIXES``  -- 22 prefixes
* admission ``activity_admission.ALLOWED_WAITING_REF_PREFIXES`` -- 13 prefixes

They overlapped in only 6 entries and neither contained the other. Two layers therefore
disagreed about what a WAIT is, and a reference produced by a real owner could be refused
by the layer that is supposed to represent canonical state. That was not theoretical: AG-2's
own default ``wait_dep:<decision_id>`` wait reference (``life_integration_ag2`` DECISION_WAIT
branch) was inside the admission allow-list but outside the LR-2 domain vocabulary, so the
canonical WAIT transition it drives was rejected by LR-2's own external-ref validator.

One schema, one parser, one validator. ``VALID_WAITING_REF_KINDS`` is the waiting domain's
vocabulary; both allow-sets are now DERIVED from it, so they cannot drift apart again.

Scope note (order section 13): a *legal* waiting reference and a reference *sufficient to
authorise a consequence* are allowed to differ only when a kind genuinely is not waiting
evidence. Six prefixes that only the admission side used were reclassified -- see
``EXCLUDED_LEGACY_PREFIXES`` -- so this module declares that difference explicitly instead of
re-typing a second literal list. Unsupported kinds fail closed: they are not accepted as
evidence at all.
"""
from __future__ import annotations

from enum import Enum
from typing import Any, Mapping, Optional


class WaitingRefRejected(ValueError):
    """A waiting reference is not in the canonical waiting-domain schema."""


class WaitingRefKind(str, Enum):
    """The canonical waiting-reference kinds (order section 11 recon, section 12 schema).

    ``str``-backed so a kind compares equal to its own prefix text, which keeps the
    existing string-based call sites (journals, LR-2 payloads, test data) unchanged.
    """

    # -- dependency family: another owner owes us a result ------------------------
    WAIT_COND = "wait_cond"
    RESUME_COND = "resume_cond"
    RCOND = "rcond"
    WEP = "wep"
    WAIT_DEP = "wait_dep"

    # -- external-result family: an external job / delivery / export settles -----
    TOOL_RESULT = "tool_result"
    TOOL_JOB = "tool_job"
    EXTERNAL_COND = "external_cond"
    JOB = "job"
    SHOP = "shop"
    DELIVERY = "delivery"
    NET = "net"
    EXPORT = "export"
    CONDITION = "condition"

    # -- event family: an observed event / receipt / reply ----------------------
    EVENT = "event"
    OBS = "obs"
    ACTION_RECEIPT = "action_receipt"
    USER_REPLY = "user_reply"
    NPC = "npc"
    WORLD_COND = "world_cond"

    # -- time family: a clock / timer / window boundary ------------------------
    TIME = "time"
    TIMER_COND = "timer_cond"
    WINDOW = "window"

    @property
    def prefix(self) -> str:
        return f"{self.value}:"


#: The waiting domain's vocabulary. Everything else in this module is derived from it.
VALID_WAITING_REF_KINDS: frozenset[WaitingRefKind] = frozenset(WaitingRefKind)

VALID_WAITING_REF_PREFIXES: frozenset[str] = frozenset(kind.value for kind in VALID_WAITING_REF_KINDS)

#: Reference kinds sufficient to authorise a WAIT consequence. Same schema: a kind that is
#: a legal waiting reference is by construction evidence about why an Activity waits, so
#: there is no second list to keep in sync. Kinds deliberately NOT here are listed in
#: ``EXCLUDED_LEGACY_PREFIXES`` with the reason each one is not waiting evidence.
ADMISSIBLE_WAITING_EVIDENCE_KINDS: frozenset[WaitingRefKind] = VALID_WAITING_REF_KINDS
ADMISSIBLE_WAITING_EVIDENCE_PREFIXES: frozenset[str] = VALID_WAITING_REF_PREFIXES

#: Condition kinds the waiting domain can build, mapped to the waiting-reference kinds each
#: one may legitimately be gated on. Keys mirror ``activity_waiting_agenda.COND_KIND_*``;
#: ``tests/test_s7f_waiting_ref_contract.py`` fails if they ever diverge, so the mapping
#: cannot silently rot into a second vocabulary.
CONDITION_KIND_WAITING_REF_KINDS: Mapping[str, frozenset[WaitingRefKind]] = {
    "TIME_AT_OR_AFTER": frozenset(
        {WaitingRefKind.TIME, WaitingRefKind.TIMER_COND, WaitingRefKind.WINDOW}
    ),
    "TIME_WINDOW_OPEN": frozenset(
        {WaitingRefKind.TIME, WaitingRefKind.TIMER_COND, WaitingRefKind.WINDOW}
    ),
    "EXTERNAL_RESULT_EVENT": frozenset(
        {
            WaitingRefKind.TOOL_RESULT,
            WaitingRefKind.TOOL_JOB,
            WaitingRefKind.EXTERNAL_COND,
            WaitingRefKind.JOB,
            WaitingRefKind.SHOP,
            WaitingRefKind.DELIVERY,
            WaitingRefKind.NET,
            WaitingRefKind.EXPORT,
            WaitingRefKind.CONDITION,
            WaitingRefKind.WAIT_DEP,
            WaitingRefKind.WAIT_COND,
            WaitingRefKind.RESUME_COND,
            WaitingRefKind.RCOND,
            WaitingRefKind.WEP,
        }
    ),
    "EVENT_MATCH": frozenset(
        {
            WaitingRefKind.EVENT,
            WaitingRefKind.OBS,
            WaitingRefKind.ACTION_RECEIPT,
            WaitingRefKind.USER_REPLY,
            WaitingRefKind.NPC,
            WaitingRefKind.WORLD_COND,
            WaitingRefKind.CONDITION,
        }
    ),
    "OBSERVED_CONDITION": frozenset(
        {
            WaitingRefKind.WORLD_COND,
            WaitingRefKind.OBS,
            WaitingRefKind.CONDITION,
            WaitingRefKind.EVENT,
        }
    ),
    "ALL": VALID_WAITING_REF_KINDS,
    "ANY": VALID_WAITING_REF_KINDS,
}

#: Prefixes the pre-Phase-F admission allow-list carried that are NOT waiting references.
#: Recorded so the removal is an evidenced decision rather than a silent narrowing.
EXCLUDED_LEGACY_PREFIXES: Mapping[str, str] = {
    "external_result": (
        "LR-5 result-reference kind (activity_progress_completion_lr5 recognises "
        "external_result:/export:/tool_result:/tool: as canonical RESULT refs); it names a "
        "settled outcome, not a reason an Activity is waiting"
    ),
    "tool": (
        "LR-5 result-reference kind, same recogniser as external_result:; an action target "
        "prefix (tool:generate_report) in AR-0/AR-1, not a waiting reference"
    ),
    "world_fact": (
        "LR-5 result-reference / world-effect kind written by AR-1 settlement; a confirmed "
        "effect of an action, not a dependent condition"
    ),
    "wept": "no producer, no owner, no call site; UNSUPPORTED -- fails closed",
    "resource": "no producer, no owner, no call site; UNSUPPORTED -- fails closed",
    "constraint": "no producer, no owner, no call site; UNSUPPORTED -- fails closed",
}


def parse_waiting_ref(ref: Any) -> Optional[tuple[WaitingRefKind, str]]:
    """``"<kind>:<rest>"`` -> ``(kind, rest)``; anything else -> ``None``.

    ``None`` is the fail-closed answer, never a guessed kind: the schema is read from the
    reference, it is never inferred from how the string happens to look.
    """
    if not isinstance(ref, str):
        return None
    head, sep, rest = ref.partition(":")
    if not sep or not rest.strip():
        return None
    try:
        kind = WaitingRefKind(head)
    except ValueError:
        return None
    return kind, rest


def waiting_ref_kind(ref: Any) -> Optional[WaitingRefKind]:
    parsed = parse_waiting_ref(ref)
    return parsed[0] if parsed is not None else None


def validate_waiting_ref(ref: Any, field_name: str = "waiting_on_ref") -> str:
    """Return ``ref`` unchanged when it is a canonical waiting reference, else raise."""
    if parse_waiting_ref(ref) is None:
        raise WaitingRefRejected(
            f"{field_name} must be a structured '<kind>:<id>' waiting reference from the "
            f"waiting-domain vocabulary; got {ref!r}"
        )
    return str(ref)


def is_valid_waiting_ref(ref: Any) -> bool:
    return parse_waiting_ref(ref) is not None
