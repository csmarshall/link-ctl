# Insta360 Link 2 — Linux USB Control Progress

Device: **Insta360 Link 2** (`2e1a:4c04`)

## Implementation status

| Component | Status |
|-----------|--------|
| `link_usb_linux.py` | Hybrid ioctl (XU 9/10/11) + libusb/v4l2 (CT/PU) |
| `tools/uvc-probe-linux` | libusb probe; `--detach` for CT/PU |
| `link_ctl.py` Linux dispatch | USB-direct first; v4l2 fallback |
| `tools/validate.py --backend usb` | Basic round-trip tests |
| `tools/99-insta360-link.rules` | udev permissions for libusb |

## Link 2 vs OG Link (`4c01`) findings

### Confirmed working via ioctl (XU unit 9)

| Sel | Len | Notes |
|-----|-----|-------|
| 0x02 | 61 | AI video mode — SET via ioctl RMW; GET via ioctl. **Link 2 readback: byte[0]=0xFF when tracking** (OG Link uses 0x01) |
| 0x09 | 2 | Exposure compensation |
| 0x1A | 8 | Pan/tilt readback: **tilt, pan** int32 LE |
| 0x1B | 2 | Func-enable bitmask (`f50b` sample) |
| 0x1E | 1 | AE mode: `2`=auto, `1`=manual |

### Broken on stock Linux driver

- V4L2 `pan_absolute` / `tilt_absolute`: readback returns garbage; SET to `0` fails with "Numerical result out of range"
- `UVCIOC_CTRL_QUERY` on CT (unit 1) and PU (unit 5): `ENOENT` — use libusb instead

### CT/PU access

- libusb control transfers work with `--detach` (see `uvc-probe-linux`)
- Zoom read via CT sel `0x0B`: `6400` hex = 100 decimal
- After `--detach` tests, rebind `uvcvideo` if `/dev/video0` disappears:
  ```bash
  sudo sh -c 'echo 1-10.4.3.1:1.0 > /sys/bus/usb/drivers/uvcvideo/bind'
  sudo sh -c 'echo 1-10.4.3.1:1.1 > /sys/bus/usb/drivers/uvcvideo/bind'
  ```
  Or unplug/replug the camera.

### Open questions

- [ ] XU2 unit 10 privacy selector map (Link 2/2 Pro gimbal-down)
- [ ] Smart composition master switch (bit 0 of `0x1B` — unconfirmed)
- [ ] Full `snapshot` inventory diff vs OG Link

## Quick test

```bash
make -C tools uvc-probe-linux
python3 link_usb_linux.py
python3 link_ctl.py status autoexposure
python3 link_ctl.py track on
python3 tools/validate.py --backend usb --only zoom,track,autoexposure
./tools/uvc-probe-linux snapshot   # libusb; use xu_snapshot_linux.py while streaming
python3 tools/xu_snapshot_linux.py # ioctl snapshot (preferred on Linux)
```

## Sample XU9 sel 0x02 payload (61 bytes)

```
000000000000000000000000000000000000000000000000000000000000000000000000000097feffffaefcffff0000000064000000202a0000000001
```
