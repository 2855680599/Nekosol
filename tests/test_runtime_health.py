"""Health reflects owner availability, and concurrent receipts stay atomic."""
import json
from pathlib import Path
import sys
import tempfile
import threading
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT/'vendor/hermes')]


class RuntimeHealthTests(unittest.TestCase):
    def test_corrupt_memory_and_failed_world_snapshot_are_unavailable(self):
        from chiyo_bundle.presence import status_snapshot
        svc = NS(memory=True, native=NS(
            memory_controls=NS(projection=lambda: NS(healthy=False)),
            m37_resolver=NS(state='READY'),
            world_body=NS(get_status=lambda: NS(ok=True, data={'ready': True}),
                          get_snapshot=lambda: NS(ok=False, data=None))))
        result = status_snapshot(svc)
        self.assertEqual(result['memory'], 'UNAVAILABLE')
        self.assertEqual(result['world_body']['state'], 'UNAVAILABLE')

    def test_two_status_writers_never_share_a_temporary_path(self):
        from chiyo_bundle.hermes_plugin import write_status_evidence
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp); (home/'chiyo').mkdir()
            barrier = threading.Barrier(2)
            original_replace = Path.replace
            errors = []
            def synchronized_replace(path, target):
                barrier.wait(timeout=5)
                return original_replace(path, target)
            def write():
                try: write_status_evidence()
                except Exception as exc: errors.append(exc)
            with patch('chiyo_bundle.hermes_plugin.configuration', return_value=(home, {})), \
                 patch('chiyo_bundle.hermes_plugin.services', return_value=object()), \
                 patch('chiyo_bundle.presence.status_snapshot', return_value={'memory': 'READY'}), \
                 patch.object(Path, 'replace', synchronized_replace):
                threads = [threading.Thread(target=write) for _ in range(2)]
                for thread in threads: thread.start()
                for thread in threads: thread.join(10)
            self.assertFalse(any(thread.is_alive() for thread in threads))
            self.assertEqual(errors, [])
            self.assertEqual(json.loads((home/'chiyo/runtime-status.json').read_text())['modules']['memory'], 'READY')
            self.assertEqual(list((home/'chiyo').glob('*.tmp')), [])


if __name__ == '__main__': unittest.main()
