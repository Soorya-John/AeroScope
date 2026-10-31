# AeroScope

A local-first GCS organized around **Connect → Setup → Configure → Plan → Fly → Analyze**. The first interoperability target is **ArduPilot Copter 4.7.2**, official Windows SITL revision `fbd0a316`. This is an engineering GCS, not Mission Planner parity or production flight qualification.

See [COMPATIBILITY.md](COMPATIBILITY.md), [VERIFICATION.md](VERIFICATION.md), and [TASKS.md](TASKS.md) for evidence and remaining work. Demo telemetry is labeled and is not SITL.

## Windows setup

```powershell
cd C:\Users\soory\Desktop\GCS
python -m venv --system-site-packages .venv
.\.venv\Scripts\python.exe -m pip install -e ".\backend[mavlink,ulog,web,dev]"
.\.venv\Scripts\python.exe start_dev.py
```

Open **http://127.0.0.1:8765/#connect**. Stop with Ctrl+C. Python 3.11+ is required. The launcher uses FastAPI/Uvicorn when installed, otherwise the stdlib HTTP/WebSocket server. To select stdlib explicitly:

The optional `start_dev.ps1` wrapper is subject to Windows script policy. This machine blocks `.ps1` execution, so the direct Python command above is the verified startup path; no execution-policy change is needed.

```powershell
$env:AEROSCOPE_USE_STDLIB = '1'
.\.venv\Scripts\python.exe start_dev.py
```

Node is needed only for development/browser tests; the shipped frontend needs no build, account or API key. `pymavlink` enables vehicle communications/DataFlash; `pyulog` enables PX4 log parsing. Missing dependencies produce explicit errors. Install inside `.venv`. On Linux, install `-e 'backend[mavlink,ulog,web,dev]'` in a venv and run `python start_dev.py`. Native Windows SITL helpers are Windows-specific.

## Connect

Claim controls, choose Physical vehicle or Autopilot SITL, and select serial/baud, UDP listener or TCP client. Serial discovery is available. Enter exact system/component IDs (default 1/1); other targets are excluded. Change target by reconnecting. Check firmware, type, heartbeat age, session and capabilities.

UDP waits for telemetry sent to the entered bind address/port. TCP initiates a connection. ArduPilot receives a default 4 Hz stream request and a GCS heartbeat each second. Connecting never arms or starts a mission.

A renewable 30-second browser lease gates vehicle mutations and connection/demo changes; other tabs can monitor. This coordinates local clients, not user authentication. Reconnect invalidates transactions and queued operations without replaying hazardous commands. Demo uses isolated synthetic services and cannot send vehicle commands.

## Setup and Configure

Setup reports observed prerequisites, disarmed state, battery/GPS availability and calibration responses. These checks are not flight-readiness certification. Follow the hardware manufacturer's instructions.

Configure has sensor/orientation, receiver, mode, battery, GPS, failsafe, fence, output and tuning pages plus advanced parameters. Only parameters reported by the current Copter appear.

- Download with indexed progress, duplicates/out-of-order handling, missing-index recovery, timeout and cancellation.
- Pending edits survive telemetry refresh. Review creates a session-scoped diff. Explicit apply writes sequentially, confirms echoes and independently reads each value back. Changes are **not atomic**; failure stops the remainder and retains per-value outcomes.
- Complete JSON backups export locally. Restore stages matching live names/types, reports skipped entries and requires review/apply. Snapshots, comparison and JSON/CSV API exports remain available.
- A partial cache of 203 definitions from the tested revision supplies labels, descriptions, units, ranges, enums and reboot requirements where present, only for matching firmware. Missing metadata is explicit; hardware-safe values are not inferred.
- Numeric-cast and advertised bytewise codecs are separate. Non-representable cast integers and unsupported wide types are rejected. PARAM_EXT has no qualified firmware adapter and is not requested speculatively.

Gyro, barometer, board-level, six-side accelerometer and compass calibration use real commands/responses. Only compass supports cancellation. Accelerometer steps follow vehicle-requested orientations. Gyro has SITL evidence; physical sensor-motion workflows remain unqualified. Motor tests require disarmed Copter, a bench checkbox and typed propeller-removal confirmation; one motor, at most 10% throttle and three seconds. Autopilot checks remain active.

## Plan

The geographic Leaflet map supports double-click addition, dragging, reordering, command parameters and altitude references in metres. OpenStreetMap tiles are opt-in and attributed. Coordinates/routes work offline; terrain and tile caching are unavailable.

