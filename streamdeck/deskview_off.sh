#!/usr/bin/env bash
source "$(dirname "$0")/_common.sh"
run_link_ctl deskview off
rc=$?
# DeskView tilts the gimbal down; ensure normal mode and center pan/tilt/zoom when exiting.
run_link_ctl normal
sleep 1
center_args=()
[[ "${LINK_CTL_USB_DETACH:-}" == "1" ]] && center_args+=(--detach)
run_link_ctl "${center_args[@]}" center
exit $rc
