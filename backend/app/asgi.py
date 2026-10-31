"""Optional FastAPI transport layer for environments with the web extra installed.

The standard-library server remains the zero-dependency local fallback. Both
transports call the same state, parser, storage, simulation, and MAVLink services.
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import uuid
from contextlib import asynccontextmanager
from email import policy
from email.parser import BytesParser
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, Response

from .config import HOST, LOG_DIR, MAX_UPLOAD_BYTES, PORT, ROOT
from .events import EventEngine
from .log_analysis import LogAnalyzer, safe_log_path
from .mavlink import MavlinkManager
from .recorder import TelemetryRecorder
from .simulator import SCENARIOS, Simulator
from .state import VehicleState
from .store import Store, now_iso
from .telemetry import TelemetryNormalizer

from .main import store, vehicle_state, events, normalizer, simulator, mavlink, analyzer, recorder
from .workflows import GET_ROUTES, POST_ROUTES, vehicle_mutation
STATIC = ROOT / "frontend" / "static"
ASSETS = {"app.js": "text/javascript", "styles.css": "text/css"}
ASSETS.update({'workflows.js':'text/javascript','planner.js':'text/javascript','workflows.css':'text/css','vendor/leaflet.js':'text/javascript','vendor/leaflet.css':'text/css','vendor/LICENSE':'text/plain'})


@asynccontextmanager
async def lifespan(_app):
    yield
    simulator.stop(); mavlink.disconnect(); recorder.stop()


app = FastAPI(title="AeroScope", version="0.1.0", docs_url="/api/docs", redoc_url=None, lifespan=lifespan)

@app.middleware('http')
async def workflows_transport(request, call_next):
    path=request.url.path
    try:
        if request.method=='GET' and path in GET_ROUTES:
            return JSONResponse(await asyncio.to_thread(mavlink.workflows.dispatch,'GET',path,{},None))
        if request.method=='POST':
            same_origin(request); client=request.headers.get('X-AeroScope-Client')
            if path in POST_ROUTES:
                raw=await request.body()
                if len(raw)>65536: raise ValueError('Request exceeds 64 KiB')
                return JSONResponse(await asyncio.to_thread(mavlink.workflows.dispatch,'POST',path,request_object(raw),client))
            if vehicle_mutation(path): mavlink.workflows.require_owner(client)
        return await call_next(request)
    except (ValueError,RuntimeError) as exc:
        return JSONResponse({'error':str(exc),'code':'operation_unavailable'},status_code=409 if isinstance(exc,RuntimeError) else 400)
    except HTTPException as exc:
        return JSONResponse({'error':str(exc.detail)},status_code=exc.status_code)


def same_origin(request: Request) -> None:
    origin = request.headers.get("origin")
    if origin and urlsplit(origin).netloc.lower() != request.headers.get("host", "").lower():
        raise HTTPException(403, "Cross-origin request denied")


def request_object(raw: bytes) -> dict:
    try: value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc: raise HTTPException(400, "Invalid JSON body") from exc
    if not isinstance(value, dict): raise HTTPException(400, "JSON request body must be an object")
    return value


def service_error(exc: Exception) -> HTTPException:
    """Keep service validation and unavailable-session errors consistent with stdlib HTTP."""
    return HTTPException(409 if isinstance(exc, RuntimeError) else 400, str(exc))


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html", media_type="text/html", headers={"Cache-Control":"no-store"})


@app.get('/vendor/{name}')
def vendor_asset(name: str): return asset('vendor/'+name)

@app.get("/{asset}")
def asset(asset: str):
    if asset not in ASSETS: raise HTTPException(404, "Asset not found")
    return FileResponse(STATIC / asset, media_type=ASSETS[asset], headers={"Cache-Control":"no-store"})


@app.get("/api/health")
def health(): return {"status":"ok","product":"AeroScope","version":"0.1.0","protocol_version":1,"time":now_iso()}


@app.get("/api/state")
def get_state(): return {"protocol_version":1,"data":vehicle_state.snapshot(),"simulation":simulator.status(),"mavlink":mavlink.status()}


@app.get("/api/control/status")
def control_status(): return {"protocol_version":1,"connection":mavlink.status(),"transactions":mavlink.transactions.list()}


@app.get("/api/control/capabilities")
def control_capabilities(): return {"protocol_version":1,"capabilities":mavlink.capabilities()}


@app.get("/api/control/mode")
def control_mode(): return {"protocol_version":1,"current":vehicle_state.snapshot()["status"]["flight_mode"],"request":mavlink.mode_status()}


@app.post("/api/control/mode")
async def set_control_mode(request: Request):
    same_origin(request); body=request_object(await request.body())
    try: return JSONResponse({"request":mavlink.request_mode(str(body.get("mode", "")),confirmed=body.get("confirmed") is True)},status_code=202)
    except (ValueError,RuntimeError) as exc: raise service_error(exc) from exc


@app.get("/api/control/arm")
def control_arm(): return {"protocol_version":1,"armed":vehicle_state.snapshot()["status"]["armed"],"request":mavlink.arm_status()}


@app.post("/api/control/arm")
async def set_control_arm(request: Request):
    same_origin(request); body=request_object(await request.body())
    try: return JSONResponse({"request":mavlink.request_arm(body.get("armed"),confirmation=str(body.get("confirmation", "")),acknowledge_unknown=body.get("acknowledge_unknown") is True)},status_code=202)
    except (ValueError,RuntimeError) as exc: raise service_error(exc) from exc


@app.get("/api/parameters")
def get_parameters(search: str = ""):
    from .metadata import parameter_metadata
    data=mavlink.parameters.snapshot(search);metadata=parameter_metadata(vehicle_state.snapshot()['vehicle'])
    for p in data['parameters']:p['metadata']=metadata.get(p['name'],{})
    return {'protocol_version':1,**data}


@app.post("/api/parameters/request")
async def request_parameters(request: Request):
    same_origin(request)
    try: mavlink.request_parameters(); return JSONResponse({"accepted":True},status_code=202)
    except RuntimeError as exc: raise service_error(exc) from exc


@app.post("/api/parameters/refresh")
async def refresh_parameter(request: Request):
    same_origin(request); body=request_object(await request.body())
    try: mavlink.refresh_parameter(str(body.get("name", ""))); return JSONResponse({"accepted":True},status_code=202)
    except (ValueError,RuntimeError) as exc: raise service_error(exc) from exc


@app.post("/api/parameters/set")
async def set_parameter(request: Request):
    same_origin(request); body=request_object(await request.body())
    try: mavlink.set_parameter(str(body.get("name", "")),body.get("value"),confirmed=body.get("confirmed") is True); return JSONResponse({"accepted":True},status_code=202)
    except (ValueError,RuntimeError) as exc: raise service_error(exc) from exc


@app.get("/api/events")
def get_events(limit: int = 100): return {"events":events.list_events(limit)}


@app.get("/api/inspector")
def get_inspector(limit: int = 200):
    limit=min(500,max(1,limit))
    return {"messages":normalizer.inspector()[:limit],"rates":vehicle_state.rate_by_type(),
            "counts":vehicle_state.count_by_type(),"total":vehicle_state.snapshot()["connection"]["messages_total"]}


@app.get("/api/series")
def get_series(limit: int = 2000): return {"samples":normalizer.series(limit)}


@app.post("/api/simulation")
async def simulation(request: Request):
    same_origin(request); body=request_object(await request.body()); action=body.get("action")
    try:
        if action == "start":
            mavlink.disconnect(); result=simulator.start(body.get("scenario","normal"),float(body.get("speed",1)))
        elif action == "stop": simulator.stop(); result=simulator.status()
        elif action == "reset": simulator.reset(); result=simulator.status()
        else: raise ValueError("action must be start, stop, or reset")
        return {"simulation":result}
    except (ValueError, TypeError) as exc: raise service_error(exc) from exc


@app.post("/api/connection")
async def connect(request: Request):
    same_origin(request); body=request_object(await request.body()); simulator.stop()
    try: return JSONResponse({"connection":mavlink.connect(body)},status_code=202)
    except (ValueError,RuntimeError) as exc: raise service_error(exc) from exc


@app.post("/api/connection/disconnect")
async def disconnect(request: Request):
    same_origin(request); mavlink.disconnect(); simulator.stop()
    return {"connection":mavlink.status(),"data":vehicle_state.snapshot()}


@app.get("/api/parameters/snapshots")
def parameter_snapshots(): return {"snapshots":store.list_parameter_snapshots()}

@app.post("/api/parameters/snapshot")
async def save_parameter_snapshot(request: Request):
    same_origin(request); body=request_object(await request.body()); params=mavlink.parameters.snapshot()["parameters"]
    if not params: raise HTTPException(400,"No downloaded parameters to snapshot")
    return JSONResponse({"snapshot":store.save_parameter_snapshot(str(body.get("name","")),params)},status_code=201)

@app.get("/api/parameters/compare")
def compare_parameters(left: str, right: str):
    a=store.get_parameter_snapshot(left); b=store.get_parameter_snapshot(right)
    if not a or not b: raise HTTPException(400,"Both valid snapshot IDs are required")
    from .parameters import compare_parameter_snapshots
    return {"changes":compare_parameter_snapshots(a["parameters"],b["parameters"])}

@app.get("/api/parameters/export")
def export_parameters(format: str = "json"):
    rows=mavlink.parameters.snapshot()["parameters"]
    if format=="json": return {"parameters":rows}
    if format!="csv": raise HTTPException(400,"format must be json or csv")
    out=io.StringIO(newline=""); writer=csv.DictWriter(out,fieldnames=["name","value","type","index","count","received_at"]); writer.writeheader(); writer.writerows(rows)
    return Response(out.getvalue(),media_type="text/csv",headers={"Content-Disposition":"attachment; filename=aeroscope-parameters.csv"})
@app.get("/api/simulation/control")
def simulation_control_status(): return {"control":simulator.control_status()}

@app.post("/api/simulation/control")
async def simulation_control(request: Request):
    same_origin(request); body=request_object(await request.body())
    try:
        if body.get("behavior") is not None: simulator.set_control_behavior(str(body["behavior"]))
        if body.get("operation") is not None:
            params=body.get("params",{})
            if not isinstance(params,dict): raise ValueError("params must be an object")
            result=simulator.submit_control(str(body["operation"]),**params)
        else: result=simulator.control_status()
        return JSONResponse({"control":result},status_code=202)
    except (ValueError,RuntimeError,TypeError) as exc: raise service_error(exc) from exc
@app.get("/api/mission")
def get_mission(): return {"protocol_version":1,**mavlink.missions.snapshot()}

@app.post("/api/mission/download")
async def download_mission(request: Request):
    same_origin(request)
    try:
        mavlink.request_mission_download()
        return JSONResponse({"accepted":True},status_code=202)
    except RuntimeError as exc: raise service_error(exc) from exc

@app.post("/api/mission/upload")
async def upload_mission(request: Request):
    same_origin(request); body=request_object(await request.body())
    try: return JSONResponse({"mission":mavlink.upload_mission(body.get("items"),confirmed=body.get("confirmed") is True)},status_code=202)
    except (ValueError,RuntimeError) as exc: raise service_error(exc) from exc
@app.post("/api/mission/clear")
async def clear_mission(request: Request):
    same_origin(request); body=request_object(await request.body())
    try: return JSONResponse({"mission":mavlink.clear_mission(confirmed=body.get("confirmed") is True)},status_code=202)
    except (ValueError,RuntimeError) as exc: raise service_error(exc) from exc
@app.post("/api/guided/reposition")
async def guided_reposition(request: Request):
    same_origin(request); body=request_object(await request.body())
    try:
        tx=mavlink.guided_reposition(body.get("latitude"),body.get("longitude"),body.get("altitude"),body.get("speed",-1),confirmation=str(body.get("confirmation","")),acknowledge_unknown=body.get("acknowledge_unknown") is True)
        return JSONResponse({"transaction":tx},status_code=202)
    except (ValueError,RuntimeError,TypeError) as exc: raise service_error(exc) from exc
@app.post("/api/guided/takeoff")
async def guided_takeoff(request: Request):
    same_origin(request); body=request_object(await request.body())
    try: return JSONResponse({"transaction":mavlink.guided_takeoff(body.get("altitude"),confirmation=str(body.get("confirmation","")),acknowledge_unknown=body.get("acknowledge_unknown") is True)},status_code=202)
    except (ValueError,RuntimeError,TypeError) as exc: raise service_error(exc) from exc

@app.post("/api/guided/land")
async def guided_land(request: Request):
    same_origin(request); body=request_object(await request.body())
    try: return JSONResponse({"transaction":mavlink.guided_land(confirmation=str(body.get("confirmation","")),acknowledge_unknown=body.get("acknowledge_unknown") is True)},status_code=202)
    except (ValueError,RuntimeError,TypeError) as exc: raise service_error(exc) from exc

@app.post("/api/guided/speed")
async def guided_speed(request: Request):
    same_origin(request); body=request_object(await request.body())
    try: return JSONResponse({"transaction":mavlink.guided_speed(body.get("speed"),confirmation=str(body.get("confirmation","")),acknowledge_unknown=body.get("acknowledge_unknown") is True)},status_code=202)
    except (ValueError,RuntimeError,TypeError) as exc: raise service_error(exc) from exc
@app.get("/api/profiles")
def profiles(): return {"profiles":store.list_profiles()}


@app.post("/api/profiles")
async def save_profile(request: Request):
    same_origin(request); body=request_object(await request.body())
    if not str(body.get("name", "")).strip(): raise HTTPException(400,"Profile name is required")
    conn=body.get("connection",{})
    if not isinstance(conn,dict) or any("password" in k.lower() or "secret" in k.lower() for k in conn):
        raise HTTPException(400,"Connection profiles may not store passwords or secrets")
    return JSONResponse({"profile":store.save_profile(body)},status_code=201)


@app.delete("/api/profiles/{profile_id}")
def delete_profile(profile_id: str):
    ok=store.delete_profile(profile_id)
    if not ok: raise HTTPException(404,"Profile not found")
    return {"deleted":True}


@app.get("/api/settings")
def settings(): return {"settings":store.load_preferences(),"map_style_configured":bool(os.environ.get("AEROSCOPE_MAP_STYLE_URL",""))}


@app.post("/api/settings")
async def save_settings(request: Request):
    same_origin(request); body=request_object(await request.body())
    for key,value in body.items():
        if key not in {"theme","map_style_url","telemetry_profile","name"}: continue
        if key == "theme" and value not in ("dark","light"): raise HTTPException(400,"theme must be dark or light")
        if key == "map_style_url" and value and not str(value).startswith(("https://","http://")):
            raise HTTPException(400,"Map style must use HTTP or HTTPS")
        store.save_preference(key,value)
    return {"settings":store.load_preferences()}


@app.get("/api/logs")
def list_logs(): return {"analyses":store.list_analyses(),"recordings":recorder.list()}


@app.get("/api/analysis/{analysis_id}")
def get_analysis(analysis_id: str):
    item=store.get_analysis(analysis_id)
    if not item: raise HTTPException(404,"Analysis not found")
    return item


@app.delete("/api/analysis/{analysis_id}")
def delete_analysis(analysis_id: str):
    item=store.get_analysis(analysis_id)
    if not item: raise HTTPException(404,"Analysis not found")
    try: safe_log_path(item["stored_name"]).unlink(missing_ok=True)
    except ValueError as exc: raise HTTPException(400,"Invalid stored file path") from exc
    with store._connect() as db: db.execute("DELETE FROM analyses WHERE id=?",(analysis_id,))
    return {"deleted":True}


@app.post("/api/logs/upload")
async def upload_log(request: Request):
    same_origin(request)
    try: size=int(request.headers.get("content-length","0"))
    except ValueError as exc: raise HTTPException(400,"Invalid Content-Length") from exc
    if size<=0 or size>MAX_UPLOAD_BYTES+1024*1024: raise HTTPException(413,"Upload size is invalid or exceeds the configured limit")
    content_type=request.headers.get("content-type","")
    if not content_type.lower().startswith("multipart/form-data;"): raise HTTPException(400,"Upload must use multipart/form-data")
    body=await request.body()
    if len(body)>MAX_UPLOAD_BYTES+1024*1024: raise HTTPException(413,"Upload exceeds the configured size limit")
    envelope=b"Content-Type: "+content_type.encode("ascii","replace")+b"\r\nMIME-Version: 1.0\r\n\r\n"+body
    message=BytesParser(policy=policy.default).parsebytes(envelope)
    files=[part for part in message.iter_parts() if part.get_content_disposition()=="form-data" and part.get_filename()]
    if len(files)!=1: raise HTTPException(400,"Upload must contain exactly one file")
    filename=Path(str(files[0].get_filename()).replace("\\","/")).name
    if re.search(r"\.(exe|dll|ps1|py|sh|js)$",filename,re.I): raise HTTPException(400,"Executable and script uploads are not accepted")
    payload=files[0].get_payload(decode=True)
    if payload is None or len(payload)>MAX_UPLOAD_BYTES: raise HTTPException(413,"Uploaded file is empty or exceeds the configured limit")
    try: return JSONResponse(analyzer.upload_bytes(filename,payload),status_code=202)
    except ValueError as exc: raise HTTPException(400,str(exc)) from exc


@app.get("/api/recording")
def recording_status(): return recorder.status()


@app.get("/api/recordings")
def recordings(): return {"recordings":recorder.list()}


@app.post("/api/recording/start")
async def start_recording(request: Request):
    same_origin(request); body=request_object(await request.body())
    if vehicle_state.snapshot()["connection"]["state"] not in ("connected","stale"):
        raise HTTPException(400,"Connect a vehicle or start simulation before recording")
    try: return JSONResponse({"session":recorder.start(str(body.get("name","Telemetry recording")))},status_code=201)
    except ValueError as exc: raise HTTPException(400,str(exc)) from exc


@app.post("/api/recording/stop")
async def stop_recording(request: Request):
    same_origin(request); return {"session":recorder.stop()}


@app.post("/api/events/acknowledge")
async def acknowledge(request: Request):
    same_origin(request); body=request_object(await request.body()); event_id=str(body.get("id",""))
    if not event_id: raise HTTPException(400,"Event id is required")
    ok=events.acknowledge(event_id)
    if not ok: raise HTTPException(404,"Event not found")
    return {"acknowledged":True}


@app.get("/api/export")
def export(kind: str="inspector", format: str="json"):
    import csv, io
    if kind=="telemetry": rows=normalizer.series(6000)
    elif kind=="inspector": rows=normalizer.inspector()
    else: raise HTTPException(400,"kind must be telemetry or inspector")
    if format=="json": return JSONResponse(rows,headers={"Content-Disposition":"attachment; filename=aeroscope-export.json"})
    if format!="csv": raise HTTPException(400,"format must be json or csv")
    out=io.StringIO(newline="")
    if rows:
        keys=sorted({key for row in rows for key in row})
        writer=csv.DictWriter(out,fieldnames=keys,extrasaction="ignore");writer.writeheader();writer.writerows(rows)
    return Response(out.getvalue(),media_type="text/csv",headers={"Content-Disposition":"attachment; filename=aeroscope-export.csv"})


async def stream(websocket: WebSocket, path: str):
    origin=websocket.headers.get("origin")
    if origin and urlsplit(origin).netloc.lower()!=websocket.headers.get("host","").lower():
        await websocket.close(code=1008,reason="Cross-origin websocket denied");return
    await websocket.accept()
    interval=.25 if path=="telemetry" else 1.0
    try:
        while True:
            if path=="telemetry": data=vehicle_state.snapshot()
            elif path=="connection": data=vehicle_state.snapshot()["connection"]
            elif path=="control": data={"connection":mavlink.status(),"transactions":mavlink.transactions.list()}
            else: data={"events":events.list_events(30)}
            packet={"protocol_version":1,"type":path,"sent_at":now_iso(),"data":data}
            await asyncio.wait_for(websocket.send_json(packet),timeout=1.0)
            await asyncio.sleep(interval)
    except (WebSocketDisconnect,asyncio.TimeoutError,RuntimeError):
        try: await websocket.close()
        except RuntimeError: pass


@app.websocket("/ws/telemetry")
async def ws_telemetry(websocket: WebSocket): await stream(websocket,"telemetry")


@app.websocket("/ws/events")
async def ws_events(websocket: WebSocket): await stream(websocket,"events")


@app.websocket("/ws/connection")
async def ws_connection(websocket: WebSocket): await stream(websocket,"connection")


@app.websocket("/ws/control")
async def ws_control(websocket: WebSocket): await stream(websocket,"control")


def run() -> None:
    if HOST not in ("127.0.0.1","localhost","::1"):
        raise SystemExit("AeroScope is local-only until authentication is implemented. Set AEROSCOPE_HOST to a loopback address.")
    import uvicorn
    uvicorn.run(app,host=HOST,port=PORT,log_level=os.environ.get("AEROSCOPE_LOG_LEVEL","info").lower(),access_log=False)
