"""Workflow and optional transport regressions; no physical vehicle I/O."""
import sys,unittest,tempfile,time,threading
from pathlib import Path
from types import SimpleNamespace
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app.state import VehicleState
from app.store import Store
from app.events import EventEngine
from app.telemetry import TelemetryNormalizer
from app.mavlink import MavlinkManager
from app.vehicle_logs import VehicleLogs
from app.parameters import validate_parameter_value
import test_core as core

class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.state=VehicleState(); self.events=EventEngine(Store(Path(self.temp.name)/'test.sqlite'))
        self.normalizer=TelemetryNormalizer(self.state,self.events);self.m=MavlinkManager(self.state,self.events,self.normalizer)
        self.m._session_id='test';self.m.transactions.begin_session('test');self.m.parameters.begin_session('test')
        self.normalizer.ingest(core.FakeMessage('HEARTBEAT',{'base_mode':0,'custom_mode':4,'type':2,'autopilot':3,'system_status':4}))
        self.state.set_connection('connected','test');self.m.workflows.claim('client-a')
    def tearDown(self):self.m.disconnect();self.temp.cleanup()
    def test_ownership_is_exclusive_and_expiry_does_not_replay(self):
        with self.assertRaises(RuntimeError):self.m.workflows.claim('client-b')
        with self.assertRaises(RuntimeError):self.m.workflows.require_owner('client-b')
        self.m.workflows.lease_until=0;self.m.workflows.claim('client-b')
        self.assertEqual(self.m.workflows.ownership()['owner'],'client-b');self.assertTrue(self.m._outbound.empty())
    def test_staging_is_local_and_rejects_changed_baseline(self):
        self.m.parameters.ingest('GAIN',1.25,9,0,1)
        staged=self.m.workflows.stage({'changes':[{'name':'GAIN','value':2.5,'before':1.25}]})
        self.assertEqual(staged['state'],'STAGED');self.assertFalse(staged['atomic']);self.assertTrue(self.m._outbound.empty())
        with self.assertRaises(ValueError):self.m.workflows.stage({'changes':[{'name':'GAIN','value':3,'before':0}]})
    def test_calibration_and_motor_interlocks(self):
        with self.assertRaises(ValueError):self.m.workflows.start_calibration('gyro','')
        with self.assertRaises(ValueError):self.m.workflows.dispatch('POST','/api/setup/motor-test',{'bench':True,'confirmation':'PROPS REMOVED','motor':1,'throttle':11,'duration':1},'client-a')
        calibration=self.m.workflows.start_calibration('compass','CALIBRATE')
        self.assertEqual(calibration['state'],'WAITING_ACK')
        self.m.workflows.ingest(SimpleNamespace(get_type=lambda:'MAG_CAL_PROGRESS',completion_pct=42))
        self.assertEqual(self.m.workflows.calibration_status()['progress'],42)
        self.m.workflows.ingest(SimpleNamespace(get_type=lambda:'MAG_CAL_REPORT',cal_mask=1,compass_id=0,cal_status=4,autosaved=1,fitness=1.1))
        self.assertEqual(self.m.workflows.calibration_status()['state'],'SUCCEEDED')
    def test_cancel_tags_queued_protocol_requests(self):
        self.m.request_parameters();queued=self.m._outbound.get_nowait()[1];self.m.parameters.cancel()
        self.assertNotEqual(queued['_generation'],self.m.parameters.generation)
    def test_float32_normalization_and_overflow(self):
        self.assertNotEqual(validate_parameter_value(1.234,9),1.234)
        with self.assertRaises(ValueError):validate_parameter_value(1e100,9)
    def test_log_out_of_order_duplicate_and_cancel(self):
        log=VehicleLogs();log.begin_session('test');log.list_started()
        log.ingest(SimpleNamespace(get_type=lambda:'LOG_ENTRY',num_logs=1,id=7,size=181,time_utc=0))
        log.download_started(7)
        for offset,count in [(90,90),(90,90),(0,90),(180,1)]:log.ingest(SimpleNamespace(get_type=lambda:'LOG_DATA',id=7,ofs=offset,count=count,data=[1]*90))
        self.assertEqual(len(log.bytes()),181);self.assertEqual(log.snapshot()['received_bytes'],181)
        log.download_started(7);log.cancel()
        with self.assertRaises(RuntimeError):log.bytes()

    def test_growing_log_final_packet_is_clipped_to_listed_snapshot(self):
        log=VehicleLogs();log.begin_session('test');log.list_started()
        log.ingest(SimpleNamespace(get_type=lambda:'LOG_ENTRY',num_logs=1,id=1,size=91,time_utc=0));log.download_started(1)
        for offset in (0,90):log.ingest(SimpleNamespace(get_type=lambda:'LOG_DATA',id=1,ofs=offset,count=90,data=list(range(90))))
        self.assertEqual(len(log.bytes()),91);self.assertEqual(log.bytes()[-1],0)

class TransportTests(unittest.TestCase):
    def test_asgi_routes_and_shared_services(self):
        try:
            from fastapi.testclient import TestClient
            from app import asgi,main
        except ImportError:self.skipTest('Install web and dev extras for ASGI checks')
        self.assertIs(asgi.mavlink,main.mavlink)
        main.mavlink.disconnect();main.mavlink.workflows.owner=None
        with TestClient(asgi.app) as client:
            for path in ['/api/health','/api/setup','/api/parameters','/api/parameters/batch','/api/control/actions','/api/vehicle-logs','/vendor/leaflet.js','/workflows.js']:
                self.assertEqual(client.get(path).status_code,200,path)
            self.assertEqual(client.post('/api/control/claim',json={},headers={'X-AeroScope-Client':'asgi-test'}).status_code,200)
            denied=client.post('/api/connection/disconnect',json={},headers={'X-AeroScope-Client':'other'})
            self.assertEqual(denied.status_code,409)
            self.assertEqual(client.post('/api/connection/disconnect',json={},headers={'X-AeroScope-Client':'asgi-test'}).status_code,200)
            main.mavlink.parameters.begin_session('export-test');main.mavlink.parameters.ingest('GAIN',1.5,9,0,1)
            exported=client.get('/api/parameters/export?format=csv');self.assertEqual(exported.status_code,200);self.assertIn('GAIN',exported.text)
            bad=client.post('/api/control/claim',json={},headers={'X-AeroScope-Client':'asgi-test','Origin':'http://evil.example'})
            self.assertEqual(bad.status_code,403)
