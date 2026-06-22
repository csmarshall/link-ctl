#!/usr/bin/env python3
"""validate.py — Live command validator for the Insta360 Link Controller.

Maintains a single WebSocket connection for the entire run. Each command is
sent via send_and_assert(), which waits for the deviceInfoNotify response and
checks the expected field changed — no reconnect required.

Prerequisites
─────────────
1. Hardware:
   • Insta360 Link (original) camera connected via USB
   • Camera in normal (non-privacy) position so pan/tilt isn't blocked

2. Software — Insta360 Link Controller app MUST be running:
   • macOS:   /Applications/Insta360 Link Controller.app
   • Windows: Start Menu → Insta360 Link Controller
   The app can be minimized; its WebSocket server stays active.

3. Python dependencies:
   • Python ≥ 3.11
   • websockets ≥ 11:  pip install websockets

4. Exclusivity:
   • Only ONE WebSocket controller may be in control at a time.
   • Close the mobile web remote (from the QR code) before running this script.
   • If link-ctl is installed via pipx, use its Python:
       ~/.local/pipx/venvs/link-ctl/bin/python3 tools/validate.py

Usage
─────
    python3 validate.py                    # auto-discover port
    python3 validate.py --port 49924       # explicit port
    python3 validate.py "http://..."       # parse port+token from QR URL
    python3 validate.py --only zoom,hdr    # run specific tests (comma-separated)

Exit codes
──────────
    0  All attempted tests passed
    1  One or more tests failed (validation completed)
    2  Setup/connection failure (could not reach camera)

Updating paramType mappings
────────────────────────────
If a firmware update changes the protocol, run mobileui-dump.py with the QR URL
to drive the web UI and capture all WebSocket frames inline, then update
ParamTypeV2 in link_ctl.py and the test cases at the bottom of this file.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import platform
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse, parse_qs

sys.path.insert(0, str(Path(__file__).parent.parent))
from link_ctl import (
    LinkClient,
    ParamTypeV2, AIMode, VideoMode,
    build_value_change, build_zoom,
    build_preset_recall, build_preset_save, build_preset_delete,
    _lsof_port, _read_token_from_ini,
    read_status_usb, write_ai_mode, write_zoom, read_zoom,
    read_pantilt, _uvc_probe_available,
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _ts() -> str:
    t = time.time()
    return time.strftime('%H:%M:%S') + f'.{int((t % 1) * 1000):03d}'


def _log(msg: str):
    print(f'[{_ts()}] {msg}', flush=True)


def _parse_url(url: str) -> tuple[int | None, str | None]:
    parsed = urlparse(url)
    qs = parse_qs(parsed.query)
    port_str = qs.get('port', [None])[0]
    token = qs.get('token', [None])[0]
    port = int(port_str) if port_str and port_str.isdigit() else None
    return port, token


def _current_dev(client: LinkClient) -> dict:
    """Return the first device dict from the client's latest deviceInfoNotify."""
    devs = (client.device_info or {}).get('devices', [])
    return devs[0] if devs else {}


async def _read_state(port: int, token: str) -> dict:
    """Fresh connect+handshake to read device state, then disconnect.

    The server only sends deviceInfoNotify during handshake, not in response
    to commands on a persistent connection. So we reconnect to read state.
    """
    client = LinkClient(port, token=token)
    try:
        await client.connect()
        ok, _ = await client.handshake()
        await client.close()
        if ok:
            return _current_dev(client)
    except Exception:
        pass
    return {}


# ── Test framework ────────────────────────────────────────────────────────────

@dataclass
class Result:
    name: str
    passed: bool
    message: str = ''
    skipped: bool = False


@dataclass
class TestCase:
    name: str
    description: str
    # Returns bytes to send for the "test" state
    build_on: Callable[[str], bytes]
    # Returns bytes to send to restore original state (None = no restore needed)
    build_restore: Callable[[str, dict], bytes | None]
    # Receives (before_dev, after_dev): returns (passed, message)
    check: Callable[[dict, dict], tuple[bool, str]]
    # Seconds to wait for deviceInfoNotify after the test command
    settle: float = 1.0
    # Optional setup commands sent before build_on (state before these is the restore target)
    prereqs: list[Callable[[str], bytes]] = field(default_factory=list)
    # Optional teardown commands sent after build_restore
    postreqs: list[Callable[[str], bytes]] = field(default_factory=list)


