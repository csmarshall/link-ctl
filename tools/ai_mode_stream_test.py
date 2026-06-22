#!/usr/bin/env python3
"""One-shot Link 2 AI-mode test that holds a video stream open during SET.

Verifies the streaming hypothesis: the Link 2 AI engine only engages (and only
reports a real mode byte at XU9/0x02 instead of 0xFF) while the camera is
streaming. Sets the requested mode with a stream held, prints byte[0] readback
during the poll, then restores normal.

Usage:
  python3 tools/ai_mode_stream_test.py overhead
  python3 tools/ai_mode_stream_test.py deskview --keep    # don't restore normal

Safe: no libusb reset, no detach. Streams via ffmpeg/v4l2-ctl on the Insta360
capture node. Close apps holding /dev/video* first (Discord/OBS) for a clean run.
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import link_ctl as lc  # noqa: E402
import link_usb_linux as ul  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('mode', choices=list(lc.AI_MODE_BYTES.keys()))
    ap.add_argument('--keep', action='store_true', help='leave mode set (skip restore)')
    ap.add_argument('--seconds', type=float, default=8.0, help='poll window')
    args = ap.parse_args()

    lc.reset_usb_caches()
    if not lc._link2():
        print('Not a Link 2 (or no USB backend); nothing stream-specific to test.')
    mode_id, flag = lc.AI_MODE_BYTES[args.mode]
    print(f'target {args.mode}: byte[0]=0x{mode_id:02x} byte[1]=0x{flag:02x}')

    try:
        before = lc._ai_mode_get_raw()
        print(f'before SET (no stream): byte[0]=0x{before[0]:02x}/0x{before[1]:02x}')
    except Exception as exc:
        print(f'before read failed: {exc}')

    print(f'--- holding stream {args.seconds + 3:.0f}s, SET + poll ---')
    with ul.video_stream(seconds=args.seconds + 3.0) as streaming:
        print(f'stream started: {streaming}')
        if args.mode != 'normal':
            lc._apply_ai_mode_buffer(0, 0)
            time.sleep(0.4)
        lc._apply_ai_mode_buffer(mode_id, flag)
        deadline = time.monotonic() + args.seconds
        settled = False
        while time.monotonic() < deadline:
            time.sleep(0.5)
            b0 = lc._ai_mode_byte0()
            print(f'  poll byte[0]=0x{b0:02x}' if b0 is not None else '  poll read failed')
            if lc._ai_mode_wire_ok(args.mode, b0):
                print(f'SETTLED: byte[0]=0x{b0:02x} matches {args.mode}')
                settled = True
                break
        if not settled:
            print(f'NOT settled within {args.seconds:.0f}s (still '
                  f'0x{lc._ai_mode_byte0():02x})')

    if not args.keep and args.mode != 'normal':
        print('--- restoring normal ---')
        try:
            lc.write_ai_mode('normal')
            print('restored normal')
        except Exception as exc:
            print(f'restore normal failed: {exc}')
    return 0 if settled else 3


if __name__ == '__main__':
    raise SystemExit(main())
