"""PHASE 11-15: the independent service over a *real* Unix socket in a /tmp/c10svc_* sandbox.

Every test starts the service (one process, three owners, one socket) on a sandbox root, talks
to it over ``AF_UNIX``, and asserts the shipped security and transport properties: owner-only
socket, peer-credential identity, typed requests, payload limit, no TCP, no foreign modules.
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import types
import unittest
from pathlib import Path
from unittest import mock

from lifesupply.service import life_supply as service_module
from lifesupply.service.life_supply import (
    DEFAULT_SOCKET_MODE,
    DEFAULT_SOCKET_PATH,
    FORBIDDEN_MODULES,
    HOSTED_OWNER_MODULES,
    ForbiddenModuleImported,
    LifeSupplyService,
    LifeSupplySocketServer,
    ServiceConfigRefused,
    ServiceSettings,
    imported_owner_modules,
    validate_request,
)
from lifesupply.service.switches import FeatureSwitches

REPO_ROOT = Path(__file__).resolve().parents[2]
TIMEOUT = 6.0
OTHER_UID = 65534


def probe_child(uid: int, socket_path: str, payload: bytes) -> str:
    """Fork, drop to ``uid``, send one request, and report exactly what happened."""
    read_fd, write_fd = os.pipe()
    pid = os.fork()
    if pid == 0:  # pragma: no cover - child process
        outcome = ""
        try:
            os.close(read_fd)
            os.setgroups([])
            os.setgid(uid)
            os.setuid(uid)
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.settimeout(5)
                client.connect(socket_path)
                client.sendall(payload + b"\n")
                outcome = client.makefile("rb").readline(4096).decode("utf-8").strip()
        except OSError as exc:
            outcome = f"OSERROR:{exc.errno}"
        except Exception as exc:  # noqa: BLE001 - the parent only ever sees the string
            outcome = f"ERROR:{type(exc).__name__}"
        finally:
            try:
                os.write(write_fd, outcome.encode("utf-8")[:4000])
            except OSError:
                pass
            os._exit(0)
    os.close(write_fd)
    with os.fdopen(read_fd, "rb") as stream:
        return stream.read().decode("utf-8")


class Sandbox:
    """A real service on a real Unix socket, rooted at /tmp/c10svc_*."""

    def __init__(self, *, root: Path | None = None, switches: FeatureSwitches | None = None,
                 max_payload_bytes: int = 1_000_000, read_only: bool = False,
                 operator_uids: set[int] | None = None, service_uids: set[int] | None = None,
                 socket_mode: int = DEFAULT_SOCKET_MODE, writer_instance: str = "sandbox") -> None:
        self.uid = os.geteuid()
        self.root = Path(root) if root is not None else Path(tempfile.mkdtemp(prefix="c10svc_"))
        self.owns_root = root is None
        self.socket_path = self.root / "life-supply.sock"
        self.data_root = self.root / "data"
        self.service = LifeSupplyService(
            self.data_root,
            operator_uids={self.uid} if operator_uids is None else operator_uids,
            service_uids={self.uid} if service_uids is None else service_uids,
            switches=switches if switches is not None else FeatureSwitches.all_on(),
            read_only=read_only, writer_instance=f"{writer_instance}:{os.getpid()}",
            max_payload_bytes=max_payload_bytes)
        self.server = LifeSupplySocketServer(str(self.socket_path), self.service,
                                             max_payload_bytes=max_payload_bytes,
                                             socket_mode=socket_mode)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def call(self, request=None, *, raw: bytes | None = None) -> dict:
        payload = raw if raw is not None else json.dumps(request, ensure_ascii=False).encode("utf-8")
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(TIMEOUT)
            client.connect(str(self.socket_path))
            client.sendall(payload + b"\n")
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


class ServiceSocketTests(unittest.TestCase):
    def _issue(self, box: Sandbox, *, subject: str, capability: str, scope: str, target: str,
               operation_id: str, use_mode: str = "REUSABLE") -> dict:
        return box.call({"op": "issue_grant", "grant": {
            "subject": subject, "capability": capability, "scope": scope, "target": target,
            "use_mode": use_mode, "operation_id": operation_id}})

    # -- 1: the happy path, end to end, over the socket ------------------------

    def test_happy_path_over_the_real_socket(self) -> None:
        box = Sandbox()
        try:
            subject = "test:chiyo"
            health = box.call({"op": "health"})
            self.assertEqual(health["status"], "OK", health)
            self.assertEqual(health["service_state"], "READY")
            self.assertEqual(sorted(health["owners"]), ["Governance", "ManagedArtifact", "Workspace"])

            workspace_grant = self._issue(
                box, subject=subject, capability="PROVISION_PERSONAL_WORKSPACE",
                scope="workspace:personal", target=f"workspace:{subject}",
                operation_id="test:issue:workspace", use_mode="ONE_SHOT")
            self.assertEqual(workspace_grant["status"], "ALLOW", workspace_grant)

            provision = box.call({"op": "provision_workspace",
                                  "grant_id": workspace_grant["grant_id"],
                                  "operation_key": "test:provision:1",
                                  "workspace": {"subject_id": subject}})
            self.assertEqual(provision["status"], "OK", provision)
            workspace_id = provision["workspace"]["workspace_id"]
            self.assertEqual(provision["workspace"]["lifecycle"], "ACTIVE")

            artifact_grant = self._issue(
                box, subject=subject, capability="COMMIT_MANAGED_ARTIFACT",
                scope="artifact:personal", target=f"workspace:{workspace_id}",
                operation_id="test:issue:artifact")
            self.assertEqual(artifact_grant["status"], "ALLOW", artifact_grant)

            created = box.call({"op": "create_artifact",
                                "grant_id": artifact_grant["grant_id"],
                                "operation_key": "test:create:1",
                                "artifact": {"subject_id": subject, "workspace_id": workspace_id,
                                             "title": "TEST only", "content": "# v1\n"}})
            self.assertEqual(created["status"], "OK", created)
            artifact_id = created["artifact_id"]
            v1 = created["version_id"]

            read_v1 = box.call({"op": "read_artifact", "subject": subject, "artifact_id": artifact_id})
            self.assertEqual(read_v1["content"], "# v1\n", read_v1)
            self.assertEqual(read_v1["head"]["version_id"], v1)

            v2 = box.call({"op": "commit_markdown", "grant_id": artifact_grant["grant_id"],
                           "operation_key": "test:commit:2",
                           "artifact": {"subject_id": subject, "artifact_id": artifact_id,
                                        "expected_head": v1, "content": "# v2\n"}})
            self.assertEqual(v2["status"], "OK", v2)
            self.assertEqual(v2["version_number"], 2)
            self.assertEqual(v2["parent_version_id"], v1)

            head_v2 = box.call({"op": "read_head", "subject": subject, "artifact_id": artifact_id})
            self.assertEqual(head_v2["head"]["version_id"], v2["version_id"])
            self.assertEqual(head_v2["head"]["content"], "# v2\n")

            revoked = box.call({"op": "revoke_grant", "grant_id": artifact_grant["grant_id"],
                                "expected_revision": 1, "operation_id": "test:revoke:1"})
            self.assertEqual(revoked["status"], "REVOKED", revoked)

            denied = box.call({"op": "commit_markdown", "grant_id": artifact_grant["grant_id"],
                               "operation_key": "test:commit:3",
                               "artifact": {"subject_id": subject, "artifact_id": artifact_id,
                                            "expected_head": v2["version_id"], "content": "# v3\n"}})
            self.assertEqual(denied["status"], "REVOKED", denied)
            self.assertEqual(denied["reason"], "GOVERNANCE_REFUSED")
            self.assertEqual(denied["stage"], "reserve_use")

            after = box.call({"op": "read_head", "subject": subject, "artifact_id": artifact_id})
            self.assertEqual(after["head"]["version_id"], v2["version_id"])
            self.assertEqual(len(box.call({"op": "list_artifacts", "subject": subject})["artifacts"]), 1)
            self.assertEqual(box.call({"op": "list_open_inquiries", "subject": subject})["inquiries"], [])
            self.assertEqual(box.call({"op": "list_personal_opportunities",
                                       "subject": subject})["opportunities"], [])
        finally:
            box.cleanup()

    # -- PHASE 15: the lifecycle commands, over the owner API ------------------

    def test_archive_and_tombstone_use_the_owner_api(self) -> None:
        box = Sandbox()
        try:
            subject = "test:lifecycle"
            workspace_grant = self._issue(box, subject=subject,
                                          capability="PROVISION_PERSONAL_WORKSPACE",
                                          scope="workspace:personal",
                                          target=f"workspace:{subject}",
                                          operation_id="lifecycle:issue:ws", use_mode="ONE_SHOT")
            provision = box.call({"op": "provision_workspace",
                                  "grant_id": workspace_grant["grant_id"],
                                  "operation_key": "lifecycle:provision",
                                  "workspace": {"subject_id": subject}})
            workspace_id = provision["workspace"]["workspace_id"]
            artifact_grant = self._issue(box, subject=subject,
                                         capability="COMMIT_MANAGED_ARTIFACT",
                                         scope="artifact:personal",
                                         target=f"workspace:{workspace_id}",
                                         operation_id="lifecycle:issue:art")
            created = box.call({"op": "create_artifact",
                                "grant_id": artifact_grant["grant_id"],
                                "operation_key": "lifecycle:create",
                                "artifact": {"subject_id": subject, "workspace_id": workspace_id,
                                             "title": "lifecycle", "content": "# v1\n"}})
            artifact_id = created["artifact_id"]
            revision = box.call({"op": "list_artifacts",
                                 "subject": subject})["artifacts"][0]["revision"]

            archived = box.call({"op": "archive_artifact",
                                 "grant_id": artifact_grant["grant_id"],
                                 "operation_key": "lifecycle:archive",
                                 "artifact": {"subject_id": subject, "artifact_id": artifact_id,
                                              "expected_revision": revision}})
            self.assertEqual(archived["status"], "OK", archived)
            self.assertEqual(archived["lifecycle"], "ARCHIVED")
            # Honest about the frozen boundary: there is no coordinated artifact-lifecycle
            # command in db.CROSS_OWNER_COMMANDS, and the response says so.
            self.assertFalse(archived["coordinated"])
            self.assertIn("CROSS_OWNER_COMMANDS", archived["detail"])
            self.assertEqual(box.call({"op": "read_artifact", "subject": subject,
                                       "artifact_id": artifact_id})["artifact"]["lifecycle"],
                             "ARCHIVED")

            tombstoned = box.call({"op": "tombstone", "grant_id": artifact_grant["grant_id"],
                                   "operation_key": "lifecycle:tombstone",
                                   "artifact": {"subject_id": subject, "artifact_id": artifact_id,
                                                "expected_revision": archived["revision"]}})
            self.assertEqual(tombstoned["status"], "OK", tombstoned)
            self.assertEqual(tombstoned["lifecycle"], "TOMBSTONED")
            self.assertEqual(box.call({"op": "list_artifacts", "subject": subject})["artifacts"], [])
            gone = box.call({"op": "read_artifact", "subject": subject, "artifact_id": artifact_id})
            self.assertEqual(gone["status"], "NOT_FOUND", gone)
            self.assertEqual(gone["reason"], "ARTIFACT_TOMBSTONED")

            revoked = box.call({"op": "revoke_grant", "grant_id": artifact_grant["grant_id"],
                                "expected_revision": 1, "operation_id": "lifecycle:revoke"})
            self.assertEqual(revoked["status"], "REVOKED", revoked)
            denied = box.call({"op": "archive_artifact",
                               "grant_id": artifact_grant["grant_id"],
                               "operation_key": "lifecycle:archive:after-revoke",
                               "artifact": {"subject_id": subject, "artifact_id": artifact_id,
                                            "expected_revision": archived["revision"] + 1}})
            self.assertEqual(denied["status"], "REVOKED", denied)
            self.assertEqual(denied["reason"], "GOVERNANCE_REFUSED")
        finally:
            box.cleanup()


    # -- 2: restart ------------------------------------------------------------

    def test_restart_keeps_workspace_and_artifact_head(self) -> None:
        root = Path(tempfile.mkdtemp(prefix="c10svc_"))
        subject = "test:restart"
        box = Sandbox(root=root, writer_instance="first")
        try:
            grant = self._issue(box, subject=subject, capability="PROVISION_PERSONAL_WORKSPACE",
                                scope="workspace:personal", target=f"workspace:{subject}",
                                operation_id="test:restart:issue:ws", use_mode="ONE_SHOT")
            provision = box.call({"op": "provision_workspace", "grant_id": grant["grant_id"],
                                  "operation_key": "test:restart:provision",
                                  "workspace": {"subject_id": subject}})
            workspace_id = provision["workspace"]["workspace_id"]
            artifact_grant = self._issue(box, subject=subject, capability="COMMIT_MANAGED_ARTIFACT",
                                         scope="artifact:personal", target=f"workspace:{workspace_id}",
                                         operation_id="test:restart:issue:art")
            created = box.call({"op": "create_artifact", "grant_id": artifact_grant["grant_id"],
                                "operation_key": "test:restart:create",
                                "artifact": {"subject_id": subject, "workspace_id": workspace_id,
                                             "title": "restart", "content": "# one\n"}})
            v2 = box.call({"op": "commit_markdown", "grant_id": artifact_grant["grant_id"],
                           "operation_key": "test:restart:v2",
                           "artifact": {"subject_id": subject, "artifact_id": created["artifact_id"],
                                        "expected_head": created["version_id"], "content": "# two\n"}})
            self.assertEqual(v2["status"], "OK", v2)
            artifact_id, head_v2 = created["artifact_id"], v2["version_id"]
        finally:
            box.stop()

        second = Sandbox(root=root, writer_instance="second")
        try:
            status = second.call({"op": "status"})
            self.assertEqual(status["service_state"], "READY", status)
            workspace = second.call({"op": "get_workspace", "subject": subject})
            self.assertEqual(workspace["workspace"]["workspace_id"], workspace_id)
            head = second.call({"op": "read_head", "artifact_id": artifact_id, "subject": subject})
            self.assertEqual(head["head"]["version_id"], head_v2)
            self.assertEqual(head["head"]["version_number"], 2)
            artifacts = second.call({"op": "list_artifacts", "subject": subject})["artifacts"]
            self.assertEqual([row["artifact_id"] for row in artifacts], [artifact_id])
            with second.service.db_store.connection() as con:
                epochs = {row["owner"]: int(row["active_epoch"]) for row in con.execute(
                    "SELECT owner, active_epoch FROM c6_writer_epoch").fetchall()}
            self.assertTrue(all(epoch >= 1 for epoch in epochs.values()), epochs)
        finally:
            second.cleanup()
            shutil.rmtree(root, ignore_errors=True)

    # -- 3: owner-only socket and unauthenticated callers ----------------------

    def test_socket_is_owner_only_and_foreign_callers_are_refused(self) -> None:
        box = Sandbox()
        try:
            applied = stat.S_IMODE(os.stat(box.socket_path).st_mode)
            self.assertEqual(applied, 0o600, oct(applied))
            with self.assertRaises(ServiceConfigRefused):
                ServiceSettings(socket_mode=0o666).validate()
            if os.geteuid() != 0:
                self.skipTest("dropping to another uid requires root")
            # mkdtemp is 0700: open the directory so only the *socket* mode decides.
            os.chmod(box.root, 0o755)
            refused = probe_child(OTHER_UID, str(box.socket_path), b'{"op":"health"}')
            self.assertEqual(refused, "OSERROR:13", refused)
            self.assertEqual(box.call({"op": "health"})["status"], "OK")
        finally:
            box.cleanup()

        # A deployment that ever widened the socket must still fail closed on peer credentials.
        #
        # Whether a cross-uid connection can be probed at all is a property of the HOST: on a
        # hardened host uid OTHER_UID cannot connect() even to a 0777 socket inside a 0755
        # directory (measured EACCES by an independent minimal experiment), so a connection probe
        # would be measuring the host, not this service.  A check that can never pass is a broken
        # check rather than a finding, so: probe first, and if the host refuses the connect,
        # assert the refusal through the SAME dispatch path the socket handler uses -- and make
        # the fallback visible here instead of hiding it.
        widened = Sandbox(root=Path(tempfile.mkdtemp(prefix="c10svc_")), socket_mode=0o666)
        try:
            self.assertEqual(stat.S_IMODE(os.stat(widened.socket_path).st_mode), 0o666)
            if os.geteuid() != 0:
                self.skipTest("dropping to another uid requires root")
            # mkdtemp is 0700: open the directory so only the *socket* mode decides.
            os.chmod(widened.root, 0o755)
            raw = probe_child(OTHER_UID, str(widened.socket_path), b'{"op":"health"}')
            try:
                answer = json.loads(raw)
            except ValueError:
                self.assertTrue(raw.startswith("OSERROR:"),
                                f"the child reported neither JSON nor an errno: {raw!r}")
                # the host itself refused the cross-uid connect; take the dispatch path instead
                answer = widened.service.dispatch({"op": "health"}, peer_uid=OTHER_UID)
            self.assertEqual(answer["status"], "DENY", answer)
            self.assertEqual(answer["reason"], "PEER_CREDENTIAL_REJECTED", answer)
        finally:
            widened.cleanup()

    def test_unknown_peer_and_separated_roles_are_refused(self) -> None:
        # The peer allowlists are the identity source: an unknown uid gets nothing at all.
        box = Sandbox(operator_uids=set(), service_uids=set())
        try:
            answer = box.call({"op": "health"})
            self.assertEqual(answer["status"], "DENY", answer)
            self.assertEqual(answer["reason"], "PEER_CREDENTIAL_REJECTED")
        finally:
            box.cleanup()

    # -- 4: oversized payload --------------------------------------------------

    def test_oversized_payload_is_refused(self) -> None:
        box = Sandbox(max_payload_bytes=4096)
        try:
            huge = json.dumps({"op": "health", "padding": "x" * 8192}).encode("utf-8")
            answer = box.call(raw=huge)
            self.assertEqual(answer["status"], "DENY", answer)
            self.assertEqual(answer["reason"], "PAYLOAD_TOO_LARGE")
            self.assertEqual(answer["limit"], 4096)
            self.assertEqual(box.call({"op": "health"})["status"], "OK")
            with self.assertRaises(Exception) as caught:
                validate_request({"op": "get_workspace", "subject": "y" * 100}, max_payload_bytes=32)
            self.assertEqual(caught.exception.payload["reason"], "PAYLOAD_TOO_LARGE")
        finally:
            box.cleanup()

    # -- typed validation ------------------------------------------------------

    def test_malformed_and_untyped_requests_fail_closed(self) -> None:
        box = Sandbox()
        try:
            cases = [
                (None, b"not json at all", "MALFORMED_REQUEST"),
                (["health"], None, "REQUEST_NOT_AN_OBJECT"),
                ({"op": ""}, None, "OP_REQUIRED"),
                ({"op": "does_not_exist"}, None, "UNKNOWN_OPERATION"),
                ({"op": "health", "extra": 1}, None, "UNKNOWN_REQUEST_FIELD"),
                ({"op": "read_artifact"}, None, "FIELD_REQUIRED:artifact_id"),
                ({"op": "get_workspace", "subject": 5}, None, "FIELD_TYPE:subject"),
                ({"op": "list_active_grants", "subject": True}, None, "FIELD_TYPE:subject"),
                ({"op": "health", "sql": "SELECT 1 FROM permission_grants"}, None,
                 "UNKNOWN_REQUEST_FIELD"),
                ({"op": "commit_markdown", "grant_id": "g", "operation_key": "k",
                  "artifact": {"subject_id": "s", "artifact_id": "a", "expected_head": None,
                               "content": "c", "operation": "tombstone"}}, None,
                 "UNKNOWN_REQUEST_FIELD"),
                ({"op": "provision_workspace", "grant_id": "g", "operation_key": "k",
                  "workspace": {"subject_id": "s", "sql": "DROP TABLE workspaces"}}, None,
                 "UNKNOWN_REQUEST_FIELD"),
            ]
            for request, raw, expected in cases:
                answer = box.call(request, raw=raw)
                self.assertEqual(answer["reason"], expected, (request, answer))
        finally:
            box.cleanup()

    # -- the service hosts three owners, nothing else, locally -----------------

    def test_local_only_socket_and_no_foreign_modules(self) -> None:
        box = Sandbox()
        try:
            self.assertEqual(box.server.socket.family, socket.AF_UNIX)
            self.assertIsInstance(box.server.server_address, str)
            self.assertTrue(box.server.server_address.endswith("life-supply.sock"))
        finally:
            box.cleanup()

        source = Path(service_module.__file__).read_text(encoding="utf-8")
        self.assertNotIn("AF_INET", source)
        for module_path in sorted((REPO_ROOT / "lifesupply" / "service").glob("*.py")):
            text = module_path.read_text(encoding="utf-8")
            for forbidden in FORBIDDEN_MODULES:
                self.assertNotIn(f"import {forbidden}", text, module_path.name)
                self.assertNotIn(f"from {forbidden}", text, module_path.name)

        self.assertEqual(imported_owner_modules(), sorted(HOSTED_OWNER_MODULES))
        blocked_root = Path(tempfile.mkdtemp(prefix="c10svc_"))
        try:
            with mock.patch.dict(sys.modules, {"chiyo_world_body": types.ModuleType("chiyo_world_body")}):
                with self.assertRaises(ForbiddenModuleImported):
                    LifeSupplyService(blocked_root / "data")
        finally:
            shutil.rmtree(blocked_root, ignore_errors=True)

    def test_entry_points_are_runnable_and_configurable(self) -> None:
        import tomllib
        project = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        self.assertEqual(project["project"]["scripts"]["life-supply"], "lifesupply.service.cli:main")

        helped = subprocess.run([sys.executable, "-m", "lifesupply.service.server", "--help"],
                                cwd=REPO_ROOT, capture_output=True, text=True, timeout=60)
        self.assertEqual(helped.returncode, 0, helped.stderr)
        self.assertIn("--socket", helped.stdout)

        settings = ServiceSettings.from_env({"LIFE_SUPPLY_SOCKET": "/tmp/c10svc_cfg/life-supply.sock",
                                             "LIFE_SUPPLY_DATA_ROOT": "/tmp/c10svc_cfg/data",
                                             "LIFE_SUPPLY_READ_ONLY": "ON",
                                             "LIFE_SUPPLY_OPERATOR_UIDS": "1000,1001"})
        self.assertEqual(settings.socket_path, "/tmp/c10svc_cfg/life-supply.sock")
        self.assertEqual(settings.read_only, True)
        self.assertEqual(sorted(settings.operator_uids), [1000, 1001])
        self.assertEqual(ServiceSettings().socket_path, DEFAULT_SOCKET_PATH)
        self.assertEqual(ServiceSettings().socket_mode, 0o600)

        sandbox_settings = ServiceSettings(socket_path="/tmp/c10svc_cfg/a.sock",
                                           data_root="/tmp/c10svc_cfg/data",
                                           operator_uids=frozenset({1000}),
                                           service_uids=frozenset({1001})).validate()
        self.assertFalse(sandbox_settings.read_only)
        with self.assertRaises(ServiceConfigRefused):
            ServiceSettings(socket_path="relative.sock", operator_uids=frozenset({1000}),
                            service_uids=frozenset({1001})).validate()
        with self.assertRaises(ServiceConfigRefused):
            ServiceSettings(operator_uids=frozenset({0}), service_uids=frozenset({1001})).validate()
        with self.assertRaises(ServiceConfigRefused):
            ServiceSettings(operator_uids=frozenset(), service_uids=frozenset({1001})).validate()


if __name__ == "__main__":
    unittest.main()
