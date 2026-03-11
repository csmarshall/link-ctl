#!/usr/bin/env python3
"""validate.py — Live command validator for the Insta360 Link Controller.

Sends each known command, reads device state before and after via the WebSocket
handshake, and reports PASS/FAIL for each test. No tshark or sudo required.

Prerequisites
─────────────
1. Hardware:
   • Insta360 Link (original) camera connected via USB
   • Camera in normal (non-privacy) position so pan/tilt isn't blocked

2. Software — Insta360 Link Controller app MUST be running:
   • macOS:   /Applications/Insta360 Link Controller.app  (≥ v2.2.1)
   • Windows: Start Menu → Insta360 Link Controller
   The app can be minimized; its WebSocket server stays active.

3. Python dependencies:
   • Python ≥ 3.11
   • websockets ≥ 11:  pip install websockets

4. Exclusivity:
   • Only ONE WebSocket controller may be in control at a time.
   • Close the mobile web remote (from the QR code) before running this script.
   • If link-ctl is installed via pipx, use its Python:
       ~/.local/pipx/venvs/link-ctl/bin/python3 validate.py

5. This file must be in the same directory as link_ctl.py.

Usage
─────
    python3 validate.py                    # auto-discover port
    python3 validate.py --port 49924       # explicit port
    python3 validate.py "http://..."       # parse port+token from QR URL
    python3 validate.py --skip NAME        # skip a test by name (repeatable)
    python3 validate.py --only NAME        # run only one test (repeatable)

Exit codes
──────────
    0  All attempted tests passed
    1  One or more tests failed (validation completed)
    2  Setup/connection failure (could not reach camera)

Updating paramType mappings
────────────────────────────
If a firmware update changes the protocol, run apitest.py with a tshark capture
to re-identify which paramTypes trigger which state changes, then update
ParamTypeV2 in link_ctl.py and the test cases at the bottom of this file.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse, parse_qs

sys.path.insert(0, str(Path(__file__).parent))
from link_ctl import (
    LinkClient,
    ParamTypeV2, AIMode, VideoMode,
    build_value_change, build_zoom,
    build_joystick, build_joystick_stop,
    build_preset_recall, build_preset_save, build_preset_delete,
    _lsof_port, _read_token_from_ini,
)


# ── Helpers ──────────────────────────────────────────────────────────────────

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


# ── Camera interface ──────────────────────────────────────────────────────────

async def get_status(port: int, token: str) -> dict | None:
    """Connect, handshake, return device info dict (first device), then disconnect."""
    client = LinkClient(port, token=token)
    try:
        await client.connect()
    except Exception as e:
        _log(f'  ✗ connect failed: {e}')
        return None
    ok, err = await client.handshake()
    await client.close()
    if not ok:
        _log(f'  ✗ handshake failed: {err}')
        return None
    info = client.device_info
    if not info or not info.get('devices'):
        _log('  ✗ no device info received')
        return None
    return info['devices'][0]


async def send_cmd(port: int, token: str, payload: bytes, extra_wait: float = 0.0):
    """Connect, handshake, send payload, optional extra wait, disconnect."""
    client = LinkClient(port, token=token)
    await client.connect()
    ok, err = await client.handshake()
    if not ok:
        await client.close()
        raise RuntimeError(f'handshake failed: {err}')
    await client._send(payload)
    if extra_wait > 0:
        await asyncio.sleep(extra_wait)
    await client.close()


async def send_joystick_pulse(
    port: int, token: str, serial: str,
    pan_vel: float, tilt_vel: float, duration: float,
):
    """Hold joystick for `duration` seconds then send stop. Guarantees stop fires."""
    client = LinkClient(port, token=token)
    await client.connect()
    ok, err = await client.handshake()
    if not ok:
        await client.close()
        raise RuntimeError(f'handshake failed: {err}')
    try:
        await client._send(build_joystick(serial, pan_vel, tilt_vel))
        await asyncio.sleep(duration)
    finally:
        await client._send(build_joystick_stop(serial))
    await client.close()


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
    # Returns bytes to send to restore original state
    build_restore: Callable[[str, dict], bytes]
    # Checks before/after dicts: returns (passed, message)
    check: Callable[[dict, dict], tuple[bool, str]]
    # Seconds to wait between send and status re-read
    settle: float = 1.0
    # Optional setup commands sent (in order) before build_on
    prereqs: list[Callable[[str], bytes]] = field(default_factory=list)
    # Optional teardown commands sent (in order) after build_restore
    postreqs: list[Callable[[str], bytes]] = field(default_factory=list)


async def run_test(
    tc: TestCase,
    port: int,
    token: str,
    serial: str,
) -> Result:
    _log(f'  → {tc.description}')

    # 1. Baseline state
    before = await get_status(port, token)
    if before is None:
        return Result(tc.name, False, 'could not read baseline state')
    await asyncio.sleep(0.3)

    # 1b. Prerequisites
    for prereq in tc.prereqs:
        try:
            await send_cmd(port, token, prereq(serial))
            await asyncio.sleep(0.5)
        except Exception as e:
            return Result(tc.name, False, f'prereq failed: {e}')

    # 2. Send test command
    payload = tc.build_on(serial)
    try:
        await send_cmd(port, token, payload)
    except Exception as e:
        return Result(tc.name, False, f'send failed: {e}')
    await asyncio.sleep(tc.settle)

    # 3. Read new state
    after = await get_status(port, token)
    if after is None:
        return Result(tc.name, False, 'could not read post-command state')
    await asyncio.sleep(0.3)

    # 4. Check
    passed, msg = tc.check(before, after)

    # 5. Restore
    restore_payload = tc.build_restore(serial, before)
    if restore_payload is not None:
        try:
            await send_cmd(port, token, restore_payload)
        except Exception as e:
            _log(f'    ! restore failed: {e}')
    await asyncio.sleep(tc.settle)

    # 5b. Post-teardown
    for postreq in tc.postreqs:
        try:
            await send_cmd(port, token, postreq(serial))
            await asyncio.sleep(0.5)
        except Exception as e:
            _log(f'    ! postreq failed: {e}')

    return Result(tc.name, passed, msg)


# ── Test case definitions ─────────────────────────────────────────────────────

def make_tests() -> list[TestCase]:
    """Define all validation test cases. Each test:
    1. Sends a command
    2. Reads device state back via a fresh handshake
    3. Asserts the expected field changed
    4. Restores the original state
    """

    def _int_field_test(field: str, on_val: int, off_val: int):
        """Generic check: field must equal on_val after command, restore to off_val."""
        def check(before: dict, after: dict) -> tuple[bool, str]:
            got = after.get(field)
            if got == on_val:
                return True, f'{field}={got} ✓'
            return False, f'{field}={got} (expected {on_val}, before={before.get(field)})'
        return check

    def _bool_field_test(field: str, expected_on: bool):
        def check(before: dict, after: dict) -> tuple[bool, str]:
            got = after.get(field)
            if got == expected_on:
                return True, f'{field}={got} ✓'
            return False, f'{field}={got} (expected {expected_on}, before={before.get(field)})'
        return check

    def _mode_test(expected_mode: int):
        def check(before: dict, after: dict) -> tuple[bool, str]:
            got = after.get('mode')
            if got == expected_mode:
                return True, f'mode={got} ✓'
            return False, f'mode={got} (expected {expected_mode}, before={before.get("mode")})'
        return check

    tests = [
        # ── Zoom ─────────────────────────────────────────────────────────────
        TestCase(
            name='zoom',
            description='Zoom: set to 200, verify zoom.curValue=200, restore to 100',
            build_on=lambda s: build_zoom(s, 200),
            build_restore=lambda s, before: build_zoom(
                s, before.get('zoom', {}).get('curValue', 100)),
            check=lambda before, after: (
                after.get('zoom', {}).get('curValue') == 200,
                f"zoom.curValue={after.get('zoom', {}).get('curValue')} "
                f"(expected 200, before={before.get('zoom', {}).get('curValue')})"
            ) if after.get('zoom', {}).get('curValue') != 200 else (
                True, f"zoom.curValue=200 ✓"
            ),
            settle=1.0,
        ),

        # ── AI modes ─────────────────────────────────────────────────────────
        TestCase(
            name='track',
            description='AI tracking: enable, verify mode=1, restore normal',
            build_on=lambda s: build_value_change(s, ParamTypeV2.AI_MODE, AIMode.TRACKING),
            build_restore=lambda s, _: build_value_change(s, ParamTypeV2.AI_MODE, AIMode.NORMAL),
            check=_mode_test(VideoMode.TRACKING),
            settle=1.5,
        ),
        TestCase(
            name='overhead',
            description='Overhead mode: enable, verify mode=4, restore normal',
            build_on=lambda s: build_value_change(s, ParamTypeV2.AI_MODE, AIMode.OVERHEAD),
            build_restore=lambda s, _: build_value_change(s, ParamTypeV2.AI_MODE, AIMode.NORMAL),
            check=_mode_test(VideoMode.OVERHEAD),
            settle=1.5,
        ),
        TestCase(
            name='deskview',
            description='DeskView mode: enable, verify mode=5, restore normal',
            build_on=lambda s: build_value_change(s, ParamTypeV2.AI_MODE, AIMode.DESKVIEW),
            build_restore=lambda s, _: build_value_change(s, ParamTypeV2.AI_MODE, AIMode.NORMAL),
            check=_mode_test(VideoMode.DESKVIEW),
            settle=1.5,
        ),
        TestCase(
            name='whiteboard',
            description='Whiteboard mode: enable, verify mode=6, restore normal',
            build_on=lambda s: build_value_change(s, ParamTypeV2.AI_MODE, AIMode.WHITEBOARD),
            build_restore=lambda s, _: build_value_change(s, ParamTypeV2.AI_MODE, AIMode.NORMAL),
            check=_mode_test(VideoMode.WHITEBOARD),
            settle=1.5,
        ),

        # ── Image settings ───────────────────────────────────────────────────
        TestCase(
            name='hdr',
            description='HDR: toggle on, verify hdr=True, restore off',
            build_on=lambda s: build_value_change(s, ParamTypeV2.HDR, '1'),
            build_restore=lambda s, before: build_value_change(
                s, ParamTypeV2.HDR, '1' if before.get('hdr') else '0'),
            check=_bool_field_test('hdr', True),
            settle=1.0,
        ),
        TestCase(
            name='brightness',
            description='Brightness: set to 75, verify brightness=75, restore',
            build_on=lambda s: build_value_change(s, ParamTypeV2.BRIGHTNESS, '75'),
            build_restore=lambda s, before: build_value_change(
                s, ParamTypeV2.BRIGHTNESS, str(before.get('brightness', 50))),
            check=_int_field_test('brightness', 75, 50),
            settle=1.0,
        ),
        TestCase(
            name='contrast',
            description='Contrast: set to 75, verify contrast=75, restore',
            build_on=lambda s: build_value_change(s, ParamTypeV2.CONTRAST, '75'),
            build_restore=lambda s, before: build_value_change(
                s, ParamTypeV2.CONTRAST, str(before.get('contrast', 50))),
            check=_int_field_test('contrast', 75, 50),
            settle=1.0,
        ),
        TestCase(
            name='saturation',
            description='Saturation: set to 75, verify saturation=75, restore',
            build_on=lambda s: build_value_change(s, ParamTypeV2.SATURATION, '75'),
            build_restore=lambda s, before: build_value_change(
                s, ParamTypeV2.SATURATION, str(before.get('saturation', 50))),
            check=_int_field_test('saturation', 75, 50),
            settle=1.0,
        ),
        TestCase(
            name='sharpness',
            description='Sharpness: set to 75, verify sharpness=75, restore',
            build_on=lambda s: build_value_change(s, ParamTypeV2.SHARPNESS, '75'),
            build_restore=lambda s, before: build_value_change(
                s, ParamTypeV2.SHARPNESS, str(before.get('sharpness', 50))),
            check=_int_field_test('sharpness', 75, 50),
            settle=1.0,
        ),

        TestCase(
            name='exposurecomp',
            description='Exposure comp: set to 75, verify exposureComp=75, restore',
            build_on=lambda s: build_value_change(s, ParamTypeV2.EXPOSURE_COMP, '75'),
            build_restore=lambda s, before: build_value_change(
                s, ParamTypeV2.EXPOSURE_COMP, str(before.get('exposureComp', 50))),
            check=_int_field_test('exposureComp', 75, 50),
            settle=1.0,
        ),

        # ── Auto controls ─────────────────────────────────────────────────────
        TestCase(
            name='autoexposure',
            description='Auto exposure: toggle off, verify autoExposure=False, restore on',
            build_on=lambda s: build_value_change(s, ParamTypeV2.AUTO_EXPOSURE, '0'),
            build_restore=lambda s, before: build_value_change(
                s, ParamTypeV2.AUTO_EXPOSURE, '1' if before.get('autoExposure', True) else '0'),
            check=_bool_field_test('autoExposure', False),
            settle=1.0,
        ),
        TestCase(
            name='awb',
            description='Auto white balance: toggle off, verify autoWhiteBalance=False, restore',
            build_on=lambda s: build_value_change(s, ParamTypeV2.AUTO_WB, '0'),
            build_restore=lambda s, before: build_value_change(
                s, ParamTypeV2.AUTO_WB, '1' if before.get('autoWhiteBalance', True) else '0'),
            check=_bool_field_test('autoWhiteBalance', False),
            settle=1.0,
        ),

        # ── WB temperature ────────────────────────────────────────────────────
        # Requires AWB=off to take effect; prereq/postreq bracket the test.
        TestCase(
            name='wb-temp',
            description='WB temp: AWB off → set 5000K, verify wbTemp=5000, restore',
            prereqs=[lambda s: build_value_change(s, ParamTypeV2.AUTO_WB, '0')],
            build_on=lambda s: build_value_change(s, ParamTypeV2.WB_TEMP, '5000'),
            build_restore=lambda s, before: build_value_change(
                s, ParamTypeV2.WB_TEMP, str(before.get('wbTemp', 4200))),
            postreqs=[lambda s: build_value_change(s, ParamTypeV2.AUTO_WB, '1')],
            check=_int_field_test('wbTemp', 5000, 4200),
            settle=1.0,
        ),

        # ── Presets ───────────────────────────────────────────────────────────
        # DeviceInfo.curPresetPos does not update after preset operations, and
        # presets store pan/tilt only (not zoom), so there is no DeviceInfo field
        # that changes in a predictable way after save/recall/delete.
        # These are smoke tests: verify the command sends and the camera doesn't
        # drop the connection. Wire format is confirmed correct from tshark captures.
        TestCase(
            name='preset-save',
            description='Preset save: ADD current position as new preset (smoke test — no readback)',
            build_on=lambda s: build_preset_save(s, 0),
            build_restore=lambda s, before: None,
            check=lambda before, after: (True, 'command accepted (no readback available) ✓'),
            settle=1.0,
        ),
        TestCase(
            name='preset',
            description='Preset recall: recall index 0 (smoke test — no readback)',
            build_on=lambda s: build_preset_recall(s, 0),
            build_restore=lambda s, before: None,
            check=lambda before, after: (True, 'command accepted (no readback available) ✓'),
            settle=1.0,
        ),
        TestCase(
            name='preset-delete',
            description='Preset delete: delete index 0 (smoke test — no readback)',
            build_on=lambda s: build_preset_delete(s, 0),
            build_restore=lambda s, before: None,
            check=lambda before, after: (True, 'command accepted (no readback available) ✓'),
            settle=1.0,
        ),
    ]
    return tests


# ── Main runner ───────────────────────────────────────────────────────────────

async def run_all(
    port: int,
    token: str,
    only: list[str],
    skip: list[str],
) -> int:
    """Run all tests. Returns exit code (0=all passed, 1=failures, 2=fatal)."""
    # Initial connection to get serial
    _log('Connecting to camera...')
    client = LinkClient(port, token=token)
    try:
        await client.connect()
    except Exception as e:
        print(f'✗ Cannot connect to ws://localhost:{port}/: {e}', file=sys.stderr)
        return 2
    ok, err = await client.handshake()
    await client.close()
    if not ok:
        print(f'✗ Handshake failed: {err}', file=sys.stderr)
        return 2

    serial = client.serial
    info = client.device_info
    if not serial or not info or not info.get('devices'):
        print('✗ No device info received during handshake', file=sys.stderr)
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
    if skip:
        tests = [t for t in tests if t.name not in skip]

    results: list[Result] = []
    for tc in tests:
        print(f'[TEST] {tc.name}')
        try:
            result = await run_test(tc, port, token, serial)
        except Exception as e:
            result = Result(tc.name, False, f'exception: {e}')
        status = 'PASS' if result.passed else 'FAIL'
        print(f'  [{status}] {result.message}')
        results.append(result)
        print()
        await asyncio.sleep(0.5)

    # Summary
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
    p.add_argument('--only', metavar='NAME', action='append', default=[],
                   help='Run only this test (repeatable)')
    p.add_argument('--skip', metavar='NAME', action='append', default=[],
                   help='Skip this test (repeatable)')
    p.add_argument('--list', action='store_true', help='List all test names and exit')
    args = p.parse_args()

    if args.list:
        for tc in make_tests():
            print(f'  {tc.name:20s}  {tc.description}')
        return

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

    sys.exit(asyncio.run(run_all(port, token, args.only, args.skip)))


if __name__ == '__main__':
    main()
