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


def _detach_allowed() -> bool:
    """True when kernel driver detach is explicitly opted in (PTZ/center only).

    Default is False — detach/rebind cycles hang the Link 2 when run in loops.
    Set LINK_CTL_USB_DETACH=1 or pass ``link-ctl --detach`` for pan/tilt SET.
    """
    if os.environ.get('LINK_CTL_NO_DETACH', '').lower() in ('1', 'true', 'yes'):
        return False
    return os.environ.get('LINK_CTL_USB_DETACH', '').lower() in ('1', 'true', 'yes')


if platform.system() != 'Linux':
    raise ImportError('link_usb_linux requires Linux')

INSTA360_VID = 0x2E1A


def _probe_xu_set(unit: int, sel: int, data: bytes) -> bool:
    """Last-resort XU SET via uvc-probe-linux; detach only when explicitly allowed."""
    import subprocess
    if not _PROBE.is_file():
        return False
    cmd = [str(_PROBE)]
    if _detach_allowed():
        cmd.append('--detach')
    cmd.extend(['set', str(unit), f'0x{sel:02x}', data.hex()])
    r = subprocess.run(
        cmd,
        capture_output=True, text=True, timeout=10)
    if r.returncode != 0:
        return False
    try:
        _wait_for_video_device(timeout=8.0)
    except RuntimeError:
        pass
    return True


def _bootstrap_uvcvideo() -> bool:
    """Attach uvcvideo via libusb when capture nodes are missing (post-detach)."""
    ctx = LibusbContext()
    if _libusb.libusb_init(byref(ctx)) != 0:
        return False
    attached = False
    list_pp = POINTER(c_void_p)()
    desc = _DeviceDescriptor()
    cnt = -1
    try:
        cnt = _libusb.libusb_get_device_list(ctx, byref(list_pp))
        if cnt < 0:
            return False
        for i in range(cnt):
            dev = list_pp[i]
            if not dev:
                continue
            if _libusb.libusb_get_device_descriptor(dev, byref(desc)) != 0:
                continue
            if desc.idVendor != INSTA360_VID or desc.idProduct not in SUPPORTED_PIDS:
                continue
            handle = LibusbDeviceHandle()
            if _libusb.libusb_open(dev, byref(handle)) != 0:
                continue
            try:
                # Broken partial bind (If0=uvcvideo, If1=none, no /dev/video): cycle both.
                if _find_video_device() is None:
                    for iface in (VC_IFACE_NUM, 1):
                        if _libusb.libusb_kernel_driver_active(handle, iface) == 1:
                            _libusb.libusb_detach_kernel_driver(handle, iface)
                for iface in (VC_IFACE_NUM, 1):
                    if _libusb.libusb_kernel_driver_active(handle, iface) == 0:
                        if _libusb.libusb_attach_kernel_driver(handle, iface) == 0:
                            attached = True
            finally:
                _libusb.libusb_close(handle)
            break
    finally:
        if cnt >= 0:
            _libusb.libusb_free_device_list(list_pp, 1)
        _libusb.libusb_exit(ctx)
    return attached


def _rebind_uvcvideo(*, unbind_first: bool = False) -> None:
    """Re-attach uvcvideo via sysfs bind (requires root; no-op when permission denied)."""
    for ok, _detail in _rebind_uvcvideo_sysfs(unbind_first=unbind_first):
        if not ok:
            pass


def _libusb_reattach_vc(handle: 'LinuxUSBHandle | None') -> None:
    """Re-bind uvcvideo through libusb after a transient CT/PU detach."""
    if handle is None or not handle._libusb_ok or not handle.dev:
        return
    for iface in (VC_IFACE_NUM, 1):
        if _libusb.libusb_kernel_driver_active(handle.dev, iface) == 0:
            _libusb.libusb_attach_kernel_driver(handle.dev, iface)


def _wait_for_video_device(timeout: float = 12.0,
                           handle: 'LinuxUSBHandle | None' = None) -> str:
    """Block until an Insta360 capture node is openable (post-detach recovery)."""
    import time
    global _video_dev_cache
    _video_dev_cache = None
    deadline = time.monotonic() + timeout
    last_err = 'device not found'
    while time.monotonic() < deadline:
        _bootstrap_uvcvideo()
        _libusb_reattach_vc(handle)
        _rebind_uvcvideo()
        dev = _find_video_device()
        if dev and os.path.exists(dev):
            try:
                fd = os.open(dev, os.O_RDWR)
                os.close(fd)
                return dev
            except OSError as e:
                last_err = str(e)
        time.sleep(0.3)
    raise RuntimeError(f'No Insta360 /dev/video device found ({last_err})')


