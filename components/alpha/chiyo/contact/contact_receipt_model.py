"""Immutable CT0-8 transport observation contracts; never an action lifecycle owner."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
import re
from typing import Final

RECEIPT_SCHEMA: Final = "chiyo.contact.transport_receipt_evidence.v1"
RECEIPT_POLICY: Final = "ct0.contact_receipt_authority.v1"
SANDBOX_NAMESPACE: Final = "sandbox.telegram"
OBSERVATION_KINDS: Final = frozenset({
    "SUBMIT_OBSERVED", "PROVIDER_ACCEPTED", "DELIVERY_CONFIRMED", "DELIVERY_FAILED",
    "FAILED_BEFORE_ACCEPT", "READ_CONFIRMED", "STATUS_UNKNOWN", "TRANSPORT_ERROR",
})
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class ContactReceiptContractError(ValueError):
    """Invalid frozen receipt evidence or correction contract."""


def _ref(value: object, prefix: str, name: str) -> str:
    if not isinstance(value, str) or not value.startswith(prefix) or len(value) <= len(prefix) or len(value) > 512:
        raise ContactReceiptContractError(f"{name} must be a bounded {prefix} reference")
    if any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in value):
        raise ContactReceiptContractError(f"{name} contains whitespace or control characters")
    return value


def _time(value: object, name: str) -> str:
    if not isinstance(value, str):
        raise ContactReceiptContractError(f"{name} must be an aware ISO timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ContactReceiptContractError(f"{name} must be an aware ISO timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ContactReceiptContractError(f"{name} must be timezone-aware")
    return value


def stable_hash(value: object) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class ContactTransportReceiptEvidence:
    evidence_ref: str
    semantic_fingerprint: str
    action_ref: str
    submission_key: str
    namespace: str
    transport_record_ref: str
    observation_kind: str
    provider_status: str
    recipient_ref: str
    channel: str
    payload_hash: str
    content_hash: str
    provider_message_ref: str | None
    provider_time: str | None
    observed_at: str
    provenance_refs: tuple[str, ...]
    effect_applied: bool
    effect_count_for_key: int
    recorded_at: str = ""
    schema_version: str = RECEIPT_SCHEMA
    policy_version: str = RECEIPT_POLICY

    def __post_init__(self) -> None:
        _ref(self.evidence_ref, "rece:", "evidence_ref")
        if not SHA256_RE.fullmatch(self.semantic_fingerprint):
            raise ContactReceiptContractError("semantic_fingerprint must be SHA-256")
        _ref(self.action_ref, "actn:", "action_ref")
        _ref(self.submission_key, "ar0_act:", "submission_key")
        if self.namespace != SANDBOX_NAMESPACE:
            raise ContactReceiptContractError("receipt namespace must be sandbox.telegram")
        _ref(self.transport_record_ref, "tlog:", "transport_record_ref")
        if self.observation_kind not in OBSERVATION_KINDS:
            raise ContactReceiptContractError("unsupported observation_kind")
        if not isinstance(self.provider_status, str) or not self.provider_status or len(self.provider_status) > 80:
            raise ContactReceiptContractError("provider_status is required and bounded")
        _ref(self.recipient_ref, "recipient:", "recipient_ref")
        _ref(self.channel, "channel:", "channel")
        for name in ("payload_hash", "content_hash"):
            if not isinstance(getattr(self, name), str) or not SHA256_RE.fullmatch(getattr(self, name)):
                raise ContactReceiptContractError(f"{name} must be SHA-256")
        if self.provider_message_ref is not None:
            _ref(self.provider_message_ref, "pmsg:", "provider_message_ref")
        if self.provider_time is not None:
            _time(self.provider_time, "provider_time")
        _time(self.observed_at, "observed_at")
        _time(self.recorded_at, "recorded_at")
        if type(self.effect_applied) is not bool or type(self.effect_count_for_key) is not int or self.effect_count_for_key < 0:
            raise ContactReceiptContractError("effect fields must be explicit boolean and non-negative integer")
        if self.observation_kind == "FAILED_BEFORE_ACCEPT":
            if self.provider_status != "FAILED_BEFORE_ACCEPT" or self.effect_applied or self.effect_count_for_key != 0:
                raise ContactReceiptContractError("pre-accept failure requires explicit zero-effect evidence")
        elif self.observation_kind in {"SUBMIT_OBSERVED", "PROVIDER_ACCEPTED", "DELIVERY_CONFIRMED", "READ_CONFIRMED"}:
            if not self.effect_applied or self.effect_count_for_key < 1:
                raise ContactReceiptContractError("accepted observations require a recorded fake effect")
        elif self.observation_kind == "DELIVERY_FAILED":
            if not self.effect_applied or self.effect_count_for_key < 1:
                raise ContactReceiptContractError("delivery failure is not proof of zero side effects")
        if not isinstance(self.provenance_refs, tuple) or len(self.provenance_refs) > 256:
            raise ContactReceiptContractError("provenance_refs must be a bounded tuple")
        if len(set(self.provenance_refs)) != len(self.provenance_refs):
            raise ContactReceiptContractError("provenance_refs must be unique")
        for ref in self.provenance_refs:
            if not isinstance(ref, str) or ":" not in ref or any(c.isspace() for c in ref):
                raise ContactReceiptContractError("invalid provenance reference")
        if self.schema_version != RECEIPT_SCHEMA or not self.policy_version:
            raise ContactReceiptContractError("unsupported schema or missing policy version")
        material = (self.action_ref, self.submission_key, self.namespace, self.transport_record_ref,
                    self.observation_kind, self.provider_status, self.recipient_ref, self.channel,
                    self.payload_hash, self.content_hash, self.provider_message_ref, self.provider_time,
                    self.observed_at, self.provenance_refs, self.effect_applied, self.effect_count_for_key,
                    self.schema_version, self.policy_version)
        expected = stable_hash(material)
        if self.semantic_fingerprint != expected or self.evidence_ref != f"rece:{expected}":
            raise ContactReceiptContractError("evidence identity is not derived from its semantic content")

    @classmethod
    def create(cls, **fields: object) -> "ContactTransportReceiptEvidence":
        fields["schema_version"] = fields.get("schema_version", RECEIPT_SCHEMA)
        fields["policy_version"] = fields.get("policy_version", RECEIPT_POLICY)
        fields.setdefault("provider_message_ref", None)
        fields.setdefault("provider_time", None)
        fields.setdefault("effect_applied", False)
        fields.setdefault("effect_count_for_key", 0)
        material_names = ("action_ref", "submission_key", "namespace", "transport_record_ref",
                          "observation_kind", "provider_status", "recipient_ref", "channel", "payload_hash",
                          "content_hash", "provider_message_ref", "provider_time", "observed_at",
                          "provenance_refs", "effect_applied", "effect_count_for_key", "schema_version", "policy_version")
        digest = stable_hash(tuple(fields[name] for name in material_names))
        fields["semantic_fingerprint"] = digest
        fields["evidence_ref"] = f"rece:{digest}"
        return cls(**fields)  # type: ignore[arg-type]


@dataclass(frozen=True, slots=True)
class ReceiptSupersession:
    supersession_ref: str
    action_ref: str
    old_evidence_ref: str
    new_evidence_ref: str
    reason: str
    authority_ref: str
    recorded_at: str

    def __post_init__(self) -> None:
        _ref(self.supersession_ref, "rsup:", "supersession_ref")
        _ref(self.action_ref, "actn:", "action_ref")
        _ref(self.old_evidence_ref, "rece:", "old_evidence_ref")
        _ref(self.new_evidence_ref, "rece:", "new_evidence_ref")
        _ref(self.authority_ref, "rauth:", "authority_ref")
        if self.old_evidence_ref == self.new_evidence_ref or not isinstance(self.reason, str) or not self.reason.strip():
            raise ContactReceiptContractError("supersession requires distinct evidence and a reason")
        _time(self.recorded_at, "recorded_at")
        expected = stable_hash((self.action_ref, self.old_evidence_ref, self.new_evidence_ref,
                                self.reason, self.authority_ref, self.recorded_at))
        if self.supersession_ref != f"rsup:{expected}":
            raise ContactReceiptContractError("supersession identity mismatch")

    @classmethod
    def create(cls, *, action_ref: str, old_evidence_ref: str, new_evidence_ref: str,
               reason: str, authority_ref: str, recorded_at: str) -> "ReceiptSupersession":
        digest = stable_hash((action_ref, old_evidence_ref, new_evidence_ref, reason, authority_ref, recorded_at))
        return cls(f"rsup:{digest}", action_ref, old_evidence_ref, new_evidence_ref, reason, authority_ref, recorded_at)


__all__ = ["ContactReceiptContractError", "ContactTransportReceiptEvidence", "ReceiptSupersession",
           "OBSERVATION_KINDS", "RECEIPT_POLICY", "RECEIPT_SCHEMA", "SANDBOX_NAMESPACE", "stable_hash"]
