#!/usr/bin/env python3
"""xu_discover.py — Enumerate UVC Extension Unit controls on the Insta360 Link.

Discovers the camera's XU capabilities by probing every known control selector
from the ControlSelector enum in insta360linkcontroller.proto.

For each selector, queries:
  GET_LEN  — data buffer size
  GET_INFO — capability flags (can_get, can_set, disabled, etc.)
  GET_CUR  — current value
  GET_MIN  — minimum value
  GET_MAX  — maximum value
  GET_DEF  — default value
  GET_RES  — resolution/step

Platforms
─────────
  Linux:  UVCIOC_CTRL_QUERY ioctl on /dev/videoN (non-exclusive)
  macOS:  libuvc via ctypes (exclusive — close Zoom/Teams first)
           brew install libuvc
  Windows: not yet supported

Usage
─────
  # Enumerate all XU controls
  python tools/xu_discover.py

  # Machine-readable output (for xu_capture.py)
  python tools/xu_discover.py --json

  # Show what would be probed without touching the camera
  python tools/xu_discover.py --dry-run

  # Probe a specific selector only
  python tools/xu_discover.py --selector 27

  # Override the XU unit ID (default is auto-detected or 3)
  python tools/xu_discover.py --unit-id 4

  # Specify device path (Linux)
  python tools/xu_discover.py --device /dev/video2
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Add parent directory to path so we can import link_usb
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from link_usb import (
    XU_SELECTOR_NAMES, XU_MAX_SELECTOR, XUControlInfo,
    get_xu_backend, find_insta360_device,
    UVC_GET_CUR, UVC_GET_MIN, UVC_GET_MAX, UVC_GET_DEF, UVC_GET_LEN,
    UVC_GET_INFO, UVC_GET_RES, UVC_REQ_NAMES,
    XU_INFO_GET_SUPPORTED, XU_INFO_SET_SUPPORTED, XU_INFO_DISABLED,
    DEFAULT_XU_UNIT_ID,
)


# ── Formatting helpers ───────────────────────────────────────────────────────

def hex_dump(data: bytes, max_bytes: int = 32) -> str:
    """Format bytes as hex string with optional truncation."""
    if not data:
        return '(empty)'
    hex_str = data.hex()
    if len(data) > max_bytes:
        hex_str = data[:max_bytes].hex() + f'... ({len(data)} bytes total)'
    # Insert spaces every 2 chars for readability
    spaced = ' '.join(hex_str[i:i+2] for i in range(0, min(len(hex_str), max_bytes * 2), 2))
    if len(data) > max_bytes:
        spaced += f' ... ({len(data)} bytes)'
    return spaced


def info_flags_str(flags: int) -> str:
    """Format GET_INFO capability flags as human-readable string."""
    parts = []
    if flags & XU_INFO_GET_SUPPORTED:
        parts.append('GET')
    if flags & XU_INFO_SET_SUPPORTED:
        parts.append('SET')
    if flags & XU_INFO_DISABLED:
        parts.append('DISABLED')
    if flags & 0x08:
        parts.append('AUTO_UPDATE')
    if flags & 0x10:
        parts.append('ASYNC')
    return ' | '.join(parts) if parts else f'0x{flags:02x}'


def print_control(info: XUControlInfo, verbose: bool = True) -> None:
    """Pretty-print a single XU control's information."""
    sel = info.selector
    name = info.name

    if not info.supported:
        if info.error:
            print(f'  [{sel:2d}] {name:45s}  ✗ error: {info.error}')
        else:
            print(f'  [{sel:2d}] {name:45s}  ✗ not supported (GET_LEN=0)')
        return

    flags = info_flags_str(info.info_flags)
    print(f'  [{sel:2d}] {name:45s}  ✓ len={info.length:4d}  info={flags}')

    if verbose and info.can_get:
        if info.cur_value:
            print(f'         GET_CUR: {hex_dump(info.cur_value)}')
        if info.min_value:
            print(f'         GET_MIN: {hex_dump(info.min_value)}')
        if info.max_value:
            print(f'         GET_MAX: {hex_dump(info.max_value)}')
        if info.def_value:
            print(f'         GET_DEF: {hex_dump(info.def_value)}')
        if info.res_value:
            print(f'         GET_RES: {hex_dump(info.res_value)}')


def control_to_dict(info: XUControlInfo) -> dict:
    """Convert XUControlInfo to a JSON-serializable dict."""
    d = {
        'selector': info.selector,
        'name': info.name,
        'supported': info.supported,
        'length': info.length,
        'info_flags': info.info_flags,
        'can_get': info.can_get,
        'can_set': info.can_set,
    }
    if info.cur_value:
        d['cur_value'] = info.cur_value.hex()
    if info.min_value:
        d['min_value'] = info.min_value.hex()
    if info.max_value:
        d['max_value'] = info.max_value.hex()
    if info.def_value:
        d['def_value'] = info.def_value.hex()
    if info.res_value:
        d['res_value'] = info.res_value.hex()
    if info.error:
        d['error'] = info.error
    return d


# ── Dry-run mode ─────────────────────────────────────────────────────────────

