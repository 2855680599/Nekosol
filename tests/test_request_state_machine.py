"""Request state machine: one execution per request_id, and clean teardown.

These drive the real ``Instance`` methods through ``Instance.__new__`` so they
need no model, no credentials and no Hermes host, and therefore run on native
Windows as well as POSIX. What is exercised is the production code path:
``_claim_request`` / ``_settle_request`` / ``chat`` / ``new_session`` / ``close``.

Design notes asserted here
--------------------------
* ``chat`` serialises turns on ``Instance._request_lock``, so a second caller
  for the same ``request_id`` waits and then replays the stored reply: the model
  runs exactly once and no ``UNIQUE``/``database is locked`` error appears.
* The claim (lookup + insert PROCESSING) runs inside one short
  ``BEGIN IMMEDIATE`` transaction, and no sqlite transaction is held across the
  model call.
* A model failure leaves the row in PROCESSING on purpose: that is the existing
  documented fail-closed "uncertain outcome" state, and a retry is refused with
  an explicit reason rather than silently repeated. The test pins that
  behaviour; it does not redesign the protocol.
"""
import shutil
import sqlite3
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
# app.session is needed to stub the conversation store in the teardown tests.
NATIVE_SRC = ROOT / "components" / "native" / "src"
if str(NATIVE_SRC) not in sys.path:
    sys.path.insert(0, str(NATIVE_SRC))

from chiyo_bundle.instance import Instance  # noqa: E402

CREATE_REQUESTS = (
    "CREATE TABLE IF NOT EXISTS requests("
    "id TEXT PRIMARY KEY, digest TEXT NOT NULL, status TEXT NOT NULL, "
    "response TEXT, source TEXT)")


class FakeRuntime:
    """Records turns instead of calling a model."""

    def __init__(self, fail=False, delay=0.0):
        self.turns = []
        self.committed = []
        self.fail = fail
        self.delay = delay

    def handle_turn(self, conversation_id, text, message_id=None):
        if self.delay:
            # widen the window a concurrent caller could exploit
            threading.Event().wait(self.delay)
        if self.fail:
            raise RuntimeError("model unavailable")
        self.turns.append((conversation_id, text, message_id))
        return {"raw_content": "reply to " + text,
                "turn_id": "turn-%d" % len(self.turns)}

    def commit_turn(self, result, text):
        self.committed.append((result["turn_id"], text))


def build_instance(tmp, *, memory=False, runtime=None):
    """An Instance wired only with stdlib parts, bypassing _assemble."""
    inst = Instance.__new__(Instance)
    inst.owner = "fixture"
    inst.memory = memory
    inst.life = False
    inst.counter = 0
    inst.supply_subject = None
    inst.life_wrapper = None
    inst._closed = False
    inst._request_lock = threading.RLock()
    inst.state = Path(tmp)
    inst.binding = {"conversation_id": "tg-fixture"}
    inst.requests = sqlite3.connect(str(Path(tmp) / "requests.sqlite"),
                                   check_same_thread=False, isolation_level=None)
    inst.requests.execute(CREATE_REQUESTS)
    inst.native = SimpleNamespace(provider=SimpleNamespace(last_report={}),
                                  m37_resolver=None, m37_bridge=None,
                                  memory_controls=None, store=None)
    inst.runtime = runtime if runtime is not None else FakeRuntime()
    return inst