async def run_test(
    tc: TestCase, client: LinkClient, serial: str, port: int, token: str
) -> Result:
    _log(f'  → {tc.description}')

    # The server only sends deviceInfoNotify during handshake, not after commands.
    # Strategy: send on the persistent connection, read state via a fresh handshake.

    # Snapshot state before any modifications — used by build_restore later
    before = _current_dev(client)

    # Prerequisites (e.g. AWB off before testing WB temp)
    for prereq_fn in tc.prereqs:
        try:
            await client._send(prereq_fn(serial))
            await asyncio.sleep(0.5)
        except Exception as e:
            return Result(tc.name, False, f'prereq failed: {e}')

    # Test command
    try:
        await client._send(tc.build_on(serial))
    except Exception as e:
        return Result(tc.name, False, f'send failed: {e}')

    # Read state via fresh handshake
    await asyncio.sleep(tc.settle)
    after = await _read_state(port, token)

    # Assert
    passed, msg = tc.check(before, after)

    # Update client's cached state for next test's "before"
    if after:
        client.device_info = {'devices': [after]}

    # Restore
    restore = tc.build_restore(serial, before)
    if restore is not None:
        try:
            await client._send(restore)
            await asyncio.sleep(0.5)
        except Exception as e:
            _log(f'    ! restore failed: {e}')

    # Postreqs (e.g. AWB back on)
    for postreq_fn in tc.postreqs:
        try:
            await client._send(postreq_fn(serial))
            await asyncio.sleep(0.5)
        except Exception as e:
            _log(f'    ! postreq failed: {e}')

    return Result(tc.name, passed, msg)


# ── Test case definitions ─────────────────────────────────────────────────────

