"""Run owned suites and the official Hermes runner without personal credentials."""
import argparse, os, subprocess, sys, tempfile
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
def main():
 p=argparse.ArgumentParser();p.add_argument('--full-hermes',action='store_true');a=p.parse_args()
 suites=[('closeout','components','',['-m','unittest','-q','test_closeout']),
  ('native','components','native:native/src',['-m','unittest','discover','-s','native/src/tests','-q']),
  ('memory','components','native',['-m','unittest','-q','memory_runtime_v1.test_runtime']),
  ('supply','components','supply',['-m','unittest','discover','-s','supply/lifesupply/tests','-q']),
  ('world','components','world:world/service:world/service/bridge:world/scripts',['-m','unittest','discover','-s','world/service','-q']),
  ('life','components','',['check_life.py']),
  ('host-boundaries','','.:vendor/hermes',['-m','unittest','discover','-s','tests','-p','test_*.py','-q']),
  ('cognition-faults','','.:vendor/hermes',['-m','pytest','tests/test_cognition_reliability.py','-q'])]
 failed=[]
 with tempfile.TemporaryDirectory(prefix='chiyo-validation-') as home:
  env={k:v for k,v in os.environ.items() if k in ('PATH','LANG','TZ','SYSTEMROOT')}
  env.update(HOME=home,HERMES_HOME=home,PYTHONDONTWRITEBYTECODE='1',PYTHONUTF8='1')
  for name,subdir,paths,args in suites:
   cwd=ROOT/subdir;env['PYTHONPATH']=os.pathsep.join(str(cwd/x) for x in paths.split(':') if x)
   print('SUITE',name,flush=True)
   if subprocess.run([sys.executable,'-B',*args],cwd=cwd,env=env).returncode:failed.append(name)
  env.pop('PYTHONPATH',None);env['HERMES_PYTHON']=sys.executable
  paths=[] if a.full_hermes else ['tests/agent/test_context_engine.py','tests/agent/test_context_engine_host_contract.py','tests/agent/test_context_engine_select_context.py','tests/run_agent/test_plugin_context_engine_init.py']
  if subprocess.run(['bash','scripts/run_tests.sh','-j','2',*paths],cwd=ROOT/'vendor/hermes',env=env).returncode:failed.append('Hermes')
 print('FAILED_SUITES',failed,flush=True);return bool(failed)
if __name__=='__main__':raise SystemExit(main())
