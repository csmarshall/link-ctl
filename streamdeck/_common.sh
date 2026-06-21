#!/usr/bin/env bash
# Shared helpers for Stream Deck / hotkey scripts.
unset TMOUT
STREAMDECK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LINK_CTL_ROOT="$(cd "$STREAMDECK_DIR/.." && pwd)"
LINK_CTL_PY="${LINK_CTL_PY:-$LINK_CTL_ROOT/link_ctl.py}"
# Prefer system Python — avoid AppImage/binfmt shims that OpenDeck/Cursor may inject.
if [[ -z "${LINK_CTL_PYTHON:-}" ]]; then
  if [[ -x /usr/bin/python3 ]]; then
    LINK_CTL_PYTHON=/usr/bin/python3
  else
    LINK_CTL_PYTHON="$(command -v python3 || true)"
  fi
fi
LINK_CTL_QUIET="${LINK_CTL_QUIET:-0}"

run_link_ctl() {
  local args=()
  if [[ "$LINK_CTL_QUIET" == "1" ]]; then
    args+=(--quiet)
  fi
  args+=("$@")
  if [[ -z "$LINK_CTL_PYTHON" ]]; then
    echo "link-ctl: python3 not found in PATH" >&2
    return 127
  fi
  "$LINK_CTL_PYTHON" "$LINK_CTL_PY" "${args[@]}"
}
