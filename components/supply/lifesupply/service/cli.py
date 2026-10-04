"""PHASE 18: the plain Life Supply management CLI.  Unix socket only, no web admin UI.

    life-supply health
    life-supply status
    life-supply workspace show --subject chiyo
    life-supply artifacts list --subject chiyo
    life-supply grants list [--subject chiyo]
    life-supply operations unresolved
    life-supply read-only
    life-supply serve [--socket ...] [--data-root ...] [--read-only]

Every subcommand except ``serve`` sends exactly one typed read request to the local service
socket and prints the answer; ``--json`` prints the raw response instead of key=value lines.
Exit codes: 0 = OK/KNOWN, 1 = refused or unknown, 2 = ``read-only`` when the service is not
read-only.

The C6 two-process launcher is kept verbatim as ``_legacy_split_service`` and is selected by
``--mode`` so the deployments that still run the split service are unaffected.
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import sys
from typing import Any

from lifesupply.service.life_supply import DEFAULT_SOCKET_PATH

DEFAULT_TIMEOUT = 5.0
MAX_RESPONSE = 1_000_001
OK_STATUSES = frozenset({"OK", "KNOWN"})


class CliError(RuntimeError):
    pass


def call(socket_path: str, request: dict[str, Any], *, timeout: float = DEFAULT_TIMEOUT) -> dict[str, Any]:
    """One request in, one typed response out.  Any transport problem fails closed."""
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(timeout)
            client.connect(socket_path)
            client.sendall(json.dumps(request, ensure_ascii=False,
                                      separators=(",", ":")).encode() + b"\n")
            line = client.makefile("rb").readline(MAX_RESPONSE)
    except OSError as exc:
        return {"status": "UNKNOWN", "reason": "SERVICE_UNREACHABLE", "detail": str(exc)}
    if not line:
        return {"status": "UNKNOWN", "reason": "EMPTY_RESPONSE", "detail": "service closed the socket"}
    try:
        result = json.loads(line.decode("utf-8"))
    except (UnicodeError, ValueError):
        return {"status": "UNKNOWN", "reason": "MALFORMED_RESPONSE", "detail": "not JSON"}
    if not isinstance(result, dict) or not isinstance(result.get("status"), str):
        return {"status": "UNKNOWN", "reason": "UNTYPED_RESPONSE", "detail": "no status field"}
    return result


def _render(result: dict[str, Any], lines: list[str]) -> None:
    for line in lines:
        print(line)


def _summary(result: dict[str, Any]) -> str:
    parts = [f"status={result.get('status')}"]
    for key in ("reason", "state", "detail"):
        value = result.get(key)
        if value:
            parts.append(f"{key}={value}")
    return " ".join(parts)


def _health_lines(result: dict[str, Any]) -> list[str]:
    state = result.get("state") or {}
    switches = result.get("switches") or {}
    lines = [f"status={result.get('status')}",
             f"reason={result.get('reason')}",
             f"service={result.get('service')}",
             f"service_state={result.get('service_state')}",
             f"read_only={str(bool(result.get('read_only'))).lower()}",
             f"writes_permitted={str(bool(state.get('writes_permitted'))).lower()}",
             f"canonical_db={result.get('canonical_db')}",
             f"owners={','.join(result.get('owners') or [])}",
             f"workspace_count={result.get('workspace_count')}",
             f"artifact_count={result.get('artifact_count')}",
             f"unresolved_operations={result.get('unresolved_operations')}"]
    for name in sorted(switches):
        lines.append(f"switch.{name}={switches[name]}")
    if result.get("detail"):
        lines.append(f"detail={result['detail']}")
    return lines


def _status_lines(result: dict[str, Any]) -> list[str]:
    state = result.get("state") or {}
    lines = _health_lines(result)
    lines.append(f"writer_instance={result.get('writer_instance')}")
    for owner, writer in sorted((result.get("writers") or {}).items()):
        lines.append(f"writer.{owner}={writer.get('state')} epoch={writer.get('epoch')}")
    for entry in state.get("transitions") or []:
        lines.append(f"transition={entry.get('from')}->{entry.get('to')} reason={entry.get('reason')}")
    return lines


def _workspace_lines(result: dict[str, Any]) -> list[str]:
    if result.get("status") not in OK_STATUSES:
        return [_summary(result)]
    workspace = result.get("workspace") or {}
    return [f"workspace_id={workspace.get('workspace_id')}",
            f"subject_id={workspace.get('subject_id')}",
            f"lifecycle={workspace.get('lifecycle')}",
            f"visibility={workspace.get('visibility')}",
            f"artifact_scope={workspace.get('artifact_scope')}",
            f"revision={workspace.get('revision')}",
            f"operation_id={workspace.get('operation_id')}"]


def _artifact_lines(result: dict[str, Any]) -> list[str]:
    if result.get("status") not in OK_STATUSES:
        return [_summary(result)]
    rows = result.get("artifacts") or []
    lines = [f"artifacts={len(rows)}"]
    for row in rows:
        lines.append("artifact "
                     f"id={row.get('artifact_id')} lifecycle={row.get('lifecycle')} "
                     f"revision={row.get('revision')} title={row.get('title')!r}")
    return lines


def _grant_lines(result: dict[str, Any]) -> list[str]:
    if result.get("status") not in OK_STATUSES:
        return [_summary(result)]
    rows = result.get("grants") or []
    lines = [f"grants={len(rows)}"]
    for row in rows:
        lines.append("grant "
                     f"id={row.get('grant_id')} subject={row.get('subject')} "
                     f"capability={row.get('capability')} scope={row.get('scope')} "
                     f"target={row.get('target')} state={row.get('revocation_state')} "
                     f"revision={row.get('revision')}")
    return lines


def _unresolved_lines(result: dict[str, Any]) -> list[str]:
    if result.get("status") not in OK_STATUSES:
        return [_summary(result)]
    rows = result.get("unresolved") or []
    lines = [f"unresolved={len(rows)}"]
    for row in rows:
        lines.append("operation "
                     f"owner={row.get('owner')} kind={row.get('kind')} "
                     f"state={row.get('state')} id={row.get('reservation_id') or row.get('action_key')}")
    return lines


def _read_only_lines(result: dict[str, Any]) -> list[str]:
    state = result.get("state") or {}
    value = bool(result.get("read_only"))
    return [f"read-only: {'ON' if value else 'OFF'}",
            f"service_state={result.get('service_state')}",
            f"writes_permitted={str(bool(state.get('writes_permitted'))).lower()}",
            f"status={result.get('status')}"]


# ---------------------------------------------------------------------------------
# argument parsing
# ---------------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="life-supply",
        description="Life Supply management CLI (local Unix socket only)")
    parser.add_argument("--socket", default=os.environ.get("LIFE_SUPPLY_SOCKET", DEFAULT_SOCKET_PATH),
                        help="service socket path")
    parser.add_argument("--json", action="store_true", help="print the raw response JSON")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("health")
    sub.add_parser("status")
    sub.add_parser("read-only")

    workspace = sub.add_parser("workspace")
    workspace_sub = workspace.add_subparsers(dest="subcommand", required=True)
    show = workspace_sub.add_parser("show")
    show.add_argument("--subject", required=True)

    artifacts = sub.add_parser("artifacts")
    artifacts_sub = artifacts.add_subparsers(dest="subcommand", required=True)
    artifacts_list = artifacts_sub.add_parser("list")
    artifacts_list.add_argument("--subject", required=True)

    grants = sub.add_parser("grants")
    grants_sub = grants.add_subparsers(dest="subcommand", required=True)
    grants_list = grants_sub.add_parser("list")
    grants_list.add_argument("--subject", default=None)

    operations = sub.add_parser("operations")
    operations_sub = operations.add_subparsers(dest="subcommand", required=True)
    operations_sub.add_parser("unresolved")

    serve = sub.add_parser("serve", help="start the independent service")
    serve.add_argument("--socket", dest="serve_socket", default=None)
    serve.add_argument("--data-root", dest="serve_data_root", default=None)
    serve.add_argument("--read-only", dest="serve_read_only", action="store_true")
    serve.add_argument("--operator-uid", dest="serve_operator_uid", type=int, action="append", default=[])
    serve.add_argument("--service-uid", dest="serve_service_uid", type=int, action="append", default=[])
    serve.add_argument("--writer-instance", dest="serve_writer_instance", default=None)
    serve.add_argument("--allow-root", dest="serve_allow_root", action="store_true")
    return parser


def _route(args: argparse.Namespace) -> tuple[dict[str, Any], str]:
    """Map a parsed command line onto one typed service request and a renderer name."""
    if args.command == "health":
        return {"op": "health"}, "health"
    if args.command == "status":
        return {"op": "status"}, "status"
    if args.command == "read-only":
        return {"op": "status"}, "read-only"
    if args.command == "workspace" and args.subcommand == "show":
        return {"op": "get_workspace", "subject": args.subject}, "workspace"
    if args.command == "artifacts" and args.subcommand == "list":
        return {"op": "list_artifacts", "subject": args.subject}, "artifacts"
    if args.command == "grants" and args.subcommand == "list":
        request: dict[str, Any] = {"op": "list_active_grants"}
        if args.subject:
            request["subject"] = args.subject
        return request, "grants"
    if args.command == "operations" and args.subcommand == "unresolved":
        return {"op": "list_unresolved_operations"}, "unresolved"
    raise CliError(f"unrouted command {args.command!r}")


def _render_result(name: str, result: dict[str, Any]) -> None:
    renderers = {"health": _health_lines, "status": _status_lines, "read-only": _read_only_lines,
                 "workspace": _workspace_lines, "artifacts": _artifact_lines,
                 "grants": _grant_lines, "unresolved": _unresolved_lines}
    _render(result, renderers[name](result))


def _exit_code(name: str, result: dict[str, Any]) -> int:
    if name == "read-only":
        return 0 if result.get("read_only") else 2
    return 0 if result.get("status") in OK_STATUSES else 1


def main(argv: list[str] | None = None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    if "--mode" in raw:
        return _legacy_split_service(raw)
    args = build_parser().parse_args(raw)
    if args.command == "serve":
        from lifesupply.service import server as server_module
        serve_argv: list[str] = []
        if args.serve_socket:
            serve_argv += ["--socket", args.serve_socket]
        if args.serve_data_root:
            serve_argv += ["--data-root", args.serve_data_root]
        if args.serve_read_only:
            serve_argv += ["--read-only"]
        for uid in args.serve_operator_uid:
            serve_argv += ["--operator-uid", str(uid)]
        for uid in args.serve_service_uid:
            serve_argv += ["--service-uid", str(uid)]
        if args.serve_writer_instance:
            serve_argv += ["--writer-instance", args.serve_writer_instance]
        if args.serve_allow_root:
            serve_argv += ["--allow-root"]
        return server_module.main(serve_argv)
    try:
        request, renderer = _route(args)
    except CliError as exc:
        print(f"status=UNKNOWN reason=CLI_ROUTE_ERROR detail={exc}", file=sys.stderr)
        return 1
    result = call(args.socket, request, timeout=args.timeout)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    else:
        _render_result(renderer, result)
    return _exit_code(renderer, result)


# ---------------------------------------------------------------------------------
# the C6 two-process launcher, kept verbatim for the split deployments
# ---------------------------------------------------------------------------------


def _legacy_split_service(argv: list[str] | None = None) -> int:
    # C10 PHASE 06: this entry point *is* the legacy opt-in, so it declares it here.  The gate
    # lives in server.run() and keeps the three-database layout from being started by accident.
    os.environ.setdefault("LIFE_SUPPLY_ALLOW_LEGACY_THREE_DB", "ON")
    from lifesupply.service.server import run


    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", required=True, choices=("governance", "supply"))
    parser.add_argument("--socket", required=True)
    parser.add_argument("--data-root", required=True)
    parser.add_argument("--governance-socket", default="/run/chiyo-governance/governance.sock")
    args = parser.parse_args(argv)
    import pwd
    raw_operator = os.environ.get("GOVERNANCE_OPERATOR_UIDS", "")
    if args.mode == "supply":
        raw_operator = os.environ.get("GOVERNANCE_OPERATOR_UIDS", "")
    if args.mode == "governance" and not raw_operator:
        operator_user = os.environ.get("GOVERNANCE_OPERATOR_USER", "")
        if not operator_user:
            raise SystemExit("GOVERNANCE_OPERATOR_UIDS or GOVERNANCE_OPERATOR_USER must be configured")
        try:
            raw_operator = str(pwd.getpwnam(operator_user).pw_uid)
        except KeyError as exc:
            raise SystemExit("configured Governance operator account is absent") from exc
    operator_uids = {int(v) for v in raw_operator.split(",") if v}
    if args.mode == "supply" and not operator_uids:
        operator_user = os.environ.get("GOVERNANCE_OPERATOR_USER", "")
        if operator_user:
            try:
                operator_uids = {pwd.getpwnam(operator_user).pw_uid}
            except KeyError as exc:
                raise SystemExit("configured Governance operator account is absent") from exc
    if args.mode == "supply" and (not operator_uids or 0 in operator_uids):
        raise SystemExit("Governance operator must be a dedicated non-root UID")
    if args.mode == "governance" and (not operator_uids or 0 in operator_uids):
        raise SystemExit("Governance operator must be a dedicated non-root UID")
    raw_supply_uids = os.environ.get("LIFESUPPLY_SERVICE_UIDS", "")
    supply_uids = {int(v) for v in raw_supply_uids.split(",") if v} if raw_supply_uids else set()
    if args.mode == "governance" and not supply_uids:
        service_user = os.environ.get("LIFESUPPLY_SERVICE_USER", "chiyo-life-supply")
        try:
            supply_uids = {pwd.getpwnam(service_user).pw_uid}
        except KeyError as exc:
            raise SystemExit("Life Supply service account is absent") from exc
    if args.mode == "governance" and (not supply_uids or 0 in supply_uids):
        raise SystemExit("Life Supply service peer must be dedicated and non-root")
    raw_control = os.environ.get("LIFESUPPLY_CONTROL_UIDS", "")
    if args.mode == "supply" and not raw_control:
        control_user = os.environ.get("LIFESUPPLY_CONTROL_USER", "")
        if not control_user:
            raise SystemExit("LIFESUPPLY_CONTROL_UIDS or LIFESUPPLY_CONTROL_USER must be configured")
        try:
            raw_control = str(pwd.getpwnam(control_user).pw_uid)
        except KeyError as exc:
            raise SystemExit("configured Life Supply control account is absent") from exc
    control_uids = {int(v) for v in raw_control.split(",") if v}
    if args.mode == "supply" and (not control_uids or 0 in control_uids):
        raise SystemExit("Life Supply control peer must be dedicated and non-root")
    owner_uids = set(supply_uids)
    if args.mode == "supply" and not owner_uids:
        raise SystemExit("LIFESUPPLY_SERVICE_UIDS must be configured")
    service_user = os.environ.get("LIFESUPPLY_SERVICE_USER", "")
    if args.mode == "supply" and not service_user:
        raise SystemExit("LIFESUPPLY_SERVICE_USER must identify service principal")
    try:
        owner_uid = pwd.getpwnam(service_user).pw_uid if service_user else None
    except KeyError as exc:
        raise SystemExit("configured Life Supply service account is absent") from exc
    read_only = os.environ.get("LIFESUPPLY_READ_ONLY", "OFF") == "ON"
    instance = os.environ.get("LIFESUPPLY_WRITER_INSTANCE")
    principal = os.environ.get("LIFESUPPLY_SERVICE_PRINCIPAL", "service:chiyo-life-supply")
    run(args.mode, args.socket, args.data_root, args.governance_socket,
        operator_uids=operator_uids, supply_uids=supply_uids,
        control_uids=control_uids, owner_uid=owner_uid, owner_uids=owner_uids,
        writer_instance=instance, read_only=read_only, service_principal=principal,
        require_non_root_identity=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
