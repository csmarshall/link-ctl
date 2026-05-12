#!/usr/bin/env python3
"""probe_unmapped_xu.py — Discover which proto-declared XU selectors the
OG Insta360 Link firmware actually supports.

Pulls a list of candidate (unit, selector) pairs from
insta360linkcontroller.proto that link-ctl does NOT currently implement
and probes each one over UVC via the existing link_usb_macos backend.
For every selector it issues, in order:

  GET_INFO  — capability bits (supports GET? SET? auto-update? etc.)
  GET_LEN   — the firmware's declared payload size
  GET_MIN   — minimum value
  GET_MAX   — maximum value
  GET_DEF   — power-on default
  GET_RES   — step resolution
  GET_CUR   — current value

With --write it then performs a SET round-trip on each selector the
firmware reports as writable, picking a probe value inside [MIN, MAX]
that's different from current, writes it, re-reads, then restores the
original. The verdict per selector is one of:

  UNSUPPORTED       GET_INFO failed → firmware does not implement
  READ_ONLY         GET works, SET capability bit clear
  SUPPORTED         GET+SET both work, round-trip persisted
  ACCEPTS_NOOP      SET succeeded but readback was unchanged (Link-2 stub)
  SET_REJECTED      firmware refused the SET (probably out of range)

Read-only by default. No WebSocket, no Link Controller, no token, no sudo.
Run with the camera connected and Insta360 Link Controller closed.

    python3 tools/probe_unmapped_xu.py            # safe read-only probe
    python3 tools/probe_unmapped_xu.py --write    # round-trip writes
    python3 tools/probe_unmapped_xu.py --json     # machine-readable
    python3 tools/probe_unmapped_xu.py --only track-speed,iso

Output is a per-selector report plus a final summary table. JSON output
goes to stdout; the human-readable form is printed alongside on stderr
so you can pipe to `tee` for a log and `jq` simultaneously.
"""
from __future__ import annotations
import argparse
import json
import os
import struct
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

# Import the USB-direct backend the rest of link-ctl uses.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import link_usb_macos as usb  # noqa: E402

# ── UVC class-specific request codes (UVC 1.5 §4.2) ──────────────────────────
SET_CUR   = 0x01
GET_CUR   = 0x81
GET_MIN   = 0x82
GET_MAX   = 0x83
GET_RES   = 0x84
GET_LEN   = 0x85
GET_INFO  = 0x86
GET_DEF   = 0x87

# bmRequestType for class-specific interface IN / OUT.
BMRT_IN   = 0xA1
BMRT_OUT  = 0x21
IFACE_NUM = 0   # video control interface number — same as link_usb_macos

# ── Candidate selectors ──────────────────────────────────────────────────────
# Source: insta360linkcontroller.proto, ControlSelector enum at line 43.
# Skips selectors link-ctl already knows about (FUNC_ENABLE, AE_MODE,
# AI mode, framing, pan/tilt readback). All on Extension Unit 9 unless
# noted; the proto's enum value IS the XU selector number.
@dataclass
class Candidate:
    name: str
    unit: int
    sel:  int
    proto_param: str   # ParamType name (or '—' if XU-only)
    proto_xu:    str   # XU enum name
    note: str = ''

