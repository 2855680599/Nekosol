"""Missing configured forgetting controls refuse reinitialization after restart."""
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT),str(ROOT/'vendor/hermes')]


class InstanceLifecycleTests(unittest.TestCase):
    def test_existing_memory_never_silently_recreates_a_missing_control_ledger(self):
        from chiyo_bundle.instance import Instance
        with tempfile.TemporaryDirectory() as temp:
            state=Path(temp)/'state'; memory=state/'memory'; memory.mkdir(parents=True)
            evidence=memory/'evidence.sqlite'
            # A pre-existing canonical store distinguishes a restart from first use.
            connection=sqlite3.connect(evidence)
            connection.execute('CREATE TABLE retained_evidence(content TEXT)')
            connection.execute('INSERT INTO retained_evidence VALUES(?)',('retain original audit',))
            connection.commit();connection.close()
            before=evidence.read_bytes()
            with patch.dict(os.environ,{},clear=False), self.assertRaisesRegex(RuntimeError,'control ledger'):
                Instance(state,owner='fixture-owner',memory=True)
            self.assertFalse((state/'memory-controls.sqlite').exists())
            self.assertEqual(evidence.read_bytes(),before)

    def test_failed_assembly_closes_its_request_database(self):
        from chiyo_bundle.instance import Instance
        connections=[]; real_connect=sqlite3.connect
        def track(path,*args,**kwargs):
            connection=real_connect(path,*args,**kwargs)
            if str(path).endswith('requests.sqlite'): connections.append(connection)
            return connection
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ,{},clear=False), \
             patch('chiyo_bundle.instance.sqlite3.connect',side_effect=track):
            # Fail the real assembly after opening its durable request journal.
            with patch('chiyo_bundle.instance.install_paths',side_effect=RuntimeError('assembly failure')):
                with self.assertRaisesRegex(RuntimeError,'assembly failure'):
                    Instance(Path(temp)/'state',owner='fixture-owner')
            self.assertEqual(len(connections),1)
            with self.assertRaises(sqlite3.ProgrammingError): connections[0].execute('SELECT 1')

    def test_surviving_request_journal_is_a_restart_even_when_all_memory_dbs_are_lost(self):
        from chiyo_bundle.instance import Instance
        with tempfile.TemporaryDirectory() as temp:
            state=Path(temp)/'state'; state.mkdir()
            connection=sqlite3.connect(state/'requests.sqlite')
            connection.execute('CREATE TABLE requests(id TEXT PRIMARY KEY,digest TEXT,status TEXT,response TEXT,source TEXT)')
            connection.commit();connection.close()
            with patch.dict(os.environ,{},clear=False), self.assertRaisesRegex(RuntimeError,'control ledger'):
                Instance(state,owner='fixture-owner',memory=True)
            self.assertFalse((state/'memory-controls.sqlite').exists())
            self.assertFalse((state/'memory/evidence.sqlite').exists())


if __name__=='__main__':unittest.main()
