#!/usr/bin/env python3
"""xu_capture.py — Automated XU control discovery via uvc-probe + WebSocket.

Reverse-engineers the Insta360 Link's UVC Extension Unit protocol by:
  1. Reading XU register state via uvc-probe (IOKit, non-exclusive)
  2. Triggering operations via WebSocket (link_ctl.py API)
  3. Reading XU state again and diffing

No tshark, no SIP disable, no pcap — just direct XU register reads via IOKit
while the desktop app stays running.

Architecture
────────────
Phase A — CAPTURE: Snapshot XU state, trigger operation, snapshot again, diff
  1. Start `sudo tools/uvc-probe server` as a subprocess
  2. Ensure the desktop app is running (WS server must be up)
  3. For each operation:
     a. Take "before" snapshot (send newline to uvc-probe server)
     b. Trigger via WebSocket (existing link_ctl API)
     c. Wait for settling
     d. Take "after" snapshot
     e. Diff → record changed {unit, sel, before_hex, after_hex}
     f. Trigger inverse/reset, snapshot again
  4. Output: {operation → [changed unit/sel/before/after bytes]}

Phase B — REPLAY: Send captured XU bytes without the desktop app
  1. Kill the desktop app
  2. For each captured XU command: send via link_usb.XUBackend.xu_set()
  3. Restart the desktop app
  4. Read device state via WebSocket (LinkClient) and validate

Phase C — REPORT: Generate validated command mapping
  1. JSON file with {command, xu_unit, xu_selector, data_hex, validated: bool}

Prerequisites
─────────────
  1. Insta360 Link camera connected and desktop app running
  2. tools/uvc-probe compiled (clang command in uvc-probe.m header)
  3. sudo access (uvc-probe needs IOKit USB access)
  4. For Phase B replay: link_usb.py (in parent directory)

Usage
─────
  # Full capture → replay → validate cycle
  sudo python tools/xu_capture.py

  # Capture only (no replay)
  sudo python tools/xu_capture.py --capture-only

  # Dry-run: show planned operations without doing anything
  python tools/xu_capture.py --dry-run

  # Run specific operations only
  sudo python tools/xu_capture.py --only zoom,hdr,brightness

  # Custom settle time (seconds)
  sudo python tools/xu_capture.py --settle 3.0
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from link_usb import (
    get_xu_backend, find_insta360_device, LinkUSB,
    XU_SELECTOR_NAMES, UVC_SET_CUR, UVC_GET_CUR,
)


# ── Logging ──────────────────────────────────────────────────────────────────

def ts() -> str:
    t = time.time()
    return time.strftime('%H:%M:%S') + f'.{int((t % 1) * 1000):03d}'

def log(msg: str):
    print(f'[{ts()}] {msg}', flush=True)

def sep(label: str):
    print(f'\n[{ts()}] {"─" * 3} {label} {"─" * 40}', flush=True)


# ── Operations to capture ────────────────────────────────────────────────────

from typing import Callable, Awaitable

# Type for custom async trigger: (port, token) -> None
TriggerFunc = Callable[[int, str], Awaitable[None]]

# Known value ranges for specific paramTypes
PARAM_DEFAULTS: dict[int, tuple[str, str]] = {
    # paramType: (low/off value, high/on value)
    4:  ('100', '400'),     # zoom
    5:  ('0', '1'),         # AI mode (0=normal)
    21: ('6500', '2800'),   # WB temp Kelvin
    27: ('0', '1'),         # anti-flicker (0=auto)
}


@dataclass
class Operation:
    """A single operation to trigger during Phase A capture.

    For simple paramType operations: set ws_param_type + test_value.
    For multi-step sequences (e.g. privacy): set trigger_on/trigger_off callbacks.
    """
    name: str
    description: str
    ws_param_type: int = 0
    test_value: str = ''
    restore_value: str = ''    # explicit restore value (overrides auto-detect)
    ws_state_key: str = ''     # key in device info to read current state
    settle_seconds: float = 2.0
    trigger_on: TriggerFunc | None = None   # custom async trigger for "set"
    trigger_off: TriggerFunc | None = None  # custom async trigger for "restore"

    @property
    def is_custom(self) -> bool:
        return self.trigger_on is not None

    def pick_values(self, device_state: dict | None) -> tuple[str, str]:
        """Return (test_value, restore_value) based on current device state."""
        if self.is_custom:
            return self.test_value or 'on', self.restore_value or 'off'

        test = self.test_value
        restore = self.restore_value or self._opposite(test)

        if device_state and self.ws_state_key:
            cur = device_state.get(self.ws_state_key)
            if cur is not None:
                cur_str = self._state_to_param(cur)
                if cur_str == test:
                    test, restore = restore, test
                else:
                    restore = cur_str
        return test, restore

    def _opposite(self, val: str) -> str:
        """Return a plausible opposite/default value for restore."""
        if self.ws_param_type in PARAM_DEFAULTS:
            low, high = PARAM_DEFAULTS[self.ws_param_type]
            return low if val == high else high
        if val in ('0', '1'):
            return '1' if val == '0' else '0'
        return '50'

    def _state_to_param(self, state_val) -> str:
        if isinstance(state_val, bool):
            return '1' if state_val else '0'
        if isinstance(state_val, dict):
            return str(state_val.get('curValue', 100))
        return str(state_val)


# ── Custom trigger: privacy mode ─────────────────────────────────────────────

async def _preset_save_recall(port: int, token: str) -> None:
    """Save current position as preset 19 (scratch slot), then recall preset 0."""
    from link_ctl import LinkClient, build_preset_save, build_preset_recall
    client = LinkClient(port, token=token)
    await client.connect()
    ok, _ = await client.handshake()
    if not ok:
        await client.close()
        return
    # Save current position to slot 19 (scratch)
    await client._send(build_preset_save(client.serial, 19))
    await asyncio.sleep(1.0)
    # Recall preset 0 (should move camera if position differs)
    await client._send(build_preset_recall(client.serial, 0))
    await client.close()


async def _preset_restore(port: int, token: str) -> None:
    """Recall preset 19 (our scratch save), then delete it."""
    from link_ctl import LinkClient, build_preset_recall, build_preset_delete
    client = LinkClient(port, token=token)
    await client.connect()
    ok, _ = await client.handshake()
    if not ok:
        await client.close()
        return
    await client._send(build_preset_recall(client.serial, 19))
    await asyncio.sleep(2.0)
    await client._send(build_preset_delete(client.serial, 19))
    await client.close()


async def _camera_off(port: int, token: str) -> None:
    """Turn camera preview OFF via desktop app GUI click."""
    _click_app_camera_toggle()
    await asyncio.sleep(3.0)


async def _camera_on(port: int, token: str) -> None:
    """Turn camera preview ON via desktop app GUI click."""
    _click_app_camera_toggle()
    await asyncio.sleep(3.0)


def _click_app_camera_toggle() -> None:
    """Click the camera on/off toggle in the desktop app via cliclick.

    Activates the app first, then clicks the camera toggle button.
    Button position discovered via cliclick p with manual hover.
    The position is window-relative, so we read the window position
    each time and offset from it.
    """
    # Activate the app
    subprocess.run(
        ['osascript', '-e', 'tell application "Insta360 Link Controller" to activate'],
        capture_output=True, timeout=5)
    time.sleep(0.3)

    # Get current window position so we can compute absolute click coords
    result = subprocess.run(
        ['osascript', '-e',
         'tell application "System Events" to tell process "Webcam-desktop" '
         'to return position of window 1'],
        capture_output=True, text=True, timeout=5)
    # Parse "x, y" response
    try:
        parts = result.stdout.strip().split(',')
        wx, wy = int(parts[0].strip()), int(parts[1].strip())
    except (ValueError, IndexError):
        # Fallback to last known absolute position
        wx, wy = 940, 528

    # Camera toggle is at ~(123, 694) relative to window top-left
    # Discovered: absolute (1063, 1222) with window at (940, 528)
    cx = wx + 123
    cy = wy + 694
    subprocess.run(['cliclick', f'c:{cx},{cy}'], capture_output=True, timeout=5)


async def _joystick_pan_right(port: int, token: str) -> None:
    """Pan right at half speed for 1.5s, then stop."""
    from link_ctl import LinkClient, build_joystick, build_joystick_stop
    client = LinkClient(port, token=token)
    await client.connect()
    ok, _ = await client.handshake()
    if not ok:
        await client.close()
        return
    await client._send(build_joystick(client.serial, 0.5, 0.0))
    await asyncio.sleep(1.5)
    await client._send(build_joystick_stop(client.serial))
    await client.close()


async def _joystick_pan_left(port: int, token: str) -> None:
    """Pan left at half speed for 1.5s, then stop."""
    from link_ctl import LinkClient, build_joystick, build_joystick_stop
    client = LinkClient(port, token=token)
    await client.connect()
    ok, _ = await client.handshake()
    if not ok:
        await client.close()
        return
    await client._send(build_joystick(client.serial, -0.5, 0.0))
    await asyncio.sleep(1.5)
    await client._send(build_joystick_stop(client.serial))
    await client.close()


async def _joystick_tilt_up(port: int, token: str) -> None:
    """Tilt up at half speed for 1.5s, then stop."""
    from link_ctl import LinkClient, build_joystick, build_joystick_stop
    client = LinkClient(port, token=token)
    await client.connect()
    ok, _ = await client.handshake()
    if not ok:
        await client.close()
        return
    await client._send(build_joystick(client.serial, 0.0, 0.5))
    await asyncio.sleep(1.5)
    await client._send(build_joystick_stop(client.serial))
    await client.close()


async def _center_reset(port: int, token: str) -> None:
    """Center reset (paramType=3)."""
    await ws_send_command(port, token, 3, '')


async def _awb_off_then_wb(port: int, token: str) -> None:
    """Disable AWB, then set WB temp to 2800K (AWB must be off for temp to take effect)."""
    await ws_send_command(port, token, 20, '0')    # AWB off
    await asyncio.sleep(1.0)
    await ws_send_command(port, token, 21, '2800')  # WB temp 2800K


async def _awb_restore_wb(port: int, token: str) -> None:
    """Set WB temp to 6500K, then re-enable AWB."""
    await ws_send_command(port, token, 21, '6500')  # WB temp 6500K
    await asyncio.sleep(0.5)
    await ws_send_command(port, token, 20, '1')    # AWB on


# ── Operations list ──────────────────────────────────────────────────────────

OPERATIONS = [
    Operation('zoom', 'Zoom to 400',
              ws_param_type=4, test_value='400',
              ws_state_key='zoom', settle_seconds=2.0),
    Operation('tracking', 'AI Tracking on',
              ws_param_type=5, test_value='1',
              ws_state_key='mode', settle_seconds=3.0),
    Operation('deskview', 'DeskView on',
              ws_param_type=5, test_value='5',
              ws_state_key='mode', settle_seconds=3.0),
    Operation('whiteboard', 'Whiteboard on',
              ws_param_type=5, test_value='6',
              ws_state_key='mode', settle_seconds=3.0),
    Operation('overhead', 'Overhead on',
              ws_param_type=5, test_value='4',
              ws_state_key='mode', settle_seconds=3.0),
    Operation('hdr', 'HDR toggle',
              ws_param_type=26, test_value='1',
              ws_state_key='hdr', settle_seconds=3.0),
    Operation('brightness', 'Brightness to 100',
              ws_param_type=22, test_value='100',
              ws_state_key='brightness', settle_seconds=1.5),
    Operation('contrast', 'Contrast to 100',
              ws_param_type=23, test_value='100',
              ws_state_key='contrast', settle_seconds=1.5),
    Operation('saturation', 'Saturation to 0',
              ws_param_type=24, test_value='0',
              ws_state_key='saturation', settle_seconds=1.5),
    Operation('sharpness', 'Sharpness to 100',
              ws_param_type=25, test_value='100',
              ws_state_key='sharpness', settle_seconds=1.5),
    Operation('exposurecomp', 'Exposure comp to 100',
              ws_param_type=16, test_value='100',
              ws_state_key='exposureComp', settle_seconds=1.5),
    Operation('autoexposure', 'Auto Exposure toggle',
              ws_param_type=17, test_value='0',
              ws_state_key='autoExposure', settle_seconds=2.0),
    Operation('awb', 'Auto White Balance toggle',
              ws_param_type=20, test_value='0',
              ws_state_key='autoWhiteBalance', settle_seconds=2.0),
    Operation('wb_temp', 'AWB off → WB 2800K → WB 6500K → AWB on',
              trigger_on=_awb_off_then_wb, trigger_off=_awb_restore_wb,
              test_value='on', restore_value='off',
              settle_seconds=2.0),
    Operation('anti_flicker_50', 'Anti-flicker → 50Hz',
              ws_param_type=27, test_value='1',
              settle_seconds=1.5),
    Operation('anti_flicker_60', 'Anti-flicker → 60Hz',
              ws_param_type=27, test_value='2',
              settle_seconds=1.5),
    Operation('mirror', 'Mirror toggle',
              ws_param_type=2, test_value='1',
              ws_state_key='mirror', settle_seconds=2.0),
    Operation('gesture_zoom', 'Gesture zoom toggle',
              ws_param_type=39, test_value='1',
              settle_seconds=2.0),
    Operation('autofocus', 'Auto focus toggle',
              ws_param_type=18, test_value='0',
              settle_seconds=2.0),
    Operation('smart_comp_on', 'Smart composition toggle',
              ws_param_type=11, test_value='1',
              ws_state_key='smartComposition', settle_seconds=2.0),
    Operation('smart_comp_head', 'Smart comp framing → Head',
              ws_param_type=10, test_value='1',
              settle_seconds=2.0),
    Operation('pan_right', 'Joystick pan right → center',
              trigger_on=_joystick_pan_right, trigger_off=_center_reset,
              test_value='on', restore_value='off',
              settle_seconds=2.0),
    Operation('pan_left', 'Joystick pan left → center',
              trigger_on=_joystick_pan_left, trigger_off=_center_reset,
              test_value='on', restore_value='off',
              settle_seconds=2.0),
    Operation('tilt_up', 'Joystick tilt up → center',
              trigger_on=_joystick_tilt_up, trigger_off=_center_reset,
              test_value='on', restore_value='off',
              settle_seconds=2.0),
    Operation('preset_recall', 'Save preset 19 → recall preset 0 → restore',
              trigger_on=_preset_save_recall, trigger_off=_preset_restore,
              test_value='on', restore_value='off',
              settle_seconds=3.0),
    Operation('camera_off', 'Camera preview off/on via desktop app GUI',
              trigger_on=_camera_off, trigger_off=_camera_on,
              test_value='off', restore_value='on',
              settle_seconds=1.0),  # triggers have built-in waits
]


# ── uvc-probe server interface ───────────────────────────────────────────────

PROBE_BIN = Path(__file__).resolve().parent / 'uvc-probe'


@dataclass
class XUEntry:
    """A single XU register value."""
    unit: int
    sel: int
    length: int
    hex_data: str


_SNAPSHOT_RE = re.compile(
    r'unit=\s*(\d+)\s+sel=(0x[0-9a-fA-F]+)\s+len=(\d+)\s+hex=([0-9a-fA-F]+)')


def parse_snapshot(text: str) -> list[XUEntry]:
    """Parse uvc-probe snapshot output into XUEntry list."""
    entries = []
    for line in text.strip().splitlines():
        m = _SNAPSHOT_RE.search(line)
        if not m:
            continue
        entries.append(XUEntry(
            unit=int(m.group(1)),
            sel=int(m.group(2), 16),
            length=int(m.group(3)),
            hex_data=m.group(4)))
    return entries


def diff_snapshots(before: list[XUEntry], after: list[XUEntry]) -> list[dict]:
    """Diff two snapshots, return list of changed entries."""
    before_map = {(e.unit, e.sel): e for e in before}
    after_map = {(e.unit, e.sel): e for e in after}

    changes = []
    for key in after_map:
        if key in before_map and before_map[key].hex_data != after_map[key].hex_data:
            b, a = before_map[key], after_map[key]
            changes.append({
                'unit': a.unit,
                'sel': a.sel,
                'sel_hex': f'0x{a.sel:02x}',
                'len': a.length,
                'before': b.hex_data,
                'after': a.hex_data,
            })
    return changes


class ProbeServer:
    """Manages a `uvc-probe server` subprocess.

    Sends a newline to trigger a snapshot, reads lines until "END",
    parses the result into XUEntry list.
    """

    def __init__(self):
        self.proc: subprocess.Popen | None = None

    def start(self) -> None:
        if not PROBE_BIN.exists():
            raise FileNotFoundError(
                f'{PROBE_BIN} not found. Compile with:\n'
                f'  clang -o tools/uvc-probe tools/uvc-probe.m '
                f'-framework IOKit -framework CoreFoundation -framework Foundation -ObjC')

        log(f'Starting uvc-probe server: {PROBE_BIN} server')
        self.proc = subprocess.Popen(
            [str(PROBE_BIN), 'server'],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
        )
        # Wait for "ready" message on stderr
        # uvc-probe prints discovery info to stderr, then "server: ready (...)"
        while True:
            line = self.proc.stderr.readline().decode('utf-8', errors='replace')
            if not line:
                raise RuntimeError('uvc-probe server exited during startup')
            log(f'  probe: {line.rstrip()}')
            if 'ready' in line:
                break

    def snapshot(self) -> list[XUEntry]:
        """Request a snapshot and parse the response."""
        if not self.proc or self.proc.poll() is not None:
            raise RuntimeError('uvc-probe server is not running')

        self.proc.stdin.write(b'\n')
        self.proc.stdin.flush()

        lines = []
        while True:
            line = self.proc.stdout.readline().decode('utf-8', errors='replace')
            if not line:
                raise RuntimeError('uvc-probe server closed stdout unexpectedly')
            if line.strip() == 'END':
                break
            lines.append(line)

        return parse_snapshot('\n'.join(lines))

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.stdin.close()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
            log('uvc-probe server stopped')


# ── Desktop app management ───────────────────────────────────────────────────

APP_NAME_MACOS = 'Insta360 Link Controller'
APP_PATH_MACOS = '/Applications/Insta360 Link Controller.app'


def is_app_running() -> bool:
    system = platform.system()
    if system == 'Darwin':
        try:
            out = subprocess.run(
                ['pgrep', '-f', APP_NAME_MACOS],
                capture_output=True, text=True, timeout=5)
            return out.returncode == 0
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return False
    elif system == 'Linux':
        try:
            out = subprocess.run(
                ['pgrep', '-f', 'Insta360'],
                capture_output=True, text=True, timeout=5)
            return out.returncode == 0
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return False
    return False


def launch_app() -> bool:
    if is_app_running():
        log('Desktop app already running')
        return True
    system = platform.system()
    if system == 'Darwin':
        if os.path.exists(APP_PATH_MACOS):
            log(f'Launching {APP_NAME_MACOS}...')
            subprocess.Popen(['open', APP_PATH_MACOS])
            for _ in range(30):
                time.sleep(1)
                if is_app_running():
                    log('Desktop app started')
                    time.sleep(5)
                    return True
            log('Desktop app did not start within 30s')
            return False
        else:
            log(f'App not found at {APP_PATH_MACOS}')
            return False
    else:
        log('Auto-launch not supported on this platform. Start the app manually.')
        return False


def kill_app() -> bool:
    if not is_app_running():
        return True
    system = platform.system()
    if system == 'Darwin':
        try:
            subprocess.run(['pkill', '-f', APP_NAME_MACOS], timeout=5)
            time.sleep(2)
            return not is_app_running()
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return False
    elif system == 'Linux':
        try:
            subprocess.run(['pkill', '-f', 'Insta360'], timeout=5)
            time.sleep(2)
            return not is_app_running()
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return False
    return False


# ── WebSocket helpers ────────────────────────────────────────────────────────

async def ws_send_command(port: int, token: str, param_type: int, value: str) -> None:
    from link_ctl import LinkClient, build_value_change
    client = LinkClient(port, token=token)
    await client.connect()
    ok, _ = await client.handshake()
    if ok:
        await client.send_command(
            build_value_change(client.serial, param_type, value), wait_ms=300)
    await client.close()


async def ws_get_status(port: int, token: str) -> dict | None:
    from link_ctl import LinkClient
    client = LinkClient(port, token=token)
    try:
        await client.connect()
        ok, _ = await client.handshake()
        await client.close()
        if ok and client.device_info and client.device_info.get('devices'):
            return client.device_info['devices'][0]
    except Exception as e:
        log(f'  WS status error: {e}')
    return None


async def discover_ws_connection() -> tuple[int, str] | None:
    from link_ctl import _lsof_port, _read_token_from_ini
    port = _lsof_port()
    if not port:
        log('Could not discover WebSocket port. Is the desktop app running?')
        return None
    token = _read_token_from_ini() or ''
    return port, token


# ── Phase A: Capture via uvc-probe server ────────────────────────────────────

@dataclass
class CaptureResult:
    """Result of capturing one operation."""
    operation: str
    description: str
    ws_param_type: int
    on_changes: list[dict] = field(default_factory=list)
    off_changes: list[dict] = field(default_factory=list)
    on_value: str = ''
    off_value: str = ''
    error: str = ''


async def run_phase_a(operations: list[Operation],
                      settle_override: float | None = None) -> list[CaptureResult]:
    """Phase A: For each operation, snapshot XU state before/after via uvc-probe server."""
    sep('PHASE A: CAPTURE (uvc-probe server)')
    results: list[CaptureResult] = []

    # Ensure desktop app is running
    if not is_app_running():
        if not launch_app():
            log('Cannot proceed without the desktop app running.')
            return results

    # Discover WS connection
    ws_info = await discover_ws_connection()
    if not ws_info:
        return results
    port, token = ws_info
    log(f'WebSocket: port={port}, token={"*" * len(token) if token else "(none)"}')

    # Read baseline WS state (used to pick test/restore values)
    device_state = await ws_get_status(port, token)
    if device_state:
        log(f'Baseline WS state: mode={device_state.get("mode")}, '
            f'hdr={device_state.get("hdr")}, brightness={device_state.get("brightness")}, '
            f'zoom={device_state.get("zoom")}')
    else:
        log('WARNING: Could not read device state — will use default test/restore values')

    # Start uvc-probe server
    probe = ProbeServer()
    try:
        probe.start()
    except (FileNotFoundError, RuntimeError) as e:
        log(f'Failed to start uvc-probe server: {e}')
        return results

    # Take initial baseline snapshot
    baseline_snap = probe.snapshot()
    log(f'Baseline: {len(baseline_snap)} XU registers')

    for op in operations:
        settle = settle_override if settle_override is not None else op.settle_seconds
        test_val, restore_val = op.pick_values(device_state)

        sep(f'CAPTURE: {op.name} — {op.description}')
        log(f'  test={test_val!r}, restore={restore_val!r} '
            f'(paramType={op.ws_param_type})')

        result = CaptureResult(
            operation=op.name, description=op.description,
            ws_param_type=op.ws_param_type,
            on_value=test_val, off_value=restore_val)

        try:
            # Step 1: Snapshot → SET test value → Snapshot → diff
            before = probe.snapshot()
            if op.is_custom:
                log(f'  → SET: {op.name} (custom trigger)')
                await op.trigger_on(port, token)
            else:
                log(f'  → SET: paramType={op.ws_param_type}, value={test_val!r}')
                await ws_send_command(port, token, op.ws_param_type, test_val)
            await asyncio.sleep(settle)
            after = probe.snapshot()

            changes = diff_snapshots(before, after)
            result.on_changes = changes
            if changes:
                for c in changes:
                    log(f'    CHANGED unit={c["unit"]:2d} sel={c["sel_hex"]} '
                        f'{c["before"]} → {c["after"]}')
            else:
                log(f'    (no XU register changes detected)')

            # Step 2: Snapshot → RESTORE original value → Snapshot → diff
            before2 = probe.snapshot()
            if op.is_custom:
                log(f'  → RESTORE: {op.name} (custom trigger)')
                await op.trigger_off(port, token)
            else:
                log(f'  → RESTORE: paramType={op.ws_param_type}, value={restore_val!r}')
                await ws_send_command(port, token, op.ws_param_type, restore_val)
            await asyncio.sleep(settle)
            after2 = probe.snapshot()

            changes2 = diff_snapshots(before2, after2)
            result.off_changes = changes2
            if changes2:
                for c in changes2:
                    log(f'    CHANGED unit={c["unit"]:2d} sel={c["sel_hex"]} '
                        f'{c["before"]} → {c["after"]}')
            else:
                log(f'    (no XU register changes detected)')

            # Brief pause between operations
            await asyncio.sleep(0.5)

        except Exception as e:
            result.error = str(e)
            log(f'    ERROR: {e}')

        results.append(result)

    probe.stop()

    # Summary
    sep('CAPTURE SUMMARY')
    for r in results:
        on_n = len(r.on_changes)
        off_n = len(r.off_changes)
        status = 'ERROR' if r.error else f'{on_n} on / {off_n} off changes'
        log(f'  {r.operation:20s}  {status}')

    return results


# ── Phase B: Replay ──────────────────────────────────────────────────────────

async def run_phase_b(results: list[CaptureResult]) -> list[dict]:
    """Phase B: Replay captured XU SET_CUR commands directly, then validate via WS."""
    sep('PHASE B: REPLAY')
    replay_results = []

    # Collect unique SET_CUR-equivalent commands from on_changes
    # (the "on" changes show what bytes to write to activate a feature)
    set_commands = []
    for r in results:
        if r.error:
            continue
        for change in r.on_changes:
            set_commands.append({
                'operation': r.operation,
                'action': 'on',
                'xu_unit': change['unit'],
                'xu_selector': change['sel'],
                'data_hex': change['after'],
                'before_hex': change['before'],
            })

    if not set_commands:
        log('No XU changes captured. Nothing to replay.')
        return replay_results

    log(f'Found {len(set_commands)} XU register changes to replay')

    # Kill desktop app for direct USB access
    log('Stopping desktop app for direct USB replay...')
    if not kill_app():
        log('WARNING: Could not stop desktop app. Replay may conflict.')
    time.sleep(3)

    # Open USB backend
    device = find_insta360_device()
    if not device:
        log('No camera found for USB replay.')
        launch_app()
        return replay_results

    backend = get_xu_backend()
    try:
        backend.open(device.device_path or None)
    except RuntimeError as e:
        log(f'Failed to open USB device: {e}')
        launch_app()
        return replay_results

    # Replay each command
    for cmd in set_commands:
        sel = cmd['xu_selector']
        unit = cmd['xu_unit']
        data = bytes.fromhex(cmd['data_hex'])
        sel_name = XU_SELECTOR_NAMES.get(sel, f'UNKNOWN_{sel}')

        log(f'  REPLAY: {cmd["operation"]}/on → '
            f'unit={unit} sel={sel} ({sel_name}) data={cmd["data_hex"][:40]}')
        try:
            backend.xu_set(unit, sel, data)
            log(f'    sent OK')
            cmd['replay_success'] = True
        except OSError as e:
            log(f'    FAILED: {e}')
            cmd['replay_success'] = False
        replay_results.append(cmd)
        time.sleep(0.5)

    backend.close()

    # Restart desktop app for WS validation
    log('Restarting desktop app for state validation...')
    launch_app()
    time.sleep(5)

    # Validate via WS
    ws_info = await discover_ws_connection()
    if ws_info:
        port, token = ws_info
        state = await ws_get_status(port, token)
        if state:
            log(f'  Post-replay state: mode={state.get("mode")}, '
                f'hdr={state.get("hdr")}, brightness={state.get("brightness")}')
            for r in replay_results:
                r['post_state'] = state
        else:
            log('  Could not read state after replay')
    else:
        log('  Could not connect to WS for validation')

    return replay_results


# ── Phase C: Report ──────────────────────────────────────────────────────────

def generate_report(results: list[CaptureResult],
                    replay_results: list[dict],
                    report_file: str) -> None:
    """Generate final JSON report with command mappings."""
    sep('PHASE C: REPORT')

    report = {
        'generated_at': time.strftime('%Y-%m-%d %H:%M:%S'),
        'platform': platform.system(),
        'operations': {},
    }

    for r in results:
        op_report = {
            'ws_param_type': r.ws_param_type,
            'description': r.description,
            'on_value': r.on_value,
            'off_value': r.off_value,
            'on_changes': r.on_changes,
            'off_changes': r.off_changes,
            'error': r.error or None,
        }

        # Find corresponding replay results
        replays = [rr for rr in replay_results if rr.get('operation') == r.operation]
        op_report['replay_results'] = replays
        op_report['validated'] = any(rr.get('replay_success') for rr in replays)

        report['operations'][r.operation] = op_report

    Path(report_file).write_text(json.dumps(report, indent=2, default=str))
    log(f'Report written: {report_file}')

    # Summary table
    validated = sum(1 for op in report['operations'].values() if op.get('validated'))
    total = len(report['operations'])
    has_changes = sum(1 for op in report['operations'].values()
                      if op.get('on_changes') or op.get('off_changes'))

    log(f'\n  Operations tested:  {total}')
    log(f'  With XU changes:   {has_changes}')
    log(f'  Replay validated:  {validated}')

    # Print the command map
    print(f'\n── Discovered XU Command Map ──')
    for op_name, op_data in report['operations'].items():
        validated_mark = 'V' if op_data.get('validated') else '?'
        for change in op_data.get('on_changes', []):
            sel_name = XU_SELECTOR_NAMES.get(change['sel'], '?')
            print(f'  [{validated_mark}] {op_name:20s}  '
                  f'unit={change["unit"]:2d} sel={change["sel_hex"]} ({sel_name:20s})  '
                  f'{change["before"]} → {change["after"]}')
        if not op_data.get('on_changes') and not op_data.get('error'):
            print(f'  [ ] {op_name:20s}  (no XU changes detected)')
        if op_data.get('error'):
            print(f'  [E] {op_name:20s}  ERROR: {op_data["error"]}')


# ── Dry-run mode ─────────────────────────────────────────────────────────────

def dry_run(operations: list[Operation]):
    sep('DRY RUN')
    print('Phase A will trigger these operations while reading XU state via uvc-probe:\n')

    total_time = 0.0
    for op in operations:
        if op.is_custom:
            print(f'  {op.name:20s}  (custom trigger)  settle={op.settle_seconds}s')
        else:
            print(f'  {op.name:20s}  paramType={op.ws_param_type:2d}  '
                  f'test={op.test_value!r:>6s}  state_key={op.ws_state_key or "(none)":20s}  '
                  f'settle={op.settle_seconds}s')
        total_time += op.settle_seconds * 2 + 1.0

    print(f'\nTotal operations: {len(operations)}')
    print(f'Estimated capture time: ~{total_time:.0f}s')

    print(f'\nRequirements:')
    print(f'  - tools/uvc-probe compiled: {"YES" if PROBE_BIN.exists() else "NO — compile first"}')
    print(f'  - Desktop app running:      {"YES" if is_app_running() else "NO — start it first"}')
    print(f'  - sudo access:              required for uvc-probe')

    print(f'\nPhase B will:')
    print(f'  1. Kill the desktop app')
    print(f'  2. Replay each XU SET_CUR command via direct USB (link_usb)')
    print(f'  3. Restart the desktop app')
    print(f'  4. Read state via WebSocket and validate')


# ── Main ─────────────────────────────────────────────────────────────────────

async def async_main(args):
    timestamp = time.strftime('%Y%m%d-%H%M%S')
    report_file = args.report or f'xu_capture_report_{timestamp}.json'

    # Filter operations if --only specified
    operations = OPERATIONS
    if args.only:
        names = {n.strip() for n in args.only.split(',')}
        operations = [op for op in OPERATIONS if op.name in names]
        unknown = names - {op.name for op in operations}
        if unknown:
            log(f'Unknown operations: {", ".join(sorted(unknown))}')
            log(f'Available: {", ".join(op.name for op in OPERATIONS)}')
            return
        if not operations:
            log('No matching operations found.')
            return

    if args.dry_run:
        dry_run(operations)
        return

    # Phase A: Capture
    results = await run_phase_a(operations, settle_override=args.settle)
    if not results:
        log('No results captured. Check the setup and try again.')
        return

    # Save capture results immediately
    capture_file = Path(report_file).with_suffix('.capture.json')
    capture_data = []
    for r in results:
        capture_data.append({
            'operation': r.operation, 'description': r.description,
            'ws_param_type': r.ws_param_type,
            'on_value': r.on_value, 'off_value': r.off_value,
            'on_changes': r.on_changes, 'off_changes': r.off_changes,
            'error': r.error or None,
        })
    capture_file.write_text(json.dumps(capture_data, indent=2))
    log(f'Capture data saved: {capture_file}')

    if not args.capture_only:
        # Phase B: Replay
        replay_results = await run_phase_b(results)
        # Phase C: Report
        generate_report(results, replay_results, report_file)
    else:
        generate_report(results, [], report_file)
        log('Phase B (replay) skipped.')


def main():
    parser = argparse.ArgumentParser(
        description='Automated XU control discovery via uvc-probe + WebSocket.',
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--dry-run', action='store_true',
                        help='Show planned operations without doing anything')
    parser.add_argument('--capture-only', action='store_true',
                        help='Run only Phase A (capture); skip replay and validation')
    parser.add_argument('--report', type=str, default=None,
                        help='Output report file path')
    parser.add_argument('--only', type=str, default=None,
                        help='Comma-separated list of operation names to run')
    parser.add_argument('--settle', type=float, default=None,
                        help='Override settle time (seconds) for all operations')
    args = parser.parse_args()

    asyncio.run(async_main(args))


if __name__ == '__main__':
    main()