CANDIDATES: list[Candidate] = [
    Candidate('track-speed',      9, 0x12, 'PARAM_TRACK_SPEED (20)',
              'XU_TRACK_SPEED_CONTROL (18)',
              'tracking speed / smoothing'),
    Candidate('layout-style',     9, 0x13, 'PARAM_COMPOSITION_STYLE (21)',
              'XU_LAYOUT_STYLE_CONTROL (19)',
              'SAME SEL as current smartcomp-frame; probe for richer enum'),
    Candidate('head-list',        9, 0x14, '—',
              'XU_HEAD_LIST_CONTROL (20)',
              'detected faces; expected variable-length read-only buffer'),
    Candidate('track-target',     9, 0x15, '—',
              'XU_TRACK_TARGET_CONTROL (21)',
              'index of detected head to track'),
    Candidate('pantilt-relative', 9, 0x16, 'PARAM_PAN_TILT_RELATIVE (12)',
              'XU_PANTILT_RELATIVE_CONTROL (22)',
              'relative move (probably SET-only)'),
    Candidate('mobvoi-pubkey',    9, 0x17, '—',
              'XU_MOBVOI_PUBKEY_CONTROL (23)',
              'voice integration key; almost certainly Link 2+'),
    Candidate('bias',             9, 0x18, 'PARAM_BIAS (16)',
              'XU_BIAS_CONTROL (24)',
              'opaque'),
    Candidate('iso',              9, 0x19, 'PARAM_ISO_VALUE (23)',
              'XU_ISO_CONTROL (25)',
              'manual ISO (likely requires autoexposure off)'),
    Candidate('shutter',          9, 0x1D, 'PARAM_SHUTTER_VALUE (24)',
              'XU_EXPOSURE_TIME_ABSOLUTE_CONTROL (29)',
              'manual shutter speed'),
    Candidate('video-res',        9, 0x1C, 'PARAM_RESOLUTION (18)',
              'XU_VIDEO_RES_CONTROL (28)',
              '1080p / 4K selector; OG Link claims fixed res'),
    Candidate('noise-cancel',     9, 0x07, '—',
              'XU_NOISE_CANCEL_CONTROL (7)',
              'mic noise cancel; possibly Link 2+'),
    Candidate('af-mode',          9, 0x0F, '—',
              'XU_AF_MODE_CONTROL (15)',
              'multi-mode AF (continuous/single); OG has on/off only'),
    Candidate('exposure-curve',   9, 0x10, '—',
              'XU_EXPOSURE_CURVE_CONTROL (16)',
              'tone curve LUT; almost certainly Link 2+'),
]

# Selectors we deliberately do NOT probe writes on even with --write:
# anything packed/structured, paired with the device identity, or where a
# wrong write could destabilise the camera. Limit --write to selectors
# whose interpretation is obviously a single scalar.
NO_WRITE_TEST = {
    'head-list',         # GET_INFO says read-only; 240-byte face list
    'pantilt-relative',  # 4-byte packed (pan_vel, tilt_vel) — set-only
    'exposure-curve',    # 255-byte tone-curve LUT
    'mobvoi-pubkey',     # 129-byte paired voice key — DO NOT modify
    'bias',              # 4-byte opaque packed struct
    'track-target',      # 8-byte packed (probably head index + coords)
    'af-mode',           # 12-byte packed AF state
    'video-res',         # 10-byte resolution struct — wrong write may kill stream
}


# ── UVC primitives via control_request ───────────────────────────────────────
def _req(handle, bmrt: int, req: int, unit: int, sel: int,
         data: bytes = b'', read_length: int = 0) -> tuple[bool, bytes]:
    return usb.control_request(handle, bmrt, req,
                               sel << 8, (unit << 8) | IFACE_NUM,
                               data=data, read_length=read_length)

def get_info(handle, unit, sel) -> int | None:
    ok, data = _req(handle, BMRT_IN, GET_INFO, unit, sel, read_length=1)
    return data[0] if ok and data else None

def get_len(handle, unit, sel) -> int | None:
    ok, data = _req(handle, BMRT_IN, GET_LEN, unit, sel, read_length=2)
    if not ok or len(data) < 2: return None
    return struct.unpack('<H', data)[0]

def _get(handle, req, unit, sel, length) -> bytes | None:
    ok, data = _req(handle, BMRT_IN, req, unit, sel, read_length=length)
    return data if ok else None

def _set(handle, unit, sel, payload: bytes) -> bool:
    ok, _ = _req(handle, BMRT_OUT, SET_CUR, unit, sel, data=payload)
    return ok


# ── Value helpers ────────────────────────────────────────────────────────────
def as_int(raw: bytes, signed=False) -> int | None:
    """Decode 1/2/4-byte little-endian integers; return None for other sizes."""
    if raw is None: return None
    if   len(raw) == 1: return struct.unpack('<b' if signed else '<B', raw)[0]
    elif len(raw) == 2: return struct.unpack('<h' if signed else '<H', raw)[0]
    elif len(raw) == 4: return struct.unpack('<i' if signed else '<I', raw)[0]
    return None

def pack_int(value: int, length: int, signed=False) -> bytes:
    fmt = {1: '<b' if signed else '<B',
           2: '<h' if signed else '<H',
           4: '<i' if signed else '<I'}[length]
    return struct.pack(fmt, value)

