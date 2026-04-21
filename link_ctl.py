#!/usr/bin/env python3
from __future__ import annotations
"""link-ctl — Insta360 Link webcam controller

Controls the Insta360 Link (original) via the Insta360 Link Controller desktop
app's local WebSocket server. The app must be running.

Protocol: binary Protocol Buffers over WebSocket.
Reference: https://dt.in.th/Insta360LinkControllerWebSocketProtocol
"""

import argparse
import asyncio
import json
import os
import platform
import re
import struct
import subprocess
import sys
import time
from pathlib import Path

# ── Protobuf wire encoding ───────────────────────────────────────────────────
# Manual implementation — no protobuf library required.
# Wire types: 0=varint, 1=64-bit, 2=LEN, 5=32-bit

def _varint(n: int) -> bytes:
    """Encode integer as protobuf varint. Handles negatives via 64-bit two's-complement."""
    if n < 0:
        n += 1 << 64
    out = []
    while True:
        b = n & 0x7F
        n >>= 7
        out.append(b | (0x80 if n else 0))
        if not n:
            break
    return bytes(out)

def _tag(field: int, wire: int) -> bytes:
    return _varint((field << 3) | wire)

def _bool_f(field: int, val: bool) -> bytes:
    return _tag(field, 0) + (b'\x01' if val else b'\x00')

def _float32_f(field: int, val: float) -> bytes:
    """Encode float as protobuf fixed32 (wire type 5)."""
    return _tag(field, 5) + struct.pack('<f', val)

def _int_f(field: int, val: int) -> bytes:
    return _tag(field, 0) + _varint(val)

def _str_f(field: int, val: str) -> bytes:
    b = val.encode('utf-8')
    return _tag(field, 2) + _varint(len(b)) + b

def _msg_f(field: int, payload: bytes) -> bytes:
    return _tag(field, 2) + _varint(len(payload)) + payload

# ── Protobuf wire decoding ───────────────────────────────────────────────────

def _read_varint(data: bytes, pos: int) -> tuple:
    result, shift = 0, 0
    while pos < len(data):
        b = data[pos]; pos += 1
        result |= (b & 0x7F) << shift
        shift += 7
        if not (b & 0x80):
            break
    return result, pos

def _decode_fields(data: bytes) -> dict:
    """Decode protobuf bytes → {field_num: [values...]}. Values are int or bytes."""
    pos, fields = 0, {}
    while pos < len(data):
        if pos >= len(data):
            break
        tag, pos = _read_varint(data, pos)
        field_num, wire_type = tag >> 3, tag & 0x7
        if wire_type == 0:
            val, pos = _read_varint(data, pos)
            fields.setdefault(field_num, []).append(val)
        elif wire_type == 2:
            length, pos = _read_varint(data, pos)
            val = data[pos:pos + length]; pos += length
            fields.setdefault(field_num, []).append(val)
        elif wire_type == 1:   # 64-bit fixed
            pos += 8
        elif wire_type == 5:   # 32-bit fixed
            pos += 4
        else:
            break  # unknown wire type — stop parsing
    return fields

def _str_from(fields: dict, num: int) -> str:
    v = fields.get(num)
    return v[0].decode('utf-8', errors='replace') if v and isinstance(v[0], bytes) else ''

def _int_from(fields: dict, num: int, default: int = 0) -> int:
    v = fields.get(num)
    return v[0] if v and isinstance(v[0], int) else default

# ── Enum constants ────────────────────────────────────────────────────────────

class ParamTypeV2:
    """v2.2.1 ValueChangeNotification paramTypes.

    Sources: empirical captures + insta360linkcontroller.proto enum + DeviceInfo field numbers.
    NOTE: v2.2.1 renumbered several params relative to the old proto ParamType enum.
    """
    # ── Confirmed from captures ───────────────────────────────────────────────
    NORMAL_RESET      = 3   # Reset pan/tilt to center (no value field)
    ZOOM              = 4   # Zoom level, string "100"-"400"
    AI_MODE           = 5   # AI mode selector; see AIMode for values
    JOYSTICK_MOVE     = 6   # Pan/tilt velocity, float32 sub-msg
    JOYSTICK_STOP     = 7   # Joystick release; both floats = 0.0
    HORIZONTAL_FLIP   = 2   # Horizontal flip on/off; "1"/"0" (confirmed from tshark capture)
    # ── Confirmed from direct WS tests (v2.2.1) ───────────────────────────────
    EXPOSURE_COMP     = 16  # Exposure compensation; numeric string "0"-"100" (default 50)
    AUTO_EXPOSURE     = 17  # Auto exposure on/off; "1"/"0"
    WB_TEMP           = 21  # White balance temperature in Kelvin; e.g. "3350", "10000"
                            # Only effective when AUTO_WB is off. Range: ~2800–10000.
    AUTO_WB           = 20  # Auto white balance on/off; "1"/"0"
    BRIGHTNESS        = 22  # Brightness slider; numeric string "0"-"100"
    CONTRAST          = 23  # Contrast slider; numeric string "0"-"100"
    SATURATION        = 24  # Saturation slider; "0"=B&W, default ~50
    SHARPNESS         = 25  # Sharpness slider; numeric string "0"-"100"
    HDR               = 26  # HDR on/off; "1"/"0"
    GESTURE_ZOOM      = 39  # Gesture control zoom on/off; "1"/"0" (confirmed from tshark capture)
    ANTI_FLICKER      = 27  # Anti-flicker: "0"=Auto, "1"=50Hz, "2"=60Hz (all confirmed 2026-03-10)
    AUTOFOCUS         = 18  # Auto focus; "1"/"0" (confirmed from tshark capture 2026-03-10)
    SMART_COMPOSITION = 11  # Smart composition on/off; "1"/"0" (confirmed from tshark 2026-03-10)
    SMART_COMP_FRAME  = 10  # Smart composition framing; "1"=Head, "2"=HalfBody, "3"=WholeBody (confirmed)

class AIMode:
    """Command values for ParamTypeV2.AI_MODE (paramType=5). Confirmed from captures."""
    NORMAL     = "0"   # Standard mode (disables all AI)
    TRACKING   = "1"   # Subject tracking
    OVERHEAD   = "4"   # Overhead / top-down
    DESKVIEW   = "5"   # DeskView (desk surface)
    WHITEBOARD = "6"   # Whiteboard

class VideoMode:
    """Device info mode field values. In v2.2.1 these match the AIMode command values."""
    NORMAL     = 0
    TRACKING   = 1
    OVERHEAD   = 4
    DESKVIEW   = 5
    WHITEBOARD = 6

# ── Request builders ─────────────────────────────────────────────────────────
# Request wrapper (outer envelope, v2.2.1):
#   field 4  bool hasControlRequest      → ControlRequest (auth handshake)
#   field 5  bool hasHeartbeatRequest    → HeartbeatRequest
#   field 6  bool hasPresetUpdateRequest → PresetUpdateRequest (preset recall/save)
#   field 7  bool hasValueChangeNotify   → ValueChangeNotification (zoom/AI/joystick)
#   field 13 ControlRequest   controlRequest
#   field 14 HeartbeatRequest heartbeatRequest (empty)
#   field 15 PresetUpdateRequest presetUpdateRequest
#   field 16 ValueChangeNotification valueChangeNotify

def build_control_request(token: str = "link-ctl") -> bytes:
    ctrl = _str_f(1, token)
    return _bool_f(4, True) + _msg_f(13, ctrl)

def build_heartbeat() -> bytes:
    return _bool_f(5, True) + _msg_f(14, b'')

def build_value_change(serial: str, param_type: int, value: str | None = None) -> bytes:
    """Build a ValueChangeNotification command (v2.2.1 protocol).
    ValueChangeNotification: field 1=serial, field 2=paramType, field 3=newValue (string).
    Outer envelope: field 7=true, field 16=message.
    """
    vc = _str_f(1, serial) + _int_f(2, param_type)
    if value is not None:
        vc += _str_f(3, value)
    return _bool_f(7, True) + _msg_f(16, vc)

def build_zoom(serial: str, value: int) -> bytes:
    # v2.2.1: paramType=4, string value (confirmed from phone capture)
    return build_value_change(serial, ParamTypeV2.ZOOM, str(value))

def build_preset_save(serial: str, index: int) -> bytes:
    """PresetUpdateRequest type=0 (ADD): save current position as a new preset at index.

    This matches the mobile web UI's "+ Add new preset" button — confirmed by
    wire-level capture on 2026-04-17: the mobile UI sends type=0 ADD and the
    preset persists. Our previous type=1 (UPDATE) implementation returned
    success=0 from the server and failed to persist empty slots, which looked
    like a broken command. The server's success field is a misleading ack —
    both types echo success=0, but only ADD actually creates the preset.

    Use build_preset_update() (type=1) if you want to overwrite an already
    populated slot with a new position.
    """
    inner = _int_f(1, 0) + _int_f(2, index) + _str_f(4, serial)
    return _bool_f(6, True) + _msg_f(15, inner)

def build_preset_update(serial: str, index: int) -> bytes:
    """PresetUpdateRequest type=1 (UPDATE): overwrite an already-populated
    preset at index with the current camera position. Only works for non-empty
    slots; for empty slots use build_preset_save (type=0 ADD)."""
    inner = _int_f(1, 1) + _int_f(2, index) + _str_f(4, serial)
    return _bool_f(6, True) + _msg_f(15, inner)

def build_preset_delete(serial: str, index: int) -> bytes:
    """PresetUpdateRequest type=2 (DELETE): remove preset at index.
    Confirmed wire format from capture: field1=op, field2=index, field4=serial.
    """
    inner = _int_f(1, 2) + _int_f(2, index) + _str_f(4, serial)
    return _bool_f(6, True) + _msg_f(15, inner)

def build_preset_rename(serial: str, index: int, name: str) -> bytes:
    """PresetUpdateRequest type=3 (RENAME): rename preset at index.
    Wire format follows the existing pattern plus field3=name (proto:
    PresetUpdateRequest.name = 3).
    """
    inner = (_int_f(1, 3) + _int_f(2, index)
             + _str_f(3, name) + _str_f(4, serial))
    return _bool_f(6, True) + _msg_f(15, inner)

def build_preset_recall(serial: str, index: int) -> bytes:
    """PresetUpdateRequest type=4 (RECALL): load preset at index.
    Confirmed wire format from capture: field1=op, field2=index, field4=serial.
    """
    inner = _int_f(1, 4) + _int_f(2, index) + _str_f(4, serial)
    return _bool_f(6, True) + _msg_f(15, inner)

def build_joystick(serial: str, pan_vel: float, tilt_vel: float) -> bytes:
    """Build a joystick velocity command (paramType=6). pan/tilt: -1.0=left/down, +1.0=right/up."""
    sub = _float32_f(1, pan_vel) + _float32_f(2, tilt_vel)
    vc = _str_f(1, serial) + _int_f(2, ParamTypeV2.JOYSTICK_MOVE) + _msg_f(4, sub)
    return _bool_f(7, True) + _msg_f(16, vc)

def build_joystick_stop(serial: str) -> bytes:
    """Build a joystick stop command (paramType=7); both velocities = 0.0."""
    sub = _float32_f(1, 0.0) + _float32_f(2, 0.0)
    vc = _str_f(1, serial) + _int_f(2, ParamTypeV2.JOYSTICK_STOP) + _msg_f(4, sub)
    return _bool_f(7, True) + _msg_f(16, vc)

# ── Response parser ──────────────────────────────────────────────────────────
# Response wrapper fields:
#   1  bool hasDeviceInfoNotify
#   3  bool hasControlResponse
#   4  bool hasConnectionNotify
#   10 DeviceInfoNotification  deviceInfoNotify
#   12 ControlResponse         controlResponse
#   13 ConnectionNotification  connectionNotify

