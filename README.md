# link-ctl

[![PyPI version](https://img.shields.io/pypi/v/link-ctl.svg)](https://pypi.org/project/link-ctl/)
[![Python](https://img.shields.io/pypi/pyversions/link-ctl.svg)](https://pypi.org/project/link-ctl/)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![Buy Me a Coffee](https://img.shields.io/badge/Buy%20Me%20a%20Coffee-csmarshall-yellow?logo=buy-me-a-coffee)](https://buymeacoffee.com/cs_marshall)
[![Support the EFF](https://img.shields.io/badge/Support%20the%20EFF-donate-blue)](https://supporters.eff.org/donate/donate-eff-4)

## Why this exists

The Insta360 Link (original) is a great webcam. The Link Controller desktop app is fine.
But when Insta360 added first-party Stream Deck plugin support, it was [only for the Link 2](https://www.reddit.com/r/Insta360/comments/1rmmpxh/og_insta360_link_doesnt_get_streamdeck_support/) —
leaving original Link owners with a suggestion to use global hotkeys instead.

That's a reasonable workaround, but it felt like a miss for people who had invested in the
original hardware. The Link Controller app already exposes a local WebSocket API for its
mobile remote — all the capability was there. This tool uses it.

If you have an OG Link and want proper automation, Stream Deck support, or scripting,
this is for you. Connecting software you own to hardware you own is [something worth protecting](https://www.eff.org/deeplinks/2019/10/adversarial-interoperability).

---

## Table of Contents

- [Feature Matrix](#feature-matrix)
- [Requirements](#requirements)
- [Installation](#installation)
- [Quick Start](#quick-start)
- [Commands](#commands)
  - [PTZ Control](#ptz-control)
  - [Privacy](#privacy)
  - [AI Modes](#ai-modes-macoswindows-only)
  - [Image Settings](#image-settings-macoswindows-only)
  - [Presets](#presets)
  - [Diagnostics](#diagnostics)
  - [Global Flags](#global-flags)
- [Preflight Checks](#preflight-checks)
- [Port Discovery](#port-discovery)
- [State Cache](#state-cache)
- [Exit Codes](#exit-codes)
- [Stream Deck Setup](#stream-deck-setup)
- [Linux](#linux)
- [Protocol Notes](#protocol-notes)
- [Testing & Validation](#testing--validation)
- [Reverse Engineering the API](#reverse-engineering-the-api)
- [Development](#development)

---

CLI tool to control an **Insta360 Link** (original) webcam by communicating
with the **Insta360 Link Controller** desktop app via its local WebSocket server.

> **Newer models:** Compatibility with the
> [Link 2](https://github.com/csmarshall/link-ctl/issues/5),
> [Link 2C](https://github.com/csmarshall/link-ctl/issues/6),
> [Link 2 Pro](https://github.com/csmarshall/link-ctl/issues/7), and
> [Link 2C Pro](https://github.com/csmarshall/link-ctl/issues/8)
> is unverified — if you have one, give it a try and report back!

The Link Controller app must be running — that's expected and fine. You can
minimize it or disable the preview; the WebSocket server stays active.

---

## Feature Matrix

Legend: ✅ confirmed + validated · ⚠️ confirmed sent, limited/no readback · ❌ not supported

**PTZ**

| UI Feature | CLI Command | paramType | validate.py | Notes |
|---|---|---|---|---|
| Zoom | `zoom N` / `zoom-rel ±N` | 4 | ✅ zoom | 100–400 |
| Pan | `pan-rel N` | 6/7 | — | Velocity pulse; no absolute positioning |
| Tilt | `tilt-rel N` | 6/7 | — | Velocity pulse |
| Center/reset | `center` | 3 | — | Resets pan, tilt, and zoom |
| Privacy | `privacy on\|off\|toggle` | 6/7 | — | Tilt to bottom stop (3.5 s pulse) |
| Absolute pan/tilt | — | — | — | ❌ v2.2.1 is velocity-only; exits with code 4 |

**AI Modes**

| UI Feature | CLI Command | paramType | validate.py | Notes |
|---|---|---|---|---|
| Normal | `normal` | 5 | — | Clears all AI modes |
| AI Tracking | `track on\|off\|toggle` | 5 | ✅ track | Smart toggle reads mode field |
| Overhead | `overhead on\|off\|toggle` | 5 | ✅ overhead | |
| DeskView | `deskview on\|off\|toggle` | 5 | ✅ deskview | |
| Whiteboard | `whiteboard on\|off\|toggle` | 5 | ✅ whiteboard | |

**Image Settings**

| UI Feature | CLI Command | paramType | validate.py | Notes |
|---|---|---|---|---|
| HDR | `hdr on\|off\|toggle` | 26 | ✅ hdr | |
| Auto Focus | `autofocus on\|off` | 18 | — | ✅ Confirmed from tshark; no DeviceInfo readback, explicit on/off required |
| Auto Exposure | `autoexposure on\|off\|toggle` | 17 | ✅ autoexposure | |
| Exposure Comp | `exposurecomp 0-100` | 16 | ✅ exposurecomp | 50 = 0 EV; only active when AE is off |
| Auto White Balance | `awb on\|off\|toggle` | 20 | ✅ awb | |
| WB Temperature | `wb-temp 2800-10000` | 21 | ✅ wb-temp | Kelvin; only active when AWB is off |
| Brightness | `brightness 0-100` | 22 | ✅ brightness | Default 50 |
| Contrast | `contrast 0-100` | 23 | ✅ contrast | Default 50 |
| Saturation | `saturation 0-100` | 24 | ✅ saturation | Default 50; 0 = greyscale |
| Sharpness | `sharpness 0-100` | 25 | ✅ sharpness | Default 50 |
| Anti-Flicker | `anti-flicker auto\|50hz\|60hz` | 27 | — | ✅ All three values confirmed |
| Horizontal Flip | `mirror on\|off\|toggle` | 2 | — | ⚠️ DeviceInfo mirror field does not update; toggle defaults to on |
| Smart Composition | `smartcomposition on\|off\|toggle` | 11 | — | ✅ Confirmed from capture; requires AI tracking on |
| Smart Comp Frame | `smartcomp-frame head\|halfbody\|wholebody` | 10 | — | ✅ Confirmed from capture |
| Gesture Zoom | `gesture-zoom on\|off\|toggle` | 39 | — | ⚠️ No DeviceInfo readback |

**Presets**

| UI Feature | CLI Command | paramType | validate.py | Notes |
|---|---|---|---|---|
| Preset recall | `preset 0-19` | — | ⚠️ preset | ✅ Live tested; serial in field 4 (confirmed from capture) |
| Preset save | `preset-save 0-19` | — | ⚠️ preset-save | ✅ Live tested |
| Preset delete | `preset-delete 0-19` | — | ⚠️ preset-delete | ✅ Live tested; slot disappears from UI |

**Diagnostics**

| UI Feature | CLI Command | paramType | validate.py | Notes |
|---|---|---|---|---|
| Status | `status` | — | — | JSON dump of all DeviceInfo fields |
| Preflight | `preflight` | — | — | Checks process, USB, port, handshake |
| Port discovery | `discover` | — | — | lsof → priority ports → range scan |

### Platform coverage

| Platform | PTZ | AI modes | Image settings | Preflight | Tested |
|----------|-----|----------|----------------|-----------|--------|
| macOS | ✓ WebSocket | ✓ | ✓ | ✓ | Yes |
| Windows | ✓ WebSocket | ✓ | ✓ | ✓ (tasklist/wmic) | **No — code written, never run on Windows** |
| Linux | ✓ v4l2-ctl | ✗ (exit 4) | ✗ (exit 4) | ✗ | Partial |

### Outstanding work

- [ ] **Windows validation** ([#2](https://github.com/csmarshall/link-ctl/issues/2)) — run `preflight`, `status`, and `validate.py` on a real Windows machine with the camera connected
- [ ] **Newer camera compatibility** — unverified on [Link 2](https://github.com/csmarshall/link-ctl/issues/5), [Link 2C](https://github.com/csmarshall/link-ctl/issues/6), [Link 2 Pro](https://github.com/csmarshall/link-ctl/issues/7), [Link 2C Pro](https://github.com/csmarshall/link-ctl/issues/8)

### Resolved

- [x] **Anti-flicker** — paramType=27 confirmed; Auto=`"0"`, 50Hz=`"1"`, 60Hz=`"2"` all eyeball-confirmed.
- [x] **`smartcomposition` + `smartcomp-frame`** — confirmed from tshark capture. paramType=11 (on/off), paramType=10 (head/halfbody/wholebody). Requires AI tracking to be active.
- [x] **`autofocus` paramType** — paramType=18 confirmed from tshark capture. No DeviceInfo readback; explicit on/off required.
- [x] **Exposure compensation range** — paramType=16 confirmed, value 0–100, validated in `validate.py`.
- [x] **`preset-save`/`preset`/`preset-delete`** — live tested: save moves camera to position, recall returns to it, delete removes slot from UI. Wire format confirmed from tshark capture — serial is in field 4 (not field 3 as the proto schema suggested).
- [x] **Mirror/flip readback** — `DeviceInfo.mirror` does not update when paramType=2 is sent. Accepted limitation; `toggle` defaults to `on` when state is unknown. Documented in API.md.
- [x] **`autofocus` state readback** — structurally impossible; DeviceInfo has no autofocus field. Requires explicit `on|off`. Documented.
- [x] **WB temperature** — paramType=21 confirmed from tshark; `wb-temp <K>` command added; validated in `validate.py`.
- [x] **Gesture control zoom** — paramType=39 confirmed from tshark; `gesture-zoom on|off|toggle` command added.
- [x] **`validate.py`** — 17/17 tests passing: zoom, track, overhead, deskview, whiteboard, hdr, brightness, contrast, saturation, sharpness, exposurecomp, autoexposure, awb, wb-temp, preset-save, preset, preset-delete.

---

## Requirements

| Requirement | Notes |
|---|---|
| macOS or Windows (primary) | Linux supported for PTZ via `v4l2-ctl`; AI/image commands require the desktop app |
| Insta360 Link Controller ≥ v2.2.1 | Exposes the WebSocket server used by the mobile remote |
| Python ≥ 3.11 | |
| `websockets` ≥ 11 | `pip install websockets` |

---

## Installation

```bash
# Homebrew (macOS)
brew tap csmarshall/link-ctl https://github.com/csmarshall/link-ctl
brew install link-ctl

# pipx — recommended for Python CLI tools
pipx install link-ctl

# pip
pip install link-ctl
```

> **No pipx?** `brew install pipx && pipx ensurepath`

---

## Quick Start

```bash
# Verify everything is working
link-ctl preflight

# Find the WebSocket port
link-ctl discover

# Show current device state
link-ctl status

# Center the camera
link-ctl center

# Enable AI tracking
link-ctl track on

# Zoom in
link-ctl zoom 200

# Enter privacy mode (lens points straight down)
link-ctl privacy on
```

---

## Commands

### PTZ Control

```bash
link-ctl pan-rel <steps>   # Relative pan,  -30 .. 30 steps (velocity pulse)
link-ctl tilt-rel <steps>  # Relative tilt, -30 .. 30 steps (velocity pulse)
link-ctl zoom <value>      # Absolute zoom:  100 .. 400
link-ctl zoom-rel <delta>  # Relative zoom (e.g. 50 or -50)
link-ctl center            # Reset pan/tilt to center, zoom to 100
```

> **Note:** The v2.2.1 WebSocket API is velocity-only for pan/tilt. There is no
> absolute pan/tilt command via WebSocket. `pan-rel` and `tilt-rel` work by
> sending a joystick velocity for a calibrated duration.

### Privacy

```bash
link-ctl privacy on        # Tilt lens straight down (privacy position)
link-ctl privacy off       # Return to center
link-ctl privacy           # Smart toggle (same as 'toggle')
link-ctl privacy toggle    # Explicit toggle
```

### AI Modes _(macOS/Windows only)_

All AI mode commands accept `on`, `off`, or `toggle`. Omitting the argument
smart-toggles based on the current mode.

```bash
link-ctl track [on|off|toggle]      # Subject tracking
link-ctl deskview [on|off|toggle]   # DeskView (desk surface view)
link-ctl whiteboard [on|off|toggle] # Whiteboard mode
link-ctl overhead [on|off|toggle]   # Overhead / top-down
link-ctl normal                     # Return to standard mode (clears all AI modes)
```

### Image Settings _(macOS/Windows only)_

Toggle commands accept `on`, `off`, or `toggle`. Omitting the argument
smart-toggles based on current device state. Exceptions:
- `autofocus` — requires explicit `on` or `off`; camera does not report autofocus state
- `mirror` — toggle defaults to `on` when state is unknown (DeviceInfo mirror field is unreliable)
- `gesture-zoom` — toggle defaults to `on` when state is unknown (no DeviceInfo readback)

```bash
link-ctl hdr [on|off|toggle]               # HDR
link-ctl autoexposure [on|off|toggle]      # Auto exposure
link-ctl awb [on|off|toggle]               # Auto white balance
link-ctl smartcomposition [on|off|toggle]  # Smart Composition (requires AI tracking on)
link-ctl smartcomp-frame head|halfbody|wholebody  # Smart Composition framing
link-ctl autofocus on|off                  # Auto focus (explicit on/off; no readback)
link-ctl anti-flicker auto|50hz|60hz      # Anti-flicker mode
link-ctl brightness <0-100>               # Brightness (default 50)
link-ctl contrast <0-100>                 # Contrast (default 50)
link-ctl saturation <0-100>               # Saturation (default 50; 0 = greyscale)
link-ctl sharpness <0-100>               # Sharpness (default 50)
link-ctl exposurecomp <0-100>            # Exposure compensation (50 = 0 EV; AE must be off)
link-ctl wb-temp <2800-10000>            # WB temperature in Kelvin (AWB must be off)
link-ctl mirror [on|off|toggle]          # Horizontal flip (toggle defaults to on)
link-ctl gesture-zoom [on|off|toggle]    # Gesture control zoom (toggle defaults to on)
```

### Presets

```bash
link-ctl preset <0-19>         # Recall a saved preset position
link-ctl preset-save <0-19>    # Save current position as preset
link-ctl preset-delete <0-19>  # Delete a preset slot
```

### Diagnostics

```bash
link-ctl preflight          # Run all validation checks (process, USB, port, handshake)
link-ctl discover           # Find the WebSocket port and cache it
link-ctl discover --verbose # Show every port tried during scan
link-ctl status             # Dump full device info as JSON
```

### Global Flags

```
-v, --verbose     Show current and new values on each change (default)
-q, --quiet       Suppress informational output; show errors only
-s, --silent      Suppress all output including errors (useful for scripts)
-d, --debug       Hex-dump every WebSocket frame sent/received (to stderr)
    --port N      Override port discovery; connect to port N directly
    --skip-preflight  Skip process/USB/port checks (use cached port)
```

By default (`--verbose`) every command prints a `current → new` line to stderr
before applying, e.g. `brightness: 50 → 80`. Use `--quiet` for Stream Deck
scripts where you only want errors, or `--silent` for fully silent automation.

---

## Preflight Checks

Before every command, `link-ctl` validates:

1. **Controller running** — `pgrep -f "Insta360 Link Controller"` (macOS) / `tasklist` (Windows)
2. **Camera USB** — `ioreg -p IOUSB` (macOS) or `wmic Win32_PnPEntity` (Windows)
3. **Port discovery** — cache → lsof → priority scan → range scan
4. **WebSocket handshake** — connects and exchanges controlRequest/Response

Skip with `--skip-preflight` when you need maximum speed (e.g., rapid Stream
Deck presses) and know the environment is already validated.

---

## Port Discovery

The tool finds the WebSocket port in this order:

1. Cached value in `~/.config/link-ctl/state.json` (valid for 5 minutes)
2. `lsof` by PID (macOS/Linux) or `netstat -ano` (Windows)
3. Priority ports: 7878, 9090, 9091, 9000, 8080
4. Range scan: 7000–7999, 9000–9099, 49900–49950

The discovered port is cached automatically.

---

## State Cache

`~/.config/link-ctl/state.json` stores:

```json
{
  "port": 7878,
  "timestamp": 1710000000.0,
  "deviceSerialNum": "AABBCCDD12345678",
  "zoom": 100
}
```

`zoom` is read from the device on each connection and used by `zoom-rel` to
compute the new absolute value.

---

## Exit Codes

| Code | Meaning |
|------|---------|
| 0 | Success |
| 1 | Command error (bad args, out of range) |
| 2 | Preflight failed (controller not running, camera not found, port not found) |
| 3 | Connection error (WebSocket failed, timeout, rejected) |
| 4 | Command not supported on this platform |

---

## Stream Deck Setup

The `streamdeck/` directory contains ready-to-use shell scripts.

### Setup

1. In Elgato Stream Deck software, add a **System → Open** or **Multi Action**
   button for each script, or use the **Stream Deck Shell** / **KMtricks** plugin
   to run shell scripts.
2. Point the script path at the file, e.g.:
   ```
   /Users/you/work/link-ctl/streamdeck/track_on.sh
   ```
3. All scripts are self-contained — they resolve `link-ctl.py` relative to
   their own location, so moving the directory keeps them working.

### Available Scripts

| Script | Action |
|---|---|
| `center.sh` | Reset pan, tilt, zoom to defaults |
| `track_on.sh` / `track_off.sh` | Toggle AI tracking |
| `deskview_on.sh` / `deskview_off.sh` | Toggle DeskView |
| `whiteboard_on.sh` / `whiteboard_off.sh` | Toggle whiteboard mode |
| `privacy_on.sh` / `privacy_off.sh` | Enter / exit privacy mode |
| `zoom_in.sh` | Zoom in +50 (incremental) |
| `zoom_out.sh` | Zoom out −50 (incremental) |
| `normal.sh` | Return to standard mode |

### Performance Tips

- First run after boot may take ~0.5 s while the port is discovered and cached.
- Subsequent runs use the cached port and complete in well under 1 s.
- If the Link Controller is restarted, the cache invalidates automatically on
  the next failed connection and re-discovery happens transparently.

---

## Linux

PTZ commands fall back to `v4l2-ctl` automatically:

```bash
sudo apt install v4l-utils
link-ctl zoom 200      # → v4l2-ctl -d /dev/video0 --set-ctrl zoom_absolute=200
link-ctl pan-rel 5     # → v4l2-ctl -d /dev/video0 --set-ctrl pan_relative=5
```

Override the device with:
```bash
LINK_CTL_V4L2_DEVICE=/dev/video2 link-ctl zoom 150
```

AI modes and image settings exit with code 4 on Linux — they require the
Link Controller app (macOS/Windows only).

---

## Protocol Notes

Messages are binary Protocol Buffers sent over a local WebSocket. The tool uses
manual wire encoding — no compiled protobuf library required, just `websockets`.

**Tested against Link Controller v2.2.1.**

Key message flow:
```
client → server: (connect to ws://localhost:PORT/?token=TOKEN)
server → client: connectionNotify { connectNum, inControl }
client → server: controlRequest  { token }
server → client: controlResponse { success: true }
server → client: deviceInfoNotify { devices: [...] }
client → server: <command>
client → server: (close)
```

All camera commands use `ValueChangeNotification` (outer field 7 + field 16).
Preset recall uses `PresetUpdateRequest` (outer field 6 + field 15).

### Confirmed paramType Map

| paramType | Feature | Value |
|-----------|---------|-------|
| 2 | Horizontal flip | `"1"` / `"0"` |
| 3 | Reset pan/tilt to center | _(no value)_ |
| 4 | Zoom | `"100"` – `"400"` |
| 5 | AI mode | `"0"`=normal `"1"`=tracking `"4"`=overhead `"5"`=deskview `"6"`=whiteboard |
| 6 | Joystick velocity (pan/tilt) | float32 sub-message: pan_vel, tilt_vel (−1.0 to +1.0) |
| 7 | Joystick stop | float32 sub-message: 0.0, 0.0 |
| 10 | Smart composition framing | `"1"`=Head `"2"`=HalfBody `"3"`=WholeBody |
| 11 | Smart composition on/off | `"1"` / `"0"` (requires AI tracking on) |
| 16 | Exposure compensation | `"0"` – `"100"` (default 50 = 0 EV) |
| 17 | Auto exposure | `"1"` / `"0"` |
| 18 | Auto focus | `"1"` / `"0"` |
| 20 | Auto white balance | `"1"` / `"0"` |
| 21 | WB temperature | `"2800"` – `"10000"` K (AWB must be off) |
| 22 | Brightness | `"0"` – `"100"` (default 50) |
| 23 | Contrast | `"0"` – `"100"` (default 50) |
| 24 | Saturation | `"0"` – `"100"` (default 50; 0 = greyscale) |
| 25 | Sharpness | `"0"` – `"100"` (default 50) |
| 26 | HDR | `"1"` / `"0"` |
| 27 | Anti-flicker | `"0"`=Auto `"1"`=50Hz `"2"`=60Hz |
| 39 | Gesture control zoom | `"1"` / `"0"` |

> **Note:** All paramTypes above were confirmed by tshark capture or direct WS test (2026-03-10).
> The proto enum values in the app bundle have incorrect numbers for v2.2.1 — do not use them.

Preset recall uses `PresetUpdateRequest`: type=4 (RECALL), position=index (0-based), serial.

Reference: <https://dt.in.th/Insta360LinkControllerWebSocketProtocol>

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
python3 validate.py

# From a QR code URL (extracts port + token automatically)
python3 validate.py "http://link-controller.insta360.com/v3/link/?port=49924&token=..."

# List all test names
python3 validate.py --list

# Run specific tests
python3 validate.py --only zoom --only hdr

# Skip a test
python3 validate.py --skip overhead
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
python3 apitest.py

# 3. Ctrl-C tshark when done, then decode the capture:
#    correlate timestamps in apitest output with packet hex in the capture
```

Flags:
```
python3 apitest.py --port 49924   # explicit port
python3 apitest.py "http://..."   # port+token from QR URL
python3 apitest.py --no-wait      # skip the Enter prompt (non-interactive)
python3 apitest.py --debug        # hex-dump every WebSocket frame
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
python3 mobileui-dump.py "http://link-controller.insta360.com/v3/link/?port=PORT&token=TOKEN"
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
python3 apitest.py --port PORT
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
field 7  = true (bool, varint 1)   ← "hasValueChangeNotify"
field 16 = <bytes>                  ← ValueChangeNotification message
  field 1 = <serial number string>
  field 2 = <paramType int>
  field 3 = <newValue string>       ← optional; absent for reset (paramType 3)
  field 4 = <sub-message>           ← joystick only (float32 pan, float32 tilt)
```

Server responses include `deviceInfoNotify` (field 10) after most commands,
which contains the updated camera state — this is how `validate.py` verifies
that a command actually changed something.

### Outer message envelope (v2.2.1)

```
field 4  bool + field 13 msg  → ControlRequest   (auth handshake)
field 5  bool + field 14 msg  → HeartbeatRequest
field 6  bool + field 15 msg  → PresetUpdateRequest   (preset recall/save)
field 7  bool + field 16 msg  → ValueChangeNotification (all camera commands)
```

### DeviceInfo response structure (v2.2.1)

After handshake and after most commands, the server sends `deviceInfoNotify`
(outer field 10). Its inner layout:

```
field 1  → DeviceInfo message (repeated)
field 2  → curDeviceSerialNum string

DeviceInfo:
  field 1  → deviceName string
  field 2  → serialNum string
  field 4  → mode int  (0=normal, 1=tracking, 4=overhead, 5=deskview, 6=whiteboard)
  field 5  → ZoomInfo message { field1=curValue, field2=minValue, field3=maxValue }
             ⚠️  mirror is also read from field 5 as a varint fallback, but is
             unreliable — DeviceInfo does not update after a flip command.
  field 9  → curPresetPos int
  field 10 → image settings sub-message:
               field 9  = HDR bool
               field 12 = brightness int
               field 13 = contrast int
               field 14 = saturation int
               field 15 = sharpness int
               field 17 = autoExposure bool
               field 20 = exposureComp int (0–100; 50 = 0 EV)
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
# → commit message becomes: "v1.0.5 — add brightness command"
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
