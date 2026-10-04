"""Native host seam for the existing Life owner and LPC0B audit/shadow worker."""
from __future__ import annotations

import importlib
import logging
import os
from pathlib import Path
import sys
from types import SimpleNamespace
from typing import Any

from integration.live_inbound import ObservationDispatcher, LifeObservationEvent, JsonlObservationPort
from integration.live_runtime_observer import LifeObservingRuntime

LOG = logging.getLogger('chiyo.native_life')


class NativeLLMFacade:
    """Narrow host-owned facade: isolated sampling/timeouts, shared routing/auth only."""
    def __init__(self, runtime: Any):
        self._runtime = runtime

    def complete(self, *, messages, purpose, temperature=0.0, timeout=20, max_tokens=512):
        from app.model import DirectProvider
        from agency_gateway_transport import PURPOSE_AGENCY_COGNITION
        if purpose != PURPOSE_AGENCY_COGNITION:
            raise ValueError('unsupported cognition purpose')
        config = SimpleNamespace(**vars(self._runtime.model_cfg))
        config.timeout_seconds = min(60.0, max(0.1, float(timeout)))
        config.sampling = {**self._runtime.model_cfg.sampling, 'temperature': 0.0,
                           'top_p': 1.0, 'max_tokens': max(1, min(int(max_tokens), 1024))}
        result = DirectProvider(config).complete(messages)
        usage = result.usage or {}
        return SimpleNamespace(text=result.content, model=config.model, provider='native',
            finish_reason=result.finish_reason, audit={'purpose': purpose},
            usage=SimpleNamespace(input_tokens=usage.get('prompt_tokens'),
                                  output_tokens=usage.get('completion_tokens'),
                                  total_tokens=usage.get('total_tokens'), cost_usd=None))


class NativeLifePort:
    def __init__(self, adapter):
        self.adapter = adapter
        self.receipts = JsonlObservationPort(adapter.LOG_DIR / 'native_inbound_observations.jsonl')

    def observe(self, event: LifeObservationEvent):
        event.validate()
        receipt = self.receipts.observe(event)
        if receipt.get('duplicate') or not receipt.get('accepted'):
            return receipt
        # Hash-only native event identity, same across redelivery/restart.
        self.adapter.life_hook(session_id=event.conversation_ref, turn_id=event.event_id,
                               platform=event.source)
        self.adapter.write_identity('native_inbound')
        return {'accepted': True, 'duplicate': False, 'port': 'native-life'}


class NativeLifeRuntime:
    def __init__(self, runtime: Any, *, environment):
        self._runtime = runtime
        self._adapter = None
        self._dispatcher = None
        self._wrapped = runtime
        self.load_error = None
        try:
            root = Path(environment['CHIYO_NATIVE_LIFE_ROOT'])
            plugin = root / 'plugin'
            scripts = root / 'scripts'
            os.environ['CHIYO_LIFE_RELEASE_SCRIPTS'] = str(scripts)
            for p in (plugin, scripts):
                sys.path.insert(0, str(p))
            adapter = importlib.import_module('life_runtime_adapter')
            self._adapter = adapter
            adapter.register_context(SimpleNamespace(llm=getattr(runtime, "_life_llm_facade", None) or NativeLLMFacade(runtime)))
            if not adapter.start_audit():
                raise RuntimeError('Life audit writer unavailable')
            adapter.note_registration()
            status = adapter.runtime_status()
            if not status.get('runtime_loaded'):
                raise RuntimeError('Life canonical owner unavailable')
            if environment.get('CHIYO_NATIVE_LIFE_SUPPLY_ENABLED', '').lower() == 'true':
                from life_supply_candidate_source import LifeSupplyCandidateSource, ServiceUserReadClient
                from candidate_sources_ag0 import SOURCE_PERSONAL_OPPORTUNITY
                source = LifeSupplyCandidateSource(subject_id=environment['CHIYO_NATIVE_LIFE_SUPPLY_SUBJECT'],
                                                  client=getattr(runtime, "_life_supply_client", None) or ServiceUserReadClient())
                adapter._RUNTIME.candidate_registry.register_adapter(source, expected_kind=SOURCE_PERSONAL_OPPORTUNITY)
            self._dispatcher = ObservationDispatcher(NativeLifePort(adapter))
            self._wrapped = LifeObservingRuntime(runtime, self._dispatcher)
            adapter.write_identity('native_ready')
            LOG.info('native.life.ready writer_lease_held=%s cognition_shadow=%s',
                     status.get('lease', {}).get('held'), adapter._COGNITION is not None)
        except Exception as exc:
            self.load_error = type(exc).__name__
            LOG.error('native.life.unavailable error_class=%s', self.load_error)
            self.close()

    def handle_turn(self, *args, **kwargs):
        return self._wrapped.handle_turn(*args, **kwargs)

    def commit_turn(self, *args, **kwargs):
        return self._runtime.commit_turn(*args, **kwargs)

    def record_delivery_success(self, *args, **kwargs):
        return self._runtime.record_delivery_success(*args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._runtime, name)

    def close(self):
        if self._dispatcher is not None:
            self._dispatcher.close(timeout_s=3)
        if self._adapter is not None:
            self._adapter._stop_cognition()
            self._adapter._close_runtime()
            self._adapter.stop_audit(drain=True, timeout_s=3)