class _InstanceTestCase(unittest.TestCase):
    """Builds a stdlib-only Instance and always disposes its request database.

    Disposal order matters on Windows: sqlite keeps the file handle open, so the
    temporary directory cannot be removed until the connection is closed
    (WinError 32). Cleanups are LIFO, so the directory removal is registered in
    setUp and the connection close afterwards, which closes the connection first.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="request-state-")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def _instance(self, **kwargs):
        inst = build_instance(self.tmp, **kwargs)
        self.addCleanup(self._dispose, inst)
        return inst

    @staticmethod
    def _dispose(inst):
        try:
            if getattr(inst, "requests", None) is not None:
                inst.requests.close()
        except Exception:  # noqa: BLE001 - best-effort cleanup only
            pass


class RequestConcurrencyTest(_InstanceTestCase):
    def test_same_request_id_runs_the_model_exactly_once(self):
        inst = self._instance(runtime=FakeRuntime(delay=0.05))
        outcomes = []
        start = threading.Barrier(2)

        def worker():
            start.wait()
            try:
                outcomes.append(inst.chat("hello", request_id="rid-1"))
            except BaseException as exc:  # noqa: BLE001 - asserted below
                outcomes.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        # the model executed once and both callers received the settled reply
        self.assertEqual(len(inst.runtime.turns), 1)
        self.assertEqual(len(outcomes), 2)
        for outcome in outcomes:
            self.assertIsInstance(outcome, dict, outcome)
            self.assertEqual(outcome["reply"], "reply to hello")
            self.assertEqual(outcome["request_id"], "rid-1")
        rows = inst.requests.execute("SELECT id, status FROM requests").fetchall()
        self.assertEqual(rows, [("rid-1", "READY")])

    def test_different_request_ids_both_complete_without_deadlock(self):
        inst = self._instance()
        outcomes = []
        start = threading.Barrier(2)

        def worker(rid):
            start.wait()
            try:
                outcomes.append(inst.chat("text " + rid, request_id=rid))
            except BaseException as exc:  # noqa: BLE001
                outcomes.append(exc)

        threads = [threading.Thread(target=worker, args=(rid,)) for rid in ("rid-a", "rid-b")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(len(inst.runtime.turns), 2)
        self.assertEqual(len(outcomes), 2)
        for outcome in outcomes:
            self.assertIsInstance(outcome, dict, outcome)
        rows = inst.requests.execute("SELECT id, status FROM requests ORDER BY id").fetchall()
        self.assertEqual(rows, [("rid-a", "READY"), ("rid-b", "READY")])

    def test_replay_of_a_settled_request_does_not_call_the_model_again(self):
        inst = self._instance()
        first = inst.chat("same", request_id="rid-x")
        again = inst.chat("same", request_id="rid-x")
        self.assertEqual(again["reply"], first["reply"])
        self.assertEqual(len(inst.runtime.turns), 1)

    def test_same_request_id_with_different_content_is_rejected(self):
        inst = self._instance()
        inst.chat("original", request_id="rid-y")
        with self.assertRaises(ValueError):
            inst.chat("different", request_id="rid-y")

    def test_model_failure_leaves_the_documented_uncertain_state(self):
        inst = self._instance(runtime=FakeRuntime(fail=True))
        with self.assertRaises(RuntimeError):
            inst.chat("boom", request_id="rid-z")
        rows = inst.requests.execute("SELECT id, status FROM requests").fetchall()
        self.assertEqual(rows, [("rid-z", "PROCESSING")])
        # the existing fail-closed design refuses to repeat it, with a reason
        with self.assertRaises(RuntimeError) as caught:
            inst.chat("boom", request_id="rid-z")
        self.assertIn("uncertain outcome", str(caught.exception))

    def test_no_transaction_is_open_across_the_model_call(self):
        observed = {}

        class ProbingRuntime(FakeRuntime):
            def handle_turn(self, conversation_id, text, message_id=None):
                observed["in_transaction"] = inst.requests.in_transaction
                return FakeRuntime.handle_turn(self, conversation_id, text, message_id)

        inst = self._instance(runtime=ProbingRuntime())
        inst.chat("probe", request_id="rid-t")
        self.assertFalse(observed["in_transaction"])


class TeardownTest(_InstanceTestCase):
    def setUp(self):
        super().setUp()
        from app import session as session_module
        self.session_module = session_module
        self.real_store = session_module.ConversationStore
        self.closed = []
        self.addCleanup(setattr, session_module, "ConversationStore", self.real_store)

    def _closable(self, name):
        closed = self.closed

        class Closable:
            def __init__(self, root):
                self.root = root

            def close(self):
                closed.append(name)

        return Closable

    def test_new_session_closes_the_previous_store(self):
        inst = self._instance()
        inst.native.store = self._closable("old")("old")
        replacement = self._closable("new")
        self.session_module.ConversationStore = replacement
        inst.new_session()
        self.assertEqual(self.closed, ["old"])
        self.assertIsInstance(inst.native.store, replacement)

    def test_failed_new_session_keeps_the_previous_store_usable(self):
        inst = self._instance()
        previous = self._closable("old")("old")
        inst.native.store = previous

        class Exploding:
            def __init__(self, root):
                raise RuntimeError("cannot open session store")

        self.session_module.ConversationStore = Exploding
        with self.assertRaises(RuntimeError):
            inst.new_session()
        self.assertIs(inst.native.store, previous)
        self.assertEqual(self.closed, [])

    def test_close_releases_resolver_store_and_is_idempotent(self):
        inst = self._instance()
        inst.native.m37_resolver = self._closable("resolver")("r")
        inst.native.memory_controls = self._closable("controls")("c")
        inst.native.store = self._closable("store")("s")
        inst.close()
        inst.close()  # must be safe to repeat
        self.assertEqual(sorted(self.closed), ["controls", "resolver", "store"])
        self.assertIsNone(inst.native.m37_resolver)
        self.assertIsNone(inst.native.memory_controls)

    def test_close_tolerates_objects_without_close(self):
        inst = self._instance()
        inst.native.store = SimpleNamespace(root="no-close-attr")
        inst.close()
        inst.close()


if __name__ == "__main__":
    unittest.main()
