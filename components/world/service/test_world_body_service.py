#!/usr/bin/env python3
"""M13A service tests -- the standalone World/Body runtime.

Covers ticket section 57 plus the surrounding refusals.  Every test works in an
isolated temp home; none of them touches the live gateway tree, and one test
exists purely to prove that.
"""

from __future__ import annotations

import json
import os
import pathlib
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

import conftest  # noqa: F401  (path setup)

import body_consequence as bcon
import body_reader as bread
import body_runtime as br
import world_body_config as wbc
import world_body_service as wbsvc
import world_execution_ledger as wel

RUNTIME_ROOT = pathlib.Path(__file__).resolve().parent.parent
NOW = datetime(2026, 9, 25, 12, 0, tzinfo=timezone.utc)


def seed_world_home(root: pathlib.Path, *, world_id: str = "shiomi_city") -> pathlib.Path:
    (root / "data" / "world").mkdir(parents=True, exist_ok=True)
    (root / "data" / "world" / "world_state.json").write_text(
        json.dumps(
            {
                "facts": [],
                "location": {"area_id": "bedroom", "place_id": "home"},
                "revision": 5,
                "scene": {"revision": 5, "scene_id": "home.bedroom"},
                "schema_version": "world.foundation.v0",
                "timezone": "Asia/Tokyo",
                "world_id": world_id,
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    (root / "data" / "world" / "world_timeline.json").write_text(
        json.dumps(
            {"schema_version": "world.timeline.v1", "revision": 1, "world_id": world_id,
             "processes": []},
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    (root / "data" / "life_execution.jsonl").write_text("", encoding="utf-8")
    return root


def terminal_entry(execution_id: str, action: str = "MOVE", *, status: str = "committed"):
    return {
        "execution_id": execution_id,
        "action_type": action,
        "actor_id": "chiyo",
        "status": status,
        "reason_code": "OK",
        "timestamp": NOW.isoformat(),
        "proposal_ref": action,
        "observed_dependencies": {},
        "expected_versions": {},
        "world_mutation_refs": [],
        "body_consequence_refs": [],
        "cross_authority_refs": [],
        "before_state_ref": None,
        "after_state_ref": None,
        "before": None,
        "after": None,
        "reconciliation_required": False,
    }


def make_config(home, *, mode="canonical", authority="production",
                world_id="shiomi_city", environment=None):
    return wbc.load_config(
        dict(environment or {}),
        runtime_root=RUNTIME_ROOT,
        world_home=home,
        runtime_mode=mode,
        authority=authority,
        world_id=world_id,
    )


class ServiceTestBase(unittest.TestCase):
    def setUp(self):
        self._saved_env = dict(os.environ)
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="m13a-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        seed_world_home(self.tmp)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._saved_env)

    def start(self, home=None, *, now=NOW, instance_id=None, config=None):
        home = home or self.tmp
        cfg = config or make_config(home)
        service = wbsvc.WorldBodyService(
            cfg, now=now, instance_id=instance_id or ("inst-%d" % id(self))
        )
        self.addCleanup(service.shutdown)
        return service, service.startup()

    def run_startup_failure(self, home=None, *, config=None, now=NOW,
                            world_id="shiomi_city"):
        service = wbsvc.WorldBodyService(
            config or make_config(home or self.tmp, world_id=world_id),
            now=now,
            instance_id="failing-%d" % id(self),
        )
        self.addCleanup(service.shutdown)
        with self.assertRaises(wbsvc.StartupRefused) as caught:
            service.startup()
        return caught.exception


# ----------------------------------------------------------------------
# 1. BodyReader production gate (ticket sections 11/12)
# ----------------------------------------------------------------------


class BodyReaderGateTests(unittest.TestCase):
    def test_production_canonical_allowed_without_test_only(self):
        env = {
            bread.MODE_ENV: "canonical",
            bread.PRODUCTION_AUTHORITY_ENV: "production",
            bread.RUNTIME_MODE_ENV: "canonical",
        }
        self.assertNotIn(bread.TEST_ONLY_ENV, env)
        self.assertEqual(bread.resolve_mode(env), bread.MODE_CANONICAL)
        self.assertTrue(bread.production_canonical_configured(env))

    def test_canonical_without_configuration_is_still_downgraded(self):
        self.assertEqual(
            bread.resolve_mode({bread.MODE_ENV: "canonical"}), bread.MODE_LEGACY
        )

    def test_half_configured_production_is_not_enough(self):
        for env in (
            {bread.MODE_ENV: "canonical", bread.PRODUCTION_AUTHORITY_ENV: "production"},
            {bread.MODE_ENV: "canonical", bread.RUNTIME_MODE_ENV: "canonical"},
            {bread.MODE_ENV: "canonical", bread.PRODUCTION_AUTHORITY_ENV: "shadow",
             bread.RUNTIME_MODE_ENV: "canonical"},
        ):
            self.assertEqual(bread.resolve_mode(env), bread.MODE_LEGACY, env)

    def test_test_only_path_still_works(self):
        self.assertEqual(
            bread.resolve_mode({bread.MODE_ENV: "canonical", bread.TEST_ONLY_ENV: "1"}),
            bread.MODE_CANONICAL,
        )


# ----------------------------------------------------------------------
# 2. Configuration
# ----------------------------------------------------------------------


class ConfigTests(unittest.TestCase):
    def test_refuses_the_live_gateway_home(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as home, patch.object(wbc, "LIVE_GATEWAY_HOME", pathlib.Path(home)):
            with self.assertRaises(wbc.ConfigError):
                make_config(pathlib.Path(home))

    def test_refuses_a_home_inside_the_gateway_tree(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as home, patch.object(wbc, "LIVE_GATEWAY_HOME", pathlib.Path(home)):
            with self.assertRaises(wbc.ConfigError):
                make_config(pathlib.Path(home) / "data" / "world")

    def test_refuses_a_relative_home(self):
        with self.assertRaises(wbc.ConfigError):
            wbc.load_config({"CHIYO_WORLD_HOME": "relative/path"}, require_existing=False)

    def test_canonical_requires_explicit_production_authority(self):
        tmp = pathlib.Path(tempfile.mkdtemp(prefix="m13a-cfg-"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        seed_world_home(tmp)
        with self.assertRaises(wbc.ConfigError):
            make_config(tmp, mode="canonical", authority="shadow")

    def test_owned_environment_never_points_at_the_gateway(self):
        tmp = pathlib.Path(tempfile.mkdtemp(prefix="m13a-env-"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        seed_world_home(tmp)
        config = make_config(tmp)
        owned = config.owned_environment()
        self.assertEqual(owned["HERMES_HOME"], str(tmp))
        self.assertEqual(wbc.environment_leaks_into_gateway(owned), [])
        self.assertNotIn(str(wbc.LIVE_GATEWAY_HOME), owned["HERMES_HOME"])

    def test_canonical_opens_the_world_substrate_gate(self):
        tmp = pathlib.Path(tempfile.mkdtemp(prefix="m13a-gate-"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        seed_world_home(tmp)
        canonical = make_config(tmp)
        self.assertEqual(
            canonical.owned_environment()[wbc.SUBSTRATE_ENABLED_ENV], "1"
        )
        shadow = make_config(tmp, mode="shadow", authority="shadow")
        self.assertEqual(
            shadow.owned_environment()[wbc.SUBSTRATE_ENABLED_ENV], "0"
        )

# ----------------------------------------------------------------------
# 3. Authority lease (ticket sections 13/14)
# ----------------------------------------------------------------------


class AuthorityTests(ServiceTestBase):
    def _lease(self, instance_id):
        from world_body_authority import AuthorityLease

        self.tmp.joinpath("run").mkdir(parents=True, exist_ok=True)
        return AuthorityLease(
            self.tmp / "run", instance_id=instance_id, world_home=self.tmp, now=NOW
        )

    def test_acquire_release_round_trip(self):
        lease = self._lease("a")
        first = lease.acquire()
        self.assertTrue(first["held_by_this_instance"])
        self.assertTrue(lease.held)
        lease.release()
        self.assertFalse(lease.held)

    def test_second_instance_fails_closed(self):
        from world_body_authority import AuthorityConflict, lock_is_held

        first = self._lease("a")
        first.acquire()
        self.addCleanup(first.release)
        with self.assertRaises(AuthorityConflict):
            self._lease("b").acquire()
        self.assertTrue(lock_is_held(self.tmp / "run"))

    def test_marker_is_evidence_not_truth(self):
        lease = self._lease("a")
        lease.acquire()
        self.addCleanup(lease.release)
        status = lease.status()
        self.assertFalse(status["marker_is_truth"])
        self.assertIn("flock", status["marker_disclaimer"])
        self.assertTrue(status["marker"]["marker_is_truth"] is False)

    def test_stale_marker_is_reclaimed_and_recorded(self):
        from world_body_authority import AuthorityLease

        run = self.tmp / "run"
        run.mkdir(parents=True, exist_ok=True)
        (run / "authority.json").write_text(
            json.dumps({"instance_id": "dead-instance", "pid": 999999}), encoding="utf-8"
        )
        lease = AuthorityLease(run, instance_id="fresh", world_home=self.tmp, now=NOW)
        status = lease.acquire()
        self.addCleanup(lease.release)
        self.assertEqual(status["reclaimed_from"]["instance_id"], "dead-instance")


# ----------------------------------------------------------------------
# 4. Startup: fail closed, then ready (ticket sections 9/10/26/27/28)
# ----------------------------------------------------------------------


class StartupTests(ServiceTestBase):
    def test_clean_start_is_ready(self):
        service, report = self.start()
        self.assertTrue(report["ready"])
        self.assertTrue(service.body_bootstrapped)
        self.assertEqual(service.health()["status"], "OK")
        self.assertEqual(service.readiness()["status"], "READY")

    def test_wrong_home_fails(self):
        missing = self.tmp / "does-not-exist"
        error = self.run_startup_failure(
            config=wbc.load_config(
                {}, runtime_root=RUNTIME_ROOT, world_home=missing,
                runtime_mode="canonical", authority="production", require_existing=False,
            )
        )
        self.assertEqual(error.condition, "HOME_PATH_WRONG")

    def test_wrong_world_id_fails(self):
        error = self.run_startup_failure(world_id="somewhere_else")
        self.assertEqual(error.condition, "WORLD_ID_MISMATCH")

    def test_unknown_world_schema_fails(self):
        path = self.tmp / "data" / "world" / "world_state.json"
        payload = json.loads(path.read_text())
        payload["schema_version"] = "galaxy.v9"
        path.write_text(json.dumps(payload), encoding="utf-8")
        error = self.run_startup_failure()
        self.assertEqual(error.condition, "WORLD_SCHEMA_UNKNOWN")

    def test_missing_world_state_fails(self):
        (self.tmp / "data" / "world" / "world_state.json").unlink()
        error = self.run_startup_failure()
        self.assertEqual(error.condition, "WORLD_UNAVAILABLE")

    def test_invalid_binding_fails(self):
        import world_body_substrate as wbs

        store = wbs.SubstrateStore(wbs.substrate_path(self.tmp))
        store.save(wbs.initial_substrate(world_id="not_shiomi_city"))
        error = self.run_startup_failure()
        self.assertEqual(error.condition, "BINDING_INVALID")

    def test_corrupt_ledger_fails(self):
        ledger_path = self.tmp / "data" / "life_execution.jsonl"
        wel.ExecutionLedger(ledger_path).append(terminal_entry("tamper-me"))
        lines = [json.loads(line) for line in ledger_path.read_text().splitlines() if line.strip()]
        lines[-1]["entry_hash"] = "0" * 64
        ledger_path.write_text(
            "\n".join(json.dumps(line, sort_keys=True) for line in lines) + "\n",
            encoding="utf-8",
        )
        error = self.run_startup_failure()
        self.assertEqual(error.condition, "LEDGER_CORRUPT")

    def test_missing_body_layer_bootstraps_instead_of_resetting(self):
        service, report = self.start()
        self.assertTrue(report["body_bootstrapped"])
        self.assertTrue(service.body_state is not None)
        self.assertEqual(service.body_state["version"], 0)

    def test_health_and_readiness_are_distinct(self):
        service, _ = self.start()
        service.ready = False  # process still alive, reconciliation not complete
        self.assertEqual(service.health()["status"], "OK")
        self.assertEqual(service.readiness()["status"], "NOT_READY")
        self.assertIn("reconciliation_complete", service.readiness()["checks"])


# ----------------------------------------------------------------------
# 5. Reconciliation: Crash A / B / C (ticket sections 21/43/44)
# ----------------------------------------------------------------------


class ReconciliationTests(ServiceTestBase):
    def _consequences(self):
        return bcon.ConsequenceLedger(self.tmp / "data" / "body" / "consequences.jsonl")

    def test_crash_a_gap_is_healed_exactly_once(self):
        wel.ExecutionLedger(self.tmp / "data" / "life_execution.jsonl").append(
            terminal_entry("crash-a")
        )
        service, _ = self.start()
        records = self._consequences().read_all()
        self.assertEqual([r["state"] for r in records], ["applied"])
        self.assertEqual(len(service.startup_reconciliation["healed"]), 1)
        service.shutdown()

        second, _ = self.start(instance_id="second")
        self.assertEqual(len(second.startup_reconciliation["healed"]), 0)
        self.assertEqual(second.startup_reconciliation["already_applied"], ["crash-a"])
        self.assertEqual(len(self._consequences().read_all()), 1)

    def test_crash_b_already_applied_writes_nothing(self):
        """World and ledger committed, body consequence already recorded."""

        wel.ExecutionLedger(self.tmp / "data" / "life_execution.jsonl").append(
            terminal_entry("crash-b")
        )
        ledger = self._consequences()
        store = br.BodyStore(self.tmp / "data" / "body")
        runtime = br.initial_runtime(at=NOW)
        store.save_profile(br.build_profile())
        outcome = bcon.apply_for_execution(
            {"kind": "world_action_result", "execution_id": "crash-b", "action": "MOVE",
             "status": "applied", "reason_code": "OK"},
            body_id="chiyo_body", ledger=ledger,
            runtime_state=runtime, at=NOW, pose="standing",
        )
        self.assertTrue(outcome["applied"])
        store.save_runtime(outcome["state"])

        before = len(ledger.read_all())
        service, _ = self.start()
        self.assertEqual(service.startup_reconciliation["healed"], [])
        self.assertEqual(service.startup_reconciliation["already_applied"], ["crash-b"])
        self.assertEqual(len(ledger.read_all()), before)
        self.assertEqual(len(ledger.read_all()), before)

    def test_crash_c_unknown_is_never_graded(self):
        ledger_path = self.tmp / "data" / "life_execution.jsonl"
        entry = terminal_entry("crash-c", status="prepared")
        entry["reconciliation_required"] = True
        wel.ExecutionLedger(ledger_path).append(entry)

        service, _ = self.start()
        unresolved = service.startup_reconciliation["unresolved"]
        self.assertEqual(len(unresolved), 1)
        self.assertEqual(unresolved[0]["resolver_verdict"], "UNKNOWN")
        self.assertEqual(service.startup_reconciliation["healed"], [])
        self.assertFalse(service.startup_reconciliation["wrote_anything"])
        self.assertEqual(self._consequences().read_all(), [])
        service.shutdown()

        second, _ = self.start(instance_id="second")
        self.assertEqual(len(second.startup_reconciliation["unresolved"]), 1)
        self.assertFalse(second.startup_reconciliation["wrote_anything"])

    def test_legacy_journal_rows_are_never_healed(self):
        ledger_path = self.tmp / "data" / "life_execution.jsonl"
        ledger_path.write_text(
            json.dumps({"kind": "life_loop_execution_journal_entry", "schema_version": 1,
                        "execution_id": "legacy-1", "action_type": "MOVE",
                        "status": "applied"}, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        service, _ = self.start()
        self.assertEqual(service.startup_reconciliation["candidate_count"], 0)
        self.assertEqual(self._consequences().read_all(), [])


# ----------------------------------------------------------------------
# 6. Lifecycle: restart, cold restart, shutdown, idling
# ----------------------------------------------------------------------


class LifecycleTests(ServiceTestBase):
    def _fingerprint(self):
        from world_body_reconcile import body_era_execution_ids

        return {
            "world_revision": json.loads(
                (self.tmp / "data" / "world" / "world_state.json").read_text()
            )["revision"],
            "body_version": json.loads(
                (self.tmp / "data" / "body" / "runtime.json").read_text()
            )["version"],
            "ledger": len(body_era_execution_ids(
                wel.ExecutionLedger(self.tmp / "data" / "life_execution.jsonl"))),
            "transitions": len(
                br.BodyStore(self.tmp / "data" / "body").read_transitions()
            ),
        }

    def test_restart_preserves_continuity(self):
        first, _ = self.start()
        before = self._fingerprint()
        first.shutdown()

        second, report = self.start(instance_id="second")
        self.assertFalse(second.body_bootstrapped)
        self.assertFalse(report["body_bootstrapped"])
        self.assertEqual(self._fingerprint(), before)

    def test_cold_restart_is_bounded_catch_up(self):
        first, _ = self.start()
        first.shutdown()

        cold = NOW + timedelta(days=30)
        second, _ = self.start(now=cold, instance_id="cold")
        catch_up = second.startup_catch_up
        self.assertIsNotNone(catch_up)
        self.assertTrue(catch_up["catch_up"]["partial"])
        self.assertLessEqual(
            catch_up["catch_up"]["applied_hours"], br.MAX_HOURS_PER_ADVANCE
        )

    def test_graceful_shutdown_releases_authority_and_socket(self):
        from world_body_authority import lock_is_held

        service, _ = self.start()
        self.assertTrue(service.lease.held)
        report = service.shutdown()
        self.assertTrue(report["lease_released"])
        self.assertFalse(service.lease.held)
        self.assertFalse(lock_is_held(self.tmp / "run"))

    def test_idle_ticks_write_nothing(self):
        service, _ = self.start()
        runtime_path = self.tmp / "data" / "body" / "runtime.json"
        before = runtime_path.read_bytes()
        transitions_before = len(service.body_store.read_transitions())
        for _ in range(5):
            self.assertIsNone(service.tick())
        self.assertEqual(service.counters["writes"], 0)
        self.assertEqual(service.counters["idle_ticks"], 5)
        self.assertEqual(runtime_path.read_bytes(), before)
        self.assertEqual(len(service.body_store.read_transitions()), transitions_before)


# ----------------------------------------------------------------------
# 7. Boundaries (ticket sections 16/17/24/25/31/47/48)
# ----------------------------------------------------------------------


class BoundaryTests(ServiceTestBase):
    def test_no_live_home_write(self):
        """The World/Body service must not put World content into the live home.

        M13B *does* install one gateway-side plugin there on purpose
        (`plugins/chiyo-world-bridge`), and that plugin holds no World code --
        it is a socket client.  What must never appear is the World runtime
        itself: `scripts/`, `data/`, or any World state file.
        """

        service, _ = self.start()
        service.shutdown()
        live = wbc.LIVE_GATEWAY_HOME
        for name in ("scripts", "data", "plugin-data"):
            self.assertFalse((live / name).exists(),
                             "live gateway home gained a %s directory" % name)
        plugin_dir = live / "plugins"
        if plugin_dir.is_dir():
            for child in plugin_dir.iterdir():
                self.assertNotIn(child.name, {
                    "world_living_skeleton", "body_runtime", "world_action_resolver",
                })
            # and nothing World-shaped may be stored in the plugin itself
            for path in plugin_dir.rglob("*"):
                if path.is_file():
                    self.assertNotIn(path.suffix, {".jsonl"},
                                     "world journal inside the gateway home: %s" % path)
        self.assertFalse((live / "world_state.json").exists())

    def test_canonical_service_is_not_feature_disabled(self):
        """A canonical service must actually let World actions reach the gate."""

        service, _ = self.start()
        reply = service.handle("act", {
            "action": "MOVE", "execution_id": "gate-1",
            "authority": "verified_main_self_tool", "confirm_apply": True,
            "params": {"destination_location_id": "nowhere_at_all"},
        })
        self.assertTrue(reply["ok"])
        self.assertNotEqual(reply["result"]["reason_code"], "FEATURE_DISABLED")

    def test_no_heartmind_fallback(self):
        view = bread.read_body(
            hermes_home=self.tmp,
            runtime_state=None,
            environment={
                bread.MODE_ENV: "canonical",
                bread.PRODUCTION_AUTHORITY_ENV: "production",
                bread.RUNTIME_MODE_ENV: "canonical",
            },
            now=NOW,
        )
        self.assertFalse(view["fell_back_to_heartmind"])
        self.assertIsNone(view["source"])
        self.assertFalse(view["readable"])

    def test_control_surface_is_read_only(self):
        service, _ = self.start()
        self.assertTrue(service.handle("status")["ok"])
        self.assertTrue(service.handle("health")["ok"])
        self.assertTrue(service.handle("readiness")["ok"])
        self.assertTrue(service.handle("world")["ok"])
        self.assertTrue(service.handle("body")["ok"])
        self.assertTrue(service.handle("ledger")["ok"])
        self.assertTrue(service.handle("reconcile")["ok"])

    def test_act_requires_a_proposal_and_goes_through_the_resolver(self):
        service, _ = self.start()
        reply = service.handle("act")
        self.assertFalse(reply["ok"])
        self.assertEqual(reply["error"], "act_requires_a_proposal_object")
        self.assertIn("act", wbsvc.WorldBodyService.MUTATING_COMMANDS)
        # a Resolver-shaped proposal is accepted and answered by the Resolver
        outcome = service.handle("act", {"action": "MOVE", "execution_id": "m13a-probe"})
        self.assertTrue(outcome["ok"])
        self.assertIn(outcome["result"]["status"],
                      ("rejected", "failed", "applied", "noop", "disabled"))

    def test_debug_mutation_is_disabled(self):
        service, _ = self.start()
        for forbidden in ("set_fatigue", "debug_set_fatigue", "reset", "force_advance",
                          "write_body", "inject"):
            reply = service.handle(forbidden)
            self.assertFalse(reply["ok"], forbidden)
            self.assertFalse(reply["debug_mutation_enabled"])
        self.assertNotIn("set_fatigue", wbsvc.WorldBodyService.ALLOWED_COMMANDS)
    def test_unit_does_not_depend_on_the_gateway(self):
        unit = pathlib.Path(__file__).resolve().parents[1] / 'deploy/chiyo-world-body.service'
        text = unit.read_text(encoding="utf-8")
        self.assertEqual(unit.name, "chiyo-world-body.service")
        self.assertIn("/opt/chiyo/world/service/world_body_service.py", text)
        self.assertIn("/var/lib/chiyo/world", text)
        self.assertNotIn("./data/persona", text)
        for line in text.splitlines():
            if line.startswith(("Requires=", "After=", "PartOf=", "BindsTo=")):
                self.assertNotIn("hermes-gateway", line, line)
        self.assertNotIn("Requires=hermes-gateway", text)
        for line in text.splitlines():
            if line.startswith(("Requires=", "After=", "PartOf=", "BindsTo=")):
                self.assertNotIn("hermes-gateway", line, line)
        self.assertIn("/var/lib/chiyo/world", text)
        self.assertNotIn("./data/persona", text)

    def test_service_module_imports_only_expected_roots(self):
        """The service must not import the gateway's runtime."""
        source = pathlib.Path(wbsvc.__file__).read_text(encoding="utf-8")
        for forbidden in ("hermes_cli", "hermes-agent-stock", ".hermes-stock"):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main(verbosity=2)