def _after_ct_detach(handle: 'LinuxUSBHandle | None' = None) -> None:
    """Re-bind uvcvideo and reopen ioctl after CT/PU libusb detach."""
    global _video_dev_cache, _handle
    _video_dev_cache = None

    if handle is not None and handle._libusb_ok and handle.dev:
        for iface in (VC_IFACE_NUM, 1):
            if _libusb.libusb_kernel_driver_active(handle.dev, iface) == 0:
                _libusb.libusb_attach_kernel_driver(handle.dev, iface)

    try:
        path = _wait_for_video_device(handle=handle, timeout=6.0)
    except RuntimeError:
        # An open libusb handle with a detached iface blocks bootstrap; drop it.
        if handle is not None:
            handle._ioctl.close()
            if handle._libusb_ok and handle.dev:
                for iface in (VC_IFACE_NUM, 1):
                    if _libusb.libusb_kernel_driver_active(handle.dev, iface) == 0:
                        _libusb.libusb_attach_kernel_driver(handle.dev, iface)
                _libusb.libusb_close(handle.dev)
                handle.dev = LibusbDeviceHandle()
                handle._libusb_ok = False
        if _handle is handle:
            _handle = None
        _bootstrap_uvcvideo()
        path = _wait_for_video_device(timeout=10.0)

    if handle is not None:
        if handle._ioctl._fd is not None:
            try:
                handle._ioctl.close()
            except Exception:
                pass
        handle._ioctl.open()
    try:
        import link_ctl
        link_ctl.reset_usb_caches()
    except Exception:
        pass
    if handle is not None and handle._ioctl._fd is None:
        raise RuntimeError(f'No Insta360 /dev/video device found (reopen failed for {path})')


def recover() -> None:
    """Rebind uvcvideo and reset cached handles after a CT/PU detach cycle."""
    global _handle
    h = _handle
    try:
        _after_ct_detach(h)
    except RuntimeError:
        _bootstrap_uvcvideo()
        _wait_for_video_device()
        _invalidate_handle()
    try:
        import link_ctl
        link_ctl.reset_usb_caches()
    except Exception:
        pass


def recover_ai_mode_stuck(*, verbose: bool = False) -> bool:
    """Recover when Link 2 AI mode SET is ignored (steady 0xFF/0x10 readback).

    Tries handle reopen, sysfs uvcvideo unbind/rebind (when permitted), then
  probes whether a normal-mode SET changes readback. Returns True when SET
    works again (readback left 0xFF/0x10 or reached 0x00).
    """
    import time

    global _video_dev_cache

    def _say(msg: str) -> None:
        if verbose:
            print(msg)

    _video_dev_cache = None
    _invalidate_handle()
    ok, detail = _reset_handle_reopen()
    _say(f'handle reopen: {"ok" if ok else "failed"} — {detail}')

    results = _rebind_uvcvideo_sysfs(unbind_first=True)
    for rok, rdetail in results:
        _say(f'sysfs: {"ok" if rok else "failed"} — {rdetail}')
    if not any(r[0] for r in results):
        if _bootstrap_uvcvideo():
            _say('bootstrap: attached uvcvideo via libusb')

    try:
        _wait_for_video_device(timeout=15.0)
    except RuntimeError:
        _say('recover_ai_mode_stuck: no /dev/video* after rebind')
        return False

    _invalidate_handle()
    try:
        import link_ctl
        link_ctl.reset_usb_caches()
        if not link_ctl._link2_track_stuck():
            return True
        before = link_ctl._ai_mode_get_raw()[:2]
        link_ctl._apply_ai_mode_buffer(0, 0)
        time.sleep(2.0)
        link_ctl.reset_usb_caches()
        after = link_ctl._ai_mode_get_raw()[:2]
        ok = before != after or after[0] == 0x00
        _say(f'AI readback: {before[0]:02x}/{before[1]:02x} → {after[0]:02x}/{after[1]:02x}')
        return ok
    except Exception as exc:
        _say(f'recover_ai_mode_stuck: {exc}')
        return False


