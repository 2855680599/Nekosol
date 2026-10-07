"""General plugin: no global/core module monkey patching.

Command names
-------------
The public names are ``nyairo_*``. The ``chiyo_*`` names are the previous public
names and stay registered as compatibility aliases -- the same handler, so an
older guide, bookmark or habit keeps working; their help text says which command
they alias. Both spellings register the underscore and hyphen form, because the
gateway looks up the exact name first and only then the hyphen alias (see
HERMES_FIXES.md).
"""

#: public command name -> (handler attribute, description, args hint)
PUBLIC_COMMANDS = (
    ('nyairo_memory', 'memory_command',
     'List, correct or logically forget personal memories',
     'list | delete ID | correct ID TEXT'),
    ('nyairo_status', 'status_command',
     'Read current life, world/body, resource and cognition status', ''),
    ('nyairo_consider', 'consider_command',
     'Submit an explicit request for cognition Shadow observation', 'REQUEST'),
    ('nyairo_note', 'note_command',
     'Save or read a personal ManagedArtifact', 'TITLE | CONTENT ; read ID'),
)

#: legacy public name -> the public name it aliases
LEGACY_COMMANDS = {
    'chiyo_memory': 'nyairo_memory',
    'chiyo_status': 'nyairo_status',
    'chiyo_consider': 'nyairo_consider',
    'chiyo_note': 'nyairo_note',
}


def register(ctx):
    from chiyo_bundle.hermes_plugin import ChiyoContextEngine,memory_command,status_command,consider_command,note_command,close_services,configure_host_llm,memory_tool_gate,write_status_evidence
    handlers={'memory_command':memory_command,'status_command':status_command,
              'consider_command':consider_command,'note_command':note_command}
    configure_host_llm(ctx.llm)
    ctx.register_context_engine(ChiyoContextEngine())
    for name,handler,description,args_hint in PUBLIC_COMMANDS:
        for spelling in (name,name.replace('_','-')):
            ctx.register_command(spelling,handlers[handler],description=description,args_hint=args_hint)
    for legacy,current in LEGACY_COMMANDS.items():
        public=next(entry for entry in PUBLIC_COMMANDS if entry[0]==current)
        handler=handlers[public[1]]
        description='Legacy alias of /%s' % current
        for spelling in (legacy,legacy.replace('_','-')):
            ctx.register_command(spelling,handler,description=description,args_hint=public[3])
    ctx.register_hook("pre_tool_call",memory_tool_gate)
    ctx.on_unload(close_services)
    try:write_status_evidence()
    except Exception:
        import logging
        logging.getLogger('nyairo.hermes').warning('nyairo.startup.status.unavailable')
