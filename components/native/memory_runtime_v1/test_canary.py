from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import unittest

from .canary import CanaryConfig, CanaryMode, MatchBy, ScopedCanaryGate


class ScopedCanaryTests(unittest.TestCase):
    def decision(self, data, user="u1", session="s1", conversation="c1", **kwargs):
        return ScopedCanaryGate.from_dict(data).decide("r1", user, session, conversation, **kwargs)

    def test_master_off_is_hard_deny(self):
        result = self.decision({"master_enabled": False, "mode": "CANARY", "allowed_user_ids": ["u1"]})
        self.assertFalse(result.native_context_allowed)
        self.assertEqual(result.reason_code, "MASTER_OFF")

    def test_mode_off_denies(self):
        result = self.decision({"master_enabled": True, "mode": "OFF", "allowed_user_ids": ["u1"]})
        self.assertEqual(result.reason_code, "MODE_OFF")

    def test_empty_allowlist_denies(self):
        result = self.decision({"master_enabled": True, "mode": "CANARY"})
        self.assertEqual(result.reason_code, "EMPTY_ALLOWLIST")

    def test_user_session_conversation_scopes(self):
        user = self.decision({"master_enabled": True, "mode": "CANARY", "allowed_user_ids": ["u1"]})
        session = self.decision({"master_enabled": True, "mode": "CANARY", "allowed_session_ids": ["s1"]})
        conversation = self.decision({"master_enabled": True, "mode": "CANARY", "allowed_conversation_ids": ["c1"]})
        self.assertEqual((user.native_context_allowed, user.matched_by, user.reason_code), (True, MatchBy.USER, "USER_MATCH"))
        self.assertEqual((session.native_context_allowed, session.matched_by, session.reason_code), (True, MatchBy.SESSION, "SESSION_MATCH"))
        self.assertEqual((conversation.native_context_allowed, conversation.matched_by, conversation.reason_code), (True, MatchBy.CONVERSATION, "CONVERSATION_MATCH"))

    def test_wrong_scope_denies_and_same_user_different_session_does_not_spread(self):
        data = {"master_enabled": True, "mode": "CANARY", "allowed_session_ids": ["s1"]}
        self.assertTrue(self.decision(data, user="same", session="s1").native_context_allowed)
        self.assertFalse(self.decision(data, user="same", session="s2").native_context_allowed)

    def test_strict_binding_requires_user_and_session(self):
        data = {"master_enabled": True, "mode": "CANARY", "strict_canary_binding": True, "bindings": [{"user_id": "u1", "session_id": "s1"}]}
        self.assertEqual(self.decision(data).reason_code, "STRICT_BINDING_MATCH")
        self.assertFalse(self.decision(data, user="u1", session="s2").native_context_allowed)

    def test_invalid_and_unknown_config_fail_closed(self):
        self.assertEqual(self.decision({"master_enabled": True, "mode": "UNKNOWN", "allowed_user_ids": ["u1"]}).reason_code, "MASTER_OFF")
        self.assertFalse(ScopedCanaryGate.from_dict({"master_enabled": True, "mode": "CANARY", "allowed_user_ids": "u1"}).decide("r", "u1", "s1", "c1").native_context_allowed)

    def test_interlocks_and_circuit_breaker_override_scope(self):
        data = {"master_enabled": True, "mode": "CANARY", "allowed_user_ids": ["u1"]}
        self.assertEqual(self.decision(data, double_authority=True).reason_code, "DOUBLE_AUTHORITY_BLOCKED")
        self.assertEqual(self.decision(data, double_injection=True).reason_code, "DOUBLE_INJECTION_BLOCKED")
        self.assertEqual(self.decision(data, circuit_fault=True).reason_code, "CIRCUIT_BREAKER_BYPASS")

    def test_global_is_only_a_staging_semantic(self):
        result = self.decision({"master_enabled": True, "mode": "GLOBAL"}, user=None, session=None, conversation=None)
        self.assertTrue(result.native_context_allowed)

    def test_missing_identifiers_never_guess(self):
        data = {"master_enabled": True, "mode": "CANARY", "allowed_user_ids": ["u1"], "allowed_session_ids": ["s1"], "allowed_conversation_ids": ["c1"]}
        result = self.decision(data, user=None, session=None, conversation=None)
        self.assertFalse(result.native_context_allowed)
        self.assertEqual(result.reason_code, "NO_SCOPE_MATCH")

    def test_concurrent_decisions_do_not_cross_contaminate(self):
        data = {"master_enabled": True, "mode": "CANARY", "allowed_user_ids": ["allowed"]}
        gate = ScopedCanaryGate.from_dict(data)
        inputs = [(f"r{i}", "allowed" if i % 2 == 0 else "denied", f"s{i}", f"c{i}") for i in range(100)]
        with ThreadPoolExecutor(max_workers=16) as pool:
            results = list(pool.map(lambda item: gate.decide(*item), inputs))
        for index, result in enumerate(results):
            self.assertEqual(result.native_context_allowed, index % 2 == 0)
            self.assertEqual(result.user_id, "allowed" if index % 2 == 0 else "denied")

    def test_invariants(self):
        for master in (False, True):
            result = self.decision({"master_enabled": master, "mode": "CANARY", "allowed_user_ids": ["u1"]})
            if not master:
                self.assertFalse(result.native_context_allowed)
        self.assertFalse(self.decision({"master_enabled": True, "mode": "CANARY"}).native_context_allowed)


if __name__ == "__main__":
    unittest.main()