def _video_device_ready() -> str | None:
    """Return openable Insta360 /dev/video path, or None."""
    global _video_dev_cache
    dev = _find_video_device()
    if dev and os.path.exists(dev):
        try:
            fd = os.open(dev, os.O_RDWR)
            os.close(fd)
            return dev
        except OSError:
            _video_dev_cache = None
    return None


def _reset_handle_reopen() -> tuple[bool, str]:
    """Close cached USB/ioctl handles and reopen."""
    global _video_dev_cache
    _video_dev_cache = None
    _invalidate_handle()
    try:
        h = _get_handle()
        mask = h.get(9, 0x1B, 2)
        return True, f'handle reopened (func_enable={mask.hex()})'
    except Exception as e:
        _invalidate_handle()
        return False, str(e)


def _reset_libusb_device() -> tuple[bool, str]:
    """USB bus reset via libusb — only the discovered Insta360 device."""
    _invalidate_handle()
    ctx = LibusbContext()
    if _libusb.libusb_init(byref(ctx)) != 0:
        return False, 'libusb_init failed'
    desc = _DeviceDescriptor()
    list_pp = POINTER(c_void_p)()
    cnt = _libusb.libusb_get_device_list(ctx, byref(list_pp))
    if cnt < 0:
        _libusb.libusb_exit(ctx)
        return False, 'libusb_get_device_list failed'
    target_dev = None
    try:
        for i in range(cnt):
            dev = list_pp[i]
            if not dev:
                continue
            if _libusb.libusb_get_device_descriptor(dev, byref(desc)) != 0:
                continue
            if desc.idVendor != INSTA360_VID or desc.idProduct not in SUPPORTED_PIDS:
                continue
            target_dev = dev
            break
    finally:
        _libusb.libusb_free_device_list(list_pp, 1)
    if target_dev is None:
        _libusb.libusb_exit(ctx)
        return False, 'Insta360 device not found on USB bus'
    handle = LibusbDeviceHandle()
    if _libusb.libusb_open(target_dev, byref(handle)) != 0:
        _libusb.libusb_exit(ctx)
        return False, 'libusb_open failed (udev rule installed?)'
    try:
        _libusb.libusb_reset_device.argtypes = [LibusbDeviceHandle]
        _libusb.libusb_reset_device.restype = c_int
        rc = _libusb.libusb_reset_device(handle)
        if rc != 0:
            err = _libusb.libusb_strerror(rc)
            msg = err.decode() if err else f'error {rc}'
            return False, f'libusb_reset_device: {msg}'
        return True, 'libusb_reset_device ok'
    finally:
        _libusb.libusb_close(handle)
        _libusb.libusb_exit(ctx)


