from __future__ import annotations

import importlib.util
import json
import struct
import sys
import threading
import time
from contextlib import contextmanager
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.events import EventEngine
from app.log_analysis import LogAnalyzer, identify_log, parse_dataflash, parse_ulog
from app.state import VehicleState
from app.store import Store
from app.telemetry import TelemetryNormalizer
from app.simulator import Simulator
from app.mavlink import MavlinkManager, validate_connection
from app.transactions import TransactionManager
from app.parameters import (ParameterStore, validate_parameter_value, validate_ext_parameter_value,
    parse_ext_parameter_value, compare_parameter_snapshots)
from app.missions import MissionManager, validate_mission

TEST_TEMP_ROOT = Path(__file__).resolve().parent / ".test-tmp"
TEST_TEMP_ROOT.mkdir(exist_ok=True)


@contextmanager
def tempdir():
    # TemporaryDirectory applies a restrictive ACL on Windows which is not
    # compatible with this managed test sandbox. Keep named fixtures directly
    # under a project-owned workspace directory instead.
    yield str(TEST_TEMP_ROOT)


class FakeMessage:
    def __init__(self, kind: str, fields: dict, system: int = 1, component: int = 1):
        self.kind, self.fields, self.system, self.component = kind, fields, system, component
    def get_type(self): return self.kind
    def to_dict(self): return {"mavpackettype": self.kind, **self.fields}
    def get_srcSystem(self): return self.system
    def get_srcComponent(self): return self.component
    def __getattr__(self, name):
        if name in self.fields: return self.fields[name]
        raise AttributeError(name)


def dataflash_fixture(path: Path, count: int = 12, include_events: bool = False) -> Path:
    def fmt(msg_type: int, length: int, name: str, form: str, columns: str) -> bytes:
        payload = struct.pack("<BB4s16s64s", msg_type, length,
            name.encode().ljust(4, b"\0"), form.encode().ljust(16, b"\0"), columns.encode().ljust(64, b"\0"))
        return b"\xa3\x95\x80" + payload
    data = fmt(129, 23, "ATT", "Qcccccc", "TimeUS,Roll,Pitch,Yaw,DesRoll,DesPitch,DesYaw")
    if include_events:
        data += fmt(130, 12, "EV", "QB", "TimeUS,Id")
    for idx in range(count):
        data += b"\xa3\x95\x81" + struct.pack("<Qhhhhhh", idx * 100_000, 2300, 0, 100, 0, 0, 100)
    if include_events:
        data += b"\xa3\x95\x82" + struct.pack("<QB", 1_000_000, 10)
        data += b"\xa3\x95\x82" + struct.pack("<QB", 2_000_000, 11)
    path.write_bytes(data)
    return path


class TelemetryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TEST_TEMP_ROOT
        self.db_path = TEST_TEMP_ROOT / "telemetry-test.sqlite3"
        self.db_path.unlink(missing_ok=True)
        self.store = Store(self.db_path)
        self.state = VehicleState()
        self.events = EventEngine(self.store)
        self.normalizer = TelemetryNormalizer(self.state, self.events)

    def tearDown(self): self.db_path.unlink(missing_ok=True)

    def test_disconnected_fields_are_explicitly_unavailable(self):
        snapshot = self.state.snapshot()
        self.assertEqual(snapshot["connection"]["state"], "disconnected")
        self.assertFalse(snapshot["attitude"]["roll"]["available"])
        self.assertIsNone(snapshot["position"]["latitude_deg"]["value"])

    def test_command_transaction_ack_correlation_and_session_cancellation(self):
        txs = TransactionManager()
        txs.begin_session("session-a")
        tx = txs.create(400, 1, 1, timeout=1)
        txs.mark_sent(tx.id)
        self.assertIsNone(txs.acknowledge(22, 1, 1, 0))
        self.assertIsNone(txs.acknowledge(400, 2, 1, 0))
        self.assertIsNone(txs.acknowledge(400, 1, 2, 0))
        result = txs.acknowledge(400, 1, 1, 5, progress=40)
        self.assertEqual(result["state"], "IN_PROGRESS")
        result = txs.acknowledge(400, 1, 1, 0)
        self.assertEqual(result["state"], "SUCCEEDED")
        failed = txs.create(21, 1, 1)
        txs.mark_sent(failed.id)
        txs.transmission_failed(failed.id, "serial port closed")
        self.assertEqual(next(x for x in txs.list() if x["id"] == failed.id)["state"], "FAILED")
        pending = txs.create(22, 1, 1)
        txs.begin_session("session-b")
        self.assertEqual(next(x for x in txs.list() if x["id"] == pending.id)["state"], "CANCELLED")

    def test_writer_failure_is_recorded_for_modes_and_commands(self):
        manager=MavlinkManager(self.state,self.events,self.normalizer)
        manager._session_id="writer-session"; manager.transactions.begin_session("writer-session")
        self.normalizer.ingest(FakeMessage("HEARTBEAT",{"base_mode":0,"custom_mode":0,"type":2,"autopilot":3,"system_status":4}))
        self.state.set_connection("connected","test","test")
        class FakeMav:
            def command_long_send(self,*args): raise OSError("link write failed")
        class FakeConnection:
            mav=FakeMav()
            def set_mode(self,mode): raise OSError("mode write failed")
        connection=FakeConnection(); manager._connection=connection
        manager._mode_request={"request_id":"mode-1","session_id":"writer-session","state":"PENDING"}
        manager._outbound.put(("writer-session",{"kind":"mode","request_id":"mode-1","mode":"GUIDED"}))
        tx=manager.transactions.create(400,1,1)
        manager._outbound.put(("writer-session",(tx.id,400,(1,0,0,0,0,0,0),1,1)))
        manager._writer_stop.clear()
        worker=threading.Thread(target=manager._writer,args=(connection,"writer-session"),daemon=True)
        worker.start()
        deadline=time.monotonic()+2
        while time.monotonic()<deadline and manager._mode_request["state"]=="PENDING":time.sleep(.01)
        manager._writer_stop.set(); worker.join(1)
        self.assertEqual(manager._mode_request["state"],"FAILED")
        self.assertIn("mode write failed",manager._mode_request["detail"])
        command=next(item for item in manager.transactions.list() if item["id"]==tx.id)
        self.assertEqual(command["state"],"FAILED")
        self.assertIn("link write failed",command["detail"])

    def test_mavlink_command_uses_normalized_heartbeat_target_ids(self):
        manager = MavlinkManager(self.state, self.events, self.normalizer)
        manager._session_id = "session-target"
        manager.transactions.begin_session("session-target")
        self.normalizer.ingest(FakeMessage("HEARTBEAT", {"base_mode": 0, "custom_mode": 0,
            "type": 2, "autopilot": 3, "system_status": 4}, system=7, component=42))
        self.state.set_connection("connected", "test", "test")
        tx = manager.queue_command(400, (1, 0, 0, 0, 0, 0, 0))
        self.assertEqual(tx["target_system"], 7)
        self.assertEqual(tx["target_component"], 42)
        manager.disconnect()

    def test_mode_request_requires_confirmation_and_firmware_mapping(self):
        manager = MavlinkManager(self.state, self.events, self.normalizer)
        manager._session_id = "mode-session"
        manager.transactions.begin_session("mode-session")
        self.normalizer.ingest(FakeMessage("HEARTBEAT", {"base_mode": 0, "custom_mode": 0,
            "type": 2, "autopilot": 3, "system_status": 4}, system=1, component=1))
        self.state.set_connection("connected", "test", "test")
        manager._capabilities = {"available": True, "modes": ["GUIDED"]}
        with self.assertRaisesRegex(ValueError, "confirmation"):
            manager.request_mode("GUIDED")
        with self.assertRaisesRegex(ValueError, "not available"):
            manager.request_mode("UNKNOWN", confirmed=True)
        request = manager.request_mode("GUIDED", confirmed=True)
        self.assertEqual(request["state"], "PENDING")
        self.assertEqual(manager._outbound.qsize(), 1)
        manager.disconnect()

    def test_autopilot_version_is_requested_once_and_capabilities_are_decoded(self):
        manager=MavlinkManager(self.state,self.events,self.normalizer)
        manager._session_id="cap-session"; manager.transactions.begin_session("cap-session")
        self.normalizer.ingest(FakeMessage("HEARTBEAT",{"base_mode":0,"custom_mode":0,"type":2,"autopilot":3,"system_status":4}))
        self.state.set_connection("connected","test","test")
        tx=manager._request_autopilot_version_once()
        self.assertEqual(tx["command"],512)
        queued=manager._outbound.get_nowait()
        self.assertEqual(queued[1][1],512)
        self.assertEqual(queued[1][2][0],148.0)
        self.assertIsNone(manager._request_autopilot_version_once())
        manager.transactions.mark_sent(tx["id"])
        self.normalizer.ingest(FakeMessage("AUTOPILOT_VERSION",{"flight_sw_version":0x01020304,
            "middleware_sw_version":0x02000000,"os_sw_version":0x03000000,"board_version":7,
            "flight_custom_version":bytes(range(1,9)),"middleware_custom_version":b"\0"*8,
            "os_custom_version":b"\0"*8,"vendor_id":42,"product_id":9,
            "capabilities":8192|4|16|16384}))
        caps=manager.capabilities()
        self.assertEqual(self.state.snapshot()["vehicle"]["firmware"]["value"],"1.2.3.4")
        self.assertEqual(caps["firmware_details"]["vendor_id"],42)
        self.assertTrue(caps["protocol_flags"]["mission_int"])
        self.assertTrue(caps["protocol_flags"]["param_bytewise"])
        self.assertTrue(caps["protocol_flags"]["mavlink2"])
        self.assertTrue(caps["protocol_flags"]["mission_fence"])

    def test_mission_wire_format_uses_advertised_capabilities(self):
        manager=MavlinkManager(self.state,self.events,self.normalizer); manager._session_id="mission-caps"
        manager.transactions.begin_session("mission-caps")
        self.normalizer.ingest(FakeMessage("HEARTBEAT",{"base_mode":0,"custom_mode":0,"type":2,"autopilot":3,"system_status":4}))
        self.state.set_connection("connected","test","test")
        self.state.set("vehicle","protocol_capabilities",1)
        self.assertIs(manager._mission_integer_protocol(),False)
        manager._require_mission_support()
        self.state.set("vehicle","protocol_capabilities",4)
        self.assertIs(manager._mission_integer_protocol(),True)
        self.state.set("vehicle","protocol_capabilities",0)
        with self.assertRaisesRegex(RuntimeError,"no supported MAVLink mission"):
            manager.request_mission_download()

    def test_writer_sends_legacy_mission_format_when_vehicle_requests_it(self):
        manager=MavlinkManager(self.state,self.events,self.normalizer); manager._session_id="legacy-mission"
        manager.transactions.begin_session("legacy-mission")
        self.normalizer.ingest(FakeMessage("HEARTBEAT",{"base_mode":0,"custom_mode":0,"type":2,"autopilot":3,"system_status":4}))
        self.state.set_connection("connected","test","test"); self.state.set("vehicle","protocol_capabilities",1)
        calls=[]; both=threading.Event()
        class FakeMav:
            def __getattr__(self,name):
                def send(*args):
                    calls.append((name,args))
                    if len(calls)==2: both.set()
                return send
        class FakeConnection: mav=FakeMav()
        connection=FakeConnection(); manager._connection=connection
        manager._outbound.put(("legacy-mission",{"kind":"mission_request","seq":2}))
        item={"kind":"mission_item","integer":False,"seq":0,"frame":6,"command":16,"current":0,"autocontinue":1,
              "param1":0.,"param2":0.,"param3":0.,"param4":0.,"latitude":37.5,"longitude":-122.1,"altitude":25.}
        manager._outbound.put(("legacy-mission",item)); manager._writer_stop.clear()
        worker=threading.Thread(target=manager._writer,args=(connection,"legacy-mission"),daemon=True); worker.start()
        self.assertTrue(both.wait(2)); manager._writer_stop.set(); worker.join(1)
        self.assertEqual([name for name,_ in calls],["mission_request_send","mission_item_send"])
        self.assertEqual(calls[1][1][3],3)
        self.assertEqual(calls[1][1][11:13],(37.5,-122.1))

    def test_writer_serializes_param_ext_set_on_live_session(self):
        manager=MavlinkManager(self.state,self.events,self.normalizer); manager._session_id="ext-session"
        manager.transactions.begin_session("ext-session")
        self.normalizer.ingest(FakeMessage("HEARTBEAT",{"base_mode":0,"custom_mode":0,"type":2,"autopilot":3,"system_status":4}))
        self.state.set_connection("connected","test","test")
        calls=[]; sent=threading.Event()
        class FakeMav:
            def param_ext_set_send(self,*args): calls.append(args); sent.set()
        class FakeConnection: mav=FakeMav()
        connection=FakeConnection(); manager._connection=connection
        manager._outbound.put(("ext-session",{"kind":"param_ext_set","name":"WIDE","value":2**40,"type":7}))
        manager._writer_stop.clear(); worker=threading.Thread(target=manager._writer,args=(connection,"ext-session"),daemon=True); worker.start()
        self.assertTrue(sent.wait(2)); manager._writer_stop.set(); worker.join(1)
        self.assertEqual(len(calls),1)
        self.assertEqual(calls[0][3],str(2**40).encode().ljust(128,b"\0"))
        self.assertEqual(calls[0][4],7)

    def test_arm_request_requires_exact_confirmation_and_unknown_safety_ack(self):
        manager = MavlinkManager(self.state, self.events, self.normalizer)
        manager._session_id = "arm-session"
        manager.transactions.begin_session("arm-session")
        self.normalizer.ingest(FakeMessage("HEARTBEAT", {"base_mode": 0, "custom_mode": 0,
            "type": 2, "autopilot": 3, "system_status": 4}, system=3, component=1))
        self.state.set_connection("connected", "test", "test")
        with self.assertRaisesRegex(ValueError, "Type ARM"):
            manager.request_arm(True, confirmation="yes")
        with self.assertRaisesRegex(ValueError, "Safety data unavailable"):
            manager.request_arm(True, confirmation="ARM")
        result = manager.request_arm(True, confirmation="ARM", acknowledge_unknown=True)
        self.assertEqual(result["state"], "PENDING")
        self.assertTrue(result["unknown_safety"])
        queued = manager._outbound.get_nowait()
        self.assertEqual(queued[1][1], 400)
        self.assertEqual(queued[1][2][0], 1.0)
        self.assertEqual(queued[1][2][1], 0.0)

    def test_arm_confirmation_requires_accepted_ack_and_matching_heartbeat(self):
        manager=MavlinkManager(self.state,self.events,self.normalizer)
        manager._session_id="arm-confirm"; manager.transactions.begin_session("arm-confirm")
        self.normalizer.ingest(FakeMessage("HEARTBEAT",{"base_mode":0,"custom_mode":0,"type":2,"autopilot":3,"system_status":4}))
        self.state.set_connection("connected","test","test")
        tx=manager.transactions.create(400,1,1); manager.transactions.mark_sent(tx.id)
        manager._arm_request={"transaction_id":tx.id,"session_id":"arm-confirm","armed":True,"state":"PENDING"}
        manager._sync_arm_status()
        self.assertEqual(manager.arm_status()["state"],"PENDING")
        manager.transactions.acknowledge(400,1,1,0)
        manager._sync_arm_status()
        self.assertEqual(manager.arm_status()["state"],"PENDING")
        self.normalizer.ingest(FakeMessage("HEARTBEAT",{"base_mode":128,"custom_mode":0,"type":2,"autopilot":3,"system_status":4}))
        manager._sync_arm_status()
        self.assertEqual(manager.arm_status()["state"],"CONFIRMED")

    def test_guided_reposition_requires_supported_firmware_mode_and_valid_target(self):
        manager=MavlinkManager(self.state,self.events,self.normalizer); manager._session_id="guided"
        manager.transactions.begin_session("guided")
        self.normalizer.ingest(FakeMessage("HEARTBEAT",{"base_mode":0,"custom_mode":4,"type":2,"autopilot":3,"system_status":4},system=1,component=1))
        self.state.set_connection("connected","test","test")
        self.state.set('status','flight_mode','GUIDED')
        with self.assertRaisesRegex(ValueError,"Type GO"):
            manager.guided_reposition(37,-122,30)
        with self.assertRaisesRegex(ValueError,"GPS fix"):
            manager.guided_reposition(37,-122,30,confirmation="GO")
        tx=manager.guided_reposition(37,-122,30,confirmation="GO",acknowledge_unknown=True)
        self.assertEqual(tx["command"],192)
        payload=manager._outbound.get_nowait()[1]
        self.assertEqual(payload[2][4:7],(37.0,-122.0,30.0))
        with self.assertRaises(ValueError): manager.guided_reposition(91,0,1,confirmation="GO",acknowledge_unknown=True)

    def test_parameter_types_validation_and_echo_confirmation(self):
        self.assertEqual(validate_parameter_value(42, 6), 42)
        self.assertEqual(validate_parameter_value(1.25, 9), 1.25)
        with self.assertRaises(ValueError): validate_parameter_value(1.2, 6)
        with self.assertRaises(ValueError): validate_parameter_value(-1, 5)
        params=ParameterStore(); params.begin_session("p1"); params.request_all(); params.pending_set("SERVOx", 3)
        params.ingest("SERVOx", 3.0, 6, 0, 1)
        snap=params.snapshot("servo")
        self.assertTrue(snap["progress"]["complete"])
        self.assertEqual(snap["pending"]["SERVOx"]["state"], "CONFIRMED")
        params.request_one("MISSING")
        params.expire(time.monotonic()+9)
        self.assertEqual(params.snapshot()["pending"]["MISSING"]["state"],"UNKNOWN")
        manager=MavlinkManager(self.state,self.events,self.normalizer); manager._session_id="param-write"
        manager.transactions.begin_session("param-write"); manager.parameters.begin_session("param-write")
        self.state.update_vehicle(simulated=False)
        self.normalizer.ingest(FakeMessage("HEARTBEAT",{"base_mode":0,"custom_mode":0,"type":2,"autopilot":3,"system_status":4}))
        self.state.set_connection("connected","test","test")
        manager.parameters.ingest("BIG",1.0,5,0,1)
        with self.assertRaisesRegex(ValueError,"represented exactly"):
            manager.set_parameter("BIG",2**25+1)
        manager.parameters.request_all()
        manager.parameters.ingest("WIDE",1.0,10,1,2)
        with self.assertRaisesRegex(ValueError,"not qualified"):
            manager.set_parameter("WIDE",1.25)
        self.assertEqual(validate_ext_parameter_value("custom",11),"custom")
        self.assertEqual(validate_ext_parameter_value("18446744073709551615",7),2**64-1)
        self.assertEqual(parse_ext_parameter_value(b"18446744073709551615\0",7),2**64-1)
        self.assertEqual(parse_ext_parameter_value(b"mode-A\0",11),"mode-A")
        with self.assertRaises(ValueError): validate_ext_parameter_value("x\0y",11)
        params.pending_set("EXT_VALUE",2**40,"param_ext",7)
        params.ingest("EXT_VALUE",float(2**40),7,1,2,protocol="param_ext")
        self.assertEqual(params.snapshot()["pending"]["EXT_VALUE"]["state"],"PENDING")
        ack=params.ingest_ext_ack("EXT_VALUE",2**40,7,3)
        self.assertEqual(ack["state"],"IN_PROGRESS")
        ack=params.ingest_ext_ack("EXT_VALUE",2**40,7,0)
        self.assertEqual(ack["state"],"CONFIRMED")
        changes=compare_parameter_snapshots([{"name":"A","value":1,"type":6},{"name":"B","value":2,"type":6}],
            [{"name":"A","value":3,"type":6},{"name":"C","value":4,"type":6}])
        self.assertEqual([x["change"] for x in changes],["modified","removed","added"])

    def test_parameter_snapshots_persist_and_compare(self):
        a=self.store.save_parameter_snapshot("before",[{"name":"GAIN","value":1.0,"type":9}])
        b=self.store.save_parameter_snapshot("after",[{"name":"GAIN","value":2.0,"type":9}])
        self.assertEqual(self.store.get_parameter_snapshot(a["id"])["name"],"before")
        self.assertEqual(compare_parameter_snapshots(self.store.get_parameter_snapshot(a["id"])["parameters"],
            self.store.get_parameter_snapshot(b["id"])["parameters"])[0]["change"],"modified")

    def test_mission_download_completeness_upload_ack_and_validation(self):
        manager=MissionManager(); manager.begin_session("mission-1"); manager.download_started(); manager.count(2)
        self.assertTrue(manager.item({"seq":1,"command":16})); self.assertEqual(manager.snapshot()["state"],"DOWNLOADING")
        self.assertTrue(manager.item({"seq":0,"command":16})); self.assertEqual(manager.snapshot()["state"],"SUCCEEDED")
        items=validate_mission([{"command":16,"latitude":37.0,"longitude":-122.0,"altitude":50}])
        manager.upload_started(items); self.assertEqual(manager.requested_item(0)["seq"],0)
        manager.ack(3); self.assertEqual(manager.snapshot()["state"],"FAILED")
        manager.update_current(0,2); manager.item_reached(0)
        self.assertEqual(manager.snapshot()["progress"]["current"],0)
        self.assertIn(0,manager.snapshot()["progress"]["reached"])
        manager.clear_started(); manager.ack(0); self.assertEqual(manager.snapshot()["state"],"SUCCEEDED")
        manager.upload_started(items); manager.expire(time.monotonic()+31); self.assertEqual(manager.snapshot()["state"],"UNKNOWN")
        with self.assertRaises(ValueError): validate_mission([{"command":999}])

    def test_attitude_radians_are_normalized_to_degrees(self):
        self.normalizer.ingest(FakeMessage("ATTITUDE", {"roll": 0.5, "pitch": -0.25, "yaw": 1.0,
            "rollspeed": 0.1, "pitchspeed": 0.2, "yawspeed": 0.3}))
        snap = self.state.snapshot()
        self.assertAlmostEqual(snap["attitude"]["roll"]["value"], 28.6479, places=3)
        self.assertAlmostEqual(snap["attitude"]["pitch"]["value"], -14.3239, places=3)
        self.assertAlmostEqual(snap["attitude"]["yaw"]["value"], 57.2958, places=3)
        self.assertEqual(snap["attitude"]["roll"]["units"], "deg")

    def test_position_scaling_and_valid_zero_zero_location(self):
        self.normalizer.ingest(FakeMessage("GLOBAL_POSITION_INT", {"lat": 377_000_000, "lon": -1_220_000_000,
            "alt": 123450, "relative_alt": 25100, "vx": 300, "vy": 400, "vz": -50, "hdg": 9000}))
        self.normalizer.ingest(FakeMessage("GLOBAL_POSITION_INT", {"lat": 0, "lon": 0,
            "alt": 123450, "relative_alt": 25100, "vx": 300, "vy": 400, "vz": -50, "hdg": 65535}))
        snap = self.state.snapshot()
        self.assertEqual(snap["position"]["latitude_deg"]["value"], 0.0)
        self.assertTrue(snap["position"]["latitude_deg"]["available"])
        self.assertEqual(snap["position"]["relative_altitude_m"]["value"], 25.1)
        self.assertEqual(snap["position"]["ground_speed_m_s"]["value"], 5)
        self.assertEqual(snap["position"]["vertical_speed_m_s"]["value"], 0.5)
        self.assertFalse(snap["position"]["heading_deg"]["available"])

    def test_battery_sentinels_do_not_become_zero_measurements(self):
        self.normalizer.ingest(FakeMessage("SYS_STATUS", {"voltage_battery": 65535,
            "current_battery": -1, "battery_remaining": -1, "load": 0,
            "onboard_control_sensors_present": 0, "onboard_control_sensors_enabled": 0,
            "onboard_control_sensors_health": 0}))
        battery = self.state.snapshot()["battery"]
        self.assertFalse(battery["voltage_v"]["available"])
        self.assertFalse(battery["current_a"]["available"])
        self.assertFalse(battery["remaining_percent"]["available"])
        self.assertFalse(self.state.snapshot()["gps"]["fix_type"]["available"])

    def test_heartbeat_identification_and_unknown_message_inspector(self):
        self.normalizer.ingest(FakeMessage("HEARTBEAT", {"base_mode": 128, "autopilot": 3, "type": 2,
            "system_status": 4, "custom_mode": 5}))
        self.normalizer.ingest(FakeMessage("UNKNOWN_222", {"vendor_value": 4}))
        snap = self.state.snapshot()
        self.assertTrue(snap["status"]["armed"]["value"])
        self.assertEqual(snap["vehicle"]["autopilot"]["value"], "ArduPilot")
        self.assertEqual(snap["vehicle"]["type"]["value"], "Quadrotor")
        self.assertEqual(self.normalizer.inspector()[0]["type"], "UNKNOWN_222")

    def test_stale_telemetry_and_heartbeat_timeout_are_distinct(self):
        self.state.set_connection("connected", "Connected", "test")
        self.state.mark_message("HEARTBEAT", heartbeat=True)
        with self.state._lock:
            self.state._last_telemetry_monotonic = time.monotonic() - 3
        self.assertEqual(self.state.snapshot()["connection"]["state"], "stale")
        with self.state._lock:
            self.state._last_heartbeat_monotonic = time.monotonic() - 6
        self.assertEqual(self.state.snapshot()["connection"]["state"], "reconnecting")

    def test_connection_uris_preserve_udp_listener_tcp_client_and_serial_semantics(self):
        self.assertEqual(validate_connection({"kind":"udp_listener","bind_address":"0.0.0.0","port":14550})[0], "udpin:0.0.0.0:14550")
        self.assertEqual(validate_connection({"kind":"tcp","host":"localhost","port":5760})[0], "tcp:localhost:5760")
        self.assertEqual(validate_connection({"kind":"serial","port":"COM4","baud":115200})[0], "COM4")
        with self.assertRaisesRegex(ValueError, "serial port"):
            validate_connection({"kind":"serial","port":"udpin:127.0.0.1:14550","baud":115200})

    def test_event_deduplication_and_recovery(self):
        self.state.set_connection("connected", "Connected", "simulation")
        self.state.mark_message("HEARTBEAT", heartbeat=True)
        self.state.set("battery", "remaining_percent", 15, "%", "test")
        first = self.events.evaluate(self.state.snapshot())
        second = self.events.evaluate(self.state.snapshot())
        self.assertEqual(len([x for x in first if x["type"] == "battery.low"]), 1)
        self.assertFalse(any(x["type"] == "battery.low" for x in second))
        self.state.set("battery", "remaining_percent", 60, "%", "test")
        self.events.evaluate(self.state.snapshot())
        stored = [e for e in self.events.list_events() if e["type"] == "battery.low"]
        self.assertIsNotNone(stored[0]["ended_at"])

    def test_simulated_control_responses_are_isolated_and_configurable(self):
        sim=Simulator(self.state,self.events,self.normalizer); sim.start("normal",1)
        with self.assertRaises(ValueError): sim.submit_control("mode",mode="UNKNOWN")
        with self.assertRaises(ValueError): sim.submit_control("reposition",latitude=91,longitude=0,altitude=10)
        with self.assertRaises(ValueError): sim.submit_control("parameter_set",name="SIM_FAILSAFE",value=1.5)
        with self.assertRaises(ValueError): sim.submit_control("mission_upload",items=[{"command":999}])
        sim.set_control_behavior("rejected"); sim.submit_control("arm"); sim._process_control()
        self.assertEqual(sim.control_status()["transactions"][0]["state"],"FAILED")
        self.assertFalse(sim._armed)
        sim.set_control_behavior("accepted"); sim.submit_control("arm"); sim._process_control()
        self.assertEqual(sim.control_status()["transactions"][0]["state"],"SUCCEEDED")
        self.assertTrue(sim._armed)
        sim.set_control_behavior("missing_ack"); sim.submit_control("mode",mode="RTL"); sim._process_control()
        self.assertEqual(sim.control_status()["transactions"][0]["state"],"UNKNOWN")
        self.assertEqual(sim._mode,"GUIDED")
        sim.set_control_behavior("accepted"); sim.submit_control("parameter_set",name="SIM_RATE",value=25); sim._process_control()
        self.assertEqual(sim._sim_parameters["SIM_RATE"],25)
        sim.submit_control("mission_upload",items=[{"seq":0,"command":16}]); sim._process_control()
        self.assertEqual(len(sim._sim_mission),1)
        sim.set_control_behavior("rejected"); sim.submit_control("mission_upload",items=[]); sim._process_control()
        self.assertEqual(sim._sim_mission[0]["seq"],0)
        sim.set_control_behavior("delayed"); sim.submit_control("mode",mode="RTL")
        started=time.monotonic(); sim._process_control()
        self.assertLess(time.monotonic()-started,0.05)
        self.assertEqual(sim.control_status()["transactions"][0]["state"],"WAITING_ACK")
        time.sleep(.45); sim._process_control()
        self.assertEqual(sim.control_status()["transactions"][0]["state"],"SUCCEEDED")
        sim.set_control_behavior("disconnect"); sim.submit_control("speed",speed=2); sim._process_control()
        self.assertGreater(sim._control_link_lost_until,time.monotonic())
        self.assertEqual(sim.control_status()["transactions"][0]["state"],"CANCELLED")
        sim.stop()

    def test_simulator_uses_mavlink_normalizer_and_exercises_rules(self):
        sim = Simulator(self.state, self.events, self.normalizer)
        try:
            sim.start("normal", 10)
            time.sleep(0.3)
            seen = {m["type"] for m in self.normalizer.inspector()}
            self.assertTrue({"HEARTBEAT", "ATTITUDE", "GLOBAL_POSITION_INT", "SYS_STATUS"}.issubset(seen))
            self.assertTrue(self.state.snapshot()["vehicle"]["simulated"])
            sim.start("gps_degradation", 10); time.sleep(1.35)
            self.assertEqual(self.state.snapshot()["gps"]["fix_type"]["value"], 2)
            self.assertTrue(any(e["type"] == "gps.fix_degraded" for e in self.events.list_events()))
            sim.start("elevated_vibration", 10); time.sleep(0.95)
            self.assertGreaterEqual(self.state.snapshot()["vibration"]["x"]["value"], 30)
            self.assertTrue(any(e["type"] == "vibration.elevated" for e in self.events.list_events()))
            sim.start("rc_degradation", 10); time.sleep(1.15)
            self.assertEqual(self.state.snapshot()["rc"]["rssi_percent"]["value"], 0)
            self.assertTrue(any(e["type"] == "rc.signal_degraded" for e in self.events.list_events()))
            # Freshness timeouts are wall-clock safety checks; the injected
            # simulated scenario itself runs at 10×, then waits five real
            # seconds for the configured heartbeat-loss threshold.
            sim.start("heartbeat_loss", 10); time.sleep(6.1)
            self.assertEqual(self.state.snapshot()["connection"]["state"], "reconnecting")
            sim.start("ekf_warning", 10); time.sleep(1.25)
            self.assertTrue(any(e["source"] == "Simulated autopilot STATUSTEXT" for e in self.events.list_events()))
        finally:
            sim.stop()


