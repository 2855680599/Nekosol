"""Check source completeness and hashes, not publisher authenticity."""
from pathlib import Path
import argparse,hashlib,json,os
ROOT=Path(__file__).resolve().parents[1]
VALID_STATUSES={'AUTOMATED_QUALIFIED_EXPERIENCE_CANDIDATE','SOURCE_REVIEWED_TARGETED_TESTS_PASSED'}

def installed_directory(relative):
 parts=relative.parts
 return (parts==('.git',) or '__pycache__' in parts or
  parts[:3] in (('vendor','hermes','.venv'),('vendor','hermes','venv')) or
  (len(parts)==3 and parts[:2]==('vendor','hermes') and parts[-1].endswith('.egg-info')))

def verify(root):
 root=Path(root).resolve()
 data=json.loads((root/'MANIFEST.json').read_text(encoding='utf8'));files=data['files'];errors=[]
 for name,expected in files.items():
  relative=Path(name);p=root/relative
  if relative.is_absolute() or '..' in relative.parts or root not in p.resolve().parents:errors.append(name);continue
  if any(q.is_symlink() for q in (p,*p.parents) if q!=root):errors.append(name);continue
  if not p.is_file() or hashlib.sha256(p.read_bytes()).hexdigest()!=expected:errors.append(name)
 actual=set()
 for folder,dirs,names in os.walk(root,followlinks=False):
  base=Path(folder)
  for directory in list(dirs):
   path=base/directory;relative=path.relative_to(root)
   if installed_directory(relative):dirs.remove(directory)
   elif path.is_symlink():actual.add(relative.as_posix());dirs.remove(directory)
  for name in names:
   relative=(base/name).relative_to(root)
   if relative.as_posix() not in ('MANIFEST.json','.git'):actual.add(relative.as_posix())
 extras=sorted(actual-set(files))
 lines=''.join(f'{sha}  {name}\n' for name,sha in sorted(files.items()))
 if hashlib.sha256(lines.encode()).hexdigest()!=data['tree_sha256']:errors.append('tree_sha256')
 if len(files)!=data['file_count']:errors.append('file_count')
 if data.get('status') not in VALID_STATUSES:errors.append('unsupported_status')
 return {'files':len(files),'changed_or_missing':errors,'unexpected_files':extras,'status':data.get('status'),'valid':not errors and not extras}

def main():
 parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--root',type=Path,default=ROOT);args=parser.parse_args()
 result=verify(args.root);print(json.dumps(result));return not result['valid']
if __name__=='__main__':raise SystemExit(main())
