"""Typed MAVLink parameter cache and validation helpers."""
from __future__ import annotations
import math
import struct
import threading
import time
from typing import Any

INTEGER_TYPES = {1, 2, 3, 4, 5, 6, 7, 8}
UNSIGNED_TYPES = {1, 3, 5, 7}
WIDTHS = {1: 8, 2: 8, 3: 16, 4: 16, 5: 32, 6: 32, 7: 64, 8: 64}

def validate_parameter_value(value: Any, param_type: int) -> int | float:
    if param_type in INTEGER_TYPES:
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)) or int(value) != value:
            raise ValueError("Integer parameter requires a finite whole-number value")
        width = WIDTHS[param_type]; lower = 0 if param_type in UNSIGNED_TYPES else -(1 << (width-1))
        upper = (1 << width)-1 if param_type in UNSIGNED_TYPES else (1 << (width-1))-1
        if not lower <= int(value) <= upper: raise ValueError(f"Value is outside MAV_PARAM_TYPE range [{lower}, {upper}]")
        return int(value)
    if param_type in {9, 10}:
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
            raise ValueError("Float parameter requires a finite numeric value")
        if param_type==9:
            try: value=struct.unpack('<f',struct.pack('<f',value))[0]
            except (OverflowError,struct.error) as exc:raise ValueError('Value exceeds finite REAL32 range') from exc
            if not math.isfinite(value):raise ValueError('Value exceeds finite REAL32 range')
        return float(value)
    raise ValueError(f"Unsupported MAV_PARAM_TYPE {param_type}")

def validate_ext_parameter_value(value: Any, param_type: int) -> int | float | str:
    if param_type == 11:
        if not isinstance(value,str) or "\0" in value or len(value.encode("utf-8"))>128:
            raise ValueError("Custom extended parameter must be text of at most 128 UTF-8 bytes without NUL")
        return value
    if param_type in INTEGER_TYPES and isinstance(value,str):
        try: value=int(value,10)
        except ValueError as exc: raise ValueError("Extended integer parameter requires a whole-number value") from exc
    elif param_type in {9,10} and isinstance(value,str):
        try: value=float(value)
        except ValueError as exc: raise ValueError("Extended float parameter requires a numeric value") from exc
    return validate_parameter_value(value,param_type)

def parse_ext_parameter_value(value: Any, param_type: int) -> int | float | str:
    if isinstance(value,bytes): value=value.split(b"\0",1)[0].decode("utf-8","replace")
    elif not isinstance(value,str): value=str(value)
    if param_type == 11: return validate_ext_parameter_value(value,param_type)
    try:
        parsed=float(value) if param_type in {9,10} else int(value,10)
    except (TypeError,ValueError) as exc:
        raise ValueError(f"Invalid PARAM_EXT value for type {param_type}") from exc
    return validate_ext_parameter_value(parsed,param_type)

