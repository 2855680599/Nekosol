"""In-memory CT0-5 Draft authority with idempotent replay and no side effects."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
from typing import Protocol

from contact_intent_model import ContactIntent
from contact_draft_model import (
    DECISION_CONTACT_SELECTED,
    DRAFT_POLICY_VERSION,
    ContactDecisionReceipt,
    ContactDraftRender,
    ContactGroundingSource,
    ContactMessageDraft,
    build_frozen_draft,
    sha256_text,
)
from contact_draft_projection import validate_frozen_draft_projection


RESULT_CREATED = "DRAFT_CREATED"
RESULT_FAILED = "DRAFT_FAILED"
RESULT_NO_DRAFT = "NO_DRAFT"
RESULT_REPLAYED = "DRAFT_REPLAYED"
RESULT_REPLAYED_FAILED = "DRAFT_FAILURE_REPLAYED"


class ContactDraftAuthorityError(ValueError):
    """Base error for isolated Draft authority input or state failures."""


class ContactDraftRenderer(Protocol):
    """Injected expression-only renderer with no delivery/action capability."""

    def render(
        self,
        *,
        intent: ContactIntent,
        decision: ContactDecisionReceipt,
        grounding_sources: tuple[ContactGroundingSource, ...],
    ) -> ContactDraftRender:
        ...


@dataclass(frozen=True, slots=True)
class ContactDraftResult:
    status: str
    intent_ref: str
    decision_ref: str
    draft: ContactMessageDraft | None
    failure_code: str | None
    idempotency_key: str
    replayed: bool


@dataclass(frozen=True, slots=True)
class _StoredDraftOutcome:
    result_status: str
    intent_ref: str
    decision_ref: str
    decision_fingerprint: str
    draft: ContactMessageDraft | None
    failure_code: str | None
    idempotency_key: str


class ContactDraftAuthority:
    """Create at most one frozen Draft outcome per Intent/committed Decision.

    This is an in-memory contract fixture. It does not persist, resolve canonical
    source text, decide, schedule, create Actions, or contact any transport.
    """

    def __init__(self, *, renderer: ContactDraftRenderer, policy_version: str = DRAFT_POLICY_VERSION) -> None:
        if not isinstance(policy_version, str) or not policy_version:
            raise ContactDraftAuthorityError("policy_version is required")
        self._renderer = renderer
        self.policy_version = policy_version
        self._outcomes: dict[tuple[str, str], _StoredDraftOutcome] = {}

    @staticmethod
    def _key(intent: ContactIntent, decision: ContactDecisionReceipt) -> tuple[str, str]:
        return intent.intent_id, decision.decision_ref

    @staticmethod
    def _decision_fingerprint(decision: ContactDecisionReceipt) -> str:
        material = json.dumps(asdict(decision), sort_keys=True, separators=(",", ":"))
        return sha256_text(material)

    def create_draft(
        self,
        *,
        intent: object,
        decision: object,
        grounding_sources: tuple[ContactGroundingSource, ...],
        created_at: str,
    ) -> ContactDraftResult:
        if not isinstance(intent, ContactIntent):
            raise ContactDraftAuthorityError("Draft request requires a typed ContactIntent")
        if not isinstance(decision, ContactDecisionReceipt):
            raise ContactDraftAuthorityError("Draft request requires a typed Decision receipt")
        if not isinstance(grounding_sources, tuple) or any(
            not isinstance(source, ContactGroundingSource) for source in grounding_sources
        ):
            raise ContactDraftAuthorityError("grounding_sources must be an immutable typed tuple")
        try:
            intent = ContactIntent.from_mapping(intent.to_dict())
            decision = ContactDecisionReceipt.from_mapping(asdict(decision))
            grounding_sources = tuple(
                ContactGroundingSource.from_mapping(asdict(source))
                for source in grounding_sources
            )
        except Exception as exc:
            raise ContactDraftAuthorityError("Draft inputs failed contract revalidation") from exc

        key = self._key(intent, decision)
        request_key = f"cdraft-request:{intent.intent_id}:{decision.decision_ref}"
        fingerprint = self._decision_fingerprint(decision)

        # Every receipt outcome, including NO_ACTION/DEFER, must belong to this
        # Intent/Candidate before it can create or replay any cached result.
        if decision.intent_ref != intent.intent_id or decision.candidate_ref != intent.candidate_ref:
            return ContactDraftResult(
                RESULT_FAILED, intent.intent_id, decision.decision_ref, None,
                "DECISION_INTENT_BINDING_INVALID", request_key, False,
            )

        prior = self._outcomes.get(key)
        if prior is not None:
            if prior.decision_fingerprint != fingerprint:
                return ContactDraftResult(
                    RESULT_FAILED, intent.intent_id, decision.decision_ref, None,
                    "DECISION_RECEIPT_CONFLICT", prior.idempotency_key, True,
                )
            if prior.draft is not None:
                try:
                    frozen = validate_frozen_draft_projection(
                        intent=intent,
                        decision=decision,
                        draft=prior.draft,
                        grounding_sources=grounding_sources,
                    )
                except ValueError:
                    return ContactDraftResult(
                        RESULT_FAILED, intent.intent_id, decision.decision_ref, None,
                        "REPLAY_VALIDATION_FAILED", prior.idempotency_key, True,
                    )
                return ContactDraftResult(
                    RESULT_REPLAYED, intent.intent_id, decision.decision_ref, frozen,
                    None, prior.idempotency_key, True,
                )
            if prior.result_status == RESULT_NO_DRAFT:
                return ContactDraftResult(
                    RESULT_NO_DRAFT, intent.intent_id, decision.decision_ref, None,
                    None, prior.idempotency_key, True,
                )
            return ContactDraftResult(
                RESULT_REPLAYED_FAILED, intent.intent_id, decision.decision_ref, None,
                prior.failure_code, prior.idempotency_key, True,
            )

        if not decision.committed or decision.outcome != DECISION_CONTACT_SELECTED:
            self._outcomes[key] = _StoredDraftOutcome(
                RESULT_NO_DRAFT, intent.intent_id, decision.decision_ref,
                fingerprint, None, None, request_key,
            )
            return ContactDraftResult(
                RESULT_NO_DRAFT, intent.intent_id, decision.decision_ref, None,
                None, request_key, False,
            )
        if decision.selected_candidate_ref != intent.candidate_ref or intent.status != "SELECTED":
            failure_code = "DECISION_INTENT_BINDING_INVALID"
            self._outcomes[key] = _StoredDraftOutcome(
                RESULT_FAILED, intent.intent_id, decision.decision_ref,
                fingerprint, None, failure_code, request_key,
            )
            return ContactDraftResult(
                RESULT_FAILED, intent.intent_id, decision.decision_ref, None,
                failure_code, request_key, False,
            )

        try:
            rendered = self._renderer.render(
                intent=intent,
                decision=decision,
                grounding_sources=grounding_sources,
            )
            draft = build_frozen_draft(
                intent=intent,
                decision=decision,
                grounding_sources=grounding_sources,
                render=rendered,
                created_at=created_at,
                policy_version=self.policy_version,
            )
        except Exception as exc:
            # No template fallback, and no implicit regeneration on replay.
            failure_code = f"RENDER_OR_GROUNDING_FAILED:{type(exc).__name__}"
            self._outcomes[key] = _StoredDraftOutcome(
                RESULT_FAILED, intent.intent_id, decision.decision_ref,
                fingerprint, None, failure_code, request_key,
            )
            return ContactDraftResult(
                RESULT_FAILED, intent.intent_id, decision.decision_ref, None,
                failure_code, request_key, False,
            )

        self._outcomes[key] = _StoredDraftOutcome(
            RESULT_CREATED, intent.intent_id, decision.decision_ref,
            fingerprint, draft, None, draft.idempotency_key,
        )
        return ContactDraftResult(
            RESULT_CREATED, intent.intent_id, decision.decision_ref, draft,
            None, draft.idempotency_key, False,
        )

    def replay(
        self,
        *,
        intent: ContactIntent,
        decision: ContactDecisionReceipt,
        grounding_sources: tuple[ContactGroundingSource, ...],
    ) -> ContactDraftResult | None:
        """Read/revalidate the stored outcome without invoking the renderer."""
        key = self._key(intent, decision)
        if key not in self._outcomes:
            return None
        return self.create_draft(
            intent=intent,
            decision=decision,
            grounding_sources=grounding_sources,
            created_at=intent.created_at,
        )


__all__ = [
    "RESULT_CREATED", "RESULT_FAILED", "RESULT_NO_DRAFT", "RESULT_REPLAYED",
    "RESULT_REPLAYED_FAILED", "ContactDraftAuthority", "ContactDraftAuthorityError",
    "ContactDraftRenderer", "ContactDraftResult",
]
