# Development & Reverse Engineering

This document covers protocol internals, reverse-engineering methodology, USB direct
control, and development tooling for `link-ctl`. For installation, usage, and command
reference, see [README.md](README.md). For the full WebSocket protocol specification,
see [API.md](API.md).

---

## Testing & Validation

Two scripts support protocol testing and future re-mapping.

### validate.py — Live command validator

Sends each known command, reads device state before and after via a fresh
WebSocket handshake, and reports PASS/FAIL. No tshark or sudo required.

**Prerequisites:**
- Camera connected via USB, Link Controller running
- No mobile web remote open (only one controller at a time)
- `websockets` installed in the Python you run it with

```bash
# Run all tests
python3 tools/validate.py

# From a QR code URL (extracts port + token automatically)
python3 tools/validate.py "http://link-controller.insta360.com/v3/link/?port=49924&token=..."

# List all test names
python3 tools/validate.py --list

# Run specific tests
python3 tools/validate.py --only zoom --only hdr

# Skip a test
python3 tools/validate.py --skip overhead
```

Tests: `zoom`, `track`, `overhead`, `deskview`, `whiteboard`, `hdr`,
`brightness`, `contrast`, `saturation`, `sharpness`, `exposurecomp`, `autoexposure`,
`awb`, `wb-temp`, `preset-save`, `preset`, `preset-delete` (17 total)

Each test sends the command, reconnects to read the updated device state, asserts
the expected field changed, then restores the original value.

### apitest.py — Protocol capture exerciser

Drives every known command in sequence with millisecond-precision timestamps,
for correlating against a simultaneous `tshark` packet capture. Use this when
you need to re-discover paramType assignments after a firmware update.

**Prerequisites (in addition to validate.py prerequisites):**
- `sudo` access to run tshark

```bash
# 1. Start tshark (in a separate terminal)
sudo tshark -i lo0 -Y "websocket" -T fields \
    -e frame.time_relative -e data.data \
    -E header=y > ~/apitest-capture.txt

# 2. Run the exerciser (press Enter when tshark is running)
python3 tools/apitest.py

# 3. Ctrl-C tshark when done, then decode the capture:
#    correlate timestamps in apitest output with packet hex in the capture
```

Flags:
```
python3 tools/apitest.py --port 49924   # explicit port
python3 tools/apitest.py "http://..."   # port+token from QR URL
python3 tools/apitest.py --no-wait      # skip the Enter prompt (non-interactive)
python3 tools/apitest.py --debug        # hex-dump every WebSocket frame
```

### Updating paramType mappings

If a firmware update changes the protocol:

1. Run `apitest.py` with a tshark capture as above.
2. Decode the capture hex and correlate timestamps with the action log.
3. Update `ParamTypeV2` in `link_ctl.py` with any changed values.
4. Update the `build_on` / `build_restore` lambdas in `validate.py`'s `make_tests()`.
5. Run `validate.py` to confirm all commands work end-to-end.

---

## Reverse Engineering the API

This section documents how the WebSocket protocol was discovered and how to
extend it if Insta360 changes things in a future firmware release.

### How the protocol was discovered

The Insta360 Link Controller app exposes a local WebSocket server so its
**mobile web remote** (accessed by scanning a QR code in the app) can control
the camera from a phone. All traffic between the phone and the desktop app
travels over `lo0` (loopback), which makes it trivially capturable without any
HTTPS interception.

The protocol uses **binary Protocol Buffers** — there is no JSON or human-readable
framing. The `.proto` schema (`insta360linkcontroller.proto` in this repo)
provides field names and enum values, but the wire numbers are what matter for
encoding commands.

### Tools required

| Tool | Purpose |
|------|---------|
| `tshark` | CLI packet capture, ships with Wireshark |
| `mobileui-dump.py` | Playwright script that drives the mobile web UI while tshark captures; discovers unknown paramTypes |
| `apitest.py` | Directly sends every known command over WebSocket with timestamps |
| Python protobuf decoder | Inline snippet (see below) to parse raw hex from captures |

Install tshark:
```bash
brew install wireshark   # macOS — tshark is included
sudo apt install tshark  # Linux
```

Install Playwright (for mobileui-dump.py):
```bash
pip install playwright pillow
playwright install chromium
```

### Step 1 — Find the WebSocket port and token

