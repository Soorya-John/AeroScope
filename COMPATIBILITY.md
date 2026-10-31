# AeroScope compatibility and evidence

Updated 2026-10-31. **Implemented** describes code. **Verified** identifies the actual test environment. No physical aircraft was commanded. Demo/unit tests do not establish autopilot interoperability.

## Firmware boundaries

| Firmware | Boundary |
|---|---|
| ArduPilot Copter 4.7.2, revision fbd0a316 | First verified target: official Windows CopterStable SITL, quad/X. See exact checks below. |
| Physical Copter / other Copter versions | Implemented workflows; hardware and other versions unverified. Metadata is enabled only for matching 4.7.2 firmware. |
| ArduPilot Plane/Rover | Telemetry and generic protocol code implemented but unverified. Copter setup/guided and mission writes are unavailable. |
| PX4 | Telemetry, version and generic parameter/mode groundwork implemented but unverified against PX4 SITL. Copter setup/guided, mission writes and controller logs unavailable. |
| INAV / Betaflight | Unsupported: no MSP adapter or firmware-specific services. MAVLink does not imply MSP support. |
| Unknown firmware | Unknown encoding blocks parameter writes. Copter-specific workflows reject unknown identity. No support is inferred merely from generic message definitions. |

## Feature matrix

| Workflow | Status | Evidence / limits |
|---|---|---|
| UDP connection/identity/freshness | Implemented and verified | Copter 4.7.2 SITL, explicit 1/1 target, version/capabilities, reconnect/new session |
| TCP connection/telemetry/parameters/mission | Implemented and verified on earlier run | Same native build; closing TCP caused native runtime exit, so final reconnect campaign uses UDP |
| Serial discovery/baud/connection | Implemented but unverified on vehicle | pyserial discovery and URI validation; no physical port opened |
| Browser ownership, loopback, origin checks | Implemented and verified | Exclusive expiring lease, HTTP/ASGI rejection tests; not authentication |
| Parameter list, edit, confirmation/readback | Implemented and verified | All 1,370 parameters, staged INS_GYRO_FILTER edit/restore, independent readback, browser-to-SITL |
| Loss/duplicate/out-of-order parameter handling | Implemented and verified in regression tests | Indexed recovery, cancellation/late echo, float32 and codec tests; no RF-loss campaign |
| Backup/restore/compare/export | Implemented; core path verified | SITL JSON backup and restored value; UI JSON restore stages matches. CSV tested on both transports. Broad real backup migration unverified. |
| Firmware parameter metadata | Partial | 203 source-pinned definitions for exact version; missing entries/ranges/units remain unavailable |
| PARAM_EXT | Unsupported for current firmware adapters | Speculative discovery disabled; wide writes refused |
| Friendly Copter setup/configuration | Partial | Live groups, metadata and prerequisite checks; no receiver capture wizard or complete hardware setup qualification |
| Gyro calibration | Implemented and verified | Actual Copter command/result in SITL |
| Barometer, level, six-side accel, compass | Implemented but unverified on sensors | Real command/progress/result/cancel paths; deterministic regression coverage only; compass-only cancellation |
| Motor test | Implemented; protocol verified | Copter SITL accepted a one-motor 3%, 1 s test; hardware output/motor mapping unverified |
| Geographic map/editing/WPL import/export | Implemented and browser-verified | Offline routes, drag/add/reorder/delete; opt-in attributed tiles. Tile-network availability not guaranteed. |
| Mission upload/download/clear | Implemented and verified | Copter integer transfer, takeoff/waypoint/RTL round trip, clear and empty download, browser-to-SITL |
| Broader mission commands | Implemented but unverified individually | Loiter/spline/speed/camera subset reviewed; camera hardware and execution not tested |
| Fence/rally plans | Missing | Explicitly separate/unavailable; fence parameter editing is available |
| Survey planning | Partial | Rectangular local grid preview; no polygon/terrain/camera-coverage computation |
| Mode, arm, takeoff, reposition, BRAKE, RTL | Implemented and verified | Normal arming checks retained; ACK and observed position/altitude/landing/disarm tracked in Copter SITL |
| Direct land, speed, AUTO/resume | Implemented but unverified individually | No mission-execution completion claim; AUTO is a mode request |
| Timeout/rejection/session race handling | Implemented and regression-tested | Actual unsupported-command rejection plus unit/deterministic missing/late ACK/disconnect cases; no complete hazardous-command fault campaign in SITL |
| DataFlash analysis/replay | Implemented and verified | Generated fixture and real 4,915,200-byte SITL download, 122,185 records |
| ULog analysis | Implemented and verified on public fixture | pyulog 1.3.0, 667 samples, 68.99 s; selected topic coverage only, not PX4 vehicle interoperability |
| FC log listing/download/cancel | Implemented and verified for normal path | Copter LOG protocol, complete bounded snapshot, missing/duplicate chunk regression tests; max 32 MiB |
| Reboot | Implemented but unverified on autopilot | Disarmed Copter confirmation; no automatic success before ACK |
| Configuration reset / firmware flashing | Missing, deliberately unavailable | No verified board/image/bootloader/recovery implementation |

## Baseline and current environment

Audit baseline: 35 tests, 34 passed, one Windows HTTP abort. Existing uncommitted prototype work was preserved and extended. Node/npm, Edge, package access, FastAPI and pyulog are now available; older README environment limits were not treated as permanent blockers.

Current evidence: 51 backend/transport tests, three general browser tests, one browser-to-Copter SITL test, 22 protocol/workflow SITL checks, and public ULog fixture parsing. See VERIFICATION.md for commands and limitations.

## Official references reviewed

- [MAVLink parameters](https://mavlink.io/en/services/parameter.html): firmware-specific bytewise/cast encoding.
- [MAVLink missions](https://mavlink.io/en/services/mission.html): source/target/type isolation, retries, final ACK, cancellation and integer response to deprecated requests.
- [ArduPilot calibration](https://ardupilot.org/dev/docs/mavlink-calibration.html): actual Copter calibration semantics and compass-specific commands.
- [Copter guided commands](https://ardupilot.org/dev/docs/copter-commands-in-guided-mode.html): COMMAND_INT reposition versus COMMAND_LONG takeoff/land/speed.
- [ArduPilot motor test implementation](https://github.com/ArduPilot/ardupilot/blob/master/ArduCopter/motor_test.cpp).
- [Official Windows SITL binaries](https://firmware.ardupilot.org/Tools/MissionPlanner/sitl/CopterStable/).
- [PX4 MAVLink](https://docs.px4.io/main/en/mavlink/): generic definitions do not imply firmware support.

PX4 SITL remains blocked by the lack of an installed Linux/WSL/Docker runtime. Physical sensor/motor, serial, board flashing and MSP interoperability require the corresponding hardware/adapter qualification. No parity or production-readiness claim is made.
