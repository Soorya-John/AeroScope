"""Build a small parameter metadata cache from the exact tested ArduPilot commit.

Metadata comments originate in ArduPilot (GPL-3.0-or-later). Sources and commit
are retained for attribution/reproducibility. No latest-version metadata fallback.
"""
from pathlib import Path
from urllib.request import urlopen
import re,json
root=Path(__file__).resolve().parents[1]
commit=re.search(r'commit ([0-9a-f]+)',(root/'.tools/sitl/git.txt').read_text()).group(1)
sources={'ArduCopter/Parameters.cpp':'','libraries/AP_AHRS/AP_AHRS.cpp':'AHRS_',
 'libraries/AP_InertialSensor/AP_InertialSensor.cpp':'INS_','libraries/AP_BattMonitor/AP_BattMonitor.cpp':'BATT_',
 'libraries/AP_BattMonitor/AP_BattMonitor_Backend.cpp':'BATT_','libraries/AP_BattMonitor/AP_BattMonitor_Analog.cpp':'BATT_',
 'libraries/AP_GPS/AP_GPS.cpp':'GPS_','libraries/AC_Fence/AC_Fence.cpp':'FENCE_',
 'libraries/RC_Channel/RC_Channel.cpp':'RC1_','libraries/SRV_Channel/SRV_Channel.cpp':'SERVO1_',
 'libraries/AC_AttitudeControl/AC_AttitudeControl.cpp':'ATC_'}
params={}
for source,prefix in sources.items():
    url=f'https://raw.githubusercontent.com/ArduPilot/ardupilot/{commit}/{source}'
    try: code=urlopen(url,timeout=30).read().decode()
    except Exception as exc: print(source,exc);continue
    for match in re.finditer(r'//\s*@Param(?:\{[^}]*\})?:\s*(\S+)(.*?)(?=\n\s*(?!//)\S)',code,re.S):
        name=prefix+match[1];block=match[2]
        fields=dict(re.findall(r'//\s*@([A-Za-z]+):\s*([^\n\r]*)',block))
        if not fields:continue
        meta={'label':fields.get('DisplayName',name),'description':fields.get('Description',fields.get('DisplayName','')),'units':fields.get('Units',''),'reboot':fields.get('RebootRequired','').lower()=='true','source':url}
        if 'Range' in fields:
            try:meta['range']=[float(v) for v in fields['Range'].split()]
            except ValueError:pass
        if 'Values' in fields:
            meta['enum']={k.strip():v.strip() for pair in fields['Values'].split(',') if ':' in pair for k,v in [pair.split(':',1)]}
        params[name]=meta
target=root/'backend/app/metadata/arducopter-4.7.2.json';target.parent.mkdir(exist_ok=True)
target.write_text(json.dumps({'firmware':'4.7.2.255','commit':commit,'license':'GPL-3.0-or-later','parameters':params},indent=2),encoding='utf-8',newline='\n')
print('Metadata parameters:',len(params))
