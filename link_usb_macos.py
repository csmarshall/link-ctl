#!/usr/bin/env python3
"""link_usb_macos — in-process IOKit UVC Extension Unit access for the
Insta360 Link, replacing the subprocess call to tools/uvc-probe.

Uses ctypes bindings to Apple's IOKit and CoreFoundation frameworks. Same
"never open the interface" approach as tools/uvc-probe.m: no sudo, no
com.apple.security.device.usb entitlement, no codesigning required. The
technique is lifted from jtfrey/uvc-util.

Design notes:
  * IOCreatePlugInInterfaceForService returns an IOCFPlugInInterface** —
    pointer to pointer to vtable struct. To call a method you pass the
    `**` as the `self` arg and read the function pointer from the vtable.
  * After calling the plugin's QueryInterface(kIOUSBInterfaceInterfaceID)
    we get an IOUSBInterfaceInterface** whose vtable is laid out per
    IOKit/usb/IOUSBLib.h — ControlRequest sits at slot 23 (stable across
    every InterfaceInterface revision because new methods are appended).
  * CFUUIDBytes is passed by value (16 bytes). ctypes handles this when
    the Structure is declared with the right fields.

Public API (used by link_ctl.py):
    open_vc_interface()             # returns an opaque handle
    close_vc_interface(handle)
    control_request(handle, bmRequestType, bRequest, wValue, wIndex, data)
        → (status_ok, bytes_read_or_empty)

The higher-level get/set helpers from link_ctl.py (_uvc_get, _uvc_set)
will be rewritten to use this when running on macOS.
"""
from __future__ import annotations

import ctypes
import platform
import struct
from ctypes import (
    CFUNCTYPE, POINTER, Structure, byref, cast, sizeof,
    c_char_p, c_int, c_int32, c_uint, c_uint8, c_uint16, c_uint32, c_void_p,
)

# ── Framework loading ────────────────────────────────────────────────────────

if platform.system() != 'Darwin':
    raise ImportError('link_usb_macos requires macOS')

_iokit = ctypes.CDLL('/System/Library/Frameworks/IOKit.framework/IOKit')
_cf    = ctypes.CDLL('/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation')

# ── Type aliases ─────────────────────────────────────────────────────────────

# CoreFoundation
CFAllocatorRef         = c_void_p
CFStringRef            = c_void_p
CFDictionaryRef        = c_void_p
CFMutableDictionaryRef = c_void_p
CFTypeRef              = c_void_p
CFNumberRef            = c_void_p
CFUUIDRef              = c_void_p

# IOKit
mach_port_t         = c_uint
io_object_t         = c_uint
io_service_t        = c_uint
io_iterator_t       = c_uint
io_registry_entry_t = c_uint
IOReturn            = c_int32
kern_return_t       = c_int32
IOOptionBits        = c_uint32
SInt32              = c_int32

# Constants
kIOReturnSuccess           = 0
kIOReturnExclusiveAccess   = 0xE00002C5 - 0x100000000  # signed view
kCFNumberSInt32Type        = 3
kCFStringEncodingASCII     = 0x0600
kCFAllocatorDefault        = None
kIOMasterPortDefault       = 0

# ── CFUUIDBytes — 16 bytes passed BY VALUE to QueryInterface ─────────────────

class CFUUIDBytes(Structure):
    _fields_ = [(f'byte{i:02d}', c_uint8) for i in range(16)]

# ── IOUSBDevRequest — 22 bytes (+ 2 bytes padding on 64-bit) ─────────────────

class IOUSBDevRequest(Structure):
    _fields_ = [
        ('bmRequestType', c_uint8),
        ('bRequest',      c_uint8),
        ('wValue',        c_uint16),
        ('wIndex',        c_uint16),
        ('wLength',       c_uint16),
        ('pData',         c_void_p),
        ('wLenDone',      c_uint32),
    ]

# ── Function prototypes ──────────────────────────────────────────────────────

# CoreFoundation
_cf.CFUUIDGetConstantUUIDWithBytes.restype  = CFUUIDRef
_cf.CFUUIDGetConstantUUIDWithBytes.argtypes = [
    CFAllocatorRef,
    c_uint8, c_uint8, c_uint8, c_uint8,
    c_uint8, c_uint8, c_uint8, c_uint8,
    c_uint8, c_uint8, c_uint8, c_uint8,
    c_uint8, c_uint8, c_uint8, c_uint8,
]
_cf.CFUUIDGetUUIDBytes.restype  = CFUUIDBytes
_cf.CFUUIDGetUUIDBytes.argtypes = [CFUUIDRef]