class ParameterStore:
    def __init__(self) -> None:
        self._lock=threading.RLock(); self._session: str|None=None; self._items: dict[str,dict[str,Any]]={}
        self._count: int|None=None; self._started: float|None=None; self._pending: dict[str,Any]={}; self._error: str|None=None
        self._indices: dict[int,str]={}; self._last_recovery=0.0; self._recovery_round=0
        self.generation=0

    def begin_session(self, session: str) -> None:
        with self._lock: self._session=session; self._items={}; self._count=None; self._started=None; self._pending={}; self._error=None; self._indices={}

    def request_all(self) -> None:
        self.generation+=1
        with self._lock: self._items={}; self._count=None; self._started=time.monotonic(); self._error=None; self._indices={}; self._last_recovery=self._started; self._recovery_round=0

    def cancel(self, reason="Cancelled by operator"):
        with self._lock:
            self.generation+=1
            self._error=reason; self._started=None
            for pending in self._pending.values():
                if pending['state'] in {'PENDING','IN_PROGRESS'}: pending.update(state='UNKNOWN',detail=reason)

    def recovery_indices(self, now=None):
        now=time.monotonic() if now is None else now
        with self._lock:
            if self._started is None or self._error or self._count is None or now-self._last_recovery<2: return []
            missing=[i for i in range(self._count) if i not in self._indices]
            if not missing: return []
            self._last_recovery=now; self._recovery_round+=1
            return missing[:32]

    def request_one(self, name: str, protocol: str | None = None) -> None:
        with self._lock: self._pending[name]={"state":"PENDING","started":time.monotonic(),"protocol":protocol}

    def pending_set(self, name: str, value: Any, protocol: str = "param", param_type: int | None = None) -> None:
        with self._lock:
            if self._pending.get(name,{}).get('state') in {'PENDING','IN_PROGRESS'}: raise RuntimeError('A parameter operation is already pending for '+name)
        with self._lock: self._pending[name]={"state":"PENDING","requested_value":value,"requested_type":param_type,
            "protocol":protocol,"requires_ack":protocol=="param_ext","started":time.monotonic()}

    def fail_pending(self, name: str, detail: str) -> None:
        with self._lock:
            if name in self._pending and self._pending[name]["state"] == "PENDING":
                self._pending[name].update(state="FAILED", detail=str(detail)[:300])

    def fail_download(self, detail: str) -> None:
        with self._lock: self._error=str(detail)[:300]

    def expire(self, now: float | None = None, timeout: float = 8.0) -> None:
        now=time.monotonic() if now is None else now
        with self._lock:
            for pending in self._pending.values():
                if pending["state"] in {"PENDING","IN_PROGRESS"} and now-pending["started"] >= timeout:
                    pending.update(state="UNKNOWN",detail="No matching PARAM_VALUE received; outcome is uncertain")
            if self._started is not None and not self._error and (self._count is None or len(self._indices)<self._count) and now-self._started >= 90:
                self._error="Parameter download timed out with missing responses"

    def ingest(self, name: str, value: Any, param_type: int, index: int, count: int, protocol: str = "param") -> dict[str,Any]:
        with self._lock:
            if not 0 <= count <= 32767 or index < -1 or index >= count and index != 65535:
                raise ValueError('Invalid parameter index/count')
            if self._count is not None and count != self._count:
                self._error='Vehicle parameter count changed; restart download'
                return {}
            if 0 <= index < count:
                if index in self._indices and self._indices[index] != name:
                    self._error='Conflicting parameter index; restart download'; return {}
                self._indices[index]=name
            record={"name":name,"value":value,"type":param_type,"index":index,"count":count,"protocol":protocol,"received_at":time.time()}
            old=self._items.get(name)
            if old and old.get("protocol")=="param_ext" and protocol=="param":
                record={**old,"count":count,"received_at":record["received_at"]}
            self._items[name]=record; self._count=max(self._count or 0,max(0,int(count)))
            pending=self._pending.get(name)
            if pending and pending['state'] in {'PENDING','IN_PROGRESS'}:
                protocol_match=pending.get("protocol") is None or pending.get("protocol")==protocol
                type_match=pending.get("requested_type") is None or int(pending["requested_type"])==int(param_type)
                if protocol_match and type_match and not pending.get("requires_ack") and ("requested_value" not in pending or pending["requested_value"]==value):
                    pending["state"]="CONFIRMED"
                elif protocol_match and type_match and not pending.get("requires_ack") and "requested_value" in pending: pending["state"]="REJECTED"
            return {**record,"changed":old is not None and old["value"]!=value}

    def ingest_ext_ack(self, name: str, value: Any, param_type: int, result: int) -> dict[str,Any] | None:
        outcomes={0:("CONFIRMED","Parameter accepted"),1:("FAILED","Parameter value unsupported"),
                  2:("FAILED","Vehicle failed to set parameter"),3:("IN_PROGRESS","Vehicle is applying parameter"),
                  4:("FAILED","Unexpected PARAM_EXT enum-end result")}
        state,detail=outcomes.get(int(result),("FAILED",f"Unknown PARAM_EXT_ACK result {result}"))
        with self._lock:
            old=self._items.get(name)
            if int(result) in {0,3}:
                index=int(old["index"]) if old else -1; count=int(old["count"]) if old else (self._count or 0)
                self._items[name]={"name":name,"value":value,"type":int(param_type),"index":index,"count":count,
                    "protocol":"param_ext","received_at":time.time()}
            pending=self._pending.get(name)
            if not pending: return None
            if pending.get("protocol") not in (None,"param_ext"):
                return None
            type_matches=pending.get("requested_type") is None or int(pending["requested_type"])==int(param_type)
            if int(result)==0 and not type_matches:
                pending.update(state="REJECTED",detail="PARAM_EXT_ACK type differs from requested type")
            elif int(result)==0 and "requested_value" not in pending:
                pending.update(state="CONFIRMED",detail=detail)
            elif int(result)==0:
                pending.update(state="CONFIRMED" if pending.get("requested_value")==value else "REJECTED",
                    detail=detail if pending.get("requested_value")==value else "PARAM_EXT_ACK value differs from requested value")
            elif int(result)==3: pending.update(state=state,detail=detail)
            else: pending.update(state=state,detail=detail)
            return {"name":name,"state":pending["state"],"detail":pending.get("detail"),"value":value,"type":int(param_type)}

    def snapshot(self, search: str = "") -> dict[str,Any]:
        with self._lock:
            values=sorted((dict(v) for v in self._items.values() if search.casefold() in v["name"].casefold()),key=lambda x:x["index"])
            known=len(self._indices); count=self._count
            return {"parameters":values,"progress":{"received":known,"count":count,"complete":not self._error and count is not None and known==count,"recovery_round":self._recovery_round,
                    "elapsed_s":round(time.monotonic()-self._started,1) if self._started else None,"error":self._error},
                    "pending":{k:{x:y for x,y in v.items() if x!="started"} for k,v in self._pending.items()},"session_id":self._session}

def compare_parameter_snapshots(left: list[dict[str,Any]], right: list[dict[str,Any]]) -> list[dict[str,Any]]:
    a={p["name"]:p for p in left}; b={p["name"]:p for p in right}; rows=[]
    for name in sorted(a.keys()|b.keys()):
        av=a.get(name); bv=b.get(name)
        if av is None or bv is None or av.get("value")!=bv.get("value") or av.get("type")!=bv.get("type"):
            rows.append({"name":name,"before":av.get("value") if av else None,"after":bv.get("value") if bv else None,
                         "before_type":av.get("type") if av else None,"after_type":bv.get("type") if bv else None,
                         "change":"added" if av is None else "removed" if bv is None else "modified"})
    return rows
