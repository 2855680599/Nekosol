"""Read loaded module objects and code objects inside the actual chat process."""
import hashlib,json,marshal,os,sys,time
from pathlib import Path

def write_identity(runtime, life, data_dir):
    base=getattr(runtime,'_runtime',runtime)
    methods={'native.handle_turn':base.handle_turn,
             'persona.load_system_prompt':base.persona_provider.load_system_prompt,
             'memory_controls.projection':base.memory_controls.projection if base.memory_controls else None}
    if base.m37_resolver:
        methods['resolver.current_turn']=base.m37_resolver.resolve_current_turn
        methods['resolver.call']=base.m37_resolver.__call__
    if life and life._adapter:
        methods['life.hook']=life._adapter.life_hook
    loaded={}
    for name,method in methods.items():
        if method is None:continue
        module=sys.modules.get(method.__module__)
        code=method.__code__
        loaded[name]={'module':method.__module__,'file':str(getattr(module,'__file__','')),
                      'loaded_code_sha256':hashlib.sha256(marshal.dumps(code)).hexdigest()}
    projection=base.memory_controls.projection() if base.memory_controls else None
    doc={'build':'chiyo-v0.1-closeout-20261002','pid':os.getpid(),'at':time.time(),
        'legacy_memory_injection':base.persona_provider.include_legacy,
        'memory_control_healthy':projection.healthy if projection else False,
        'memory_control_count':projection.controls if projection else None,
        'resolver_state':getattr(base.m37_resolver,'state','UNAVAILABLE'),
        'life_loaded':bool(life and life._adapter and life._adapter.runtime_status()['runtime_loaded']),
        'life_load_error':getattr(life,'load_error',None),'loaded_objects':loaded}
    if base.world_body is not None:
        status=base.world_body.get_status()
        doc['world_body_outcome']=status.outcome
        doc['world_body_ready']=bool((status.data or {}).get('ready'))
    path=Path(data_dir)/'runtime_identity.json';temp=path.with_suffix('.tmp')
    temp.write_text(json.dumps(doc,indent=2),encoding='utf8');os.chmod(temp,0o600);os.replace(temp,path)
    return doc