def reset_device(*, verbose: bool = False, skip_usb_reset: bool = False,
                 force: bool = False) -> dict:
    """Recover a hung Insta360 Link after detach/crash without unplugging.

    Tries in order: handle reopen → libusb reset → uvcvideo unbind/rebind →
    wait for /dev/video*. Safe: only touches Insta360 VID/PID from sysfs scan.

    When *force* is True, run handle reopen (and libusb reset unless skipped)
    even if /dev/video* is already present — useful when the node exists but
    XU/ioctl state is stale (Stream Deck reset button).
    """
    import time

    global _video_dev_cache
    log: list[str] = []
    methods: list[str] = []

    def _say(msg: str) -> None:
        log.append(msg)
        if verbose:
            print(msg)

    devices = discover_insta360_devices()
    if not devices:
        raise RuntimeError(
            'No Insta360 Link device found on USB (expected 2e1a:4c01/4c02/4c03/4c04)')
    dev = devices[0]
    if len(devices) > 1 and verbose:
        _say(f'warning: multiple Insta360 devices; using {dev["bus_path"]}')
    _say(f'found {dev["product"] or "Insta360 Link"} at {dev["bus_path"]} '
         f'({INSTA360_VID:04x}:{dev["product_id"]:04x})')

    ready = _video_device_ready()
    if ready and not force:
        _say(f'video device already ready: {ready}')
        _invalidate_handle()
        try:
            import link_ctl
            link_ctl.reset_usb_caches()
        except Exception:
            pass
        return {'ok': True, 'video': ready, 'methods': ['already_ready'], 'log': log}
    if ready and force:
        _say(f'video device ready: {ready} (force recovery)')
        _invalidate_handle()
        try:
            import link_ctl
            link_ctl.reset_usb_caches()
        except Exception:
            pass
        ok, detail = _reset_handle_reopen()
        methods.append('handle_reopen')
        _say(f'handle reopen: {"ok" if ok else "failed"} — {detail}')
        # libusb_reset_device on Link 2 can leave AI mode stuck at 0xFF/0x10 (track)
        # with XU SET no-ops — only use when the video node is missing.
        if not skip_usb_reset and not ready:
            ok, detail = _reset_libusb_device()
            methods.append('libusb_reset')
            _say(f'libusb reset: {"ok" if ok else "failed"} — {detail}')
            if ok:
                time.sleep(1.0)
        ready = _video_device_ready() or ready
        return {'ok': True, 'video': ready, 'methods': methods, 'log': log}

    ok, detail = _reset_handle_reopen()
    methods.append('handle_reopen')
    _say(f'handle reopen: {"ok" if ok else "failed"} — {detail}')
    ready = _video_device_ready()
    if ready:
        _say(f'video device ready: {ready}')
        try:
            import link_ctl
            link_ctl.reset_usb_caches()
        except Exception:
            pass
        return {'ok': True, 'video': ready, 'methods': methods, 'log': log}

    if not skip_usb_reset:
        ok, detail = _reset_libusb_device()
        methods.append('libusb_reset')
        _say(f'libusb reset: {"ok" if ok else "failed"} — {detail}')
        if ok:
            time.sleep(1.0)
        ready = _video_device_ready()
        if ready:
            _say(f'video device ready: {ready}')
            try:
                import link_ctl
                link_ctl.reset_usb_caches()
            except Exception:
                pass
            return {'ok': True, 'video': ready, 'methods': methods, 'log': log}

    sysfs_results = _rebind_uvcvideo_sysfs(unbind_first=True)
    methods.append('uvcvideo_rebind')
    for ok, detail in sysfs_results:
        _say(f'sysfs: {"ok" if ok else "failed"} — {detail}')
    if sysfs_results and not any(r[0] for r in sysfs_results):
        need_sudo = any('permission denied' in r[1] for r in sysfs_results)
        if need_sudo:
            script = Path(__file__).resolve().parent / 'tools' / 'reset_link2.sh'
            ifaces = '; '.join(
                f'echo {i} > /sys/bus/usb/drivers/uvcvideo/bind'
                for i in _insta360_uvc_interfaces())
            raise RuntimeError(
                'sysfs uvcvideo bind requires root. Run:\n'
                f'  sudo {script}\n'
                f'Or manually:\n  sudo sh -c \'{ifaces}\'')

    _video_dev_cache = None
    try:
        video = _wait_for_video_device()
    except RuntimeError as e:
        raise RuntimeError(f'recovery failed: {e}') from e
    _invalidate_handle()
    try:
        import link_ctl
        link_ctl.reset_usb_caches()
    except Exception:
        pass
    _say(f'video device ready: {video}')
    return {'ok': True, 'video': video, 'methods': methods, 'log': log}


def xu_get_len(unit: int, sel: int) -> int:
    return _get_handle()._ioctl.get_len(unit, sel)


def device_product_id() -> int | None:
    """Return USB product ID for the open Insta360 device, if known."""
    h = _get_handle()
    if not h._libusb_ok:
        return None
    desc = _DeviceDescriptor()
    # libusb doesn't expose get_device from handle easily; scan sysfs instead.
    sysfs = Path('/sys/bus/usb/devices')
    for entry in sysfs.iterdir():
        pid = entry / 'idProduct'
        vid = entry / 'idVendor'
        if not pid.exists() or not vid.exists():
            continue
        try:
            if vid.read_text().strip() != f'{INSTA360_VID:04x}':
                continue
            return int(pid.read_text().strip(), 16)
        except (OSError, ValueError):
            continue
    return None


def is_link2() -> bool:
    pid = device_product_id()
    return pid in (0x4C04, 0x4C02, 0x4C03)


