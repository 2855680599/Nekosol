"""CT0-10 Phase J -- canonical Telegram boundary, wired but fenced (order sections 65-70).

WHAT THIS IS
------------
`CanonicalTelegramBoundary` walks the REAL Telegram outbound code path as far as is
legally possible on this machine, and then STOPS at the fence:

 1. it resolves a contact recipient to a Telegram destination using the real recipient
    resolution surface -- but ONLY against an ISOLATED test configuration, never a real
    user target (order 69);
 2. it builds the serialized Telegram dispatch request in the exact shape the real native
    owner builds (method, URL template with the TOKEN NAME only, JSON body, length rule,
    headers), and says where that shape came from;
 3. it validates the request the canonical way: the frozen CT0-6
    `build_message_action_request` binds Draft/Decision/Intent/channel into a request whose
    `idempotency_key` the AR-0 owner would receive as `ar0_act:<sha256>`;
 4. it executes the in-tree canonical boundary -- `ActionAdapterProtocol` /
    `MessageActionAdapter` over `FakeMessageProvider` (scripts/action_reality_ledger.py:1322
    /:1451/:1354) -- which is a RECORDING fake, never a transport;
 5. it double-fences the side effect: `PROACTIVE_ENABLED` / `ACTION_EXECUTION_ENABLED` must
    be false (`ContactActionGate.would_submit` false), a `NoSideEffect` recording transport
    is bound, and any socket call inside the boundary raises.

HARD PROHIBITION
----------------
This module NEVER imports or invokes the real senders/services. The concrete, side-effect
capable owners are named in `FORBIDDEN_REAL_SENDERS` and asserted absent from `sys.modules`
before and after every smoke (`assert_no_real_sender_imported`). Two read-only modules ARE
used deliberately, and are recorded: the in-tree canonical adapter interface required by
the order (`action_reality_ledger.py`), and the real recipient-resolution code
(`plugins/platforms/telegram/telegram_ids.py`, `gateway/channel_directory.py`) loaded BY
FILE, with the channel directory's write helper replaced by a refusing stub so the real
path cannot persist anything.

VERDICT (order 115/W) -- three parts, always reported separately:
    telegram_adapter_bound               (expected true)
    telegram_dispatch_request_build_verified (expected true)
    telegram_real_side_effect_enabled    (expected FALSE -- it would really send otherwise)
"""

from __future__ import annotations

import contextlib
import importlib.util
import json
import os
import socket
import sys
import types
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

try:  # when <ct0-10>/src/ct0_10 is on sys.path
    from canonical_spine import (
        CanonicalActionRef,
        CanonicalIntegrationError,
        NAMESPACE_ISOLATED_TEST,
        assert_isolated_store_root,
        payload_hash,
    )
except ImportError:  # pragma: no cover - when <ct0-10>/src is on sys.path
    from ct0_10.canonical_spine import (
        CanonicalActionRef,
        CanonicalIntegrationError,
        NAMESPACE_ISOLATED_TEST,
        assert_isolated_store_root,
        payload_hash,
    )

# Frozen CT0-6 / CT0-9 contact contracts (read-only imports).
from contact_message_action_model import (
    ContactActionGate,
    ContactMessageActionRequest,
    build_message_action_request,
)


MODULE_ID = "ct0_10.canonical_ports.telegram_boundary"
ORDER_SECTIONS = "65-70"
BOUNDARY_VERSION = "chiyo.ct0_10.telegram_boundary.v1"

# ==========================================================================
# 1. the real adapters: names, shapes, and what must never run
# ==========================================================================
# (a) the native telegram adapter on the owner's deployment -- a stdlib long-polling client behind the
#     ACTIVE service in the owner's deployment. Local read-only reference: the prior-session
#     capture in TELEGRAM_ADAPTER_CAPTURE_CANDIDATES.
# (b) the hermes gateway plugins/platforms/telegram adapter `async def send()`.
REAL_NATIVE_OWNER = ("<real native telegram owner install>/telegram_adapter.py "
                    "(TelegramApi; a remote deployment, NOT available locally)")
REAL_GATEWAY_OWNER = "plugins/platforms/telegram/adapter.py TelegramAdapter.send (owner deployment, not available locally)"
TELEGRAM_API_URL_TEMPLATE = "https://api.telegram.org/bot<TELEGRAM_BOT_TOKEN>"
NATIVE_SEND_METHOD = "sendMessage"
MAX_TELEGRAM_TEXT = 4096  # telegram_adapter.py:25 (MAX_TELEGRAM_TEXT)
NATIVE_SEND_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json",
    "User-Agent": "ChiyoNativeTelegram/0.1",
}
REQUEST_SHAPE_PROVENANCE = (
    "shape reproduced from the real native owner, read-only: TelegramApi.__init__ builds "
    "'https://api.telegram.org/bot' + token (telegram_adapter.py:65); TelegramApi.call() "
    "JSON-POSTs body=json.dumps(payload) with headers Content-Type/Accept/User-Agent "
    "'ChiyoNativeTelegram/0.1' (telegram_adapter.py:68-79); TelegramApi.send_message() "
    "sends {'chat_id': chat_id, 'text': text} and refuses text longer than MAX_TELEGRAM_TEXT "
    "= 4096 (telegram_adapter.py:119-128)."
)
REPLY_SUPPORT_EVIDENCE = {
    "native_owner": {
        "supports_reply": False,
        "evidence": (
            "TelegramApi.send_message(self, chat_id, text) builds exactly "
            "{'chat_id','text'} (telegram_adapter.py:119-128); there is no reply field "
            "anywhere in the native client"
        ),
    },
    "hermes_gateway_owner": {
        "supports_reply": True,
        "evidence": (
            "plugins/platforms/telegram/adapter.py:4415 async def send(self, chat_id, "
            "content, reply_to=None, metadata=None)"
        ),
        "importable_locally": False,
        "reason": "python-telegram-bot + the hermes gateway stack are not installed here",
    },
}

