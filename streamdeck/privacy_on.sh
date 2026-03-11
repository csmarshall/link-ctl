#!/usr/bin/env bash
unset TMOUT
DIR="$(cd "$(dirname "$0")/.." && pwd)"
python3 "$DIR/link_ctl.py" privacy on
exit $?
