#!/usr/bin/env python3
"""link_usb — Direct UVC Extension Unit communication with the Insta360 Link.

Bypasses the Insta360 Link Controller desktop app entirely by sending UVC
Extension Unit (XU) GET/SET requests directly to the camera over USB.

Platform backends:
  Linux:   UVCIOC_CTRL_QUERY ioctl on /dev/videoN (non-exclusive; camera
           remains available to other apps)
  macOS:   libuvc via ctypes (exclusive; camera unavailable to other apps
           while connection is open)
  Windows: stub — prints instructions (IKsControl via comtypes, future)

Dependencies:
  Linux:   stdlib only (fcntl, ctypes)
  macOS:   libuvc system library (brew install libuvc)
  Windows: none yet

Usage:
  from link_usb import get_xu_backend, LinkUSB

  backend = get_xu_backend()
  cam = LinkUSB(backend)
  cam.connect()
  info = cam.get_device_info()
  cam.set_privacy(True)
  cam.close()
"""
from __future__ import annotations

import ctypes
import os
import platform
import re
import struct
import subprocess
import sys
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path

# ── UVC request codes ────────────────────────────────────────────────────────

UVC_SET_CUR = 0x01
UVC_GET_CUR = 0x81
UVC_GET_MIN = 0x82
UVC_GET_MAX = 0x83
UVC_GET_RES = 0x84
UVC_GET_LEN = 0x85
UVC_GET_INFO = 0x86
UVC_GET_DEF = 0x87

UVC_REQ_NAMES = {
    UVC_SET_CUR: 'SET_CUR', UVC_GET_CUR: 'GET_CUR', UVC_GET_MIN: 'GET_MIN',
    UVC_GET_MAX: 'GET_MAX', UVC_GET_RES: 'GET_RES', UVC_GET_LEN: 'GET_LEN',
    UVC_GET_INFO: 'GET_INFO', UVC_GET_DEF: 'GET_DEF',
}

# ── UVC GET_INFO capability bits ─────────────────────────────────────────────

XU_INFO_GET_SUPPORTED = 0x01
XU_INFO_SET_SUPPORTED = 0x02
XU_INFO_DISABLED      = 0x04
XU_INFO_AUTOUPDATE    = 0x08
XU_INFO_ASYNC         = 0x10

# ── Insta360 Link ControlSelector enum ───────────────────────────────────────
# From insta360linkcontroller.proto lines 43-75.  These are the XU control
# selector values the desktop app uses to talk to the camera firmware.

XU_CONTROL_UNDEFINED                    = 0
XU_EXEC_SCRIPT_CONTROL                  = 1
XU_VIDEO_MODE_CONTROL                   = 2
XU_DEVICE_INFO_CONTROL                  = 3
XU_PTZ_CMD_CONTROL                      = 4
XU_GESTURE_STATUS_CONTROL               = 5
XU_GESTURE_BIND_CONTROL                 = 6
XU_NOISE_CANCEL_CONTROL                 = 7
XU_FIRMWARE_UPGRADE_OR_BLEND_DRAW       = 8
XU_EXPOSURE_VALUE_CONTROL               = 9
XU_TAKE_PICTURE_CONTROL                 = 10
XU_DEVICE_STATUS_CONTROL                = 11
XU_DEVICE_SN_CONTROL                    = 12
XU_DEVICE_LICENSE_CONTROL               = 13
XU_DEVICE_PARAM_CONTROL                 = 14
XU_AF_MODE_OR_DOWNLOAD_FILE             = 15
XU_EXPOSURE_CURVE_OR_UPLOAD_FILE        = 16
XU_USB_MODE_SWITCH_CONTROL              = 17
XU_TRACK_SPEED_CONTROL                  = 18
XU_LAYOUT_STYLE_CONTROL                 = 19
XU_HEAD_LIST_CONTROL                    = 20
XU_TRACK_TARGET_CONTROL                 = 21
XU_PANTILT_RELATIVE_CONTROL             = 22
XU_MOBVOI_PUBKEY_CONTROL                = 23
XU_BIAS_CONTROL                         = 24
XU_ISO_CONTROL                          = 25
XU_PANTILT_ABSOLUTE_CONTROL             = 26
XU_FUNC_ENABLE_CONTROL                  = 27
XU_VIDEO_RES_CONTROL                    = 28
XU_EXPOSURE_TIME_ABSOLUTE_CONTROL       = 29
XU_AE_MODE_CONTROL                      = 30

XU_SELECTOR_NAMES = {
    0: 'XU_CONTROL_UNDEFINED',
    1: 'XU_EXEC_SCRIPT_CONTROL',
    2: 'XU_VIDEO_MODE_CONTROL',
    3: 'XU_DEVICE_INFO_CONTROL',
    4: 'XU_PTZ_CMD_CONTROL',
    5: 'XU_GESTURE_STATUS_CONTROL',
    6: 'XU_GESTURE_BIND_CONTROL',
    7: 'XU_NOISE_CANCEL_CONTROL',
    8: 'XU_FIRMWARE_UPGRADE_OR_BLEND_DRAW',
    9: 'XU_EXPOSURE_VALUE_CONTROL',
    10: 'XU_TAKE_PICTURE_CONTROL',
    11: 'XU_DEVICE_STATUS_CONTROL',
    12: 'XU_DEVICE_SN_CONTROL',
    13: 'XU_DEVICE_LICENSE_CONTROL',
    14: 'XU_DEVICE_PARAM_CONTROL',
    15: 'XU_AF_MODE_OR_DOWNLOAD_FILE',
    16: 'XU_EXPOSURE_CURVE_OR_UPLOAD_FILE',
    17: 'XU_USB_MODE_SWITCH_CONTROL',
    18: 'XU_TRACK_SPEED_CONTROL',
    19: 'XU_LAYOUT_STYLE_CONTROL',
    20: 'XU_HEAD_LIST_CONTROL',
    21: 'XU_TRACK_TARGET_CONTROL',
    22: 'XU_PANTILT_RELATIVE_CONTROL',
    23: 'XU_MOBVOI_PUBKEY_CONTROL',
    24: 'XU_BIAS_CONTROL',
    25: 'XU_ISO_CONTROL',
    26: 'XU_PANTILT_ABSOLUTE_CONTROL',
    27: 'XU_FUNC_ENABLE_CONTROL',
    28: 'XU_VIDEO_RES_CONTROL',
    29: 'XU_EXPOSURE_TIME_ABSOLUTE_CONTROL',
    30: 'XU_AE_MODE_CONTROL',
}

