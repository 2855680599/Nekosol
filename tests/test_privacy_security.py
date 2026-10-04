"""Offline regressions: real local HTTP, storage, permissions and instance assembly."""
import hashlib
import importlib.util
import io
import json
import logging
import os
from pathlib import Path
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import traceback
from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock, patch
import urllib.error
import urllib.request

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT),str(ROOT/'vendor/hermes'),str(ROOT/'components/native'),str(ROOT/'components/native/src')]


def load_file(name,path):
    spec=importlib.util.spec_from_file_location(name,path)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);return module


class NativePrivacyTests(unittest.TestCase):
    def test_http_rejects_unauthorized_reads_and_writes_before_runtime(self):
        from app.server import Handler
        from http.server import ThreadingHTTPServer
        runtime=NS(store=NS(load=Mock(return_value=[])),traces=NS(find=Mock()),evidence=NS(status=Mock()),
            handle_turn=Mock(return_value={'conversation_id':'private','turn_id':'turn','trace_id':'trace'}),record_delivery_success=Mock())
        handler=type('TestHandler',(Handler,),{'runtime':runtime,'auth_token':'x'*40})
        server=ThreadingHTTPServer(('127.0.0.1',0),handler)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        base='http://127.0.0.1:'+str(server.server_port)
        try:
            for path in ('/v1/conversations/private','/v1/traces/private','/v1/evidence/status','/v1/turn'):
                body=json.dumps({'conversation_id':'private','user_text':'secret'}).encode() if path.endswith('turn') else None
                for token in ('','Bearer wrong'):
                    request=urllib.request.Request(base+path,data=body,headers={'Authorization':token})
                    with self.assertRaises(urllib.error.HTTPError) as caught:urllib.request.urlopen(request)
                    self.assertEqual(caught.exception.code,401)
            runtime.store.load.assert_not_called();runtime.handle_turn.assert_not_called();runtime.traces.find.assert_not_called()
            request=urllib.request.Request(base+'/v1/conversations/private',headers={'Authorization':'Bearer '+'x'*40})
            with urllib.request.urlopen(request) as response:self.assertEqual(response.status,200)
            runtime.store.load.assert_called_once_with('private')
        finally:server.shutdown();server.server_close();thread.join()

    def test_http_startup_requires_token_and_remote_opt_in(self):
        from app.server import create_server
        config=NS(auth_token_env='TEST_NATIVE_TOKEN',host='0.0.0.0',port=0,allow_remote=False)
        with patch.dict(os.environ,{'TEST_NATIVE_TOKEN':''}):
            with self.assertRaisesRegex(ValueError,'auth token'):create_server(config,NS())
        with patch.dict(os.environ,{'TEST_NATIVE_TOKEN':'a'*40}):
            with self.assertRaisesRegex(ValueError,'allow_remote'):create_server(config,NS())

    def test_provider_error_never_exposes_upstream_echo_or_transport_details(self):
        from app.model import DirectProvider,ProviderError
        config=NS(api_key_env='TEST_PROVIDER_KEY',endpoint='https://example.invalid/v1',model='test',sampling={},timeout_seconds=1)
        provider=DirectProvider(config,api_key='not-a-real-credential')
        errors=[urllib.error.HTTPError(config.endpoint,400,'bad',{},io.BytesIO(b'PRIVATE_CHAT_ECHO')),
                urllib.error.URLError('PRIVATE_CHAT_ECHO')]
        for error in errors:
            with patch('urllib.request.urlopen',side_effect=error):
                messages=[{'role':'user','content':errors[1].reason}]
                try:provider.complete(messages)
                except ProviderError:
                    rendered=traceback.format_exc();self.assertNotIn('PRIVATE_CHAT_ECHO',rendered)
                else:self.fail('provider must reject the upstream error')

    def test_evidence_logs_hash_source_identity_but_keeps_deduplication(self):
        from app.evidence import EvidenceWriter,EvidenceEvent,USER_ORIGIN,USER_ROLE,RECEIVED
        stream=io.StringIO();logger=logging.Logger('private-regression');logger.addHandler(logging.StreamHandler(stream))
        with tempfile.TemporaryDirectory() as temp:
            event=EvidenceEvent(event_id='e1',occurred_at='2026-10-04T00:00:00+00:00',created_at='2026-10-04T00:00:00+00:00',
                memory_owner='owner',source_origin=USER_ORIGIN,delivery_status=RECEIVED,epistemic_role=USER_ROLE,speaker='user',
                content='PRIVATE_CHAT_ECHO',conversation_id='opaque-conversation',turn_id='turn',
                source_refs=[{'kind':'native_telegram_input','id':'telegram:fixture-private-owner:1'}])
            writer=EvidenceWriter(temp,logger=logger)
            self.assertEqual(writer.append(event),'inserted');self.assertEqual(writer.append(event),'duplicate')
            text=stream.getvalue();self.assertNotIn('fixture-private-owner',text);self.assertNotIn(event.content,text)
            self.assertIn('source_ref_hash=',text)


