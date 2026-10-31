from __future__ import annotations

import base64
import json
import socket
import sys
import threading
import time
import unittest
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.main import AeroScopeServer, Handler, simulator, mavlink, vehicle_state


def get_json(url: str):
    with urlopen(url, timeout=4) as response:
        return response.status, response.headers.get_content_type(), response.read()


def post_json(url: str, body: dict):
    req = Request(url, json.dumps(body).encode(), {"Content-Type": "application/json",'X-AeroScope-Client':'http-tests'}, method="POST")
    with urlopen(req, timeout=4) as response:
        return response.status, json.loads(response.read())


def receive_exact(stream, size: int) -> bytes:
    data = bytearray()
    while len(data) < size:
        part = stream.read(size-len(data))
        if not part: raise EOFError("WebSocket closed during frame")
        data.extend(part)
    return bytes(data)


def open_ws(host: str, port: int):
    sock = socket.create_connection((host, port), timeout=3)
    sock.settimeout(3)
    key = base64.b64encode(b"aeroscope-test-key").decode()
    sock.sendall((f"GET /ws/telemetry HTTP/1.1\r\nHost: {host}:{port}\r\nOrigin: http://{host}:{port}\r\n"
                  f"Upgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
    stream = sock.makefile("rb")
    status = stream.readline()
    headers = {}
    while True:
        line = stream.readline()
        if line in (b"\r\n", b"\n", b""): break
        name, _, value = line.decode("latin1").partition(":")
        headers[name.lower()] = value.strip()
    if b"101" not in status:
        raise AssertionError(f"WebSocket upgrade failed: {status!r}")
    return sock, stream


def read_ws_json(stream):
    a, b = receive_exact(stream, 2)
    opcode, length = a & 0x0f, b & 0x7f
    if length == 126: length = int.from_bytes(receive_exact(stream, 2), "big")
    elif length == 127: length = int.from_bytes(receive_exact(stream, 8), "big")
    payload = receive_exact(stream, length)
    if opcode != 1: raise AssertionError(f"Expected WebSocket text frame; got opcode {opcode}")
    return json.loads(payload)


class HttpWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = AeroScopeServer(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.host, cls.port = cls.server.server_address
        cls.base = f"http://{cls.host}:{cls.port}"

    @classmethod
    def tearDownClass(cls):
        simulator.stop(); mavlink.disconnect()
        cls.server.shutdown(); cls.server.server_close(); cls.thread.join(timeout=2)

    def test_dashboard_and_health_load_without_a_vehicle(self):
        status, content_type, html = get_json(self.base + "/")
        self.assertEqual(status, 200)
        self.assertEqual(content_type, "text/html")
        self.assertIn(b"Mission overview", html)
        self.assertIn(b"SIMULATOR ONLY", html)
        health = json.loads(get_json(self.base + "/api/health")[2])
        state = json.loads(get_json(self.base + "/api/state")[2])["data"]
        self.assertEqual(health["status"], "ok")
        self.assertEqual(state["connection"]["state"], "disconnected")
        self.assertFalse(state["gps"]["fix_type"]["available"])

    def test_control_service_unavailable_is_a_conflict_not_server_error(self):
        with self.assertRaises(HTTPError) as err:
            post_json(self.base+"/api/parameters/request",{})
        self.assertEqual(err.exception.code,409)
        payload=json.loads(err.exception.read())
        self.assertEqual(payload["code"],"operation_unavailable")

    def test_post_body_is_consumed_before_next_keepalive_request(self):
        import http.client
        connection=http.client.HTTPConnection(self.host,self.port,timeout=4)
        try:
            connection.request('POST','/api/connection/disconnect','{}',{'Content-Type':'application/json','X-AeroScope-Client':'http-tests'})
            response=connection.getresponse();self.assertEqual(response.status,200);response.read()
            connection.request('GET','/api/state');response=connection.getresponse()
            self.assertEqual(response.status,200);response.read()
        finally:connection.close()

    def setUp(self):
        simulator.stop(); mavlink.disconnect(); vehicle_state.reset()
        mavlink.workflows.owner=None
        mavlink.workflows.claim('http-tests')

    def test_simulation_stream_and_graceful_stop(self):
        status, started = post_json(self.base + "/api/simulation", {"action":"start", "scenario":"normal", "speed":1})
        self.assertEqual(status, 200)
        self.assertTrue(started["simulation"]["running"])
        time.sleep(0.5)
        state = json.loads(get_json(self.base + "/api/state")[2])["data"]
        self.assertTrue(state["vehicle"]["simulated"])
        self.assertTrue(state["attitude"]["roll"]["available"])
        self.assertGreater(state["connection"]["messages_total"], 0)
        _, stopped = post_json(self.base + "/api/simulation", {"action":"stop"})
        self.assertFalse(stopped["simulation"]["running"])

    def test_multiple_websocket_clients_do_not_block_ingestion(self):
        post_json(self.base + "/api/simulation", {"action":"start", "scenario":"normal", "speed":1})
        first_sock, first_stream = open_ws(self.host, self.port)
        second_sock, second_stream = open_ws(self.host, self.port)
        try:
            first_packet = read_ws_json(first_stream)
            second_packet = read_ws_json(second_stream)
            self.assertEqual(first_packet["protocol_version"], 1)
            self.assertEqual(first_packet["type"], "telemetry")
            self.assertTrue(first_packet["data"]["vehicle"]["simulated"])
            self.assertTrue(second_packet["data"]["vehicle"]["simulated"])
            baseline = first_packet["data"]["connection"]["messages_total"]
            # Leave one client unread while the other remains healthy.
            time.sleep(1.1)
            active_packet = read_ws_json(second_stream)
            latest = json.loads(get_json(self.base + "/api/state")[2])["data"]["connection"]["messages_total"]
            self.assertGreater(latest, baseline)
            self.assertGreater(active_packet["data"]["connection"]["messages_total"], baseline)
        finally:
            first_sock.close(); first_stream.close(); second_sock.close(); second_stream.close()
            post_json(self.base + "/api/simulation", {"action":"stop"})

    def test_control_api_isolation_and_parameter_snapshot_endpoints(self):
        post_json(self.base+"/api/simulation",{"action":"start","scenario":"normal","speed":1})
        try:
            _,accepted=post_json(self.base+"/api/simulation/control",{"operation":"parameter_download"})
            self.assertEqual(accepted["control"]["kind"],"parameter_download")
            _,rejected=post_json(self.base+"/api/simulation/control",{"behavior":"rejected","operation":"mission_upload","params":{"items":[]}})
            time.sleep(.25)
            status,_,body=get_json(self.base+"/api/simulation/control")
            self.assertEqual(status,200)
            self.assertEqual(json.loads(body)["control"]["behavior"],"rejected")
            self.assertFalse(mavlink.status()["running"])
            with self.assertRaises(HTTPError) as err:
                post_json(self.base+"/api/mission/upload",{"items":[],"confirmed":False})
            self.assertEqual(err.exception.code,400)
        finally:
            post_json(self.base+"/api/simulation",{"action":"stop"})


if __name__ == "__main__":
    unittest.main()