With the Link Controller running and camera connected, the port is on loopback:

```bash
# Find the PID then the listening port
pgrep -f "Insta360 Link Controller"   # → e.g. 1234
lsof -i -a -n -P -p 1234 | grep LISTEN
```

The authentication token is in:
```
~/Library/Application Support/Insta360 Link Controller/startup.ini   # macOS
%LOCALAPPDATA%\Insta360 Link Controller\startup.ini                   # Windows
```

Look for the `[Token]` section — each key is a token string, its value is a
Unix timestamp. The key with the highest timestamp is the current token.

### Step 2 — Get the mobile web URL from the QR code

The mobile web remote URL is shown as a QR code in the app's "Remote Control"
screen. To decode it without a phone:

```bash
# 1. Take a screenshot of the QR code (macOS)
#    Cmd+Shift+4, drag over the QR code — saved to Desktop

# 2. Decode the QR code from the screenshot
brew install zbar   # one-time install
zbarimg --raw -q ~/Desktop/qr_code_screenshot.png
# Prints: http://link-controller.insta360.com/v3/link/?port=49924&token=ABC123...
```

The URL contains the `port` and `token` parameters needed by `validate.py`,
`apitest.py`, and `mobileui-dump.py`.

### Step 3 — Capture the mobile web UI

Start a tshark capture (see Step 1 above for install), then drive the UI:

```bash
# Terminal 1: start capture on loopback
sudo tshark -i lo0 -Y "websocket" -T fields \
    -e frame.time_relative -e data.data \
    -E header=y > ~/mobileui-capture.txt

# Terminal 2: drive every button and slider in the mobile UI
python3 tools/mobileui-dump.py "http://link-controller.insta360.com/v3/link/?port=PORT&token=TOKEN"
# Press Enter when tshark is running, then wait for the script to finish.
```

`mobileui-dump.py` clicks every visible button, sweeps every slider, and prints
a timestamped action log. Correlate the timestamps with packet hex in the capture
to map UI actions to wire bytes.

### Step 4 — Send known commands directly and capture responses

Once you have a working connection, use `apitest.py` to send every command in
isolation and watch what the server sends back:

```bash
# Terminal 1
sudo tshark -i lo0 -Y "websocket" -T fields \
    -e frame.time_relative -e data.data \
    -E header=y > ~/apitest-capture.txt

# Terminal 2
python3 tools/apitest.py --port PORT
```

`apitest.py` also probes unknown paramTypes (8–30) with values `"1"` and `"0"`,
which is how the brightness/contrast/saturation/sharpness/HDR/AWB/autofocus
paramTypes were confirmed.

### Step 5 — Decode the raw protobuf hex

tshark outputs hex-encoded frame data (WebSocket payload). Decode it with this
Python snippet:

```python
import binascii

def read_varint(data, pos):
    result, shift = 0, 0
    while pos < len(data):
        b = data[pos]; pos += 1
        result |= (b & 0x7F) << shift
        shift += 7
        if not (b & 0x80): break
    return result, pos

def decode_fields(data):
    pos, fields = 0, {}
    while pos < len(data):
        tag, pos = read_varint(data, pos)
        field_num, wire = tag >> 3, tag & 7
        if wire == 0:
            val, pos = read_varint(data, pos); fields.setdefault(field_num, []).append(val)
        elif wire == 2:
            l, pos = read_varint(data, pos); fields.setdefault(field_num, []).append(data[pos:pos+l]); pos += l
        elif wire == 1: pos += 8
        elif wire == 5: pos += 4
        else: break
    return fields

# Example: decode a raw hex string from the tshark capture
raw = binascii.unhexlify("3a06...your hex here...")
print(decode_fields(raw))
```

### Step 6 — Map wire bytes to features

Each outgoing command from the client looks like:

```
field 7  = true (bool, varint 1)   <- "hasValueChangeNotify"
field 16 = <bytes>                  <- ValueChangeNotification message
  field 1 = <serial number string>
  field 2 = <paramType int>
  field 3 = <newValue string>       <- optional; absent for reset (paramType 3)
  field 4 = <sub-message>           <- joystick only (float32 pan, float32 tilt)
```

Server responses include `deviceInfoNotify` (field 10) after most commands,
which contains the updated camera state — this is how `validate.py` verifies
that a command actually changed something.