def parse_response(data: bytes) -> dict:
    f = _decode_fields(data)
    result = {}

    if 13 in f:
        cn = _decode_fields(f[13][0])
        result['connectionNotify'] = {
            'connectNum': _int_from(cn, 1),
            'inControl':  bool(_int_from(cn, 2)),
        }

    if 12 in f:
        cr = _decode_fields(f[12][0])
        result['controlResponse'] = {
            'success': bool(_int_from(cr, 1)),
            'reason':  _int_from(cr, 2),
        }

    if 10 in f:
        di = _decode_fields(f[10][0])
        info = {'curDeviceSerialNum': '', 'devices': []}

        def _parse_device(dev: dict) -> dict:
            # v2.2.1: ZoomInfo at field 5; v1.4.1: field 3
            zoom_info = {}
            for zoom_field in (5, 3):
                v = dev.get(zoom_field, [None])[0]
                if isinstance(v, bytes):
                    zi = _decode_fields(v)
                    zoom_info = {
                        'curValue': _int_from(zi, 1),
                        'minValue': _int_from(zi, 2),
                        'maxValue': _int_from(zi, 3),
                    }
                    break

            # v2.2.1: image/camera settings moved to a nested sub-message at field 10.
            # Sub-field numbering matches the old v1.4.1 DeviceInfo proto fields.
            img = {}
            img_bytes = dev.get(10, [None])[0]
            if isinstance(img_bytes, bytes):
                s = _decode_fields(img_bytes)
                img = {
                    'hdr':                bool(_int_from(s,  9)),
                    'brightness':         _int_from(s, 12),
                    'contrast':           _int_from(s, 13),
                    'saturation':         _int_from(s, 14),
                    'sharpness':          _int_from(s, 15),
                    'autoExposure':       bool(_int_from(s, 17)),
                    'exposureComp':       _int_from(s, 20),
                    'autoWhiteBalance':   bool(_int_from(s, 21)),
                    'wbTemp':             _int_from(s, 22),
                    'smartComposition':   bool(_int_from(s, 24)),
                }

            return {
                'deviceName':   _str_from(dev, 1),
                'serialNum':    _str_from(dev, 2),
                'zoom':         zoom_info,
                'mode':         _int_from(dev, 4),
                'mirror':       bool(_int_from(dev, 5)),
                'curPresetPos': _int_from(dev, 9),
                **img,
            }

        # v2.2.1 layout: field 1 = DeviceInfo message, field 2 = curDeviceSerialNum string
        # v1.4.1 layout: field 1 = curDeviceSerialNum string, field 2 = repeated DeviceInfo
        # Detect by checking if field 1 is a short ASCII string (<64 chars)
        f1_is_string = False
        if 1 in di and isinstance(di[1][0], bytes):
            try:
                s = di[1][0].decode('utf-8')
                f1_is_string = len(s) < 64 and '\x00' not in s
            except UnicodeDecodeError:
                pass

        if f1_is_string:
            # v1.4.1: field 1 = serial string, field 2 = repeated DeviceInfo
            info['curDeviceSerialNum'] = di[1][0].decode('utf-8', errors='replace')
            for dev_bytes in di.get(2, []):
                info['devices'].append(_parse_device(_decode_fields(dev_bytes)))
        else:
            # v2.2.1: field 1 = DeviceInfo message, field 2 = curDeviceSerialNum string
            for dev_bytes in di.get(1, []):
                info['devices'].append(_parse_device(_decode_fields(dev_bytes)))
            info['curDeviceSerialNum'] = _str_from(di, 2)

        result['deviceInfoNotify'] = info

    if 14 in f:
        result['heartbeatResponse'] = {}

    return result

# ── State / cache ────────────────────────────────────────────────────────────

CONFIG_DIR = Path.home() / '.config' / 'link-ctl'
STATE_FILE  = CONFIG_DIR / 'state.json'
PRESET_FILE = CONFIG_DIR / 'presets.json'   # host-side USB preset storage
PORT_CACHE_TTL = 300  # 5 minutes

def load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {}

def save_state(state: dict):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2) + '\n')

def invalidate_port_cache():
    state = load_state()
    state.pop('port', None)
    state.pop('timestamp', None)
    save_state(state)

# ── USB-direct PTZ + host-side preset storage ────────────────────────────────
# The Insta360 Link has no firmware-side preset feature — presets are purely
# host-side state (same design as vrwallace/Insta360-Link-Controller-for-Linux
# and the Insta360 SDK). link-ctl stores (pan, tilt, zoom) tuples in a local
# JSON file and uses standard UVC Camera-Terminal controls to read/write
# position via our uvc-probe binary (no WebSocket, no Link Controller app,
# no token required).
#
# Wire format gotcha: unit 9 sel 0x1a readback returns the 8-byte tuple in
# (tilt, pan) order, whereas unit 1 sel 0x0D SET_CUR expects standard UVC
# (pan, tilt) order. We convert on each boundary; logical pan/tilt values
# in this module are always in the natural (pan, tilt) sense.

UVC_PROBE = Path(__file__).resolve().parent / 'tools' / 'uvc-probe'

# Camera Terminal (unit 1) standard UVC controls
CT_PANTILT_ABSOLUTE_SEL = 0x0D   # 8-byte: pan int32 LE + tilt int32 LE
CT_ZOOM_ABSOLUTE_SEL    = 0x0B   # 2-byte: zoom uint16 LE (100..400)

# Insta360 XU1 (unit 9) pan/tilt readback (SET_CUR ignored here; reads in
# reversed (tilt, pan) order).
XU_PANTILT_READ_UNIT = 9
XU_PANTILT_READ_SEL  = 0x1A

# Prefer the in-process ctypes IOKit path (link_usb_macos) when running on
# macOS — no subprocess overhead, no external binary required. Falls back
# to tools/uvc-probe (subprocess) if the ctypes module can't find the
# camera.
_usb_backend = None
try:
    if platform.system() == 'Darwin':
        import link_usb_macos as _usb_backend   # type: ignore
except Exception:
    _usb_backend = None

def _uvc_probe_available() -> bool:
    """True if any USB-direct path can reach the camera — either the
    in-process ctypes backend or the `tools/uvc-probe` helper binary."""
    if _usb_backend is not None:
        try:
            _usb_backend._get_handle()
            return True
        except Exception:
            pass
    return UVC_PROBE.is_file() and os.access(UVC_PROBE, os.X_OK)

def _uvc_get(unit: int, sel: int, length: int) -> bytes:
    if _usb_backend is not None:
        try:
            return _usb_backend.get(unit, sel, length)
        except Exception:
            pass
    r = subprocess.run(
        [str(UVC_PROBE), 'get', str(unit), f'0x{sel:02x}', str(length)],
        capture_output=True, text=True, timeout=3)
    if r.returncode != 0:
        raise RuntimeError(f'uvc-probe get u={unit} s=0x{sel:02x} failed: {r.stderr.strip()}')
    return bytes.fromhex(r.stdout.strip())

def _uvc_set(unit: int, sel: int, data: bytes) -> None:
    if _usb_backend is not None:
        try:
            _usb_backend.set(unit, sel, data); return
        except Exception:
            pass
    r = subprocess.run(
        [str(UVC_PROBE), 'set', str(unit), f'0x{sel:02x}', data.hex()],
        capture_output=True, text=True, timeout=3)
    if r.returncode != 0:
        raise RuntimeError(f'uvc-probe set u={unit} s=0x{sel:02x} failed: {r.stderr.strip()}')

def read_pantilt() -> tuple[int, int]:
    """Return current (pan, tilt). Reads XU1 sel 0x1a which returns the pair
    in reversed (tilt, pan) order — we swap so callers always see (pan, tilt)."""
    raw = _uvc_get(XU_PANTILT_READ_UNIT, XU_PANTILT_READ_SEL, 8)
    t, p = struct.unpack('<ii', raw)
    return p, t

def write_pantilt(pan: int, tilt: int) -> None:
    """Send standard UVC CT_PANTILT_ABSOLUTE at unit 1 sel 0x0d. Firmware
    snaps the requested position to the nearest supported stop."""
    _uvc_set(1, CT_PANTILT_ABSOLUTE_SEL, struct.pack('<ii', pan, tilt))

def read_zoom() -> int:
    raw = _uvc_get(1, CT_ZOOM_ABSOLUTE_SEL, 2)
    return struct.unpack('<H', raw)[0]

def write_zoom(zoom: int) -> None:
    if not (100 <= zoom <= 400):
        raise ValueError(f'zoom {zoom} out of range 100..400')
    _uvc_set(1, CT_ZOOM_ABSOLUTE_SEL, struct.pack('<H', zoom))

def load_presets() -> dict:
    try:
        d = json.loads(PRESET_FILE.read_text())
        return d if isinstance(d, dict) and 'presets' in d else {'version': 1, 'presets': {}}
    except Exception:
        return {'version': 1, 'presets': {}}

def save_presets(presets: dict) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    PRESET_FILE.write_text(json.dumps(presets, indent=2) + '\n')

def usb_preset_save(idx: int, name: str | None = None) -> dict:
    pan, tilt = read_pantilt()
    zoom = read_zoom()
    presets = load_presets()
    entry = presets['presets'].get(str(idx), {})
    entry.update({
        'name': name or entry.get('name') or f'preset_{idx}',
        'pan': pan, 'tilt': tilt, 'zoom': zoom,
    })
    presets['presets'][str(idx)] = entry
    save_presets(presets)
    return entry

def usb_preset_recall(idx: int) -> dict:
    presets = load_presets()
    entry = presets['presets'].get(str(idx))
    if not entry:
        raise KeyError(f'no preset at slot {idx}')
    write_pantilt(entry['pan'], entry['tilt'])
    write_zoom(entry['zoom'])
    return entry

def usb_preset_delete(idx: int) -> bool:
    presets = load_presets()
    if str(idx) not in presets['presets']:
        return False
    del presets['presets'][str(idx)]
    save_presets(presets)
    return True

def usb_preset_rename(idx: int, name: str) -> dict:
    presets = load_presets()
    entry = presets['presets'].get(str(idx))
    if not entry:
        raise KeyError(f'no preset at slot {idx}')
    entry['name'] = name
    presets['presets'][str(idx)] = entry
    save_presets(presets)
    return entry

def usb_preset_list() -> list[dict]:
    presets = load_presets()
    return [
        {'id': int(k), **v}
        for k, v in sorted(presets['presets'].items(), key=lambda x: int(x[0]))
    ]

# ── USB-direct image / AI control selector map ──────────────────────────────
# Selectors for the original Link's Processing Unit (5), Camera Terminal (1),
# and XU1 (unit 9). High-confidence entries come from xu_verify.py roundtrip
# tests on this repo; medium-confidence entries (AI mode buffer layout, smart
# framing selector) come from vrwallace's Linux controller research. Range
# limits match the desktop app's sliders.
#
# References:
#   - session-state.md "Verified XU Control Map (16 controls)"
#   - https://github.com/vrwallace/Insta360-Link-1-and-2-Controller-for-Linux

# Unit 9 sel 0x1b: 2-byte LE bitmask driving XU_FUNC_ENABLE_CONTROL. Each bit
# toggles a separate feature; read-modify-write to avoid clobbering.
#
# Bit assignments are aligned with the order of the SDK's ExtendFunction
# enum (sdk.ts: AiZoom, AF, HDR, Mirror, Ai, VScreen, …). HDR/Mirror/Ai at
# bits 2/3/4 are empirically verified via xu_verify.py. Other bits (AiZoom
# aka "smart composition" at bit 0 per enum order; VScreen / EnableStartup /
# SingleTap / FullTracking at bits 5..9 per enum order) are not yet
# mapped — the `smartcomposition` command still falls through to the WS
# path (paramType 11) until we can empirically confirm each bit.
BITMASK_UNIT = 9
BITMASK_SEL  = 0x1B
BIT_HDR           = 2
BIT_MIRROR        = 3
BIT_GESTURE_ZOOM  = 4
# TBD: BIT_SMARTCOMP / BIT_AF / BIT_VSCREEN / … — need capture to confirm

