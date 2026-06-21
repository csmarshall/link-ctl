#!/usr/bin/env bash
# Smoke-test the exact paths OpenDeck runs (bash -lc streamdeck/*.sh).
# One pass, then restore normal — not a destructive loop.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PY="${LINK_CTL_PYTHON:-/usr/bin/python3}"
if [[ ! -x "$PY" ]]; then PY="$(command -v python3)"; fi
SD="$ROOT/streamdeck"
LOG="${1:-streamdeck_smoke_$(date +%F-%H%M%S).log}"

if ! ls /dev/video* >/dev/null 2>&1; then
  echo "No /dev/video* — plug in Link and retry." >&2
  exit 1
fi
if ! lsusb 2>/dev/null | grep -q '2e1a:4c'; then
  echo "Insta360 USB device not found (lsusb 2e1a:4c*)" >&2
  exit 1
fi

exec > >(tee -a "$LOG") 2>&1
echo "=== streamdeck smoke $(date -Is) ==="
echo "log: $LOG"

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
print(f'AI={raw[0]:02x}/{raw[1]:02x} bm=0x{bm:04x} mirror={lc._bitmask_get_bit(3)}')
" 2>/dev/null || echo "RAW?"
}

opendeck() {
  local script=$1
  echo ""
  echo "--- bash -lc $SD/$script ---"
  bash -lc "'$SD/$script'"
  echo "exit=$?"
  echo "status: mode=$(status mode) mirror=$(status mirror) privacy=$(status privacy)"
  raw_xu
}

recover() {
  "$PY" "$ROOT/link_ctl.py" normal >/dev/null 2>&1 || true
  sleep 2
}

recover
echo "baseline: mode=$(status mode) mirror=$(status mirror)"
raw_xu

FAIL=0
for script in mirror_on.sh overhead_on.sh deskview_on.sh deskview_off.sh; do
  if ! opendeck "$script"; then
    echo "FAIL: $script exited non-zero"
    FAIL=$((FAIL + 1))
  fi
  recover
done

echo ""
echo "--- reset.sh (force path) ---"
if bash -lc "'$SD/reset.sh'"; then
  echo "reset exit=0"
else
  echo "FAIL: reset.sh exited non-zero"
  FAIL=$((FAIL + 1))
fi
echo "after reset: mode=$(status mode) mirror=$(status mirror)"
raw_xu
ls -la /dev/video* 2>/dev/null || echo "WARN: no /dev/video* after reset"

echo ""
if [[ $FAIL -eq 0 ]]; then
  echo "PASS: streamdeck smoke ($LOG)"
else
  echo "FAIL: $FAIL script(s) failed ($LOG)"
  exit 1
fi
