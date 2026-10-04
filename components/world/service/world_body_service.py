#!/usr/bin/env python3
"""chiyo-world-body.service -- standalone World / Body production runtime (M13A).

This is *productionisation glue*.  It owns process lifecycle, authority, health,
readiness, reconciliation and shutdown.  It changes no World rule, no Resolver
semantics and no somatic mathematics -- it calls the M12-verified modules exactly
as the acceptance harness does.

Design notes worth stating out loud:

* **Fail closed.**  Every startup condition in ticket section 10 raises
  :class:`StartupRefused`.  Nothing is auto-repaired, auto-created or reset.
* **Absent is not lost.**  A body layer that never existed is bootstrapped once
  and reported as ``body_bootstrapped``; it is never reported as a lost state.
* **Idle means idle.**  With no recovery session open and no event, a tick
  writes nothing at all (ticket sections 18/19).
* **No debug mutation.**  The control surface is read-only plus ``reconcile``
  (ticket section 31).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import pathlib
import selectors
import signal
import socket
import sys
import time
from datetime import datetime, timezone
from typing import Any, Mapping, Optional

HERE = pathlib.Path(__file__).resolve().parent

SCHEMA_SERVICE = "world.body.service.v1"
KIND_SERVICE = "world_body_service_status"
SERVICE_UNIT = "chiyo-world-body.service"

CONTROL_SOCKET_NAME = "control.sock"
DEFAULT_TICK_SECONDS = 60.0
MIN_ADVANCE_SECONDS = 60.0

LOG = logging.getLogger("chiyo-world-body")


def _ensure_sys_path(runtime_root: pathlib.Path) -> pathlib.Path:
    scripts = runtime_root / "scripts"
    text = str(scripts)
    if text not in sys.path:
        sys.path.insert(0, text)
    return scripts


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat()


class StartupRefused(RuntimeError):
    """The service must not run.  Carries the ticket section 10 condition name."""

    def __init__(self, condition: str, detail: str):
        super().__init__("%s: %s" % (condition, detail))
        self.condition = condition
        self.detail = detail


#: M14A: only this uid may call the gateway surface (transport layer).
GATEWAY_ALLOWED_PEER_UID = 0  # root; the deployment runs as root


def gateway_peer_allowed(uid: int) -> bool:
    """Transport-layer admission: OS peer authorization (M14A section 7).

    Belt-and-braces with the 0600 socket mode.  Note this is *not* the
    business authority: a permitted peer still needs an Intent-Executor-issued
    authorization for any mutation.
    """

    return int(uid) == GATEWAY_ALLOWED_PEER_UID


class WorldBodyService:
    """One process, one World/Body home, one writer."""

    def __init__(
        self,
        config: Any,
        *,
        now: Optional[Any] = None,
        instance_id: Optional[str] = None,
        socket_path: Optional[pathlib.Path | str] = None,
        tick_seconds: float = DEFAULT_TICK_SECONDS,
    ):
        self.config = config
        self._now = now
        self.instance_id = instance_id
        self.tick_seconds = float(tick_seconds)
        self.socket_path = pathlib.Path(socket_path) if socket_path else (
            config.run_dir / CONTROL_SOCKET_NAME
        )

        self.scripts_dir = _ensure_sys_path(config.runtime_root)

        import body_consequence as bcon
        import body_continuity as bct
        import body_reader as bread
        import body_runtime as br
        import world_action_resolver as war
        import world_body_binding as wbb
        import world_body_substrate as wbs
        import world_execution_ledger as wel

        self.br, self.bcon, self.bct, self.bread = br, bcon, bct, bread
        self.war, self.wbb, self.wbs, self.wel = war, wbb, wbs, wel

        self.body_store = br.BodyStore(config.body_dir)
        self.consequence_ledger = bcon.ConsequenceLedger(config.body_dir / "consequences.jsonl")
        self.execution_ledger = wel.ExecutionLedger(config.ledger_path)
        # The Resolver must see the *service's* runtime identity, not whatever
        # the caller happened to pass in: the World substrate gate lives here,
        # and a caller-supplied environment must never be able to disable or
        # redirect production authority.
        self.resolver = war.WorldResolver(
            config.world_home,
            ledger_path=config.ledger_path,
            environment={**dict(config.environment), **config.owned_environment()},
        )

        self.lease = None  # set in startup()
        # The gateway talks to a separate, narrow socket (M13B).  Different
        # recipient, different surface, different permissions story.
        from world_body_gateway_api import GATEWAY_SOCKET_NAME, GatewaySurface

        self.gateway_socket_path = config.run_dir / GATEWAY_SOCKET_NAME
        self.request_timeout_seconds = 5.0
        self.gateway_surface = GatewaySurface(
            self, include_test_objects=bool(getattr(config, "include_test_objects", False))
        )
        self.ready = False
        self.started_at: Optional[str] = None
        self.shutdown_at: Optional[str] = None
        self.body_bootstrapped = False
        self.startup_report: dict[str, Any] = {}
        self.counters = {"ticks": 0, "idle_ticks": 0, "writes": 0, "reconciliations": 0}
        self.world_state: Optional[dict[str, Any]] = None
        self.body_state: Optional[dict[str, Any]] = None
        self.startup_reconciliation: dict[str, Any] = {}
        self.startup_catch_up: Optional[dict[str, Any]] = None

    # ------------------------------------------------------------------
    # time
    # ------------------------------------------------------------------
    def _moment(self) -> datetime:
        return self._now if self._now is not None else datetime.now(timezone.utc)

    # ------------------------------------------------------------------
    # startup
    # ------------------------------------------------------------------
    def startup(self) -> dict[str, Any]:
        from world_body_authority import AuthorityLease
        from world_body_config import apply_process_environment

        cfg = self.config
        steps: list[dict[str, Any]] = []

        def step(name: str, detail: str, **extra: Any) -> None:
            entry = {"step": name, "ok": True, "detail": detail}
            entry.update(extra)
            steps.append(entry)
            LOG.info("startup %s: %s", name, detail)

        # 1. environment ---------------------------------------------------
        apply_process_environment(cfg)
        step("environment", "HERMES_HOME -> %s (process-local)" % cfg.world_home)

        # 2. runtime root / home existence ---------------------------------
        if not cfg.scripts_dir.is_dir():
            raise StartupRefused("HOME_PATH_WRONG", "missing scripts dir %s" % cfg.scripts_dir)
        if self.scripts_dir != cfg.scripts_dir:
            raise StartupRefused("HOME_PATH_WRONG", "sys.path scripts dir mismatch")
        if not os.access(cfg.world_home, os.R_OK | os.W_OK):
            raise StartupRefused("HOME_PATH_WRONG", "world home not read/write: %s" % cfg.world_home)
        step("paths", "runtime=%s home=%s" % (cfg.runtime_root, cfg.world_home))

        # 3. authority (single writer, fail closed) ------------------------
        self.lease = AuthorityLease(
            cfg.run_dir,
            service_name=SERVICE_UNIT,
            instance_id=self.instance_id,
            world_home=cfg.world_home,
            now=self._moment(),
        )
        try:
            authority = self.lease.acquire()
        except Exception as error:  # AuthorityConflict and friends
            raise StartupRefused("DUPLICATE_AUTHORITY", str(error)) from error
        step(
            "authority",
            "instance=%s pid=%s" % (authority["instance_id"], authority["pid"]),
            reclaimed_from=authority.get("reclaimed_from"),
        )

        # 4. world ---------------------------------------------------------
        if not cfg.world_state_path.exists():
            raise StartupRefused(
                "WORLD_UNAVAILABLE", "no world state at %s" % cfg.world_state_path
            )
        try:
            world = json.loads(cfg.world_state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise StartupRefused("WORLD_UNAVAILABLE", "world state unreadable") from error
        schema = str(world.get("schema_version") or "")
        if not schema.startswith("world."):
            raise StartupRefused("WORLD_SCHEMA_UNKNOWN", "schema=%r" % schema)
        if not world.get("world_id"):
            raise StartupRefused("WORLD_SCHEMA_UNKNOWN", "world_id missing")
        if str(world.get("world_id")) != cfg.expected_world_id:
            raise StartupRefused(
                "WORLD_ID_MISMATCH",
                "expected %s, found %s" % (cfg.expected_world_id, world.get("world_id")),
            )
        self.world_state = world
        step("world", "world_id=%s revision=%s schema=%s"
             % (world.get("world_id"), world.get("revision"), schema))

        # 5. binding -------------------------------------------------------
        substrate = None
        try:
            store = self.wbs.SubstrateStore(self.wbs.substrate_path(cfg.world_home))
            substrate = store.load_with_migration()[0]
        except Exception as error:
            raise StartupRefused("BINDING_INVALID", "substrate unreadable: %s" % error) from error
        if substrate is not None:
            binding = self.wbs.verify_binding(substrate, world)
            if not binding.get("bound"):
                raise StartupRefused("BINDING_INVALID", "reasons=%s" % binding.get("reasons"))
        step("binding", "substrate=%s" % ("present" if substrate else "absent"))

        # 6. ledger --------------------------------------------------------
        try:
            chain = self.execution_ledger.verify()
        except Exception as error:
            raise StartupRefused("LEDGER_CORRUPT", str(error)) from error
        step("ledger", "valid=%s legacy=%s v2=%s"
             % (chain.get("valid"), chain.get("legacy_entries"), chain.get("v2_entries")))

        # 7. body ----------------------------------------------------------
        self.body_bootstrapped = False
        if self.body_store.runtime_path.exists():
            try:
                body, migrated = self.body_store.load_runtime_with_migration()
            except Exception as error:
                raise StartupRefused("BODY_SCHEMA_UNKNOWN", str(error)) from error
            if body is None:
                raise StartupRefused("BODY_SCHEMA_UNKNOWN", "runtime state vanished")
        else:
            body = self.br.initial_runtime(at=self._moment())
            self.body_store.save_runtime(body)
            self.body_bootstrapped = True
            migrated = False
        if self.body_store.load_profile() is None:
            self.body_store.save_profile(self.br.build_profile())
        self.body_state = body
        step("body", "body_id=%s version=%s bootstrapped=%s migrated=%s"
             % (body["body_id"], body["version"], self.body_bootstrapped, migrated))

        # 8. continuity / migration (fail closed on unknown schema) --------
        migration = self.bct.migrate_all(cfg.world_home)
        if not migration.get("all_ok"):
            broken = [k for k, v in migration["layers"].items() if not v.get("ok")]
            raise StartupRefused("MIGRATION_REQUIRED_UNAVAILABLE", "layers=%s" % broken)
        step("continuity", "layers=%d external_untouched=%s"
             % (len(migration["layers"]), migration.get("external_layers_untouched")))

        # 9. reconciliation ------------------------------------------------
        reconciliation = self.reconcile_now(persist=True)
        self.startup_reconciliation = reconciliation
        step("reconciliation", "in_flight=%s healed=%d unresolved=%d"
             % (reconciliation["in_flight"], len(reconciliation["healed"]),
                len(reconciliation["unresolved"])))

        # 10. bounded catch-up --------------------------------------------
        catch_up = self._advance_due(force=True)
        self.startup_catch_up = catch_up
        step("catch_up", "advanced=%s" % (catch_up or {}).get("advanced"))

        # 11. canonical activation ----------------------------------------
        mode = self.bread.resolve_mode(dict(os.environ))
        if cfg.canonical and mode != self.bread.MODE_CANONICAL:
            raise StartupRefused(
                "CANONICAL_NOT_ACTIVE", "requested canonical but reader mode=%s" % mode
            )
        step("authority_mode", "reader_mode=%s canonical=%s" % (mode, cfg.canonical))

        # 12. ready --------------------------------------------------------
        self.started_at = _iso(self._moment())
        self.ready = True
        report = {
            "kind": KIND_SERVICE,
            "schema_version": SCHEMA_SERVICE,
            "service": "chiyo-world-body.service",
            "ready": True,
            "started_at": self.started_at,
            "config": cfg.as_dict(),
            "steps": steps,
            "body_bootstrapped": self.body_bootstrapped,
            "authority": self.lease.status(),
        }
        self.startup_report = report
        return report

    # ------------------------------------------------------------------
    # work
    # ------------------------------------------------------------------
    def reconcile_now(self, *, persist: bool = False) -> dict[str, Any]:
        from world_body_reconcile import reconcile_startup

        cfg = self.config
        report = reconcile_startup(
            ledger=self.execution_ledger,
            resolver=self.resolver,
            consequence_ledger=self.consequence_ledger,
            body_store=self.body_store,
            body_id=self.body_state["body_id"],
            runtime_state=self.body_state,
            at=self._moment(),
            world_facts={"world_id": (self.world_state or {}).get("world_id"),
                         "world_revision": (self.world_state or {}).get("revision")},
            pose=None,
        )
        self.body_state = report["state"]
        if persist:
            self.body_store.save_runtime(self.body_state)
        self.counters["reconciliations"] += 1
        return report


    def _persist_consequence(self, outcome: Mapping[str, Any]) -> dict[str, Any]:
        """Persist an applied consequence: runtime **and** its transition record.

        `apply_for_execution` returns the transition; the caller owns writing it.
        Skipping this leaves every body consequence missing from the transition
        log, so the consequence ledger would reference a transition the journal
        never contained.
        """

        state = outcome["state"]
        self.body_store.save_runtime(state)
        transition = outcome.get("transition")
        if transition:
            self.body_store.append_transition(transition)
        self.counters["writes"] += 1
        return state

    def act(self, proposal: Mapping[str, Any]) -> dict[str, Any]:
        """Resolve one proposal and apply its body consequence, in-process.

        This is the only mutation path, and it is the Resolver's path.  The
        service is the single writer, so actions are submitted to it rather than
        written alongside it.
        """

        now = self._moment()
        result = self.resolver.resolve(proposal)
        payload: dict[str, Any] = {
            "execution_id": result.get("execution_id"),
            "action": result.get("action") or result.get("action_type"),
            "status": result.get("status"),
            "reason_code": result.get("reason_code"),
            "applied_to_body": False,
        }
        if result.get("status") == "applied":
            outcome = self.bcon.apply_for_execution(
                result,
                body_id=self.body_state["body_id"],
                ledger=self.consequence_ledger,
                runtime_state=self.body_state,
                at=now,
                world_facts={
                    "world_id": (self.world_state or {}).get("world_id"),
                    "world_revision": (self.world_state or {}).get("revision"),
                },
                pose=result.get("pose"),
            )
            payload["applied_to_body"] = bool(outcome["applied"])
            payload["consequence_reason"] = outcome.get("reason")
            payload["consequence_id"] = (outcome.get("consequence") or {}).get(
                "consequence_id"
            )
            if outcome["applied"]:
                self.body_state = self._persist_consequence(outcome)
        return payload

    def apply_result(self, result: Mapping[str, Any]) -> dict[str, Any]:
        """Apply one canonical execution result to the body, exactly once.

        This is the M9 integration seam: an execution layer submits a canonical
        world action result and the body derives its consequence.  In M13A the
        execution layer is not connected yet (that is M13B), so a local client
        may submit a well-formed result here.  It is *not* a way to set body
        state: the consequence mapping rejects anything that is not a canonical
        result, and `build_consequence` refuses raw somatic input.
        """

        outcome = self.bcon.apply_for_execution(
            result,
            body_id=self.body_state["body_id"],
            ledger=self.consequence_ledger,
            runtime_state=self.body_state,
            at=self._moment(),
            world_facts={
                "world_id": (self.world_state or {}).get("world_id"),
                "world_revision": (self.world_state or {}).get("revision"),
            },
            pose=result.get("pose"),
        )
        payload = {
            "execution_id": (outcome.get("consequence") or {}).get("execution_id"),
            "action": (outcome.get("consequence") or {}).get("action_type"),
            "verdict": (outcome.get("consequence") or {}).get("verdict"),
            "applied": bool(outcome["applied"]),
            "reason": outcome.get("reason"),
            "consequence_id": (outcome.get("consequence") or {}).get("consequence_id"),
        }
        if outcome["applied"]:
            self.body_state = self._persist_consequence(outcome)
        return payload
    def _recovery_open(self) -> bool:
        return (self.body_state or {}).get("recovery", {}).get("state") == "active"

    def _advance_due(self, *, force: bool = False) -> Optional[dict[str, Any]]:
        """Advance only when there is real work: an open recovery session."""

        state = self.body_state
        if state is None:
            return None
        last = state.get("last_advanced_at")
        if not last:
            return None
        try:
            last_dt = datetime.fromisoformat(str(last))
        except ValueError:
            return None
        now = self._moment()
        elapsed = (now - last_dt).total_seconds()
        if not force and not self._recovery_open():
            return None
        if not force and elapsed < MIN_ADVANCE_SECONDS:
            return None

        result = self.br.catch_up(state, last, now)
        if not result.get("advanced"):
            # Nothing actually changed.  An idle restart must not fabricate body
            # history, so memory and disk are both left exactly as they were.
            # (`advance` always refreshes last_advanced_at, which would otherwise
            # make "state differs" true on every single boot.)
            return {
                "advanced": False,
                "elapsed_seconds": round(elapsed, 3),
                "wrote": False,
                "catch_up": result.get("catch_up"),
            }

        before_state, after_state = state, result["state"]
        self.body_state = after_state
        transition = self.br.build_transition(
            before_state,
            after_state,
            at=now,
            cause_class=self.br.CAUSE_RECOVERY if self._recovery_open()
            else self.br.CAUSE_TIME_ADVANCE,
            input_summary={"catch_up": result.get("catch_up")},
        )
        self.body_store.append_transition(transition)
        self.body_store.save_runtime(after_state)
        self.counters["writes"] += 1
        return {
            "advanced": True,
            "elapsed_seconds": round(elapsed, 3),
            "wrote": True,
            "catch_up": result.get("catch_up"),
        }

    def tick(self) -> Optional[dict[str, Any]]:
        """One bounded wake.  Returns None when there was nothing to do."""

        self.counters["ticks"] += 1
        work = self._advance_due()
        if work is None:
            self.counters["idle_ticks"] += 1
        return work

    # ------------------------------------------------------------------
    # reporting
    # ------------------------------------------------------------------
    def health(self) -> dict[str, Any]:
        """Liveness only.  Says nothing about whether the runtime is usable."""

        checks = {
            "process_alive": True,
            "home_readable": os.access(self.config.world_home, os.R_OK),
            "schemas_valid": self.body_state is not None and self.world_state is not None,
            "authority_lock_held": bool(self.lease and getattr(self.lease, "held", False)),
        }
        return {
            "kind": "world_body_health",
            "status": "OK" if all(checks.values()) else "DEGRADED",
            "checks": checks,
        }

    def readiness(self) -> dict[str, Any]:
        """Usability only.  Every clause in ticket section 27 must hold."""

        try:
            chain = self.execution_ledger.chain_status()
            ledger_ok = bool(chain.get("valid"))
        except Exception:
            ledger_ok = False
        binding_ok = True
        try:
            store = self.wbs.SubstrateStore(self.wbs.substrate_path(self.config.world_home))
            substrate = store.load_with_migration()[0]
            if substrate is not None and self.world_state is not None:
                binding_ok = bool(self.wbs.verify_binding(substrate, self.world_state).get("bound"))
        except Exception:
            binding_ok = False

        checks = {
            "world_loaded": self.world_state is not None,
            "ledger_verified": ledger_ok,
            "body_loaded": self.body_state is not None,
            "binding_valid": binding_ok,
            "reconciliation_complete": bool(
                self.startup_report.get("steps") and self.ready
            ),
            "canonical_mode_active": self.config.canonical
            and self.bread.resolve_mode(dict(os.environ)) == self.bread.MODE_CANONICAL,
            "single_authority_confirmed": bool(self.lease and getattr(self.lease, "held", False)),
        }
        return {
            "kind": "world_body_readiness",
            "status": "READY" if all(checks.values()) else "NOT_READY",
            "checks": checks,
        }

    def status(self) -> dict[str, Any]:
        return {
            "kind": KIND_SERVICE,
            "schema_version": SCHEMA_SERVICE,
            "service": "chiyo-world-body.service",
            "ready": self.ready,
            "started_at": self.started_at,
            "shutdown_at": self.shutdown_at,
            "counters": dict(self.counters),
            "authority": self.lease.status() if self.lease else None,
            "config": self.config.as_dict(),
            "body_bootstrapped": self.body_bootstrapped,
        }

    def world_summary(self) -> dict[str, Any]:
        world = self.world_state or {}
        return {
            "world_id": world.get("world_id"),
            "revision": world.get("revision"),
            "schema_version": world.get("schema_version"),
            "location": world.get("location"),
        }

    def body_summary(self) -> dict[str, Any]:
        """Qualitative only: no raw somatic value ever leaves here."""

        state = self.body_state or {}
        import body_signal as bs
        import body_signal as bsi

        signal = bsi.produce_signals(state, occurred_at=self._moment()) if state else None
        return {
            "body_id": state.get("body_id"),
            "version": state.get("version"),
            "recovery": {
                "state": (state.get("recovery") or {}).get("state"),
                "mode": (state.get("recovery") or {}).get("mode"),
                "trend": (state.get("recovery") or {}).get("trend"),
            },
            "signal": signal,
            "raw_values_exposed": False,
        }

    def ledger_summary(self) -> dict[str, Any]:
        chain = self.execution_ledger.chain_status()
        return {
            "valid": chain.get("valid"),
            "anchor": chain.get("anchor"),
            "legacy_entries": chain.get("legacy_entries"),
            "v2_entries": chain.get("v2_entries"),
            "verified_v2_entries": chain.get("verified_v2_entries"),
            "total_entries": (chain.get("legacy_entries") or 0) + (chain.get("v2_entries") or 0),
            "errors": chain.get("errors"),
        }

    # ------------------------------------------------------------------
    # control surface
    # ------------------------------------------------------------------
    READ_ONLY_COMMANDS = ("status", "health", "readiness", "world", "body", "ledger", "ping")
    #: `act` is not a debug back door: it submits an ActionProposal to the same
    #: Resolver that owns every World mutation.  There is deliberately no command
    #: that writes body or world state directly.  (Ticket sections 30, 31.)
    MUTATING_COMMANDS = ("reconcile", "act", "apply_result")
    ALLOWED_COMMANDS = READ_ONLY_COMMANDS + MUTATING_COMMANDS

    def handle(self, command: str, argument: Any = None) -> dict[str, Any]:
        name = str(command or "").strip().lower()
        if name not in self.ALLOWED_COMMANDS:
            # Ticket section 31: no debug mutation, no test back door.
            return {
                "ok": False,
                "error": "unknown_or_forbidden_command",
                "command": name,
                "allowed": list(self.ALLOWED_COMMANDS),
                "mutating_commands": list(self.MUTATING_COMMANDS),
                "debug_mutation_enabled": False,
            }
        if name == "ping":
            return {"ok": True, "command": name, "pong": True}
        if name == "reconcile":
            report = self.reconcile_now(persist=True)
            return {"ok": True, "command": name, "result": {
                "candidates": report["candidate_count"],
                "healed": len(report["healed"]),
                "unresolved": len(report["unresolved"]),
                "wrote_anything": report["wrote_anything"],
            }}
        if name == "act":
            if not isinstance(argument, Mapping):
                return {"ok": False, "error": "act_requires_a_proposal_object"}
            return {"ok": True, "command": name, "result": self.act(argument)}
        if name == "apply_result":
            if not isinstance(argument, Mapping):
                return {"ok": False, "error": "apply_result_requires_a_result_object"}
            return {"ok": True, "command": name, "result": self.apply_result(argument)}
        payload = {
            "status": self.status,
            "health": self.health,
            "readiness": self.readiness,
            "world": self.world_summary,
            "body": self.body_summary,
            "ledger": self.ledger_summary,
        }[name]()
        return {"ok": True, "command": name, "result": payload}

    def _bind_socket(self, path: pathlib.Path, mode: int = 0o600) -> socket.socket:
        """Bind one unix socket, owned by the service and readable only by root."""

        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            path.unlink()
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(path))
        os.chmod(path, mode)
        server.listen(8)
        server.setblocking(False)
        return server

    def _control_socket(self) -> socket.socket:
        return self._bind_socket(self.socket_path)

    def _gateway_socket(self) -> socket.socket:
        return self._bind_socket(self.gateway_socket_path)

    def _serve_connection(
        self, connection: socket.socket, *, surface: str = "admin"
    ) -> None:
        try:
            connection.settimeout(self.request_timeout_seconds)
            data = connection.recv(65536)
            if not data:
                return
            try:
                request = json.loads(data.decode("utf-8").strip().splitlines()[0])
            except (ValueError, IndexError):
                request = {"command": data.decode("utf-8", "replace").strip()}
            if surface == "gateway":
                payload = self.gateway_surface.handle(
                    request.get("command"), request.get("argument")
                )
            else:
                payload = self.handle(request.get("command"), request.get("argument"))
        except Exception as error:  # never take the service down for a control call
            payload = {"ok": False, "error": type(error).__name__, "detail": str(error)[:400]}
        try:
            connection.sendall((json.dumps(payload, ensure_ascii=False) + "\n").encode())
        except OSError:
            pass
        finally:
            connection.close()

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------
    def shutdown(self) -> dict[str, Any]:
        """Stop accepting work, flush, release authority, exit cleanly."""

        self.ready = False
        self.shutdown_at = _iso(self._moment())
        reports: dict[str, Any] = {"shutdown_at": self.shutdown_at}
        if self.body_state is not None:
            self.body_store.save_runtime(self.body_state)
            reports["runtime_persisted"] = True
        for path in (self.socket_path, self.gateway_socket_path):
            try:
                if path.exists():
                    path.unlink()
            except OSError:
                pass
        if self.lease is not None:
            reports["authority"] = self.lease.release()
        reports["lease_released"] = True
        return reports

    def serve(self, *, max_seconds: Optional[float] = None, stop_after_ticks: Optional[int] = None) -> dict[str, Any]:
        """Event-driven loop with a bounded wake.  Never a 1s spin."""

        stopping = {"flag": False}

        def _stop(signum, _frame):
            LOG.info("signal %s received: shutting down", signum)
            stopping["flag"] = True

        previous = {}
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                previous[sig] = signal.signal(sig, _stop)
            except (ValueError, OSError):
                pass

        admin_server = self._bind_socket(self.socket_path)
        gateway_server = self._bind_socket(self.gateway_socket_path)
        selector = selectors.DefaultSelector()
        selector.register(admin_server, selectors.EVENT_READ, "admin")
        selector.register(gateway_server, selectors.EVENT_READ, "gateway")
        started = time.monotonic()
        ticks = 0
        try:
            while not stopping["flag"]:
                if max_seconds is not None and time.monotonic() - started >= max_seconds:
                    break
                for key, _events in selector.select(timeout=self.tick_seconds):
                    try:
                        connection, _ = key.fileobj.accept()
                    except OSError:
                        continue
                    if key.data == "gateway":
                        # M14A: transport peer-credential check (fail-closed).
                        # OS peer authorization != business intent
                        # authorization; both layers are enforced.
                        try:
                            import struct as _struct
                            _cr = getattr(socket, "SO_PEERCRED", None)
                            if _cr is not None:
                                _raw = connection.getsockopt(
                                    socket.SOL_SOCKET, _cr, _struct.calcsize("3i")
                                )
                                _pid, _uid, _gid = _struct.unpack("3i", _raw)
                                if not gateway_peer_allowed(_uid):
                                    LOG.warning(
                                        "gateway surface refused non-root peer uid=%s pid=%s",
                                        _uid, _pid,
                                    )
                                    try:
                                        connection.sendall(
                                            (json.dumps({
                                                "ok": False,
                                                "error": "unauthorized_peer",
                                                "detail": "gateway surface requires uid 0",
                                            }) + "\n").encode()
                                        )
                                    except OSError:
                                        pass
                                    connection.close()
                                    continue
                        except OSError:
                            pass
                    self._serve_connection(connection, surface=key.data)
                work = self.tick()
                ticks += 1
                if work:
                    LOG.info("tick work: %s", work)
                if stop_after_ticks is not None and ticks >= stop_after_ticks:
                    break
        finally:
            selector.close()
            admin_server.close()
            gateway_server.close()
            for sig, handler in previous.items():
                try:
                    signal.signal(sig, handler)
                except (ValueError, OSError):
                    pass
        return {"ticks": ticks, "counters": dict(self.counters)}


# ----------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------



def _cli(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="world_body_service")
    parser.add_argument(
        "command", choices=("run", "check", "status"), nargs="?", default="run"
    )
    parser.add_argument("--runtime-root", default=None)
    parser.add_argument("--world-home", default=None)
    parser.add_argument("--tick-seconds", type=float, default=DEFAULT_TICK_SECONDS)
    parser.add_argument("--max-seconds", type=float, default=None)
    args = parser.parse_args(argv)

    from world_body_config import ConfigError, load_config

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stderr,
    )

    try:
        config = load_config(
            runtime_root=args.runtime_root, world_home=args.world_home
        )
    except ConfigError as error:
        print(json.dumps({"ok": False, "condition": "CONFIG_INVALID", "detail": str(error)}))
        return 2

    if args.command == "status":
        # Read-only: report files and the lease without taking it.
        from world_body_authority import read_marker, lock_is_held

        payload = {
            "ok": True,
            "command": "status",
            "config": config.as_dict(),
            "authority_lock_held": lock_is_held(config.run_dir),
            "authority_marker": read_marker(config.run_dir),
            "world_state_present": config.world_state_path.exists(),
            "ledger_present": config.ledger_path.exists(),
        }
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2))
        return 0

    service = WorldBodyService(config, tick_seconds=args.tick_seconds)
    try:
        report = service.startup()
    except StartupRefused as error:
        print(json.dumps({"ok": False, "condition": error.condition, "detail": error.detail}))
        return 3

    if args.command == "check":
        service.shutdown()
        print(json.dumps({"ok": True, "command": "check", "startup": report}, ensure_ascii=False))
        return 0

    try:
        loop = service.serve(max_seconds=args.max_seconds)
    finally:
        service.shutdown()
    print(json.dumps({"ok": True, "command": "run", "loop": loop}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(_cli())