def _bitmask_get() -> int:
    return int.from_bytes(_uvc_get(BITMASK_UNIT, BITMASK_SEL, 2), 'little')

def _bitmask_set_bit(bit: int, on: bool) -> None:
    val = _bitmask_get()
    val = val | (1 << bit) if on else val & ~(1 << bit)
    _uvc_set(BITMASK_UNIT, BITMASK_SEL, val.to_bytes(2, 'little'))

def _bitmask_get_bit(bit: int) -> bool:
    return bool(_bitmask_get() & (1 << bit))

# AI video-mode buffer at unit 9 sel 0x02 — 52 bytes, byte[0]=mode_id,
# byte[1]=mode_flag, rest zero. vrwallace's map:
AI_MODE_SEL   = 0x02
AI_MODE_LEN   = 52
AI_MODE_BYTES = {
    'normal':     (0x00, 0x00),
    'track':      (0x01, 0x00),
    'whiteboard': (0x04, 0x01),
    'overhead':   (0x05, 0x03),
    'deskview':   (0x06, 0x10),
}

def write_ai_mode(mode_name: str) -> None:
    mode_id, flag = AI_MODE_BYTES[mode_name]
    buf = bytearray(AI_MODE_LEN)
    buf[0] = mode_id
    buf[1] = flag
    _uvc_set(9, AI_MODE_SEL, bytes(buf))

def read_ai_mode() -> str:
    """Read the current video mode. The firmware's GET_LEN reports 1 for
    this selector even though SET_CUR accepts the full 52-byte AI buffer;
    we try the short form first, then fall back to the long one."""
    raw = None
    for length in (1, 2, 52):
        try:
            raw = _uvc_get(9, AI_MODE_SEL, length); break
        except Exception: continue
    if not raw: return 'unknown'
    mid = raw[0]; flag = raw[1] if len(raw) > 1 else 0
    for name, (m, f) in AI_MODE_BYTES.items():
        if m == mid and (len(raw) < 2 or f == flag):
            return name
    return f'unknown(0x{mid:02x}/0x{flag:02x})'

# Smart framing (composition style) at unit 9 sel 0x13 (19), 1 byte.
FRAMING_SEL = 0x13
FRAMING_BYTES = {'head': 1, 'halfbody': 2, 'wholebody': 3}

# Exposure-compensation scale: XU stores as int16 LE where 100 = 1 EV.
# Desktop app exposes 0..100 where 50 = 0 EV; here we keep the same API
# surface for consistency. Internal value = (user_value - 50) * 6 (gives
# ±3 EV range across 0..100 in 0.06 EV steps, matching the WS behavior).
def _ec_user_to_wire(v: int) -> bytes:
    return struct.pack('<h', (v - 50) * 6)

def _ec_wire_to_user(raw: bytes) -> int:
    return struct.unpack('<h', raw)[0] // 6 + 50

# AE mode at unit 9 sel 0x1e — 1 byte. 2=auto, 1=manual.
# Anti-flicker at unit 5 sel 0x05 — 1 byte. 3=auto, 1=50Hz, 2=60Hz.
AF_MAP = {'auto': 3, '50hz': 1, '60hz': 2}


# ── USB-direct PTZ commands ──────────────────────────────────────────────────
# Same step sizing as the joystick UI so behavior matches across interfaces.
USB_PAN_STEP  = 3000
USB_TILT_STEP = 3000

def usb_image_dispatch(args) -> None:
    """USB-direct image and AI-mode commands via uvc-probe — no WebSocket,
    no Link Controller, no token required. Covers every control the desktop
    app exposes except firmware update / device rename / factory reset."""
    cmd = args.command
    try:
        # ── Bitmask-bit controls (HDR / mirror / gesture-zoom) ────────────────
        if cmd in ('hdr', 'mirror', 'gesture-zoom'):
            bit = {'hdr': BIT_HDR, 'mirror': BIT_MIRROR,
                   'gesture-zoom': BIT_GESTURE_ZOOM}[cmd]
            target = args.state
            cur = _bitmask_get_bit(bit)
            if target in (None, 'toggle'):
                target = 'off' if cur else 'on'
            _bitmask_set_bit(bit, target == 'on')
            _info(f"{cmd}: {'on' if cur else 'off'} → {target}")

        # ── Processing Unit 1-byte scalars ────────────────────────────────────
        elif cmd == 'brightness':
            v = args.value
            if not (0 <= v <= 100): raise ValueError('brightness 0..100')
            _uvc_set(5, 0x02, bytes([v])); _info(f'brightness → {v}')
        elif cmd == 'contrast':
            v = args.value
            if not (0 <= v <= 100): raise ValueError('contrast 0..100')
            _uvc_set(5, 0x03, bytes([v])); _info(f'contrast → {v}')

        # ── Processing Unit 2-byte scalars ────────────────────────────────────
        elif cmd == 'saturation':
            v = args.value
            if not (0 <= v <= 100): raise ValueError('saturation 0..100')
            _uvc_set(5, 0x07, struct.pack('<H', v)); _info(f'saturation → {v}')
        elif cmd == 'sharpness':
            v = args.value
            if not (0 <= v <= 100): raise ValueError('sharpness 0..100')
            _uvc_set(5, 0x08, struct.pack('<H', v)); _info(f'sharpness → {v}')
        elif cmd == 'wb-temp':
            v = args.value
            if not (2800 <= v <= 10000):
                raise ValueError('wb-temp 2800..10000 K (AWB must be off)')
            _uvc_set(5, 0x0A, struct.pack('<H', v))
            _info(f'wb-temp → {v} K')

        # ── Exposure / AWB / AF / anti-flicker ────────────────────────────────
        elif cmd == 'exposurecomp':
            v = args.value
            if not (0 <= v <= 100): raise ValueError('exposurecomp 0..100')
            _uvc_set(9, 0x09, _ec_user_to_wire(v))
            _info(f'exposurecomp → {v}  (50 = 0 EV)')
        elif cmd == 'autoexposure':
            target = args.state
            cur_raw = _uvc_get(9, 0x1e, 1)
            cur = 'on' if cur_raw == bytes([2]) else 'off'
            if target in (None, 'toggle'):
                target = 'off' if cur == 'on' else 'on'
            _uvc_set(9, 0x1e, bytes([2 if target == 'on' else 1]))
            _info(f'autoexposure: {cur} → {target}')
        elif cmd == 'awb':
            target = args.state
            cur = 'on' if _uvc_get(5, 0x0B, 1) == bytes([1]) else 'off'
            if target in (None, 'toggle'):
                target = 'off' if cur == 'on' else 'on'
            _uvc_set(5, 0x0B, bytes([1 if target == 'on' else 0]))
            _info(f'awb: {cur} → {target}')
        elif cmd == 'anti-flicker':
            val = AF_MAP.get(args.mode.lower())
            if val is None: raise ValueError('anti-flicker: auto|50hz|60hz')
            _uvc_set(5, 0x05, bytes([val]))
            _info(f'anti-flicker → {args.mode}')
        elif cmd == 'autofocus':
            state = args.state
            _uvc_set(1, 0x08, bytes([1 if state == 'on' else 0]))
            _info(f'autofocus → {state}')

        # ── AI modes (mutually exclusive video modes via unit 9 sel 2) ────────
        elif cmd in ('track', 'deskview', 'whiteboard', 'overhead', 'normal'):
            # Commands with optional on/off/toggle: ON sets the mode, OFF
            # resets to normal, TOGGLE swaps between current and mode.
            target_mode = {'track': 'track', 'deskview': 'deskview',
                           'whiteboard': 'whiteboard', 'overhead': 'overhead',
                           'normal': 'normal'}[cmd]
            if cmd == 'normal':
                write_ai_mode('normal'); _info('mode → normal'); return
            state = args.state
            cur = read_ai_mode()
            if state in (None, 'toggle'):
                state = 'off' if cur == target_mode else 'on'
            new_mode = target_mode if state == 'on' else 'normal'
            write_ai_mode(new_mode)
            _info(f"{cmd}: {cur} → {new_mode}")

        # ── Smart composition framing ─────────────────────────────────────────
        elif cmd == 'smartcomp-frame':
            v = FRAMING_BYTES.get(args.frame)
            if v is None: raise ValueError('frame: head|halfbody|wholebody')
            _uvc_set(9, FRAMING_SEL, bytes([v]))
            _info(f'smartcomp-frame → {args.frame}')

        else:
            raise ValueError(f'usb_image_dispatch: unexpected {cmd!r}')

    except Exception as e:
        _warn(f'✗ {cmd} failed: {e}')
        sys.exit(3)


def usb_ptz_dispatch(args) -> None:
    """Execute PTZ commands directly over USB via uvc-probe — no WebSocket,
    no Link Controller, no token required. Mirrors linux_ptz_dispatch() but
    uses our own IOKit ControlRequest path on macOS."""
    cmd = args.command
    try:
        if cmd == 'zoom':
            if not (100 <= args.value <= 400):
                _warn('✗ zoom must be 100..400'); sys.exit(1)
            write_zoom(args.value)
            _info(f'zoom → {args.value}')

        elif cmd == 'zoom-rel':
            cur = read_zoom()
            new = max(100, min(400, cur + args.delta))
            write_zoom(new)
            _info(f'zoom: {cur} → {new}')

        elif cmd == 'pan':
            _, tilt = read_pantilt()
            write_pantilt(args.value, tilt)
            _info(f'pan → {args.value}  (tilt unchanged)')

        elif cmd == 'tilt':
            pan, _ = read_pantilt()
            write_pantilt(pan, args.value)
            _info(f'tilt → {args.value}  (pan unchanged)')

        elif cmd == 'pan-rel':
            pan, tilt = read_pantilt()
            new_pan = pan + args.steps * USB_PAN_STEP
            write_pantilt(new_pan, tilt)
            _info(f'pan: {pan} → {new_pan}  ({args.steps:+d} steps)')

        elif cmd == 'tilt-rel':
            pan, tilt = read_pantilt()
            new_tilt = tilt + args.steps * USB_TILT_STEP
            write_pantilt(pan, new_tilt)
            _info(f'tilt: {tilt} → {new_tilt}  ({args.steps:+d} steps)')

        elif cmd == 'center':
            write_pantilt(0, 0)
            write_zoom(100)
            _info('centered: pan=0 tilt=0 zoom=100')

        else:
            raise ValueError(f'usb_ptz_dispatch: unexpected command {cmd!r}')
    except Exception as e:
        _warn(f'✗ {cmd} failed: {e}')
        sys.exit(3)

# ── Preflight: process / USB checks ─────────────────────────────────────────

CONTROLLER_NAME = "Insta360 Link Controller"

# startup.ini location varies by platform
def _startup_ini_path() -> Path:
    system = platform.system()
    if system == 'Darwin':
        return Path.home() / "Library" / "Application Support" / "Insta360 Link Controller" / "startup.ini"
    elif system == 'Windows':
        # %LOCALAPPDATA%\Insta360 Link Controller\startup.ini
        local_app = os.environ.get('LOCALAPPDATA', str(Path.home() / 'AppData' / 'Local'))
        return Path(local_app) / "Insta360 Link Controller" / "startup.ini"
    else:
        # Linux: not officially supported by Link Controller, but try XDG
        xdg = os.environ.get('XDG_CONFIG_HOME', str(Path.home() / '.config'))
        return Path(xdg) / "Insta360 Link Controller" / "startup.ini"

STARTUP_INI = _startup_ini_path()

