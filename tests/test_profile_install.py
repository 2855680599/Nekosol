"""Documented profile creation preserves user settings and refuses before writing."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import yaml

ROOT = Path(__file__).resolve().parents[1]


class ProfileInstallTests(unittest.TestCase):
    def setup_profile(self, home):
        return subprocess.run([sys.executable, '-B', str(ROOT/'scripts/setup_profile.py'),
            '--home', str(home), '--owner', 'fixture-owner', '--memory', '--life'],
            capture_output=True, text=True, timeout=20)

    def test_normal_profile_install_preserves_model_and_persona_and_refuses_overwrite(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            config = home/'config.yaml'; config.write_text('model: {default: fixture-model}\n')
            soul = home/'SOUL.md'; soul.write_text('My existing persona\n')
            result = self.setup_profile(home)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(soul.read_text(), 'My existing persona\n')
            host = yaml.safe_load(config.read_text())
            self.assertEqual(host['model']['default'], 'fixture-model')
            self.assertEqual(host['context']['engine'], 'chiyo')
            self.assertFalse(host['memory']['memory_enabled'])
            personal = home/'chiyo/config.json'
            self.assertEqual(json.loads(personal.read_text())['owner'], 'fixture-owner')
            self.assertTrue((home/'plugins/chiyo/__init__.py').is_file())
            before = (personal.read_bytes(), config.read_bytes(), soul.read_bytes())
            self.assertNotEqual(self.setup_profile(home).returncode, 0)
            self.assertEqual(before, (personal.read_bytes(), config.read_bytes(), soul.read_bytes()))

    def test_invalid_host_config_or_existing_plugin_leaves_no_partial_personal_config(self):
        for cause in ('invalid-config', 'existing-plugin'):
            with self.subTest(cause=cause), tempfile.TemporaryDirectory() as temp:
                home = Path(temp)
                config = home/'config.yaml'; config.write_text('42\n' if cause=='invalid-config' else '{}\n')
                if cause=='existing-plugin':
                    plugin = home/'plugins/chiyo'; plugin.mkdir(parents=True)
                    (plugin/'__init__.py').write_text('existing plugin\n')
                before = config.read_bytes()
                self.assertNotEqual(self.setup_profile(home).returncode, 0)
                self.assertEqual(before, config.read_bytes())
                self.assertFalse((home/'chiyo/config.json').exists())


if __name__ == '__main__': unittest.main()
