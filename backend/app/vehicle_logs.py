"""Bounded MAVLink LOG_* download state, driven by the sole vehicle reader."""
import threading,time

class VehicleLogs:
    def __init__(self):
        self.lock=threading.RLock(); self.begin_session(None)
    def begin_session(self,session):
        with self.lock:
            self.session=session; self.entries={}; self.state='IDLE'; self.detail=None
            self.buffer=None; self.received=set(); self.log_id=None; self.last=0.; self.retries=0; self.expected=None
            self.window_start=0;self.window_count=0
    def list_started(self):
        with self.lock:
            if self.state in {'DOWNLOADING','LISTING'}:raise RuntimeError('A vehicle log operation is active')
            self.entries={}; self.state='LISTING'; self.last=time.monotonic(); self.retries=0
    def download_started(self,log_id):
        with self.lock:
            if self.state in {'DOWNLOADING','LISTING'}:raise RuntimeError('A vehicle log operation is active')
            entry=self.entries.get(log_id)
            if not entry:raise ValueError('List logs and select a reported log ID first')
            size=entry['size']
            if not 0<size<=32*1024*1024:raise ValueError('This downloader supports logs up to 32 MiB; use vendor tools for larger logs')
            self.buffer=bytearray(size); self.received=set(); self.log_id=log_id; self.state='DOWNLOADING'; self.last=time.monotonic(); self.retries=0; self.detail=None
            self.window_start=0;self.window_count=min(size,90*32)
            return {'kind':'log_data','id':log_id,'offset':0,'count':min(size,90*32)}
    def ingest(self,msg):
        with self.lock:
            if msg.get_type()=='LOG_ENTRY' and self.state=='LISTING':
                if int(msg.num_logs)==0:self.state='SUCCEEDED';return
                if len(self.entries)>=1000: self.state='FAILED';self.detail='Log list exceeds 1000 entries';return
                self.entries[int(msg.id)]={'id':int(msg.id),'size':int(msg.size),'time_utc':int(msg.time_utc)}
                self.expected=int(msg.num_logs);self.last=time.monotonic()
                if len(self.entries)==self.expected:self.state='SUCCEEDED'
            elif msg.get_type()=='LOG_DATA' and self.state=='DOWNLOADING' and int(msg.id)==self.log_id:
                offset=int(msg.ofs); count=int(msg.count); size=len(self.buffer)
                if offset<0 or offset%90 or not 0<count<=90 or offset+count>size or count!=min(90,size-offset):return
                self.buffer[offset:offset+count]=bytes(msg.data)[:count]; self.received.add(offset);self.last=time.monotonic();self.retries=0
                if len(self.received)==(size+89)//90:self.state='DOWNLOADED'
                elif all(i in self.received for i in range(self.window_start,self.window_start+self.window_count,90)):self.last=0
    def next_request(self,now=None):
        now=time.monotonic() if now is None else now
        with self.lock:
            if self.state not in {'DOWNLOADING','LISTING'} or now-self.last<1:return None
            if self.retries>=8:self.state='UNKNOWN';self.detail='Log responses timed out; retry list/download manually';return None
            self.retries+=1;self.last=now
            if self.state=='LISTING':return {'kind':'log_list'}
            offset=next((i for i in range(0,len(self.buffer),90) if i not in self.received),None)
            if offset is None:return None
            self.window_start=offset;self.window_count=min(90*32,len(self.buffer)-offset)
            return {'kind':'log_data','id':self.log_id,'offset':offset,'count':min(90*32,len(self.buffer)-offset)}
    def cancel(self):
        with self.lock:
            if self.state in {'DOWNLOADING','LISTING'}:self.state='CANCELLED';self.detail='Vehicle log operation cancelled'
    def bytes(self):
        with self.lock:
            if self.state!='DOWNLOADED':raise RuntimeError('Log has not been completely downloaded')
            return bytes(self.buffer)
    def snapshot(self):
        with self.lock:return {'session_id':self.session,'state':self.state,'detail':self.detail,'logs':sorted(self.entries.values(),key=lambda e:e['id'],reverse=True),'log_id':self.log_id,'received_bytes':sum(min(90,len(self.buffer)-o) for o in self.received) if self.buffer else 0,'total_bytes':len(self.buffer) if self.buffer else None}
