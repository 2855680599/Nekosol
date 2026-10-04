"""Verify the shipped allowlist; installed dependencies are outside this seal."""
from pathlib import Path
import hashlib,json
ROOT=Path(__file__).resolve().parents[1]
def main():
 data=json.loads((ROOT/'MANIFEST.json').read_text(encoding='utf8'));files=data['files'];errors=[]
 for name,expected in files.items():
  p=ROOT/name
  if ROOT not in p.resolve().parents or p.is_symlink():errors.append(name);continue
  if not p.is_file() or hashlib.sha256(p.read_bytes()).hexdigest()!=expected:errors.append(name)
 lines=''.join(f'{sha}  {name}\n' for name,sha in sorted(files.items()))
 if hashlib.sha256(lines.encode()).hexdigest()!=data['tree_sha256']:errors.append('tree_sha256')
 if len(files)!=data['file_count']:errors.append('file_count')
 print(json.dumps({'files':len(files),'changed_or_missing':errors,'status':data['status']}))
 return bool(errors)
if __name__=='__main__':raise SystemExit(main())
