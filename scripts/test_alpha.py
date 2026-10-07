"""Run Alpha acceptance scripts with all artifacts outside the source tree."""
import os,sys,subprocess,tempfile,json
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]/'components/alpha'
def main():
 rows=[]
 with tempfile.TemporaryDirectory(prefix='nyairo-alpha-validation-') as home:
  env={k:v for k,v in os.environ.items() if k in ('PATH','LANG','TZ')}
  env.update(HOME=home,PYTHONPATH=str(ROOT),PYTHONDONTWRITEBYTECODE='1',PYTHONUTF8='1')
  for name in ('a_clean_install','b_core_demo','c_contact_demo','d_semantics','e_safety','f_regression'):
   r=subprocess.run([sys.executable,'-B',str(ROOT/'tests/acceptance'/f'{name}.py'),'--work-dir',str(Path(home)/name),'--json'],cwd=ROOT,env=env,capture_output=True,text=True)
   try:payload=json.loads(r.stdout)
   except ValueError:payload={'status':'FAIL','error':'invalid acceptance output'}
   rows.append({'script':name,'exit':r.returncode,'result':payload})
 print(json.dumps(rows,ensure_ascii=False,indent=2))
 # PARTIAL explicitly retains the unimplemented N8 gate, not a full acceptance.
 return any(r['exit']!=0 or r['result']['status']=='FAIL' for r in rows)
if __name__=='__main__':raise SystemExit(main())
