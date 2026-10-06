"""A bundled dependency must never mutate itself through the source updater."""
import json
import pytest
from hermes_cli.update_contract import evaluate_update_admission, record_refusal_receipt


def test_distribution_owns_update_and_receipt(tmp_path, monkeypatch):
    root = tmp_path / 'bundle/vendor/hermes'
    root.mkdir(parents=True)
    script = tmp_path / 'bundle/scripts/update.sh'
    script.parent.mkdir()
    script.write_text('#!/bin/bash\n')
    (root / '.distribution.json').write_text(json.dumps({
        'schema': 1, 'manager': 'test-bundle', 'update_script': '../../scripts/update.sh'}))
    refusal = evaluate_update_admission(root)
    assert refusal and refusal.code == 'distribution'
    assert str(script) in refusal.update_command
    monkeypatch.setenv('HERMES_HOME', str(tmp_path / 'profile'))
    import hermes_cli.update_receipt as receipts
    monkeypatch.setattr(receipts, '_receipt_dir', lambda: tmp_path / 'receipts')
    record_refusal_receipt(refusal)
    reports = [path for path in (tmp_path / 'receipts').glob('*.json') if path.name != 'latest.json']
    report = json.loads(reports[0].read_text())
    assert report['outcome'] == 'refused'
    assert report['stop_reason'] == refusal.code


@pytest.mark.parametrize('payload', ['invalid JSON', '{"schema": true}', '{"schema": 1, "manager": "x", "update_script": "/tmp/updater"}'])
def test_invalid_distribution_never_falls_back_to_mutable_git(tmp_path, payload):
    (tmp_path / '.git').mkdir()
    (tmp_path / '.distribution.json').write_text(payload)
    refusal = evaluate_update_admission(tmp_path)
    assert refusal and refusal.code == 'distribution-invalid'
