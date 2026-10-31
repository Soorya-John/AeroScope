"""Authoritative local workflows shared by both HTTP transports."""
from collections import deque
import threading, time, uuid, math
from .parameters import validate_parameter_value
from .metadata import parameter_metadata, validate_metadata

GROUPS = {
 'Sensors / orientation':['AHRS_ORIENTATION','COMPASS_USE','INS_GYRO_FILTER'],
 'Receiver':['RCMAP_ROLL','RCMAP_PITCH','RCMAP_THROTTLE','RCMAP_YAW','RC1_MIN','RC1_MAX','RC1_TRIM','RC2_MIN','RC2_MAX','RC3_MIN','RC3_MAX','RC4_MIN','RC4_MAX'],
 'Flight modes':['FLTMODE_CH','FLTMODE1','FLTMODE2','FLTMODE3','FLTMODE4','FLTMODE5','FLTMODE6'],
 'Battery':['BATT_MONITOR','BATT_CAPACITY','BATT_VOLT_MULT','BATT_AMP_PERVLT','BATT_LOW_VOLT','BATT_CRT_VOLT','BATT_FS_LOW_ACT','BATT_FS_CRT_ACT'],
 'GPS':['GPS1_TYPE','GPS_TYPE','GPS_AUTO_CONFIG'],
 'Failsafes':['FS_THR_ENABLE','FS_THR_VALUE','FS_GCS_ENABLE','FS_EKF_ACTION','RTL_ALT'],
 'Geofence':['FENCE_ENABLE','FENCE_TYPE','FENCE_ALT_MAX','FENCE_RADIUS','FENCE_MARGIN','FENCE_ACTION'],
 'Outputs':['SERVO1_FUNCTION','SERVO2_FUNCTION','SERVO3_FUNCTION','SERVO4_FUNCTION','MOT_PWM_TYPE','MOT_SPIN_ARM','MOT_SPIN_MIN'],
 'Tuning':['ATC_RAT_RLL_P','ATC_RAT_RLL_I','ATC_RAT_RLL_D','ATC_RAT_PIT_P','ATC_RAT_PIT_I','ATC_RAT_PIT_D','WPNAV_SPEED','WPNAV_ACCEL','WP_SPD','WP_ACCEL']}

GET_ROUTES={'/api/setup','/api/control/ownership','/api/parameters/batch','/api/connection/ports'}
POST_ROUTES={'/api/control/claim','/api/control/release','/api/parameters/stage','/api/parameters/apply','/api/parameters/cancel','/api/mission/cancel','/api/setup/calibrate','/api/setup/position','/api/setup/cancel-calibration','/api/setup/motor-test','/api/maintenance/reboot'}
def vehicle_mutation(path):
    return path.startswith(('/api/control/','/api/guided/','/api/mission/','/api/setup/','/api/maintenance/')) or path in {'/api/parameters/request','/api/parameters/set','/api/parameters/refresh','/api/connection','/api/connection/disconnect'}

