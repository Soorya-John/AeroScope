"""Only apply a cached metadata set to its exact firmware version."""
from pathlib import Path
import json
_cache=None
def parameter_metadata(vehicle):
    global _cache
    if _cache is None:
        path=Path(__file__).with_name('metadata')/'arducopter-4.7.2.json'
        _cache=json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
    if vehicle.get('autopilot',{}).get('value')!='ArduPilot' or vehicle.get('type',{}).get('value') not in {'Quadrotor','Hexarotor','Octorotor','Tricopter','Coaxial','Helicopter'}:return {}
    if vehicle.get('firmware',{}).get('value')!=_cache.get('firmware'):return {}
    return _cache.get('parameters',{})

def validate_metadata(value, metadata):
    limits=metadata.get('range')
    if limits and len(limits)==2 and not limits[0]<=value<=limits[1]: raise ValueError(f"Outside firmware range {limits[0]}–{limits[1]} {metadata.get('units','')}")
    enums=metadata.get('enum')
    if enums and str(int(value)) not in enums: raise ValueError('Value is not in the firmware enum')
