#!/usr/bin/env bash
# Recover a hung Insta360 Link 2 after libusb detach tests (no physical unplug).
# Discovers USB bus path from sysfs (2e1a:4c01/4c02/4c03/4c04) — never hardcoded.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
if [[ -x /usr/bin/python3 ]]; then
  PY=/usr/bin/python3
else
  PY="$(command -v python3 || true)"
fi
if [[ -z "$PY" ]]; then
  echo "reset_link2: python3 not found" >&2
  exit 127
fi

RESET_ARGS=(reset --verbose --force --skip-usb-reset "$@")

# Always try user-level recovery first (handle reopen + libusb reset; no sysfs).
if "$PY" "$ROOT/link_ctl.py" "${RESET_ARGS[@]}"; then
  exit 0
fi

# sysfs bind requires root — use non-interactive sudo when available.
if [[ ! -w /sys/bus/usb/drivers/uvcvideo/bind ]]; then
  if sudo -n "$PY" "$ROOT/link_ctl.py" "${RESET_ARGS[@]}" 2>/dev/null; then
    exit 0
  fi
  echo "reset: USB recovery failed; sysfs bind needs passwordless sudo." >&2
  echo "  Run: sudo $0" >&2
  exit 1
fi

exec "$PY" "$ROOT/link_ctl.py" "${RESET_ARGS[@]}"
