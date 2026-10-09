"""Helper for ``tests/test_cli_process_cleanup.py``: one plugin-owned Instance in a
child process, exiting the way the CLI does.

Not a test module (the suite discovers ``test_*.py``). It builds the profile the way
the installer does (``<HERMES_HOME>/chiyo/config.json``), gets the instance through
``chiyo_bundle.hermes_plugin.services()`` -- the same entry the plugin uses -- runs one
offline turn so the M0 writer worker really starts, prints the worker's real pid and
socket path as one JSON line, and then takes the exit path named by argv[1]:

    oneshot          Hermes' one-shot exit (cleanup table, then os._exit)
    oneshot_no_table the same, with the new table entry removed (pre-fix world)
    atexit           a normal interpreter exit, i.e. the atexit chain
    hold             stay alive like a long-running gateway until stdin says stop
    kill             stay alive until the parent kills us
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "vendor/hermes"),
                str(ROOT / "components/native"), str(ROOT / "components/native/src")]

# A hang in a child would otherwise leave the parent waiting on a pipe: dump the
# stack to stderr so the failure report says where it stopped.
import faulthandler  # noqa: E402
faulthandler.dump_traceback_later(float(os.environ.get("CLI_EXIT_STACK_AFTER_S", "30")), exit=False)


def _profile(home: Path) -> None:
    (home / "chiyo").mkdir(parents=True, exist_ok=True)
    (home / "chiyo/config.json").write_text(json.dumps({
        "owner": "cli-exit-owner", "cli_owner": True, "memory": True, "life": False,
        "cognition_shadow": False, "gateway_bindings": {}, "world_body_socket": None,
        "life_supply_socket": None, "life_supply_subject": None,
    }), encoding="utf8")


def _one_turn(instance) -> None:
    reply = SimpleNamespace(content="cli exit reply", usage={}, finish_reason="stop")
    with patch("chiyo_bundle.host.HermesCompletionProvider.complete",
               side_effect=lambda messages: reply):
        instance.chat("cli exit turn", request_id="cli-exit-1")
    instance.confirm_visible("cli-exit-1")


def main() -> int:
    mode = sys.argv[1]
    home = Path(os.environ["HERMES_HOME"])
    _profile(home)

    from chiyo_bundle import hermes_plugin
    instance = hermes_plugin.services()
    _one_turn(instance)

    import m0_writer_worker as mww
    m0_db = instance.state / "memory" / "evidence.sqlite"
    print(json.dumps({"mode": mode, "pid": os.getpid(),
                      "workers": [entry["pid"] for entry in mww.status_all()],
                      "socket": str(mww.socket_path_for(m0_db)),
                      "m0_db": str(m0_db)}), flush=True)

    if mode == "hold":
        sys.stdin.readline()          # the parent releases us
        hermes_plugin.close_services()
        print(json.dumps({"mode": mode, "workers_after": [e["pid"] for e in mww.status_all()],
                          "socket_exists": Path(mww.socket_path_for(m0_db)).exists()}), flush=True)
        return 0
    if mode == "close_thrice":
        for _ in range(3):
            hermes_plugin.close_services()   # idempotent: silent and change nothing
        print(json.dumps({"mode": mode, "workers_after": [e["pid"] for e in mww.status_all()],
                          "socket_exists": Path(mww.socket_path_for(m0_db)).exists()}), flush=True)
        return 0
    if mode == "kill":
        while True:
            import time
            time.sleep(1)
    if mode == "atexit":
        return 0
    if mode in ("oneshot", "oneshot_no_table"):
        from hermes_cli import main as hermes_main
        if mode == "oneshot_no_table":
            hermes_main._ONESHOT_CLEANUPS = tuple(
                entry for entry in hermes_main._ONESHOT_CLEANUPS
                if not (entry[0] == "chiyo_bundle.hermes_plugin" and entry[1] == "close_services"))
        hermes_main._cleanup_oneshot_runtime()
        if mode == "oneshot":
            # What the one-shot cleanup did, reported from inside the process just
            # before the hard exit (which flushes stdout first). This is the
            # deterministic reading of the mechanism; the parent also checks the
            # end-to-end symptom (worker and socket gone) afterwards.
            print(json.dumps({"mode": mode, "instance_open": hermes_plugin._instance is not None,
                              "workers_after": [e["pid"] for e in mww.status_all()],
                              "socket_exists": Path(mww.socket_path_for(m0_db)).exists()}), flush=True)
        if mode == "oneshot_no_table":
            # What the cleanup table did for us, before any exit path runs: the
            # pre-fix world had no entry here, so the instance stayed open. Reported
            # rather than asserted so the parent owns the judgement; this child then
            # exits normally (atexit still closes it) and leaves nothing behind.
            print(json.dumps({"mode": mode, "instance_open": hermes_plugin._instance is not None,
                              "workers_after": [e["pid"] for e in mww.status_all()]}), flush=True)
            return 0
        hermes_main._exit_after_oneshot(0)
    raise SystemExit("unknown mode: " + mode)


if __name__ == "__main__":
    raise SystemExit(main())
