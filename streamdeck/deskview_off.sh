#!/usr/bin/env bash
source "$(dirname "$0")/_common.sh"
run_link_ctl deskview off
rc=$?
# DeskView tilts the gimbal down; ensure normal mode and center pan/tilt/zoom when exiting.
run_link_ctl normal
sleep 1
run_link_ctl center
exit $rc
