#!/usr/bin/env bash
# Increase zoom by 50 (incremental, clamped to 400)
unset TMOUT
DIR="$(cd "$(dirname "$0")/.." && pwd)"
python3 "$DIR/link_ctl.py" zoom-rel 50
exit $?
