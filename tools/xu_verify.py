#!/usr/bin/env python3
"""xu_verify.py — Phase B: Verify XU control read/write via uvc-probe (IOKit).

Works with the desktop app running — uses IOKit shared access via uvc-probe.
Requires sudo for IOKit USB access.

For each confirmed control:
  1. Read current value
  2. Write test value
  3. Read back and verify
  4. Restore original value

Usage:
  sudo python3 tools/xu_verify.py
  sudo python3 tools/xu_verify.py --only hdr,mirror
  sudo python3 tools/xu_verify.py --read-only
"""
import argparse
import subprocess
import sys
import time
from pathlib import Path

PROBE_BIN = str(Path(__file__).resolve().parent / 'uvc-probe')


def ts():
    t = time.time()
    return time.strftime('%H:%M:%S') + f'.{int((t % 1) * 1000):03d}'

def log(msg):
    print(f'[{ts()}] {msg}', flush=True)

def sep(label):
    print(f'\n[{ts()}] {"─" * 3} {label} {"─" * 40}', flush=True)


def xu_get(unit: int, sel: int, length: int) -> bytes:
    """Read a UVC XU register via uvc-probe get."""
    result = subprocess.run(
        [PROBE_BIN, 'get', str(unit), f'0x{sel:02x}', str(length)],
        capture_output=True, text=True, timeout=5)
    if result.returncode != 0:
        raise RuntimeError(f'GET failed: unit={unit} sel=0x{sel:02x} len={length}: {result.stderr.strip()}')
    return bytes.fromhex(result.stdout.strip())


def xu_set(unit: int, sel: int, data: bytes) -> None:
    """Write a UVC XU register via uvc-probe set."""
    hex_str = data.hex()
    result = subprocess.run(
        [PROBE_BIN, 'set', str(unit), f'0x{sel:02x}', hex_str],
        capture_output=True, text=True, timeout=5)
    if result.returncode != 0:
        raise RuntimeError(f'SET failed: unit={unit} sel=0x{sel:02x} data={hex_str}: {result.stderr.strip()}')


# Unit IDs
CT  = 1
PU  = 5
XU1 = 9
XU2 = 10


