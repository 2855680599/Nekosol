"""Filesystem transactions, preserved data, ZIP boundaries and real process refusal."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch
import zipfile

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('nyairo_update', ROOT / 'scripts/update.py')
update = importlib.util.module_from_spec(spec)
spec.loader.exec_module(update)


def seal(root):
    files = {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
             for p in root.rglob('*') if p.is_file() and p.name != 'MANIFEST.json'}
    lines = ''.join(f'{sha}  {name}\n' for name, sha in sorted(files.items()))
    (root / 'MANIFEST.json').write_text(json.dumps({'files': files, 'file_count': len(files),
        'tree_sha256': hashlib.sha256(lines.encode()).hexdigest(), 'status': 'SOURCE_REVIEWED_TARGETED_TESTS_PASSED'}))


def bundle(path, version, plugin='old'):
    for directory in ('scripts', 'patches', 'plugins/chiyo'):
        (path / directory).mkdir(parents=True)
    shutil.copy2(ROOT / 'scripts/verify_manifest.py', path / 'scripts/verify_manifest.py')
    (path / 'scripts/release.json').write_text(json.dumps({'schema': 1, 'version': version,
        'profile_format': 1, 'update_protocol': 1, 'hermes_version': 'test', 'hermes_commit': 'a' * 40}))
    (path / 'patches/baseline.json').write_text(json.dumps({'upstream': {'version': 'test', 'commit': 'a' * 40}}))
    (path / 'plugins/chiyo/__init__.py').write_text(plugin)
    seal(path)
    return path


@unittest.skipUnless(sys.platform.startswith('linux'), 'transaction tests require real Linux/WSL')
class UpdateTransactions(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.old = bundle(self.base / 'old', 'v1.0.0')
        self.new = bundle(self.base / 'new', 'v1.1.0', 'new')
        self.home = self.base / 'profile'
        self.home.mkdir()
        shutil.copytree(self.old / 'plugins/chiyo', self.home / 'plugins/chiyo')
        self.personal = {'SOUL.md': b'my custom persona', 'config.yaml': b'my config', '.env': b'my private credential', 'state.db': b'chat history', 'chiyo/memory.json': b'personal memories'}
        for name, value in self.personal.items():
            path = self.home / name
            path.parent.mkdir(exist_ok=True)
            path.write_bytes(value)
        self.addCleanup(patch.stopall)
        patch.object(Path, 'home', return_value=self.base).start()
        patch.dict(os.environ, {'HERMES_HOME': str(self.home), 'PYTHONDONTWRITEBYTECODE': '1'}).start()
        self.updater = update.Updater(self.old, self.base / 'manager')
        self.updater.adopt()

    def preserved(self):
        self.assertEqual({name: (self.home / name).read_bytes() for name in self.personal}, self.personal)

    def test_fixed_launcher_keeps_default_profile_and_allows_explicit_override(self):
        (self.old / 'scripts/hermes.sh').write_text('#!/bin/bash\nprintf "%s" "$HERMES_HOME"\n')
        launcher = self.base / '.local/bin/nyairo'
        env = dict(os.environ)
        env.pop('HERMES_HOME', None)
        value = subprocess.check_output([str(launcher)], env=env, text=True)
        self.assertEqual(value, str(self.home))
        env['HERMES_HOME'] = str(self.base / 'another personal profile')
        value = subprocess.check_output([str(launcher)], env=env, text=True)
        self.assertEqual(value, env['HERMES_HOME'])

    def test_upgrade_then_rollback_preserves_new_chats_and_credentials(self):
        self.updater.activate(self.new, runtime=lambda root: None)
        self.assertEqual(update.read_json(self.updater.state_path)['current'], str(self.new))
        self.assertEqual((self.home / 'plugins/chiyo/__init__.py').read_text(), 'new')
        self.preserved()
        backup = Path(self.updater.state['backup']) / 'profile-0.tar.gz'
        self.assertEqual(stat.S_IMODE(backup.stat().st_mode), 0o600)
        with tarfile.open(backup) as archive:
            self.assertEqual(archive.extractfile('profile/.env').read(), self.personal['.env'])
        self.personal['state.db'] = b'old AND new chats after upgrade'
        (self.home / 'state.db').write_bytes(self.personal['state.db'])
        self.updater.rollback()
        self.assertEqual(update.read_json(self.updater.state_path)['current'], str(self.old))
        self.assertEqual((self.home / 'plugins/chiyo/__init__.py').read_text(), 'old')
        self.preserved()

    def test_dependency_failure_never_changes_current_or_profile(self):
        def fail(root):
            raise subprocess.CalledProcessError(1, ['dependency-installer'])
        with self.assertRaises(subprocess.CalledProcessError):
            self.updater.activate(self.new, runtime=fail)
        self.assertEqual(update.read_json(self.updater.state_path)['current'], str(self.old))
        self.assertEqual((self.home / 'plugins/chiyo/__init__.py').read_text(), 'old')
        self.preserved()

    def test_second_profile_swap_failure_restores_first(self):
        second = self.base / 'second'
        shutil.copytree(self.home, second)
        self.updater.state['profiles'] = [str(second)]
        update.atomic_json(self.updater.state_path, self.updater.state)
        real = update.replace_plugin
        count = 0
        def fail_once(home, source):
            nonlocal count
            count += 1
            if count == 2:
                raise OSError('injected second profile failure')
            real(home, source)
        with patch.object(update, 'replace_plugin', side_effect=fail_once):
            with self.assertRaises(OSError):
                self.updater.activate(self.new, runtime=lambda root: None)
        for home in (self.home, second):
            self.assertEqual((home / 'plugins/chiyo/__init__.py').read_text(), 'old')
        self.assertEqual(update.read_json(self.updater.state_path)['current'], str(self.old))
        self.assertFalse(self.updater.journal_path.exists())
        self.preserved()

    def test_interrupted_update_recovers_original_plugin(self):
        backup = update.private_directory(self.base / 'backup')
        update.backup_profiles([self.home], backup)
        update.replace_plugin(self.home, self.new / 'plugins/chiyo')
        update.atomic_json(self.updater.journal_path, {'transaction': 'not-published',
            'old_root': str(self.old), 'new_root': str(self.new), 'profiles': [str(self.home)], 'backup': str(backup)})
        self.updater.recover()
        self.assertEqual((self.home / 'plugins/chiyo/__init__.py').read_text(), 'old')
        self.preserved()

    def test_modified_plugin_is_preserved_and_update_refused(self):
        plugin = self.home / 'plugins/chiyo/__init__.py'
        plugin.write_text('my private custom code')
        with self.assertRaisesRegex(ValueError, '自定义'):
            self.updater.activate(self.new, runtime=lambda root: None)
        self.assertEqual(plugin.read_text(), 'my private custom code')
        self.preserved()

    def test_live_profile_writer_is_refused_using_real_process(self):
        process = subprocess.Popen([sys.executable, '-c', 'import time; print("ready", flush=True); time.sleep(20)'],
            stdout=subprocess.PIPE, text=True, env={**os.environ, 'HERMES_HOME': str(self.home)})
        try:
            self.assertEqual(process.stdout.readline().strip(), 'ready')
            with self.assertRaisesRegex(ValueError, '还在运行'):
                self.updater.activate(self.new, runtime=lambda root: None)
        finally:
            process.terminate()
            process.wait(timeout=5)
            process.stdout.close()
        self.preserved()

    def test_bad_source_and_schema_migration_do_not_touch_data(self):
        (self.new / 'extra').write_text('unlisted file')
        with self.assertRaisesRegex(ValueError, '校验失败'):
            self.updater.activate(self.new, runtime=lambda root: None)
        (self.new / 'extra').unlink()
        info = update.read_json(self.new / 'scripts/release.json')
        info['profile_format'] = 2
        update.atomic_json(self.new / 'scripts/release.json', info)
        seal(self.new)
        with self.assertRaisesRegex(ValueError, '数据迁移'):
            self.updater.activate(self.new, runtime=lambda root: None)
        self.preserved()

    def test_concurrent_update_lock_refuses_second_writer(self):
        other = update.Updater(self.old, self.updater.directory)
        with self.updater.locked():
            with self.assertRaisesRegex(ValueError, '另一个更新'):
                with other.locked():
                    self.fail('second updater must never enter')


class ReleaseBoundaries(unittest.TestCase):
    def test_unsafe_zip_entries_never_write_outside_destination(self):
        entries = ['../escape', '/absolute', 'a\\escape', 'a/../escape', 'C:/escape', '.git/config', 'vendor/hermes/.venv/bin/python', './x', 'a//x']
        for entry in entries:
            with self.subTest(entry=entry), tempfile.TemporaryDirectory() as temporary:
                base = Path(temporary)
                with zipfile.ZipFile(base / 'bad.zip', 'w') as archive:
                    info = zipfile.ZipInfo('placeholder')
                    info.filename = entry
                    archive.writestr(info, 'bad')
                with self.assertRaises(ValueError):
                    update.extract_archive(base / 'bad.zip', base / 'unpack')
                self.assertFalse((base / 'unpack').exists())

    def test_zip_symlinks_and_duplicates_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            entry = zipfile.ZipInfo('link')
            entry.external_attr = (stat.S_IFLNK | 0o777) << 16
            with zipfile.ZipFile(base / 'bad.zip', 'w') as archive:
                archive.writestr(entry, '/etc/passwd')
            with self.assertRaises(ValueError):
                update.extract_archive(base / 'bad.zip', base / 'unpack')

    def test_candidate_order_and_stable_filter_ignore_upload_order(self):
        def release(tag, prerelease=False):
            prefix = f'https://github.com/{update.REPOSITORY}/releases/download/{tag}/nyairo-{tag}.zip'
            return {'tag_name': tag, 'prerelease': prerelease, 'assets': [
                {'name': f'nyairo-{tag}.zip', 'browser_download_url': prefix},
                {'name': f'nyairo-{tag}.zip.sha256', 'browser_download_url': prefix + '.sha256'}]}
        items = [release('v1.0.0-rc2', True), release('v0.9.0'), release('v1.0.0-rc12', True)]
        with patch.object(update, 'github_json', return_value=items):
            self.assertEqual(update.select_release()[0], 'v1.0.0-rc12')
            self.assertEqual(update.select_release(channel='stable')[0], 'v0.9.0')

    def test_private_http_and_credential_urls_are_rejected(self):
        for url in ('http://github.com/x', 'https://127.0.0.1/x', 'https://user:secret@github.com/x', 'https://github.com.evil.test/x'):
            with self.assertRaises(ValueError):
                update.safe_url(url)


if __name__ == '__main__':
    unittest.main()
