"""Linux/WSL filesystem boundaries exercised with real files and sockets."""
from contextlib import ExitStack
import importlib.util
import os
from pathlib import Path
import socket
import stat
import subprocess
import sys
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'patches'),str(ROOT/'components/life/plugin'),str(ROOT/'components/life/scripts'),
 str(ROOT/'components/world'),str(ROOT/'components/world/service'),str(ROOT/'components/world/scripts')]


class FilesystemSecurityTests(unittest.TestCase):
    def test_socket_startup_refuses_regular_symlink_and_live_paths(self):
        from world_body_service import WorldBodyService,StartupRefused
        service=object.__new__(WorldBodyService)
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);path=root/'socket';path.write_text('keep me')
            with self.assertRaises(StartupRefused):service._bind_socket(path)
            self.assertEqual(path.read_text(),'keep me')
            link=root/'link';link.symlink_to(path)
            with self.assertRaises(StartupRefused):service._bind_socket(link)
            self.assertTrue(link.is_symlink())
            path.unlink()
            with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as live:
                live.bind(str(path));live.listen(1);before=path.lstat().st_ino
                with self.assertRaises(StartupRefused):service._bind_socket(path)
                self.assertEqual(path.lstat().st_ino,before)
            bound=service._bind_socket(path)
            try:self.assertEqual(stat.S_IMODE(path.stat().st_mode),0o600)
            finally:bound.close()

    def test_audit_permissions_under_permissive_umask(self):
        from lpc0b_audit_journal import SegmentJournal
        from lpc0b_audit_writer import AuditWriter
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)/'audit';old=os.umask(0)
            try:
                journal=SegmentJournal(root);journal.recover();journal.commit_new('metrics',{'count':1})
                path=Path(temp)/'metrics/health.json';AuditWriter._atomic_write(path,{'status':'ok'})
            finally:os.umask(old)
            self.assertEqual(stat.S_IMODE(root.stat().st_mode),0o700)
            for _,segment in journal.segments():self.assertEqual(stat.S_IMODE(segment.stat().st_mode),0o600)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode),0o600)
            self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode),0o700)

    def test_patch_publish_cannot_follow_file_or_directory_symlink(self):
        from secure_target import Target
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)/'target';root.mkdir();outside=Path(temp)/'outside';outside.mkdir()
            secret=outside/'value';secret.write_bytes(b'KEEP')
            (root/'leaf').write_bytes(b'old')
            with ExitStack() as stack:
                target=Target(root,stack)
                self.assertEqual(target.read('leaf'),b'old')
                # Swap after validation, before publication: never overwrite link target.
                (root/'leaf').unlink();(root/'leaf').symlink_to(secret)
                target.publish('leaf',b'new')
                self.assertEqual(secret.read_bytes(),b'KEEP');self.assertEqual((root/'leaf').read_bytes(),b'new')
                (root/'directory').symlink_to(outside,target_is_directory=True)
                with self.assertRaises(OSError):target.publish('directory/value',b'attack')
                self.assertEqual(secret.read_bytes(),b'KEEP')
            with ExitStack() as stack:
                with self.assertRaises(OSError):Target(root/'directory',stack)

    def test_patch_roundtrip_keeps_exact_bytes_and_drift_refuses_mutation(self):
        import json
        from patch_manager import manage
        for bundled in (ROOT/'vendor/hermes',ROOT/'vendor/../vendor/hermes'):
            with self.assertRaisesRegex(ValueError,'Bundled Hermes'):
                manage('rollback',bundled)
        baseline=json.loads((ROOT/'patches/baseline.json').read_text())
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            for record in baseline['tracked_files']+baseline['new_modules']:
                name=record['path'];path=root/name;path.parent.mkdir(parents=True,exist_ok=True)
                path.write_bytes((ROOT/'vendor/hermes'/name).read_bytes())
            before={p:p.read_bytes() for p in root.rglob('*') if p.is_file()}
            def action(name):
                result=subprocess.run([sys.executable,str(ROOT/'patches/patch_manager.py'),name,str(root)],capture_output=True,text=True)
                self.assertEqual(result.returncode,0,result.stderr)
            for name in ('check','apply','rollback','rollback','apply','check'):action(name)
            self.assertEqual(before,{p:p.read_bytes() for p in root.rglob('*') if p.is_file()})
            changed=next(iter(before));changed.write_bytes(b'drift')
            snapshot={p:p.read_bytes() for p in root.rglob('*') if p.is_file()}
            result=subprocess.run([sys.executable,str(ROOT/'patches/patch_manager.py'),'rollback',str(root)],capture_output=True,text=True)
            self.assertNotEqual(result.returncode,0)
            self.assertEqual(snapshot,{p:p.read_bytes() for p in root.rglob('*') if p.is_file()})


if __name__=='__main__':unittest.main()