def privacy_supported() -> bool:
    """Gimbal-down privacy (XU10) — Link 2 / 2 Pro, not OG Link or 2C shutter models."""
    pid = device_product_id()
    return pid in (0x4C04, 0x4C03)


def _invalidate_handle() -> None:
    global _handle
    if _handle is not None:
        try:
            _handle.close()
        except Exception:
            pass
        _handle = None

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
_libusb.libusb_strerror.argtypes = [c_int]
_libusb.libusb_strerror.restype = ctypes.c_char_p

SUPPORTED_PIDS = (0x4C01, 0x4C04, 0x4C02, 0x4C03)
VC_IFACE_NUM = 0
# UVC streaming/control interfaces (not audio :1.2/:1.3).
_UVC_IFACE_SUFFIXES = (':1.0', ':1.1')


def discover_insta360_devices(
    *,
    vid: int = INSTA360_VID,
    pids: tuple[int, ...] = SUPPORTED_PIDS,
) -> list[dict]:
    """Return attached Insta360 Link devices discovered via sysfs.

    Each entry: bus_path, product_id, product, interfaces[{sysfs, bound, driver}].
    Only matches known Insta360 vendor/product IDs.
    """
    sysfs = Path('/sys/bus/usb/devices')
    if not sysfs.is_dir():
        return []
    results: list[dict] = []
    for entry in sorted(sysfs.iterdir(), key=lambda p: p.name):
        name = entry.name
        if ':' in name:
            continue
        vid_path = entry / 'idVendor'
        pid_path = entry / 'idProduct'
        if not vid_path.is_file() or not pid_path.is_file():
            continue
        try:
            if vid_path.read_text().strip() != f'{vid:04x}':
                continue
            pid = int(pid_path.read_text().strip(), 16)
            if pid not in pids:
                continue
        except (OSError, ValueError):
            continue
        product_path = entry / 'product'
        product = product_path.read_text().strip() if product_path.is_file() else ''
        interfaces: list[dict] = []
        for suffix in _UVC_IFACE_SUFFIXES:
            iface = f'{name}{suffix}'
            iface_path = sysfs / iface
            if not iface_path.is_dir():
                continue
            driver_link = iface_path / 'driver'
            bound = driver_link.is_symlink()
            driver = driver_link.resolve().name if bound else None
            interfaces.append({
                'sysfs': iface,
                'bound': bound,
                'driver': driver,
            })
        results.append({
            'bus_path': name,
            'product_id': pid,
            'product': product,
            'interfaces': interfaces,
        })
    return results


def _insta360_uvc_interfaces() -> list[str]:
    """Sysfs names (e.g. 1-10.4.3.1:1.0) for Insta360 UVC interfaces."""
    names: list[str] = []
    for dev in discover_insta360_devices():
        for iface in dev['interfaces']:
            names.append(iface['sysfs'])
    return names


def _sysfs_driver_op(driver: str, op: str, iface: str) -> tuple[bool, str]:
    """Write iface name to /sys/bus/usb/drivers/<driver>/<op>. Returns (ok, detail)."""
    path = Path(f'/sys/bus/usb/drivers/{driver}/{op}')
    if not path.is_file():
        return False, f'{path} missing'
    if not os.access(path, os.W_OK):
        return False, f'permission denied writing {path} (try sudo)'
    try:
        path.write_text(iface + '\n')
        return True, f'{op} {iface}'
    except OSError as e:
        return False, f'{op} {iface}: {e}'


def _rebind_uvcvideo_sysfs(*, unbind_first: bool = False) -> list[tuple[bool, str]]:
    """Bind Insta360 UVC interfaces to uvcvideo; optionally unbind first."""
    results: list[tuple[bool, str]] = []
    for iface in _insta360_uvc_interfaces():
        if unbind_first:
            driver_link = Path('/sys/bus/usb/devices') / iface / 'driver'
            if driver_link.is_symlink() and driver_link.resolve().name == 'uvcvideo':
                results.append(_sysfs_driver_op('uvcvideo', 'unbind', iface))
        results.append(_sysfs_driver_op('uvcvideo', 'bind', iface))
    return results

UVC_GET_CUR = 0x81
UVC_SET_CUR = 0x01
UVC_GET_LEN = 0x85

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
}


