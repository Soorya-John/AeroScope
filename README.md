# AeroScope

AeroScope is a local-first engineering console for viewing MAVLink telemetry, generating synthetic flights, and inspecting ArduPilot DataFlash BIN and PX4 ULog files. The current release is deliberately read-only: it sends no arming, mode, mission, actuator, or other flight-control commands.

## What is implemented

- A responsive dark engineering interface with dashboard, live telemetry, flight logs/replay, diagnostics, MAVLink inspector, vehicle profiles, and settings views.
- A deterministic simulator (normal, GPS degradation, battery sag, elevated vibration, heartbeat loss, EKF warning, and RC degradation). Simulation runs through the same telemetry normalizer and event engine as MAVLink input.
- A single pymavlink reader supporting UDP listener, serial, and TCP client connections; heartbeat/freshness tracking; normalized telemetry; bounded message history; and three WebSocket feeds shared by browser clients.
- SQLite-backed local profiles, preferences, events, and analysis metadata; local JSONL recordings; bounded diagnostics; JSON/CSV exports.
- A real pymavlink DataFlash BIN parser with selected flight metrics/events/findings and a pyulog-backed ULog parser adapter. ULog parsing requires the optional `pyulog` dependency. The parsers report data coverage and limitations; they do not claim to interpret every firmware-specific record.
- Local flight replay controls for parsed analysis samples, with linked time selection across the chart, path plot, attitude display, and event list when the data is available.
- Evidence-based deterministic rules for selected vibration, battery, attitude-tracking, GPS, estimator/failsafe event, and logging-gap patterns. Findings are engineering leads, not root-cause diagnoses.

### Current implementation boundaries

This workspace had Python and pymavlink, but no Node.js/npm, FastAPI/Uvicorn, pyulog, or available browser surface; package-index access also failed. To keep the project runnable here, the actual UI is dependency-free HTML/CSS/Canvas/JavaScript, and the primary HTTP/WebSocket service uses Python's standard library. An optional FastAPI transport is included for installations with the `web` extra. No React/Vite/Tailwind build, MapLibre tiled basemap, ECharts, Three.js horizon, browser automation, real PX4 ULog parse, or hardware/SITL connection was testable here. The map is a local coordinate plot and is intentionally blank when there is no valid GPS track. These limitations are visible in the UI and should be addressed before treating this as a full production ground station.

## Architecture

```text
ArduPilot / PX4 ──MAVLink──┐
                           ├─ single reader ─ normalizer ─ vehicle state ─┬─ event engine ─ SQLite
Simulator ─────────────────┘                                             ├─ bounded telemetry history
                                                                         ├─ REST / WebSocket feeds ─ browser UI
DataFlash BIN / ULog ─ upload validation ─ background parser ─ metrics/findings ─ SQLite + local files
```

`backend/app/` separates connection handling, normalization, state, events, simulation, log analysis, persistence, and HTTP transport. `frontend/static/` contains the local interface. The standard-library server and optional ASGI server use the same service objects. Only one live vehicle reader is started, regardless of browser client count.

## Requirements

- Python 3.11 or newer (Python 3.12 was used for the checks below).
- `pymavlink` for real MAVLink and DataFlash BIN support. The current checked environment already provides it. On a fresh machine, install the optional dependency as shown below.
- `pyulog` for PX4 ULog parsing (optional).
- Optional FastAPI/Uvicorn (`web` extra) for the ASGI transport. Without it, the standard-library HTTP/WebSocket server is used.
- Node.js is not needed to run this implementation.

No cloud account, API key, Docker, or paid service is required. The optional map-style setting is not used to fetch map tiles in this release.

## Install and start

### Windows PowerShell

```powershell
cd C:\Users\<you>\path\to\GCS
python -m venv --system-site-packages .venv
.\.venv\Scripts\python.exe -m pip install -e ".\backend[mavlink,ulog]"
.\start_dev.ps1
```

