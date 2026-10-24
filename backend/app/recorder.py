from __future__ import annotations

import json
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from .config import DATA_DIR
from .state import VehicleState
from .store import Store


class TelemetryRecorder:
    """Optional local JSONL recorder, separate from uploaded-flight analysis."""

    def __init__(self, store: Store, state: VehicleState) -> None:
        self.store, self.state = store, state
        self.directory = DATA_DIR / "recordings"
        self.directory.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._session: dict[str, Any] | None = None
        self._count = 0

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {"recording": bool(self._session), "session": self._session}

    def start(self, name: str = "Telemetry recording") -> dict[str, Any]:
        with self._lock:
            if self._session:
                raise ValueError("A telemetry recording is already active")
            filename = f"{uuid.uuid4().hex}.jsonl"
            session = self.store.start_recording(name, filename)
            self._session = session
            self._count = 0
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="aeroscope-recorder", daemon=True)
            self._thread.start()
            return session

    def stop(self) -> dict[str, Any] | None:
        self._stop.set()
        with self._lock:
            thread, session = self._thread, self._session
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=3)
        with self._lock:
            if session:
                self.store.end_recording(session["id"], self._count)
                session["ended_at"] = __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat()
                session["sample_count"] = self._count
            self._session = None
            self._thread = None
        return session

    def list(self) -> list[dict[str, Any]]:
        return self.store.list_recordings()

    def _run(self) -> None:
        with self._lock:
            session = dict(self._session or {})
        if not session:
            return
        target = (self.directory / session["stored_name"]).resolve()
        if target.parent != self.directory.resolve():
            self.stop()
            return
        with target.open("x", encoding="utf-8") as output:
            while not self._stop.is_set():
                row = {"received_at": __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat(),
                       "state": self.state.snapshot()}
                output.write(json.dumps(row, separators=(",", ":")) + "\n")
                output.flush()
                with self._lock:
                    self._count += 1
                self._stop.wait(1.0)
