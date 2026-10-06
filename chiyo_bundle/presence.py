"""Bounded owner read projections for chat and authenticated status commands."""
import json
import socket
from pathlib import Path


def _read_supply(client, subject, op, artifact_id=None):
    if op not in ('get_workspace', 'list_artifacts', 'read_artifact'):
        raise ValueError('Not a declared read port')
    request={'op':op,'subject':subject}
    if op=='read_artifact':request['artifact_id']=artifact_id
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(client.timeout)
        connection.connect(client.socket_path)
        connection.sendall((json.dumps(request) + '\n').encode())
        with connection.makefile('rb') as reader:
            raw = reader.readline(1000001)
        if len(raw) > 1000000:
            raise ValueError('Oversized owner response')
    return json.loads(raw)


def supply_view(svc):
    client = getattr(svc.native, '_life_supply_client', None)
    subject = getattr(svc, 'supply_subject', None)
    if client is None or not subject:
        return {'state': 'OFF'}
    try:
        answer = client.list_personal_opportunities(subject)
        if answer.get('status') != 'OK':
            return {'state': 'UNAVAILABLE'}
        rows = answer.get('opportunities')
        if not isinstance(rows, list):
            return {'state': 'UNAVAILABLE'}
        workspace = _read_supply(client, subject, 'get_workspace')
        artifacts = _read_supply(client, subject, 'list_artifacts')
        if workspace.get('status') != 'OK' or artifacts.get('status') != 'OK':
            return {'state': 'UNAVAILABLE'}
        return {'state': 'READY', 'workspace_active': (workspace.get('workspace') or {}).get('lifecycle') == 'ACTIVE',
                'artifacts': [{'id': row.get('artifact_id'), 'title': str(row.get('title', ''))[:240], 'lifecycle': row.get('lifecycle')}
                              for row in (artifacts.get('artifacts') or [])[:4]],
                'opportunity_count': len(rows), 'opportunities': [
            {'id': str(row.get('opportunity_id', ''))[:180],
             'topic': str((row.get('inquiry') or {}).get('topic', ''))[:240]}
            for row in rows[:4] if isinstance(row, dict)]}
    except Exception:
        return {'state': 'UNAVAILABLE'}


def life_view(svc):
    wrapper = getattr(svc, 'life_wrapper', None)
    adapter = getattr(wrapper, '_adapter', None)
    runtime = getattr(adapter, '_RUNTIME', None)
    if runtime is None:
        return {'state': 'OFF' if wrapper is None else 'UNAVAILABLE'}
    try:
        view = runtime.activity_read.get_current_life_view()
        result = {'state': 'READY', 'life_state': view.get('life_state'),
                  'foreground_activity_kind': view.get('foreground_activity_kind'),
                  'attention_occupancy': view.get('attention_occupancy')}
        wiring = getattr(adapter, '_COGNITION', None)
        harness = getattr(wiring, 'harness', None)
        if harness is None or wiring.is_off():
            result['cognition'] = {'state': 'OFF' if wiring is None else 'UNAVAILABLE'}
        else:
            snap = harness.snapshot()
            result['cognition'] = {'state': 'SHADOW', **{k: snap.get(k, 0) for k in (
                'turns_observed', 'candidate_sets_with_candidates', 'provider_calls',
                'valid_outputs', 'invalid_outputs', 'provider_errors', 'budget_refusals',
                'canonical_decision_written', 'activity_mutations')}}
            # Read only the end of our own bounded journal, never the raw model output.
            path = Path(harness.journal_path)
            if path.exists():
                with path.open('rb') as handle:
                    handle.seek(max(0, path.stat().st_size - 65536))
                    lines = handle.read(65536).splitlines()
                for line in reversed(lines):
                    try:
                        row = json.loads(line)
                    except ValueError:
                        continue
                    result['cognition']['last_decision'] = {k: row.get(k) for k in (
                        'at', 'result', 'decision_kind', 'provider_calls')}
                    break
        return result
    except Exception:
        return {'state': 'UNAVAILABLE'}