# Maximum selector value to probe
XU_MAX_SELECTOR = 30


# ── Data classes ─────────────────────────────────────────────────────────────

@dataclass
class XUInfo:
    """Describes a UVC Extension Unit discovered on the device."""
    guid: bytes = b''
    guid_str: str = ''
    unit_id: int = 0
    num_controls: int = 0
    bm_controls: int = 0

@dataclass
class XUControlInfo:
    """Capabilities and current value of a single XU control selector."""
    selector: int = 0
    name: str = ''
    supported: bool = False
    length: int = 0
    info_flags: int = 0
    can_get: bool = False
    can_set: bool = False
    cur_value: bytes = b''
    min_value: bytes = b''
    max_value: bytes = b''
    def_value: bytes = b''
    res_value: bytes = b''
    error: str = ''


# ── Abstract backend ─────────────────────────────────────────────────────────

class XUBackend(ABC):
    """Abstract base for platform-specific UVC Extension Unit access."""

    @abstractmethod
    def open(self, device_path: str | None = None) -> None:
        """Open the camera device. Raises RuntimeError on failure."""

    @abstractmethod
    def close(self) -> None:
        """Release the device."""

    @abstractmethod
    def xu_get(self, unit: int, selector: int, size: int,
               query: int = UVC_GET_CUR) -> bytes:
        """Issue a UVC XU GET request. Returns raw data bytes."""

    @abstractmethod
    def xu_set(self, unit: int, selector: int, data: bytes) -> None:
        """Issue a UVC XU SET_CUR request."""

    def xu_get_len(self, unit: int, selector: int) -> int:
        """Query the data length for a control selector (GET_LEN)."""
        raw = self.xu_get(unit, selector, 2, UVC_GET_LEN)
        return int.from_bytes(raw[:2], 'little') if raw else 0

    def xu_get_info(self, unit: int, selector: int) -> int:
        """Query capability flags for a control selector (GET_INFO)."""
        raw = self.xu_get(unit, selector, 1, UVC_GET_INFO)
        return raw[0] if raw else 0

    def query_control(self, unit: int, selector: int) -> XUControlInfo:
        """Probe a single XU control: length, info, current/min/max/def values."""
        name = XU_SELECTOR_NAMES.get(selector, f'UNKNOWN_{selector}')
        info = XUControlInfo(selector=selector, name=name)
        try:
            length = self.xu_get_len(unit, selector)
            if length == 0:
                return info
            info.length = length
            info.supported = True

            flags = self.xu_get_info(unit, selector)
            info.info_flags = flags
            info.can_get = bool(flags & XU_INFO_GET_SUPPORTED)
            info.can_set = bool(flags & XU_INFO_SET_SUPPORTED)

            if info.can_get:
                try:
                    info.cur_value = self.xu_get(unit, selector, length, UVC_GET_CUR)
                except OSError:
                    pass
                try:
                    info.min_value = self.xu_get(unit, selector, length, UVC_GET_MIN)
                except OSError:
                    pass
                try:
                    info.max_value = self.xu_get(unit, selector, length, UVC_GET_MAX)
                except OSError:
                    pass
                try:
                    info.def_value = self.xu_get(unit, selector, length, UVC_GET_DEF)
                except OSError:
                    pass
                try:
                    info.res_value = self.xu_get(unit, selector, length, UVC_GET_RES)
                except OSError:
                    pass
        except OSError as e:
            info.error = str(e)
        return info



# ── Linux backend (UVCIOC_CTRL_QUERY ioctl) ──────────────────────────────────

def _linux_ioctl_number():
    """Compute UVCIOC_CTRL_QUERY = _IOWR('u', 0x21, struct uvc_xu_control_query).

    The ioctl number encodes the direction, type char, command number, and
    struct size.  We compute it dynamically to handle 32-bit vs 64-bit
    differences in pointer size (which affects struct padding).
    """
    # _IOC(dir, type, nr, size)
    # dir: _IOC_READ | _IOC_WRITE = 3 for _IOWR
    # type: ord('u') = 0x75
    # nr: 0x21
    # size: sizeof(struct uvc_xu_control_query)
    #
    # struct uvc_xu_control_query {
    #   __u8  unit;       // offset 0
    #   __u8  selector;   // offset 1
    #   __u8  query;      // offset 2
    #   __u16 size;       // offset 4 (after 1 byte padding)
    #   __u8 *data;       // offset 8 (64-bit) or 4 (32-bit, but kernel packs at 8)
    # };
    #
    # On 64-bit Linux: total size = 16 bytes (8 bytes for fields + 8 for pointer)
    # On 32-bit Linux: total size = 8 bytes (6 bytes for fields + pad + 4 for pointer)

    ptr_size = ctypes.sizeof(ctypes.c_void_p)
    if ptr_size == 8:
        struct_size = 16
    else:
        struct_size = 8

    _IOC_NRBITS = 8
    _IOC_TYPEBITS = 8
    _IOC_SIZEBITS = 14
    _IOC_NRSHIFT = 0
    _IOC_TYPESHIFT = _IOC_NRSHIFT + _IOC_NRBITS
    _IOC_SIZESHIFT = _IOC_TYPESHIFT + _IOC_TYPEBITS
    _IOC_DIRSHIFT = _IOC_SIZESHIFT + _IOC_SIZEBITS

    _IOC_WRITE = 1
    _IOC_READ = 2

    direction = _IOC_READ | _IOC_WRITE  # _IOWR
    return (
        (direction << _IOC_DIRSHIFT) |
        (ord('u') << _IOC_TYPESHIFT) |
        (0x21 << _IOC_NRSHIFT) |
        (struct_size << _IOC_SIZESHIFT)
    )