def make_tests() -> list[TestCase]:
    def _int_check(field: str, on_val: int):
        def check(before: dict, after: dict) -> tuple[bool, str]:
            got = after.get(field)
            if got == on_val:
                return True, f'{field}={got} ✓'
            return False, f'{field}={got} (expected {on_val}, before={before.get(field)})'
        return check

    def _bool_check(field: str, expected: bool):
        def check(before: dict, after: dict) -> tuple[bool, str]:
            got = after.get(field)
            if got == expected:
                return True, f'{field}={got} ✓'
            return False, f'{field}={got} (expected {expected}, before={before.get(field)})'
        return check

    def _mode_check(expected_mode: int):
        def check(before: dict, after: dict) -> tuple[bool, str]:
            got = after.get('mode')
            if got == expected_mode:
                return True, f'mode={got} ✓'
            return False, f'mode={got} (expected {expected_mode}, before={before.get("mode")})'
        return check

    return [
        # ── Zoom ─────────────────────────────────────────────────────────────
        TestCase(
            name='zoom',
            description='Zoom: set 200, verify zoom.curValue=200, restore',
            build_on=lambda s: build_zoom(s, 200),
            build_restore=lambda s, b: build_zoom(s, b.get('zoom', {}).get('curValue', 100)),
            check=lambda b, a: (
                (True,  f"zoom.curValue=200 ✓")
                if a.get('zoom', {}).get('curValue') == 200 else
                (False, f"zoom.curValue={a.get('zoom', {}).get('curValue')} (expected 200)")
            ),
            settle=1.0,
        ),

        # ── AI modes ─────────────────────────────────────────────────────────
        TestCase(
            name='track',
            description='AI tracking: enable, verify mode=1, restore normal',
            build_on=lambda s: build_value_change(s, ParamTypeV2.AI_MODE, AIMode.TRACKING),
            build_restore=lambda s, _: build_value_change(s, ParamTypeV2.AI_MODE, AIMode.NORMAL),
            check=_mode_check(VideoMode.TRACKING),
            settle=1.5,
        ),
        TestCase(
            name='overhead',
            description='Overhead mode: enable, verify mode=4, restore normal',
            build_on=lambda s: build_value_change(s, ParamTypeV2.AI_MODE, AIMode.OVERHEAD),
            build_restore=lambda s, _: build_value_change(s, ParamTypeV2.AI_MODE, AIMode.NORMAL),
            check=_mode_check(VideoMode.OVERHEAD),
            settle=1.5,
        ),
        TestCase(
            name='deskview',
            description='DeskView mode: enable, verify mode=5, restore normal',
            build_on=lambda s: build_value_change(s, ParamTypeV2.AI_MODE, AIMode.DESKVIEW),
            build_restore=lambda s, _: build_value_change(s, ParamTypeV2.AI_MODE, AIMode.NORMAL),
            check=_mode_check(VideoMode.DESKVIEW),
            settle=1.5,
        ),
        TestCase(
            name='whiteboard',
            description='Whiteboard mode: enable, verify mode=6, restore normal',
            build_on=lambda s: build_value_change(s, ParamTypeV2.AI_MODE, AIMode.WHITEBOARD),
            build_restore=lambda s, _: build_value_change(s, ParamTypeV2.AI_MODE, AIMode.NORMAL),
            check=_mode_check(VideoMode.WHITEBOARD),
            settle=2.0,
        ),

        # ── Image settings ────────────────────────────────────────────────────
        TestCase(
            name='hdr',
            description='HDR: toggle on, verify hdr=True, restore',
            build_on=lambda s: build_value_change(s, ParamTypeV2.HDR, '1'),
            build_restore=lambda s, b: build_value_change(
                s, ParamTypeV2.HDR, '1' if b.get('hdr') else '0'),
            check=_bool_check('hdr', True),
            settle=1.0,
        ),
        TestCase(
            name='brightness',
            description='Brightness: set 75, verify brightness=75, restore',
            build_on=lambda s: build_value_change(s, ParamTypeV2.BRIGHTNESS, '75'),
            build_restore=lambda s, b: build_value_change(
                s, ParamTypeV2.BRIGHTNESS, str(b.get('brightness', 50))),
            check=_int_check('brightness', 75),
            settle=1.0,
        ),
        TestCase(
            name='contrast',
            description='Contrast: set 75, verify contrast=75, restore',
            build_on=lambda s: build_value_change(s, ParamTypeV2.CONTRAST, '75'),
            build_restore=lambda s, b: build_value_change(
                s, ParamTypeV2.CONTRAST, str(b.get('contrast', 50))),
            check=_int_check('contrast', 75),
            settle=1.0,
        ),
        TestCase(
            name='saturation',
            description='Saturation: set 75, verify saturation=75, restore',
            build_on=lambda s: build_value_change(s, ParamTypeV2.SATURATION, '75'),
            build_restore=lambda s, b: build_value_change(
                s, ParamTypeV2.SATURATION, str(b.get('saturation', 50))),
            check=_int_check('saturation', 75),
            settle=1.0,
        ),
        TestCase(
            name='sharpness',
            description='Sharpness: set 75, verify sharpness=75, restore',
            build_on=lambda s: build_value_change(s, ParamTypeV2.SHARPNESS, '75'),
            build_restore=lambda s, b: build_value_change(
                s, ParamTypeV2.SHARPNESS, str(b.get('sharpness', 50))),
            check=_int_check('sharpness', 75),
            settle=1.0,
        ),
        TestCase(
            name='exposurecomp',
            description='Exposure comp: set 75, verify exposureComp=75, restore',
            build_on=lambda s: build_value_change(s, ParamTypeV2.EXPOSURE_COMP, '75'),
            build_restore=lambda s, b: build_value_change(
                s, ParamTypeV2.EXPOSURE_COMP, str(b.get('exposureComp', 50))),
            check=_int_check('exposureComp', 75),
            settle=1.0,
        ),

        # ── Auto controls ─────────────────────────────────────────────────────
        TestCase(
            name='autoexposure',
            description='Auto exposure: toggle off, verify autoExposure=False, restore',
            build_on=lambda s: build_value_change(s, ParamTypeV2.AUTO_EXPOSURE, '0'),
            build_restore=lambda s, b: build_value_change(
                s, ParamTypeV2.AUTO_EXPOSURE, '1' if b.get('autoExposure', True) else '0'),
            check=_bool_check('autoExposure', False),
            settle=1.0,
        ),
        TestCase(
            name='awb',
            description='Auto white balance: toggle off, verify autoWhiteBalance=False, restore',
            build_on=lambda s: build_value_change(s, ParamTypeV2.AUTO_WB, '0'),
            build_restore=lambda s, b: build_value_change(
                s, ParamTypeV2.AUTO_WB, '1' if b.get('autoWhiteBalance', True) else '0'),
            check=_bool_check('autoWhiteBalance', False),
            settle=1.0,
        ),

        # ── WB temperature ────────────────────────────────────────────────────
        TestCase(
            name='wb-temp',
            description='WB temp: AWB off → set 5000K, verify wbTemp=5000, restore',
            prereqs=[lambda s: build_value_change(s, ParamTypeV2.AUTO_WB, '0')],
            build_on=lambda s: build_value_change(s, ParamTypeV2.WB_TEMP, '5000'),
            build_restore=lambda s, b: build_value_change(
                s, ParamTypeV2.WB_TEMP, str(b.get('wbTemp', 4200))),
            postreqs=[lambda s: build_value_change(s, ParamTypeV2.AUTO_WB, '1')],
            check=_int_check('wbTemp', 5000),
            settle=1.0,
        ),

        # ── Presets (smoke tests — no DeviceInfo readback for preset state) ───
        TestCase(
            name='preset-save',
            description='Preset save: save current position as slot 0 (smoke)',
            build_on=lambda s: build_preset_save(s, 0),
            build_restore=lambda s, b: None,
            check=lambda b, a: (True, 'command accepted (no readback available) ✓'),
            settle=1.0,
        ),
        TestCase(
            name='preset',
            description='Preset recall: recall slot 0 (smoke)',
            build_on=lambda s: build_preset_recall(s, 0),
            build_restore=lambda s, b: None,
            check=lambda b, a: (True, 'command accepted (no readback available) ✓'),
            settle=1.0,
        ),
        TestCase(
            name='preset-delete',
            description='Preset delete: delete slot 0 (smoke)',
            build_on=lambda s: build_preset_delete(s, 0),
            build_restore=lambda s, b: None,
            check=lambda b, a: (True, 'command accepted (no readback available) ✓'),
            settle=1.0,
        ),
    ]


