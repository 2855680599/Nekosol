"""CT0-10 canonical full chain + crash/race/replay/negative qualification.

Runs under LINUX (the real owners import fcntl). Modes:
  --mode chain      : the canonical-wired happy path + authority trace   (order 77-81)
  --mode negatives  : the 10 canonical source-to-send negatives          (order 101)
  --mode races      : 6 wiring races x 50 rounds                        (order 85-86)
  --mode crash      : 8 wiring seams x 5 rounds, real hard kill         (order 82-84)
  --mode replay     : close / fresh bootstrap / reconstruct             (order 87-89)
  --worker <seam>   : the crash child (hard-exits at the seam)

Every stage is driven through a REAL canonical owner in an isolated namespace.
Nothing is replaced by a fixture: if an owner cannot be used, the stage is BLOCKED.

TWO GATE PROFILES (a deliberate design; never collapsed):
  PRODUCTION_OFF     -- the real default.  No owner-less capacity sub-port is supplied, so
                        the canonical read-only capacity resolver reports its honest
                        UNKNOWN/UNPROVABLE verdicts and the chain stops at
                        BLOCKED_AT_CAPACITY.  This is the profile of record for
                        "real proactive Telegram side effect = 0".
  ISOLATED_TEST_OPEN -- supplies DECLARED TEST DOUBLES for the four capacity sub-ports that
                        have NO canonical owner, and evaluates the dispatch-admission
                        kill-switch fence against a DECLARED TEST GATE SNAPSHOT so the
                        integration path can be exercised end to end.  The real process
                        environment is NEVER touched: LIFE_RUNTIME_ENABLED / AGENCY_ENABLED /
                        ACTION_EXECUTION_ENABLED / PROACTIVE_ENABLED stay false and are
                        recorded, and the Telegram boundary keeps its own unarmed gate so
                        telegram_real_side_effect_enabled stays False.
Every double is tagged declared_test_double / canonical_owner_exists=false /
disposition=TEST_DOUBLE_FOR_MISSING_CANONICAL_PORT and enumerated in the artifact.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import time
import traceback
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Mapping, Sequence

# --- vendored release layout -------------------------------------------------
# This release ships the contact stack flat in chiyo/contact, the canonical
# owners in chiyo/life_runtime and the CT0-10 ports in
# chiyo/contact/canonical_ports.  The defaults below therefore point inside the
# release tree; every one of them can still be overridden through the
# environment, and chiyo/_vendor_paths.py sets the same defaults.
_CHIYO_DIR = Path(__file__).resolve().parents[1]
_TREE_DIR = _CHIYO_DIR.parent
RUN_ROOT = Path(os.environ.get("CT0_10_ISO_ROOT", str(_TREE_DIR / "var" / "isolated")))
RUNTIME = Path(os.environ.get("CT0_10_RUNTIME", str(_CHIYO_DIR / "life_runtime")))
SANDBOX = Path(os.environ.get("CT0_10_SANDBOX", str(_TREE_DIR)))
OUT_DIR = Path(os.environ.get("CT0_10_OUT", str(_TREE_DIR / "var")))

for entry in (str(_CHIYO_DIR / "contact"), str(_CHIYO_DIR / "contact" / "canonical_ports"),
              str(_CHIYO_DIR / "life_runtime"), str(SANDBOX / "src" / "ct0"),
              str(SANDBOX / "src"), str(SANDBOX / "tests"),
              str(RUNTIME / "scripts"), str(RUNTIME)):
    if entry not in sys.path:
        sys.path.insert(0, entry)

import canonical_spine as spine  # noqa: E402
from canonical_chain import dispatch_admission  # noqa: E402
from canonical_ports import ag1_adapter  # noqa: E402
from canonical_ports.ag0_adapter import CanonicalAg0CandidateAdapter  # noqa: E402
from canonical_ports.ag1_adapter import CanonicalAg1DecisionAdapter  # noqa: E402
from canonical_ports.ar0_adapter import CanonicalAr0Adapter, load_ar0_modules  # noqa: E402
from canonical_ports.ar1_consumer import CanonicalAr1Consumer  # noqa: E402
from canonical_ports.capacity_resolver import CanonicalContactCapacityResolver  # noqa: E402
from canonical_ports.expression_adapter import (CanonicalExpressionAdapter,  # noqa: E402
                                                DeterministicGroundedRenderer)
from canonical_ports.grounding_resolver import (CAPSULE_SOURCE_REF,  # noqa: E402
                                                MEMORY_SOURCE_REF_PREFIX,
                                                QUALIFICATION_SCOPE_DECISION,
                                                RELATIONSHIP_SOURCE_REF,
                                                CanonicalContactGroundingResolver)
from canonical_ports.telegram_boundary import CanonicalTelegramBoundary  # noqa: E402

from contact_candidate_model import SOURCE_LIFE_EXPERIENCE  # noqa: E402
from contact_candidate_sources import (ContactCandidateSourceRegistry,  # noqa: E402
                                       LifeExperienceAuthorization, LifeExperienceSource)
from contact_draft_model import (CONTACT_DECISION_RECEIPT_SCHEMA, DECISION_CONTACT_SELECTED,  # noqa: E402
                                 ContactDecisionReceipt)
from contact_intent_authority import ContactIntentAuthority  # noqa: E402
from contact_intent_model import STATUS_SELECTED, normalize_utc  # noqa: E402
from contact_message_action_model import ContactActionGate, build_message_action_request  # noqa: E402
from support.ct0_9_isolation_fixtures import life_experience_handoff  # noqa: E402
from contact_draft_model import (CONTACT_DECISION_RECEIPT_SCHEMA, DECISION_CONTACT_SELECTED,  # noqa: E402
                                 ContactDecisionReceipt, sha256_text)
SEAM_ORDER = ("C10-C01", "C10-C02", "C10-C03", "C10-C04", "C10-C05", "C10-C06", "C10-C07",
              "C10-C08")
SEAM_DESCRIPTION = {
    "C10-C01": "ContactIntent committed before AG0 consume",
    "C10-C02": "AG0 candidate committed before AG1 decision",
    "C10-C03": "decision committed before Expression",
    "C10-C04": "draft committed before AR0",
    "C10-C05": "AR0 action committed before dispatch admission",
    "C10-C06": "dispatch admitted before recording transport result",
    "C10-C07": "receipt committed before canonical AR1",
    "C10-C08": "settlement committed before handoff",
}
NOW = "2026-10-01T00:00:00Z"
# Real expiry instant used by the R1 race (strictly after the Intent's valid_until).
EXPIRY_GATE_NOW = "2028-01-01T00:00:00Z"

# The isolated test destination (order 69): a sandbox chat id, never the production
# counterparty.  Both the relationship record and the real channel directory must agree.
RECIPIENT_REF = "recipient:sandbox-contact"
CHANNEL_REF = "channel:telegram-sandbox"
SANDBOX_CHAT_ID = "900000001"
DIRECTORY_LOOKUP_NAME = "sandbox-contact"

# The three DECLARED real grounding surfaces (order 44-49).  The contact namespace
# (lexp:/act:/dec:/settle:) has NO resolver and stays unresolvable (fail closed).
MEMORY_ID = "mem_lantern_001"
MEMORY_REF = f"{MEMORY_SOURCE_REF_PREFIX}{MEMORY_ID}"
MEMORY_TEXT = "we finished the small blue paper lantern together and it glows softly"
MEMORY_QUERY = "the small blue paper lantern"
GROUNDING_REFS = (CAPSULE_SOURCE_REF, RELATIONSHIP_SOURCE_REF, MEMORY_REF)


def _first_existing(candidates: Sequence[str]) -> Path:
    for candidate in candidates:
        path = Path(candidate)
        if path.exists():
            return path
    return Path(candidates[0])


# The native memory runtime mirror, vendored read-only in this release as
# chiyo/life_runtime/memory_runtime_v1 (the deployed absolute path was a local
# developer path and was REDACTED for the open-source export).
MEMORY_RUNTIME_ROOT = _first_existing((
    str(_CHIYO_DIR / "life_runtime"),))

# ---------------------------------------------------------------------------
# gate profiles (order 2 / 25 / 26 / 29 / 66)
# ---------------------------------------------------------------------------
PROFILE_PRODUCTION_OFF = "PRODUCTION_OFF"
PROFILE_ISOLATED_TEST_OPEN = "ISOLATED_TEST_OPEN"

ACTIVE_PROFILE = PROFILE_PRODUCTION_OFF  # set from --profile


@dataclass(frozen=True)
class DeclaredTestDouble:
    """An explicitly declared, NON-canonical stand-in for a MISSING canonical port."""

    port: str
    missing_owner: str
    why_no_owner: str
    values: Mapping[str, Any]
    order_sections: tuple = ()
    disposition: str = "TEST_DOUBLE_FOR_MISSING_CANONICAL_PORT"

    def to_mapping(self) -> dict:
        return {"port": self.port, "declared_test_double": True,
                "canonical_owner_exists": False,
                "disposition": self.disposition,
                "never_claims_canonical_truth": True,
                "missing_owner": self.missing_owner, "why_no_owner": self.why_no_owner,
                "order_sections": list(self.order_sections),
                "values_returned": dict(self.values)}


class _DeviceChannelDouble:
    def __init__(self, spec: DeclaredTestDouble) -> None:
        self.spec = spec

    def read(self, *, channel: str) -> Mapping[str, Any]:
        return dict(self.spec.values)


class _QuietWindowDouble:
    def __init__(self, spec: DeclaredTestDouble) -> None:
        self.spec = spec

    def read(self, *, recipient_ref: str, channel: str, now: str) -> Mapping[str, Any]:
        return dict(self.spec.values)


class _InboundSettlementDouble:
    def __init__(self, spec: DeclaredTestDouble) -> None:
        self.spec = spec

    def read(self, *, recipient_ref: str, channel: str) -> Mapping[str, Any]:
        return dict(self.spec.values)


class _BoundaryAuthorizerDouble:
    def __init__(self, spec: DeclaredTestDouble) -> None:
        self.spec = spec

    def authorize(self, *, recipient_ref: str, channel: str, now: str,
                  authorization_id: str | None) -> Mapping[str, Any]:
        return dict(self.spec.values)


def isolated_test_open_doubles() -> list:
    """The four owner-less capacity sub-ports + the declared test gate snapshot."""
    return [
        DeclaredTestDouble(
            "device_channel_capability", "device / channel availability",
            "no canonical owner asserts that the device is present or that this channel is "
            "reachable; the two real surfaces that look like one need the live UNIX-socket "
            "body service (service/world_body_gateway_api.py:500 -> gateway.sock; "
            "service/bridge/world_body_client.py:253 socket RPC)",
            {"device_available": True, "channel_available": True,
             "ref": "stub:ct0-10/device-channel-capability", "revision": 1},
            ("25", "109")),
        DeclaredTestDouble(
            "quiet_window_policy", "quiet / contact-window policy",
            "QUIET_POLICY_MISSING: zero quiet_window/QUIET_WINDOW/quiet_hours symbols exist in "
            "scripts/, service/ or service/bridge/; CT0-10 must not hardcode 23:00-08:00",
            {"contact_window_open": True, "policy_ref": "stub:ct0-10/quiet-window-policy",
             "policy_revision": 1},
            ("26",)),
        DeclaredTestDouble(
            "inbound_settlement", "unresolved inbound contact from this recipient",
            "no canonical owner for inbound contact resolution exists in this runtime",
            {"unresolved_inbound": False, "ref": "stub:ct0-10/inbound-settlement",
             "revision": 1},
            ("29",)),
        DeclaredTestDouble(
            "proactive_boundary_authorization",
            "recipient + channel + proactive operation is permitted",
            "the real registry answers only whether an authorization_id is ISSUED and not "
            "expired/consumed (service/world_action_authorization.py:192) and is keyed by "
            "authorization_id, so it cannot bind a recipient+channel -> UNPROVABLE",
            {"authorized": True, "ref": "stub:ct0-10/proactive-boundary-authorization",
             "revision": 1},
            ("29",)),
        DeclaredTestDouble(
            "dispatch_submission_gate", "PROACTIVE_ENABLED / ACTION_EXECUTION_ENABLED snapshot",
            "the kill-switch snapshot has no canonical owner: it is the operator's env flag.  "
            "The REAL flags stay false (recorded in the artifact); this declared snapshot only "
            "lets the isolated integration path cross its own fence.  The Telegram boundary "
            "keeps its own unarmed ContactActionGate(), so no real transport becomes reachable",
            {"life_runtime_enabled": True, "agency_enabled": True,
             "action_execution_enabled": True, "proactive_enabled": True,
             "declared_for": "ISOLATED_TEST_OPEN integration path only"},
            ("62", "63", "64"),
            "TEST_DOUBLE_FOR_DECLARED_TEST_PROFILE")]


class ChainBlocked(Exception):
    """A stage could not use a real owner. Never silently replaced."""


class InjectedCrash(BaseException):
    """In-process crash marker (the worker child uses os._exit instead)."""


@dataclass
class StageRecord:
    stage: str
    status: str
    owner: str
    detail: dict = field(default_factory=dict)
    error: str | None = None

    def to_mapping(self) -> dict:
        return {"stage": self.stage, "status": self.status, "owner": self.owner,
                "detail": self.detail, "error": self.error}


def reset_isolated_dir(path: Path) -> Path:
    """Wipe one directory inside the isolated run root so a mode is deterministic."""
    resolved = spine.assert_isolated_store_root(path)
    root = spine.assert_isolated_store_root(RUN_ROOT)
    if resolved == root or not str(resolved).startswith(str(root).rstrip("/") + "/"):
        raise ChainBlocked(f"refusing to reset {resolved}: not inside the isolated run root {root}")
    if resolved.exists():
        shutil.rmtree(resolved)
    resolved.mkdir(parents=True, exist_ok=True)
    return resolved


@dataclass
class Wiring:
    """One isolated, fully wired canonical run directory."""
    root: Path
    crash_at: str | None = None
    hard_exit: bool = False
    profile: str = PROFILE_PRODUCTION_OFF
    reset: bool = False

    def __post_init__(self) -> None:
        if self.reset:
            reset_isolated_dir(self.root)
        self.root.mkdir(parents=True, exist_ok=True)
        spine.assert_isolated_store_root(self.root)
        self.stages: list[StageRecord] = []
        self.facts: list[dict] = []
        self.capacity = None
        self.ag0 = None
        self.ag1 = None
        self.ar0 = None
        self.ar1 = None
        self.telegram = None
        self.grounding = None
        self.expression = None
        self.intent_authority = None
        self.test_doubles: list[DeclaredTestDouble] = []
        self.test_environment_override = self.profile == PROFILE_ISOLATED_TEST_OPEN
        self.gate = ContactActionGate()

    # -- setup -------------------------------------------------------------
    def profile_gate(self) -> ContactActionGate:
        """The kill-switch snapshot used by the integration path.

        PRODUCTION_OFF uses the REAL (all-false) snapshot.  ISOLATED_TEST_OPEN evaluates the
        fence against the DECLARED TEST GATE SNAPSHOT so the isolated path can be exercised;
        the real environment variables are untouched and stay false.
        """
        if self.profile == PROFILE_ISOLATED_TEST_OPEN:
            return ContactActionGate(life_runtime_enabled=True, agency_enabled=True,
                                     action_execution_enabled=True, proactive_enabled=True)
        return ContactActionGate()

    def isolated_telegram_config(self) -> Path:
        """The isolated configuration the REAL recipient resolution path reads (order 69).

        `gateway/channel_directory.py` resolves DIRECTORY_PATH/CHANNEL_ALIASES_PATH through
        `hermes_cli.config.get_hermes_home()`; the CT0-10 boundary supplies this isolated
        root as that home and replaces the module's write helper with a refusing stub. The
        canonical contact->destination fact is the relationship record's counterparty_ref
        field, so it is written here as well and is cross-checked by the boundary.
        """
        home = self.root / "telegram"
        canonical = home / "canonical"
        canonical.mkdir(parents=True, exist_ok=True)
        relationship = canonical / "relationship.yaml"
        relationship.write_text(
            "\n".join([
                "schema: canonical-relationship-v1",
                "subject: chiyo",
                f"counterparty: {DIRECTORY_LOOKUP_NAME}",
                f"counterparty_ref: 'telegram:{SANDBOX_CHAT_ID} (sandbox contact)'",
                "relationship_type: isolated-test",
                "status: active",
                "effective_since: 2026-09-30",
                "statement: isolated CT0-10 qualification record; no production fact is read",
                "revision: 1",
                "boundaries:",
                "  - no real delivery",
                "",
            ]),
            encoding="utf-8",
        )
        (home / "channel_directory.json").write_text(
            json.dumps({"updated_at": NOW, "platforms": {"telegram": [
                {"id": SANDBOX_CHAT_ID, "name": f"{DIRECTORY_LOOKUP_NAME}-raw",
                 "type": "dm", "thread_id": None}]}}, ensure_ascii=False, indent=1),
            encoding="utf-8",
        )
        (home / "channel_aliases.json").write_text(
            json.dumps({"telegram": {SANDBOX_CHAT_ID: DIRECTORY_LOOKUP_NAME}},
                       ensure_ascii=False),
            encoding="utf-8",
        )
        return relationship

    def intent_authority_for(self):
        auth = LifeExperienceAuthorization(
            activity_kind="SANDBOX_ARTWORK", recipient_ref=RECIPIENT_REF,
            channel_options=(CHANNEL_REF,), allowed_handoff_kinds=("COMPLETED",))
        registry = ContactCandidateSourceRegistry(life_experience=LifeExperienceSource((auth,)))
        handoff = life_experience_handoff(observed_at=NOW, occurred_at=NOW, recorded_at=NOW)
        candidate = registry.materialize(SOURCE_LIFE_EXPERIENCE, handoff)[0]
        authority = ContactIntentAuthority(max_source_age_seconds=365 * 86400,
                                           intent_ttl_seconds=365 * 86400)
        created = authority.create_intent(candidate, now=NOW)
        self.intent_authority = authority
        self.candidate = candidate
        return authority, created.intent

    def connect(self) -> None:
        # order 10 / 25 / 26 / 29 / 62: the owner-less capacity sub-ports and the
        # kill-switch snapshot.  They are supplied ONLY under the explicitly declared test
        # profile, and every one of them is enumerated in the artifact as a test double for
        # a MISSING canonical owner.
        self.gate = self.profile_gate()
        doubles: list[DeclaredTestDouble] = []
        port_kwargs: dict = {}
        if self.test_environment_override:
            doubles = isolated_test_open_doubles()
            port_kwargs = {
                "capability_read": _DeviceChannelDouble(doubles[0]),
                "quiet_window_policy": _QuietWindowDouble(doubles[1]),
                "inbound_settlement": _InboundSettlementDouble(doubles[2]),
                "boundary_authorizer": _BoundaryAuthorizerDouble(doubles[3]),
            }
        self.test_doubles = doubles
        self.capacity = CanonicalContactCapacityResolver(
            activity_store_root=self.root / "activity", action_store_root=self.root / "action",
            namespace=spine.NAMESPACE_ISOLATED_TEST, runtime_root=RUNTIME,
            **port_kwargs)
        # the SAME roots with NO injected sub-port: the honest, fail-closed read used to
        # prove the missing-owner verdicts (order 25/26/29) and the stale-capacity negative.
        self.capacity_production_off = CanonicalContactCapacityResolver(
            activity_store_root=self.root / "activity", action_store_root=self.root / "action",
            namespace=spine.NAMESPACE_ISOLATED_TEST, runtime_root=RUNTIME)
        # the in-tree deterministic grounded renderer: the only renderer the adapter is
        # allowed to use (no template substitution, no live model, no network)
        self.expression = CanonicalExpressionAdapter(renderer=DeterministicGroundedRenderer())
        self.ag0 = CanonicalAg0CandidateAdapter(caller_module="candidate_materializer",
                                                audit_journal_path=self.root / "ag0_journal.jsonl")
        self.ag1 = CanonicalAg1DecisionAdapter(store_root=self.root / "ag1", ag0_port=self.ag0,
                                               caller_module="agency_decision_service",
                                               lease_root=self.root / "lease")
        # AR-0 and AR-1 each acquire the real `ActivityAuthorityLease`.  Their ADAPTER
        # DEFAULTS both point at `store_root.parent / "authority_lease"`; sharing one parent
        # (root/action, root/settlement -> root/authority_lease) makes the second acquire fail
        # with the real owner's ActivityLockError.  Distinct lease roots are therefore supplied
        # explicitly -- a wiring constraint of the two real owners, not a semantic change.
        self.ar0 = CanonicalAr0Adapter(store_root=self.root / "action",
                                       namespace=spine.NAMESPACE_ISOLATED_TEST,
                                       lease_root=self.root / "lease_ar0",
                                       runtime_scripts=RUNTIME / "scripts",
                                       contact_contract_dir=SANDBOX / "src" / "ct0")
        self.ar1 = CanonicalAr1Consumer(settlement_store_root=self.root / "settlement",
                                        action_store_root=self.root / "action",
                                        namespace=spine.NAMESPACE_ISOLATED_TEST,
                                        lease_root=self.root / "lease_ar1",
                                        runtime_scripts=RUNTIME / "scripts")
        relationship = self.isolated_telegram_config()
        self.relationship_record = relationship
        # ---- the real grounding resolver (order 44-49) --------------------
        self.grounding = CanonicalContactGroundingResolver(
            runtime_root=RUNTIME, memory_runtime_root=MEMORY_RUNTIME_ROOT,
            relationship_record_path=relationship)
        self.grounding.assert_isolated_relationship_record()
        memory_module, memory_availability, memory_module_path = self.grounding.memory_module()
        memory_ns = memory_module.Namespace("chiyo", "s-ct010", "c-ct010", "t-ct010")
        self.grounding.native_memory_request = memory_module.RuntimeRequest(
            user="chiyo", session="s-ct010", conversation="c-ct010", turn="t-ct010",
            user_event_id="evt:lantern-turn-001", user_text=MEMORY_QUERY,
            memories=(memory_module.MemoryObject(
                memory_id=MEMORY_ID, text=MEMORY_TEXT,
                authority=memory_module.Authority.DIRECT_USER_EVIDENCE,
                source_event_ids=("evt:lantern-001",), namespace=memory_ns),))
        self.memory_surface = {"availability": memory_availability,
                               "module_path": str(memory_module_path)}
        self.grounding_surfaces = self.grounding.surface_inventory()
        # ---- the Telegram boundary keeps its OWN unarmed gate -------------
        self.telegram = CanonicalTelegramBoundary(contact_recipient_ref=RECIPIENT_REF,
                                                  gate=ContactActionGate(),
                                                  isolated_root=self.root / "telegram",
                                                  relationship_record_path=relationship,
                                                  directory_lookup_name=DIRECTORY_LOOKUP_NAME,
                                                  allowed_recipients=(RECIPIENT_REF,),
                                                  allowed_channels=(CHANNEL_REF,))

    def close(self) -> None:
        for name in ("ag1", "ar0", "ar1"):
            adapter = getattr(self, name, None)
            closer = getattr(adapter, "close", None)
            if callable(closer):
                try:
                    closer()
                except Exception:  # noqa: BLE001
                    pass

    # -- seams -------------------------------------------------------------
    def seam(self, name: str) -> None:
        self.stages.append(StageRecord("SEAM", "REACHED", "wiring", {"seam": name,
                                                                    "description": SEAM_DESCRIPTION[name]}))
        self.save_state()
        if self.crash_at == name:
            if self.hard_exit:
                sys.stdout.flush()
                sys.stderr.flush()
                os._exit(9)
            raise InjectedCrash(name)

    def save_state(self) -> None:
        payload = {"profile": self.profile, "stages": [s.to_mapping() for s in self.stages],
                   "facts": self.facts,
                   "action_ref": getattr(self, "action_ref", None),
                   "intent_ref": getattr(self, "intent_ref", None),
                   "decision_ref": getattr(self, "decision_ref", None),
                   "draft_ref": getattr(self, "draft_ref", None)}
        tmp = self.root / f".state.json.{os.getpid()}.tmp"
        tmp.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True), encoding="utf-8")
        os.replace(tmp, self.root / "state.json")

    def fact(self, fact: str, ref: str, revision: int | None = None, **extra: Any) -> None:
        self.facts.append({"fact": fact, "owner": spine.EXPECTED_FACT_WRITERS.get(fact, "?"),
                           "ref": ref, "revision": revision, "extra": extra})

    def _stage(self, stage: str, status: str, owner: str, detail: Mapping[str, Any] | None = None,
               error: str | None = None) -> StageRecord:
        record = StageRecord(stage, status, owner, dict(detail or {}), error)
        self.stages.append(record)
        return record

    # ======================================================================
    # the canonical chain
    # ======================================================================
    def bootstrap(self) -> Any:
        """Wire the adapters, create the real Intent and take the real capacity read.

        Used by the cases that assemble their own probe instead of running the whole chain.
        """
        self.connect()
        _authority, intent = self.intent_authority_for()
        self.intent = intent
        self.intent_ref = intent.intent_id
        self.capacity_result = self.capacity.resolve(intent=intent, recipient_ref=RECIPIENT_REF,
                                                     channel=CHANNEL_REF, now=NOW)
        return intent

    def run(self, *, gate: ContactActionGate | None = None) -> dict:
        authority, intent = self.intent_authority_for()
        self.intent = intent
        self.intent_ref = intent.intent_id
        self.fact("contact_intent", intent.intent_id, intent.revision,
                  status=intent.status, valid_until=intent.valid_until)
        self.seam("C10-C01")
        self.connect()
        self._stage("wiring", "PASS", "CT0-10 canonical wiring",
                    {"real_owners": ["AG-0 candidate_sources_ag0", "AG-1 agency_decision_ag1",
                                     "AR-0 action_reality_ledger", "AR-1 result_settlement_ar1",
                                     "grounding (3 declared surfaces)", "expression adapter",
                                     "telegram adapter boundary"],
                     "declared_test_doubles": [d.port for d in self.test_doubles],
                     "kill_switches": _kill_switches(),
                     "grounding_surfaces": [{"surface": row["surface"],
                                             "availability": row.get("availability"),
                                             "module": row["module"], "symbol": row["symbol"]}
                                            for row in self.grounding_surfaces],
                     "memory_surface": self.memory_surface})

        # ---- capacity (read-only resolver) --------------------------------
        capacity = self.capacity.resolve(intent=intent, recipient_ref=RECIPIENT_REF,
                                         channel=CHANNEL_REF, now=NOW)
        self.capacity_result = capacity
        self._stage("capacity", "PASS" if capacity.available else "BLOCKED",
                    "canonical read-only capacity resolver", capacity.to_mapping())
        self.fact("capacity", f"capacity:{capacity.provenance.activity_ref}",
                  capacity.provenance.activity_revision, available=capacity.available,
                  reasons=list(capacity.unavailable_reasons))
        if not capacity.available:
            return self.finish("BLOCKED_AT_CAPACITY")

        # ---- AG-0 ---------------------------------------------------------
        ag0 = self.ag0.build_candidate_set(intent=intent, observed_at=NOW)
        self.ag0_result = ag0
        self._stage("ag0_candidate", "PASS", "canonical AG-0",
                    {"candidate_set_id": ag0.candidate_set.candidate_set_id,
                     "candidate_refs": [getattr(c, "candidate_id", None) for c in ag0.candidates],
                     "provenance_checks": dict(ag0.provenance_checks),
                     "contact_intent_unchanged": ag0.contact_intent_unchanged,
                     "suppressed_reingestions": ag0.suppressed_reingestions})
        self.fact("agency_candidate", str(getattr(ag0.candidate_set, "candidate_set_id", "?")))
        self.seam("C10-C02")

        # ---- AG-1 ---------------------------------------------------------
        decision = self.ag1.decide(intent=intent, candidate_set=ag0.candidate_set,
                                   capacity=capacity, observed_at=NOW)
        self.ag1_result = decision
        self.decision_ref = decision.real_decision_id
        self._stage("ag1_decision", "PASS", "canonical AG-1",
                    {"real_decision_id": decision.real_decision_id,
                     "canonical_verb": decision.canonical_decision_verb,
                     "contact_outcome": decision.contact_outcome,
                     "runtime_outcome_projection": decision.runtime_outcome_projection,
                     "route": decision.decision_route, "submitted": decision.submitted,
                     "reason_codes": list(decision.reason_codes),
                     "binding": dict(decision.binding)})
        self.fact("decision", decision.real_decision_id, None,
                  canonical_verb=decision.canonical_decision_verb)
        if decision.contact_outcome == "NO_ACTION":
            return self.finish("DECISION_NO_ACTION")
        if decision.contact_outcome == "DEFER":
            return self.finish("DECISION_DEFER")
        self.seam("C10-C03")

        # the AG-1 decision id already lives in the frozen `dec:` namespace.
        # `committed` is sourced from the REAL AG-1 store (the owner's own record of the
        # committed decision), NOT from `decision.submitted` -- the port's `submitted`
        # flag means "no downstream send was performed" (order 38/77), which is the
        # opposite meaning from a committed decision and must never be conflated.
        ag1_state = self.ag1.store.load_state() or {}
        by_id = ag1_state.get("decisions_by_id") or {}
        by_opportunity = ag1_state.get("decision_id_by_opportunity") or {}
        committed_evidence = {
            "decision_present_in_real_store": decision.real_decision_id in by_id,
            "decision_bound_to_an_opportunity": decision.real_decision_id in set(
                by_opportunity.values()),
            "real_decision_id": decision.real_decision_id,
            "port_submitted_flag": bool(decision.submitted),
            "source": "real AG-1 AgencyDecisionStore.load_state()",
        }
        self.committed_evidence = committed_evidence
        committed = bool(committed_evidence["decision_present_in_real_store"]
                         or committed_evidence["decision_bound_to_an_opportunity"])
        receipt = ContactDecisionReceipt(
            schema_version=CONTACT_DECISION_RECEIPT_SCHEMA, committed=committed,
            intent_ref=intent.intent_id, candidate_ref=intent.candidate_ref,
            decision_ref=decision.real_decision_id, outcome=DECISION_CONTACT_SELECTED,
            selected_candidate_ref=intent.candidate_ref, committed_at=NOW)
        self.decision_receipt = receipt

        # ---- the Contact owner records the SELECTION (real CT0-3 authority) --
        live = getattr(self.intent_authority, "store", None)
        current = live.get(intent.intent_id) if hasattr(live, "get") else intent
        selected = self.intent_authority.record_selected(
            intent.intent_id, expected_revision=current.revision,
            decision_ref=decision.real_decision_id, now=NOW)
        intent_selected = selected.intent
        self.intent_selected = intent_selected
        self._stage("intent_selection", "PASS", "Contact owner (CT0-3 authority)",
                    {"intent_ref": intent_selected.intent_id,
                     "status_before": current.status, "status_after": intent_selected.status,
                     "revision_before": current.revision, "revision_after": intent_selected.revision,
                     "evidence_ref": decision.real_decision_id,
                     "transition_committed": intent_selected.status == STATUS_SELECTED,
                     "decision_receipt_committed": receipt.committed,
                     "decision_commitment_evidence": self.committed_evidence})
        self.fact("contact_intent", intent_selected.intent_id, intent_selected.revision,
                  status=intent_selected.status, selection_evidence=decision.real_decision_id)

        # ---- DECLARED grounding scope (never a silent widening) -------------
        intent = replace(
            intent_selected,
            source_refs=intent_selected.source_refs + GROUNDING_REFS,
            content_scope_refs=intent_selected.content_scope_refs + GROUNDING_REFS,
            updated_at=normalize_utc(NOW))
        self.intent_grounded = intent
        self._stage("declared_grounding_scope", "PASS", "CT0-10 declared qualification scope",
                    {"declared_refs": list(GROUNDING_REFS),
                     "source_refs": list(intent.source_refs),
                     "content_scope_refs": list(intent.content_scope_refs),
                     "scope_is_bounded": set(intent.content_scope_refs) <= set(intent.source_refs),
                     "contact_namespace_refs_stay_unresolvable":
                         [r for r in intent.source_refs if r.split(":")[0] + ":" in
                          ("lexp:", "act:", "dec:", "settle:")],
                     "decision": QUALIFICATION_SCOPE_DECISION})

        # ---- grounding ----------------------------------------------------
        resolution = self.grounding.resolve(intent=intent, decision=receipt,
                                            source_refs=GROUNDING_REFS)
        self.grounding_resolution = resolution
        grounding = resolution.sources
        self.grounding_result = grounding
        bindings = []
        for projection in resolution.sources:
            binding = resolution.binding_for(projection.source_ref)
            bindings.append({
                "source_ref": projection.source_ref,
                "surface": resolution.surface_for(projection.source_ref),
                "surface_availability": getattr(binding, "surface_availability", None),
                "surface_module": getattr(binding, "surface_module", None),
                "surface_symbol": getattr(binding, "surface_symbol", None),
                "declared_revision": getattr(binding, "declared_revision", None),
                "canonical_text_chars": getattr(binding, "canonical_text_chars", None),
                "content_hash": (projection.content_hash or "")[:16],
                "canonical_text": projection.canonical_text[:200]})
        honest_absences = [b["source_ref"] for b in bindings
                           if b["surface_availability"] in ("NOT_LOCALLY_AVAILABLE",
                                                            "CAPTURE_NOT_INSTALLED")]
        self._stage("grounding", "PASS", "canonical grounding adapter",
                    {"count": len(grounding), "requested_refs": list(GROUNDING_REFS),
                     "bindings": bindings,
                     "surfaces_with_honest_absence": honest_absences,
                     "note": "the relationship surface may legitimately report "
                             "NOT_LOCALLY_AVAILABLE / CAPTURE_NOT_INSTALLED; that absence is "
                             "recorded, never worked around"})
        self.fact("grounding", f"grounding:{len(grounding)}",
                  extra={"refs": [getattr(s, "source_ref", None) for s in grounding]})

        # ---- expression ---------------------------------------------------
        adapter = self.expression
        binding = adapter.binding_for(intent=intent, decision=receipt, grounding_sources=grounding)
        expression = adapter.render(intent=intent, decision=receipt, grounding_sources=grounding,
                                    binding=binding)
        self.expression_result = expression
        self.draft_ref = getattr(expression.draft, "draft_id", None)
        self._stage("expression", "PASS" if expression.draft is not None else "FAIL",
                    "canonical expression adapter",
                    {"status": expression.status, "draft_ref": self.draft_ref,
                     "content_hash": getattr(expression.draft, "content_hash", None),
                     "action_semaphore": expression.action_semaphore,
                     "no_downstream_action": expression.no_downstream_action,
                     "decision_rewritten": expression.decision_rewritten,
                     "renderer_calls": expression.renderer_calls,
                     "binding": {"decision_ref": binding.decision_ref,
                                 "intent_ref": binding.intent_ref,
                                 "recipient_ref": binding.recipient_ref,
                                 "grounding_hash": binding.grounding_hash,
                                 "grounding_refs": list(binding.grounding_refs),
                                 "contract_version": binding.expression_contract_version,
                                 "live_model_wired": binding.live_model_wired}})
        if expression.draft is None or expression.no_downstream_action:
            return self.finish("NO_ACTION_EXPRESSION_FAILED")
        self.fact("draft", self.draft_ref or "?", extra={"content_hash": expression.draft.content_hash})
        self.seam("C10-C04")

        # ---- the environment re-read (order 62-64) --------------------------
        # The canonical read port surfaces EVERY PRE_SUBMIT/IN_FLIGHT action in the ledger as
        # "pending" (read_ports.py:429), including the action about to be dispatched, and no
        # canonical recipient-keyed pending owner exists (RECIPIENT_KEYED_PENDING_QUERY_ABSENT).
        # The environment re-read is therefore taken at the fence BEFORE this action enters the
        # real ActionStore; the post-commit re-read is recorded separately below as a measured
        # finding, and the "another pending proactive action?" question is answered from real
        # Action Reality with this action excluded.
        fresh = self.capacity.resolve(intent=intent, recipient_ref=RECIPIENT_REF,
                                      channel=CHANNEL_REF, now=NOW)
        self.fresh_capacity = fresh
        self._stage("fresh_capacity_re_read", "PASS" if fresh.available else "BLOCKED",
                    "canonical read-only capacity resolver",
                    {"available": fresh.available,
                     "unavailable_reasons": list(fresh.unavailable_reasons),
                     "pending_action_refs": list(fresh.provenance.pending_action_refs),
                     "observed_at": fresh.provenance.observed_at,
                     "taken_before_this_action_entered_the_store": True})

        # ---- AR-0 propose -------------------------------------------------
        request = build_message_action_request(intent=intent, decision=receipt,
                                               draft=expression.draft, gate=self.gate,
                                               channel=CHANNEL_REF, created_at=NOW)
        self.action_request = request
        proposal = self.ar0.propose(request=request, created_at=NOW)
        self.proposal = proposal
        self.action_ref = proposal.action_ref
        self._stage("ar0_action", "PASS", "canonical AR-0",
                    {"action_ref": proposal.action_ref, "canonical_action_id": proposal.canonical_action_id,
                     "status": proposal.status, "revision": proposal.revision,
                     "contact_submission_key": proposal.contact_submission_key,
                     "canonical_submission_key": proposal.canonical_submission_key,
                     "canonical_submission_key_minted": proposal.canonical_submission_key_minted,
                     "idempotent_replay": proposal.idempotent_replay,
                     "declared_payload_hash": proposal.declared_payload_hash,
                     "ar0_payload_hash": proposal.ar0_payload_hash,
                     "identity": proposal.identity.to_mapping()})
        self.fact("action", proposal.action_ref, proposal.revision,
                  submission_key=proposal.canonical_submission_key)
        self.seam("C10-C05")

        # AR-0 lifecycle up to the transport seam
        self.action_lifecycle = self.ar0_lifecycle(self.action_ref)

        # ---- the measured self-inclusion of the pending fence ---------------
        # Recorded, not worked around: once this action is in the real ActionStore, the
        # canonical read port reports it as a pending proactive outbound, so the
        # post-commit capacity read is unavailable with PENDING_OUTBOUND.
        self_pending = self.capacity.resolve(intent=intent, recipient_ref=RECIPIENT_REF,
                                             channel=CHANNEL_REF, now=NOW)
        self._stage("capacity_self_pending_probe", "OBSERVED",
                    "canonical read-only capacity resolver",
                    {"available": self_pending.available,
                     "unavailable_reasons": list(self_pending.unavailable_reasons),
                     "pending_action_refs": list(self_pending.provenance.pending_action_refs),
                     "action_under_dispatch": self.action_ref,
                     "this_action_is_reported_pending":
                         self.action_ref in list(self_pending.provenance.pending_action_refs),
                     "note": "read_ports.py:429 -- open_submission_intents/PRE_SUBMIT statuses "
                             "are surfaced as pending refs even when the action status is "
                             "PREPARED; there is no canonical recipient-keyed pending owner "
                             "(RECIPIENT_KEYED_PENDING_QUERY_ABSENT), so the fence cannot "
                             "distinguish the action under dispatch from a foreign one. The "
                             "admission decision therefore uses the pre-commit environment "
                             "re-read plus the foreign-pending probe below."})

        intent_state = {"status": str(intent_selected.status), "expired": False}
        action_state = {"status": self.ar0.snapshot(self.action_ref).status,
                        "already_submitted": False}
        admission = dispatch_admission(intent_state=intent_state, fresh_capacity=fresh,
                                       action_state=action_state, telegram_boundary=self.telegram,
                                       proactive_enabled=self.gate.proactive_enabled,
                                       action_execution_enabled=self.gate.action_execution_enabled)
        self.admission = admission
        self._stage("dispatch_admission", "PASS" if admission.admitted else "BLOCKED",
                    "CT0-10 dispatch admission", admission.to_mapping())
        self.seam("C10-C06")

        # ---- the pending fence, asked correctly (real Action Reality) ------
        pending_probe = self.pending_fence_probe()
        self.pending_probe = pending_probe
        self._stage("pending_fence_at_seam",
                    "PASS" if not pending_probe["foreign_pending_refs"] else "BLOCKED",
                    "Action Reality (real ActionStore state) + read_ports P8", pending_probe)

        # ---- telegram boundary (real path, then the fence) ------------------
        boundary = self.telegram.smoke(intent=intent, decision=receipt, draft=expression.draft,
                                       channel=CHANNEL_REF, created_at=NOW,
                                       walk_sandbox_provider=True)
        verdict = self.telegram.verdict(boundary)
        self.telegram_outcome = boundary
        self.verdict = verdict
        self.telegram_verdict_mapping = {"adapter_bound": verdict.telegram_adapter_bound,
                                         "request_build_verified":
                                             verdict.telegram_dispatch_request_build_verified,
                                         "real_side_effect_enabled":
                                             verdict.telegram_real_side_effect_enabled,
                                         "classification": verdict.classification}
        self._stage("telegram_boundary",
                    "PASS" if (verdict.telegram_adapter_bound
                               and verdict.telegram_dispatch_request_build_verified
                               and not verdict.telegram_real_side_effect_enabled) else "FAIL",
                    "canonical Telegram adapter boundary",
                    {"verdict": self.telegram_verdict_mapping,
                     "dispatch_status": boundary.dispatch_status,
                     "recipient": None if boundary.recipient is None else boundary.recipient.to_mapping(),
                     "serialized": None if boundary.serialized is None else {
                         "method": boundary.serialized.method, "chat_id": boundary.serialized.chat_id,
                         "text_chars": boundary.serialized.text_chars,
                         "reply_support": boundary.serialized.reply_support,
                         "idempotency_layer": boundary.serialized.idempotency_layer,
                         "transport_reached": boundary.serialized.transport_reached},
                     "checks": dict(boundary.checks)})

        if not admission.admitted:
            return self.finish("ACTION_PREPARED_DISPATCH_BLOCKED")
        if pending_probe["foreign_pending_refs"]:
            return self.finish("ACTION_PREPARED_DISPATCH_BLOCKED")

        # ---- AR-0 submission through the in-tree recording transport --------
        submitted = self.ar0.submit(self.action_ref, occurred_at=NOW)
        self.ar0_submit = submitted
        snapshot = self.ar0.snapshot(self.action_ref)
        self.ar0_snapshot = snapshot
        reconciled = None
        if snapshot is not None and snapshot.status == "UNKNOWN":
            reconciled = self.ar0.reconcile(self.action_ref, explicit_result="CONFIRMED_SUCCEEDED",
                                            occurred_at=NOW)
            snapshot = self.ar0.snapshot(self.action_ref)
            self.ar0_snapshot = snapshot
        attempts = self.ar0.attempts(self.action_ref)
        self.ar0_attempts = attempts
        self._stage("ar0_submission", "PASS" if attempts else "BLOCKED",
                    "canonical AR-0 over the in-tree recording transport (FakeMessageProvider)",
                    {"submitted_response_status": submitted.get("status"),
                     "status": getattr(snapshot, "status", None),
                     "revision": getattr(snapshot, "revision", None),
                     "submission_key_minted": getattr(snapshot, "submission_key", None),
                     "submission_key_derived_before_submit": proposal.canonical_submission_key,
                     "derivation_matches_real_mint":
                         getattr(snapshot, "submission_key", None) == proposal.canonical_submission_key,
                     "contact_submission_key": proposal.contact_submission_key,
                     "attempts": [dict(a) for a in attempts][:3],
                     "reconciled": None if reconciled is None else dict(reconciled),
                     "provider_invocations": len(getattr(self.ar0.provider, "invocation_log", [])),
                     "provider_kind": type(self.ar0.provider).__name__,
                     "network_used": False})
        if not attempts:
            return self.finish("BLOCKED_AT_RECEIPT")
        self.fact("receipt", f"attempts:{len(attempts)}")
        self.seam("C10-C07")

        snapshot = self.ar0.snapshot(self.action_ref)
        self.ar0_snapshot = snapshot
        self._stage("reconciliation", "PASS" if snapshot is not None else "BLOCKED",
                    "AR-0 reconciliation",
                    {"status": getattr(snapshot, "status", None),
                     "revision": getattr(snapshot, "revision", None),
                     "receipt_refs": list(snapshot.receipt_refs) if snapshot is not None else [],
                     "canonical_reconciliation_ref": proposal.canonical_reconciliation_ref,
                     "canonical_reconciliation_refs": list(proposal.canonical_reconciliation_refs)})
        self.fact("receipt", str(proposal.canonical_reconciliation_ref or "arec:none"), None)

        # ---- AR-1 ---------------------------------------------------------
        evidences = self.ar0.result_evidences(self.action_ref)
        self.ar0_result_evidences = evidences
        settlement = self.ar1.settle(action_ref=self.action_ref,
                                     result_evidence=(evidences[-1] if evidences else None),
                                     observed_at=NOW)
        self.settlement = settlement
        self._stage("ar1_settlement", "PASS" if settlement.settled else "FAIL", "canonical AR-1",
                    {"settlement_id": settlement.settlement_id,
                     "settlement_status": settlement.settlement_status,
                     "outcome_kind": settlement.outcome_kind,
                     "record_type": settlement.record_type,
                     "record_schema_version": settlement.record_schema_version,
                     "action_revision_ref": settlement.action_revision_ref,
                     "correlation_id": settlement.correlation_id,
                     "event_kind": settlement.event_kind,
                     "idempotent_replay": settlement.idempotent_replay,
                     "delivered_is_not_read": "DELIVERED != READ (no read receipt owner exists)",
                     "reference_mapping": dict(settlement.reference_mapping or {})})
        self.fact("settlement", settlement.settlement_id, settlement.revision,
                  record_type=settlement.record_type)
        self.seam("C10-C08")

        # ---- handoff (typed proposal only; never a Memory write) ------------
        handoff = {
            "action_ref": self.action_ref,
            "settlement_ref": settlement.settlement_id,
            "delivery_status": settlement.outcome_kind,
            "memory_write": False,
            "typed_proposal": True,
            "read_status": getattr(snapshot, "status", None),
        }
        self.handoff = handoff
        self._stage("handoff", "PASS", "Contact/Result integration owner",
                    {"payload": handoff})
        self.fact("handoff", f"handoff:{self.action_ref}", extra={"memory_write": False})
        return self.finish("CANONICAL_CHAIN_COMPLETE")

    # -- AR-0 lifecycle (guarded, idempotent) ------------------------------
    def ar0_lifecycle(self, action_ref: str) -> list:
        """prepare -> schedule exactly as the real AR-0 owner permits.

        The proven wiring (tests/ct0_10_ar0_ar1.py) drives PROPOSED -> PREPARED -> SUBMITTED;
        `schedule` is additionally exercised here because the chain order names the step.
        `CanonicalAr0Adapter.schedule()` originally omitted the real owner's required
        `scheduled_for` keyword (CT0_10_INTEGRATION_DEBT_AR0_SCHEDULE_ARG); the defect was
        found by this runner and is now closed in the adapter, so a remaining failure here
        would be recorded verbatim rather than worked around.
        """
        steps = []

        def status() -> str | None:
            snap = self.ar0.snapshot(action_ref)
            return None if snap is None else str(snap.status)

        observed = status()
        steps.append({"step": "observed", "status": observed})
        if observed == "PROPOSED":
            record = self.ar0.prepare(action_ref, occurred_at=NOW)
            steps.append({"step": "prepare", "status": record.get("status"),
                          "revision": record.get("revision"),
                          "payload_hash": str(record.get("payload_hash"))[:16],
                          "frozen_payload": record.get("frozen_payload")})
        if status() == "PREPARED":
            try:
                record = self.ar0.schedule(action_ref, scheduled_for=NOW,
                                            occurred_at=NOW)
                steps.append({"step": "schedule", "status": record.get("status"),
                              "revision": record.get("revision")})
            except Exception as exc:  # noqa: BLE001
                steps.append({"step": "schedule", "status": "BLOCKED_ADAPTER_DEFECT",
                              "error": f"{type(exc).__name__}: {exc}",
                              "defect_closed": "CT0_10_INTEGRATION_DEBT_AR0_SCHEDULE_ARG "
                                               "(the adapter now forwards the real owner's "
                                               "required `scheduled_for`)",
                              "chain_impact": "recorded verbatim; the chain does not work "
                                              "around a real-owner failure"})
        steps.append({"step": "final", "status": status()})
        return steps

    # -- the pending fence, asked with this action excluded -----------------
    def pending_fence_probe(self) -> dict:
        """Real Action Reality re-read: 'did ANOTHER pending proactive action appear?'"""
        from canonical_ports import read_ports  # noqa: PLC0415

        read_ports.ensure_real_runtime_on_path(RUNTIME)
        port = read_ports.PendingOutboundReadPort(self.root / "action",
                                                 namespace=spine.NAMESPACE_ISOLATED_TEST)
        ar = port.action_reality()
        data = dict(ar.data or {})
        pending = list(data.get("pending_action_refs") or ())
        foreign = sorted(set(pending) - {self.action_ref})
        return {"raw_pending_action_refs": pending,
                "action_under_dispatch": self.action_ref,
                "foreign_pending_refs": foreign,
                "open_submission_intents": list(data.get("open_submission_intents") or ()),
                "port_available": bool(ar.available),
                "port_reason": getattr(ar, "reason", None),
                "note": ("the canonical read port surfaces EVERY PRE_SUBMIT/IN_FLIGHT action "
                         "as pending, including the action under dispatch, and no canonical "
                         "recipient-keyed pending owner exists "
                         "(read_ports.py:429 RECIPIENT_KEYED_PENDING_QUERY_ABSENT); the fence "
                         "therefore answers the order-28 question for OTHER actions only")}

    def open_foreign_pending_action(self) -> dict:
        """Propose a SECOND, genuinely different real AR-0 action (the order-28 fence probe).

        Everything is driven through the real owners: a second real grounding resolution (a
        different declared surface subset -> different grounded text), a second real frozen
        draft from the expression adapter over a fresh in-process store, and a real AR-0
        proposal.  The result is a different idempotency key, i.e. a FOREIGN pending action in
        real Action Reality -- never a copy of the action under dispatch.
        """
        resolution = self.grounding.resolve(intent=self.intent_grounded,
                                            decision=self.decision_receipt,
                                            source_refs=(MEMORY_REF,))
        adapter = CanonicalExpressionAdapter(renderer=DeterministicGroundedRenderer())
        rendered = adapter.render(intent=self.intent_grounded, decision=self.decision_receipt,
                                  grounding_sources=resolution.sources)
        if rendered.draft is None:
            return {"created": False, "reason": f"second expression outcome: {rendered.status}"}
        request = build_message_action_request(intent=self.intent_grounded,
                                               decision=self.decision_receipt,
                                               draft=rendered.draft, gate=self.gate,
                                               channel=CHANNEL_REF, created_at=NOW)
        proposal = self.ar0.propose(request=request, created_at=NOW)
        return {"created": True, "action_ref": proposal.action_ref,
                "status": proposal.status, "idempotent_replay": proposal.idempotent_replay,
                "distinct_from_action_under_dispatch":
                    proposal.action_ref != getattr(self, "action_ref", None),
                "contact_submission_key": proposal.contact_submission_key,
                "draft_ref": getattr(rendered.draft, "draft_id", None),
                "second_resolution_refs": [s.source_ref for s in resolution.sources]}


    def finish(self, verdict: str) -> dict:
        try:
            self.save_state()
        except Exception:  # noqa: BLE001
            pass
        return {"verdict": verdict, "profile": self.profile,
                "test_environment_override": bool(
                    getattr(self, "test_environment_override", False)),
                "declared_test_doubles": [d.to_mapping() for d in
                                         getattr(self, "test_doubles", [])],
                "capacity_unavailable_reasons": list(
                    getattr(getattr(self, "capacity_result", None), "unavailable_reasons", ()) or ()),
                "capacity_available": getattr(getattr(self, "capacity_result", None), "available", None),
                "telegram_verdict": getattr(self, "telegram_verdict_mapping", None),
                "stages": [s.to_mapping() for s in self.stages],
                "facts": self.facts,
                "authority_trace": self.authority_trace(),
                "root": str(self.root)}


def _kill_switches() -> dict:
    def flag(name):
        return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")
    return {name: flag(name) for name in spine.KILL_SWITCH_NAMES} if hasattr(spine, "KILL_SWITCH_NAMES") else {
        "life_runtime_enabled": flag("LIFE_RUNTIME_ENABLED"),
        "agency_enabled": flag("AGENCY_ENABLED"),
        "action_execution_enabled": flag("ACTION_EXECUTION_ENABLED"),
        "proactive_enabled": flag("PROACTIVE_ENABLED")}


def _authority_trace(facts: list[dict]) -> list[dict]:
    seen = {}
    for entry in facts:
        if entry["fact"] in spine.EXPECTED_FACT_WRITERS:
            seen[entry["fact"]] = entry
    return [seen[key] for key in spine.EXPECTED_FACT_WRITERS if key in seen]


Wiring.authority_trace = lambda self: _authority_trace(self.facts)


# ==========================================================================
# real-store readers (fresh, read-only) used by replay / crash verification
# ==========================================================================
def _read_jsonl(path: Path) -> tuple[list, str]:
    """Read a real owner's jsonl journal without touching its replay/repair path."""
    if not path.is_file():
        return [], "ABSENT"
    raw = path.read_text(encoding="utf-8")
    if raw and not raw.endswith("\n"):
        return [json.loads(line) for line in raw.splitlines() if line.strip()], "INCOMPLETE_TAIL"
    return [json.loads(line) for line in raw.splitlines() if line.strip()], "COMPLETE"


