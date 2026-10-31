"""Mission protocol transaction state. Wire I/O is delegated to MavlinkManager."""
from __future__ import annotations
import math, threading, time, uuid
from typing import Any

MAX_ITEMS=500
SUPPORTED_GLOBAL={16,17,18,19,21,22,82}
SUPPORTED_ACTION={20,178,206}

def validate_mission(items: list[dict[str,Any]]) -> list[dict[str,Any]]:
    if not isinstance(items,list) or len(items)>MAX_ITEMS: raise ValueError(f"Mission must contain 0–{MAX_ITEMS} items")
    out=[]
    for seq,item in enumerate(items):
        if not isinstance(item,dict): raise ValueError(f"Mission item {seq} must be an object")
        command=int(item.get("command",16)); frame=int(item.get("frame",6))
        if command not in SUPPORTED_GLOBAL|SUPPORTED_ACTION: raise ValueError(f"Mission command {command} is not supported by this editor")
        if frame not in ({5,6} if command in SUPPORTED_GLOBAL else {2,5,6}): raise ValueError(f"Mission item {seq} uses unsupported coordinate frame {frame}")
        lat=float(item.get("latitude",0)); lon=float(item.get("longitude",0)); alt=float(item.get("altitude",0))
        if command in {16,17,18,19,21,22,82} and (not math.isfinite(lat) or not -90<=lat<=90 or not math.isfinite(lon) or not -180<=lon<=180):
            raise ValueError(f"Mission item {seq} has invalid coordinates")
        if not math.isfinite(alt) or not -1000<=alt<=100000: raise ValueError(f"Mission item {seq} has invalid altitude")
        params=[float(item.get(f"param{i}",0)) for i in range(1,5)]
        if not all(math.isfinite(x) for x in params): raise ValueError(f"Mission item {seq} contains a non-finite parameter")
        out.append({"seq":seq,"frame":frame,"command":command,"current":0,"autocontinue":1,
                    "param1":params[0],"param2":params[1],"param3":params[2],"param4":params[3],
                    "latitude":lat,"longitude":lon,"altitude":alt})
    return out

class MissionManager:
    def __init__(self):
        self._lock=threading.RLock(); self._session=None; self._items=[]; self._state="IDLE"; self._expected=0
        self._received={}; self._upload=[]; self._id=None; self._detail=None; self._started=None
        self._current: int|None=None; self._reached: list[int]=[]; self._execution_state=None
        self._last_activity=0.; self._retries=0; self._requested=set(); self._got_count=False
    def cancel(self):
        with self._lock:
            if self._state in {'DOWNLOADING','UPLOADING','CLEARING'}:
                self._state='CANCELLED'; self._detail='Transfer stopped locally; onboard outcome may be unknown'
    def _start(self):
        if self._state in {'DOWNLOADING','UPLOADING','CLEARING'}: raise RuntimeError('A mission transfer is already active')
        self._last_activity=time.monotonic(); self._retries=0; self._requested=set(); self._got_count=False
    def retry_payload(self, now=None):
        now=time.monotonic() if now is None else now
        with self._lock:
            if self._state != 'DOWNLOADING' or now-self._last_activity<2: return None
            if self._retries>=5:
                self._state='UNKNOWN'; self._detail='Mission read retry limit reached'; return None
            self._last_activity=now; self._retries+=1
            if not self._got_count: return {'kind':'mission_list'}
            missing=next((i for i in range(self._expected) if i not in self._received),None)
            return {'kind':'mission_request','seq':missing} if missing is not None else None
    def begin_session(self,session):
        with self._lock: self._session=session; self._items=[]; self._state="IDLE"; self._received={}; self._upload=[]; self._detail=None; self._current=None; self._reached=[]; self._execution_state=None
    def download_started(self):
        self._start()
        with self._lock: self._received={}; self._expected=0; self._state="DOWNLOADING"; self._id=str(uuid.uuid4()); self._started=time.monotonic(); self._detail=None
    def count(self,count):
        with self._lock:
            if self._state!="DOWNLOADING": return False
            if count<0 or count>MAX_ITEMS: self._state="FAILED"; self._detail="Vehicle mission count exceeds supported limit"; return False
            self._expected=int(count)
            self._got_count=True; self._last_activity=time.monotonic()
            if count==0: self._items=[]; self._state="SUCCEEDED"
            return True
    def item(self,item):
        with self._lock:
            if self._state!="DOWNLOADING" or not 0<=int(item["seq"])<self._expected: return False
            self._received[int(item["seq"])]=dict(item)
            self._last_activity=time.monotonic(); self._retries=0
            if len(self._received)==self._expected:
                self._items=[self._received[i] for i in range(self._expected)]; self._state="SUCCEEDED"
            return True
    def upload_started(self,items):
        checked=validate_mission(items)
        self._start()
        with self._lock: self._upload=checked; self._state="UPLOADING"; self._id=str(uuid.uuid4()); self._started=time.monotonic(); self._detail=None
        return checked
    def requested_item(self,seq):
        with self._lock:
            if self._state!="UPLOADING" or not 0<=int(seq)<len(self._upload): return None
            self._requested.add(int(seq)); self._last_activity=time.monotonic()
            return dict(self._upload[int(seq)])
    def ack(self,result):
        with self._lock:
            if self._state not in {"UPLOADING","CLEARING"}: return False
            operation=self._state
            if operation=='UPLOADING' and int(result)==0 and len(self._requested)!=len(self._upload):
                return False
            self._state="SUCCEEDED" if int(result)==0 else "FAILED"; self._detail=None if int(result)==0 else f"Vehicle rejected mission operation (MAV_MISSION_RESULT {int(result)})"
            if self._state=="SUCCEEDED" and operation=="UPLOADING": self._items=list(self._upload)
            if self._state=="SUCCEEDED" and operation=="CLEARING": self._items=[]
            return True
    def clear_started(self):
        self._start()
        with self._lock: self._state="CLEARING"; self._id=str(uuid.uuid4()); self._started=time.monotonic(); self._detail=None
    def expire(self, now: float|None=None):
        now=time.monotonic() if now is None else now
        with self._lock:
            if self._state in {"DOWNLOADING","UPLOADING","CLEARING"} and self._started is not None and now-self._started>30:
                self._state="UNKNOWN"; self._detail="Mission operation timed out; vehicle outcome may be uncertain"
    def fail(self, detail: str):
        with self._lock:
            if self._state in {"DOWNLOADING","UPLOADING","CLEARING"}:
                self._state="FAILED"; self._detail=str(detail)[:300]
    def update_current(self,seq: int, mission_state: int|None=None):
        with self._lock: self._current=int(seq); self._execution_state=mission_state
    def item_reached(self,seq: int):
        with self._lock:
            seq=int(seq)
            if seq not in self._reached: self._reached.append(seq)
            self._reached=self._reached[-500:]
    def snapshot(self):
        with self._lock:
            items=self._items if self._state!="UPLOADING" else self._upload
            progress=len(self._received) if self._state=="DOWNLOADING" else len(self._requested) if self._state=='UPLOADING' else len(self._items)
            total=self._expected if self._state=="DOWNLOADING" else len(self._upload) if self._state=="UPLOADING" else len(self._items)
            return {"mission_id":self._id,"state":self._state,"items":[dict(x) for x in items],"received_sequences":sorted(self._received),"progress":{"received":progress,"total":total,"current":self._current,"reached":list(self._reached),"execution_state":self._execution_state},"detail":self._detail,"session_id":self._session}
