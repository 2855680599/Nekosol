"""Typed C6 source evidence and fail-closed scope/quota policy contracts.

No production adapter is shipped: Gate B remains PENDING until a separately owned canonical
source reader and scope/quota authority are supplied and qualified.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Protocol


class SourceKind(str, Enum):
    DELIVERED_USER_TURN = "DELIVERED_USER_TURN"
    DELIVERED_ASSISTANT_TURN = "DELIVERED_ASSISTANT_TURN"
    CANONICAL_RUNTIME_EVENT = "CANONICAL_RUNTIME_EVENT"
    MODEL_IMAGINED_SOURCE = "MODEL_IMAGINED_SOURCE"
    PROMPT_TEXT = "PROMPT_TEXT"
    RAW_UNVERIFIED_STRING = "RAW_UNVERIFIED_STRING"
    SHADOW_TEST_EVENT = "SHADOW_TEST_EVENT"
    OPERATOR_FIXTURE = "OPERATOR_FIXTURE"


@dataclass(frozen=True)
class TrustedSourceRef:
    source_id: str
    source_kind: SourceKind
    producer: str
    subject_id: str
    occurred_at: float
    visibility_scope: str
    source_revision: str
    content_binding: str
    delivered_status: str
    meaning_version: str = "lifesupply.trusted_source_ref.v1"


@dataclass(frozen=True)
class DeliveredSourceEvidence:
    source_id: str
    source_kind: SourceKind
    producer: str
    subject_id: str
    occurred_at: float
    visibility_scope: str
    source_revision: str
    content_binding: str
    delivered_status: str


class EvidenceStatus(str, Enum):
    FOUND = "FOUND"
    NOT_FOUND = "NOT_FOUND"
    UNAVAILABLE = "UNAVAILABLE"
    UNTRUSTED = "UNTRUSTED"
    CONFLICT = "CONFLICT"


@dataclass(frozen=True)
class EvidenceResult:
    status: EvidenceStatus
    evidence: DeliveredSourceEvidence | None = None
    detail: str = ""


class ScopeStatus(str, Enum):
    ALLOW = "ALLOW"
    DENY = "DENY"
    UNKNOWN = "UNKNOWN"


class SourceEvidencePort(Protocol):
    def resolve_source(self, ref: TrustedSourceRef) -> EvidenceResult: ...
    def verify_source(self, ref: TrustedSourceRef) -> EvidenceResult: ...
    def read_source_scope(self, ref: TrustedSourceRef) -> str | None: ...
    def read_source_subject(self, ref: TrustedSourceRef) -> str | None: ...
    def read_source_delivery_state(self, ref: TrustedSourceRef) -> str | None: ...


@dataclass(frozen=True)
class InquiryAdmissionPolicyV1:
    policy_id: str = "inquiry-admission-v1-conservative"
    max_open: int = 3
    max_new_per_window: int = 2
    window_seconds: int = 86400
    default_expiry_seconds: int = 30 * 86400
    max_source_refs: int = 3
    max_topic_length: int = 240

    def __post_init__(self):
        if not self.policy_id or min(self.max_open, self.max_new_per_window,
            self.window_seconds, self.default_expiry_seconds, self.max_source_refs,
            self.max_topic_length) < 1:
            raise ValueError("inquiry policy limits must be finite positive values")


@dataclass(frozen=True)
class AdmissionVerdict:
    status: str
    reason: str
    policy_id: str
    allowed_sources: tuple[str, ...] = ()


class TrustedSourceUnavailable(RuntimeError):
    pass


class SourceScopePolicy:
    """Only verified delivered turns and explicit PRIVATE_SELF sources can pass V1."""
    ACCEPTED_KINDS = {SourceKind.DELIVERED_USER_TURN, SourceKind.DELIVERED_ASSISTANT_TURN}

    def __init__(self, source_port: SourceEvidencePort | None, policy: InquiryAdmissionPolicyV1 | None = None):
        self.source_port = source_port
        self.policy = policy or InquiryAdmissionPolicyV1()

    def verify(self, ref: TrustedSourceRef, *, workspace_subject: str,
               workspace_visibility: str, grant_scope: str) -> AdmissionVerdict:
        if self.source_port is None:
            return AdmissionVerdict("BLOCK", "TRUSTED_SOURCE_PORT_PENDING", self.policy.policy_id)
        if ref.source_kind not in self.ACCEPTED_KINDS:
            return AdmissionVerdict("BLOCK", "SOURCE_KIND_NOT_ACCEPTED", self.policy.policy_id)
        if (not ref.source_id or not ref.producer or not ref.source_revision or
            not ref.content_binding or ref.delivered_status != "DELIVERED"):
            return AdmissionVerdict("BLOCK", "SOURCE_REF_INVALID_OR_UNDELIVERED", self.policy.policy_id)
        if ref.subject_id != workspace_subject:
            return AdmissionVerdict("BLOCK", "SOURCE_SUBJECT_MISMATCH", self.policy.policy_id)
        if workspace_visibility != "PRIVATE" or ref.visibility_scope != "PRIVATE_SELF" or grant_scope != "inquiry:personal":
            return AdmissionVerdict("BLOCK", "VISIBILITY_OR_GRANT_SCOPE_DENIED", self.policy.policy_id)
        evidence = self.source_port.verify_source(ref)
        if evidence.status != EvidenceStatus.FOUND or evidence.evidence is None:
            return AdmissionVerdict("BLOCK", f"SOURCE_{evidence.status.value}", self.policy.policy_id)
        actual = evidence.evidence
        if (actual.source_id != ref.source_id or actual.source_kind != ref.source_kind or
            actual.producer != ref.producer or actual.subject_id != workspace_subject or
            actual.visibility_scope != "PRIVATE_SELF" or actual.delivered_status != "DELIVERED" or
            actual.source_revision != ref.source_revision or actual.content_binding != ref.content_binding):
            return AdmissionVerdict("BLOCK", "SOURCE_EVIDENCE_BINDING_MISMATCH", self.policy.policy_id)
        if self.source_port.read_source_subject(ref) != workspace_subject:
            return AdmissionVerdict("BLOCK", "SOURCE_SUBJECT_UNKNOWN_OR_MISMATCH", self.policy.policy_id)
        if self.source_port.read_source_scope(ref) != "PRIVATE_SELF":
            return AdmissionVerdict("BLOCK", "SOURCE_SCOPE_UNKNOWN_OR_DENIED", self.policy.policy_id)
        if self.source_port.read_source_delivery_state(ref) != "DELIVERED":
            return AdmissionVerdict("BLOCK", "SOURCE_DELIVERY_UNKNOWN_OR_UNDELIVERED", self.policy.policy_id)
        return AdmissionVerdict("ALLOW", "SOURCE_VERIFIED_PRIVATE_SELF", self.policy.policy_id, (ref.source_id,))
