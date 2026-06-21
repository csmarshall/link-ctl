#!/usr/bin/env bash
# Stream Deck / hotkey: recover hung Insta360 Link 2 without unplugging.
source "$(dirname "$0")/_common.sh"
"$LINK_CTL_ROOT/tools/reset_link2.sh" || exit $?
# Clear stuck AI mode after USB/handle recovery (libusb reset may leave track/0xFF).
sleep 2
run_link_ctl normal || exit $?
exit 0
