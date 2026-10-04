"""Regressions that exercise the real idle consumer contract."""
import sys,threading,unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'components/life/plugin'))
from lpc0b_audit_writer import AuditWriter
class IdleWriterTests(unittest.TestCase):
    def test_idle_loop_yields_and_stops_without_next_pump(self):
        stop=threading.Event();waits=[];pumps=[]
        class Stop:
            def is_set(self):return stop.is_set()
            def wait(self,timeout):waits.append(timeout);stop.set();return True
        class Probe(AuditWriter):
            def _pump_once(self):
                pumps.append(1)
                if len(pumps)>1:stop.set()
            def _write_metrics(self):pass
            def _publish_health(self):pass
        writer=Probe(queue=None,candidate_journal=None,poll_interval_s=.05)
        writer._stop=Stop();writer._run()
        self.assertEqual(pumps,[1],'empty consumer must yield before pumping again')
        self.assertEqual(waits,[.05])
    def test_shutdown_has_one_consumer_and_preserves_fifo(self):
        from lpc0b_audit_queue import BoundedAuditQueue
        queue=BoundedAuditQueue();entered=threading.Event();release=threading.Event();seen=[]
        class Journal:
            closed=False
            def close(self):self.closed=True
        journal=Journal()
        class Probe(AuditWriter):
            def _handle(self,record):
                if record==0:entered.set();release.wait(2)
                seen.append((record,threading.get_ident()))
            def _write_metrics(self,**kwargs):pass
            def _publish_health(self,**kwargs):pass
            def health(self):return {}
        class Record(int):
            def size_bytes(self):return 4
        for i in range(3):self.assertEqual(queue.put(Record(i)),"accepted")
        writer=Probe(queue=queue,candidate_journal=journal,flush_batch=1)
        writer._started=True;writer._thread=threading.Thread(target=writer._run,daemon=True);writer._thread.start()
        self.assertTrue(entered.wait(2))
        result=writer.stop(drain=True,timeout_s=0)
        self.assertTrue(result.get('shutdown_incomplete'));self.assertFalse(journal.closed)
        release.set();writer._thread.join(2);writer.stop(drain=True,timeout_s=2)
        self.assertEqual([r for r,t in seen],[0,1,2]);self.assertEqual(len({t for r,t in seen}),1);self.assertTrue(journal.closed)

if __name__=='__main__':unittest.main()