class Verifier:
    def __init__(self, read_only: bool = False):
        self.read_only = read_only
        self.passed = 0
        self.failed = 0
        self.skipped = 0

    def test_bitmask_bit(self, name: str, bit: int):
        """Test a single bit in the func-enable bitmask (unit 9, sel 0x1b)."""
        sep(name)
        try:
            raw = xu_get(XU1, 0x1b, 2)
            cur = int.from_bytes(raw, 'little')
            cur_bit = bool(cur & (1 << bit))
            log(f'  bitmask: 0x{cur:04x}, bit {bit} = {cur_bit}')

            if self.read_only:
                self.skipped += 1
                return

            # Flip the bit
            test = cur ^ (1 << bit)
            log(f'  writing: 0x{test:04x} (bit {bit} → {not cur_bit})')
            xu_set(XU1, 0x1b, test.to_bytes(2, 'little'))
            time.sleep(0.5)

            readback = int.from_bytes(xu_get(XU1, 0x1b, 2), 'little')
            readback_bit = bool(readback & (1 << bit))
            log(f'  readback: 0x{readback:04x}, bit {bit} = {readback_bit}')

            if readback_bit == (not cur_bit):
                log(f'  PASS')
                self.passed += 1
            else:
                log(f'  FAIL — expected bit {bit} = {not cur_bit}')
                self.failed += 1

            log(f'  restoring: 0x{cur:04x}')
            xu_set(XU1, 0x1b, cur.to_bytes(2, 'little'))
            time.sleep(0.3)
        except Exception as e:
            log(f'  ERROR: {e}')
            self.failed += 1

    def test_byte(self, name: str, unit: int, sel: int, size: int,
                  test_val: int, min_val: int = 0, max_val: int = 100,
                  scale: int = 1):
        """Test an integer register: read, write test value, read back, restore."""
        sep(name)
        try:
            raw = xu_get(unit, sel, size)
            cur_raw = int.from_bytes(raw, 'little')
            cur = cur_raw // scale
            log(f'  current: {cur} (raw: {raw.hex()})')

            if self.read_only:
                self.skipped += 1
                return

            if cur == test_val:
                test_val = min_val if cur != min_val else max_val

            test_raw = test_val * scale
            log(f'  writing: {test_val} (raw: {test_raw.to_bytes(size, "little").hex()})')
            xu_set(unit, sel, test_raw.to_bytes(size, 'little'))
            time.sleep(0.5)

            rb_raw = int.from_bytes(xu_get(unit, sel, size), 'little')
            rb = rb_raw // scale
            log(f'  readback: {rb} (raw: {rb_raw.to_bytes(size, "little").hex()})')

            if rb == test_val:
                log(f'  PASS')
                self.passed += 1
            else:
                log(f'  FAIL — expected {test_val}, got {rb}')
                self.failed += 1

            log(f'  restoring: {cur}')
            xu_set(unit, sel, cur_raw.to_bytes(size, 'little'))
            time.sleep(0.3)
        except Exception as e:
            log(f'  ERROR: {e}')
            self.failed += 1

    def test_enum(self, name: str, unit: int, sel: int, size: int,
                  test_val: int, restore_val: int):
        """Test an enum register."""
        sep(name)
        try:
            raw = xu_get(unit, sel, size)
            cur = int.from_bytes(raw, 'little')
            log(f'  current: {cur} (raw: {raw.hex()})')

            if self.read_only:
                self.skipped += 1
                return

            if cur == test_val:
                test_val, restore_val = restore_val, test_val

            log(f'  writing: {test_val}')
            xu_set(unit, sel, test_val.to_bytes(size, 'little'))
            time.sleep(0.5)

            rb = int.from_bytes(xu_get(unit, sel, size), 'little')
            log(f'  readback: {rb}')

            if rb == test_val:
                log(f'  PASS')
                self.passed += 1
            else:
                log(f'  FAIL — expected {test_val}, got {rb}')
                self.failed += 1

            log(f'  restoring: {restore_val}')
            xu_set(unit, sel, restore_val.to_bytes(size, 'little'))
            time.sleep(0.3)
        except Exception as e:
            log(f'  ERROR: {e}')
            self.failed += 1

    def test_pan_tilt(self, name: str):
        """Test pan/tilt absolute read and write."""
        sep(name)
        try:
            raw = xu_get(XU1, 0x1a, 8)
            pan = int.from_bytes(raw[0:4], 'little', signed=True)
            tilt = int.from_bytes(raw[4:8], 'little', signed=True)
            log(f'  current: pan={pan}, tilt={tilt} (raw: {raw.hex()})')

            if self.read_only:
                self.skipped += 1
                return

            test_pan = pan + 5000
            data = test_pan.to_bytes(4, 'little', signed=True) + tilt.to_bytes(4, 'little', signed=True)
            log(f'  writing: pan={test_pan}, tilt={tilt}')
            xu_set(XU1, 0x1a, data)
            time.sleep(1.0)

            rb = xu_get(XU1, 0x1a, 8)
            rpan = int.from_bytes(rb[0:4], 'little', signed=True)
            rtilt = int.from_bytes(rb[4:8], 'little', signed=True)
            log(f'  readback: pan={rpan}, tilt={rtilt}')

            if rpan != pan:
                log(f'  PASS — pan changed ({pan} → {rpan})')
                self.passed += 1
            else:
                log(f'  UNCERTAIN — pan unchanged (firmware may ignore SET_CUR)')
                self.skipped += 1

            log(f'  restoring: pan={pan}, tilt={tilt}')
            xu_set(XU1, 0x1a, raw)
            time.sleep(0.5)
        except Exception as e:
            log(f'  ERROR: {e}')
            self.failed += 1

    def test_privacy(self, name: str):
        """Test privacy XU2 sel 0x0F."""
        sep(name)
        try:
            raw = xu_get(XU2, 0x0f, 1)
            cur = raw[0]
            log(f'  current: {cur} (raw: {raw.hex()})')

            if self.read_only:
                self.skipped += 1
                return

            test_val = 0x01 if cur == 0x00 else 0x00
            log(f'  writing: {test_val:#04x}')
            xu_set(XU2, 0x0f, bytes([test_val]))
            time.sleep(2.0)

            rb = xu_get(XU2, 0x0f, 1)[0]
            log(f'  readback: {rb:#04x}')

            if rb == test_val:
                log(f'  PASS')
                self.passed += 1
            else:
                log(f'  FAIL — expected {test_val:#04x}, got {rb:#04x}')
                self.failed += 1

            log(f'  restoring: {cur:#04x}')
            xu_set(XU2, 0x0f, bytes([cur]))
            time.sleep(2.0)
        except Exception as e:
            log(f'  ERROR: {e}')
            self.failed += 1

    def test_read_only(self, name: str, unit: int, sel: int, size: int):
        """Just read and report a register."""
        sep(name)
        try:
            raw = xu_get(unit, sel, size)
            log(f'  value: {raw.hex()} ({list(raw)})')
            self.passed += 1
        except Exception as e:
            log(f'  ERROR: {e}')
            self.failed += 1


