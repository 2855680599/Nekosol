"""Reproduce the pinned Hermes delta with exact pre/post-image guards."""
import argparse,hashlib,json,shutil,subprocess
from pathlib import Path
HERE=Path(__file__).resolve().parent

def sha(path):return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None

def main():
 p=argparse.ArgumentParser();p.add_argument('action',choices=['check','apply','rollback']);p.add_argument('target',nargs='?',default=str(HERE.parent/'vendor/hermes'));a=p.parse_args();root=Path(a.target).resolve()
 data=json.loads((HERE/'baseline.json').read_text(encoding='utf8'));tracked=data['tracked_files'];new=data['new_modules']
 for x in tracked+new:
  path=root/x['path'];assert root in path.resolve().parents,'path escape'
  assert not any(q.is_symlink() for q in (path,*path.parents) if q!=root.parent),'symlink refused'
 clean=all(sha(root/x['path'])==x['clean_sha256'] for x in tracked) and all(not (root/x['path']).exists() for x in new)
 patched=all(sha(root/x['path'])==x['patched_sha256'] for x in tracked+new)
 if a.action=='check':
  print('CLEAN' if clean else 'PATCHED' if patched else 'DRIFT');raise SystemExit(0 if clean or patched else 1)
 if a.action=='apply':
  if patched:print('ALREADY_PATCHED');return
  if not clean:raise ValueError('Target differs from the exact upstream preimages')
  for x in new:assert sha(HERE/'new-modules'/x['path'])==x['patched_sha256'],'new module source drift'
  subprocess.run(['git','apply','--check',str(HERE/'01-tracked.patch')],cwd=root,check=True)
  subprocess.run(['git','apply',str(HERE/'01-tracked.patch')],cwd=root,check=True)
  for x in new:
   path=root/x['path'];path.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(HERE/'new-modules'/x['path'],path)
  assert all(sha(root/x['path'])==x['patched_sha256'] for x in tracked+new)
  print('APPLIED')
 else:
  if clean:print('ALREADY_CLEAN');return
  if not patched:raise ValueError('Target differs from the exact CHIYO postimages')
  subprocess.run(['git','apply','--reverse','--check',str(HERE/'01-tracked.patch')],cwd=root,check=True)
  subprocess.run(['git','apply','--reverse',str(HERE/'01-tracked.patch')],cwd=root,check=True)
  for x in new:(root/x['path']).unlink()
  assert all(sha(root/x['path'])==x['clean_sha256'] for x in tracked)
  print('ROLLED_BACK')
if __name__=='__main__':main()
