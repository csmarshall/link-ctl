#!/usr/bin/env python3
"""Link 2 AI-mode probe that holds a video stream open during SET.

Confirms two things on hardware:
  1. The Link 2 AI engine only reports a real mode byte (XU9/0x02 byte[0])
     while the camera is streaming; idle reads return 0xFF.
  2. Which SET *sequence* and *flag bytes* make a target mode (overhead 0x05,
     deskview 0x06) actually land on byte[0] and STAY there.

It logs byte[0] AND byte[1] on every poll so you can watch the firmware
transition (0xFF busy -> target / 0x00). Supports:
  --no-off      write TARGET only (skip the OFF=0x00 write first)
  --flag 0xNN   override byte[1] sent with the target
  --reassert N  re-send the target every N seconds during the poll
  --seconds N   poll window length (default 12)

Usage:
  python3 tools/ai_mode_stream_test.py overhead
  python3 tools/ai_mode_stream_test.py overhead --no-off --flag 0x10
  python3 tools/ai_mode_stream_test.py deskview --no-off --seconds 12

Safe: no libusb reset, no detach, no sysfs rebind. Streams via ffmpeg/v4l2-ctl
on the Insta360 capture node. Close apps holding /dev/video* (Discord/OBS) for
a clean run.
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


def _read_both() -> tuple[int | None, int | None]:
    try:
        raw = lc._ai_mode_get_raw()
    except OSError:
        return None, None
    if not raw:
        return None, None
    return raw[0], (raw[1] if len(raw) > 1 else None)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('mode', choices=list(lc.AI_MODE_BYTES.keys()))
    ap.add_argument('--no-off', action='store_true',
                    help='write target only; skip the OFF=0x00 write first')
    ap.add_argument('--flag', type=lambda s: int(s, 0), default=None,
                    help='override byte[1] sent with the target (e.g. 0x10)')
    ap.add_argument('--reassert', type=float, default=0.0,
                    help='re-send target every N seconds during poll (0=off)')
    ap.add_argument('--wait-ready', action='store_true',
                    help='wait for AI engine ready (byte[0]!=0xFF) before SET')
    ap.add_argument('--observe', action='store_true',
                    help='never break early; watch the full window')
    ap.add_argument('--stop-assert', type=float, default=None,
                    help='stop re-asserting after this many seconds (to test hold)')
    ap.add_argument('--keep', action='store_true', help='leave mode set (skip restore)')
    ap.add_argument('--seconds', type=float, default=12.0, help='poll window')
    args = ap.parse_args()

    lc.reset_usb_caches()
    if not lc._link2():
        print('Not a Link 2 (or no USB backend); nothing stream-specific to test.')
    mode_id, default_flag = lc.AI_MODE_BYTES[args.mode]
    flag = args.flag if args.flag is not None else default_flag
    seq = 'target-only' if args.no_off else 'off-then-target'
    print(f'target {args.mode}: byte[0]=0x{mode_id:02x} byte[1]=0x{flag:02x}  '
          f'seq={seq} reassert={args.reassert}s poll={args.seconds:.0f}s')

    b0, b1 = _read_both()
    if b0 is not None:
        print(f'before SET (no stream): byte[0]=0x{b0:02x} byte[1]='
              f'{"0x%02x" % b1 if b1 is not None else "--"}')

    settled = False
    last0 = None
    print(f'--- holding stream {args.seconds + 3:.0f}s, SET + poll ---')
    with ul.video_stream(seconds=args.seconds + 3.0) as streaming:
        print(f'stream started: {streaming}')
        if args.wait_ready:
            rdy = time.monotonic() + 6.0
            while time.monotonic() < rdy:
                rb0, _ = _read_both()
                if rb0 is not None and rb0 != 0xFF:
                    print(f'  engine ready: byte[0]=0x{rb0:02x}')
                    break
                time.sleep(0.3)
        if args.mode != 'normal' and not args.no_off:
            lc._apply_ai_mode_buffer(0, 0)
            time.sleep(0.4)
        lc._apply_ai_mode_buffer(mode_id, flag)

        deadline = time.monotonic() + args.seconds
        reassert_at = (time.monotonic() + args.reassert) if args.reassert else None
        stable_hits = 0
        while time.monotonic() < deadline:
            time.sleep(0.5)
            b0, b1 = _read_both()
            last0 = b0
            tag = ''
            if b0 == 0xFF:
                tag = ' (busy)'
            elif b0 == mode_id:
                tag = ' <-- TARGET'
            elif b0 == 0x00:
                tag = ' (normal)'
            print(f'  t={args.seconds - (deadline - time.monotonic()):4.1f}s  '
                  f'byte[0]=0x{b0:02x} byte[1]='
                  f'{"0x%02x" % b1 if b1 is not None else "--"}{tag}'
                  if b0 is not None else '  poll read failed')
            elapsed = args.seconds - (deadline - time.monotonic())
            if b0 == mode_id and args.mode != 'normal':
                stable_hits += 1
                if stable_hits >= 3:  # require it to hold ~1.5s
                    print(f'SETTLED+HELD: byte[0]=0x{b0:02x} matches {args.mode}')
                    settled = True
                    if not args.observe:
                        break
            else:
                stable_hits = 0
            assert_off = (args.stop_assert is not None and elapsed >= args.stop_assert)
            if reassert_at is not None and not assert_off and time.monotonic() >= reassert_at:
                print('  (re-asserting target)')
                lc._apply_ai_mode_buffer(mode_id, flag)
                reassert_at = time.monotonic() + args.reassert
        if not settled:
            print(f'NOT settled within {args.seconds:.0f}s '
                  f'(last byte[0]=0x{last0:02x})'
                  if last0 is not None else 'NOT settled (read failed)')

        # Hypothesis 5: re-read with the SAME stream still held, after a pause,
        # to see whether the target persists or reverts.
        time.sleep(1.0)
        b0, b1 = _read_both()
        print(f'in-stream readback after settle: byte[0]='
              f'{"0x%02x" % b0 if b0 is not None else "--"} byte[1]='
              f'{"0x%02x" % b1 if b1 is not None else "--"}')

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
