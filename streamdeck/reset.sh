#!/usr/bin/env bash
# Stream Deck / hotkey: recover hung Insta360 Link 2 without unplugging.
source "$(dirname "$0")/_common.sh"
exec "$LINK_CTL_ROOT/tools/reset_link2.sh"
