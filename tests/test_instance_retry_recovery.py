"""A real ``Instance`` must own startup recovery of the legacy retry spool.

The defect this pins down
-------------------------
``m37_m0_bridge`` builds its ``RetrySpool`` lazily, inside ``_append_or_queue``,
i.e. only when evidence is actually written. A state opened without a chat turn
therefore never constructed one -- and so never ran ``RetrySpool.migrate_legacy()``
nor ``EvidenceWriter.retry_pending()``. An upgraded state whose
``memory/evidence-retry`` directory still held legacy JSONL was left with its
pending evidence undrained until the first turn was written, which is exactly the
case a first-open upgrade has to cover.

The recovery owner is ``app.evidence.EvidenceWriter``: its constructor migrates
the legacy spool and then calls ``retry_pending()``. ``Instance._assemble`` now
builds one for every memory profile, after the bridge and resolver are in place
and before ``wire_runtime``, and releases it in ``close()``.

How the assertions stay honest
------------------------------
``migrate_legacy`` and ``retry_pending`` are wrapped with ``functools.wraps`` --
the real methods run unchanged and their real return values are recorded. Nothing
is stubbed out, so the test cannot pass on a fake recovery. The provider boundary
is patched to raise, so a model call during assembly would fail the test instead
of happening quietly.

The legacy JSONL shapes used here are byte-level the shapes the retired version
wrote (pending: ``{"event": ..., "queued_at": ..., "error": ...}``; committed:
``{"source_origin", "source_ref_id", "event_id", "status", "committed_at"}``), and
the refs are the ones frozen in the real P2-A acceptance state, so this file and
the real upgrade describe the same thing.

Skipped where the Hermes host is not importable (native Windows), for the same
reason as ``tests/test_instance_teardown.py``: a real ``Instance`` cannot be
assembled there, and pretending otherwise would be a fake PASS.
"""
import functools
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "vendor/hermes"),
                str(ROOT / "components/native"), str(ROOT / "components/native/src")]


def _hermes_host_available() -> bool:
    try:
        import run_agent  # noqa: F401
        return True
    except Exception:
        return False


HERMES_HOST = _hermes_host_available()

#: exactly the keys frozen in /home/nyatest/p2a-acceptance/state-original
LEGACY_CONVERSATION_ID = "tg-4d5ac152e698abed94e43f58"
LEGACY_CHIYO_REF = "telegram:p2-final-acceptance:692077822909347003"
LEGACY_USER_REF = "telegram:p2-final-acceptance:692077822909347003"
LEGACY_TURN_ID = "93d7bc5e-83a7-4b39-b8ab-58ebdac12efa"

CHIYO_ORIGIN = "CHIYO_VISIBLE_OUTPUT"
USER_ORIGIN = "USER_VISIBLE_INPUT"


def legacy_pending_record(*, event_id, source_origin, source_ref, kind, content):
    """One line of the retired ``pending.jsonl``."""
    chiyo = source_origin == CHIYO_ORIGIN
    return {
        "event": {
            "event_id": event_id,
            "occurred_at": "2026-10-07T07:43:08.271680+00:00",
            "created_at": "2026-10-07T07:43:08.271680+00:00",
            "memory_owner": "chiyo",
            "source_origin": source_origin,
            "delivery_status": "DELIVERED" if chiyo else "RECEIVED",
            "epistemic_role": "SELF_EXPRESSION" if chiyo else "OBSERVED_EXTERNAL_EXPRESSION",
            "speaker": "chiyo" if chiyo else "user",
            "content": content,
            "conversation_id": LEGACY_CONVERSATION_ID,
            "turn_id": LEGACY_TURN_ID,
            "source_refs": [{"id": source_ref, "kind": kind},
                            {"id": LEGACY_TURN_ID, "kind": "native_turn"}],
        },
        "queued_at": "2026-10-07T08:36:38.779329+00:00",
        "error": "acceptance sample",
    }


