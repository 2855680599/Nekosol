"""Standard Hermes engine, direct synthetic owner, no alternative UI."""
import json,os,sys,sqlite3,uuid
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path[:0]=[str(ROOT),str(ROOT/'vendor/hermes')]
from run_agent import AIAgent
from chiyo_bundle.hermes_plugin import memory_command,close_services,services
PLATFORM=os.environ.get('CHIYO_TEST_PLATFORM','cli')
home=Path(os.environ['HERMES_HOME']);cfg=json.loads((home/'chiyo/config.json').read_text());checks=[]
def ask(text):
 gateway={'gateway_session_key':cfg['gateway_bindings'][PLATFORM][0],'user_id':'fixture-owner','chat_id':'fixture-owner','chat_type':'dm'} if PLATFORM!='cli' else {}
 agent=AIAgent(model=os.environ['CHIYO_MODEL'],api_key=os.environ['CHIYO_MODEL_API_KEY'],base_url=os.environ['CHIYO_MODEL_BASE_URL'],provider='custom',api_mode='chat_completions',enabled_toolsets=[],skip_memory=True,skip_context_files=True,skip_background_review=True,quiet_mode=True,platform=PLATFORM,**gateway,session_id='std-'+uuid.uuid4().hex,run_budget_seconds=90)
 try:
  assert agent.context_compressor.name=='chiyo',agent.context_compressor.name
  r=agent.run_conversation(text);print(json.dumps({'reply':r['final_response'],'completed':r['completed'],'status':agent.context_compressor.get_status()},ensure_ascii=False),flush=True)
  assert r['completed'];assert not agent.context_compressor._error,agent.context_compressor._error
  return r['final_response']
 finally:agent.close()
try:
 ask('我的测试暗号是石榴936，这是专用隔离测试，请记住。');checks.append('standard_Hermes_plugin_selected')
 reply=ask('你还记得我之前说过的测试暗号是什么吗？');assert '石榴936' in reply,reply;checks.append('standard_Hermes_new_agent_memory_recall')
 from hermes_cli.plugins import _dispatch_pre_tool_call_hooks
 blocked,_=_dispatch_pre_tool_call_hooks('session_search',{'query':'石榴936'})
 assert blocked and 'CHIYO' in blocked;checks.append('real_Hermes_tool_gate_blocks_raw_transcript_recall')
 svc=services();c=sqlite3.connect(svc.paths['m0_db']);target=c.execute("SELECT event_id FROM evidence_events WHERE content LIKE '%石榴936%' AND speaker='user' ORDER BY rowid LIMIT 1").fetchone()[0]
 assert '后续以你的纠正为准' in memory_command('correct '+target+' 我的测试暗号是桃花628。');reply=ask('你还记得我之前说过的测试暗号是什么吗？');assert '桃花628' in reply and '石榴936' not in reply,reply;checks.append('standard_command_correction_consumed')
 listed=memory_command('list');assert target not in listed
 replacement=c.execute("SELECT event_id FROM evidence_events WHERE content LIKE '/memory correct %' ORDER BY rowid DESC LIMIT 1").fetchone()[0]
 assert '已停止使用' in memory_command('delete '+replacement)
 reply=ask('你还记得我之前说过的测试暗号是什么吗？');assert '石榴936' not in reply and '桃花628' not in reply,reply;checks.append('standard_command_deletion_no_resurrection')
 assert c.execute("SELECT count(*) FROM evidence_events WHERE speaker='chiyo'").fetchone()[0]==0;checks.append('no_forged_platform_delivery')
 assert all(json.loads(row[0])[0]['kind']=='hermes_personal_input' for row in c.execute('SELECT source_refs_json FROM evidence_events'));checks.append('Hermes_origin_kept_without_forged_Telegram_receipt')
 assert svc.life_wrapper._dispatcher.flush(5);checks.append('Hermes_Life_owner_observation')
 report={'status':'PASS','checks':checks,'hermes_engine':'Hermes AIAgent + chiyo context engine','profile_independent':True}
 (home/'standard-e2e.json').write_text(json.dumps(report,ensure_ascii=False,indent=2));print('RESULT',json.dumps(report),flush=True)
finally:close_services()