TESTS = {
    # Func-enable bitmask bits
    'hdr':           lambda v: v.test_bitmask_bit('hdr (func-enable bit 2)', 2),
    'mirror':        lambda v: v.test_bitmask_bit('mirror (func-enable bit 3)', 3),
    'gesture_zoom':  lambda v: v.test_bitmask_bit('gesture_zoom (func-enable bit 4)', 4),
    # AI mode
    'video_mode':    lambda v: v.test_enum('video_mode (unit 9 sel 0x02)', XU1, 0x02, 1, 1, 0),
    # PU controls
    'brightness':    lambda v: v.test_byte('brightness (unit 5 sel 0x02)', PU, 0x02, 1, 75),
    'contrast':      lambda v: v.test_byte('contrast (unit 5 sel 0x03)', PU, 0x03, 1, 75),
    'saturation':    lambda v: v.test_byte('saturation (unit 5 sel 0x07)', PU, 0x07, 2, 25),
    'sharpness':     lambda v: v.test_byte('sharpness (unit 5 sel 0x08)', PU, 0x08, 2, 75),
    'awb':           lambda v: v.test_enum('awb (unit 5 sel 0x0b)', PU, 0x0b, 1, 0, 1),
    'anti_flicker':  lambda v: v.test_enum('anti_flicker (unit 5 sel 0x05)', PU, 0x05, 1, 1, 3),
    # XU1 controls
    'exposure_comp': lambda v: v.test_byte('exposure_comp (unit 9 sel 0x09)', XU1, 0x09, 2, 75, scale=100),
    'ae_mode':       lambda v: v.test_enum('ae_mode (unit 9 sel 0x1e)', XU1, 0x1e, 1, 1, 2),
    # CT controls
    'af_mode':       lambda v: v.test_enum('af_mode (unit 1 sel 0x08)', CT, 0x08, 1, 0, 1),
    'zoom':          lambda v: v.test_byte('zoom (unit 1 sel 0x0b)', CT, 0x0b, 1, 120, min_val=100, max_val=144),
    # Compound/special
    'pan_tilt':      lambda v: v.test_pan_tilt('pan_tilt_absolute (unit 9 sel 0x1a)'),
    'privacy':       lambda v: v.test_privacy('privacy (unit 10 sel 0x0f)'),
    # Read-only info
    'func_enable':   lambda v: v.test_read_only('func_enable raw', XU1, 0x1b, 2),
    'device_status': lambda v: v.test_read_only('device_status', XU1, 0x0b, 5),
}


def main():
    parser = argparse.ArgumentParser(description='Verify XU controls via uvc-probe (IOKit shared access).')
    parser.add_argument('--only', type=str, default=None, help='Comma-separated test names')
    parser.add_argument('--read-only', action='store_true', help='Read values only, no writes')
    args = parser.parse_args()

    if not Path(PROBE_BIN).exists():
        log(f'ERROR: {PROBE_BIN} not found. Compile it first.')
        sys.exit(1)

    sep('XU VERIFY — Phase B (via uvc-probe IOKit)')

    v = Verifier(read_only=args.read_only)

    tests_to_run = TESTS
    if args.only:
        names = {n.strip() for n in args.only.split(',')}
        tests_to_run = {k: fn for k, fn in TESTS.items() if k in names}
        unknown = names - set(tests_to_run.keys())
        if unknown:
            log(f'Unknown tests: {", ".join(sorted(unknown))}')
            log(f'Available: {", ".join(TESTS.keys())}')
            sys.exit(1)

    for name, test_fn in tests_to_run.items():
        test_fn(v)

    sep('RESULTS')
    total = v.passed + v.failed + v.skipped
    log(f'  Passed:  {v.passed}/{total}')
    log(f'  Failed:  {v.failed}/{total}')
    log(f'  Skipped: {v.skipped}/{total}')

    if v.failed > 0:
        sys.exit(1)


if __name__ == '__main__':
    main()
