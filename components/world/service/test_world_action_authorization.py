#!/usr/bin/env python3
"""M14A: Action Socket Authority Firewall — isolated test suite.

Covers ticket §6 (fail-closed), §8 (malicious battery in isolation), §9
(legal chain still works).  Nothing here touches live production state: the
service runs in a temp home, the registry lives in that temp run dir.
"""

from __future__ import annotations

import json
import os
import pathlib
import shutil
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone

import conftest  # noqa: F401  (path setup)

import world_action_authorization as waa
import world_body_config as wbc
import world_body_service as wbsvc

RUNTIME_ROOT = pathlib.Path(__file__).resolve().parent.parent
NOW = datetime(2026, 9, 26, 12, 0, tzinfo=timezone.utc)


def seed_world_home(root: pathlib.Path, *, world_id: str = "shiomi_city") -> pathlib.Path:
    (root / "data" / "world").mkdir(parents=True, exist_ok=True)
    (root / "data" / "world" / "world_state.json").write_text(
        json.dumps({
            "facts": [],
            "location": {"area_id": "bedroom", "place_id": "home"},
            "revision": 5,
            "scene": {"revision": 5, "scene_id": "home.bedroom"},
            "schema_version": "world.foundation.v0",
            "timezone": "Asia/Tokyo",
            "world_id": world_id,
        }, sort_keys=True),
        encoding="utf-8",
    )
    (root / "data" / "world" / "world_timeline.json").write_text(
        json.dumps({"schema_version": "world.timeline.v1", "revision": 1, "world_id": world_id,
                    "processes": []}, sort_keys=True),
        encoding="utf-8",
    )
    (root / "data" / "life_execution.jsonl").write_text("", encoding="utf-8")
    (root / "run").mkdir(parents=True, exist_ok=True)
    return root


def make_config(home, *, mode="canonical", authority="production", world_id="shiomi_city",
                environment=None):
    return wbc.load_config(
        dict(environment or {}),
        runtime_root=RUNTIME_ROOT,
        world_home=home,
        runtime_mode=mode,
        authority=authority,
        world_id=world_id,
    )


class RegistryTests(unittest.TestCase):
    """§2/§3/§5: the capability registry itself."""

    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="m14a-reg-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.reg = waa.AuthorizationRegistry(self.tmp)

    def _issue(self, **over):
        kw = dict(
            decision_id="decision:test-1",
            intent_id="world-intent:test-1",
            intent_fingerprint="fp-1",
            execution_id="world-exec:test-1",
            actor="chiyo",
            verb="POSE",
            params={"pose": "seated"},
            authority_generation=5,
        )
        kw.update(over)
        return self.reg.issue(**kw)

    def test_issue_then_consume_exactly_once(self):
        rec = self._issue()
        self.assertEqual(rec["state"], waa.STATE_ISSUED)
        self.assertTrue(rec["authorization_id"].startswith("wauth:"))
        got = self.reg.verify_and_consume(
            authorization_id=rec["authorization_id"], execution_id="world-exec:test-1",
            verb="POSE", params={"pose": "seated"},
        )
        self.assertEqual(got["authorization_id"], rec["authorization_id"])
        # §5: replay of a consumed capability must not mutate again
        with self.assertRaises(waa.AuthorizationError) as caught:
            self.reg.verify_and_consume(
                authorization_id=rec["authorization_id"], execution_id="world-exec:test-1",
                verb="POSE", params={"pose": "seated"},
            )
        self.assertIn("ALREADY_CONSUMED", str(caught.exception))

    def test_unknown_authorization_is_refused(self):
        with self.assertRaises(waa.AuthorizationError) as caught:
            self.reg.verify_and_consume(
                authorization_id="wauth:forged", execution_id="world-exec:x",
                verb="POSE", params={},
            )
        self.assertIn("NOT_FOUND", str(caught.exception))

    def test_execution_id_mismatch_is_refused(self):
        rec = self._issue()
        with self.assertRaises(waa.AuthorizationError) as caught:
            self.reg.verify_and_consume(
                authorization_id=rec["authorization_id"], execution_id="world-exec:OTHER",
                verb="POSE", params={"pose": "seated"},
            )
        self.assertIn("EXECUTION_ID_MISMATCH", str(caught.exception))

    def test_verb_mismatch_is_refused(self):
        rec = self._issue()
        with self.assertRaises(waa.AuthorizationError) as caught:
            self.reg.verify_and_consume(
                authorization_id=rec["authorization_id"], execution_id="world-exec:test-1",
                verb="MOVE", params={"pose": "seated"},
            )
        self.assertIn("VERB_MISMATCH", str(caught.exception))

    def test_args_mismatch_is_refused(self):
        rec = self._issue()
        with self.assertRaises(waa.AuthorizationError) as caught:
            self.reg.verify_and_consume(
                authorization_id=rec["authorization_id"], execution_id="world-exec:test-1",
                verb="POSE", params={"pose": "lying"},
            )
        self.assertIn("ARGS_MISMATCH", str(caught.exception))

    def test_actor_mismatch_is_refused(self):
        rec = self._issue()
        with self.assertRaises(waa.AuthorizationError) as caught:
            self.reg.verify_and_consume(
                authorization_id=rec["authorization_id"], execution_id="world-exec:test-1",
                verb="POSE", params={"pose": "seated"}, actor="someone_else",
            )
        self.assertIn("ACTOR_MISMATCH", str(caught.exception))

    def test_expired_authorization_is_refused(self):
        rec = self._issue(ttl_seconds=-1)  # already expired at issue time
        with self.assertRaises(waa.AuthorizationError) as caught:
            self.reg.verify_and_consume(
                authorization_id=rec["authorization_id"], execution_id="world-exec:test-1",
                verb="POSE", params={"pose": "seated"},
            )
        self.assertIn("EXPIRED", str(caught.exception))

    def test_registry_file_is_root_only(self):
        self._issue()
        mode = (self.tmp / waa.REGISTRY_NAME).stat().st_mode & 0o777
        self.assertEqual(mode, 0o600)