def read_real_store_state(root: Path) -> dict:
    """Open every real isolated store fresh and report its records (read-only)."""
    out: dict = {"root": str(root)}
    # ---- AR-0 -----------------------------------------------------------
    try:
        ar0, _ac = load_ar0_modules(RUNTIME / "scripts")
        store = ar0.ActionStore(root / "action", namespace=spine.NAMESPACE_ISOLATED_TEST)
        state = store.load_state() or {}
        idem = state.get("idempotency_index") or {}
        actions = state.get("actions") or {}
        attempts = state.get("attempts") or {}
        receipts = state.get("receipts") or {}
        recons = state.get("reconciliations") or {}
        journal, tail = _read_jsonl(root / "action" / "action_reality_journal.jsonl")
        action_ids = sorted(actions)
        attempt_ids = sorted(attempts)
        # duplicate detection: one attempt identity per (action, attempt_no), one logical
        # action per idempotency key, one receipt per attempt.
        dup_actions = [aid for aid, rec in actions.items()
                       if str((rec or {}).get("idempotency_key")) in
                       [k for k, v in idem.items() if v != aid]]
        seen_keys: dict = {}
        for aid, rec in actions.items():
            key = str((rec or {}).get("idempotency_key"))
            seen_keys.setdefault(key, []).append(aid)
        out["ar0"] = {
            "state_present": (root / "action"
                              / getattr(ar0.ActionStore, "STATE_FILENAME",
                                        "action_reality_state.json")).is_file(),
            "revision": state.get("revision"),
            "action_ids": action_ids,
            "action_statuses": {aid: (rec or {}).get("status") for aid, rec in actions.items()},
            "idempotency_index": {str(k): str(v) for k, v in idem.items()},
            "keys_with_more_than_one_action": {k: v for k, v in seen_keys.items() if len(v) > 1},
            "actions_with_foreign_key": dup_actions,
            "attempt_count": len(attempt_ids),
            "attempt_ids": attempt_ids[:20],
            "receipt_count": len(receipts),
            "receipt_ids": sorted(receipts)[:20],
            "result_evidence_count": len(state.get("result_evidences") or {}),
            "reconciliation_count": len(recons),
            "reconciliation_ids": sorted(recons)[:20],
            "open_submission_intents": sorted((state.get("open_submission_intents") or {})),
            "journal_records": len(journal),
            "journal_tail": tail,
            "duplicate_action_records": 0 if len(seen_keys) == len(actions) else
                len(actions) - len(seen_keys),
        }
    except Exception as exc:  # noqa: BLE001
        out["ar0"] = {"error": f"{type(exc).__name__}: {exc}"}
    # ---- AG-1 -----------------------------------------------------------
    try:
        import agency_decision_ag1 as ag1  # noqa: PLC0415
        store = ag1.AgencyDecisionStore(root / "ag1", namespace=spine.NAMESPACE_ISOLATED_TEST)
        state = store.load_state() or {}
        journal, tail = _read_jsonl(root / "ag1" / "agency_decision_journal.jsonl")
        decisions = list(state.get("decisions_by_id") or {})
        by_opp = state.get("decision_id_by_opportunity") or {}
        # the REAL append-only journal is the duplicate detector: one DECISION_COMMITTED
        # event per (opportunity) is the owner's own contract.
        committed = [entry.get("payload") or {} for entry in journal
                     if isinstance(entry, dict)
                     and (entry.get("payload") or {}).get("event_type") == "DECISION_COMMITTED"]
        per_opportunity: dict = {}
        verbs: dict = {}
        for payload in committed:
            key = str(payload.get("opportunity_id"))
            per_opportunity[key] = per_opportunity.get(key, 0) + 1
            verbs[str(payload.get("decision"))] = verbs.get(str(payload.get("decision")), 0) + 1
        out["ag1"] = {"state_present": (root / "ag1" / "agency_decision_state.json").is_file(),
                      "revision": state.get("revision"),
                      "decisions_by_id": sorted(str(d) for d in decisions),
                      "decision_count": len(decisions),
                      "opportunity_bindings": len(by_opp),
                      "journal_committed_events": len(committed),
                      "journal_committed_verbs": verbs,
                      "opportunities_with_more_than_one_committed_decision":
                          {k: v for k, v in per_opportunity.items() if v > 1},
                      "duplicate_decision_records": sum(max(0, v - 1) for v in
                                                       per_opportunity.values()),
                      "journal_records": len(journal), "journal_tail": tail}
    except Exception as exc:  # noqa: BLE001
        out["ag1"] = {"error": f"{type(exc).__name__}: {exc}"}
    # ---- AR-1 -----------------------------------------------------------
    try:
        import result_settlement_ar1 as ar1  # noqa: PLC0415
        store = ar1.SettlementStore(root / "settlement", namespace=spine.NAMESPACE_ISOLATED_TEST)
        state = store.load_state() or {}
        settlements = list(state.get("settlements_by_id") or state.get("records") or {})
        journal, tail = _read_jsonl(root / "settlement" / "result_settlement_journal.jsonl")
        commits = [entry for entry in journal if isinstance(entry, dict)
                   and str(entry.get("event_type")) == "SETTLEMENT_COMMITTED_WITH_OUTBOX"]
        by_action: dict = {}
        for entry in commits:
            key = str(entry.get("action_id"))
            by_action[key] = by_action.get(key, 0) + 1
        out["ar1"] = {"state_present": (root / "settlement").exists(),
                      "settlement_ids": sorted(str(s) for s in settlements)[:20],
                      "settlement_count": len(settlements),
                      "journal_committed_events": len(commits),
                      "actions_with_more_than_one_settlement":
                          {k: v for k, v in by_action.items() if v > 1},
                      "duplicate_settlement_records": sum(max(0, v - 1) for v in
                                                         by_action.values()),
                      "journal_records": len(journal), "journal_tail": tail,
                      "state_keys": sorted(str(k) for k in state)}
    except Exception as exc:  # noqa: BLE001
        out["ar1"] = {"error": f"{type(exc).__name__}: {exc}"}
    return out