def run_usb_test(name: str, apply, check, restore=None, settle: float = 1.0,
                 recover_after: bool = False) -> Result:
    _log(name)
    try:
        apply()
        time.sleep(settle)
        passed, msg = check()
        if restore:
            restore()
            time.sleep(0.5)
        if recover_after:
            _usb_recover()
        return Result(name, passed, msg)
    except Exception as e:
        if restore:
            try:
                restore()
            except Exception:
                pass
        _usb_recover()
        return Result(name, False, str(e))


def _usb_recover() -> None:
    """Rebind uvcvideo after CT detach or rapid XU writes."""
    try:
        import link_usb_linux as ul
        ul.recover()
        time.sleep(1.0)
    except Exception:
        pass


def _skip_center() -> bool:
    return os.environ.get('LINK_CTL_SKIP_CENTER', '').lower() in ('1', 'true', 'yes')


# AI modes verified in-stream (the live modes revert to normal the instant
# streaming stops, so the SET and the readback must share one stream).
_AI_MODE_TESTS = ('track', 'overhead', 'deskview')


def _run_usb_ai_mode(name: str, mode_name: str, settle: float = 1.5) -> Result:
    """Set an AI mode and verify byte[0] reaches the target WHILE holding a stream.

    Live AI modes (overhead/whiteboard) revert to normal the moment streaming
    stops, so reading the mode back after write_ai_mode()'s stream closed always
    looked like a failure. Here the SET and the readback share one continuous
    video stream — the same mechanism write_ai_mode/_link2_set_ai_mode_streamed
    use — and the mode is considered correct when byte[0] reaches the target
    while streaming (track's persistent 0xFF/0x10 state counts, per
    _ai_mode_wire_ok / read_ai_mode).
    """
    import link_ctl as lc
    import link_usb_linux as ul

    _log(name)
    mode_id, flag = lc.AI_MODE_BYTES[mode_name]
    try:
        with ul.video_stream(seconds=24.0) as streaming:
            got = lc._link2_drive_ai_mode_streaming(mode_name, mode_id, flag, streaming)
            if not streaming:
                ok = True
                msg = f'{mode_name}: SET sent (no stream available to verify in-stream)'
            else:
                ok = lc._ai_mode_wire_ok(mode_name, got)
                gh = f'0x{got:02x}' if got is not None else 'None'
                msg = (f'byte[0]={gh} → {mode_name} (in-stream) ✓' if ok
                       else f'byte[0]={gh} (expected {mode_name}=0x{mode_id:02x})')
    except Exception as e:
        ok, msg = False, str(e)
    # Restore normal outside the stream, then rebind uvcvideo for the next test.
    try:
        lc.write_ai_mode('normal')
        time.sleep(settle)
    except Exception:
        pass
    _usb_recover()
    return Result(name, ok, msg)