class Workflows:
    def __init__(self, mavlink):
        self.m=mavlink; self.lock=threading.RLock(); self.owner=None; self.lease_until=0
        self.batch=None; self.calibration={'state':'IDLE'}; self.messages=deque(maxlen=60)

    def ownership(self):
        with self.lock:
            if time.monotonic()>self.lease_until: self.owner=None
            return {'owner':self.owner,'expires_in_s':max(0,round(self.lease_until-time.monotonic()))}

    def claim(self, client):
        if not client or len(client)>80: raise ValueError('A browser client ID is required')
        with self.lock:
            owner=self.ownership()['owner']
            if owner and owner!=client: raise RuntimeError('Another browser owns vehicle controls; release or wait for lease expiry')
            self.owner=client; self.lease_until=time.monotonic()+30
            return self.ownership()

    def require_owner(self, client):
        with self.lock:
            if self.ownership()['owner']!=client or not client:
                raise RuntimeError('Claim control in Connect before sending vehicle operations')
            self.lease_until=time.monotonic()+30

    def copter(self, disarmed=False):
        self.m._active_control_session(); s=self.m.state.snapshot()
        if s['vehicle']['autopilot']['value']!='ArduPilot' or s['vehicle']['type']['value'] not in {'Quadrotor','Hexarotor','Octorotor','Tricopter','Coaxial','Helicopter'}:
            raise RuntimeError('This workflow requires ArduPilot Copter; other firmware is unqualified')
        if disarmed and (not s['status']['armed']['available'] or s['status']['armed']['value'] is not False):
            raise RuntimeError('A confirmed disarmed vehicle is required')
        return s

    def setup(self):
        s=self.m.state.snapshot(); p=self.m.parameters.snapshot(); values={x['name']:x for x in p['parameters']}
        metadata=parameter_metadata(s['vehicle'])
        for name,value in values.items(): value['metadata']=metadata.get(name,{})
        copter=s['vehicle']['autopilot'].get('value')=='ArduPilot' and s['vehicle']['type'].get('value') in {'Quadrotor','Hexarotor','Octorotor','Tricopter','Coaxial','Helicopter'}
        checks=[{'label':'Fresh heartbeat','ok':s['connection']['state']=='connected' and (s['connection'].get('heartbeat_age_s') or 99)<2},
                {'label':'Supported Copter identity','ok':copter}, {'label':'Complete parameter download','ok':p['progress']['complete']},
                {'label':'Disarmed for bench setup','ok':s['status']['armed'].get('value') is False},
                {'label':'GPS 3D fix (required for position flight)','ok':s['gps']['fix_type'].get('available') and s['gps']['fix_type'].get('value',0)>=3},
                {'label':'Battery telemetry available','ok':s['battery']['voltage_v'].get('available',False)}]
        return {'checks':checks,'completed':sum(bool(x['ok']) for x in checks),'total':len(checks),'groups':{g:[values[n] for n in names if n in values] for g,names in GROUPS.items()} if copter else {},
                'calibration':self.calibration_status(),'messages':list(self.messages),'ownership':self.ownership(),
                'limitations':['Setup prerequisites are observations, not a flight-readiness certification.','Firmware flashing unavailable: no verified board/bootloader/image adapter.','Plane, Rover, PX4 setup and INAV/MSP are not qualified.']}

    def stage(self, body):
        session=self.m._active_control_session(); current={p['name']:p for p in self.m.parameters.snapshot()['parameters']}
        changes=body.get('changes')
        if not isinstance(changes,list) or not 1<=len(changes)<=500: raise ValueError('Provide 1–500 parameter changes')
        seen=set(); rows=[]
        for edit in changes:
            name=str(edit.get('name',''))
            if name in seen: raise ValueError('Duplicate parameter '+name)
            seen.add(name)
            if name not in current: raise ValueError('Parameter missing from current vehicle: '+name)
            p=current[name]; value=validate_parameter_value(edit.get('value'),int(p['type']))
            validate_metadata(value,parameter_metadata(self.m.state.snapshot()['vehicle']).get(name,{}))
            if 'before' in edit and edit['before']!=p['value']: raise ValueError('Vehicle value changed; review '+name)
            if int(p['type']) in {7,8,10,11}: raise ValueError('Extended parameter adapter unavailable: '+name)
            if value!=p['value']: rows.append({'name':name,'before':p['value'],'after':value,'type':p['type'],'state':'STAGED'})
        with self.lock:
            if self.batch and self.batch['state']=='APPLYING': raise RuntimeError('Parameter apply is already running')
            self.batch={'id':str(uuid.uuid4()),'session_id':session,'state':'STAGED','changes':rows,'atomic':False}
            return self.batch

    def apply(self, body):
        with self.lock:
            if body.get('confirmed') is not True: raise ValueError('Review and confirm the parameter diff')
            if not self.batch or self.batch['id']!=body.get('id') or self.batch['state']!='STAGED': raise ValueError('Stage a current diff first')
            if self.batch['session_id']!=self.m._active_control_session(): raise RuntimeError('Session changed; stage again')
            self.batch['state']='APPLYING'; batch=self.batch
        threading.Thread(target=self._apply_worker,args=(batch,),daemon=True).start()
        return batch

    def _apply_worker(self,batch):
        for row in batch['changes']:
            try:
                if batch['state']!='APPLYING' or batch['session_id']!=self.m._active_control_session(): raise RuntimeError('Apply cancelled or session changed')
                current=next(p for p in self.m.parameters.snapshot()['parameters'] if p['name']==row['name'])
                if current['value']!=row['before']: raise ValueError('Vehicle value changed since review')
                row['state']='REQUESTED'; self.m.set_parameter(row['name'],row['after'],confirmed=True)
                deadline=time.monotonic()+9
                while time.monotonic()<deadline:
                    pending=self.m.parameters.snapshot()['pending'].get(row['name'],{})
                    if pending.get('state') not in {'PENDING','IN_PROGRESS'}: break
                    time.sleep(.1)
                row['state']=pending.get('state','UNKNOWN'); row['detail']=pending.get('detail')
                if row['state']!='CONFIRMED': raise RuntimeError('Write was not confirmed; stopped before remaining changes')
                # Explicit independent readback after the PARAM_SET echo.
                self.m.refresh_parameter(row['name']); deadline=time.monotonic()+9
                while time.monotonic()<deadline:
                    read=self.m.parameters.snapshot()['pending'].get(row['name'],{})
                    if read.get('state')!='PENDING': break
                    time.sleep(.1)
                actual=next(p for p in self.m.parameters.snapshot()['parameters'] if p['name']==row['name'])
                if read.get('state')!='CONFIRMED' or actual['value']!=row['after']:
                    row['state']='UNKNOWN'; raise RuntimeError('Independent readback not verified')
                row['state']='VERIFIED'
            except Exception as exc:
                if row['state'] in {'STAGED','REQUESTED'}: row['state']='UNKNOWN'
                row['detail']=str(exc); batch['state']='PARTIAL_FAILURE'; return
        batch['state']='VERIFIED'

    def start_calibration(self, kind, confirmation):
        self.copter(disarmed=True)
        if confirmation!='CALIBRATE': raise ValueError('Type CALIBRATE after securing vehicle and removing propellers')
        if self.calibration_status()['state'] in {'RUNNING','WAITING_ACK'}: raise RuntimeError('Calibration already active')
        params={'gyro':(1,0,0,0,0,0,0),'barometer':(0,0,1,0,0,0,0),'level':(0,0,0,0,2,0,0),'accelerometer':(0,0,0,0,1,0,0)}
        if kind=='compass': tx=self.m.queue_command(42424,(0,0,1,0,0,0,0),timeout=15)
        elif kind in params: tx=self.m.queue_command(241,params[kind],timeout=120)
        else: raise ValueError('Unknown calibration kind')
        self.calibration={'kind':kind,'state':'WAITING_ACK','transaction_id':tx['id'],'session_id':tx['session_id'],'started':time.monotonic(),'progress':None,'instruction':'Follow vehicle prompts; keep still for gyro/level/barometer. Rotate all axes for compass.'}
        return self.calibration_status()

    def calibration_status(self):
        c=self.calibration
        if c.get('session_id') and c['session_id']!=self.m.status()['session_id'] and c['state'] in {'RUNNING','WAITING_ACK'}: c.update(state='UNKNOWN',instruction='Connection session changed; calibration outcome unknown')
        tx=next((x for x in self.m.transactions.list() if x['id']==c.get('transaction_id')),None)
        if tx and c['state'] in {'RUNNING','WAITING_ACK'}:
            if tx['state'] in {'FAILED','UNKNOWN','CANCELLED'}: c.update(state=tx['state'],instruction=tx.get('detail') or tx.get('result'))
            elif tx['state']=='SUCCEEDED': c['state']='RUNNING' if c['kind'] in {'compass','accelerometer'} else 'SUCCEEDED'
            elif tx['state']=='IN_PROGRESS': c.update(state='RUNNING',progress=tx.get('progress'))
        if c['state']=='RUNNING' and time.monotonic()-c.get('started',0)>240: c.update(state='UNKNOWN',instruction='Calibration result timed out; inspect vehicle before retrying')
        return {k:v for k,v in c.items() if k!='started'}

    def ingest(self,msg):
        kind=msg.get_type(); c=self.calibration
        if kind=='STATUSTEXT':
            text=msg.text.decode(errors='replace') if isinstance(msg.text,bytes) else str(msg.text)
            self.messages.append({'time':time.time(),'severity':int(msg.severity),'text':text})
        if c.get('session_id')!=self.m.status()['session_id'] or c.get('state') not in {'WAITING_ACK','RUNNING'}: return
        if kind=='MAG_CAL_PROGRESS' and c['kind']=='compass': c.update(state='RUNNING',progress=int(msg.completion_pct),instruction='Rotate slowly around all axes')
        elif kind=='MAG_CAL_REPORT' and c['kind']=='compass':
            c.setdefault('compasses',{})[str(msg.compass_id)]={'status':int(msg.cal_status),'autosaved':bool(msg.autosaved),'fitness':float(msg.fitness)}
            # Report every expected compass, not just the first completion.
            expected=int(getattr(msg,'cal_mask',1)); reports=c['compasses']
            if all(str(i) in reports for i in range(8) if expected & (1<<i)):
                c.update(state='SUCCEEDED' if all(r['status']==4 and r['autosaved'] for r in reports.values()) else 'FAILED',instruction='Compass reports received; inspect fitness and reboot if required')
        elif kind=='COMMAND_LONG' and int(msg.command)==42429 and c['kind']=='accelerometer':
            position=int(msg.param1)
            c['position']=position
            if position in (16777215,16777216): c.update(state='SUCCEEDED' if position==16777215 else 'FAILED',instruction='Accelerometer calibration result received')
            else: c.update(state='RUNNING',instruction='Place vehicle '+{1:'level',2:'on its left side',3:'on its right side',4:'nose down',5:'nose up',6:'upside down'}.get(position,'as requested')+' and confirm position')

    def dispatch(self, method, path, body, client):
        if method=='GET':
            if path=='/api/setup': return self.setup()
            if path=='/api/control/ownership': return self.ownership()
            if path=='/api/parameters/batch': return self.batch or {'state':'IDLE'}
            if path=='/api/connection/ports':
                from serial.tools.list_ports import comports
                return {'ports':[{'device':p.device,'description':p.description,'hwid':p.hwid} for p in comports()]}
            return None
        if path=='/api/control/claim': return self.claim(client)
        if path=='/api/control/release':
            self.require_owner(client); self.owner=None; return self.ownership()
        self.require_owner(client)
        if path=='/api/parameters/stage': return self.stage(body)
        if path=='/api/parameters/apply': return self.apply(body)
        if path=='/api/parameters/cancel':
            self.m.parameters.cancel()
            if self.batch and self.batch['state']=='APPLYING': self.batch['state']='CANCELLED'
            return {'cancelled':True}
        if path=='/api/mission/cancel': self.m.missions.cancel(); return self.m.missions.snapshot()
        if path=='/api/setup/calibrate': return self.start_calibration(body.get('kind'),body.get('confirmation'))
        if path=='/api/setup/position':
            self.copter(disarmed=True); position=self.calibration.get('position')
            if self.calibration.get('kind')!='accelerometer' or position not in range(1,7): raise ValueError('No position requested')
            return self.m.queue_command(42429,(position,0,0,0,0,0,0))
        if path=='/api/setup/cancel-calibration':
            self.copter(disarmed=True)
            if self.calibration.get('kind')!='compass': raise ValueError('Only compass cancellation is supported; other calibrations must finish on vehicle')
            tx=self.m.queue_command(42426,(0,0,0,0,0,0,0)); self.calibration.update(state='CANCEL_REQUESTED',instruction='Cancel command queued; ACK still required'); return tx
        if path=='/api/setup/motor-test':
            self.copter(disarmed=True)
            if body.get('confirmation')!='PROPS REMOVED' or body.get('bench') is not True: raise ValueError('Confirm bench setup and type PROPS REMOVED')
            motor=int(body.get('motor',0)); throttle=float(body.get('throttle',0)); duration=float(body.get('duration',0))
            if not 1<=motor<=8 or not math.isfinite(throttle) or not 0<throttle<=10 or not math.isfinite(duration) or not 0<duration<=3: raise ValueError('Motor 1–8, throttle >0–10%, duration >0–3 seconds required')
            return self.m.queue_command(209,(motor,0,throttle,duration,1,0,0),timeout=5)
        if path=='/api/maintenance/reboot':
            self.copter(disarmed=True)
            if body.get('confirmation')!='REBOOT': raise ValueError('Type REBOOT to confirm')
            return self.m.queue_command(246,(1,0,0,0,0,0,0))
        return None
