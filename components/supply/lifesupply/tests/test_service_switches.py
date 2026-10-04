"""PHASE 17: the six runtime feature switches, all shipped OFF.

A switch gates *new* writes only.  This module proves the loaded defaults (not a doc string),
the exact refusal reason codes, duplicate-provision idempotency, and that switching anything
off never deletes or rewrites canonical data.
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path

from lifesupply.db import CANONICAL_FILENAME
from lifesupply.service.life_supply import LifeSupplyService, LifeSupplySocketServer
from lifesupply.service.switches import (
    OFF,
    ON,
    SHIPPED_DEFAULTS,
    SWITCH_GATES,
    SWITCH_NAMES,
    FeatureSwitches,
    UnknownSwitch,
    assert_switch_table_complete,
)

TIMEOUT = 6.0


class Sandbox:
    def __init__(self, *, root: Path | None = None, switches: FeatureSwitches | None = None,
                 writer_instance: str = "switches") -> None:
        self.uid = os.geteuid()
        self.root = Path(root) if root is not None else Path(tempfile.mkdtemp(prefix="c10svc_"))
        self.owns_root = root is None
        self.socket_path = self.root / "life-supply.sock"
        self.data_root = self.root / "data"
        self.service = LifeSupplyService(self.data_root, operator_uids={self.uid},
                                         service_uids={self.uid},
                                         switches=switches if switches is not None
                                         else FeatureSwitches.all_on(),
                                         writer_instance=f"{writer_instance}:{os.getpid()}")
        self.server = LifeSupplySocketServer(str(self.socket_path), self.service)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def call(self, request) -> dict:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(TIMEOUT)
            client.connect(str(self.socket_path))
            client.sendall(json.dumps(request, ensure_ascii=False).encode("utf-8") + b"\n")
            line = client.makefile("rb").readline(1_000_001)
        return json.loads(line.decode("utf-8"))

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.socket_path.unlink(missing_ok=True)
        self.service.close()

    def cleanup(self) -> None:
        self.stop()
        # A sandbox root only ever lives under /tmp/c10svc_*: always clean it up.
        shutil.rmtree(self.root, ignore_errors=True)

    def counts(self) -> dict[str, int]:
        uri = f"file:{self.data_root / CANONICAL_FILENAME}?mode=ro"
        with sqlite3.connect(uri, uri=True) as con:
            return {table: int(con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
                    for table in ("workspaces", "managed_artifacts", "artifact_versions",
                                  "permission_grants", "life_supply_uow_journal")}


def issue(box: Sandbox, *, subject: str, capability: str, scope: str, target: str,
          operation_id: str, use_mode: str = "REUSABLE") -> dict:
    return box.call({"op": "issue_grant", "grant": {
        "subject": subject, "capability": capability, "scope": scope, "target": target,
        "use_mode": use_mode, "operation_id": operation_id}})


class ShippedDefaultTests(unittest.TestCase):
    def test_every_switch_ships_off_as_loaded(self) -> None:
        env_without_switches = {key: value for key, value in os.environ.items()
                                if not any(key.endswith(name) for name in SWITCH_NAMES)}
        loaded = FeatureSwitches.from_env(env_without_switches)
        self.assertEqual(sorted(loaded.as_dict()), sorted(SWITCH_NAMES))
        for name in SWITCH_NAMES:
            self.assertEqual(loaded.state(name), OFF, name)
            self.assertFalse(loaded.enabled(name), name)
        self.assertTrue(loaded.is_all_off())
        self.assertEqual(SHIPPED_DEFAULTS, {name: OFF for name in SWITCH_NAMES})
        self.assertEqual(sorted(SWITCH_GATES), sorted(SWITCH_NAMES))
        assert_switch_table_complete()
        self.assertEqual(SWITCH_GATES["PERSONAL_CANDIDATE_CONSUMPTION"], ())
        self.assertEqual(SWITCH_GATES["AUTONOMOUS_WAKE"], ())
        self.assertEqual(SWITCH_GATES["PROACTIVE_CONTACT"], ())

        # The service publishes the *loaded* values, and refuses the gated write.
        box = Sandbox(root=Path(tempfile.mkdtemp(prefix="c10svc_")), switches=loaded)
        try:
            status = box.call({"op": "status"})
            self.assertEqual(status["switches"], {name: OFF for name in SWITCH_NAMES})
            refused = box.call({"op": "provision_workspace", "grant_id": "g",
                                "operation_key": "k", "workspace": {"subject_id": "s"}})
            self.assertEqual(refused["status"], "BLOCKED", refused)
            self.assertEqual(refused["reason"], "WORKSPACE_PROVISION_OFF")
        finally:
            box.cleanup()

    def test_environment_values_are_parsed_and_fail_closed(self) -> None:
        self.assertTrue(FeatureSwitches.from_env({"LIFE_SUPPLY_WORKSPACE_PROVISION": "ON"})
                        .enabled("WORKSPACE_PROVISION"))
        self.assertTrue(FeatureSwitches.from_env({"AUTONOMOUS_WAKE": "on"})
                        .enabled("AUTONOMOUS_WAKE"))
        self.assertTrue(FeatureSwitches({"PROACTIVE_CONTACT": True}).enabled("PROACTIVE_CONTACT"))
        self.assertFalse(FeatureSwitches.from_env({"LIFE_SUPPLY_WORKSPACE_PROVISION": "banana"})
                         .enabled("WORKSPACE_PROVISION"))
        self.assertFalse(FeatureSwitches.from_env({"LIFE_SUPPLY_WORKSPACE_PROVISION": ""})
                         .enabled("WORKSPACE_PROVISION"))
        self.assertTrue(FeatureSwitches.from_env({"LIFE_SUPPLY_WORKSPACE_PROVISION": "ON",
                                                  "LIFE_SUPPLY_AUTONOMOUS_WAKE": "OFF"}).is_all_off()
                        is False)
        with self.assertRaises(UnknownSwitch):
            FeatureSwitches({"NOT_A_SWITCH": "ON"})
        with self.assertRaises(UnknownSwitch):
            FeatureSwitches.all_on().state("NOT_A_SWITCH")
        self.assertEqual(FeatureSwitches.all_off().as_dict(), SHIPPED_DEFAULTS)


class SwitchGateTests(unittest.TestCase):
    def test_workspace_provision_off_refusal_is_stable(self) -> None:
        switches = FeatureSwitches({"ARTIFACT_EXTERNAL_ACTION": ON})
        box = Sandbox(root=Path(tempfile.mkdtemp(prefix="c10svc_")), switches=switches)
        try:
            subject = "test:switch"
            grant = issue(box, subject=subject, capability="PROVISION_PERSONAL_WORKSPACE",
                          scope="workspace:personal", target=f"workspace:{subject}",
                          operation_id="switch:issue:ws", use_mode="ONE_SHOT")
            self.assertEqual(grant["status"], "ALLOW", grant)
            before = box.counts()
            for attempt in range(2):
                refused = box.call({"op": "provision_workspace", "grant_id": grant["grant_id"],
                                    "operation_key": f"switch:provision:{attempt}",
                                    "workspace": {"subject_id": subject}})
                self.assertEqual(refused["status"], "BLOCKED", refused)
                self.assertEqual(refused["reason"], "WORKSPACE_PROVISION_OFF")
                self.assertEqual(refused["switch"], "WORKSPACE_PROVISION")
            after = box.counts()
            self.assertEqual(after, before, "a switch-off refusal changed canonical data")
            self.assertEqual(after["workspaces"], 0)
        finally:
            box.cleanup()

    def test_workspace_provision_on_and_duplicate_provision_creates_no_second_workspace(self) -> None:
        switches = FeatureSwitches({"WORKSPACE_PROVISION": ON})
        box = Sandbox(root=Path(tempfile.mkdtemp(prefix="c10svc_")), switches=switches)
        try:
            subject = "test:duplicate"
            grant = issue(box, subject=subject, capability="PROVISION_PERSONAL_WORKSPACE",
                          scope="workspace:personal", target=f"workspace:{subject}",
                          operation_id="duplicate:issue:ws")
            first = box.call({"op": "provision_workspace", "grant_id": grant["grant_id"],
                              "operation_key": "duplicate:provision:1",
                              "workspace": {"subject_id": subject}})
            self.assertEqual(first["status"], "OK", first)
            workspace_id = first["workspace"]["workspace_id"]
            self.assertEqual(box.counts()["workspaces"], 1)

            replay = box.call({"op": "provision_workspace", "grant_id": grant["grant_id"],
                               "operation_key": "duplicate:provision:1",
                               "workspace": {"subject_id": subject}})
            # One coordinated commit per (command, operation id): the same operation key cannot
            # commit twice, and a *different* key cannot re-create an existing workspace.
            self.assertEqual(replay["status"], "BLOCKED", replay)
            self.assertEqual(replay["reason"], "OPERATION_ALREADY_COMMITTED")
            self.assertEqual(box.counts()["workspaces"], 1)
            self.assertEqual(box.call({"op": "get_workspace",
                                       "subject": subject})["workspace"]["workspace_id"],
                             workspace_id)

            again = box.call({"op": "provision_workspace", "grant_id": grant["grant_id"],
                              "operation_key": "duplicate:provision:2",
                              "workspace": {"subject_id": subject}})
            self.assertNotEqual(again["status"], "OK", again)
            self.assertEqual(box.counts()["workspaces"], 1)
            self.assertEqual(box.call({"op": "get_workspace",
                                       "subject": subject})["workspace"]["workspace_id"],
                             workspace_id)
        finally:
            box.cleanup()

    def test_artifact_switch_off_refuses_every_artifact_write(self) -> None:
        switches = FeatureSwitches({"WORKSPACE_PROVISION": ON})
        box = Sandbox(root=Path(tempfile.mkdtemp(prefix="c10svc_")), switches=switches)
        try:
            subject = "test:artifact:off"
            grant = issue(box, subject=subject, capability="PROVISION_PERSONAL_WORKSPACE",
                          scope="workspace:personal", target=f"workspace:{subject}",
                          operation_id="artifact:off:issue:ws", use_mode="ONE_SHOT")
            provision = box.call({"op": "provision_workspace", "grant_id": grant["grant_id"],
                                  "operation_key": "artifact:off:provision",
                                  "workspace": {"subject_id": subject}})
            workspace_id = provision["workspace"]["workspace_id"]
            artifact_grant = issue(box, subject=subject, capability="COMMIT_MANAGED_ARTIFACT",
                                   scope="artifact:personal", target=f"workspace:{workspace_id}",
                                   operation_id="artifact:off:issue:art")
            attempts = [
                {"op": "create_artifact", "grant_id": artifact_grant["grant_id"],
                 "operation_key": "artifact:off:create",
                 "artifact": {"subject_id": subject, "workspace_id": workspace_id, "title": "x"}},
                {"op": "commit_markdown", "grant_id": artifact_grant["grant_id"],
                 "operation_key": "artifact:off:commit",
                 "artifact": {"subject_id": subject, "artifact_id": "artifact:none",
                              "expected_head": None, "content": "x"}},
                {"op": "archive_artifact", "grant_id": artifact_grant["grant_id"],
                 "operation_key": "artifact:off:archive",
                 "artifact": {"subject_id": subject, "artifact_id": "artifact:none",
                              "expected_revision": 1}},
                {"op": "tombstone", "grant_id": artifact_grant["grant_id"],
                 "operation_key": "artifact:off:tombstone",
                 "artifact": {"subject_id": subject, "artifact_id": "artifact:none",
                              "expected_revision": 1}},
            ]
            for attempt in attempts:
                refused = box.call(attempt)
                self.assertEqual(refused["status"], "BLOCKED", (attempt["op"], refused))
                self.assertEqual(refused["reason"], "ARTIFACT_EXTERNAL_ACTION_OFF",
                                 attempt["op"])
            self.assertEqual(box.counts()["managed_artifacts"], 0)
            self.assertEqual(box.counts()["artifact_versions"], 0)
        finally:
            box.cleanup()

    def test_inquiry_admission_switch_codes(self) -> None:
        switches = FeatureSwitches({"WORKSPACE_PROVISION": ON})
        box = Sandbox(root=Path(tempfile.mkdtemp(prefix="c10svc_")), switches=switches)
        try:
            subject = "test:inquiry"
            grant = issue(box, subject=subject, capability="PROVISION_PERSONAL_WORKSPACE",
                          scope="workspace:personal", target=f"workspace:{subject}",
                          operation_id="inquiry:issue:ws", use_mode="ONE_SHOT")
            provision = box.call({"op": "provision_workspace", "grant_id": grant["grant_id"],
                                  "operation_key": "inquiry:provision",
                                  "workspace": {"subject_id": subject}})
            workspace_id = provision["workspace"]["workspace_id"]
            inquiry = {"subject_id": subject, "workspace_id": workspace_id,
                       "topic": "an open question", "source_refs": []}
            refused = box.call({"op": "admit_inquiry", "grant_id": grant["grant_id"],
                                "operation_key": "inquiry:admit:off", "inquiry": inquiry})
            self.assertEqual(refused["status"], "BLOCKED", refused)
            self.assertEqual(refused["reason"], "INQUIRY_ADMISSION_OFF")
        finally:
            box.cleanup()

        switched_on = FeatureSwitches({"WORKSPACE_PROVISION": ON, "INQUIRY_ADMISSION": ON})
        box = Sandbox(root=Path(tempfile.mkdtemp(prefix="c10svc_")), switches=switched_on)
        try:
            subject = "test:inquiry:on"
            grant = issue(box, subject=subject, capability="PROVISION_PERSONAL_WORKSPACE",
                          scope="workspace:personal", target=f"workspace:{subject}",
                          operation_id="inquiry:on:issue:ws", use_mode="ONE_SHOT")
            provision = box.call({"op": "provision_workspace", "grant_id": grant["grant_id"],
                                  "operation_key": "inquiry:on:provision",
                                  "workspace": {"subject_id": subject}})
            workspace_id = provision["workspace"]["workspace_id"]
            inquiry_grant = issue(box, subject=subject, capability="WRITE_OPEN_INQUIRY",
                                  scope="inquiry:personal", target=f"workspace:{workspace_id}",
                                  operation_id="inquiry:on:issue:inq")
            self.assertEqual(inquiry_grant["status"], "ALLOW", inquiry_grant)
            journal_before = box.counts()["life_supply_uow_journal"]
            answer = box.call({"op": "admit_inquiry", "grant_id": inquiry_grant["grant_id"],
                               "operation_key": "inquiry:on:admit",
                               "inquiry": {"subject_id": subject, "workspace_id": workspace_id,
                                           "topic": "an open question", "source_refs": []}})
            # The switch is ON, so the refusal is the owner's own fail-closed stub, never the
            # switch's reason code, and the refused write leaves no journal row behind.
            self.assertEqual(answer["status"], "BLOCKED", answer)
            self.assertEqual(answer["reason"], "INQUIRY_ADMISSION_OWNER_STUB", answer)
            self.assertEqual(box.counts()["life_supply_uow_journal"], journal_before)
        finally:
            box.cleanup()

    def test_switching_off_never_deletes_canonical_data(self) -> None:
        root = Path(tempfile.mkdtemp(prefix="c10svc_"))
        subject = "test:keep"
        first = Sandbox(root=root, switches=FeatureSwitches.all_on(), writer_instance="on")
        try:
            grant = issue(first, subject=subject, capability="PROVISION_PERSONAL_WORKSPACE",
                          scope="workspace:personal", target=f"workspace:{subject}",
                          operation_id="keep:issue:ws", use_mode="ONE_SHOT")
            provision = first.call({"op": "provision_workspace", "grant_id": grant["grant_id"],
                                    "operation_key": "keep:provision",
                                    "workspace": {"subject_id": subject}})
            workspace_id = provision["workspace"]["workspace_id"]
            artifact_grant = issue(first, subject=subject, capability="COMMIT_MANAGED_ARTIFACT",
                                   scope="artifact:personal", target=f"workspace:{workspace_id}",
                                   operation_id="keep:issue:art")
            created = first.call({"op": "create_artifact", "grant_id": artifact_grant["grant_id"],
                                  "operation_key": "keep:create",
                                  "artifact": {"subject_id": subject, "workspace_id": workspace_id,
                                               "title": "keep", "content": "# one\n"}})
            v2 = first.call({"op": "commit_markdown", "grant_id": artifact_grant["grant_id"],
                             "operation_key": "keep:v2",
                             "artifact": {"subject_id": subject,
                                          "artifact_id": created["artifact_id"],
                                          "expected_head": created["version_id"],
                                          "content": "# two\n"}})
            before_counts = first.counts()
            artifact_id, head = created["artifact_id"], v2["version_id"]
        finally:
            first.stop()

        second = Sandbox(root=root, switches=FeatureSwitches.all_off(), writer_instance="off")
        try:
            status = second.call({"op": "status"})
            self.assertEqual(status["switches"], {name: OFF for name in SWITCH_NAMES})
            self.assertEqual(status["service_state"], "READY", status)
            self.assertEqual(second.counts(), before_counts)
            self.assertEqual(second.call({"op": "get_workspace",
                                          "subject": subject})["workspace"]["workspace_id"],
                             workspace_id)
            head_row = second.call({"op": "read_head", "artifact_id": artifact_id})["head"]
            self.assertEqual(head_row["version_id"], head)
            self.assertEqual(head_row["content"], "# two\n")
            self.assertEqual(len(second.call({"op": "list_artifacts",
                                              "subject": subject})["artifacts"]), 1)
            refused = second.call({"op": "provision_workspace", "grant_id": "g",
                                   "operation_key": "keep:after:off",
                                   "workspace": {"subject_id": subject}})
            self.assertEqual(refused["reason"], "WORKSPACE_PROVISION_OFF")
            self.assertEqual(second.counts(), before_counts)
        finally:
            second.cleanup()
            shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