# Link 2 exposes white_balance_automatic; OG Link / other kernels use
# white_balance_temperature_auto.
_WB_AUTO_CTRLS = (
    'white_balance_automatic',
    'white_balance_temperature_auto',
    'auto_white_balance',
)


def _v4l2_get_awb_auto() -> int:
    for name in _WB_AUTO_CTRLS:
        try:
            return _v4l2_get_ctrl(name)
        except RuntimeError:
            continue
    raise RuntimeError('no AWB auto v4l2 control found')


def _v4l2_set_awb_auto(value: int) -> None:
    for name in _WB_AUTO_CTRLS:
        try:
            _v4l2_set_ctrl(name, value)
            return
        except RuntimeError:
            continue
    raise RuntimeError('no AWB auto v4l2 control found')


def _probe_ct_pu(op: str, unit: int, sel: int, *,
                 length: int | None = None, data: bytes | None = None) -> bytes | None:
    """Run uvc-probe-linux for CT/PU access; detach only when explicitly allowed."""
    import subprocess
    if not _PROBE.is_file():
        return None
    cmd = [str(_PROBE)]
    if _detach_allowed():
        cmd.insert(1, '--detach')
    cmd.extend([op, str(unit), f'0x{sel:02x}'])
    if op == 'get':
        cmd.append(str(length))
    else:
        cmd.append(data.hex() if data else '')
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
        if r.returncode != 0:
            return None
        if op == 'get':
            return bytes.fromhex(r.stdout.strip())
        return b''
    finally:
        try:
            _wait_for_video_device(timeout=8.0)
        except RuntimeError:
            pass


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
        auto = _v4l2_get_awb_auto()
        return bytes([1 if auto else 0])
    if unit == 5 and sel == 0x05 and length == 1:
        plf = _v4l2_get_ctrl('power_line_frequency')
        # UVC PU: 3=auto, 1=50Hz, 2=60Hz
        return bytes([plf if plf in (1, 2, 3) else 3])
    return None


def _v4l2_set(unit: int, sel: int, data: bytes) -> bool:
    if unit == 1 and sel == 0x0D and len(data) == 8:
        pan, tilt = struct.unpack('<ii', data)
        try:
            _v4l2_set_ctrl('pan_absolute', pan)
            _v4l2_set_ctrl('tilt_absolute', tilt)
            return True
        except RuntimeError:
            return False
    name = _V4L2_SET_MAP.get((unit, sel))
    if not name:
        return False
    if len(data) == 1:
        if (unit, sel) == (5, 0x0B):
            try:
                _v4l2_set_awb_auto(1 if data[0] else 0)
                return True
            except RuntimeError:
                return False
        _v4l2_set_ctrl(name, data[0])
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


def read_pantilt_v4l2() -> tuple[int, int]:
    """Read pan/tilt via v4l2 — reliable on Link 2 (XU 0x1A readback is stale)."""
    return _v4l2_get_ctrl('pan_absolute'), _v4l2_get_ctrl('tilt_absolute')


import contextlib


def _stream_cmd(device: str, seconds: float) -> list[str] | None:
    """Build a bounded capture command (ffmpeg preferred, v4l2-ctl fallback)."""
    import shutil
    if shutil.which('ffmpeg'):
        return [
            'ffmpeg', '-hide_banner', '-loglevel', 'error',
            '-f', 'v4l2', '-input_format', 'mjpeg',
            '-video_size', '1280x720', '-i', device,
            '-t', f'{seconds:.1f}', '-f', 'null', '-',
        ]
    if shutil.which('v4l2-ctl'):
        # Stream enough frames to span ~seconds at 30fps.
        count = max(1, int(seconds * 30))
        return [
            'v4l2-ctl', '-d', device,
            '--set-fmt-video=width=1280,height=720,pixelformat=MJPG',
            '--stream-mmap', f'--stream-count={count}', '--stream-to', '/dev/null',
        ]
    return None


