import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import unittest
from types import SimpleNamespace as NS
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent
sys.path[:0] = [str(ROOT/'native'),str(ROOT/'native/src'),str(ROOT/'native/adapters'),
                str(ROOT/'life/scripts'),str(ROOT/'supply')]
from memory_control import MemoryControlLedger
from continuity_bridge import CanonicalPersonaProvider
from app.evidence import EvidenceStore, EvidenceEvent, USER_ORIGIN, USER_ROLE, RECEIVED
from app.session import ConversationStore
from chiyo_original_runner import OriginalNativeRuntime
from integration.live_inbound import ObservationDispatcher, LifeObservationEvent, iso_now
from telegram_adapter import TelegramAdapter, TelegramState
from lifesupply.workspace.store import WorkspaceStore
from candidate_sources_ag0 import CandidateSourceRegistry, SOURCE_PERSONAL_OPPORTUNITY
from life_supply_candidate_source import LifeSupplyCandidateSource

def event(eid='old',turn='t1',conversation='c',content='old fact',source=None):
    return EvidenceEvent(eid,'2026-10-01T10:00:00+00:00','chiyo',USER_ORIGIN,RECEIVED,
        USER_ROLE,'user',content,conversation,turn,[{'kind':'test','id':source or eid}],
        '2026-10-01T10:00:00+00:00')

class MemoryTests(unittest.TestCase):
    def setUp(self):
        self.t = tempfile.TemporaryDirectory(); self.root=Path(self.t.name)
        self.evidence = EvidenceStore(self.root/'m0.sqlite')
        for e in [event(),event('other',conversation='foreign'),event('new',turn='t2',content='new fact')]:
            self.evidence.append(e)
        self.ledger=MemoryControlLedger(self.root/'control.sqlite',self.evidence.path,owner='42',conversation='c')
        self.ledger.initialize()
    def tearDown(self): self.t.cleanup()
    def apply(self,kind='DELETE',target='old',replacement=None,operation='op',actor='42'):
        return self.ledger.apply(operation_id=operation,kind=kind,target=target,replacement=replacement,actor=actor)
    def test_delete_restart_idempotent_and_evidence_unchanged(self):
        before=hashlib.sha256(self.evidence.path.read_bytes()).hexdigest()
        self.assertEqual(self.apply(),'APPLIED');self.assertEqual(self.apply(),'DUPLICATE')
        ledger=MemoryControlLedger(self.ledger.path,self.evidence.path,owner='42',conversation='c')
        p=ledger.projection();self.assertTrue(p.healthy)
        self.assertIn('old',p.blocked_events)
        self.assertEqual(p.recalls([NS(source_event_ids=('old',),text='old fact')]),([],[]))
        self.assertEqual(hashlib.sha256(self.evidence.path.read_bytes()).hexdigest(),before)
    def test_correction(self):
        self.apply('CORRECT',replacement='new')
        self.assertEqual(self.ledger.projection().recalls([NS(source_event_ids=('old',))]),([],['new fact']))
    def test_deleted_correction_cannot_resurrect(self):
        self.apply('CORRECT',replacement='new');self.apply(target='new',operation='del-new')
        self.assertEqual(self.ledger.projection().recalls([NS(source_event_ids=('old',))]),([],[]))
    def test_wrong_owner_and_conversation(self):
        with self.assertRaises(PermissionError): self.apply(actor='someone')
        with self.assertRaises(ValueError): self.apply(target='other')
    def test_operation_collision(self):
        self.apply()
        with self.assertRaises(ValueError): self.apply(target='new')
    def test_missing_and_corrupt_fail_closed(self):
        self.ledger.path.unlink();self.assertFalse(self.ledger.projection().healthy)
        self.ledger.path.write_bytes(b'invalid');self.assertEqual(self.ledger.projection().recalls([NS(text='x')]),([],[]))
    def test_ledger_immutable(self):
        self.apply();c=sqlite3.connect(self.ledger.path)
        try:
            with self.assertRaises(sqlite3.IntegrityError):c.execute('DELETE FROM memory_controls')
        finally:c.close()
    def test_old_history_and_missing_refs_excluded(self):
        self.apply();p=self.ledger.projection()
        self.assertEqual(p.history([{'turn_id':'unrelated','timestamp':'2026-01-01T00:00:00+00:00'}]),[])
        self.assertEqual(p.recalls([NS(source_event_ids=(),text='untraceable')]),([],[]))
    def test_command_uses_direct_user_evidence(self):
        self.evidence.append(event('command',turn='t3',content='/memory correct old revised fact',source='telegram:42:8'))
        self.ledger.command('/memory correct old revised fact','telegram:42:8')
        self.assertEqual(self.ledger.projection().recalls([NS(source_event_ids=('old',))]),([],['revised fact']))
    def test_no_legacy_prompt_by_default(self):
        (self.root/'memories').mkdir();(self.root/'canonical').mkdir()
        for name in ['MEMORY.md','USER.md']:(self.root/'memories'/name).write_text('legacy-canary')
        (self.root/'canonical/relationship.yaml').write_text('relationship-canary')
        prompt=CanonicalPersonaProvider(self.root).load_system_prompt()
        self.assertNotIn('legacy-canary',prompt);self.assertIn('relationship-canary',prompt)
    def test_native_actual_history_fields(self):
        runtime=OriginalNativeRuntime.__new__(OriginalNativeRuntime)
        runtime.store=ConversationStore(str(self.root/'data'))
        runtime.store.persist_turn('c','prior','2026-10-01T10:00:00+00:00','user-canary','assistant-canary','fake')
        runtime.persona_provider=CanonicalPersonaProvider(self.root)
        runtime.memory_controls=self.ledger;runtime.m37_bridge=None;runtime.m37_binding={};runtime.m37_resolver=None
        runtime.history_reader=NS(get_recent_turns=lambda *a,**kw: [])
        runtime.model_cfg=NS(model='fake');captured=[]
        runtime.provider=NS(complete=lambda messages: (captured.extend(messages) or NS(content='reply')))
        runtime.traces=NS(write=lambda x:None)
        runtime.handle_turn('c','current')
        self.assertEqual(captured[1:3],[{'role':'user','content':'user-canary'},{'role':'assistant','content':'assistant-canary'}])
        self.apply();captured.clear();runtime.handle_turn('c','current')
        self.assertEqual([m['content'] for m in captured[1:]],['current'])
        runtime.memory_controls=None;runtime.memory_control_unavailable=True
        runtime.history_reader=NS(get_recent_turns=lambda *a,**kw:[{'role':'user','content':'deleted bootstrap'}])
        captured.clear();runtime.handle_turn('c','current')
        self.assertEqual([m['content'] for m in captured[1:]],['current'])

