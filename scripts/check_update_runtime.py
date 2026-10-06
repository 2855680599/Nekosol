"""Offline installed-runtime probe; HERMES_HOME must be a disposable directory."""
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'vendor/hermes'))
from hermes_cli.update_contract import evaluate_update_admission
from hermes_cli.plugins import discover_plugins, get_plugin_context_engine
from hermes_constants import get_hermes_home

home = get_hermes_home()
if (home / 'config.yaml').exists() or (home / 'chiyo').exists():
    raise SystemExit('Probe requires a disposable HERMES_HOME')
subprocess.run([sys.executable, str(ROOT / 'scripts/setup_profile.py'), '--home', str(home),
                '--owner', 'upgrade_probe'], check=True)
discover_plugins()
from chiyo_bundle.hermes_plugin import ChiyoContextEngine, allowed
assert allowed({}, 'cli', None) is False
assert isinstance(get_plugin_context_engine(), ChiyoContextEngine)
refusal = evaluate_update_admission(ROOT / 'vendor/hermes')
assert refusal and refusal.code == 'distribution'
print(json.dumps({'plugin_import': True, 'private_owner_default_denied': True, 'managed_update_guard': True}))