class _UvcXuControlQuery(ctypes.Structure):
    """struct uvc_xu_control_query from <linux/uvcvideo.h>."""
    _fields_ = [
        ('unit', ctypes.c_uint8),
        ('selector', ctypes.c_uint8),
        ('query', ctypes.c_uint8),
        ('_pad', ctypes.c_uint8),
        ('size', ctypes.c_uint16),
        ('_pad2', ctypes.c_uint16),
        ('data', ctypes.c_void_p),
    ]


class LinuxXUBackend(XUBackend):
    """UVC Extension Unit access via UVCIOC_CTRL_QUERY ioctl on /dev/videoN.

    This is the preferred backend: non-exclusive (the camera remains available
    to other applications), no extra dependencies, works with the standard
    Linux uvcvideo driver.
    """

    def __init__(self):
        self._fd: int | None = None
        self._device_path: str = ''
        self._ioctl_num = _linux_ioctl_number()

    def open(self, device_path: str | None = None) -> None:
        if device_path is None:
            device_path = _find_linux_video_device()
        if device_path is None:
            raise RuntimeError(
                'No Insta360 Link camera found.\n'
                'Check: is the camera connected via USB?\n'
                'Try: ls /dev/video* and v4l2-ctl --list-devices')
        import fcntl  # Linux only
        self._device_path = device_path
        self._fd = os.open(device_path, os.O_RDWR)

    def close(self) -> None:
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None

    def xu_get(self, unit: int, selector: int, size: int,
               query: int = UVC_GET_CUR) -> bytes:
        import fcntl
        if self._fd is None:
            raise RuntimeError('Device not open')
        data_buf = (ctypes.c_uint8 * size)()
        q = _UvcXuControlQuery()
        q.unit = unit
        q.selector = selector
        q.query = query
        q.size = size
        q.data = ctypes.addressof(data_buf)
        fcntl.ioctl(self._fd, self._ioctl_num, q)
        return bytes(data_buf)

    def xu_set(self, unit: int, selector: int, data: bytes) -> None:
        import fcntl
        if self._fd is None:
            raise RuntimeError('Device not open')
        size = len(data)
        data_buf = (ctypes.c_uint8 * size)(*data)
        q = _UvcXuControlQuery()
        q.unit = unit
        q.selector = selector
        q.query = UVC_SET_CUR
        q.size = size
        q.data = ctypes.addressof(data_buf)
        fcntl.ioctl(self._fd, self._ioctl_num, q)


def _find_linux_video_device() -> str | None:
    """Find the Insta360 Link's /dev/videoN device node.

    Searches /sys/class/video4linux/video*/name for a device whose name
    contains 'insta360' (case-insensitive).  Falls back to the
    LINK_CTL_V4L2_DEVICE environment variable or /dev/video0.
    """
    env_device = os.environ.get('LINK_CTL_V4L2_DEVICE')
    if env_device and os.path.exists(env_device):
        return env_device

    sysfs = Path('/sys/class/video4linux')
    if sysfs.is_dir():
        for entry in sorted(sysfs.iterdir()):
            name_file = entry / 'name'
            if name_file.exists():
                try:
                    name = name_file.read_text().strip()
                    if 'insta360' in name.lower():
                        dev_path = f'/dev/{entry.name}'
                        if os.path.exists(dev_path):
                            return dev_path
                except OSError:
                    continue

    # Fallback: /dev/video0 if it exists
    if os.path.exists('/dev/video0'):
        return '/dev/video0'
    return None



# ── macOS backend (libuvc via ctypes) ────────────────────────────────────────
# Requires: brew install libuvc
# libuvc itself depends on libusb, which uses IOKit on macOS.
#
# IMPORTANT: libuvc takes exclusive device access.  The camera will not be
# available to other applications (Zoom, Teams, etc.) while the connection
# is open.  Use short-lived connections: open → get/set → close.

# libuvc type definitions (ctypes)

class _uvc_context(ctypes.Structure):
    pass

class _uvc_device(ctypes.Structure):
    pass

class _uvc_device_handle(ctypes.Structure):
    pass

_uvc_context_p = ctypes.POINTER(_uvc_context)
_uvc_device_p = ctypes.POINTER(_uvc_device)
_uvc_device_handle_p = ctypes.POINTER(_uvc_device_handle)


