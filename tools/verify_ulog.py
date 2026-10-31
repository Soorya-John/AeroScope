"""Exercise the real parser against the public upstream BSD-licensed fixture."""
from pathlib import Path
from urllib.request import urlopen
import sys,json,hashlib
root=Path(__file__).resolve().parents[1];sys.path.insert(0,str(root/'backend'))
from app.log_analysis import parse_ulog
fixture=root/'.tools'/'sample.ulg'
url='https://raw.githubusercontent.com/PX4/pyulog/main/test/sample.ulg'
if not fixture.exists():fixture.write_bytes(urlopen(url,timeout=60).read())
result=parse_ulog(fixture)
summary={'source':url,'sha256':hashlib.sha256(fixture.read_bytes()).hexdigest(),'parser':result.get('parser'),'summary':result.get('summary'),'samples':len(result.get('series',[])),'result':'PASS'}
(root/'ULOG_RESULTS.json').write_text(json.dumps(summary,indent=2),encoding='utf-8');print(json.dumps(summary,indent=2))
