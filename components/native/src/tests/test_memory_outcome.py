"""NYA-AUDIT-012: an empty resolve result must not be ambiguous.

The resolver returns a tuple of MemoryObjects. Every "nothing to recall" path and
the exception handler used to return the same empty tuple, so a caller could not
tell a normal empty result from a failed memory pipeline. These tests pin the
three required outcomes plus the refusal path, and confirm that failure is still
fail-closed: no stale memory is returned, the objects stay empty, and the verdict
is exposed through the sanitised status report (which is what /chiyo_status and
Instance.health() project).

The resolver is built with ``__new__`` so only the state machine under test is
exercised: no sealed-file check, no configuration, no model.
"""
from __future__ import annotations

from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
for _candidate in (ROOT, ROOT / "src"):
    if str(_candidate) not in sys.path:
        sys.path.insert(0, str(_candidate))

from memory_runtime_v1 import production_resolver as pr  # noqa: E402


class FakeStats:
    def __init__(self):
        self.invocations = 0
        self.identity_denied = 0
        self.no_current_turn = 0
        self.no_surface = 0
        self.consumed_bundles = 0
        self.emitted_memory_objects = 0
        self.errors = 0
        self.fault_injections = 0


def build_resolver(**overrides):
    resolver = pr.ProductionNativeMemoryResolver.__new__(pr.ProductionNativeMemoryResolver)
    resolver.environment = {}
    resolver.stats = FakeStats()
    resolver.state = "READY"
    resolver.reason = "ok"
    resolver.last_resolve_status = None
    resolver.last_resolve_reason = None
    resolver.last_error_type = None
    resolver.logger = pr.logging.getLogger("test.memory-resolver")
    resolver.live_text_gate_enabled = False
    resolver.gate_denied_live_text = 0
    resolver._classify_intent = None
    resolver._identity = {"native_conversation_id": "tg-1"}
    resolver._adapter = object()
    resolver.map_identity = lambda ids: {"ok": True}
    resolver.resolve_current_turn = lambda conversation_id: {"event_id": "ev-1"}
    resolver._consume = lambda got, trigger, mapping, ids: []
    resolver.version = pr.RESOLVER_VERSION
    resolver.recency_seconds = 900
    resolver.component_sha256 = {}
    resolver.m3_sha256 = {}
    resolver.max_memories = 3
    for name, value in overrides.items():
        setattr(resolver, name, value)
    return resolver


