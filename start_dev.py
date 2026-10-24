from __future__ import annotations

import runpy
import sys
from pathlib import Path

root = Path(__file__).resolve().parent
backend = root / "backend"
sys.path.insert(0, str(backend))
try:
    import pymavlink  # noqa: F401
    mavlink_message = "pymavlink available"
except ImportError:
    mavlink_message = "pymavlink unavailable; simulation and log upload validation remain available"
print(f"AeroScope · {mavlink_message}")
if not __import__("os").environ.get("AEROSCOPE_USE_STDLIB"):
    try:
        import fastapi  # noqa: F401
        import uvicorn  # noqa: F401
        from app.asgi import run as run_fastapi
    except ImportError:
        runpy.run_module("app.main", run_name="__main__")
    else:
        print("FastAPI transport available")
        run_fastapi()
else:
    runpy.run_module("app.main", run_name="__main__")