class SupplyTests(unittest.TestCase):
    def test_nonempty_owner_view_to_real_registry_and_no_read_mutation(self):
        with tempfile.TemporaryDirectory() as td:
            p=Path(td)/'ws.sqlite';store=WorkspaceStore(p,clock=lambda:1000)
            workspace=store.provision_workspace(subject_id='chiyo',grant_id='g',operation_key='op')['workspace']
            # Owner fixture: both live and expired rows, no service admission bypass in production.
            with store._connection() as c:
                for key,expires in [('live',2000),('expired',900)]:
                    c.execute("INSERT INTO open_inquiries(inquiry_id,workspace_id,subject_id,topic,source_refs,driver,status,review_after,expires_at,created_at,updated_at,revision,meaning_version) VALUES(?,?,?,'topic','[]','USER_SPARKED','OPEN',NULL,?,800,800,1,'workspace.open_inquiry.v1')",('inquiry:'+key,workspace['workspace_id'],'chiyo',expires))
            before=p.read_bytes();view=store.personal_opportunities_view('chiyo')
            self.assertEqual(len(view),1);self.assertEqual(p.read_bytes(),before)
            client=NS(list_personal_opportunities=lambda subject:{'status':'OK','opportunities':view})
            source=LifeSupplyCandidateSource(subject_id='chiyo',client=client)
            registry=CandidateSourceRegistry();registry.register_adapter(source,expected_kind=SOURCE_PERSONAL_OPPORTUNITY)
            records,_,_=source.materialize(observed_at='1970-01-01T00:16:40+00:00')
            self.assertEqual(len(records),1)
            self.assertEqual(records[0].candidate_kind,'CONSIDER_PERSONAL_OPPORTUNITY')
            late,_,_=source.materialize(observed_at='1970-01-01T00:40:00+00:00');self.assertEqual(late,[])
            view[0]['subject_id']='someone';self.assertEqual(source.materialize(observed_at='1970-01-01T00:16:40+00:00')[0],[])
    def test_denied_service_cannot_emit_payload(self):
        source=LifeSupplyCandidateSource(subject_id='chiyo',client=NS(list_personal_opportunities=lambda subject:{'status':'DENY','opportunities':[{}]}))
        self.assertEqual(source.materialize(observed_at=iso_now())[0],[])