class ProfilePrivacyTests(unittest.TestCase):
    def test_life_assembly_uses_explicit_config_without_changing_process_environment(self):
        from chiyo_bundle.instance import Instance
        before=dict(os.environ)
        env={'CHIYO_MODEL_API_KEY':'unused-offline','CHIYO_MODEL':'offline-model','CHIYO_MODEL_BASE_URL':'https://example.invalid/v1'}
        with tempfile.TemporaryDirectory() as temp:
            instance=Instance(Path(temp)/'state',owner='fixture-life',memory=True,life=True,environment=env)
            try:
                self.assertEqual(dict(os.environ),before)
                self.assertIsNotNone(instance.life_wrapper)
                self.assertIsNone(instance.life_wrapper.load_error)
                self.assertTrue(instance.life_wrapper._adapter.runtime_status()['runtime_loaded'])
                self.assertEqual(instance.life_wrapper._adapter.STATE_ROOT,Path(temp)/'state/life/state')
            finally:instance.close()
        self.assertEqual(dict(os.environ),before)

    def test_instance_assembly_keeps_process_environment_and_independent_memory_paths(self):
        from chiyo_bundle.instance import Instance
        before=dict(os.environ)
        env={'CHIYO_MODEL_API_KEY':'unused-offline','CHIYO_MODEL':'offline-model','CHIYO_MODEL_BASE_URL':'https://example.invalid/v1'}
        with tempfile.TemporaryDirectory() as temp:
            first=Instance(Path(temp)/'a',owner='fixture-a',memory=True,environment=env)
            second=Instance(Path(temp)/'b',owner='fixture-b',memory=True,environment=env)
            try:
                self.assertEqual(dict(os.environ),before)
                self.assertNotEqual(first.binding['conversation_id'],second.binding['conversation_id'])
                self.assertNotEqual(first.native.m37_bridge.load_m0_path(),second.native.m37_bridge.load_m0_path())
                self.assertEqual(first.native.m37_resolver.state,'READY')
                self.assertEqual(second.native.m37_resolver.state,'READY')
            finally:first.close();second.close()
        self.assertEqual(dict(os.environ),before)

    def test_profile_default_is_neutral_private_and_not_implicitly_authorized(self):
        from chiyo_bundle.hermes_plugin import allowed
        with tempfile.TemporaryDirectory() as temp:
            home=Path(temp)/'profile'
            old=os.umask(0)
            try:
                completed=subprocess.run([sys.executable,str(ROOT/'scripts/setup_profile.py'),'--home',str(home),'--owner','fixture-owner','--memory'],capture_output=True,text=True)
            finally:os.umask(old)
            self.assertEqual(completed.returncode,0,completed.stderr)
            config=json.loads((home/'chiyo/config.json').read_text())
            self.assertFalse(allowed(config,'cli',None));self.assertFalse(allowed({'cli_owner':True},'local',None))
            config.update(cli_owner=True,local_owner_uid=os.getuid())
            self.assertTrue(allowed(config,'cli',None))
            self.assertEqual(stat.S_IMODE((home/'chiyo').stat().st_mode),0o700)
            for path in (home/'chiyo/config.json',home/'config.yaml',home/'SOUL.md'):
                self.assertEqual(stat.S_IMODE(path.stat().st_mode),0o600)
            self.assertNotIn('千代',(home/'SOUL.md').read_text())
            self.assertNotIn('宝宝',(home/'SOUL.md').read_text())
        from chiyo_bundle.instance import Instance
        with tempfile.TemporaryDirectory() as temp:
            state=Path(temp)/'state'
            with self.assertRaisesRegex(ValueError,'arbitrary host tools'):Instance(state,memory=True,tools=True)
            self.assertFalse(state.exists())


class SourceSealTests(unittest.TestCase):
    def test_whole_tree_scanner_rejects_new_vendor_credentials_and_private_state(self):
        scanner=load_file('public_source_scanner',ROOT/'scripts/scan_public_source.py')
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);vendor=root/'vendor/hermes';vendor.mkdir(parents=True)
            path=vendor/'unexpected.py';path.write_text('value='+'sk-'+'X'*24)
            result=scanner.scan(root,[])
            self.assertEqual(result['status'],'FAIL')
            self.assertEqual(result['unapproved_matches'][0]['path'],'vendor/hermes/unexpected.py')
            path.unlink();(root/'private.db').write_bytes(b'private state')
            self.assertEqual(scanner.scan(root,[])['state_or_symlink_files'],['private.db'])
            (root/'private.db').unlink()
            (root/'linked-directory').symlink_to(vendor,target_is_directory=True)
            self.assertEqual(scanner.scan(root,[])['state_or_symlink_files'],['linked-directory'])

    def test_new_file_and_unknown_status_fail_while_installed_venv_is_allowed(self):
        verifier=load_file('source_verifier',ROOT/'scripts/verify_manifest.py')
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);(root/'source.py').write_text('pass\n')
            files={'source.py':hashlib.sha256((root/'source.py').read_bytes()).hexdigest()}
            manifest={'files':files,'file_count':1,'tree_sha256':hashlib.sha256(''.join(f'{sha}  {name}\n' for name,sha in sorted(files.items())).encode()).hexdigest(),'status':'SOURCE_REVIEWED_TARGETED_TESTS_PASSED'}
            path=root/'MANIFEST.json';path.write_text(json.dumps(manifest))
            self.assertTrue(verifier.verify(root)['valid'])
            dependency=root/'vendor/hermes/.venv/bin/python';dependency.parent.mkdir(parents=True);dependency.write_text('dependency')
            self.assertTrue(verifier.verify(root)['valid'])
            (root/'unexpected.py').write_text('unexpected')
            self.assertFalse(verifier.verify(root)['valid'])
            (root/'unexpected.py').unlink();manifest['status']='INVENTED_PASS';path.write_text(json.dumps(manifest))
            self.assertFalse(verifier.verify(root)['valid'])


if __name__=='__main__':unittest.main()