If installation from the configured Python package index is unavailable, the app can still start using Python's standard library; the simulator and interface work, while real MAVLink/DataFlash support requires pymavlink. Use the project's existing environment or obtain packages from an approved index when possible. Do not install packages globally.

### Linux

```bash
cd /path/to/GCS
python3 -m venv --system-site-packages .venv
.venv/bin/python -m pip install -e 'backend[mavlink,ulog]'
bash ./start_dev.sh
```

The startup scripts create a virtual environment if missing and run `start_dev.py`. If FastAPI and Uvicorn are installed, the optional ASGI transport starts; otherwise the standard-library server starts. To explicitly use the standard-library server, set `AEROSCOPE_USE_STDLIB=1` before running `python start_dev.py`.

Open **http://127.0.0.1:8765**. Health check: **http://127.0.0.1:8765/api/health**. Stop with Ctrl+C. `python start_dev.py` is the equivalent direct command after dependencies are available.

The `.env.example` file lists configuration names, but the program does not load `.env` automatically. Set variables in the shell before launch:

```powershell
$env:AEROSCOPE_PORT = '8765'
python start_dev.py
```

```bash
export AEROSCOPE_PORT=8765
python start_dev.py
```

Useful settings:

| Variable | Default | Purpose |
|---|---|---|
| `AEROSCOPE_HOST` | `127.0.0.1` | Bind address. Only loopback addresses are accepted until authentication exists. |
| `AEROSCOPE_PORT` | `8765` | HTTP and WebSocket port. |
| `AEROSCOPE_DATA_DIR` | `./data` | SQLite database, uploaded files, and recordings. |
| `AEROSCOPE_MAX_UPLOAD_MB` | `128` | Maximum uploaded flight-log size. |
| `AEROSCOPE_LOG_LEVEL` | `INFO` | Server log verbosity. |
| `AEROSCOPE_USE_STDLIB` | unset | Set to `1` to force the built-in server if FastAPI is installed. |

## Using simulation

1. Open the app and choose a scenario in the connection/simulation controls.
2. Start simulation. The UI labels the vehicle and its values **SIMULATED**.
3. Observe the attitude, bounded graph traces, alerts, and inspector. Try GPS loss, voltage sag, vibration, heartbeat loss, EKF warning, or RC degradation.
4. Stop or reset simulation from the same controls.

The optional helper starts a scenario against an already running server:

```powershell
python run_sitl_demo.py battery_sag --speed 1
```

```bash
python3 run_sitl_demo.py gps_degradation --speed 1
```

The simulator uses the backend's normalized telemetry pipeline and does not communicate with a real vehicle.

## MAVLink connections

The connection dialog exposes the following modes:

| Mode | Configuration | Meaning |
|---|---|---|
| UDP listener | Bind address, port | `udpin:<bind>:<port>`; AeroScope waits for a vehicle/GCS to send datagrams to this host and port. Default port is 14550. |
| Serial | Port, baud | COM port on Windows or a supported `/dev/tty*`/`/dev/serial/by-id/*` path on Linux. Default 115200 baud. |
| TCP | Host, port | TCP client using `tcp:<host>:<port>`; AeroScope connects to the remote endpoint. Default port is 5760. |

Only one active MAVLink reader is used. The first heartbeat selects the target system; other system IDs are ignored for that session. Missing heartbeat, stale fields, reconnecting, and errors are shown separately. The app does not request stream-rate changes automatically. The list of decoded fields depends on the firmware and messages actually sent. MAVLink values are normalized with units and per-field availability; missing values stay unavailable.

### ArduPilot SITL (example, not exercised in this environment)

Install and build ArduPilot SITL following the upstream instructions, then run (commonly from the ArduCopter directory):

```bash
sim_vehicle.py -v ArduCopter -f quad --console --map --out=udp:127.0.0.1:14550
```

In AeroScope choose UDP listener, bind address `127.0.0.1`, port `14550`. When the app runs on a different host/container, configure the sender and listener address/firewall accordingly. SITL commands and network forwarding vary by installation; this workflow was not hardware/SITL-tested here.

