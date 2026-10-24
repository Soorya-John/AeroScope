from __future__ import annotations

import math
import os
import statistics
import threading
import uuid
from collections import defaultdict
from pathlib import Path
from typing import Any

from .config import LOG_DIR, MAX_UPLOAD_BYTES
from .store import Store, now_iso

MAX_RECORDS = 2_000_000
MAX_SAMPLES = 12_000
MAX_EVENTS = 5_000
MAX_FINDINGS = 1_000
MAGIC_ULOG = b"ULog\x01\x12\x35"
MAGIC_DATAFLASH = b"\xA3\x95"


def identify_log(prefix: bytes) -> str:
    if prefix.startswith(MAGIC_ULOG):
        return "ulog"
    if prefix.startswith(MAGIC_DATAFLASH):
        return "dataflash"
    raise ValueError("File content is not a supported ArduPilot DataFlash BIN or PX4 ULog file")


def safe_log_path(stored_name: str) -> Path:
    candidate = (LOG_DIR / stored_name).resolve()
    if candidate.parent != LOG_DIR.resolve():
        raise ValueError("Invalid stored log path")
    return candidate


def _num(mapping: dict[str, Any], *names: str) -> float | None:
    for name in names:
        val = mapping.get(name)
        if val is None:
            continue
        try:
            out = float(val)
            if math.isfinite(out):
                return out
        except (ValueError, TypeError, OverflowError):
            pass
    return None


def _one_of(mapping: dict[str, Any], *names: str) -> Any:
    for name in names:
        if mapping.get(name) is not None:
            return mapping[name]
    return None


def _time(record: dict[str, Any]) -> float | None:
    for key in ("TimeUS", "time_usec", "timestamp"):
        value = _num(record, key)
        if value is not None:
            # DataFlash TimeUS and ULog timestamp are both microseconds, including
            # small values near boot; do not infer units from magnitude.
            return value / 1_000_000
    value = _num(record, "TimeMS", "time_boot_ms")
    return value / 1000 if value is not None else None


def _from_quaternion(q: list[float]) -> tuple[float, float, float] | None:
    if len(q) != 4:
        return None
    w, x, y, z = q
    roll = math.atan2(2 * (w*x + y*z), 1 - 2*(x*x+y*y))
    pitch = math.asin(max(-1.0, min(1.0, 2*(w*y-z*x))))
    yaw = math.atan2(2*(w*z+x*y), 1 - 2*(y*y+z*z))
    return tuple(math.degrees(x) for x in (roll, pitch, yaw))


class LogAnalyzer:
    def __init__(self, store: Store) -> None:
        self.store = store
        self._workers: dict[str, threading.Thread] = {}
        self._lock = threading.Lock()

    def upload_bytes(self, filename: str, body: bytes) -> dict[str, Any]:
        if not body:
            raise ValueError("The uploaded file is empty")
        if len(body) > MAX_UPLOAD_BYTES:
            raise ValueError(f"The upload exceeds the configured {MAX_UPLOAD_BYTES // (1024*1024)} MiB size limit")
        fmt = identify_log(body[:8])
        original_name = Path(filename.replace("\\", "/")).name
        if not original_name or original_name in (".", ".."):
            original_name = "flight-log.bin" if fmt == "dataflash" else "flight-log.ulg"
        suffix = ".bin" if fmt == "dataflash" else ".ulg"
        stored_name = f"{uuid.uuid4().hex}{suffix}"
        path = safe_log_path(stored_name)
        # Exclusive create inside the controlled log directory.
        with path.open("xb") as handle:
            handle.write(body)
        analysis_id = self.store.create_analysis(original_name, stored_name, fmt, len(body))
        worker = threading.Thread(target=self._analyze, args=(analysis_id, path, fmt),
                                  name=f"aeroscope-log-{analysis_id[:8]}", daemon=True)
        with self._lock:
            self._workers[analysis_id] = worker
        worker.start()
        return {"id": analysis_id, "status": "queued", "format": fmt, "size_bytes": len(body)}

    def _analyze(self, analysis_id: str, path: Path, fmt: str) -> None:
        try:
            result = parse_dataflash(path) if fmt == "dataflash" else parse_ulog(path)
            result.update({"analysis_id": analysis_id, "source_format": fmt, "analyzed_at": now_iso()})
            self.store.save_analysis_result(analysis_id, result)
        except Exception as exc:
            self.store.fail_analysis(analysis_id, f"{type(exc).__name__}: {str(exc)[:900]}")
        finally:
            with self._lock:
                self._workers.pop(analysis_id, None)

    def wait(self, analysis_id: str, timeout: float = 20) -> dict[str, Any] | None:
        with self._lock:
            worker = self._workers.get(analysis_id)
        if worker:
            worker.join(timeout)
        return self.store.get_analysis(analysis_id)


