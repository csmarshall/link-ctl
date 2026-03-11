#!/usr/bin/env python3
"""Bump the patch version in pyproject.toml and stage the change.

Called automatically by .githooks/pre-commit.
Run manually: python3 scripts/bump_version.py
"""

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
PYPROJECT = ROOT / "pyproject.toml"

content = PYPROJECT.read_text()
m = re.search(r'^version = "(\d+)\.(\d+)\.(\d+)"', content, re.MULTILINE)
if not m:
    print("bump_version: could not find version in pyproject.toml", file=sys.stderr)
    sys.exit(1)

major, minor, patch = int(m.group(1)), int(m.group(2)), int(m.group(3))
old_ver = f"{major}.{minor}.{patch}"
new_ver = f"{major}.{minor}.{patch + 1}"

PYPROJECT.write_text(content.replace(f'version = "{old_ver}"', f'version = "{new_ver}"', 1))
subprocess.run(["git", "add", str(PYPROJECT)], check=True)
print(f"version: {old_ver} → {new_ver}", flush=True)
