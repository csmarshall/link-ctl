#!/usr/bin/env bash
# Smoke test for every status option + form against a live camera.
# Mostly read-only; the round-trip block at the bottom flips hdr and
# brightness then restores. Run from the repo root.
unset TMOUT
set -uo pipefail

# Default uses the in-tree link_ctl.py; override with LINK_CTL=link-ctl to
# test the installed entrypoint.
LINK_CTL=${LINK_CTL:-"python3 link_ctl.py"}
LOG="status_smoke_$(date +"%F-%H%M.%S").log"
exec > >(tee "$LOG") 2>&1

pass=0
fail=0

run() {
  local label=$1; shift
  local out rc
  out=$($LINK_CTL "$@" 2>&1); rc=$?
  printf "[%-3s rc=%d] %-40s → %s\n" "OK"  "$rc" "$label" "$out"
  if [[ $rc -le 1 ]]; then ((pass++)); else ((fail++)); fi
}

echo "── Per-option status form (link-ctl <opt> status) ──"
for opt in track deskview whiteboard overhead \
           hdr mirror gesture-zoom autoexposure awb autofocus noise-cancel; do
  run "$opt status" "$opt" status
done
run "anti-flicker status" anti-flicker status
run "smartcomp-frame status" smartcomp-frame status

echo
echo "── Top-level status form (link-ctl status <opt>) ──"
for opt in track deskview whiteboard overhead mode \
           hdr mirror gesture-zoom autoexposure awb autofocus noise-cancel \
           anti-flicker smartcomp-frame \
           brightness contrast saturation sharpness exposurecomp wb-temp \
           track-speed iso shutter \
           zoom pan tilt; do
  run "status $opt" status "$opt"
done

echo
echo "── Flags (-q, --json) ──"
run "status hdr -q (exit code only)" status hdr -q
run "status hdr --json"               status hdr --json
run "status brightness --json"        status brightness --json
run "status -q (full dump quiet)"     status -q
echo "── Full dump (no option, default table) ──"
$LINK_CTL status
echo
echo "── Full dump JSON head ──"
$LINK_CTL status --json | head -8

echo
echo "── Negative: argparse rejects bare autofocus / smartcomp-frame ──"
$LINK_CTL autofocus      ; echo "autofocus       exit=$?"
$LINK_CTL anti-flicker   ; echo "anti-flicker    exit=$?"
$LINK_CTL smartcomp-frame; echo "smartcomp-frame exit=$?"

echo
echo "── Negative: invalid option to top-level status ──"
$LINK_CTL status nope    ; echo "status nope     exit=$?"

echo
echo "── Round-trip: set then read back ──"
$LINK_CTL hdr off >/dev/null; echo -n "after hdr off → "
$LINK_CTL status hdr; echo "exit=$?"
$LINK_CTL hdr on  >/dev/null; echo -n "after hdr on  → "
$LINK_CTL status hdr; echo "exit=$?"
$LINK_CTL brightness 42 >/dev/null; echo -n "after brightness 42 → "
$LINK_CTL status brightness
$LINK_CTL brightness 50 >/dev/null
echo
echo "summary: $pass commands returned 0/1, $fail returned other"
echo "log: $LOG"