@contextlib.contextmanager
def video_stream(seconds: float = 8.0, device: str | None = None):
    """Hold a v4l2 capture stream open for the duration of the block.

    The Link 2 AI engine (track/overhead/deskview/whiteboard) only engages and
    reports its real mode byte while the video pipeline is streaming; with no
    stream the AI mode buffer reads back 0xFF ("idle/transition"), which makes a
    SET look like it "did not stick". Holding a stream open during SET + GET
    readback matches what the desktop app does.

    Yields True when a stream was started, False otherwise (e.g. /dev/video busy
    with another app, or no ffmpeg/v4l2-ctl). The stream auto-stops on exit.
    """
    import subprocess
    import time

    dev = device or _find_video_device()
    proc = None
    started = False
    if dev:
        cmd = _stream_cmd(dev, seconds)
        if cmd is not None:
            try:
                proc = subprocess.Popen(
                    cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    stdin=subprocess.DEVNULL)
                # Let it claim the device and reach STREAMON before we report ready.
                time.sleep(1.0)
                started = proc.poll() is None
                if not started:
                    proc = None
            except Exception:
                proc = None
                started = False
    try:
        yield started
    finally:
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=2.0)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass


def _video_device() -> str:
    dev = _find_video_device()
    if dev:
        return dev
    return '/dev/video0'


_video_dev_cache: str | None = None


def _find_video_device() -> str | None:
    global _video_dev_cache
    if _video_dev_cache and os.path.exists(_video_dev_cache):
        try:
            fd = os.open(_video_dev_cache, os.O_RDWR)
            os.close(fd)
            return _video_dev_cache
        except OSError:
            _video_dev_cache = None
    env_device = os.environ.get('LINK_CTL_V4L2_DEVICE')
    if env_device and os.path.exists(env_device):
        _video_dev_cache = env_device
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
                        _video_dev_cache = dev
                        return dev
            except OSError:
                continue
    if os.path.exists('/dev/video0'):
        _video_dev_cache = '/dev/video0'
        return '/dev/video0'
    return None