_cf.CFNumberCreate.restype  = CFNumberRef
_cf.CFNumberCreate.argtypes = [CFAllocatorRef, c_int, c_void_p]

_cf.CFStringCreateWithCString.restype  = CFStringRef
_cf.CFStringCreateWithCString.argtypes = [CFAllocatorRef, c_char_p, c_uint32]

_cf.CFDictionarySetValue.restype  = None
_cf.CFDictionarySetValue.argtypes = [CFMutableDictionaryRef, c_void_p, c_void_p]

_cf.CFNumberGetValue.restype  = c_uint8   # bool
_cf.CFNumberGetValue.argtypes = [CFNumberRef, c_int, c_void_p]

_cf.CFRelease.restype  = None
_cf.CFRelease.argtypes = [CFTypeRef]

_iokit.IORegistryEntryCreateCFProperty.restype  = CFTypeRef
_iokit.IORegistryEntryCreateCFProperty.argtypes = [
    io_registry_entry_t, CFStringRef, CFAllocatorRef, IOOptionBits,
]

# IOKit
_iokit.IOServiceMatching.restype  = CFMutableDictionaryRef
_iokit.IOServiceMatching.argtypes = [c_char_p]

_iokit.IOServiceGetMatchingServices.restype  = kern_return_t
_iokit.IOServiceGetMatchingServices.argtypes = [mach_port_t, CFDictionaryRef, POINTER(io_iterator_t)]

_iokit.IOIteratorNext.restype  = io_service_t
_iokit.IOIteratorNext.argtypes = [io_iterator_t]

_iokit.IOObjectRelease.restype  = kern_return_t
_iokit.IOObjectRelease.argtypes = [io_object_t]

_iokit.IOCreatePlugInInterfaceForService.restype  = kern_return_t
_iokit.IOCreatePlugInInterfaceForService.argtypes = [
    io_service_t, CFUUIDRef, CFUUIDRef,
    POINTER(c_void_p),   # IOCFPlugInInterface *** (we treat as void**)
    POINTER(SInt32),
]

# ── UUIDs ────────────────────────────────────────────────────────────────────

def _uuid(*b: int) -> CFUUIDRef:
    return _cf.CFUUIDGetConstantUUIDWithBytes(kCFAllocatorDefault, *b)

# kIOCFPlugInInterfaceID = C244E858-109C-11D4-91D4-0050E4C6426F
kIOCFPlugInInterfaceID = _uuid(
    0xC2, 0x44, 0xE8, 0x58, 0x10, 0x9C, 0x11, 0xD4,
    0x91, 0xD4, 0x00, 0x50, 0xE4, 0xC6, 0x42, 0x6F)

# kIOUSBInterfaceUserClientTypeID = 2D9786C6-9EF3-11D4-AD51-000A27052861
kIOUSBInterfaceUserClientTypeID = _uuid(
    0x2D, 0x97, 0x86, 0xC6, 0x9E, 0xF3, 0x11, 0xD4,
    0xAD, 0x51, 0x00, 0x0A, 0x27, 0x05, 0x28, 0x61)

# kIOUSBInterfaceInterfaceID = 73C97AE8-9EF3-11D4-B1D0-000A27052861 (base)
kIOUSBInterfaceInterfaceID = _uuid(
    0x73, 0xC9, 0x7A, 0xE8, 0x9E, 0xF3, 0x11, 0xD4,
    0xB1, 0xD0, 0x00, 0x0A, 0x27, 0x05, 0x28, 0x61)

# ── vtable function pointer prototypes ──────────────────────────────────────

# Plugin's QueryInterface at slot 0 of IOCFPlugInInterface vtable.
#   IOReturn (*)(void *self, CFUUIDBytes iid, void **ppv)
QueryInterfaceFn = CFUNCTYPE(IOReturn, c_void_p, CFUUIDBytes, POINTER(c_void_p))

# IOUSBInterfaceInterface::ControlRequest at slot 23.
#   IOReturn (*)(void *self, UInt8 pipeRef, IOUSBDevRequest *req)
ControlRequestFn = CFUNCTYPE(IOReturn, c_void_p, c_uint8, POINTER(IOUSBDevRequest))

# Generic Release at slot 2 of any COM-style IUnknown vtable.
#   UInt32 (*)(void *self)
ReleaseFn = CFUNCTYPE(c_uint32, c_void_p)

# IOKit COM-style vtable layout: every interface starts with IUNKNOWN_C_GUTS
# which is (_reserved, QueryInterface, AddRef, Release) — 4 slots before the
# interface's own methods. Our slot constants count from the raw vtable base.
SLOT_QUERY_INTERFACE = 1
SLOT_ADD_REF         = 2
SLOT_RELEASE         = 3
# ControlRequest is the 21st method in IOUSBInterfaceStruct100 (and stable
# across later versions since new methods are appended), which puts it at
# offset 4 + 20 = 24.
SLOT_CONTROL_REQUEST = 24
PTR_SIZE = sizeof(c_void_p)