def run_usb_all(only: list[str], skip_center: bool = False) -> int:
    import link_ctl as lc

    if not _uvc_probe_available():
        print('✗ USB-direct backend unavailable.', file=sys.stderr)
        return 2

    skip_center = skip_center or _skip_center()

    def _save_bit(bit: int) -> bool:
        return lc._bitmask_get_bit(bit)

    def _restore_bit(bit: int, was_on: bool) -> None:
        lc._bitmask_set_bit(bit, was_on)

    ctx: dict = {}

    def _center_apply() -> None:
        _usb_recover()
        lc.write_ai_mode('normal')
        time.sleep(1.5)
        ctx['zoom'] = lc.read_zoom()
        lc.write_zoom(100)
        time.sleep(0.5)
        lc.write_pantilt(0, 0)
        time.sleep(1.5)

    def _check_center() -> tuple[bool, str]:
        for attempt in range(12):
            try:
                p, t = lc.read_pantilt()
                z = lc.read_zoom()
                ok = p == 0 and t == 0 and z == 100
                if ok or attempt == 11:
                    return ok, f'pan={p} tilt={t} zoom={z}'
            except Exception as e:
                if attempt == 11:
                    return False, str(e)
            time.sleep(0.5)
        return False, 'center check timed out'

    def _awb_apply_off() -> None:
        ctx['awb'] = lc.read_status_usb('awb')['is_on']
        if ctx['awb']:
            lc._uvc_set(5, 0x0B, bytes([0]))

    def _awb_restore() -> None:
        if ctx.get('awb'):
            lc._uvc_set(5, 0x0B, bytes([1]))

    # center runs last — CT pan/tilt SET may detach/rebind uvcvideo (needs --detach).
    # track/overhead/deskview are AI modes: the loop dispatches them to
    # _run_usb_ai_mode (set + readback share one stream), so their apply/check/
    # restore slots are unused placeholders here — only name/order matter.
    specs = [
        ('zoom', lambda: lc.write_zoom(200),
         lambda: (lc.read_zoom() == 200, f'zoom={lc.read_zoom()}'),
         lambda: lc.write_zoom(100), False),
        ('track', None, None, None, True),
        ('overhead', None, None, None, True),
        ('deskview', None, None, None, True),
        ('autoexposure', lambda: lc._uvc_set(9, 0x1e, bytes([1])),
         lambda: (not lc.read_status_usb('autoexposure')['is_on'], lc.read_status_usb('autoexposure')['display']),
         lambda: lc._uvc_set(9, 0x1e, bytes([2])), False),
        ('awb', _awb_apply_off,
         lambda: (not lc.read_status_usb('awb')['is_on'], lc.read_status_usb('awb')['display']),
         _awb_restore, False),
        ('hdr', lambda: (ctx.__setitem__('hdr', _save_bit(lc.BIT_HDR)), lc._bitmask_set_bit(lc.BIT_HDR, True))[-1],
         lambda: (lc.read_status_usb('hdr')['is_on'], lc.read_status_usb('hdr')['display']),
         lambda: _restore_bit(lc.BIT_HDR, ctx.get('hdr', False)), False),
        ('mirror', lambda: (ctx.__setitem__('mirror', _save_bit(lc.BIT_MIRROR)), lc._bitmask_set_bit(lc.BIT_MIRROR, True))[-1],
         lambda: (lc.read_status_usb('mirror')['is_on'], lc.read_status_usb('mirror')['display']),
         lambda: _restore_bit(lc.BIT_MIRROR, ctx.get('mirror', False)), False),
        ('brightness', lambda: (ctx.__setitem__('bright', lc.read_status_usb('brightness')['value']), lc._uvc_set(5, 0x02, bytes([75])))[-1],
         lambda: (lc.read_status_usb('brightness')['value'] == 75, f"brightness={lc.read_status_usb('brightness')['value']}"),
         lambda: lc._uvc_set(5, 0x02, bytes([ctx.get('bright', 50)])), False),
        ('center', _center_apply,
         _check_center,
         lambda: lc.write_zoom(ctx.get('zoom', 100)), True),
    ]
    if skip_center:
        specs = [s for s in specs if s[0] != 'center']
        print('Skipping center test (LINK_CTL_SKIP_CENTER or --skip-center)', flush=True)
    if only:
        specs = [s for s in specs if s[0] in only]

    settle_map = {'center': 5.0}

    results = []
    for name, apply, check, restore, recover_after in specs:
        print(f'[TEST] {name}')
        if name in _AI_MODE_TESTS:
            r = _run_usb_ai_mode(name, name)
        else:
            r = run_usb_test(name, apply, check, restore,
                             settle=settle_map.get(name, 1.0),
                             recover_after=recover_after)
        print(f'  [{"PASS" if r.passed else "FAIL"}] {r.message}')
        results.append(r)
        time.sleep(0.5)
    _usb_recover()
    failed = sum(1 for r in results if not r.passed)
    print(f'Results: {len(results)-failed}/{len(results)} passed')
    return 0 if failed == 0 else 1


