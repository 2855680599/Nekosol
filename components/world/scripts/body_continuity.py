#!/usr/bin/env python3
"""Final continuity / restart / replay / migration seal (M11).

What this seals
---------------
Every earlier phase proved continuity *inside one layer*.  M11 proves it
**across all of them at once**, because a body is only continuous if the world,
the body, its transitions, its consequences and its observations all come back
consistent together.

    World state            (external authority -- read-only here)
    Body profile
    Body runtime
    Body transitions
    Body consequences
    Body observations
    Execution ledger

Guarantees
----------
* **restart** — reload every layer and prove nothing was silently reset;
* **replay**  — same initial state + same event sequence => same final state in
  every layer, with zero float tolerance;
* **migration** — a real upgrade path per layer; an unknown schema fails closed
  and is never silently coerced;
* **seal** — a deterministic digest over all layers, so divergence over time is
  detectable rather than assumed away.

The World layer is an *external* authority.  This module reads and hashes it and
reports it, but never migrates or rewrites it: changing World schema is not
Body's business.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Optional

SCHEMA_SEAL = "body.continuity.seal.v1"
SCHEMA_SNAPSHOT = "body.continuity.snapshot.v1"
KIND_SEAL = "body_continuity_seal"
KIND_SNAPSHOT = "body_continuity_snapshot"

SEAL_NAME = "CONTINUITY_SEAL.json"

#: Layer identity -> (relative path, owner, migratable-by-us?)
LAYERS: dict[str, dict[str, Any]] = {
    "world_state": {
        "path": ("data", "world", "world_state.json"),
        "owner": "world",
        "migratable": False,
        "note": "external authority; read and hash only",
    },
    "world_timeline": {
        "path": ("data", "world", "world_timeline.json"),
        "owner": "world",
        "migratable": False,
        "note": "external authority; read and hash only",
    },
    "body_profile": {
        "path": ("data", "body", "profile.json"),
        "owner": "embodiment",
        "migratable": True,
        "note": "BodyProfile v1",
    },
    "body_runtime": {
        "path": ("data", "body", "runtime.json"),
        "owner": "embodiment",
        "migratable": True,
        "note": "BodyRuntimeState v1",
    },
    "body_transitions": {
        "path": ("data", "body", "transitions.jsonl"),
        "owner": "embodiment",
        "migratable": True,
        "note": "append-only BodyTransition log",
    },
    "body_consequences": {
        "path": ("data", "body", "consequences.jsonl"),
        "owner": "embodiment",
        "migratable": True,
        "note": "append-only BodyConsequence ledger",
    },
    "body_observations": {
        "path": ("data", "body", "observations.jsonl"),
        "owner": "embodiment",
        "migratable": True,
        "note": "bounded BodyObservation store",
    },
    "execution_ledger": {
        "path": ("data", "life_execution.jsonl"),
        "owner": "life/execution",
        "migratable": False,
        "note": "M3 canonical execution ledger; verified via its own hash chain",
    },
}
LAYER_NAMES = tuple(sorted(LAYERS))

#: Things that must never happen silently during a restart.
NON_RESET_INVARIANTS = (
    "body_version_preserved",
    "somatic_values_preserved",
    "recovery_state_preserved",
    "profile_preserved",
    "transitions_preserved",
    "consequences_preserved",
    "observations_preserved",
    "world_revision_preserved",
    "ledger_chain_valid",
)


class ContinuityError(ValueError):
    """The continuity contract was violated."""


def _home(hermes_home: Optional[Path | str]) -> Path:
    return Path(
        hermes_home
        if hermes_home is not None
        else os.environ.get("HERMES_HOME") or (Path.home() / ".hermes")
    )


def layer_path(layer: str, hermes_home: Optional[Path | str] = None) -> Path:
    if layer not in LAYERS:
        raise ContinuityError(f"unknown layer {layer!r}; allowed: {list(LAYER_NAMES)}")
    return _home(hermes_home).joinpath(*LAYERS[layer]["path"])


def _digest(value: Any) -> str:
    payload = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _file_digest(path: Path) -> Optional[str]:
    if not path.exists():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _line_count(path: Path) -> int:
    if not path.exists():
        return 0
    return sum(1 for line in path.read_text(encoding="utf-8").splitlines() if line.strip())


def _read_json(path: Path) -> Optional[dict[str, Any]]:
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


# --------------------------------------------------------------------------
# snapshot
# --------------------------------------------------------------------------


def snapshot(hermes_home: Optional[Path | str] = None) -> dict[str, Any]:
    """Deterministic digest of every layer.  Read-only."""

    layers: dict[str, Any] = {}
    for name in LAYER_NAMES:
        path = layer_path(name, hermes_home)
        entry: dict[str, Any] = {
            "path": "/".join(LAYERS[name]["path"]),
            "owner": LAYERS[name]["owner"],
            "migratable": LAYERS[name]["migratable"],
            "present": path.exists(),
            "sha256": _file_digest(path),
            "line_count": _line_count(path),
            "byte_size": path.stat().st_size if path.exists() else None,
        }
        payload = _read_json(path)
        if payload is not None:
            entry["schema_version"] = payload.get("schema_version")
            if "version" in payload:
                entry["version"] = payload["version"]
            if "revision" in payload:
                entry["revision"] = payload["revision"]
        layers[name] = entry

    return {
        "kind": KIND_SNAPSHOT,
        "schema_version": SCHEMA_SNAPSHOT,
        "layers": layers,
        "present_layers": sorted(name for name, e in layers.items() if e["present"]),
        "absent_layers": sorted(name for name, e in layers.items() if not e["present"]),
        "digest": _digest({name: layers[name]["sha256"] for name in LAYER_NAMES}),
    }


# --------------------------------------------------------------------------
# restart
# --------------------------------------------------------------------------


def restart_verify(
    before_snapshot: Mapping[str, Any],
    *,
    hermes_home: Optional[Path | str] = None,
) -> dict[str, Any]:
    """Reload every layer and prove nothing was silently reset.

    The caller takes a snapshot, "restarts" (drops all in-memory state), and
    passes the snapshot back.  Each invariant is then checked against freshly
    loaded state -- on disk, not in memory.
    """

    import body_consequence as bcon
    import body_observation as bob
    import body_runtime as br
    import world_execution_ledger as wel

    after = snapshot(hermes_home)
    checks: dict[str, Optional[bool]] = {}
    notes: list[str] = []

    def _absent_before_and_after(layer: str) -> bool:
        return (
            not before_snapshot["layers"][layer]["present"]
            and not after["layers"][layer]["present"]
        )

    def _unchanged(layer: str) -> Optional[bool]:
        """True/False, or None when the layer never existed in this home.

        A layer that was absent before and absent after was not "preserved" --
        there was nothing to preserve.  Reporting that as a failure would be
        dishonest, so it is reported as not applicable instead.
        """

        if _absent_before_and_after(layer):
            return None
        return (
            before_snapshot["layers"][layer]["sha256"]
            == after["layers"][layer]["sha256"]
        )

    # body runtime: version + somatic + recovery must all survive
    runtime_path = layer_path("body_runtime", hermes_home)
    state = None
    if runtime_path.exists():
        try:
            state, _migrated = br.BodyStore(
                runtime_path.parent
            ).load_runtime_with_migration()
        except Exception:
            state = None

    previous_runtime = None
    if before_snapshot["layers"]["body_runtime"]["present"]:
        previous_runtime = _read_json(runtime_path)

    if _absent_before_and_after("body_runtime"):
        checks["body_version_preserved"] = None
        checks["somatic_values_preserved"] = None
        checks["recovery_state_preserved"] = None
    else:
        checks["body_version_preserved"] = bool(
            state is not None
            and previous_runtime is not None
            and state["version"] == previous_runtime.get("version")
        )
        checks["somatic_values_preserved"] = bool(
            state is not None and _unchanged("body_runtime")
        )
        checks["recovery_state_preserved"] = bool(
            state is not None and _unchanged("body_runtime")
        )
    checks["profile_preserved"] = _unchanged("body_profile")
    checks["transitions_preserved"] = _unchanged("body_transitions")
    checks["consequences_preserved"] = _unchanged("body_consequences")
    checks["observations_preserved"] = _unchanged("body_observations")
    checks["world_revision_preserved"] = _unchanged("world_state")

    # the execution ledger must still verify its own chain
    try:
        chain = wel.ExecutionLedger(layer_path("execution_ledger", hermes_home)).chain_status()
        checks["ledger_chain_valid"] = bool(chain.get("valid"))
        if not chain.get("valid"):
            notes.append("execution ledger chain did not verify after restart")
    except Exception as exc:  # pragma: no cover - defensive
        checks["ledger_chain_valid"] = False
        notes.append(f"ledger verification failed: {type(exc).__name__}")

    # a canonical body must never come back as a default/reset body
    if state is not None and previous_runtime is not None:
        if (
            previous_runtime.get("somatic", {}).get("fatigue")
            not in (None, br.SOMATIC_CONTRACTS["fatigue"]["default"])
        ) and state["somatic"]["fatigue"] == br.SOMATIC_CONTRACTS["fatigue"]["default"]:
            checks["somatic_values_preserved"] = False
            notes.append("somatic fatigue was silently reset to its default")

    applicable = [name for name in NON_RESET_INVARIANTS if checks.get(name) is not None]
    not_applicable = [name for name in NON_RESET_INVARIANTS if checks.get(name) is None]
    return {
        "kind": "body_continuity_restart_report",
        "schema_version": SCHEMA_SEAL,
        "layers_before": before_snapshot["digest"],
        "layers_after": after["digest"],
        "same_digest": before_snapshot["digest"] == after["digest"],
        "invariants": checks,
        "invariants_total": len(NON_RESET_INVARIANTS),
        "invariants_applicable": len(applicable),
        "invariants_passed": sum(1 for value in checks.values() if value),
        "not_applicable": not_applicable,
        "all_preserved": all(checks[name] for name in applicable),
        "notes": notes,
        "after": after,
    }


# --------------------------------------------------------------------------
# replay
# --------------------------------------------------------------------------


def play_sequence(
    steps: Sequence[Mapping[str, Any]],
    *,
    body_id: str = "chiyo_body",
    hermes_home: Optional[Path | str] = None,
    workdir: Optional[Path | str] = None,
) -> dict[str, Any]:
    """Drive every layer through one event sequence.  Deterministic.

    Each step is one of:

        {"from_time": …, "to_time": …, "inputs": [...], "pose": …}
            -> advance the body
        {"result": {…ActionResult…}, "at": …, "pose": …}
            -> apply a body consequence
        {"observe": true, "at": …}
            -> derive and store observations
    """

    import body_consequence as bcon
    import body_observation as bob
    import body_runtime as br

    if workdir is None:
        raise ContinuityError("play_sequence needs an isolated workdir")

    root = Path(workdir)
    body_root = br.body_root(root)
    runtime_path = body_root / br.RUNTIME_NAME
    transitions_path = body_root / br.TRANSITIONS_NAME

    store = br.BodyStore(body_root)
    ledger = bcon.ConsequenceLedger(body_root / "consequences.jsonl")
    observations = bob.ObservationStore(bob.observation_path(root))

    if not runtime_path.exists():
        store.save_profile(br.build_profile(body_id=body_id))

    first = steps[0] if steps else {}
    start_at = first.get("from_time") or first.get("at")
    state = store.load_runtime() or br.initial_runtime(body_id=body_id, at=start_at)
    store.save_runtime(state)

    applied: list[str] = []
    for index, step in enumerate(steps):
        if not isinstance(step, Mapping):
            raise ContinuityError(f"steps[{index}] must be an object")

        if "result" in step:
            outcome = bcon.apply_for_execution(
                step["result"],
                body_id=body_id,
                ledger=ledger,
                runtime_state=state,
                at=step.get("at"),
                pose=step.get("pose"),
                object_id=step.get("object_id"),
            )
            state = outcome["state"]
            store.save_runtime(state)
            if outcome.get("transition"):
                store.append_transition(outcome["transition"])
            if outcome["applied"]:
                applied.append(step["result"].get("execution_id"))
            continue

        if step.get("observe"):
            records = bob.observe(
                state,
                at=step.get("at"),
                previous_state=step.get("previous_state"),
                source_transition_refs=step.get("source_transition_refs"),
            )
            observations.append(records)
            continue

        before = state
        result = br.advance(
            state,
            step["from_time"],
            step["to_time"],
            step.get("inputs"),
            state["runtime_version"],
        )
        state = result["state"]
        store.save_runtime(state)
        if result["advanced"]:
            store.append_transition(
                br.build_transition(
                    before,
                    state,
                    at=step["to_time"],
                    cause_class=result["cause_class"],
                    cause_classes=result["cause_classes"],
                    input_summary=result["inputs_summary"],
                )
            )

    return {
        "kind": "body_continuity_replay_run",
        "schema_version": SCHEMA_SEAL,
        "final_state": state,
        "applied_executions": sorted(applied),
        "transitions": len(store.read_transitions()),
        "consequences": len(ledger.read_all()),
        "observations": len(observations.read_all()),
        "layer_digest": snapshot(root)["digest"],
        "snapshot": snapshot(root),
    }


def replay_verify(
    steps: Sequence[Mapping[str, Any]],
    *,
    workdirs: Sequence[Path | str],
    body_id: str = "chiyo_body",
) -> dict[str, Any]:
    """Run the same sequence in independent workdirs and compare every layer."""

    if len(workdirs) < 2:
        raise ContinuityError("replay_verify needs at least two workdirs")

    runs = [
        play_sequence(steps, body_id=body_id, workdir=workdir)
        for workdir in workdirs
    ]
    first = runs[0]
    mismatches: list[str] = []
    for index, run in enumerate(runs[1:], start=2):
        if run["final_state"] != first["final_state"]:
            mismatches.append(f"run{index}: body state differs")
        if run["applied_executions"] != first["applied_executions"]:
            mismatches.append(f"run{index}: applied executions differ")
        if run["transitions"] != first["transitions"]:
            mismatches.append(f"run{index}: transition count differs")
        if run["consequences"] != first["consequences"]:
            mismatches.append(f"run{index}: consequence count differs")
        if run["observations"] != first["observations"]:
            mismatches.append(f"run{index}: observation count differs")
        for layer in LAYER_NAMES:
            left = first["snapshot"]["layers"][layer]["sha256"]
            right = run["snapshot"]["layers"][layer]["sha256"]
            if left != right and not (left is None and right is None):
                mismatches.append(f"run{index}: layer {layer} digest differs")

    return {
        "kind": "body_continuity_replay_report",
        "schema_version": SCHEMA_SEAL,
        "runs": len(runs),
        "deterministic": not mismatches,
        "mismatches": mismatches,
        "float_tolerance": 0.0,
        "final_body_version": first["final_state"]["version"],
        "digests": [run["layer_digest"] for run in runs],
    }


# --------------------------------------------------------------------------
# migration
# --------------------------------------------------------------------------


def migrate_all(hermes_home: Optional[Path | str] = None) -> dict[str, Any]:
    """Report and perform the sanctioned migration for each layer.

    Never rewrites an external (World) layer.  Never coerces an unknown schema.
    """

    import body_observation as bob
    import body_consequence as bcon
    import body_runtime as br
    import world_body_substrate as wbs

    home = _home(hermes_home)
    results: dict[str, Any] = {}

    # body runtime
    runtime_path = layer_path("body_runtime", home)
    if runtime_path.exists():
        raw = _read_json(runtime_path)
        try:
            state, migrated = br.migrate_runtime(raw)
            results["body_runtime"] = {
                "schema_version": state["schema_version"],
                "migrated": migrated,
                "ok": True,
            }
        except Exception as exc:
            results["body_runtime"] = {"ok": False, "error": type(exc).__name__}
    else:
        results["body_runtime"] = {"ok": True, "present": False}

    # body profile
    profile_path = layer_path("body_profile", home)
    if profile_path.exists():
        raw = _read_json(profile_path)
        try:
            profile = br.validate_profile(raw)
            results["body_profile"] = {
                "schema_version": profile["schema_version"], "migrated": False, "ok": True
            }
        except Exception as exc:
            results["body_profile"] = {"ok": False, "error": type(exc).__name__}
    else:
        results["body_profile"] = {"ok": True, "present": False}

    # jsonl layers: validate every record's schema
    for layer, validator in (
        ("body_transitions", br.validate_transition),
        ("body_consequences", bcon.validate_consequence),
        ("body_observations", bob.validate_observation),
    ):
        path = layer_path(layer, home)
        if not path.exists():
            results[layer] = {"ok": True, "present": False, "records": 0}
            continue
        records = 0
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                validator(json.loads(line))
                records += 1
            results[layer] = {"ok": True, "records": records, "migrated": False}
        except Exception as exc:
            results[layer] = {"ok": False, "error": type(exc).__name__}

    # body substrate (world-side embodiment substrate)
    substrate_path = wbs.substrate_path(home)
    if substrate_path.exists():
        raw = _read_json(substrate_path)
        try:
            state, migrated = wbs.migrate_substrate(raw)
            results["body_substrate"] = {
                "schema_version": state["schema_version"], "migrated": migrated, "ok": True
            }
        except Exception as exc:
            results["body_substrate"] = {"ok": False, "error": type(exc).__name__}
    else:
        results["body_substrate"] = {"ok": True, "present": False}

    # external layers are reported, never migrated
    for layer in ("world_state", "world_timeline", "execution_ledger"):
        entry = _read_json(layer_path(layer, home))
        results[layer] = {
            "ok": True,
            "migrated": False,
            "external": True,
            "schema_version": (entry or {}).get("schema_version"),
            "reason": "external authority; Body never migrates it",
        }

    return {
        "kind": "body_continuity_migration_report",
        "schema_version": SCHEMA_SEAL,
        "layers": results,
        "all_ok": all(entry.get("ok") for entry in results.values()),
        "fail_closed_on_unknown": True,
        "external_layers_untouched": True,
    }


# --------------------------------------------------------------------------
# seal
# --------------------------------------------------------------------------


def build_seal(
    hermes_home: Optional[Path | str] = None,
    *,
    note: Optional[str] = None,
) -> dict[str, Any]:
    """A deterministic seal over every layer."""

    snap = snapshot(hermes_home)
    identity = {
        "layers": {
            name: snap["layers"][name]["sha256"] for name in LAYER_NAMES
        },
        "kind": KIND_SEAL,
        "schema_version": SCHEMA_SEAL,
    }
    return {
        "kind": KIND_SEAL,
        "schema_version": SCHEMA_SEAL,
        "seal_id": "body-continuity:" + _digest(identity)[:32],
        "layer_digest": snap["digest"],
        "layers": snap["layers"],
        "present_layers": snap["present_layers"],
        "absent_layers": snap["absent_layers"],
        "note": note,
    }


def write_seal(
    hermes_home: Optional[Path | str] = None, *, note: Optional[str] = None
) -> dict[str, Any]:
    seal = build_seal(hermes_home, note=note)
    target = _home(hermes_home) / "data" / "body" / SEAL_NAME
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(seal, ensure_ascii=False, sort_keys=True, indent=2), encoding="utf-8"
    )
    return seal


def verify_seal(
    seal: Mapping[str, Any], *, hermes_home: Optional[Path | str] = None
) -> dict[str, Any]:
    """Re-snapshot and compare against a seal.  Divergence is reported, not hidden."""

    if not isinstance(seal, Mapping) or seal.get("kind") != KIND_SEAL:
        raise ContinuityError("not a continuity seal")
    current = snapshot(hermes_home)
    diverged: list[str] = []
    for name in LAYER_NAMES:
        before = (seal.get("layers") or {}).get(name, {}).get("sha256")
        after = current["layers"][name]["sha256"]
        if before != after:
            diverged.append(name)
    return {
        "kind": "body_continuity_seal_check",
        "schema_version": SCHEMA_SEAL,
        "seal_id": seal.get("seal_id"),
        "verified": not diverged,
        "diverged_layers": diverged,
        "layer_digest_before": seal.get("layer_digest"),
        "layer_digest_now": current["digest"],
    }