def legacy_committed_record(*, event_id, source_origin, source_ref, status="inserted"):
    """One line of the retired ``committed.jsonl``."""
    return {
        "source_origin": source_origin,
        "source_ref_id": source_ref,
        "event_id": event_id,
        "status": status,
        "committed_at": "2026-10-07T08:36:38.784064+00:00",
    }


def spool_rows(db_path: Path):
    """Raw spool rows, read-only, keyed by (origin, ref)."""
    connection = sqlite3.connect("file:%s?mode=ro" % db_path, uri=True)
    connection.row_factory = sqlite3.Row
    try:
        return {(row["source_origin"], row["source_ref_id"]): dict(row)
                for row in connection.execute("SELECT * FROM spool")}
    finally:
        connection.close()


def m0_rows(db_path: Path):
    """Raw M0 rows, read-only, keyed by (origin, ref)."""
    connection = sqlite3.connect("file:%s?mode=ro" % db_path, uri=True)
    connection.row_factory = sqlite3.Row
    try:
        return {(row["source_origin"], row["primary_source_ref_id"]): dict(row)
                for row in connection.execute("SELECT * FROM evidence_events")}
    finally:
        connection.close()


@unittest.skipUnless(HERMES_HOST, "Hermes host unavailable (native Windows: POSIX only)")
class InstanceStartupRecoveryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="retry-recovery-"))
        self.state = self.tmp / "state"

    # ------------------------------------------------------------ fixtures --- #
    def _environment(self) -> dict:
        env = dict(os.environ)
        env.update(CHIYO_MODEL_API_KEY="unused-retry-recovery-key",
                   CHIYO_MODEL="unused-retry-recovery-model",
                   CHIYO_MODEL_BASE_URL="https://api.example.invalid/v1")
        return env

    def _spool_dir(self) -> Path:
        return self.state / "memory" / "evidence-retry"

    def _write_legacy(self, pending=(), committed=()) -> dict:
        spool = self._spool_dir()
        spool.mkdir(parents=True, exist_ok=True)
        written = {}
        if pending:
            path = spool / "pending.jsonl"
            path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in pending),
                            encoding="utf8")
            written["pending"] = path.read_bytes()
        if committed:
            path = spool / "committed.jsonl"
            path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in committed),
                            encoding="utf8")
            written["committed"] = path.read_bytes()
        return written

    def _open(self, **kwargs):
        from chiyo_bundle.instance import Instance
        return Instance(self.state, owner="retry-recovery", memory=True,
                        environment=self._environment(), **kwargs)

    @staticmethod
    def _no_model_call():
        """Refuse every provider call; returns the patch and the attempted calls."""
        calls: list = []

        def boom(*args, **kwargs):
            calls.append(1)
            raise AssertionError("opening a state must not call the model provider")

        return patch("chiyo_bundle.host.HermesCompletionProvider.complete",
                     side_effect=boom), calls

    def _watch_recovery(self):
        """Record the real migrate/retry results; replace neither of them."""
        import app.evidence as evidence
        captured: dict = {}

        real_migrate = evidence.RetrySpool.migrate_legacy
        real_retry = evidence.EvidenceWriter.retry_pending

        @functools.wraps(real_migrate)
        def migrate(spool, *args, **kwargs):
            result = real_migrate(spool, *args, **kwargs)
            captured["migrate"] = result
            return result

        @functools.wraps(real_retry)
        def retry(writer, *args, **kwargs):
            result = real_retry(writer, *args, **kwargs)
            captured.setdefault("retries", []).append(result)
            return result

        patcher = patch.object(evidence.RetrySpool, "migrate_legacy", migrate)
        patcher_retry = patch.object(evidence.EvidenceWriter, "retry_pending", retry)
        patcher.start()
        patcher_retry.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(patcher_retry.stop)
        return captured

    # ------------------------------------------------------------------ A ---- #
    def test_startup_builds_the_recovery_owner_without_any_turn(self):
        """Wiring: the recovery owner exists after assembly, unused, with no chat."""
        refusal, calls = self._no_model_call()
        with refusal:
            inst = self._open()
            try:
                writer = inst.evidence_writer
                self.assertIsNotNone(
                    writer,
                    "Instance startup did not build the evidence-recovery owner, so a "
                    "legacy retry spool in an upgraded state would never be migrated "
                    "or drained by an open that does not chat")
                # The writer's data root is the instance state itself.
                self.assertEqual(writer.memory_root, inst.state / "memory")
                self.assertEqual(writer.memory_root.parent, inst.state)
                # ... and its spool is the directory convention the bridge queues
                # into: one spool, never "queued to A, drained from B".
                self.assertEqual(writer.spool.root, inst.state / "memory" / "evidence-retry")
                self.assertEqual(writer.spool.db_path,
                                 inst.state / "memory" / "evidence-retry" / "spool.sqlite")
                import m37_m0_bridge as bridge
                bridge._SPOOLS.clear()
                self.assertEqual(bridge.retry_spool(inst.paths["m0_db"]).db_path,
                                 writer.spool.db_path)
                self.assertEqual(Path(inst.paths["m0_db"]),
                                 inst.state / "memory" / "evidence.sqlite")
                self.assertEqual(inst.health()["memory_resolver_state"], "READY")
            finally:
                inst.close()
        self.assertEqual(calls, [], "the provider was called during startup")
        self.assertIsNone(inst.evidence_writer, "close() did not release the writer")

    def test_memory_disabled_profiles_build_no_recovery_owner(self):
        """A profile that never writes M0 evidence gets no recovery owner."""
        from chiyo_bundle.instance import Instance
        refusal, calls = self._no_model_call()
        with refusal:
            inst = Instance(self.tmp / "nostate", owner="retry-recovery", memory=False,
                            environment=self._environment())
            try:
                self.assertIsNone(inst.evidence_writer)
            finally:
                inst.close()
        self.assertEqual(calls, [])

    # ------------------------------------------------------------------ B ---- #
    def test_legacy_pending_only_spool_is_migrated_and_retried_at_startup(self):
        """The regression: a pending-only legacy spool must be recovered by the open."""
        record = legacy_pending_record(event_id="01a11550-f2f2-7ba6-95ea-9bb455b7d6db",
                                       source_origin=CHIYO_ORIGIN, source_ref=LEGACY_CHIYO_REF,
                                       kind="native_telegram_output", content="reply 001")
        written = self._write_legacy(pending=[record])
        self.assertFalse((self._spool_dir() / "spool.sqlite").exists(),
                         "fixture precondition: no new-format spool yet")

        captured = self._watch_recovery()
        refusal, calls = self._no_model_call()
        with refusal:
            inst = self._open()
            try:
                state = inst.state
                # migration really ran, and really verified the pending set
                self.assertIn("migrate", captured,
                              "startup never built a RetrySpool, so migrate_legacy never "
                              "ran and the legacy spool stayed on disk, undrained")
                self.assertEqual(captured["migrate"]["performed"], True)
                self.assertEqual(captured["migrate"]["pending_records"], 1)
                self.assertEqual(captured["migrate"]["committed_records"], 0)
                self.assertEqual(captured["migrate"]["verified"], True)
                # ... and the same open drained it, without any chat turn
                self.assertEqual(captured["retries"],
                                 [{"inserted": 1, "duplicate": 0, "failed": 0}])
                rows = m0_rows(state / "memory" / "evidence.sqlite")
                self.assertEqual(list(rows), [(CHIYO_ORIGIN, LEGACY_CHIYO_REF)])
                self.assertEqual(rows[(CHIYO_ORIGIN, LEGACY_CHIYO_REF)]["content"], "reply 001")
                self.assertEqual(rows[(CHIYO_ORIGIN, LEGACY_CHIYO_REF)]["speaker"], "chiyo")
                self.assertEqual(rows[(CHIYO_ORIGIN, LEGACY_CHIYO_REF)]["turn_id"], LEGACY_TURN_ID)

                self.assertEqual(inst.evidence_writer.spool.pending_count(), 0)
                # the legacy bytes are kept, renamed -- never unlinked
                self.assertFalse((self._spool_dir() / "pending.jsonl").exists())
                self.assertEqual((self._spool_dir() / "pending.jsonl.migrated").read_bytes(),
                                 written["pending"])
                # the recovered record is terminal, and was never re-queued
                row = spool_rows(self._spool_dir() / "spool.sqlite")[(CHIYO_ORIGIN, LEGACY_CHIYO_REF)]
                self.assertEqual(row["status"], "COMMITTED")
                self.assertEqual(row["commit_status"], "inserted")
                self.assertEqual(row["attempts"], 0)
            finally:
                inst.close()
        self.assertEqual(calls, [], "the provider was called during startup recovery")


    # ------------------------------------------------------------------ C ---- #
    def test_committed_legacy_key_is_terminal_and_the_next_open_is_idempotent(self):
        """The frozen P2-A fixture shape: the committed key is already in both files.

        The retired version wrote a committed key by ``queue(event)`` followed by
        ``mark_committed(event)``, so the key legitimately appears in
        ``pending.jsonl`` *and* ``committed.jsonl``. It is terminal: the upgrade
        must not re-offer it as active pending, and a later open must not touch it
        again.
        """
        chiyo = legacy_pending_record(event_id="01a11550-f2f2-7ba6-95ea-9bb455b7d6db",
                                      source_origin=CHIYO_ORIGIN, source_ref=LEGACY_CHIYO_REF,
                                      kind="native_telegram_output", content="reply 001")
        user = legacy_pending_record(event_id="01a11550-efcc-7469-a28a-637fb5ef04f3",
                                     source_origin=USER_ORIGIN, source_ref=LEGACY_USER_REF,
                                     kind="native_telegram_input", content="acceptance user turn 001")
        committed = legacy_committed_record(event_id="01a11550-efcc-7469-a28a-637fb5ef04f3",
                                            source_origin=USER_ORIGIN, source_ref=LEGACY_USER_REF)
        written = self._write_legacy(pending=[chiyo, user], committed=[committed])

        captured = self._watch_recovery()
        refusal, calls = self._no_model_call()
        with refusal:
            first = self._open()
            try:
                state = first.state
                self.assertIn("migrate", captured,
                              "startup never built a RetrySpool, so migrate_legacy never "
                              "ran and the legacy spool stayed on disk, undrained")
                self.assertEqual(captured["migrate"]["performed"], True)
                self.assertEqual(captured["migrate"]["pending_records"], 2)
                self.assertEqual(captured["migrate"]["committed_records"], 1)
                self.assertEqual(captured["migrate"]["verified"], True)
                # only the still-pending key was drained
                self.assertEqual(captured["retries"],
                                 [{"inserted": 1, "duplicate": 0, "failed": 0}])
                rows = m0_rows(state / "memory" / "evidence.sqlite")
                self.assertEqual(list(rows), [(CHIYO_ORIGIN, LEGACY_CHIYO_REF)])

                spool = spool_rows(self._spool_dir() / "spool.sqlite")
                self.assertEqual(spool[(CHIYO_ORIGIN, LEGACY_CHIYO_REF)]["status"], "COMMITTED")
                # the committed key is recognized as terminal: never queued, never
                # retried, never re-appended to M0 (it is durable there already)
                terminal = spool[(USER_ORIGIN, LEGACY_USER_REF)]
                self.assertEqual(terminal["status"], "COMMITTED")
                self.assertEqual(terminal["commit_status"], "inserted")
                self.assertEqual(terminal["attempts"], 0)
                self.assertNotIn((USER_ORIGIN, LEGACY_USER_REF), rows)
                self.assertEqual(first.evidence_writer.spool.pending_count(), 0)
                self.assertEqual(first.evidence_writer.retry_pending(),
                                 {"inserted": 0, "duplicate": 0, "failed": 0})
                for name in ("pending.jsonl", "committed.jsonl"):
                    self.assertFalse((self._spool_dir() / name).exists())
                    self.assertEqual((self._spool_dir() / (name + ".migrated")).read_bytes(),
                                     written[name[:-6]])
                spool_after_first = dict(spool)
            finally:
                first.close()

            # ---- an independent open of the same upgraded state ------------- #
            second = self._open()
            try:
                self.assertEqual(captured["migrate"]["performed"], False)
                self.assertEqual(captured["migrate"]["pending_records"], 0)
                self.assertEqual(captured["retries"][1],
                                 {"inserted": 0, "duplicate": 0, "failed": 0})
                self.assertEqual(m0_rows(second.state / "memory" / "evidence.sqlite"), rows)
                self.assertEqual(spool_rows(self._spool_dir() / "spool.sqlite"),
                                 spool_after_first)
                self.assertEqual(second.health()["memory_resolver_state"], "READY")
            finally:
                second.close()
        self.assertEqual(calls, [], "the provider was called while opening a state")

    # ------------------------------------------------------------------ F ---- #
    def test_deep_state_path_confirms_a_turn_without_queueing_evidence(self):
        """NYA-AUDIT-013: a state too deep for sun_path must not degrade into the spool.

        The pre-fix socket location was ``<state>/memory/m0-writer-runtime/
        m0-writer-<tag>-<pid>.sock``; past sun_path the worker child died in
        ``bind()``, the bridge queued the event for retry and ``confirm_visible``
        raised ``M0WriterUnavailable``. Nothing here sets
        ``CHIYO_M0_WRITER_SOCKET_DIR``: the default layout has to be safe.
        """
        import m0_writer_worker as mww
        import m37_m0_bridge as bridge
        self.addCleanup(bridge._SPOOLS.clear)
        self.addCleanup(mww.close_all)

        deep = self.tmp
        for index in range(3):
            deep = deep / ("deep-state-segment-%02d-" % index + "x" * 20)
        self.state = deep / "state"
        self.assertFalse(os.environ.get("CHIYO_M0_WRITER_SOCKET_DIR"),
                         "this test is about the default layout")

        db = self.state / "memory" / "evidence.sqlite"
        native = mww.socket_path_candidates(db, pid=os.getpid())[0]
        self.assertFalse(mww._fits_sun_path(native),
                         "fixture is not deep enough to exercise the defect: %s" % native)
        chosen = mww.socket_path_for(db)
        self.assertTrue(mww._fits_sun_path(chosen))

        replies: list = []

        def fake_complete(messages):
            replies.append(messages[-1]["content"])
            return SimpleNamespace(content="deep path reply", usage={}, finish_reason="stop")

        with patch("chiyo_bundle.host.HermesCompletionProvider.complete", side_effect=fake_complete):
            inst = self._open()
            try:
                response = inst.chat("turn on a deep state", request_id="deep-turn-1")
                self.assertEqual(response["reply"], "deep path reply")
                # must not raise M0WriterUnavailable
                inst.confirm_visible("deep-turn-1")
                self.assertEqual(inst.requests.execute(
                    "SELECT status FROM requests WHERE id='deep-turn-1'").fetchone()[0], "VISIBLE")
                self.assertEqual(inst.health()["m0_writer"]["state"], "READY")
                rows = m0_rows(inst.state / "memory" / "evidence.sqlite")
                self.assertEqual(sorted(origin for origin, _ in rows),
                                 ["CHIYO_VISIBLE_OUTPUT", "USER_VISIBLE_INPUT"])
                # nothing was ever queued: the spool holds no record at all
                self.assertEqual(spool_rows(self._spool_dir() / "spool.sqlite"), {})
                self.assertEqual(inst.evidence_writer.spool.pending_count(), 0)
                # and the socket really lives in the short fallback directory
                self.assertFalse((inst.state / "memory" / "m0-writer-runtime").exists())
                self.assertEqual(mww.socket_path_for(inst.state / "memory" / "evidence.sqlite"),
                                 chosen)
            finally:
                inst.close()
        self.assertEqual(len(replies), 1)


if __name__ == "__main__":
    unittest.main()
