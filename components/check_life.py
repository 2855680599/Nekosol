"""Isolated real Life owner assembly, Native forwarding and writer lease proof."""
import json,os,pathlib,subprocess,sys,tempfile,time
from types import SimpleNamespace as NS
from unittest.mock import patch
root=pathlib.Path(__file__).resolve().parent
sys.path[:0]=[str(root/'native'),str(root/'native/src'),str(root/'life/plugin'),str(root/'life/scripts')]
from native_life import NativeLifeRuntime, NativeLLMFacade
with tempfile.TemporaryDirectory() as td:
    install=pathlib.Path(td)/'install';(install/'state').mkdir(parents=True,mode=0o700)
    os.environ.update(CHIYO_LIFE_INSTALL_ROOT=str(install),CHIYO_LIFE_INTEGRATION_MODE='live',
        CHIYO_LIFE_PRODUCTION_CUTOVER='canonical',LIFE_RUNTIME_ENABLED='true',AGENCY_ENABLED='true',
        ACTION_EXECUTION_ENABLED='false',PROACTIVE_ENABLED='false',CHIYO_NATIVE_LIFE_ROOT=str(root/'life'))
    (install/'cognition-config.json').write_text(json.dumps({'enabled':True,'effect':'shadow','timeout_s':2,'max_output_tokens':512}))
    os.environ['CHIYO_COGNITION_CONFIG']=str(install/'cognition-config.json')
    calls=[]
    rt=NS(model_key='unused-offline',model_cfg=NS(model='fake',api_key_env='KEY',endpoint='http://unused',timeout_seconds=60,
                      sampling={'temperature':1,'top_p':.9}),
        handle_turn=lambda *a,**k:{'turn_id':'t','raw_content':'unchanged'},
        commit_turn=lambda *a:None,record_delivery_success=lambda *a:'inserted')
    class FakeProvider:
        def __init__(self,cfg,*,api_key=None):self.cfg=cfg;calls.append(cfg);assert api_key=='unused-offline'
        def complete(self,messages):return NS(content='{}',usage={},finish_reason='stop')
    with patch('app.model.DirectProvider',FakeProvider):
        wrapper=NativeLifeRuntime(rt,environment=os.environ)
        assert wrapper.load_error is None,wrapper.load_error
        adapter=wrapper._adapter;status=adapter.runtime_status()
        assert status['runtime_loaded'] and status['lease']['held']
        assert adapter._COGNITION is not None, adapter._COGNITION_ERROR
        assert wrapper.handle_turn('c','canary',message_id='telegram:42:77')['raw_content']=='unchanged'
        assert wrapper._dispatcher.flush(5)
        assert wrapper.handle_turn('c','canary',message_id='telegram:42:77')['raw_content']=='unchanged'
        assert wrapper._dispatcher.flush(5)
        assert adapter._METRICS['hook_invocations']==1, adapter._METRICS
        second_code='import sys;sys.path.insert(0,'+repr(str(root/'life/scripts'))+');import activity_continuity as a;lease=a.ActivityAuthorityLease('+repr(str(install/'state'))+');lease.acquire()'
        second=subprocess.run([sys.executable,'-c',second_code],capture_output=True,text=True)
        assert second.returncode!=0,'second writer acquired live lease'
        facade=NativeLLMFacade(rt);facade.complete(messages=[],purpose='agency_cognition',timeout=2,max_tokens=9999)
        assert calls[-1].timeout_seconds==2 and calls[-1].sampling['max_tokens']==1024
        assert rt.model_cfg.timeout_seconds==60 and rt.model_cfg.sampling['temperature']==1
        loaded=adapter.loaded_module_evidence('test')
        assert loaded['life_module_count']>10,loaded
        owner=adapter.write_identity('test')['runtime_identity']['activity_owner_id']
        assert owner.startswith('LR2:canonical:cap-') and 'unknown' not in owner,owner
        wrapper.close()
        assert not adapter.runtime_status()['runtime_loaded']
        reacquire=subprocess.run([sys.executable,'-c',second_code],capture_output=True,text=True)
        assert reacquire.returncode==0,reacquire.stderr
        print(json.dumps({'life_loaded':True,'shadow_loaded':True,'native_forwarding':True,
            'restart_dedup':True,'second_writer_denied':True,'lease_released':True,
            'sampling_isolated':True,'loaded_modules':loaded['life_module_count']}))
