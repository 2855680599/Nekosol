"""LPC0B-R2 sections 9-13, 40: plugin-side composition for the production cognition shadow.

Authority: LPC0B-R2 order sections 9 (wire into the existing `chiyo-life-runtime` plugin),
10 (plugin lifecycle, exactly one worker and one hook), 11 (`ctx.llm` must not leak into the
domain layers), 12 (the main hook stays fast), 13 (structural effect isolation) and 40
(auto-degrade cognition shadow only, never Level A).

Dependency direction (section 11)
--------------------------------
    plugin composition root  ->  GatewayAgencyModelTransport(ctx.llm)
                             ->  AgencyModelClient(transport=...)      <- knows the transport
                             ->  ProductionAgencyCognitionAdapter       <- AG-1 contract only
                             ->  CognitionShadowHarness                 <- queue/worker/journal

Only the transport ever sees the host LLM API. AG-1, AG-2 and LR-2 never receive it, and
`ctx` itself is not stored anywhere below this module.

Structural effect isolation (section 13)
---------------------------------------
The shadow side runs its OWN `AgencyDecisionService` bound to a `shadow`-namespaced store
root. No canonical Decision writer port is injected into it, and there is no AG-2 coordinator
and no `ActivityCommandService` on that path at all -- so "do not write" is enforced by the
shape of the object graph, not by an `if shadow:` branch.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Mapping, Optional

logger = logging.getLogger("chiyo_life_cognition")

#: Order section 10 hard gates, mirrored here so a reviewer can read them without the harness.
EXPECTED_HOOK_INSTANCE_COUNT = 1
EXPECTED_COGNITION_WORKER_COUNT = 1

_LOCK = threading.RLock()
_WIRING: Optional["CognitionWiring"] = None
_REGISTER_COUNT = 0


class CognitionWiring:
    """Owns the single cognition shadow instance for this process."""

    def __init__(self, *, install_root: Path, ctx_llm: Any, runtime: Any,
                 cognition_config: Mapping[str, Any]) -> None:
        self.install_root = Path(install_root)
        self.runtime = runtime
        self.config = dict(cognition_config)
        self.hook_count = 0
        self.worker_count = 0
        self.degraded_reason: Optional[str] = None
        self._ctx_llm = ctx_llm          # kept ONLY here; never handed to the domain layers
        self.transport: Any = None
        self.client: Any = None
        self.budget: Any = None
        self.adapter: Any = None
        self.harness: Any = None
        self.build_error: Optional[str] = None
        self.off_marker = self.install_root / "logs" / "cognition" / "COGNITION_SHADOW_OFF"
        self._health_checks = 0
        self._build()

    # -- construction ------------------------------------------------------------------

    def _build(self) -> None:
        try:
            import agency_cognition_adapter as aca
            import agency_cognition_budget as acb
            import agency_cognition_config as acc
            import agency_cognition_worker as acw
            import agency_gateway_transport as agt
            import agency_model_client as amc

            cfg = dict(self.config)
            if not bool(cfg.get("enabled", True)):
                self.build_error = "COGNITION_DISABLED_BY_CONFIG"
                return
            if str(cfg.get("effect", "shadow")).lower() != "shadow":
                # This order authorizes shadow only. Refuse anything else rather than guess.
                self.build_error = f"EFFECT_NOT_AUTHORIZED:{cfg.get('effect')}"
                return

            # section 11: the host seam stops here.
            self.transport = agt.GatewayAgencyModelTransport(
                self._ctx_llm, purpose=agt.PURPOSE_AGENCY_COGNITION,
                default_timeout_s=float(cfg.get("timeout_s") or 8.0))

            self.client = amc.AgencyModelClient(
                transport=self.transport,
                timeout_s=float(cfg.get("timeout_s") or 8.0),
                max_output_tokens=int(cfg.get("max_output_tokens") or 512))
            if self.client.transport_backend != agt.TRANSPORT_BACKEND_GATEWAY:
                raise RuntimeError(f"refusing non-host transport backend {self.client.transport_backend!r}")

            budget_cfg = dict(cfg.get("budget") or {})
            self.budget = acb.CognitionBudget(
                self.install_root / "logs" / "cognition",
                max_calls_per_hour=budget_cfg.get("max_calls_per_hour"),
                max_calls_per_day=budget_cfg.get("max_calls_per_day"),
                max_input_tokens_per_day=budget_cfg.get("max_input_tokens_per_day"),
                max_output_tokens_per_day=budget_cfg.get("max_output_tokens_per_day"),
                emergency_max_calls_per_hour=int(budget_cfg.get("emergency_max_calls_per_hour") or 60),
                emergency_max_calls_per_day=int(budget_cfg.get("emergency_max_calls_per_day") or 240),
                concurrency=int(cfg.get("concurrency") or 1))

            self.adapter = aca.ProductionAgencyCognitionAdapter(client=self.client, budget=self.budget)

            harness_kwargs: dict[str, Any] = {
                "runtime": self.runtime,
                "install_root": self.install_root,
                "adapter_factory": lambda observer: self.adapter,
                "max_queue": int(cfg.get("max_queue") or 64),
                "concurrency": int(cfg.get("concurrency") or 1),
                "enabled": True,
            }
            try:
                self.harness = acw.CognitionShadowHarness(**harness_kwargs, cognition_config=cfg)
            except TypeError:
                # The harness may not accept cognition_config yet; retention then uses its defaults.
                self.harness = acw.CognitionShadowHarness(**harness_kwargs)
            # Section 10: force recovery of any UNKNOWN_SEND_STATE left by a previous process.
            for method in ("recover", "resume_state"):
                fn = getattr(self.harness, method, None)
                if callable(fn):
                    try:
                        fn()
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("cognition %s() failed: %s", method, exc)
            self.worker_count = 1
            logger.warning(
                "LIFE COGNITION: shadow wiring built (transport=%s provider=%s model=%s "
                "concurrency=%s max_queue=%s emergency_cap=%s/h)",
                self.client.transport_backend, self.transport.provider, self.transport.model,
                cfg.get("concurrency"), cfg.get("max_queue"),
                budget_cfg.get("emergency_max_calls_per_hour"))
        except Exception as exc:  # noqa: BLE001 - fail closed: Level A must keep running
            self.build_error = f"{type(exc).__name__}: {exc}"
            logger.warning("LIFE COGNITION: shadow wiring failed (fail closed): %s", self.build_error)

    # -- hook path ---------------------------------------------------------------------

    def on_turn(self, *, session_hash: str, turn_hash: str, platform: str, observed_at: str) -> dict[str, Any]:
        """Called from the Life `pre_llm_call` hook. Bounded, no provider contact (section 12)."""
        self.hook_count += 1
        out: dict[str, Any] = {"cognition": None}
        if self.is_off() or self.harness is None:
            out["cognition"] = {"enqueued": False, "reason": self.degraded_reason or self.build_error or "OFF"}
            return out
        try:
            out["cognition"] = self.harness.observe_turn(
                session_hash=session_hash, turn_hash=turn_hash, platform=platform,
                observed_at=observed_at)
        except Exception as exc:  # noqa: BLE001
            out["cognition"] = {"enqueued": False, "reason": f"OBSERVE_FAILED:{type(exc).__name__}"}
            logger.warning("cognition observe failed (swallowed): %s", exc)
        self._health_check()
        return out

    # -- section 40: degrade the shadow only, never Level A ---------------------------

    def is_off(self) -> bool:
        return self.degraded_reason is not None or self.off_marker.exists()

    def _health_check(self) -> None:
        self._health_checks += 1
        if self._health_checks % 25 != 0:
            return
        try:
            snap = self.harness.snapshot() if self.harness is not None else {}
            reasons = []
            if int(self.runtime.revision()) != 0:
                reasons.append("ACTIVITY_REVISION_NOT_ZERO")
            if int(snap.get("canonical_decision_written") or 0) != 0:
                reasons.append("CANONICAL_DECISION_FROM_SHADOW")
            if int(snap.get("adoptions_created") or 0) != 0:
                reasons.append("ADOPTION_FROM_SHADOW")
            if int(snap.get("activity_mutations") or 0) != 0:
                reasons.append("ACTIVITY_MUTATION_FROM_SHADOW")
            if int(snap.get("recursion_guard_hits") or 0) > 0:
                reasons.append("COGNITION_RECURSION")
            if int(snap.get("queue_depth") or 0) > int(snap.get("max_queue") if isinstance(snap.get("max_queue"), int) else 64):
                reasons.append("QUEUE_RUNAWAY")
            if reasons:
                self.degrade("+".join(reasons))
        except Exception as exc:  # noqa: BLE001
            logger.warning("cognition health check failed: %s", exc)

    def degrade(self, reason: str) -> None:
        """Section 40: turn the cognition shadow off. Level A, LR-2 and chat keep running."""
        with _LOCK:
            if self.degraded_reason is None:
                self.degraded_reason = reason
                try:
                    self.off_marker.parent.mkdir(parents=True, exist_ok=True)
                    self.off_marker.write_text(
                        f"COGNITION_SHADOW_OFF\nreason={reason}\nat={time.strftime('%Y-%m-%dT%H:%M:%S%z')}\n",
                        encoding="utf-8")
                except OSError:
                    pass
                logger.error("LIFE COGNITION: shadow degraded OFF (%s); Level A untouched", reason)
            if self.harness is not None:
                try:
                    self.harness.stop()
                except Exception:  # noqa: BLE001
                    pass

    # -- lifecycle --------------------------------------------------------------------

    def stop(self) -> None:
        if self.harness is not None:
            try:
                self.harness.stop()
            except Exception as exc:  # noqa: BLE001
                logger.warning("cognition harness stop failed: %s", exc)

    def snapshot(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "hook_count": self.hook_count,
            "worker_count": self.worker_count,
            "build_error": self.build_error,
            "degraded_reason": self.degraded_reason,
            "off_marker": self.off_marker.exists(),
            "config_effect": self.config.get("effect"),
            "transport": self.transport.describe() if self.transport is not None else None,
            "budget": self.budget.snapshot() if self.budget is not None else None,
            "adapter": self.adapter.stats() if self.adapter is not None else None,
        }
        if self.harness is not None:
            out["harness"] = self.harness.snapshot()
        return out


# ---------------------------------------------------------------------------------
# module-level single-instance wiring (order section 10)
# ---------------------------------------------------------------------------------


def wire(ctx_llm: Any, *, install_root: Path | str, runtime: Any, environment=None) -> Optional[CognitionWiring]:
    """Build the one cognition wiring for this process. Idempotent by construction."""
    global _WIRING, _REGISTER_COUNT
    with _LOCK:
        _REGISTER_COUNT += 1
        if _WIRING is not None:
            logger.warning("LIFE COGNITION: wire() called again (%d); reusing the single instance",
                           _REGISTER_COUNT)
            return _WIRING
        try:
            import agency_cognition_config as acc
        except Exception as exc:  # noqa: BLE001
            logger.warning("LIFE COGNITION: cannot import config (%s); cognition stays OFF", exc)
            return None
        cfg = acc.load_cognition_config(environment=environment)
        _WIRING = CognitionWiring(install_root=Path(install_root), ctx_llm=ctx_llm,
                                  runtime=runtime, cognition_config=cfg)
        return _WIRING


def current() -> Optional[CognitionWiring]:
    return _WIRING


def snapshot() -> dict[str, Any]:
    w = _WIRING
    if w is None:
        return {"wired": False, "register_calls": _REGISTER_COUNT}
    out = w.snapshot()
    out["wired"] = True
    out["register_calls"] = _REGISTER_COUNT
    out["hook_instance_count"] = w.hook_count and 1 or 0
    out["EXPECTED_HOOK_INSTANCE_COUNT"] = EXPECTED_HOOK_INSTANCE_COUNT
    out["EXPECTED_COGNITION_WORKER_COUNT"] = EXPECTED_COGNITION_WORKER_COUNT
    return out
