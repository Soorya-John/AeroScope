from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = Path(os.environ.get("AEROSCOPE_DATA_DIR", ROOT / "data")).expanduser().resolve()
LOG_DIR = DATA_DIR / "logs"
DB_PATH = DATA_DIR / "aeroscope.sqlite3"
HOST = os.environ.get("AEROSCOPE_HOST", "127.0.0.1")
PORT = int(os.environ.get("AEROSCOPE_PORT", "8765"))
MAX_UPLOAD_BYTES = int(os.environ.get("AEROSCOPE_MAX_UPLOAD_MB", "128")) * 1024 * 1024
MAP_STYLE_URL = os.environ.get("AEROSCOPE_MAP_STYLE_URL", "")
PROTOCOL_VERSION = 1

DATA_DIR.mkdir(parents=True, exist_ok=True)
LOG_DIR.mkdir(parents=True, exist_ok=True)
