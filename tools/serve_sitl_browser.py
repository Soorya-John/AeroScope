"""Dedicated browser acceptance server; owns its simulator and cannot use hardware."""
import os,subprocess,sys,socket,atexit,tempfile,shutil
from pathlib import Path
root=Path(__file__).resolve().parents[1];assets=root/'.tools/sitl';work=Path(tempfile.mkdtemp(prefix='browser-',dir=assets));shutil.copyfile(assets/'copter.parm',work/'copter.parm')
sys.path.insert(0,str(root/'backend'));os.environ['AEROSCOPE_PORT']='8878';os.environ['AEROSCOPE_DATA_DIR']=str(root/'.tools/browser-sitl-data')
with socket.socket(socket.AF_INET,socket.SOCK_DGRAM) as sock:sock.bind(('127.0.0.1',14564))
output=(work/'browser-sitl.log').open('w')
child=subprocess.Popen([str(assets/'ArduCopter.elf'),'--model','quad','--instance','4','--home=-35.363261,149.165230,584,353','--serial0=udpclient:127.0.0.1:14564','--defaults=copter.parm','--speedup=1'],cwd=work,stdout=output,stderr=subprocess.STDOUT)
def cleanup():
    if child.poll() is None:child.terminate();child.wait(timeout=10)
    output.close()
atexit.register(cleanup)
from app.main import run
try:run()
finally:cleanup()