class _Accumulator:
    def __init__(self, fmt: str) -> None:
        self.fmt = fmt
        self.series: list[dict[str, Any]] = []
        self.events: list[dict[str, Any]] = []
        self.modes: set[str] = set()
        self.field_coverage: set[str] = set()
        self.first_t: float | None = None
        self.last_t: float | None = None
        self.last_sample_t: float | None = None
        self.record_count = 0
        self.max_altitude: float | None = None
        self.max_speed: float | None = None
        self.max_current: float | None = None
        self.min_voltage: float | None = None
        self.max_alt_ref = ""
        self.max_speed_ref = ""
        self._gaps: list[float] = []
        self._last_sample: dict[str, Any] = {}
        self._battery: list[tuple[float, float | None, float | None]] = []
        self._attitude: list[tuple[float, float | None, float | None, float | None, float | None]] = []
        self._vibes: list[tuple[float, float | None]] = []
        self._gps_fixes: list[tuple[float, int | None]] = []

    def ingest(self, kind: str, rec: dict[str, Any]) -> None:
        self.record_count += 1
        if self.record_count > MAX_RECORDS:
            raise ValueError(f"Log exceeded the bounded {MAX_RECORDS:,} record analysis limit")
        t = _time(rec)
        if t is not None:
            if self.first_t is None:
                self.first_t = t
            if self.last_t is not None and t > self.last_t:
                self._gaps.append(t - self.last_t)
            self.last_t = max(t, self.last_t or t)
            if self.last_sample_t is None:
                self.last_sample_t = t

        # DataFlash ATT records are degrees in cdeg units, decoded by pymavlink.
        if kind in ("ATT", "ATTITUDE"):
            actual_roll = _num(rec, "Roll", "roll")
            actual_pitch = _num(rec, "Pitch", "pitch")
            actual_yaw = _num(rec, "Yaw", "yaw")
            desired_roll = _num(rec, "DesRoll", "RollDes", "roll_body")
            desired_pitch = _num(rec, "DesPitch", "PitchDes", "pitch_body")
            if kind == "ATTITUDE":
                actual_roll = math.degrees(actual_roll) if actual_roll is not None else None
                actual_pitch = math.degrees(actual_pitch) if actual_pitch is not None else None
                actual_yaw = math.degrees(actual_yaw) if actual_yaw is not None else None
                desired_roll = math.degrees(desired_roll) if desired_roll is not None else None
                desired_pitch = math.degrees(desired_pitch) if desired_pitch is not None else None
            self._last_sample.update({"roll_deg": actual_roll, "pitch_deg": actual_pitch, "yaw_deg": actual_yaw,
                                      "desired_roll_deg": desired_roll, "desired_pitch_deg": desired_pitch})
            if t is not None:
                self._attitude.append((t, actual_roll, actual_pitch, desired_roll, desired_pitch))
            for f in ("roll_deg", "pitch_deg", "yaw_deg"):
                if self._last_sample.get(f) is not None: self.field_coverage.add(f)
        elif kind in ("GPS", "GPS_RAW_INT", "vehicle_global_position", "vehicle_gps_position"):
            lat = _num(rec, "Lat", "lat", "latitude_deg", "latitude")
            lon = _num(rec, "Lng", "Lon", "lon", "longitude_deg", "longitude")
            if lat is not None and abs(lat) > 180: lat /= 1e7
            if lon is not None and abs(lon) > 180: lon /= 1e7
            valid = lat is not None and lon is not None and abs(lat) <= 90 and abs(lon) <= 180
            self._last_sample.update({"latitude_deg": lat if valid else None, "longitude_deg": lon if valid else None})
            fix = _num(rec, "Status", "Fix", "fix_type")
            sats = _num(rec, "NSats", "satellites_used", "satellites_visible")
            self._last_sample.update({"gps_fix": fix, "satellites": sats})
            eph = _num(rec, "eph_m", "eph", "Eph")
            if eph is not None and kind in ("GPS", "GPS_RAW_INT"): eph /= 100
            self._last_sample["eph_m"] = eph
            if t is not None: self._gps_fixes.append((t, int(fix) if fix is not None else None))
            alt = _num(rec, "Alt", "alt", "altitude_msl_m")
            if alt is not None:
                if abs(alt) > 100000: alt /= 1000
                self._last_sample["altitude_msl_m"] = alt
                self._max("altitude", alt, "GPS altitude above mean sea level")
            speed = _num(rec, "Spd", "GSpd", "speed", "ground_speed_m_s")
            if speed is not None:
                if kind == "GPS_RAW_INT": speed /= 100
                self._last_sample["speed_m_s"] = speed
                self._max("speed", speed, "GPS/DataFlash speed")
            if valid:
                self.field_coverage.update(("latitude_deg", "longitude_deg"))
        elif kind in ("BAT", "BATTERY_STATUS", "SYS_STATUS", "battery_status"):
            voltage = _num(rec, "Volt", "volt", "voltage_v", "voltage")
            current = _num(rec, "Curr", "current_a", "current")
            remaining = _num(rec, "RemPct", "battery_remaining", "remaining")
            if kind == "battery_status" and voltage is not None:
                pass
            elif voltage is not None and voltage > 200:
                voltage /= 1000
            if kind in ("BATTERY_STATUS", "SYS_STATUS") and current is not None and abs(current) > 1000:
                current /= 100
            if kind == "BAT" and current is not None and abs(current) > 1000:
                current /= 100
            self._last_sample.update({"voltage_v": voltage, "current_a": current, "remaining_percent": remaining})
            if t is not None: self._battery.append((t, voltage, current))
            if voltage is not None:
                self.min_voltage = min(voltage, self.min_voltage) if self.min_voltage is not None else voltage
                self.field_coverage.add("voltage_v")
            if current is not None:
                self._max("current", current, "Recorded battery current")
                self.field_coverage.add("current_a")
            warning = _num(rec, "warning", "battery_warning")
            if warning is not None and warning > 0:
                self._event(t, "battery_warning", "medium", "Battery warning state was recorded by the autopilot.",
                            {"warning_code": int(warning)}, "recorded")
        elif kind in ("RCIN", "input_rc"):
            channels = [_num(rec, f"C{i}", f"values[{i-1}]") for i in range(1, 19)]
            channels = [value for value in channels if value is not None]
            if channels:
                self._last_sample["rc_inputs"] = channels
                self.field_coverage.add("rc_inputs")
        elif kind in ("RCOU", "actuator_outputs"):
            channels = [_num(rec, f"C{i}", f"output[{i-1}]") for i in range(1, 17)]
            channels = [value for value in channels if value is not None]
            if channels:
                self._last_sample["actuator_outputs"] = channels
                self.field_coverage.add("actuator_outputs")
        elif kind in ("VIBE", "vibration"):
            x, y, z = (_num(rec, axis, "vibration_"+axis) for axis in ("VibeX", "VibeY", "VibeZ"))
            peak = max([v for v in (x, y, z) if v is not None], default=None)
            self._last_sample.update({"vibe_x": x, "vibe_y": y, "vibe_z": z})
            if t is not None: self._vibes.append((t, peak))
            if peak is not None: self.field_coverage.add("vibration")
        elif kind in ("CTUN", "vehicle_local_position"):
            alt = _num(rec, "Alt", "alt", "z")
            if kind == "vehicle_local_position" and alt is not None: alt = -alt
            if alt is not None:
                self._last_sample["relative_altitude_m"] = alt
                self._max("relative_altitude", alt, "CTUN/local position relative altitude")
            climb = _num(rec, "CRt", "climb_rate", "vz")
            if kind == "vehicle_local_position" and climb is not None: climb = -climb
            if climb is not None: self._last_sample["climb_m_s"] = climb
        elif kind in ("MODE", "vehicle_status"):
            mode = _one_of(rec, "Mode", "mode", "nav_state")
            if mode is not None:
                name = str(mode)
                self.modes.add(name)
                self._event(t, "flight_mode", "informational", f"Flight mode recorded: {name}", {"mode": name}, "recorded")
            if kind == "vehicle_status":
                armed = _num(rec, "arming_state")
                if armed in (2, 3):
                    arm_kind = "arming" if armed == 2 else "disarming"
                    self._event(t, arm_kind, "informational", "Vehicle arming state recorded.", {"arming_state": int(armed)}, "recorded")
                if bool(_one_of(rec, "failsafe", "failsafe_active")):
                    self._event(t, "failsafe", "high", "Vehicle status reports an active failsafe.",
                                {"failsafe": True, "failsafe_flags": _one_of(rec, "failsafe_flags")}, "recorded")
        elif kind == "EV":
            event_id = _num(rec, "Id", "id")
            event_map = {10: ("arming", "Armed"), 11: ("disarming", "Disarmed"),
                         15: ("arming", "Auto armed"), 18: ("land_complete", "Land complete"),
                         19: ("gps_lost", "Lost GPS"), 25: ("home_set", "Home position set"),
                         28: ("takeoff", "Not landed / takeoff complete")}
            if event_id is not None:
                event_type, label = event_map.get(int(event_id), ("vehicle_event", f"Vehicle event {int(event_id)}"))
                self._event(t, event_type, "informational", f"DataFlash event: {label}", {"event_id": int(event_id)}, "recorded")
        elif kind == "ARM":
            arm_state = _num(rec, "ArmState", "arm_state")
            if arm_state is not None:
                armed = bool(arm_state)
                self._event(t, "arming" if armed else "disarming", "informational",
                    "Vehicle armed" if armed else "Vehicle disarmed", {"arm_state": int(armed)}, "recorded")
        elif kind in ("ERR", "MSG", "STATUSTEXT", "LOGGING", "FAILSAFE"):
            text = str(_one_of(rec, "Message", "Text", "text", "Id", "id") or "").strip()
            if kind == "ERR" and not text:
                subsystem, code = _one_of(rec, "Subsys", "subsys"), _one_of(rec, "ECode", "ecode")
                text = f"Autopilot error record · subsystem {subsystem} · code {code}"
                self._event(t, "autopilot_error", "medium", text, {"subsystem": subsystem, "error_code": code}, "recorded")
            if text:
                lowered = text.lower()
                if "arm" in lowered:
                    event_kind = "arming" if "disarm" not in lowered else "disarming"
                    self._event(t, event_kind, "informational", f"Recorded vehicle event: {text[:180]}", {"record": text[:300]}, "recorded")
                if any(word in lowered for word in ("fail", "failsafe", "ekf", "estimator", "battery", "vibration", "gps")):
                    category = "failsafe" if "fail" in lowered else "estimator" if "ekf" in lowered or "estimator" in lowered else "recorded_warning"
                    sev = "high" if "fail" in lowered else "medium"
                    self._event(t, category, sev, f"Autopilot log message: {text[:180]}", {"record": text[:300]}, "recorded")

        if t is not None and self.last_sample_t is not None and (t - self.last_sample_t >= 0.1 or not self.series):
            sample = {"t": round(max(0, t - (self.first_t or t)), 3), "timestamp_s": t, **self._last_sample}
            self.series.append(sample)
            self.last_sample_t = t
            if len(self.series) > MAX_SAMPLES:
                # Retain endpoints and bound analysis JSON on very long logs.
                self.series = self.series[::2]
                self.last_sample_t = t

    def _max(self, key: str, value: float, ref: str) -> None:
        field = "max_" + key
        existing = getattr(self, field)
        if existing is None or value > existing:
            setattr(self, field, value)
            setattr(self, "max_" + key + "_ref", ref)

    def _event(self, t: float | None, kind: str, severity: str, desc: str,
               evidence: dict[str, Any], source_type: str) -> None:
        if len(self.events) >= MAX_EVENTS:
            return
        offset = max(0, t - (self.first_t or t)) if t is not None else None
        timestamp = t if t is not None else None
        self.events.append({"id": str(uuid.uuid4()), "type": kind, "severity": severity,
                            "description": desc, "evidence": evidence, "source": source_type,
                            "recorded": source_type == "recorded", "timestamp_s": timestamp,
                            "t": offset})

    def finish(self) -> dict[str, Any]:
        if not self.series and self.record_count:
            self.series.append({"t": 0, "timestamp_s": self.first_t, **self._last_sample})
        findings = self._findings()
        durations = [d for d in self._gaps if d > 0]
        if durations:
            med = statistics.median(durations)
            threshold = max(2.0, med * 4)
            for gap in durations:
                if gap > threshold:
                    self._finding(findings, "telemetry_gap", "low" if gap < 5 else "medium",
                        "A gap in logged sample timestamps was detected.",
                        {"gap_s": round(gap, 3), "median_interval_s": round(med, 4), "threshold_s": round(threshold, 3)},
                        None, None, "Timestamp gap rule", ["Logging or sampling was interrupted."],
                        "Compare the interval with the original log and other message streams.",
                        "A log gap does not prove that the aircraft lost its telemetry link.")
        return {
            "summary": {"record_count": self.record_count, "duration_s": round(max(0, self.last_t - self.first_t), 2) if self.first_t is not None and self.last_t is not None else None,
                "max_altitude_m": self.max_altitude, "max_altitude_reference": self.max_alt_ref or None,
                "max_relative_altitude_m": getattr(self, "max_relative_altitude", None),
                "max_relative_altitude_reference": getattr(self, "max_relative_altitude_ref", None),
                "max_speed_m_s": self.max_speed, "max_speed_reference": self.max_speed_ref or None,
                "max_current_a": self.max_current, "min_voltage_v": self.min_voltage,
                "flight_modes": sorted(self.modes), "data_fields": sorted(self.field_coverage),
                "gps_path_points": sum(1 for x in self.series if x.get("latitude_deg") is not None and x.get("longitude_deg") is not None),
                "time_source": "embedded record timestamps" if self.first_t is not None else None},
            "series": self.series, "events": sorted(self.events, key=lambda e: (e["t"] if e["t"] is not None else -1)),
            "findings": findings, "data_limitations": self._limitations(),
        }

    def _finding(self, out: list[dict[str, Any]], kind: str, severity: str, description: str,
                 evidence: dict[str, Any], start: float | None, end: float | None, method: str,
                 explanations: list[str], investigation: str, limitations: str) -> None:
        if len(out) >= MAX_FINDINGS:
            return
        out.append({"id": str(uuid.uuid4()), "type": kind, "severity": severity,
            "description": description, "start_s": start, "end_s": end, "evidence": evidence,
            "measurements": evidence, "method": method, "confidence": "moderate" if start is not None else "low",
            "possible_explanations": explanations, "recommended_investigation": investigation,
            "data_limitations": limitations})
        if start is not None:
            self._event(start + (self.first_t or 0), kind, severity, description, evidence, "analysis")

    def _findings(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        # Vibration screening threshold is explicit and configurable in code; log units differ by source.
        if self._vibes:
            active = [(t, v) for t, v in self._vibes if v is not None and v >= 30.0]
            groups: list[list[tuple[float, float]]] = []
            for sample in active:
                if not groups or sample[0] - groups[-1][-1][0] > 1.5:
                    groups.append([sample])
                else:
                    groups[-1].append(sample)
            for group in groups:
                self._finding(out, "vibration", "medium", "Recorded vibration reached the screening threshold.",
                    {"peak": max(v for _, v in group), "threshold": 30.0, "units": "source-defined; DataFlash VIBE usually m/s²", "samples": len(group)},
                    group[0][0] - (self.first_t or group[0][0]), group[-1][0] - (self.first_t or group[-1][0]),
                    "Peak recorded VIBE axis ≥ 30 for one or more samples; intervals split after 1.5 s",
                    ["Mechanical vibration, sensor mounting, or flight conditions can affect the recorded value."],
                    "Review axis-specific VIBE values, clipping, vehicle configuration, and the relevant firmware guidance.",
                    "A threshold excursion is a screening result and does not establish a propeller, motor, or airframe fault.")
        # Battery sag is described as a pattern, never a battery-fault diagnosis.
        comparable = [(t, v, c) for t, v, c in self._battery if v is not None and c is not None]
        if len(comparable) >= 3:
            low_load = min(comparable, key=lambda item: item[2])
            high_load = max(comparable, key=lambda item: item[2])
            median_current = statistics.median(c for _, _, c in comparable)
            if low_load[1] - high_load[1] >= 1.5 and high_load[2] > max(3, median_current * 1.5):
                self._finding(out, "battery_voltage_sag", "medium", "Voltage decreased during a higher-current interval.",
                    {"low_load_voltage_v": round(low_load[1], 3), "high_load_voltage_v": round(high_load[1], 3), "drop_v": round(low_load[1]-high_load[1], 3),
                     "low_current_a": round(low_load[2], 3), "high_current_a": round(high_load[2], 3), "samples": len(comparable)},
                    low_load[0] - (self.first_t or low_load[0]), high_load[0] - (self.first_t or high_load[0]), "Observed voltage/current co-variation",
                    ["Load-related voltage response, state of charge, wiring resistance, or measurement conditions."],
                    "Compare cell-level voltage, current, battery warnings, and post-load recovery if those records are available.",
                    "Voltage sag alone cannot diagnose a defective battery; cell-level data and pack configuration may be absent.")
        # Sustained attitude tracking error only when desired and actual fields coexist.
        att = [r for r in self._attitude if r[1] is not None and r[2] is not None and r[3] is not None and r[4] is not None]
        if len(att) >= 3:
            errors = []
            for t, roll, pitch, desroll, despitch in att:
                er = abs((roll - desroll + 180) % 360 - 180)
                ep = abs((pitch - despitch + 180) % 360 - 180)
                if max(er, ep) >= 15: errors.append((t, er, ep))
            if len(errors) >= 3 and errors[-1][0] - errors[0][0] >= 1.0:
                self._finding(out, "attitude_tracking", "low", "Sustained actual-to-desired attitude difference was recorded.",
                    {"max_roll_error_deg": round(max(e[1] for e in errors), 2), "max_pitch_error_deg": round(max(e[2] for e in errors), 2), "samples": len(errors)},
                    errors[0][0]-(self.first_t or errors[0][0]), errors[-1][0]-(self.first_t or errors[-1][0]),
                    "Angle-wrapped roll/pitch difference ≥ 15° for at least three recorded samples",
                    ["Aggressive manoeuvres, saturation, setpoint changes, or tracking performance can produce this pattern."],
                    "Inspect the synchronized setpoint, actuator, rate, and flight-mode signals around this interval.",
                    "Temporary maneuvering error is not a PID diagnosis; sampling and setpoint message semantics depend on firmware.")
        # GPS quality is only reported when records explicitly contain a fix value.
        degraded = [(t, fix) for t, fix in self._gps_fixes if fix is not None and fix < 3]
        if degraded:
            self._finding(out, "gps_quality", "medium", "GPS records report a fix below 3D quality.",
                {"minimum_fix_type": min(f for _, f in degraded), "samples": len(degraded)},
                degraded[0][0]-(self.first_t or degraded[0][0]), degraded[-1][0]-(self.first_t or degraded[-1][0]),
                "Recorded GPS fix_type / Status < 3", ["Satellite geometry, reception, or receiver state may affect the logged fix."],
                "Correlate fix, satellite count, accuracy estimates, and estimator messages.",
                "A low fix is a recorded status; it does not alone establish estimator failure or causality.")
        return out

    def _limitations(self) -> list[str]:
        out = ["Diagnostics are deterministic screening rules, not root-cause diagnoses.",
               "Only message types and fields present in this log are analyzed.",
               "Missing records may reflect logging configuration or sampling gaps; absence is not proof that an event did not occur."]
        if "latitude_deg" not in self.field_coverage: out.append("No valid GPS path was found; map position is unavailable.")
        if "desired_roll_deg" not in self.field_coverage: out.append("Desired attitude data is unavailable; attitude tracking comparison is omitted.")
        return out


def parse_dataflash(path: Path) -> dict[str, Any]:
    try:
        from pymavlink import DFReader
    except ImportError as exc:
        raise RuntimeError("Install pymavlink to parse ArduPilot DataFlash logs") from exc
    with path.open("rb") as source:
        magic = source.read(8)
    if path.stat().st_size < 8 or magic[:2] != MAGIC_DATAFLASH:
        raise ValueError("DataFlash log magic is invalid or the file is truncated")
    reader = DFReader.DFReader_binary(str(path), zero_time_base=False)
    acc = _Accumulator("dataflash")
    try:
        while True:
            try:
                msg = reader.recv_msg()
            except Exception as exc:
                raise ValueError(f"DataFlash parser stopped at a corrupt record: {str(exc)[:250]}") from exc
            if msg is None:
                break
            try:
                rec = msg.to_dict()
                kind = str(rec.pop("mavpackettype", ""))
                acc.ingest(kind, rec)
            except Exception as exc:
                raise ValueError(f"Could not normalize DataFlash record: {str(exc)[:250]}") from exc
    finally:
        try:
            mode_segments = reader.flightmode_list()
            if mode_segments:
                # pymavlink resolves firmware-specific mode numbers using the
                # log parameters; prefer that over guessing from numeric MODE.
                acc.events = [event for event in acc.events if event["type"] != "flight_mode"]
                acc.modes.clear()
                for mode, start, end in mode_segments:
                    label = str(mode)
                    acc.modes.add(label)
                    acc._event(float(start) if start is not None else None, "flight_mode", "informational",
                               f"Flight mode recorded: {label}", {"mode": label,
                               "end_timestamp_s": float(end) if end is not None else None}, "recorded")
        except Exception:
            # Mode resolution is auxiliary. Keep the raw MODE record if a
            # firmware-specific parameter map is incomplete.
            pass
        reader.close()
    if acc.record_count == 0:
        raise ValueError("The file has a DataFlash header but contains no valid records")
    result = acc.finish()
    result["parser"] = {"name": "pymavlink.DFReader_binary", "limitations": "Message coverage depends on the logged DataFlash format definitions."}
    return result


def parse_ulog(path: Path) -> dict[str, Any]:
    try:
        from pyulog import ULog
    except ImportError as exc:
        raise RuntimeError("PX4 ULog detected, but pyulog is not installed. Install backend optional dependency 'ulog' and re-upload the file.") from exc
    with path.open("rb") as source:
        magic = source.read(len(MAGIC_ULOG))
    if path.stat().st_size < len(MAGIC_ULOG) or magic != MAGIC_ULOG:
        raise ValueError("PX4 ULog magic is invalid or the file is truncated")
    try:
        ulog = ULog(str(path))
    except Exception as exc:
        raise ValueError(f"pyulog could not parse this ULog file: {str(exc)[:250]}") from exc
    acc = _Accumulator("ulog")
    datasets = {dataset.name: dataset for dataset in ulog.data_list}
    # pyulog's public dataset interface: ULog.data_list / get_dataset(name, multi_instance).
    def records(name: str):
        dataset = datasets.get(name)
        if dataset is None:
            return
        values = dataset.data
        ts = values.get("timestamp")
        if ts is None: return
        n = len(ts)
        stride = max(1, n // MAX_RECORDS)
        for i in range(0, n, stride):
            row = {key: series[i].item() if hasattr(series[i], "item") else series[i] for key, series in values.items()}
            yield row
    streams: list[tuple[float, str, dict[str, Any]]] = []
    for topic in ("vehicle_attitude", "vehicle_global_position", "vehicle_local_position", "battery_status", "vehicle_gps_position", "vehicle_status", "vehicle_land_detected", "vehicle_attitude_setpoint", "input_rc", "actuator_outputs"):
        for row in records(topic) or ():
            t = _time(row)
            if t is not None: streams.append((t, topic, row))
    streams.sort(key=lambda item: item[0])
    if not streams:
        raise ValueError("The ULog parsed successfully but contains no supported timed datasets")
    attitudes: dict[int, tuple[float, float, float]] = {}
    setpoints: dict[int, tuple[float, float, float]] = {}
    for t, topic, row in streams:
        if topic == "vehicle_attitude":
            q = [float(row.get(f"q[{i}]", 0)) for i in range(4)]
            euler = _from_quaternion(q)
            if euler: attitudes[int(t*100)] = euler
            continue
        if topic == "vehicle_attitude_setpoint":
            q = [float(row.get(f"q_d[{i}]", 0)) for i in range(4)]
            euler = _from_quaternion(q)
            if euler: setpoints[int(t*100)] = euler
    mode_keys=sorted(setpoints)
    for t, topic, row in streams:
        if topic in ("vehicle_attitude", "vehicle_attitude_setpoint"):
            if topic == "vehicle_attitude":
                actual = attitudes.get(int(t*100))
                desired = min(setpoints.items(), key=lambda p: abs(p[0]-int(t*100)))[1] if mode_keys else None
                if actual:
                    att_row={"TimeUS": int(t*1_000_000), "Roll": actual[0], "Pitch": actual[1], "Yaw": actual[2]}
                    if desired: att_row.update({"DesRoll":desired[0],"DesPitch":desired[1]})
                    acc.ingest("ATT", att_row)
            continue
        kind_map = {"vehicle_global_position": "vehicle_global_position", "vehicle_local_position": "vehicle_local_position",
                    "battery_status": "battery_status", "vehicle_gps_position": "vehicle_gps_position", "vehicle_status": "vehicle_status",
                    "input_rc": "input_rc", "actuator_outputs": "actuator_outputs"}
        if topic in kind_map:
            if topic == "vehicle_global_position":
                row["latitude_deg"] = row.get("lat"); row["longitude_deg"] = row.get("lon")
                if row.get("vel_n") is not None and row.get("vel_e") is not None:
                    row["speed"] = math.hypot(float(row["vel_n"]), float(row["vel_e"]))
            if topic == "vehicle_local_position":
                row["alt"] = row.get("z"); row["vz"] = row.get("vz")
            if topic == "vehicle_gps_position":
                row["fix_type"] = row.get("fix_type"); row["satellites_used"] = row.get("satellites_used")
            acc.ingest(kind_map[topic], row)
    result = acc.finish()
    result["parser"] = {"name": "pyulog.ULog", "datasets": sorted(datasets),
                        "limitations": "Only selected position, attitude, battery, GPS, and status datasets are normalized."}
    return result