def _read_all_tokens_from_ini() -> list[str]:
    """Return every token from startup.ini, newest timestamp first.

    The Link Controller keeps multiple tokens in [Token] — some permanently
    authorized devices (old timestamps), plus ephemeral QR-scan tokens (new
    timestamps). A token generated for a previous QR session may still be
    valid even if it's not the newest. When timestamps tie, any of the tied
    tokens may be the live one; callers should try each until one succeeds.
    """
    try:
        content = STARTUP_INI.read_text(errors='replace')
        m = re.search(r'\[Token\](.*?)(?:\[|$)', content, re.DOTALL)
        if not m:
            return []
        pairs: list[tuple[int, str]] = []
        for line in m.group(1).strip().splitlines():
            if '=' in line:
                k, v = line.split('=', 1)
                ts = int(v.strip()) if v.strip().isdigit() else 0
                pairs.append((ts, k.strip()))
        # Sort by timestamp desc so freshest is tried first.
        pairs.sort(key=lambda p: -p[0])
        return [t for _, t in pairs]
    except Exception:
        return []

def _read_token_from_ini() -> str | None:
    """Return the newest token, for callers that only want one (legacy path)."""
    tokens = _read_all_tokens_from_ini()
    return tokens[0] if tokens else None

def _controller_running() -> bool:
    system = platform.system()
    try:
        if system == 'Windows':
            # tasklist /FI "IMAGENAME eq Insta360*" — outputs a line per match
            r = subprocess.run(
                ['tasklist', '/FI', f'IMAGENAME eq {CONTROLLER_NAME}.exe', '/NH'],
                capture_output=True, text=True)
            return CONTROLLER_NAME.lower() in r.stdout.lower()
        else:
            r = subprocess.run(['pgrep', '-f', CONTROLLER_NAME],
                               capture_output=True, text=True)
            return r.returncode == 0
    except Exception:
        return False

def _camera_usb_present() -> bool:
    system = platform.system()
    if system == 'Windows':
        # wmic path Win32_PnPEntity where "Name like '%Insta360%'" get Name
        try:
            r = subprocess.run(
                ['wmic', 'path', 'Win32_PnPEntity', 'where',
                 "Name like '%Insta360%'", 'get', 'Name'],
                capture_output=True, text=True, timeout=15)
            return bool(r.stdout.strip()) and 'Insta360' in r.stdout
        except Exception:
            pass
        return False

    # macOS: ioreg is faster and more reliable than system_profiler
    try:
        r = subprocess.run(
            ['ioreg', '-p', 'IOUSB', '-w', '0'],
            capture_output=True, text=True, timeout=10)
        if re.search(r'(?i)insta360', r.stdout):
            return True
    except Exception:
        pass
    # Fallback: system_profiler
    try:
        r = subprocess.run(
            'system_profiler SPUSBDataType 2>/dev/null | grep -i "insta360"',
            shell=True, capture_output=True, text=True, timeout=15)
        if r.stdout.strip():
            return True
    except Exception:
        pass
    return False

# ── Port discovery ───────────────────────────────────────────────────────────

PRIORITY_PORTS = [7878, 9090, 9091, 9000, 8080]

def _lsof_port() -> int | None:
    """Find the WebSocket port the Link Controller is listening on."""
    system = platform.system()

    if system == 'Windows':
        # netstat -ano — find PID of controller, then match listening port
        try:
            pids_r = subprocess.run(
                ['tasklist', '/FI', f'IMAGENAME eq {CONTROLLER_NAME}.exe',
                 '/FO', 'CSV', '/NH'],
                capture_output=True, text=True)
            pids = re.findall(r'"(\d+)"', pids_r.stdout)
            if not pids:
                return None
            pid_set = set(pids)
            ns = subprocess.run(['netstat', '-ano'], capture_output=True, text=True, timeout=15)
            for line in ns.stdout.splitlines():
                # TCP  0.0.0.0:PORT  0.0.0.0:0  LISTENING  PID
                m = re.search(r'TCP\s+\S+:(\d+)\s+\S+\s+LISTENING\s+(\d+)', line)
                if m and m.group(2) in pid_set:
                    port = int(m.group(1))
                    if port > 1024:  # skip well-known ports
                        return port
        except Exception:
            pass
        return None

    # macOS / Linux: lsof by PID
    try:
        pids = subprocess.run(['pgrep', '-f', CONTROLLER_NAME],
                              capture_output=True, text=True).stdout.split()
        for pid in pids:
            r = subprocess.run(['lsof', '-i', '-a', '-n', '-P', '-p', pid.strip()],
                               capture_output=True, text=True, timeout=10)
            for line in r.stdout.splitlines():
                if 'LISTEN' in line:
                    m = re.search(r':(\d+)\s+\(LISTEN\)', line)
                    if m:
                        return int(m.group(1))
    except Exception:
        pass
    return None

async def _probe_ws(port: int, timeout: float = 0.5) -> bool:
    try:
        import websockets
        async with websockets.connect(
            f'ws://localhost:{port}',
            open_timeout=timeout,
            close_timeout=0.3,
        ):
            return True
    except Exception:
        return False

async def discover_port(verbose: bool = False, debug: bool = False) -> int | None:
    # 1. Cached port
    state = load_state()
    cached_port = state.get('port')
    cached_ts   = state.get('timestamp', 0)
    if cached_port and (time.time() - cached_ts) < PORT_CACHE_TTL:
        if debug:
            _dbg(f"Using cached port {cached_port}")
        return cached_port

    # 2. lsof
    port = _lsof_port()
    if port:
        if debug:
            _dbg(f"Found port {port} via lsof")
        return port

    # 3. Priority ports
    for p in PRIORITY_PORTS:
        if verbose:
            print(f"  Trying port {p}...", file=sys.stderr)
        if await _probe_ws(p):
            if debug:
                _dbg(f"Found port {p} via priority scan")
            return p

    # 4. Range scan
    scan_ranges = list(range(7000, 8000)) + list(range(9000, 9100)) + list(range(49900, 49950))
    for p in scan_ranges:
        if p in PRIORITY_PORTS:
            continue
        if verbose:
            print(f"  Scanning port {p}...", file=sys.stderr)
        if await _probe_ws(p, 0.3):
            if debug:
                _dbg(f"Found port {p} via range scan")
            return p

    return None

# ── Verbosity / logging ───────────────────────────────────────────────────────
# 0=silent  1=quiet(errors only)  2=verbose/default  3=debug

_VERBOSITY: int = 2

def _info(msg: str):
    """Print informational message to stderr (verbose and debug modes)."""
    if _VERBOSITY >= 2:
        print(msg, file=sys.stderr)

def _warn(msg: str):
    """Print error/warning to stderr (quiet, verbose, and debug modes)."""
    if _VERBOSITY >= 1:
        print(msg, file=sys.stderr)

def _dbg(msg: str, data: bytes | None = None):
    if data is not None:
        hex_str = ' '.join(f'{b:02x}' for b in data)
        print(f'[debug] {msg}: {hex_str}', file=sys.stderr)
    else:
        print(f'[debug] {msg}', file=sys.stderr)

# ── WebSocket client ─────────────────────────────────────────────────────────

class LinkClient:
    def __init__(self, port: int, debug: bool = False, token: str = ""):
        self.port   = port
        self.debug  = debug
        self.token  = token or _read_token_from_ini() or ""
        self.ws     = None
        self.serial = ''
        self.device_info: dict | None = None

    async def _send(self, data: bytes):
        if self.debug:
            _dbg('SEND', data)
        await self.ws.send(data)

    async def _recv(self, timeout: float = 3.0) -> dict | None:
        try:
            raw = await asyncio.wait_for(self.ws.recv(), timeout=timeout)
            if isinstance(raw, str):
                raw = raw.encode('utf-8')
            if self.debug:
                _dbg('RECV', raw)
            return parse_response(raw)
        except asyncio.TimeoutError:
            return None
        except Exception as e:
            if self.debug:
                _dbg(f'recv error: {e}')
            return None

    async def connect(self):
        import websockets
        url = f'ws://localhost:{self.port}/'
        if self.token:
            url += f'?token={self.token}'
        self.ws = await websockets.connect(url, open_timeout=3, ping_interval=None)

    async def handshake(self) -> tuple[bool, str]:
        """Perform the 4-step handshake. Returns (success, error_message)."""
        # Step 1: receive connectionNotify
        msg = await self._recv(3.0)
        if not msg or 'connectionNotify' not in msg:
            return False, f"Did not receive connectionNotify on port {self.port}"

        # Step 2: send controlRequest
        await self._send(build_control_request(self.token))

        # Step 3: receive controlResponse
        msg = await self._recv(3.0)
        if not msg or 'controlResponse' not in msg:
            return False, "Did not receive controlResponse"
        cr = msg['controlResponse']
        if not cr['success']:
            reasons = {1: "another connection exists", 2: "token invalid", 3: "active disconnect"}
            reason_str = reasons.get(cr['reason'], f"reason={cr['reason']}")
            if cr['reason'] == 1:
                return False, (
                    f"Link Controller rejected the control request ({reason_str}).\n"
                    "  Close the mobile web remote interface and retry."
                )
            return False, f"Control request failed: {reason_str}"

        # Step 4: receive deviceInfoNotify (may arrive immediately or after a moment)
        for _ in range(4):
            msg = await self._recv(2.0)
            if not msg:
                break
            if 'deviceInfoNotify' in msg:
                info = msg['deviceInfoNotify']
                self.device_info = info
                devs = info.get('devices', [])
                self.serial = (
                    (devs[0]['serialNum'] if devs else '')
                    or info.get('curDeviceSerialNum', '')
                )
                # Update state cache
                state = load_state()
                state['port'] = self.port
                state['timestamp'] = time.time()
                if self.serial:
                    state['deviceSerialNum'] = self.serial
                    if devs and devs[0].get('zoom'):
                        state['zoom'] = devs[0]['zoom'].get('curValue', state.get('zoom', 100))
                save_state(state)
                break

        # Fall back to cached serial
        if not self.serial:
            self.serial = load_state().get('deviceSerialNum', '')

        return True, ''

    async def send_command(self, payload: bytes, wait_ms: int = 500):
        await self._send(payload)
        # Wait for any response/acknowledgment from the server
        if wait_ms > 0:
            msg = await self._recv(timeout=wait_ms / 1000)
            if self.debug and msg:
                import json
                _dbg(f'cmd response: {json.dumps(msg, default=str)}')

    async def close(self):
        if self.ws:
            try:
                await self.ws.close()
            except Exception:
                pass

# ── Full connect-handshake-command-disconnect flow ───────────────────────────

async def _connect_with_token_cycling(
    port: int, debug: bool = False
) -> tuple["LinkClient | None", str]:
    """Open a WS connection and handshake, cycling through every token in
    startup.ini (newest timestamp first) until one is accepted.

    Tokens can tie on timestamp — after the Link Controller app restarts it
    trims the [Token] list and reuses a uniform timestamp — so picking only
    the "newest" token by naive max-ts is brittle. Returns an open+
    handshook client on success, or (None, error_message) on failure.
    """
    tokens = _read_all_tokens_from_ini() or [""]
    last_err = ""
    for i, token in enumerate(tokens):
        client = LinkClient(port, debug=debug, token=token)
        try:
            await client.connect()
        except Exception as e:
            return None, f"Could not connect to WebSocket on port {port}: {e}"

        ok, err = await client.handshake()
        if ok:
            if debug and i > 0:
                _dbg(f'token[{i}] of {len(tokens)} accepted')
            return client, ''

        await client.close()
        last_err = err
        # Only keep cycling on token-auth failures; other errors (another
        # connection holds the slot, network drop, etc.) won't be fixed by
        # trying a different token.
        if 'token invalid' not in err.lower():
            return None, err

    return None, (
        f"{last_err} — tried {len(tokens)} token(s) from startup.ini. "
        "Open the Link Controller app's QR code screen to regenerate a "
        "token, or reconnect the mobile web remote once."
    )


