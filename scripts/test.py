"""Run owned suites and the official Hermes runner without personal credentials.

Each suite reports exactly one of PASS, FAIL or SKIP:

* PASS - the runner exited 0.
* FAIL - the runner exited non-zero. Assertion failures, import errors,
  collection failures and runtime errors are never downgraded.
* SKIP - a *missing optional dependency* was proven, not assumed. The only
  recognised case is pytest being absent, reported as SKIPPED_MISSING_PYTEST.
  A standard install ships ``--extra messaging --extra web`` and therefore has
  no pytest, so the two pytest-based suites cannot run there; that is "not run",
  not "failed".

Only a definitive "No module named pytest" is treated as a missing dependency.
Any other non-zero probe result counts as "not a known missing dependency": the
suite is executed and a real failure surfaces as FAIL.
"""
import argparse, os, subprocess, sys, tempfile
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
MISSING_PYTEST='SKIPPED_MISSING_PYTEST'
# Keep pytest from writing .pytest_cache inside the source tree: the verifier
# walks the filesystem (it does not read .gitignore), so test byproducts left in
# the tree would otherwise show up as unexpected_files. Passing this through the
# environment also covers the per-file pytest subprocesses that
# vendor/hermes/scripts/run_tests.sh spawns.
PYTEST_NO_CACHE='-p no:cacheprovider'
_PYTEST_CACHE={}
def module_importable(python,module):
 """True/False when the answer is definitive for `module`, else None (unknown).

 Only a definitive ModuleNotFoundError for `module` returns False, which is the
 single condition allowed to turn a suite into SKIP. Anything else returns None,
 so the caller runs the suite and lets a real error fail it.
 """
 try:
  probe=subprocess.run([python,'-B','-c','import '+module],capture_output=True,text=True)
 except OSError:
  return None
 if probe.returncode==0:return True
 text=(probe.stderr or '')+(probe.stdout or '')
 if "No module named '%s'"%module in text or 'No module named %s'%module in text:
  return False
 return None
def pytest_available():
 """Probe sys.executable plus the venvs scripts/run_tests.sh would itself use."""
 if 'pytest' in _PYTEST_CACHE:return _PYTEST_CACHE['pytest']
 candidates=[sys.executable]
 for name in ('.venv','venv'):
  candidate=ROOT/'vendor/hermes'/name/'bin/python'
  if candidate.exists():candidates.append(str(candidate))
 unknown=False
 verdict=False
 for python in candidates:
  state=module_importable(python,'pytest')
  if state is True:
   verdict=True;break
  if state is None:unknown=True
 # only claim it is missing when every candidate definitively lacks it
 _PYTEST_CACHE['pytest']=None if (unknown and not verdict) else verdict
 return _PYTEST_CACHE['pytest']
def main():
 p=argparse.ArgumentParser();p.add_argument('--full-hermes',action='store_true');a=p.parse_args()
 # name, cwd-relative subdir, PYTHONPATH entries, argv, required optional module
 suites=[('closeout','components','',['-m','unittest','-q','test_closeout'],None),
  ('native','components','native:native/src',['-m','unittest','discover','-s','native/src/tests','-q'],None),
  ('memory','components','native',['-m','unittest','-q','memory_runtime_v1.test_runtime'],None),
  ('supply','components','supply',['-m','unittest','discover','-s','supply/lifesupply/tests','-q'],None),
  ('world','components','world:world/service:world/service/bridge:world/scripts',['-m','unittest','discover','-s','world/service','-q'],None),
  ('life','components','',['check_life.py'],None),
  ('host-boundaries','','.:vendor/hermes',['-m','unittest','discover','-s','tests','-p','test_*.py','-q'],None),
  ('cognition-faults','','.:vendor/hermes',['-m','pytest',PYTEST_NO_CACHE,'tests/test_cognition_reliability.py','-q'],'pytest')]
 results=[]
 def record(name,status,reason=''):
  print('STATUS %s %s%s'%(name,status,(' '+reason) if reason else ''),flush=True)
  results.append((name,status,reason))
 def missing(needs):
  return needs=='pytest' and pytest_available() is False
 with tempfile.TemporaryDirectory(prefix='nyairo-validation-') as home:
  env={k:v for k,v in os.environ.items() if k in ('PATH','LANG','TZ','SYSTEMROOT')}
  env.update(HOME=home,HERMES_HOME=home,PYTHONDONTWRITEBYTECODE='1',PYTHONUTF8='1',PYTEST_ADDOPTS=PYTEST_NO_CACHE)
  for name,subdir,paths,args,needs in suites:
   cwd=ROOT/subdir;env['PYTHONPATH']=os.pathsep.join(str(cwd/x) for x in paths.split(':') if x)
   print('SUITE',name,flush=True)
   if missing(needs):
    record(name,'SKIP',MISSING_PYTEST);continue
   record(name,'PASS' if not subprocess.run([sys.executable,'-B',*args],cwd=cwd,env=env).returncode else 'FAIL')
  env.pop('PYTHONPATH',None);env['HERMES_PYTHON']=sys.executable
  hermes=['tests/agent/test_context_engine.py','tests/agent/test_context_engine_host_contract.py','tests/agent/test_context_engine_select_context.py','tests/run_agent/test_plugin_context_engine_init.py']
  paths=[] if a.full_hermes else hermes
  print('SUITE Hermes',flush=True)
  if missing('pytest'):
   record('Hermes','SKIP',MISSING_PYTEST)
  else:
   record('Hermes','PASS' if not subprocess.run(['bash','scripts/run_tests.sh','-j','2',*paths],cwd=ROOT/'vendor/hermes',env=env).returncode else 'FAIL')
 failed=[name for name,status,_ in results if status=='FAIL']
 skipped=[(name,reason) for name,status,reason in results if status=='SKIP']
 print('SUMMARY pass=%d fail=%d skip=%d'%(len(results)-len(failed)-len(skipped),len(failed),len(skipped)),flush=True)
 print('FAILED_SUITES',failed,flush=True)
 print('SKIPPED_SUITES',skipped,flush=True)
 return bool(failed)
if __name__=='__main__':raise SystemExit(main())
