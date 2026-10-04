"""Installer boundary checks; network downloads are replaced only in the failure case.

Run with: python3 -m unittest discover -s tests -p test_bootstrap_installer.py
Real ordinary-account installs and installed-CLI checks are recorded in TESTING.md.
"""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'scripts/bootstrap.sh'


@unittest.skipUnless(os.name == 'posix', 'Run in Linux or WSL')
class InstallerBoundaries(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.env = dict(os.environ, HOME=str(self.home), PATH='/usr/bin:/bin')

    def run_installer(self, *args, headless=True):
        flags = ['--no-setup', '--no-launch'] if headless else []
        return subprocess.run(['bash', str(SCRIPT), *flags, *args],
                              env=self.env, cwd='/', stdin=subprocess.DEVNULL,
                              capture_output=True, text=True, start_new_session=True)

    def test_help_has_no_install_side_effect(self):
        result = self.run_installer('--help')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(list(self.home.iterdir()), [])

    def test_interactive_install_refuses_missing_terminal_before_writing(self):
        result = self.run_installer(headless=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('模型设置需要终端', result.stderr)
        self.assertEqual(list(self.home.iterdir()), [])

    def test_existing_unrelated_command_is_preserved(self):
        launcher = self.home / '.local/bin/nyairo'
        launcher.parent.mkdir(parents=True)
        launcher.write_text('unrelated command\n')
        result = self.run_installer()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('不会覆盖', result.stderr)
        self.assertEqual(launcher.read_text(), 'unrelated command\n')

    def test_existing_unrelated_personal_directory_is_preserved(self):
        profile = self.home / '.nyairo'
        profile.mkdir()
        sentinel = profile / 'personal.txt'
        sentinel.write_text('keep me')
        result = self.run_installer()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('不会写入', result.stderr)
        self.assertEqual(sentinel.read_text(), 'keep me')
        self.assertEqual(list(profile.iterdir()), [sentinel])

    def test_program_and_personal_directory_cannot_overlap(self):
        result = self.run_installer('--prefix', str(self.home / 'program'),
                                    '--profile', str(self.home / 'program/data'))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('必须放在程序目录之外', result.stderr)
        self.assertFalse((self.home / 'program/data').exists())

    def test_tool_failure_releases_lock_and_keeps_personal_data(self):
        binary = self.home / 'bin'
        binary.mkdir()
        uv = binary / 'uv'
        uv.write_text('#!/bin/sh\nexit 47\n')
        uv.chmod(0o700)
        self.env['PATH'] = f'{binary}:/usr/bin:/bin'
        profile = self.home / '.nyairo/chiyo'
        profile.mkdir(parents=True)
        config = profile / 'config.json'
        config.write_text('{"fixture":"preserve"}\n')
        result = self.run_installer()
        self.assertEqual(result.returncode, 47, result.stderr)
        prefix = self.home / '.local/share/nyairo'
        self.assertFalse((prefix / '.install-lock').exists())
        self.assertEqual(list(prefix.glob('.download.*')), [])
        self.assertEqual(config.read_text(), '{"fixture":"preserve"}\n')

    def test_website_and_repository_installers_are_identical(self):
        self.assertEqual(SCRIPT.read_bytes(), (ROOT / 'website/install.sh').read_bytes())


if __name__ == '__main__':
    unittest.main()
