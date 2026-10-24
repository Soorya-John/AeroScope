from __future__ import annotations

import math
import threading
import time
from collections import deque
from typing import Any

from .events import EventEngine
from .state import VehicleState, finite
from .store import now_iso


def _field(obj: Any, name: str, default: Any = None) -> Any:
    try:
        value = getattr(obj, name)
        return value if value is not None else default
    except (AttributeError, TypeError):
        return default


def _degrees(rad: Any) -> float | None:
    value = finite(rad)
    return math.degrees(value) if value is not None else None


def _clean_text(value: Any) -> str:
    if isinstance(value, bytes):
        return value.split(b"\0", 1)[0].decode("utf-8", "replace").strip()
    return str(value or "").split("\0", 1)[0].strip()


class TelemetryNormalizer:
    def __init__(self, state: VehicleState, events: EventEngine, source: str = "MAVLink") -> None:
        self.state, self.events, self.source = state, events, source
        self._inspector: deque[dict[str, Any]] = deque(maxlen=500)
        self._series: deque[dict[str, Any]] = deque(maxlen=6000)
        self._lock = threading.Lock()
        self._last_sample = 0.0

    def reset_session(self) -> None:
        with self._lock:
            self._inspector.clear()
            self._series.clear()
            self._last_sample = 0.0

    def inspector(self) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._inspector)

    def series(self, limit: int = 2000) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._series)[-min(max(limit, 1), 6000):]

    def ingest(self, msg: Any) -> dict[str, Any] | None:
        try:
            msg_type = str(msg.get_type())
            if msg_type == "BAD_DATA":
                return None
            received_at = now_iso()
            as_dict = msg.to_dict() if hasattr(msg, "to_dict") else {}
            self.state.mark_message(msg_type, heartbeat=msg_type == "HEARTBEAT")
            sysid = msg.get_srcSystem() if hasattr(msg, "get_srcSystem") else None
            compid = msg.get_srcComponent() if hasattr(msg, "get_srcComponent") else None
            if sysid:
                self.state.set("vehicle", "system_id", int(sysid), source=self.source, received_at=received_at)
            if compid:
                self.state.set("vehicle", "component_id", int(compid), source=self.source, received_at=received_at)
            self._decode(msg, msg_type, as_dict, received_at)
            selected = {k: v for k, v in as_dict.items() if k not in ("mavpackettype",) and isinstance(v, (int, float, str, bool))}
            record = {"type": msg_type, "source_system": sysid, "source_component": compid,
                      "timestamp": received_at, "fields": selected}
            with self._lock:
                self._inspector.appendleft(record)
            state = self.state.snapshot()
            self.events.evaluate(state)
            now = time.monotonic()
            if now - self._last_sample >= 0.1:
                self._last_sample = now
                self._record_sample(state, received_at)
            return record
        except Exception as exc:
            # A malformed/firmware-specific message must not stop the sole reader.
            with self._lock:
                self._inspector.appendleft({"type": "DECODE_ERROR", "timestamp": now_iso(),
                    "fields": {"error": f"{type(exc).__name__}: {str(exc)[:180]}"}})
            return None

    def _put(self, section: str, key: str, value: Any, unit: str | None, stamp: str,
             valid: bool = True) -> None:
        self.state.set(section, key, value, units=unit, source=self.source,
                       available=valid and value is not None, received_at=stamp)

    def _decode(self, msg: Any, kind: str, data: dict[str, Any], stamp: str) -> None:
        if kind == "HEARTBEAT":
            base = int(_field(msg, "base_mode", 0))
            self._put("status", "armed", bool(base & 128), None, stamp)
            mode: str | None = None
            try:
                from pymavlink import mavutil
                mode = mavutil.mode_string_v10(msg)
            except Exception:
                mode = data.get("custom_mode")
            self._put("status", "flight_mode", mode, None, stamp, mode is not None)
            autopilot = int(_field(msg, "autopilot", -1))
            vehicle_type = int(_field(msg, "type", -1))
            self.state.set("vehicle", "autopilot", {3: "ArduPilot", 12: "PX4"}.get(autopilot, f"MAV_AUTOPILOT_{autopilot}"), source=self.source, received_at=stamp)
            self.state.set("vehicle", "type", {1: "Fixed wing", 2: "Quadrotor", 3: "Coaxial", 4: "Helicopter", 7: "Airship", 10: "Ground rover", 12: "Submarine", 13: "Hexarotor", 14: "Octorotor", 15: "Tricopter"}.get(vehicle_type, f"MAV_TYPE_{vehicle_type}"), source=self.source, received_at=stamp)
            self._put("status", "system_status", int(_field(msg, "system_status", -1)), None, stamp)
        elif kind == "ATTITUDE":
            for axis in ("roll", "pitch", "yaw"):
                val = _degrees(_field(msg, axis))
                self._put("attitude", axis, val, "deg", stamp)
            for axis in ("roll", "pitch", "yaw"):
                val = _degrees(_field(msg, axis + "speed"))
                self._put("attitude", axis + "_speed", val, "deg/s", stamp)
        elif kind == "ATTITUDE_QUATERNION":
            q = [finite(_field(msg, f"q{i}")) for i in range(4)]
            if all(x is not None for x in q):
                w, x, y, z = q
                roll = math.atan2(2 * (w*x + y*z), 1 - 2 * (x*x + y*y))
                sinp = max(-1.0, min(1.0, 2 * (w*y - z*x)))
                pitch = math.asin(sinp)
                yaw = math.atan2(2 * (w*z + x*y), 1 - 2 * (y*y + z*z))
                for axis, value in zip(("roll", "pitch", "yaw"), (roll, pitch, yaw)):
                    self._put("attitude", axis, math.degrees(value), "deg", stamp)
        elif kind == "GLOBAL_POSITION_INT":
            lat, lon = int(_field(msg, "lat", 0)), int(_field(msg, "lon", 0))
            valid = abs(lat) <= 900000000 and abs(lon) <= 1800000000
            self._put("position", "latitude_deg", lat / 1e7 if valid else None, "deg", stamp, valid)
            self._put("position", "longitude_deg", lon / 1e7 if valid else None, "deg", stamp, valid)
            self._put("position", "altitude_msl_m", int(_field(msg, "alt", 0)) / 1000, "m", stamp)
            self._put("position", "relative_altitude_m", int(_field(msg, "relative_alt", 0)) / 1000, "m", stamp)
            self._put("position", "vertical_speed_m_s", -int(_field(msg, "vz", 0)) / 100, "m/s", stamp)
            hdg = int(_field(msg, "hdg", 65535))
            self._put("position", "heading_deg", hdg / 100 if hdg != 65535 and hdg < 36000 else None, "deg", stamp, hdg != 65535 and hdg < 36000)
            vx, vy = int(_field(msg, "vx", 0)), int(_field(msg, "vy", 0))
            self._put("position", "ground_speed_m_s", math.hypot(vx, vy) / 100, "m/s", stamp)
        elif kind == "GPS_RAW_INT":
            fix = int(_field(msg, "fix_type", 0))
            satellites = int(_field(msg, "satellites_visible", 255))
            eph, epv = int(_field(msg, "eph", 65535)), int(_field(msg, "epv", 65535))
            self._put("gps", "fix_type", fix, None, stamp)
            self._put("gps", "satellites", satellites if satellites != 255 else None, None, stamp, satellites != 255)
            self._put("gps", "eph_m", eph / 100 if eph != 65535 else None, "m", stamp, eph != 65535)
            self._put("gps", "epv_m", epv / 100 if epv != 65535 else None, "m", stamp, epv != 65535)
        elif kind == "SYS_STATUS":
            self._battery(int(_field(msg, "voltage_battery", 65535)), int(_field(msg, "current_battery", -1)), int(_field(msg, "battery_remaining", -1)), stamp)
            load = int(_field(msg, "load", 0))
            self._put("system", "cpu_load_percent", load / 10, "%", stamp, load <= 1000)
            for dest, source in (("sensors_present", "onboard_control_sensors_present"), ("sensors_enabled", "onboard_control_sensors_enabled"), ("sensors_health", "onboard_control_sensors_health")):
                self._put("system", dest, int(_field(msg, source, 0)), None, stamp)
        elif kind == "BATTERY_STATUS":
            voltages = _field(msg, "voltages", [])
            total_mv = sum(int(v) for v in voltages if int(v) != 65535) if voltages is not None else 0
            unknown = any(int(v) == 65535 for v in (voltages or [])[:1])
            current = int(_field(msg, "current_battery", -1))
            remaining = int(_field(msg, "battery_remaining", -1))
            self._battery(total_mv if total_mv > 0 and not unknown else 65535, current, remaining, stamp)
            self._put("battery", "battery_id", int(_field(msg, "id", 0)), None, stamp)
            consumed = int(_field(msg, "current_consumed", -1))
            self._put("battery", "consumed_mah", consumed if consumed >= 0 else None, "mAh", stamp, consumed >= 0)
        elif kind == "VFR_HUD":
            alt = finite(_field(msg, "alt")); climb = finite(_field(msg, "climb")); speed = finite(_field(msg, "groundspeed"))
            if alt is not None: self._put("position", "altitude_msl_m", alt, "m", stamp)
            if climb is not None: self._put("position", "vertical_speed_m_s", climb, "m/s", stamp)
            if speed is not None: self._put("position", "ground_speed_m_s", speed, "m/s", stamp)
            heading = finite(_field(msg, "heading"))
            if heading is not None: self._put("position", "heading_deg", heading % 360, "deg", stamp)
        elif kind == "RC_CHANNELS":
            count = min(18, max(0, int(_field(msg, "chancount", 0))))
            channels = [int(_field(msg, f"chan{i}_raw", 0)) for i in range(1, min(count, 18) + 1)]
            self._put("rc", "channels_us", channels if channels else None, "µs", stamp, bool(channels))
            rssi = int(_field(msg, "rssi", 255))
            self._put("rc", "rssi_percent", rssi if 0 <= rssi <= 100 else None, "%", stamp, 0 <= rssi <= 100)
        elif kind == "RC_CHANNELS_RAW":
            count = int(_field(msg, "chan_count", 8))
            channels = [int(_field(msg, f"chan{i}_raw", 0)) for i in range(1, min(max(count, 0), 8) + 1)]
            self._put("rc", "channels_us", channels if channels else None, "µs", stamp, bool(channels))
        elif kind == "SERVO_OUTPUT_RAW":
            port = int(_field(msg, "port", 0))
            outputs = [int(_field(msg, f"servo{i}_raw", 0)) for i in range(1, 9)]
            self._put("actuators", "outputs_us", outputs, "µs", stamp)
            self._put("actuators", "port", port, None, stamp)
        elif kind == "VIBRATION":
            for axis in ("x", "y", "z"):
                self._put("vibration", axis, finite(_field(msg, "vibration_" + axis)), "m/s²", stamp)
            self._put("vibration", "clip_count", int(_field(msg, "clipping_0", 0)) + int(_field(msg, "clipping_1", 0)) + int(_field(msg, "clipping_2", 0)), None, stamp)
        elif kind in ("EKF_STATUS_REPORT", "ESTIMATOR_STATUS"):
            selected = {k: v for k, v in data.items() if isinstance(v, (int, float, bool))}
            selected["message_type"] = kind
            self._put("status", "estimator", selected, None, stamp)
            # EKF_STATUS_REPORT flags are feature/validity flags, not a single
            # universal healthy mask. Keep them as evidence; only explicit
            # STATUSTEXT warnings raise an estimator event.
        elif kind == "STATUSTEXT":
            severity = int(_field(msg, "severity", 6))
            text = _clean_text(_field(msg, "text", ""))
            if text and severity <= 4:
                self.events.autopilot_text(text, "high" if severity <= 2 else "medium")
        elif kind == "AUTOPILOT_VERSION":
            version = int(_field(msg, "flight_sw_version", 0))
            packed = f"{(version >> 24)&255}.{(version >> 16)&255}.{(version >> 8)&255}.{version&255}" if version else None
            self._put("vehicle", "firmware", packed, None, stamp, packed is not None)
        elif kind == "MISSION_CURRENT":
            self._put("status", "mission_current", int(_field(msg, "seq", 0)), None, stamp)
        elif kind == "EXTENDED_SYS_STATE":
            self._put("status", "landed_state", int(_field(msg, "landed_state", 0)), None, stamp)
        elif kind == "COMMAND_ACK":
            self._put("status", "last_command_ack", {k: data.get(k) for k in ("command", "result", "progress", "result_param2")}, None, stamp)

    def _battery(self, voltage_mv: int, current_ca: int, remaining: int, stamp: str) -> None:
        self._put("battery", "voltage_v", voltage_mv / 1000 if voltage_mv != 65535 and voltage_mv > 0 else None, "V", stamp, voltage_mv != 65535 and voltage_mv > 0)
        self._put("battery", "current_a", current_ca / 100 if current_ca >= 0 else None, "A", stamp, current_ca >= 0)
        self._put("battery", "remaining_percent", remaining if 0 <= remaining <= 100 else None, "%", stamp, 0 <= remaining <= 100)

    def _record_sample(self, state: dict[str, Any], stamp: str) -> None:
        def value(path: tuple[str, str]) -> float | None:
            item = state[path[0]][path[1]]
            return finite(item.get("value")) if item.get("available") else None
        sample = {"timestamp": stamp, "roll_deg": value(("attitude", "roll")), "pitch_deg": value(("attitude", "pitch")),
                  "yaw_deg": value(("attitude", "yaw")), "altitude_m": value(("position", "relative_altitude_m")),
                  "speed_m_s": value(("position", "ground_speed_m_s")), "climb_m_s": value(("position", "vertical_speed_m_s")),
                  "voltage_v": value(("battery", "voltage_v")), "current_a": value(("battery", "current_a")),
                  "latitude_deg": value(("position", "latitude_deg")), "longitude_deg": value(("position", "longitude_deg")),
                  "gps_fix": value(("gps", "fix_type")), "gps_satellites": value(("gps", "satellites")),
                  "gps_eph_m": value(("gps", "eph_m")), "vibe_x": value(("vibration", "x")),
                  "vibe_y": value(("vibration", "y")), "vibe_z": value(("vibration", "z"))}
        with self._lock:
            self._series.append(sample)
