#!/usr/bin/env python3
"""link_usb_linux — UVC control access for Insta360 Link on Linux.

Hybrid backend matching link_usb_macos surface (get/set/_get_handle):

  * Extension units 9/10/11: UVCIOC_CTRL_QUERY on /dev/videoN (no root,
    camera stays available to other apps).
  * Camera Terminal (1) and Processing Unit (5): libusb control transfers.
    Requires udev rule in tools/99-insta360-link.rules (or root).

Public API:
    get(unit, sel, length) -> bytes
    set(unit, sel, data) -> None
    _get_handle() -> LinuxUSBHandle
"""
from __future__ import annotations

import ctypes
import os
import platform
import struct
from ctypes import POINTER, byref, c_int, c_uint, c_uint8, c_uint16, c_void_p
from pathlib import Path

_PROBE = Path(__file__).resolve().parent / 'tools' / 'uvc-probe-linux'

if platform.system() != 'Linux':
    raise ImportError('link_usb_linux requires Linux')

# ── libusb ctypes ────────────────────────────────────────────────────────────

_libusb = ctypes.CDLL('libusb-1.0.so.0')

class LibusbContext(c_void_p):
    pass

class LibusbDeviceHandle(c_void_p):
    pass

_libusb.libusb_init.argtypes = [POINTER(LibusbContext)]
_libusb.libusb_init.restype = c_int
_libusb.libusb_exit.argtypes = [LibusbContext]
_libusb.libusb_exit.restype = None
_libusb.libusb_open.argtypes = [c_void_p, POINTER(LibusbDeviceHandle)]
_libusb.libusb_open.restype = c_int
_libusb.libusb_close.argtypes = [LibusbDeviceHandle]
_libusb.libusb_close.restype = None
_libusb.libusb_get_device_list.argtypes = [LibusbContext, POINTER(POINTER(c_void_p))]
_libusb.libusb_get_device_list.restype = c_int
_libusb.libusb_free_device_list.argtypes = [POINTER(c_void_p), c_int]
_libusb.libusb_free_device_list.restype = None
_libusb.libusb_get_device_descriptor.argtypes = [c_void_p, c_void_p]
_libusb.libusb_get_device_descriptor.restype = c_int
_libusb.libusb_control_transfer.argtypes = [
    LibusbDeviceHandle, c_uint8, c_uint8, c_uint16, c_uint16,
    POINTER(c_uint8), c_uint16, c_uint,
]
_libusb.libusb_control_transfer.restype = c_int
_libusb.libusb_kernel_driver_active.argtypes = [LibusbDeviceHandle, c_int]
_libusb.libusb_kernel_driver_active.restype = c_int
_libusb.libusb_detach_kernel_driver.argtypes = [LibusbDeviceHandle, c_int]
_libusb.libusb_detach_kernel_driver.restype = c_int
_libusb.libusb_attach_kernel_driver.argtypes = [LibusbDeviceHandle, c_int]
_libusb.libusb_attach_kernel_driver.restype = c_int

INSTA360_VID = 0x2E1A
SUPPORTED_PIDS = (0x4C01, 0x4C04, 0x4C02, 0x4C03)
VC_IFACE_NUM = 0
UVC_GET_CUR = 0x81
UVC_SET_CUR = 0x01

XU_UNITS = frozenset({9, 10, 11})

# v4l2 control names for CT/PU when libusb is unavailable
_V4L2_GET_MAP: dict[tuple[int, int, int], str] = {
    (1, 0x0B, 2): 'zoom_absolute',
    (5, 0x02, 1): 'brightness',
    (5, 0x03, 1): 'contrast',
    (5, 0x07, 2): 'saturation',
    (5, 0x08, 2): 'sharpness',
    (5, 0x0A, 2): 'white_balance_temperature',
}

_V4L2_SET_MAP: dict[tuple[int, int], str] = {
    (1, 0x0B): 'zoom_absolute',
    (1, 0x08): 'focus_automatic_continuous',
    (5, 0x02): 'brightness',
    (5, 0x03): 'contrast',
    (5, 0x05): 'power_line_frequency',
    (5, 0x07): 'saturation',
    (5, 0x08): 'sharpness',
    (5, 0x0A): 'white_balance_temperature',
    (5, 0x0B): 'white_balance_temperature_auto',
}