# The two upstream hermes-agent files this boundary executes READ-ONLY to prove the real
# recipient-resolution path (the chat-id normaliser and the channel directory).  They are
# MIT-licensed (Copyright (c) 2025 Nous Research) and are shipped under `third_party/hermes/`
# together with their LICENSE; see THIRD_PARTY_NOTICES.md.  A caller who has its own hermes
# checkout may point CHIYO_HERMES_ROOT at it instead -- no machine-specific path is baked in.
TELEGRAM_ADAPTER_CAPTURE_CANDIDATES = (
    "<telegram_adapter.py: not shipped; the native adapter lives on the owner's deployment>",
)
_HERMES_ROOTS = tuple(
    candidate for candidate in (
        os.environ.get("CHIYO_HERMES_ROOT"),
        str(Path(__file__).resolve().parents[3] / "third_party" / "hermes"),
    ) if candidate
)
REAL_TELEGRAM_IDS_CANDIDATES = tuple(
    str(Path(root) / "plugins" / "platforms" / "telegram" / "telegram_ids.py")
    for root in _HERMES_ROOTS
)
REAL_CHANNEL_DIRECTORY_CANDIDATES = tuple(
    str(Path(root) / "gateway" / "channel_directory.py") for root in _HERMES_ROOTS
)
# The AR-0 owner is vendored in this tree; the original source location is gone.
ACTION_REALITY_LEDGER_CANDIDATES = (
    str(Path(__file__).resolve().parents[2] / "life_runtime" / "action_reality_ledger.py"),
)
# A caller-supplied isolated root always wins; this default is only a last resort.
ISOLATED_ROOT_CANDIDATES = (
    str(Path(__file__).resolve().parents[2].parent / "var" / "telegram-boundary-isolated"),
)

# PRODUCTION counterparty. The real chat id and the real counterparty name were
# REDACTED for the open-source export; the constant is still the production
# destination this boundary REFUSES, and the refusal logic below is unchanged.
PRODUCTION_COUNTERPARTY_REF_NOT_USED = "telegram:<REDACTED_PRODUCTION_CHAT_ID>"
PRODUCTION_COUNTERPARTY_USED = False

# An installation may declare its own sender and service names here. The open-source
# release ships none of the maintainer's private deployment names.
_EXTRA_SENDERS = tuple(x for x in os.environ.get("NYAIRO_FORBIDDEN_SENDERS", "").split(",") if x)
_EXTRA_SERVICES = tuple(x for x in os.environ.get("NYAIRO_FORBIDDEN_SERVICES", "").split(",") if x)
FORBIDDEN_REAL_SENDERS = _EXTRA_SENDERS + (
    "telegram_adapter",                     # the real native telegram adapter
    "chiyo_native_v0",                      # the harness package around it
    "plugins.platforms.telegram.adapter",   # hermes gateway sender
    "plugins.platforms.telegram",           # that plugin package
    "delivery_ledger",                      # gateway outbound-obligation writer
    "intent_executor",                      # service/bridge/intent_executor.py
    "world_body_service",                   # service/world_body_service.py
    "world_body_binding",
    "world_body_substrate",
    "world_property_economy",
    "world_action_resolver",
    "approval_queue",                       # approve/reject/add
    "telegram",                             # python-telegram-bot
)
FORBIDDEN_SERVICES = _EXTRA_SERVICES + (
    "hermes-gateway",
    "chiyo-world-body",
)
CREDENTIAL_NAMES_ONLY = (
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_ALLOWED_USERS",
    "TELEGRAM_ALLOW_ALL_USERS",
    "TELEGRAM_HOME_CHANNEL",
)

SANDBOX_PROVIDER_WALK_MARKER = "IN_TREE_FAKE_MESSAGE_PROVIDER_WALK"


# ==========================================================================
# 2. errors
# ==========================================================================
class TelegramBoundaryError(CanonicalIntegrationError):
    """Base error for the CT0-10 Telegram boundary."""


class TelegramFenceViolationError(TelegramBoundaryError):
    """Something tried to cross the side-effect fence. Refused."""


class TelegramRequestBuildError(TelegramBoundaryError):
    """The serialized request cannot be built in the real owner's shape."""


class TelegramRecipientError(TelegramBoundaryError):
    """The contact recipient could not be resolved through the real resolution surface."""


# ==========================================================================
# 3. records
# ==========================================================================
@dataclass(frozen=True, slots=True)
class TelegramRecipientResolution:
    contact_recipient_ref: str
    resolved_chat_id: int | str
    chat_id_key: str
    directory_lookup_name: str
    directory_module: str
    directory_module_kind: str
    normalization_symbol: str
    relationship_record_path: str | None
    relationship_counterparty_raw: str | None
    production_counterparty_used: bool = PRODUCTION_COUNTERPARTY_USED
    notes: tuple[str, ...] = ()

    def to_mapping(self) -> dict[str, Any]:
        return {
            "contact_recipient_ref": self.contact_recipient_ref,
            "resolved_chat_id": self.resolved_chat_id,
            "chat_id_key": self.chat_id_key,
            "directory_lookup_name": self.directory_lookup_name,
            "directory_module": self.directory_module,
            "directory_module_kind": self.directory_module_kind,
            "normalization_symbol": self.normalization_symbol,
            "relationship_record_path": self.relationship_record_path,
            "relationship_counterparty_raw": self.relationship_counterparty_raw,
            "production_counterparty_used": self.production_counterparty_used,
            "production_counterparty_ref_recorded_not_used": PRODUCTION_COUNTERPARTY_REF_NOT_USED,
            "notes": list(self.notes),
        }


