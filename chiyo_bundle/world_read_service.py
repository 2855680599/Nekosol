"""An independent World/Body owner with a same-user, read-only Unix port.

Uses the real World engine and its finite GatewaySurface projection. No admin
or mutation surface is exposed and the original root gateway is unchanged.
"""
import argparse
import json
import os
from pathlib import Path
import signal
import socket
import stat
import struct
import sys
import time

ROOT = Path(__file__).resolve().parents[1]


def authorized_peer(connection, uid):
    try:
        if not hasattr(socket, 'SO_PEERCRED'):
            return False
        _, peer_uid, _ = struct.unpack('3i', connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
        return peer_uid == uid
    except OSError:
        return False


def dispatch_read(surface, request):
    from world_body_gateway_api import GATEWAY_READ_COMMANDS
    if not isinstance(request, dict) or request.get('command') not in GATEWAY_READ_COMMANDS:
        return {'ok': False, 'error': 'read_only_port'}
    return surface.handle(request['command'], request.get('argument'))


def initialize(home):
    """Explicit, create-once generic bedroom world; never imports another instance."""
    home = Path(home).resolve()
    if ROOT == home or ROOT in home.parents:
        raise ValueError('World state must be outside the source tree')
    world = home / 'data/world'
    if home.exists():
        raise ValueError('World home already exists; initialization never overwrites it')
    world.mkdir(parents=True, mode=0o700)
    os.chmod(home, 0o700)
    (world / 'world_state.json').write_text(json.dumps({
        'schema_version': 'world.foundation.v0', 'world_id': 'shiomi_city',
        'timezone': 'Asia/Shanghai', 'revision': 1, 'facts': [],
        'location': {'place_id': 'home', 'area_id': 'bedroom'},
        'scene': {'revision': 1, 'scene_id': 'home.bedroom'}}), encoding='utf8')
    (world / 'world_timeline.json').write_text(json.dumps({
        'schema_version': 'world.timeline.v1', 'world_id': 'shiomi_city',
        'revision': 1, 'processes': []}), encoding='utf8')
    (home / 'data/life_execution.jsonl').write_text('', encoding='utf8')


def serve(home):
    sys.path[:0] = [str(ROOT / 'components/world/service'), str(ROOT / 'components/world/scripts')]
    from world_body_config import load_config
    from world_body_service import WorldBodyService
    config = load_config({}, runtime_root=ROOT / 'components/world', world_home=Path(home).resolve(),
                         runtime_mode='canonical', authority='production')
    service = WorldBodyService(config, tick_seconds=1)
    path = config.run_dir / 'read.sock'
    stop = [False]
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.__setitem__(0, True))
    server = None
    bound_identity = None
    started = False
    try:
        service.startup()
        started = True
        # Seed only absent embodiment through its canonical writer. A damaged
        # existing store must refuse startup rather than become a fresh body.
        import world_body_substrate as substrate
        store = substrate.SubstrateStore(substrate.substrate_path(config.world_home))
        if store.load() is None:
            store.save(substrate.initial_substrate(world_id=config.expected_world_id))
        if path.exists():
            info = path.lstat()
            if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
                raise RuntimeError('Refusing a foreign/non-socket path')
            probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                probe.settimeout(1)
                probe.connect(str(path))
            except (ConnectionRefusedError, FileNotFoundError):
                path.unlink()
            else:
                raise RuntimeError('World read port is already active')
            finally:
                probe.close()
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(path))
        info = path.lstat()
        bound_identity = (info.st_dev, info.st_ino)
        os.chmod(path, 0o600)
        server.listen(8); server.settimeout(1)
        tick_at = time.monotonic()
        while not stop[0]:
            try:
                connection, _ = server.accept()
            except socket.timeout:
                connection = None
            if connection is not None:
                with connection:
                    connection.settimeout(1)
                    payload = {'ok': False, 'error': 'unauthorized_peer'}
                    if authorized_peer(connection, os.getuid()):
                        try:
                            with connection.makefile('rb') as reader:
                                line = reader.readline(65537)
                            if len(line) > 65536:
                                raise ValueError('request too large')
                            payload = dispatch_read(service.gateway_surface, json.loads(line))
                        except Exception:
                            payload = {'ok': False, 'error': 'invalid_request'}
                    try:
                        connection.sendall((json.dumps(payload, ensure_ascii=False) + '\n').encode())
                    except OSError:
                        pass
            if time.monotonic() - tick_at >= 1:
                service.tick(); tick_at = time.monotonic()
    finally:
        if server is not None:
            server.close()
        try:
            info = path.lstat()
        except FileNotFoundError:
            info = None
        if info is not None and (info.st_dev, info.st_ino) == bound_identity:
            path.unlink()
        try:
            if started:
                service.shutdown()
        finally:
            # Partial startup or a failed state flush must still drop the
            # single-writer lease; another instance's lease stays untouched.
            if service.lease is not None and service.lease.held:
                service.lease.release()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('command', choices=['init', 'run'])
    parser.add_argument('--home', required=True)
    args = parser.parse_args()
    initialize(args.home) if args.command == 'init' else serve(args.home)


if __name__ == '__main__':
    main()
