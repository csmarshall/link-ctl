# Insta360 Link 2 — Linux USB Control Progress

Device: **Insta360 Link 2** (`2e1a:4c04`)

## Implementation status

| Component | Status |
|-----------|--------|
| `link_usb_linux.py` | Hybrid ioctl (XU 9/10/11) + libusb/v4l2 (CT/PU) |
| `tools/uvc-probe-linux` | libusb probe; `--detach` for CT/PU |
| `link_ctl.py` Linux dispatch | USB-direct first; v4l2 fallback |
| `tools/validate.py --backend usb` | 8/8 round-trip tests on Link 2 |
| `streamdeck/*.sh` | Linux Stream Deck scripts (USB-direct) |
| `streamdeck/opendeck/` | OpenDeck profile + `install.sh` |
| `tools/99-insta360-link.rules` | udev permissions for libusb |

## Link 2 vs OG Link (`4c01`) findings

### Confirmed working via ioctl (XU unit 9)

| Sel | Len | Notes |
|-----|-----|-------|
| 0x02 | 61 | AI video mode — **SET must zero-fill the full 61-byte buffer** then write byte[0]/byte[1] (vrwallace `XU_SetMode`; RMW leaves stale tail bytes and deskview flag `0x10` stuck in byte[1]). GET via ioctl. **Link 2 readback:** byte[0] is authoritative in steady state (`0x00`=off, `0x01`=track, `0x04`=whiteboard, `0x05`=overhead, `0x06`=deskview); byte[1] on GET is often `0x00` or stale `0x10` from a prior deskview — when byte[0]=`0xFF` (active), byte[1] disambiguates track/whiteboard/overhead (`0x00`/`0x01`/`0x03`) |
| 0x09 | 2 | Exposure compensation |
| 0x1A | 8 | Pan/tilt readback on OG Link (**stale on Link 2** — use v4l2) |
| 0x1B | 2 | Func-enable bitmask (`f50b` sample) |
| 0x1E | 1 | AE mode: `2`=auto, `1`=manual |

### XU unit 10 (Link 2 privacy)

| Sel | Len | Notes |
|-----|-----|-------|
| 0x0F | 2 | Privacy mode: `0x0002`=on, `0x0000`=off (+ func-enable bit 11) |

```bash
python3 link_ctl.py privacy on
python3 link_ctl.py status privacy
streamdeck/privacy_on.sh
```

### Broken on stock Linux driver

- V4L2 `pan_absolute` / `tilt_absolute`: **GET reliable on Link 2**; SET to `(0,0)` via v4l2 fails — use libusb CT `0x0D`
- XU1 sel `0x1A` readback: **stale on Link 2** (tilt word stuck ~`-306000`); use v4l2 for pan/tilt readback
- V4L2 AWB control is named `white_balance_automatic` (not `white_balance_temperature_auto`)
- `UVCIOC_CTRL_QUERY` on CT (unit 1) and PU (unit 5): `ENOENT` — use libusb instead

### CT/PU access

- libusb control transfers work with `--detach` (see `uvc-probe-linux`)
- Zoom read via CT sel `0x0B`: `6400` hex = 100 decimal
- After `--detach` tests, rebind `uvcvideo` if `/dev/video0` disappears:
  ```bash
  link-ctl reset              # preferred — auto-discovers bus path, tries handle/libusb/sysfs
  sudo tools/reset_link2.sh   # when sysfs bind needs root (typical)
  streamdeck/reset.sh         # OpenDeck / hotkey wrapper
  ```
  Manual sysfs rebind (bus path varies — discover with `lsusb -t` or sysfs):
  ```bash
  # Example only; use link-ctl reset to auto-discover 1-10.4.3.1:1.0 etc.
  sudo sh -c 'echo 1-10.4.3.1:1.0 > /sys/bus/usb/drivers/uvcvideo/bind'
  sudo sh -c 'echo 1-10.4.3.1:1.1 > /sys/bus/usb/drivers/uvcvideo/bind'
  ```
  Or unplug/replug the camera.

### USB recovery (`reset` / `recover`)

When libusb detach tests leave the Link hung (USB visible in `lsusb` but no `/dev/video*`):

| Step | Method | Sudo? | When it helps |
|------|--------|-------|---------------|
| 1 | Close/reopen USB handle | No | Stale handle after soft detach |
| 2 | `libusb_reset_device` | No* | USB protocol reset (*udev `99-insta360-link.rules`) |
| 3 | `uvcvideo` unbind/rebind via sysfs | **Yes** | Interfaces detached, no video node |
| 4 | Wait for `/dev/video*` | No | Confirms recovery |

```bash
link-ctl reset --verbose
link-ctl recover              # alias
python3 -m link_usb_linux     # smoke test after reset
```

Discovery scans `/sys/bus/usb/devices` for `idVendor=2e1a` and known PIDs only — never resets unrelated USB devices.


### AI mode wire format (XU9 sel 0x02, 61 bytes on Link 2)

SET (vrwallace / link-ctl `write_ai_mode`):

| Mode | byte[0] | byte[1] | Notes |
|------|---------|---------|-------|
| Off / normal | `0x00` | `0x00` | Link 2: RMW — preserve tail bytes (offset 52+); zero-fill breaks mode SET |
| Track | `0x01` | `0x00` | |
| Whiteboard | `0x04` | `0x01` | |
| Overhead | `0x05` | `0x03` | Exit current mode + disable privacy before SET on Link 2 |
| DeskView | `0x06` | `0x10` | Active readback: `0x06/0x11` or `0xFF/0x10`; byte[1] `0x10` cleared on normal |

**DeskView off:** `streamdeck/deskview_off.sh` calls `deskview off`, `normal`, then `center`.

**Privacy readback:** use func-enable bit 11 on Link 2; unit 10/0x0F GET can echo `0x03fd` (func-enable) when idle.

### Open questions

- [x] DeskView on/off — Link 2 RMW tail bytes + explicit normal + center on off
- [x] Overhead — exit prior mode and disable privacy before SET; RMW not zero-fill
- [x] Privacy readback — func-enable bit 11 (unit 10/0x0F GET unreliable)
- [x] Mirror — func-enable bit 3 SET with verify/retry on Link 2
- [ ] Smart composition master switch (bit 0 of `0x1B` — unconfirmed)
- [ ] Full `snapshot` inventory diff vs OG Link

Probe AI modes (camera must be plugged in):

```bash
python3 tools/probe_ai_modes.py
```

## Quick test

```bash
make -C tools uvc-probe-linux
python3 link_usb_linux.py
python3 link_ctl.py status autoexposure
python3 link_ctl.py track on
python3 tools/validate.py --backend usb --only zoom,track,center,autoexposure,awb,hdr,mirror,brightness
./tools/uvc-probe-linux snapshot   # libusb; use xu_snapshot_linux.py while streaming
python3 tools/xu_snapshot_linux.py # ioctl snapshot (preferred on Linux)
```

## Sample XU9 sel 0x02 payload (61 bytes)

```
000000000000000000000000000000000000000000000000000000000000000000000000000097feffffaefcffff0000000064000000202a0000000001
```
