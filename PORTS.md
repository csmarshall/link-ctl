# Cross-platform USB-direct — port plan

v2.1 shipped the macOS USB-direct backend as `link_usb_macos.py` (pure
Python, ctypes → IOKit). Linux and Windows still fall back to
subprocess / WebSocket. This doc sketches what each would take.

## Shared design

`link_ctl.py` selects a backend at import time:

```python
if platform.system() == 'Darwin':
    import link_usb_macos as _usb_backend
elif platform.system() == 'Linux':
    import link_usb_linux as _usb_backend   # TBD (v2.2)
elif platform.system() == 'Windows':
    import link_usb_windows as _usb_backend # TBD (v2.3)
```

Each backend exposes the same surface:

```python
def get(unit: int, sel: int, length: int) -> bytes: ...
def set(unit: int, sel: int, data: bytes) -> None: ...
```

Everything above that — PTZ, presets, image/AI, joystick — is platform-
agnostic and reuses the macOS code path unchanged.

---

## Linux backend (`link_usb_linux.py`)

**Platform API:** Linux kernel's UVC driver exposes `UVCIOC_CTRL_QUERY`
on `/dev/video*`. It's a plain `fcntl.ioctl()` call — no C helper needed,
no `libuvc`, no `libusb`. Coexists cleanly with another process streaming
video from the same device.

**Dependencies:** stdlib only (`os`, `fcntl`, `ctypes`).

**Implementation sketch:**

```python
import ctypes, fcntl, os

UVC_SET_CUR = 0x01
UVC_GET_CUR = 0x81

class uvc_xu_control_query(ctypes.Structure):
    _fields_ = [
        ('unit',     ctypes.c_uint8),
        ('selector', ctypes.c_uint8),
        ('query',    ctypes.c_uint8),   # GET_CUR / SET_CUR / GET_LEN / …
        ('size',     ctypes.c_uint16),
        ('data',     ctypes.POINTER(ctypes.c_uint8)),
    ]

# _IOWR('u', 0x21, struct uvc_xu_control_query)
UVCIOC_CTRL_QUERY = (3 << 30) | (ctypes.sizeof(uvc_xu_control_query) << 16) \
                    | (ord('u') << 8) | 0x21

def _find_device() -> str:
    # Walk /sys/class/video4linux/video*/device/.. and match idVendor/
    # idProduct. Pick the videoN with bInterfaceNumber=0 (VideoControl).
    ...

def get(unit, sel, length):
    buf = (ctypes.c_uint8 * length)()
    q = uvc_xu_control_query(unit, sel, UVC_GET_CUR, length,
                             ctypes.cast(buf, ctypes.POINTER(ctypes.c_uint8)))
    fcntl.ioctl(_fd, UVCIOC_CTRL_QUERY, q)
    return bytes(buf)

def set(unit, sel, data):
    buf = (ctypes.c_uint8 * len(data))(*data)
    q = uvc_xu_control_query(unit, sel, UVC_SET_CUR, len(data),
                             ctypes.cast(buf, ctypes.POINTER(ctypes.c_uint8)))
    fcntl.ioctl(_fd, UVCIOC_CTRL_QUERY, q)
```

**Prior art in this repo:** `link_usb.py:310` already has a
`LinuxXUBackend` class with this exact pattern. Port job is mostly:
verify the ioctl encoding, adapt the `get`/`set` signature to match
`link_usb_macos.py`, wire into `link_ctl.py`.

**Access:** user must be in the `video` group (or own `/dev/video0`) —
no sudo needed.

**Gotchas:**
- UVC driver validates length against `GET_LEN`. The Insta360 firmware
  reports weird lengths for some XU selectors (e.g. sel 0x02 reports
  `len=1` but accepts 52-byte writes). Our macOS backend works around
  this because IOKit doesn't enforce length; Linux UVC driver DOES. May
  need to either patch the driver's length check or find a quirk-mode
  alternative.
- Some distros need `options uvcvideo quirks=0x80` (or similar) in
  modprobe config to unlock raw XU access.

**Effort:** ~100 lines of Python. Half a day.

**Test coverage needed:**
- `tools/xu_verify.py` already tests the 16 XU controls — ideally it
  works unchanged once `link_ctl._usb_backend` resolves to Linux.

---

## Windows backend (`link_usb_windows.py`)

**Platform API:** Windows Kernel Streaming (KS). UVC cameras are owned
by the inbox `usbvideo.sys` driver. Extension Unit access goes through
`DeviceIoControl(IOCTL_KS_PROPERTY)` with a `KSPROPERTY` struct whose
`Set` field is the XU's 128-bit GUID and `Id` is the selector.