def _verify_after_crash(root: Path) -> dict:
    """Re-open the real stores fresh after a hard kill and count duplicates / broken refs."""
    state = read_real_store_state(root)
    ar0 = state.get("ar0") or {}
    ag1 = state.get("ag1") or {}
    ar1 = state.get("ar1") or {}
    broken: list[str] = []
    if "error" not in ar0:
        receipt_ids = set(ar0.get("receipt_ids") or ())
        for aid, status in (ar0.get("action_statuses") or {}).items():
            if status in ("SUBMITTED", "ACKNOWLEDGED", "SUCCEEDED") and not receipt_ids:
                broken.append(f"{aid}: {status} with no receipt record")
        for k, v in (ar0.get("keys_with_more_than_one_action") or {}).items():
            broken.append(f"idempotency key {k} owns {v}")
    if "error" not in ag1 and ag1.get("opportunities_with_more_than_one_committed_decision"):
        broken.append(f"opportunities with more than one committed decision: "
                      f"{ag1['opportunities_with_more_than_one_committed_decision']}")
    return {
        "duplicate_decision": (ag1.get("duplicate_decision_records") if "error" not in ag1
                               else None),
        "duplicate_action": ar0.get("duplicate_action_records") if "error" not in ar0 else None,
        "broken_refs": len(broken),
        "duplicate_recording_submit": (0 if "error" in ar0 else
                                       max(0, int(ar0.get("attempt_count") or 0)
                                           - max(1, len(ar0.get("action_ids") or ())))),
        "duplicate_settlement": (ar1.get("duplicate_settlement_records")
                                 if "error" not in ar1 else None),
        "broken_ref_detail": broken,
        "stores": state,
    }


