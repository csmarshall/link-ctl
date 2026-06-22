#!/usr/bin/env python3
"""ioctl-based UVC selector snapshot for Linux (works while uvcvideo is streaming).

Usage:
  python3 tools/xu_snapshot_linux.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from link_usb import LinuxXUBackend

UNITS = [1, 2, 3, 4, 5, 9, 10, 11, 12, 13, 14, 15]
TRY_LENS = [1, 2, 4, 8, 16, 32, 52, 61, 64, 128, 240]


def main() -> int:
    b = LinuxXUBackend()
    b.open()
    found = 0
    try:
        for unit in UNITS:
            for sel in range(0x01, 0x41):
                length = 0
                try:
                    length = b.xu_get_len(unit, sel)
                except OSError:
                    pass
                if not length:
                    for ln in TRY_LENS:
                        try:
                            data = b.xu_get(unit, sel, ln)
                            length = ln
                            break
                        except OSError:
                            continue
                if not length:
                    continue
                try:
                    data = b.xu_get(unit, sel, length)
                except OSError:
                    continue
                found += 1
                print(f'unit={unit:2d} sel=0x{sel:02x} len={length} hex={data.hex()}')
    finally:
        b.close()
    print(f'# found {found} selectors', file=sys.stderr)
    return 0 if found else 1


if __name__ == '__main__':
    sys.exit(main())