def _probe_ct_pu(op: str, unit: int, sel: int, *,
                 length: int | None = None, data: bytes | None = None) -> bytes | None:
    """Run uvc-probe-linux with transient kernel detach for CT/PU access."""
    import subprocess
    if not _PROBE.is_file():
        return None
    cmd = [str(_PROBE), '--detach', op, str(unit), f'0x{sel:02x}']
    if op == 'get':
        cmd.append(str(length))
    else:
        cmd.append(data.hex() if data else '')
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
    if r.returncode != 0:
        return None
    if op == 'get':
        return bytes.fromhex(r.stdout.strip())
    return b''
    return _find_video_device() or '/dev/video0'


def _v4l2_get_ctrl(name: str) -> int:
    import subprocess
    r = subprocess.run(
        ['v4l2-ctl', '-d', _video_device(), '--get-ctrl', name],
        capture_output=True, text=True, check=False)
    if r.returncode != 0:
        raise RuntimeError(f'v4l2-ctl --get-ctrl {name} failed: {r.stderr.strip()}')
    return int(r.stdout.partition(':')[2].strip())


def _v4l2_set_ctrl(name: str, value: int) -> None:
    import subprocess
    r = subprocess.run(
        ['v4l2-ctl', '-d', _video_device(), '--set-ctrl', f'{name}={value}'],
        capture_output=True, text=True, check=False)
    if r.returncode != 0:
        raise RuntimeError(f'v4l2-ctl --set-ctrl {name}={value} failed: {r.stderr.strip()}')


def _v4l2_get(unit: int, sel: int, length: int) -> bytes | None:
    key = (unit, sel, length)
    if key in _V4L2_GET_MAP:
        val = _v4l2_get_ctrl(_V4L2_GET_MAP[key])
        if length == 1:
            return bytes([val & 0xFF])
        return struct.pack('<H', val & 0xFFFF)
    if unit == 1 and sel == 0x08 and length == 1:
        try:
            val = _v4l2_get_ctrl('focus_automatic_continuous')
        except RuntimeError:
            val = _v4l2_get_ctrl('focus_auto')
        return bytes([1 if val else 0])
    if unit == 5 and sel == 0x0B and length == 1:
        auto = _v4l2_get_ctrl('white_balance_temperature_auto')
        return bytes([1 if auto else 0])
    if unit == 5 and sel == 0x05 and length == 1:
        plf = _v4l2_get_ctrl('power_line_frequency')
        # UVC PU: 3=auto, 1=50Hz, 2=60Hz
        return bytes([plf if plf in (1, 2, 3) else 3])
    return None


def _v4l2_set(unit: int, sel: int, data: bytes) -> bool:
    if unit == 1 and sel == 0x0D and len(data) == 8:
        pan, tilt = struct.unpack('<ii', data)
        _v4l2_set_ctrl('pan_absolute', pan)
        _v4l2_set_ctrl('tilt_absolute', tilt)
        return True
    name = _V4L2_SET_MAP.get((unit, sel))
    if not name:
        return False
    if len(data) == 1:
        val = data[0]
        if (unit, sel) == (5, 0x0B):
            val = 1 if data[0] else 0
        _v4l2_set_ctrl(name, val)
        return True
    if len(data) == 2:
        val = struct.unpack('<H', data)[0]
        _v4l2_set_ctrl(name, val)
        return True
    return False


class _DeviceDescriptor(ctypes.Structure):
    _fields_ = [
        ('bLength', c_uint8), ('bDescriptorType', c_uint8),
        ('bcdUSB', c_uint16), ('bDeviceClass', c_uint8),
        ('bDeviceSubClass', c_uint8), ('bDeviceProtocol', c_uint8),
        ('bMaxPacketSize0', c_uint8), ('idVendor', c_uint16),
        ('idProduct', c_uint16), ('bcdDevice', c_uint16),
        ('iManufacturer', c_uint8), ('iProduct', c_uint8),
        ('iSerialNumber', c_uint8), ('bNumConfigurations', c_uint8),
    ]


# ── ioctl backend for XU units ───────────────────────────────────────────────

