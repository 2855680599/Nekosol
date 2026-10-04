"""R3 bridge adapter: no local Activity persistence; delegate only to LR-2."""
from __future__ import annotations
from typing import Any, Sequence
class BridgeWriteRejected(RuntimeError): pass
class CanonicalActivityBridge:
 def __init__(self,command_service=None): self._command=command_service
 def _call(self,method,*,decision_ref,adoption_ref,expected_revision,idempotency_key,source_refs,activity_id=None,reason_code=None,**kwargs):
  if self._command is None: raise BridgeWriteRejected('CANONICAL_OWNER_UNAVAILABLE')
  if not decision_ref or not adoption_ref: raise BridgeWriteRejected('BRIDGE_PROVENANCE_REQUIRED')
  if not isinstance(expected_revision,int) or expected_revision<0: raise BridgeWriteRejected('BRIDGE_EXPECTED_REVISION_REQUIRED')
  if not idempotency_key or not source_refs: raise BridgeWriteRejected('BRIDGE_LINEAGE_REQUIRED')
  api=getattr(self._command,method,None)
  if not callable(api): raise BridgeWriteRejected('LR2_COMMAND_UNAVAILABLE')
  refs=list(dict.fromkeys([*source_refs,decision_ref,adoption_ref]))
  call={'expected_revision':expected_revision,'idempotency_key':idempotency_key,'source_refs':refs,'decision_ref':decision_ref,'occurred_at':kwargs.pop('occurred_at',None),'observed_at':kwargs.pop('observed_at',None),'enqueued_at':kwargs.pop('enqueued_at',None),'causal_parent_refs':list(dict.fromkeys([decision_ref,adoption_ref,*kwargs.pop('causal_parent_refs',[])]))}
  if activity_id is not None: call['activity_id']=activity_id
  if method in {'start_activity','resume_activity'}: call['adoption_ref']=adoption_ref
  if reason_code is not None: call['reason_code']=reason_code
  call.update(kwargs)
  return api(**call)
 def start(self,*,decision_ref,adoption_ref,expected_revision,idempotency_key,source_refs,**kwargs):return self._call('start_activity',decision_ref=decision_ref,adoption_ref=adoption_ref,expected_revision=expected_revision,idempotency_key=idempotency_key,source_refs=source_refs,**kwargs)
 def pause(self,*,activity_id,decision_ref,adoption_ref,expected_revision,idempotency_key,source_refs,**kwargs):return self._call('pause_activity',activity_id=activity_id,decision_ref=decision_ref,adoption_ref=adoption_ref,expected_revision=expected_revision,idempotency_key=idempotency_key,source_refs=source_refs,**kwargs)
 def wait(self,*,activity_id,waiting_on_ref,decision_ref,adoption_ref,expected_revision,idempotency_key,source_refs,**kwargs):return self._call('wait_activity',activity_id=activity_id,waiting_on_ref=waiting_on_ref,decision_ref=decision_ref,adoption_ref=adoption_ref,expected_revision=expected_revision,idempotency_key=idempotency_key,source_refs=source_refs,**kwargs)
 def resume(self,*,activity_id,decision_ref,adoption_ref,expected_revision,idempotency_key,source_refs,**kwargs):return self._call('resume_activity',activity_id=activity_id,decision_ref=decision_ref,adoption_ref=adoption_ref,expected_revision=expected_revision,idempotency_key=idempotency_key,source_refs=source_refs,**kwargs)
 def complete(self,*,activity_id,completion_evidence_ref,decision_ref,adoption_ref,expected_revision,idempotency_key,source_refs,**kwargs):return self._call('complete_activity',activity_id=activity_id,completion_evidence_ref=completion_evidence_ref,decision_ref=decision_ref,adoption_ref=adoption_ref,expected_revision=expected_revision,idempotency_key=idempotency_key,source_refs=source_refs,**kwargs)
 def abandon(self,*,activity_id,decision_ref,adoption_ref,expected_revision,idempotency_key,source_refs,**kwargs):return self._call('abandon_activity',activity_id=activity_id,decision_ref=decision_ref,adoption_ref=adoption_ref,expected_revision=expected_revision,idempotency_key=idempotency_key,source_refs=source_refs,**kwargs)
 def cancel(self,*,activity_id,decision_ref,adoption_ref,expected_revision,idempotency_key,source_refs,**kwargs):return self._call('cancel_activity',activity_id=activity_id,decision_ref=decision_ref,adoption_ref=adoption_ref,expected_revision=expected_revision,idempotency_key=idempotency_key,source_refs=source_refs,**kwargs)
 def expire(self,*,activity_id,decision_ref,adoption_ref,expected_revision,idempotency_key,source_refs,**kwargs):return self._call('expire_activity',activity_id=activity_id,decision_ref=decision_ref,adoption_ref=adoption_ref,expected_revision=expected_revision,idempotency_key=idempotency_key,source_refs=source_refs,**kwargs)
 def direct_save(self,*args,**kwargs):raise BridgeWriteRejected('BRIDGE_LOCAL_WRITE_REJECTED')
