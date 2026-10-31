from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import DB_PATH


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class Store:
    """Small SQLite repository. Each operation owns a short-lived connection."""

    def __init__(self, path: Path = DB_PATH) -> None:
        self.path = path
        self._schema_lock = threading.Lock()
        self.initialize()

    @contextmanager
    def _connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys = ON")
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def initialize(self) -> None:
        with self._schema_lock, self._connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS events (
                    id TEXT PRIMARY KEY, type TEXT NOT NULL, severity TEXT NOT NULL,
                    source TEXT NOT NULL, description TEXT NOT NULL, evidence TEXT NOT NULL,
                    started_at TEXT NOT NULL, ended_at TEXT, acknowledged INTEGER NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS events_started ON events(started_at DESC);
                CREATE TABLE IF NOT EXISTS profiles (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, vehicle_type TEXT,
                    expected_autopilot TEXT, connection_json TEXT NOT NULL,
                    telemetry_profile TEXT NOT NULL DEFAULT 'standard', notes TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS analyses (
                    id TEXT PRIMARY KEY, original_name TEXT NOT NULL, stored_name TEXT NOT NULL,
                    format TEXT NOT NULL, size_bytes INTEGER NOT NULL, status TEXT NOT NULL,
                    created_at TEXT NOT NULL, result_json TEXT, error TEXT
                );
                CREATE TABLE IF NOT EXISTS preferences (
                    key TEXT PRIMARY KEY, value_json TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS recordings (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, stored_name TEXT NOT NULL,
                    started_at TEXT NOT NULL, ended_at TEXT, sample_count INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS parameter_snapshots (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, created_at TEXT NOT NULL, parameters_json TEXT NOT NULL
                );
            """)

    def add_event(self, event: dict[str, Any]) -> None:
        with self._connect() as db:
            db.execute("""INSERT OR IGNORE INTO events
                (id,type,severity,source,description,evidence,started_at,ended_at,acknowledged)
                VALUES (?,?,?,?,?,?,?,?,?)""", (
                event["id"], event["type"], event["severity"], event["source"],
                event["description"], json.dumps(event.get("evidence", {})),
                event["started_at"], event.get("ended_at"), int(event.get("acknowledged", False))))

    def close_event(self, event_id: str, ended_at: str) -> None:
        with self._connect() as db:
            db.execute("UPDATE events SET ended_at=? WHERE id=? AND ended_at IS NULL", (ended_at, event_id))

    def list_events(self, limit: int = 200) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute("SELECT * FROM events ORDER BY started_at DESC LIMIT ?", (max(1, min(limit, 1000)),)).fetchall()
        return [{**dict(row), "evidence": json.loads(row["evidence"]), "acknowledged": bool(row["acknowledged"])} for row in rows]

    def acknowledge(self, event_id: str) -> bool:
        with self._connect() as db:
            cur = db.execute("UPDATE events SET acknowledged=1 WHERE id=?", (event_id,))
            return cur.rowcount > 0

    def save_profile(self, profile: dict[str, Any]) -> dict[str, Any]:
        profile_id = profile.get("id") or str(uuid.uuid4())
        created = profile.get("created_at") or now_iso()
        updated = now_iso()
        with self._connect() as db:
            db.execute("""INSERT INTO profiles(id,name,vehicle_type,expected_autopilot,connection_json,telemetry_profile,notes,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name,
                vehicle_type=excluded.vehicle_type, expected_autopilot=excluded.expected_autopilot,
                connection_json=excluded.connection_json, telemetry_profile=excluded.telemetry_profile,
                notes=excluded.notes, updated_at=excluded.updated_at""", (
                profile_id, profile["name"][:80], profile.get("vehicle_type"), profile.get("expected_autopilot"),
                json.dumps(profile.get("connection", {})), profile.get("telemetry_profile", "standard"),
                profile.get("notes", "")[:2000], created, updated))
        return {**profile, "id": profile_id, "created_at": created, "updated_at": updated}

    def list_profiles(self) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute("SELECT * FROM profiles ORDER BY updated_at DESC").fetchall()
        return [{**dict(row), "connection": json.loads(row["connection_json"])} for row in rows]

    def delete_profile(self, profile_id: str) -> bool:
        with self._connect() as db:
            return db.execute("DELETE FROM profiles WHERE id=?", (profile_id,)).rowcount > 0

    def create_analysis(self, name: str, stored_name: str, fmt: str, size: int) -> str:
        analysis_id = str(uuid.uuid4())
        with self._connect() as db:
            db.execute("INSERT INTO analyses(id,original_name,stored_name,format,size_bytes,status,created_at) VALUES(?,?,?,?,?,'queued',?)",
                       (analysis_id, name[:255], stored_name, fmt, size, now_iso()))
        return analysis_id

    def save_analysis_result(self, analysis_id: str, result: dict[str, Any]) -> None:
        with self._connect() as db:
            db.execute("UPDATE analyses SET status='complete',result_json=?,error=NULL WHERE id=?", (json.dumps(result), analysis_id))

    def fail_analysis(self, analysis_id: str, error: str) -> None:
        with self._connect() as db:
            db.execute("UPDATE analyses SET status='error',error=? WHERE id=?", (error[:1000], analysis_id))

    def get_analysis(self, analysis_id: str) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute("SELECT * FROM analyses WHERE id=?", (analysis_id,)).fetchone()
        if row is None:
            return None
        value = dict(row)
        value["result"] = json.loads(value.pop("result_json")) if value.get("result_json") else None
        return value

    def list_analyses(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute("SELECT id,original_name,format,size_bytes,status,created_at,error FROM analyses ORDER BY created_at DESC LIMIT ?", (min(max(limit, 1), 200),)).fetchall()
        return [dict(row) for row in rows]

    def save_preference(self, key: str, value: Any) -> None:
        with self._connect() as db:
            db.execute("INSERT INTO preferences(key,value_json,updated_at) VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json,updated_at=excluded.updated_at",
                       (key[:100], json.dumps(value), now_iso()))

    def load_preferences(self) -> dict[str, Any]:
        with self._connect() as db:
            rows = db.execute("SELECT key,value_json FROM preferences").fetchall()
        return {r["key"]: json.loads(r["value_json"]) for r in rows}

    def start_recording(self, name: str, stored_name: str) -> dict[str, Any]:
        session_id = str(uuid.uuid4())
        started = now_iso()
        with self._connect() as db:
            db.execute("INSERT INTO recordings(id,name,stored_name,started_at) VALUES(?,?,?,?)", (session_id, name[:100], stored_name, started))
        return {"id": session_id, "name": name[:100], "stored_name": stored_name, "started_at": started, "ended_at": None, "sample_count": 0}

    def end_recording(self, session_id: str, sample_count: int) -> None:
        with self._connect() as db:
            db.execute("UPDATE recordings SET ended_at=?,sample_count=? WHERE id=?", (now_iso(), sample_count, session_id))

    def list_recordings(self) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute("SELECT id,name,started_at,ended_at,sample_count FROM recordings ORDER BY started_at DESC LIMIT 100").fetchall()
        return [dict(r) for r in rows]

    def save_parameter_snapshot(self, name: str, parameters: list[dict[str, Any]]) -> dict[str, Any]:
        snapshot={"id":str(uuid.uuid4()),"name":name.strip()[:100] or "Parameter snapshot","created_at":now_iso(),"parameters":parameters}
        with self._connect() as db:
            db.execute("INSERT INTO parameter_snapshots(id,name,created_at,parameters_json) VALUES(?,?,?,?)",
                       (snapshot["id"],snapshot["name"],snapshot["created_at"],json.dumps(parameters)))
        return snapshot

    def list_parameter_snapshots(self) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows=db.execute("SELECT id,name,created_at FROM parameter_snapshots ORDER BY created_at DESC LIMIT 100").fetchall()
        return [dict(r) for r in rows]

    def get_parameter_snapshot(self, snapshot_id: str) -> dict[str, Any] | None:
        with self._connect() as db:
            row=db.execute("SELECT id,name,created_at,parameters_json FROM parameter_snapshots WHERE id=?",(snapshot_id,)).fetchone()
        if row is None:return None
        result=dict(row); result["parameters"]=json.loads(result.pop("parameters_json")); return result
