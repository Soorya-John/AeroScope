# AeroScope task tracker

## Delivered in this workspace

- [x] Inspect repository and runtime; confirm initially empty workspace, Python/Git/pymavlink availability, and missing Node/npm, FastAPI/Uvicorn, pyulog, and package-index access.
- [x] Establish Python backend, static frontend, development scripts, local configuration, SQLite storage, and a documented structure.
- [x] Implement responsive dashboard/navigation, live status and unavailable states, simulator controls, Canvas attitude and telemetry views, local GPS track plot, logs/replay, diagnostics, inspector, profiles, and settings.
- [x] Implement deterministic simulator scenarios through the shared normalizer, state, WebSocket, and event paths.
- [x] Implement a single pymavlink reader for UDP listener, serial, and TCP client, normalized selected messages, heartbeat timeout, stale status, and reconnect handling.
- [x] Implement bounded WebSocket broadcasts, inspector, graph history, telemetry recorder, local exports, and persisted events/profiles/analysis metadata.
- [x] Implement content-signature upload validation, bounded background analysis, pymavlink DataFlash parsing, optional pyulog-based ULog adapter, selected metrics/findings, and replay data.
- [x] Add `unittest` coverage for core normalization/events/storage/parser/simulation/HTTP/WebSocket behavior.
- [x] Write setup, API, configuration, simulator, parser scope, connection, SITL, security, known-limitations, and troubleshooting documentation.
- [x] Run the backend tests (18 passing), Python compile check, JavaScript parse check, and final local HTTP/simulator smoke check.
- [x] Initialize Git for the new workspace. Final local smoke check: `/api/health` returned `ok`; dashboard and JS assets were served; battery-sag simulation reported `connected`, `simulated=true`, and 33 decoded messages; simulation stop returned `running=false`.

## Remaining work / environmental limits

- [ ] Replace the dependency-free static UI with React + TypeScript + Vite + Tailwind, then run a production frontend build and browser workflow tests. Node/npm and a browser were unavailable here.
- [ ] Upgrade map plot to MapLibre with a configured, permitted map style and offline behavior.
- [ ] Verify optional FastAPI transport after installing FastAPI/Uvicorn; this environment lacked those packages.
- [ ] Install pyulog and validate the ULog adapter against a redistributable, generated or authorized real ULog fixture.
- [ ] Validate serial, UDP, and TCP connection behavior against ArduPilot/PX4 SITL and hardware in a safe bench setup.
- [ ] Broaden firmware-specific DataFlash/ULog field coverage and diagnostic calibration with supported reference logs.
- [ ] Add browser-level component and end-to-end tests when a supported frontend toolchain is available.

## Notes

- The app currently favors a working stdlib service and dependency-free browser UI so it can run in the provided environment. README lists this explicit deviation from the requested production stack; do not mark the project as meeting the React/Vite/MapLibre acceptance items until those are implemented and verified.
- Test-generated temporary folders under `backend/tests/.test-tmp/` are ignored by Git. Several inherited temporary folders were not readable through the current Windows filesystem ACL, so they were left in place.
- No flight-control commands are implemented or sent.
