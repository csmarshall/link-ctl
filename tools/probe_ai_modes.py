#!/usr/bin/env python3
"""Probe Link 2 AI mode readback after SET for each mode (requires camera plugged in).

Uses ioctl-only link-ctl commands (no libusb detach). Runs recover() between modes
so rapid XU writes do not hang the camera.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CLI = ROOT / 'link_ctl.py'
PY = '/usr/bin/python3' if Path('/usr/bin/python3').is_file() else 'python3'

MODES = ['normal', 'track', 'deskview', 'whiteboard', 'overhead']
SETTLE = float(os.environ.get('LINK_CTL_PROBE_SETTLE', '3.0'))
BETWEEN = float(os.environ.get('LINK_CTL_PROBE_BETWEEN', '1.5'))


def run(*args: str) -> str:
    out = subprocess.check_output(
        [PY, str(CLI), *args],
        stderr=subprocess.STDOUT,
        text=True,
        cwd=ROOT,
    )
    return out.strip()


def recover() -> None:
    try:
        run('recover')
    except subprocess.CalledProcessError:
        pass
    time.sleep(BETWEEN)


def raw_bytes() -> tuple[int, int, int]:
    code = '''
import link_ctl
link_ctl.reset_usb_caches()
raw = link_ctl._uvc_get(9, link_ctl.AI_MODE_SEL, link_ctl._ai_mode_len())
print(len(raw), raw[0], raw[1] if len(raw) > 1 else 0)
'''
    out = subprocess.check_output([PY, '-c', code], text=True, cwd=ROOT).strip()
    ln, b0, b1 = out.split()
    return int(ln), int(b0), int(b1)


def main() -> int:
    try:
        ln, b0, b1 = raw_bytes()
        print(f'buffer length: {ln}  initial raw: 0x{b0:02x}/0x{b1:02x}')
    except Exception as exc:
        print(f'Camera not available: {exc}', file=sys.stderr)
        return 1

    print(f'{"mode":12} {"read_after":14} {"raw b0/b1"}')
    for mode in MODES:
        recover()
        if mode == 'normal':
            run('normal')
        else:
            run(mode, 'on')
        time.sleep(SETTLE)
        after = run('status', 'mode')
        try:
            _, b0, b1 = raw_bytes()
            raw = f'0x{b0:02x}/0x{b1:02x}'
        except Exception:
            raw = '?'
        print(f'{mode:12} {after:14} {raw}')
        time.sleep(BETWEEN)

    recover()
    run('normal')
    print('\nRestored normal mode.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
