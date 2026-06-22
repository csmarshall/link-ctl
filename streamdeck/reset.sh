#!/usr/bin/env bash
# Stream Deck / hotkey: recover hung Insta360 Link 2 without unplugging.
source "$(dirname "$0")/_common.sh"
"$LINK_CTL_ROOT/tools/reset_link2.sh" || exit $?
# Clear stuck AI mode after USB/handle recovery (libusb reset may leave track/0xFF).
sleep 2
if ! run_link_ctl normal; then
  "$LINK_CTL_PYTHON" -c "
import sys
sys.path.insert(0, '$LINK_CTL_ROOT')
import link_ctl as lc
import link_usb_linux as ul
if lc._link2() and lc._link2_track_stuck():
    ul.recover_ai_mode_stuck(verbose=True)
    lc.reset_usb_caches()
" 2>/dev/null || true
  run_link_ctl normal || exit $?
fi
exit 0