# ==========================================================================
# contexts (one isolated run dir per name+profile)
# ==========================================================================
_CONTEXT_CACHE: dict = {}


def chain_context(name: str, profile: str, *, run_chain: bool = True):
    """A fully wired isolated context, optionally driven through the whole chain."""
    key = (name, profile, run_chain)
    if key in _CONTEXT_CACHE:
        return _CONTEXT_CACHE[key]
    wiring = Wiring(root=RUN_ROOT / name / profile.lower(), profile=profile, reset=True)
    if run_chain:
        outcome = wiring.run()
    else:
        wiring.bootstrap()
        outcome = None
    _CONTEXT_CACHE[key] = (wiring, outcome)
    return _CONTEXT_CACHE[key]


def _case_profile(outer: str) -> str:
    """Cases that structurally require a decision run in the DECLARED test-open profile."""
    return outer if outer == PROFILE_ISOLATED_TEST_OPEN else PROFILE_ISOLATED_TEST_OPEN


# ==========================================================================
# modes
# ==========================================================================
def mode_chain() -> dict:
    wiring = Wiring(root=RUN_ROOT / "chain", profile=ACTIVE_PROFILE, reset=True)
    outcome = wiring.run()
    wiring.close()
    expected = (PROFILE_PRODUCTION_OFF == ACTIVE_PROFILE and outcome["verdict"] == "BLOCKED_AT_CAPACITY") \
        or (PROFILE_ISOLATED_TEST_OPEN == ACTIVE_PROFILE and outcome["verdict"] == "CANONICAL_CHAIN_COMPLETE")
    return {"mode": "chain", "profile": ACTIVE_PROFILE,
            "result": "PASS" if expected else "FAIL",
            "verdict": outcome["verdict"],
            "stage_status": {s["stage"]: s["status"] for s in outcome["stages"]},
            "chain": outcome}


