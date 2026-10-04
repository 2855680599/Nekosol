"""Prove finite read-only world transport and authenticated chat projections."""
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'vendor/hermes'), str(ROOT / 'components/world/service')]
from chiyo_bundle.world_read_service import authorized_peer, dispatch_read, initialize
from chiyo_bundle.presence import life_view, presence_context


class PresenceTests(unittest.TestCase):
    def test_mutations_never_reach_the_world_surface(self):
        surface = Mock()
        for command in ('submit_action', 'act', 'apply_result', 'bootstrap', 'set_body_state', None):
            self.assertFalse(dispatch_read(surface, {'command': command})['ok'])
        surface.handle.assert_not_called()
        dispatch_read(surface, {'command': 'get_status'})
        surface.handle.assert_called_once_with('get_status', None)

    @unittest.skipUnless(hasattr(socket, 'SO_PEERCRED'), 'Linux peer credentials')
    def test_peer_identity_is_os_supplied_and_fails_closed(self):
        a, b = socket.socketpair()
        try:
            self.assertTrue(authorized_peer(a, os.getuid()))
            self.assertFalse(authorized_peer(a, os.getuid() + 1))
        finally:
            a.close(); b.close()

    def test_life_projection_does_not_expose_owner_authority_or_raw_state(self):
        runtime = NS(activity_read=NS(get_current_life_view=lambda: {
            'life_state': 'IDLE', 'foreground_activity_kind': None, 'attention_occupancy': 'FREE',
            'subject_id': 'private-owner', 'secret': 'hidden', 'view_revision': 123}))
        svc = NS(native=NS(), life_wrapper=NS(_adapter=NS(_RUNTIME=runtime, _COGNITION=None)))
        view = life_view(svc)
        self.assertEqual(view['life_state'], 'IDLE')
        self.assertNotIn('private-owner', json.dumps(view)); self.assertNotIn('123', json.dumps(view))
        self.assertIn('IDLE', presence_context(svc))
        self.assertNotIn('hidden', presence_context(svc))

    def test_status_and_consider_reject_unbound_chat_before_owner_access(self):
        from chiyo_bundle.hermes_plugin import status_command, consider_command, note_command
        with patch('chiyo_bundle.hermes_plugin.configuration', return_value=(ROOT, {})), \
             patch('chiyo_bundle.hermes_plugin.services') as services:
            self.assertIn('拒绝', status_command())
            self.assertIn('拒绝', consider_command('request'))
            self.assertIn('拒绝', note_command('title | content'))
            services.assert_not_called()

    def test_initialization_never_replaces_existing_world(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp) / 'world'
            initialize(home)
            state = (home / 'data/world/world_state.json').read_bytes()
            with self.assertRaises(ValueError): initialize(home)
            self.assertEqual(state, (home / 'data/world/world_state.json').read_bytes())


