#!/usr/bin/env python3
"""xu_capture.py — Synthetic USB capture, replay, and validation harness.

Automates reverse-engineering the Insta360 Link's UVC Extension Unit protocol
by capturing USB traffic while triggering known operations, then replaying the
captured XU commands directly (without the desktop app) and validating state.

Architecture
────────────
Phase A — CAPTURE: Trigger operations while USB-sniffing
  1. Start tshark USB capture on the appropriate interface
  2. Ensure the desktop app is running (launch if needed)
  3. For each operation (zoom, tracking, HDR, privacy, etc.):
     a. Record timestamp
     b. Trigger via WebSocket (existing API) or AppleScript (GUI-only)
     c. Wait for settling
     d. Trigger inverse/reset
     e. Record timestamp
  4. Stop tshark capture
  5. Parse pcap file — correlate timestamps to USB control transfers
  6. Output: {operation → [xu_selector, xu_data_bytes]}

Phase B — REPLAY: Send captured XU bytes without the desktop app
  1. Kill the desktop app
  2. For each captured XU command: send via link_usb.XUBackend.xu_set()
  3. Restart the desktop app
  4. Read device state via WebSocket (LinkClient) and validate

Phase C — REPORT: Generate validated command mapping
  1. JSON file with {command, xu_selector, xu_data_hex, validated: bool}

Prerequisites
─────────────
  1. Insta360 Link camera connected and desktop app installed
  2. tshark installed (brew install wireshark / apt install tshark)
  3. USB capture interface available:
     - macOS: sudo ifconfig XHC20 up (may require SIP disabled on Catalina+)
     - Linux: sudo modprobe usbmon
  4. For Phase B replay: link_usb.py (in parent directory)

Usage
─────
  # Full capture → replay → validate cycle
  python tools/xu_capture.py

  # Dry-run: show planned operations without doing anything
  python tools/xu_capture.py --dry-run

  # Analyze a pre-existing pcap file (skip Phase A capture)
  python tools/xu_capture.py --pcap /path/to/capture.pcapng

  # Calibrate AppleScript button positions for GUI automation
  python tools/xu_capture.py --calibrate

  # Run only Phase A (capture) without replay/validation
  python tools/xu_capture.py --capture-only

  # Run only Phase B (replay) using a previous capture report
  python tools/xu_capture.py --replay report.json

USB capture setup
─────────────────
macOS:
  # Check if XHC20 interface exists
  ifconfig -l | grep XHC

  # Enable it (may need SIP disabled on Catalina+)
  sudo ifconfig XHC20 up

  # If XHC20 doesn't exist, SIP may need to be disabled:
  #   1. Boot into Recovery (hold power on Apple Silicon, Cmd+R on Intel)
  #   2. Terminal → csrutil disable → reboot
  #   3. After capture, re-enable: csrutil enable

Linux:
  sudo modprobe usbmon
  # Find the right bus number for the camera:
  lsusb | grep -i insta
  # Bus 001 → usbmon1, Bus 002 → usbmon2, etc.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import shutil
import signal
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

@dataclass
class Operation:
    """A single operation to trigger during Phase A capture."""
    name: str
    trigger: str           # 'ws' or 'applescript'
    description: str
    # For WS-triggered operations:
    ws_param_type: int = 0
    ws_on_value: str = ''
    ws_off_value: str = ''
    # For AppleScript-triggered operations:
    applescript_on: str = ''
    applescript_off: str = ''
    # Settling time after each command
    settle_seconds: float = 2.0
    # Expected XU selector (best guess from proto; confirmed by capture)
    expected_xu_selector: int = -1


# WebSocket-triggered operations (reuse existing known paramTypes from link_ctl)
WS_OPERATIONS = [
    Operation('zoom', 'ws', 'Zoom 100 → 400 → 100',
              ws_param_type=4, ws_on_value='400', ws_off_value='100',
              expected_xu_selector=4, settle_seconds=2.0),
    Operation('tracking_on', 'ws', 'AI Tracking on → off',
              ws_param_type=5, ws_on_value='1', ws_off_value='0',
              expected_xu_selector=2, settle_seconds=3.0),
    Operation('deskview', 'ws', 'DeskView on → off',
              ws_param_type=5, ws_on_value='5', ws_off_value='0',
              expected_xu_selector=2, settle_seconds=3.0),
    Operation('whiteboard', 'ws', 'Whiteboard on → off',
              ws_param_type=5, ws_on_value='6', ws_off_value='0',
              expected_xu_selector=2, settle_seconds=3.0),
    Operation('overhead', 'ws', 'Overhead on → off',
              ws_param_type=5, ws_on_value='4', ws_off_value='0',
              expected_xu_selector=2, settle_seconds=3.0),
    Operation('hdr', 'ws', 'HDR on → off',
              ws_param_type=26, ws_on_value='1', ws_off_value='0',
              settle_seconds=2.0),
    Operation('brightness', 'ws', 'Brightness 50 → 100 → 50',
              ws_param_type=22, ws_on_value='100', ws_off_value='50',
              settle_seconds=1.5),
    Operation('contrast', 'ws', 'Contrast 50 → 100 → 50',
              ws_param_type=23, ws_on_value='100', ws_off_value='50',
              settle_seconds=1.5),
    Operation('saturation', 'ws', 'Saturation 50 → 0 → 50',
              ws_param_type=24, ws_on_value='0', ws_off_value='50',
              settle_seconds=1.5),
    Operation('sharpness', 'ws', 'Sharpness 50 → 100 → 50',
              ws_param_type=25, ws_on_value='100', ws_off_value='50',
              settle_seconds=1.5),
    Operation('autoexposure', 'ws', 'Auto Exposure off → on',
              ws_param_type=17, ws_on_value='0', ws_off_value='1',
              expected_xu_selector=30, settle_seconds=2.0),
    Operation('awb', 'ws', 'Auto White Balance off → on',
              ws_param_type=20, ws_on_value='0', ws_off_value='1',
              settle_seconds=2.0),
    Operation('anti_flicker_50', 'ws', 'Anti-flicker → 50Hz → Auto',
              ws_param_type=27, ws_on_value='1', ws_off_value='0',
              settle_seconds=1.5),
    Operation('anti_flicker_60', 'ws', 'Anti-flicker → 60Hz → Auto',
              ws_param_type=27, ws_on_value='2', ws_off_value='0',
              settle_seconds=1.5),
    Operation('mirror', 'ws', 'Mirror on → off',
              ws_param_type=2, ws_on_value='1', ws_off_value='0',
              settle_seconds=2.0),
    Operation('gesture_zoom', 'ws', 'Gesture zoom on → off',
              ws_param_type=39, ws_on_value='1', ws_off_value='0',
              expected_xu_selector=5, settle_seconds=2.0),
    Operation('autofocus', 'ws', 'Auto focus off → on',
              ws_param_type=18, ws_on_value='0', ws_off_value='1',
              expected_xu_selector=15, settle_seconds=2.0),
]

# AppleScript-triggered operations (GUI-only features not in WS API)
APPLESCRIPT_OPERATIONS = [
    Operation('privacy', 'applescript', 'Privacy mode on → off',
              expected_xu_selector=27, settle_seconds=3.0,
              applescript_on='_privacy_toggle_on',
              applescript_off='_privacy_toggle_off'),
]

ALL_OPERATIONS = WS_OPERATIONS + APPLESCRIPT_OPERATIONS


# ── Calibration data ─────────────────────────────────────────────────────────

CALIBRATION_FILE = Path.home() / '.config' / 'link-ctl' / 'gui_calibration.json'

DEFAULT_CALIBRATION = {
    'app_name': 'Insta360 Link Controller',
    'privacy_button': {
        'description': 'Privacy toggle button position (x, y) in app window',
        'x': None,
        'y': None,
        'method': 'click_at_position',
        'notes': 'Run --calibrate to set these positions',
    },
}


def load_calibration() -> dict:
    if CALIBRATION_FILE.exists():
        try:
            return json.loads(CALIBRATION_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return DEFAULT_CALIBRATION


def save_calibration(cal: dict) -> None:
    CALIBRATION_FILE.parent.mkdir(parents=True, exist_ok=True)
    CALIBRATION_FILE.write_text(json.dumps(cal, indent=2))


# ── AppleScript GUI automation ───────────────────────────────────────────────

def _run_applescript(script: str) -> str:
    """Run an AppleScript and return stdout."""
    result = subprocess.run(
        ['osascript', '-e', script],
        capture_output=True, text=True, timeout=15)
    if result.returncode != 0:
        log(f'  AppleScript error: {result.stderr.strip()}')
    return result.stdout.strip()


def _activate_app(app_name: str) -> None:
    """Bring the desktop app to the front."""
    _run_applescript(f'''
        tell application "{app_name}"
            activate
        end tell
    ''')
    time.sleep(0.5)


def _get_app_window_bounds(app_name: str) -> dict | None:
    """Get the app window position and size."""
    out = _run_applescript(f'''
        tell application "System Events"
            tell process "{app_name}"
                if (count of windows) > 0 then
                    set w to window 1
                    set p to position of w
                    set s to size of w
                    return (item 1 of p as text) & "," & (item 2 of p as text) & "," & (item 1 of s as text) & "," & (item 2 of s as text)
                end if
            end tell
        end tell
    ''')
    if out:
        try:
            parts = [int(x) for x in out.split(',')]
            return {'x': parts[0], 'y': parts[1], 'width': parts[2], 'height': parts[3]}
        except (ValueError, IndexError):
            pass
    return None


def _click_at_position(x: int, y: int) -> None:
    """Click at absolute screen coordinates using AppleScript."""
    _run_applescript(f'''
        tell application "System Events"
            click at {{{x}, {y}}}
        end tell
    ''')


def applescript_privacy_toggle(cal: dict, action: str) -> bool:
    """Toggle privacy mode via AppleScript GUI automation.

    Returns True if the action was performed (or attempted).
    """
    app_name = cal.get('app_name', 'Insta360 Link Controller')
    privacy = cal.get('privacy_button', {})
    x, y = privacy.get('x'), privacy.get('y')

    if x is None or y is None:
        log('  Privacy button position not calibrated.')
        log('  Run: python tools/xu_capture.py --calibrate')
        log('  Falling back to manual prompt...')
        input(f'  → Toggle privacy {action.upper()} in the desktop app, then press Enter: ')
        return True

    _activate_app(app_name)
    time.sleep(0.3)

    # Get current window bounds to compute absolute position
    bounds = _get_app_window_bounds(app_name)
    if bounds:
        abs_x = bounds['x'] + x
        abs_y = bounds['y'] + y
        log(f'  Clicking privacy button at ({abs_x}, {abs_y})')
        _click_at_position(abs_x, abs_y)
    else:
        log(f'  Could not get window bounds; clicking at ({x}, {y}) as absolute')
        _click_at_position(x, y)

    return True


# ── Calibration mode ─────────────────────────────────────────────────────────

def run_calibrate():
    """Interactive calibration: identify GUI button positions."""
    sep('CALIBRATION MODE')
    cal = load_calibration()
    app_name = cal.get('app_name', 'Insta360 Link Controller')

    print(f'This will help you identify button positions in "{app_name}".')
    print(f'The app must be open and visible.\n')

    _activate_app(app_name)
    bounds = _get_app_window_bounds(app_name)
    if bounds:
        print(f'Window found: position=({bounds["x"]}, {bounds["y"]}), '
              f'size=({bounds["width"]}x{bounds["height"]})')
    else:
        print('Could not find the app window. Is it open?')
        return

    print(f'\n── Privacy Button ──')
    print(f'Move your mouse over the privacy toggle button in the app.')
    print(f'Enter the position RELATIVE to the app window (not screen coordinates).')
    print(f'Tip: window top-left is (0, 0).')

    try:
        x_str = input('  Privacy button X (relative to window): ').strip()
        y_str = input('  Privacy button Y (relative to window): ').strip()
        x, y = int(x_str), int(y_str)
        cal['privacy_button'] = {
            'x': x, 'y': y,
            'method': 'click_at_position',
            'description': 'Privacy toggle button position relative to window',
        }
        save_calibration(cal)
        print(f'\n  Saved: privacy button at ({x}, {y}) relative to window.')
        print(f'  Config: {CALIBRATION_FILE}')

        # Test click
        test = input('\n  Test the click now? (y/n): ').strip().lower()
        if test == 'y':
            abs_x = bounds['x'] + x
            abs_y = bounds['y'] + y
            print(f'  Clicking at ({abs_x}, {abs_y})...')
            _click_at_position(abs_x, abs_y)
            print(f'  Did the privacy button toggle? If not, adjust coordinates.')

    except (ValueError, KeyboardInterrupt):
        print('\nCalibration cancelled.')


# ── tshark USB capture management ────────────────────────────────────────────

def find_usb_capture_interface() -> str | None:
    """Find the USB capture interface for tshark."""
    system = platform.system()
    if system == 'Darwin':
        # macOS: XHC20, XHC0, etc.
        try:
            out = subprocess.run(['ifconfig', '-l'], capture_output=True, text=True, timeout=5)
            for iface in out.stdout.split():
                if iface.startswith('XHC'):
                    return iface
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass
        return None

    elif system == 'Linux':
        # Linux: usbmonN
        # Find the bus number for the Insta360 Link
        try:
            out = subprocess.run(['lsusb'], capture_output=True, text=True, timeout=5)
            for line in out.stdout.splitlines():
                if 'insta360' in line.lower():
                    # "Bus 001 Device 003: ..."
                    parts = line.split()
                    if len(parts) >= 2:
                        bus = parts[1].lstrip('0') or '0'
                        return f'usbmon{bus}'
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass
        # Fallback: try usbmon0 (captures all buses)
        if os.path.exists('/dev/usbmon0'):
            return 'usbmon0'
        return None

    return None


def check_tshark() -> bool:
    """Check if tshark is available."""
    return shutil.which('tshark') is not None


def start_tshark_capture(interface: str, output_file: str) -> subprocess.Popen | None:
    """Start tshark USB capture in the background."""
    if not check_tshark():
        log('tshark not found. Install: brew install wireshark (macOS) or apt install tshark (Linux)')
        return None

    cmd = [
        'sudo', 'tshark',
        '-i', interface,
        '-w', output_file,
        # Capture USB control transfers
        '-f', 'usb',
    ]
    log(f'Starting tshark: {" ".join(cmd)}')
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        time.sleep(2)  # Let tshark initialize
        if proc.poll() is not None:
            stderr = proc.stderr.read().decode() if proc.stderr else ''
            log(f'tshark failed to start: {stderr}')
            return None
        log(f'tshark capturing on {interface} → {output_file}')
        return proc
    except (FileNotFoundError, PermissionError) as e:
        log(f'Failed to start tshark: {e}')
        return None


def stop_tshark(proc: subprocess.Popen) -> None:
    """Stop a running tshark capture."""
    if proc and proc.poll() is None:
        proc.send_signal(signal.SIGINT)
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        log('tshark stopped')


# ── Desktop app management ───────────────────────────────────────────────────

APP_NAME_MACOS = 'Insta360 Link Controller'
APP_PATH_MACOS = '/Applications/Insta360 Link Controller.app'


def is_app_running() -> bool:
    """Check if the Insta360 Link Controller desktop app is running."""
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
    """Launch the desktop app. Returns True if launched (or already running)."""
    if is_app_running():
        log('Desktop app already running')
        return True

    system = platform.system()
    if system == 'Darwin':
        if os.path.exists(APP_PATH_MACOS):
            log(f'Launching {APP_NAME_MACOS}...')
            subprocess.Popen(['open', APP_PATH_MACOS])
            # Wait for the app to start and WS server to come up
            for _ in range(30):
                time.sleep(1)
                if is_app_running():
                    log('Desktop app started')
                    time.sleep(5)  # Extra time for WS server init
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
    """Kill the desktop app. Returns True if killed (or wasn't running)."""
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



# ── WebSocket trigger (Phase A) ──────────────────────────────────────────────

async def ws_get_status(port: int, token: str) -> dict | None:
    """Read device state via WebSocket. Only call when browser/WS is free."""
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


async def ws_send_command(port: int, token: str, param_type: int, value: str) -> None:
    """Send a single WS command."""
    from link_ctl import LinkClient, build_value_change
    client = LinkClient(port, token=token)
    await client.connect()
    ok, _ = await client.handshake()
    if ok:
        await client.send_command(
            build_value_change(client.serial, param_type, value), wait_ms=300)
    await client.close()


async def discover_ws_connection() -> tuple[int, str] | None:
    """Find the WebSocket port and token."""
    from link_ctl import _lsof_port, _read_token_from_ini
    port = _lsof_port()
    if not port:
        log('Could not discover WebSocket port. Is the desktop app running?')
        return None
    token = _read_token_from_ini() or ''
    return port, token


# ── Phase A: Capture ─────────────────────────────────────────────────────────

@dataclass
class CaptureEvent:
    """A timestamped capture event."""
    operation: str
    action: str  # 'on' or 'off'
    timestamp: float
    ws_param_type: int = 0
    ws_value: str = ''
    expected_xu_selector: int = -1


async def run_phase_a(operations: list[Operation], pcap_file: str) -> list[CaptureEvent]:
    """Phase A: Trigger operations while tshark captures USB traffic.

    Returns a list of timestamped events for correlation with the pcap.
    """
    sep('PHASE A: CAPTURE')
    events: list[CaptureEvent] = []
    cal = load_calibration()

    # Find USB capture interface
    iface = find_usb_capture_interface()
    if not iface:
        log('No USB capture interface found.')
        log('macOS: sudo ifconfig XHC20 up')
        log('Linux: sudo modprobe usbmon')
        log('')
        log('Alternatively, start a manual capture and use --pcap afterwards.')
        return events

    # Start capture
    tshark_proc = start_tshark_capture(iface, pcap_file)
    if not tshark_proc:
        log('Failed to start tshark. Falling back to manual capture mode.')
        log(f'Start your own capture, then use: --pcap <file>')
        return events

    # Ensure desktop app is running
    if not launch_app():
        stop_tshark(tshark_proc)
        log('Cannot proceed without the desktop app running.')
        return events

    # Discover WS connection
    ws_info = await discover_ws_connection()
    if not ws_info:
        stop_tshark(tshark_proc)
        return events
    port, token = ws_info

    # Read baseline state
    log('Reading baseline state...')
    baseline = await ws_get_status(port, token)
    if baseline:
        log(f'  Baseline: mode={baseline.get("mode")}, hdr={baseline.get("hdr")}, '
            f'brightness={baseline.get("brightness")}')
    else:
        log('  Warning: could not read baseline state')

    # Execute each operation
    for op in operations:
        sep(f'CAPTURE: {op.name} — {op.description}')

        if op.trigger == 'ws':
            # ON
            t_on = time.time()
            log(f'  → ON: paramType={op.ws_param_type}, value={op.ws_on_value!r}')
            try:
                await ws_send_command(port, token, op.ws_param_type, op.ws_on_value)
            except Exception as e:
                log(f'  WS error: {e}')
                continue
            events.append(CaptureEvent(
                operation=op.name, action='on', timestamp=t_on,
                ws_param_type=op.ws_param_type, ws_value=op.ws_on_value,
                expected_xu_selector=op.expected_xu_selector))

            await asyncio.sleep(op.settle_seconds)

            # OFF / RESET
            t_off = time.time()
            log(f'  → OFF: paramType={op.ws_param_type}, value={op.ws_off_value!r}')
            try:
                await ws_send_command(port, token, op.ws_param_type, op.ws_off_value)
            except Exception as e:
                log(f'  WS error: {e}')
                continue
            events.append(CaptureEvent(
                operation=op.name, action='off', timestamp=t_off,
                ws_param_type=op.ws_param_type, ws_value=op.ws_off_value,
                expected_xu_selector=op.expected_xu_selector))

            await asyncio.sleep(1.0)

        elif op.trigger == 'applescript':
            if platform.system() != 'Darwin':
                log(f'  SKIP: AppleScript only available on macOS')
                continue

            # ON
            t_on = time.time()
            log(f'  → Privacy ON via AppleScript')
            applescript_privacy_toggle(cal, 'on')
            events.append(CaptureEvent(
                operation=op.name, action='on', timestamp=t_on,
                expected_xu_selector=op.expected_xu_selector))

            await asyncio.sleep(op.settle_seconds)

            # OFF
            t_off = time.time()
            log(f'  → Privacy OFF via AppleScript')
            applescript_privacy_toggle(cal, 'off')
            events.append(CaptureEvent(
                operation=op.name, action='off', timestamp=t_off,
                expected_xu_selector=op.expected_xu_selector))

            await asyncio.sleep(2.0)

    # Stop capture
    stop_tshark(tshark_proc)

    # Save event log
    event_log = Path(pcap_file).with_suffix('.events.json')
    event_data = [{
        'operation': e.operation, 'action': e.action,
        'timestamp': e.timestamp, 'ws_param_type': e.ws_param_type,
        'ws_value': e.ws_value, 'expected_xu_selector': e.expected_xu_selector,
    } for e in events]
    event_log.write_text(json.dumps(event_data, indent=2))
    log(f'Event log: {event_log}')
    log(f'Pcap file: {pcap_file}')
    log(f'Total events captured: {len(events)}')

    return events


# ── pcap analysis ────────────────────────────────────────────────────────────

@dataclass
class USBControlTransfer:
    """A parsed USB control transfer from tshark output."""
    timestamp: float
    direction: str          # 'OUT' (host→device) or 'IN' (device→host)
    bm_request_type: int
    b_request: int          # 0x01=SET_CUR, 0x81=GET_CUR, etc.
    w_value: int            # (selector << 8) for XU controls
    w_index: int            # (unit_id << 8 | interface) for XU controls
    data: bytes
    xu_selector: int = 0    # Decoded: w_value >> 8
    xu_unit_id: int = 0     # Decoded: w_index >> 8


def parse_pcap(pcap_file: str) -> list[USBControlTransfer]:
    """Parse a pcap file for USB control transfers using tshark.

    Extracts UVC class-specific control transfers (bmRequestType 0x21 or 0xA1)
    which are how XU GET/SET commands travel over USB.
    """
    if not os.path.exists(pcap_file):
        log(f'Pcap file not found: {pcap_file}')
        return []

    if not check_tshark():
        log('tshark not found — cannot parse pcap')
        return []

    # Extract USB control transfer fields
    cmd = [
        'tshark', '-r', pcap_file,
        '-Y', 'usb.transfer_type == 0x02',  # URB_CONTROL
        '-T', 'fields',
        '-e', 'frame.time_epoch',
        '-e', 'usb.endpoint_address.direction',
        '-e', 'usb.setup.bmRequestType',
        '-e', 'usb.setup.bRequest',
        '-e', 'usb.setup.wValue',
        '-e', 'usb.setup.wIndex',
        '-e', 'usb.data_fragment',
        '-E', 'header=n',
        '-E', 'separator=|',
    ]

    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        log(f'tshark parse error: {e}')
        return []

    transfers = []
    for line in out.stdout.strip().splitlines():
        parts = line.split('|')
        if len(parts) < 6:
            continue
        try:
            ts_str, direction, bm_req, b_req, w_val, w_idx = parts[:6]
            data_hex = parts[6] if len(parts) > 6 else ''

            timestamp = float(ts_str) if ts_str else 0.0
            bm_request_type = int(bm_req, 16) if bm_req else 0
            b_request = int(b_req, 16) if b_req else 0
            w_value = int(w_val, 16) if w_val else 0
            w_index = int(w_idx, 16) if w_idx else 0
            data = bytes.fromhex(data_hex.replace(':', '')) if data_hex else b''

            # Filter for UVC class-specific requests (0x21=SET, 0xA1=GET)
            if bm_request_type not in (0x21, 0xA1):
                continue

            xfer = USBControlTransfer(
                timestamp=timestamp,
                direction='OUT' if bm_request_type == 0x21 else 'IN',
                bm_request_type=bm_request_type,
                b_request=b_request,
                w_value=w_value,
                w_index=w_index,
                data=data,
                xu_selector=w_value >> 8,
                xu_unit_id=w_index >> 8,
            )
            transfers.append(xfer)
        except (ValueError, IndexError):
            continue

    log(f'Parsed {len(transfers)} UVC class-specific control transfers from {pcap_file}')
    return transfers


def correlate_events(events: list[CaptureEvent],
                     transfers: list[USBControlTransfer],
                     window_ms: float = 500) -> dict:
    """Correlate capture events with USB transfers by timestamp.

    For each event, find USB control transfers that occurred within
    window_ms milliseconds after the event timestamp.

    Returns: {operation_name: {action: [transfers]}}
    """
    result = {}
    window_s = window_ms / 1000.0

    for event in events:
        key = event.operation
        if key not in result:
            result[key] = {'on': [], 'off': []}

        matching = []
        for xfer in transfers:
            dt = xfer.timestamp - event.timestamp
            if 0 <= dt <= window_s:
                matching.append({
                    'delta_ms': round(dt * 1000, 1),
                    'direction': xfer.direction,
                    'request': UVC_REQ_NAMES.get(xfer.b_request, f'0x{xfer.b_request:02x}'),
                    'xu_selector': xfer.xu_selector,
                    'xu_unit_id': xfer.xu_unit_id,
                    'data': xfer.data.hex() if xfer.data else '',
                    'w_value': f'0x{xfer.w_value:04x}',
                    'w_index': f'0x{xfer.w_index:04x}',
                })

        result[key][event.action] = matching
        if matching:
            log(f'  {key}/{event.action}: {len(matching)} USB transfers within {window_ms}ms')
        else:
            log(f'  {key}/{event.action}: no USB transfers found within {window_ms}ms')

    return result



# ── Phase B: Replay ──────────────────────────────────────────────────────────

async def run_phase_b(correlation: dict, report_file: str) -> list[dict]:
    """Phase B: Replay captured XU commands directly, then validate via WS.

    1. Kill the desktop app
    2. Send each captured XU SET_CUR command directly via link_usb
    3. Restart the desktop app
    4. Read state via WS and compare to expected
    """
    sep('PHASE B: REPLAY')
    results = []

    # Extract SET_CUR commands from correlation data
    set_commands = []
    for op_name, actions in correlation.items():
        for action in ['on', 'off']:
            for xfer in actions.get(action, []):
                if xfer.get('request') == 'SET_CUR' and xfer.get('data'):
                    set_commands.append({
                        'operation': op_name,
                        'action': action,
                        'xu_selector': xfer['xu_selector'],
                        'xu_unit_id': xfer['xu_unit_id'],
                        'data': xfer['data'],
                    })

    if not set_commands:
        log('No SET_CUR commands found in correlation data. Cannot replay.')
        return results

    log(f'Found {len(set_commands)} SET_CUR commands to replay')

    # Kill desktop app
    log('Stopping desktop app for direct USB replay...')
    if not kill_app():
        log('WARNING: Could not stop desktop app. Replay may conflict.')

    time.sleep(3)

    # Open USB backend
    device = find_insta360_device()
    if not device:
        log('No camera found for USB replay.')
        launch_app()
        return results

    backend = get_xu_backend()
    try:
        backend.open(device.device_path or None)
    except RuntimeError as e:
        log(f'Failed to open USB device: {e}')
        launch_app()
        return results

    # Replay each command
    for cmd in set_commands:
        sel = cmd['xu_selector']
        data = bytes.fromhex(cmd['data'])
        sel_name = XU_SELECTOR_NAMES.get(sel, f'UNKNOWN_{sel}')

        log(f'  REPLAY: {cmd["operation"]}/{cmd["action"]} → '
            f'XU SET_CUR sel={sel} ({sel_name}) data={cmd["data"][:40]}...')
        try:
            backend.xu_set(cmd['xu_unit_id'], sel, data)
            log(f'    ✓ Sent successfully')
            cmd['replay_success'] = True
        except OSError as e:
            log(f'    ✗ Failed: {e}')
            cmd['replay_success'] = False
        results.append(cmd)
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
            for r in results:
                r['post_state'] = state
        else:
            log('  Could not read state after replay')
    else:
        log('  Could not connect to WS for validation')

    return results


# ── Phase C: Report ──────────────────────────────────────────────────────────

def generate_report(events: list[CaptureEvent], correlation: dict,
                    replay_results: list[dict], report_file: str) -> None:
    """Generate final JSON report with validated command mappings."""
    sep('PHASE C: REPORT')

    report = {
        'generated_at': time.strftime('%Y-%m-%d %H:%M:%S'),
        'platform': platform.system(),
        'operations': {},
    }

    for op_name, actions in correlation.items():
        op_report = {'actions': {}}
        for action in ['on', 'off']:
            transfers = actions.get(action, [])
            set_curs = [t for t in transfers if t.get('request') == 'SET_CUR']
            get_curs = [t for t in transfers if t.get('request') == 'GET_CUR']
            op_report['actions'][action] = {
                'set_cur_commands': set_curs,
                'get_cur_commands': get_curs,
                'total_usb_transfers': len(transfers),
            }

        # Find corresponding replay results
        replays = [r for r in replay_results if r.get('operation') == op_name]
        op_report['replay_results'] = replays
        op_report['validated'] = any(r.get('replay_success') for r in replays)

        report['operations'][op_name] = op_report

    # Write report
    Path(report_file).write_text(json.dumps(report, indent=2, default=str))
    log(f'Report written: {report_file}')

    # Summary
    validated = sum(1 for op in report['operations'].values() if op.get('validated'))
    total = len(report['operations'])
    log(f'\n  Operations captured: {total}')
    log(f'  Validated (replay succeeded): {validated}')
    log(f'  Not validated: {total - validated}')

    # Print the command map
    if any(report['operations'].values()):
        print(f'\n── Discovered XU Command Map ──')
        for op_name, op_data in report['operations'].items():
            for action, action_data in op_data.get('actions', {}).items():
                for cmd in action_data.get('set_cur_commands', []):
                    sel = cmd.get('xu_selector', '?')
                    sel_name = XU_SELECTOR_NAMES.get(sel, f'?') if isinstance(sel, int) else '?'
                    data = cmd.get('data', '')
                    validated = '✓' if op_data.get('validated') else '?'
                    print(f'  {validated} {op_name}/{action}: '
                          f'XU sel={sel} ({sel_name}) data={data[:32]}{"..." if len(data) > 32 else ""}')


# ── Dry-run mode ─────────────────────────────────────────────────────────────

def dry_run():
    """Print what would be captured without doing anything."""
    sep('DRY RUN')
    print('Phase A would trigger these operations while capturing USB traffic:\n')

    print('WebSocket-triggered operations (via existing link_ctl API):')
    for op in WS_OPERATIONS:
        sel = f'XU sel≈{op.expected_xu_selector}' if op.expected_xu_selector >= 0 else 'XU sel=?'
        print(f'  {op.name:20s}  paramType={op.ws_param_type:2d}  '
              f'{op.ws_on_value!r:>5s} → {op.ws_off_value!r:<5s}  ({sel})')

    print(f'\nAppleScript-triggered operations (GUI-only):')
    for op in APPLESCRIPT_OPERATIONS:
        sel = f'XU sel≈{op.expected_xu_selector}' if op.expected_xu_selector >= 0 else 'XU sel=?'
        print(f'  {op.name:20s}  (GUI click)  ({sel})')

    print(f'\nTotal operations: {len(ALL_OPERATIONS)}')
    print(f'Estimated capture time: ~{sum(op.settle_seconds * 2 + 2 for op in ALL_OPERATIONS):.0f}s')

    print(f'\nPhase B would:')
    print(f'  1. Kill the desktop app')
    print(f'  2. Replay each captured XU SET_CUR command via direct USB')
    print(f'  3. Restart the desktop app')
    print(f'  4. Read state via WebSocket and validate')

    print(f'\nPhase C would generate a JSON report with validated command mappings.')

    cal = load_calibration()
    privacy = cal.get('privacy_button', {})
    if privacy.get('x') is None:
        print(f'\n⚠  Privacy button not calibrated.')
        print(f'   Run: python tools/xu_capture.py --calibrate')
    else:
        print(f'\n✓  Privacy button calibrated at ({privacy["x"]}, {privacy["y"]})')


# ── Main ─────────────────────────────────────────────────────────────────────

async def async_main(args):
    timestamp = time.strftime('%Y%m%d-%H%M%S')
    pcap_file = args.pcap or f'xu_capture_{timestamp}.pcapng'
    report_file = args.report or f'xu_capture_report_{timestamp}.json'

    if args.dry_run:
        dry_run()
        return

    if args.calibrate:
        run_calibrate()
        return

    if args.pcap and os.path.exists(args.pcap):
        # Analyze existing pcap (skip Phase A)
        sep('ANALYZING EXISTING PCAP')
        events_file = Path(args.pcap).with_suffix('.events.json')
        if events_file.exists():
            events_data = json.loads(events_file.read_text())
            events = [CaptureEvent(**e) for e in events_data]
            log(f'Loaded {len(events)} events from {events_file}')
        else:
            log(f'No event log found at {events_file}')
            log('Cannot correlate without timestamps. Run a full capture first.')
            return

        transfers = parse_pcap(args.pcap)
    else:
        # Full Phase A capture
        events = await run_phase_a(ALL_OPERATIONS, pcap_file)
        if not events:
            log('No events captured. Check the setup and try again.')
            return
        transfers = parse_pcap(pcap_file)

    if not transfers:
        log('No USB control transfers found in capture.')
        log('Possible causes:')
        log('  - USB capture interface not active (sudo ifconfig XHC20 up)')
        log('  - Camera on a different USB bus')
        log('  - SIP preventing USB capture on macOS')
        return

    # Correlate events with USB transfers
    sep('CORRELATING EVENTS WITH USB TRANSFERS')
    correlation = correlate_events(events, transfers)

    if not args.capture_only:
        # Phase B: Replay
        replay_results = await run_phase_b(correlation, report_file)

        # Phase C: Report
        generate_report(events, correlation, replay_results, report_file)
    else:
        # Save correlation without replay
        correlation_file = Path(pcap_file).with_suffix('.correlation.json')
        correlation_file.write_text(json.dumps(correlation, indent=2, default=str))
        log(f'Correlation saved: {correlation_file}')
        log('Phase B (replay) skipped. Use --replay to replay later.')


def main():
    parser = argparse.ArgumentParser(
        description='Synthetic USB capture, replay, and validation harness.',
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--dry-run', action='store_true',
                        help='Show planned operations without doing anything')
    parser.add_argument('--calibrate', action='store_true',
                        help='Calibrate AppleScript button positions')
    parser.add_argument('--pcap', type=str, default=None,
                        help='Analyze existing pcap file (skip capture)')
    parser.add_argument('--capture-only', action='store_true',
                        help='Run only Phase A (capture); skip replay and validation')
    parser.add_argument('--report', type=str, default=None,
                        help='Output report file path')
    parser.add_argument('--replay', type=str, default=None,
                        help='Replay from a previous report JSON (Phase B only)')
    args = parser.parse_args()

    asyncio.run(async_main(args))


if __name__ == '__main__':
    main()