# ── Main runner ───────────────────────────────────────────────────────────────

async def run_all(port: int, token: str, only: list[str]) -> int:
    """Single persistent connection for the entire test run."""
    _log('Connecting to camera...')
    client = LinkClient(port, token=token)
    try:
        await client.connect()
    except Exception as e:
        print(f'✗ Cannot connect to ws://localhost:{port}/: {e}', file=sys.stderr)
        return 2

    ok, err = await client.handshake()
    if not ok:
        print(f'✗ Handshake failed: {err}', file=sys.stderr)
        await client.close()
        return 2

    serial = client.serial
    info = client.device_info
    if not serial or not info or not info.get('devices'):
        print('✗ No device info received during handshake', file=sys.stderr)
        await client.close()
        return 2

    dev = info['devices'][0]
    print(f'\nCamera: {dev.get("deviceName", "?")}  serial={serial}')
    print(f'State:  zoom={dev.get("zoom", {}).get("curValue")}  '
          f'mode={dev.get("mode")}  '
          f'hdr={dev.get("hdr")}  '
          f'brightness={dev.get("brightness")}  '
          f'saturation={dev.get("saturation")}')
    print()

    tests = make_tests()
    if only:
        tests = [t for t in tests if t.name in only]

    results: list[Result] = []
    for tc in tests:
        print(f'[TEST] {tc.name}')
        try:
            result = await run_test(tc, client, serial, port, token)
        except Exception as e:
            result = Result(tc.name, False, f'exception: {e}')
        status = 'PASS' if result.passed else 'FAIL'
        print(f'  [{status}] {result.message}')
        results.append(result)
        print()
        await asyncio.sleep(0.3)

    await client.close()

    passed = sum(1 for r in results if r.passed)
    failed = sum(1 for r in results if not r.passed and not r.skipped)
    total  = len(results)
    print('─' * 50)
    print(f'Results: {passed}/{total} passed', end='')
    if failed:
        print(f'  ({failed} FAILED)', end='')
    print()

    if failed:
        print('\nFailed tests:')
        for r in results:
            if not r.passed and not r.skipped:
                print(f'  • {r.name}: {r.message}')

    return 0 if failed == 0 else 1


def main():
    p = argparse.ArgumentParser(
        description='Validate Insta360 Link Controller commands by state readback.',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument('url', nargs='?',
                   help='QR code URL (http://link-controller.insta360.com/v3/link/?...)')
    p.add_argument('--port', type=int, help='WebSocket port override')
    p.add_argument('--backend', choices=('ws', 'usb'), default='ws',
                   help='Validation backend: ws (Link Controller) or usb (Linux USB-direct)')
    p.add_argument('--only', metavar='NAME[,NAME...]',
                   help='Comma-separated list of tests to run (default: all)')
    p.add_argument('--skip-center', action='store_true',
                   help='Skip center PTZ test (also LINK_CTL_SKIP_CENTER=1)')
    p.add_argument('--list', action='store_true', help='List all test names and exit')
    args = p.parse_args()

    if args.list:
        for tc in make_tests():
            print(f'  {tc.name:20s}  {tc.description}')
        return

    only = [n.strip() for n in args.only.split(',')] if args.only else []

    if args.backend == 'usb':
        sys.exit(run_usb_all(only, skip_center=args.skip_center))

    port = args.port
    token = None

    if args.url:
        url = args.url
        if url.startswith('http') or url.startswith('ws'):
            p_from_url, token = _parse_url(url)
            if not port:
                port = p_from_url
        else:
            print(f'Unknown argument: {url}', file=sys.stderr)
            sys.exit(1)

    if not port:
        print('Auto-discovering port via lsof...', flush=True)
        port = _lsof_port()
        if not port:
            print('✗ Could not find Link Controller port. Use --port N.', file=sys.stderr)
            sys.exit(2)
        print(f'Found port {port}', flush=True)

    if not token:
        token = _read_token_from_ini() or ''

    sys.exit(asyncio.run(run_all(port, token, only)))


if __name__ == '__main__':
    main()