class DeliveryTests(unittest.TestCase):
    def test_pending_and_failed_receipts_never_resend(self):
        for status in ['queued','failed','unknown','evidence-uuid',None]:
            with self.subTest(status=status),tempfile.TemporaryDirectory() as td:
                state=TelegramState(Path(td)/'state.sqlite');sends=[]
                rt=NS(handle_turn=lambda *a,**kw: {'turn_id':'t','trace_id':'trace','conversation_id':'c','raw_content':'reply','input_message_id':'i','outbound_message_id':'o'},
                      commit_turn=lambda *a:None,record_delivery_success=lambda *a:status)
                api=NS(send_message=lambda *a:(sends.append(a) or '99'))
                adapter=TelegramAdapter(rt,api,state,'42')
                update={'update_id':1,'message':{'message_id':10,'from':{'id':42},'chat':{'id':42,'type':'private'},'text':'test'}}
                adapter.process_update(update);adapter.process_update(update)
                self.assertEqual(len(sends),1);self.assertEqual(len(state.rows_in_state('TELEGRAM_CONFIRMED')),1)
                rt.record_delivery_success=lambda *a:'duplicate';adapter.process_update(update)
                self.assertEqual(len(sends),1);self.assertEqual(len(state.rows_in_state('DELIVERED')),1)
    def test_flush_tracks_inflight_work(self):
        started=threading.Event();release=threading.Event()
        def observe(event):started.set();release.wait(2);return {'accepted':True}
        dispatcher=ObservationDispatcher(NS(observe=observe))
        event=LifeObservationEvent('linb:test','ORDINARY_COMMUNICATION','telegram','telegram:subject:test','c',iso_now())
        dispatcher.submit(event);self.assertTrue(started.wait(1))
        self.assertFalse(dispatcher.flush(.01));release.set();self.assertTrue(dispatcher.flush(2));dispatcher.close()

class RecallAnchorTests(unittest.TestCase):
    def test_world_body_read_projection_no_hidden_metadata_or_actions(self):
        from integration.world_body_client import render_grounded_context
        client=NS(enabled=True,get_status=lambda:NS(outcome='OK',data={'ready':True}),
            get_snapshot=lambda:NS(outcome='OK',data={'location':{'place_id':'room'},'pose':'sitting',
                'world_revision':999,'world_id':'hidden-id','visible_objects':[{'label':'cup'}]}),
            get_body_signals=lambda:NS(outcome='OK',data={'signals':[{'type':'tired'}]}))
        text=render_grounded_context(client)
        self.assertIn('cup',text);self.assertIn('tired',text)
        self.assertNotIn('999',text);self.assertNotIn('hidden-id',text)
        client.get_status=lambda:NS(outcome='NOT_READY',data={'ready':False})
        self.assertEqual(render_grounded_context(client),'')
    def test_older_recall_cannot_anchor_new_ordinary_turn(self):
        from memory_runtime_v1.production_resolver import ProductionNativeMemoryResolver
        resolver=ProductionNativeMemoryResolver.__new__(ProductionNativeMemoryResolver)
        resolver.recency_seconds=300
        resolver._classify_intent=lambda text:('P1' if text=='recall' else 'OTHER','test')
        rows=[{'event_id':'old','conversation_id':'c','source_origin':'USER_VISIBLE_INPUT',
            'delivery_status':'RECEIVED','content':'recall','occurred_at':iso_now()},
            {'event_id':'new','conversation_id':'c','source_origin':'USER_VISIBLE_INPUT',
            'delivery_status':'RECEIVED','content':'ordinary','occurred_at':iso_now()}]
        resolver._adapter=NS(reader=NS(events=lambda:rows))
        self.assertIsNone(resolver.resolve_current_turn('c'))
        rows[-1]['content']='recall'
        self.assertEqual(resolver.resolve_current_turn('c')['event_id'],'new')

if __name__ == '__main__':unittest.main()
