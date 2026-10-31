# AeroScope compatibility and evidence

Audit: 2026-10-31. Status is implementation status, not certification or flight readiness.
No physical vehicle is used by development tests. Deterministic demo is not SITL.

| Workflow | Status at audit | Firmware boundary / evidence |
|---|---|---|
| UDP, TCP, serial telemetry | Implemented but unverified on autopilot | pymavlink 2.4.50; unit tests and demo only |
| Version, identity, heartbeat freshness | Partial | First heartbeat selected implicitly; component filtering incomplete |
| Commands, mode, arm, guided | Partial | ACK and heartbeat state exist; no autopilot verification |
| Parameter list/read/write | Partial | No missing-index recovery; numeric encoding assumed; speculative PARAM_EXT requests |
| Snapshots, comparison, JSON/CSV | Implemented and verified | Local storage/unit tests; restore UI missing |
| Mission transfer/editor | Partial | No retry/cancel; empty-download ACK missing; local coordinate plot only |
| Setup/calibration/output tests | Missing | No firmware adapter or guided setup |
| DataFlash analysis/replay | Implemented and verified | Generated DataFlash fixture; selected records only |
| ULog analysis | Implemented but unverified | pyulog absent at audit; no real fixture |
| FC log download, firmware flashing | Missing | No board identification or bootloader implementation |
| INAV/Betaflight | Unsupported | No MSP adapter; MAVLink is not MSP |
| ArduPilot Copter | Partial | First implementation target; no version claimed verified yet |
| ArduPilot Plane/Rover, PX4 | Implemented but unverified (telemetry only) | No complete setup/control compatibility claim |

Baseline: 35 tests run, 34 pass; one Windows HTTP connection-abort error in unavailable-parameter test. Existing uncommitted application work is preserved and extended. Node/npm now present. Python 3.12, pymavlink 2.4.50, pyserial 3.5. WSL command exists but WSL is not installed. Docker unavailable. Native Windows ArduPilot SITL binaries are being investigated; PX4 SITL requires a suitable Linux environment.

## Protocol and firmware references checked

- [MAVLink parameter protocol](https://mavlink.io/en/services/parameter.html): encoding must follow advertised bytewise/C-cast capability and firmware behavior.
- [MAVLink mission protocol](https://mavlink.io/en/services/mission.html): retries, separate mission types, source/target validation, final ACK.
- [ArduPilot Windows SITL binaries](https://firmware.ardupilot.org/Tools/MissionPlanner/sitl/).
- [ArduPilot Windows build choices](https://ardupilot.org/dev/docs/building-setup-windows.html).
- [PX4 MAVLink documentation](https://docs.px4.io/main/en/mavlink/): generic command definitions do not imply firmware support.

## Remaining acceptance work

Actual SITL results and exact versions will be recorded in VERIFICATION.md. Hardware support remains unverified. No Mission Planner parity or production-readiness claim is made.