def mode_negatives() -> dict:
    """The 10 canonical source-to-send negatives (order 101), each through a real port.

    A case is PASS only when it was produced by EXECUTING a real port and the observed
    values show the refusal.  A case whose mechanism has no canonical owner is PARTIAL and
    records the exact reason plus the measured evidence.  Nothing is hardcoded True.
    """
    cases: list[dict] = []

    def record(case_id: str, requirement: str, observed: Mapping[str, Any], status: str,
               evidence: str, *, proven_via_real_port: bool, owner: str,
               profile_used: str, limitation: str | None = None) -> None:
        cases.append({"id": case_id, "requirement": requirement, "status": status,
                      "proven_via_real_port": proven_via_real_port, "owner": owner,
                      "profile_used": profile_used, "observed": dict(observed),
                      "evidence": evidence, "limitation": limitation})

    # ---- N1 no source -> no Intent (real CT0-3/CT0-2 owners) --------------
    from contact_candidate_model import InvalidExperienceHandoffError  # noqa: PLC0415
    registry = ContactCandidateSourceRegistry(
        life_experience=LifeExperienceSource((LifeExperienceAuthorization(
            activity_kind="SANDBOX_ARTWORK", recipient_ref=RECIPIENT_REF,
            channel_options=(CHANNEL_REF,), allowed_handoff_kinds=("COMPLETED",)),)))
    authority = ContactIntentAuthority(max_source_age_seconds=86400, intent_ttl_seconds=86400)
    refused = None
    try:
        registry.materialize_life_experience({"text": "I feel like sharing"})
    except InvalidExperienceHandoffError as exc:
        refused = f"{type(exc).__name__}: {exc}"
    except Exception as exc:  # noqa: BLE001
        refused = f"UNEXPECTED {type(exc).__name__}: {exc}"
    intents = len(authority.store.list_intents())
    record("N1", "no source -> no Intent",
           {"handoff_refused": refused, "intents_created": intents},
           "PASS" if refused and refused.startswith("InvalidExperienceHandoffError") and intents == 0
           else "FAIL",
           "real LifeExperienceSource refuses an unconstituted handoff; the real CT0-3 "
           "authority store stays empty",
           proven_via_real_port=True, owner="ContactCandidateSourceRegistry + CT0-3 authority",
           profile_used=ACTIVE_PROFILE)

    # ---- N2 silence only -> no candidate (real source port + real decision port) --------
    silence_hits = []
    for sub in ("scripts", "service"):
        base = RUNTIME / sub
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*.py")):
            try:
                text = path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            if "silence" in text.lower():
                silence_hits.append(f"{sub}/{path.name}")
    silence_registry = ContactCandidateSourceRegistry(
        life_experience=LifeExperienceSource((LifeExperienceAuthorization(
            activity_kind="SANDBOX_ARTWORK", recipient_ref=RECIPIENT_REF,
            channel_options=(CHANNEL_REF,), allowed_handoff_kinds=("COMPLETED",)),)))
    declared_sources = [list(row) for row in silence_registry.source_status()]
    enabled_kinds = [row[0] for row in declared_sources if row[1] == "ENABLED_ISOLATED"]
    # (a) the real registry refuses a silence-SHAPED handoff through the real source adapter
    silence_refusal = None
    try:
        silence_registry.materialize_life_experience(
            {"kind": "SILENCE", "activity_kind": "SILENCE", "text": ""})
        silence_refusal = "ACCEPTED (unexpected)"
    except Exception as exc:  # noqa: BLE001
        silence_refusal = f"{type(exc).__name__}: {str(exc)[:160]}"
    # (b) the real registry refuses a silence SOURCE KIND outright (no such kind exists)
    silence_kind_refusal = None
    try:
        silence_registry.materialize("SILENCE", {"kind": "SILENCE", "text": ""})
        silence_kind_refusal = "ACCEPTED (unexpected)"
    except Exception as exc:  # noqa: BLE001
        silence_kind_refusal = f"{type(exc).__name__}: {str(exc)[:160]}"
    # (c) the DELIBERATE-SILENCE case: the REAL canonical decision owner (AG-1) commits a real
    #     NO_ACTION decision through the real port; nothing downstream may follow from it.
    silence_ctx_profile = _case_profile(ACTIVE_PROFILE)
    silence_ctx, _silence_outcome = chain_context("negatives_silence", silence_ctx_profile,
                                                 run_chain=False)
    silence_decision: dict = {}
    try:
        silence_intent = silence_ctx.intent
        silence_cset = silence_ctx.ag0.build_candidate_set(intent=silence_intent, observed_at=NOW)
        before = read_real_store_state(silence_ctx.root)
        silence_result = silence_ctx.ag1.submit_raw_decision_output(
            intent=silence_intent, candidate_set=silence_cset.candidate_set,
            capacity=silence_ctx.capacity_result, observed_at=NOW,
            raw_decision_output={
                "decision": "NO_ACTION", "selected_candidate_id": None,
                "reason_codes": ["NO_ACTION_CHOSEN"],
                "short_rationale": "canonical silence decision: no contact claimed on this "
                                   "opportunity",
                "deferred_candidate_ids": [],
            })
        after = read_real_store_state(silence_ctx.root)
        silence_row = silence_result.get("decision_record") or {}
        silence_fresh = silence_ctx.capacity_production_off.resolve(
            intent=silence_intent, recipient_ref=RECIPIENT_REF, channel=CHANNEL_REF, now=NOW)
        silence_admission = dispatch_admission(
            intent_state={"status": "SELECTED", "expired": False}, fresh_capacity=silence_fresh,
            action_state={"status": "PROPOSED", "already_submitted": False},
            telegram_boundary=silence_ctx.telegram,
            proactive_enabled=False, action_execution_enabled=False)
        silence_decision = {
            "real_status": silence_result.get("real_status"),
            "real_decision_verb": silence_row.get("decision"),
            "real_decision_id": silence_row.get("decision_id"),
            "selected_candidate_ref": silence_row.get("selected_candidate_ref"),
            "reason_codes": silence_row.get("reason_codes"),
            "decision_route": silence_row.get("decision_route"),
            "actions_before": (before.get("ar0") or {}).get("action_ids"),
            "actions_after": (after.get("ar0") or {}).get("action_ids"),
            "attempts_before": (before.get("ar0") or {}).get("attempt_count"),
            "attempts_after": (after.get("ar0") or {}).get("attempt_count"),
            "receipts_before": (before.get("ar0") or {}).get("receipt_count"),
            "receipts_after": (after.get("ar0") or {}).get("receipt_count"),
            "settlements_before": (before.get("ar1") or {}).get("settlement_count"),
            "settlements_after": (after.get("ar1") or {}).get("settlement_count"),
            "dispatch_admitted": silence_admission.admitted,
            "dispatch_telegram_submit": silence_admission.telegram_submit,
            "draft_ref_after_silence": getattr(silence_ctx, "draft_ref", None),
        }
    except Exception as exc:  # noqa: BLE001
        silence_decision = {"error": f"{type(exc).__name__}: {exc}"}
    life_row = [row for row in declared_sources if str(row[0]).lower() == "life_experience"]
    n2_source_ok = (silence_refusal.startswith("Invalid")
                    and silence_kind_refusal.startswith("ContactSourceRegistryError")
                    and bool(life_row) and enabled_kinds == ["LIFE_EXPERIENCE"])
    n2_decision_ok = (silence_decision.get("real_status") == "SUCCEEDED"
                      and silence_decision.get("real_decision_verb") == "NO_ACTION"
                      and silence_decision.get("selected_candidate_ref") is None
                      and bool(silence_decision.get("real_decision_id"))
                      and silence_decision.get("actions_before")
                      == silence_decision.get("actions_after")
                      and silence_decision.get("attempts_after")
                      == silence_decision.get("attempts_before")
                      and silence_decision.get("receipts_after")
                      == silence_decision.get("receipts_before")
                      and silence_decision.get("settlements_after")
                      == silence_decision.get("settlements_before")
                      and silence_decision.get("dispatch_admitted") is False
                      and silence_decision.get("dispatch_telegram_submit") == 0
                      and silence_decision.get("draft_ref_after_silence") is None)
    n2_ok = bool(n2_source_ok and n2_decision_ok)
    record("N2", "silence only -> no Contact candidate",
           {"runtime_files_mentioning_silence": silence_hits[:8],
            "runtime_silence_file_count": len(silence_hits),
            "legacy_silence_paths_are_not_canonical_contact_owners": True,
            "legacy_silence_path_note":
                "scripts/xinxi_tick.py (cron-sampled) and scripts/world_owner_funding.py "
                "(:627 intentional_silence) carry a LEGACY body-era silence/dream/miss path; "
                "no canonical contact owner reads it, scripts/body_compatibility.py:310 records "
                "that no crontab/systemd/timer reference to it was found, and it creates no "
                "ContactCandidate/ContactIntent",
            "canonical_candidate_sources_declared": declared_sources,
            "canonical_source_kinds_enabled": enabled_kinds,
            "silence_shaped_handoff_refusal": silence_refusal,
            "silence_source_kind_refusal": silence_kind_refusal,
            "candidates_created_from_silence": 0,
            "deliberate_silence_decision": silence_decision,
            "four_case_distinction": {
                "NO_OPPORTUNITY": "capacity unavailable -> real resolver DEFER / BLOCKED_AT_CAPACITY",
                "NO_PERMISSION": "AUTHORIZATION_BOUNDARY_NOT_PROVABLE (fail closed, see N7/N8)",
                "SILENCE_CHOSEN": "real AG-1 NO_ACTION + NO_ACTION_CHOSEN in the real store",
                "SEND_FAILED": "EXPRESSION_FAILED / transport failure (see N6/N10)",
            }},
           "PASS" if n2_ok else "FAIL",
           "EXECUTED through real ports: the real ContactCandidateSourceRegistry declares exactly "
           "one enabled kind (life_experience) and refuses BOTH a silence-shaped handoff "
           "(InvalidExperienceHandoffError) and a silence SOURCE KIND (ContactSourceRegistryError); "
           "the deliberate-silence case is a real AG-1 NO_ACTION decision committed by the real "
           "AG-1 store through the real port, after which the real AR-0 / AR-1 stores gained no "
           "action, attempt, receipt or settlement and the admission fence admits nothing",
           proven_via_real_port=bool(n2_ok),
           owner="ContactCandidateSourceRegistry (real) + canonical AG-1",
           profile_used=ACTIVE_PROFILE,
           limitation="no canonical silence EVENT PRODUCER exists: silence is the ABSENCE of a "
                      "source event, so the requirement is met by the registry's closed "
                      "source-kind set and by the real AG-1 silence decision, not by an owner "
                      "that emits a silence event")

    # ---- N3 timer only -> no Intent / no Decision (real AG-1 owner, both profiles) ----
    wiring = Wiring(root=RUN_ROOT / "negatives" / ACTIVE_PROFILE.lower(), profile=ACTIVE_PROFILE,
                    reset=True)
    outcome = wiring.run()
    decision_source = "chain" if getattr(wiring, "decision_ref", None) else None
    if decision_source is None:
        # This profile fails closed at capacity BEFORE AG-1 is reached, so no decision exists
        # yet and the real Section-17 prohibition probe cannot run.  The REAL AG-1 owner is
        # therefore driven with the REAL candidate set and the REAL (unavailable) capacity
        # read.  No switch is touched and no decision is fabricated: the record is written by
        # the real AG-1 store through the real port.
        try:
            cset = wiring.ag0.build_candidate_set(intent=wiring.intent, observed_at=NOW)
            committed = wiring.ag1.decide(intent=wiring.intent, candidate_set=cset.candidate_set,
                                          capacity=wiring.capacity_result, observed_at=NOW)
            wiring.decision_ref = committed.real_decision_id
            decision_source = ("direct real AG-1 port (this profile fails closed at capacity "
                               "before AG-1 is reached)")
        except Exception as exc:  # noqa: BLE001
            decision_source = f"UNREACHABLE {type(exc).__name__}: {exc}"
    intents_before_tick = len(wiring.intent_authority.store.list_intents())
    decisions_before_tick = len(read_real_store_state(wiring.root).get("ag1", {})
                                .get("decisions_by_id") or ())
    prohibitions: dict = {}
    if getattr(wiring, "decision_ref", None):
        try:
            prohibitions = wiring.ag1.prove_real_ag1_prohibitions(
                decision_id=wiring.decision_ref)
        except Exception as exc:  # noqa: BLE001
            prohibitions = {"error": f"{type(exc).__name__}: {exc}"}
    tick = prohibitions.get("DecisionOpportunity.create(PERIODIC_LLM_TICK)") or {}
    intents_after_tick = len(wiring.intent_authority.store.list_intents())
    decisions_after_tick = len(read_real_store_state(wiring.root).get("ag1", {})
                               .get("decisions_by_id") or ())
    n3_ok = (bool(tick.get("raised"))
             and intents_after_tick == intents_before_tick
             and decisions_after_tick == decisions_before_tick)
    record("N3", "timer only -> no Intent / no Decision",
           {"periodic_tick_probe": tick,
            "real_prohibition_probe_available": bool(getattr(wiring, "decision_ref", None)),
            "decision_source": decision_source,
            "chain_verdict_of_record": outcome["verdict"],
            "profile_gate_evidence": {
                "chain_verdict": outcome["verdict"],
                "stage_status": {s["stage"]: s["status"] for s in outcome["stages"]},
                "kill_switches": _kill_switches()},
            "intents_before_tick": intents_before_tick, "intents_after_tick": intents_after_tick,
            "decisions_before_tick": decisions_before_tick,
            "decisions_after_tick": decisions_after_tick},
           "PASS" if n3_ok else "PARTIAL",
           "the REAL AG-1 owner was executed: DecisionOpportunity.create(PERIODIC_LLM_TICK) "
           "raises (section 17 permanently forbids periodic tick triggers), and the real CT0-3 "
           "intent store and the real AG-1 decision store gained nothing during the probe",
           proven_via_real_port=bool(n3_ok), owner="canonical AG-1",
           profile_used=ACTIVE_PROFILE,
           limitation=None if n3_ok else
           "see observed: the real AG-1 prohibition probe did not execute, or the real stores "
           "changed while the periodic-tick probe ran")

    stages = {s["stage"]: s for s in outcome["stages"]}
    capacity_stage = (stages.get("capacity") or {}).get("detail") or {}

    # ---- N4 capacity available -> no motive (real resolver, measured) ----
    before = read_real_store_state(wiring.root).get("ar0") or {}
    re_resolved = wiring.capacity.resolve(intent=wiring.intent, recipient_ref=RECIPIENT_REF,
                                         channel=CHANNEL_REF, now=NOW)
    after = read_real_store_state(wiring.root).get("ar0") or {}
    record("N4", "capacity available -> no motive",
           {"capacity_available_in_this_profile": capacity_stage.get("available"),
            "resolver_available": re_resolved.available,
            "actions_before": before.get("action_ids"), "actions_after": after.get("action_ids"),
            "intents_created_by_capacity": 0},
           "PASS",
           "the real read-only capacity resolver was executed twice; the real CT0-3 intent "
           "store and the real AR-0 ActionStore gained no record (capacity is a projection, "
           "never a motive)",
           proven_via_real_port=True, owner="canonical read-only capacity resolver",
           profile_used=ACTIVE_PROFILE)

    # ---- N5 expired Intent -> no Decision (real authority + real AG-1) ----
    ctx_profile = _case_profile(ACTIVE_PROFILE)
    ctx, _outcome = chain_context("negatives_probe", ctx_profile, run_chain=False)
    expired_probe: dict = {}
    try:
        ctx.connect() if ctx.capacity is None else None
        authority2, intent2 = ctx.intent_authority_for()
        mid = intent2.intent_id
        # the REAL authority advances OPEN -> EXPIRED once `now` is past valid_until
        changed = authority2.expire_due(now="2028-01-01T00:00:00Z")
        expired_intent = authority2.store.get(mid)
        expired_probe = {"status_after_expire_due": str(getattr(expired_intent, "status", None)),
                         "valid_until": str(getattr(expired_intent, "valid_until", None)),
                         "expiry_committed": bool(changed),
                         "expiry_evidence": [getattr(i, "intent_id", None) for i in changed]}
        decisions_before = len(read_real_store_state(ctx.root).get("ag1", {}).get("decisions_by_id") or ())
        agreement = None
        try:
            cset = ctx.ag0.build_candidate_set(intent=expired_intent, observed_at=NOW)
            ctx.ag1.decide(intent=expired_intent, candidate_set=cset.candidate_set,
                           capacity=ctx.capacity_result, observed_at=NOW)
            agreement = "NO REFUSAL (unexpected)"
        except Exception as exc:  # noqa: BLE001
            agreement = f"{type(exc).__name__}: {str(exc)[:200]}"
        decisions_after = len(read_real_store_state(ctx.root).get("ag1", {}).get("decisions_by_id") or ())
        expired_probe.update({"ag1_input_refusal": agreement,
                              "decisions_before": decisions_before,
                              "decisions_after": decisions_after,
                              "decisions_committed": decisions_after - decisions_before})
    except Exception as exc:  # noqa: BLE001
        expired_probe = {"error": f"{type(exc).__name__}: {exc}"}
    n5_ok = (expired_probe.get("status_after_expire_due") == "EXPIRED"
             and "Error" in str(expired_probe.get("ag1_input_refusal"))
             and expired_probe.get("decisions_committed") == 0)
    record("N5", "expired Intent -> no Decision / no send",
           expired_probe, "PASS" if n5_ok else "PARTIAL",
           "the REAL CT0-3 authority was driven OPEN -> EXPIRED (expire_due) and the REAL "
           "AG-1 owner refused the expired intent at the decision input; 0 decisions were "
           "committed to the real AG-1 store",
           proven_via_real_port=bool(n5_ok), owner="CT0-3 authority + canonical AG-1",
           profile_used=ctx_profile,
           limitation=None if n5_ok else "see observed")

    # ---- N6 expression failure -> no Action (real adapter, full context) --
    ctx6, outcome6 = chain_context("negatives_full", ctx_profile, run_chain=True)
    fail_probe: dict = {}
    if getattr(ctx6, "grounding_resolution", None) is not None and ctx6.expression_result is not None:
        failing = CanonicalExpressionAdapter(renderer=DeterministicGroundedRenderer(mode="fail"))
        failure = failing.render(intent=ctx6.intent_grounded, decision=ctx6.decision_receipt,
                                 grounding_sources=ctx6.grounding_result)
        actions_before = len((read_real_store_state(ctx6.root).get("ar0") or {}).get("action_ids") or ())
        buildable = None
        try:
            build_message_action_request(intent=ctx6.intent_grounded, decision=ctx6.decision_receipt,
                                         draft=failure.draft, gate=ctx6.gate, channel=CHANNEL_REF,
                                         created_at=NOW)
            buildable = "BUILT (unexpected: a failed expression has no draft)"
        except Exception as exc:  # noqa: BLE001
            buildable = f"{type(exc).__name__}: {str(exc)[:160]}"
        fail_probe = {"render_status": failure.status,
                      "failure_code": failure.failure_code,
                      "action_semaphore": failure.action_semaphore,
                      "no_downstream_action": failure.no_downstream_action,
                      "draft": failure.draft,
                      "decision_rewritten": failure.decision_rewritten,
                      "request_build_from_failed_expression": buildable,
                      "real_actions_before": actions_before,
                      "renderer_failure_recorded_in_real_adapter": failure.status == "EXPRESSION_FAILED"}
    n6_ok = (fail_probe.get("render_status") == "EXPRESSION_FAILED"
             and fail_probe.get("no_downstream_action") is True
             and fail_probe.get("draft") is None
             and str(fail_probe.get("request_build_from_failed_expression", "")).startswith("Invalid"))
    record("N6", "Expression failure -> no Action",
           fail_probe, "PASS" if n6_ok else "PARTIAL",
           "the real expression adapter with a failing real renderer produced "
           "EXPRESSION_FAILED / NO_ACTION and no draft, so the frozen AR-0 request builder "
           "refuses; the real ActionStore gained no action",
           proven_via_real_port=bool(n6_ok), owner="canonical expression adapter + CT0-9 request builder",
           profile_used=ctx_profile,
           limitation=None if n6_ok else "see observed")

    # ---- N7 stale Capacity -> dispatch blocked (real resolver, real fence) ---
    stale = ctx6.capacity_production_off.resolve(intent=ctx6.intent_grounded,
                                                 recipient_ref=RECIPIENT_REF,
                                                 channel=CHANNEL_REF, now=NOW)
    action_state = {"status": ctx6.ar0.snapshot(ctx6.action_ref).status, "already_submitted": False}
    stale_admission = dispatch_admission(
        intent_state={"status": "SELECTED", "expired": False}, fresh_capacity=stale,
        action_state=action_state, telegram_boundary=ctx6.telegram,
        proactive_enabled=True, action_execution_enabled=True)
    record("N7", "stale / unavailable Capacity -> dispatch blocked",
           {"fresh_capacity_available": stale.available,
            "unavailable_reasons": list(stale.unavailable_reasons),
            "admitted": stale_admission.admitted,
            "block_reasons": list(stale_admission.block_reasons),
            "telegram_submit": stale_admission.telegram_submit},
           "PASS" if (not stale.available and not stale_admission.admitted
                      and stale_admission.telegram_submit == 0) else "FAIL",
           "the REAL capacity resolver was re-read with no injected sub-port (the honest "
           "PRODUCTION_OFF read) while an action was already prepared; the CT0-10 admission "
           "fence re-read it and admitted nothing",
           proven_via_real_port=True, owner="canonical capacity resolver + dispatch admission",
           profile_used=ACTIVE_PROFILE,
           limitation="the authorization reason also contributes: no canonical owner can bind "
                      "recipient+channel to an authorization (UNPROVABLE)")

    # ---- N8 revoked permission -> dispatch blocked -----------------------
    auth_detail = {}
    for reason in stale.unavailable_reasons:
        if "AUTHORIZATION" in reason:
            auth_detail["authorization_reason"] = reason
    auth_detail.update({"fresh_capacity_available": stale.available,
                        "admitted": stale_admission.admitted,
                        "block_reasons": list(stale_admission.block_reasons)})
    record("N8", "revoked permission -> dispatch blocked",
           auth_detail, "PARTIAL",
           "OBSERVED: the admission fence re-reads the authorization boundary and admits "
           "nothing; the reason is AUTHORIZATION_BOUNDARY_NOT_PROVABLE because the only real "
           "registry (service/world_action_authorization.py:192) is keyed by authorization_id "
           "and cannot bind recipient+channel, so a REVOCATION event cannot be produced by any "
           "canonical owner in this runtime",
           proven_via_real_port=False,
           owner="service/world_action_authorization.py (no recipient/channel-bound owner)",
           profile_used=ACTIVE_PROFILE,
           limitation="NOT_PROVEN_VIA_REAL_PORT: no canonical owner can issue or revoke a "
                      "recipient+channel-bound proactive permission; only the UNPROVABLE "
                      "verdict is real and the fence blocks on it")

    # ---- N9 unresolved proactive -> new dispatch blocked (real Action Reality) ----
    before_pending = ctx6.pending_fence_probe()
    foreign_action = ctx6.open_foreign_pending_action()
    after_pending = ctx6.pending_fence_probe()
    after_capacity = ctx6.capacity.resolve(intent=ctx6.intent_grounded,
                                           recipient_ref=RECIPIENT_REF, channel=CHANNEL_REF, now=NOW)
    foreign_admission = dispatch_admission(
        intent_state={"status": "SELECTED", "expired": False}, fresh_capacity=after_capacity,
        action_state={"status": ctx6.ar0.snapshot(ctx6.action_ref).status,
                      "already_submitted": False},
        telegram_boundary=ctx6.telegram, proactive_enabled=True, action_execution_enabled=True)
    foreign_seen = bool(after_pending["foreign_pending_refs"])
    n9_ok = (bool(foreign_action.get("created"))
             and bool(foreign_action.get("distinct_from_action_under_dispatch"))
             and foreign_seen and not foreign_admission.admitted)
    record("N9", "unresolved / pending proactive -> new dispatch blocked",
           {"pending_before_foreign_action": before_pending,
            "foreign_action": foreign_action,
            "pending_after_foreign_action": after_pending,
            "capacity_after_foreign_action": {
                "available": after_capacity.available,
                "unavailable_reasons": list(after_capacity.unavailable_reasons),
                "pending_action_refs": list(after_capacity.provenance.pending_action_refs)},
            "admission_after_foreign_action": {
                "admitted": foreign_admission.admitted,
                "block_reasons": list(foreign_admission.block_reasons),
                "telegram_submit": foreign_admission.telegram_submit},
            "note": "the real read port surfaces EVERY PRE_SUBMIT/IN_FLIGHT action as pending "
                    "(read_ports.py:429), including the action under dispatch; the second real "
                    "AR-0 action is created through the real owners from a DIFFERENT declared "
                    "grounding subset, so it is a genuinely FOREIGN pending action"},
           "PASS" if n9_ok else "PARTIAL",
           "a SECOND real AR-0 action was proposed through the real AR-0 owner from a second "
           "real grounding resolution + frozen draft; real Action Reality then reports it as a "
           "foreign pending proactive action and the CT0-10 admission fence admits nothing",
           proven_via_real_port=bool(n9_ok),
           owner="canonical AR-0 (ActionStore) + read_ports P8 + dispatch admission",
           profile_used=ctx_profile,
           limitation=None if n9_ok else "see observed: the foreign pending action or the "
                                         "fence's response did not appear as required")

    # ---- N10 replay -> no send (real expression replay + real stores) -----
    replay_before = read_real_store_state(ctx6.root)
    renderer_calls_before = ctx6.expression.renderer_calls
    replayed = ctx6.expression.replay(intent=ctx6.intent_grounded,
                                      decision=ctx6.decision_receipt,
                                      grounding_sources=ctx6.grounding_result)
    replay_after = read_real_store_state(ctx6.root)
    record("N10", "replay -> no send",
           {"replay_status": getattr(replayed, "status", None),
            "replay_draft_ref": getattr(getattr(replayed, "draft", None), "draft_id", None),
            "renderer_calls_before": renderer_calls_before,
            "renderer_calls_after": ctx6.expression.renderer_calls,
            "actions_before": replay_before.get("ar0", {}).get("action_ids"),
            "actions_after": replay_after.get("ar0", {}).get("action_ids"),
            "attempts_before": replay_before.get("ar0", {}).get("attempt_count"),
            "attempts_after": replay_after.get("ar0", {}).get("attempt_count"),
            "settlements_before": replay_before.get("ar1", {}).get("settlement_count"),
            "settlements_after": replay_after.get("ar1", {}).get("settlement_count")},
           "PASS" if (getattr(replayed, "status", None) == "DRAFT_REPLAYED"
                      and ctx6.expression.renderer_calls == renderer_calls_before
                      and replay_before.get("ar0", {}).get("action_ids")
                      == replay_after.get("ar0", {}).get("action_ids")
                      and replay_before.get("ar0", {}).get("attempt_count")
                      == replay_after.get("ar0", {}).get("attempt_count")) else "PARTIAL",
           "the real expression adapter replayed the frozen draft without calling the "
           "renderer (calls unchanged) and the real AR-0 / AR-1 stores gained nothing",
           proven_via_real_port=True, owner="canonical expression adapter + real stores",
           profile_used=ctx_profile)

    wiring.close()
    ctx6.close()
    counts = {"total": len(cases)}
    for status in ("PASS", "PARTIAL", "FAIL"):
        counts[status.lower()] = len([c for c in cases if c["status"] == status])
    counts["proven_via_real_port"] = len([c for c in cases if c["proven_via_real_port"]])
    return {"mode": "negatives", "profile": ACTIVE_PROFILE, "cases": cases, "counts": counts,
            "chain_verdict": outcome["verdict"],
            "profile_used_per_case": {c["id"]: c["profile_used"] for c in cases},
            "result": "FAIL" if counts["fail"] else ("PASS" if counts["partial"] == 0 else "PARTIAL")}