@dataclass(frozen=True, slots=True)
class TelegramSerializedRequest:
    method: str
    url_template: str
    payload: Mapping[str, Any]
    chat_id: int | str
    text_chars: int
    text_hash: str
    reply_parameters: Mapping[str, Any] | None
    reply_support: str
    idempotency_key: str
    idempotency_layer: str
    headers: Mapping[str, str]
    credential_names: tuple[str, ...]
    transport_reached: bool
    shape_provenance: str
    notes: tuple[str, ...] = ()

    def to_mapping(self) -> dict[str, Any]:
        return {
            "method": self.method,
            "url_template": self.url_template,
            "payload": dict(self.payload),
            "chat_id": self.chat_id,
            "text_chars": self.text_chars,
            "text_hash": self.text_hash,
            "reply_parameters": None if self.reply_parameters is None else dict(self.reply_parameters),
            "reply_support": self.reply_support,
            "idempotency_key": self.idempotency_key,
            "idempotency_layer": self.idempotency_layer,
            "headers": dict(self.headers),
            "credential_names": list(self.credential_names),
            "credential_values_present": False,
            "transport_reached": self.transport_reached,
            "shape_provenance": self.shape_provenance,
            "notes": list(self.notes),
        }


@dataclass(frozen=True, slots=True)
class TelegramFenceReport:
    proactive_enabled: bool
    action_execution_enabled: bool
    life_runtime_enabled: bool
    agency_enabled: bool
    would_submit: bool
    transport_bound: str
    transport_submit_called_by_real_sender: bool
    socket_calls_blocked: int
    real_sender_modules_present: tuple[str, ...]
    forbidden_services_invoked: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()

    def to_mapping(self) -> dict[str, Any]:
        return {
            "PROACTIVE_ENABLED": self.proactive_enabled,
            "ACTION_EXECUTION_ENABLED": self.action_execution_enabled,
            "life_runtime_enabled": self.life_runtime_enabled,
            "agency_enabled": self.agency_enabled,
            "would_submit": self.would_submit,
            "transport_bound": self.transport_bound,
            "transport_submit_called_by_real_sender": self.transport_submit_called_by_real_sender,
            "socket_calls_blocked": self.socket_calls_blocked,
            "real_sender_modules_present": list(self.real_sender_modules_present),
            "forbidden_services_invoked": list(self.forbidden_services_invoked),
            "notes": list(self.notes),
        }


@dataclass(frozen=True, slots=True)
class SandboxProviderWalk:
    """The in-tree canonical adapter interface walked over the RECORDING fake provider."""

    marker: str
    adapter_symbol: str
    provider_symbol: str
    capability_symbol: str
    action_identity: Mapping[str, Any]
    attempt_identity: Mapping[str, Any]
    transport_result: str | None
    receipt: Mapping[str, Any] | None
    provider_operation_ref: str | None
    provider_invocation_log: tuple[Mapping[str, Any], ...]
    adapter_submit_call_count: int
    transport_submit_calls: int
    dedup_replay_transport_calls: int
    provider_operation_count: int
    dedup_operation_ref_matches: bool
    dedup_note: str
    error: str | None = None

    def to_mapping(self) -> dict[str, Any]:
        return {
            "marker": self.marker,
            "adapter_symbol": self.adapter_symbol,
            "provider_symbol": self.provider_symbol,
            "capability_symbol": self.capability_symbol,
            "action_identity": dict(self.action_identity),
            "attempt_identity": dict(self.attempt_identity),
            "transport_result": self.transport_result,
            "receipt": None if self.receipt is None else dict(self.receipt),
            "provider_operation_ref": self.provider_operation_ref,
            "provider_invocation_log": [dict(row) for row in self.provider_invocation_log],
            "adapter_submit_call_count": self.adapter_submit_call_count,
            "provider_invocations_after_first_submit": self.transport_submit_calls,
            "provider_invocations_after_repeat_submit": self.dedup_replay_transport_calls,
            "provider_operation_count": self.provider_operation_count,
            "dedup_operation_ref_matches": self.dedup_operation_ref_matches,
            "dedup_note": self.dedup_note,
            "error": self.error,
        }