# ── Helpers ──────────────────────────────────────────────────────────────────

def _cfstr(s: str) -> CFStringRef:
    return _cf.CFStringCreateWithCString(kCFAllocatorDefault, s.encode('ascii'),
                                         kCFStringEncodingASCII)

def _cfnum_sint32(v: int) -> CFNumberRef:
    buf = c_int32(v)
    return _cf.CFNumberCreate(kCFAllocatorDefault, kCFNumberSInt32Type, byref(buf))

def _vtable_slot(handle_pp: int, slot: int) -> int:
    """Given the integer address of an IOKit interface handle (a pointer to
    pointer to vtable), return the function-pointer address at vtable slot N."""
    # *handle = vtable base address
    vtable_base = cast(handle_pp, POINTER(c_void_p))[0]
    # Read slot N (each slot is one pointer-sized word)
    slot_ptr_addr = vtable_base + slot * PTR_SIZE
    return cast(slot_ptr_addr, POINTER(c_void_p))[0]


# ── Public API ───────────────────────────────────────────────────────────────

class VCInterface:
    """Opaque handle to the Insta360 Link's VideoControl interface (iface 0).
    Holds the IOUSBInterfaceInterface** pointer plus its plugin so we can
    release both on close."""
    __slots__ = ('iface_pp', 'plugin_pp')

    def __init__(self, iface_pp: int, plugin_pp: int):
        self.iface_pp  = iface_pp   # IOUSBInterfaceInterface**
        self.plugin_pp = plugin_pp  # IOCFPlugInInterface**

    def close(self):
        """Call Release on interface + plugin (vtable slot 3)."""
        for handle in (self.iface_pp, self.plugin_pp):
            if not handle:
                continue
            try:
                fn = cast(_vtable_slot(handle, SLOT_RELEASE), ReleaseFn)
                fn(c_void_p(handle))
            except Exception:
                pass
        self.iface_pp = 0
        self.plugin_pp = 0


def _read_int_prop(service: int, key: str) -> int | None:
    """Read a numeric registry property (e.g. idVendor) from an IOKit service
    and return its int value, or None if missing / not a number."""
    cfkey = _cfstr(key)
    try:
        cfval = _iokit.IORegistryEntryCreateCFProperty(service, cfkey,
                                                       kCFAllocatorDefault, 0)
    finally:
        _cf.CFRelease(cfkey)
    if not cfval:
        return None
    out = c_int32(0)
    ok = _cf.CFNumberGetValue(cfval, kCFNumberSInt32Type, byref(out))
    _cf.CFRelease(cfval)
    return out.value if ok else None


def open_vc_interface(vid: int = 0x2e1a, pid: int = 0x4c01,
                      iface_num: int = 0) -> VCInterface | None:
    """Find the Insta360 Link's video-control interface and return a handle
    we can pass to control_request(). Returns None if not found.

    Enumerates all IOUSBHostInterface services, reads idVendor/idProduct/
    bInterfaceNumber off each, and picks the first match. (Mirrors the
    pattern in tools/uvc-probe.m — embedding the filters in the matching
    dictionary doesn't reliably work across macOS versions.)
    """
    match = _iokit.IOServiceMatching(b'IOUSBHostInterface')
    if not match:
        return None

    iterator = io_iterator_t()
    rc = _iokit.IOServiceGetMatchingServices(kIOMasterPortDefault, match, byref(iterator))
    if rc != kIOReturnSuccess:
        return None

    service = None
    try:
        while True:
            svc = _iokit.IOIteratorNext(iterator)
            if not svc:
                break
            v = _read_int_prop(svc, 'idVendor')
            p = _read_int_prop(svc, 'idProduct')
            n = _read_int_prop(svc, 'bInterfaceNumber')
            if v == vid and p == pid and n == iface_num:
                service = svc
                break
            _iokit.IOObjectRelease(svc)
    finally:
        _iokit.IOObjectRelease(iterator)

    if service is None:
        return None

    # Create plugin interface, then upgrade via QueryInterface.
    plugin = c_void_p()
    score  = SInt32()
    rc = _iokit.IOCreatePlugInInterfaceForService(
        service,
        kIOUSBInterfaceUserClientTypeID,
        kIOCFPlugInInterfaceID,
        byref(plugin),
        byref(score),
    )
    _iokit.IOObjectRelease(service)
    if rc != kIOReturnSuccess or not plugin.value:
        return None

    qi_fn = cast(_vtable_slot(plugin.value, SLOT_QUERY_INTERFACE), QueryInterfaceFn)
    iid = _cf.CFUUIDGetUUIDBytes(kIOUSBInterfaceInterfaceID)
    iface = c_void_p()
    hr = qi_fn(c_void_p(plugin.value), iid, byref(iface))
    if hr != 0 or not iface.value:
        release_fn = cast(_vtable_slot(plugin.value, SLOT_RELEASE), ReleaseFn)
        release_fn(c_void_p(plugin.value))
        return None

    return VCInterface(iface_pp=iface.value, plugin_pp=plugin.value)


