"""Use the real Hermes agent loop as CHIYO's completion engine."""
from __future__ import annotations
import uuid
from types import SimpleNamespace

class HermesCompletionProvider:
    def __init__(self, config, *, tools=False):
        self.config=config;self.tools=tools;self.last_report={}

    def complete(self, messages):
        from run_agent import AIAgent
        import os
        if not messages or messages[-1].get('role')!='user':
            raise ValueError('Hermes host requires a final direct user message')
        system='\n\n'.join(m['content'] for m in messages if m.get('role')=='system')
        history=[dict(m) for m in messages[:-1] if m.get('role')!='system']
        agent=AIAgent(model=self.config.model,api_key=os.environ[self.config.api_key_env],
            base_url=self.config.base_url,provider='custom',api_mode='chat_completions',
            max_iterations=8,enabled_toolsets=['terminal','file'] if self.tools else [],
            disabled_toolsets=['memory','session_search','cronjob'],skip_memory=True,
            skip_context_files=True,quiet_mode=True,save_trajectories=False,
            platform='cli',session_id='chiyo-'+uuid.uuid4().hex,
            request_overrides={'temperature':self.config.sampling.get('temperature',1.0)},skip_background_review=True,run_budget_seconds=90)
        try:
            result=agent.run_conversation(user_message=messages[-1]['content'],
                system_message=system,conversation_history=history)
            content=result.get('final_response')
            if not content:raise RuntimeError('Hermes returned no final response')
            self.last_report={'engine':'Hermes AIAgent','module':AIAgent.__module__,
                'api_calls':result.get('api_calls'),'completed':result.get('completed'),
                'tools_enabled':self.tools,'builtin_memory_enabled':False,
                'message_roles':[m.get('role') for m in result.get('messages',[])]}
            return SimpleNamespace(content=content,usage={},finish_reason='stop')
        finally:agent.close()