class ResolveOutcomeTest(unittest.TestCase):
    def test_no_results_is_ok_empty(self):
        """B: a real empty result reads as OK_EMPTY, not as a failure."""
        resolver = build_resolver()
        resolver._adapter = type("A", (), {
            "evaluate_trigger": lambda self, event_id: {
                "availability": {"outcome": "NO_SURFACE"}, "candidates": []},
        })()
        objects = resolver(current_user_event_id="ev-1")
        self.assertEqual(objects, ())
        self.assertEqual(resolver.last_resolve_status, pr.RESOLVE_OK_EMPTY)
        self.assertEqual(resolver.last_resolve_reason, "NO_SURFACE")
        self.assertIsNone(resolver.last_error_type)
        self.assertEqual(resolver.stats.errors, 0)

    def test_results_are_ok_with_results(self):
        """A: recall that surfaces something reads as OK_WITH_RESULTS."""
        object_ = pr.MemoryObject(
            memory_id="m1", text="用户喜欢蓝色",
            authority=pr.Authority.DIRECT_USER_EVIDENCE,
            source_event_ids=("ev-1",), namespace=pr.Namespace("chiyo", "s", "c", "t"))
        resolver = build_resolver()
        resolver._adapter = type("A", (), {
            "evaluate_trigger": lambda self, event_id: {
                "availability": {"outcome": "SURFACE", "selected_refs": ["c1"]},
                "candidates": []},
        })()
        resolver._consume = lambda got, trigger, mapping, ids: [object_]
        objects = resolver(current_user_event_id="ev-1")
        self.assertEqual(len(objects), 1)
        self.assertEqual(resolver.last_resolve_status, pr.RESOLVE_OK_WITH_RESULTS)
        self.assertIsNone(resolver.last_error_type)

    def test_fault_injection_is_error_and_distinct_from_empty(self):
        """C: a failing pipeline is ERROR, clearly different from OK_EMPTY."""
        resolver = build_resolver(environment={pr.FAULT_ENV: "resolver_raise"})
        with self.assertRaises(RuntimeError):
            resolver(current_user_event_id="ev-1")
        self.assertEqual(resolver.last_resolve_status, pr.RESOLVE_ERROR)
        self.assertEqual(resolver.last_resolve_reason, "FAULT_INJECTION_RESOLVER_RAISE")
        self.assertEqual(resolver.last_error_type, "RuntimeError")
        self.assertNotEqual(resolver.last_resolve_status, pr.RESOLVE_OK_EMPTY)

    def test_adapter_exception_is_error_and_fail_closed(self):
        """C (natural): an internal failure yields NO objects and an ERROR status."""
        def explode(*_args, **_kwargs):
            raise OSError("m3 database is unreadable")

        resolver = build_resolver()
        resolver._consume = explode
        resolver._adapter = type("A", (), {
            "evaluate_trigger": lambda self, event_id: {
                "availability": {"outcome": "SURFACE", "selected_refs": ["c1"]},
                "candidates": []},
        })()
        objects = resolver(current_user_event_id="ev-1")
        # fail-closed: nothing is returned, so no stale memory can leak
        self.assertEqual(objects, ())
        self.assertEqual(resolver.last_resolve_status, pr.RESOLVE_ERROR)
        self.assertEqual(resolver.last_resolve_reason, "RESOLVER_EXCEPTION")
        self.assertEqual(resolver.last_error_type, "OSError")
        self.assertEqual(resolver.stats.errors, 1)
        self.assertNotEqual(resolver.last_resolve_status, pr.RESOLVE_OK_EMPTY)

    def test_not_ready_is_error_not_empty(self):
        resolver = build_resolver(state="UNAVAILABLE", reason="sealed_component_mismatch")
        objects = resolver()
        self.assertEqual(objects, ())
        self.assertEqual(resolver.last_resolve_status, pr.RESOLVE_ERROR)
        self.assertEqual(resolver.last_resolve_reason, "RESOLVER_NOT_READY")

    def test_identity_refusal_is_denied_not_empty(self):
        resolver = build_resolver()
        resolver.map_identity = lambda ids: {"ok": False, "reason": "IDENTITY_DENIED"}
        objects = resolver()
        self.assertEqual(objects, ())
        self.assertEqual(resolver.last_resolve_status, pr.RESOLVE_DENIED)
        self.assertNotEqual(resolver.last_resolve_status, pr.RESOLVE_OK_EMPTY)
        self.assertNotEqual(resolver.last_resolve_status, pr.RESOLVE_ERROR)

    def test_status_report_exposes_the_outcome_without_secrets(self):
        resolver = build_resolver(environment={pr.FAULT_ENV: "resolver_raise"})
        with self.assertRaises(RuntimeError):
            resolver(current_user_event_id="ev-1")
        report = resolver.report()
        self.assertEqual(report["last_resolve_status"], pr.RESOLVE_ERROR)
        self.assertEqual(report["last_resolve_reason"], "FAULT_INJECTION_RESOLVER_RAISE")
        self.assertEqual(report["last_error_type"], "RuntimeError")
        self.assertEqual(report["resolver_state"], "READY")
        # the report must not carry user content or secrets
        blob = repr(report)
        for forbidden in ("用户喜欢蓝色", "api_key", "API_KEY", "Bearer", "/home/", "C:\\"):
            self.assertNotIn(forbidden, blob)


if __name__ == "__main__":
    unittest.main()