def control_request(handle: VCInterface, bm_request_type: int, b_request: int,
                    w_value: int, w_index: int,
                    data: bytes | bytearray = b'',
                    read_length: int = 0) -> tuple[bool, bytes]:
    """Issue a UVC control transfer on the plugin's ControlRequest slot.

    Returns (ok, bytes). For OUT transfers `data` is the payload and the
    returned bytes are empty. For IN transfers pass read_length and the
    returned bytes are the received payload.
    """
    if read_length and data:
        raise ValueError('control_request: pass either data (OUT) or read_length (IN), not both')

    length = read_length if read_length else len(data)
    buf = ctypes.create_string_buffer(max(length, 1))
    if data:
        ctypes.memmove(buf, bytes(data), len(data))

    req = IOUSBDevRequest(
        bmRequestType = bm_request_type,
        bRequest      = b_request,
        wValue        = w_value,
        wIndex        = w_index,
        wLength       = length,
        pData         = ctypes.addressof(buf),
        wLenDone      = 0,
    )
    cr_fn = cast(_vtable_slot(handle.iface_pp, SLOT_CONTROL_REQUEST), ControlRequestFn)
    rc = cr_fn(c_void_p(handle.iface_pp), 0, byref(req))
    if rc != kIOReturnSuccess:
        return (False, b'')
    if read_length:
        return (True, bytes(buf.raw[:req.wLenDone or read_length]))
    return (True, b'')


# ── Convenience wrappers matching uvc-probe's surface ────────────────────────

_IFACE_NUM = 0
_UVC_GET_CUR = 0x81
_UVC_SET_CUR = 0x01

def ctrl_get(handle: VCInterface, unit: int, sel: int, length: int) -> bytes:
    """UVC GET_CUR on `unit`/`sel` — returns `length` bytes."""
    bmRT  = 0xA1
    wVal  = sel << 8
    wIdx  = (unit << 8) | _IFACE_NUM
    ok, data = control_request(handle, bmRT, _UVC_GET_CUR, wVal, wIdx,
                               read_length=length)
    if not ok:
        raise RuntimeError(f'UVC GET_CUR failed u={unit} s=0x{sel:02x} len={length}')
    return data

def ctrl_set(handle: VCInterface, unit: int, sel: int, data: bytes) -> None:
    """UVC SET_CUR on `unit`/`sel` with `data` payload."""
    bmRT  = 0x21
    wVal  = sel << 8
    wIdx  = (unit << 8) | _IFACE_NUM
    ok, _ = control_request(handle, bmRT, _UVC_SET_CUR, wVal, wIdx, data=data)
    if not ok:
        raise RuntimeError(f'UVC SET_CUR failed u={unit} s=0x{sel:02x} '
                           f'len={len(data)} data={data.hex()}')


# ── Module-level convenience (auto-manage a singleton handle) ────────────────

_handle: VCInterface | None = None

def _get_handle() -> VCInterface:
    global _handle
    if _handle is None:
        _handle = open_vc_interface()
        if _handle is None:
            raise RuntimeError('Insta360 Link not found via IOKit')
    return _handle

def get(unit: int, sel: int, length: int) -> bytes:
    return ctrl_get(_get_handle(), unit, sel, length)

def set(unit: int, sel: int, data: bytes) -> None:
    ctrl_set(_get_handle(), unit, sel, data)


if __name__ == '__main__':
    # Smoke test: read func-enable bitmask and pan/tilt readback.
    import sys
    h = open_vc_interface()
    if h is None:
        print('FAIL: no camera found', file=sys.stderr); sys.exit(1)
    try:
        mask = ctrl_get(h, 9, 0x1B, 2)
        print(f'func_enable bitmask @ 9/0x1B: {mask.hex()}')
        pt = ctrl_get(h, 9, 0x1A, 8)
        tilt, pan = struct.unpack('<ii', pt)
        print(f'pan/tilt @ 9/0x1A: pan={pan} tilt={tilt}')
    finally:
        h.close()
