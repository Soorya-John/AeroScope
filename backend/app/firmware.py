"""Firmware policy and wire codecs, independent of transports and UI."""
import struct
from .parameters import validate_parameter_value

FORMATS = {1:'B',2:'b',3:'H',4:'h',5:'I',6:'i'}

def parameter_encoding(autopilot, flags):
    # ArduPilot explicitly implements numeric cast, including older versions
    # which omit the newer capability bit. PX4 requires bytewise advertisement.
    if autopilot == 'ArduPilot': return 'cast'
    if flags is not None and flags & 16: return 'bytewise'
    if flags is not None and flags & 131072: return 'cast'
    raise ValueError('Parameter encoding unknown; wait for AUTOPILOT_VERSION')

def decode_parameter(wire, kind, encoding):
    if kind == 9: return float(wire)
    if kind not in FORMATS: raise ValueError('Standard parameter type unsupported')
    if encoding == 'cast': return validate_parameter_value(float(wire), kind)
    fmt = FORMATS[kind]
    return struct.unpack('<'+fmt, struct.pack('<f', wire)[:struct.calcsize(fmt)])[0]

def encode_parameter(value, kind, encoding):
    value = validate_parameter_value(value, kind)
    if kind == 9: return struct.unpack('<f', struct.pack('<f',value))[0]
    if kind not in FORMATS: raise ValueError('Use a qualified extended parameter adapter for this type')
    if encoding == 'cast':
        wire = struct.unpack('<f',struct.pack('<f',value))[0]
        if wire != value: raise ValueError('Value cannot be represented exactly with firmware C-cast encoding')
        return wire
    return struct.unpack('<f',struct.pack('<'+FORMATS[kind],value).ljust(4,b'\0'))[0]
