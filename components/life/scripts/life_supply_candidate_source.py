"""C16 / LPC integration candidate: LifeSupplyCandidateSource.

Read-only candidate source that exposes Chiyo's own PersonalOpportunityView (served by the
independent Life Supply service over its local Unix socket) to the LPC AG-0 candidate pipeline.

Contract this module implements (read from the current LPC core, not from prose):
``./alpha/chiyo/life_runtime/candidate_sources_ag0.py``

    class CandidateSourceAdapter:
        source_kind: str
        canonical_owner: str
        def is_enabled(self) -> bool
        def availability_status(self) -> dict
        def discover(self, *, observed_at) -> (raw_items, snapshot_ref, meta)
        def validate_source(self, raw_item, *, observed_at) -> (bool, reason)
        def materialize(self, *, observed_at) -> (CandidateRecords, snapshot_ref, meta)
        def refresh(self, candidate, *, observed_at) -> CandidateRecord
        # invalidate() is implemented by the base class

Hard rules honoured here:

* Life Supply is the only canonical writer of its own state.  This adapter only *reads*, only
  over the service's Unix socket, and never opens the Life Supply SQLite file or takes its
  writer identity.
* It creates no Activity and no Action: the base class's ``start_activity`` /
  ``resume_activity`` / ``propose_action`` raise ``CandidateAdoptionForbiddenError``.
* It fails closed.  If the service cannot be reached, ``is_enabled()`` is False,
  ``availability_status()`` is DISABLED, ``discover()`` returns no items and
  ``validate_source()`` returns ``(False, "SOURCE_UNAVAILABLE")``.  No candidate is invented.
* The new ``source_kind`` needs one line added to ``VALID_CANDIDATE_SOURCE_KINDS`` in the LPC
  core before ``CandidateSourceRegistry.register_adapter`` will accept this adapter; see
 ``INTEGRATION_PROPOSAL.md``.  That admission is the LPC owner's decision.

The candidate kind is a constructor argument because mapping a personal opportunity onto one of
the seven existing candidate kinds is a semantic decision that belongs to the LPC owner.  The
default is the closest existing kind and is documented as such.
"""
from __future__ import annotations

import hashlib
import json
import os
import socket
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, Optional

# The LPC core adds its own package directory to sys.path; mirror that here.
import sys

_LIFERUNTIME = str(Path(__file__).resolve().parent)
if _LIFERUNTIME not in sys.path:
    sys.path.insert(0, _LIFERUNTIME)

from candidate_sources_ag0 import (  # noqa: E402
    ADAPTER_STATUS_DISABLED,
    ADAPTER_STATUS_PARTIAL,
    ADAPTER_STATUS_READY,
    CANDIDATE_KIND_CONSIDER_PERSONAL_OPPORTUNITY,
    CANDIDATE_STATUS_OPEN,
    CANDIDATE_STATUS_STALE,
    CANDIDATE_STATUS_WITHDRAWN,
    CandidateRecord,
    CandidateSourceAdapter,
    CandidateSourceError,
    DEFAULT_PER_SOURCE_CAP,
    GLOBAL_SUBJECT_ID,
    POLICY_VERSION_AG0_V1,
    SCHEMA_CANDIDATE_RECORD,
    _STRUCTURED_REF_RE,
    _require_iso,
    sha256_hex,
)

#: The one new source kind this integration asks the LPC owner to admit.
SOURCE_PERSONAL_OPPORTUNITY = "PERSONAL_OPPORTUNITY"

DEFAULT_LIFE_SUPPLY_SOCKET = "./data/supply/life-supply.sock"
DEFAULT_TIMEOUT = 5.0
#: Refuse a payload larger than the service itself accepts.
MAX_RESPONSE_BYTES = 1_000_000
#: Read-only operation.  This adapter never sends a command op.
READ_OP = "list_personal_opportunities"


class LifeSupplyUnavailable(CandidateSourceError):
    """The Life Supply read port could not be reached, or answered unusably."""


