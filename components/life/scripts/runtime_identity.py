"""Candidate-only identity from already-loaded Python module objects.

Loaded code and on-disk source are reported as distinct measurements. This module
never treats a disk read as proof of what a process imported.
"""
from __future__ import annotations
import hashlib
import inspect
import marshal
import os
import sys
import types
import uuid
from pathlib import Path
from life_owner_identity import owner_id_of

_RUNTIME_EPOCH = uuid.uuid4().hex

class IdentityNotReady(ValueError): pass

def _loaded_code_digest(module: types.ModuleType) -> str:
    """Hash code objects reachable from a live module namespace, not source files."""
    found: dict[str, bytes] = {}
    seen: set[int] = set()
    def visit(label: str, obj: object) -> None:
        if id(obj) in seen: return
        seen.add(id(obj))
        if inspect.ismodule(obj): return
        if inspect.isfunction(obj) or inspect.ismethod(obj):
            code=getattr(obj,'__code__',None)
            if code is not None: found[label]=marshal.dumps(code)
            return
        if isinstance(obj,(staticmethod,classmethod)):
            visit(label,obj.__func__);return
        if isinstance(obj,property):
            for n,fn in [('get',obj.fget),('set',obj.fset),('del',obj.fdel)]:
                if fn:visit(label+'.'+n,fn)
            return
        if inspect.isclass(obj) and getattr(obj,'__module__',None)==module.__name__:
            for n,value in vars(obj).items(): visit(label+'.'+n,value)
    for name,obj in vars(module).items(): visit(module.__name__+'.'+name,obj)
    if not found: raise IdentityNotReady(f'no loaded code objects found for {module.__name__}')
    h=hashlib.sha256()
    for name,payload in sorted(found.items()):
        h.update(name.encode());h.update(b'\0');h.update(payload);h.update(b'\0')
    return h.hexdigest()

def _proc_start_identity() -> str:
    """Read this process's kernel start tick; it is not a module/source claim."""
    try:
        fields=Path('/proc/self/stat').read_text().split()
        return f'{fields[21]}:{Path("/proc/sys/kernel/random/boot_id").read_text().strip()}'
    except (OSError,IndexError):
        return 'UNAVAILABLE'

def _module_record(logical_name: str, module: types.ModuleType, expected_path: str | None) -> dict[str,object]:
    if sys.modules.get(module.__name__) is not module:
        raise IdentityNotReady(f'{logical_name}: module object is not the object registered in sys.modules')
    loaded_path=getattr(module,'__file__',None)
    if not loaded_path: raise IdentityNotReady(f'{logical_name}: loaded module has no __file__')
    resolved=Path(loaded_path).resolve(strict=True)
    spec_origin=getattr(getattr(module,'__spec__',None),'origin',None)
    if spec_origin and Path(spec_origin).resolve(strict=True)!=resolved:
        raise IdentityNotReady(f'{logical_name}: module __file__ differs from spec origin')
    try:
        expected=str(Path(expected_path).resolve(strict=True)) if expected_path else None
    except OSError as exc:
        raise IdentityNotReady(f'{logical_name}: expected module path is unavailable') from exc
    disk_hash=None
    if resolved.is_file(): disk_hash=hashlib.sha256(resolved.read_bytes()).hexdigest()
    return {'logical_name':logical_name,'absolute_path':str(resolved),'module_object_id':id(module),
            'module_name':module.__name__,'loaded_code_sha256':_loaded_code_digest(module),
            'disk_sha256_observed_separately':disk_hash,'expected_path':expected,
            'path_matches_expected':(expected is None or str(resolved)==expected),
            'version':str(getattr(module,'__version__',getattr(module,'BUILD_ID','unknown')))}

def _owner_observation(owner: object) -> tuple[str, bool]:
    """Derive owner identity and writer authority from the actual LR-2 service lease/capability."""
    typ=type(owner)
    if typ.__name__ != 'ActivityCommandService' or typ.__module__ != 'activity_continuity':
        raise IdentityNotReady('owner instance is not the imported LR-2 ActivityCommandService')
    store=getattr(owner,'_store',None); lease=getattr(owner,'_lease',None); cap=getattr(owner,'_capability',None)
    if store is None or lease is None or cap is None:
        raise IdentityNotReady('LR-2 owner is missing store/lease/capability')
    domain=getattr(cap,'writer_domain',None)
    if domain!='life_activity_authority':
        raise IdentityNotReady('LR-2 writer domain mismatch')
    verifier=getattr(lease,'verify_capability',None)
    held=bool(getattr(lease,'held',False))
    if not callable(verifier) or not verifier(cap):
        raise IdentityNotReady('LR-2 capability signature invalid')
    owner_id=owner_id_of(store, cap)
    # A held, verifiable LR-2 writer capability is enabled; no caller boolean is trusted.
    return owner_id, held

def build_identity(*, modules: dict[str,types.ModuleType], expected_paths: dict[str,str],
                   candidate_id: str, build_id: str,
                   owner_instances: tuple[object,...]) -> dict[str,object]:
    if not modules: raise IdentityNotReady('no loaded modules supplied')
    ids=[id(x) for x in owner_instances]
    if len(ids)!=len(set(ids)): raise IdentityNotReady('duplicate owner object supplied')
    if len(owner_instances)!=1: raise IdentityNotReady('canonical Activity owner count is not exactly one')
    owner_id,writer_state=_owner_observation(owner_instances[0])
    if not any(name in modules for name in ('LR-2','LR2','activity_continuity')):
        raise IdentityNotReady('canonical LR-2 module object missing')
    records=[]
    for logical,module in sorted(modules.items()):
        if not isinstance(module,types.ModuleType): raise IdentityNotReady(f'{logical}: not a live module object')
        records.append(_module_record(logical,module,expected_paths.get(logical)))
    mismatches=[x['logical_name'] for x in records if not x['path_matches_expected']]
    if mismatches: raise IdentityNotReady('module path mismatch: '+','.join(mismatches))
    return {'process_id':os.getpid(),'process_started_at':_proc_start_identity(),
            'python_version':sys.version,'runtime_epoch_id':_RUNTIME_EPOCH,
            'activity_owner_id':owner_id,'owner_count':len(owner_instances),
            'writer_enabled':writer_state,'candidate_id':candidate_id,'build_id':build_id,
            'modules':records}

def validate_cutover_identity(identity: dict[str,object], *, expected_candidate_id: str,
                              expected_owner_id: str, expected_loaded_code: dict[str,str],
                              expected_writer_enabled: bool, expected_disk_hashes: dict[str,str] | None=None) -> dict[str,object]:
    if not isinstance(identity,dict): return {'ready':False,'reason':'identity_not_mapping'}
    if identity.get('candidate_id')!=expected_candidate_id:return {'ready':False,'reason':'wrong_candidate'}
    if identity.get('activity_owner_id')!=expected_owner_id or identity.get('owner_count')!=1:return {'ready':False,'reason':'owner_mismatch'}
    rows={x.get('logical_name'):x for x in identity.get('modules',[]) if isinstance(x,dict)}
    for name,digest in expected_loaded_code.items():
        if name not in rows or rows[name].get('loaded_code_sha256')!=digest:return {'ready':False,'reason':'loaded_code_mismatch:'+name}
    for name,digest in (expected_disk_hashes or {}).items():
        if name not in rows or rows[name].get('disk_sha256_observed_separately')!=digest:return {'ready':False,'reason':'disk_manifest_mismatch:'+name}
    if identity.get('writer_enabled') is not expected_writer_enabled:return {'ready':False,'reason':'writer_state_unexpected'}
    return {'ready':True,'reason':'identity_verified'}