def _linux_ioctl_number() -> int:
    ptr_size = ctypes.sizeof(ctypes.c_void_p)
    struct_size = 16 if ptr_size == 8 else 8
    _IOC_NRBITS, _IOC_TYPEBITS, _IOC_SIZEBITS = 8, 8, 14
    _IOC_NRSHIFT, _IOC_TYPESHIFT = 0, 8
    _IOC_SIZESHIFT = _IOC_TYPESHIFT + _IOC_TYPEBITS
    _IOC_DIRSHIFT = _IOC_SIZESHIFT + _IOC_SIZEBITS
    direction = 2 | 1  # _IOWR
    return (
        (direction << _IOC_DIRSHIFT)
        | (ord('u') << _IOC_TYPESHIFT)
        | (0x21 << _IOC_NRSHIFT)
        | (struct_size << _IOC_SIZESHIFT)
    )


class _UvcXuControlQuery(ctypes.Structure):
    _fields_ = [
        ('unit', ctypes.c_uint8),
        ('selector', ctypes.c_uint8),
        ('query', ctypes.c_uint8),
        ('_pad', ctypes.c_uint8),
        ('size', ctypes.c_uint16),
        ('_pad2', ctypes.c_uint16),
        ('data', ctypes.c_void_p),
    ]


def _video_device() -> str:
    return _find_video_device() or '/dev/video0'


def _find_video_device() -> str | None:
    env_device = os.environ.get('LINK_CTL_V4L2_DEVICE')
    if env_device and os.path.exists(env_device):
        return env_device
    sysfs = Path('/sys/class/video4linux')
    if sysfs.is_dir():
        for entry in sorted(sysfs.iterdir()):
            name_file = entry / 'name'
            if not name_file.exists():
                continue
            try:
                if 'insta360' in name_file.read_text().strip().lower():
                    dev = f'/dev/{entry.name}'
                    if os.path.exists(dev):
                        return dev
            except OSError:
                continue
    return '/dev/video0' if os.path.exists('/dev/video0') else None


class _IoctlBackend:
    def __init__(self) -> None:
        self._fd: int | None = None
        self._ioctl_num = _linux_ioctl_number()

    def open(self) -> None:
        import fcntl
        path = _find_video_device()
        if not path:
            raise RuntimeError('No Insta360 /dev/video device found')
        self._fd = os.open(path, os.O_RDWR)

    def close(self) -> None:
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None

    def get(self, unit: int, sel: int, length: int) -> bytes:
        import fcntl
        if self._fd is None:
            raise RuntimeError('ioctl backend not open')
        data_buf = (ctypes.c_uint8 * length)()
        q = _UvcXuControlQuery()
        q.unit = unit
        q.selector = sel
        q.query = UVC_GET_CUR
        q.size = length
        q.data = ctypes.addressof(data_buf)
        fcntl.ioctl(self._fd, self._ioctl_num, q)
        return bytes(data_buf)

    def set(self, unit: int, sel: int, data: bytes) -> None:
        import fcntl
        if self._fd is None:
            raise RuntimeError('ioctl backend not open')
        size = len(data)
        data_buf = (ctypes.c_uint8 * size)(*data)
        q = _UvcXuControlQuery()
        q.unit = unit
        q.selector = sel
        q.query = UVC_SET_CUR
        q.size = size
        q.data = ctypes.addressof(data_buf)
        fcntl.ioctl(self._fd, self._ioctl_num, q)


# ── libusb backend for CT/PU ─────────────────────────────────────────────────