async def _run(port: int, payloads: list, debug: bool = False) -> int:
    """Connect, handshake, send payload(s), disconnect. Returns exit code."""
    client, err = await _connect_with_token_cycling(port, debug=debug)
    if client is None:
        _warn(f"✗ {err}")
        invalidate_port_cache()
        return 3

    for payload in payloads:
        await client.send_command(payload)

    await client.close()
    return 0

# ── Preflight ────────────────────────────────────────────────────────────────

async def preflight(
    debug: bool = False,
    port_override: int | None = None,
    verbose: bool = False,
) -> int:
    """Run all preflight checks. Returns discovered port or calls sys.exit(2)."""
    system = platform.system()

    if system in ('Darwin', 'Windows'):
        if not _controller_running():
            _warn("✗ Insta360 Link Controller is not running.")
            if system == 'Darwin':
                _warn("  Start it from /Applications/Insta360 Link Controller.app, then retry.")
            else:
                _warn("  Start Insta360 Link Controller from the Start menu, then retry.")
            _warn("  Tip: You can minimize it or turn off preview — the WebSocket server stays active.")
            sys.exit(2)

        if not _camera_usb_present():
            _warn("✗ Insta360 Link not detected via USB.")
            _warn("  Check the cable and try replugging. The camera requires USB power to operate.")
            sys.exit(2)

    if port_override:
        return port_override

    port = await discover_port(verbose=verbose, debug=debug)
    if not port:
        _warn("✗ Could not find Insta360 Link Controller WebSocket server.")
        _warn("  The app is running but the WebSocket server was not detected.")
        _warn("  Ensure you are running Link Controller v1.4.1 or later (check About in the app).")
        _warn("  Try: link-ctl discover --verbose to scan all ports.")
        sys.exit(2)

    # Connectivity test
    client = LinkClient(port, debug=debug)
    try:
        await client.connect()
    except Exception:
        _warn(f"✗ Link Controller WebSocket server found on port {port} but could not connect.")
        _warn("  The camera may be initializing. Wait a few seconds and retry.")
        invalidate_port_cache()
        sys.exit(3)

    try:
        ok, err = await asyncio.wait_for(client.handshake(), timeout=2.0)
    except asyncio.TimeoutError:
        _warn(f"✗ Link Controller WebSocket server found on port {port} but did not respond.")
        _warn("  The camera may be initializing. Wait a few seconds and retry.")
        await client.close()
        sys.exit(3)

    if not ok:
        _warn(f"✗ {err}")
        await client.close()
        sys.exit(3)

    # Update cache
    state = load_state()
    state['port'] = port
    state['timestamp'] = time.time()
    if client.serial:
        state['deviceSerialNum'] = client.serial
    save_state(state)

    await client.close()
    return port

# ── cmd: preflight (diagnostic) ─────────────────────────────────────────────

async def cmd_preflight_check(debug: bool = False, port_override: int | None = None):
    all_pass = True
    system = platform.system()

    if system in ('Darwin', 'Windows'):
        ok = _controller_running()
        print(f"[{'PASS' if ok else 'FAIL'}] Link Controller process running")
        if not ok:
            if system == 'Darwin':
                print("       → Start /Applications/Insta360 Link Controller.app")
            else:
                print("       → Start Insta360 Link Controller from the Start menu")
            all_pass = False

        ok = _camera_usb_present()
        print(f"[{'PASS' if ok else 'FAIL'}] Insta360 Link detected via USB")
        if not ok:
            print("       → Check USB cable; replug if needed")
            all_pass = False
    else:
        print(f"[ -- ] Process/USB checks (not supported on {system})")

    port = port_override or await discover_port(debug=debug)
    if port:
        print(f"[PASS] WebSocket server found on port {port}")
    else:
        print("[FAIL] WebSocket server not found")
        print("       → Ensure Link Controller v1.4.1+ is running")
        all_pass = False
        sys.exit(2 if not all_pass else 0)

    # Connectivity test
    client = LinkClient(port, debug=debug)
    try:
        await client.connect()
        ok, err = await asyncio.wait_for(client.handshake(), timeout=2.0)
        await client.close()
        if ok:
            serial_str = f"; serial {client.serial}" if client.serial else ""
            print(f"[PASS] WebSocket handshake succeeded{serial_str}")
        else:
            print(f"[FAIL] WebSocket handshake: {err}")
            all_pass = False
    except asyncio.TimeoutError:
        print("[FAIL] WebSocket handshake timed out")
        all_pass = False
    except Exception as e:
        print(f"[FAIL] WebSocket connectivity: {e}")
        all_pass = False

    sys.exit(0 if all_pass else 2)

# ── cmd: discover ────────────────────────────────────────────────────────────

async def cmd_discover(verbose: bool = False, debug: bool = False, port_override: int | None = None):
    if port_override:
        print(port_override)
        return

    print("Scanning for Insta360 Link Controller WebSocket server...", file=sys.stderr)

    # lsof first
    port = _lsof_port()
    if port:
        if verbose:
            print(f"  Found via lsof: port {port}", file=sys.stderr)
    else:
        port = await discover_port(verbose=verbose, debug=debug)

    if port:
        state = load_state()
        state['port'] = port
        state['timestamp'] = time.time()
        save_state(state)
        print(port)
        sys.exit(0)
    else:
        print("✗ No WebSocket server found.", file=sys.stderr)
        sys.exit(2)

# ── cmd: status ──────────────────────────────────────────────────────────────

async def cmd_status(port: int, debug: bool = False):
    client, err = await _connect_with_token_cycling(port, debug=debug)
    if client is None:
        print(f"✗ {err}", file=sys.stderr)
        sys.exit(3)

    state = load_state()
    output = {
        'port':            port,
        'deviceSerialNum': client.serial,
        'deviceInfo':      client.device_info,
        'cachedState':     state,
    }
    print(json.dumps(output, indent=2))
    await client.close()

# ── Linux PTZ fallback ───────────────────────────────────────────────────────

def _v4l2(*args):
    device = os.environ.get('LINK_CTL_V4L2_DEVICE', '/dev/video0')
    try:
        subprocess.run(['v4l2-ctl', '--version'], capture_output=True, check=True)
    except (FileNotFoundError, subprocess.CalledProcessError):
        print("v4l2-ctl not found. Install v4l-utils: sudo apt install v4l-utils", file=sys.stderr)
        sys.exit(1)
    subprocess.run(['v4l2-ctl', '-d', device] + list(args), check=True)

def linux_ptz_dispatch(args):
    cmd = args.command
    if cmd == 'zoom':
        v = args.value
        if not (100 <= v <= 400):
            print("zoom value must be 100..400", file=sys.stderr); sys.exit(1)
        _v4l2('--set-ctrl', f'zoom_absolute={v}')
    elif cmd == 'pan':
        _v4l2('--set-ctrl', f'pan_absolute={args.value}')
    elif cmd == 'tilt':
        _v4l2('--set-ctrl', f'tilt_absolute={args.value}')
    elif cmd == 'pan-rel':
        _v4l2('--set-ctrl', f'pan_relative={args.steps}')
    elif cmd == 'tilt-rel':
        _v4l2('--set-ctrl', f'tilt_relative={args.steps}')
    elif cmd == 'center':
        _v4l2('--set-ctrl', 'pan_absolute=0')
        _v4l2('--set-ctrl', 'tilt_absolute=0')
        _v4l2('--set-ctrl', 'zoom_absolute=100')
    elif cmd == 'zoom-rel':
        state = load_state()
        cur = state.get('zoom', 100)
        new_zoom = max(100, min(400, cur + args.delta))
        _v4l2('--set-ctrl', f'zoom_absolute={new_zoom}')
        state['zoom'] = new_zoom
        save_state(state)

# ── Command dispatch ─────────────────────────────────────────────────────────

