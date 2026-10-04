"""R6-only composition of AG-1 and AG-2 owner read ports; no central registry."""
from __future__ import annotations
from dataclasses import dataclass
from typing import Any, Protocol

class DecisionReadPort(Protocol):
    def get_decision(self, decision_id: str) -> dict[str, Any] | None: ...
class AdoptionReadPort(Protocol):
    def get_adoption_by_id(self, adoption_id: str) -> dict[str, Any] | None: ...

@dataclass(frozen=True)
class VerifiedProvenance:
    decision_id: str
    decision_revision: int
    adoption_id: str
    adoption_revision: int
    adoption_status: str
    decision_content_hash: str

class ProvenanceRejected(ValueError): pass

class ProvenanceVerifier:
    """Validates refs against their respective canonical owners, not caller dictionaries."""
    ACCEPTED_ADOPTION_STATES = frozenset({'APPLIED','NOOP','AUTHORIZED'})
    def __init__(self, decision_reader: DecisionReadPort, adoption_reader: AdoptionReadPort):
        if not callable(getattr(decision_reader,'get_decision',None)):
            raise TypeError('decision_reader must be AG-1 owner read port')
        if not callable(getattr(adoption_reader,'get_adoption_by_id',None)):
            raise TypeError('adoption_reader must be AG-2 owner read port')
        self._decision_reader=decision_reader; self._adoption_reader=adoption_reader
    def verify(self, *, decision_ref: str, adoption_ref: str,
               expected_decision_revision: int | None = None,
               expected_activity_ref: str | None = None,
               expected_command_id: str | None = None,
               expected_mutation_kind: str | None = None) -> VerifiedProvenance:
        if not decision_ref or not adoption_ref: raise ProvenanceRejected('MISSING_DECISION_OR_ADOPTION')
        decision=self._decision_reader.get_decision(decision_ref)
        if decision is None: raise ProvenanceRejected('UNKNOWN_DECISION')
        adoption=self._adoption_reader.get_adoption_by_id(adoption_ref)
        if adoption is None: raise ProvenanceRejected('UNKNOWN_ADOPTION')
        d_id=decision.get('decision_id'); d_rev=decision.get('revision')
        if d_id != decision_ref: raise ProvenanceRejected('DECISION_OWNER_ID_MISMATCH')
        if isinstance(d_rev,bool) or not isinstance(d_rev,int) or d_rev<1: raise ProvenanceRejected('BAD_DECISION_REVISION')
        if expected_decision_revision is not None and expected_decision_revision != d_rev: raise ProvenanceRejected('STALE_DECISION_REVISION')
        if adoption.get('adoption_id') != adoption_ref: raise ProvenanceRejected('ADOPTION_OWNER_ID_MISMATCH')
        if adoption.get('decision_ref') != d_id: raise ProvenanceRejected('DECISION_ADOPTION_MISMATCH')
        if adoption.get('decision_revision') != d_rev: raise ProvenanceRejected('DECISION_ADOPTION_REVISION_MISMATCH')
        if adoption.get('adoption_status') not in self.ACCEPTED_ADOPTION_STATES: raise ProvenanceRejected('ADOPTION_NOT_ACCEPTED')
        if expected_mutation_kind is not None:
            # Admitting a transition: the Adoption must carry the command-scoped grant
            # that authorises this exact command. A finished record keeps its grant, so
            # this also stops an old PAUSE (or a grant-less) Adoption from being spent
            # on a different transition or a different command.
            granted_kind = adoption.get('authorized_mutation_kind')
            granted_command = adoption.get('authorized_command_id')
            if not granted_kind or not granted_command:
                raise ProvenanceRejected('ADOPTION_HAS_NO_COMMAND_GRANT')
            if granted_command != expected_command_id:
                raise ProvenanceRejected('AUTHORIZED_COMMAND_SCOPE_MISMATCH')
            if granted_kind != expected_mutation_kind:
                raise ProvenanceRejected('AUTHORIZED_MUTATION_SCOPE_MISMATCH')
        if expected_activity_ref is not None:
            activity_refs={adoption.get('created_activity_ref'),adoption.get('target_activity_ref'),adoption.get('authorized_activity_ref')}
            if expected_activity_ref not in activity_refs: raise ProvenanceRejected('ACTIVITY_SCOPE_MISMATCH')
        fingerprint=decision.get('snapshot_fingerprint')
        if not isinstance(fingerprint,str) or not fingerprint: raise ProvenanceRejected('DECISION_FINGERPRINT_MISSING')
        return VerifiedProvenance(d_id,d_rev,adoption_ref,int(adoption.get('revision',1)),adoption['adoption_status'],fingerprint)

class VerifiedActivityCommandPort:
    """R6 candidate adapter: owner-backed provenance verification before LR-2 mutation."""
    def __init__(self, verifier: ProvenanceVerifier, lr2_command: Any):
        if lr2_command is None: raise TypeError('lr2_command is required')
        self._verifier=verifier;self._lr2=lr2_command
    def start_activity(self, **kwargs: Any) -> Any:
        decision_ref=kwargs.get('decision_ref');adoption_ref=kwargs.get('adoption_ref')
        self._verifier.verify(decision_ref=decision_ref,adoption_ref=adoption_ref,
          expected_decision_revision=kwargs.pop('expected_decision_revision',None),
          expected_activity_ref=kwargs.pop('expected_activity_ref',None))
        return self._lr2.start_activity(**kwargs)
    def transition(self, method: str, **kwargs: Any) -> Any:
        if method not in {'pause_activity','resume_activity','complete_activity','abandon_activity','cancel_activity','wait_activity'}:
            raise ProvenanceRejected('UNKNOWN_LR2_COMMAND')
        self._verifier.verify(decision_ref=kwargs.get('decision_ref'),adoption_ref=kwargs.get('adoption_ref'),
          expected_decision_revision=kwargs.pop('expected_decision_revision',None),
          expected_activity_ref=kwargs.pop('expected_activity_ref',kwargs.get('activity_id')))
        command=getattr(self._lr2,method,None)
        if not callable(command): raise ProvenanceRejected('LR2_COMMAND_UNAVAILABLE')
        return command(**kwargs)
