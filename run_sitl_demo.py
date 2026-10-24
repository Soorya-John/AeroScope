from __future__ import annotations

import argparse
import json
import urllib.error
import urllib.request


def main() -> None:
    parser = argparse.ArgumentParser(description="Start an AeroScope synthetic telemetry scenario")
    parser.add_argument("scenario", choices=("normal", "gps_degradation", "battery_sag", "elevated_vibration", "heartbeat_loss", "ekf_warning", "rc_degradation"), nargs="?", default="normal")
    parser.add_argument("--speed", type=float, default=1.0)
    parser.add_argument("--url", default="http://127.0.0.1:8765")
    args = parser.parse_args()
    body = json.dumps({"action": "start", "scenario": args.scenario, "speed": args.speed}).encode()
    request = urllib.request.Request(args.url.rstrip("/") + "/api/simulation", body,
        {"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=4) as response:
            print(response.read().decode())
            print("AeroScope now publishes clearly marked synthetic telemetry.")
    except urllib.error.URLError as exc:
        raise SystemExit(f"Could not reach {args.url}. Start the local app first. {exc}") from exc


if __name__ == "__main__":
    main()
