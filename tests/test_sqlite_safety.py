"""Version gates and real file journal downgrade preserve committed rows."""
import sys,sqlite3,tempfile,unittest,importlib
from pathlib import Path
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'components/native/src'),str(ROOT/'components/supply'),str(ROOT/'components/alpha')]
MODULES=['app.sqlite_safety','lifesupply.sqlite_safety','chiyo.contact.durable.sqlite_safety']
class SqliteSafetyTests(unittest.TestCase):
    def test_every_owner_has_precise_patched_version_gate(self):
        for name in MODULES:
            m=importlib.import_module(name)
            for version in [(3,43,2),(3,44,5),(3,46,1),(3,50,6),(3,51,0),(3,51,2)]:
                with self.subTest(module=name,unsafe=version):self.assertFalse(m.wal_is_safe(version))
            for version in [(3,44,6),(3,44,7),(3,50,7),(3,50,8),(3,51,3),(3,52,0)]:
                with self.subTest(module=name,safe=version):self.assertTrue(m.wal_is_safe(version))
    def test_existing_wal_downgrade_preserves_committed_data_on_restart(self):
        for name in MODULES:
            m=importlib.import_module(name)
            with tempfile.TemporaryDirectory() as t:
                path=Path(t)/'state.sqlite';c=sqlite3.connect(path);c.execute('PRAGMA journal_mode=WAL');c.execute('CREATE TABLE rows(value TEXT)');c.execute('INSERT INTO rows VALUES(?)',('committed',));c.commit();c.close()
                c=sqlite3.connect(path)
                with patch.object(m.sqlite3,'sqlite_version_info',(3,46,1)):self.assertEqual(m.configure_journal(c),'DELETE')
                c.close();c=sqlite3.connect(path)
                self.assertEqual(c.execute('PRAGMA journal_mode').fetchone()[0],'delete');self.assertEqual(c.execute('SELECT value FROM rows').fetchone()[0],'committed');c.close()
    def test_patched_version_keeps_wal_and_unavailable_mode_fails_closed(self):
        m=importlib.import_module(MODULES[0])
        with tempfile.TemporaryDirectory() as t:
            c=sqlite3.connect(Path(t)/'state.sqlite')
            with patch.object(m.sqlite3,'sqlite_version_info',(3,51,3)):self.assertEqual(m.configure_journal(c),'WAL')
            c.close()
        c=sqlite3.connect(':memory:')
        try:
            with self.assertRaises(RuntimeError):m.configure_journal(c)
        finally:c.close()
if __name__=='__main__':unittest.main()
