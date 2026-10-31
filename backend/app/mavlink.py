from __future__ import annotations

import ipaddress
import math
import re
import threading
import time
import uuid
from queue import Queue, Empty
from typing import Any

from .events import EventEngine
from .state import VehicleState
from .telemetry import TelemetryNormalizer
from .transactions import TransactionManager
from .parameters import (ParameterStore, validate_ext_parameter_value, validate_parameter_value,
                         parse_ext_parameter_value)
from .missions import MissionManager, validate_mission
from .firmware import parameter_encoding, encode_parameter, decode_parameter
from .workflows import Workflows

class OperationQueue(Queue):
    """Tag mutable protocol operations so cancellation invalidates queued writes."""
    def __init__(self, manager): super().__init__(maxsize=1024); self.manager=manager
    def put(self, item, block=True, timeout=None):
        session,payload=item
        if isinstance(payload,dict):
            payload=dict(payload); kind=payload.get('kind','')
            if kind.startswith('param_'): payload['_generation']=self.manager.parameters.generation
            if kind.startswith('mission_'): payload['_operation']=self.manager.missions.snapshot()['mission_id']
        # Never stall the authoritative reader on an exhausted write queue.
        return super().put((session,payload),block=False)


def validate_connection(config: dict[str, Any]) -> tuple[str, str]:
    kind = config.get("kind")
    if kind == "udp_listener":
        address = str(config.get("bind_address", "0.0.0.0")).strip()
        port = int(config.get("port", 14550))
        if not (1 <= port <= 65535):
            raise ValueError("UDP port must be between 1 and 65535")
        if address not in ("0.0.0.0", "::", "localhost"):
            try:
                ipaddress.ip_address(address)
            except ValueError as exc:
                raise ValueError("UDP bind address must be an IP address or localhost") from exc
        return f"udpin:{address}:{port}", "UDP listener"
    if kind == "serial":
        port = str(config.get("port", "")).strip()
        if not re.fullmatch(r"(?i:COM\d+)|/dev/(tty(USB|ACM|S|AMA)[\w.-]*|serial/by-id/[\w.+-]+)", port):
            raise ValueError("Enter a valid serial port, such as COM4 or /dev/ttyUSB0")
        baud = int(config.get("baud", 115200))
        if baud not in {9600, 19200, 38400, 57600, 115200, 230400, 460800, 921600}:
            raise ValueError("Unsupported serial baud rate")
        return port, f"Serial Â· {baud} baud"
    if kind == "tcp":
        host = str(config.get("host", "")).strip()
        port = int(config.get("port", 5760))
        if not host or len(host) > 253 or not re.fullmatch(r"[a-zA-Z0-9.:-]+", host):
            raise ValueError("Enter a valid TCP host or IP address")
        if not (1 <= port <= 65535):
            raise ValueError("TCP port must be between 1 and 65535")
        return f"tcp:{host}:{port}", f"TCP Â· {host}:{port}"
    raise ValueError("Connection kind must be udp_listener, serial, or tcp")