class LogTests(unittest.TestCase):
    def test_magic_validation_is_content_based(self):
        self.assertEqual(identify_log(b"\xA3\x95\x80junk"), "dataflash")
        self.assertEqual(identify_log(b"ULog\x01\x12\x35abc"), "ulog")
        with self.assertRaisesRegex(ValueError, "not a supported"):
            identify_log(b"BIN filename but plain text")

    def test_real_pymavlink_dataflash_parser_and_attitude_finding(self):
        with tempdir() as td:
            path = dataflash_fixture(Path(td) / "fixture.BIN", 25)
            result = parse_dataflash(path)
            self.assertEqual(result["parser"]["name"], "pymavlink.DFReader_binary")
            self.assertEqual(result["summary"]["record_count"], 26)
            self.assertAlmostEqual(result["summary"]["duration_s"], 2.4, places=2)
            self.assertTrue(any(f["type"] == "attitude_tracking" for f in result["findings"]))
            self.assertEqual(result["series"][0]["roll_deg"], 23)
            path.unlink()

    def test_corrupt_dataflash_is_reported_without_leaking_handle(self):
        with tempdir() as td:
            path = Path(td) / "broken.BIN"
            path.write_bytes(b"\xA3\x95\x00\x00bad")
            with self.assertRaises(ValueError):
                parse_dataflash(path)
            path.unlink()

    def test_dataflash_arming_events_are_mapped_from_recorded_ev_ids(self):
        with tempdir() as td:
            path = dataflash_fixture(Path(td) / "events.BIN", 3, include_events=True)
            result = parse_dataflash(path)
            kinds = [event["type"] for event in result["events"]]
            self.assertIn("arming", kinds)
            self.assertIn("disarming", kinds)
            self.assertTrue(all(event["recorded"] for event in result["events"]))
            path.unlink()

    def test_ulog_requires_the_real_optional_pyulog_parser(self):
        if importlib.util.find_spec("pyulog") is None:
            with tempdir() as td:
                path = Path(td) / "flight.ulg"
                path.write_bytes(b"ULog\x01\x12\x35" + b"\0" * 32)
                with self.assertRaisesRegex(RuntimeError, "pyulog is not installed"):
                    parse_ulog(path)

    def test_sqlite_analysis_persists_and_upload_rejects_wrong_content(self):
        with tempdir() as td:
            db_path = Path(td) / "upload-test.sqlite3"
            db_path.unlink(missing_ok=True)
            store = Store(db_path)
            analyzer = LogAnalyzer(store)
            with self.assertRaisesRegex(ValueError, "not a supported"):
                analyzer.upload_bytes("spoof.BIN", b"not a binary log")
            with patch("app.log_analysis.LOG_DIR", Path(td) / "logs"):
                (Path(td) / "logs").mkdir(exist_ok=True)
                fixture = dataflash_fixture(Path(td) / "good.BIN", 5)
                record = analyzer.upload_bytes("../../good.BIN", fixture.read_bytes())
                result = analyzer.wait(record["id"], 5)
                self.assertEqual(result["status"], "complete")
                self.assertEqual(result["original_name"], "good.BIN")
                self.assertEqual(result["result"]["summary"]["record_count"], 6)
                (Path(td) / "logs" / result["stored_name"]).unlink(missing_ok=True)
            db_path.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