@unittest.skipUnless(sys.platform.startswith('linux'), 'Unix World service integration')
class WorldTransportTests(unittest.TestCase):
    def test_partial_startup_releases_writer_lease_without_resetting_corrupt_state(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)/'world'; initialize(home)
            state = home/'data/world/world_state.json'
            state.write_text('invalid JSON', encoding='utf8')
            code = '''
import gc, os
from pathlib import Path
from chiyo_bundle.world_read_service import serve
gc.disable()
home = Path(os.environ['TEST_WORLD_HOME'])
try:
    serve(home)
except Exception:
    pass
else:
    raise AssertionError('Damaged world unexpectedly started')
from world_body_authority import lock_is_held
assert not lock_is_held(home/'run'), 'Failed startup retained the single-writer lease'
assert (home/'data/world/world_state.json').read_text() == 'invalid JSON'
'''
            result = subprocess.run([sys.executable, '-B', '-c', code], capture_output=True,
                env={**os.environ, 'PYTHONPATH': str(ROOT), 'TEST_WORLD_HOME': str(home)}, timeout=20)
            self.assertEqual(result.returncode, 0, result.stderr.decode('utf8', 'replace')[-2000:])

    def test_refused_regular_file_or_live_socket_is_never_removed(self):
        for live_socket in (False, True):
            with self.subTest(live_socket=live_socket), tempfile.TemporaryDirectory() as temp:
                home = Path(temp) / 'world'; initialize(home)
                address = home / 'run/read.sock'; address.parent.mkdir()
                listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) if live_socket else None
                try:
                    if listener:
                        listener.bind(str(address)); listener.listen(1)
                    else:
                        address.write_text('preserve this file', encoding='utf8')
                    inode = address.stat().st_ino
                    process = subprocess.run([sys.executable, '-m', 'chiyo_bundle.world_read_service',
                        'run', '--home', str(home)], env={**os.environ, 'PYTHONPATH': str(ROOT)},
                        capture_output=True, timeout=20)
                    self.assertNotEqual(process.returncode, 0)
                    self.assertTrue(address.exists(), 'Refusal removed a path this process did not bind')
                    self.assertEqual(address.stat().st_ino, inode)
                    if not listener: self.assertEqual(address.read_text(), 'preserve this file')
                finally:
                    if listener: listener.close()

    def test_real_engine_reads_refuses_action_and_recovers_after_restart(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp) / 'world'; initialize(home)
            address = home / 'run/read.sock'
            env = {**os.environ, 'PYTHONPATH': str(ROOT)}

            def call(command):
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                    client.settimeout(3); client.connect(str(address))
                    client.sendall((json.dumps({'command': command}) + '\n').encode())
                    with client.makefile('rb') as reader: return json.loads(reader.readline(1000000))

            for _ in range(2):
                process = subprocess.Popen([sys.executable, '-m', 'chiyo_bundle.world_read_service',
                    'run', '--home', str(home)], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                try:
                    deadline = time.monotonic() + 20
                    while not address.exists() and process.poll() is None and time.monotonic() < deadline:
                        time.sleep(0.05)
                    self.assertTrue(address.exists(), 'World service did not start')
                    self.assertTrue(call('get_status')['result']['ready'])
                    snapshot = call('get_snapshot')['result']
                    self.assertEqual(snapshot['location']['area_id'], 'bedroom')
                    self.assertEqual(snapshot['pose'],'standing')
                    self.assertFalse(call('submit_action')['ok'])
                    self.assertEqual(snapshot, call('get_snapshot')['result'])
                    self.assertTrue(call('get_body_signals')['ok'])
                finally:
                    process.terminate()
                    out, err = process.communicate(timeout=20)
                self.assertEqual(process.returncode, 0, err.decode('utf8', 'replace')[-2000:])
                self.assertFalse(address.exists())


@unittest.skipUnless(sys.platform.startswith('linux'), 'Unix Life Supply transport')
class SupplyTransportTests(unittest.TestCase):
    def test_owner_grants_bound_notes_idempotency_and_cross_subject_refusal(self):
        import threading
        sys.path.insert(0,str(ROOT/'components/supply'))
        sys.path.insert(0,str(ROOT/'components/life/scripts'))
        from lifesupply.service.life_supply import LifeSupplyService,LifeSupplySocketServer
        from lifesupply.service.switches import FeatureSwitches
        from life_supply_candidate_source import LifeSupplyReadClient
        from chiyo_bundle.presence import _read_supply,save_note,supply_view
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'supply.sock'
            # The isolated fixture explicitly grants both test roles to this UID.
            # Deployed profiles use separate operator/service peer UIDs.
            service=LifeSupplyService(Path(temp)/'data',operator_uids=[os.getuid()],service_uids=[os.getuid()],
                allowed_subjects=['fixture-owner'],switches=FeatureSwitches({'WORKSPACE_PROVISION':'ON','ARTIFACT_EXTERNAL_ACTION':'ON'}))
            server=LifeSupplySocketServer(str(path),service)
            thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            def request(payload):
                with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as connection:
                    connection.connect(str(path));connection.sendall((json.dumps(payload)+'\n').encode())
                    with connection.makefile('rb') as reader:return json.loads(reader.readline(1000000))
            try:
                grant=request({'op':'issue_grant','grant':{'subject':'fixture-owner','capability':'PROVISION_PERSONAL_WORKSPACE',
                    'scope':'workspace:personal','target':'workspace:fixture-owner','operation_id':'fixture:workspace-grant'}})
                self.assertEqual(grant['status'],'ALLOW',grant)
                workspace=request({'op':'provision_workspace','grant_id':grant['grant_id'],'operation_key':'fixture:workspace',
                    'workspace':{'subject_id':'fixture-owner'}})
                self.assertEqual(workspace['status'],'OK',workspace)
                grant=request({'op':'issue_grant','grant':{'subject':'fixture-owner','capability':'COMMIT_MANAGED_ARTIFACT',
                    'scope':'artifact:personal','target':'workspace:'+workspace['workspace']['workspace_id'],
                    'operation_id':'fixture:artifact-grant','max_uses':3,'use_mode':'REUSABLE'}})
                self.assertEqual(grant['status'],'ALLOW',grant)
                svc=NS(supply_subject='fixture-owner',native=NS(_life_supply_client=LifeSupplyReadClient(str(path))))
                first=save_note(svc,grant['grant_id'],'a title','a body')
                self.assertEqual(first['status'],'OK',first)
                second=save_note(svc,grant['grant_id'],'a title','a body')
                self.assertEqual(second['artifact_id'],first['artifact_id'])
                read=_read_supply(svc.native._life_supply_client,svc.supply_subject,'read_artifact',first['artifact_id'])
                self.assertEqual(read['content'],'a body')
                self.assertNotEqual(save_note(svc,'forged-grant','another title','body')['status'],'OK')
                foreign=_read_supply(svc.native._life_supply_client,'foreign-owner','read_artifact',first['artifact_id'])
                self.assertNotEqual(foreign['status'],'OK')
                view=supply_view(svc)
                self.assertEqual(view['state'],'READY');self.assertEqual(len(view['artifacts']),1)
            finally:
                server.shutdown();server.server_close();thread.join(5);service.close()


if __name__ == '__main__': unittest.main()
