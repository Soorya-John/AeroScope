from __future__ import annotations

import importlib.util
import json
import struct
import sys
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
from app.mavlink import validate_connection

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
