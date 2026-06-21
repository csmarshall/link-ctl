#!/usr/bin/env bash
# Shared helpers for Stream Deck / hotkey scripts.
unset TMOUT
STREAMDECK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LINK_CTL_ROOT="$(cd "$STREAMDECK_DIR/.." && pwd)"
LINK_CTL_PY="${LINK_CTL_PY:-$LINK_CTL_ROOT/link_ctl.py}"
LINK_CTL_QUIET="${LINK_CTL_QUIET:-1}"

run_link_ctl() {
  local args=()
  if [[ "$LINK_CTL_QUIET" == "1" ]]; then
    args+=(--quiet)
  fi
  args+=("$@")
  python3 "$LINK_CTL_PY" "${args[@]}"
}
