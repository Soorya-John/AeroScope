from __future__ import annotations

import math
import threading
import time
import uuid
from collections import deque
from typing import Any

from .events import EventEngine
from .state import VehicleState
from .store import now_iso
from .telemetry import TelemetryNormalizer
from .missions import validate_mission
from .parameters import validate_parameter_value

SCENARIOS = {
    "normal": "Normal flight", "gps_degradation": "GPS degradation",
    "battery_sag": "Battery voltage sag", "elevated_vibration": "Elevated vibration",
    "heartbeat_loss": "Heartbeat loss", "ekf_warning": "Estimator warning",
    "rc_degradation": "RC signal degradation",
}


class SimulatedMessage:
    def __init__(self, kind: str, fields: dict[str, Any]) -> None:
        self.kind, self.fields = kind, fields
    def get_type(self) -> str: return self.kind
    def to_dict(self) -> dict[str, Any]: return {"mavpackettype": self.kind, **self.fields}
    def get_srcSystem(self) -> int: return 1
    def get_srcComponent(self) -> int: return 1
    def __getattr__(self, name: str) -> Any:
        if name in self.fields: return self.fields[name]
        raise AttributeError(name)


class Simulator:
    def __init__(self, state: VehicleState, events: EventEngine, normalizer: TelemetryNormalizer) -> None:
        self.state, self.events, self.normalizer = state, events, normalizer
        self._lock = threading.RLock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._scenario = "normal"
        self._speed = 1.0
        self._control_lock=threading.RLock(); self._armed=False; self._mode="GUIDED"; self._control_behavior="accepted"
        self._control_transactions: deque[dict[str,Any]]=deque(maxlen=100); self._control_queue: deque[dict[str,Any]]=deque()
        self._control_deferred: deque[dict[str,Any]]=deque()
        self._sim_parameters={"SIM_RATE":10.0,"SIM_FAILSAFE":1,"SIM_BATT_MON":1}
        self._sim_mission: list[dict[str,Any]]=[]
        self._control_link_lost_until=0.0

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {"running": self._thread is not None and self._thread.is_alive(), "scenario": self._scenario, "speed": self._speed}

    def start(self, scenario: str = "normal", speed: float = 1.0) -> dict[str, Any]:
        if scenario not in SCENARIOS:
            raise ValueError("Unknown simulator scenario")
        if not 0.1 <= speed <= 10:
            raise ValueError("Simulation speed must be between 0.1× and 10×")
        self.stop()
        with self._lock:
            self._scenario, self._speed = scenario, speed
            self._stop.clear()
            self.normalizer.reset_session()
            self.normalizer.source = "Simulator"
            self.state.reset()
            self.state.update_vehicle(name="AeroScope SITL Demo", simulated=True)
            with self._control_lock:
                self._armed=False; self._mode="GUIDED"; self._control_transactions.clear(); self._control_queue.clear(); self._control_deferred.clear()
            self.state.set_connection("connecting", "Starting simulated telemetry", "simulation")
            self._thread = threading.Thread(target=self._run, name="aeroscope-simulator", daemon=True)
            self._thread.start()
            return self.status()

    def stop(self) -> None:
        self._stop.set()
        with self._lock:
            thread = self._thread
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=2)
        with self._lock:
            self._thread = None
        if self.state.snapshot()["vehicle"].get("simulated"):
            self.state.set_connection("disconnected", "Simulation stopped")
            self.state.update_vehicle(simulated=False)

    def reset(self) -> None:
        scenario, speed = self._scenario, self._speed
        self.start(scenario, speed)

    def set_control_behavior(self, behavior: str) -> None:
        if behavior not in {"accepted","rejected","unsupported","delayed","missing_ack","in_progress","disconnect"}:
            raise ValueError("Unknown simulated control response behavior")
        with self._control_lock: self._control_behavior=behavior

    def submit_control(self, kind: str, **params: Any) -> dict[str,Any]:
        if not self.status()["running"] or not self.state.snapshot()["vehicle"].get("simulated"):
            raise RuntimeError("Simulated control requires an active simulator session")
        if kind not in {"mode","arm","disarm","reposition","takeoff","land","speed","parameter_download","parameter_set","mission_download","mission_upload","mission_clear"}:
            raise ValueError("Unsupported simulated operation")
        # Keep the simulator API input contract strict too. Simulated requests
        # exercise the same data bounds without ever entering the MAVLink queue.
        if kind == "mode" and params.get("mode") not in {"STABILIZE","ALT_HOLD","AUTO","GUIDED","LOITER","RTL"}:
            raise ValueError("Unsupported simulated flight mode")
        if kind == "reposition":
            lat=float(params.get("latitude")); lon=float(params.get("longitude")); alt=float(params.get("altitude")); speed=float(params.get("speed",-1))
            if not math.isfinite(lat) or not -90<=lat<=90: raise ValueError("Latitude must be between -90 and 90")
            if not math.isfinite(lon) or not -180<=lon<=180: raise ValueError("Longitude must be between -180 and 180")
            if not math.isfinite(alt) or not -1000<=alt<=100000: raise ValueError("Altitude is outside supported validation range")
            if not math.isfinite(speed) or speed!=-1 and not .1<=speed<=100: raise ValueError("Speed must be -1 or between 0.1 and 100 m/s")
        if kind == "takeoff":
            altitude=float(params.get("altitude"))
            if not math.isfinite(altitude) or not 1<=altitude<=120: raise ValueError("Takeoff altitude must be between 1 and 120 m")
        if kind == "speed":
            speed=float(params.get("speed"))
            if not math.isfinite(speed) or not .5<=speed<=30: raise ValueError("Speed must be between 0.5 and 30 m/s")
        if kind == "parameter_set":
            name=str(params.get("name","")); types={"SIM_RATE":9,"SIM_FAILSAFE":1,"SIM_BATT_MON":1}
            if name not in types: raise ValueError("Unknown simulated parameter")
            params["value"]=validate_parameter_value(params.get("value"),types[name])
        if kind == "mission_upload":
            params["items"]=validate_mission(params.get("items"))
        transaction={"id":str(uuid.uuid4()),"kind":kind,"state":"QUEUED","params":params,"behavior":self._control_behavior,"created_at":now_iso()}
        with self._control_lock:
            self._control_transactions.append(transaction); self._control_queue.append(transaction)
        return {k:v for k,v in transaction.items() if k!="params"}

    def control_status(self) -> dict[str,Any]:
        with self._control_lock:
            return {"behavior":self._control_behavior,"transactions":[{k:v for k,v in x.items() if k!="params"} for x in reversed(self._control_transactions)]}

    def _process_control(self) -> None:
        with self._control_lock:
            now=time.monotonic()
            due=next((x for x in self._control_deferred if x.get("due_at",now+1)<=now),None)
            if due:
                self._control_deferred.remove(due); tx=due; behavior=tx["behavior"]
            elif self._control_queue:
                tx=self._control_queue.popleft(); behavior=tx["behavior"]
            else:return
            if behavior in {"delayed","in_progress"} and "due_at" not in tx:
                tx["state"]="WAITING_ACK" if behavior=="delayed" else "IN_PROGRESS"
                if behavior=="in_progress": tx["progress"]=50
                tx["due_at"]=now+(.4 if behavior=="delayed" else .6); self._control_deferred.append(tx); return
        tx.pop("due_at",None)
        tx.pop("behavior",None)
        self._complete_control(tx,behavior)

    def _complete_control(self,tx:dict[str,Any],behavior:str)->None:
        if behavior=="missing_ack": tx["state"]="UNKNOWN"; tx["detail"]="Simulated acknowledgement intentionally omitted"; return
        if behavior in {"rejected","unsupported"}:
            tx["state"]="FAILED"; tx["result"]=behavior.upper(); tx["detail"]="Simulated vehicle rejected operation"; return
        if behavior=="disconnect":
            tx["state"]="CANCELLED"; tx["detail"]="Simulated connection lost during operation"
            with self._control_lock:self._control_link_lost_until=time.monotonic()+6
            self.state.set_connection("reconnecting","Simulated link interruption","simulation"); return
        kind=tx["kind"]; params=tx["params"]
        if kind=="arm": self._armed=True
        elif kind=="disarm": self._armed=False
        elif kind=="mode": self._mode=str(params.get("mode","GUIDED"))
        elif kind=="parameter_set":
            name=str(params.get("name","")); value=params.get("value")
            if name not in self._sim_parameters: tx["state"]="FAILED"; tx["detail"]="Unknown simulated parameter"; return
            self._sim_parameters[name]=value
        elif kind=="mission_upload":
            if behavior=="rejected": tx["state"]="FAILED"; tx["detail"]="Simulated mission rejected"; return
            self._sim_mission=list(params.get("items",[]))
        elif kind=="mission_clear": self._sim_mission=[]
        elif kind=="mission_download": tx["items"]=[dict(x) for x in self._sim_mission]
        elif kind=="parameter_download": tx["parameters"]=dict(self._sim_parameters)
        tx["state"]="SUCCEEDED"; tx["result"]="ACCEPTED"

    def _run(self) -> None:
        self.state.set_connection("connected", f"Simulated · {SCENARIOS[self._scenario]}", "simulation")
        started = time.monotonic()
        step = 0
        ekf_emitted = False
        while not self._stop.wait(0.2 / self._speed):
            elapsed = (time.monotonic() - started) * self._speed
            phase = elapsed * 0.22
            scenario = self._scenario
            roll = 18 * math.sin(phase)
            pitch = 9 * math.sin(phase * 0.67 + 0.4)
            yaw = (elapsed * 11) % 360
            with self._control_lock: control_link_ok=time.monotonic()>=self._control_link_lost_until
            heartbeat_ok = not (scenario == "heartbeat_loss" and elapsed > 8) and control_link_ok
            stamp = now_iso()
            def emit(kind: str, **fields: Any) -> None:
                self.normalizer.ingest(SimulatedMessage(kind, fields))
            if heartbeat_ok and step % 5 == 0:
                with self._control_lock: armed=self._armed; mode=self._mode
                custom={"STABILIZE":0,"ALT_HOLD":2,"AUTO":3,"GUIDED":4,"LOITER":5,"RTL":6}.get(mode,4)
                emit("HEARTBEAT", base_mode=128 if armed else 0, custom_mode=custom, type=3, autopilot=3, system_status=4, mavlink_version=3)
                self.state.set_connection("connected", f"Simulated · {SCENARIOS[self._scenario]}", "simulation")
            alt = 32 + 7 * math.sin(phase * .34)
            vertical_speed = 7 * .34 * .22 * math.cos(phase * .34)
            ground_speed = 12 + 2 * math.sin(phase)
            # Deterministic demo-only coordinates. The vehicle status is explicitly simulated.
            lat = 37.4219999 + 0.00042 * math.cos(phase * .28)
            lon = -122.0840575 + 0.00055 * math.sin(phase * .28)
            fix = 2 if scenario == "gps_degradation" and elapsed > 12 else 3
            sats = 5 if fix == 2 else 14
            voltage = (15.5 - min(1.1, elapsed / 150))
            if scenario == "battery_sag" and elapsed > 10:
                voltage -= 1.8 * min(1, (elapsed - 10) / 5)
            current = 16.5 + 4 * abs(math.sin(phase)) + (14 if scenario == "battery_sag" and elapsed > 10 else 0)
            remaining = max(8, int(93 - elapsed / 10))
            vib = (38 if scenario == "elevated_vibration" and elapsed > 8 else 8 + abs(math.sin(phase)) * 2)
            rssi = 0 if scenario == "rc_degradation" and elapsed > 10 else max(50, 98 - int(elapsed / 3))
            emit("ATTITUDE", roll=math.radians(roll), pitch=math.radians(pitch), yaw=math.radians(yaw),
                 rollspeed=math.radians(18 * math.cos(phase) * .22),
                 pitchspeed=math.radians(9 * .67 * math.cos(phase * .67 + .4) * .22), yawspeed=math.radians(11))
            emit("GLOBAL_POSITION_INT", lat=int(lat*1e7), lon=int(lon*1e7), alt=int((128+alt)*1000),
                 relative_alt=int(alt*1000), vx=int(ground_speed*math.cos(math.radians(yaw))*100),
                 vy=int(ground_speed*math.sin(math.radians(yaw))*100), vz=int(-vertical_speed*100), hdg=int(yaw*100))
            emit("GPS_RAW_INT", fix_type=fix, satellites_visible=sats, eph=int((3.8 if fix==3 else 9.6)*100), epv=520)
            emit("SYS_STATUS", voltage_battery=int(voltage*1000), current_battery=int(current*100),
                 battery_remaining=remaining, load=240, onboard_control_sensors_present=0x3fffffff,
                 onboard_control_sensors_enabled=0x3fffffff, onboard_control_sensors_health=0x3fffffff)
            emit("VIBRATION", vibration_x=vib, vibration_y=vib*.8, vibration_z=vib*1.2,
                 clipping_0=int(elapsed) if scenario=="elevated_vibration" and elapsed>8 else 0, clipping_1=0, clipping_2=0)
            channels=[1500+int(150*math.sin(phase)),1500+int(80*math.cos(phase)),1500,1600]
            emit("RC_CHANNELS", chancount=4, chan1_raw=channels[0],chan2_raw=channels[1],chan3_raw=channels[2],chan4_raw=channels[3],rssi=rssi)
            outputs=[1460+int(130*math.sin(phase+j*.3)) for j in range(4)]
            emit("SERVO_OUTPUT_RAW", port=0, **{f"servo{i+1}_raw":outputs[i] for i in range(4)})
            est_ok = scenario != "ekf_warning" or elapsed < 12
            emit("ESTIMATOR_STATUS", flags=0x1ff if est_ok else 0x1ef, vel_ratio=.2 if est_ok else 1.8,
                 pos_horiz_ratio=.18, pos_vert_ratio=.13, mag_ratio=.16, hagl_ratio=.0)
            if scenario == "ekf_warning" and elapsed >= 12 and not ekf_emitted:
                emit("STATUSTEXT", severity=4, text=b"SIMULATED: EKF warning: velocity estimate degraded")
                ekf_emitted = True
            step += 1
            self._process_control()