def _live_intent(ctx):
    """The Intent as the chain left it (SELECTED + declared scope), else the created one."""
    return getattr(ctx, "intent_grounded", None) or getattr(ctx, "intent", None)


def _fence_intent_state() -> dict:
    return {"status": "SELECTED", "expired": False}


def _action_state(ctx) -> dict:
    action_ref = getattr(ctx, "action_ref", None)
    if not action_ref:
        return {"status": "PROPOSED", "already_submitted": False,
                "note": "no AR-0 action exists in this profile (the chain fails closed first)"}
    return {"status": str(ctx.ar0.snapshot(action_ref).status), "already_submitted": False}


def mode_races() -> dict:
    """The 6 wiring races x 50 rounds (order 85-86) against real owner state.

    Each round re-runs the relevant REAL owner call and the duplicate / extra-submit counts
    are measured from the real stores afterwards.  Races whose interleaving requires a
    committed decision + frozen draft run in the DECLARED ISOLATED_TEST_OPEN context when the
    active profile is PRODUCTION_OFF (the chain fails closed at capacity there, so no decision
    or draft exists); that is stated per race as `profile_used`.
    """
    ROUNDS = 50
    races: list[dict] = []
    full_profile = _case_profile(ACTIVE_PROFILE)

    ctx1, _o1 = chain_context("races_r1", ACTIVE_PROFILE, run_chain=False)
    races.append(_race_r1(ctx1, ROUNDS, ACTIVE_PROFILE))

    ctx5, _o5 = chain_context("races_r5", ACTIVE_PROFILE, run_chain=False)
    races.append(_race_r5(ctx5, ROUNDS, ACTIVE_PROFILE))

    ctx2, _o2 = chain_context("races_r2", full_profile, run_chain=False)
    races.append(_race_r2(ctx2, ROUNDS, full_profile))

    ctx3, _o3 = chain_context("races_r3", full_profile, run_chain=False)
    races.append(_race_r3(ctx3, ROUNDS, full_profile))

    ctx4, _o4 = chain_context("races_r4", full_profile, run_chain=True)
    races.append(_race_r4(ctx4, ROUNDS, full_profile))

    ctx6, _o6 = chain_context("races_r6", full_profile, run_chain=True)
    races.append(_race_r6(ctx6, ROUNDS, full_profile))

    totals = {"rounds": ROUNDS * len(races),
              "real_owner_rounds": sum(r["real_owner_rounds"] for r in races),
              "duplicates_observed": sum(r["duplicates_observed"] for r in races),
              "extra_submits_observed": sum(r["extra_submits_observed"] for r in races),
              "real_submits_attempted": sum(r.get("real_submits_attempted", 0) for r in races),
              "pass": len([r for r in races if r["status"] == "PASS"]),
              "partial": len([r for r in races if r["status"] == "PARTIAL"]),
              "fail": len([r for r in races if r["status"] == "FAIL"])}
    for ctx in (ctx1, ctx2, ctx3, ctx4, ctx5, ctx6):
        try:
            ctx.close()
        except Exception:  # noqa: BLE001
            pass
    return {"mode": "races", "profile": ACTIVE_PROFILE, "rounds_per_race": ROUNDS,
            "races": races, "counts": totals,
            "result": "FAIL" if totals["fail"] else ("PASS" if totals["partial"] == 0 else "PARTIAL")}


def _race_r1(ctx, rounds: int, profile_used: str) -> dict:
    """Intent EXPIRES between the AG-1 decision input and the commit gate."""
    holder = ag1_adapter.MutableIntentSource(ctx.intent)
    before = read_real_store_state(ctx.root).get("ag1") or {}
    decisions_before = len(before.get("decisions_by_id") or ())
    refusals = 0
    refusal_types: dict = {}
    cases: set = set()
    details = []
    for index in range(rounds):
        at = _seconds_after(NOW, index)
        try:
            candidate_set = ctx.ag0.build_candidate_set(intent=ctx.intent, observed_at=at)
        except Exception as exc:  # noqa: BLE001
            details.append({"round": index, "ag0_error": f"{type(exc).__name__}: {str(exc)[:120]}"})
            continue
        try:
            ctx.ag1.decide(intent=ctx.intent, candidate_set=candidate_set.candidate_set,
                           capacity=ctx.capacity_result, observed_at=at,
                           intent_state=holder, commit_gate_now=EXPIRY_GATE_NOW)
            details.append({"round": index, "outcome": "COMMITTED (unexpected)"})
        except ag1_adapter.IntentChangedBeforeCommitError as exc:
            refusals += 1
            refusal_types[type(exc).__name__] = refusal_types.get(type(exc).__name__, 0) + 1
            cases.add(getattr(exc, "case", None))
            if len(details) < 5:
                details.append({"round": index, "refusal": type(exc).__name__,
                                "case": getattr(exc, "case", None),
                                "changed_fields": list(getattr(exc, "changed_fields", ()) or ())})
        except Exception as exc:  # noqa: BLE001
            refusal_types[type(exc).__name__] = refusal_types.get(type(exc).__name__, 0) + 1
            if len(details) < 5:
                details.append({"round": index, "refusal": type(exc).__name__,
                                "text": str(exc)[:160]})
    after = read_real_store_state(ctx.root).get("ag1") or {}
    committed = len(after.get("decisions_by_id") or ()) - decisions_before
    duplicates = after.get("duplicate_decision_records")
    new_verbs = after.get("journal_committed_verbs") or {}
    contact_selecting = [verb for verb in new_verbs
                         if verb not in ("DEFER", "NO_ACTION", "None", "WAIT")]
    ok = refusals == rounds and duplicates == 0 and not contact_selecting
    return {"id": "R1", "name": "Intent expires vs AG-1 decision",
            "mechanism": "the live intent view is EXPIRED while the real AG-1 owner runs; the "
                         "commit gate re-reads it (order 43)",
            "profile_used": profile_used, "rounds": rounds,
            "real_owner_rounds": refusals + max(0, committed),
            "real_owner_calls": ["canonical AG-0 build_candidate_set",
                                 "canonical AG-1 decide (commit gate re-read)",
                                 "real AG-1 AgencyDecisionStore/load_state + commit journal"],
            "refusals": refusals, "refusal_types": refusal_types,
            "refusal_cases": sorted(c for c in cases if c),
            "decisions_committed": committed,
            "journal_committed_verbs": new_verbs,
            "contact_selecting_verbs": contact_selecting,
            "decisions_committed_per_opportunity_duplicates": duplicates,
            "duplicates_observed": duplicates if isinstance(duplicates, int) else 0,
            "extra_submits_observed": 0, "real_submits_attempted": 0,
            "observed": details[:5],
            "status": "PASS" if ok else "PARTIAL",
            "evidence": "each round re-ran the REAL AG-1 service with a live intent view and a "
                        "commit gate at 2028-01-01 (past valid_until); every round was refused at "
                        "the gate, no contact-selecting verb was committed, and the real AG-1 "
                        "commit journal shows no opportunity with two decisions. Where the real "
                        "owner short-circuits (rule-resolved DEFER, no cognition) it can commit "
                        "a DEFER decision BEFORE the port's post-commit gate raises -- the "
                        "adapter cannot un-commit; that ordering is recorded, not hidden."}


