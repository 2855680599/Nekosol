"""Actual Hermes calls on synthetic personal state. Never imports production state."""
import json,sqlite3,sys,hashlib
from pathlib import Path
from chiyo_bundle.instance import Instance
state=Path(sys.argv[1]);inst=Instance(state,owner='release-test',memory=True,life=True)
report={};checks=[]
def call(text,rid):
 r=inst.chat(text,request_id=rid);inst.confirm_visible(r['request_id']);print(json.dumps({'request':rid,'reply':r['reply'],'engine':r['engine'],'memory':r['memory']},ensure_ascii=False),flush=True);return r
try:
 report['health']=inst.health();assert report['health']['memory_resolver_state']=='READY'
 first=call('这是隔离测试：我的测试暗号是蓝鲸472，请记住。','fact-1');assert first['engine']['engine']=='Hermes AIAgent';checks.append('actual_Hermes_model_loop')
 cached=call('这是隔离测试：我的测试暗号是蓝鲸472，请记住。','fact-1');assert cached['turn_id']==first['turn_id'];checks.append('request_idempotency')
 try:inst.chat('不同内容',request_id='fact-1');raise AssertionError('request collision accepted')
 except ValueError:checks.append('request_collision_denied')
 db=sqlite3.connect(inst.paths['m0_db']);n=db.execute('SELECT count(*) FROM evidence_events').fetchone()[0];assert n==2,n
 event=db.execute("SELECT event_id FROM evidence_events WHERE speaker='user' ORDER BY rowid LIMIT 1").fetchone()[0]
 inst.new_session();recall=call('你还记得我之前说过的测试暗号是什么吗？','recall-1');assert recall['memory']['resolver_stats']['emitted_memory_objects']>0,recall['memory'];assert '蓝鲸472' in recall['reply'],recall['reply'];checks.append('new_session_actual_memory_consumption')
 correction=call('/memory correct '+event+' 我的测试暗号改为雪兔815。','correct-1');assert '后续以你的纠正为准' in correction['reply'];inst.new_session()
 corrected=call('你还记得我之前说过的测试暗号是什么吗？','recall-2');assert '雪兔815' in corrected['reply'] and '蓝鲸472' not in corrected['reply'],corrected['reply'];checks.append('correction_changes_actual_recall')
 target=db.execute("SELECT event_id FROM evidence_events WHERE primary_source_ref_id=? AND speaker='user'",('telegram:release-test:'+str(int(hashlib.sha256(b'correct-1').hexdigest()[:15],16)),)).fetchone()[0]
 deleted=call('/memory delete '+target,'delete-1');assert '已停止使用' in deleted['reply'];inst.new_session()
 forgotten=call('你还记得我之前说过的测试暗号是什么吗？','recall-3');assert '雪兔815' not in forgotten['reply'] and '蓝鲸472' not in forgotten['reply'],forgotten['reply'];checks.append('logical_deletion_excludes_recall_and_short_history')
 assert db.execute('SELECT count(*) FROM evidence_events WHERE event_id=?',(event,)).fetchone()[0]==1;checks.append('raw_evidence_retained')
 if inst.life_wrapper:assert inst.life_wrapper._dispatcher.flush(5);checks.append('life_observation_flush')
 report.update(checks=checks,status='PASS',evidence_events=db.execute('SELECT count(*) FROM evidence_events').fetchone()[0])
finally:
 inst.close();(state/'hermes-e2e.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
 print('RESULT',json.dumps(report,ensure_ascii=False),flush=True)
