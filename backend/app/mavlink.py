from __future__ import annotations

import ipaddress
import re
import threading
import time
from typing import Any

from .events import EventEngine
from .state import VehicleState
from .telemetry import TelemetryNormalizer


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
        return port, f"Serial · {baud} baud"
    if kind == "tcp":
        host = str(config.get("host", "")).strip()
        port = int(config.get("port", 5760))
        if not host or len(host) > 253 or not re.fullmatch(r"[a-zA-Z0-9.:-]+", host):
            raise ValueError("Enter a valid TCP host or IP address")
        if not (1 <= port <= 65535):
            raise ValueError("TCP port must be between 1 and 65535")
        return f"tcp:{host}:{port}", f"TCP · {host}:{port}"
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

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {"running": self._thread is not None and self._thread.is_alive(),
                    "transport": self._transport, "kind": (self._config or {}).get("kind")}

    def connect(self, config: dict[str, Any]) -> dict[str, Any]:
        uri, label = validate_connection(config)
        self.disconnect()
        try:
            import pymavlink  # noqa: F401
        except ImportError as exc:
            raise RuntimeError("pymavlink is unavailable. Install backend optional dependency 'mavlink'.") from exc
        with self._lock:
            self._uri, self._transport = uri, label
            self._config = dict(config)
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
                    dialect="ardupilotmega", source_system=255, source_component=190)
                with self._lock:
                    self._connection = connection
                first_deadline = time.monotonic() + 15
                seen_heartbeat = False
                target_system: int | None = None
                while not self._stop.is_set():
                    msg = connection.recv_match(blocking=True, timeout=0.5)
                    if msg is not None:
                        kind = msg.get_type()
                        source_system = msg.get_srcSystem() if hasattr(msg, "get_srcSystem") else None
                        if kind == "HEARTBEAT" and target_system is None:
                            target_system = source_system
                        elif target_system is not None and source_system not in (None, target_system):
                            continue
                        self.normalizer.ingest(msg)
                        if kind == "HEARTBEAT":
                            seen_heartbeat = True
                            retry = 0
                            self.state.set_connection("connected", f"Connected · {self._transport}", self._transport)
                            expected = str((self._config or {}).get("expected_autopilot", "")).strip()
                            actual = self.state.snapshot()["vehicle"]["autopilot"].get("value")
                            if expected and actual and expected.lower() != actual.lower():
                                self.state.update_vehicle(identity_warning={"expected": expected, "actual": actual})
                            else:
                                self.state.update_vehicle(identity_warning=None)
                    self.events.evaluate(self.state.snapshot())
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
                if connection is not None:
                    try:
                        connection.close()
                    except Exception:
                        pass
                with self._lock:
                    if self._connection is connection:
                        self._connection = None
        if self._stop.is_set():
            self.state.set_connection("disconnected", "Disconnected")