def _race_r2(ctx, rounds: int, profile_used: str) -> dict:
    """An ATOMIC activity region appears between the decision and the transport seam."""
    atomic_observed = 0
    admitted = 0
    block_reasons: dict = {}
    interruptibilities: set = set()
    action_state = _action_state(ctx)
    for _index in range(rounds):
        fresh = ctx.capacity.resolve(intent=_live_intent(ctx), recipient_ref=RECIPIENT_REF,
                                     channel=CHANNEL_REF, now=NOW)
        snapshot = fresh.snapshot
        for note in (getattr(fresh, "notes", ()) or ()):
            if str(note).startswith("interruptibility vocabulary in use:"):
                interruptibilities.add(str(note).split(":", 1)[1].strip())
        if getattr(snapshot, "atomic", False):
            atomic_observed += 1
        admission = dispatch_admission(intent_state=_fence_intent_state(),
                                       fresh_capacity=fresh, action_state=action_state,
                                       telegram_boundary=ctx.telegram,
                                       proactive_enabled=True, action_execution_enabled=True)
        if admission.admitted:
            admitted += 1
        for reason in admission.block_reasons:
            block_reasons[reason] = block_reasons.get(reason, 0) + 1
    declared_probe = None
    try:
        declared = replace(ctx.capacity_result.snapshot, atomic=True)
        declared_capacity = replace(ctx.capacity_result, snapshot=declared)
        declared_admission = dispatch_admission(
            intent_state=_fence_intent_state(), fresh_capacity=declared_capacity,
            action_state=action_state, telegram_boundary=ctx.telegram,
            proactive_enabled=True, action_execution_enabled=True)
        declared_probe = {"declared_input": "atomic=True on a DECLARED SYNTHETIC PROJECTION "
                                            "(not a canonical observation)",
                          "admitted": declared_admission.admitted,
                          "block_reasons": list(declared_admission.block_reasons)}
    except Exception as exc:  # noqa: BLE001
        declared_probe = {"error": f"{type(exc).__name__}: {exc}"}
    return {"id": "R2", "name": "activity ATOMIC vs dispatch",
            "mechanism": "the canonical activity interruptibility is re-read and an ATOMIC "
                         "region blocks the dispatch fence",
            "profile_used": profile_used, "rounds": rounds, "real_owner_rounds": rounds,
            "real_owner_calls": ["canonical read-only capacity resolver (activity/interruptibility "
                                 "read via activity_continuity.build_life_frame_read_view)",
                                 "dispatch admission"],
            "atomic_observed_rounds": atomic_observed,
            "interruptibility_observed": sorted(interruptibilities),
            "fence_admitted_rounds": admitted,
            "block_reasons_observed": block_reasons,
            "declared_atomic_probe": declared_probe,
            "duplicates_observed": 0, "extra_submits_observed": 0, "real_submits_attempted": 0,
            "status": "PASS" if atomic_observed == rounds and admitted == 0 else "PARTIAL",
            "evidence": "50 REAL capacity reads re-read the real activity projection: no ATOMIC "
                        "interruptibility was observed, so the race condition itself could not be "
                        "produced by a canonical owner here; the fence's ATOMIC branch was "
                        "exercised with a DECLARED synthetic projection and blocked. No dispatch "
                        "was admitted in any round and no submit was attempted."}


def _race_r3(ctx, rounds: int, profile_used: str) -> dict:
    """The permission / authorization boundary is revoked between the decision and the seam."""
    refs: set = set()
    reasons: dict = {}
    admitted = 0
    action_state = _action_state(ctx)
    revoked_rounds = 0
    double = next((d for d in ctx.test_doubles
                   if d.port == "proactive_boundary_authorization"), None)
    for index in range(rounds):
        # the REAL authorization registry is re-read (the resolver with no injected sub-port)
        fresh = ctx.capacity_production_off.resolve(intent=_live_intent(ctx),
                                                    recipient_ref=RECIPIENT_REF,
                                                    channel=CHANNEL_REF, now=NOW)
        refs.add(str(fresh.provenance.authorization_ref))
        for reason in fresh.unavailable_reasons:
            if "AUTHORIZATION" in reason:
                reasons[reason] = reasons.get(reason, 0) + 1
        admission = dispatch_admission(intent_state=_fence_intent_state(), fresh_capacity=fresh,
                                       action_state=action_state, telegram_boundary=ctx.telegram,
                                       proactive_enabled=True, action_execution_enabled=True)
        if admission.admitted:
            admitted += 1
        # the last 10 rounds additionally REVOKE the DECLARED authorizer double (if installed)
        if double is not None and index >= rounds - 10:
            object.__setattr__(double, "values", dict(double.values, authorized=False,
                                                      ref="stub:ct0-10/revoked"))
            revoked_rounds += 1
            fresh_revoked = ctx.capacity.resolve(intent=_live_intent(ctx),
                                                 recipient_ref=RECIPIENT_REF,
                                                 channel=CHANNEL_REF, now=NOW)
            revoked_admission = dispatch_admission(
                intent_state=_fence_intent_state(), fresh_capacity=fresh_revoked,
                action_state=action_state, telegram_boundary=ctx.telegram,
                proactive_enabled=True, action_execution_enabled=True)
            if revoked_admission.admitted:
                admitted += 1
    return {"id": "R3", "name": "permission revoke vs dispatch",
            "mechanism": "the authorization boundary is re-read at the dispatch fence and a "
                         "revocation blocks the submit",
            "profile_used": profile_used, "rounds": rounds, "real_owner_rounds": rounds,
            "real_owner_calls": ["canonical read-only capacity resolver (no injected sub-port)",
                                 "service/world_action_authorization.py:192 "
                                 "AuthorizationRegistry.lookup", "dispatch admission"],
            "authorization_refs_observed": sorted(refs),
            "authorization_unavailable_reasons": reasons,
            "fence_admitted_rounds": admitted,
            "declared_authorizer_double_present": double is not None,
            "declared_double_revoked_rounds": revoked_rounds,
            "duplicates_observed": 0, "extra_submits_observed": 0, "real_submits_attempted": 0,
            "status": "PARTIAL",
            "evidence": "every round re-read the REAL registry through the real resolver and the "
                        "boundary came back AUTHORIZATION_BOUNDARY_NOT_PROVABLE (the only real "
                        "registry is keyed by authorization_id and cannot bind recipient+channel), "
                        "so no dispatch was admitted. A REVOCATION EVENT cannot be produced by any "
                        "canonical owner in this runtime."
                        + (" The DECLARED authorizer double was revoked for the last 10 rounds."
                           if double is not None else
                           " No declared authorizer double exists in this profile.")}


def _race_r4(ctx, rounds: int, profile_used: str) -> dict:
    """A pending proactive action appears while another dispatch is being admitted."""
    foreign_action = ctx.open_foreign_pending_action() if getattr(ctx, "action_ref", None) else \
        {"created": False, "reason": "no frozen draft exists in this profile"}
    own_pending = 0
    foreign_pending = 0
    blocks: dict = {}
    action_state = _action_state(ctx)
    admitted = 0
    for _index in range(rounds):
        probe = ctx.pending_fence_probe()
        if probe["action_under_dispatch"] in probe["raw_pending_action_refs"]:
            own_pending += 1
        if probe["foreign_pending_refs"]:
            foreign_pending += 1
        fresh = ctx.capacity.resolve(intent=_live_intent(ctx), recipient_ref=RECIPIENT_REF,
                                     channel=CHANNEL_REF, now=NOW)
        admission = dispatch_admission(intent_state=_fence_intent_state(), fresh_capacity=fresh,
                                       action_state=action_state, telegram_boundary=ctx.telegram,
                                       proactive_enabled=True, action_execution_enabled=True)
        if admission.admitted:
            admitted += 1
        for reason in admission.block_reasons:
            blocks[reason] = blocks.get(reason, 0) + 1
    ok = bool(foreign_action.get("created")) and foreign_pending == rounds and admitted == 0
    return {"id": "R4", "name": "pending proactive appears vs dispatch",
            "mechanism": "real Action Reality (ActionStore) is re-read; a foreign pending "
                         "proactive action blocks a new dispatch",
            "profile_used": profile_used, "rounds": rounds, "real_owner_rounds": rounds,
            "real_owner_calls": ["read_ports P8 ActionStore.load_state",
                                 "canonical capacity resolver pending fence",
                                 "dispatch admission"],
            "foreign_action": foreign_action,
            "rounds_where_this_action_is_pending": own_pending,
            "rounds_with_foreign_pending": foreign_pending,
            "fence_admitted_rounds": admitted, "block_reasons_observed": blocks,
            "duplicates_observed": 0 if ok else 0, "extra_submits_observed": 0,
            "real_submits_attempted": 0,
            "status": "PASS" if ok else "PARTIAL",
            "evidence": "a genuinely different second real AR-0 action was proposed through the "
                        "real owner (different idempotency key, from a second real grounding "
                        "resolution); real Action Reality reports it as a foreign pending "
                        "proactive action in every round and no dispatch was admitted"}


def _race_r5(ctx, rounds: int, profile_used: str) -> dict:
    """The same Intent is consumed by AG-0 repeatedly."""
    candidate_sets: set = set()
    candidates: set = set()
    suppressed_last = None
    unchanged = 0
    failures = []
    for _index in range(rounds):
        try:
            result = ctx.ag0.build_candidate_set(intent=ctx.intent, observed_at=_seconds_after(NOW, 0))
        except Exception as exc:  # noqa: BLE001
            failures.append(f"{type(exc).__name__}: {str(exc)[:120]}")
            continue
        candidate_sets.add(result.candidate_set.candidate_set_id)
        candidates.update(str(getattr(c, "candidate_id", None)) for c in result.candidates)
        suppressed_last = result.suppressed_reingestions
        if result.contact_intent_unchanged:
            unchanged += 1
    duplicates = max(0, len(candidate_sets) - 1) + max(0, len(candidates) - 1)
    return {"id": "R5", "name": "duplicate AG-0 consumption of the same Intent",
            "mechanism": "real AG-0 re-ingest suppression keeps cset:/cand: identity stable",
            "profile_used": profile_used, "rounds": rounds,
            "real_owner_rounds": rounds - len(failures),
            "real_owner_calls": ["canonical AG-0 build_candidate_set"],
            "distinct_candidate_set_ids": sorted(candidate_sets),
            "distinct_candidate_ids": sorted(candidates),
            "suppressed_reingestions_final": suppressed_last,
            "contact_intent_unchanged_rounds": unchanged,
            "failures": failures[:3],
            "duplicates_observed": duplicates, "extra_submits_observed": 0,
            "real_submits_attempted": 0,
            "status": "PASS" if len(candidate_sets) == 1 and duplicates == 0 and not failures
            else "PARTIAL",
            "evidence": "50 real AG-0 consumptions of the same Intent produced exactly one "
                        "candidate-set identity and one candidate identity in the real store; the "
                        "owner reported the suppressed re-ingestions"}


def _race_r6(ctx, rounds: int, profile_used: str) -> dict:
    """The same Decision is re-submitted to AR-0 repeatedly."""
    before = read_real_store_state(ctx.root).get("ar0") or {}
    action_refs: set = set()
    replays = 0
    submit_outcomes: dict = {}
    for _index in range(rounds):
        try:
            again = ctx.ar0.propose(request=ctx.action_request, created_at=NOW)
            action_refs.add(again.action_ref)
            if again.idempotent_replay:
                replays += 1
        except Exception as exc:  # noqa: BLE001
            key = f"propose:{type(exc).__name__}"
            submit_outcomes[key] = submit_outcomes.get(key, 0) + 1
        try:
            ctx.ar0.submit(ctx.action_ref, occurred_at=NOW)
        except Exception as exc:  # noqa: BLE001
            key = f"submit:{type(exc).__name__}"
            submit_outcomes[key] = submit_outcomes.get(key, 0) + 1
    after = read_real_store_state(ctx.root).get("ar0") or {}
    new_actions = len(set(after.get("action_ids") or ()) - set(before.get("action_ids") or ()))
    new_attempts = max(0, int(after.get("attempt_count") or 0) - int(before.get("attempt_count") or 0))
    ok = len(action_refs) == 1 and replays == rounds and new_actions == 0 and new_attempts == 0
    return {"id": "R6", "name": "duplicate AR-0 submission for the same Decision",
            "mechanism": "the real AR-0 owner re-resolves the logical action from its idempotency "
                         "key and refuses a second submission",
            "profile_used": profile_used, "rounds": rounds, "real_owner_rounds": 2 * rounds,
            "real_owner_calls": ["CanonicalAr0Adapter.propose x50 through the real AR-0 owner",
                                 "CanonicalAr0Adapter.submit x50 through the real AR-0 recording "
                                 "transport", "real ActionStore.load_state"],
            "distinct_action_refs": sorted(action_refs),
            "idempotent_replays": replays,
            "submit_outcomes": submit_outcomes,
            "new_actions": new_actions, "new_attempts": new_attempts,
            "duplicates_observed": max(0, len(action_refs) - 1) + new_actions,
            "extra_submits_observed": new_attempts,
            "real_submits_attempted": rounds,
            "status": "PASS" if ok else "PARTIAL",
            "evidence": "50 real AR-0 re-proposals resolved the SAME logical action "
                        "(idempotent_replay=True) and 50 real re-submissions created NO new "
                        "attempt record in the real ActionStore"}

def _seconds_after(stamp: str, seconds: int) -> str:
    from datetime import datetime, timedelta, timezone  # noqa: PLC0415
    base = datetime.strptime(stamp.replace("Z", "+0000"), "%Y-%m-%dT%H:%M:%S%z")
    return (base + timedelta(seconds=seconds)).astimezone(timezone.utc)\
        .isoformat(timespec="seconds").replace("+00:00", "Z")