class LifeSupplyReadClient:
    """Minimal read-only client for the Life Supply Unix socket.

    Deliberately not a copy of the Life Supply CLI: it speaks the same one-line JSON protocol but
    offers only the read op this adapter needs, and never sends a command.
    """

    def __init__(self, socket_path: str = DEFAULT_LIFE_SUPPLY_SOCKET,
                 timeout: float = DEFAULT_TIMEOUT) -> None:
        self.socket_path = str(socket_path)
        self.timeout = float(timeout)

    def list_personal_opportunities(self, subject: str) -> dict[str, Any]:
        request = {"op": READ_OP, "subject": str(subject)}
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.settimeout(self.timeout)
                client.connect(self.socket_path)
                client.sendall(json.dumps(request, ensure_ascii=False,
                                          separators=(",", ":")).encode() + b"\n")
                line = client.makefile("rb").readline(MAX_RESPONSE_BYTES)
        except OSError as exc:
            raise LifeSupplyUnavailable(f"LIFE_SUPPLY_UNREACHABLE:{exc}") from exc
        if not line:
            raise LifeSupplyUnavailable("LIFE_SUPPLY_EMPTY_RESPONSE")
        try:
            answer = json.loads(line.decode("utf-8"))
        except (UnicodeError, ValueError) as exc:
            raise LifeSupplyUnavailable("LIFE_SUPPLY_MALFORMED_RESPONSE") from exc
        if not isinstance(answer, dict) or not isinstance(answer.get("status"), str):
            raise LifeSupplyUnavailable("LIFE_SUPPLY_UNTYPED_RESPONSE")
        return answer