### PX4 SITL (example, not exercised in this environment)

Follow the current PX4 SITL installation and simulator documentation for your host. A common local build launch is:

```bash
make px4_sitl gz_x500
```

Configure PX4/MAVLink forwarding to a UDP endpoint reachable by AeroScope, then select UDP listener with the corresponding port. PX4 simulator versions and default forwarding endpoints vary; confirm the MAVLink route from the PX4 console before troubleshooting AeroScope.

### Real flight controller

Use a bench setup first, secure the aircraft, remove propellers, and follow the flight-controller manufacturer's connection procedure. Select the correct telemetry/USB port and baud rate; do not open a serial port simultaneously in another GCS if the driver/device allows only one client. Start with read-only monitoring. AeroScope currently sends no vehicle-control commands, but connecting a GCS does not make an aircraft safe to operate and this application is not a certified safety system.

## Flight-log workflow

Open **Flight Logs**, drop a `.BIN` or `.ulg` file, or use the file picker. The server checks file signatures (not only extensions), rejects unsupported content, applies the upload limit, stores a unique file under the configured data directory, and parses it in a bounded background worker. The analysis list shows queued/running/completed/failed status. Select a completed item to view available metrics, findings, timeline/events, data coverage, charts, local path plot, and replay controls. Replay is detached from live telemetry and sends no commands.

### Parser and analysis scope

- **ArduPilot DataFlash BIN:** uses `pymavlink.DFReader_binary` and iterates decoded records. The implementation extracts a selected set of attitude, GPS, battery, vibration, mode, and event data. DataFlash message layouts differ by firmware/version, so the parser reports selected coverage and does not promise complete log decoding.
- **PX4 ULog:** uses the public `pyulog.ULog` dataset API when `pyulog` is installed. Selected attitude, position, battery, GPS, RC, actuator, and status topics are normalized. The current environment did not have pyulog, so this adapter could not be run against a real ULog fixture here. Install with `python -m pip install -e 'backend[ulog]'`.
- Upload processing limits records, samples, events, findings, and file size. A corrupt file should produce a failed analysis with an error rather than being treated as a valid flight.
- Metrics show their altitude/speed reference where available. Findings use explicit thresholds and recorded evidence; correlations and possible explanations are labeled as such. Missing data can prevent a calculation. No remaining-flight-time estimate or AI diagnosis is provided.