def presence_context(svc):
    parts = []
    life = life_view(svc)
    if life['state'] == 'READY':
        parts.append('生活只读观察：' + json.dumps({k: life[k] for k in (
            'life_state', 'foreground_activity_kind', 'attention_occupancy')}, ensure_ascii=False))
        parts.append('认知判断仅为 Shadow 观察；没有执行活动或主动联系。')
    supply = supply_view(svc)
    if supply['state'] == 'READY':
        parts.append('个人资源候选（待考虑事项，不代表已经执行）：' + json.dumps(supply, ensure_ascii=False))
    return '\n'.join(parts)


def save_note(svc, grant_id, title, content):
    """One declared owner command. The service validates the real, scoped grant."""
    import hashlib
    client=svc.native._life_supply_client;subject=svc.supply_subject
    workspace=_read_supply(client,subject,'get_workspace')
    if workspace.get('status')!='OK':return workspace
    digest=hashlib.sha256(json.dumps([subject,title,content],ensure_ascii=False).encode()).hexdigest()
    key='hermes-note:'+digest
    def rpc(request):
        with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as connection:
            connection.settimeout(max(3,client.timeout));connection.connect(client.socket_path)
            connection.sendall((json.dumps(request,ensure_ascii=False)+'\n').encode())
            with connection.makefile('rb') as reader:raw=reader.readline(1000001)
        if len(raw)>1000000:raise ValueError('Oversized owner response')
        return json.loads(raw)
    prior=rpc({'op':'lookup_operation','owner':'ManagedArtifact','operation_id':key})
    if prior.get('status')=='KNOWN':
        creation=json.loads(prior['operation']['result_json'])
        version=rpc({'op':'lookup_operation','owner':'ManagedArtifact','operation_id':key+'#v1'})
        if version.get('status')!='KNOWN':return {'status':'UNKNOWN'}
        committed=json.loads(version['operation']['result_json'])
        actual=_read_supply(client,subject,'read_artifact',creation['artifact_id'])
        if actual.get('status')!='OK' or actual.get('content')!=content or actual['artifact'].get('title')!=title:
            return {'status':'CONFLICT'}
        return {'status':'NO_CHANGE','artifact_id':creation['artifact_id'],'version_number':committed.get('version_number')}
    if prior.get('status')!='NOT_FOUND':return {'status':'UNKNOWN'}
    request={'op':'create_artifact','grant_id':grant_id,'operation_key':key,
        'artifact':{'subject_id':subject,'workspace_id':workspace['workspace']['workspace_id'],'title':title,'content':content}}
    return rpc(request)


def _storage_view(svc):
    """M0 size / event count / formation cursor, or None when unavailable.

    Read-only observation: it never counts as a retention action. ``svc`` is the
    Instance itself, which owns storage_policy().
    """
    policy = getattr(svc, 'storage_policy', None)
    if not callable(policy):
        return None
    try:
        return policy()
    except Exception:
        return None


def status_snapshot(svc):
    world = getattr(svc.native, 'world_body', None)
    memory = 'OFF'
    memory_last_resolve = None
    memory_reason = None
    if svc.memory:
        try:
            healthy = svc.native.memory_controls.projection().healthy
            resolver = svc.native.m37_resolver
            memory = 'READY' if healthy and resolver.state == 'READY' else 'UNAVAILABLE'
            # A bare READY/UNAVAILABLE cannot distinguish "nothing was recalled"
            # from "recall failed"; surface the last attempt's own verdict too.
            memory_last_resolve = getattr(resolver, 'last_resolve_status', None)
            memory_reason = (getattr(resolver, 'last_error_type', None)
                             or getattr(resolver, 'last_resolve_reason', None))
        except Exception:
            memory = 'UNAVAILABLE'
    result = {'memory': memory, 'memory_last_resolve': memory_last_resolve,
              'memory_reason': memory_reason, 'storage': _storage_view(svc),
              'life': life_view(svc),
              'supply': supply_view(svc), 'world_body': {'state': 'OFF'}}
    if world is not None:
        status = world.get_status()
        result['world_body'] = {'state': 'READY' if status.ok and (status.data or {}).get('ready') else 'UNAVAILABLE'}
        if result['world_body']['state'] == 'READY':
            snapshot = world.get_snapshot()
            if snapshot.ok:
                result['world_body'].update({k: (snapshot.data or {}).get(k) for k in ('location', 'pose')})
            else:
                result['world_body']['state'] = 'UNAVAILABLE'
    return result
