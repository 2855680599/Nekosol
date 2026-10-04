"""Owner scope and generic slash dispatch, no platform network calls."""
import sys,unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path[:0]=[str(ROOT),str(ROOT/'vendor/hermes')]
from chiyo_bundle.hermes_plugin import allowed,source_scope
from hermes_cli.plugins_dispatch import invoke_plugin_command
from gateway.config import Platform
from gateway.session import SessionSource
class PlatformScopeTests(unittest.TestCase):
    def test_registered_chiyo_commands_route_from_real_telegram_events(self):
        import asyncio, importlib.util
        from types import SimpleNamespace
        from unittest.mock import patch
        from gateway.run_inbound import GatewayInboundMixin
        from gateway.platforms.base import MessageEvent
        spec=importlib.util.spec_from_file_location('fixture_registered_chiyo',ROOT/'plugins/chiyo/__init__.py')
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        commands={}
        ctx=SimpleNamespace(llm=None,register_context_engine=lambda engine:None,
            register_command=lambda name,handler,**kwargs:commands.update({name:handler}),
            register_hook=lambda *args:None,on_unload=lambda callback:None)
        with patch('chiyo_bundle.hermes_plugin.write_status_evidence'):
            module.register(ctx)
        source=SessionSource(platform=Platform.TELEGRAM,chat_type='dm',user_id='fixture-owner',chat_id='fixture-room')
        platform,key=source_scope(source)
        config={'gateway_bindings':{platform:[key]},'cognition_shadow':False}
        runner=SimpleNamespace(_draining=False,_hm_quick_commands=lambda:{})
        cases={'chiyo_status':('','还没有启用'),'chiyo_memory':('list','没有启用'),
            'chiyo_consider':('request','没有启用'),'chiyo_note':('title | content','尚未接通')}
        with patch('hermes_cli.plugins.get_plugin_command_handler',side_effect=commands.get),\
             patch('chiyo_bundle.hermes_plugin.configuration',return_value=(ROOT,config)),\
             patch('chiyo_bundle.hermes_plugin.services',return_value=None):
            for name,(args,expected) in cases.items():
                with self.subTest(command=name):
                    event=MessageEvent(text='/'+name+' '+args,source=source,message_id='fixture-'+name)
                    result=asyncio.run(GatewayInboundMixin._hm_dispatch_quick_and_plugin_commands(runner,event,source,event.get_command()))
                    self.assertTrue(result[0]);self.assertIn(expected,result[1])
            stranger=SessionSource(platform=Platform.TELEGRAM,chat_type='dm',user_id='stranger',chat_id='other-room')
            event=MessageEvent(text='/chiyo_status',source=stranger,message_id='fixture-denied')
            result=asyncio.run(GatewayInboundMixin._hm_dispatch_quick_and_plugin_commands(runner,event,stranger,event.get_command()))
            self.assertTrue(result[0]);self.assertIn('拒绝',result[1])
    def test_plugin_registers_a_Telegram_compatible_memory_command(self):
        import importlib.util
        from types import SimpleNamespace
        spec=importlib.util.spec_from_file_location('fixture_chiyo_plugin',ROOT/'plugins/chiyo/__init__.py')
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        commands={}
        ctx=SimpleNamespace(llm=None,register_context_engine=lambda engine:None,
            register_command=lambda name,handler,**kwargs:commands.update({name:handler}),
            register_hook=lambda *args:None,on_unload=lambda callback:None)
        module.register(ctx)
        self.assertRegex('chiyo_memory',r'^[a-z0-9_]{1,32}$')
        self.assertIs(commands['chiyo_memory'],commands['chiyo-memory'])
    def test_three_platforms_are_owner_bound_without_id_equality(self):
        for name in ('qqbot','weixin','feishu','telegram'):
            platform=Platform(name)
            source=SessionSource(platform=platform,chat_type='dm',user_id=name+'-owner',chat_id=name+'-room')
            kind,key=source_scope(source);self.assertEqual(kind,name)
            cfg={'gateway_bindings':{name:[key]},'cli_owner':True}
            self.assertTrue(allowed(cfg,kind,key))
            stranger=SessionSource(platform=platform,chat_type='dm',user_id='stranger',chat_id='other-room')
            k,v=source_scope(stranger);self.assertFalse(allowed(cfg,k,v))
            group=SessionSource(platform=platform,chat_type='group',user_id=name+'-owner',chat_id=name+'-room')
            k,v=source_scope(group);self.assertIsNone(v);self.assertFalse(allowed(cfg,k,v))
    def test_dispatch_preserves_legacy_and_supplies_authentic_source(self):
        sentinel=object();self.assertEqual(invoke_plugin_command(lambda args:args,'list',source=sentinel),'list')
        seen={}
        def handler(args,*,source=None):seen['source']=source;return args
        self.assertEqual(invoke_plugin_command(handler,'list',source=sentinel),'list');self.assertIs(seen['source'],sentinel)
    def test_callback_exception_is_not_retried_without_context(self):
        calls=[]
        def broken(args,*,source=None):calls.append(source);raise RuntimeError('failure')
        with self.assertRaises(RuntimeError):invoke_plugin_command(broken,'delete',source='owner')
        self.assertEqual(calls,['owner'])
if __name__=='__main__':unittest.main()