Upstream parser references: [pymavlink DataFlash reader](https://github.com/ArduPilot/pymavlink/blob/master/DFReader.py), [pyulog ULog API](https://github.com/PX4/pyulog/blob/main/pyulog/core.py). ArduPilot event-code references include [AP_Logger event definitions](https://github.com/ArduPilot/ardupilot/blob/master/libraries/AP_Logger/AP_Logger.h).

## API and WebSocket schema

Interactive API documentation is available at `/api/docs` when the optional FastAPI transport is installed. The built-in server provides JSON endpoints directly. Main routes include:

| Route | Use |
|---|---|
| `GET /api/health` | Local health/version/protocol check. |
| `GET /api/state` | Current normalized state, simulator status, and connection status. |
| `GET /api/events?limit=100` | Recent persisted events. |
| `GET /api/inspector?limit=200` | Bounded decoded message history and rates. |
| `GET /api/series?limit=2000` | Bounded telemetry samples. |
| `POST /api/simulation` | Start/stop/reset a scenario. |
| `POST /api/connection`, `POST /api/connection/disconnect` | Connect/disconnect the single MAVLink reader. |
| `POST /api/logs/upload`, `GET /api/logs`, `GET /api/analysis/{id}` | Upload and inspect log analyses. |
| `GET/POST /api/profiles` | List/create local profiles. `DELETE /api/profiles/{id}` removes one. |
| `POST /api/recording/start`, `POST /api/recording/stop` | Record normalized telemetry locally. |
| `GET /api/export?kind=inspector&format=json` | Export bounded inspector or telemetry history as JSON/CSV. |

WebSockets are `/ws/telemetry`, `/ws/events`, and `/ws/connection`. Each packet is an envelope of this form:

```json
{"protocol_version":1,"type":"telemetry","sent_at":"2026-10-24T00:00:00+00:00","data":{"connection":{"state":"connected"},"attitude":{"roll_deg":{"value":0.0,"available":true,"units":"deg"}}}}
```

Telemetry state is published at about 4 Hz; events and connection status at about 1 Hz. Fields carry value, availability, units, source/update metadata, and freshness where applicable. A missing field is not interpreted as zero. WebSocket clients do not read from the MAVLink connection themselves, so multiple browser clients share the same backend session.

## Storage and security

By default, AeroScope stores `data/aeroscope.sqlite3`, uploaded logs in `data/logs/`, and recordings in the data directory under the project root. Override with `AEROSCOPE_DATA_DIR`; deleting an analysis through the API/UI deletes its stored source file. Back up or remove this directory using normal file tools.

The service rejects non-loopback binds because authentication and authorization are not implemented. Keep it on a trusted local machine. Upload processing does not execute files, stores generated filenames within the controlled log directory, and caps file size and parser records. Vehicle profiles reject password/secret fields. Do not expose the service through a router, reverse proxy, or public network. The UI does not provide arbitrary commands or control actions.

## Tests and checks

Run the backend test suite from the project root:

```powershell
python -m unittest discover -s backend/tests -v
```

```bash
python3 -m unittest discover -s backend/tests -v
```

The suite covers state normalization/conversions, missing values and freshness, profile/database persistence, event deduplication/recovery, simulator scenarios, DataFlash BIN parsing via a generated fixture, upload validation, and HTTP/WebSocket behavior including multiple clients. In this environment, all 18 backend tests passed. `compileall` and JavaScript syntax checks passed. There was no Node/Vite build, browser automation, FastAPI run, real ULog fixture, physical flight controller, or SITL available for verification. Tests use generated test data and do not include private flight logs.

## Troubleshooting

- **Port already in use:** set `AEROSCOPE_PORT` to another port and update the browser URL.
- **UI says disconnected:** start a simulation or check the selected serial/UDP/TCP endpoint and confirm the autopilot is sending MAVLink heartbeats.
- **UDP never connects:** verify that the vehicle/SITL is sending to the listener host and port; a UDP listener does not initiate a connection to a remote peer.
- **Serial unavailable:** verify the OS port name, permissions, baud, cabling, and that another application has not claimed it.
- **MAVLink support unavailable:** install the `mavlink` extra in the project virtual environment with `python -m pip install -e 'backend[mavlink]'`.
- **ULog parser unavailable:** install the `ulog` extra with `python -m pip install -e 'backend[ulog]'`; unsupported/corrupt content is reported as an analysis failure.
- **Upload rejected:** confirm the file is a genuine DataFlash BIN or ULog and is within `AEROSCOPE_MAX_UPLOAD_MB`.
- **Map plot is empty:** valid GPS samples are required; AeroScope intentionally does not fabricate a coordinate. This release plots the recorded local track and does not load a tiled map.
- **App is unexpectedly reachable externally:** startup refuses a non-loopback bind. Do not remove that guard until authentication and authorization have been implemented.

## Known limitations and roadmap

This is an engineering prototype, not a certified GCS. The current UI does not yet use the requested React/TypeScript/Vite/Tailwind/MapLibre/ECharts/Three.js stack. The coordinate plot is not a geographic basemap; no mission editor/upload, vehicle command service, cloud sync, authentication, or AI-provider integration exists. ULog parsing needs installation and real fixture verification. Firmware-specific field coverage varies. Connection reconnect behavior and timings require validation against real serial/UDP/TCP autopilot setups. The optional FastAPI transport is provided but was unavailable in this environment. See [TASKS.md](TASKS.md) for implementation status and remaining work.
