"""Reproduce pinned Hermes patches using isolated staging and no-follow targets."""
import argparse,hashlib,json,subprocess,tempfile
from contextlib import ExitStack
from pathlib import Path
from secure_target import Target
HERE=Path(__file__).resolve().parent

def digest(value):return hashlib.sha256(value).hexdigest() if value is not None else None

def manage(action,root,*,allow_shipped_rollback=False):
 root=Path(root).absolute()
 if action=='rollback' and root.resolve()==(HERE.parent/'vendor/hermes').resolve() and not allow_shipped_rollback:
  raise ValueError('Bundled Hermes needs these hooks. Use a separate upstream checkout; --allow-shipped-rollback is for deliberate development only.')
 baseline=(HERE/'baseline.json').read_bytes()
 if hashlib.sha256(baseline).hexdigest()!=(HERE/'baseline.sha256').read_text().split()[0]:raise ValueError('baseline checksum mismatch')
 data=json.loads(baseline);tracked=data['tracked_files'];new=data['new_modules'];records=tracked+new
 with ExitStack() as stack:
  target=Target(root,stack)
  import fcntl
  fcntl.flock(target.root,fcntl.LOCK_EX)
  snapshot={x['path']:target.read(x['path']) for x in records}
  clean=all(digest(snapshot[x['path']])==x['clean_sha256'] for x in tracked) and all(snapshot[x['path']] is None for x in new)
  patched=all(digest(snapshot[x['path']])==x['patched_sha256'] for x in records)
  if action=='check':print('CLEAN' if clean else 'PATCHED' if patched else 'DRIFT');return 0 if clean or patched else 1
  if (action=='apply' and patched) or (action=='rollback' and clean):print('ALREADY_PATCHED' if patched else 'ALREADY_CLEAN');return 0
  if not (clean if action=='apply' else patched):raise ValueError('Target differs from exact recorded preimages')
  with tempfile.TemporaryDirectory(prefix='nyairo-patch-') as temporary:
   stage=Path(temporary)
   for name,value in snapshot.items():
    if value is not None:
     path=stage/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(value)
   command=['git','apply']+(['--reverse'] if action=='rollback' else [])
   subprocess.run([*command,'--check',str(HERE/'01-tracked.patch')],cwd=stage,check=True)
   subprocess.run([*command,str(HERE/'01-tracked.patch')],cwd=stage,check=True)
   values={}
   for record in records:
    name=record['path']
    value=((HERE/'new-modules'/name).read_bytes() if action=='apply' else None) if record in new else (stage/name).read_bytes()
    expected=record['patched_sha256'] if action=='apply' else record.get('clean_sha256')
    if digest(value)!=expected:raise ValueError('Patch source differs from expected postimage')
    values[name]=value
   if any(target.read(name)!=before for name,before in snapshot.items()):raise ValueError('Concurrent target modification')
   for name,value in values.items():target.publish(name,value)
   if any(target.read(name)!=value for name,value in values.items()):raise ValueError('Postimage verification failed')
  print('APPLIED' if action=='apply' else 'ROLLED_BACK');return 0

def main():
 p=argparse.ArgumentParser();p.add_argument('action',choices=['check','apply','rollback']);p.add_argument('target',nargs='?',default=str(HERE.parent/'vendor/hermes'));p.add_argument('--allow-shipped-rollback',action='store_true');a=p.parse_args()
 return manage(a.action,a.target,allow_shipped_rollback=a.allow_shipped_rollback)
if __name__=='__main__':raise SystemExit(main())