**Dependencies:** stdlib only (`ctypes`, `ctypes.wintypes`). No pywin32.

**Implementation sketch:**

```python
import ctypes
from ctypes import wintypes

IOCTL_KS_PROPERTY = 0x002f0003   # CTL_CODE(FILE_DEVICE_KS, 0x0, METHOD_NEITHER, FILE_ANY_ACCESS)
KSPROPERTY_TYPE_GET = 0x00000001
KSPROPERTY_TYPE_SET = 0x00000002

class GUID(ctypes.Structure):
    _fields_ = [('Data1', wintypes.DWORD), ('Data2', wintypes.WORD),
                ('Data3', wintypes.WORD), ('Data4', ctypes.c_ubyte * 8)]

class KSP_NODE(ctypes.Structure):
    _fields_ = [('Set',      GUID),
                ('Id',       wintypes.DWORD),
                ('Flags',    wintypes.DWORD),
                ('NodeId',   wintypes.DWORD),
                ('Reserved', wintypes.DWORD)]

def get(unit, sel, length):
    # unit here is the XU's KS node id (discovered once at connect time)
    ksp = KSP_NODE(Set=_XU_GUIDS[unit], Id=sel, Flags=KSPROPERTY_TYPE_GET,
                   NodeId=unit, Reserved=0)
    buf = (ctypes.c_uint8 * length)()
    bytes_ret = wintypes.DWORD()
    ok = ctypes.windll.kernel32.DeviceIoControl(
        _handle, IOCTL_KS_PROPERTY, ctypes.byref(ksp), ctypes.sizeof(ksp),
        buf, length, ctypes.byref(bytes_ret), None)
    if not ok:
        raise OSError(ctypes.get_last_error())
    return bytes(buf[:bytes_ret.value])
```

**Device discovery:** walk `SetupDiGetClassDevs(GUID_DEVINTERFACE_USB_DEVICE)`
to find `USB\VID_2E1A&PID_4C01`, then enumerate its interfaces to find the
VideoControl node.

**XU GUIDs:** Windows KS requires the full 128-bit GUID, not just the unit
id. Prefixes are known:
  - unit 9:  `faf1672d-…` (AI / core controls)
  - unit 10: `e307e649-…` (Link 2 privacy)
  - unit 11: `a8bd5df2-…` (audio / misc)

Full GUIDs need a one-time capture — easiest via `USBView.exe` (free Microsoft
tool) or by dumping the UVC descriptors with `libusb` / `usbdesc.c`. Would need
to do this on a machine with the camera + Windows.

**Access:** no elevated privileges required; `DeviceIoControl` just needs a
valid handle. The camera can be actively streaming in another app.

**Gotchas:**
- The XU node ID in KS topology isn't always the same as the USB descriptor
  unit ID; needs topology enumeration via `KSPROPERTY_TOPOLOGY_NODES`.
- Some Windows installations block XU access if the device has no per-vendor
  INF — the Insta360 Link Controller app installs one; the CLI would need to
  either rely on that or ship its own INF.

**Effort:** 250-300 lines of Python. One or two days including discovery
code and GUID capture. Requires access to a Windows machine with the
camera for testing — main blocker for development in this repo.

---

## Open items per platform

| Item | macOS | Linux | Windows |
|---|---|---|---|
| PTZ (pan/tilt absolute + relative, zoom) | ✅ ctypes IOKit (v2.1) | ⚠️ `v4l2-ctl` subprocess | ❌ WS fallback only |
| Image settings (brightness/contrast/etc) | ✅ ctypes IOKit | ❌ WS fallback | ❌ WS fallback |
| AI modes (track/whiteboard/…) | ✅ ctypes IOKit | ❌ WS fallback | ❌ WS fallback |
| Presets (USB-direct, host-side JSON) | ✅ | ❌ WS fallback | ❌ WS fallback |
| Interactive joystick | ✅ | ❌ requires USB-direct | ❌ requires USB-direct |
| Exclusive-access-free coexistence | ✅ | native (UVC ioctls don't grab the stream) | ⚠️ KS sharing depends on driver |
| Requires sudo / elevated privileges | no | no (member of `video` group) | no |
| Requires compiled helper binary | no | no | no |

## Sequence

1. **v2.1 (shipped)** — macOS ctypes IOKit.
2. **v2.2** — Linux `fcntl.ioctl()` backend. Reuse the scaffolding in
   `link_usb.py:310`. Low risk; stdlib only; can be developed on any
   Linux machine with the camera.
3. **v2.3** — Windows `DeviceIoControl(IOCTL_KS_PROPERTY)` backend.
   Needs a one-time XU GUID capture from a Windows USBView dump and
   testing on a Windows machine.