def _load_libuvc():
    """Load libuvc shared library. Returns None if not available."""
    names = ['libuvc.dylib', 'libuvc.so', 'libuvc.0.dylib', 'libuvc.so.0']
    for name in names:
        try:
            lib = ctypes.cdll.LoadLibrary(name)
            return lib
        except OSError:
            continue
    # Try Homebrew paths explicitly
    brew_paths = [
        '/opt/homebrew/lib/libuvc.dylib',  # Apple Silicon
        '/usr/local/lib/libuvc.dylib',      # Intel
    ]
    for path in brew_paths:
        if os.path.exists(path):
            try:
                return ctypes.cdll.LoadLibrary(path)
            except OSError:
                continue
    return None


class MacOSXUBackend(XUBackend):
    """UVC Extension Unit access via libuvc (ctypes wrapper).

    libuvc provides cross-platform UVC camera access via libusb.
    On macOS it uses IOKit internally.

    CAVEAT: Takes exclusive device access.  Other apps cannot use the
    camera while this backend has it open.  Keep connections short.
    """

    def __init__(self):
        self._lib = None
        self._ctx = _uvc_context_p()
        self._dev = _uvc_device_p()
        self._devh = _uvc_device_handle_p()
        self._opened = False

    def open(self, device_path: str | None = None) -> None:
        self._lib = _load_libuvc()
        if self._lib is None:
            raise RuntimeError(
                'libuvc not found.\n'
                'Install: brew install libuvc\n'
                'If installed, check: ls /opt/homebrew/lib/libuvc.dylib')

        # Set up function signatures
        self._lib.uvc_init.argtypes = [ctypes.POINTER(_uvc_context_p), ctypes.c_void_p]
        self._lib.uvc_init.restype = ctypes.c_int
        self._lib.uvc_exit.argtypes = [_uvc_context_p]
        self._lib.uvc_exit.restype = None
        self._lib.uvc_find_device.argtypes = [
            _uvc_context_p, ctypes.POINTER(_uvc_device_p),
            ctypes.c_int, ctypes.c_int, ctypes.c_char_p]
        self._lib.uvc_find_device.restype = ctypes.c_int
        self._lib.uvc_open.argtypes = [_uvc_device_p, ctypes.POINTER(_uvc_device_handle_p)]
        self._lib.uvc_open.restype = ctypes.c_int
        self._lib.uvc_close.argtypes = [_uvc_device_handle_p]
        self._lib.uvc_close.restype = None
        self._lib.uvc_unref_device.argtypes = [_uvc_device_p]
        self._lib.uvc_unref_device.restype = None

        self._lib.uvc_get_ctrl.argtypes = [
            _uvc_device_handle_p, ctypes.c_uint8, ctypes.c_uint8,
            ctypes.c_void_p, ctypes.c_int, ctypes.c_int]
        self._lib.uvc_get_ctrl.restype = ctypes.c_int
        self._lib.uvc_set_ctrl.argtypes = [
            _uvc_device_handle_p, ctypes.c_uint8, ctypes.c_uint8,
            ctypes.c_void_p, ctypes.c_int]
        self._lib.uvc_set_ctrl.restype = ctypes.c_int

        # Initialize context
        ret = self._lib.uvc_init(ctypes.byref(self._ctx), None)
        if ret < 0:
            raise RuntimeError(f'uvc_init failed: {ret}')

        # Find Insta360 Link device by VID:PID
        ret = self._lib.uvc_find_device(
            self._ctx, ctypes.byref(self._dev), 0x2E1A, 0x4C01, None)
        if ret < 0:
            self._lib.uvc_exit(self._ctx)
            raise RuntimeError(
                'No UVC device found.\n'
                'Check: is the Insta360 Link connected via USB?\n'
                'On macOS, you may need to grant Terminal camera access in\n'
                'System Settings > Privacy & Security > Camera.')

        # Open device
        ret = self._lib.uvc_open(self._dev, ctypes.byref(self._devh))
        if ret < 0:
            self._lib.uvc_unref_device(self._dev)
            self._lib.uvc_exit(self._ctx)
            raise RuntimeError(
                f'uvc_open failed: {ret}\n'
                'The camera may be in use by another application.\n'
                'Close the Insta360 Link Controller and any video apps, then retry.')
        self._opened = True

    def close(self) -> None:
        if self._opened:
            self._lib.uvc_close(self._devh)
            self._opened = False
        if self._dev:
            try:
                self._lib.uvc_unref_device(self._dev)
            except Exception:
                pass
        if self._ctx:
            try:
                self._lib.uvc_exit(self._ctx)
            except Exception:
                pass

    def xu_get(self, unit: int, selector: int, size: int,
               query: int = UVC_GET_CUR) -> bytes:
        if not self._opened:
            raise RuntimeError('Device not open')
        buf = (ctypes.c_uint8 * size)()
        ret = self._lib.uvc_get_ctrl(
            self._devh, unit, selector,
            ctypes.cast(buf, ctypes.c_void_p), size, query)
        if ret < 0:
            raise OSError(f'uvc_get_ctrl failed: {ret} '
                          f'(unit={unit}, sel={selector}, query={UVC_REQ_NAMES.get(query, hex(query))})')
        return bytes(buf)

    def xu_set(self, unit: int, selector: int, data: bytes) -> None:
        if not self._opened:
            raise RuntimeError('Device not open')
        size = len(data)
        buf = (ctypes.c_uint8 * size)(*data)
        ret = self._lib.uvc_set_ctrl(
            self._devh, unit, selector,
            ctypes.cast(buf, ctypes.c_void_p), size)
        if ret < 0:
            raise OSError(f'uvc_set_ctrl failed: {ret} '
                          f'(unit={unit}, sel={selector}, size={size})')


# ── Windows backend (stub) ───────────────────────────────────────────────────