@dataclass(frozen=True, slots=True)
class TelegramBoundaryOutcome:
    dispatch_status: str
    request: ContactMessageActionRequest | None
    serialized: TelegramSerializedRequest | None
    recipient: TelegramRecipientResolution | None
    fence: TelegramFenceReport
    sandbox_walk: SandboxProviderWalk | None
    checks: Mapping[str, bool] = field(default_factory=dict)
    notes: tuple[str, ...] = ()

    @property
    def request_checks_passed(self) -> bool:
        return bool(self.checks) and all(self.checks.values())

    def to_mapping(self) -> dict[str, Any]:
        request = self.request
        return {
            "dispatch_status": self.dispatch_status,
            "contact_request": None
            if request is None
            else {
                "request_id": request.request_id,
                "draft_ref": request.draft_ref,
                "intent_ref": request.intent_ref,
                "decision_ref": request.decision_ref,
                "recipient_ref": request.recipient_ref,
                "channel": request.channel,
                "target_ref": request.target_ref,
                "payload_ref": request.payload_ref,
                "content_hash": request.content_hash,
                "idempotency_key": request.idempotency_key,
                "correlation_id": request.correlation_id,
                "would_submit": request.would_submit,
                "payload_text_chars": len(str(request.payload_data.get("text") or "")),
            },
            "serialized_request": None if self.serialized is None else self.serialized.to_mapping(),
            "recipient": None if self.recipient is None else self.recipient.to_mapping(),
            "fence": self.fence.to_mapping(),
            "sandbox_provider_walk": None if self.sandbox_walk is None else self.sandbox_walk.to_mapping(),
            "checks": dict(self.checks),
            "notes": list(self.notes),
        }


@dataclass(frozen=True, slots=True)
class TelegramBoundaryVerdict:
    telegram_adapter_bound: bool
    telegram_dispatch_request_build_verified: bool
    telegram_real_side_effect_enabled: bool
    classification: str
    evidence: Mapping[str, Any] = field(default_factory=dict)

    def to_mapping(self) -> dict[str, Any]:
        return {
            "telegram_adapter_bound": self.telegram_adapter_bound,
            "telegram_dispatch_request_build_verified": self.telegram_dispatch_request_build_verified,
            "telegram_real_side_effect_enabled": self.telegram_real_side_effect_enabled,
            "classification": self.classification,
            "evidence": dict(self.evidence),
        }


# ==========================================================================
# 4. fences
# ==========================================================================
_SOCKET_CALL_BLOCKED = {"count": 0}


@contextlib.contextmanager
def no_socket_available():
    """Any socket construction inside the boundary raises. The fence, not a promise."""
    originals = {
        name: getattr(socket, name, None)
        for name in ("socket", "create_connection", "socketpair")
    }

    def _refuse(*_args: Any, **_kwargs: Any) -> Any:
        _SOCKET_CALL_BLOCKED["count"] += 1
        raise TelegramFenceViolationError(
            "a socket call was attempted inside the CT0-10 Telegram boundary; refused "
            "(the boundary must stop before the transport fence)"
        )

    socket.socket = _refuse  # type: ignore[assignment]
    socket.create_connection = _refuse  # type: ignore[assignment]
    if originals.get("socketpair") is not None:
        socket.socketpair = _refuse  # type: ignore[assignment]
    try:
        yield
    finally:
        for name, value in originals.items():
            if value is not None:
                setattr(socket, name, value)


def forbidden_modules_present() -> tuple[str, ...]:
    present: list[str] = []
    for name, module in list(sys.modules.items()):
        base = name.split(".")[-1]
        if name in FORBIDDEN_REAL_SENDERS or base in FORBIDDEN_REAL_SENDERS:
            present.append(name)
    return tuple(sorted(set(present)))


def assert_no_real_sender_imported() -> tuple[str, ...]:
    present = forbidden_modules_present()
    if present:
        raise TelegramFenceViolationError(
            "real sender/service modules are imported and must not be: " + ", ".join(present)
        )
    return present


def first_existing(candidates: Sequence[str | Path]) -> Path | None:
    for candidate in candidates:
        try:
            path = Path(candidate)
        except (TypeError, ValueError):  # pragma: no cover - defensive
            continue
        if path.exists():
            return path
    return None


