"""Session-scoped MAVLink command transaction bookkeeping.

This module intentionally does not write to pymavlink. The authoritative reader
calls ``acknowledge`` and the manager's serialized writer calls ``mark_sent``.
"""
from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any


TERMINAL = {"SUCCEEDED", "FAILED", "TIMED_OUT", "CANCELLED", "UNKNOWN"}
RESULTS = {0: "ACCEPTED", 1: "TEMPORARILY_REJECTED", 2: "DENIED", 3: "UNSUPPORTED",
           4: "FAILED", 5: "IN_PROGRESS", 6: "CANCELLED"}


@dataclass
class Transaction:
    id: str
    session_id: str
    command: int
    target_system: int
    target_component: int
    timeout: float
    safe_retry: bool = False
    state: str = "QUEUED"
    created: float = field(default_factory=time.monotonic)
    sent: float | None = None
    progress: int | None = None
    result: str | None = None
    detail: str | None = None
    retries: int = 0

    def public(self) -> dict[str, Any]:
        return {"id": self.id, "session_id": self.session_id, "command": self.command,
                "target_system": self.target_system, "target_component": self.target_component,
                "state": self.state, "progress": self.progress, "result": self.result,
                "detail": self.detail, "retries": self.retries}


class TransactionManager:
    """Correlates ACKs by session, source target, and command ID."""
    def __init__(self, history_limit: int = 200) -> None:
        self._lock = threading.RLock()
        self._items: dict[str, Transaction] = {}
        self._session: str | None = None
        self._history_limit = history_limit

    def begin_session(self, session_id: str) -> None:
        with self._lock:
            self._cancel_all("Connection session changed")
            self._session = session_id

    def create(self, command: int, system: int, component: int, timeout: float = 5.0,
               safe_retry: bool = False) -> Transaction:
        if not 0 < timeout <= 120: raise ValueError("timeout must be between 0 and 120 seconds")
        with self._lock:
            if not self._session: raise RuntimeError("No active MAVLink session")
            if any(t.session_id==self._session and t.command==command and t.state=='UNKNOWN' for t in self._items.values()):
                raise RuntimeError('Previous command outcome is unknown; reconnect and inspect vehicle before repeating this command')
            # ACK has no transaction token: never permit ambiguous same-command concurrency.
            if any(t.session_id == self._session and t.command == command and t.state not in TERMINAL
                   for t in self._items.values()):
                raise RuntimeError("A transaction for this command is already pending")
            t = Transaction(str(uuid.uuid4()), self._session, int(command), int(system), int(component), timeout, safe_retry)
            self._items[t.id] = t
            self._trim()
            return t

    def mark_sent(self, transaction_id: str) -> None:
        with self._lock:
            t = self._items[transaction_id]
            if t.state != "QUEUED": return
            if t.session_id != self._session: t.state = "CANCELLED"; t.detail = "Stale connection session"; return
            t.state, t.sent = "WAITING_ACK", time.monotonic()

    def transmission_failed(self, transaction_id: str, detail: str) -> None:
        with self._lock:
            t = self._items[transaction_id]
            if t.state not in TERMINAL:
                t.state, t.detail = "FAILED", f"Transmission failed: {detail}"[:500]

    def acknowledge(self, command: int, system: int, component: int, result: int,
                    progress: int | None = None, detail: str | None = None) -> dict[str, Any] | None:
        with self._lock:
            candidates = [t for t in self._items.values() if t.session_id == self._session and
                          t.state in {"WAITING_ACK", "IN_PROGRESS"} and t.command == command and
                          t.target_system == system and t.target_component in (component, 0)]
            if len(candidates) != 1: return None
            t = candidates[0]; t.result = RESULTS.get(int(result), f"UNKNOWN_RESULT_{result}")
            t.detail = detail
            if result == 5:
                t.state, t.progress = "IN_PROGRESS", progress
            elif result == 0: t.state, t.progress = "SUCCEEDED", progress
            else: t.state = "FAILED"
            return t.public()

    def expire(self, now: float | None = None) -> list[dict[str, Any]]:
        now = time.monotonic() if now is None else now
        with self._lock:
            out = []
            for t in self._items.values():
                if t.state in {"WAITING_ACK", "IN_PROGRESS"} and t.sent is not None and now - t.sent >= t.timeout:
                    t.state, t.detail = "UNKNOWN", "Outcome uncertain: acknowledgement timed out"
                    out.append(t.public())
            return out

    def cancel_session(self, session_id: str, reason: str = "Connection lost") -> None:
        with self._lock:
            for t in self._items.values():
                if t.session_id == session_id and t.state not in TERMINAL:
                    t.state, t.detail = "CANCELLED", reason
            if self._session == session_id: self._session = None

    def list(self) -> list[dict[str, Any]]:
        with self._lock: return [t.public() for t in reversed(list(self._items.values()))]

    def _cancel_all(self, reason: str) -> None:
        for t in self._items.values():
            if t.state not in TERMINAL: t.state, t.detail = "CANCELLED", reason

    def _trim(self) -> None:
        while len(self._items) > self._history_limit:
            key = next((k for k, v in self._items.items() if v.state in TERMINAL), None)
            if key is None: break
            self._items.pop(key)