class LinuxUSBHandle:
    """Opaque handle holding libusb context + device."""

    def __init__(self) -> None:
        self.ctx = LibusbContext()
        self.dev = LibusbDeviceHandle()
        self._ioctl = _IoctlBackend()
        self._libusb_ok = False

        if _libusb.libusb_init(byref(self.ctx)) != 0:
            raise RuntimeError('libusb_init failed')

        self._detach = os.environ.get('LINK_CTL_USB_DETACH', '').lower() in ('1', 'true', 'yes')

        desc = _DeviceDescriptor()
        list_pp = POINTER(c_void_p)()
        cnt = _libusb.libusb_get_device_list(self.ctx, byref(list_pp))
        if cnt < 0:
            _libusb.libusb_exit(self.ctx)
            raise RuntimeError('libusb_get_device_list failed')

        try:
            for i in range(cnt):
                dev = list_pp[i]
                if not dev:
                    continue
                if _libusb.libusb_get_device_descriptor(dev, byref(desc)) != 0:
                    continue
                if desc.idVendor != INSTA360_VID or desc.idProduct not in SUPPORTED_PIDS:
                    continue
                if _libusb.libusb_open(dev, byref(self.dev)) == 0:
                    self._libusb_ok = True
                    if self._detach and _libusb.libusb_kernel_driver_active(self.dev, VC_IFACE_NUM) == 1:
                        _libusb.libusb_detach_kernel_driver(self.dev, VC_IFACE_NUM)
                    break
        finally:
            _libusb.libusb_free_device_list(list_pp, 1)

        self._ioctl.open()

        if not self._libusb_ok:
            # XU-only mode still useful for AI/exposure; CT/PU need udev rule.
            pass

    def close(self) -> None:
        self._ioctl.close()
        if self._libusb_ok and self.dev:
            if self._detach and _libusb.libusb_kernel_driver_active(self.dev, VC_IFACE_NUM) == 0:
                _libusb.libusb_attach_kernel_driver(self.dev, VC_IFACE_NUM)
            _libusb.libusb_close(self.dev)
            self.dev = LibusbDeviceHandle()
            self._libusb_ok = False
        if self.ctx:
            _libusb.libusb_exit(self.ctx)
            self.ctx = LibusbContext()

    def _usb_get(self, unit: int, sel: int, length: int) -> bytes:
        if self._libusb_ok:
            buf = (c_uint8 * length)()
            r = _libusb.libusb_control_transfer(
                self.dev, 0xA1, UVC_GET_CUR,
                sel << 8, (unit << 8) | VC_IFACE_NUM,
                buf, length, 1000)
            if r >= 0:
                return bytes(buf[:r])
        fb = _v4l2_get(unit, sel, length)
        if fb is not None:
            return fb
        if unit == 1 and sel == 0x0D and length == 8:
            return self._ioctl.get(9, 0x1A, 8)
        probed = _probe_ct_pu('get', unit, sel, length=length)
        if probed is not None:
            return probed
        raise RuntimeError(f'GET_CUR failed u={unit} s=0x{sel:02x}')

    def _usb_set(self, unit: int, sel: int, data: bytes) -> None:
        if self._libusb_ok:
            buf = (c_uint8 * len(data))(*data)
            r = _libusb.libusb_control_transfer(
                self.dev, 0x21, UVC_SET_CUR,
                sel << 8, (unit << 8) | VC_IFACE_NUM,
                buf, len(data), 1000)
            if r >= 0:
                return
        if _v4l2_set(unit, sel, data):
            return
        if _probe_ct_pu('set', unit, sel, data=data) is not None:
            return
        raise RuntimeError(f'SET_CUR failed u={unit} s=0x{sel:02x}')

    def get(self, unit: int, sel: int, length: int) -> bytes:
        if unit in XU_UNITS:
            return self._ioctl.get(unit, sel, length)
        return self._usb_get(unit, sel, length)

    def set(self, unit: int, sel: int, data: bytes) -> None:
        if unit in XU_UNITS:
            self._ioctl.set(unit, sel, data)
        else:
            self._usb_set(unit, sel, data)


_handle: LinuxUSBHandle | None = None


def _get_handle() -> LinuxUSBHandle:
    global _handle
    if _handle is None:
        _handle = LinuxUSBHandle()
    return _handle


def get(unit: int, sel: int, length: int) -> bytes:
    return _get_handle().get(unit, sel, length)


def set(unit: int, sel: int, data: bytes) -> None:
    _get_handle().set(unit, sel, data)


if __name__ == '__main__':
    import sys
    h = None
    try:
        h = _get_handle()
        mask = h.get(9, 0x1B, 2)
        print(f'func_enable @ 9/0x1B: {mask.hex()}')
        pt = h.get(9, 0x1A, 8)
        tilt, pan = struct.unpack('<ii', pt)
        print(f'pan/tilt @ 9/0x1A: pan={pan} tilt={tilt}')
        if h._libusb_ok:
            try:
                zoom = struct.unpack('<H', h.get(1, 0x0B, 2))[0]
                print(f'zoom @ 1/0x0B: {zoom}')
            except Exception as e:
                print(f'zoom read failed: {e}')
        else:
            print('libusb unavailable — using ioctl+v4l2 hybrid')
    except Exception as e:
        print(f'FAIL: {e}', file=sys.stderr)
        sys.exit(1)
    finally:
        if h is not None:
            h.close()
            globals()['_handle'] = None