async def dispatch(args, port: int, debug: bool):
    cmd     = args.command
    state   = load_state()

    # ── status (handled separately) ──────────────────────────────────────────
    if cmd == 'status':
        await cmd_status(port, debug=debug)
        return

    # ── Build payload(s) ─────────────────────────────────────────────────────
    # Connect + handshake, cycling through every candidate token. We rebuild
    # serial-dependent payloads after the handshake returns the live serial.
    client, err = await _connect_with_token_cycling(port, debug=debug)
    if client is None:
        _warn(f"✗ {err}")
        invalidate_port_cache()
        sys.exit(3)

    serial = client.serial
    # Extract current device state for verbose reporting
    devs = (client.device_info or {}).get('devices', [])
    dev  = devs[0] if devs else {}
    # Update zoom cache from live device info
    if dev.get('zoom'):
        state['zoom'] = dev['zoom'].get('curValue', state.get('zoom', 100))

    payloads = []

    # ── Absolute pan/tilt (not supported in v2.2.1 velocity-only API) ─────────
    if cmd == 'pan':
        _warn("✗ Absolute pan is not supported in Link Controller v2.2.1 (velocity-only API).")
        _warn("  Use pan-rel <steps> (-30..30) for relative movement.")
        await client.close(); sys.exit(4)

    elif cmd == 'tilt':
        _warn("✗ Absolute tilt is not supported in Link Controller v2.2.1 (velocity-only API).")
        _warn("  Use tilt-rel <steps> (-30..30) for relative movement.")
        await client.close(); sys.exit(4)

    # ── Velocity-based pan-rel / tilt-rel (joystick pulse) ────────────────────
    elif cmd == 'pan-rel':
        steps = args.steps
        sign = '+' if steps >= 0 else ''
        _info(f"pan-rel: {sign}{steps} steps")
        if steps != 0:
            vel = 1.0 if steps > 0 else -1.0
            duration = abs(steps) * 0.08  # 80 ms per step
            await client._send(build_joystick(serial, vel, 0.0))
            await asyncio.sleep(duration)
            await client._send(build_joystick_stop(serial))
        await client.close()
        save_state(state)
        return

    elif cmd == 'tilt-rel':
        steps = args.steps
        sign = '+' if steps >= 0 else ''
        _info(f"tilt-rel: {sign}{steps} steps")
        if steps != 0:
            vel = 1.0 if steps > 0 else -1.0
            duration = abs(steps) * 0.08
            await client._send(build_joystick(serial, 0.0, vel))
            await asyncio.sleep(duration)
            await client._send(build_joystick_stop(serial))
        await client.close()
        save_state(state)
        return

    # ── Zoom ──────────────────────────────────────────────────────────────────
    elif cmd == 'zoom':
        v = args.value
        if not (100 <= v <= 400):
            _warn("✗ zoom value must be 100..400")
            await client.close(); sys.exit(1)
        cur = dev.get('zoom', {}).get('curValue', state.get('zoom', '?'))
        _info(f"zoom: {cur} → {v}")
        payloads.append(build_zoom(serial, v))
        state['zoom'] = v

    elif cmd == 'zoom-rel':
        cur_zoom = state.get('zoom', 100)
        new_zoom = max(100, min(400, cur_zoom + args.delta))
        _info(f"zoom: {cur_zoom} → {new_zoom}")
        payloads.append(build_zoom(serial, new_zoom))
        state['zoom'] = new_zoom

    # ── Center (paramType=3 resets pan/tilt; also reset zoom) ─────────────────
    elif cmd == 'center':
        _info("center: resetting pan, tilt, and zoom")
        payloads.append(build_value_change(serial, ParamTypeV2.NORMAL_RESET))
        payloads.append(build_zoom(serial, 100))
        state['pan'] = 0; state['tilt'] = 0; state['zoom'] = 100

    # `privacy` used to live here as a velocity-based tilt-down pulse. It was
    # removed because on the original Link it could never be real privacy —
    # the LED stays on, the sensor keeps streaming, other apps still see the
    # feed. Pointing the camera at the ceiling gives a false sense of
    # security. Users who actually want the camera off should use a physical
    # lens cover, or the (sudo-requiring) USB suspend path when we ship it.

    # ── AI modes (all use paramType=5 with AIMode enum value) ─────────────────
    elif cmd == 'track':
        target = args.state
        if target in (None, 'toggle'):
            target = 'off' if (dev.get('mode') == VideoMode.TRACKING) else 'on'
        cur = 'on' if dev.get('mode') == VideoMode.TRACKING else 'off'
        _info(f"track: {cur} → {target}")
        payloads.append(build_value_change(serial, ParamTypeV2.AI_MODE,
                                           AIMode.TRACKING if target == 'on' else AIMode.NORMAL))

    elif cmd == 'deskview':
        target = args.state
        if target in (None, 'toggle'):
            target = 'off' if (dev.get('mode') == VideoMode.DESKVIEW) else 'on'
        cur = 'on' if dev.get('mode') == VideoMode.DESKVIEW else 'off'
        _info(f"deskview: {cur} → {target}")
        payloads.append(build_value_change(serial, ParamTypeV2.AI_MODE,
                                           AIMode.DESKVIEW if target == 'on' else AIMode.NORMAL))

    elif cmd == 'whiteboard':
        target = args.state
        if target in (None, 'toggle'):
            target = 'off' if (dev.get('mode') == VideoMode.WHITEBOARD) else 'on'
        cur = 'on' if dev.get('mode') == VideoMode.WHITEBOARD else 'off'
        _info(f"whiteboard: {cur} → {target}")
        payloads.append(build_value_change(serial, ParamTypeV2.AI_MODE,
                                           AIMode.WHITEBOARD if target == 'on' else AIMode.NORMAL))

    elif cmd == 'overhead':
        target = args.state
        if target in (None, 'toggle'):
            target = 'off' if (dev.get('mode') == VideoMode.OVERHEAD) else 'on'
        cur = 'on' if dev.get('mode') == VideoMode.OVERHEAD else 'off'
        _info(f"overhead: {cur} → {target}")
        payloads.append(build_value_change(serial, ParamTypeV2.AI_MODE,
                                           AIMode.OVERHEAD if target == 'on' else AIMode.NORMAL))

    elif cmd == 'normal':
        _info("mode: → normal")
        payloads.append(build_value_change(serial, ParamTypeV2.AI_MODE, AIMode.NORMAL))

    # ── Preset recall / save (PresetUpdateRequest: field6+field15) ───────────
    elif cmd == 'preset':
        idx = args.index
        if not (0 <= idx <= 19):
            _warn("✗ preset index must be 0-19")
            await client.close(); sys.exit(1)
        _info(f"preset {idx}: recall")
        payloads.append(build_preset_recall(serial, idx))

    elif cmd == 'preset-save':
        idx = args.index
        if not (0 <= idx <= 19):
            _warn("✗ preset index must be 0-19")
            await client.close(); sys.exit(1)
        _info(f"preset {idx}: save")
        payloads.append(build_preset_save(serial, idx))

    elif cmd == 'preset-add':
        # Alias for preset-save (both are type=0 ADD); kept for explicitness.
        idx = args.index
        if not (0 <= idx <= 19):
            _warn("✗ preset index must be 0-19")
            await client.close(); sys.exit(1)
        _info(f"preset {idx}: add")
        payloads.append(build_preset_save(serial, idx))

    elif cmd == 'preset-update':
        idx = args.index
        if not (0 <= idx <= 19):
            _warn("✗ preset index must be 0-19")
            await client.close(); sys.exit(1)
        _info(f"preset {idx}: update")
        payloads.append(build_preset_update(serial, idx))

    elif cmd == 'preset-delete':
        idx = args.index
        if not (0 <= idx <= 19):
            _warn("✗ preset index must be 0-19")
            await client.close(); sys.exit(1)
        _info(f"preset {idx}: delete")
        payloads.append(build_preset_delete(serial, idx))

    elif cmd == 'preset-rename':
        idx = args.index
        if not (0 <= idx <= 19):
            _warn("✗ preset index must be 0-19")
            await client.close(); sys.exit(1)
        name = args.name
        if not name:
            _warn("✗ preset name cannot be empty")
            await client.close(); sys.exit(1)
        _info(f"preset {idx}: rename → {name!r}")
        payloads.append(build_preset_rename(serial, idx, name))

    # ── Image / camera settings ───────────────────────────────────────────────
    elif cmd == 'hdr':
        target = args.state
        if target in (None, 'toggle'):
            target = 'off' if dev.get('hdr') else 'on'
        cur = 'on' if dev.get('hdr') else 'off'
        _info(f"hdr: {cur} → {target}")
        payloads.append(build_value_change(serial, ParamTypeV2.HDR,
                                           "1" if target == 'on' else "0"))

    elif cmd == 'autofocus':
        # Note: autofocus state is not included in DeviceInfo, so smart toggle is not possible.
        # on/off are required arguments.
        _info(f"autofocus: → {args.state}")
        payloads.append(build_value_change(serial, ParamTypeV2.AUTOFOCUS,
                                           "1" if args.state == 'on' else "0"))

    elif cmd == 'autoexposure':
        target = args.state
        if target in (None, 'toggle'):
            target = 'off' if dev.get('autoExposure') else 'on'
        cur = 'on' if dev.get('autoExposure') else 'off'
        _info(f"autoexposure: {cur} → {target}")
        payloads.append(build_value_change(serial, ParamTypeV2.AUTO_EXPOSURE,
                                           "1" if target == 'on' else "0"))

    elif cmd == 'awb':
        target = args.state
        if target in (None, 'toggle'):
            target = 'off' if dev.get('autoWhiteBalance') else 'on'
        cur = 'on' if dev.get('autoWhiteBalance') else 'off'
        _info(f"awb: {cur} → {target}")
        payloads.append(build_value_change(serial, ParamTypeV2.AUTO_WB,
                                           "1" if target == 'on' else "0"))

    elif cmd == 'mirror':
        target = args.state
        if target in (None, 'toggle'):
            # Note: DeviceInfo field 5 (mirror) may not reliably reflect flip state.
            # Toggle falls back to 'on' when unknown.
            target = 'off' if dev.get('mirror') else 'on'
        cur = 'on' if dev.get('mirror') else 'off'
        _info(f"mirror: {cur} → {target}")
        payloads.append(build_value_change(serial, ParamTypeV2.HORIZONTAL_FLIP,
                                           "1" if target == 'on' else "0"))

    elif cmd == 'smartcomposition':
        target = args.state
        if target in (None, 'toggle'):
            target = 'off' if dev.get('smartComposition') else 'on'
        cur = 'on' if dev.get('smartComposition') else 'off'
        _info(f"smartcomposition: {cur} → {target}")
        payloads.append(build_value_change(serial, ParamTypeV2.SMART_COMPOSITION,
                                           "1" if target == 'on' else "0"))

    elif cmd == 'smartcomp-frame':
        val = {'head': '1', 'halfbody': '2', 'wholebody': '3'}.get(args.frame.lower())
        if val is None:
            _warn("✗ smartcomp-frame must be: head, halfbody, wholebody")
            await client.close(); sys.exit(1)
        _info(f"smartcomp-frame: → {args.frame}")
        payloads.append(build_value_change(serial, ParamTypeV2.SMART_COMP_FRAME, val))

    elif cmd == 'exposurecomp':
        v = args.value
        if not (0 <= v <= 100):
            _warn("✗ exposurecomp value must be 0..100 (50 = 0 EV)")
            await client.close(); sys.exit(1)
        _info(f"exposurecomp: {dev.get('exposureComp', '?')} → {v}")
        payloads.append(build_value_change(serial, ParamTypeV2.EXPOSURE_COMP, str(v)))

    elif cmd == 'brightness':
        v = args.value
        if not (0 <= v <= 100):
            _warn("✗ brightness value must be 0..100")
            await client.close(); sys.exit(1)
        _info(f"brightness: {dev.get('brightness', '?')} → {v}")
        payloads.append(build_value_change(serial, ParamTypeV2.BRIGHTNESS, str(v)))

    elif cmd == 'contrast':
        v = args.value
        if not (0 <= v <= 100):
            _warn("✗ contrast value must be 0..100")
            await client.close(); sys.exit(1)
        _info(f"contrast: {dev.get('contrast', '?')} → {v}")
        payloads.append(build_value_change(serial, ParamTypeV2.CONTRAST, str(v)))

    elif cmd == 'saturation':
        v = args.value
        if not (0 <= v <= 100):
            _warn("✗ saturation value must be 0..100 (0=B&W, 50=default)")
            await client.close(); sys.exit(1)
        _info(f"saturation: {dev.get('saturation', '?')} → {v}")
        payloads.append(build_value_change(serial, ParamTypeV2.SATURATION, str(v)))

    elif cmd == 'sharpness':
        v = args.value
        if not (0 <= v <= 100):
            _warn("✗ sharpness value must be 0..100")
            await client.close(); sys.exit(1)
        _info(f"sharpness: {dev.get('sharpness', '?')} → {v}")
        payloads.append(build_value_change(serial, ParamTypeV2.SHARPNESS, str(v)))

    elif cmd == 'wb-temp':
        v = args.value
        if not (2800 <= v <= 10000):
            _warn("✗ wb-temp value must be 2800..10000 (Kelvin); AWB must be off")
            await client.close(); sys.exit(1)
        _info(f"wb-temp: {dev.get('wbTemp', '?')} → {v} K")
        payloads.append(build_value_change(serial, ParamTypeV2.WB_TEMP, str(v)))

    elif cmd == 'gesture-zoom':
        target = args.state
        if target in (None, 'toggle'):
            # DeviceInfo has no gesture-zoom readback; default to 'on' when unknown
            target = 'on'
        _info(f"gesture-zoom: → {target}")
        payloads.append(build_value_change(serial, ParamTypeV2.GESTURE_ZOOM,
                                           "1" if target == 'on' else "0"))

    elif cmd == 'anti-flicker':
        mode = args.mode.lower()
        val = {'auto': '0', '50hz': '1', '60hz': '2'}.get(mode)
        if val is None:
            _warn("✗ anti-flicker mode must be: auto, 50hz, 60hz")
            await client.close(); sys.exit(1)
        _info(f"anti-flicker: → {mode}")
        payloads.append(build_value_change(serial, ParamTypeV2.ANTI_FLICKER, val))

    for payload in payloads:
        await client.send_command(payload, wait_ms=80)

    await client.close()
    save_state(state)

# ── CLI ──────────────────────────────────────────────────────────────────────

# ── Interactive joystick (curses) ────────────────────────────────────────────

