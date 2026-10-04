"""LPC0B sections 17-22, 26-29, 34-35, 41-47, 53: the production cognition shadow harness.

Authority: the order wants the real model to think without its Decision ever entering
canonical Decision/Adoption/Activity.

Shape
-----
```
real Telegram turn
  -> existing pre_llm_call (Life hook)          non-blocking
  -> typed ObservedUserRequestEvent (in-memory)
  -> AG-0 CandidateSet                          local, no network
  -> bounded, deduplicated queue
  -> return None                               the chat reply never waits
background worker (concurrency 1)
  -> DecisionOpportunity (AG-1 factory, untouched)
  -> AG-1 service wired to a SHADOW-namespaced store and the REAL cognition adapter
  -> CognitionShadowRecord
```

Why no canonical write can happen
---------------------------------
The worker drives a *second* `AgencyDecisionService` whose store root is
`<INSTALL_ROOT>/shadow-state` and whose namespace is `shadow`.  It shares only the live
runtime's **read** services and its single already-held `ActivityAuthorityLease` (used to
sign the shadow capability).  There is no second `ActivityCommandService`, no second LR-2
store and no AG-2 coordinator in this path, so `ACTIVITY_CANONICAL_WRITER_COUNT` stays 1 and
`SHADOW_ADOPTION` / `SHADOW_ACTIVITY_WRITE` are 0 by construction.

Why the model cannot escalate (order section 32)
------------------------------------------------
The adapter returns the model's raw output; AG-1's `DecisionOutputValidator` accepts exactly
nine top-level keys and rejects anything else, so an injected instruction that makes the
model emit `grant`, `permit`, `decision_id` or a fabricated candidate is refused by AG-1, not
by trust.

Idempotency and staleness (sections 19, 21)
-------------------------------------------
One stable `request_key` per (trigger, CandidateSet, observed epoch).  Re-enqueueing it any
number of times yields at most one provider call.  A queued request that is no longer the
newest epoch is recorded `STALE_SHADOW` without spending a call; an in-flight result whose
epoch went stale is recorded `STALE_SHADOW` and never committed.
Send state and crash safety (LPC0B-R2 Gap 1)
--------------------------------------------
Every request carries a persisted send state, appended to
`<install_root>/logs/cognition/send_state.jsonl`, using exactly these words:
`NOT_SENT -> SEND_STARTED -> RESPONSE_RECEIVED -> SETTLED`.  The transitions are written at the
dequeue/reserve boundary, immediately before the transport call, immediately after it returns,
and right after the shadow record exists.  On start-up the journal is replayed: a request whose
last state is `SEND_STARTED` never reached a response and is marked `UNKNOWN_SEND_STATE`,
recording its `request_id`, `request_key`, `transport_attempt_id` and `started_at`.
**UNKNOWN_SEND_STATE is never retried, never becomes a FAILED decision, never becomes NO_ACTION
and never produces a DecisionRecord**; it can only be superseded by a genuinely new candidate
epoch, which mints a new request key.  `unknown_send_state_count` and
`unknown_send_state_auto_retry` (always 0) make that provable from outside the process.

Late provider results (LPC0B-R2 Gap 2)
-------------------------------------
The host transport is not cancellable, so a result that arrives after its deadline is recorded
`LATE_DISCARDED` in the shadow journal and discarded: it is never promoted into a shadow
DecisionRecord, never produces canonical effect, and never touches a newer epoch.
`late_result_promoted` and `late_response_overwrote_newer_epoch` both stay 0.

Shadow journal retention (LPC0B-R2 Gap 3)
----------------------------------------
`logs/cognition/cognition_shadow.jsonl` holds **debug shadow records only**.  Its retention
(`shadow_journal_max_records` / `shadow_journal_max_bytes`, defaults 5000 / 2000000) rotates the
file to `cognition_shadow.<utc-timestamp>.jsonl` and keeps the newest three rotated files.  This
retention deliberately governs the debug shadow journal ONLY: it is strictly separate from the
canonical AG-1 Decision journal, which is a different file in a different root and is never
rotated, pruned or deleted by any code path here.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
import uuid
from collections import deque
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

logger = logging.getLogger("chiyo_life_cognition")

#: Outcome words for one cognition attempt (order section 27).
RESULT_VALID = "VALID"
RESULT_RULE_RESOLVED = "RULE_RESOLVED_NO_LLM"
RESULT_STALE = "STALE_SHADOW"
RESULT_INVALID = "INVALID_OUTPUT"
RESULT_PROVIDER_ERROR = "PROVIDER_ERROR"
RESULT_BUDGET_REFUSED = "BUDGET_REFUSED"
RESULT_NOT_CONFIGURED = "COGNITION_NOT_CONFIGURED"
RESULT_ERROR = "ERROR"
RESULT_LATE_DISCARDED = "LATE_DISCARDED"
RESULT_UNKNOWN_SEND_STATE = "UNKNOWN_SEND_STATE"
RESULT_RESUMED_SETTLED = "RESUMED_SETTLED"

#: Gap 1 -- the per-request send state machine, persisted append-only to
#: `<install_root>/logs/cognition/send_state.jsonl`.  These four words are the whole machine;
#: UNKNOWN_SEND_STATE is the only terminal word for a state that cannot be known.
SEND_NOT_SENT = "NOT_SENT"
SEND_STARTED = "SEND_STARTED"
SEND_RESPONSE_RECEIVED = "RESPONSE_RECEIVED"
SEND_SETTLED = "SETTLED"
SEND_UNKNOWN = "UNKNOWN_SEND_STATE"
SEND_STATE_WORDS = (SEND_NOT_SENT, SEND_STARTED, SEND_RESPONSE_RECEIVED, SEND_SETTLED, SEND_UNKNOWN)

#: Recovery verdicts for a replayed journal (never written by a live request).
SEND_SAFE_TO_REPROCESS = "SAFE_TO_REPROCESS"
SEND_RESUMED_SETTLED = "RESUMED_SETTLED"
SEND_LATE_DISCARDED = "LATE_DISCARDED"

#: Gap 3 -- retention of the DEBUG shadow journal only (never the canonical AG-1 journal).
DEFAULT_SHADOW_JOURNAL_MAX_RECORDS = 5000
DEFAULT_SHADOW_JOURNAL_MAX_BYTES = 2000000
SHADOW_JOURNAL_ROTATED_KEEP = 3
SHADOW_JOURNAL_ROTATED_PREFIX = "cognition_shadow."

_THREAD_STATE = threading.local()


def _sha(value: Any, n: int = 16) -> str:
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:n]


def in_cognition() -> bool:
    """True while this thread is inside a cognition call (order section 26 recursion guard)."""
    return bool(getattr(_THREAD_STATE, "in_cognition", False))


@dataclass
class CognitionShadowRecord:
    """One cognition attempt.  Bounded: never a full prompt, never private text."""

    record_id: str
    at: str
    result: str
    request_key: str
    turn_hash: str
    trigger_ref: str
    opportunity_id: Optional[str]
    candidate_set_id: Optional[str]
    candidate_count: int
    candidate_ids: list
    candidate_kinds: list
    provider: Optional[str]
    model: Optional[str]
    provider_calls: int
    epoch_replayed: bool = False
    latency_ms: Optional[int] = None
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    total_tokens: Optional[int] = None
    reported_cost_usd: Any = None
    usage_source: Optional[str] = None
    decision_kind: Optional[str] = None
    selected_candidate_id: Optional[str] = None
    reason_codes: list = field(default_factory=list)
    validation: Optional[str] = None
    failure_class: Optional[str] = None
    error_category: Optional[str] = None
    stale: bool = False
    canonical_decision_written: bool = False
    adoption_created: bool = False
    activity_mutation: bool = False
    queue_depth_at_dequeue: int = 0
    evidence: dict = field(default_factory=dict)
    # LPC0B-R2: send-state / late-result evidence carried by every shadow record.
    transport_attempt_id: Optional[str] = None
    send_state: Optional[str] = None
    late_result_received: bool = False
    late_result_promoted: bool = False
    resume_verdict: Optional[str] = None
    decision_record_produced: bool = False


class CognitionShadowHarness:
    """Owns the queue, the worker and the shadow journal.  Never writes canonical state."""

    def __init__(
        self,
        *,
        runtime: Any,
        install_root: Path | str,
        adapter_factory: Callable[[Callable[[Mapping[str, Any]], None]], Any],
        max_queue: int = 64,
        concurrency: int = 1,
        enabled: bool = True,
        state_hook: Optional[Callable[[str], None]] = None,
    ) -> None:
        self.runtime = runtime
        self.install_root = Path(install_root)
        self.shadow_state_root = self.install_root / "shadow-state"
        self.journal_dir = self.install_root / "logs" / "cognition"
        self.journal_path = self.journal_dir / "cognition_shadow.jsonl"
        self.send_state_path = self.journal_dir / "send_state.jsonl"
        self._retention = self._load_retention_limits()
        #: Offline test seam only (default None): called with the state word after each durable
        #: send-state write.  Anything it raises propagates, so a test can crash at an exact
        #: transition.  It never changes what is written.
        self.state_hook = state_hook
        self.max_queue = int(max_queue)
        self.concurrency = int(concurrency)
        self._adapter_factory = adapter_factory
        self.enabled = bool(enabled)

        self._lock = threading.RLock()
        self._queue: deque[dict[str, Any]] = deque()
        self._seen_request_keys: set[str] = set()
        self._cognition_turn_by_request_key: dict[str, str] = {}
        self._terminal_request_keys: set[str] = set()
        self._latest_epoch: Optional[str] = None
        self._inflight_epoch: Optional[str] = None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()

        self._shadow_store = None
        self._shadow_service = None
        self._shadow_error: Optional[str] = None
        self._adapter: Any = None
        self._observer_records: deque[dict[str, Any]] = deque(maxlen=512)
        self._send_state_lock = threading.Lock()
        self._journal_lock = threading.Lock()
        self._shadow_record_count: Optional[int] = None
        self._unknown_send_state: dict[str, dict[str, Any]] = {}
        self._no_retry_request_keys: set[str] = set()
        self._attempt_info: dict[str, dict[str, Any]] = {}
        self._late_epochs: dict[str, dict[str, Any]] = {}
        self._budget_marked: set[str] = set()
        self._resumed = False

        self.stats: dict[str, Any] = {
            "schema": "lpc0b.cognition_shadow_stats.v1",
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "turns_observed": 0,
            "candidate_sets_built": 0,
            "candidate_sets_empty": 0,
            "candidate_sets_with_candidates": 0,
            "requests_enqueued": 0,
            "requests_deduplicated": 0,
            "requests_dropped_queue_full": 0,
            "requests_processed": 0,
            "requests_stale_skipped": 0,
            "rule_resolved_no_llm": 0,
            "provider_calls": 0,
            "epoch_replays": 0,
            "budget_refusals": 0,
            "provider_errors": 0,
            "invalid_outputs": 0,
            "valid_outputs": 0,
            "last_error": None,
            "queue_depth_max": 0,
            "shadow_store_root": str(self.shadow_state_root),
            "send_state_path": str(self.send_state_path),
            "send_state_records_written": 0,
            "hook_invocations": 0,
            "cognition_eligible_sets": 0,
            "unknown_send_state_count": 0,
            "unknown_send_state_auto_retry": 0,
            "unknown_send_state_suppressed": 0,
            "unknown_send_state_request_keys": [],
            "late_results_received": 0,
            "late_results_promoted": 0,
            "late_response_overwrote_newer_epoch": 0,
            "late_results_superseded_by_newer_epoch": 0,
            "resumed_settles": 0,
            "shadow_journal_rotations": 0,
            "shadow_journal_records_current": 0,
            "shadow_journal_rotated_pruned": 0,
            "shadow_journal_max_records": self._retention["max_records"],
            "shadow_journal_max_bytes": self._retention["max_bytes"],
            "recursion_guard_hits": 0,
            "canonical_decision_written": 0,
            "adoptions_created": 0,
            "activity_mutations": 0,
        }

    # -- shadow AG-1 assembly ---------------------------------------------------------

    def _ensure_shadow_service(self) -> Any:
        if self._shadow_service is not None:
            return self._shadow_service
        if self._shadow_error is not None:
            return None
        if not self.enabled:
            return None
        # Gap 1 rule 5: replay the send-state journal once, before anything can be sent.
        self._recover_once()
        if self._shadow_error is not None:
            return None
        try:
            import agency_decision_ag1 as ag1
            import activity_continuity as ac

            self.shadow_state_root.mkdir(parents=True, exist_ok=True)
            os.chmod(self.shadow_state_root, 0o700)
            if ac.is_production_path(self.shadow_state_root):
                raise RuntimeError("shadow root must not be a production home")

            adapter = self._adapter or self._build_adapter()
            self._adapter = adapter
            budget = getattr(adapter, "_budget", None)
            if budget is not None and hasattr(budget, "check"):
                budget.check()
            self._mark_unknown_budget()
            if self.stats.get("budget_unknown_state_write_failed"):
                raise RuntimeError("BUDGET_UNKNOWN_STATE_JOURNAL_UNAVAILABLE")

            store = ag1.AgencyDecisionStore(self.shadow_state_root, namespace="shadow")
            capability = ag1.AgencyDecisionAuthority.issue_capability(
                self.runtime.lease,
                caller_module="agency_decision_service",
                store_root=self.shadow_state_root,
                namespace="shadow",
            )
            service = ag1.AgencyDecisionService(
                store=store,
                lease=self.runtime.lease,
                capability=capability,
                candidate_read_service=self.runtime.candidate_read,
                activity_read_service=self.runtime.activity_read,
                resume_eligibility_store=self.runtime.eligibility_store,
                cognition_adapter=adapter,
                max_schema_retries=0,
            )
            self._shadow_store = store
            self._shadow_service = service
            self.stats["shadow_service_ready"] = True
            self.stats["shadow_store_namespace"] = "shadow"
            self.stats["shadow_store_root"] = str(self.shadow_state_root)
            self.stats["max_schema_retries"] = 0
            return service
        except Exception as exc:  # noqa: BLE001
            self._shadow_error = f"{type(exc).__name__}: {exc}"
            self.stats["last_error"] = self._shadow_error
            logger.warning("shadow cognition service unavailable (fail closed): %s", self._shadow_error)
            return None

    def _build_adapter(self) -> Any:
        def observer(record: Mapping[str, Any]) -> None:
            payload = dict(record)
            self._observer_records.append(payload)
            event = payload.get("event")
            if event in ("cognition_send_started", "cognition_send_failed",
                         "cognition_send_response_received", "cognition_late_result"):
                # Gap 1/2: turn the adapter's transport boundaries into durable state.
                self._on_adapter_send_event(payload)

        return self._adapter_factory(observer)

    # -- non-blocking hook path -------------------------------------------------------

    def observe_turn(self, *, session_hash: str, turn_hash: str, platform: str,
                     observed_at: str) -> dict[str, Any]:
        """Called from the ``pre_llm_call`` hook.  Must be fast, must never raise."""
        started = time.monotonic()
        out: dict[str, Any] = {"enqueued": False, "reason": None}
        try:
            if not self.enabled:
                out["reason"] = "SHADOW_DISABLED"
                return out
            if in_cognition():
                out["reason"] = "COGNITION_RECURSION_GUARD"
                self.stats["recursion_guard_hits"] = int(self.stats.get("recursion_guard_hits", 0)) + 1
                return out
            import activity_interruption as _ai  # noqa: F401  (ensures the release is on sys.path)

            with self._lock:
                self.stats["turns_observed"] += 1
                self.stats["hook_invocations"] += 1

            service = self._ensure_shadow_service()
            if service is None:
                out["reason"] = self._shadow_error or "SHADOW_SERVICE_UNAVAILABLE"
                return out

            import agency_decision_ag1 as ag1
            import candidate_sources_ag0 as ag0

            # A Telegram turn is a typed ORDINARY_COMMUNICATION observation, NOT proof of an
            # explicit request. AG-0 explicitly filters this event kind from USER_REQUEST
            # candidates. Other independently grounded candidate sources remain eligible.
            event_id = f"ureq:gw:{_sha(f'{platform}|{session_hash}|{turn_hash}', 24)}"
            event = ag0.ObservedUserRequestEvent(
                event_id=event_id, channel=platform, event_kind="ORDINARY_COMMUNICATION",
                request_status="NONE", target_refs=[f"comm:{platform}:{_sha(f'{session_hash}|{turn_hash}', 24)}"],
                occurred_at=observed_at, observed_at=observed_at, valid_from=observed_at,
            )
            self.runtime.user_request_source.ingest_typed_event(event)

            cset = self.runtime.candidate_materializer.build_candidate_set(observed_at=observed_at)
            # A frozen CandidateSet carries candidate REFS, not records (chiyo.agency.candidate_set.v1).
            candidate_refs = [str(r) for r in (getattr(cset, "candidate_refs", None) or [])]
            with self._lock:
                self.stats["candidate_sets_built"] += 1
                if candidate_refs:
                    self.stats["candidate_sets_with_candidates"] += 1
                else:
                    self.stats["candidate_sets_empty"] += 1

            # Order section 6: no cognition-eligible candidate means ZERO provider calls.
            if candidate_refs:
                with self._lock:
                    self.stats["cognition_eligible_sets"] += 1
            if not candidate_refs:
                out["reason"] = "NO_COGNITION_ELIGIBLE_CANDIDATE"
                return out

            # Order section 19: the key depends on the SOURCE EVENT plus the candidate CONTENT,
            # never on the freshly minted CandidateSet id, so re-observing the same turn still
            # costs exactly one call.
            cset_id = getattr(cset, "candidate_set_id", None)
            source_event_refs: list[str] = []
            for candidate_ref in candidate_refs:
                candidate_row = self.runtime.candidate_read.get_candidate(candidate_ref) or {}
                for ref in candidate_row.get("source_refs") or [candidate_row.get("source_ref")]:
                    if ref and str(ref) not in source_event_refs:
                        source_event_refs.append(str(ref))
            stable_event_identity = "|".join(sorted(source_event_refs)) or f"UNATTRIBUTED:{session_hash}:{turn_hash}"
            content_fp = _sha("|".join([stable_event_identity, *sorted(candidate_refs)]), 24)
            epoch = f"{stable_event_identity}|{content_fp}"
            request_key = f"creq:{_sha(epoch, 24)}"
            item = {
                "request_key": request_key, "epoch": epoch, "trigger_ref": event_id,
                "candidate_set_ref": cset_id, "candidate_set": cset, "observed_at": observed_at,
                "turn_hash": turn_hash, "platform": platform, "candidate_count": len(candidate_refs),
                "candidate_refs": candidate_refs, "candidate_set": cset,
                "trigger_kind": ("USER_REQUEST_EVENT" if any(
                    "USER_REQUEST" in ((self.runtime.candidate_read.get_candidate(cid) or {}).get("source_kinds") or [])
                    or (self.runtime.candidate_read.get_candidate(cid) or {}).get("source_kind") == "USER_REQUEST"
                    for cid in candidate_refs) else "CANDIDATE_SET_CHANGED"),
                "enqueued_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            }
            with self._lock:
                if request_key in self._no_retry_request_keys or request_key in self._terminal_request_keys:
                    # Gap 1 rule 6 / Gap 2: this request key's send state is already unknown, or
                    # its response was already bought.  Sending again would be a second paid call
                    # for work nobody can read back.  Only a genuinely NEW candidate epoch --
                    # which mints a new request key -- may be sent.
                    self.stats["unknown_send_state_suppressed"] = int(
                        self.stats.get("unknown_send_state_suppressed", 0)) + 1
                    out["reason"] = "SEND_STATE_NO_RETRY"
                    out["request_key"] = request_key
                    return out
                if request_key in self._seen_request_keys:
                    self.stats["requests_deduplicated"] += 1
                    out["reason"] = "DEDUPLICATED"
                    return out
                self._seen_request_keys.add(request_key)
                self._cognition_turn_by_request_key[request_key] = turn_hash
                if len(self._queue) >= self.max_queue:
                    # Keep the newest: drop the oldest queued item, never the arriving one.
                    self._queue.popleft()
                    self.stats["requests_dropped_queue_full"] += 1
                self._queue.append(item)
                self._latest_epoch = epoch
                self.stats["requests_enqueued"] += 1
                self.stats["queue_depth_max"] = max(self.stats["queue_depth_max"], len(self._queue))
                depth = len(self._queue)
            self._ensure_worker()
            out.update({"enqueued": True, "reason": "ENQUEUED", "request_key": request_key,
                        "epoch": epoch, "candidate_count": len(candidate_refs), "candidate_refs": candidate_refs, "queue_depth": depth})
        except Exception as exc:  # noqa: BLE001 - the chat turn must never break
            out["reason"] = f"OBSERVE_FAILED:{type(exc).__name__}"
            with self._lock:
                self.stats["last_error"] = f"observe_turn: {type(exc).__name__}: {exc}"
            logger.warning("cognition observe_turn failed (swallowed): %s", exc)
        finally:
            out["hook_ms"] = int((time.monotonic() - started) * 1000)
        return out

    # -- worker ----------------------------------------------------------------------

    def _ensure_worker(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(target=self._loop, name="chiyo-cognition-worker", daemon=True)
            self._thread.start()

    def _loop(self) -> None:
        while not self._stop.is_set():
            item = None
            depth = 0
            with self._lock:
                if self._queue:
                    item = self._queue.popleft()
                    depth = len(self._queue)
            if item is None:
                time.sleep(0.25)
                continue
            try:
                self._process(item, depth)
            except Exception as exc:  # noqa: BLE001
                logger.warning("cognition worker item failed: %s", exc)
                with self._lock:
                    self.stats["last_error"] = f"worker: {type(exc).__name__}: {exc}"

    def _process(self, item: Mapping[str, Any], depth: int) -> None:
        import agency_decision_ag1 as ag1

        service = self._shadow_service
        adapter = self._adapter
        request_key = str(item["request_key"])
        epoch = str(item["epoch"])

        # Order section 21: a queued request that is no longer the newest epoch costs nothing.
        with self._lock:
            newest = self._latest_epoch
        if newest is not None and newest != epoch:
            self._record(item, depth, result=RESULT_STALE, provider_calls=0, stale=True,
                         error_category="SUPERSEDED_BY_NEWER_EPOCH")
            with self._lock:
                self.stats["requests_stale_skipped"] += 1
            return
        if request_key in self._no_retry_request_keys or request_key in self._terminal_request_keys:
            # Gap 1 rule 6 / ABSOLUTE: a request whose send state is unknown (or whose response
            # was already received) is NEVER re-sent.  It is recorded, never promoted, and it
            # can never become a FAILED->NO_ACTION decision or a DecisionRecord.
            self._record(item, depth, result=RESULT_UNKNOWN_SEND_STATE, provider_calls=0,
                         error_category="UNKNOWN_SEND_STATE_NO_RETRY",
                         resume_verdict=SEND_UNKNOWN, send_state=SEND_UNKNOWN)
            with self._lock:
                self.stats["unknown_send_state_suppressed"] = int(
                    self.stats.get("unknown_send_state_suppressed", 0)) + 1
            return

        # Gap 1 transition 1: the request was dequeued and its budget slot is about to be
        # reserved.  NOT_SENT means no transmission has started, so a crash here is safe and the
        # request may be reprocessed without any possibility of a double spend.
        self.mark_request_reserved(item)

        cset = item["candidate_set"]
        observed_at = str(item["observed_at"])
        try:
            current_ref = None
            for act in self.runtime.activity_read.list_open_activities():
                if act.get("status") == "ACTIVE":
                    current_ref = act.get("activity_id")
                    break
            opp = ag1.DecisionOpportunity.create(
                trigger_kind=str(item.get("trigger_kind") or ag1.TRIGGER_CANDIDATE_SET_CHANGED),
                trigger_ref=str(item["trigger_ref"]),
                candidate_set=cset,
                current_activity_ref=current_ref,
                observed_at=observed_at,
                causal_parent_refs=[str(item["trigger_ref"]), str(item["candidate_set_ref"])],
            )
        except Exception as exc:  # noqa: BLE001
            self._record(item, depth, result=RESULT_ERROR, provider_calls=0,
                         error_category=f"OPPORTUNITY_{type(exc).__name__}")
            return

        calls_before = int(getattr(adapter, "call_count", 0))
        _THREAD_STATE.in_cognition = True
        # Gap 1: the adapter's transport-boundary observations are attributed to THIS request.
        _THREAD_STATE.send_state_ctx = {"request_key": request_key, "epoch": epoch,
                                        "turn_hash": item.get("turn_hash"),
                                        "transport_attempt_id": None}
        if hasattr(adapter, "_request_key"):
            adapter._request_key = request_key
        with self._lock:
            self._inflight_epoch = epoch
        try:
            result = service.evaluate_opportunity(opportunity=opp, candidate_set=cset)
        except Exception as exc:  # noqa: BLE001 - never fatal
            calls_after = int(getattr(adapter, "call_count", 0))
            category = getattr(exc, "failure_class", None) or type(exc).__name__
            attempt = self._attempt_info.get(epoch) or {}
            late_info = self._late_epochs.pop(epoch, None)
            if late_info is not None:
                self._record(item, depth, result=RESULT_LATE_DISCARDED,
                             provider_calls=max(0, calls_after - calls_before),
                             failure_class="LATE_RESULT_DISCARDED",
                             error_category="TRANSPORT_DEADLINE_EXCEEDED",
                             late_result_received=True, late_result_promoted=False,
                             send_state=SEND_RESPONSE_RECEIVED)
                with self._lock:
                    self.stats["requests_processed"] += 1
                    self.stats["provider_calls"] += max(0, calls_after - calls_before)
                return
            if attempt.get("send_state") == SEND_RESPONSE_RECEIVED and attempt.get("late"):
                self._record(item, depth, result=RESULT_LATE_DISCARDED,
                             provider_calls=max(0, calls_after - calls_before),
                             failure_class="LATE_RESULT_DISCARDED",
                             error_category="TRANSPORT_DEADLINE_EXCEEDED",
                             late_result_received=True, late_result_promoted=False,
                             send_state=SEND_RESPONSE_RECEIVED)
                with self._lock:
                    self.stats["requests_processed"] += 1
                    self.stats["provider_calls"] += max(0, calls_after - calls_before)
                return
            if attempt.get("send_state") == SEND_STARTED:
                # After the durable transmission boundary, an exception before a durable
                # response is ambiguous: the provider may have received and billed it.
                # Never turn it into a failure Decision or a retryable request.
                self._write_send_state(request_key=request_key, state=SEND_UNKNOWN, epoch=epoch,
                                       turn_hash=item.get("turn_hash"),
                                       transport_attempt_id=attempt.get("transport_attempt_id"),
                                       started_at=attempt.get("started_at"),
                                       detail="EXCEPTION_AFTER_SEND_STARTED_WITHOUT_RESPONSE")
                self._register_unknown({"request_key": request_key, "epoch": epoch,
                                        "turn_hash": item.get("turn_hash"),
                                        "transport_attempt_id": attempt.get("transport_attempt_id"),
                                        "started_at": attempt.get("started_at")}, verdict=SEND_UNKNOWN)
                self._record(item, depth, result=RESULT_UNKNOWN_SEND_STATE, provider_calls=0,
                             error_category="UNKNOWN_SEND_STATE", resume_verdict=SEND_UNKNOWN,
                             send_state=SEND_UNKNOWN)
            else:
                self._record(item, depth, result=RESULT_ERROR, provider_calls=calls_after - calls_before,
                             error_category=str(category)[:120])
            with self._lock:
                self.stats["requests_processed"] += 1
                self.stats["provider_calls"] += max(0, calls_after - calls_before)
            return
        finally:
            if hasattr(adapter, "_request_key"):
                adapter._request_key = None
            _THREAD_STATE.in_cognition = False
            _THREAD_STATE.send_state_ctx = None
            # Gap 2: only the epoch that owns the in-flight slot may release it, so a late
            # completion of an older epoch can never clear a newer epoch's slot.
            self._release_inflight(epoch)

        calls_after = int(getattr(adapter, "call_count", 0))
        provider_calls = max(0, calls_after - calls_before)
        attempt = self._attempt_info.get(epoch) or {}
        if attempt.get("send_state") == SEND_STARTED:
            # AG-1 converts adapter exceptions into a FAILED attempt result, so handle the
            # ambiguous transport window here before it can be mislabeled as a settled failure.
            # A SEND_STARTED with no durable response status is unknown and permanently no-retry.
            self._write_send_state(request_key=request_key, state=SEND_UNKNOWN, epoch=epoch,
                                   turn_hash=item.get("turn_hash"),
                                   transport_attempt_id=attempt.get("transport_attempt_id"),
                                   started_at=attempt.get("started_at"),
                                   detail="AG1_RETURNED_WITHOUT_DURABLE_PROVIDER_RESPONSE")
            self._register_unknown({"request_key": request_key, "epoch": epoch,
                                    "turn_hash": item.get("turn_hash"),
                                    "transport_attempt_id": attempt.get("transport_attempt_id"),
                                    "started_at": attempt.get("started_at")}, verdict=SEND_UNKNOWN)
            self._record(item, depth, result=RESULT_UNKNOWN_SEND_STATE, provider_calls=0,
                         error_category="UNKNOWN_SEND_STATE", resume_verdict=SEND_UNKNOWN,
                         send_state=SEND_UNKNOWN)
            with self._lock:
                self.stats["requests_processed"] += 1
                # SEND_STARTED is evidence of one attempted transmission, even if the host
                # threw before usage became observable; budget reservation already accounts it.
                self.stats["provider_calls"] += max(1, provider_calls)
            return

        decision_record = result.get("decision_record") if isinstance(result, dict) else None
        failure_class = result.get("failure_class") if isinstance(result, dict) else None
        status = result.get("status") if isinstance(result, dict) else None

        late_info = self._late_epochs.pop(epoch, None)
        if late_info is not None:
            # Gap 2: the provider answered after its own deadline.  Recorded LATE_DISCARDED and
            # discarded -- never promoted into a DecisionRecord, never applied to another epoch,
            # zero canonical effect.  No retry: that call was already paid for.
            self._record(item, depth, result=RESULT_LATE_DISCARDED, provider_calls=provider_calls,
                         failure_class="LATE_RESULT_DISCARDED",
                         error_category="TRANSPORT_DEADLINE_EXCEEDED",
                         validation=str(status) if status else None,
                         late_result_received=True, late_result_promoted=False,
                         send_state=SEND_RESPONSE_RECEIVED)
            with self._lock:
                self.stats["requests_processed"] += 1
                self.stats["provider_calls"] += provider_calls
            return

        if decision_record is not None:
            decided = getattr(decision_record, "decision", None) or (
                decision_record.get("decision") if isinstance(decision_record, Mapping) else None)
            selected = getattr(decision_record, "selected_candidate_id", None) or (
                decision_record.get("selected_candidate_id") if isinstance(decision_record, Mapping) else None)
            reasons = getattr(decision_record, "reason_codes", None) or (
                decision_record.get("reason_codes") if isinstance(decision_record, Mapping) else None)
            result_word = RESULT_VALID
            with self._lock:
                self.stats["valid_outputs"] += 1
        elif provider_calls == 0 and (failure_class
                                      or str(status or "").upper() in ("FAILED", "ERROR")):
            # LPC0B-R2: a provider/transport failure that AG-1 turned into a failed attempt must
            # never be recorded as a rule-resolved no-LLM outcome.  Still not a decision.
            decided, selected, reasons = None, None, []
            result_word = RESULT_PROVIDER_ERROR
            with self._lock:
                self.stats["provider_errors"] += 1
        elif provider_calls == 0:
            decided, selected, reasons = None, None, []
            result_word = RESULT_RULE_RESOLVED
            with self._lock:
                self.stats["rule_resolved_no_llm"] += 1
        else:
            decided, selected, reasons = None, None, []
            result_word = RESULT_INVALID
            with self._lock:
                self.stats["invalid_outputs"] += 1

        self._record(item, depth, result=result_word, provider_calls=provider_calls,
                     decision_kind=decided, selected_candidate_id=selected,
                     reason_codes=list(reasons or []), failure_class=failure_class,
                     validation=str(status) if status else None)
        with self._lock:
            self.stats["requests_processed"] += 1
            self.stats["provider_calls"] += provider_calls
            self.stats["epoch_replays"] = int(getattr(adapter, "replayed_count", 0))
            self.stats["budget_refusals"] = int(getattr(adapter, "refused_count", 0))

    # -- journal ---------------------------------------------------------------------

    def _record(self, item: Mapping[str, Any], depth: int, *, result: str, provider_calls: int,
                stale: bool = False, decision_kind: Optional[str] = None,
                selected_candidate_id: Optional[str] = None, reason_codes: Optional[list] = None,
                failure_class: Optional[str] = None, validation: Optional[str] = None,
                error_category: Optional[str] = None, send_state: Optional[str] = None,
                transport_attempt_id: Optional[str] = None, resume_verdict: Optional[str] = None,
                late_result_received: bool = False,
                late_result_promoted: bool = False) -> None:
        usage: dict[str, Any] = {}
        for rec in reversed(self._observer_records):
            if rec.get("epoch") == item.get("epoch") and rec.get("event") == "cognition_result":
                usage = dict(rec)
                break
        record = CognitionShadowRecord(
            record_id=f"csr:{uuid.uuid4().hex[:16]}",
            at=time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            result=result,
            request_key=str(item.get("request_key")),
            turn_hash=_sha(str(item.get("turn_hash")), 16),
            trigger_ref=(str(item.get("trigger_ref"))[:120] if str(item.get("trigger_ref")) != "None" else "unknown"),
            opportunity_id=None,
            candidate_set_id=None,
            candidate_count=int(item.get("candidate_count") or 0),
            candidate_ids=[_sha(value, 16) for value in list(item.get("candidate_refs") or [])[:16]],
            candidate_kinds=[],
            provider=getattr(self._adapter, "model_id", None) and getattr(self._adapter, "_client", None) and getattr(self._adapter._client, "provider", None),
            model=getattr(self._adapter, "_client", None) and getattr(self._adapter._client, "model", None),
            provider_calls=provider_calls,
            epoch_replayed=bool(usage.get("event") == "cognition_epoch_replay"),
            latency_ms=usage.get("latency_ms"),
            input_tokens=usage.get("input_tokens"),
            output_tokens=usage.get("output_tokens"),
            total_tokens=usage.get("total_tokens"),
            reported_cost_usd=usage.get("reported_cost_usd"),
            usage_source=usage.get("usage_source"),
            decision_kind=decision_kind,
            selected_candidate_id=selected_candidate_id,
            reason_codes=list(reason_codes or []),
            validation=validation,
            failure_class=failure_class,
            error_category=error_category,
            stale=stale,
            queue_depth_at_dequeue=depth,
            evidence={"platform": item.get("platform"), "observed_at": item.get("observed_at")},
            transport_attempt_id=(transport_attempt_id or item.get("transport_attempt_id")
                                  or (self._attempt_info.get(str(item.get("epoch"))) or {}).get("transport_attempt_id")),
            send_state=send_state,
            late_result_received=bool(late_result_received),
            # Gap 2 / ABSOLUTE: no call site may ever pass True here, and the aggregate
            # counter late_results_promoted is never incremented anywhere in this module.
            late_result_promoted=bool(late_result_promoted),
            resume_verdict=resume_verdict,
            decision_record_produced=(result == RESULT_VALID),
        )
        payload = asdict(record)
        if not self._append_shadow_record(payload):
            raise RuntimeError("COGNITION_SHADOW_JOURNAL_WRITE_FAILED")
        self._terminal_request_keys.add(str(item.get("request_key")))
        # Gap 1 transition 4: SETTLED only follows a durable response marker, after the
        # shadow journal write. Ambiguous SEND_STARTED must stay unknown, never be mislabeled.
        attempt = self._attempt_info.get(str(item.get("epoch"))) or {}
        if (attempt.get("transport_attempt_id") and resume_verdict is None
                and attempt.get("send_state") == SEND_RESPONSE_RECEIVED):
            self._write_send_state(request_key=str(item.get("request_key")), state=SEND_SETTLED,
                                   epoch=item.get("epoch"), turn_hash=item.get("turn_hash"),
                                   transport_attempt_id=attempt.get("transport_attempt_id"),
                                   started_at=attempt.get("started_at"), detail=str(result))
            with self._lock:
                self._no_retry_request_keys.add(str(item.get("request_key")))
                self._terminal_request_keys.add(str(item.get("request_key")))
        with self._lock:
            self.stats["last_record"] = {k: payload.get(k) for k in
                                         ("at", "result", "provider_calls", "decision_kind",
                                          "selected_candidate_id", "latency_ms", "stale",
                                          "send_state", "late_result_received")}

    # -- send-state machine (LPC0B-R2 Gap 1) -------------------------------------------

    @staticmethod
    def _load_retention_limits() -> dict[str, int]:
        """Gap 3 knobs: cognition config first, module defaults second.  Never raises."""
        cfg: Mapping[str, Any] = {}
        try:
            import agency_cognition_config as _cognition_config
            cfg = _cognition_config.load_cognition_config() or {}
        except Exception:  # noqa: BLE001 - retention must never break cognition
            cfg = {}

        def _limit(key: str, default: int) -> int:
            try:
                value = int(cfg.get(key, default))
            except (TypeError, ValueError):
                return default
            return value if value > 0 else default

        return {"max_records": _limit("shadow_journal_max_records",
                                      DEFAULT_SHADOW_JOURNAL_MAX_RECORDS),
                "max_bytes": _limit("shadow_journal_max_bytes",
                                    DEFAULT_SHADOW_JOURNAL_MAX_BYTES)}

    @staticmethod
    def _ss_kwargs(entry: Mapping[str, Any]) -> dict[str, Any]:
        return {"epoch": entry.get("epoch"), "turn_hash": entry.get("turn_hash"),
                "transport_attempt_id": entry.get("transport_attempt_id"),
                "started_at": entry.get("started_at")}

    def _write_send_state(self, *, request_key: str, state: str, epoch: Optional[str] = None,
                          turn_hash: Optional[str] = None,
                          transport_attempt_id: Optional[str] = None,
                          started_at: Optional[str] = None,
                          response_digest: Optional[str] = None,
                          response_verifiable: Optional[bool] = None,
                          late: bool = False, detail: Optional[str] = None,
                          recovered: bool = False) -> dict[str, Any]:
        """Append ONE send-state transition (Gap 1).  Durable before this returns."""
        record: dict[str, Any] = {
            "schema": "lpc0b.cognition_send_state.v1",
            "request_id": f"cogreq:{_sha(request_key, 16)}",
            "at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "state": state,
            "request_key": request_key,
            "epoch": epoch,
            "turn_hash": turn_hash,
            "transport_attempt_id": transport_attempt_id,
            "started_at": started_at,
            "response_digest": response_digest,
            "response_verifiable": response_verifiable,
            "late": bool(late),
            "recovered": bool(recovered),
            "detail": detail,
        }
        if (state in (SEND_STARTED, SEND_RESPONSE_RECEIVED, SEND_SETTLED)
                and request_key in self._no_retry_request_keys
                and not recovered):
            # ABSOLUTE (Gap 1 rule 6): a request key whose send state is unknown/attempted
            # must never be sent again. This tripwire is a hard refusal.
            with self._lock:
                self.stats["unknown_send_state_auto_retry"] = int(
                    self.stats.get("unknown_send_state_auto_retry", 0)) + 1
            raise RuntimeError("SEND_STATE_NO_RETRY: refusing repeated SEND_STARTED")
        with self._send_state_lock:
            try:
                self.journal_dir.mkdir(parents=True, exist_ok=True)
                with self.send_state_path.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(record, ensure_ascii=False,
                                            separators=(",", ":")) + "\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                with self._lock:
                    self.stats["send_state_records_written"] = int(
                        self.stats.get("send_state_records_written", 0)) + 1
            except OSError as exc:
                logger.error("cognition send-state journal write failed closed: %s", exc)
                raise RuntimeError("SEND_STATE_JOURNAL_UNAVAILABLE: transmission boundary not durable") from exc
        if self.state_hook is not None:
            # Offline test seam: a raising hook is how an offline test simulates a crash at an
            # exact transition.  Production always passes state_hook=None.
            self.state_hook(state)
        return record

    def mark_request_reserved(self, item: Mapping[str, Any], *,
                              transport_attempt_id: Optional[str] = None) -> dict[str, Any]:
        """Gap 1 transition 1: the request was dequeued and its budget slot reserved."""
        return self._write_send_state(request_key=str(item.get("request_key")),
                                      state=SEND_NOT_SENT, epoch=item.get("epoch"),
                                      turn_hash=item.get("turn_hash"),
                                      transport_attempt_id=transport_attempt_id)

    def _on_adapter_send_event(self, record: Mapping[str, Any]) -> None:
        """Translate an adapter transport boundary into a durable send-state transition."""
        ctx = getattr(_THREAD_STATE, "send_state_ctx", None) or {}
        event = record.get("event")
        # The adapter's own epoch key (opportunity_id|snapshot_fingerprint) is NOT the cognition
        # epoch (event|content): the worker's context is authoritative, so the send-state journal,
        # the shadow record and the late-result guard all speak about the same request.
        epoch = str(ctx.get("epoch") or record.get("epoch") or "")
        if event == "cognition_late_result":
            request_key = str(ctx.get("request_key") or record.get("request_key") or "")
            attempt_id = record.get("transport_attempt_id") or ctx.get("transport_attempt_id")
            if request_key:
                if epoch:
                    info = self._attempt_info.setdefault(epoch, {})
                    info.update({"transport_attempt_id": attempt_id,
                                 "started_at": record.get("started_at"),
                                 "send_state": SEND_RESPONSE_RECEIVED, "late": True})
                self._write_send_state(
                    request_key=request_key, state=SEND_RESPONSE_RECEIVED,
                    epoch=epoch or None, turn_hash=ctx.get("turn_hash"),
                    transport_attempt_id=attempt_id, started_at=record.get("started_at"),
                    response_digest=record.get("response_digest"),
                    response_verifiable=record.get("response_verifiable"), late=True,
                    detail="LATE_RESPONSE_RECEIVED_DISCARDED")
            self._note_late_arrival(epoch or None, record)
            return
        request_key = str(ctx.get("request_key") or record.get("request_key") or "")
        if not request_key:
            return
        attempt_id = record.get("transport_attempt_id")
        if attempt_id and epoch:
            info = self._attempt_info.setdefault(epoch, {})
            info["transport_attempt_id"] = str(attempt_id)
            info["started_at"] = record.get("started_at") or info.get("started_at")
            info["late"] = (bool(record.get("late") or event == "cognition_late_result") if event in ("cognition_send_response_received", "cognition_late_result")
                            else info.get("late", False))
            info["send_state"] = (SEND_STARTED if event == "cognition_send_started"
                                   else SEND_RESPONSE_RECEIVED if event in ("cognition_send_response_received", "cognition_late_result")
                                   or (event == "cognition_send_failed" and record.get("response_received"))
                                   else info.get("send_state"))
            if len(self._attempt_info) > 256:
                for stale in list(self._attempt_info.keys())[:128]:
                    self._attempt_info.pop(stale, None)
        if event == "cognition_send_started":
            ctx["transport_attempt_id"] = attempt_id
            # Gap 1 transition 2: written immediately BEFORE the transport call.
            self._write_send_state(request_key=request_key, state=SEND_STARTED,
                                   epoch=epoch or None, turn_hash=ctx.get("turn_hash"),
                                   transport_attempt_id=attempt_id,
                                   started_at=record.get("started_at"))
        elif event == "cognition_send_failed":
            if record.get("response_received"):
                self._write_send_state(request_key=request_key, state=SEND_RESPONSE_RECEIVED,
                                       epoch=epoch or None, turn_hash=ctx.get("turn_hash"),
                                       transport_attempt_id=attempt_id,
                                       started_at=record.get("started_at"),
                                       response_verifiable=False,
                                       detail=f"DEFINITIVE_PROVIDER_ERROR:{record.get('error_category')}")
            # Otherwise SEND_STARTED remains the last durable state and recovery must
            # classify the provider outcome as unknown, never as a retryable failure.
        elif event == "cognition_send_response_received":
            # Gap 1 transition 3: written immediately AFTER the transport returned.
            self._write_send_state(request_key=request_key, state=SEND_RESPONSE_RECEIVED,
                                   epoch=epoch or None, turn_hash=ctx.get("turn_hash"),
                                   transport_attempt_id=attempt_id,
                                   started_at=record.get("started_at"),
                                   response_digest=record.get("response_digest"),
                                   response_verifiable=record.get("response_verifiable"),
                                   late=bool(record.get("late")))

    def _note_late_arrival(self, epoch: Optional[str], record: Mapping[str, Any]) -> None:
        """Gap 2: a late result is recorded, counted, and promoted into nothing."""
        with self._lock:
            self.stats["late_results_received"] = int(
                self.stats.get("late_results_received", 0)) + 1
            # late_results_promoted is NEVER incremented anywhere: a late result is discarded.
            latest = self._latest_epoch
            if epoch is not None and latest is not None and latest != epoch:
                # Older than the newest observed epoch: discarded, and the newer epoch is
                # neither read, written nor invalidated.
                self.stats["late_results_superseded_by_newer_epoch"] = int(
                    self.stats.get("late_results_superseded_by_newer_epoch", 0)) + 1
        key = str(epoch or "")
        self._late_epochs[key] = {
            "at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "elapsed_ms": record.get("elapsed_ms"),
            "transport_attempt_id": record.get("transport_attempt_id"),
            "response_verifiable": record.get("response_verifiable"),
            "late_result_promoted": False,
        }
        if len(self._late_epochs) > 64:
            for stale in list(self._late_epochs.keys())[:32]:
                self._late_epochs.pop(stale, None)

    def _release_inflight(self, epoch: str) -> bool:
        """Gap 2: only the epoch that OWNS the in-flight slot may release it."""
        with self._lock:
            owner = self._inflight_epoch
            if owner is None or owner == epoch:
                self._inflight_epoch = None
                return True
            # A late completion of an older epoch must never clear a newer epoch's slot.
            self.stats["late_response_overwrote_newer_epoch"] = int(
                self.stats.get("late_response_overwrote_newer_epoch", 0)) + 1
            return False

    def is_retry_allowed(self, request_key: str) -> bool:
        """Gap 1 rule 6, checkable from outside: unknown / already-answered keys are never re-sent."""
        return request_key not in self._no_retry_request_keys

    def _recover_once(self) -> None:
        if self._resumed:
            return
        try:
            outcome = self.resume_state()
            if outcome.get("unknown_send_state"):
                self._mark_unknown_budget()
        except Exception as exc:  # noqa: BLE001
            self._resumed = False
            # Recovery errors are fail-closed: if prior sends cannot be classified, do not
            # enqueue work or make any provider calls during this process lifetime.
            logger.error("cognition send-state recovery failed closed: %s", exc)
            self._shadow_error = f"SEND_STATE_RECOVERY_UNAVAILABLE:{type(exc).__name__}"
            with self._lock:
                self.stats["last_error"] = f"resume_state: {type(exc).__name__}: {exc}"
                self.stats["send_state_recovery_failed_closed"] = True

    def _read_send_state_journal(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        try:
            text = self.send_state_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return rows
        except OSError as exc:
            raise RuntimeError("SEND_STATE_JOURNAL_UNREADABLE") from exc
        journal_lines = text.splitlines()
        for line_no, line in enumerate(journal_lines, 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except ValueError as exc:
                # A truncated final JSONL record is a normal crash artifact, but malformed
                # content anywhere else is not proof that earlier provider sends are absent.
                if line_no == len(journal_lines):
                    break
                raise RuntimeError(f"SEND_STATE_JOURNAL_CORRUPT:{line_no}") from exc
            if isinstance(row, Mapping):
                rows.append(dict(row))
        return rows

    def _register_unknown(self, entry: Mapping[str, Any], *, verdict: str) -> None:
        """Gap 1 rule 5: remember an unknowable request so that it can NEVER be re-sent."""
        key = str(entry.get("request_key"))
        self._terminal_request_keys.add(key)
        self._unknown_send_state[key] = {
            "request_id": entry.get("request_id"), "request_key": key,
            "transport_attempt_id": entry.get("transport_attempt_id"),
            "started_at": entry.get("started_at"), "epoch": entry.get("epoch"),
            "verdict": verdict,
        }
        self._no_retry_request_keys.add(key)
        with self._lock:
            self.stats["unknown_send_state_count"] = len(self._unknown_send_state)
            self.stats["unknown_send_state_request_keys"] = sorted(self._unknown_send_state)[:32]

    def _record_recovery(self, entry: Mapping[str, Any], *, result: str, verdict: str,
                         error_category: str) -> None:
        """Write the shadow record for a recovered request.  NEVER a DecisionRecord."""
        item = {"request_key": entry.get("request_key"), "epoch": entry.get("epoch"),
                "turn_hash": entry.get("turn_hash"), "trigger_ref": "recovery:unknown",
                "candidate_set_ref": None, "candidate_count": 0, "candidate_refs": [],
                "platform": None, "observed_at": None,
                "transport_attempt_id": entry.get("transport_attempt_id")}
        self._record(item, 0, result=result, provider_calls=0, error_category=error_category,
                     resume_verdict=verdict, send_state=verdict,
                     transport_attempt_id=entry.get("transport_attempt_id"),
                     late_result_received=(result == RESULT_LATE_DISCARDED),
                     late_result_promoted=False)

    def _mark_unknown_budget(self) -> None:
        """Best-effort Gap 1 accounting: the budget ledger shows an unknowable outcome."""
        adapter = self._adapter
        budget = getattr(adapter, "_budget", None) if adapter is not None else None
        if budget is None or not hasattr(budget, "mark_send_state_unknown"):
            return
        for key, info in list(self._unknown_send_state.items()):
            if key in self._budget_marked:
                continue
            try:
                budget.mark_send_state_unknown(
                    request_key=key, transport_attempt_id=info.get("transport_attempt_id"),
                    detail=str(info.get("verdict")))
                self._budget_marked.add(key)
            except Exception as exc:  # noqa: BLE001
                logger.error("cognition budget unknown-state write failed closed: %s", exc)
                self.stats["budget_unknown_state_write_failed"] = True

    def resume_state(self) -> dict[str, Any]:
        """Gap 1 rule 5: replay the persistent send-state journal after a (re)start.

        A request whose LAST state is SEND_STARTED reached the transport and no response was ever
        observed: that is the only genuinely unknowable window, so it is marked
        UNKNOWN_SEND_STATE, recording its request_id, request_key, transport_attempt_id and
        started_at, and it is **never re-sent** -- only a genuinely new candidate epoch (a new
        request key) may be sent afterwards.  A request whose last state is RESPONSE_RECEIVED is
        settled idempotently when the response is verifiable, and is marked UNKNOWN when it is
        not.  A lone NOT_SENT means no transmission ever started, so it is safe to reprocess.
        No path here starts a provider transmission, and no path here produces a DecisionRecord.
        Calling it more than once is safe.
        """
        groups: dict[str, dict[str, Any]] = {}
        for row in self._read_send_state_journal():
            key = str(row.get("request_key") or "")
            if not key:
                continue
            entry = groups.setdefault(key, {
                "request_key": key, "request_id": row.get("request_id"), "epoch": None,
                "turn_hash": None, "transport_attempt_id": None, "started_at": None,
                "response_digest": None, "response_verifiable": None, "late": False,
                "last": None, "attempted": False})
            entry["last"] = row.get("state")
            entry["late"] = bool(row.get("late")) or entry.get("late", False)
            if row.get("state") != SEND_NOT_SENT:
                # Any state beyond NOT_SENT means a transmission may already have started.
                entry["attempted"] = True
            for field_name in ("request_id", "epoch", "turn_hash", "transport_attempt_id",
                               "started_at", "response_digest"):
                if row.get(field_name) is not None:
                    entry[field_name] = row.get(field_name)
            if "response_verifiable" in row:
                entry["response_verifiable"] = row.get("response_verifiable")

        outcome: dict[str, Any] = {
            "schema": "lpc0b.cognition_resume_state.v1",
            "at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "journal": str(self.send_state_path),
            "scanned_requests": len(groups),
            "safe_to_reprocess": [], "unknown_send_state": [], "resumed_settled": [],
            "late_discarded": [], "already_settled": [], "sends_started_during_recovery": 0,
        }
        for key in sorted(groups):
            entry = groups[key]
            last = entry.get("last")
            if last == SEND_SETTLED:
                self._terminal_request_keys.add(key)
                self._no_retry_request_keys.add(key)
                outcome["already_settled"].append(key)
                continue
            if last == SEND_NOT_SENT:
                self._terminal_request_keys.add(key)
                # Reserved, but no transmission ever started: safe to reprocess (no double spend).
                # Queue payloads are memory-only; this is safe only if a later natural turn
                # re-observes the same stable request, not an automatic startup replay.
                outcome["safe_to_reprocess"].append(key)
                continue
            if last == SEND_UNKNOWN:
                self._terminal_request_keys.add(key)
                self._register_unknown(entry, verdict=SEND_UNKNOWN)
                outcome["unknown_send_state"].append(key)
                continue
            if last == SEND_STARTED:
                self._terminal_request_keys.add(key)
                # The only genuinely unknowable window: it may have been billed and answered.
                self._write_send_state(request_key=key, state=SEND_UNKNOWN,
                                       **self._ss_kwargs(entry),
                                       detail="NO_RESPONSE_RECEIVED_AFTER_SEND_STARTED",
                                       recovered=True)
                self._register_unknown(entry, verdict=SEND_UNKNOWN)
                self._record_recovery(entry, result=RESULT_UNKNOWN_SEND_STATE,
                                      verdict=SEND_UNKNOWN, error_category="UNKNOWN_SEND_STATE")
                outcome["unknown_send_state"].append(key)
                continue
            self._terminal_request_keys.add(key)
            # RESPONSE_RECEIVED without SETTLED: settle idempotently when the response is
            # verifiable, otherwise the outcome is unknowable.
            if entry.get("late"):
                verdict, result_word = SEND_LATE_DISCARDED, RESULT_LATE_DISCARDED
            elif entry.get("response_verifiable"):
                verdict, result_word = SEND_RESUMED_SETTLED, RESULT_RESUMED_SETTLED
            # NOTE: `response_verifiable is False` means "a response arrived but it carries no
            # usable content" (the adapter derives it from bool(raw_text.strip())), which is an
            # UNKNOWABLE outcome -- it must NOT be closed as a known-and-final provider error.
            # Definitive provider failures travel on the separate `cognition_send_failed`
            # boundary.  So an unverifiable response falls through to the UNKNOWN branch below,
            # exactly as this function's docstring requires.
            else:
                verdict, result_word = SEND_UNKNOWN, RESULT_UNKNOWN_SEND_STATE
            if verdict == SEND_UNKNOWN:
                # UNKNOWN_SEND_STATE is TERMINAL and is deliberately NOT settled away, so a later
                # restart re-derives it from the journal instead of forgetting that this key was
                # ever sent.
                self._write_send_state(request_key=key, state=SEND_UNKNOWN,
                                       **self._ss_kwargs(entry),
                                       detail="RESPONSE_RECEIVED_BUT_NOT_VERIFIABLE",
                                       recovered=True)
                self._register_unknown(entry, verdict=SEND_UNKNOWN)
                self._record_recovery(entry, result=result_word, verdict=verdict,
                                      error_category="RESUMED_AFTER_RESTART")
                outcome["unknown_send_state"].append(key)
                continue
            if result_word == RESULT_LATE_DISCARDED:
                with self._lock:
                    self.stats["late_results_received"] = int(
                        self.stats.get("late_results_received", 0)) + 1
            self._record_recovery(entry, result=result_word, verdict=verdict,
                                  error_category="RESUMED_AFTER_RESTART")
            # Idempotent settle: closes the attempt with NO second provider call, and always
            # AFTER the shadow record exists (Gap 1 rule 4 ordering).
            self._write_send_state(request_key=key, state=SEND_SETTLED, **self._ss_kwargs(entry),
                                   detail=f"IDEMPOTENT_SETTLE:{verdict}", recovered=True)
            if verdict == SEND_RESUMED_SETTLED:
                with self._lock:
                    self.stats["resumed_settles"] = int(self.stats.get("resumed_settles", 0)) + 1
                outcome["resumed_settled"].append(key)
            else:
                outcome["late_discarded"].append(key)

        # ABSOLUTE (Gap 1 rule 6 / Gap 2): once a request key's journal shows that a transmission
        # ever started -- it reached SEND_STARTED, or a response was bought -- that key is never
        # re-sent by this or any later process, whatever the classification above decided.  Only a
        # lone NOT_SENT is safe to reprocess, because nothing was ever sent for it.
        for key, entry in groups.items():
            if entry.get("attempted") or entry.get("last") == SEND_SETTLED:
                self._terminal_request_keys.add(key)
                self._no_retry_request_keys.add(key)
        outcome["no_retry_request_keys"] = sorted(self._no_retry_request_keys)
        outcome["retry_allowed_keys"] = sorted(key for key, entry in groups.items()
                                               if not entry.get("attempted"))
        self._resumed = True
        with self._lock:
            self.stats["unknown_send_state_count"] = len(self._unknown_send_state)
            self.stats["unknown_send_state_request_keys"] = sorted(self._unknown_send_state)[:32]
            self.stats["resume_state"] = {name: (len(value) if isinstance(value, list) else value)
                                          for name, value in outcome.items()}
        self._mark_unknown_budget()
        return outcome

    # -- shadow journal retention (LPC0B-R2 Gap 3) -------------------------------------

    @staticmethod
    def _count_jsonl_lines(path: Path) -> int:
        try:
            with path.open("r", encoding="utf-8") as handle:
                return sum(1 for line in handle if line.strip())
        except FileNotFoundError:
            return 0
        except OSError as exc:
            raise RuntimeError("COGNITION_SHADOW_JOURNAL_COUNT_FAILED") from exc

    def _prune_rotated_journals(self, keep: int = SHADOW_JOURNAL_ROTATED_KEEP) -> bool:
        """Prune rotated DEBUG journals to a requested count, failing closed on errors."""
        try:
            with os.scandir(self.journal_dir) as entries:
                rotated = sorted(
                    [(Path(entry.path), entry.stat(follow_symlinks=False).st_mtime)
                     for entry in entries
                     if entry.name.startswith(SHADOW_JOURNAL_ROTATED_PREFIX)
                     and entry.name.endswith(".jsonl")
                     and entry.is_file(follow_symlinks=False)],
                    key=lambda pair: (pair[1], pair[0].name),
                )
        except OSError as exc:
            logger.error("cognition shadow journal enumeration failed closed: %s", exc)
            with self._lock:
                self.stats["shadow_retention_violation"] = True
            return False
        try:
            old_entries = rotated[:-max(0, int(keep))] if keep else rotated
            for old, _mtime in old_entries:
                os.unlink(old)
                with self._lock:
                    self.stats["shadow_journal_rotated_pruned"] = int(
                        self.stats.get("shadow_journal_rotated_pruned", 0)) + 1
            with os.scandir(self.journal_dir) as entries:
                remaining = [entry.name for entry in entries
                             if entry.name.startswith(SHADOW_JOURNAL_ROTATED_PREFIX)
                             and entry.name.endswith(".jsonl")
                             and entry.is_file(follow_symlinks=False)]
            if len(remaining) > max(0, int(keep)):
                raise OSError("rotated journal count remains above configured cap")
            return True
        except OSError as exc:
            logger.error("cognition shadow journal prune failed closed: %s", exc)
            with self._lock:
                self.stats["shadow_retention_violation"] = True
            return False

    def _rotate_shadow_journal_if_needed(self, incoming_bytes: int) -> bool:
        """Gap 3 rotation -- DEBUG shadow journal only (never the canonical AG-1 journal).

        Sequence, which never loses the record being appended:
          1. measure the current file (records + bytes) and decide whether a limit is met;
          2. atomically rename it to cognition_shadow.<utc-timestamp>.jsonl (os.replace);
          3. if the rename fails, keep appending to the current file instead of failing;
          4. prune rotated files down to the newest SHADOW_JOURNAL_ROTATED_KEEP;
          5. the caller then opens the (fresh or unchanged) current file and appends the record.
        """
        limits = self._retention
        if not self._prune_rotated_journals():
            return False
        try:
            size = self.journal_path.stat().st_size if self.journal_path.exists() else 0
        except OSError:
            size = 0
        count = int(self._shadow_record_count or 0)
        if count <= 0 and size <= 0:
            # Nothing current to rotate; pruning is complete, no rotation was needed.
            return False
        if (count < int(limits["max_records"])
                and (size + int(incoming_bytes)) <= int(limits["max_bytes"])):
            return False
        if int(incoming_bytes) > int(limits["max_bytes"]):
            return False  # caller emits the fixed omission marker for a single oversized record
        stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime())
        rotated = self.journal_path.with_name(f"{SHADOW_JOURNAL_ROTATED_PREFIX}{stamp}.jsonl")
        suffix = 1
        while rotated.exists():
            rotated = self.journal_path.with_name(
                f"{SHADOW_JOURNAL_ROTATED_PREFIX}{stamp}.{suffix}.jsonl")
            suffix += 1
        try:
            if self.journal_path.exists():
                os.replace(self.journal_path, rotated)   # atomic within one directory
        except OSError as exc:
            logger.error("cognition shadow journal rotation failed closed: %s", exc)
            with self._lock:
                self.stats["shadow_retention_violation"] = True
            return False
        self._shadow_record_count = 0
        if not self._prune_rotated_journals():
            return False
        with self._lock:
            self.stats["shadow_journal_rotations"] = int(
                self.stats.get("shadow_journal_rotations", 0)) + 1
            self.stats["shadow_journal_last_rotation"] = rotated.name
            self.stats["shadow_journal_records_current"] = 0
        return True

    def _append_shadow_record(self, payload: Mapping[str, Any]) -> bool:
        """Append one DEBUG shadow record, rotating first when a retention limit is met.

        This retention governs the debug shadow journal ONLY.  The canonical AG-1 Decision
        journal lives in a different root and is never rotated, pruned or deleted by any code
        path here.
        """
        line = json.dumps(dict(payload), ensure_ascii=False, separators=(",", ":")) + "\n"
        incoming = len(line.encode("utf-8"))
        with self._journal_lock:
            try:
                self.journal_dir.mkdir(parents=True, exist_ok=True)
                oversized = incoming > int(self._retention["max_bytes"])
                if not oversized and self._shadow_record_count is None:
                    self._shadow_record_count = self._count_jsonl_lines(self.journal_path)
                if oversized:
                    payload = {"schema": "lpc0b.cognition_shadow_record_omitted.v1",
                               "reason": "RECORD_EXCEEDS_RETENTION_BYTE_LIMIT"}
                    line = json.dumps(payload, separators=(",", ":")) + "\n"
                    incoming = len(line.encode("utf-8"))
                if oversized:
                    with self._lock:
                        self.stats["shadow_retention_omitted_oversized"] = int(
                            self.stats.get("shadow_retention_omitted_oversized", 0)) + 1
                    if int(self._shadow_record_count or 0) + 1 > int(self._retention["max_records"]):
                        return False
                if self._shadow_record_count is None:
                    self._shadow_record_count = self._count_jsonl_lines(self.journal_path)
                if oversized:
                    if not self._prune_rotated_journals():
                        return False
                    current_size = self.journal_path.stat().st_size if self.journal_path.exists() else 0
                    if (current_size + incoming > int(self._retention["max_bytes"])
                            or int(self._shadow_record_count or 0) + 1 > int(self._retention["max_records"])):
                        return False
                    with self.journal_path.open("a", encoding="utf-8") as handle:
                        handle.write(line)
                        handle.flush()
                        os.fsync(handle.fileno())
                    self._shadow_record_count = int(self._shadow_record_count or 0) + 1
                    with self._lock:
                        self.stats["shadow_journal_records_current"] = self._shadow_record_count
                    return True
                rotated = self._rotate_shadow_journal_if_needed(incoming)
                if not rotated and bool(self.stats.get("shadow_retention_violation")):
                    return False
                current_size_before = self.journal_path.stat().st_size if self.journal_path.exists() else 0
                current_count_before = int(self._shadow_record_count or 0)
                if ((current_size_before + incoming > int(self._retention["max_bytes"])
                     or current_count_before + 1 > int(self._retention["max_records"]))
                        and not rotated):
                    with self._lock:
                        self.stats["shadow_retention_violation"] = True
                    logger.error("cognition shadow append stopped: retention rotation failed")
                    return False
                current_size = self.journal_path.stat().st_size if self.journal_path.exists() else 0
                current_count = int(self._shadow_record_count or 0)
                if (current_size + incoming > int(self._retention["max_bytes"])
                        or current_count + 1 > int(self._retention["max_records"])):
                    marker = {"schema": "lpc0b.cognition_shadow_record_omitted.v1",
                              "reason": "RETENTION_LIMIT_BELOW_SINGLE_MARKER"}
                    line = json.dumps(marker, separators=(",", ":")) + "\n"
                    incoming = len(line.encode("utf-8"))
                    if (current_size + incoming > int(self._retention["max_bytes"])
                            or current_count + 1 > int(self._retention["max_records"])):
                        logger.error("cognition shadow record omitted: journal retention limit reached")
                        return False
                with self.journal_path.open("a", encoding="utf-8") as handle:
                    handle.write(line)
                    handle.flush()
                    os.fsync(handle.fileno())
                self._shadow_record_count = int(self._shadow_record_count) + 1
                with self._lock:
                    self.stats["shadow_journal_records_current"] = self._shadow_record_count
                return True
            except OSError as exc:
                logger.error("cognition shadow journal write failed closed: %s", exc)
                with self._lock:
                    self.stats["shadow_retention_violation"] = True
                return False

    # -- lifecycle -------------------------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            out = dict(self.stats)
            out["queue_depth"] = len(self._queue)
            out["worker_running"] = bool(self._thread is not None and self._thread.is_alive())
            out["shadow_service_error"] = self._shadow_error
            out["send_state_path"] = str(self.send_state_path)
            out["shadow_journal_path"] = str(self.journal_path)
            out["retention"] = dict(self._retention)
            out["unknown_send_state_request_keys"] = sorted(self._unknown_send_state)[:32]
            out["no_retry_request_keys"] = sorted(self._no_retry_request_keys)[:32]
            if self._adapter is not None and hasattr(self._adapter, "stats"):
                out["adapter"] = self._adapter.stats()
        return out

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=3.0)
