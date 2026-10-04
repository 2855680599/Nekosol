#!/usr/bin/env python3
"""M14A: world_action_authorization.py — one-shot capability registry.

Owned by: BOTH sides write/read a single JSON registry under run_dir.
  - Intent Executor (gateway proc) ISSUES records (append + rewrite state).
  - Socket server (body service proc) VERIFIES + CONSUMES (state -> CONSUMED).
Trust anchor: registry path lives in the service-owned run dir (0600 root).
The socket server itself never creates authorizations (ticket §3).

Concurrency: a module-level fcntl lock on the registry file serializes
issue/consume between the two processes.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import pathlib
import time
from typing import Any, Mapping, Optional

SCHEMA = "world.action.authorization.v1"
KIND_ISSUED = "world_action_authorization"
STATE_ISSUED = "ISSUED"
STATE_CONSUMED = "CONSUMED"
DEFAULT_TTL_SECONDS = 120.0
REGISTRY_NAME = "action_authorizations.json"


def _digest(obj: Any) -> str:
    payload = json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S+08:00", time.localtime(ts))


def _parse_iso(text: str) -> Optional[float]:
    try:
        from datetime import datetime
        return datetime.fromisoformat(text).timestamp()
    except (ValueError, TypeError):
        return None


def canonical_args_digest(params: Mapping[str, Any]) -> str:
    return _digest(dict(params or {}))


class AuthorizationError(RuntimeError):
    """Fail-closed rejection with a machine-readable reason."""


class AuthorizationRegistry:
    def __init__(self, run_dir: pathlib.Path | str, *, ttl_seconds: float = DEFAULT_TTL_SECONDS):
        self.path = pathlib.Path(run_dir) / REGISTRY_NAME
        self.ttl_seconds = float(ttl_seconds)

    # -- low-level io (locked) ------------------------------------------
    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"schema_version": SCHEMA, "records": []}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except ValueError:
            # corrupt registry: fail closed, but do not wedge future issues
            return {"schema_version": SCHEMA, "records": []}
        if not isinstance(data, dict) or not isinstance(data.get("records"), list):
            return {"schema_version": SCHEMA, "records": []}
        return data

    def _save(self, data: dict[str, Any]) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=1, sort_keys=True, ensure_ascii=False) + "\n",
                       encoding="utf-8")
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.path)

    class _Lock:
        def __enter__(self):
            self.fh = open(os.fspath(self.path), "a+")
            fcntl.flock(self.fh, fcntl.LOCK_EX)
            return self

        def __exit__(self, *exc):
            fcntl.flock(self.fh, fcntl.LOCK_UN)
            self.fh.close()
            return False

        def __init__(self, path: pathlib.Path):
            self.path = path

    def _locked(self):
        return AuthorizationRegistry._Lock(self.path)

    # -- public ----------------------------------------------------------
    def issue(
        self,
        *,
        decision_id: Optional[str],
        intent_id: str,
        intent_fingerprint: str,
        execution_id: str,
        actor: str,
        verb: str,
        params: Mapping[str, Any],
        authority_generation: Any,
        ttl_seconds: Optional[float] = None,
        now: Optional[float] = None,
    ) -> dict[str, Any]:
        now_ts = float(now if now is not None else time.time())
        ttl = float(ttl_seconds if ttl_seconds is not None else self.ttl_seconds)
        core = {
            "decision_id": decision_id,
            "intent_id": intent_id,
            "intent_fingerprint": intent_fingerprint,
            "execution_id": execution_id,
            "actor": actor,
            "verb": verb,
            "args_digest": canonical_args_digest(params),
        }
        record = {
            "kind": KIND_ISSUED,
            "schema_version": SCHEMA,
            **core,
            "authority_generation": authority_generation,
            "issued_at": _iso(now_ts),
            "expires_at": _iso(now_ts + ttl),
            "state": STATE_ISSUED,
        }
        record["authorization_id"] = "wauth:" + _digest(
            {k: record[k] for k in core} | {"issued_at": record["issued_at"]}
        )[:32]
        with self._locked():
            data = self._load()
            data["records"].append(record)
            # bound the file: keep the last 256 records
            if len(data["records"]) > 256:
                data["records"] = data["records"][-256:]
            self._save(data)
        return record

    def verify_and_consume(
        self,
        *,
        authorization_id: str,
        execution_id: str,
        verb: str,
        params: Mapping[str, Any],
        actor: str = "chiyo",
        decision_id: Optional[str] = None,
        now: Optional[float] = None,
    ) -> dict[str, Any]:
        """Consume one authorization; raise AuthorizationError on any mismatch.

        Fail-closed: missing / unknown / consumed / expired / field mismatch.
        """
        now_ts = float(now if now is not None else time.time())
        with self._locked():
            data = self._load()
            record = next(
                (r for r in data["records"] if r.get("authorization_id") == authorization_id),
                None,
            )
            if record is None:
                raise AuthorizationError("AUTHORIZATION_NOT_FOUND")
            if record.get("state") == STATE_CONSUMED:
                raise AuthorizationError("AUTHORIZATION_ALREADY_CONSUMED")
            exp = _parse_iso(record.get("expires_at", ""))
            if exp is None or now_ts > exp:
                raise AuthorizationError("AUTHORIZATION_EXPIRED")
            if record.get("execution_id") != execution_id:
                raise AuthorizationError("EXECUTION_ID_MISMATCH")
            if record.get("verb") != verb:
                raise AuthorizationError("VERB_MISMATCH")
            if record.get("actor") != actor:
                raise AuthorizationError("ACTOR_MISMATCH")
            # M15G: bind the capability to the Decision that produced it.  An
            # authorization minted for decision A must not be consumed by a
            # submission that claims decision B (cross-decision replay).
            if decision_id is not None and record.get("decision_id") != decision_id:
                raise AuthorizationError("DECISION_MISMATCH")
            if record.get("args_digest") != canonical_args_digest(params):
                raise AuthorizationError("ARGS_MISMATCH")
            record["state"] = STATE_CONSUMED
            record["consumed_at"] = _iso(now_ts)
            self._save(data)
            return record

    def lookup(self, authorization_id: str) -> Optional[dict[str, Any]]:
        data = self._load()
        return next(
            (r for r in data["records"] if r.get("authorization_id") == authorization_id),
            None,
        )
