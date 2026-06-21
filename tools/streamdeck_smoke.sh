#!/usr/bin/env bash
# Smoke-test the exact paths OpenDeck runs (bash -lc streamdeck/*.sh).
# One pass, then restore normal — not a destructive loop.
#
# Visual verification: JPEG + register readback after each step under
# tools/smoke_captures/<timestamp>/ (close Discord/OBS if capture says busy).
#
# Optional: LINK_CTL_SMOKE_CENTER=1 or LINK_CTL_USB_DETACH=1 runs center after
# each step (needs udev detach permission; restores gimbal after overhead/deskview).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PY="${LINK_CTL_PYTHON:-/usr/bin/python3}"
if [[ ! -x "$PY" ]]; then PY="$(command -v python3)"; fi
SD="$ROOT/streamdeck"
CAPTURE_PY="$ROOT/tools/smoke_capture.py"
LOG="${1:-streamdeck_smoke_$(date +%F-%H%M%S).log}"
CAP_DIR="$ROOT/tools/smoke_captures/$(date +%Y%m%d-%H%M%S)"
CAP_N=0
VISUAL_WARN=0

if ! ls /dev/video* >/dev/null 2>&1; then
  echo "No /dev/video* — plug in Link and retry." >&2
  exit 1
fi
if ! lsusb 2>/dev/null | grep -q '2e1a:4c'; then
  echo "Insta360 USB device not found (lsusb 2e1a:4c*)" >&2
  exit 1
fi

mkdir -p "$CAP_DIR"

exec > >(tee -a "$LOG") 2>&1
echo "=== streamdeck smoke $(date -Is) ==="
echo "log: $LOG"
echo "captures: $CAP_DIR"

status() {
  local opt=$1
  "$PY" "$ROOT/link_ctl.py" status "$opt" 2>/dev/null || echo "?"
}

raw_xu() {
  "$PY" -c "
import sys; sys.path.insert(0, '$ROOT')
import link_ctl as lc
lc.reset_usb_caches()
raw = lc._ai_mode_get_raw()
bm = lc._bitmask_get()
print(f'AI={raw[0]:02x}/{raw[1]:02x} bm=0x{bm:04x} mirror={lc._bitmask_get_bit(3)} privacy_bit11={lc._bitmask_get_bit(11)}')
" 2>/dev/null || echo "RAW?"
}

capture_step() {
  local slug=$1
  CAP_N=$((CAP_N + 1))
  local num
  num=$(printf '%02d' "$CAP_N")
  local jpg="$CAP_DIR/${num}_${slug}.jpg"
  echo ""
  echo "=== capture ${num}_${slug} ==="
  set +e
  "$PY" "$CAPTURE_PY" "${num}_${slug}" "$jpg"
  local rc=$?
  set -e
  if [[ $rc -eq 2 ]]; then
    echo "VISUAL_MISMATCH: ${num}_${slug}"
    VISUAL_WARN=$((VISUAL_WARN + 1))
  fi
}

recover() {
  # Privacy off + normal is safe ioctl-only. Overhead/deskview tilt the gimbal
  # down (looks like privacy) while bit 11 stays off — center restores position
  # when LINK_CTL_SMOKE_CENTER=1 or LINK_CTL_USB_DETACH=1.
  "$PY" "$ROOT/link_ctl.py" privacy off >/dev/null 2>&1 || true
  "$PY" "$ROOT/link_ctl.py" mirror off >/dev/null 2>&1 || true
  "$PY" "$ROOT/link_ctl.py" normal >/dev/null 2>&1 || true
  sleep 1
  if [[ "${LINK_CTL_SMOKE_CENTER:-}" == "1" || "${LINK_CTL_USB_DETACH:-}" == "1" ]]; then
    local center_args=()
    [[ "${LINK_CTL_USB_DETACH:-}" == "1" ]] && center_args+=(--detach)
    "$PY" "$ROOT/link_ctl.py" "${center_args[@]}" center >/dev/null 2>&1 || true
    sleep 1
  fi
  sleep 2
}

opendeck() {
  local script=$1
  local slug=$2
  echo ""
  echo "--- bash -lc $SD/$script ---"
  bash -lc "'$SD/$script'"
  echo "exit=$?"
  echo "status: mode=$(status mode) mirror=$(status mirror) privacy=$(status privacy)"
  raw_xu
  capture_step "$slug"
}

recover
echo "baseline: mode=$(status mode) mirror=$(status mirror) privacy=$(status privacy)"
raw_xu
capture_step "baseline"

FAIL=0
for pair in "mirror_on.sh:mirror_on" "overhead_on.sh:overhead_on" "deskview_on.sh:deskview_on" "deskview_off.sh:deskview_off"; do
  script="${pair%%:*}"
  slug="${pair##*:}"
  if ! opendeck "$script" "$slug"; then
    echo "FAIL: $script exited non-zero"
    FAIL=$((FAIL + 1))
  fi
  recover
  capture_step "after_recover_${slug}"
done

echo ""
echo "--- reset.sh (force path) ---"
if bash -lc "'$SD/reset.sh'"; then
  echo "reset exit=0"
else
  echo "FAIL: reset.sh exited non-zero"
  FAIL=$((FAIL + 1))
fi
echo "after reset: mode=$(status mode) mirror=$(status mirror) privacy=$(status privacy)"
raw_xu
capture_step "after_reset"
ls -la /dev/video* 2>/dev/null || echo "WARN: no /dev/video* after reset"

recover
capture_step "final_baseline"

echo ""
echo "Capture directory: $CAP_DIR"
ls -la "$CAP_DIR" 2>/dev/null || true

if [[ $VISUAL_WARN -gt 0 ]]; then
  echo "VISUAL_WARN: $VISUAL_WARN step(s) — dark frame while privacy=off (see CAPTURE lines above)"
  echo "  Likely overhead/deskview gimbal angle without center; set LINK_CTL_SMOKE_CENTER=1 if detach allowed"
fi

if [[ $FAIL -eq 0 ]]; then
  if [[ $VISUAL_WARN -gt 0 ]]; then
    echo "PASS (with visual warnings): streamdeck smoke ($LOG)"
  else
    echo "PASS: streamdeck smoke ($LOG)"
  fi
else
  echo "FAIL: $FAIL script(s) failed ($LOG)"
  exit 1
fi