class WindowsXUBackend(XUBackend):
    """Stub for Windows UVC Extension Unit access.

    Full implementation will use IKsControl via comtypes (DirectShow/KS
    property sets).  The extension unit's GUID (discovered by xu_discover.py)
    becomes the KS property set GUID, and control selectors map to property IDs.

    For now, this raises an error with guidance.
    """

    def open(self, device_path: str | None = None) -> None:
        raise RuntimeError(
            'Windows USB backend is not yet implemented.\n'
            'Use the WebSocket transport (--ws) with the Link Controller app running.\n'
            'For direct USB control, use Linux (best support) or macOS.\n\n'
            'Implementation notes for contributors:\n'
            '  - Use comtypes to access IKsControl from DirectShow filter graph\n'
            '  - Property set GUID = XU GUID from camera descriptor\n'
            '  - Property ID = control selector number\n'
            '  - See: https://learn.microsoft.com/en-us/windows-hardware/drivers/'
            'stream/extension-unit-plug-in-architecture')

    def close(self) -> None:
        pass

    def xu_get(self, unit: int, selector: int, size: int,
               query: int = UVC_GET_CUR) -> bytes:
        raise RuntimeError('Windows backend not implemented')

    def xu_set(self, unit: int, selector: int, data: bytes) -> None:
        raise RuntimeError('Windows backend not implemented')


# ── Backend factory ──────────────────────────────────────────────────────────

def get_xu_backend() -> XUBackend:
    """Return the appropriate XUBackend for the current platform."""
    system = platform.system()
    if system == 'Linux':
        return LinuxXUBackend()
    elif system == 'Darwin':
        return MacOSXUBackend()
    elif system == 'Windows':
        return WindowsXUBackend()
    else:
        raise RuntimeError(f'Unsupported platform: {system}')


# ── Device discovery (cross-platform) ────────────────────────────────────────

@dataclass
class DeviceInfo:
    """Information about a discovered Insta360 Link camera."""
    platform: str = ''
    device_path: str = ''
    name: str = ''
    vid: int = 0
    pid: int = 0


def find_insta360_device() -> DeviceInfo | None:
    """Find an Insta360 Link camera on any platform."""
    system = platform.system()
    if system == 'Linux':
        return _find_linux_device_info()
    elif system == 'Darwin':
        return _find_macos_device_info()
    elif system == 'Windows':
        return _find_windows_device_info()
    return None


def _find_linux_device_info() -> DeviceInfo | None:
    """Find Insta360 Link on Linux via sysfs."""
    sysfs = Path('/sys/class/video4linux')
    if not sysfs.is_dir():
        return None
    for entry in sorted(sysfs.iterdir()):
        name_file = entry / 'name'
        if not name_file.exists():
            continue
        try:
            name = name_file.read_text().strip()
        except OSError:
            continue
        if 'insta360' not in name.lower():
            continue
        dev_path = f'/dev/{entry.name}'
        info = DeviceInfo(platform='Linux', device_path=dev_path, name=name)
        # Try to read VID:PID from sysfs
        try:
            uevent = (entry / 'device' / '..' / 'uevent').resolve()
            if uevent.exists():
                text = uevent.read_text()
                for line in text.splitlines():
                    if line.startswith('PRODUCT='):
                        parts = line.split('=')[1].split('/')
                        if len(parts) >= 2:
                            info.vid = int(parts[0], 16)
                            info.pid = int(parts[1], 16)
        except (OSError, ValueError, IndexError):
            pass
        return info
    return None


def _find_macos_device_info() -> DeviceInfo | None:
    """Find Insta360 Link on macOS via ioreg."""
    try:
        out = subprocess.run(
            ['ioreg', '-p', 'IOUSB', '-w', '0', '-l'],
            capture_output=True, text=True, timeout=5)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None

    # Parse ioreg output for Insta360 device
    lines = out.stdout.splitlines()
    in_insta = False
    name = ''
    vid = pid = 0
    for line in lines:
        lower = line.lower()
        if 'insta360' in lower and '+-o' in line:
            in_insta = True
            # Extract device name from +-o line
            match = re.search(r'\+-o\s+(.+?)\s+<', line)
            if match:
                name = match.group(1).strip()
            continue
        if in_insta:
            if '+-o' in line:
                break  # next device
            vid_match = re.search(r'"idVendor"\s*=\s*(\d+)', line)
            if vid_match:
                vid = int(vid_match.group(1))
            pid_match = re.search(r'"idProduct"\s*=\s*(\d+)', line)
            if pid_match:
                pid = int(pid_match.group(1))

    if name or vid:
        return DeviceInfo(platform='macOS', name=name, vid=vid, pid=pid)
    return None


def _find_windows_device_info() -> DeviceInfo | None:
    """Find Insta360 Link on Windows via wmic."""
    try:
        out = subprocess.run(
            ['wmic', 'path', 'Win32_PnPEntity', 'where',
             "Name like '%Insta360%'", 'get', 'Name'],
            capture_output=True, text=True, timeout=10)
        for line in out.stdout.splitlines():
            line = line.strip()
            if line and line != 'Name' and 'insta360' in line.lower():
                return DeviceInfo(platform='Windows', name=line)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return None


# ── High-level camera control ────────────────────────────────────────────────

# Default Extension Unit ID — will be discovered by xu_discover.py.
# This is a placeholder; the real value comes from the USB descriptor.
# UVC unit IDs for the Insta360 Link (discovered via uvc-probe)
UNIT_CT  = 1   # Camera Terminal — autofocus, zoom
UNIT_PU  = 5   # Processing Unit — brightness, contrast, saturation, sharpness, AWB, anti-flicker
UNIT_XU1 = 9   # Extension Unit 1 — AI mode, func-enable bitmask, exposure, pan/tilt, tracking
UNIT_XU2 = 10  # Extension Unit 2 — privacy (physical tilt)

