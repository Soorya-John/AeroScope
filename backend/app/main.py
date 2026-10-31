from __future__ import annotations

import base64
import hashlib
import json
import math
import logging
import os
import select
import socket
import struct
import threading
import time
from email import policy
from email.parser import BytesParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from .config import HOST, LOG_DIR, MAX_UPLOAD_BYTES, PORT, ROOT
from .events import EventEngine
from .log_analysis import LogAnalyzer, safe_log_path
from .mavlink import MavlinkManager
from .recorder import TelemetryRecorder
from .simulator import SCENARIOS, Simulator
from .state import VehicleState
from .store import Store, now_iso
from .telemetry import TelemetryNormalizer
from .workflows import GET_ROUTES, POST_ROUTES, vehicle_mutation

logging.basicConfig(level=os.environ.get("AEROSCOPE_LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("aeroscope")

store = Store()
vehicle_state = VehicleState()
events = EventEngine(store)
normalizer = TelemetryNormalizer(vehicle_state, events)
simulator = Simulator(vehicle_state, events, normalizer)
mavlink = MavlinkManager(vehicle_state, events, normalizer)
analyzer = LogAnalyzer(store)
recorder = TelemetryRecorder(store, vehicle_state)

STATIC_DIR = ROOT / "frontend" / "static"
STATIC_FILES = {"/": ("index.html", "text/html; charset=utf-8"),
                "/index.html": ("index.html", "text/html; charset=utf-8"),
                "/app.js": ("app.js", "text/javascript; charset=utf-8"),
                "/styles.css": ("styles.css", "text/css; charset=utf-8")}
for asset, content_type in {'workflows.js':'text/javascript','planner.js':'text/javascript','workflows.css':'text/css','vendor/leaflet.js':'text/javascript','vendor/leaflet.css':'text/css','vendor/LICENSE':'text/plain'}.items():
    STATIC_FILES['/'+asset]=(asset,content_type+'; charset=utf-8')


def _json_bytes(value: Any) -> bytes:
    def safe(item: Any) -> Any:
        if item is None or isinstance(item, (str, bool, int)):
            return item
        if isinstance(item, float):
            return item if math.isfinite(item) else None
        if isinstance(item, dict):
            return {str(k): safe(v) for k, v in item.items()}
        if isinstance(item, (list, tuple)):
            return [safe(v) for v in item]
        return str(item)
    return json.dumps(safe(value), ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _ws_frame(payload: bytes, opcode: int = 1) -> bytes:
    length = len(payload)
    prefix = bytes([0x80 | opcode])
    if length < 126:
        return prefix + bytes([length]) + payload
    if length < 65536:
        return prefix + bytes([126]) + struct.pack("!H", length) + payload
    return prefix + bytes([127]) + struct.pack("!Q", length) + payload


def _read_ws_frame(sock: socket.socket, timeout: float = 0.01) -> tuple[int, bytes] | None:
    sock.settimeout(timeout)
    try:
        head = sock.recv(2)
    except (TimeoutError, socket.timeout, BlockingIOError):
        return None
    if not head:
        return (8, b"")
    if len(head) != 2:
        return (8, b"")
    opcode = head[0] & 0x0F
    masked = bool(head[1] & 0x80)
    length = head[1] & 0x7F
    if length == 126:
        extra = sock.recv(2)
        if len(extra) != 2: return (8, b"")
        length = struct.unpack("!H", extra)[0]
    elif length == 127:
        extra = sock.recv(8)
        if len(extra) != 8: return (8, b"")
        length = struct.unpack("!Q", extra)[0]
    if length > 4096 or not masked:
        return (8, b"")
    mask = sock.recv(4) if masked else b""
    if len(mask) != 4: return (8, b"")
    parts = bytearray()
    while len(parts) < length:
        part = sock.recv(length - len(parts))
        if not part: return (8, b"")
        parts.extend(part)
    return opcode, bytes(b ^ mask[i % 4] for i, b in enumerate(parts))


class AeroScopeServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 32


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "AeroScope/0.1"
    sys_version = ""

    def handle(self) -> None:
        # Browser navigation, process shutdown, and transient networks can close
        # an idle keep-alive socket between requests. Treat that as a normal
        # client disconnect instead of printing a server traceback.
        try:
            super().handle()
        except (ConnectionResetError, BrokenPipeError, TimeoutError):
            pass

    def log_message(self, fmt: str, *args: Any) -> None:
        if self.path.startswith("/api") and "/upload" in self.path:
            logger.info("%s %s upload request", self.address_string(), self.command)
        elif not self.path.startswith("/ws/"):
            logger.info("%s %s %s", self.address_string(), self.command, self.path.split("?", 1)[0])

    def _headers(self, content_type: str = "application/json; charset=utf-8", length: int | None = None) -> None:
        self.send_header("Content-Type", content_type)
        if length is not None: self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob: https://tile.openstreetmap.org; connect-src 'self' ws: wss:; object-src 'none'; base-uri 'self'; frame-ancestors 'none'")

    def _send_json(self, status: int, payload: Any) -> None:
        body = _json_bytes(payload)
        self.send_response(status)
        self._headers(length=len(body))
        self.end_headers()
        self.wfile.write(body)

    def _send_bytes(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self._headers(content_type, len(body))
        self.end_headers()
        self.wfile.write(body)

    def _origin_ok(self) -> bool:
        origin = self.headers.get("Origin")
        if not origin:
            return True
        try:
            return urlsplit(origin).netloc.lower() == self.headers.get("Host", "").lower()
        except ValueError:
            return False

    def _read_body(self, max_size: int) -> bytes:
        try:
            size = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ValueError("Invalid Content-Length") from exc
        if size < 0 or size > max_size:
            raise ValueError(f"Request body exceeds the {max_size // (1024*1024)} MiB limit")
        body = self.rfile.read(size)
        if len(body) != size:
            raise ValueError("Incomplete request body")
        return body

    def do_OPTIONS(self) -> None:
        if not self._origin_ok():
            self._send_json(403, {"error": "Cross-origin request denied"})
            return
        self.send_response(204)
        self.send_header("Allow", "GET, POST, DELETE, OPTIONS")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self) -> None:
        url = urlsplit(self.path)
        path = url.path
        if path in {"/api/setup","/api/control/ownership","/api/parameters/batch","/api/connection/ports"}:
            try: self._send_json(200,mavlink.workflows.dispatch('GET',path,{},None))
            except (ValueError,RuntimeError,ImportError) as exc: self._send_json(409,{'error':str(exc)})
            return
        if path.startswith("/ws/"):
            self._websocket(path)
            return
        if path in STATIC_FILES:
            name, content_type = STATIC_FILES[path]
            file_path = STATIC_DIR / name
            try:
                data = file_path.read_bytes()
            except OSError:
                self._send_json(404, {"error": "Frontend files are missing"})
                return
            self._send_bytes(200, data, content_type)
            return
        query = parse_qs(url.query)
        try:
            if path == "/api/health":
                self._send_json(200, {"status": "ok", "product": "AeroScope", "version": "0.1.0", "protocol_version": 1, "time": now_iso()})
            elif path == "/api/state":
                self._send_json(200, {"protocol_version": 1, "data": vehicle_state.snapshot(), "simulation": simulator.status(), "mavlink": mavlink.status()})
            elif path == "/api/control/status":
                self._send_json(200, {"protocol_version": 1, "connection": mavlink.status(), "transactions": mavlink.transactions.list()})
            elif path == "/api/control/capabilities":
                self._send_json(200, {"protocol_version": 1, "capabilities": mavlink.capabilities()})
            elif path == "/api/control/mode":
                self._send_json(200, {"protocol_version": 1, "current": vehicle_state.snapshot()["status"]["flight_mode"], "request": mavlink.mode_status()})
            elif path == "/api/control/arm":
                self._send_json(200, {"protocol_version": 1, "armed": vehicle_state.snapshot()["status"]["armed"], "request": mavlink.arm_status()})
            elif path == "/api/parameters":
                self._send_json(200, {"protocol_version": 1, **mavlink.parameters.snapshot(str(query.get("search", [""])[0]))})
            elif path == "/api/parameters/snapshots":
                self._send_json(200,{"snapshots":store.list_parameter_snapshots()})
            elif path == "/api/parameters/compare":
                left=store.get_parameter_snapshot(str(query.get("left",[""])[0])); right=store.get_parameter_snapshot(str(query.get("right",[""])[0]))
                if not left or not right: raise ValueError("Both valid snapshot IDs are required")
                from .parameters import compare_parameter_snapshots
                self._send_json(200,{"changes":compare_parameter_snapshots(left["parameters"],right["parameters"])})
            elif path == "/api/parameters/export":
                rows=mavlink.parameters.snapshot()["parameters"]; fmt=query.get("format",["json"])[0]
                if fmt=="json": self._send_json(200,{"parameters":rows}); return
                if fmt!="csv": raise ValueError("format must be json or csv")
                out=io.StringIO(newline=""); writer=csv.DictWriter(out,fieldnames=["name","value","type","index","count","received_at"],extrasaction='ignore'); writer.writeheader(); writer.writerows(rows)
                self._send_bytes(200,out.getvalue().encode(),"text/csv; charset=utf-8")
            elif path == "/api/mission":
                self._send_json(200, {"protocol_version": 1, **mavlink.missions.snapshot()})
            elif path == "/api/simulation/control":
                self._send_json(200,{"control":simulator.control_status()})
            elif path == "/api/guided/reposition":
                self._send_json(200,{"transaction":mavlink.transactions.list()[:20]})
            elif path in ("/api/guided/takeoff","/api/guided/land","/api/guided/speed"):
                self._send_json(200,{"transactions":mavlink.transactions.list()[:20]})
            elif path == "/api/events":
                self._send_json(200, {"events": events.list_events(int(query.get("limit", ["100"])[0]))})
            elif path == "/api/inspector":
                limit = min(500, max(1, int(query.get("limit", ["200"])[0])))
                self._send_json(200, {"messages": normalizer.inspector()[:limit], "rates": vehicle_state.rate_by_type(), "counts": vehicle_state.count_by_type(), "total": vehicle_state.snapshot()["connection"]["messages_total"]})
            elif path == "/api/series":
                self._send_json(200, {"samples": normalizer.series(int(query.get("limit", ["2000"])[0]))})
            elif path == "/api/profiles":
                self._send_json(200, {"profiles": store.list_profiles()})
            elif path == "/api/settings":
                self._send_json(200, {"settings": store.load_preferences(), "map_style_configured": bool(os.environ.get("AEROSCOPE_MAP_STYLE_URL", ""))})
            elif path == "/api/logs":
                self._send_json(200, {"analyses": store.list_analyses(), "recordings": recorder.list()})
            elif path.startswith("/api/analysis/"):
                analysis_id = path.rsplit("/", 1)[-1]
                record = store.get_analysis(analysis_id)
                if not record: self._send_json(404, {"error": "Analysis not found"})
                else: self._send_json(200, record)
            elif path == "/api/recording":
                self._send_json(200, recorder.status())
            elif path.startswith("/api/export"):
                self._export(query)
            else:
                self._send_json(404, {"error": "Route not found"})
        except (ValueError, TypeError) as exc:
            self._send_json(400, {"error": str(exc)})
        except Exception:
            logger.exception("GET handler failed for %s", path)
            self._send_json(500, {"error": "Internal server error"})

    def do_POST(self) -> None:
        if not self._origin_ok():
            self._send_json(403, {"error": "Cross-origin request denied"})
            return
        path = urlsplit(self.path).path
        try:
            client=self.headers.get('X-AeroScope-Client')
            if path in POST_ROUTES:
                self._send_json(200,mavlink.workflows.dispatch('POST',path,self._json_body(),client)); return
            if vehicle_mutation(path): mavlink.workflows.require_owner(client)
            if path == "/api/control/mode":
                body = self._json_body()
                request = mavlink.request_mode(str(body.get("mode", "")), confirmed=body.get("confirmed") is True)
                self._send_json(202, {"request": request})
            elif path == "/api/control/arm":
                body = self._json_body()
                request = mavlink.request_arm(body.get("armed"), confirmation=str(body.get("confirmation", "")), acknowledge_unknown=body.get("acknowledge_unknown") is True)
                self._send_json(202, {"request": request})
            elif path == "/api/parameters/request":
                mavlink.request_parameters(); self._send_json(202, {"accepted": True})
            elif path == "/api/parameters/refresh":
                mavlink.refresh_parameter(str(self._json_body().get("name", ""))); self._send_json(202, {"accepted": True})
            elif path == "/api/parameters/set":
                body=self._json_body(); mavlink.set_parameter(str(body.get("name", "")),body.get("value"),confirmed=body.get("confirmed") is True)
                self._send_json(202, {"accepted": True})
            elif path == "/api/parameters/snapshot":
                body=self._json_body(); params=mavlink.parameters.snapshot()["parameters"]
                if not params: raise ValueError("No downloaded parameters to snapshot")
                self._send_json(201,{"snapshot":store.save_parameter_snapshot(str(body.get("name","")),params)})
            elif path == "/api/mission/download":
                mavlink.request_mission_download(); self._send_json(202, {"accepted": True})
            elif path == "/api/mission/upload":
                body=self._json_body(); mission=mavlink.upload_mission(body.get("items"),confirmed=body.get("confirmed") is True)
                self._send_json(202, {"mission":mission})
            elif path == "/api/simulation/control":
                body=self._json_body()
                if body.get("behavior") is not None: simulator.set_control_behavior(str(body["behavior"]))
                if body.get("operation") is not None:
                    params=body.get("params",{}); result=simulator.submit_control(str(body["operation"]),**(params if isinstance(params,dict) else {}))
                else: result=simulator.control_status()
                self._send_json(202,{"control":result})
            elif path == "/api/mission/clear":
                body=self._json_body(); self._send_json(202,{"mission":mavlink.clear_mission(confirmed=body.get("confirmed") is True)})
            elif path == "/api/guided/reposition":
                body=self._json_body(); tx=mavlink.guided_reposition(body.get("latitude"),body.get("longitude"),body.get("altitude"),body.get("speed",-1),confirmation=str(body.get("confirmation","")),acknowledge_unknown=body.get("acknowledge_unknown") is True)
                self._send_json(202,{"transaction":tx})
            elif path in ("/api/guided/takeoff","/api/guided/land","/api/guided/speed"):
                body=self._json_body(); confirm=str(body.get("confirmation",""))
                unknown=body.get("acknowledge_unknown") is True
                if path.endswith("takeoff"): tx=mavlink.guided_takeoff(body.get("altitude"),confirmation=confirm,acknowledge_unknown=unknown)
                elif path.endswith("land"): tx=mavlink.guided_land(confirmation=confirm,acknowledge_unknown=unknown)
                else: tx=mavlink.guided_speed(body.get("speed"),confirmation=confirm,acknowledge_unknown=unknown)
                self._send_json(202,{"transaction":tx})
            elif path == "/api/simulation":
                body = self._json_body()
                action = body.get("action")
                if action == "start":
                    mavlink.disconnect()
                    result = simulator.start(body.get("scenario", "normal"), float(body.get("speed", 1)))
                elif action == "stop":
                    simulator.stop(); result = simulator.status()
                elif action == "reset":
                    simulator.reset(); result = simulator.status()
                else: raise ValueError("action must be start, stop, or reset")
                self._send_json(200, {"simulation": result})
            elif path == "/api/connection":
                body = self._json_body()
                simulator.stop()
                result = mavlink.connect(body)
                self._send_json(202, {"connection": result})
            elif path == "/api/connection/disconnect":
                mavlink.disconnect(); simulator.stop()
                self._send_json(200, {"connection": mavlink.status(), "data": vehicle_state.snapshot()})
            elif path == "/api/events/acknowledge":
                event_id = str(self._json_body().get("id", ""))
                if not event_id: raise ValueError("Event id is required")
                ok = events.acknowledge(event_id)
                self._send_json(200 if ok else 404, {"acknowledged": ok})
            elif path == "/api/profiles":
                body = self._json_body()
                if not body.get("name", "").strip(): raise ValueError("Profile name is required")
                conn = body.get("connection", {})
                if not isinstance(conn, dict) or any("password" in k.lower() or "secret" in k.lower() for k in conn):
                    raise ValueError("Connection profiles may not store passwords or secrets")
                self._send_json(201, {"profile": store.save_profile(body)})
            elif path == "/api/settings":
                body = self._json_body()
                allowed = {"theme", "map_style_url", "telemetry_profile", "name"}
                for key, value in body.items():
                    if key not in allowed: continue
                    if key == "theme" and value not in ("dark", "light"): raise ValueError("theme must be dark or light")
                    if key == "map_style_url" and value and not (str(value).startswith("https://") or str(value).startswith("http://")):
                        raise ValueError("Map style must use HTTP or HTTPS")
                    store.save_preference(key, value)
                self._send_json(200, {"settings": store.load_preferences()})
            elif path == "/api/recording/start":
                body = self._json_body()
                state = vehicle_state.snapshot()
                if state["connection"]["state"] not in ("connected", "stale"):
                    raise ValueError("Connect a vehicle or start simulation before recording")
                self._send_json(201, {"session": recorder.start(str(body.get("name", "Telemetry recording")))})
            elif path == "/api/recording/stop":
                self._send_json(200, {"session": recorder.stop()})
            elif path == "/api/logs/upload":
                upload = self._multipart_upload()
                result = analyzer.upload_bytes(upload[0], upload[1])
                self._send_json(202, result)
            else:
                self._send_json(404, {"error": "Route not found"})
        except (ValueError, TypeError) as exc:
            self._send_json(400, {"error": str(exc)})
        except RuntimeError as exc:
            self._send_json(409, {"error": str(exc), "code": "operation_unavailable"})
        except Exception as exc:
            logger.exception("POST handler failed for %s", path)
            self._send_json(500, {"error": "Internal server error"})

    def do_DELETE(self) -> None:
        if not self._origin_ok():
            self._send_json(403, {"error": "Cross-origin request denied"})
            return
        path = urlsplit(self.path).path
        if path.startswith("/api/profiles/"):
            profile_id = path.rsplit("/", 1)[-1]
            deleted = store.delete_profile(profile_id)
            self._send_json(200 if deleted else 404, {"deleted": deleted})
        elif path.startswith("/api/analysis/"):
            analysis_id = path.rsplit("/", 1)[-1]
            record = store.get_analysis(analysis_id)
            if not record:
                self._send_json(404, {"error": "Analysis not found"}); return
            try:
                safe_log_path(record["stored_name"]).unlink(missing_ok=True)
            except ValueError:
                self._send_json(400, {"error": "Invalid stored file path"}); return
            with store._connect() as db:
                db.execute("DELETE FROM analyses WHERE id=?", (analysis_id,))
            self._send_json(200, {"deleted": True})
        else:
            self._send_json(404, {"error": "Route not found"})

    def _json_body(self) -> dict[str, Any]:
        ctype = self.headers.get("Content-Type", "")
        if not ctype.startswith("application/json"):
            raise ValueError("Content-Type must be application/json")
        data = json.loads(self._read_body(64 * 1024).decode("utf-8"))
        if not isinstance(data, dict): raise ValueError("JSON request body must be an object")
        return data

    def _multipart_upload(self) -> tuple[str, bytes]:
        ctype = self.headers.get("Content-Type", "")
        if not ctype.lower().startswith("multipart/form-data;"):
            raise ValueError("Upload must use multipart/form-data")
        body = self._read_body(MAX_UPLOAD_BYTES + 1024 * 1024)
        envelope = b"Content-Type: " + ctype.encode("ascii", "replace") + b"\r\nMIME-Version: 1.0\r\n\r\n" + body
        msg = BytesParser(policy=policy.default).parsebytes(envelope)
        if not msg.is_multipart(): raise ValueError("Malformed multipart upload")
        files = [part for part in msg.iter_parts() if part.get_content_disposition() == "form-data" and part.get_filename()]
        if len(files) != 1: raise ValueError("Upload must contain exactly one file")
        part = files[0]
        if (part.get_filename() or "").lower().endswith((".exe", ".dll", ".ps1", ".py", ".sh", ".js")):
            raise ValueError("Executable and script uploads are not accepted")
        payload = part.get_payload(decode=True)
        if payload is None or len(payload) > MAX_UPLOAD_BYTES: raise ValueError("The uploaded file is empty or too large")
        return str(part.get_filename() or "flight-log"), payload

    def _export(self, query: dict[str, list[str]]) -> None:
        kind = query.get("kind", ["inspector"])[0]
        fmt = query.get("format", ["json"])[0]
        if kind == "telemetry": rows = normalizer.series(6000)
        elif kind == "inspector": rows = normalizer.inspector()
        else: self._send_json(400, {"error": "kind must be telemetry or inspector"}); return
        if fmt == "json": self._send_bytes(200, _json_bytes(rows), "application/json; charset=utf-8")
        elif fmt == "csv":
            import csv
            import io
            out = io.StringIO(newline="")
            if rows:
                keys = sorted({key for row in rows for key in row})
                writer = csv.DictWriter(out, fieldnames=keys, extrasaction="ignore")
                writer.writeheader(); writer.writerows(rows)
            self._send_bytes(200, out.getvalue().encode("utf-8"), "text/csv; charset=utf-8")
        else: self._send_json(400, {"error": "format must be json or csv"})

    def _websocket(self, path: str) -> None:
        key = self.headers.get("Sec-WebSocket-Key")
        if not key or self.headers.get("Upgrade", "").lower() != "websocket":
            self._send_json(400, {"error": "WebSocket upgrade required"}); return
        if not self._origin_ok():
            self._send_json(403, {"error": "Cross-origin WebSocket denied"}); return
        accept = base64.b64encode(hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()).decode()
        self.send_response(101, "Switching Protocols")
        self.send_header("Upgrade", "websocket"); self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", accept); self.end_headers()
        self.close_connection = True
        sock = self.connection
        sock.settimeout(0.5)
        last_send = 0.0
        interval = 0.25 if path == "/ws/telemetry" else 1.0
        try:
            while True:
                ready, _, _ = select.select([sock], [], [], 0.05)
                if ready:
                    frame = _read_ws_frame(sock, 0.2)
                    if frame is None: continue
                    opcode, payload = frame
                    if opcode == 8:
                        sock.sendall(_ws_frame(b"", 8)); break
                    if opcode == 9:
                        sock.sendall(_ws_frame(payload, 10))
                    elif opcode == 1:
                        # Application heartbeat only; no command/control message is executed.
                        if payload == b'{"type":"ping"}': sock.sendall(_ws_frame(b'{"type":"pong"}'))
                now = time.monotonic()
                if now - last_send < interval: continue
                last_send = now
                if path == "/ws/control":
                    data = {"connection": mavlink.status(), "transactions": mavlink.transactions.list()}
                    packet = {"protocol_version": 1, "type": "control", "sent_at": now_iso(), "data": data}
                elif path == "/ws/events":
                    packet = {"protocol_version": 1, "type": "events", "sent_at": now_iso(), "data": {"events": events.list_events(30)}}
                elif path == "/ws/connection":
                    packet = {"protocol_version": 1, "type": "connection", "sent_at": now_iso(), "data": vehicle_state.snapshot()["connection"]}
                elif path == "/ws/telemetry":
                    packet = {"protocol_version": 1, "type": "telemetry", "sent_at": now_iso(), "data": vehicle_state.snapshot()}
                else:
                    sock.sendall(_ws_frame(b'{"error":"unknown websocket route"}')); break
                # No queue accumulates: each client receives the newest snapshot only.
                sock.sendall(_ws_frame(_json_bytes(packet)))
        except (OSError, ValueError, BrokenPipeError):
            pass
        finally:
            try: sock.shutdown(socket.SHUT_RDWR)
            except OSError: pass


def run() -> None:
    if HOST not in ("127.0.0.1", "localhost", "::1"):
        raise SystemExit("AeroScope is local-only until authentication is implemented. Set AEROSCOPE_HOST to 127.0.0.1, localhost, or ::1.")
    server = AeroScopeServer((HOST, PORT), Handler)
    logger.info("AeroScope listening at http://%s:%s", HOST, PORT)
    try:
        server.serve_forever(poll_interval=0.3)
    except KeyboardInterrupt:
        pass
    finally:
        simulator.stop(); mavlink.disconnect(); recorder.stop(); server.server_close()


if __name__ == "__main__":
    run()
