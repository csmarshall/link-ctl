#!/usr/bin/env python3
"""Capture JPEG + USB/v4l2 diagnostics for streamdeck smoke tests.

Usage:
  python3 tools/smoke_capture.py <label> <output.jpg>

Prints one line of register readback to stdout and the image path (or SKIP if
capture failed, e.g. /dev/video0 held open by Discord/OBS).
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import link_ctl as lc  # noqa: E402


def _video_device() -> str:
    return os.environ.get('LINK_CTL_V4L2_DEVICE', '/dev/video0')


def read_diagnostics() -> dict:
    """Pan/tilt, privacy bit 11, func-enable 0x1b, AI mode buffer."""
    lc.reset_usb_caches()
    diag: dict = {}
    try:
        pan, tilt = lc.read_pantilt()
        diag['pan'] = pan
        diag['tilt'] = tilt
    except Exception as exc:
        diag['pan'] = None
        diag['tilt'] = None
        diag['pantilt_error'] = str(exc)

    try:
        bm = lc._bitmask_get()
        diag['func_enable'] = f'0x{bm:04x}'
        diag['privacy_bit11'] = lc._bitmask_get_bit(lc.BIT_PRIVACY)
        diag['mirror_bit3'] = lc._bitmask_get_bit(lc.BIT_MIRROR)
        diag['privacy_status'] = lc.read_privacy()
    except Exception as exc:
        diag['func_enable'] = '?'
        diag['privacy_bit11'] = None
        diag['diagnostics_error'] = str(exc)

    try:
        raw = lc._ai_mode_get_raw()
        diag['ai_mode'] = f'{raw[0]:02x}/{raw[1]:02x}'
        diag['ai_mode_name'] = lc.read_ai_mode()
    except Exception as exc:
        diag['ai_mode'] = '?/?'
        diag['ai_mode_name'] = 'unknown'
        diag['ai_error'] = str(exc)

    return diag


def format_diagnostics(label: str, diag: dict) -> str:
    pan = diag.get('pan')
    tilt = diag.get('tilt')
    pt = f'pan={pan} tilt={tilt}' if pan is not None else 'pan/tilt=?'
    priv = diag.get('privacy_status')
    b11 = diag.get('privacy_bit11')
    fe = diag.get('func_enable', '?')
    ai = diag.get('ai_mode', '?/?')
    mode = diag.get('ai_mode_name', '?')
    mirror = diag.get('mirror_bit3')
    return (
        f'[{label}] {pt} privacy={priv} bit11={b11} func_enable={fe} '
        f'AI={ai} mode={mode} mirror={mirror}'
    )


def _ffmpeg_capture(device: str, path: Path) -> str | None:
    if not shutil_which('ffmpeg'):
        return 'ffmpeg not found'
    cmd = [
        'ffmpeg', '-hide_banner', '-loglevel', 'error',
        '-f', 'v4l2', '-input_format', 'mjpeg',
        '-video_size', '1280x720',
        '-i', device,
        '-frames:v', '1', '-q:v', '2',
        str(path),
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    if r.returncode != 0:
        err = (r.stderr or r.stdout or '').strip().splitlines()
        return err[-1] if err else f'ffmpeg exit {r.returncode}'
    if not path.is_file() or path.stat().st_size < 500:
        return 'ffmpeg produced empty file'
    return None


def _v4l2_capture(device: str, path: Path) -> str | None:
    if not shutil_which('v4l2-ctl'):
        return 'v4l2-ctl not found'
    cmd = [
        'v4l2-ctl', '-d', device,
        '--set-fmt-video=width=1280,height=720,pixelformat=MJPG',
        '--stream-mmap', '--stream-count=1', '--stream-to', str(path),
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    if r.returncode != 0:
        err = (r.stderr or r.stdout or '').strip().splitlines()
        return err[-1] if err else f'v4l2-ctl exit {r.returncode}'
    if not path.is_file() or path.stat().st_size < 500:
        return 'v4l2-ctl produced empty file'
    return None


def shutil_which(name: str) -> str | None:
    from shutil import which
    return which(name)


def capture_jpeg(path: Path, device: str | None = None) -> tuple[bool, str]:
    """Return (ok, detail). detail is path on success or error/skip reason."""
    dev = device or _video_device()
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()

    for attempt, fn in (('ffmpeg', _ffmpeg_capture), ('v4l2-ctl', _v4l2_capture)):
        err = fn(dev, path)
        if err is None:
            return True, str(path)
        last = f'{attempt}: {err}'
        if path.exists():
            path.unlink(missing_ok=True)
    return False, last


def image_mean_luma(path: Path) -> float | None:
    """Estimate mean luma 0..255 via ffmpeg signalstats (no PIL required)."""
    if not path.is_file() or not shutil_which('ffmpeg'):
        return None
    cmd = [
        'ffmpeg', '-hide_banner', '-i', str(path),
        '-vf', 'format=gray,signalstats', '-f', 'null', '-',
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
    text = r.stderr + r.stdout
    m = re.search(r'YAVG:([\d.]+)', text)
    if m:
        return float(m.group(1))
    # Fallback: sample first few KB of MJPEG — rough dark-frame heuristic
    try:
        data = path.read_bytes()[:8192]
        if data[:2] == b'\xff\xd8':
            return sum(data) / len(data)  # very rough
    except OSError:
        pass
    return None


def privacy_visual_mismatch(diag: dict, luma: float | None, *, dark_threshold: float = 35.0) -> str | None:
    """Flag when register says privacy off but frame looks like lens capped."""
    if luma is None:
        return None
    if diag.get('privacy_status') or diag.get('privacy_bit11'):
        return None
    if luma >= dark_threshold:
        return None
    mode = diag.get('ai_mode_name', '')
    if mode in ('overhead', 'deskview'):
        return (
            f'dark frame (YAVG={luma:.1f}) with privacy=off — likely {mode} gimbal angle, '
            'not privacy bit; recover should center gimbal'
        )
    return (
        f'dark frame (YAVG={luma:.1f}) but privacy=off bit11={diag.get("privacy_bit11")} '
        '— register/physical mismatch or stale gimbal position'
    )


def main() -> int:
    ap = argparse.ArgumentParser(description='Smoke-test frame capture + diagnostics')
    ap.add_argument('label', help='step label, e.g. 02_mirror_on')
    ap.add_argument('output', type=Path, help='output JPEG path')
    ap.add_argument('--device', default=None, help='v4l2 device (default /dev/video0)')
    ap.add_argument('--skip-image', action='store_true', help='diagnostics only')
    args = ap.parse_args()

    diag = read_diagnostics()
    line = format_diagnostics(args.label, diag)
    print(line, flush=True)

    if args.skip_image:
        print(f'CAPTURE: SKIP (diagnostics only) {args.output}', flush=True)
        return 0

    ok, detail = capture_jpeg(args.output, args.device)
    if ok:
        luma = image_mean_luma(args.output)
        if luma is not None:
            print(f'CAPTURE: {detail}  YAVG={luma:.1f}', flush=True)
        else:
            print(f'CAPTURE: {detail}', flush=True)
        warn = privacy_visual_mismatch(diag, luma)
        if warn:
            print(f'VISUAL_WARN: {warn}', flush=True)
            return 2
        return 0

    print(f'CAPTURE: SKIP ({detail}) — close apps using {_video_device()} and retry', flush=True)
    # Write sidecar so agents can still correlate diagnostics with the step.
    sidecar = args.output.with_suffix('.txt')
    sidecar.write_text(line + '\n' + f'capture_error: {detail}\n')
    print(f'SIDECAR: {sidecar}', flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
