"""Default-disabled CT-0 ContactCandidate sources (isolated, pure Python only)."""

from __future__ import annotations

import re
import dataclasses
from dataclasses import dataclass
from typing import Mapping, Sequence

from contact_candidate_model import (
    AG2_HANDOFF_KINDS,
    CANDIDATE_SHARE_EXPERIENCE,
    CONTACT_CANDIDATE_POLICY_VERSION,
    CONTACT_CANDIDATE_SCHEMA,
    GLOBAL_SUBJECT_ID,
    SOURCE_LIFE_EXPERIENCE,
    ContactCandidate,
    ContactContractError,
    InvalidExperienceHandoffError,
    LifeExperienceHandoff,
    _require_ref,
    stable_candidate_identity,
    unique_refs,
)


_ACTIVITY_KIND_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.:-]{0,95}$")

SOURCE_SHARED_CONTEXT_RESULT = "SHARED_CONTEXT_RESULT"
SOURCE_SOCIAL_COMMITMENT = "SOCIAL_COMMITMENT"
SOURCE_REPAIR_ITEM = "REPAIR_ITEM"
SOURCE_EXPLICIT_FOLLOWUP = "EXPLICIT_FOLLOWUP"
CONTACT_SOURCE_KINDS = (
    SOURCE_LIFE_EXPERIENCE,
    SOURCE_SHARED_CONTEXT_RESULT,
    SOURCE_SOCIAL_COMMITMENT,
    SOURCE_REPAIR_ITEM,
    SOURCE_EXPLICIT_FOLLOWUP,
)

SOURCE_DISABLED_NO_CANONICAL_OWNER = "DISABLED_NO_CANONICAL_OWNER"
SOURCE_DISABLED_NO_AUTHORIZATION = "DISABLED_NO_AUTHORIZATION"
SOURCE_ENABLED_ISOLATED = "ENABLED_ISOLATED"


class ContactSourceRegistryError(ContactContractError):
    """Raised for an invalid source registry or an untrusted source input."""


@dataclass(frozen=True, slots=True)
class LifeExperienceAuthorization:
    """Explicit sandbox policy for one exact activity kind and intended recipient."""

    activity_kind: str
    recipient_ref: str
    channel_options: tuple[str, ...]
    allowed_handoff_kinds: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.activity_kind, str) or not _ACTIVITY_KIND_RE.fullmatch(self.activity_kind):
            raise ContactSourceRegistryError("activity_kind authorization is invalid")
        try:
            _require_ref(self.recipient_ref, "recipient_ref")
        except ContactContractError as exc:
            raise ContactSourceRegistryError("recipient_ref authorization is invalid") from exc
        if not isinstance(self.channel_options, tuple) or not self.channel_options:
            raise ContactSourceRegistryError("channel_options must be a non-empty immutable tuple")
        if len(set(self.channel_options)) != len(self.channel_options):
            raise ContactSourceRegistryError("channel_options must be unique")
        try:
            for channel in self.channel_options:
                _require_ref(channel, "channel_options")
        except ContactContractError as exc:
            raise ContactSourceRegistryError("channel option authorization is invalid") from exc
        if not isinstance(self.allowed_handoff_kinds, tuple) or not self.allowed_handoff_kinds:
            raise ContactSourceRegistryError("allowed_handoff_kinds must be a non-empty immutable tuple")
        if len(set(self.allowed_handoff_kinds)) != len(self.allowed_handoff_kinds):
            raise ContactSourceRegistryError("allowed_handoff_kinds must be unique")
        if not set(self.allowed_handoff_kinds) <= AG2_HANDOFF_KINDS:
            raise ContactSourceRegistryError("authorization contains an unsupported AG-2 handoff kind")
        # Validate the activity kind with the canonical AG-2 field contract.
        LifeExperienceHandoff.from_mapping(_authorization_validation_event(self))


def _authorization_validation_event(auth: LifeExperienceAuthorization) -> dict[str, object]:
    """Build a minimal in-memory schema probe; it is never emitted or persisted."""
    return {
        "handoff_id": "lexp:authorization-validation",
        "correlation_id": "corr:authorization-validation",
        "activity_ref": "act:authorization-validation",
        "activity_revision": 1,
        "activity_kind": auth.activity_kind,
        "terminal_or_milestone_kind": auth.allowed_handoff_kinds[0],
        "decision_refs": [],
        "adoption_refs": [],
        "action_refs": [],
        "settlement_refs": [],
        "progress_refs": [],
        "completion_evidence_ref": None,
        "fact_refs": [],
        "artifact_refs": [],
        "world_refs": [],
        "occurred_at": "2026-01-01T00:00:00+00:00",
        "observed_at": "2026-01-01T00:00:00+00:00",
        "recorded_at": "2026-01-01T00:00:00+00:00",
        "causal_parent_refs": [],
        "subject_id": GLOBAL_SUBJECT_ID,
    }


