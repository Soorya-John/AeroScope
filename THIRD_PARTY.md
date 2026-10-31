# Third-party components and verification data

- Leaflet 1.9.4, BSD-2-Clause: JS/CSS and full LICENSE under `frontend/static/vendor`. https://github.com/Leaflet/Leaflet/tree/v1.9.4
- Map data: © OpenStreetMap contributors, ODbL. https://www.openstreetmap.org/copyright. Tiles are opt-in; no offline cache is bundled.
- Copter metadata comes from ArduPilot comments at revision fbd0a3160c90a557c24ce4c38c16975532652536, GPL-3.0-or-later. Definitions retain source URLs. https://github.com/ArduPilot/ardupilot/blob/fbd0a3160c90a557c24ce4c38c16975532652536/COPYING.txt
- Official SITL binaries/runtime are downloaded into ignored `.tools/sitl`, not committed. https://firmware.ardupilot.org/Tools/MissionPlanner/sitl/CopterStable/
- Public PX4/pyulog `test/sample.ulg`, BSD-3-Clause: source/SHA-256 in ULOG_RESULTS.json. https://github.com/PX4/pyulog/blob/main/LICENSE.md
- Other installed dependencies retain their upstream licenses.