def decode_info(info: int) -> str:
    flags = []
    if info & 0x01: flags.append('GET')
    if info & 0x02: flags.append('SET')
    if info & 0x04: flags.append('DISABLED_BY_AUTO_MODE')
    if info & 0x08: flags.append('AUTOUPDATE')
    if info & 0x10: flags.append('ASYNC')
    if info & 0x20: flags.append('DISABLED')
    return '|'.join(flags) if flags else '(none)'


# ── Probing logic ────────────────────────────────────────────────────────────
@dataclass
class Probe:
    name: str
    unit: int
    sel: int
    proto_param: str
    proto_xu: str
    note: str

    info: int | None = None
    info_flags: str = ''
    length: int | None = None
    raw_min: bytes | None = None
    raw_max: bytes | None = None
    raw_def: bytes | None = None
    raw_res: bytes | None = None
    raw_cur: bytes | None = None

    # SET round-trip
    probe_value:  int | None = None
    set_ok:       bool | None = None
    raw_after:    bytes | None = None
    persisted:    bool | None = None
    restored:     bool | None = None

    verdict: str = ''
    errors:  list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        d = {k: v for k, v in self.__dict__.items() if not k.startswith('_')}
        for k in ('raw_min', 'raw_max', 'raw_def', 'raw_res', 'raw_cur',
                  'raw_after'):
            d[k] = d[k].hex() if isinstance(d[k], (bytes, bytearray)) else None
        return d


def probe_one(handle, c: Candidate, *, write: bool) -> Probe:
    p = Probe(name=c.name, unit=c.unit, sel=c.sel,
              proto_param=c.proto_param, proto_xu=c.proto_xu, note=c.note)

    p.info = get_info(handle, c.unit, c.sel)
    if p.info is None:
        p.verdict = 'UNSUPPORTED'
        return p
    p.info_flags = decode_info(p.info)

    p.length = get_len(handle, c.unit, c.sel)
    if not p.length:
        # GET_INFO worked but firmware reports zero-length — call it
        # unsupported even though the request didn't error. Some Link 2
        # selectors do this on the OG firmware.
        p.verdict = 'UNSUPPORTED'
        return p

    # All the optional GET_* requests. Failures are non-fatal — record
    # what we can.
    p.raw_min = _get(handle, GET_MIN, c.unit, c.sel, p.length)
    p.raw_max = _get(handle, GET_MAX, c.unit, c.sel, p.length)
    p.raw_def = _get(handle, GET_DEF, c.unit, c.sel, p.length)
    p.raw_res = _get(handle, GET_RES, c.unit, c.sel, p.length)
    p.raw_cur = _get(handle, GET_CUR, c.unit, c.sel, p.length)

    if p.raw_cur is None:
        p.verdict = 'INFO_ONLY'    # GET_INFO says supported, GET_CUR fails
        p.errors.append('GET_CUR failed despite GET_INFO success')
        return p

    if not (p.info & 0x02):
        p.verdict = 'READ_ONLY'
        return p

    if not write or c.name in NO_WRITE_TEST:
        p.verdict = 'WRITABLE (not probed)'
        return p

    # ── Choose a safe probe value ─────────────────────────────────────────
    if p.length not in (1, 2, 4):
        p.verdict = 'WRITABLE (length not int-shaped, skipping)'
        return p

    cur = as_int(p.raw_cur)
    lo  = as_int(p.raw_min) if p.raw_min else None
    hi  = as_int(p.raw_max) if p.raw_max else None
    res = as_int(p.raw_res) if p.raw_res else None
    if cur is None:
        p.verdict = 'WRITABLE (cur unreadable as int)'
        return p

    # Walk one resolution step away from cur, clamped to [min, max].
    step = res if (res and res > 0) else 1
    if hi is not None and cur + step <= hi:
        probe = cur + step
    elif lo is not None and cur - step >= lo:
        probe = cur - step
    elif lo is not None and hi is not None and lo != hi:
        probe = hi if cur != hi else lo
    else:
        p.verdict = 'WRITABLE (no usable probe value)'
        return p

    p.probe_value = probe
    payload = pack_int(probe, p.length)

    p.set_ok = _set(handle, c.unit, c.sel, payload)
    if not p.set_ok:
        p.verdict = 'SET_REJECTED'
        return p

    # Give the firmware a beat then read back.
    time.sleep(0.05)
    p.raw_after = _get(handle, GET_CUR, c.unit, c.sel, p.length)
    p.persisted = (p.raw_after == payload)

    # Restore original value. If restore fails, surface it loudly.
    restore_ok = _set(handle, c.unit, c.sel, p.raw_cur)
    time.sleep(0.05)
    restored_raw = _get(handle, GET_CUR, c.unit, c.sel, p.length)
    p.restored = (restored_raw == p.raw_cur)
    if not restore_ok or not p.restored:
        p.errors.append(f'restore failed: wrote {p.raw_cur.hex()} '
                        f'got {restored_raw.hex() if restored_raw else "?"}')

    if p.persisted:
        p.verdict = 'SUPPORTED'
    else:
        p.verdict = 'ACCEPTS_NOOP'
    return p