The reviewed Copter subset includes waypoint, takeoff, land, loiter/time/turns, spline, RTL, speed and camera trigger distance. Camera hardware is unverified. QGC WPL 110 and AeroScope items JSON import/export reject unsupported items without silently dropping them.

Uploads replace the plan but never start it. Transfers show progress, retries, cancellation and uncertainty. Integer items preserve precision even after a deprecated request. Round-trip comparison explicitly excludes ArduPilot's home slot and unused action fields. Fence/rally transfer remains separate and unavailable. Survey grids are rectangular local previews; add takeoff/landing and assess terrain, obstacles and camera coverage. Distance is a qualified straight-line sum; no flight time is invented.

## Fly

Measured attitude, position, altitude reference, speed, battery, GPS, RC, estimator, mode and armed state show missing/stale conditions. Status text and pre-arm rejections remain visible. Mode choices come from the active firmware mapping.

Normal arm/disarm and Copter GUIDED takeoff/land/speed/reposition require fresh identity and operation confirmation. No force-arm exists. Mode shortcuts include RTL, LAND, BRAKE/pause, AUTO/resume, LOITER and GUIDED where reported. AUTO acceptance does not prove mission resumption/completion.

The transaction table separates ACKs from observed altitude, position, landing and speed criteria. Unknown ACK outcomes are not automatically retried or repeated in the same session, avoiding late-ACK ambiguity.

## Analyze and maintain

DataFlash BIN and ULog analysis provide selected metrics, linked charts/path/replay/events, exports, coverage explanations and diagnostic hypotheses. Replay sends no commands. Findings are leads, not root causes. The real pyulog sample has been exercised; supported timed records are bounded and stale setpoint comparisons are excluded.

Disarmed Copter controller logs use MAVLink LOG messages with listing, chunk recovery, progress and cancellation. Downloads are capped at 32 MiB; only complete snapshots enter analysis. Other firmware log protocols/MAVLink FTP are unavailable.

Setup offers disarmed Copter reboot. Flashing and configuration reset are unavailable: no verified board/image/bootloader/recovery adapter exists. Use vendor-supported tools; flashing success is never fabricated.

## Architecture and API

`backend/app/` separates the sole reader, serialized writer, transactions, parameters, missions, firmware codecs/metadata, workflows/ownership, controller logs, state, storage and analysis. `frontend/static/` contains dependency-free UI and vendored Leaflet. Both HTTP transports share services. Bounded outbound operations carry session/generation tags so cancellation invalidates queued work.

Core routes: `/api/state`, `/api/setup`, `/api/control/{status,capabilities,ownership,claim,release,mode,arm,actions}`, `/api/connection`, `/api/connection/ports`, `/api/parameters/{request,stage,apply,batch,cancel,export}`, `/api/mission/{download,upload,clear,cancel}`, `/api/guided/*`, `/api/vehicle-logs/*`, `/api/logs`, `/api/analysis/{id}`. Mutations require `X-AeroScope-Client` matching the lease. WebSockets publish telemetry/events/connection/control snapshots and accept no vehicle commands.

Only loopback binds are allowed; request origins are checked. Remote authentication, MAVLink signing and cloud sync are unavailable. Do not expose through a proxy. SQLite, uploads and recordings stay in `data/`; override `AEROSCOPE_DATA_DIR`. Other variables: `AEROSCOPE_HOST` (loopback), `AEROSCOPE_PORT` (8765), `AEROSCOPE_MAX_UPLOAD_MB` (128), `AEROSCOPE_LOG_LEVEL`, `AEROSCOPE_USE_STDLIB`. `.env` is not loaded automatically.

## Verification commands

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s backend/tests -v
.\.venv\Scripts\python.exe -m compileall -q backend/app
node --check frontend/static/app.js
npm.cmd ci
npm.cmd run test:browser
python tools/fetch_sitl.py
.\.venv\Scripts\python.exe tools/verify_sitl.py
npx.cmd playwright test -c playwright.sitl.config.js
.\.venv\Scripts\python.exe tools/verify_ulog.py
```

Tests use installed Edge headlessly. SITL helpers own isolated official Windows Copter processes, refuse occupied ports and never attach to supplied physical endpoints. Normal flight commands go only to their own simulator. Binaries/fixtures/screenshots stay in ignored `.tools/`; result summaries are versioned.

PX4 SITL was not run: no installed WSL/Linux runtime. INAV/Betaflight need an MSP adapter and remain unsupported. See [THIRD_PARTY.md](THIRD_PARTY.md) for provenance/licenses.