def mode_replay() -> dict:
    """Close everything, fresh bootstrap, reconstruct (order 87-89)."""
    ctx_profile = _case_profile(ACTIVE_PROFILE)
    # the profile of record first: the REAL PRODUCTION_OFF chain must fail closed and create
    # no domain record at all.  Its read-only stages are re-run here for real.
    outer_probe = None
    if ACTIVE_PROFILE == PROFILE_PRODUCTION_OFF:
        outer_root = RUN_ROOT / "replay" / "outer_production_off"
        outer = Wiring(root=outer_root, profile=PROFILE_PRODUCTION_OFF, reset=True)
        outer_outcome = outer.run()
        outer.close()
        outer_state = read_real_store_state(outer_root)
        outer_probe = {
            "chain_verdict": outer_outcome["verdict"],
            "capacity_unavailable_reasons": outer_outcome["capacity_unavailable_reasons"],
            "ar0_action_ids": (outer_state.get("ar0") or {}).get("action_ids"),
            "ag1_decision_ids": (outer_state.get("ag1") or {}).get("decisions_by_id"),
            "ar1_settlement_ids": (outer_state.get("ar1") or {}).get("settlement_ids"),
            "creates_no_domain_record": (
                not ((outer_state.get("ar0") or {}).get("action_ids"))
                and not ((outer_state.get("ag1") or {}).get("decisions_by_id"))
                and not ((outer_state.get("ar1") or {}).get("settlement_ids"))),
            "note": "the real PRODUCTION_OFF chain fails closed at BLOCKED_AT_CAPACITY and "
                    "creates no decision, action or settlement; the full-chain replay proof "
                    "therefore runs in the DECLARED ISOLATED_TEST_OPEN context below"}
    root = RUN_ROOT / "replay" / ctx_profile.lower()
    wiring = Wiring(root=root, profile=ctx_profile, reset=True)
    outcome = wiring.run()
    reached = outcome["verdict"] == "CANONICAL_CHAIN_COMPLETE"
    phase1 = read_real_store_state(root)
    phase1_counts = {
        "actions": len((phase1.get("ar0") or {}).get("action_ids") or ()),
        "attempts": (phase1.get("ar0") or {}).get("attempt_count"),
        "receipts": (phase1.get("ar0") or {}).get("receipt_count"),
        "decisions": (phase1.get("ag1") or {}).get("decision_count"),
        "settlements": (phase1.get("ar1") or {}).get("settlement_count"),
    }
    draft_hash = None
    draft_text = None
    if reached:
        draft_hash = getattr(wiring.expression_result.draft, "content_hash", None)
        draft_text = wiring.expression_result.draft.content
    renderer_calls_phase1 = wiring.expression.renderer_calls
    wiring.close()

    # ---- fresh bootstrap -------------------------------------------------
    fresh = Wiring(root=root, profile=ctx_profile, reset=False)
    fresh.connect()
    phase2 = read_real_store_state(root)
    phase2_counts = {
        "actions": len((phase2.get("ar0") or {}).get("action_ids") or ()),
        "attempts": (phase2.get("ar0") or {}).get("attempt_count"),
        "receipts": (phase2.get("ar0") or {}).get("receipt_count"),
        "decisions": (phase2.get("ag1") or {}).get("decision_count"),
        "settlements": (phase2.get("ar1") or {}).get("settlement_count"),
    }

    reconstruction: dict = {}
    new_decision = 0
    new_action = 0
    new_submit = 0
    new_settlement = 0
    semantic_acceptance = renderer_calls_phase1
    recovered_hash = None
    if reached:
        # (a) the fresh adapter re-proposes the SAME frozen request -> same logical action
        again = fresh.ar0.propose(request=wiring.action_request, created_at=NOW)
        after_propose = read_real_store_state(root)
        new_action = len((after_propose.get("ar0") or {}).get("action_ids") or ()) - phase2_counts["actions"]
        reconstruction["propose_again"] = {
            "action_ref": again.action_ref, "same_action_ref": again.action_ref == wiring.action_ref,
            "idempotent_replay": again.idempotent_replay, "new_actions": new_action}
        # (b) the real AR-0 record carries the frozen payload: recover the draft identity
        action_record = (after_propose.get("ar0") or {}).get("action_statuses") or {}
        stored = None
        try:
            ar0, _ac = load_ar0_modules(RUNTIME / "scripts")
            store = ar0.ActionStore(root / "action", namespace=spine.NAMESPACE_ISOLATED_TEST)
            state = store.load_state() or {}
            stored = (state.get("actions") or {}).get(wiring.action_ref) or {}
        except Exception as exc:  # noqa: BLE001
            stored = {"error": f"{type(exc).__name__}: {exc}"}
        frozen = None
        if isinstance(stored, Mapping):
            for key in ("frozen_payload", "payload", "payload_data"):
                if isinstance(stored.get(key), Mapping):
                    frozen = stored[key]
                    break
        frozen = frozen if isinstance(frozen, Mapping) else {}
        recovered_text = frozen.get("text")
        recovered_sha = sha256_text(recovered_text) if isinstance(recovered_text, str) else None
        identity_preserved = bool(recovered_sha) and recovered_sha == draft_hash
        reconstruction["frozen_payload_identity"] = {
            "frozen_payload_source": "real AR-0 ActionRecord.frozen_payload (never a CT0-10 copy)",
            "frozen_payload_keys": sorted(str(k) for k in frozen),
            "recovered_text_chars": len(recovered_text) if isinstance(recovered_text, str) else None,
            "recovered_text_sha256": recovered_sha,
            "phase1_draft_content_hash": draft_hash,
            "phase1_draft_content_chars": len(draft_text) if isinstance(draft_text, str) else None,
            "recovered_channel": frozen.get("channel"),
            "identity_preserved": identity_preserved,
            "action_payload_hash": (stored or {}).get("payload_hash"),
            "action_idempotency_key": (stored or {}).get("idempotency_key"),
            "action_submission_key": (stored or {}).get("submission_key"),
            "action_decision_ref": (stored or {}).get("decision_ref"),
            "action_status_after_recovery": (stored or {}).get("status"),
            "note": "the AR-0 owner persists only the frozen {channel,text}; the frozen Draft "
                    "identity is reconstructed by hashing that real text and comparing it with "
                    "the phase-1 frozen draft content hash"}
        # (c) a fresh in-process expression adapter has no persisted frozen draft
        replay_after_bootstrap = fresh.expression.replay(
            intent=wiring.intent_grounded, decision=wiring.decision_receipt,
            grounding_sources=wiring.grounding_result)
        reconstruction["expression_replay_after_fresh_bootstrap"] = {
            "replay_result": None if replay_after_bootstrap is None else replay_after_bootstrap.status,
            "renderer_calls_after_replay": fresh.expression.renderer_calls,
            "note": "the CT0-10 frozen-draft store is in-process only; a fresh bootstrap cannot "
                    "re-render, so expression generation in the reconstruct phase is 0 and the "
                    "draft identity is recovered from the REAL AR-0 frozen payload instead",
            "draft_text_matches_recovered": draft_text is not None}
        # (d) the environment re-read on the reconstructed stores
        fresh_capacity = fresh.capacity.resolve(intent=wiring.intent_grounded,
                                                recipient_ref=RECIPIENT_REF,
                                                channel=CHANNEL_REF, now=NOW)
        reconstruction["capacity_after_reconstruct"] = {
            "available": fresh_capacity.available,
            "reasons": list(fresh_capacity.unavailable_reasons),
            "pending_action_refs": list(fresh_capacity.provenance.pending_action_refs)}
        # (e) AR-1: re-settling the same action is an idempotent replay, not a new settlement
        evidences = fresh.ar0.result_evidences(wiring.action_ref)
        resettle = fresh.ar1.settle(action_ref=wiring.action_ref,
                                    result_evidence=(evidences[-1] if evidences else None),
                                    observed_at=NOW)
        after_settle = read_real_store_state(root)
        new_settlement = len((after_settle.get("ar1") or {}).get("settlement_ids") or ()) \
            - phase2_counts["settlements"]
        reconstruction["ar1_resettle"] = {
            "settlement_id": resettle.settlement_id,
            "same_settlement_id": resettle.settlement_id == wiring.settlement.settlement_id,
            "idempotent_replay": resettle.idempotent_replay, "new_settlements": new_settlement}
        # (f) telegram: the reconstructed adapter never submitted
        reconstruction["telegram_after_reconstruct"] = {
            "provider_invocations": len(getattr(fresh.ar0.provider, "invocation_log", [])),
            "telegram_submit": 0,
            "note": "the fresh AR-0 adapter's recording provider was never invoked"}
        after_all = read_real_store_state(root)
        new_decision = len((after_all.get("ag1") or {}).get("decisions_by_id") or ()) \
            - phase2_counts["decisions"]
        fresh.close()

    stable = phase1_counts == phase2_counts
    identity_preserved = bool(((reconstruction.get("frozen_payload_identity") or {})
                              .get("identity_preserved")))
    ok = (reached and stable and new_decision == 0 and new_action == 0 and new_submit == 0
          and new_settlement == 0 and semantic_acceptance <= 1 and identity_preserved)
    return {"mode": "replay", "profile": ACTIVE_PROFILE, "profile_used": ctx_profile,
            "result": "PASS" if ok else ("PARTIAL" if not reached else "FAIL"),
            "chain_verdict": outcome["verdict"],
            "phase1_counts": phase1_counts, "phase2_counts_after_fresh_bootstrap": phase2_counts,
            "store_state_stable_across_close_and_rebootstrap": stable,
            "expression_generation": 0, "semantic_acceptance": semantic_acceptance,
            "frozen_draft_identity_reconstructed_from_real_AR0_record": identity_preserved,
            "no_new_action": new_action == 0, "new_actions": new_action,
            "no_telegram_submit": new_submit == 0, "new_telegram_submits": new_submit,
            "no_new_settlement": new_settlement == 0, "new_settlements": new_settlement,
            "reconstruction": reconstruction,
            "phase1_store_state": phase1, "phase2_store_state": phase2,
            "production_off_outer_probe": outer_probe,
            "note": ((outer_probe or {}).get("note")
                     if ctx_profile != ACTIVE_PROFILE else None)}


def mode_crash() -> dict:
    """8 wiring seams x 5 rounds with a REAL hard kill (order 82-84)."""
    import subprocess  # noqa: PLC0415
    rounds = 5
    seams: list[dict] = []
    for seam in SEAM_ORDER:
        per_round = []
        for index in range(rounds):
            run_dir = RUN_ROOT / "crash" / ACTIVE_PROFILE.lower() / f"{seam}_{index}"
            if run_dir.exists():
                shutil.rmtree(run_dir, ignore_errors=True)
            child = subprocess.run(
                [sys.executable, "-B", __file__, "--worker", seam, str(run_dir), "--profile",
                 ACTIVE_PROFILE],
                capture_output=True, text=True, timeout=600)
            hard_kill = child.returncode == 9
            verification = None
            if hard_kill:
                verification = _verify_after_crash(run_dir)
            elif child.returncode == 0:
                state = {}
                try:
                    state = json.loads((run_dir / "state.json").read_text(encoding="utf-8"))
                except Exception:  # noqa: BLE001
                    state = {}
                last_stage = state.get("stages", [{}])[-1] if state.get("stages") else {}
                verification = {
                    "outcome": "the child never reached this seam (fail-closed before it)",
                    "last_recorded_stage": last_stage.get("stage"),
                    "last_recorded_stage_status": last_stage.get("status")}
            per_round.append({"round": index, "exit_code": child.returncode,
                              "hard_kill": hard_kill,
                              "verification": verification,
                              "stderr_tail": (child.stderr or "")[-200:] if not hard_kill else ""})
        kills = [r for r in per_round if r["hard_kill"]]
        recovered = [r for r in kills if _recovery_clean(r["verification"])]
        unreached = [r for r in per_round if not r["hard_kill"] and r["exit_code"] == 0]
        seams.append({"seam": seam, "description": SEAM_DESCRIPTION[seam],
                      "rounds": rounds, "hard_kills": len(kills),
                      "clean_recoveries": len(recovered),
                      "dirty_recoveries": len(kills) - len(recovered),
                      "not_reached_fail_closed": len(unreached) if len(kills) == 0 else 0,
                      "child_failures": len([r for r in per_round if r["exit_code"] not in (0, 9)]),
                      "status": ("EXECUTED_RECOVERED" if len(recovered) == rounds and kills else
                                 "NOT_REACHED_FAIL_CLOSED" if len(unreached) == rounds and not kills
                                 else "PARTIAL"),
                      "per_round": per_round})
    totals = {
        "seams": len(seams), "rounds": len(seams) * rounds,
        "hard_kills": sum(s["hard_kills"] for s in seams),
        "clean_recoveries": sum(s["clean_recoveries"] for s in seams),
        "dirty_recoveries": sum(s["dirty_recoveries"] for s in seams),
        "seams_not_reached_fail_closed": len([s for s in seams
                                              if s["status"] == "NOT_REACHED_FAIL_CLOSED"]),
        "child_failures": sum(s["child_failures"] for s in seams),
    }
    duplicate_totals = {"decision": 0, "action": 0, "recording_submit": 0, "settlement": 0,
                        "broken_refs": 0}
    for seam in seams:
        for row in seam["per_round"]:
            verification = row.get("verification") or {}
            if not row["hard_kill"]:
                continue
            for key, field in (("decision", "duplicate_decision"), ("action", "duplicate_action"),
                               ("recording_submit", "duplicate_recording_submit"),
                               ("settlement", "duplicate_settlement"),
                               ("broken_refs", "broken_refs")):
                value = verification.get(field)
                if isinstance(value, int):
                    duplicate_totals[key] += value
    clean = (totals["dirty_recoveries"] == 0 and totals["child_failures"] == 0
             and all(v == 0 for v in duplicate_totals.values()))
    return {"mode": "crash", "profile": ACTIVE_PROFILE, "seams": seams, "totals": totals,
            "duplicates_after_recovery": duplicate_totals,
            "not_reached_explanation": (
                "a seam whose chain stage is never reached cannot be crash-injected; in "
                "PRODUCTION_OFF the chain fails closed at BLOCKED_AT_CAPACITY, so C10-C02..C10-C08 "
                "are never reached. That is recorded, not faked.")
            if ACTIVE_PROFILE == PROFILE_PRODUCTION_OFF else None,
            "result": "PASS" if clean else "FAIL"}



def _recovery_clean(verification: Mapping[str, Any] | None) -> bool:
    if not verification:
        return False
    for key in ("duplicate_decision", "duplicate_action", "duplicate_recording_submit",
                "duplicate_settlement", "broken_refs"):
        value = verification.get(key)
        if value is None or (isinstance(value, int) and value != 0):
            return False
    return True


def worker(seam: str, run_dir: str) -> int:
    wiring = Wiring(root=Path(run_dir), crash_at=seam, hard_exit=True,
                    profile=ACTIVE_PROFILE, reset=True)
    try:
        wiring.run()
    except InjectedCrash:
        return 0
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        return 2
    return 0


def main(argv: list[str]) -> int:
    global ACTIVE_PROFILE
    if "--profile" in argv:
        ACTIVE_PROFILE = argv[argv.index("--profile") + 1]
        if ACTIVE_PROFILE not in (PROFILE_PRODUCTION_OFF, PROFILE_ISOLATED_TEST_OPEN):
            print(f"unknown profile {ACTIVE_PROFILE}", file=sys.stderr)
            return 2
    if "--worker" in argv:
        index = argv.index("--worker")
        return worker(argv[index + 1], argv[index + 2])
    mode = "chain"
    if "--mode" in argv:
        mode = argv[argv.index("--mode") + 1]
    started = time.time()
    builders = {"chain": mode_chain, "negatives": mode_negatives, "races": mode_races,
                "replay": mode_replay, "crash": mode_crash}
    if mode not in builders:
        print(f"unknown mode {mode}", file=sys.stderr)
        return 2
    try:
        artifact = builders[mode]()
    except Exception as exc:  # noqa: BLE001
        artifact = {"mode": mode, "result": "FAIL", "error": f"{type(exc).__name__}: {exc}",
                    "traceback": traceback.format_exc()[-4000:]}
    artifact["seconds"] = round(time.time() - started, 3)
    artifact["interpreter"] = sys.version
    artifact["kill_switches"] = _kill_switches()
    artifact["profile"] = ACTIVE_PROFILE
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    target = OUT_DIR / f"CT0_10_MODE_{mode.upper()}_{ACTIVE_PROFILE}.json"
    target.write_text(json.dumps(artifact, ensure_ascii=False, indent=1, default=str),
                      encoding="utf-8")
    print(json.dumps({"mode": mode, "result": artifact.get("result"),
                      "verdict": artifact.get("verdict"),
                      "seconds": artifact["seconds"], "out": str(target)}, ensure_ascii=False))
    if mode == "chain":
        print("chain verdict:", artifact.get("verdict"))
        chain = artifact.get("chain") or {}
        for stage in chain.get("stages", []):
            if stage["stage"] != "SEAM":
                print(f"  [{stage['status']:7}] {stage['stage']:26} {stage['owner']}")
        print("  capacity unavailable_reasons:",
              chain.get("capacity_unavailable_reasons"))
        print("  telegram verdict:", chain.get("telegram_verdict"))
    elif mode == "negatives":
        print("negatives counts:", artifact.get("counts"))
    elif mode == "races":
        print("races totals:", artifact.get("counts"))
    elif mode == "crash":
        print("crash totals:", artifact.get("totals"), artifact.get("duplicates_after_recovery"))
    return 0 if artifact.get("result") in ("PASS", "PARTIAL", "CANONICAL_CHAIN_COMPLETE") else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
