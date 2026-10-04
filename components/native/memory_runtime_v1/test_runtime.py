from __future__ import annotations

import copy
import os
import unittest

from .runtime import (
    Authority,
    ContextType,
    ConversationDigest,
    ConversationRound,
    Eligibility,
    FormationFirewall,
    KillSwitch,
    MemoryObject,
    NativeMemoryRuntime,
    Namespace,
    Persistence,
    RuntimeRequest,
    Visibility,
)
from .canary import CanaryConfig, CanaryMode, ScopedCanaryGate


def memory(ns: Namespace, text: str, authority: Authority = Authority.DIRECT_USER_EVIDENCE) -> MemoryObject:
    return MemoryObject("m-" + text[:4], text, authority, ("event-1",), ns)


class RuntimeContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.ns = Namespace("chiyo", "s1", "c1", "t1")

    def request(self, memories=(), history=(), **kwargs) -> RuntimeRequest:
        return RuntimeRequest(
            user="chiyo", session="s1", conversation="c1", turn="t1",
            user_event_id="event-current", user_text=kwargs.pop("user_text", "我还喜欢蓝色"),
            history=tuple(history), memories=tuple(memories), **kwargs,
        )

    def test_current_user_exact_and_memory_is_ephemeral(self) -> None:
        result = NativeMemoryRuntime(live_switch={KillSwitch.ENV: "false"}).compose(
            self.request((memory(self.ns, "用户喜欢蓝色"),))
        )
        self.assertEqual(result.manifest.current_user_event_id, "event-current")
        self.assertEqual(result.provider_context, [])
        self.assertTrue(result.bypassed)
        self.assertIsNotNone(result.capsule)
        self.assertFalse(result.capsule.valid)

    def test_memory_can_have_material_shadow_effect(self) -> None:
        off = NativeMemoryRuntime().compose(self.request((), user_text="我最喜欢什么颜色"))
        on = NativeMemoryRuntime().compose(self.request((memory(self.ns, "用户最喜欢蓝色"),), user_text="我最喜欢什么颜色"))
        self.assertNotEqual(off.hypothetical_context, on.hypothetical_context)
        self.assertEqual(len(on.manifest.consumed_memory_ids), 1)

    def test_unsupported_authority_is_not_consumed(self) -> None:
        result = NativeMemoryRuntime().compose(
            self.request((memory(self.ns, "可能喜欢绿色", Authority.DERIVED_MEMORY),))
        )
        self.assertEqual(result.manifest.consumed_memory_ids, [])

    def test_digest_is_not_dialogue_or_formation(self) -> None:
        round_item = ConversationRound("r1", self.ns, "e1", "我去了图书馆", "e2", "知道了")
        digest = ConversationDigest("d1", ("r1",), ("e1", "e2"), ("h1",), "用户去过图书馆", "now", "test", "v1", "FULL")
        result = NativeMemoryRuntime().compose(self.request(history=(round_item,), digests=(digest,)))
        self.assertIn("d1", result.manifest.conversation_digest_ids)
        self.assertEqual(result.manifest.formation_eligible_source_ids, ["event-current"])

    def test_tool_chain_atomicity(self) -> None:
        good = ConversationRound("r1", self.ns, "e1", "查天气", "e2", "结果", ((
            {"kind": "tool_call", "id": "call-1"}, {"kind": "tool_result", "id": "result-1"},
        ),))
        good.validate()
        bad = ConversationRound("r2", self.ns, "e3", "查天气", "e4", "结果", ((
            {"kind": "tool_result", "id": "result-2"},
        ),))
        with self.assertRaises(ValueError):
            bad.validate()

    def test_double_authority_fails_closed(self) -> None:
        result = NativeMemoryRuntime().compose(self.request((memory(self.ns, "事实"),), legacy_authoritative=True, legacy_visible=True))
        self.assertTrue(result.bypassed)
        self.assertEqual(result.bypass_reason, "DOUBLE_AUTHORITY")
        self.assertTrue(result.manifest.double_injection_detected)

    def test_kill_switch_fail_closed_and_explicit_live_is_possible_only_outside_shadow(self) -> None:
        shadow = NativeMemoryRuntime(live_switch={KillSwitch.ENV: "true"}).compose(self.request((memory(self.ns, "蓝色"),)))
        self.assertEqual(shadow.provider_context, [])
        live = NativeMemoryRuntime(
            live_switch={KillSwitch.ENV: "true"},
            canary_gate=ScopedCanaryGate(CanaryConfig(master_enabled=True, mode=CanaryMode.GLOBAL)),
        ).compose(self.request((memory(self.ns, "蓝色"),), shadow=False))
        self.assertNotEqual(live.provider_context, [])

    def test_formation_firewall_blocks_recall_writeback(self) -> None:
        firewall = FormationFirewall()
        self.assertTrue(firewall.admit("real_current_user_event", ["event-1"]))
        self.assertFalse(firewall.admit("recall_output", ["memory-1"]))
        self.assertFalse(firewall.block_recall_writeback())
        self.assertGreaterEqual(firewall.blocked, 2)

    def test_namespace_isolation(self) -> None:
        other = Namespace("chiyo", "s2", "c2", "t2")
        result = NativeMemoryRuntime().compose(self.request((memory(other, "异 namespace"),)))
        self.assertEqual(result.manifest.consumed_memory_ids, [])

    def test_manifest_is_not_provider_visible(self) -> None:
        result = NativeMemoryRuntime().compose(self.request())
        self.assertFalse(result.manifest.provider_visible()["provider_visible"])
        self.assertNotIn("user_event_id", result.manifest.provider_visible())

    def test_memory_inputs_and_writes_are_unchanged(self) -> None:
        item = memory(self.ns, "用户喜欢蓝色")
        before = copy.deepcopy(item)
        runtime = NativeMemoryRuntime()
        runtime.compose(self.request((item,)))
        self.assertEqual(item, before)
        self.assertEqual(runtime.snapshot_writes(), {
            "writes": 0, "touch": 0, "reinforce": 0,
            "importance_mutation": 0, "accessibility_mutation": 0,
        })


if __name__ == "__main__":
    unittest.main()
