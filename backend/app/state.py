from __future__ import annotations

import copy
import math
import threading
import time
from collections import Counter, deque
from typing import Any

from .config import PROTOCOL_VERSION
from .store import now_iso


def field(units: str | None = None) -> dict[str, Any]:
    result = {"value": None, "available": False, "source": "", "received_at": None}
    if units:
        result["units"] = units
    return result


def initial_state() -> dict[str, Any]:
    return {
        "protocol_version": PROTOCOL_VERSION,
        "vehicle": {"name": "Vehicle 1", "type": field(), "autopilot": field(), "firmware": field(),
                    "firmware_details": {}, "protocol_capabilities": field(), "mavlink_version": field(),
                    "system_id": field(), "component_id": field(), "simulated": False},
        "connection": {"state": "disconnected", "transport": None, "message": "No vehicle connected",
                       "connected_since": None, "last_heartbeat": None, "last_update": None,
                       "heartbeat_age_s": None, "telemetry_age_s": None, "message_rate_hz": 0.0,
                       "messages_total": 0, "link_lost": True, "error": None},
        "status": {"armed": field(), "flight_mode": field(), "system_status": field(),
                   "estimator": field(), "mission_current": field()},
        "attitude": {key: field("deg" if key in ("roll", "pitch", "yaw") else "deg/s") for key in ("roll", "pitch", "yaw", "roll_speed", "pitch_speed", "yaw_speed")},
        "position": {key: field("m" if key in ("altitude_msl_m", "relative_altitude_m", "vertical_speed_m_s", "ground_speed_m_s") else "deg") for key in
                     ("latitude_deg", "longitude_deg", "altitude_msl_m", "relative_altitude_m", "vertical_speed_m_s", "ground_speed_m_s", "heading_deg")},
        "gps": {"fix_type": field(), "satellites": field(), "hdop": field(), "eph_m": field(), "epv_m": field()},
        "battery": {"voltage_v": field("V"), "current_a": field("A"), "remaining_percent": field("%"), "consumed_mah": field("mAh"), "battery_id": field()},
        "rc": {"channels_us": field("µs"), "rssi_percent": field("%")},
        "actuators": {"outputs_us": field("µs")},
        "vibration": {"x": field(), "y": field(), "z": field(), "clip_count": field()},
        "system": {"cpu_load_percent": field("%"), "sensors_present": field(), "sensors_enabled": field(), "sensors_health": field()},
        "availability": {},
        "updated_at": None,
    }


