"""General plugin: no global/core module monkey patching."""
def register(ctx):
    from chiyo_bundle.hermes_plugin import ChiyoContextEngine,memory_command,status_command,consider_command,note_command,close_services,configure_host_llm,memory_tool_gate,write_status_evidence
    configure_host_llm(ctx.llm)
    ctx.register_context_engine(ChiyoContextEngine())
    for command in ('chiyo_memory','chiyo-memory'):
        ctx.register_command(command,memory_command,description='List, correct or logically forget personal memories',args_hint='list | delete ID | correct ID TEXT')
    ctx.register_hook("pre_tool_call",memory_tool_gate)
    ctx.register_command('chiyo_status',status_command,description='Read current life, world/body, resource and cognition status')
    ctx.register_command('chiyo_consider',consider_command,description='Submit an explicit request for cognition Shadow observation',args_hint='REQUEST')
    ctx.register_command('chiyo_note',note_command,description='Save or read a personal ManagedArtifact',args_hint='TITLE | CONTENT ; read ID')
    ctx.on_unload(close_services)
    try:write_status_evidence()
    except Exception:
        import logging
        logging.getLogger('chiyo.hermes').warning('chiyo.startup.status.unavailable')
