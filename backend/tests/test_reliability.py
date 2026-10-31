import sys, time, unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app.firmware import encode_parameter, decode_parameter, parameter_encoding
from app.parameters import ParameterStore
from app.missions import MissionManager
from app.transactions import TransactionManager

class ReliabilityTests(unittest.TestCase):
    def test_bytewise_and_cast_integer_semantics(self):
        for kind, value in [(1,255),(2,-100),(3,65535),(4,-30000),(5,123456789),(6,-123456789)]:
            self.assertEqual(decode_parameter(encode_parameter(value,kind,'bytewise'),kind,'bytewise'),value)
        with self.assertRaises(ValueError): encode_parameter(2**25+1,5,'cast')
        self.assertEqual(encode_parameter(2**25,5,'cast'),2**25)
        with self.assertRaises(ValueError): parameter_encoding('PX4',None)

    def test_download_duplicate_out_of_order_and_missing_recovery(self):
        store=ParameterStore(); store.begin_session('a'); store.request_all()
        store.ingest('C',3,6,2,3); store.ingest('A',1,6,0,3); store.ingest('A',1,6,0,3)
        self.assertEqual(store.snapshot()['progress']['received'],2)
        self.assertEqual(store.recovery_indices(time.monotonic()+3),[1])
        store.ingest('B',2,6,1,3)
        self.assertTrue(store.snapshot()['progress']['complete'])

    def test_cancelled_write_does_not_confirm_from_late_echo(self):
        store=ParameterStore(); store.begin_session('a'); store.pending_set('A',2)
        store.cancel(); store.ingest('A',2,6,0,1)
        self.assertEqual(store.snapshot()['pending']['A']['state'],'UNKNOWN')

    def test_immediate_ack_and_stale_session(self):
        tx=TransactionManager(); tx.begin_session('a'); t=tx.create(400,1,1)
        tx.mark_sent(t.id); tx.acknowledge(400,1,1,0); tx.mark_sent(t.id)
        self.assertEqual(tx.list()[0]['state'],'SUCCEEDED')
        t=tx.create(22,1,1); tx.begin_session('b'); tx.mark_sent(t.id)
        self.assertEqual(next(x for x in tx.list() if x['id']==t.id)['state'],'CANCELLED')

    def test_mission_retry_cancel_and_premature_ack(self):
        m=MissionManager(); m.begin_session('a'); m.download_started()
        self.assertEqual(m.retry_payload(time.monotonic()+3),{'kind':'mission_list'})
        m.count(2); m.item({'seq':1}); self.assertEqual(m.retry_payload(time.monotonic()+6)['seq'],0)
        m.cancel(); self.assertFalse(m.item({'seq':0}))
        m.upload_started([{'latitude':1,'longitude':2,'altitude':3}])
        self.assertFalse(m.ack(0)); m.requested_item(0); self.assertTrue(m.ack(0))
