"""Launch our own official Copter process and test through the local HTTP API.

Never attaches to hardware or an externally supplied endpoint. Run from repo root.
"""
import json, os, subprocess, sys, threading, time, socket, tempfile, shutil
from pathlib import Path
from urllib.request import Request,urlopen
root=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(root/'backend'))
os.environ['AEROSCOPE_DATA_DIR']=str(root/'.tools'/'verification-data')
from app.main import AeroScopeServer,Handler,mavlink,simulator

def wait(fn,timeout=30,label='operation'):
    deadline=time.monotonic()+timeout
    while time.monotonic()<deadline:
        value=fn()
        if value:return value
        time.sleep(.2)
    raise AssertionError('Timed out: '+label)

def main():
    assets=root/'.tools'/'sitl'; exe=assets/'ArduCopter.elf'
    work=Path(tempfile.mkdtemp(prefix='protocol-',dir=assets))
    shutil.copyfile(assets/'copter.parm',work/'copter.parm')
    # Refuse an occupied test port before spawning; do not attach to it.
    with socket.socket(socket.AF_INET,socket.SOCK_DGRAM) as sock:
        sock.bind(('127.0.0.1',14562))
    output=(work/'verification.log').open('w')
    (work/'quad.parm').write_text('FRAME_CLASS 1\nFRAME_TYPE 1\n',encoding='ascii')
    args=[str(exe),'--model','quad','--instance','2','--home=-35.363261,149.165230,584,353','--serial0=udpclient:127.0.0.1:14562','--defaults=copter.parm,quad.parm','--speedup=1']
    child=subprocess.Popen(args,cwd=work,stdout=output,stderr=subprocess.STDOUT)
    server=AeroScopeServer(('127.0.0.1',0),Handler); thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    base='http://127.0.0.1:'+str(server.server_port)
    results=[]
    def get(path):
        with urlopen(base+path,timeout=10) as r:return json.load(r)
    def post(path,body=None):
        request=Request(base+path,json.dumps(body or {}).encode(),{'Content-Type':'application/json','X-AeroScope-Client':'sitl-verifier'},method='POST')
        with urlopen(request,timeout=10) as r:return json.load(r)
    def passed(label,data=None):
        results.append({'check':label,'result':'PASS','data':data});print('PASS',label,flush=True)
    try:
        time.sleep(3)
        if child.poll() is not None:raise RuntimeError('SITL exited: '+(work/'verification.log').read_text())
        post('/api/control/claim');post('/api/connection',dict(kind='udp_listener',bind_address='127.0.0.1',port=14562,target_system=1,target_component=1,environment='sitl'))
        state=wait(lambda:get('/api/state')['data'] if get('/api/state')['data']['vehicle']['firmware']['available'] else None,label='version heartbeat')
        passed('Connect and version',state['vehicle'])
        post('/api/parameters/request');params=wait(lambda:(p if (p:=get('/api/parameters'))['progress']['complete'] else None),100,'full parameter list')
        passed('Parameter download',params['progress'])
        # Assert SIM-only parameters from the child before any modifying operation.
        values={p['name']:p for p in params['parameters']}
        assert 'SIM_SPEEDUP' in values
        name='INS_GYRO_FILTER';before=values[name]['value'];after=before+1
        stage=post('/api/parameters/stage',{'changes':[{'name':name,'value':after,'before':before}]});post('/api/parameters/apply',{'id':stage['id'],'confirmed':True})
        batch=wait(lambda:(b if (b:=get('/api/parameters/batch'))['state']!='APPLYING' else None),25,'parameter apply')
        assert batch['state']=='VERIFIED',batch;passed('Staged write and independent readback',batch)
        stage=post('/api/parameters/stage',{'changes':[{'name':name,'value':before,'before':after}]});post('/api/parameters/apply',{'id':stage['id'],'confirmed':True})
        batch=wait(lambda:(b if (b:=get('/api/parameters/batch'))['state']!='APPLYING' else None),25,'restore')
        assert batch['state']=='VERIFIED',batch;passed('Restore original parameter',batch)
        with urlopen(base+'/api/parameters/export?format=json') as r: backup=json.load(r)
        assert len(backup['parameters'])==len(params['parameters']);passed('Backup export')
        post('/api/setup/calibrate',{'kind':'gyro','confirmation':'CALIBRATE'})
        cal=wait(lambda:(c if (c:=get('/api/setup')['calibration'])['state'] not in {'RUNNING','WAITING_ACK'} else None),20,'gyro calibration')
        assert cal['state']=='SUCCEEDED',cal;passed('Gyro calibration result',cal)
        motor=post('/api/setup/motor-test',{'bench':True,'confirmation':'PROPS REMOVED','motor':1,'throttle':3,'duration':1})
        motor_tx=wait(lambda:next((t for t in get('/api/control/status')['transactions'] if t['id']==motor['id'] and t['state'] not in {'QUEUED','WAITING_ACK','IN_PROGRESS'}),None),10,'motor test acknowledgement')
        passed('Bounded motor-test protocol response (SITL only)',motor_tx)
        unsupported=mavlink.queue_command(65534,(0,0,0,0,0,0,0))
        rejected=wait(lambda:next((t for t in mavlink.transactions.list() if t['id']==unsupported['id'] and t['state']=='FAILED'),None),10,'unsupported command rejection')
        assert rejected['result']=='UNSUPPORTED',rejected;passed('Actual autopilot unsupported-command rejection',rejected)
        items=[dict(command=16,frame=6,latitude=-35.363261,longitude=149.165230,altitude=0),dict(command=22,frame=6,latitude=-35.363261,longitude=149.165230,altitude=10),dict(command=16,frame=6,latitude=-35.3631,longitude=149.1653,altitude=10),dict(command=20,frame=2,latitude=0,longitude=0,altitude=0)]
        post('/api/mission/upload',{'items':items,'confirmed':True})
        mission=wait(lambda:(m if (m:=get('/api/mission'))['state'] not in ('UPLOADING','DOWNLOADING','CLEARING') else None),35,'mission upload')
        assert mission['state']=='SUCCEEDED',mission;passed('Mission upload',mission)
        post('/api/mission/download');mission=wait(lambda:(m if (m:=get('/api/mission'))['state']!='DOWNLOADING' else None),35,'mission download')
        assert mission['state']=='SUCCEEDED' and len(mission['items'])==len(items),mission
        for a,b in zip(items[1:],mission['items'][1:]):
            assert a['command']==b['command'] and abs(a['altitude']-b['altitude'])<.01,(a,b)
            if a['command'] in {16,22}:assert abs(a['latitude']-b['latitude'])<1e-7 and abs(a['longitude']-b['longitude'])<1e-7,(a,b)
        passed('Mission download round trip (home slot excluded)',mission)
        post('/api/mission/clear',{'confirmed':True});wait(lambda:get('/api/mission')['state']=='SUCCEEDED',15,'clear')
        post('/api/mission/download');wait(lambda:get('/api/mission')['state']=='SUCCEEDED',15,'empty download')
        assert get('/api/mission')['items']==[];passed('Mission clear and empty download')
        # Allow the fresh EKF/GPS to initialize before requesting a position mode.
        for _ in range(12):
            post('/api/control/claim');time.sleep(5)
        post('/api/control/mode',{'mode':'GUIDED','confirmed':True});wait(lambda:get('/api/control/mode')['request']['state']=='CONFIRMED',12,'GUIDED heartbeat');passed('Mode observed GUIDED')
        # Normal arming only. Do not change ARMING_CHECK or force-arm.
        post('/api/control/arm',{'armed':True,'confirmation':'ARM','acknowledge_unknown':True})
        arm=wait(lambda:(a if (a:=get('/api/control/arm')['request'])['state']!='PENDING' else None),15,'arm')
        if arm['state']!='CONFIRMED':
            results.append({'check':'Arm/takeoff/land/RTL','result':'BLOCKED','data':{'arm':arm,'setup':get('/api/setup')}})
        else:
            passed('Arm ACK and observed state')
            post('/api/guided/takeoff',{'altitude':5,'confirmation':'TAKEOFF','acknowledge_unknown':True})
            wait(lambda:(get('/api/state')['data']['position']['relative_altitude_m'].get('value') or 0)>4,40,'takeoff altitude');passed('Takeoff observed above 4m')
            post('/api/control/claim')
            post('/api/guided/reposition',{'latitude':-35.36323,'longitude':149.16526,'altitude':5,'confirmation':'GO','acknowledge_unknown':True})
            reposition=wait(lambda:next((a for a in get('/api/control/actions')['actions'] if a['command']==192 and a['state'] in {'OBSERVED','FAILED','UNKNOWN'}),None),35,'guided reposition')
            assert reposition['state']=='OBSERVED',reposition;passed('COMMAND_INT guided reposition observed',reposition)
            post('/api/control/mode',{'mode':'BRAKE','confirmed':True});wait(lambda:get('/api/control/mode')['request']['state']=='CONFIRMED',12,'BRAKE');passed('Pause via observed BRAKE mode')
            post('/api/control/mode',{'mode':'RTL','confirmed':True});wait(lambda:get('/api/control/mode')['request']['state']=='CONFIRMED',15,'RTL');passed('RTL heartbeat')
            wait(lambda:get('/api/state')['data']['status']['armed']['value'] is False,90,'RTL landing/disarm');passed('RTL landing and disarm observed')
        post('/api/control/claim');post('/api/vehicle-logs/list')
        logs=wait(lambda:(s if (s:=get('/api/vehicle-logs'))['state']!='LISTING' else None),20,'FC log listing')
        assert logs['state']=='SUCCEEDED',logs;passed('Flight-controller log listing',logs)
        if logs['logs']:
            post('/api/vehicle-logs/download',{'id':logs['logs'][0]['id']})
            log=wait(lambda:(s if (s:=get('/api/vehicle-logs'))['state']!='DOWNLOADING' else None),300,'FC log download')
            assert log['state']=='DOWNLOADED',log
            passed('Complete controller log download',log)
            post('/api/control/claim');analysis=post('/api/vehicle-logs/analyze')
            analysis_id=analysis['analysis']['id'] if 'analysis' in analysis else analysis['id']
            parsed=wait(lambda:(a if (a:=get('/api/analysis/'+analysis_id))['status'] not in {'queued','running'} else None),30,'downloaded log analysis')
            assert parsed['status']=='complete',parsed
            passed('Downloaded controller log parsed as DataFlash',{'id':analysis_id,'summary':parsed.get('result',{}).get('summary')})
        post('/api/control/claim'); session=mavlink.status()['session_id'];post('/api/connection/disconnect');post('/api/connection',dict(kind='udp_listener',bind_address='127.0.0.1',port=14562,environment='sitl'))
        wait(lambda:mavlink.status()['session_id']!=session and get('/api/state')['data']['connection']['state']=='connected',20,'reconnect')
        assert not any(t['state']=='QUEUED' for t in mavlink.transactions.list());passed('Disconnect/reconnect new session without queued command replay')
    except Exception as exc:
        results.append({'check':'run','result':'FAIL','detail':repr(exc),'logs':mavlink.vehicle_logs.snapshot(),'mode':mavlink.mode_status(),'setup':mavlink.workflows.setup()});raise
    finally:
        mavlink.disconnect();simulator.stop();server.shutdown();server.server_close();child.terminate();child.wait(timeout=10);output.close()
        target=root/'SITL_RESULTS.json';target.write_text(json.dumps({'binary_source':'CopterStable','git':(assets/'git.txt').read_text(),'results':results},indent=2),encoding='utf-8')
if __name__=='__main__':main()
