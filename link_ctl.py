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
    """PresetUpdateRequest type=1 (UPDATE): overwrite existing preset at index with current position.
    Confirmed wire format from capture: field1=op, field2=index, field4=serial.
    """
    inner = _int_f(1, 1) + _int_f(2, index) + _str_f(4, serial)
    return _bool_f(6, True) + _msg_f(15, inner)

def build_preset_delete(serial: str, index: int) -> bytes:
    """PresetUpdateRequest type=2 (DELETE): remove preset at index.
    Confirmed wire format from capture: field1=op, field2=index, field4=serial.
    """
    inner = _int_f(1, 2) + _int_f(2, index) + _str_f(4, serial)
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

def _read_token_from_ini() -> str | None:
    """Read the most-recently-used token from startup.ini (case-preserved)."""
    try:
        content = STARTUP_INI.read_text(errors='replace')
        m = re.search(r'\[Token\](.*?)(?:\[|$)', content, re.DOTALL)
        if not m:
            return None
        best_token, best_ts = None, 0
        for line in m.group(1).strip().splitlines():
            if '=' in line:
                k, v = line.split('=', 1)
                ts = int(v.strip()) if v.strip().isdigit() else 0
                if ts > best_ts:
                    best_ts, best_token = ts, k.strip()
        return best_token
    except Exception:
        return None

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

async def _run(port: int, payloads: list, debug: bool = False) -> int:
    """Connect, handshake, send payload(s), disconnect. Returns exit code."""
    client = LinkClient(port, debug=debug)
    try:
        await client.connect()
    except Exception as e:
        _warn(f"✗ Could not connect to WebSocket on port {port}: {e}")
        invalidate_port_cache()
        return 3

    ok, err = await client.handshake()
    if not ok:
        _warn(f"✗ {err}")
        await client.close()
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
    client = LinkClient(port, debug=debug)
    try:
        await client.connect()
    except Exception as e:
        print(f"✗ Connection failed: {e}", file=sys.stderr)
        sys.exit(3)

    ok, err = await client.handshake()
    if not ok:
        print(f"✗ {err}", file=sys.stderr)
        await client.close()
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
    elif cmd == 'privacy':
        target = args.state
        if target is None:
            state = load_state()
            target = 'off' if state.get('tilt', 0) == -277920 else 'on'
        if target == 'on':
            _v4l2('--set-ctrl', 'tilt_absolute=-277920')
        else:
            _v4l2('--set-ctrl', 'tilt_absolute=0')
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
    # We use a dummy serial here; will be replaced with the real one after
    # handshake. We connect first, then rebuild.

    client = LinkClient(port, debug=debug)
    try:
        await client.connect()
    except Exception as e:
        _warn(f"✗ Could not connect to WebSocket on port {port}: {e}")
        invalidate_port_cache()
        sys.exit(3)

    ok, err = await client.handshake()
    if not ok:
        _warn(f"✗ {err}")
        await client.close()
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

    # ── Privacy (velocity-based tilt-down pulse; off = NORMAL_RESET) ──────────
    elif cmd == 'privacy':
        target = args.state
        if target in (None, 'toggle'):
            target = 'off' if state.get('tilt', 0) == -277920 else 'on'
        _info(f"privacy: → {target}")
        if target == 'on':
            # Tilt full-speed down for 3.5 s — enough to reach bottom stop
            await client._send(build_joystick(serial, 0.0, -1.0))
            await asyncio.sleep(3.5)
            await client._send(build_joystick_stop(serial))
            state['tilt'] = -277920
            await client.close()
            save_state(state)
            return
        else:
            payloads.append(build_value_change(serial, ParamTypeV2.NORMAL_RESET))
            state['tilt'] = 0

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

    elif cmd == 'preset-delete':
        idx = args.index
        if not (0 <= idx <= 19):
            _warn("✗ preset index must be 0-19")
            await client.close(); sys.exit(1)
        _info(f"preset {idx}: delete")
        payloads.append(build_preset_delete(serial, idx))

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
  privacy  [on|off|toggle]  tilt lens straight down / restore

  ── AI modes  (macOS/Windows only) ─────────────────────────────────
  track        [on|off|toggle]   subject tracking
  deskview     [on|off|toggle]   desk surface view
  whiteboard   [on|off|toggle]   whiteboard mode
  overhead     [on|off|toggle]   overhead / top-down view
  normal                         return to standard mode

  ── Image settings  (macOS/Windows only) ────────────────────────────
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

  ── Presets ─────────────────────────────────────────────────────────
  preset        <0-19>   recall saved preset position
  preset-save   <0-19>   save current position as preset
  preset-delete <0-19>   delete a preset slot

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

    # Privacy
    s = sub.add_parser('privacy');   s.add_argument('state', nargs='?', choices=['on', 'off', 'toggle'])

    # AI modes
    s = sub.add_parser('track');     s.add_argument('state', nargs='?', choices=['on', 'off', 'toggle'])
    s = sub.add_parser('deskview');  s.add_argument('state', nargs='?', choices=['on', 'off', 'toggle'])
    s = sub.add_parser('whiteboard'); s.add_argument('state', nargs='?', choices=['on', 'off', 'toggle'])
    s = sub.add_parser('overhead');  s.add_argument('state', nargs='?', choices=['on', 'off', 'toggle'])
    sub.add_parser('normal')

    # Presets
    s = sub.add_parser('preset');        s.add_argument('index', type=int)
    s = sub.add_parser('preset-save');   s.add_argument('index', type=int)
    s = sub.add_parser('preset-delete'); s.add_argument('index', type=int)

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

    # ── Platform-specific AI mode restriction ─────────────────────────────────
    AI_CMDS = {'track', 'deskview', 'whiteboard', 'overhead', 'normal'}
    if system == 'Linux' and cmd in AI_CMDS:
        _warn("✗ AI modes require Insta360 Link Controller (macOS/Windows only).")
        sys.exit(4)

    # ── Linux PTZ via v4l2-ctl ────────────────────────────────────────────────
    PTZ_CMDS = {'pan', 'tilt', 'pan-rel', 'tilt-rel', 'zoom', 'zoom-rel', 'center', 'privacy'}
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