class MavlinkManager:
    """One authoritative pymavlink reader, shared by every browser client."""

    def __init__(self, state: VehicleState, events: EventEngine, normalizer: TelemetryNormalizer) -> None:
        self.state, self.events, self.normalizer = state, events, normalizer
        self._lock = threading.RLock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._connection: Any = None
        self._config: dict[str, Any] | None = None
        self._uri = ""
        self._transport = ""
        self._dialect = "ardupilotmega"
        self.transactions = TransactionManager()
        self._session_id: str | None = None
        self._write_lock = threading.Lock()
        self._outbound: Queue[tuple[str, Any]] = OperationQueue(self)
        self._writer_stop = threading.Event()
        self._writer_thread: threading.Thread | None = None
        self._capabilities: dict[str, Any] = {"available": False, "modes": [], "reason": "No identified vehicle"}
        self._version_requested_session: str | None = None
        self._mode_request: dict[str, Any] | None = None
        self._arm_request: dict[str, Any] | None = None
        self.parameters = ParameterStore()
        self.missions = MissionManager()
        self._selected_target: tuple[int, int] | None = None
        self.workflows=Workflows(self)

    def status(self) -> dict[str, Any]:
        with self._lock:
            result = {"running": self._thread is not None and self._thread.is_alive(),
                      "transport": self._transport, "kind": (self._config or {}).get("kind"),
                      "dialect": self._dialect,
                      "session_id": self._session_id, "pending_transactions": sum(
                          t["state"] in {"QUEUED", "WAITING_ACK", "IN_PROGRESS"} for t in self.transactions.list())}
            result['environment']=(self._config or {}).get('environment','physical')
        return result

    def connect(self, config: dict[str, Any]) -> dict[str, Any]:
        uri, label = validate_connection(config)
        dialect=str(config.get("dialect","ardupilotmega"))
        if dialect not in {"common","ardupilotmega"}: raise ValueError("MAVLink dialect must be common or ardupilotmega")
        system, component = int(config.get("target_system", 1)), int(config.get("target_component", 1))
        if not 1 <= system <= 254 or not 1 <= component <= 255:
            raise ValueError("Select target system 1–254 and component 1–255")
        self.disconnect()
        try:
            import pymavlink  # noqa: F401
        except ImportError as exc:
            raise RuntimeError("pymavlink is unavailable. Install backend optional dependency 'mavlink'.") from exc
        with self._lock:
            self._uri, self._transport = uri, label
            self._dialect=dialect
            self._capabilities = {"available": False, "modes": [], "reason": "Waiting for heartbeat"}
            self._version_requested_session = None
            self._session_id = str(uuid.uuid4())
            self.transactions.begin_session(self._session_id)
            self._config = dict(config)
            self._selected_target = (system, component)
            self._stop.clear()
            self.normalizer.source = "MAVLink"
            self.normalizer.reset_session()
            self.state.reset()
            self.state.set_connection("connecting", f"Opening {label}", label)
            self._thread = threading.Thread(target=self._reader, name="aeroscope-mavlink-reader", daemon=True)
            self._thread.start()
        return self.status()

    def disconnect(self) -> None:
        self._stop.set()
        with self._lock:
            connection = self._connection
            thread = self._thread
            session_id = self._session_id
        if session_id:
            self.transactions.cancel_session(session_id)
            self.parameters.cancel("Disconnected; pending write outcomes may be unknown")
            self.missions.cancel()
        while True:
            try: self._outbound.get_nowait(); self._outbound.task_done()
            except Empty: break
        if connection is not None:
            try:
                connection.close()
            except Exception:
                pass
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=3)
        with self._lock:
            self._connection = None
            self._thread = None
            self._session_id = None
        self.state.set_connection("disconnected", "Disconnected")

    def _reader(self) -> None:
        try:
            from pymavlink import mavutil
        except ImportError as exc:
            self.state.set_connection("error", "MAVLink support unavailable", self._transport, str(exc))
            return
        retry = 0
        while not self._stop.is_set():
            connection = None
            try:
                self.state.set_connection("connecting" if retry == 0 else "reconnecting",
                    "Waiting for vehicle heartbeat" if retry == 0 else f"Reconnect attempt {retry}", self._transport)
                connection = mavutil.mavlink_connection(self._uri, autoreconnect=False, robust_parsing=True,
                    dialect=self._dialect, source_system=255, source_component=190)
                with self._lock:
                    self._connection = connection
                    # A new transport after reconnect is a new control session.
                    self._session_id = str(uuid.uuid4())
                    self.transactions.begin_session(self._session_id)
                    self.parameters.begin_session(self._session_id)
                    self.missions.begin_session(self._session_id)
                    self._capabilities = {"available": False, "modes": [], "reason": "Waiting for heartbeat"}
                    self._version_requested_session = None
                    self._mode_request = None
                    self._arm_request = None
                    writer_session = self._session_id
                self._writer_stop.clear()
                self._writer_thread = threading.Thread(target=self._writer, args=(connection, writer_session),
                    name="aeroscope-mavlink-writer", daemon=True)
                self._writer_thread.start()
                self.normalizer.reset_session()
                self.state.reset()
                first_deadline = time.monotonic() + 15
                seen_heartbeat = False
                next_gcs_heartbeat=0.0
                target_system: int | None = None
                while not self._stop.is_set():
                    with self._write_lock:
                        msg = connection.recv_match(blocking=True, timeout=0.5)
                    if msg is not None:
                        kind = msg.get_type()
                        source_system = msg.get_srcSystem() if hasattr(msg, "get_srcSystem") else None
                        source_component = msg.get_srcComponent()
                        if self._selected_target and (source_system, source_component) != self._selected_target:
                            continue
                        if kind == "HEARTBEAT" and int(getattr(msg, "autopilot", 8)) == 8:
                            continue
                        if kind.startswith("MISSION_") and not self._valid_protocol_message(msg):
                            continue
                        if kind == "HEARTBEAT" and target_system is None:
                            target_system = source_system
                        elif target_system is not None and source_system not in (None, target_system):
                            continue
                        if kind == "COMMAND_ACK" and target_system is not None:
                            acknowledged=self.transactions.acknowledge(int(msg.command), int(source_system or 0),
                                int(msg.get_srcComponent() or 0), int(msg.result),
                                getattr(msg, "progress", None), getattr(msg, "result_param2", None))
                            if acknowledged and acknowledged["command"]==400:
                                self._sync_arm_status()
                            if acknowledged and acknowledged["command"]==512:
                                with self._lock:
                                    if acknowledged["state"]=="SUCCEEDED": self._capabilities["version_request_state"]="ACKNOWLEDGED"
                                    elif acknowledged["state"] in {"FAILED","UNKNOWN","CANCELLED"}:
                                        self._capabilities["version_request_state"]=acknowledged["state"]
                        if kind in {"PARAM_VALUE","PARAM_EXT_VALUE","PARAM_EXT_ACK"}:
                            self._ingest_parameter_message(msg,kind)
                        if kind == "MISSION_COUNT" and self.missions.snapshot()["state"] == "DOWNLOADING":
                            if self.missions.count(int(msg.count)):
                                self._outbound.put((self._session_id,{"kind":"mission_request","seq":0} if int(msg.count)>0 else {"kind":"mission_ack","result":0}))
                        elif kind in ("MISSION_ITEM_INT", "MISSION_ITEM") and self.missions.snapshot()["state"] == "DOWNLOADING":
                            incoming_frame=int(msg.frame)
                            x=int(msg.x)/1e7 if kind=="MISSION_ITEM_INT" else float(msg.x)
                            y=int(msg.y)/1e7 if kind=="MISSION_ITEM_INT" else float(msg.y)
                            frame_map={0:5,3:6}
                            item={"seq":int(msg.seq),"frame":frame_map.get(incoming_frame,incoming_frame),"command":int(msg.command),"current":int(msg.current),"autocontinue":int(msg.autocontinue),
                                  "param1":float(msg.param1),"param2":float(msg.param2),"param3":float(msg.param3),"param4":float(msg.param4),
                                  "latitude":float(x),"longitude":float(y),"altitude":float(msg.z)}
                            if self.missions.item(item):
                                snap=self.missions.snapshot()
                                if snap["state"]=="SUCCEEDED": self._outbound.put((self._session_id,{"kind":"mission_ack","result":0}))
                                elif snap["state"]=="DOWNLOADING":
                                    missing=next((i for i in range(int(snap["progress"]["total"])) if i not in snap["received_sequences"]),None)
                                    if missing is not None:self._outbound.put((self._session_id,{"kind":"mission_request","seq":missing}))
                        elif kind in ("MISSION_REQUEST_INT","MISSION_REQUEST") and self.missions.snapshot()["state"]=="UPLOADING":
                            item=self.missions.requested_item(int(msg.seq))
                            if item:self._outbound.put((self._session_id,{"kind":"mission_item","integer":kind=="MISSION_REQUEST_INT",**item}))
                        elif kind=="MISSION_ACK": self.missions.ack(int(msg.type))
                        elif kind=="MISSION_CURRENT": self.missions.update_current(int(msg.seq),getattr(msg,"mission_state",None))
                        elif kind=="MISSION_ITEM_REACHED": self.missions.item_reached(int(msg.seq))
                        self.normalizer.ingest(msg)
                        self.workflows.ingest(msg)
                        if kind=="AUTOPILOT_VERSION":
                            with self._lock:
                                self._capabilities["version_received"]=True
                                self._capabilities["version_request_state"]="RECEIVED"
                        if kind == "HEARTBEAT":
                            seen_heartbeat = True
                            retry = 0
                            try:
                                mapping = connection.mode_mapping() or {}
                                modes = sorted({str(name) for name in mapping.keys()})
                                with self._lock:
                                    self._capabilities.update({"available": bool(modes), "modes": modes,
                                        "autopilot": self.state.snapshot()["vehicle"]["autopilot"].get("value"),
                                        "reason": None if modes else "Firmware did not provide a mode mapping"})
                            except Exception as exc:
                                with self._lock:
                                    self._capabilities.update({"available": False, "modes": [],
                                        "reason": f"Mode mapping unavailable: {str(exc)[:160]}"})
                            self.state.set_connection("connected", f"Connected Â· {self._transport}", self._transport)
                            expected = str((self._config or {}).get("expected_autopilot", "")).strip()
                            actual = self.state.snapshot()["vehicle"]["autopilot"].get("value")
                            if expected and actual and expected.lower() != actual.lower():
                                self.state.update_vehicle(identity_warning={"expected": expected, "actual": actual})
                            else:
                                self.state.update_vehicle(identity_warning=None)
                            try:
                                packet=msg.get_msgbuf()
                                magic=packet[0] if packet else None
                                if magic in (0xFE,0xFD):
                                    self.state.set("vehicle","mavlink_version",2 if magic==0xFD else 1,source="MAVLink")
                            except Exception:
                                pass
                            self._request_autopilot_version_once()
                            with self._lock:
                                request = self._mode_request
                                reported_mode = self.state.snapshot()["status"]["flight_mode"].get("value")
                                if request and request["session_id"] == self._session_id and reported_mode == request["mode"]:
                                    request["state"] = "CONFIRMED"
                                    request["confirmed_at"] = time.time()
                            self._sync_arm_status()
                    expired=self.transactions.expire()
                    if any(t["command"]==512 for t in expired):
                        with self._lock:
                            if not self._capabilities.get("version_received"):
                                self._capabilities["version_request_state"]="UNKNOWN"
                    self.parameters.expire()
                    for index in self.parameters.recovery_indices():
                        self._outbound.put_nowait((self._session_id,{"kind":"param_read","name":"","index":index}))
                    retry_payload = self.missions.retry_payload()
                    if retry_payload:
                        self._outbound.put_nowait((self._session_id,retry_payload))
                    self.missions.expire()
                    with self._lock:
                        if self._mode_request and self._mode_request.get("state") == "PENDING" and time.time() - self._mode_request["requested_at"] > 8:
                            self._mode_request["state"] = "UNKNOWN"
                            self._mode_request["detail"] = "Mode change was not confirmed by heartbeat"
                        if self._arm_request and self._arm_request.get("state") == "PENDING" and time.time() - self._arm_request["requested_at"] > 10:
                            self._arm_request["state"] = "UNKNOWN"
                            self._arm_request["detail"] = "Armed state was not confirmed by heartbeat"
                    self.events.evaluate(self.state.snapshot())
                    if seen_heartbeat and time.monotonic()>=next_gcs_heartbeat:
                        self._outbound.put_nowait((self._session_id,{'kind':'gcs_heartbeat'}))
                        next_gcs_heartbeat=time.monotonic()+1
                    if not seen_heartbeat and time.monotonic() >= first_deadline:
                        raise TimeoutError("No MAVLink heartbeat received within 15 seconds")
                    hb = self.state.last_heartbeat_monotonic
                    if seen_heartbeat and hb is not None and time.monotonic() - hb > 5:
                        raise TimeoutError("MAVLink heartbeat timed out after 5 seconds")
                break
            except Exception as exc:
                if self._stop.is_set():
                    break
                retry += 1
                state = "reconnecting" if retry <= 8 else "error"
                self.state.set_connection(state, str(exc)[:220], self._transport, str(exc)[:500])
                self.events.evaluate(self.state.snapshot())
                if retry > 8:
                    break
                self._stop.wait(min(15, 0.5 * (2 ** min(retry - 1, 5))))
            finally:
                self._writer_stop.set()
                if self._writer_thread and self._writer_thread.is_alive() and self._writer_thread is not threading.current_thread():
                    self._writer_thread.join(timeout=1)
                if connection is not None:
                    try:
                        connection.close()
                    except Exception:
                        pass
                with self._lock:
                    if self._connection is connection:
                        self._connection = None
                        stale_session = self._session_id
                        self._session_id = None
                    else:
                        stale_session = None
                if stale_session:
                    self.transactions.cancel_session(stale_session, "MAVLink transport disconnected")
                    self.parameters.cancel("Transport disconnected")
                    self.missions.cancel()
                    with self._lock:
                        if self._mode_request and self._mode_request.get("session_id") == stale_session and self._mode_request.get("state") == "PENDING":
                            self._mode_request["state"] = "CANCELLED"
                            self._mode_request["detail"] = "Connection session ended"
                        if self._arm_request and self._arm_request.get("session_id") == stale_session and self._arm_request.get("state") == "PENDING":
                            self._arm_request["state"] = "CANCELLED"
                            self._arm_request["detail"] = "Connection session ended"
        if self._stop.is_set():
            self.state.set_connection("disconnected", "Disconnected")

    def queue_command(self, command: int, parameters: tuple[float, ...], *,
                      timeout: float = 5.0, safe_retry: bool = False, frame: int | None = None) -> dict[str, Any]:
        """Queue a COMMAND_LONG on the active live session; never blocks the reader for ACK."""
        snap = self.state.snapshot()
        with self._lock:
            session = self._session_id
        if not session or snap["connection"]["state"] != "connected" or snap["vehicle"].get("simulated"):
            raise RuntimeError("A connected real MAVLink vehicle is required")
        if len(parameters) != 7:
            raise ValueError("COMMAND_LONG requires exactly seven parameters")
        sys_field = snap["vehicle"].get("system_id")
        comp_field = snap["vehicle"].get("component_id")
        sysid = sys_field.get("value") if isinstance(sys_field, dict) else sys_field
        compid = comp_field.get("value") if isinstance(comp_field, dict) else comp_field
        if not sysid or not compid: raise RuntimeError("Vehicle target identity is unavailable")
        transaction = self.transactions.create(command, sysid, compid, timeout, safe_retry)
        with self._lock:
            if self._session_id != session:
                self.transactions.cancel_session(session, "Connection session changed")
                raise RuntimeError("Connection session changed")
        self._outbound.put((session, (transaction.id, command, tuple(parameters), sysid, compid) if frame is None else {'kind':'command_int','transaction_id':transaction.id,'command':command,'params':tuple(parameters),'system':sysid,'component':compid,'frame':frame}))
        return transaction.public()

    def capabilities(self) -> dict[str, Any]:
        with self._lock: result={**self._capabilities, "session_id": self._session_id}
        snapshot=self.state.snapshot(); vehicle=snapshot["vehicle"]
        protocol=vehicle.get("protocol_capabilities",{})
        flags=protocol.get("value") if isinstance(protocol,dict) and protocol.get("available") else None
        if flags is not None:
            result["protocol_flags"]={"mission_float":bool(flags&1),"param_float":bool(flags&2),
                "mission_int":bool(flags&4),"command_int":bool(flags&8),"param_bytewise":bool(flags&16),
                "mavlink2":bool(flags&8192),"mission_fence":bool(flags&16384),"mission_rally":bool(flags&32768),
                "param_c_cast":bool(flags&131072)}
        else: result["protocol_flags"]=None
        result.update({"vehicle_type":vehicle.get("type"),"autopilot":vehicle.get("autopilot"),
            "firmware":vehicle.get("firmware"),"firmware_details":vehicle.get("firmware_details"),
            "mavlink_version":vehicle.get("mavlink_version"),"system_id":vehicle.get("system_id"),
            "component_id":vehicle.get("component_id"),"heartbeat_age_s":snapshot["connection"].get("heartbeat_age_s"),
            "connection_state":snapshot["connection"].get("state"),"simulated":vehicle.get("simulated")})
        return result

    def _request_autopilot_version_once(self) -> dict[str, Any] | None:
        """Request AUTOPILOT_VERSION once in each live MAVLink session."""
        with self._lock:
            session=self._session_id
            if not session or self._version_requested_session==session: return None
            self._version_requested_session=session
        try:
            # MAV_CMD_REQUEST_MESSAGE (512), AUTOPILOT_VERSION message ID (148).
            transaction=self.queue_command(512,(148.0,0,0,0,0,0,0),timeout=4.0)
        except Exception as exc:
            with self._lock:
                self._capabilities["version_request_state"]="FAILED"
                self._capabilities["version_request_detail"]=str(exc)[:200]
            return None
        with self._lock:
            if self._session_id==session:
                self._capabilities["version_request_state"]="QUEUED"
                self._capabilities["version_request_transaction_id"]=transaction["id"]
                self._capabilities["version_request_message_id"]=148
        if self.state.snapshot()['vehicle']['autopilot'].get('value')=='ArduPilot':
            self._outbound.put((session,{'kind':'stream_rates'}))
        return transaction

    def _mission_integer_protocol(self) -> bool | None:
        field=self.state.snapshot()["vehicle"].get("protocol_capabilities",{})
        if not isinstance(field,dict) or not field.get("available"): return None
        flags=int(field["value"])
        if flags & 4: return True
        if flags & 1: return False
        return None

    def _require_mission_support(self) -> None:
        field=self.state.snapshot()["vehicle"].get("protocol_capabilities",{})
        if isinstance(field,dict) and field.get("available") and not (int(field["value"]) & (1|4)):
            raise RuntimeError("Vehicle advertised no supported MAVLink mission transfer capability")

    def mode_status(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._mode_request or {"state": "IDLE"})

    def arm_status(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._arm_request or {"state": "IDLE"})

    def _sync_arm_status(self) -> None:
        snapshot=self.state.snapshot()
        armed_field=snapshot["status"]["armed"]
        armed_now=armed_field.get("value") if isinstance(armed_field,dict) else armed_field
        with self._lock:
            request=self._arm_request
            if not request or request.get("state")!="PENDING" or request.get("session_id")!=self._session_id:
                return
            transaction=next((t for t in self.transactions.list() if t["id"]==request.get("transaction_id")),None)
            if not transaction:
                return
            if transaction["state"] in {"FAILED","UNKNOWN","CANCELLED"}:
                request["state"]=transaction["state"]
                request["detail"]=transaction.get("detail") or transaction.get("result")
            elif (transaction["state"]=="SUCCEEDED" and armed_field.get("available")
                  and armed_now is request["armed"]):
                request["state"]="CONFIRMED"
                request["confirmed_at"]=time.time()

    def request_arm(self, armed: bool, *, confirmation: str, acknowledge_unknown: bool = False) -> dict[str, Any]:
        if not isinstance(armed, bool): raise ValueError("armed must be a boolean")
        expected = "ARM" if armed else "DISARM"
        if confirmation != expected: raise ValueError(f"Type {expected} to confirm this operation")
        snapshot = self.state.snapshot(); conn = snapshot["connection"]
        with self._lock: session = self._session_id
        if snapshot["vehicle"].get("simulated"): raise RuntimeError("Arming controls are disabled for simulated vehicles")
        if not session or conn["state"] != "connected" or conn.get("heartbeat_age_s") is None or conn["heartbeat_age_s"] > 2:
            raise RuntimeError("A fresh real-vehicle heartbeat is required")
        if not snapshot["vehicle"].get("system_id", {}).get("available") or not snapshot["vehicle"].get("component_id", {}).get("available"):
            raise RuntimeError("Vehicle identity is unavailable")
        armed_field=snapshot["status"]["armed"]
        if armed_field.get("available") and armed_field.get("value") is armed:
            raise ValueError("Vehicle is already in the requested armed state")
        safety = [snapshot["gps"]["fix_type"], snapshot["battery"]["voltage_v"], snapshot["status"]["estimator"]]
        unknown = [name for name, item in zip(("GPS fix", "battery voltage", "estimator health"), safety)
                   if not isinstance(item, dict) or not item.get("available")]
        if unknown and not acknowledge_unknown:
            raise ValueError("Safety data unavailable: " + ", ".join(unknown) + "; explicitly acknowledge unknowns")
        with self._lock:
            if self._arm_request and self._arm_request.get("state") == "PENDING":
                raise RuntimeError("An arm/disarm request is already pending")
        # MAV_CMD_COMPONENT_ARM_DISARM; param2=0 preserves normal autopilot checks.
        tx = self.queue_command(400, (1.0 if armed else 0.0, 0.0, 0, 0, 0, 0, 0), timeout=5)
        request = {"transaction_id": tx["id"], "session_id": session, "armed": armed,
                   "requested_at": time.time(), "state": "PENDING", "unknown_safety": unknown}
        with self._lock: self._arm_request = request
        return dict(request)

    def request_parameters(self) -> None:
        session=self._active_control_session(); self.parameters.request_all()
        self._outbound.put((session,{"kind":"param_list"}))

    def refresh_parameter(self, name: str) -> None:
        name=self._validate_param_name(name); session=self._active_control_session()
        current=next((p for p in self.parameters.snapshot()["parameters"] if p["name"]==name),None)
        protocol="param_ext" if current and int(current["type"]) in {7,8,10,11} else None
        self.parameters.request_one(name,protocol)
        if protocol=="param_ext": self._outbound.put((session,{"kind":"param_ext_read","name":name}))
        else: self._outbound.put((session,{"kind":"param_read","name":name}))

    def set_parameter(self, name: str, value: Any, *, confirmed: bool = False) -> None:
        name=self._validate_param_name(name); session=self._active_control_session()
        current=next((p for p in self.parameters.snapshot()["parameters"] if p["name"]==name),None)
        if not current: raise ValueError("Parameter is not in the current vehicle cache; refresh it first")
        param_type=int(current["type"])
        extended=param_type in {7,8,10,11}
        if extended: raise ValueError("PARAM_EXT is not qualified for this firmware; write unavailable")
        converted=validate_ext_parameter_value(value,param_type) if extended else validate_parameter_value(value,param_type)
        from .metadata import parameter_metadata, validate_metadata
        validate_metadata(converted,parameter_metadata(self.state.snapshot()['vehicle']).get(name,{}))
        wire=encode_parameter(converted,param_type,self._parameter_encoding())
        sensitive=any(x in name.upper() for x in ("FAIL", "ARM", "BATT", "GPS", "EKF", "RC", "MOT", "NAV", "WPNAV", "ATC_"))
        if sensitive and confirmed is not True: raise ValueError("This safety-related parameter requires explicit confirmation")
        protocol="param_ext" if extended else "param"
        self.parameters.pending_set(name,converted,protocol,param_type)
        kind="param_ext_set" if extended else "param_set"
        self._outbound.put((session,{"kind":kind,"name":name,"value":wire,"type":param_type}))

    def _parameter_encoding(self):
        vehicle=self.state.snapshot()['vehicle']
        return parameter_encoding(vehicle.get('autopilot',{}).get('value'),vehicle.get('protocol_capabilities',{}).get('value'))

    def _active_control_session(self) -> str:
        snap=self.state.snapshot()
        with self._lock: session=self._session_id
        if not session or snap["connection"]["state"]!="connected" or snap["vehicle"].get("simulated"):
            raise RuntimeError("A connected real MAVLink vehicle is required")
        age = snap["connection"].get("heartbeat_age_s")
        if age is None or age > 2: raise RuntimeError("A fresh target heartbeat is required")
        return session

    def _valid_protocol_message(self, msg: Any) -> bool:
        if self._selected_target and (msg.get_srcSystem(),msg.get_srcComponent()) != self._selected_target: return False
        if int(getattr(msg,"mission_type",0)) != 0: return False
        return int(getattr(msg,"target_system",255)) in (0,255) and int(getattr(msg,"target_component",190)) in (0,190)

    def _ingest_parameter_message(self, msg: Any, kind: str) -> bool:
        """Accept typed parameter responses only from the selected vehicle component."""
        snapshot=self.state.snapshot(); component=snapshot["vehicle"].get("component_id",{})
        expected=component.get("value") if isinstance(component,dict) else component
        source=msg.get_srcComponent() if hasattr(msg,"get_srcComponent") else None
        if self._selected_target and hasattr(msg,"get_srcSystem") and (msg.get_srcSystem(),source) != self._selected_target: return False
        if expected and source not in (None,expected): return False
        name=getattr(msg,"param_id",b"")
        if isinstance(name,bytes): name=name.split(b"\0",1)[0].decode("ascii","replace")
        name=str(name)
        try:
            if kind=="PARAM_VALUE":
                value=decode_parameter(float(msg.param_value),int(msg.param_type),self._parameter_encoding())
                self.parameters.ingest(name,value,int(msg.param_type),int(msg.param_index),int(msg.param_count))
            elif kind=="PARAM_EXT_VALUE":
                value=parse_ext_parameter_value(getattr(msg,"param_value",b""),int(msg.param_type))
                self.parameters.ingest(name,value,int(msg.param_type),int(msg.param_index),int(msg.param_count),protocol="param_ext")
            elif kind=="PARAM_EXT_ACK":
                value=parse_ext_parameter_value(getattr(msg,"param_value",b""),int(msg.param_type))
                self.parameters.ingest_ext_ack(name,value,int(msg.param_type),int(msg.param_result))
            else: return False
        except (TypeError,ValueError) as exc:
            self.parameters.fail_pending(name,f"Invalid {kind}: {str(exc)[:200]}")
            return False
        return True

    @staticmethod
    def _validate_param_name(name: str) -> str:
        name=str(name).strip()
        if not name or len(name)>16 or not name.isascii(): raise ValueError("MAVLink parameter name must be 1–16 ASCII characters")
        return name

    def request_mission_download(self) -> None:
        session=self._active_control_session(); self._require_mission_support(); self.missions.download_started(); self._outbound.put((session,{"kind":"mission_list"}))

    def upload_mission(self, items: list[dict[str,Any]], *, confirmed: bool = False) -> dict[str,Any]:
        if confirmed is not True: raise ValueError("Explicit mission upload confirmation is required")
        session=self._active_control_session(); self._require_mission_support(); checked=self.missions.upload_started(items)
        self._outbound.put((session,{"kind":"mission_count","count":len(checked)}))
        return self.missions.snapshot()

    def clear_mission(self, *, confirmed: bool = False) -> dict[str,Any]:
        if confirmed is not True: raise ValueError("Explicit mission clear confirmation is required")
        session=self._active_control_session(); self._require_mission_support(); self.missions.clear_started(); self._outbound.put((session,{"kind":"mission_clear"}))
        return self.missions.snapshot()

    def guided_reposition(self, latitude: float, longitude: float, altitude: float, speed: float = -1,
                          *, confirmation: str = "", acknowledge_unknown: bool = False) -> dict[str,Any]:
        snap=self.state.snapshot(); vehicle=snap["vehicle"]; mode=snap["status"]["flight_mode"]
        if confirmation!="GO": raise ValueError("Type GO to confirm the guided reposition")
        if vehicle.get("simulated"): raise RuntimeError("Guided operations are disabled for simulation")
        if vehicle.get("autopilot",{}).get("value")!="ArduPilot": raise RuntimeError("Guided reposition is only enabled for identified ArduPilot vehicles")
        if not mode.get("available") or mode.get("value")!="GUIDED": raise RuntimeError("Vehicle must report GUIDED mode")
        conn=snap["connection"]
        if conn["state"]!="connected" or conn.get("heartbeat_age_s") is None or conn["heartbeat_age_s"]>2: raise RuntimeError("A fresh heartbeat is required")
        lat=float(latitude); lon=float(longitude); alt=float(altitude); speed=float(speed)
        if not math.isfinite(lat) or not -90<=lat<=90: raise ValueError("Latitude must be between -90 and 90")
        if not math.isfinite(lon) or not -180<=lon<=180: raise ValueError("Longitude must be between -180 and 180")
        if not math.isfinite(alt) or not -1000<=alt<=100000: raise ValueError("Altitude is outside supported validation range")
        if not math.isfinite(speed) or speed!=-1 and not 0.1<=speed<=100: raise ValueError("Speed must be -1 (vehicle default) or 0.1–100 m/s")
        gps=snap["gps"]["fix_type"]
        if gps.get("available") and int(gps["value"])<3: raise RuntimeError("Guided reposition requires GPS fix type 3 or better")
        missing=[name for name,item in (("GPS fix",gps),("battery voltage",snap["battery"]["voltage_v"]),("estimator health",snap["status"]["estimator"])) if not item.get("available")]
        if missing and not acknowledge_unknown: raise ValueError("Safety data unavailable: "+", ".join(missing)+"; explicitly acknowledge unknowns")
        # MAV_CMD_DO_REPOSITION (192), only in explicitly supported ArduPilot GUIDED modes.
        self.workflows.copter()
        return self.queue_command(192,(speed,0.0,0.0,0.0,lat,lon,alt),timeout=8,frame=6)

    def guided_takeoff(self, altitude: float, *, confirmation: str = "", acknowledge_unknown: bool = False) -> dict[str,Any]:
        snap=self.state.snapshot()
        if confirmation!="TAKEOFF": raise ValueError("Type TAKEOFF to confirm")
        self._require_ardupilot_guided(snap,acknowledge_unknown=acknowledge_unknown)
        armed=snap["status"]["armed"]
        if not armed.get("available") or armed.get("value") is not True: raise RuntimeError("Vehicle must already be confirmed armed; AeroScope will not arm automatically")
        altitude=float(altitude)
        if not math.isfinite(altitude) or not 1<=altitude<=120: raise ValueError("Takeoff altitude must be between 1 and 120 m")
        return self.queue_command(22,(0.0,0.0,0.0,0.0,0.0,0.0,altitude),timeout=10)

    def guided_land(self, *, confirmation: str = "", acknowledge_unknown: bool = False) -> dict[str,Any]:
        snap=self.state.snapshot()
        if confirmation!="LAND": raise ValueError("Type LAND to confirm")
        self._require_ardupilot_guided(snap,acknowledge_unknown=acknowledge_unknown)
        armed=snap["status"]["armed"]
        if not armed.get("available") or armed.get("value") is not True: raise RuntimeError("Vehicle is not confirmed armed")
        return self.queue_command(21,(0.0,0.0,0.0,0.0,0.0,0.0,0.0),timeout=10)

    def guided_speed(self, speed: float, *, confirmation: str = "", acknowledge_unknown: bool = False) -> dict[str,Any]:
        snap=self.state.snapshot()
        if confirmation!="SPEED": raise ValueError("Type SPEED to confirm")
        self._require_ardupilot_guided(snap,acknowledge_unknown=acknowledge_unknown)
        speed=float(speed)
        if not math.isfinite(speed) or not 0.5<=speed<=30: raise ValueError("Speed must be between 0.5 and 30 m/s")
        return self.queue_command(178,(1.0,speed,-1.0,0.0,0.0,0.0,0.0),timeout=8)

    @staticmethod
    def _require_ardupilot_guided(snapshot: dict[str,Any], *, acknowledge_unknown: bool = False) -> None:
        vehicle=snapshot["vehicle"]; mode=snapshot["status"]["flight_mode"]; conn=snapshot["connection"]
        if vehicle.get("simulated"): raise RuntimeError("Guided operations are disabled for simulation")
        if vehicle.get("autopilot",{}).get("value")!="ArduPilot": raise RuntimeError("Operation is enabled only for identified ArduPilot")
        if not mode.get("available") or mode.get("value")!="GUIDED": raise RuntimeError("Vehicle must report GUIDED mode")
        if conn["state"]!="connected" or conn.get("heartbeat_age_s") is None or conn["heartbeat_age_s"]>2: raise RuntimeError("A fresh heartbeat is required")
        gps=snapshot["gps"]["fix_type"]
        if gps.get("available") and int(gps["value"])<3: raise RuntimeError("Guided operation requires GPS fix type 3 or better")
        missing=[name for name,item in (("GPS fix",gps),("battery voltage",snapshot["battery"]["voltage_v"]),("estimator health",snapshot["status"]["estimator"])) if not item.get("available")]
        if missing and not acknowledge_unknown: raise ValueError("Safety data unavailable: "+", ".join(missing)+"; explicitly acknowledge unknowns")

    def request_mode(self, mode: str, *, confirmed: bool = False) -> dict[str, Any]:
        if confirmed is not True:
            raise ValueError("Explicit operator confirmation is required")
        snapshot = self.state.snapshot()
        connection = snapshot["connection"]
        with self._lock:
            session, capabilities = self._session_id, dict(self._capabilities)
        if snapshot["vehicle"].get("simulated"):
            raise RuntimeError("Mode changes are unavailable for simulated vehicles")
        if not session or connection["state"] != "connected" or connection.get("heartbeat_age_s") is None or connection["heartbeat_age_s"] > 2:
            raise RuntimeError("A fresh connected vehicle heartbeat is required")
        if not capabilities.get("available") or mode not in capabilities.get("modes", []):
            raise ValueError("Requested mode is not available from the connected firmware mapping")
        with self._lock:
            if self._mode_request and self._mode_request.get("state") == "PENDING":
                raise RuntimeError("A flight-mode request is already pending")
            request = {"request_id": str(uuid.uuid4()), "session_id": session, "mode": mode,
                       "requested_at": time.time(), "state": "PENDING", "detail": None}
            self._mode_request = request
        self._outbound.put((session, {"kind": "mode", "request_id": request["request_id"], "mode": mode}))
        return dict(request)

    def _writer(self, connection: Any, session: str) -> None:
        while not self._writer_stop.is_set() and not self._stop.is_set():
            try: queued_session, payload = self._outbound.get(timeout=0.1)
            except Empty: continue
            txid: str | None = None
            try:
                with self._lock:
                    current, target = self._session_id, self._connection
                    system_field = self.state.snapshot()["vehicle"].get("system_id")
                    system = system_field.get("value") if isinstance(system_field, dict) else system_field
                if queued_session != session or session != current or connection is not target or not system:
                    self.transactions.cancel_session(queued_session, "Stale or unconfirmed vehicle session")
                    continue
                if isinstance(payload, dict) and payload.get("kind") == "mode":
                    with self._write_lock:
                        connection.set_mode(payload["mode"])
                    continue
                if isinstance(payload,dict) and payload.get('kind') in {'gcs_heartbeat','stream_rates'}:
                    with self._write_lock:
                        if payload['kind']=='gcs_heartbeat': connection.mav.heartbeat_send(6,8,0,0,4,3)
                        else: connection.mav.request_data_stream_send(int(system),int(self.state.snapshot()['vehicle']['component_id']['value']),0,4,1)
                    continue
                if isinstance(payload,dict) and payload.get('kind')=='command_int':
                    txid=payload['transaction_id']; params=payload['params']
                    self.transactions.mark_sent(txid)
                    with self._write_lock:
                        if self._session_id!=session or self._connection is not connection: continue
                        connection.mav.command_int_send(payload['system'],payload['component'],payload['frame'],payload['command'],0,0,*params[:4],round(params[4]*1e7),round(params[5]*1e7),params[6])
                    continue
                if isinstance(payload, dict) and payload.get("kind", "").startswith("param_"):
                    if payload.get('_generation')!=self.parameters.generation: continue
                    system_field=self.state.snapshot()["vehicle"].get("system_id"); component_field=self.state.snapshot()["vehicle"].get("component_id")
                    sysid=system_field.get("value") if isinstance(system_field,dict) else system_field
                    compid=component_field.get("value") if isinstance(component_field,dict) else component_field
                    if not sysid or not compid: raise RuntimeError("Vehicle target identity unavailable")
                    with self._write_lock:
                        if payload["kind"]=="param_list": connection.mav.param_request_list_send(sysid,compid)
                        elif payload["kind"]=="param_read": connection.mav.param_request_read_send(sysid,compid,payload["name"].encode().ljust(16,b"\0"),payload.get("index",-1))
                        elif payload["kind"]=="param_set": connection.mav.param_set_send(sysid,compid,payload["name"].encode().ljust(16,b"\0"),float(payload["value"]),payload["type"])
                        elif payload["kind"]=="param_ext_list": connection.mav.param_ext_request_list_send(sysid,compid)
                        elif payload["kind"]=="param_ext_read": connection.mav.param_ext_request_read_send(sysid,compid,payload["name"].encode().ljust(16,b"\0"),-1)
                        elif payload["kind"]=="param_ext_set":
                            value=str(payload["value"]).encode("utf-8")
                            connection.mav.param_ext_set_send(sysid,compid,payload["name"].encode().ljust(16,b"\0"),value.ljust(128,b"\0"),payload["type"])
                    continue
                if isinstance(payload, dict) and payload.get("kind", "").startswith("mission_"):
                    mission=self.missions.snapshot()
                    if payload.get('_operation')!=mission['mission_id'] or mission['state']=='CANCELLED': continue
                    sf=self.state.snapshot()["vehicle"]; sysf=sf.get("system_id"); cpf=sf.get("component_id")
                    sysid=sysf.get("value") if isinstance(sysf,dict) else sysf; compid=cpf.get("value") if isinstance(cpf,dict) else cpf
                    if not sysid or not compid: raise RuntimeError("Vehicle target identity unavailable")
                    with self._write_lock:
                        k=payload["kind"]
                        if k=="mission_list": connection.mav.mission_request_list_send(sysid,compid,0)
                        elif k=="mission_request":
                            integer=self._mission_integer_protocol()
                            if integer is not False: connection.mav.mission_request_int_send(sysid,compid,payload["seq"],0)
                            else: connection.mav.mission_request_send(sysid,compid,payload["seq"],0)
                        elif k=="mission_count": connection.mav.mission_count_send(sysid,compid,payload["count"],0)
                        elif k=="mission_clear": connection.mav.mission_clear_all_send(sysid,compid,0)
                        elif k=="mission_ack": connection.mav.mission_ack_send(sysid,compid,payload["result"],0)
                        elif k=="mission_item":
                            item=payload
                            if item.get("integer",True):
                                connection.mav.mission_item_int_send(sysid,compid,item["seq"],item["frame"],item["command"],item["current"],item["autocontinue"],item["param1"],item["param2"],item["param3"],item["param4"],round(item["latitude"]*1e7),round(item["longitude"]*1e7),item["altitude"],0)
                            else:
                                frame={5:0,6:3}.get(item["frame"],item["frame"])
                                connection.mav.mission_item_send(sysid,compid,item["seq"],frame,item["command"],item["current"],item["autocontinue"],item["param1"],item["param2"],item["param3"],item["param4"],item["latitude"],item["longitude"],item["altitude"],0)
                    continue
                txid, command, params, sysid, compid = payload
                if sysid != system:
                    self.transactions.cancel_session(queued_session, "Vehicle target changed")
                    continue
                # Register before the write: an ACK can be received immediately
                # after bytes reach the transport, before send() returns.
                self.transactions.mark_sent(txid)
                with self._write_lock:
                    if self._session_id!=session or self._connection is not connection: continue
                    connection.mav.command_long_send(sysid, compid, command, 0, *params)
            except Exception as exc:
                if txid is not None:
                    self.transactions.transmission_failed(txid, str(exc))
                elif isinstance(payload, dict):
                    kind=payload.get("kind")
                    if kind == "mode":
                        with self._lock:
                            request=self._mode_request
                            if request and request.get("request_id")==payload.get("request_id") and request.get("state")=="PENDING":
                                request.update(state="FAILED",detail=f"Mode transmission failed: {str(exc)[:200]}")
                    elif kind in {"param_list","param_read","param_set","param_ext_list","param_ext_read","param_ext_set"}:
                        if kind == "param_list": self.parameters.fail_download(f"Parameter request transmission failed: {str(exc)[:200]}")
                        elif kind == "param_ext_list": self.parameters.fail_download(f"Extended parameter request transmission failed: {str(exc)[:200]}")
                        else: self.parameters.fail_pending(payload.get("name", ""),f"Parameter transmission failed: {str(exc)[:200]}")
                    elif isinstance(kind,str) and kind.startswith("mission_"):
                        self.missions.fail(f"Mission protocol transmission failed: {str(exc)[:200]}")
            finally:
                self._outbound.task_done()