class VehicleState:
    """Thread-safe canonical state shared by ingestion, API clients, and broadcasters."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._state = initial_state()
        self._counts: Counter[str] = Counter()
        self._type_times: dict[str, deque[float]] = {}
        self._message_times: deque[float] = deque(maxlen=512)
        self._last_telemetry_monotonic: float | None = None
        self._last_heartbeat_monotonic: float | None = None
        self._connected_monotonic: float | None = None

    def reset(self) -> None:
        with self._lock:
            self._state = initial_state()
            self._counts.clear()
            self._type_times.clear()
            self._message_times.clear()
            self._last_telemetry_monotonic = None
            self._last_heartbeat_monotonic = None
            self._connected_monotonic = None

    def set_connection(self, state: str, message: str, transport: str | None = None, error: str | None = None) -> None:
        with self._lock:
            self._state["connection"].update({"state": state, "message": message, "transport": transport,
                "error": error, "link_lost": state in ("disconnected", "error", "reconnecting")})
            if state == "connected" and self._connected_monotonic is None:
                self._connected_monotonic = time.monotonic()
                self._state["connection"]["connected_since"] = now_iso()
            if state in ("disconnected", "error"):
                self._connected_monotonic = None

    def mark_message(self, msg_type: str, heartbeat: bool = False) -> None:
        current = time.monotonic()
        with self._lock:
            self._counts[msg_type] += 1
            if msg_type not in self._type_times:
                if len(self._type_times) >= 256:
                    self._type_times.pop(next(iter(self._type_times)))
                self._type_times[msg_type] = deque(maxlen=512)
            self._type_times[msg_type].append(current)
            self._message_times.append(current)
            self._last_telemetry_monotonic = current
            self._state["connection"]["messages_total"] += 1
            self._state["connection"]["last_update"] = now_iso()
            self._state["updated_at"] = self._state["connection"]["last_update"]
            if heartbeat:
                self._last_heartbeat_monotonic = current
                self._state["connection"]["last_heartbeat"] = now_iso()
            self._state["connection"]["message_rate_hz"] = self._rate(current)

    def _rate(self, current: float) -> float:
        if len(self._message_times) < 2:
            return float(len(self._message_times))
        elapsed = max(0.1, current - self._message_times[0])
        return round((len(self._message_times) - 1) / elapsed, 1)

    def set(self, section: str, key: str, value: Any, units: str | None = None,
            source: str = "MAVLink", available: bool = True, received_at: str | None = None) -> None:
        stamp = received_at or now_iso()
        with self._lock:
            target = self._state[section]
            existing = target.get(key)
            if isinstance(existing, dict) and "available" in existing:
                target[key] = {"value": value if available else None, "available": bool(available),
                               "source": source, "received_at": stamp, **({"units": units or existing.get("units")} if units or existing.get("units") else {})}
            else:
                target[key] = value
            if section not in ("vehicle", "connection", "availability", "status"):
                self._state["availability"][f"{section}.{key}"] = bool(available)

    def update_vehicle(self, **values: Any) -> None:
        with self._lock:
            self._state["vehicle"].update(values)

    def count_by_type(self) -> dict[str, int]:
        with self._lock:
            return dict(self._counts)

    def rate_by_type(self, window_s: float = 10.0) -> dict[str, float]:
        current = time.monotonic()
        with self._lock:
            rates = {}
            for kind, times in self._type_times.items():
                recent = [stamp for stamp in times if current - stamp <= window_s]
                if len(recent) >= 2:
                    elapsed = max(0.1, recent[-1] - recent[0])
                    rates[kind] = round((len(recent) - 1) / elapsed, 2)
                else:
                    rates[kind] = 0.0
            return rates

    def snapshot(self) -> dict[str, Any]:
        current = time.monotonic()
        with self._lock:
            result = copy.deepcopy(self._state)
            c = result["connection"]
            c["heartbeat_age_s"] = round(current - self._last_heartbeat_monotonic, 1) if self._last_heartbeat_monotonic is not None else None
            c["telemetry_age_s"] = round(current - self._last_telemetry_monotonic, 1) if self._last_telemetry_monotonic is not None else None
            if c["state"] == "connected":
                if self._last_heartbeat_monotonic is None or current - self._last_heartbeat_monotonic > 5:
                    c["state"] = "reconnecting"
                    c["message"] = "Heartbeat lost · attempting recovery"
                    c["link_lost"] = True
                elif self._last_telemetry_monotonic is None or current - self._last_telemetry_monotonic > 2.5:
                    c["state"] = "stale"
                    c["message"] = "Telemetry is stale"
                    c["link_lost"] = False
            for section, values in result.items():
                if not isinstance(values, dict):
                    continue
                for key, item in values.items():
                    if isinstance(item, dict) and item.get("available") and item.get("received_at"):
                        try:
                            dt = __import__("datetime").datetime.fromisoformat(item["received_at"].replace("Z", "+00:00"))
                            age = max(0, (time.time() - dt.timestamp()))
                        except (ValueError, TypeError):
                            age = None
                        item["age_s"] = round(age, 1) if age is not None else None
                        item["fresh"] = age is not None and age <= 5
            return result

    @property
    def last_heartbeat_monotonic(self) -> float | None:
        with self._lock:
            return self._last_heartbeat_monotonic


def finite(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if math.isfinite(parsed) else None
