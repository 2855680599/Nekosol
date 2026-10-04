"""PHASE 18: the plain management CLI, exercised as a real subprocess over the real socket.

``health``, ``status``, ``workspace show``, ``artifacts list``, ``grants list``,
``operations unresolved`` and ``read-only`` all talk to the local Unix socket; there is no web
admin UI and no write command in the CLI.
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

from lifesupply.service.life_supply import LifeSupplyService, LifeSupplySocketServer
from lifesupply.service.switches import FeatureSwitches

REPO_ROOT = Path(__file__).resolve().parents[2]
TIMEOUT = 6.0
SUBCOMMANDS = ("health", "status", "workspace", "artifacts", "grants", "operations", "read-only",
               "serve")


class Sandbox:
    def __init__(self, *, root: Path | None = None, read_only: bool = False,
                 switches: FeatureSwitches | None = None, writer_instance: str = "cli") -> None:
        self.uid = os.geteuid()
        self.root = Path(root) if root is not None else Path(tempfile.mkdtemp(prefix="c10svc_"))
        self.owns_root = root is None
        self.socket_path = self.root / "life-supply.sock"
        self.data_root = self.root / "data"
        self.service = LifeSupplyService(self.data_root, operator_uids={self.uid},
                                         service_uids={self.uid},
                                         switches=switches if switches is not None
                                         else FeatureSwitches.all_on(),
                                         read_only=read_only,
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


def run_cli(socket_path: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-m", "lifesupply.service.cli",
                           "--socket", socket_path, *args],
                          cwd=REPO_ROOT, capture_output=True, text=True, timeout=60)


def seed(box: Sandbox, subject: str) -> dict:
    grant = box.call({"op": "issue_grant", "grant": {
        "subject": subject, "capability": "PROVISION_PERSONAL_WORKSPACE",
        "scope": "workspace:personal", "target": f"workspace:{subject}",
        "use_mode": "ONE_SHOT", "operation_id": f"cli:{subject}:ws"}})
    provision = box.call({"op": "provision_workspace", "grant_id": grant["grant_id"],
                          "operation_key": f"cli:{subject}:provision",
                          "workspace": {"subject_id": subject}})
    workspace_id = provision["workspace"]["workspace_id"]
    artifact_grant = box.call({"op": "issue_grant", "grant": {
        "subject": subject, "capability": "COMMIT_MANAGED_ARTIFACT", "scope": "artifact:personal",
        "target": f"workspace:{workspace_id}", "use_mode": "REUSABLE",
        "operation_id": f"cli:{subject}:art"}})
    created = box.call({"op": "create_artifact", "grant_id": artifact_grant["grant_id"],
                        "operation_key": f"cli:{subject}:create",
                        "artifact": {"subject_id": subject, "workspace_id": workspace_id,
                                     "title": "cli", "content": "# one\n"}})
    box.call({"op": "commit_markdown", "grant_id": artifact_grant["grant_id"],
              "operation_key": f"cli:{subject}:v2",
              "artifact": {"subject_id": subject, "artifact_id": created["artifact_id"],
                           "expected_head": created["version_id"], "content": "# two\n"}})
    return {"subject": subject, "workspace_id": workspace_id, "artifact_id": created["artifact_id"]}


class ManagementCliTests(unittest.TestCase):
    def test_read_subcommands_against_a_live_service(self) -> None:
        box = Sandbox()
        try:
            ids = seed(box, "test:cli")
            expectations = [
                (("health",), "status=OK", "service_state=READY"),
                (("status",), "service_state=READY", "transition="),
                (("workspace", "show", "--subject", ids["subject"]),
                 f"workspace_id={ids['workspace_id']}", "lifecycle=ACTIVE"),
                (("artifacts", "list", "--subject", ids["subject"]), "artifacts=1",
                 f"id={ids['artifact_id']}"),
                (("grants", "list"), "grants=", "grant id="),
                (("grants", "list", "--subject", ids["subject"]), "grants=", "capability="),
                (("operations", "unresolved"), "unresolved=0"),
            ]
            for args, *expected in expectations:
                done = run_cli(str(box.socket_path), *args)
                self.assertEqual(done.returncode, 0, (args, done.stdout, done.stderr))
                for fragment in expected:
                    self.assertIn(fragment, done.stdout, (args, done.stdout))

            structured = run_cli(str(box.socket_path), "--json", "health")
            self.assertEqual(structured.returncode, 0, structured.stderr)
            payload = json.loads(structured.stdout)
            self.assertEqual(payload["service"], "chiyo-life-supply")
            self.assertEqual(payload["service_state"], "READY")

            read_only = run_cli(str(box.socket_path), "read-only")
            self.assertEqual(read_only.returncode, 2, read_only.stdout)
            self.assertIn("read-only: OFF", read_only.stdout)
            self.assertIn("writes_permitted=true", read_only.stdout)
        finally:
            box.cleanup()

    def test_cli_has_no_write_command_and_reports_unreachable_service(self) -> None:
        helped = subprocess.run([sys.executable, "-m", "lifesupply.service.cli", "--help"],
                                cwd=REPO_ROOT, capture_output=True, text=True, timeout=60)
        self.assertEqual(helped.returncode, 0, helped.stderr)
        for name in SUBCOMMANDS:
            self.assertIn(name, helped.stdout)
        for absent in ("commit", "tombstone", "sql", "upload"):
            self.assertNotIn(f"    {absent}", helped.stdout)

        missing = Path(tempfile.mkdtemp(prefix="c10svc_")) / "absent.sock"
        try:
            done = run_cli(str(missing), "health")
            self.assertEqual(done.returncode, 1, done.stdout)
            self.assertIn("SERVICE_UNREACHABLE", done.stdout)
        finally:
            shutil.rmtree(missing.parent, ignore_errors=True)

        box = Sandbox()
        try:
            unknown = run_cli(str(box.socket_path), "commit")
            self.assertEqual(unknown.returncode, 2, unknown.stdout)
        finally:
            box.cleanup()

    def test_read_only_subcommand_matches_a_read_only_service(self) -> None:
        root = Path(tempfile.mkdtemp(prefix="c10svc_"))
        writer = Sandbox(root=root)
        try:
            ids = seed(writer, "test:cli:readonly")
        finally:
            writer.stop()
        reader = Sandbox(root=root, read_only=True, writer_instance="cli-reader")
        try:
            done = run_cli(str(reader.socket_path), "read-only")
            self.assertEqual(done.returncode, 0, (done.stdout, done.stderr))
            self.assertIn("read-only: ON", done.stdout)
            self.assertIn("writes_permitted=false", done.stdout)
            status = run_cli(str(reader.socket_path), "status")
            self.assertEqual(status.returncode, 0, status.stderr)
            self.assertIn("service_state=READ_ONLY", status.stdout)
            workspace = run_cli(str(reader.socket_path), "workspace", "show",
                                "--subject", ids["subject"])
            self.assertEqual(workspace.returncode, 0, workspace.stderr)
            self.assertIn(ids["workspace_id"], workspace.stdout)
            refused = reader.call({"op": "provision_workspace", "grant_id": "g",
                                   "operation_key": "k", "workspace": {"subject_id": "s"}})
            self.assertEqual(refused["reason"], "SERVICE_READ_ONLY", refused)
        finally:
            reader.cleanup()
            shutil.rmtree(root, ignore_errors=True)

    def test_legacy_split_launcher_and_serve_entry_point_still_work(self) -> None:
        served = subprocess.run([sys.executable, "-m", "lifesupply.service.cli", "serve", "--help"],
                                cwd=REPO_ROOT, capture_output=True, text=True, timeout=60)
        self.assertEqual(served.returncode, 0, served.stderr)
        self.assertIn("--data-root", served.stdout)

        env = {key: value for key, value in os.environ.items()
               if not key.startswith(("GOVERNANCE_OPERATOR", "LIFESUPPLY_"))}
        legacy = subprocess.run(
            [sys.executable, "-m", "lifesupply.service.cli", "--mode", "supply",
             "--socket", "/tmp/c10svc_cfg/life-supply.sock",
             "--data-root", "/tmp/c10svc_cfg/data"],
            cwd=REPO_ROOT, capture_output=True, text=True, timeout=60, env=env)
        self.assertNotEqual(legacy.returncode, 0)
        self.assertIn("dedicated non-root UID", legacy.stderr + legacy.stdout)



if __name__ == "__main__":
    unittest.main()