def dry_run(selectors: list[int], unit_id: int) -> None:
    """Print what would be probed without touching the camera."""
    print('DRY RUN — no camera commands will be sent.\n')
    print(f'Unit ID: {unit_id}')
    print(f'Selectors to probe: {len(selectors)}\n')
    for sel in selectors:
        name = XU_SELECTOR_NAMES.get(sel, f'UNKNOWN_{sel}')
        print(f'  [{sel:2d}] {name}')
        print(f'         → GET_LEN(unit={unit_id}, sel={sel})')
        print(f'         → GET_INFO(unit={unit_id}, sel={sel})')
        print(f'         → GET_CUR(unit={unit_id}, sel={sel}, size=<from GET_LEN>)')
        print(f'         → GET_MIN, GET_MAX, GET_DEF, GET_RES')
    print(f'\nTotal queries: {len(selectors) * 7} (max, if all supported)')


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description='Enumerate UVC Extension Unit controls on the Insta360 Link.',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__)
    parser.add_argument('--json', action='store_true',
                        help='Output machine-readable JSON')
    parser.add_argument('--dry-run', action='store_true',
                        help='Show planned probes without touching the camera')
    parser.add_argument('--selector', '-s', type=int, default=None,
                        help='Probe a single selector (0-30)')
    parser.add_argument('--unit-id', '-u', type=int, default=None,
                        help=f'Override XU unit ID (default: auto or {DEFAULT_XU_UNIT_ID})')
    parser.add_argument('--device', '-d', type=str, default=None,
                        help='Device path override (Linux: /dev/videoN)')
    parser.add_argument('--verbose', '-v', action='store_true', default=True,
                        help='Show full value dumps (default)')
    parser.add_argument('--quiet', '-q', action='store_true',
                        help='Show only supported controls')
    args = parser.parse_args()

    unit_id = args.unit_id if args.unit_id is not None else DEFAULT_XU_UNIT_ID

    # Determine which selectors to probe
    if args.selector is not None:
        selectors = [args.selector]
    else:
        selectors = list(range(XU_MAX_SELECTOR + 1))

    # Dry-run mode
    if args.dry_run:
        dry_run(selectors, unit_id)
        return

    # Find camera
    device = find_insta360_device()
    if device:
        if not args.json:
            print(f'Camera: {device.name}')
            if device.device_path:
                print(f'Device: {device.device_path}')
            if device.vid:
                print(f'VID:PID: {device.vid:04x}:{device.pid:04x}')
            print()
    else:
        if args.json:
            print(json.dumps({'error': 'No Insta360 Link camera found', 'controls': {}}))
        else:
            print('No Insta360 Link camera found.', file=sys.stderr)
            print('Check: is the camera connected via USB?', file=sys.stderr)
        sys.exit(1)

    # Open device
    backend = get_xu_backend()
    try:
        backend.open(args.device or (device.device_path if device.device_path else None))
    except RuntimeError as e:
        if args.json:
            print(json.dumps({'error': str(e), 'controls': {}}))
        else:
            print(f'Failed to open device: {e}', file=sys.stderr)
        sys.exit(2)

    # Probe controls
    results = {}
    supported_count = 0
    if not args.json:
        print(f'Probing {len(selectors)} XU control selector(s) on unit {unit_id}...\n')

    for sel in selectors:
        info = backend.query_control(unit_id, sel)
        results[sel] = info
        if info.supported:
            supported_count += 1
        if not args.json and not (args.quiet and not info.supported):
            print_control(info, verbose=args.verbose and not args.quiet)

    backend.close()

    # Summary
    if args.json:
        output = {
            'device': {
                'name': device.name,
                'path': device.device_path,
                'vid': device.vid,
                'pid': device.pid,
                'platform': device.platform,
            },
            'unit_id': unit_id,
            'controls': {str(sel): control_to_dict(info) for sel, info in results.items()},
            'summary': {
                'total_probed': len(selectors),
                'supported': supported_count,
            },
        }
        print(json.dumps(output, indent=2))
    else:
        print(f'\n── Summary ──')
        print(f'  Probed: {len(selectors)} selectors')
        print(f'  Supported: {supported_count}')
        print(f'  Not supported: {len(selectors) - supported_count}')

        # Highlight interesting selectors
        interesting = {
            27: 'XU_FUNC_ENABLE_CONTROL — likely privacy mode',
            2:  'XU_VIDEO_MODE_CONTROL — AI modes',
            4:  'XU_PTZ_CMD_CONTROL — PTZ',
            26: 'XU_PANTILT_ABSOLUTE_CONTROL — absolute pan/tilt (new!)',
            11: 'XU_DEVICE_STATUS_CONTROL — device state',
            3:  'XU_DEVICE_INFO_CONTROL — device info',
        }
        found_interesting = []
        for sel, label in interesting.items():
            info = results.get(sel)
            if info and info.supported:
                found_interesting.append(f'  ✓ [{sel:2d}] {label} (len={info.length})')
        if found_interesting:
            print(f'\n── Key controls found ──')
            for line in found_interesting:
                print(line)

        print(f'\nNext steps:')
        print(f'  1. Run tools/xu_capture.py to capture USB traffic while')
        print(f'     toggling features in the desktop app')
        print(f'  2. Correlate captured bytes with these selectors to determine')
        print(f'     the data format for each command')
        print(f'  3. Update link_usb.py high-level methods with the discovered formats')


if __name__ == '__main__':
    main()