def load_module_from_file(module_name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(module_name, str(path))
    if spec is None or spec.loader is None:  # pragma: no cover - defensive
        raise TelegramBoundaryError(f"cannot load {module_name} from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def ar0_action_id(idempotency_key: str) -> str:
    """Real AR-0 identity: action_id = actn:<sha256('actn:'+idem)[:16]> (:2439)."""
    import hashlib

    return "actn:" + hashlib.sha256(f"actn:{idempotency_key}".encode("utf-8")).hexdigest()[:16]


def ar0_submission_key(idempotency_key: str, attempt_no: int) -> str:
    """Real AR-0 identity: submission_key = subk:<sha256(f'{idem}:att:{n}')[:16]> (:2734)."""
    import hashlib

    return "subk:" + hashlib.sha256(
        f"{idempotency_key}:att:{attempt_no}".encode("utf-8")
    ).hexdigest()[:16]


def ct0_attempt_id(action_ref: str, attempt_no: int) -> str:
    """CT0-10-side attempt identity. The real `aatt:` derivation is not reproduced."""
    import hashlib

    return "aatt:" + hashlib.sha256(
        f"ct0_10:{action_ref}:{attempt_no}".encode("utf-8")
    ).hexdigest()[:16]


# ==========================================================================
# 5. the boundary
# ==========================================================================
class CanonicalTelegramBoundary:
    """Wired to the real adapter code path; fenced before the transport."""

    def __init__(
        self,
        *,
        contact_recipient_ref: str,
        gate: ContactActionGate | None = None,
        isolated_root: str | Path | None = None,
        directory_lookup_name: str | None = None,
        relationship_record_path: str | Path | None = None,
        allowed_recipients: Sequence[str] = (),
        allowed_channels: Sequence[str] = (),
    ) -> None:
        self.contact_recipient_ref = contact_recipient_ref
        self.gate = gate if gate is not None else ContactActionGate()
        if not isinstance(self.gate, ContactActionGate):
            raise TelegramBoundaryError("gate must be a frozen ContactActionGate")
        if self.gate.would_submit:
            raise TelegramFenceViolationError(
                "PROACTIVE_ENABLED/ACTION_EXECUTION_ENABLED must be false for the CT0-10 "
                "boundary; refusing to construct an armed boundary"
            )
        root = first_existing((isolated_root,) if isolated_root else ISOLATED_ROOT_CANDIDATES)
        if root is None:
            raise TelegramBoundaryError(
                "an isolated configuration root is required; refusing to use a production home"
            )
        self.isolated_root = assert_isolated_store_root(root, namespace=NAMESPACE_ISOLATED_TEST)
        self.directory_lookup_name = directory_lookup_name or contact_recipient_ref.split(":", 1)[-1]
        self.relationship_record_path = (
            Path(relationship_record_path)
            if relationship_record_path is not None
            else self.isolated_root / "canonical" / "relationship.yaml"
        )
        self.allowed_recipients = tuple(allowed_recipients)
        self.allowed_channels = tuple(allowed_channels)
        self.last_outcome: TelegramBoundaryOutcome | None = None
        self.socket_calls_blocked = 0

    # -- recipient resolution (order 69: isolated config only) ------------
    def _telegram_ids_module(self) -> tuple[Any, str]:
        path = first_existing(REAL_TELEGRAM_IDS_CANDIDATES)
        if path is None:
            raise TelegramRecipientError(
                "the real plugins/platforms/telegram/telegram_ids.py was not found; refusing "
                "to normalise a Telegram chat id by hand"
            )
        return load_module_from_file("ct0_10_real_telegram_ids", path), str(path)

    def _channel_directory_module(self) -> tuple[Any, str]:
        """Execute the REAL gateway channel directory with isolated accessors.

        `gateway/channel_directory.py` resolves DIRECTORY_PATH through
        `hermes_cli.config.get_hermes_home()` and imports `utils.atomic_json_write`. CT0-10
        supplies an isolated home and replaces the write helper with a refusing stub, so the
        real matching code runs and the real write path is impossible. That substitution is
        recorded in the returned provenance string.
        """
        path = first_existing(REAL_CHANNEL_DIRECTORY_CANDIDATES)
        if path is None:
            raise TelegramRecipientError(
                "the real gateway/channel_directory.py was not found; refusing to invent a "
                "channel directory"
            )
        stubs: dict[str, Any] = {}
        saved: dict[str, Any] = {}
        config_mod = types.ModuleType("hermes_cli.config")
        config_mod.get_hermes_home = lambda: self.isolated_root  # type: ignore[attr-defined]
        utils_mod = types.ModuleType("utils")

        def _refuse_write(*_args: Any, **_kwargs: Any) -> Any:
            raise TelegramFenceViolationError(
                "the real channel directory tried to persist to disk; the CT0-10 boundary is "
                "read-only"
            )

        utils_mod.atomic_json_write = _refuse_write  # type: ignore[attr-defined]
        stubs["hermes_cli.config"] = config_mod
        stubs["utils"] = utils_mod
        hermes_cli_pkg = types.ModuleType("hermes_cli")
        hermes_cli_pkg.__path__ = []  # type: ignore[attr-defined]
        stubs["hermes_cli"] = hermes_cli_pkg
        for name, module in stubs.items():
            saved[name] = sys.modules.get(name)
            sys.modules[name] = module
        try:
            module = load_module_from_file("ct0_10_real_channel_directory", path)
        finally:
            for name, previous in saved.items():
                if previous is None:
                    sys.modules.pop(name, None)
                else:
                    sys.modules[name] = previous
        provenance = (
            f"{path} executed read-only; hermes home accessor supplied by CT0-10 "
            f"(isolated root {self.isolated_root}); the module's write helper "
            "(utils.atomic_json_write) replaced by a refusing stub"
        )
        return module, provenance

    def resolve_recipient(self) -> TelegramRecipientResolution:
        ids_module, ids_path = self._telegram_ids_module()
        directory_module, directory_provenance = self._channel_directory_module()
        directory_id = directory_module.resolve_channel_name("telegram", self.directory_lookup_name)
        if directory_id is None:
            raise TelegramRecipientError(
                f"the real channel directory did not resolve {self.directory_lookup_name!r}; "
                "fail closed rather than guessing a destination"
            )
        relationship_raw: str | None = None
        if self.relationship_record_path.is_file():
            text = self.relationship_record_path.read_text(encoding="utf-8")
            for line in text.splitlines():
                if line.strip().startswith("counterparty_ref:"):
                    relationship_raw = line.split(":", 1)[1].strip().strip("'\"")
                    break
        if relationship_raw is None:
            raise TelegramRecipientError(
                "the isolated relationship record carries no counterparty_ref field; the "
                "canonical contact->destination fact has no other owner"
            )
        relationship_id = relationship_raw.split(" ")[0].split(":", 1)[-1]
        if relationship_id != str(directory_id):
            raise TelegramRecipientError(
                "the relationship record's counterparty_ref and the channel directory disagree "
                f"({relationship_id!r} != {str(directory_id)!r}); refusing to pick one"
            )
        if PRODUCTION_COUNTERPARTY_REF_NOT_USED.split(":", 1)[-1] in {str(directory_id), relationship_id}:
            raise TelegramFenceViolationError(
                "the resolved destination is the PRODUCTION counterparty; CT0-10 must only "
                "resolve isolated test recipients (order 69)"
            )
        normalized = ids_module.normalize_telegram_chat_id(directory_id)
        return TelegramRecipientResolution(
            contact_recipient_ref=self.contact_recipient_ref,
            resolved_chat_id=normalized,
            chat_id_key=ids_module.telegram_chat_id_key(directory_id),
            directory_lookup_name=self.directory_lookup_name,
            directory_module=directory_provenance,
            directory_module_kind="REAL_MODULE_BY_FILE_WITH_ISOLATED_ACCESSORS",
            normalization_symbol=f"normalize_telegram_chat_id/telegram_chat_id_key ({ids_path})",
            relationship_record_path=str(self.relationship_record_path),
            relationship_counterparty_raw=relationship_raw,
            notes=(
                "recipient resolution used the real code paths against an ISOLATED test "
                "configuration; no real user target was resolved or contacted (order 69)",
                "the production counterparty_ref is recorded in "
                "PRODUCTION_COUNTERPARTY_REF_NOT_USED and was refused if it ever matched",
                "the canonical contact->destination fact is a YAML field on the relationship "
                "record (canonical/relationship.yaml counterparty_ref), not a mapping table",
            ),
        )

    # -- serialized dispatch request --------------------------------------
    def build_serialized_request(
        self,
        *,
        text: str,
        resolution: TelegramRecipientResolution,
        idempotency_key: str,
        reply_to: str | None = None,
    ) -> TelegramSerializedRequest:
        if not isinstance(text, str) or not text:
            raise TelegramRequestBuildError("request text must be a non-empty string")
        if reply_to is not None:
            raise TelegramRequestBuildError(
                "the real native owner's send_message() has no reply parameter "
                "(telegram_adapter.py:119-128); CT0-10 will not invent a field the owner "
                "does not have"
            )
        if len(text) > MAX_TELEGRAM_TEXT:
            raise TelegramRequestBuildError(
                f"text of {len(text)} chars exceeds MAX_TELEGRAM_TEXT={MAX_TELEGRAM_TEXT}; the "
                "real native owner refuses it and CT0-10 never truncates or splits (split_send "
                "is a forbidden sender)"
            )
        payload = {"chat_id": resolution.resolved_chat_id, "text": text}
        return TelegramSerializedRequest(
            method=NATIVE_SEND_METHOD,
            url_template=f"{TELEGRAM_API_URL_TEMPLATE}/{NATIVE_SEND_METHOD}",
            payload=payload,
            chat_id=resolution.resolved_chat_id,
            text_chars=len(text),
            text_hash=payload_hash({"text": text}),
            reply_parameters=None,
            reply_support="UNSUPPORTED_BY_REAL_NATIVE_OWNER",
            idempotency_key=idempotency_key,
            idempotency_layer=(
                "AR-0 / CT0-6 action layer (ar0_act:<sha256>); NOT a Bot API field -- the "
                "provider-side dedup key is FakeMessageProvider.operations_by_idem_key "
                "(action_reality_ledger.py:1389)"
            ),
            headers=dict(NATIVE_SEND_HEADERS),
            credential_names=CREDENTIAL_NAMES_ONLY,
            transport_reached=False,
            shape_provenance=REQUEST_SHAPE_PROVENANCE,
            notes=(
                "reply parameters: the native owner supports NO reply field; the hermes "
                "gateway owner does (adapter.py:4415 reply_to=None) but is not importable here "
                "-- recorded, not invented",
                "the URL template carries the credential NAME only; no credential value is "
                "read, stored or logged anywhere in CT0-10",
            ),
        )

    # -- the canonical in-tree boundary walk ------------------------------
    def _action_reality_module(self) -> tuple[Any, str]:
        path = first_existing(ACTION_REALITY_LEDGER_CANDIDATES)
        if path is None:
            raise TelegramBoundaryError("scripts/action_reality_ledger.py was not found")
        scripts_dir = path.parent
        if str(scripts_dir) not in sys.path:
            sys.path.insert(0, str(scripts_dir))
        return load_module_from_file("ct0_10_real_action_reality_ledger", path), str(path)

    def walk_sandbox_provider(
        self,
        *,
        contact_request: ContactMessageActionRequest,
        text: str,
        attempt_no: int = 1,
    ) -> SandboxProviderWalk:
        """Submit the frozen request to the in-tree RECORDING fake provider.

        This is the canonical adapter interface (`ActionAdapterProtocol` /
        `MessageActionAdapter`, scripts/action_reality_ledger.py:1322/:1451) over the
        in-process `FakeMessageProvider` (:1354). It performs zero I/O: the fake records the
        invocation in memory. The capability is issued by the real authority guard, which
        refuses production roots and refuses production executor capabilities.
        """
        module, module_path = self._action_reality_module()
        idem = contact_request.idempotency_key
        action_id = ar0_action_id(idem)
        submission_key = ar0_submission_key(idem, attempt_no)
        attempt_id = ct0_attempt_id(action_id, attempt_no)
        contact_action = CanonicalActionRef(
            action_ref=action_id,
            contact_submission_key=idem,
            canonical_submission_key=submission_key,
        )
        provider = module.FakeMessageProvider()
        adapter = module.MessageActionAdapter(provider)
        capability = module.ActionRealityAuthority.issue_sandbox_executor_capability(
            caller_module="sandbox_action_harness", namespace=NAMESPACE_ISOLATED_TEST
        )
        self.socket_calls_blocked = _SOCKET_CALL_BLOCKED["count"]
        with no_socket_available():
            outcome = adapter.submit(
                executor_capability=capability,
                action={"action_id": action_id, "idempotency_key": idem, "target_ref": contact_request.target_ref},
                attempt={"attempt_id": attempt_id, "submission_key": submission_key},
                payload={"channel": contact_request.channel, "text": text},
            )
            first_transport_calls = len(provider.invocation_log)
            # Provider-side idempotency: the SAME key must not create a second operation.
            second = adapter.submit(
                executor_capability=capability,
                action={"action_id": action_id, "idempotency_key": idem, "target_ref": contact_request.target_ref},
                attempt={"attempt_id": attempt_id, "submission_key": submission_key},
                payload={"channel": contact_request.channel, "text": text},
            )
            dedup_transport_calls = len(provider.invocation_log)
        self.socket_calls_blocked = _SOCKET_CALL_BLOCKED["count"]
        receipt = outcome.receipt
        return SandboxProviderWalk(
            marker=SANDBOX_PROVIDER_WALK_MARKER,
            adapter_symbol=f"{module_path}:1451 MessageActionAdapter",
            provider_symbol=f"{module_path}:1354 FakeMessageProvider",
            capability_symbol=f"{module_path}:452 issue_sandbox_executor_capability",
            action_identity=contact_action.to_mapping(),
            attempt_identity={
                "attempt_id": attempt_id,
                "attempt_id_derivation": "ct0-10 side derivation (the real aatt: formula was not reproduced)",
                "submission_key": submission_key,
                "submission_key_derivation": f"real AR-0 formula subk:<sha256('{idem}:att:{attempt_no}')[:16]> (:2734)",
                "attempt_no": attempt_no,
            },
            transport_result=outcome.transport_result,
            receipt=None
            if receipt is None
            else {
                "receipt_id": receipt.receipt_id,
                "receipt_kind": receipt.receipt_kind,
                "status_claim": receipt.status_claim,
                "provider": receipt.provider,
                "provider_operation_ref": receipt.provider_operation_ref,
                "details": dict(receipt.details),
            },
            provider_operation_ref=outcome.provider_operation_ref,
            provider_invocation_log=tuple(provider.invocation_log),
            adapter_submit_call_count=adapter.submit_call_count,
            transport_submit_calls=first_transport_calls,
            dedup_replay_transport_calls=dedup_transport_calls,
            provider_operation_count=len(provider.operations_by_idem_key),
            dedup_operation_ref_matches=(
                second.provider_operation_ref == outcome.provider_operation_ref
                and second.provider_operation_ref is not None
            ),
            dedup_note=(
                "the real FakeMessageProvider marks deduplication only on the RETURNED copy "
                "(action_reality_ledger.py:1389-1392: existing = deepcopy(...); "
                "existing['deduplicated'] = True), so the walk asserts the observable: two "
                "adapter submissions, two provider invocations, ONE provider operation and an "
                "identical provider_operation_ref"
            ),
        )

    # -- the smoke ---------------------------------------------------------
    def smoke(
        self,
        *,
        intent: Any,
        decision: Any,
        draft: Any,
        channel: str,
        created_at: str,
        walk_sandbox_provider: bool = True,
    ) -> TelegramBoundaryOutcome:
        assert_no_real_sender_imported()
        self.socket_calls_blocked = _SOCKET_CALL_BLOCKED["count"]
        notes: list[str] = []
        try:
            resolution = self.resolve_recipient()
        except TelegramBoundaryError as exc:
            notes.append(f"recipient resolution failed closed: {exc}")
            raise
        try:
            contact_request = build_message_action_request(
                intent=intent,
                decision=decision,
                draft=draft,
                gate=self.gate,
                channel=channel,
                created_at=created_at,
            )
        except Exception as exc:
            raise TelegramRequestBuildError(
                f"the frozen CT0-6 request builder refused this draft: {exc}"
            ) from exc
        text = str(contact_request.payload_data.get("text") or "")
        serialized = self.build_serialized_request(
            text=text,
            resolution=resolution,
            idempotency_key=contact_request.idempotency_key,
        )
        checks = {
            "recipient_resolved_through_real_surface": serialized.chat_id == resolution.resolved_chat_id,
            "contact_request_target_matches_draft_recipient": contact_request.target_ref == draft.recipient_ref,
            "serialized_chat_id_is_isolated_test_target": str(serialized.chat_id)
            != PRODUCTION_COUNTERPARTY_REF_NOT_USED.split(":", 1)[-1],
            "text_matches_frozen_draft_content": text == draft.content,
            "text_within_real_owner_limit": serialized.text_chars <= MAX_TELEGRAM_TEXT,
            "reply_parameters_absent_like_the_real_owner": serialized.reply_parameters is None,
            "idempotency_key_bound_to_frozen_request": serialized.idempotency_key
            == contact_request.idempotency_key,
            "url_carries_credential_name_only": "<TELEGRAM_BOT_TOKEN>" in serialized.url_template,
            "transport_not_reached": serialized.transport_reached is False,
        }
        dispatch_status = "BLOCKED_BY_GATE" if not self.gate.would_submit else "WOULD_DISPATCH"
        if self.gate.would_submit:  # pragma: no cover - the fence refuses this upstream
            raise TelegramFenceViolationError(
                "the CT0-10 Telegram boundary never dispatches: kill switches must stay off"
            )
        sandbox_walk = None
        if walk_sandbox_provider:
            sandbox_walk = self.walk_sandbox_provider(
                contact_request=contact_request, text=text
            )
            notes.append(
                "the in-tree recording fake provider was walked to prove the canonical "
                "adapter interface binds; it is NOT the Telegram transport"
            )
        notes.extend(
            (
                "the boundary stops at the serialized request: no real sender is imported, "
                "no socket is opened, and the URL exists only as a template with the "
                "credential NAME in it",
                "dispatch_status=BLOCKED_BY_GATE: PROACTIVE_ENABLED and "
                "ACTION_EXECUTION_ENABLED are false, so nothing downstream may act",
            )
        )
        fence = TelegramFenceReport(
            proactive_enabled=self.gate.proactive_enabled,
            action_execution_enabled=self.gate.action_execution_enabled,
            life_runtime_enabled=self.gate.life_runtime_enabled,
            agency_enabled=self.gate.agency_enabled,
            would_submit=self.gate.would_submit,
            transport_bound=(
                "in-tree ActionAdapterProtocol/MessageActionAdapter + FakeMessageProvider "
                "(recording, in-process) -- the real Telegram transport is NOT bound"
            ),
            transport_submit_called_by_real_sender=False,
            socket_calls_blocked=self.socket_calls_blocked,
            real_sender_modules_present=forbidden_modules_present(),
            notes=(
                "forbidden real senders/services: " + ", ".join(FORBIDDEN_REAL_SENDERS[:8]) + ", ...",
                "forbidden side-effect-capable services: " + ", ".join(FORBIDDEN_SERVICES),
            ),
        )
        outcome = TelegramBoundaryOutcome(
            dispatch_status=dispatch_status,
            request=contact_request,
            serialized=serialized,
            recipient=resolution,
            fence=fence,
            sandbox_walk=sandbox_walk,
            checks=checks,
            notes=tuple(notes),
        )
        self.last_outcome = outcome
        assert_no_real_sender_imported()
        return outcome

    # -- verdict ------------------------------------------------------------
    def verdict(self, outcome: TelegramBoundaryOutcome | None = None) -> TelegramBoundaryVerdict:
        outcome = outcome or self.last_outcome
        if outcome is None:
            raise TelegramBoundaryError("no smoke outcome; run smoke() first")
        present = assert_no_real_sender_imported()
        bound = outcome.request is not None and outcome.recipient is not None
        build_verified = bool(outcome.serialized is not None and outcome.request_checks_passed)
        side_effect_enabled = bool(
            self.gate.would_submit
            or outcome.serialized is None
            or outcome.serialized.transport_reached
            or present
        )
        return TelegramBoundaryVerdict(
            telegram_adapter_bound=bound,
            telegram_dispatch_request_build_verified=build_verified,
            telegram_real_side_effect_enabled=side_effect_enabled,
            classification="WIRED_BUT_FENCED",
            evidence={
                "transport_bound": outcome.fence.transport_bound,
                "dispatch_status": outcome.dispatch_status,
                "PROACTIVE_ENABLED": self.gate.proactive_enabled,
                "ACTION_EXECUTION_ENABLED": self.gate.action_execution_enabled,
                "serialized_request": None
                if outcome.serialized is None
                else {
                    "method": outcome.serialized.method,
                    "url_template": outcome.serialized.url_template,
                    "chat_id": outcome.serialized.chat_id,
                    "text_chars": outcome.serialized.text_chars,
                    "reply_parameters": outcome.serialized.reply_parameters,
                    "idempotency_key": outcome.serialized.idempotency_key,
                },
                "sandbox_provider_walk": None
                if outcome.sandbox_walk is None
                else {
                    "marker": outcome.sandbox_walk.marker,
                    "transport_result": outcome.sandbox_walk.transport_result,
                    "provider_invocations_after_first_submit": outcome.sandbox_walk.transport_submit_calls,
                    "provider_invocations_after_repeat_submit": outcome.sandbox_walk.dedup_replay_transport_calls,
                },
                "real_sender_modules_present": list(present),
                "socket_calls_blocked": outcome.fence.socket_calls_blocked,
            },
        )


__all__ = [
    "BOUNDARY_VERSION",
    "CREDENTIAL_NAMES_ONLY",
    "CanonicalTelegramBoundary",
    "FORBIDDEN_REAL_SENDERS",
    "FORBIDDEN_SERVICES",
    "MAX_TELEGRAM_TEXT",
    "MODULE_ID",
    "NATIVE_SEND_METHOD",
    "ORDER_SECTIONS",
    "PRODUCTION_COUNTERPARTY_REF_NOT_USED",
    "PRODUCTION_COUNTERPARTY_USED",
    "REAL_GATEWAY_OWNER",
    "REAL_NATIVE_OWNER",
    "REPLY_SUPPORT_EVIDENCE",
    "REQUEST_SHAPE_PROVENANCE",
    "SANDBOX_PROVIDER_WALK_MARKER",
    "TELEGRAM_API_URL_TEMPLATE",
    "TelegramBoundaryError",
    "TelegramBoundaryOutcome",
    "TelegramBoundaryVerdict",
    "TelegramFenceReport",
    "TelegramFenceViolationError",
    "TelegramRecipientError",
    "TelegramRecipientResolution",
    "TelegramRequestBuildError",
    "TelegramSerializedRequest",
    "SandboxProviderWalk",
    "ar0_action_id",
    "ar0_submission_key",
    "assert_no_real_sender_imported",
    "ct0_attempt_id",
    "forbidden_modules_present",
    "no_socket_available",
]
