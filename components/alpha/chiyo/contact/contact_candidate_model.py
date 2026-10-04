"""Pure, isolated CT-0 ContactCandidate contract.

This module deliberately has no filesystem, network, persistence, runtime, or
production adapter dependencies. ContactCandidate is distinct from AG-0's
CandidateRecord and does not represent intent, agency decision, or action.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import hashlib
import re
from typing import Iterable


CONTACT_CANDIDATE_SCHEMA = "chiyo.contact.candidate.v1"
CONTACT_CANDIDATE_POLICY_VERSION = "ct0.contact_candidate_policy.v1"
SOURCE_LIFE_EXPERIENCE = "LIFE_EXPERIENCE"
CANDIDATE_SHARE_EXPERIENCE = "SHARE_EXPERIENCE"
AG2_HANDOFF_SCHEMA = "chiyo.life.experience_handoff.v1"
AG2_HANDOFF_KINDS = frozenset(
    {"COMPLETED", "ABANDONED", "SIGNIFICANT_PROGRESS", "CONFLICT"}
)
GLOBAL_SUBJECT_ID = "chiyo.global"

_REF_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.:-]{0,191}$")
_ACTIVITY_KIND_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.:-]{0,95}$")


class ContactContractError(ValueError):
    """Base error for invalid isolated contact contracts."""


class InvalidExperienceHandoffError(ContactContractError):
    """The input is not a valid canonical AG-2 Experience Handoff projection."""


class InvalidContactCandidateError(ContactContractError):
    """A ContactCandidate violates the CT-0 contract."""


def _require_ref(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not _REF_RE.fullmatch(value):
        raise ContactContractError(f"{field_name} must be a bounded structured reference")
    return value


def _require_timestamp(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ContactContractError(f"{field_name} must be an ISO-8601 timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ContactContractError(f"{field_name} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ContactContractError(f"{field_name} must include a timezone")
    return value


def _require_ref_tuple(value: object, field_name: str) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        raise ContactContractError(f"{field_name} must be a list of references")
    if len(value) > 256:
        raise ContactContractError(f"{field_name} exceeds the reference bound")
    refs = tuple(_require_ref(item, field_name) for item in value)
    if len(set(refs)) != len(refs):
        raise ContactContractError(f"{field_name} must not contain duplicate references")
    return refs


@dataclass(frozen=True, slots=True)
class LifeExperienceHandoff:
    """Detached, validated reference-only projection of the AG-2 v1 event."""

    handoff_id: str
    correlation_id: str
    activity_ref: str
    activity_revision: int
    activity_kind: str
    terminal_or_milestone_kind: str
    decision_refs: tuple[str, ...]
    adoption_refs: tuple[str, ...]
    action_refs: tuple[str, ...]
    settlement_refs: tuple[str, ...]
    progress_refs: tuple[str, ...]
    completion_evidence_ref: str | None
    fact_refs: tuple[str, ...]
    artifact_refs: tuple[str, ...]
    world_refs: tuple[str, ...]
    occurred_at: str
    observed_at: str
    recorded_at: str
    causal_parent_refs: tuple[str, ...]
    subject_id: str
    policy_version: str
    schema_version: str

    @classmethod
    def from_mapping(cls, raw: object) -> "LifeExperienceHandoff":
        if not isinstance(raw, dict):
            raise InvalidExperienceHandoffError("handoff must be a mapping")

        required = {
            "handoff_id", "correlation_id", "activity_ref", "activity_revision",
            "activity_kind", "terminal_or_milestone_kind", "decision_refs",
            "adoption_refs", "action_refs", "settlement_refs", "progress_refs",
            "completion_evidence_ref", "fact_refs", "artifact_refs", "world_refs",
            "occurred_at", "observed_at", "recorded_at", "causal_parent_refs",
        }
        optional = {"subject_id", "policy_version", "schema_version"}
        missing = required - raw.keys()
        unknown = raw.keys() - required - optional
        if missing:
            raise InvalidExperienceHandoffError("handoff is missing required contract fields")
        if unknown:
            raise InvalidExperienceHandoffError("handoff contains fields outside the AG-2 v1 schema")

        schema = raw.get("schema_version", AG2_HANDOFF_SCHEMA)
        if schema != AG2_HANDOFF_SCHEMA:
            raise InvalidExperienceHandoffError("unsupported Experience Handoff schema")
        subject = raw.get("subject_id", GLOBAL_SUBJECT_ID)
        if subject != GLOBAL_SUBJECT_ID:
            raise InvalidExperienceHandoffError("handoff subject is outside chiyo.global")

        handoff_id = _require_ref(raw["handoff_id"], "handoff_id")
        if not handoff_id.startswith("lexp:"):
            raise InvalidExperienceHandoffError("handoff_id must use the AG-2 lexp namespace")
        correlation_id = _require_ref(raw["correlation_id"], "correlation_id")
        if not correlation_id.startswith("corr:"):
            raise InvalidExperienceHandoffError("correlation_id must use the AG-2 corr namespace")
        activity_ref = _require_ref(raw["activity_ref"], "activity_ref")
        if not isinstance(raw["activity_revision"], int) or isinstance(raw["activity_revision"], bool) or raw["activity_revision"] < 1:
            raise InvalidExperienceHandoffError("activity_revision must be an integer >= 1")
        activity_kind = raw["activity_kind"]
        if not isinstance(activity_kind, str) or not _ACTIVITY_KIND_RE.fullmatch(activity_kind):
            raise InvalidExperienceHandoffError("activity_kind must be a bounded identifier")
        milestone = raw["terminal_or_milestone_kind"]
        if milestone not in AG2_HANDOFF_KINDS:
            raise InvalidExperienceHandoffError("unsupported AG-2 handoff kind")
        completion = raw["completion_evidence_ref"]
        if completion is not None:
            completion = _require_ref(completion, "completion_evidence_ref")

        return cls(
            handoff_id=handoff_id,
            correlation_id=correlation_id,
            activity_ref=activity_ref,
            activity_revision=raw["activity_revision"],
            activity_kind=activity_kind,
            terminal_or_milestone_kind=milestone,
            decision_refs=_require_ref_tuple(raw["decision_refs"], "decision_refs"),
            adoption_refs=_require_ref_tuple(raw["adoption_refs"], "adoption_refs"),
            action_refs=_require_ref_tuple(raw["action_refs"], "action_refs"),
            settlement_refs=_require_ref_tuple(raw["settlement_refs"], "settlement_refs"),
            progress_refs=_require_ref_tuple(raw["progress_refs"], "progress_refs"),
            completion_evidence_ref=completion,
            fact_refs=_require_ref_tuple(raw["fact_refs"], "fact_refs"),
            artifact_refs=_require_ref_tuple(raw["artifact_refs"], "artifact_refs"),
            world_refs=_require_ref_tuple(raw["world_refs"], "world_refs"),
            occurred_at=_require_timestamp(raw["occurred_at"], "occurred_at"),
            observed_at=_require_timestamp(raw["observed_at"], "observed_at"),
            recorded_at=_require_timestamp(raw["recorded_at"], "recorded_at"),
            causal_parent_refs=_require_ref_tuple(raw["causal_parent_refs"], "causal_parent_refs"),
            subject_id=subject,
            policy_version=_require_ref(raw.get("policy_version", "ag2.life_integration.v1"), "policy_version"),
            schema_version=schema,
        )


@dataclass(frozen=True, slots=True)
class ContactCandidate:
    """An opportunity to consider contact; never intent, decision, or action."""

    candidate_id: str
    schema_version: str
    revision: int
    subject_id: str
    recipient_ref: str
    source_kind: str
    source_refs: tuple[str, ...]
    experience_ref: str
    activity_ref: str
    activity_revision: int
    candidate_kind: str
    channel_options: tuple[str, ...]
    created_at: str
    observed_at: str
    causal_parent_refs: tuple[str, ...]
    policy_version: str
    idempotency_key: str

    def __post_init__(self) -> None:
        if self.schema_version != CONTACT_CANDIDATE_SCHEMA:
            raise InvalidContactCandidateError("unsupported ContactCandidate schema")
        if not isinstance(self.revision, int) or isinstance(self.revision, bool) or self.revision != 1:
            raise InvalidContactCandidateError("new ContactCandidate revision must be integer 1")
        if self.subject_id != GLOBAL_SUBJECT_ID:
            raise InvalidContactCandidateError("ContactCandidate subject must be chiyo.global")
        if self.source_kind != SOURCE_LIFE_EXPERIENCE:
            raise InvalidContactCandidateError("unsupported ContactCandidate source kind")
        if self.candidate_kind != CANDIDATE_SHARE_EXPERIENCE:
            raise InvalidContactCandidateError("unsupported ContactCandidate kind")
        _require_ref(self.recipient_ref, "recipient_ref")
        _require_ref(self.experience_ref, "experience_ref")
        _require_ref(self.activity_ref, "activity_ref")
        _require_ref(self.candidate_id, "candidate_id")
        _require_ref(self.idempotency_key, "idempotency_key")
        _require_ref(self.policy_version, "policy_version")
        if not self.candidate_id.startswith("ccand:"):
            raise InvalidContactCandidateError("candidate_id must use the ccand namespace")
        if not self.experience_ref.startswith("lexp:"):
            raise InvalidContactCandidateError("experience_ref must reference an AG-2 handoff")
        if not isinstance(self.activity_revision, int) or isinstance(self.activity_revision, bool) or self.activity_revision < 1:
            raise InvalidContactCandidateError("activity_revision must be integer >= 1")
        if not self.source_refs or self.source_refs[0] != self.experience_ref:
            raise InvalidContactCandidateError("primary source_refs entry must be experience_ref")
        if not isinstance(self.channel_options, tuple) or not self.channel_options:
            raise InvalidContactCandidateError("channel_options must be a non-empty immutable tuple")
        if len(set(self.channel_options)) != len(self.channel_options):
            raise InvalidContactCandidateError("channel_options must be unique")
        for channel in self.channel_options:
            _require_ref(channel, "channel_options")
        expected_candidate_id, expected_idempotency_key = stable_candidate_identity(
            handoff_id=self.experience_ref,
            policy_version=self.policy_version,
            recipient_ref=self.recipient_ref,
            channel_options=self.channel_options,
        )
        if self.candidate_id != expected_candidate_id or self.idempotency_key != expected_idempotency_key:
            raise InvalidContactCandidateError("candidate identity does not match its source and authorization")
        for name, refs in (
            ("source_refs", self.source_refs),
            ("causal_parent_refs", self.causal_parent_refs),
        ):
            if not isinstance(refs, tuple) or len(set(refs)) != len(refs):
                raise InvalidContactCandidateError(f"{name} must be a unique immutable tuple")
            for ref in refs:
                _require_ref(ref, name)
        _require_timestamp(self.created_at, "created_at")
        _require_timestamp(self.observed_at, "observed_at")


def stable_candidate_identity(
    *, handoff_id: str, policy_version: str, recipient_ref: str,
    channel_options: Iterable[str],
) -> tuple[str, str]:
    """Return deterministic candidate/idempotency references for one eligible handoff."""
    channels = tuple(channel_options)
    material = "\x00".join((handoff_id, CANDIDATE_SHARE_EXPERIENCE, policy_version, recipient_ref, *channels))
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()
    return f"ccand:{digest[:32]}", f"ct0-idem:{digest}"


def unique_refs(*groups: Iterable[str]) -> tuple[str, ...]:
    """Preserve causal order while freezing and de-duplicating reference groups."""
    result: list[str] = []
    seen: set[str] = set()
    for group in groups:
        for ref in group:
            if ref not in seen:
                result.append(ref)
                seen.add(ref)
    return tuple(result)


__all__ = [
    "AG2_HANDOFF_KINDS", "AG2_HANDOFF_SCHEMA", "CANDIDATE_SHARE_EXPERIENCE",
    "CONTACT_CANDIDATE_POLICY_VERSION", "CONTACT_CANDIDATE_SCHEMA", "ContactCandidate",
    "ContactContractError", "GLOBAL_SUBJECT_ID", "InvalidContactCandidateError",
    "InvalidExperienceHandoffError", "LifeExperienceHandoff", "SOURCE_LIFE_EXPERIENCE",
    "stable_candidate_identity", "unique_refs",
]