def interactive_joystick():
    """Full-screen curses joystick. Arrow keys pan/tilt, +/- zoom, digit keys
    recall presets, `s` saves a preset, `n` renames, `d` deletes, `q` quits.

    Uses the same USB direct PTZ + preset storage as the `preset-*` commands
    so saves made here show up in `preset list` and vice versa.
    """
    import curses

    if not _uvc_probe_available():
        _warn("✗ interactive joystick requires tools/uvc-probe (macOS).")
        _warn("  Compile it: clang -o tools/uvc-probe tools/uvc-probe.m "
              "-framework IOKit -framework CoreFoundation "
              "-framework Foundation -ObjC")
        sys.exit(1)

    # Step sizes — tuned from empirical range of the Link's XU pan/tilt
    # readback (roughly ±150000 each axis). Shift+arrow uses FAST_MULT to
    # compensate for terminals that don't auto-repeat modifier+arrow combos
    # (macOS Terminal.app doesn't; iTerm2 does) — one fast press covers
    # about 20% of the full range, which is visible and hard to miss.
    PAN_STEP  = 3000
    TILT_STEP = 3000
    FAST_MULT = 5
    ZOOM_STEP = 10

    def draw(scr, pan, tilt, zoom, presets, status, last_dir, fast_mode):
        scr.erase()
        h, w = scr.getmaxyx()
        title = 'link-ctl joystick  —  USB-direct, no app, no token'
        scr.addstr(0, max(0, (w - len(title)) // 2), title, curses.A_BOLD)
        mode = '  [FAST]' if fast_mode else ''
        scr.addstr(2, 2, f'pan  = {pan:>+8d}    tilt = {tilt:>+8d}    zoom = {zoom:>4d}{mode}',
                   curses.A_BOLD if fast_mode else curses.A_NORMAL)

        # Crosshair box with directional arrows on all 4 sides. The arrow
        # matching the most-recent keypress is rendered in reverse video so
        # the user sees which way the camera just moved.
        box_top, box_left, box_h, box_w = 6, 6, 9, 25
        box_cy = box_top + (box_h - 2) // 2
        box_cx = box_left + box_w // 2

        def arrow(y, x, glyph, d):
            attr = curses.A_REVERSE | curses.A_BOLD if last_dir == d else curses.A_DIM
            scr.addstr(y, x, glyph, attr)

        arrow(box_top - 2,            box_cx, '▲', 'up')
        arrow(box_top + box_h - 1,    box_cx, '▼', 'down')
        arrow(box_cy,                 box_left - 3, '◀', 'left')
        arrow(box_cy,                 box_left + box_w + 1, '▶', 'right')

        scr.addstr(box_top - 1, box_left, '┌' + '─' * (box_w - 2) + '┐')
        for i in range(box_h - 2):
            scr.addstr(box_top + i, box_left, '│' + ' ' * (box_w - 2) + '│')
        scr.addstr(box_top + box_h - 2, box_left, '└' + '─' * (box_w - 2) + '┘')
        # position indicator — map pan/tilt into the box (bounds ~±150000)
        PX_BOUND = 150000
        cx = box_left + 1 + int((box_w - 3) * (pan + PX_BOUND) / (2 * PX_BOUND))
        cy = box_top + int((box_h - 3) * (PX_BOUND - tilt) / (2 * PX_BOUND))
        cx = max(box_left + 1, min(box_left + box_w - 2, cx))
        cy = max(box_top, min(box_top + box_h - 3, cy))
        scr.addstr(cy, cx, '✚')

        # preset table
        scr.addstr(box_top + box_h + 1, 2, 'presets:', curses.A_UNDERLINE)
        for i, p in enumerate(presets[:10]):
            line = f"  {p['id']}  {p['name']:<16s}  pan={p['pan']:>+7d} tilt={p['tilt']:>+7d} zoom={p['zoom']}"
            if box_top + box_h + 2 + i < h - 3:
                scr.addstr(box_top + box_h + 2 + i, 2, line[: w - 4])

        # keybinds
        binds = [
            'arrows  pan/tilt',
            'f  toggle fast (5×)',
            '+/-  zoom',
            '0-9  recall preset',
            's  save preset',
            'n  rename',
            'd  delete',
            'c  center',
            'q  quit',
        ]
        for i, b in enumerate(binds):
            if h - 2 - len(binds) + i > 0:
                scr.addstr(h - 2 - len(binds) + i, max(2, w - 30), b)

        if status:
            scr.addstr(h - 1, 2, status[: w - 4], curses.A_REVERSE)
        scr.refresh()

    def prompt(scr, prompt_str, default=''):
        curses.echo()
        h, w = scr.getmaxyx()
        scr.addstr(h - 1, 0, ' ' * (w - 1))
        scr.addstr(h - 1, 0, prompt_str + ' ')
        scr.refresh()
        try:
            ans = scr.getstr(h - 1, len(prompt_str) + 1, w - len(prompt_str) - 2).decode()
        except Exception:
            ans = ''
        curses.noecho()
        return ans.strip() or default

    def run(scr):
        curses.curs_set(0)
        scr.keypad(True)

        try:
            pan, tilt = read_pantilt()
            zoom = read_zoom()
        except Exception as e:
            scr.addstr(0, 0, f'error reading camera: {e}')
            scr.getch()
            return
        status = 'ready'
        last_dir = None        # which arrow glyph to highlight
        last_key_time = 0.0    # wall-clock of last arrow keystroke
        HIGHLIGHT_MS = 150     # decay window — key-repeat is ~30-50ms on macOS
        fast_mode = False      # `f` toggles; terminals that don't distinguish
                               # shift+arrow from plain arrow (macOS Terminal.app)
                               # still get fast-mode via this toggle.

        # Non-blocking getch: returns -1 if no key within `timeout` ms, so we
        # can decay the highlight shortly after the user releases the key.
        scr.timeout(30)

        last_raw_seq = ['']   # debug: last raw escape sequence seen
        def drain_escape_sequence():
            """Drain remaining bytes of a CSI sequence whose leading ESC we
            just peeked. Returns ('up'|'down'|'left'|'right', fast_flag) if
            recognised, else (None, False). Shift+arrow on most terminals is
            ESC [ 1 ; 2 A/B/C/D; plain arrow when keypad() doesn't intercept
            is ESC [ A/B/C/D."""
            buf = []
            scr.timeout(8)
            for _ in range(8):
                k = scr.getch()
                if k == -1: break
                buf.append(k)
            scr.timeout(30)
            s = bytes(b & 0xff for b in buf).decode('latin-1', errors='replace')
            last_raw_seq[0] = 'ESC' + repr(s)
            m = re.match(r'\[(\d+)?(?:;(\d+))?([ABCD])', s)
            if not m: return (None, False)
            mod = int(m.group(2) or 1)   # 1=none, 2=shift, 3=alt, 4=shift+alt, 5=ctrl…
            d = {'A': 'up', 'B': 'down', 'C': 'right', 'D': 'left'}[m.group(3)]
            return (d, mod == 2)

        while True:
            now = time.time()
            if last_dir and (now - last_key_time) * 1000 > HIGHLIGHT_MS:
                last_dir = None
            presets = usb_preset_list()
            draw(scr, pan, tilt, zoom, presets, status, last_dir, fast_mode)
            c = scr.getch()
            if c == -1:
                continue       # no key this tick — loop and let highlight decay
            dp = dt = dz = 0
            shift = False
            mult = FAST_MULT if fast_mode else 1
            shift = fast_mode
            # ESC-prefixed escape sequences. On terminals that report
            # modifiers (iTerm2), shift+arrow arrives here and we use its
            # modifier flag directly; on plain Terminal.app it's just ESC[A
            # and we fall back to fast_mode state.
            if c == 27:
                d, term_shift = drain_escape_sequence()
                if d is None:
                    continue
                if term_shift:
                    mult = FAST_MULT; shift = True
                if d == 'left':    dp = -PAN_STEP * mult
                elif d == 'right': dp = +PAN_STEP * mult
                elif d == 'up':    dt = +TILT_STEP * mult
                elif d == 'down':  dt = -TILT_STEP * mult
                last_dir = d; last_key_time = now
            elif c == curses.KEY_LEFT:   dp = -PAN_STEP * mult;  last_dir = 'left';  last_key_time = now
            elif c == curses.KEY_RIGHT: dp = +PAN_STEP * mult; last_dir = 'right'; last_key_time = now
            elif c == curses.KEY_UP:    dt = +TILT_STEP * mult; last_dir = 'up';   last_key_time = now
            elif c == curses.KEY_DOWN:  dt = -TILT_STEP * mult; last_dir = 'down'; last_key_time = now
            # curses KEY_S* constants (shift-arrow) for terminals that map them
            elif c == curses.KEY_SLEFT:  dp = -PAN_STEP * FAST_MULT; shift = True; last_dir = 'left';  last_key_time = now
            elif c == curses.KEY_SRIGHT: dp = +PAN_STEP * FAST_MULT; shift = True; last_dir = 'right'; last_key_time = now
            elif c == ord('f'):
                fast_mode = not fast_mode
                status = f"fast mode: {'ON (5×)' if fast_mode else 'off'}"
                continue
            elif c in (ord('+'), ord('=')): dz = +ZOOM_STEP
            elif c in (ord('-'), ord('_')): dz = -ZOOM_STEP
            elif c == ord('c'):
                pan, tilt, zoom = 0, 0, 100
                try:
                    write_pantilt(pan, tilt); write_zoom(zoom)
                    status = 'centered'
                except Exception as e: status = f'center failed: {e}'
                continue
            elif c == ord('q'):
                return
            elif c == ord('s'):
                idx = prompt(scr, 'save to slot (0-9):')
                if idx.isdigit() and 0 <= int(idx) <= 9:
                    name = prompt(scr, f'name for slot {idx}:', f'preset_{idx}')
                    try:
                        e = usb_preset_save(int(idx), name)
                        status = f"saved slot {idx} '{e['name']}'"
                    except Exception as err: status = f'save failed: {err}'
                continue
            elif c == ord('n'):
                idx = prompt(scr, 'rename slot (0-9):')
                if idx.isdigit() and 0 <= int(idx) <= 9:
                    name = prompt(scr, 'new name:')
                    try:
                        usb_preset_rename(int(idx), name)
                        status = f'renamed slot {idx} to {name!r}'
                    except KeyError: status = f'no preset at slot {idx}'
                continue
            elif c == ord('d'):
                idx = prompt(scr, 'delete slot (0-9):')
                if idx.isdigit() and 0 <= int(idx) <= 9:
                    ok = usb_preset_delete(int(idx))
                    status = f'deleted slot {idx}' if ok else f'no preset at {idx}'
                continue
            elif ord('0') <= c <= ord('9'):
                idx = c - ord('0')
                try:
                    e = usb_preset_recall(idx)
                    pan, tilt, zoom = e['pan'], e['tilt'], e['zoom']
                    status = f"recalled slot {idx} '{e['name']}'"
                except KeyError: status = f'no preset at slot {idx}'
                except Exception as err: status = f'recall failed: {err}'
                continue
            else:
                continue

            if dp or dt:
                pan += dp; tilt += dt
                try:
                    write_pantilt(pan, tilt)
                    step = f'{FAST_MULT}×' if shift else '1×'
                    seq = f'  raw={last_raw_seq[0]}' if last_raw_seq[0] else ''
                    status = f"[{step}] move → pan={pan} tilt={tilt}{seq}"
                    last_raw_seq[0] = ''
                except Exception as e: status = f'move failed: {e}'
            if dz:
                new_zoom = max(100, min(400, zoom + dz))
                if new_zoom != zoom:
                    zoom = new_zoom
                    try:
                        write_zoom(zoom)
                        status = f'zoom → {zoom}'
                    except Exception as e: status = f'zoom failed: {e}'

    curses.wrapper(run)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog='link-ctl',
        description='Control Insta360 Link webcam via Link Controller app',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""commands:
  ── PTZ ────────────────────────────────────────────────────────────
  pan-rel  <steps>        relative pan   (-30..30 steps)
  tilt-rel <steps>        relative tilt  (-30..30 steps)
  zoom     <100-400>      absolute zoom
  zoom-rel <delta>        relative zoom  (e.g. +50 or -50)
  center                  reset pan, tilt, and zoom to defaults

  ── AI modes  (USB-direct on macOS; WS elsewhere) ──────────────────
  track        [on|off|toggle]   subject tracking
  deskview     [on|off|toggle]   desk surface view
  whiteboard   [on|off|toggle]   whiteboard mode
  overhead     [on|off|toggle]   overhead / top-down view
  normal                         return to standard mode

  ── Image settings  (USB-direct on macOS; WS elsewhere) ────────────
  hdr              [on|off|toggle]   HDR
  autofocus         on|off           auto focus  (explicit arg required)
  autoexposure     [on|off|toggle]   auto exposure
  awb              [on|off|toggle]   auto white balance
  mirror           [on|off|toggle]   horizontal flip
  gesture-zoom     [on|off|toggle]   gesture control zoom
  anti-flicker     auto|50hz|60hz    anti-flicker mode
  smartcomposition [on|off|toggle]   smart composition
  smartcomp-frame  head|halfbody|wholebody   smart composition framing mode
  brightness  <0-100>    brightness  (default 50)
  contrast    <0-100>    contrast    (default 50)
  saturation  <0-100>    saturation  (default 50; 0 = greyscale)
  sharpness   <0-100>    sharpness   (default 50)
  exposurecomp <0-100>   exposure compensation (50 = 0 EV)
  wb-temp  <2800-10000>  white balance temperature in Kelvin (AWB must be off)

  ── Presets (USB-direct on macOS; WebSocket elsewhere) ─────────────
  preset          <0-19>        recall saved preset
  preset-save     <0-19> [name] save current pan/tilt/zoom as preset
  preset-recall   <0-19>        alias for preset
  preset-add      <0-19>        WS path only — alias for preset-save (ADD)
  preset-update   <0-19>        WS path only — overwrite existing (UPDATE)
  preset-rename   <0-19> <name> rename a preset
  preset-delete   <0-19>        delete a preset
  preset-list                   list saved presets (USB path only)

  ── Interactive (USB-direct, macOS only) ──────────────────────────
  joystick                      curses-based PTZ + preset UI

  ── Diagnostics ─────────────────────────────────────────────────────
  status                 show device info as JSON
  discover [--verbose]   find WebSocket port and cache it
  preflight              run all checks (process, USB, port, handshake)

toggle commands: omit the argument to smart-toggle based on current state
""",
    )
    p.add_argument('-d', '--debug',   action='store_true', help='Hex-dump WebSocket frames (stderr)')
    p.add_argument('-v', '--verbose', action='store_true', help='Show current and new values on change (default)')
    p.add_argument('-q', '--quiet',   action='store_true', help='Suppress informational output; show errors only')
    p.add_argument('-s', '--silent',  action='store_true', help='Suppress all output including errors')
    p.add_argument('--port',     type=int,                  help='Override WebSocket port discovery')
    p.add_argument('--skip-preflight', action='store_true', help='Skip preflight checks')

    sub = p.add_subparsers(dest='command', required=True)

    # PTZ
    s = sub.add_parser('pan');       s.add_argument('value', type=int)
    s = sub.add_parser('tilt');      s.add_argument('value', type=int)
    s = sub.add_parser('pan-rel');   s.add_argument('steps', type=int)
    s = sub.add_parser('tilt-rel');  s.add_argument('steps', type=int)
    s = sub.add_parser('zoom');      s.add_argument('value', type=int)
    s = sub.add_parser('zoom-rel');  s.add_argument('delta', type=int)
    sub.add_parser('center')


    # AI modes
    s = sub.add_parser('track');     s.add_argument('state', nargs='?', choices=['on', 'off', 'toggle'])
    s = sub.add_parser('deskview');  s.add_argument('state', nargs='?', choices=['on', 'off', 'toggle'])
    s = sub.add_parser('whiteboard'); s.add_argument('state', nargs='?', choices=['on', 'off', 'toggle'])
    s = sub.add_parser('overhead');  s.add_argument('state', nargs='?', choices=['on', 'off', 'toggle'])
    sub.add_parser('normal')

    # Presets. When tools/uvc-probe is available (macOS), these go through the
    # USB-direct path (host-side JSON storage + standard UVC PTZ). Otherwise
    # they fall back to the WebSocket PresetUpdateRequest path.
    s = sub.add_parser('preset');         s.add_argument('index', type=int)
    s = sub.add_parser('preset-save')
    s.add_argument('index', type=int)
    s.add_argument('name', type=str, nargs='?', default=None,
                   help='optional name (USB path only; WS path ignores)')
    s = sub.add_parser('preset-recall');  s.add_argument('index', type=int)
    s = sub.add_parser('preset-add');     s.add_argument('index', type=int)
    s = sub.add_parser('preset-update');  s.add_argument('index', type=int)
    s = sub.add_parser('preset-delete');  s.add_argument('index', type=int)
    s = sub.add_parser('preset-rename')
    s.add_argument('index', type=int)
    s.add_argument('name', type=str)
    s = sub.add_parser('preset-list',
                       help='list saved presets (USB path only)')
    s.add_argument('--json', action='store_true')

    # Interactive curses joystick (USB-direct; macOS only)
    sub.add_parser('joystick', help='interactive curses PTZ + preset UI')

    # Image settings
    s = sub.add_parser('hdr');              s.add_argument('state', nargs='?', choices=['on', 'off', 'toggle'])
    s = sub.add_parser('autofocus');        s.add_argument('state', choices=['on', 'off'])  # no smart toggle: state not readable
    s = sub.add_parser('autoexposure');     s.add_argument('state', nargs='?', choices=['on', 'off', 'toggle'])
    s = sub.add_parser('awb');              s.add_argument('state', nargs='?', choices=['on', 'off', 'toggle'])
    s = sub.add_parser('mirror');           s.add_argument('state', nargs='?', choices=['on', 'off', 'toggle'])
    s = sub.add_parser('smartcomposition'); s.add_argument('state', nargs='?', choices=['on', 'off', 'toggle'])
    s = sub.add_parser('smartcomp-frame');  s.add_argument('frame', choices=['head', 'halfbody', 'wholebody'])
    s = sub.add_parser('brightness');    s.add_argument('value', type=int)
    s = sub.add_parser('contrast');      s.add_argument('value', type=int)
    s = sub.add_parser('saturation');    s.add_argument('value', type=int)
    s = sub.add_parser('sharpness');     s.add_argument('value', type=int)
    s = sub.add_parser('exposurecomp');  s.add_argument('value', type=int, help='Exposure compensation 0..100 (50 = 0 EV)')
    s = sub.add_parser('wb-temp');       s.add_argument('value', type=int, help='White balance temperature 2800..10000 K (AWB must be off)')
    s = sub.add_parser('gesture-zoom');  s.add_argument('state', nargs='?', choices=['on', 'off', 'toggle'])
    s = sub.add_parser('anti-flicker');  s.add_argument('mode', choices=['auto', '50hz', '60hz'])

    # Diagnostics
    sub.add_parser('status')
    s = sub.add_parser('discover');  s.add_argument('--verbose', action='store_true')
    sub.add_parser('preflight')

    return p

def main():
    global _VERBOSITY
    parser = build_parser()
    args   = parser.parse_args()
    debug  = args.debug
    system = platform.system()
    cmd    = args.command

    # Set verbosity (silent < quiet < verbose < debug)
    if args.silent:
        _VERBOSITY = 0
    elif args.quiet:
        _VERBOSITY = 1
    elif args.debug:
        _VERBOSITY = 3
    # --verbose is explicit but same as default (2); no change needed

    # ── Diagnostics that don't need a full connection ─────────────────────────
    if cmd == 'discover':
        asyncio.run(cmd_discover(
            verbose=getattr(args, 'verbose', False),
            debug=debug,
            port_override=args.port,
        ))
        return

    if cmd == 'preflight':
        asyncio.run(cmd_preflight_check(debug=debug, port_override=args.port))
        return

    # ── USB-direct commands (no WebSocket, no Link Controller, no token) ──────
    if cmd == 'joystick':
        interactive_joystick()
        return

    # Full PTZ over USB when uvc-probe is available. On Linux we still prefer
    # v4l2-ctl (standard UVC ioctl); on macOS uvc-probe is the only option.
    USB_PTZ_CMDS = {'zoom', 'zoom-rel', 'pan', 'tilt', 'pan-rel', 'tilt-rel',
                    'center'}
    if cmd in USB_PTZ_CMDS and _uvc_probe_available():
        usb_ptz_dispatch(args)
        return

    # Image / AI controls over USB — fully frees the CLI from the desktop
    # app for every user-facing control. Firmware update, device rename,
    # and factory reset still require the desktop app (they aren't exposed
    # as XU selectors we can discover).
    # smartcomposition (on/off master) is intentionally NOT in the USB set —
    # its XU bit hasn't been confirmed, so the command still falls through
    # to the WebSocket path (paramType 11). smartcomp-frame (head/half/full)
    # IS USB-direct since that selector is documented.
    USB_IMG_CMDS = {'hdr', 'mirror', 'gesture-zoom',
                    'brightness', 'contrast', 'saturation', 'sharpness',
                    'wb-temp', 'exposurecomp', 'autoexposure', 'awb',
                    'anti-flicker', 'autofocus',
                    'track', 'deskview', 'whiteboard', 'overhead', 'normal',
                    'smartcomp-frame'}
    if cmd in USB_IMG_CMDS and _uvc_probe_available():
        usb_image_dispatch(args)
        return

    if cmd in {'preset', 'preset-save', 'preset-recall', 'preset-delete',
               'preset-rename', 'preset-list'} and _uvc_probe_available():
        try:
            if cmd == 'preset-list':
                lst = usb_preset_list()
                if args.json:
                    print(json.dumps(lst, indent=2))
                elif not lst:
                    _info('(no presets saved)')
                else:
                    for p in lst:
                        print(f"{p['id']:>2}  {p['name']:<16s}  "
                              f"pan={p['pan']:>+7d}  tilt={p['tilt']:>+7d}  zoom={p['zoom']}")
                return
            if cmd in {'preset', 'preset-recall'}:
                e = usb_preset_recall(args.index)
                _info(f"recalled slot {args.index} '{e['name']}' → "
                      f"pan={e['pan']} tilt={e['tilt']} zoom={e['zoom']}")
                return
            if cmd == 'preset-save':
                name = getattr(args, 'name', None)
                e = usb_preset_save(args.index, name)
                _info(f"saved slot {args.index} '{e['name']}' ← "
                      f"pan={e['pan']} tilt={e['tilt']} zoom={e['zoom']}")
                return
            if cmd == 'preset-delete':
                ok = usb_preset_delete(args.index)
                _info(f"deleted slot {args.index}" if ok else
                      f"(slot {args.index} was empty)")
                return
            if cmd == 'preset-rename':
                e = usb_preset_rename(args.index, args.name)
                _info(f"renamed slot {args.index} → {e['name']!r}")
                return
        except KeyError as e:
            _warn(f'✗ {e}')
            sys.exit(1)
        except Exception as e:
            _warn(f'✗ USB preset {cmd} failed: {e}')
            sys.exit(3)

    # ── Platform-specific AI mode restriction ─────────────────────────────────
    AI_CMDS = {'track', 'deskview', 'whiteboard', 'overhead', 'normal'}
    if system == 'Linux' and cmd in AI_CMDS:
        _warn("✗ AI modes require Insta360 Link Controller (macOS/Windows only).")
        sys.exit(4)

    # ── Linux PTZ via v4l2-ctl ────────────────────────────────────────────────
    PTZ_CMDS = {'pan', 'tilt', 'pan-rel', 'tilt-rel', 'zoom', 'zoom-rel', 'center'}
    if system == 'Linux' and cmd in PTZ_CMDS:
        linux_ptz_dispatch(args)
        return

    # ── Preflight (macOS/Windows) ─────────────────────────────────────────────
    if not args.skip_preflight:
        port = asyncio.run(preflight(debug=debug, port_override=args.port))
    else:
        port = args.port
        if not port:
            state = load_state()
            port  = state.get('port')
        if not port:
            _warn("✗ No port available. Use --port N or run without --skip-preflight.")
            sys.exit(2)

    # ── Run command ───────────────────────────────────────────────────────────
    rc = asyncio.run(dispatch(args, port, debug))
    if rc:
        sys.exit(rc)

if __name__ == '__main__':
    main()
