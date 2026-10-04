"""Memory authority and forgetting hold at the real Hermes host boundaries."""
import asyncio,sys,unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import patch,Mock
ROOT=Path(__file__).resolve().parents[1];sys.path[:0]=[str(ROOT),str(ROOT/'vendor/hermes')]
from chiyo_bundle.hermes_plugin import ChiyoContextEngine,memory_tool_gate
from agent.agent_init import _inject_context_engine_tools
from gateway.run_inbound import GatewayInboundMixin

class ContextSafetyTests(unittest.TestCase):
    def test_missing_completion_hook_does_not_poison_the_next_direct_input(self):
        from chiyo_bundle.instance import install_paths
        install_paths()
        for memory in (False, True):
            with self.subTest(memory=memory):
                observed=[]
                bridge=Mock()
                bridge.write_user_event.side_effect=[{'status':'inserted','event_id':'event-1'},
                    {'status':'inserted','event_id':'event-2'}]
                projection=NS(controls=0,healthy=True,recalls=lambda objects:(objects,[]),active_corrections=lambda:[])
                svc=NS(memory=memory,owner='fixture-owner',binding={'conversation_id':'fixture-conversation'},
                    native=NS(world_body=None,m37_bridge=bridge,m37_resolver=lambda **kwargs:[],
                        memory_controls=NS(projection=lambda:projection)),
                    life_wrapper=NS(_dispatcher=NS(submit=lambda event:observed.append(event))))
                engine=ChiyoContextEngine();engine._authorized=True;engine._service=svc
                engine._platform='cli';engine._session_id='fixture-session'
                first=[{'role':'user','content':'first input'}]
                engine.select_context(first,incoming_message=first[-1])
                engine.select_context(first,incoming_message=first[-1])
                self.assertEqual(len(observed),1)
                # Hermes documents terminal provider failures that skip completion.
                # The next authenticated input remains real user authority.
                second=[{'role':'user','content':'second input'}]
                engine.select_context(second,incoming_message=second[-1])
                self.assertEqual(len(observed),2)
                self.assertEqual(engine._pending['raw'],'second input')
                self.assertIsNone(engine._error)
                self.assertEqual(bridge.write_user_event.call_count,2 if memory else 0)

    def test_life_without_memory_observes_each_direct_turn_once(self):
        from chiyo_bundle.instance import install_paths
        install_paths()
        observed=[]
        svc=NS(memory=False,owner='fixture-owner',binding={'conversation_id':'fixture-conversation'},
            native=NS(world_body=None),life_wrapper=NS(_dispatcher=NS(submit=lambda event:observed.append(event))),form_evidence=Mock())
        e=ChiyoContextEngine();e._authorized=True;e._service=svc;e._platform='cli';e._session_id='fixture-session'
        messages=[{'role':'user','content':'hello'}]
        self.assertIsNone(e.select_context(messages,incoming_message=messages[0]))
        e.select_context(messages,incoming_message=messages[0])
        self.assertEqual(len(observed),1)
        self.assertEqual(observed[0].event_type,'ORDINARY_COMMUNICATION')
        e.on_turn_complete(messages);svc.form_evidence.assert_not_called()
        e.select_context(messages,incoming_message=messages[0])
        self.assertEqual(len(observed),2);self.assertNotEqual(observed[0].event_id,observed[1].event_id)
        e._authorized=False;e.select_context(messages,incoming_message=messages[0]);self.assertEqual(len(observed),2)
        self.assertEqual(messages,[{'role':'user','content':'hello'}])
    def test_real_host_passes_child_identity_and_plugin_refuses_authority(self):
        engine=ChiyoContextEngine()
        agent=NS(context_compressor=engine,tools=None,enabled_toolsets=[],session_id='child',
            platform='cli',model='fixture',_parent_session_id='parent')
        with patch('chiyo_bundle.hermes_plugin.configuration',return_value=(ROOT,{'cli_owner':True})),patch('chiyo_bundle.hermes_plugin.services') as services:
            _inject_context_engine_tools(agent)
            self.assertFalse(engine._authorized);services.assert_not_called()
            engine.on_session_start('rotated-child',platform='cli')
            self.assertFalse(engine._authorized);services.assert_not_called()
            self.assertIsNone(engine.select_context([{'role':'user','content':'invented child testimony'}],incoming_message={'content':'invented child testimony'}))
    def test_root_keeps_backwards_compatible_session_metadata(self):
        engine=NS(on_session_start=Mock())
        agent=NS(context_compressor=engine,tools=None,enabled_toolsets=[],session_id='root',platform='cli',model='fixture',_parent_session_id=None)
        _inject_context_engine_tools(agent)
        self.assertNotIn('parent_session_id',engine.on_session_start.call_args.kwargs)
    def test_real_gateway_dispatch_passes_authenticated_source(self):
        runner=NS(_draining=False,_hm_quick_commands=lambda:{})
        source=object();event=NS(get_command_args=lambda:'list');handler=Mock(return_value='reply')
        with patch('hermes_cli.plugins.get_plugin_command_handler',return_value=handler):
            result=asyncio.run(GatewayInboundMixin._hm_dispatch_quick_and_plugin_commands(runner,event,source,'chiyo-memory'))
        self.assertEqual(result,(True,'reply','chiyo-memory'));self.assertIs(handler.call_args.kwargs['source'],source)
    def test_unavailable_owner_excludes_old_context_preserving_current_tools(self):
        e=ChiyoContextEngine();e._authorized=True;e._service=None
        messages=[{'role':'system','content':'persona'},{'role':'user','content':'forgotten fact'},
            {'role':'assistant','content':'forgotten answer'},{'role':'user','content':'current'},
            {'role':'assistant','tool_calls':[{'id':'tool-1'}]},{'role':'tool','tool_call_id':'tool-1','content':'fresh result'}]
        self.assertEqual(e.select_context(messages,incoming_message={'content':'current'}),[messages[0]]+messages[3:])
    def test_multimodal_turn_cannot_resurrect_deleted_history_or_mint_text_authority(self):
        projection=NS(controls=['delete'],healthy=True)
        bridge=Mock();ledger=NS(projection=lambda:projection)
        e=ChiyoContextEngine();e._authorized=True;e._service=NS(memory=True,native=NS(memory_controls=ledger,m37_bridge=bridge))
        current=[{'type':'image_url','image_url':{'url':'data:image/png;base64,fixture'}}]
        messages=[{'role':'system','content':'persona'},{'role':'user','content':'forgotten fact'}, {'role':'user','content':current}]
        self.assertEqual(e.select_context(messages,incoming_message={'content':current}),[messages[0],messages[2]])
        bridge.write_user_event.assert_not_called()
    def test_memory_tools_do_not_bypass_forgetting_via_raw_transcripts(self):
        with patch('chiyo_bundle.hermes_plugin.configuration',return_value=(ROOT,{'memory':True})):
            for name in ['memory','session_search']:self.assertEqual(memory_tool_gate(name,{})['action'],'block')
            self.assertIsNone(memory_tool_gate('terminal',{}))
        with patch('chiyo_bundle.hermes_plugin.configuration',return_value=(ROOT,{'memory':False})):
            self.assertIsNone(memory_tool_gate('session_search',{}))
        with patch('chiyo_bundle.hermes_plugin.configuration',side_effect=ValueError('damaged')):
            self.assertEqual(memory_tool_gate('session_search',{})['action'],'block')
    def test_invalid_owner_is_rejected_before_personal_state_is_created(self):
        import tempfile
        from chiyo_bundle.instance import Instance
        with tempfile.TemporaryDirectory() as t:
            state=Path(t)/'state'
            for owner in ['', '_owner','主人','x'*129]:
                with self.assertRaises(ValueError):Instance(state,owner=owner)
                self.assertFalse(state.exists())
    def test_corrupt_ledger_excludes_multimodal_history(self):
        e=ChiyoContextEngine();e._authorized=True;e._service=NS(memory=True,native=NS(memory_controls=NS(projection=lambda:NS(controls=[],healthy=False))))
        messages=[{'role':'user','content':'forgotten'},{'role':'user','content':[{'type':'text','text':'current'}]}]
        self.assertEqual(e.select_context(messages,incoming_message=messages[-1]),[messages[-1]])
if __name__=='__main__':unittest.main()
