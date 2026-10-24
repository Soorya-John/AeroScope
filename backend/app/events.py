from __future__ import annotations

import threading
import uuid
from typing import Any

from .store import Store, now_iso

SEVERITIES = {"informational", "low", "medium", "high", "critical"}


class EventEngine:
    """Rule-based alerts with active-event deduplication and recovery timestamps."""

    def __init__(self, store: Store) -> None:
        self.store = store
        self._lock = threading.Lock()
        self._active: dict[str, str] = {}
        self._memory: list[dict[str, Any]] = []
        self.vibration_threshold = 30.0

    def trigger(self, kind: str, severity: str, source: str, description: str,
                evidence: dict[str, Any] | None = None) -> dict[str, Any] | None:
        with self._lock:
            if kind in self._active:
                return None
            event = {"id": str(uuid.uuid4()), "type": kind, "severity": severity,
                     "source": source, "description": description, "evidence": evidence or {},
                     "started_at": now_iso(), "ended_at": None, "acknowledged": False}
            self._active[kind] = event["id"]
            self._memory.insert(0, event)
            del self._memory[500:]
        self.store.add_event(event)
        return event

    def recover(self, kind: str) -> None:
        with self._lock:
            event_id = self._active.pop(kind, None)
            if event_id:
                for event in self._memory:
                    if event["id"] == event_id:
                        event["ended_at"] = now_iso()
                        break
        if event_id:
            self.store.close_event(event_id, now_iso())

    def evaluate(self, state: dict[str, Any]) -> list[dict[str, Any]]:
        emitted: list[dict[str, Any]] = []
        conn = state["connection"]
        hb_age = conn.get("heartbeat_age_s")
        if conn.get("state") in ("stale", "reconnecting") or (hb_age is not None and hb_age > 5):
            event = self.trigger("link.heartbeat_lost", "high", "AeroScope", "Vehicle heartbeat has been lost or telemetry is stale.",
                                 {"heartbeat_age_s": hb_age, "connection_state": conn.get("state")})
            if event: emitted.append(event)
        else:
            self.recover("link.heartbeat_lost")
        fix = state["gps"]["fix_type"].get("value") if state["gps"]["fix_type"].get("available") else None
        if fix is not None and fix < 3:
            event = self.trigger("gps.fix_degraded", "medium", "AeroScope", "GPS fix is below 3D quality.", {"fix_type": fix})
            if event: emitted.append(event)
        else:
            self.recover("gps.fix_degraded")
        remaining = state["battery"]["remaining_percent"].get("value") if state["battery"]["remaining_percent"].get("available") else None
        voltage = state["battery"]["voltage_v"].get("value") if state["battery"]["voltage_v"].get("available") else None
        if remaining is not None and remaining <= 20:
            event = self.trigger("battery.low", "high" if remaining <= 10 else "medium", "Autopilot telemetry", "Reported battery remaining is low.", {"remaining_percent": remaining, "voltage_v": voltage})
            if event: emitted.append(event)
        else:
            self.recover("battery.low")
        vib = state["vibration"]
        peak = max([vib[k]["value"] for k in ("x", "y", "z") if vib[k].get("available")], default=None)
        if peak is not None and peak >= self.vibration_threshold:
            event = self.trigger("vibration.elevated", "medium", "AeroScope rule", "Vibration exceeds the configured threshold; this is a screening alert, not a component diagnosis.", {"peak_axis_value": peak, "threshold": self.vibration_threshold, "units": "m/s² (firmware dependent)"})
            if event: emitted.append(event)
        else:
            self.recover("vibration.elevated")
        rssi_field = state["rc"]["rssi_percent"]
        rssi = rssi_field.get("value") if rssi_field.get("available") else None
        if rssi is not None and rssi <= 10:
            event = self.trigger("rc.signal_degraded", "high" if rssi == 0 else "medium", "Vehicle telemetry", "The reported RC signal level is low.", {"rssi_percent": rssi})
            if event: emitted.append(event)
        else:
            self.recover("rc.signal_degraded")
        return emitted

    def autopilot_text(self, text: str, severity: str = "medium") -> dict[str, Any] | None:
        key = "autopilot." + str(abs(hash(text)))
        source = "Simulated autopilot STATUSTEXT" if text and self._simulated_text(text) else "Autopilot STATUSTEXT"
        return self.trigger(key, severity if severity in SEVERITIES else "medium", source, text[:500], {"reported_text": text[:500]})

    @staticmethod
    def _simulated_text(text: str) -> bool:
        # Called by the normalizer for synthetic STATUSTEXT. The explicit prefix
        # prevents demo events from being presented as real flight-controller data.
        return text.startswith("SIMULATED:")

    def list_events(self, limit: int = 200) -> list[dict[str, Any]]:
        return self.store.list_events(limit)

    def acknowledge(self, event_id: str) -> bool:
        return self.store.acknowledge(event_id)
