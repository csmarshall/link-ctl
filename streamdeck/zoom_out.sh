#!/usr/bin/env bash
# Decrease zoom by 50 (incremental, clamped to 100)
unset TMOUT
DIR="$(cd "$(dirname "$0")/.." && pwd)"
python3 "$DIR/link_ctl.py" zoom-rel -- -50
exit $?