DEFAULT_XU_UNIT_ID = UNIT_XU1


class LinkUSB:
    """High-level Insta360 Link camera control via direct USB XU commands.

    This class wraps an XUBackend and provides named methods for each camera
    feature.  The actual XU data formats are stubs until xu_capture.py
    populates them from real USB captures.

    Usage:
        backend = get_xu_backend()
        cam = LinkUSB(backend)
        cam.connect()
        try:
            cam.set_privacy(True)
        finally:
            cam.close()
    """

    def __init__(self, backend: XUBackend | None = None, unit_id: int = DEFAULT_XU_UNIT_ID):
        self.backend = backend or get_xu_backend()
        self.unit_id = unit_id
        self._connected = False

    def connect(self, device_path: str | None = None) -> None:
        """Open the camera device."""
        self.backend.open(device_path)
        self._connected = True

    def close(self) -> None:
        """Release the camera device."""
        if self._connected:
            self.backend.close()
            self._connected = False

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, *exc):
        self.close()

    # ── Queries ──────────────────────────────────────────────────────────────

    def xu_get_raw(self, selector: int, size: int,
                   query: int = UVC_GET_CUR) -> bytes:
        """Raw XU GET request."""
        return self.backend.xu_get(self.unit_id, selector, size, query)

    def xu_set_raw(self, selector: int, data: bytes) -> None:
        """Raw XU SET_CUR request."""
        self.backend.xu_set(self.unit_id, selector, data)

    def query_control(self, selector: int) -> XUControlInfo:
        """Query a single XU control's capabilities and current value."""
        return self.backend.query_control(self.unit_id, selector)

    def enumerate_controls(self) -> dict[int, XUControlInfo]:
        """Enumerate all known XU controls (0..30)."""
        controls = {}
        for sel in range(XU_MAX_SELECTOR + 1):
            info = self.query_control(sel)
            controls[sel] = info
        return controls

    # ── High-level commands ──────────────────────────────────────────────────
    # Data formats confirmed via xu_capture.py Phase A (uvc-probe server).
    # Controls are spread across multiple UVC units:
    #   Unit 1 (CT)  — autofocus, zoom
    #   Unit 5 (PU)  — brightness, contrast, saturation, sharpness, AWB, anti-flicker
    #   Unit 9 (XU1) — AI mode, func-enable bitmask, exposure, pan/tilt
    #   Unit 10 (XU2) — privacy

    def _xu_get(self, unit: int, selector: int, size: int) -> bytes:
        return self.backend.xu_get(unit, selector, size)

    def _xu_set(self, unit: int, selector: int, data: bytes) -> None:
        self.backend.xu_set(unit, selector, data)

    # ── Func-enable bitmask (unit 9, sel 0x1b) ─────────────────────────────
    # 2-byte LE bitmask controlling multiple features:
    #   bit 2 (0x0004) = HDR
    #   bit 3 (0x0008) = mirror/horizontal flip
    #   bit 4 (0x0010) = gesture zoom
    #   bit 11 (0x0800) = privacy (physical tilt)

    def _read_func_enable(self) -> int:
        """Read the current func-enable bitmask (unit 9, sel 0x1b, 2 bytes LE)."""
        data = self._xu_get(UNIT_XU1, 0x1b, 2)
        return int.from_bytes(data, 'little')

    def _write_func_enable(self, value: int) -> None:
        """Write the func-enable bitmask."""
        self._xu_set(UNIT_XU1, 0x1b, value.to_bytes(2, 'little'))

    def _set_func_bit(self, bit: int, on: bool) -> None:
        """Set or clear a single bit in the func-enable bitmask."""
        cur = self._read_func_enable()
        if on:
            cur |= (1 << bit)
        else:
            cur &= ~(1 << bit)
        self._write_func_enable(cur)

    # ── Queries ─────────────────────────────────────────────────────────────

    def get_device_info(self) -> bytes:
        """Read device info (unit 9, sel 0x03)."""
        length = self.backend.xu_get_len(UNIT_XU1, XU_DEVICE_INFO_CONTROL)
        if length == 0:
            raise RuntimeError('XU_DEVICE_INFO_CONTROL not supported')
        return self._xu_get(UNIT_XU1, XU_DEVICE_INFO_CONTROL, length)

    def get_device_status(self) -> bytes:
        """Read device status (unit 9, sel 0x0b, 5 bytes)."""
        return self._xu_get(UNIT_XU1, 0x0b, 5)

    def get_serial_number(self) -> bytes:
        """Read serial number (unit 9, sel 0x0c)."""
        length = self.backend.xu_get_len(UNIT_XU1, XU_DEVICE_SN_CONTROL)
        if length == 0:
            raise RuntimeError('XU_DEVICE_SN_CONTROL not supported')
        return self._xu_get(UNIT_XU1, XU_DEVICE_SN_CONTROL, length)

    # ── AI mode (unit 9, sel 0x02) ──────────────────────────────────────────
    # Confirmed: 1-byte value.
    #   0=normal, 1=tracking, 4=overhead, 5=deskview, 6=whiteboard

    def set_video_mode(self, mode: int) -> None:
        """Set AI mode. 0=normal, 1=tracking, 4=overhead, 5=deskview, 6=whiteboard."""
        if mode not in (0, 1, 4, 5, 6):
            raise ValueError(f'Invalid mode {mode}: must be 0, 1, 4, 5, or 6')
        self._xu_set(UNIT_XU1, 0x02, bytes([mode]))

    def get_video_mode(self) -> int:
        """Read current AI mode."""
        return self._xu_get(UNIT_XU1, 0x02, 1)[0]

    # ── HDR (func-enable bit 2) ─────────────────────────────────────────────
    # Confirmed: 0x95→0x91 (off), 0x91→0x95 (on) = bit 2 toggle

    def set_hdr(self, on: bool) -> None:
        """Toggle HDR via func-enable bitmask bit 2."""
        self._set_func_bit(2, on)

    def get_hdr(self) -> bool:
        return bool(self._read_func_enable() & (1 << 2))

    # ── Mirror / horizontal flip (func-enable bit 3) ────────────────────────
    # Confirmed: 0x95→0x9d (on), 0x9d→0x95 (off) = bit 3 toggle

    def set_mirror(self, on: bool) -> None:
        """Toggle horizontal flip via func-enable bitmask bit 3."""
        self._set_func_bit(3, on)

    def get_mirror(self) -> bool:
        return bool(self._read_func_enable() & (1 << 3))

    # ── Gesture zoom (func-enable bit 4) ────────────────────────────────────
    # Confirmed: 0x95→0x85 (off) = bit 4 cleared

    def set_gesture_control(self, on: bool) -> None:
        """Toggle gesture zoom via func-enable bitmask bit 4."""
        self._set_func_bit(4, on)

    def get_gesture_control(self) -> bool:
        return bool(self._read_func_enable() & (1 << 4))

    # ── Privacy (unit 10, sel 0x0F + func-enable bit 11) ────────────────────
    # From prior research (unverified via capture — WS joystick causes reboot):
    #   unit 10, sel 0x0F: 1-byte (0x01=on, 0x00=off)
    #   unit 9, sel 0x1b: bit 11 (0x0800)
    # Phase B will verify. For now, write both.

    def set_privacy(self, on: bool) -> None:
        """Toggle privacy mode (physical camera tilt to face-down position)."""
        self._xu_set(UNIT_XU2, 0x0f, bytes([0x01 if on else 0x00]))
        self._set_func_bit(11, on)

    def get_privacy(self) -> bool:
        """Read privacy state from XU2 sel 0x0F."""
        return self._xu_get(UNIT_XU2, 0x0f, 1)[0] != 0

    # ── Brightness (unit 5, sel 0x02) ───────────────────────────────────────
    # Confirmed: 1-byte, 0-100. Standard UVC PU Brightness.

    def set_brightness(self, value: int) -> None:
        """Set brightness 0-100."""
        if not 0 <= value <= 100:
            raise ValueError(f'Brightness must be 0-100, got {value}')
        self._xu_set(UNIT_PU, 0x02, bytes([value]))

    def get_brightness(self) -> int:
        return self._xu_get(UNIT_PU, 0x02, 1)[0]

    # ── Contrast (unit 5, sel 0x03) ─────────────────────────────────────────
    # Confirmed: 1-byte, 0-100. Standard UVC PU Contrast.

    def set_contrast(self, value: int) -> None:
        """Set contrast 0-100."""
        if not 0 <= value <= 100:
            raise ValueError(f'Contrast must be 0-100, got {value}')
        self._xu_set(UNIT_PU, 0x03, bytes([value]))

    def get_contrast(self) -> int:
        return self._xu_get(UNIT_PU, 0x03, 1)[0]

    # ── Saturation (unit 5, sel 0x07) ───────────────────────────────────────
    # Confirmed: 2-byte LE, 0-100.

    def set_saturation(self, value: int) -> None:
        """Set saturation 0-100."""
        if not 0 <= value <= 100:
            raise ValueError(f'Saturation must be 0-100, got {value}')
        self._xu_set(UNIT_PU, 0x07, value.to_bytes(2, 'little'))

    def get_saturation(self) -> int:
        return int.from_bytes(self._xu_get(UNIT_PU, 0x07, 2), 'little')

    # ── Sharpness (unit 5, sel 0x08) ────────────────────────────────────────
    # Confirmed: 2-byte LE, 0-100.

    def set_sharpness(self, value: int) -> None:
        """Set sharpness 0-100."""
        if not 0 <= value <= 100:
            raise ValueError(f'Sharpness must be 0-100, got {value}')
        self._xu_set(UNIT_PU, 0x08, value.to_bytes(2, 'little'))

    def get_sharpness(self) -> int:
        return int.from_bytes(self._xu_get(UNIT_PU, 0x08, 2), 'little')

    # ── Exposure compensation (unit 9, sel 0x09) ────────────────────────────
    # Confirmed: 2-byte LE. 0x0000=0%, 0x2710=100% (value × 100 internal).

    def set_exposure_comp(self, value: int) -> None:
        """Set exposure compensation 0-100."""
        if not 0 <= value <= 100:
            raise ValueError(f'Exposure comp must be 0-100, got {value}')
        internal = value * 100  # 0→0, 100→10000
        self._xu_set(UNIT_XU1, 0x09, internal.to_bytes(2, 'little'))

    def get_exposure_comp(self) -> int:
        raw = int.from_bytes(self._xu_get(UNIT_XU1, 0x09, 2), 'little')
        return raw // 100

    # ── Auto exposure (unit 9, sel 0x1e) ────────────────────────────────────
    # Confirmed: 1-byte. 0x02=auto, 0x01=manual.

    def set_ae_mode(self, auto: bool) -> None:
        """Set auto-exposure mode."""
        self._xu_set(UNIT_XU1, 0x1e, bytes([0x02 if auto else 0x01]))

    def get_ae_mode(self) -> bool:
        """Returns True if auto-exposure is on."""
        return self._xu_get(UNIT_XU1, 0x1e, 1)[0] == 0x02

    # ── Auto white balance (unit 5, sel 0x0b) ──────────────────────────────
    # Confirmed: 1-byte. 0x01=auto, 0x00=manual.

    def set_awb(self, auto: bool) -> None:
        """Set auto white balance."""
        self._xu_set(UNIT_PU, 0x0b, bytes([0x01 if auto else 0x00]))

    def get_awb(self) -> bool:
        return self._xu_get(UNIT_PU, 0x0b, 1)[0] == 0x01

    # ── Anti-flicker (unit 5, sel 0x05) ─────────────────────────────────────
    # Confirmed: 1-byte. 0x03=auto, 0x01=50Hz, 0x02=60Hz.

    def set_anti_flicker(self, mode: int) -> None:
        """Set anti-flicker. 0 or 3=auto, 1=50Hz, 2=60Hz."""
        if mode == 0:
            mode = 3  # normalize 0 → auto
        if mode not in (1, 2, 3):
            raise ValueError(f'Anti-flicker must be 1(50Hz), 2(60Hz), or 3(auto), got {mode}')
        self._xu_set(UNIT_PU, 0x05, bytes([mode]))

    def get_anti_flicker(self) -> int:
        return self._xu_get(UNIT_PU, 0x05, 1)[0]

    # ── Autofocus (unit 1, sel 0x08) ────────────────────────────────────────
    # Confirmed: 1-byte. 0x01=auto, 0x00=manual.

    def set_af_mode(self, auto: bool) -> None:
        """Set autofocus mode."""
        self._xu_set(UNIT_CT, 0x08, bytes([0x01 if auto else 0x00]))

    def get_af_mode(self) -> bool:
        return self._xu_get(UNIT_CT, 0x08, 1)[0] == 0x01

    # ── Zoom (unit 1, sel 0x0b) ─────────────────────────────────────────────
    # Confirmed: 1-byte. 0x64=1x(100). Observed 0x90=144 at 400% zoom.
    # Mapping: linear scale where 100(0x64)=1x, need more data points.

    def set_zoom(self, value: int) -> None:
        """Set zoom level. Value 100-400 (1x-4x).

        Internal mapping approximate — 0x64=100(1x), 0x90=144(4x).
        """
        if not 100 <= value <= 400:
            raise ValueError(f'Zoom must be 100-400, got {value}')
        # Linear map: 100→0x64(100), 400→0x90(144)
        internal = 100 + (value - 100) * (144 - 100) // (400 - 100)
        self._xu_set(UNIT_CT, 0x0b, bytes([internal]))

    def get_zoom(self) -> int:
        raw = self._xu_get(UNIT_CT, 0x0b, 1)[0]
        # Reverse map
        if raw <= 100:
            return 100
        return 100 + (raw - 100) * (400 - 100) // (144 - 100)

    # ── Pan/Tilt absolute (unit 9, sel 0x1a) ────────────────────────────────
    # Confirmed readable: 8-byte LE int32 pair (pan, tilt).
    # Write format unverified — joystick capture showed no XU state change,
    # suggesting movement is firmware-driven. SET_CUR may or may not work.

    def set_pan_tilt_absolute(self, pan: int, tilt: int) -> None:
        """Set absolute pan/tilt position (int32 pair, little-endian).

        WARNING: Write format unverified. The camera may ignore SET_CUR
        on this register if movement is firmware-only.
        """
        data = pan.to_bytes(4, 'little', signed=True) + tilt.to_bytes(4, 'little', signed=True)
        self._xu_set(UNIT_XU1, 0x1a, data)

    def get_pan_tilt_absolute(self) -> tuple[int, int]:
        """Read absolute pan/tilt position. Returns (pan, tilt) as signed int32."""
        data = self._xu_get(UNIT_XU1, 0x1a, 8)
        pan = int.from_bytes(data[0:4], 'little', signed=True)
        tilt = int.from_bytes(data[4:8], 'little', signed=True)
        return pan, tilt

    # ── Raw func-enable access ──────────────────────────────────────────────

    def set_func_enable(self, data: bytes) -> None:
        """Raw func-enable write (unit 9, sel 0x1b)."""
        self._xu_set(UNIT_XU1, 0x1b, data)

    def get_func_enable(self) -> bytes:
        """Raw func-enable read (unit 9, sel 0x1b, 2 bytes)."""
        return self._xu_get(UNIT_XU1, 0x1b, 2)

    # ── Raw replay (for xu_capture.py Phase B) ───────────────────────────────

    def replay_xu_command(self, unit: int, selector: int, data: bytes) -> None:
        """Replay a captured XU SET_CUR command to a specific unit."""
        self._xu_set(unit, selector, data)

    def replay_xu_get(self, unit: int, selector: int, size: int) -> bytes:
        """Replay a captured XU GET_CUR command from a specific unit."""
        return self._xu_get(unit, selector, size)


# ── CLI (for quick testing) ──────────────────────────────────────────────────

def main():
    """Quick device check — not a full CLI, just a smoke test."""
    print(f'Platform: {platform.system()}')

    device = find_insta360_device()
    if device:
        print(f'Camera found: {device.name}')
        print(f'  Path: {device.device_path}')
        if device.vid:
            print(f'  VID:PID: {device.vid:04x}:{device.pid:04x}')
    else:
        print('No Insta360 Link camera found.')
        return

    print('\nAttempting to open device...')
    backend = get_xu_backend()
    try:
        backend.open(device.device_path or None)
        print('Device opened successfully.')
        backend.close()
        print('Device closed.')
    except RuntimeError as e:
        print(f'Failed: {e}')


if __name__ == '__main__':
    main()