# ── Pretty print ─────────────────────────────────────────────────────────────
def fmt_raw(raw: bytes | None) -> str:
    if raw is None: return '—'
    n = as_int(raw)
    s = raw.hex()
    return f'{s} ({n})' if n is not None else s

def print_human(probes: list[Probe], stream=sys.stdout):
    for p in probes:
        print(f'\n── {p.name}  (unit {p.unit}, sel 0x{p.sel:02x})', file=stream)
        print(f'   proto:  {p.proto_param}  /  {p.proto_xu}', file=stream)
        if p.note:
            print(f'   note:   {p.note}', file=stream)
        if p.info is None:
            print(f'   VERDICT: {p.verdict}  (GET_INFO failed)', file=stream)
            continue
        print(f'   info=0x{p.info:02x} [{p.info_flags}]   len={p.length}',
              file=stream)
        print(f'   min={fmt_raw(p.raw_min)}  max={fmt_raw(p.raw_max)}  '
              f'def={fmt_raw(p.raw_def)}  res={fmt_raw(p.raw_res)}',
              file=stream)
        print(f'   cur={fmt_raw(p.raw_cur)}', file=stream)
        if p.probe_value is not None:
            print(f'   SET probe={p.probe_value} → {p.raw_after.hex() if p.raw_after else "?"}'
                  f'   persisted={p.persisted}  restored={p.restored}',
                  file=stream)
        for e in p.errors:
            print(f'   ! {e}', file=stream)
        print(f'   VERDICT: {p.verdict}', file=stream)

    # Summary table.
    print('\n── Summary ──', file=stream)
    print(f'{"name":<18}{"verdict":<22}{"len":>4}  {"cur":<14}{"min..max":<22}',
          file=stream)
    for p in probes:
        cur_s = fmt_raw(p.raw_cur).split(' ')[-1].strip('()') if p.raw_cur else '—'
        rng   = f'{as_int(p.raw_min)}..{as_int(p.raw_max)}' if p.raw_min and p.raw_max else '—'
        ln    = p.length if p.length is not None else '—'
        print(f'{p.name:<18}{p.verdict:<22}{str(ln):>4}  {cur_s:<14}{rng:<22}',
              file=stream)


# ── Entry point ──────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--write', action='store_true',
                    help='attempt SET round-trips on writable selectors')
    ap.add_argument('--json', action='store_true',
                    help='emit JSON to stdout (human report on stderr)')
    ap.add_argument('--only', metavar='NAMES',
                    help='comma-separated subset of candidate names to probe')
    ap.add_argument('--list', action='store_true',
                    help='list candidate names and exit')
    args = ap.parse_args()

    if args.list:
        for c in CANDIDATES:
            print(f'  {c.name:<18} u{c.unit} sel=0x{c.sel:02x}  {c.proto_xu}')
        return

    selected = CANDIDATES
    if args.only:
        wanted = {n.strip() for n in args.only.split(',')}
        selected = [c for c in CANDIDATES if c.name in wanted]
        unknown = wanted - {c.name for c in CANDIDATES}
        if unknown:
            print(f'✗ unknown candidate(s): {sorted(unknown)}', file=sys.stderr)
            sys.exit(2)

    try:
        handle = usb._get_handle()
    except RuntimeError as e:
        print(f'✗ {e}', file=sys.stderr)
        sys.exit(3)

    print(f'# probing {len(selected)} selector(s)  '
          f'write={args.write}  json={args.json}', file=sys.stderr)

    probes = [probe_one(handle, c, write=args.write) for c in selected]

    if args.json:
        print_human(probes, stream=sys.stderr)
        print(json.dumps([p.to_dict() for p in probes], indent=2))
    else:
        print_human(probes)


if __name__ == '__main__':
    main()