class LifeExperienceSource:
    """Materialize at most one candidate from an authorized AG-2 handoff.

    No authorization is installed by default. The AG-2 handoff event itself
    does not claim that an experience should be shared; an exact activity-kind,
    recipient, channel, and milestone allowlist is required.
    """

    source_kind = SOURCE_LIFE_EXPERIENCE

    def __init__(
        self,
        authorizations: Sequence[LifeExperienceAuthorization] = (),
        *,
        policy_version: str = CONTACT_CANDIDATE_POLICY_VERSION,
    ) -> None:
        if not isinstance(policy_version, str) or not policy_version:
            raise ContactSourceRegistryError("policy_version is required")
        self.policy_version = policy_version
        by_activity_kind: dict[str, LifeExperienceAuthorization] = {}
        for authorization in tuple(authorizations):
            if not isinstance(authorization, LifeExperienceAuthorization):
                raise ContactSourceRegistryError("authorizations must be LifeExperienceAuthorization values")
            if authorization.activity_kind in by_activity_kind:
                raise ContactSourceRegistryError("activity_kind has duplicate source authorization")
            by_activity_kind[authorization.activity_kind] = authorization
        self._authorizations = by_activity_kind

    @property
    def enabled(self) -> bool:
        return bool(self._authorizations)

    def materialize(self, raw_handoff: object) -> tuple[ContactCandidate, ...]:
        if not self.enabled:
            return ()
        if isinstance(raw_handoff, LifeExperienceHandoff):
            raw_handoff = dataclasses.asdict(raw_handoff)
        handoff = LifeExperienceHandoff.from_mapping(raw_handoff)

        authorization = self._authorizations.get(handoff.activity_kind)
        if authorization is None or handoff.terminal_or_milestone_kind not in authorization.allowed_handoff_kinds:
            return ()

        candidate_id, idempotency_key = stable_candidate_identity(
            handoff_id=handoff.handoff_id,
            policy_version=self.policy_version,
            recipient_ref=authorization.recipient_ref,
            channel_options=authorization.channel_options,
        )
        # Keep provenance as structured references only. Event payload/facts are
        # never copied into candidate text or surfaced as a message draft.
        source_refs = unique_refs((handoff.handoff_id,), handoff.causal_parent_refs)
        causal_parent_refs = unique_refs(
            handoff.causal_parent_refs,
            (handoff.activity_ref,),
            handoff.decision_refs,
            handoff.adoption_refs,
            handoff.action_refs,
            handoff.settlement_refs,
            handoff.progress_refs,
            (handoff.completion_evidence_ref,) if handoff.completion_evidence_ref else (),
        )
        candidate = ContactCandidate(
            candidate_id=candidate_id,
            schema_version=CONTACT_CANDIDATE_SCHEMA,
            revision=1,
            subject_id=handoff.subject_id,
            recipient_ref=authorization.recipient_ref,
            source_kind=SOURCE_LIFE_EXPERIENCE,
            source_refs=source_refs,
            experience_ref=handoff.handoff_id,
            activity_ref=handoff.activity_ref,
            activity_revision=handoff.activity_revision,
            candidate_kind=CANDIDATE_SHARE_EXPERIENCE,
            channel_options=authorization.channel_options,
            created_at=handoff.recorded_at,
            observed_at=handoff.observed_at,
            causal_parent_refs=causal_parent_refs,
            policy_version=self.policy_version,
            idempotency_key=idempotency_key,
        )
        return (candidate,)


class ContactCandidateSourceRegistry:
    """Versioned source registry. Non-LIFE sources stay disabled without owners."""

    def __init__(self, *, life_experience: LifeExperienceSource | None = None) -> None:
        if life_experience is not None and not isinstance(life_experience, LifeExperienceSource):
            raise ContactSourceRegistryError("life_experience must use the canonical isolated source adapter")
        self._life_experience = life_experience

    def source_status(self) -> tuple[tuple[str, str], ...]:
        rows: list[tuple[str, str]] = []
        for kind in CONTACT_SOURCE_KINDS:
            if kind == SOURCE_LIFE_EXPERIENCE:
                if self._life_experience is None:
                    status = SOURCE_DISABLED_NO_AUTHORIZATION
                elif self._life_experience.enabled:
                    status = SOURCE_ENABLED_ISOLATED
                else:
                    status = SOURCE_DISABLED_NO_AUTHORIZATION
            else:
                status = SOURCE_DISABLED_NO_CANONICAL_OWNER
            rows.append((kind, status))
        return tuple(rows)

    def materialize_life_experience(self, raw_handoff: object) -> tuple[ContactCandidate, ...]:
        if self._life_experience is None:
            return ()
        try:
            return self._life_experience.materialize(raw_handoff)
        except InvalidExperienceHandoffError:
            raise
        except ContactContractError:
            raise

    def materialize(self, source_kind: str, source_event: object) -> tuple[ContactCandidate, ...]:
        """Accept only the known typed LIFE handoff source; other owners are disabled."""
        if source_kind not in CONTACT_SOURCE_KINDS:
            raise ContactSourceRegistryError("unknown ContactCandidate source kind")
        if source_kind != SOURCE_LIFE_EXPERIENCE:
            return ()
        return self.materialize_life_experience(source_event)


__all__ = [
    "CONTACT_SOURCE_KINDS", "ContactCandidateSourceRegistry", "ContactSourceRegistryError",
    "LifeExperienceAuthorization", "LifeExperienceSource", "SOURCE_DISABLED_NO_AUTHORIZATION",
    "SOURCE_DISABLED_NO_CANONICAL_OWNER", "SOURCE_ENABLED_ISOLATED", "SOURCE_EXPLICIT_FOLLOWUP",
    "SOURCE_LIFE_EXPERIENCE", "SOURCE_REPAIR_ITEM", "SOURCE_SHARED_CONTEXT_RESULT",
    "SOURCE_SOCIAL_COMMITMENT",
]
