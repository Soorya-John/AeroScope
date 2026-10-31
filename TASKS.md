# AeroScope milestones and remaining work

Updated 2026-10-31. A checked implementation item does not imply hardware qualification. Evidence is in VERIFICATION.md, SITL_RESULTS.json and ULOG_RESULTS.json.

## 1. Audit and reliability

- [x] Read actual backend/frontend/tests and preserve inherited uncommitted work.
- [x] Baseline: 35 tests, 34 passed and one HTTP connection-abort error. Rechecked tooling rather than inheriting old environment limitations.
- [x] Keep one reader and serialized writer; bound the outbound queue and histories.
- [x] Exact target selection, source/destination filtering, session invalidation, cancellation generations, immediate ACK handling and stale-operation rejection.
- [x] Fix ignored JSON bodies poisoning persistent HTTP connections; add regression coverage.
- [x] Shared stdlib/FastAPI service instances, structured errors, ownership lease and optional-transport checks.
- [x] Never replay flight commands or permit same-session repeats after an uncertain ACK.
- [ ] Broader lossy-link integration campaigns: packet reordering, port failures, long-duration soak and MAVLink signing.

## 2. Copter connection, configuration and setup

- [x] Serial discovery/baud, UDP listener, TCP client, exact IDs, version/capability and environment display.
- [x] Copter 4.7.2 real Windows SITL connection, complete 1,370-parameter download and missing-index recovery state machine.
- [x] Cast/bytewise codec separation, float32 normalization, type/range/enum validation where metadata exists.
- [x] Persistent pending UI edits, staged review, sequential apply, confirmed echo plus independent readback, partial-failure/cancel states.
- [x] Backup export, local snapshots/comparison and restore-to-staged-diff workflow; CSV exports on both transports.
- [x] Friendly live parameter groups and partial version-pinned metadata (203 definitions).
- [x] Setup prerequisites; gyro/barometer/level/compass/six-side accelerometer workflows; bench interlocks and bounded motor test.
- [x] SITL confirms gyro result and motor-test acceptance with normal firmware checks.
- [ ] Physical sensor-motion qualification for compass, level, barometer and six-side calibration, including all compass combinations and cancellation.
- [ ] Dedicated receiver endpoint capture/calibration wizard (receiver parameters are currently editable).
- [ ] Complete metadata coverage, firmware hash matching beyond version, additional firmware versions and qualified PARAM_EXT adapters.
- [ ] Physical serial/USB reconnection and motor/output mapping qualification; no hardware operations were sent during development.

## 3. Geographic missions

- [x] Vendored geographic map, optional attributed OSM tiles, usable offline coordinates/routes.
- [x] Add/drag/reorder/delete, command parameter labels, metre/altitude references, validation and qualified route distance.
- [x] QGC WPL 110/items JSON import/export; reject unsupported items without dropping data.
- [x] Real Copter upload/download/clear/empty download; integer precision, request recovery, duplicate handling, cancellation and round-trip comparison.
- [x] Browser → HTTP → protocol → Copter mission transfer verified.
- [x] Rectangular survey preview after core transfer verification.
- [ ] Fence/rally data transfer (kept separate and explicitly unavailable).
- [ ] Polygon surveys, camera footprint/overlap, terrain clearance, obstacle checks, geographic flight-log basemap and offline tile bundles.
- [ ] Camera hardware and all supported mission commands individually exercised; current round trip uses takeoff/waypoint/RTL subset.

## 4. Flight operations

- [x] Telemetry freshness/unknown states, status/pre-arm messages, firmware mode mapping, normal arm/disarm and guided controls.
- [x] Separate ACK transaction history and observed action criteria; explicit control ownership across browser clients.
- [x] Real Copter SITL normal arm, takeoff, COMMAND_INT reposition, BRAKE, RTL, observed landing/disarm and reconnect.
- [x] Actual unsupported-command rejection tested without weakening arming checks.
- [ ] Separate direct LAND command, speed-achievement and mission AUTO/resume acceptance campaigns; these are implemented but not independently SITL-qualified here.
- [ ] Systematic flight-command timeout/disconnect/late-response campaigns against autopilot SITL; deterministic/unit regressions cover these state-machine cases only.

## 5. Logs and maintenance

- [x] Preserve linked DataFlash/ULog analysis, events/replay/exports and missing-data explanations.
- [x] Run public upstream ULog through pyulog 1.3.0; fix relative-altitude initialization and stale setpoint matching; bound selected records.
- [x] Controller LOG listing/download/cancel/progress, 32 MiB cap, duplicate/missing-chunk recovery and growing-file snapshot handling.
- [x] Download 4,915,200 bytes from Copter SITL and parse the complete snapshot as 122,185 DataFlash records.
- [x] Disarmed Copter reboot API/UI implemented (interoperability still unverified).
- [ ] Board-specific firmware flashing, board/image checks, bootloader recovery and reset workflow. Deliberately unavailable, with explicit UI explanation.
- [ ] MAVLink FTP, larger logs and firmware-specific PX4 controller-log retrieval.

## 6. Broader support and usability

- [x] Connect/Setup/Configure/Plan/Fly/Analyze navigation, units/current/pending values, review/apply feedback, unsupported-action explanations and mobile checks.
- [x] 51 backend/transport tests, three general browser tests and one full browser-to-SITL test pass.
- [x] Native Windows Copter SITL download and isolated reproducible test helpers; real ULog fixture helper.
- [x] Verified direct Windows venv startup. `.ps1` wrapper is blocked by host execution policy; no policy change was made.
- [ ] PX4 SITL: blocked by absent WSL/Linux/Docker runtime on this host. No PX4 flight-operation support claim.
- [ ] Plane/Rover setup and mission-write qualification. Telemetry and generic protocols remain unverified on these families.
- [ ] INAV/Betaflight MSP adapter and firmware semantics, as a separate milestone. Unsupported today.
- [ ] Hardware acceptance, production-readiness work and Mission Planner-class breadth. Do not claim parity.

## Delivery history

The requested work is divided into ten substantive commit/push batches: audit, protocol reliability, regression tests, shared workflows, geographic UI, firmware/browser qualification, controller logs/action observations, transport/ULog fixes, Copter browser acceptance/refinement, and final evidence/documentation. No empty activity commits are used.