### Outer message envelope (v2.2.1)

```
field 4  bool + field 13 msg  -> ControlRequest   (auth handshake)
field 5  bool + field 14 msg  -> HeartbeatRequest
field 6  bool + field 15 msg  -> PresetUpdateRequest   (preset recall/save)
field 7  bool + field 16 msg  -> ValueChangeNotification (all camera commands)
```

### DeviceInfo response structure (v2.2.1)

After handshake and after most commands, the server sends `deviceInfoNotify`
(outer field 10). Its inner layout:

```
field 1  -> DeviceInfo message (repeated)
field 2  -> curDeviceSerialNum string

DeviceInfo:
  field 1  -> deviceName string
  field 2  -> serialNum string
  field 4  -> mode int  (0=normal, 1=tracking, 4=overhead, 5=deskview, 6=whiteboard)
  field 5  -> ZoomInfo message { field1=curValue, field2=minValue, field3=maxValue }
             Warning: mirror is also read from field 5 as a varint fallback, but is
             unreliable — DeviceInfo does not update after a flip command.
  field 9  -> curPresetPos int
  field 10 -> image settings sub-message:
               field 9  = HDR bool
               field 12 = brightness int
               field 13 = contrast int
               field 14 = saturation int
               field 15 = sharpness int
               field 17 = autoExposure bool
               field 20 = exposureComp int (0-100; 50 = 0 EV)
               field 21 = autoWhiteBalance bool
               field 22 = wbTemp int (Kelvin)
               field 24 = smartComposition bool
```

### Known cascade effects

Some paramTypes trigger cascades on the server side:

| Command | Cascades to |
|---------|-------------|
| paramType=20 (AWB off) | Sets WB temperature to ~4200 K (DeviceInfo field 22) |
| paramType=28/29/30 | Triggers a full device state dump from the server |

### Tips for future firmware updates

- **Start with `validate.py`** — if tests still pass, nothing changed.
- **Check the `.proto` file** — Insta360 occasionally ships an updated
  `insta360linkcontroller.proto` in the app bundle. Diffing it against the
  previous version quickly shows new or renumbered paramTypes.
  On macOS: `find /Applications/Insta360\ Link\ Controller.app -name "*.proto"`
- **Re-run `apitest.py`** — it probes paramTypes 8–30 automatically. Watch the
  tshark capture for server responses (state changes) to identify new mappings.
- **Check `deviceInfoNotify` field numbers** — if image settings stop parsing,
  the sub-message field numbers may have shifted. Use the decoder snippet above
  on a raw capture frame to inspect the actual field layout.

---

## Development

### Version bumping

When `link_ctl.py` is staged, two hooks fire automatically:

1. **`pre-commit`** — bumps the patch version in `pyproject.toml` and stages it
2. **`prepare-commit-msg`** — prepends `v{version} — ` to your commit message

So you just write the description:
```bash
git commit -m "add brightness command"
# -> commit message becomes: "v1.0.5 — add brightness command"
```

Doc-only commits (README, API.md, workflows, etc.) leave the version unchanged.

Activate once after cloning:
```bash
git config core.hooksPath .githooks
```

### CI

Two GitHub Actions workflows run on every push:

- **`ci.yml`** — syntax check, import check, and `--help` smoke test for all
  subcommands (Ubuntu); Homebrew formula install + test (macOS).
- **`release.yml`** — triggered by a version tag (`v*`): publishes to PyPI via
  OIDC trusted publishing, then auto-updates `Formula/link-ctl.rb` with the new
  sdist URL and SHA256 from the PyPI JSON API.

### Releasing

```bash
# Tag the current commit — CI does the rest
git tag v1.0.3
git push origin v1.0.3
```

### Scripts

| Script | Purpose |
|--------|---------|
| `scripts/bump_version.py` | Bumps patch version in `pyproject.toml`; called by pre-commit hook |
| `scripts/update_formula.py` | Updates `Formula/link-ctl.rb` from PyPI JSON API; called by release workflow |

---

## USB Direct Control

The Insta360 Link can be controlled directly over USB via UVC Extension Unit (XU)
registers, removing the dependency on the Link Controller desktop app entirely.
This was discovered through automated XU register probing (see Development Tools below).

