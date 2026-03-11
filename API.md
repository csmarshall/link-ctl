# Insta360 Link Controller WebSocket Protocol — v2.2.1

This document describes the WebSocket protocol used by the Insta360 Link Controller
desktop application (v2.2.1) to accept commands from its mobile remote-control web UI.

It supersedes the [v1.4.1 reference by @dtinth](https://dt.in.th/Insta360LinkControllerWebSocketProtocol).
The connection procedure and protobuf framing are similar, but **the command structure
changed entirely between v1.4.1 and v2.2.1**: the old `uvcRequest`/`uvcExtendRequest`
JSON messages are gone, replaced by binary Protocol Buffers with a `ValueChangeNotification`
envelope. All paramType numbers also changed.

All values here were confirmed by live tshark captures on loopback (`lo0`) and direct
WebSocket tests against a real camera. See [`link_ctl.py`](link_ctl.py) for a complete
Python implementation and [`validate.py`](validate.py) for automated tests.

---

## Connection

The desktop app runs a local WebSocket server. The port is not fixed; discover it by
inspecting the process's open sockets:

```bash
# macOS
pgrep -f "Insta360 Link Controller" | xargs -I{} lsof -i -a -n -P -p {}

# Windows
netstat -ano | findstr LISTENING
# then match PID to: tasklist | findstr "Insta360"
```

The auth token is stored in plaintext:

| Platform | Path |
|----------|------|
| macOS    | `~/Library/Application Support/Insta360 Link Controller/startup.ini` |
| Windows  | `%LOCALAPPDATA%\Insta360 Link Controller\startup.ini` |

Read the `[Token]` section; the key is the token value, the value is a Unix timestamp.
If multiple entries exist, use the one with the highest timestamp. **Read the file
with raw I/O — do not use `configparser` or any INI parser that lowercases keys.**

Connect:

```
ws://localhost:{PORT}/?token={TOKEN}
```

Use `ping_interval=None` (Python `websockets`) or equivalent to disable automatic pings;
some operations (e.g. tilt-to-privacy at -1.0 velocity for 3.5 s) exceed default ping
timeouts and will drop the connection.

---

## Handshake

The server initiates the sequence after the TCP handshake:

```
Server → Client   connectionNotify        (field 1 in outer message)
Client → Server   controlRequest{token}   (field 4 bool=true + field 13 msg)
Server → Client   controlResponse         (field 2 in outer message)
Server → Client   deviceInfoNotify        (field 10 in outer message)
```

The `deviceInfoNotify` response contains the device serial number required for all
subsequent commands.

**One connection holds exclusive control.** The server accepts only one active control
client; opening a second connection (e.g. while the mobile web UI is open) causes the
first to lose control silently. Close the mobile UI before sending direct WS commands.

---

## Wire format

All messages are binary Protocol Buffers — no JSON, no length prefix, raw protobuf bytes
over the WebSocket text/binary frame.

### Outer message (Request)

| Field | Wire type | Content |
|-------|-----------|---------|
| 4     | bool      | `true` — marks this as a ControlRequest |
| 5     | bool      | `true` — marks this as a HeartbeatRequest |
| 6     | bool      | `true` — marks this as a PresetUpdateRequest |
| 7     | bool      | `true` — marks this as a ValueChangeNotification |
| 13    | len       | ControlRequest inner message |
| 14    | len       | HeartbeatRequest inner message (empty) |
| 15    | len       | PresetUpdateRequest inner message |
| 16    | len       | ValueChangeNotification inner message |

A request always pairs a flag field with the corresponding message field
(e.g. field 7 + field 16 for ValueChangeNotification).

### Minimal Python encoder (no protobuf library required)

```python
def _encode_varint(v: int) -> bytes:
    # Handles non-negative integers only. Negative values are not needed for
    # any command in this protocol; if required, add 2**64 before encoding.
    out = []
    while v > 0x7F:
        out.append((v & 0x7F) | 0x80)
        v >>= 7
    out.append(v)
    return bytes(out)

def _field(num: int, wire: int, payload: bytes) -> bytes:
    tag = (num << 3) | wire
    if wire == 0:   # varint
        return _encode_varint(tag) + payload
    if wire == 2:   # len-delimited
        return _encode_varint(tag) + _encode_varint(len(payload)) + payload
    raise ValueError(wire)

def _str(num: int, s: str) -> bytes:
    b = s.encode()
    return _field(num, 2, b)

def _bool(num: int) -> bytes:
    return _encode_varint((num << 3) | 0) + b'\x01'
```

---

## HeartbeatRequest

Send periodically (every ~5 s) to maintain the connection:

```python
# field 5 (bool) + field 14 (empty msg)
heartbeat = _bool(5) + _field(14, 2, b'')
```

No response is sent by the server.

---

## ValueChangeNotification — main command type

Used for all camera setting commands.

```python
def build_value_change(serial: str, param_type: int, value: str | None) -> bytes:
    inner = _str(1, serial)                              # field 1: curDeviceSerialNum
    inner += _field(2, 0, _encode_varint(param_type))   # field 2: paramType (varint)
    if value is not None:
        inner += _str(3, value)                          # field 3: newValue (string)
    outer = _bool(7) + _field(16, 2, inner)
    return outer
```

### Confirmed paramTypes (v2.2.1)

> **Breaking change from v1.4.1:** All paramType numbers changed. The old proto enum
> values (10–15, 19, 25–26 etc.) do nothing in v2.2.1. Use only the values below.

| paramType | Feature | Value format | Notes |
|-----------|---------|--------------|-------|
| 2  | Horizontal Flip | `"1"` / `"0"` | Mirror on/off |
| 3  | Reset pan/tilt to center | _(no value field)_ | Pass `value=None` |
| 4  | Zoom | `"100"` – `"400"` | Absolute zoom level |
| 5  | AI mode | see AI Mode values | Selects tracking/overhead/etc. |
| 6  | Joystick velocity | float32 sub-message | Pan + tilt velocity; see below |
| 7  | Joystick stop | float32 sub-message | Both velocities = 0.0 |
| 10 | Smart composition framing | `"1"` / `"2"` / `"3"` | `"1"`=Head, `"2"`=Half Body, `"3"`=Whole Body; requires AI tracking on |
| 11 | Smart composition on/off | `"1"` / `"0"` | Requires AI tracking on |
| 16 | Exposure compensation | `"0"` – `"100"` | 50 = 0 EV (default) |
| 17 | Auto exposure | `"1"` / `"0"` | |
| 18 | Auto focus | `"1"` / `"0"` | No DeviceInfo readback; explicit on/off required |
| 20 | Auto white balance | `"1"` / `"0"` | Turning off cascades wbTemp to ~4200 K |
| 21 | WB temperature | `"2800"` – `"10000"` | Kelvin; only effective when AWB is off |
| 22 | Brightness | `"0"` – `"100"` | Default 50 |
| 23 | Contrast | `"0"` – `"100"` | Default 50 |
| 24 | Saturation | `"0"` – `"100"` | Default 50; `"0"` = greyscale |
| 25 | Sharpness | `"0"` – `"100"` | Default 50 |
| 26 | HDR | `"1"` / `"0"` | |
| 27 | Anti-flicker | `"0"` / `"1"` / `"2"` | `"0"`=Auto, `"1"`=50 Hz, `"2"`=60 Hz |
| 39 | Gesture control zoom | `"1"` / `"0"` | Enables/disables zoom-by-gesture |

**Unknown / not yet mapped:**
- Any vertical-flip or additional mirror modes

### AI Mode values (paramType 5)

| Value | Mode |
|-------|------|
| `"0"` | Normal (AI off) |
| `"1"` | AI Tracking |
| `"4"` | Overhead |
| `"5"` | DeskView |
| `"6"` | Whiteboard |

### Joystick velocity (paramTypes 6 and 7)

Pan/tilt is **velocity-only** in v2.2.1 — there is no absolute positioning via WebSocket.

The value field is not a string but a nested protobuf message containing two `float32`
fields (wire type 5):

```python
import struct

def _float32(num: int, v: float) -> bytes:
    tag = _encode_varint((num << 3) | 5)   # wire type 5 = 32-bit
    return tag + struct.pack('<f', v)

def build_joystick(serial: str, pan_vel: float, tilt_vel: float) -> bytes:
    sub = _float32(1, pan_vel) + _float32(2, tilt_vel)
    inner  = _str(1, serial)
    inner += _field(2, 0, _encode_varint(6))   # paramType 6 = JOYSTICK_MOVE
    inner += _field(4, 2, sub)                 # field 4: float sub-message (no field 3)
    return _bool(7) + _field(16, 2, inner)

def build_joystick_stop(serial: str) -> bytes:
    return build_joystick(serial, 0.0, 0.0)   # paramType 7 can also be used
```

Velocity range: `-1.0` (full speed negative) to `+1.0` (full speed positive).
Positive pan = right, positive tilt = up (empirically confirmed).
Duration is controlled by how long you hold the velocity before sending stop.

---

## PresetUpdateRequest

```python
def build_preset(serial: str, op: int, index: int) -> bytes:
    inner  = _field(1, 0, _encode_varint(op))     # field 1: type enum
    inner += _field(2, 0, _encode_varint(index))  # field 2: position (0-based)
    inner += _str(4, serial)                      # field 4: serial number (confirmed from capture)
    return _bool(6) + _field(15, 2, inner)
```

| Op value | Operation |
|----------|-----------|
| 0 | ADD (save new preset — untested; use UPDATE in practice) |
| 1 | UPDATE (overwrite existing preset at index — use this for save) |
| 2 | DELETE |
| 3 | RENAME |
| 4 | RECALL (load preset) |

---

## DeviceInfoNotification (server → client)

Sent by the server on connect and after some state changes. Outer field 10 contains:

| Field | Content |
|-------|---------|
| 1 | Repeated `DeviceInfo` message |
| 2 | `curDeviceSerialNum` string |

> **v1.4.1 vs v2.2.1:** In v1.4.1, field 1 was the serial string and field 2 was
> DeviceInfo. These are **swapped** in v2.2.1. Detect by checking whether field 1
> bytes decode as a short ASCII string (< 64 chars, no nulls) — if so it's v1.4.1.

### DeviceInfo message fields

| Field | Type | Content |
|-------|------|---------|
| 1 | string | deviceName |
| 2 | string | serialNum |
| 4 | varint | mode (0=normal, 1=tracking, 4=overhead, 5=deskview, 6=whiteboard) |
| 5 | message | ZoomInfo {field1=curValue, field2=minValue, field3=maxValue} |
| 5 | varint | mirror/horizontal-flip bool — same field number, only present when ZoomInfo is absent; **unreliable**: DeviceInfo does not update after a flip command is sent |
| 9 | varint | curPresetPos |
| 10 | message | Image settings sub-message (see below) |

### Image settings sub-message (DeviceInfo field 10)

| Sub-field | Content |
|-----------|---------|
| 9  | HDR (bool) |
| 12 | brightness (int, 0–100) |
| 13 | contrast (int, 0–100) |
| 14 | saturation (int, 0–100) |
| 15 | sharpness (int, 0–100) |
| 17 | autoExposure (bool) |
| 20 | exposureComp (int, 0–100; default 50 = 0 EV) |
| 21 | autoWhiteBalance (bool) |
| 22 | wbTemp (int, Kelvin) |
| 24 | smartComposition (bool) |

**No autofocus readback exists in DeviceInfo.** The camera never reports autofocus
state; smart toggle is structurally impossible.

---

## Notable cascade effects

- Turning **Auto Exposure off** (paramType 17 → `"0"`) unlocks the Exposure
  Compensation slider in the UI but does not change any other DeviceInfo field.
- Turning **Auto White Balance off** (paramType 20 → `"0"`) cascades wbTemp to
  approximately 4200 K and unlocks the WB Temperature slider.
- Sending **paramType 28, 29, or 30** triggers a full device state dump from the server.

---

## Version history

| App version | Protocol notes |
|-------------|---------------|
| 1.4.1 | `uvcRequest` / `uvcExtendRequest` JSON-over-WS; paramTypes 1–30 per original proto enum. Documented at [dt.in.th](https://dt.in.th/Insta360LinkControllerWebSocketProtocol). |
| 2.2.1 | Pure binary protobuf; `ValueChangeNotification` replaces all uvc* messages. All paramType numbers changed. Pan/tilt changed from relative UVC to velocity-only joystick. DeviceInfo field layout changed (serial ↔ DeviceInfo fields swapped). |

---

## Discovery method

Protocol was reverse-engineered by:
1. Capturing loopback WebSocket traffic with `tshark -i lo0 -Y websocket`
2. Hex-decoding frames and parsing manually with a minimal protobuf field decoder
3. Correlating UI actions in the mobile web remote (Playwright-driven) with captured
   frames to map UI controls → paramTypes
4. Confirming each paramType with direct WebSocket send + DeviceInfo readback

The `.proto` schema shipped in the app bundle (`insta360linkcontroller.proto`) provided
message structure but **incorrect paramType enum values** for v2.2.1 — all numbers were
verified empirically.
