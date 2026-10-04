"""§29 -- ReadOnlyProductionRecallAdapter.

Extracts the REAL M3 read path -- candidate generation, ranking, source trace and
Surface payload generation -- out of `app/m3.py` by giving `M3Worker` a store object
that has NO write capability and only CAPTURES what `record()` would have written.

It never calls `M3Worker.process_once()`, never opens M0/M1/M2/M3 for writing
(every connection is a `file:...?mode=ro` URI with `PRAGMA query_only=ON`), and never
touches `M3Store` at all.
"""
from __future__ import annotations

import json
import pathlib
import sqlite3
import sys
from typing import Any
from urllib.parse import quote

NATIVE_ROOT = pathlib.Path(__file__).resolve().parents[1] / "src"
if str(NATIVE_ROOT) not in sys.path:
    sys.path.insert(0, str(NATIVE_ROOT))

from app.m3 import M3Worker, SourceReader, connect_ro  # noqa: E402

ADAPTER_VERSION = "readonly-production-recall-v0.1"
M3_SHA256 = "0d5f095b35cfa05b574ab3728a79dd09d7af26dd252400abf8de8bcdfc668053"


def _ro(path: str | pathlib.Path) -> sqlite3.Connection:
    enc = quote(str(path), safe="/:")
    conn = sqlite3.connect("file:" + enc + "?mode=ro", uri=True, timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


class ReadOnlyRecallStore:
    """Interface-compatible with M3Store for the READ and COMPUTE path only.

    `record()` cannot write: it appends to an in-memory list and returns the
    evaluation id, which is the only value `M3Worker._evaluate` consumes.
    """

    write_capable = False

    def __init__(self, m3_db: str, replay: bool = True):
        self.path = m3_db
        self.conn = _ro(m3_db)
        self.captured: list[dict[str, Any]] = []
        self.replay = replay
        self.write_attempts = 0

    # -- read side ---------------------------------------------------------- #
    def is_processed(self, trigger_id: str) -> bool:
        if self.replay:
            return False          # replaying an already-processed trigger, not processing it
        row = self.conn.execute(
            "SELECT 1 FROM processed_triggers WHERE trigger_evidence_id=?",
            (trigger_id,)).fetchone()
        return row is not None

    def processed_triggers(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self.conn.execute(
            "SELECT trigger_evidence_id, trigger_sequence, evaluation_id, processed_at "
            "FROM processed_triggers ORDER BY rowid")]

    def stored_rows(self, evaluation_id: str) -> dict[str, Any]:
        ev = self.conn.execute(
            "SELECT * FROM evaluations WHERE evaluation_id=?", (evaluation_id,)).fetchone()
        cands = [dict(r) for r in self.conn.execute(
            "SELECT rowid, candidate_id, candidate_type, episode_id, source_event_ids_json, "
            "source_max_sequence, generator_signals_json, provenance_json, candidate_digest "
            "FROM candidates WHERE evaluation_id=? ORDER BY rowid", (evaluation_id,))]
        elig = [dict(r) for r in self.conn.execute(
            "SELECT rowid, candidate_ref, candidate_type, eligible, reason_code "
            "FROM eligibility_decisions WHERE evaluation_id=? ORDER BY rowid", (evaluation_id,))]
        avail = self.conn.execute(
            "SELECT outcome, selected_refs_json, reason_code, surface_payload_json "
            "FROM availability_decisions WHERE evaluation_id=?", (evaluation_id,)).fetchone()
        exp = [dict(r) for r in self.conn.execute(
            "SELECT rowid, understanding_id, episode_id, status, eligible, surfaceable, "
            "withheld_reason, provenance_json FROM secondary_expansions "
            "WHERE evaluation_id=? ORDER BY rowid", (evaluation_id,))]
        return {"evaluation": dict(ev) if ev else None, "candidates": cands,
                "eligibility": elig, "availability": dict(avail) if avail else None,
                "expansions": exp}

    # -- the write interface M3Worker expects, made inert ------------------- #
    def record(self, evaluation, candidates, eligibility, availability, expansions, status):
        self.captured.append({
            "evaluation": dict(evaluation), "candidates": list(candidates),
            "eligibility": list(eligibility), "availability": dict(availability),
            "expansions": list(expansions), "status": status})
        return evaluation["evaluation_id"]

    def _write_forbidden(self, *a, **k):
        self.write_attempts += 1
        raise RuntimeError("ReadOnlyRecallStore is not write capable")

    ensure_cursor = _write_forbidden
    verify = _write_forbidden
    _initialize = _write_forbidden


class ReadOnlyProductionRecallAdapter:
    """The extracted production read path.  One instance, no side effects."""

    def __init__(self, config: dict[str, Any]):
        self.config = config
        self.reader = SourceReader(config["m0_db"], config["m1_db"], config["m2_db"],
                                   config.get("epoch_path"))
        self.store = ReadOnlyRecallStore(config["m3_db"], replay=True)
        self.worker = M3Worker(
            self.reader, self.store,
            generator_version=config.get("generator_version", "m3-shadow-v0.1"),
            availability_version=config.get("availability_version", "m3-e2-v0.1"),
            max_candidates=int(config.get("max_candidates", 200)))

    # -- the extracted stages, each callable on its own --------------------- #
    def events(self):
        return self.reader.events()

    def candidate_generation(self, trigger, all_events):
        """Stage 1: episode candidates (generation + ranking inputs)."""
        episodes, _visible = self.reader.episode_candidates(trigger, all_events)
        return episodes[: self.worker.max_candidates]

    def build_trigger(self, event, all_events):
        return self.reader.build_trigger(event, all_events)

    def source_trace(self, candidate):
        return {"episode_id": candidate["episode_id"],
                "evidence_refs": candidate["source_event_ids"]}

    def surface_payload(self, trigger, all_events, trigger_id):
        """Stage 4: the Surface V1 payload for one trigger."""
        return self.evaluate_trigger(trigger_id)["availability"]["surface_payload"]

    def evaluate_trigger(self, trigger_id: str) -> dict[str, Any]:
        """Run generation -> ranking -> eligibility -> availability for ONE trigger."""
        all_events = self.reader.events()
        event = None
        for item in all_events:
            if str(item["event_id"]) == str(trigger_id):
                event = item
                break
        if event is None:
            raise RuntimeError("trigger event not found: " + str(trigger_id))
        trigger = self.reader.build_trigger(event, all_events)
        self.store.captured.clear()
        self.worker._evaluate(trigger, all_events)
        if not self.store.captured:
            raise RuntimeError("adapter produced no result for " + str(trigger_id))
        out = self.store.captured[-1]
        out["trigger"] = {"trigger_evidence_id": trigger_id,
                          "trigger_sequence": trigger.sequence,
                          "cognitive_intent": trigger.intent,
                          "intent_reason": trigger.intent_reason,
                          "epoch_id": trigger.epoch_id,
                          "boundary_id": trigger.boundary_id,
                          "visible_history_digest": trigger.visible_history_digest}
        return out

    def recall(self, triggers: list[str] | None = None) -> list[dict[str, Any]]:
        ids = triggers or [t["trigger_evidence_id"] for t in self.store.processed_triggers()]
        return [self.evaluate_trigger(t) for t in ids]

    def close(self):
        self.store.conn.close()

    @property
    def write_attempts(self) -> int:
        return self.store.write_attempts

    def audit(self) -> dict[str, Any]:
        return {"adapter_version": ADAPTER_VERSION, "m3_sha256": M3_SHA256,
                "write_capable": self.store.write_capable,
                "write_attempts": self.store.write_attempts,
                "uses_M3Store": False, "calls_process_once": False,
                "connections": "file:...?mode=ro + PRAGMA query_only=ON"}
