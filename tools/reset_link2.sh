#!/usr/bin/env bash
# Recover a hung Insta360 Link 2 after libusb detach tests (no physical unplug).
# Discovers USB bus path from sysfs (2e1a:4c01/4c02/4c03/4c04) — never hardcoded.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VID="2e1a"
PIDS="4c01 4c02 4c03 4c04"

need_sudo=0
if [[ ! -w /sys/bus/usb/drivers/uvcvideo/bind ]]; then
  need_sudo=1
fi

run() {
  if [[ "$need_sudo" -eq 1 ]]; then
    sudo "$@"
  else
    "$@"
  fi
}

# Prefer link_ctl reset (handle reopen → libusb reset → sysfs rebind).
if [[ "$need_sudo" -eq 0 ]]; then
  exec python3 "$ROOT/link_ctl.py" reset --verbose "$@"
fi

echo "sysfs bind requires root; running recovery with sudo..." >&2
exec sudo python3 "$ROOT/link_ctl.py" reset --verbose "$@"