For the full confirmed control map, unit layout, func-enable bitmask details, and
noise register documentation, see [API.md — USB Direct Control](API.md#usb-direct-control-uvc-extension-units).

Key points:

- **Camera identity:** VID:PID `2e1a:4c01`
- **Extension Units:** XU1 (unit 9) handles AI mode, HDR/mirror/gesture bitmask,
  exposure, pan/tilt readback. XU2 (unit 10) handles "privacy" on Link 2 only.
  Standard UVC units (CT=1, PU=5) handle zoom, brightness, contrast, etc.
- **macOS access:** IOKit `IOUSBDevRequest` works alongside UVCAssistant for reads
  and writes. No exclusive access required, no `sudo`, and no code signing —
  `uvc-probe` deliberately never calls `USBInterfaceOpen`, which is what would
  otherwise trigger the privilege/entitlement check. This technique is borrowed
  from [jtfrey/uvc-util](https://github.com/jtfrey/uvc-util), which has shipped
  in Homebrew for years using the same no-open pattern. Camera off/on
  (`usb-suspend`) is a different API (`USBDeviceSuspend`) that does require
  an open device handle and therefore still needs `sudo`.

### Confirmed XU selector map (v2.0)

All selectors below are routed through `tools/uvc-probe` by the USB-direct
dispatch in `link_ctl.py` (see `usb_image_dispatch`, `usb_ptz_dispatch`).
Each has an empirical round-trip test in `tools/xu_verify.py`.

| Logical control | CLI command | Unit | Sel | Length | Format | Source |
|---|---|---|---|---|---|---|
| Autofocus | `autofocus` | CT (1) | 0x08 | 1 | 1=auto, 0=manual | verified |
| Zoom | `zoom`, `zoom-rel` | CT (1) | 0x0B | 2 | uint16 LE, 100..400 | verified |
| Pan/Tilt absolute | `pan`, `tilt`, `center` | CT (1) | 0x0D | 8 | int32 LE pan + int32 LE tilt | verified |
| Brightness | `brightness` | PU (5) | 0x02 | 1 | 0..100 | verified |
| Contrast | `contrast` | PU (5) | 0x03 | 1 | 0..100 | verified |
| Anti-flicker | `anti-flicker` | PU (5) | 0x05 | 1 | 3=auto, 1=50Hz, 2=60Hz | verified |
| Saturation | `saturation` | PU (5) | 0x07 | 2 | uint16 LE 0..100 | verified |
| Sharpness | `sharpness` | PU (5) | 0x08 | 2 | uint16 LE 0..100 | verified |
| WB temperature | `wb-temp` | PU (5) | 0x0A | 2 | uint16 LE 2800..10000 (K) | standard UVC |
| AWB | `awb` | PU (5) | 0x0B | 1 | 1=auto, 0=manual | verified |
| AI video mode | `track`, `whiteboard`, `overhead`, `deskview`, `normal` | XU1 (9) | 0x02 | 52 | byte[0]=mode_id, byte[1]=flag | vrwallace |
| Exposure comp | `exposurecomp` | XU1 (9) | 0x09 | 2 | int16 LE `(val−50)×6` | verified |
| Smartcomp framing | `smartcomp-frame` | XU1 (9) | 0x13 | 1 | 1=head, 2=half, 3=full | vrwallace |
| Pan/tilt readback | (internal `read_pantilt`) | XU1 (9) | 0x1A | 8 | int32 LE **tilt** + int32 LE **pan** | verified |
| Func-enable bitmask | `hdr`, `mirror`, `gesture-zoom` | XU1 (9) | 0x1B | 2 | uint16 LE; bit 2=HDR, bit 3=mirror, bit 4=gesture-zoom | verified |
| AE mode | `autoexposure` | XU1 (9) | 0x1E | 1 | 2=auto, 1=manual | verified |

**Byte-order gotcha** — unit 9 sel 0x1A returns `(tilt, pan)` LE, whereas
CT_PANTILT_ABSOLUTE at unit 1 sel 0x0D writes standard UVC `(pan, tilt)` LE.
`read_pantilt()` and `write_pantilt()` hide this.

**Still TBD** — the `smartcomposition` (AiZoom) master switch is presumed
to live at bit 0 of the XU1 sel 0x1B bitmask (per SDK `ExtendFunction` enum
ordering), but empirical verification is inconclusive. The command still
falls through to the WebSocket path for now.

---

## Camera Off/On

The original Link has no firmware-level "privacy" mode — the Insta360 privacy feature
(gimbal tilts lens down) only exists on the Link 2 and 2 Pro. For the original Link,
the only way to truly disable the camera is USB device suspend.

```bash
# Camera OFF (kills UVCAssistant, then suspends USB device)
sudo killall UVCAssistant; sudo tools/usb-suspend suspend

# Camera ON (re-enumerates USB — simulates unplug/replug)
sudo tools/usb-suspend resume
```

Settings (AI tracking, image adjustments, etc.) persist through suspend/resume.
Resume triggers `USBDeviceReEnumerate`, which causes UVCAssistant to auto-respawn
and re-discover the camera. Apps like FaceTime will need to reselect the camera
after resume. Recovery takes about 5 seconds.

For passwordless operation (e.g., Stream Deck buttons), add to `/etc/sudoers`:
```
username ALL=(root) NOPASSWD: /path/to/tools/usb-suspend, /usr/bin/killall UVCAssistant
```

For a comparison of USB suspend vs. Insta360's native privacy mode vs. the Link 2C's
physical lens cover, see [API.md — "Privacy" Mode Comparison](API.md#privacy-mode-comparison).

---

## Development Tools

| File | Description |
|------|-------------|
| `tools/uvc-probe.m` | IOKit UVC register read/write tool. Supports snapshot (dump all selectors), watch (poll for changes), server (pipe mode for xu_capture.py), and direct get/set of individual unit/selector values. Uses the no-open `ControlRequest` pattern from [jtfrey/uvc-util](https://github.com/jtfrey/uvc-util) — no `sudo` or signing required. |
| `tools/usb_suspend.m` | USB device suspend/resume via IOKit. Powers the camera fully off/on without unplugging. Requires sudo (device-level `USBDeviceSuspend` needs an open device handle). |
| `tools/xu_capture.py` | Automated XU control discovery. Snapshots XU register state before/after each WebSocket command to identify which registers change. Three phases: capture, replay (verify writes work without the desktop app), and report. |
| `tools/xu_verify.py` | Phase B verification of XU control read/write. For each confirmed control: reads current value, writes a test value, reads back to verify, and restores the original. Works with the desktop app running. |
| `tools/validate.py` | Live WebSocket command validator. Sends each known command and asserts the expected device state change via DeviceInfo readback. 17 tests covering all confirmed paramTypes. |

---

## Building Native Tools

The IOKit-based tools are Objective-C and must be compiled on macOS.

### uvc-probe

```bash
clang -o tools/uvc-probe tools/uvc-probe.m \
    -framework IOKit -framework CoreFoundation -framework Foundation -ObjC
```

### usb-suspend

```bash
clang -o tools/usb-suspend tools/usb_suspend.m \
    -framework IOKit -framework CoreFoundation -framework Foundation -ObjC
```

### No code signing required

Earlier versions of these tools relied on ad-hoc signing with a
`com.apple.security.device.usb` entitlement to avoid `sudo`. That turned out to
be unnecessary: the entitlement is only enforced for sandboxed apps, and the
privilege check was actually triggered by `USBInterfaceOpen`, not by
`ControlRequest`. `uvc-probe` now skips `USBInterfaceOpen` entirely and
issues control transfers on the plugin handle alone — the same pattern
[jtfrey/uvc-util](https://github.com/jtfrey/uvc-util) has used in Homebrew for
years. No `sudo`, no signing, no entitlement — just `clang` and run.

`usb-suspend` is different: `USBDeviceSuspend` is a device-level call that
requires an open handle, and opening the device while UVCAssistant owns it
requires seizing (root UID check). A paid Apple Developer account would not
change this — seize is a kernel check on effective UID, not a signing check.
The practical options for camera off/on without sudo would be an SMJobBless
privileged helper (large engineering lift) or a full CMIO Camera Extension
(even larger). The sudoers entry documented below is the pragmatic answer.

---

## Acknowledgements

- **[jtfrey/uvc-util](https://github.com/jtfrey/uvc-util)** — the IOKit
  "never open the interface" pattern used by `tools/uvc-probe` comes from
  this tool. Without it, `link-ctl` would still depend on `sudo` or ad-hoc
  code signing to read and write UVC Extension Unit registers on macOS.
