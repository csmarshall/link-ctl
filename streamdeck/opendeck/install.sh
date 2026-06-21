#!/usr/bin/env bash
# Install / refresh the OpenDeck "Insta360 Link 2" profile (Run Command → link-ctl scripts).
set -euo pipefail
REPO="$(cd "$(dirname "$0")/../.." && pwd)"
exec python3 "$REPO/tools/build_opendeck_profile.py" --repo "$REPO" --install