class LifeSupplyCandidateSource(CandidateSourceAdapter):
    """AG-0 candidate source over Chiyo's own PersonalOpportunityView (read-only, 0 LLM)."""

    source_kind = SOURCE_PERSONAL_OPPORTUNITY
    canonical_owner = "chiyo-life-supply (Workspace owner)"

    #: Fields every raw item must carry before it may become a candidate (order PHASE 11).
    REQUIRED_ITEM_FIELDS = ("opportunity_id", "subject_id", "source_ref", "source_kind",
                            "availability", "visibility", "valid_from")

    def __init__(self, *,
                 socket_path: str = DEFAULT_LIFE_SUPPLY_SOCKET,
                 subject_id: str,
                 client: Optional[LifeSupplyReadClient] = None,
                 per_source_cap: int = DEFAULT_PER_SOURCE_CAP,
                 timeout: float = DEFAULT_TIMEOUT,
                 candidate_kind: str = CANDIDATE_KIND_CONSIDER_PERSONAL_OPPORTUNITY) -> None:
        super().__init__(per_source_cap=per_source_cap)
        if not subject_id:
            raise CandidateSourceError("subject_id is required")
        self.subject_id = str(subject_id)
        self.socket_path = str(socket_path)
        self.client = client or LifeSupplyReadClient(self.socket_path, timeout=timeout)
        self.candidate_kind = str(candidate_kind)
        self._last_snapshot_ref: str = "snap:PERSONAL_OPPORTUNITY:rev:0"
        self._revision: int = 0

    # -- availability ---------------------------------------------------------------------
    def is_enabled(self) -> bool:
        """Enabled only while the Life Supply read port actually answers."""
        try:
            answer = self.client.list_personal_opportunities(self.subject_id)
        except LifeSupplyUnavailable:
            return False
        return answer.get("status") == "OK"

    def availability_status(self) -> dict[str, Any]:
        try:
            answer = self.client.list_personal_opportunities(self.subject_id)
        except LifeSupplyUnavailable as exc:
            return {
                "source_kind": self.source_kind,
                "status": ADAPTER_STATUS_DISABLED,
                "canonical_owner": self.canonical_owner,
                "reason_code": "LIFE_SUPPLY_UNREACHABLE",
                "detail": str(exc)[:200],
            }
        if answer.get("status") != "OK":
            return {
                "source_kind": self.source_kind,
                "status": ADAPTER_STATUS_DISABLED,
                "canonical_owner": self.canonical_owner,
                "reason_code": f"LIFE_SUPPLY_{answer.get('status')}",
                "detail": str(answer.get("reason"))[:200],
            }
        return {
            "source_kind": self.source_kind,
            "status": ADAPTER_STATUS_READY,
            "canonical_owner": self.canonical_owner,
            "reason_code": "LIFE_SUPPLY_READ_PORT_OK",
            "opportunity_count": len(answer.get("opportunities") or []),
        }

    # -- discovery ------------------------------------------------------------------------
    def discover(self, *, observed_at: str) -> tuple[list[dict[str, Any]], str, dict[str, int]]:
        obs_iso = _require_iso(observed_at, "observed_at")
        try:
            answer = self.client.list_personal_opportunities(self.subject_id)
        except LifeSupplyUnavailable:
            return [], self._snapshot_ref(), {"discovered": 0, "emitted": 0, "rejected": 0}

        raw_items = answer.get("opportunities")
        if answer.get('status') != 'OK' or not isinstance(raw_items, list):
            # A malformed payload is not an empty workspace: refuse rather than invent.
            return [], self._snapshot_ref(), {"discovered": 0, "emitted": 0, "rejected": 0}

        valid: list[dict[str, Any]] = []
        rejected = 0
        seen: set[str] = set()
        for item in raw_items:
            if not isinstance(item, Mapping):
                rejected += 1
                continue
            ok, _reason = self.validate_source(item, observed_at=obs_iso)
            if not ok:
                rejected += 1
                continue
            key = str(item.get("opportunity_id"))
            if key in seen:
                rejected += 1
                continue
            seen.add(key)
            valid.append(dict(item))

        discovered = len(valid)
        bounded = valid[: self.per_source_cap]
        return bounded, self._snapshot_ref(discovered), {
            "discovered": discovered,
            "emitted": len(bounded),
            "rejected": rejected,
        }

    def _snapshot_ref(self, discovered: int = 0) -> str:
        self._revision += 1
        self._last_snapshot_ref = (
            f"snap:{self.source_kind}:subject:{self.subject_id}:rev:{self._revision}:n{discovered}")
        return self._last_snapshot_ref

    # -- validation ------------------------------------------------------------------------
    def validate_source(self, raw_item: Mapping[str, Any], *,
                        observed_at: str) -> tuple[bool, Optional[str]]:
        """Every candidate must carry subject, source ref, kind, availability, scope and validity.

        Order PHASE 11: only a candidate that passes both Life Supply's and LPC's validation may
        reach Agency.  Stale / withdrawn / out-of-scope / wrong-subject items are refused.
        """
        try:
            _require_iso(observed_at, "observed_at")
        except CandidateSourceError:
            return False, "OBSERVED_AT_INVALID"
        if not isinstance(raw_item, Mapping):
            return False, "ITEM_NOT_A_MAPPING"

        for name in self.REQUIRED_ITEM_FIELDS:
            if raw_item.get(name) in (None, ""):
                return False, f"MISSING_FIELD:{name}"

        if str(raw_item.get("subject_id")) != self.subject_id:
            return False, "SUBJECT_MISMATCH"
        if str(raw_item.get("source_kind")) != self.source_kind:
            return False, "SOURCE_KIND_MISMATCH"
        if str(raw_item.get("visibility")) not in {"PRIVATE", "PERSONAL"}:
            return False, "VISIBILITY_NOT_PERSONAL"
        if str(raw_item.get("availability")) != CANDIDATE_STATUS_OPEN:
            # STALE / WITHDRAWN / EXPIRED / CONSUMED / INVALID never become candidates
            return False, f"NOT_AVAILABLE:{raw_item.get('availability')}"

        source_ref = str(raw_item.get("source_ref"))
        if not _STRUCTURED_REF_RE.fullmatch(source_ref):
            return False, "SOURCE_REF_NOT_STRUCTURED"
        opportunity_id = str(raw_item.get("opportunity_id"))
        if not _STRUCTURED_REF_RE.fullmatch(opportunity_id):
            return False, "OPPORTUNITY_ID_NOT_STRUCTURED"
        try:
            _require_iso(str(raw_item.get("valid_from")), "valid_from")
        except CandidateSourceError:
            return False, "VALID_FROM_INVALID"
        valid_until = raw_item.get("valid_until")
        if valid_until not in (None, ""):
            try:
                _require_iso(str(valid_until), "valid_until")
            except CandidateSourceError:
                return False, "VALID_UNTIL_INVALID"
        try:
            parse = lambda x: datetime.fromisoformat(str(x).replace('Z', '+00:00'))
            now = parse(observed_at)
            if parse(raw_item['valid_from']) > now:
                return False, 'NOT_YET_AVAILABLE'
            if valid_until and parse(valid_until) <= now:
                return False, 'EXPIRED'
            if int(raw_item.get('source_revision') or 1) < 1:
                return False, 'REVISION_INVALID'
        except (ValueError, TypeError, OverflowError):
            return False, 'VALIDITY_INVALID'
        return True, None

    # -- materialization -------------------------------------------------------------------
    def materialize(self, *, observed_at: str) -> tuple[list[CandidateRecord], str, dict[str, int]]:
        obs_iso = _require_iso(observed_at, "observed_at")
        items, snap_ref, meta = self.discover(observed_at=obs_iso)
        results: list[CandidateRecord] = []

        for item in items:
            opportunity_id = str(item["opportunity_id"])
            source_ref = str(item["source_ref"])
            revision = int(item.get("source_revision") or 1)
            target_refs = [source_ref, opportunity_id]
            for extra in item.get("target_refs") or []:
                if isinstance(extra, str) and _STRUCTURED_REF_RE.fullmatch(extra) \
                        and extra not in target_refs:
                    target_refs.append(extra)

            cand_id = f"cand:{sha256_hex(f'{self.source_kind}:{opportunity_id}:v{revision}')[:16]}"
            sem_id = f"semcand:{sha256_hex(f'{GLOBAL_SUBJECT_ID}:{self.source_kind}:{opportunity_id}')[:16]}"
            cand = CandidateRecord(
                candidate_id=cand_id,
                subject_id=GLOBAL_SUBJECT_ID,
                candidate_kind=self.candidate_kind,
                source_kind=self.source_kind,
                source_ref=source_ref,
                source_refs=[source_ref],
                source_revision=revision,
                target_refs=target_refs,
                availability_status=CANDIDATE_STATUS_OPEN,
                created_at=str(item["valid_from"]),
                observed_at=obs_iso,
                recorded_at=obs_iso,
                valid_from=str(item["valid_from"]),
                valid_until=item.get("valid_until") or None,
                causal_parent_refs=[opportunity_id],
                provenance_refs=[opportunity_id, source_ref, snap_ref],
                provenance_chain=[
                    {"step": "candidate", "ref": cand_id, "kind": self.candidate_kind},
                    {"step": "adapter", "ref": f"adapter:{self.source_kind}",
                     "owner": self.canonical_owner},
                    {"step": "canonical_source", "ref": source_ref, "revision": revision,
                     "availability": item["availability"]},
                ],
                semantic_identity=sem_id,
                idempotency_key=f"idem_cand:personal_opportunity:{opportunity_id}:v{revision}",
                policy_version=POLICY_VERSION_AG0_V1,
                schema_version=SCHEMA_CANDIDATE_RECORD,
            )
            results.append(cand)
        return results, snap_ref, meta

    # -- refresh ---------------------------------------------------------------------------
    def refresh(self, candidate: CandidateRecord, *, observed_at: str) -> CandidateRecord:
        """Re-check one candidate against the live read port; never mutates Life Supply."""
        obs_iso = _require_iso(observed_at, "observed_at")
        try:
            answer = self.client.list_personal_opportunities(self.subject_id)
        except LifeSupplyUnavailable:
            return self.invalidate(candidate, reason_code="SOURCE_UNAVAILABLE",
                                   observed_at=obs_iso, new_status=CANDIDATE_STATUS_STALE)
        if answer.get("status") != "OK":
            return self.invalidate(candidate, reason_code="READ_PORT_NOT_OK",
                                   observed_at=obs_iso, new_status=CANDIDATE_STATUS_STALE)

        current = {str(item.get("source_ref")): item
                   for item in (answer.get("opportunities") or [])
                   if isinstance(item, Mapping)}
        live = current.get(candidate.source_ref)
        if live is None:
            return self.invalidate(candidate, reason_code="OPPORTUNITY_NO_LONGER_PRESENT",
                                   observed_at=obs_iso, new_status=CANDIDATE_STATUS_WITHDRAWN)
        ok, reason = self.validate_source(live, observed_at=obs_iso)
        if not ok:
            return self.invalidate(candidate, reason_code=reason or "SOURCE_INVALID",
                                   observed_at=obs_iso, new_status=CANDIDATE_STATUS_STALE)
        refreshed = CandidateRecord(**{**candidate.__dict__})
        refreshed.availability_status = CANDIDATE_STATUS_OPEN
        refreshed.invalidation_reason = None
        refreshed.recorded_at = obs_iso
        refreshed.revision = int(candidate.revision) + 1
        return refreshed


class ServiceUserReadClient:
    """Read under the existing socket owner; no added root ACL or writer grant."""
    def __init__(self, socket_path=DEFAULT_LIFE_SUPPLY_SOCKET, timeout=2.0):
        self.socket_path, self.timeout = str(socket_path), float(timeout)

    def list_personal_opportunities(self, subject):
        try:
            result = subprocess.run(['/usr/sbin/runuser', '-u', 'chiyo-life-supply', '--',
                '/usr/bin/python3', str(Path(__file__).resolve()), '--read-view',
                self.socket_path, str(subject)], capture_output=True, timeout=self.timeout, check=True)
            answer = json.loads(result.stdout)
            if not isinstance(answer, dict):
                raise ValueError('untyped view')
            return answer
        except (OSError, subprocess.SubprocessError, ValueError) as exc:
            raise LifeSupplyUnavailable('SERVICE_READ_UNAVAILABLE') from exc


if __name__ == '__main__':
    if len(sys.argv) != 4 or sys.argv[1] != '--read-view':
        raise SystemExit('only the owner read-view operation is supported')
    print(json.dumps(LifeSupplyReadClient(sys.argv[2], timeout=1.5).list_personal_opportunities(sys.argv[3])))