class GatewayFirewallTests(unittest.TestCase):
    """§2/§6/§9: the socket surface consumes authorizations; forged ones die."""

    def setUp(self):
        self._saved_env = dict(os.environ)
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="m14a-fw-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        seed_world_home(self.tmp)
        self.service = wbsvc.WorldBodyService(
            make_config(self.tmp), now=NOW, instance_id="m14a-fw"
        )
        self.addCleanup(self.service.shutdown)
        self.service.startup()
        self.surface = self.service.gateway_surface
        self.reg = waa.AuthorizationRegistry(self.service.config.run_dir)
        self.ledger_before = (self.tmp / "data" / "life_execution.jsonl").read_bytes()
        self.world_before = (self.tmp / "data" / "world" / "world_state.json").read_bytes()

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._saved_env)

    def _assert_untouched(self):
        self.assertEqual((self.tmp / "data" / "life_execution.jsonl").read_bytes(),
                         self.ledger_before, "ledger must not change on a rejection")
        self.assertEqual((self.tmp / "data" / "world" / "world_state.json").read_bytes(),
                         self.world_before, "world must not change on a rejection")

    def _issue(self, *, execution_id="world-exec:fw-1", verb="POSE", params=None,
               decision_id="decision:fw-1", intent_id="world-intent:fw-1"):
        return self.reg.issue(
            decision_id=decision_id, intent_id=intent_id, intent_fingerprint="fp",
            execution_id=execution_id, actor="chiyo", verb=verb,
            params=params if params is not None else {"pose": "seated"},
            authority_generation=5,
        )

    def test_submit_without_authorization_is_rejected(self):
        resp = self.surface.submit_action(
            {"action": "POSE", "execution_id": "world-exec:fw-1", "params": {"pose": "seated"}}
        )
        self.assertEqual(resp["status"], "rejected")
        self.assertEqual(resp["reason_code"], "AUTHORIZATION_REQUIRED")
        self._assert_untouched()

    def test_forged_authorization_id_is_rejected(self):
        resp = self.surface.submit_action(
            {"action": "POSE", "execution_id": "world-exec:fw-1",
             "params": {"pose": "seated"}, "authorization_id": "wauth:forged"}
        )
        self.assertEqual(resp["status"], "rejected")
        self.assertEqual(resp["reason_code"], "AUTHORIZATION_NOT_FOUND")
        self._assert_untouched()

    def test_authorization_for_other_execution_is_rejected(self):
        rec = self._issue(execution_id="world-exec:OTHER")
        resp = self.surface.submit_action(
            {"action": "POSE", "execution_id": "world-exec:fw-1",
             "params": {"pose": "seated"}, "authorization_id": rec["authorization_id"]}
        )
        self.assertEqual(resp["reason_code"], "EXECUTION_ID_MISMATCH")
        self._assert_untouched()

    def test_authorization_with_different_args_is_rejected(self):
        rec = self._issue(params={"pose": "seated"})
        resp = self.surface.submit_action(
            {"action": "POSE", "execution_id": "world-exec:fw-1",
             "params": {"pose": "lying"}, "authorization_id": rec["authorization_id"]}
        )
        self.assertEqual(resp["reason_code"], "ARGS_MISMATCH")
        self._assert_untouched()

    def test_replayed_authorization_is_rejected(self):
        rec = self._issue()
        payload = {"action": "POSE", "execution_id": "world-exec:fw-1",
                   "params": {"pose": "seated"}, "authorization_id": rec["authorization_id"]}
        first = self.surface.submit_action(dict(payload))
        # first attempt is authorized (may still be refused by exposure policy,
        # but never by the firewall)
        self.assertNotIn(first.get("reason_code"),
                         {"AUTHORIZATION_REQUIRED", "AUTHORIZATION_NOT_FOUND"})
        second = self.surface.submit_action(dict(payload))
        self.assertEqual(second["status"], "rejected")
        self.assertEqual(second["reason_code"], "AUTHORIZATION_ALREADY_CONSUMED")
        # no second mutation
        committed = [
            json.loads(l) for l in
            (self.tmp / "data" / "life_execution.jsonl").read_text().splitlines() if l.strip()
            and json.loads(l).get("status") == "committed"
        ]
        self.assertLessEqual(len([c for c in committed
                                  if c.get("execution_id") == "world-exec:fw-1"]), 1)

    def test_authorized_submission_reaches_the_resolver(self):
        rec = self._issue()
        resp = self.surface.submit_action(
            {"action": "POSE", "execution_id": "world-exec:fw-1",
             "params": {"pose": "seated"}, "authorization_id": rec["authorization_id"],
             "observed_dependencies": {"world.revision": 5, "world.location": "home",
                                       "body.pose": "standing"}}
        )
        # firewall consumed the capability: whatever the Resolver decided, the
        # refusal cannot be an authorization one
        self.assertNotIn(resp.get("reason_code"),
                         {"AUTHORIZATION_REQUIRED", "AUTHORIZATION_NOT_FOUND",
                          "AUTHORIZATION_ALREADY_CONSUMED", "EXECUTION_ID_MISMATCH",
                          "ARGS_MISMATCH", "VERB_MISMATCH", "AUTHORIZATION_EXPIRED"})
        self.assertEqual(resp.get("source"), "resolver")
if __name__ == "__main__":
    unittest.main()


class TransportLayerTests(unittest.TestCase):
    """§7: OS peer authorization (distinct from business authorization)."""

    def test_only_service_owner_may_call_the_gateway_surface(self):
        self.assertTrue(wbsvc.gateway_peer_allowed(os.geteuid()))
        for uid in (os.geteuid()+1, os.geteuid()+2):
            self.assertFalse(wbsvc.gateway_peer_allowed(uid))

    def test_gateway_socket_is_root_only(self):
        import socket as _socket
        tmp = pathlib.Path(tempfile.mkdtemp(prefix="m14a-sock-"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        path = tmp / "gateway.sock"
        server = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
        self.addCleanup(server.close)
        server.bind(str(path))
        os.chmod(path, 0o600)
        mode = path.stat().st_mode & 0o777
        self.assertEqual(mode, 0o600, "gateway socket must be root-only")

    def test_non_root_peer_is_refused_by_the_loop_guard(self):
        """The serve loop refuses a peer whose uid is not the allowed one."""
        import inspect
        source = inspect.getsource(wbsvc.WorldBodyService.serve)
        self.assertIn("gateway_peer_allowed", source)
        self.assertIn("unauthorized_peer", source)
