"""Download official native Windows Copter SITL and runtime (never hardware)."""
from pathlib import Path
from urllib.request import urlopen
from urllib.parse import unquote
import re

root = Path(__file__).resolve().parents[1] / '.tools' / 'sitl'
root.mkdir(parents=True, exist_ok=True)
base = 'https://firmware.ardupilot.org/Tools/MissionPlanner/sitl/'
for folder in ('CopterStable/',):
    html = urlopen(base + folder, timeout=30).read().decode()
    names = re.findall(r'href="([^"]+)"', html)
    for link in names:
        name = unquote(link.rsplit('/', 1)[-1])
        if name.endswith('.dll') or name in ('ArduCopter.elf', 'git.txt'):
            target = root / name
            if not target.exists():
                print('Downloading', folder + name, flush=True)
                target.write_bytes(urlopen('https://firmware.ardupilot.org' + link, timeout=60).read())
print('SITL files:', ', '.join(p.name for p in root.iterdir()))
