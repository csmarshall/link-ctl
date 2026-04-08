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

        # Find Insta360 Link device
        # VID:PID unknown — search by name (vid=0, pid=0 means any device)
        # We try finding any device first, then filter by name if needed
        ret = self._lib.uvc_find_device(
            self._ctx, ctypes.byref(self._dev), 0, 0, None)
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


def _find_macos_camera() -> bool:
    """Check if an Insta360 Link is connected on macOS."""
    try:
        out = subprocess.run(
            ['ioreg', '-p', 'IOUSB', '-w', '0'],
            capture_output=True, text=True, timeout=5)
        return 'insta360' in out.stdout.lower()
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False



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
DEFAULT_XU_UNIT_ID = 3  # Common default for UVC extension units


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
    # Data formats are STUBS — they need to be populated from USB captures.
    # Each method documents the expected XU selector and what xu_capture.py
    # needs to fill in.

    def get_device_info(self) -> bytes:
        """Read device info via XU_DEVICE_INFO_CONTROL (selector 3).

        Returns raw bytes; format TBD from USB capture.
        """
        length = self.backend.xu_get_len(self.unit_id, XU_DEVICE_INFO_CONTROL)
        if length == 0:
            raise RuntimeError('XU_DEVICE_INFO_CONTROL not supported or zero length')
        return self.xu_get_raw(XU_DEVICE_INFO_CONTROL, length)

    def get_device_status(self) -> bytes:
        """Read device status via XU_DEVICE_STATUS_CONTROL (selector 11)."""
        length = self.backend.xu_get_len(self.unit_id, XU_DEVICE_STATUS_CONTROL)
        if length == 0:
            raise RuntimeError('XU_DEVICE_STATUS_CONTROL not supported')
        return self.xu_get_raw(XU_DEVICE_STATUS_CONTROL, length)

    def get_serial_number(self) -> bytes:
        """Read serial number via XU_DEVICE_SN_CONTROL (selector 12)."""
        length = self.backend.xu_get_len(self.unit_id, XU_DEVICE_SN_CONTROL)
        if length == 0:
            raise RuntimeError('XU_DEVICE_SN_CONTROL not supported')
        return self.xu_get_raw(XU_DEVICE_SN_CONTROL, length)

    def set_privacy(self, on: bool) -> None:
        """Toggle privacy mode (sensor disable) via XU_FUNC_ENABLE_CONTROL (27).

        STUB: The actual data format is unknown.  xu_capture.py will determine
        the correct bytes by capturing the desktop app toggling privacy mode
        via AppleScript.

        Best guess: a single byte or int32 flag (0=off, 1=on), but this MUST
        be confirmed from a USB capture before use.
        """
        # TODO: Replace with actual data format from xu_capture.py
        raise NotImplementedError(
            'Privacy mode data format unknown.\n'
            'Run tools/xu_capture.py to capture the desktop app toggling privacy,\n'
            'then update this method with the discovered XU data bytes.')

    def set_video_mode(self, mode: int) -> None:
        """Set AI mode via XU_VIDEO_MODE_CONTROL (selector 2).

        Mode values (from proto VideoModeType enum):
          0 = Normal, 1 = Auto Composition, 2 = Tracking,
          4 = Whiteboard, 5 = Overhead, 6 = DeskView

        STUB: Data format unknown — likely a single int32 or byte.
        """
        raise NotImplementedError(
            'Video mode XU data format unknown.\n'
            'Run tools/xu_capture.py to discover the correct bytes.')

    def set_zoom(self, value: int) -> None:
        """Set zoom level via XU_PTZ_CMD_CONTROL (selector 4).

        Value: 100-400.
        STUB: The PTZ command format likely packs zoom into a multi-field
        struct alongside pan/tilt.  Needs USB capture to decode.
        """
        raise NotImplementedError(
            'PTZ XU data format unknown.\n'
            'Run tools/xu_capture.py to discover the correct bytes.')

    def set_pan_tilt_absolute(self, pan: int, tilt: int) -> None:
        """Set absolute pan/tilt via XU_PANTILT_ABSOLUTE_CONTROL (selector 26).

        This is a NEW capability not available via the WebSocket API
        (which only supports velocity-based joystick movement).
        STUB: Data format unknown.
        """
        raise NotImplementedError(
            'Absolute pan/tilt XU data format unknown.\n'
            'Run tools/xu_capture.py to discover the correct bytes.')

    def set_pan_tilt_relative(self, pan: int, tilt: int) -> None:
        """Set relative pan/tilt via XU_PANTILT_RELATIVE_CONTROL (selector 22)."""
        raise NotImplementedError(
            'Relative pan/tilt XU data format unknown.\n'
            'Run tools/xu_capture.py to discover the correct bytes.')

    def set_hdr(self, on: bool) -> None:
        """Toggle HDR.  Selector TBD (not directly mapped in proto ControlSelector)."""
        raise NotImplementedError(
            'HDR XU selector/data format unknown.\n'
            'Run tools/xu_capture.py to discover the mapping.')

    def set_exposure_value(self, value: int) -> None:
        """Set exposure via XU_EXPOSURE_VALUE_CONTROL (selector 9)."""
        raise NotImplementedError('Exposure XU data format unknown.')

    def set_ae_mode(self, auto: bool) -> None:
        """Set auto-exposure mode via XU_AE_MODE_CONTROL (selector 30)."""
        raise NotImplementedError('AE mode XU data format unknown.')

    def set_exposure_time(self, value: int) -> None:
        """Set shutter speed via XU_EXPOSURE_TIME_ABSOLUTE_CONTROL (selector 29)."""
        raise NotImplementedError('Exposure time XU data format unknown.')

    def set_af_mode(self, auto: bool) -> None:
        """Set auto-focus mode via XU_AF_MODE_OR_DOWNLOAD_FILE (selector 15)."""
        raise NotImplementedError('AF mode XU data format unknown.')

    def set_iso(self, value: int) -> None:
        """Set ISO via XU_ISO_CONTROL (selector 25)."""
        raise NotImplementedError('ISO XU data format unknown.')

    def set_bias(self, value: int) -> None:
        """Set image bias via XU_BIAS_CONTROL (selector 24)."""
        raise NotImplementedError('Bias XU data format unknown.')

    def set_track_speed(self, value: int) -> None:
        """Set tracking speed via XU_TRACK_SPEED_CONTROL (selector 18)."""
        raise NotImplementedError('Track speed XU data format unknown.')

    def set_noise_cancel(self, on: bool) -> None:
        """Toggle noise cancellation via XU_NOISE_CANCEL_CONTROL (selector 7)."""
        raise NotImplementedError('Noise cancel XU data format unknown.')

    def set_gesture_control(self, on: bool) -> None:
        """Toggle gesture control via XU_GESTURE_STATUS_CONTROL (selector 5)."""
        raise NotImplementedError('Gesture control XU data format unknown.')

    def set_video_resolution(self, value: int) -> None:
        """Set video resolution via XU_VIDEO_RES_CONTROL (selector 28)."""
        raise NotImplementedError('Video resolution XU data format unknown.')

    def set_usb_mode(self, value: int) -> None:
        """Switch USB mode via XU_USB_MODE_SWITCH_CONTROL (selector 17)."""
        raise NotImplementedError('USB mode XU data format unknown.')

    def set_layout_style(self, value: int) -> None:
        """Set layout style via XU_LAYOUT_STYLE_CONTROL (selector 19)."""
        raise NotImplementedError('Layout style XU data format unknown.')

    def set_func_enable(self, data: bytes) -> None:
        """Raw func-enable control via XU_FUNC_ENABLE_CONTROL (selector 27).

        This is likely the privacy/feature-enable selector.  Use xu_capture.py
        to determine the data format, then call this with the correct bytes.
        """
        self.xu_set_raw(XU_FUNC_ENABLE_CONTROL, data)

    # ── Raw replay (for xu_capture.py Phase B) ───────────────────────────────

    def replay_xu_command(self, selector: int, data: bytes) -> None:
        """Replay a captured XU SET_CUR command (used by xu_capture.py Phase B)."""
        self.xu_set_raw(selector, data)

    def replay_xu_get(self, selector: int, size: int) -> bytes:
        """Replay a captured XU GET_CUR command."""
        return self.xu_get_raw(selector, size)


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
