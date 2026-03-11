#!/usr/bin/env python3
"""apitest.py — Exercise every known link-ctl WebSocket command with labeled timestamps.

Connects directly to the Link Controller WebSocket server and fires every command
in sequence, printing a timestamped action log to stdout. Run tshark alongside to
capture the raw packets, then correlate timestamps to identify any gaps or wrong
paramTypes.

Usage:
    # Auto-discover port via lsof (camera must be connected, Link Controller running)
    python3 apitest.py

    # Use port+token from a QR code image
    python3 apitest.py qr_code.png

    # Use the QR URL directly (paste from browser or decode yourself)
    python3 apitest.py "http://link-controller.insta360.com/v3/link/?ip=...&port=PORT&token=TOKEN"

    # Override port explicitly
    python3 apitest.py --port 49924

Suggested tshark command (run first, Ctrl-C when done):
    sudo tshark -i lo0 -Y "websocket" -T fields \\
        -e frame.time_relative -e data.data \\
        -E header=y > ~/apitest-capture.txt

Requirements:
    pip install websockets
    # For QR decode: brew install zbar  OR  pip install pyzbar pillow
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
import os
import re
import subprocess
from urllib.parse import urlparse, parse_qs
from pathlib import Path

# ── Import protocol builders from link_ctl ───────────────────────────────────
sys.path.insert(0, str(Path(__file__).parent))
from link_ctl import (
    LinkClient,
    ParamTypeV2, AIMode, VideoMode,
    build_value_change, build_zoom,
    build_joystick, build_joystick_stop,
    build_preset_recall,
    _lsof_port, _read_token_from_ini,
    CONTROLLER_NAME,
)


# ── Logging ───────────────────────────────────────────────────────────────────

def log(action: str):
    ts = time.strftime('%H:%M:%S')
    ms = int((time.time() % 1) * 1000)
    print(f"[{ts}.{ms:03d}] ACTION: {action}", flush=True)


async def pause(seconds: float = 1.5):
    await asyncio.sleep(seconds)


# ── QR / URL parsing ─────────────────────────────────────────────────────────

def decode_qr(image_path: str) -> str:
    try:
        r = subprocess.run(['zbarimg', '--raw', '-q', image_path],
                           capture_output=True, text=True)
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip()
    except FileNotFoundError:
        pass
    try:
        from PIL import Image
        from pyzbar.pyzbar import decode
        decoded = decode(Image.open(image_path))
        if decoded:
            return decoded[0].data.decode('utf-8')
    except ImportError:
        pass
    raise RuntimeError(
        "Cannot decode QR code.\n"
        "  Install zbarimg:  brew install zbar\n"
        "  Or pyzbar:        pip install pyzbar pillow\n"
        "  Or pass the URL directly as the argument."
    )


def parse_url(url: str) -> tuple[int | None, str | None]:
    """Extract (port, token) from a Link Controller QR code URL."""
    parsed = urlparse(url)
    qs = parse_qs(parsed.query)
    port_str = qs.get('port', [None])[0]
    token = qs.get('token', [None])[0]
    port = int(port_str) if port_str and port_str.isdigit() else None
    return port, token


# ── Test sequence ─────────────────────────────────────────────────────────────

async def run_tests(port: int, token: str | None, debug: bool = False):
    client = LinkClient(port, debug=debug, token=token or "")

    print(f"\n[apitest] Connecting to ws://localhost:{port}/", flush=True)
    try:
        await client.connect()
    except Exception as e:
        print(f"✗ Connection failed: {e}", file=sys.stderr)
        sys.exit(3)

    ok, err = await client.handshake()
    if not ok:
        print(f"✗ Handshake failed: {err}", file=sys.stderr)
        sys.exit(3)

    serial = client.serial
    print(f"[apitest] Connected. Serial: {serial}", flush=True)
    print("[apitest] Starting test sequence...\n", flush=True)

    # Helper: send a payload with label
    async def send(label: str, payload: bytes):
        log(label)
        await client._send(payload)
        await pause(1.5)

    # Helper: joystick velocity pulse
    async def joystick_pulse(label: str, pan_vel: float, tilt_vel: float, duration: float):
        log(label)
        await client._send(build_joystick(serial, pan_vel, tilt_vel))
        await asyncio.sleep(duration)
        await client._send(build_joystick_stop(serial))
        await pause(1.5)

    # ── Zoom ─────────────────────────────────────────────────────────────────
    await send("zoom 200 (in)", build_zoom(serial, 200))
    await send("zoom 300 (in more)", build_zoom(serial, 300))
    await send("zoom 100 (out/reset)", build_zoom(serial, 100))

    # ── Pan/tilt via joystick ─────────────────────────────────────────────────
    await joystick_pulse("pan right (vel=+1.0, 0.5s)", 1.0, 0.0, 0.5)
    await joystick_pulse("pan left  (vel=-1.0, 0.5s)", -1.0, 0.0, 0.5)
    await joystick_pulse("tilt up   (vel=+1.0, 0.5s)", 0.0, 1.0, 0.5)
    await joystick_pulse("tilt down (vel=-1.0, 0.5s)", 0.0, -1.0, 0.5)
    await joystick_pulse("pan+tilt diagonal (vel=+0.7,+0.7, 0.5s)", 0.7, 0.7, 0.5)

    # ── Center / reset ────────────────────────────────────────────────────────
    await send("center/reset (paramType=3)", build_value_change(serial, ParamTypeV2.NORMAL_RESET))

    # ── AI modes (all via paramType=5) ────────────────────────────────────────
    await send("track on  (paramType=5, value='1')",
               build_value_change(serial, ParamTypeV2.AI_MODE, AIMode.TRACKING))
    await pause(1.0)
    await send("track off (paramType=5, value='0')",
               build_value_change(serial, ParamTypeV2.AI_MODE, AIMode.NORMAL))

    await send("overhead on  (paramType=5, value='4')",
               build_value_change(serial, ParamTypeV2.AI_MODE, AIMode.OVERHEAD))
    await pause(1.0)
    await send("overhead off (paramType=5, value='0')",
               build_value_change(serial, ParamTypeV2.AI_MODE, AIMode.NORMAL))

    await send("deskview on  (paramType=5, value='5')",
               build_value_change(serial, ParamTypeV2.AI_MODE, AIMode.DESKVIEW))
    await pause(1.0)
    await send("deskview off (paramType=5, value='0')",
               build_value_change(serial, ParamTypeV2.AI_MODE, AIMode.NORMAL))

    await send("whiteboard on  (paramType=5, value='6')",
               build_value_change(serial, ParamTypeV2.AI_MODE, AIMode.WHITEBOARD))
    await pause(1.0)
    await send("whiteboard off (paramType=5, value='0')",
               build_value_change(serial, ParamTypeV2.AI_MODE, AIMode.NORMAL))

    # ── Smart Composition (paramType=11, still verifying feature) ────────────
    await send("smart_composition on  (paramType=11, value='1')",
               build_value_change(serial, ParamTypeV2.SMART_COMPOSITION, "1"))
    await send("smart_composition off (paramType=11, value='0')",
               build_value_change(serial, ParamTypeV2.SMART_COMPOSITION, "0"))

    # ── Preset recall ─────────────────────────────────────────────────────────
    await send("preset recall 0", build_preset_recall(serial, 0))
    await send("preset recall 1", build_preset_recall(serial, 1))

    # ── Privacy (velocity tilt-down — stop always fires via try/finally) ───────
    log("privacy on  (tilt down at -1.0 for 3.5s)")
    try:
        await client._send(build_joystick(serial, 0.0, -1.0))
        await asyncio.sleep(3.5)
    finally:
        await client._send(build_joystick_stop(serial))
    await pause(1.0)

    await send("privacy off (center/reset, paramType=3)",
               build_value_change(serial, ParamTypeV2.NORMAL_RESET))

    # ── Probe unknown paramTypes ──────────────────────────────────────────────
    # These are guesses based on proto field numbers. Watch capture for responses.
    print("\n[apitest] Probing unknown paramTypes (watch capture for camera response)...\n",
          flush=True)

    UNKNOWN_PARAMS = [
        (8,  "unknown-8  (guess: something else)"),
        (9,  "unknown-9  (guess: auto-focus?)"),
        (10, "unknown-10 (guess: auto-exposure?)"),
        (12, "unknown-12"),
        (13, "unknown-13"),
        (14, "unknown-14"),
        (15, "unknown-15"),
        (16, "unknown-16"),
        (17, "unknown-17"),
        (18, "unknown-18"),
        (19, "unknown-19 (guess: HDR?)"),
        (20, "unknown-20"),
        (21, "unknown-21"),
        (22, "unknown-22"),
        (23, "unknown-23"),
        (24, "unknown-24"),
        (25, "unknown-25"),
        (26, "unknown-26 (guess: auto-white-balance?)"),
        (27, "unknown-27"),
        (28, "unknown-28"),
        (29, "unknown-29"),
        (30, "unknown-30"),
    ]

    for pt, label in UNKNOWN_PARAMS:
        # Try ON ("1"), wait for server response, then OFF ("0")
        log(f"PROBE on  {label}")
        await client._send(build_value_change(serial, pt, "1"))
        await pause(1.2)
        log(f"PROBE off {label}")
        await client._send(build_value_change(serial, pt, "0"))
        await pause(1.2)

    # ── Done ──────────────────────────────────────────────────────────────────
    print(f"\n[apitest] Done.", flush=True)
    await client.close()


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    p = argparse.ArgumentParser(
        description='Exercise every link-ctl command for tshark capture correlation.',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split('\n\n')[0],
    )
    p.add_argument('url_or_qr', nargs='?',
                   help='QR code image path, QR URL, or omit to auto-discover')
    p.add_argument('--port', type=int, help='Override WebSocket port')
    p.add_argument('--debug', action='store_true', help='Hex-dump every WS frame')
    p.add_argument('--no-wait', action='store_true',
                   help='Skip the "press Enter" prompt (for non-interactive use)')
    args = p.parse_args()

    port = args.port
    token = None

    if args.url_or_qr:
        s = args.url_or_qr
        if s.startswith('http') or s.startswith('ws'):
            url = s
        else:
            print(f"[apitest] Decoding QR: {s}", flush=True)
            url = decode_qr(s)
            print(f"[apitest] URL: {url}", flush=True)
        p_from_url, token = parse_url(url)
        if not port:
            port = p_from_url

    if not port:
        # Auto-discover via lsof
        print("[apitest] No port specified — discovering via lsof...", flush=True)
        port = _lsof_port()
        if not port:
            print("✗ Could not find Link Controller port. "
                  "Pass a QR code or --port N.", file=sys.stderr)
            sys.exit(2)
        print(f"[apitest] Found port {port} via lsof", flush=True)

    if not token:
        token = _read_token_from_ini()
        if token:
            print(f"[apitest] Using token from startup.ini", flush=True)

    if not args.no_wait:
        print("[apitest] Ready. Start your tshark capture, then press Enter.")
        input()

    asyncio.run(run_tests(port, token, debug=args.debug))


if __name__ == '__main__':
    main()