class _IoctlBackend:
    def __init__(self) -> None:
        self._fd: int | None = None
        self._ioctl_num = _linux_ioctl_number()

    def open(self) -> None:
        path = _find_video_device()
        if not path:
            try:
                _wait_for_video_device(timeout=8.0)
                path = _find_video_device()
            except RuntimeError:
                pass
        if not path:
            raise RuntimeError('No Insta360 /dev/video device found (device not found)')
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

    def get_len(self, unit: int, sel: int) -> int:
        import fcntl
        if self._fd is None:
            raise RuntimeError('ioctl backend not open')
        data_buf = (ctypes.c_uint8 * 2)()
        q = _UvcXuControlQuery()
        q.unit = unit
        q.selector = sel
        q.query = UVC_GET_LEN
        q.size = 2
        q.data = ctypes.addressof(data_buf)
        fcntl.ioctl(self._fd, self._ioctl_num, q)
        return int.from_bytes(bytes(data_buf), 'little')

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

    def _libusb_xfer(self, unit: int, sel: int, data: bytes | None, length: int,
                     *, force_detach: bool = False) -> bytes | None:
        """CT/PU GET (data=None) or SET. Returns GET bytes, b'' on SET success, None on failure."""
        if not self._libusb_ok:
            return None
        detached = False
        try:
            if force_detach and _libusb.libusb_kernel_driver_active(self.dev, VC_IFACE_NUM) == 1:
                if _libusb.libusb_detach_kernel_driver(self.dev, VC_IFACE_NUM) == 0:
                    detached = True
            if data is None:
                buf = (c_uint8 * length)()
                r = _libusb.libusb_control_transfer(
                    self.dev, 0xA1, UVC_GET_CUR,
                    sel << 8, (unit << 8) | VC_IFACE_NUM,
                    buf, length, 1000)
                if r >= 0:
                    return bytes(buf[:r])
                return None
            buf = (c_uint8 * len(data))(*data)
            r = _libusb.libusb_control_transfer(
                self.dev, 0x21, UVC_SET_CUR,
                sel << 8, (unit << 8) | VC_IFACE_NUM,
                buf, len(data), 1000)
            return b'' if r >= 0 else None
        finally:
            if detached:
                try:
                    _after_ct_detach(self)
                except RuntimeError:
                    _invalidate_handle()
                    raise

    def _pu_get(self, sel: int, length: int) -> bytes:
        """Processing Unit — v4l2 only (never detach; libusb breaks PU on Link 2)."""
        fb = _v4l2_get(5, sel, length)
        if fb is not None:
            return fb
        raise RuntimeError(f'PU GET failed s=0x{sel:02x} via v4l2')

    def _pu_set(self, sel: int, data: bytes) -> None:
        if _v4l2_set(5, sel, data):
            return
        # v4l2 SET can fail for AWB while GET works; libusb (no detach) is safe for PU.
        if self._libusb_xfer(5, sel, data, 0) is not None:
            return
        raise RuntimeError(f'PU SET failed s=0x{sel:02x}')

    def _ct_get(self, sel: int, length: int) -> bytes:
        """Camera Terminal GET — v4l2, then XU pan readback; no detach."""
        try:
            fb = _v4l2_get(1, sel, length)
            if fb is not None:
                return fb
        except RuntimeError:
            pass
        if sel == 0x0D and length == 8:
            try:
                pan, tilt = read_pantilt_v4l2()
                return struct.pack('<ii', pan, tilt)
            except RuntimeError:
                pass
            if self._ioctl._fd is None:
                self._ioctl.open()
            return self._ioctl.get(9, 0x1A, 8)
        got = self._libusb_xfer(1, sel, None, length)
        if got is not None:
            return got
        raise RuntimeError(f'CT GET failed s=0x{sel:02x}')

    def _ct_set(self, sel: int, data: bytes) -> None:
        """Camera Terminal SET — pan/tilt may need detach; zoom uses v4l2."""
        if sel != 0x0D:
            if self._libusb_xfer(1, sel, data, 0) is not None:
                return
            if _v4l2_set(1, sel, data):
                return
            raise RuntimeError(f'CT SET failed s=0x{sel:02x}')
        # Pan/tilt: subprocess probe owns detach/attach lifecycle (avoids stale in-process handle).
        if _probe_ct_pu('set', 1, sel, data=data) is not None:
            try:
                _after_ct_detach(None)
            except RuntimeError:
                _invalidate_handle()
                raise
            _invalidate_handle()
            return
        if _detach_allowed():
            if self._libusb_xfer(1, sel, data, 0, force_detach=True) is not None:
                return
        if _v4l2_set(1, sel, data):
            return
        if not _detach_allowed():
            raise RuntimeError(
                f'CT SET failed s=0x{sel:02x} (pan/tilt needs --detach or LINK_CTL_USB_DETACH=1)')
        raise RuntimeError(f'CT SET failed s=0x{sel:02x}')

    def _usb_get(self, unit: int, sel: int, length: int) -> bytes:
        if unit == 5:
            return self._pu_get(sel, length)
        if unit == 1:
            return self._ct_get(sel, length)
        raise RuntimeError(f'GET_CUR failed u={unit} s=0x{sel:02x}')

    def _usb_set(self, unit: int, sel: int, data: bytes) -> None:
        if unit == 5:
            return self._pu_set(sel, data)
        if unit == 1:
            return self._ct_set(sel, data)
        raise RuntimeError(f'SET_CUR failed u={unit} s=0x{sel:02x}')

    def get(self, unit: int, sel: int, length: int) -> bytes:
        if unit in XU_UNITS:
            if self._ioctl._fd is None:
                self._ioctl.open()
            return self._ioctl.get(unit, sel, length)
        return self._usb_get(unit, sel, length)

    def set(self, unit: int, sel: int, data: bytes) -> None:
        if unit in XU_UNITS:
            if self._ioctl._fd is None:
                self._ioctl.open()
            # ioctl SET works for Link 2 XU (use RMW payloads from link_ctl).
            # In-process libusb SET fails with EIO while uvcvideo is bound.
            try:
                self._ioctl.set(unit, sel, data)
                return
            except OSError as e:
                last = e
            if _probe_xu_set(unit, sel, data):
                _invalidate_handle()
                return
            raise RuntimeError(f'XU SET failed u={unit} s=0x{sel:02x}: {last}')
        self._usb_set(unit, sel, data)


_handle: LinuxUSBHandle | None = None


def _get_handle() -> LinuxUSBHandle:
    global _handle
    if _handle is None:
        _handle = LinuxUSBHandle()
        return _handle
    if _handle._ioctl._fd is None:
        try:
            _handle._ioctl.open()
        except Exception:
            _handle.close()
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
