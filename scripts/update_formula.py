#!/usr/bin/env python3
"""Update Homebrew formula with sdist URL and SHA256 from PyPI JSON API.

Usage: python3 scripts/update_formula.py <version> <formula_path>
"""

import json
import re
import sys
import time
import urllib.request


def fetch_pypi(package: str, version: str, retries: int = 12, delay: int = 10) -> dict:
    url = f"https://pypi.org/pypi/{package}/{version}/json"
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(url) as r:
                return json.load(r)
        except Exception as e:
            if attempt < retries - 1:
                print(f"  Waiting for PyPI to index v{version}... ({attempt + 1}/{retries}): {e}", flush=True)
                time.sleep(delay)
            else:
                raise RuntimeError(f"PyPI never indexed v{version}: {e}") from e


def update_formula(formula_path: str, sdist_url: str, sdist_sha256: str) -> None:
    with open(formula_path) as f:
        content = f.read()

    # Match top-level url/sha256 (2-space indent) — not the resource block (4-space indent)
    new = re.sub(
        r'^  url "https://files\.pythonhosted\.org/[^"]+"',
        f'  url "{sdist_url}"',
        content, count=1, flags=re.MULTILINE,
    )
    new = re.sub(
        r'^  sha256 "[0-9a-f]+"',
        f'  sha256 "{sdist_sha256}"',
        new, count=1, flags=re.MULTILINE,
    )

    if new == content:
        print("  Warning: formula unchanged — regex may not have matched", file=sys.stderr)
    else:
        with open(formula_path, "w") as f:
            f.write(new)


def main() -> None:
    if len(sys.argv) != 3:
        print(f"Usage: {sys.argv[0]} <version> <formula_path>", file=sys.stderr)
        sys.exit(1)

    version, formula_path = sys.argv[1], sys.argv[2]
    package = "link-ctl"

    print(f"Fetching PyPI metadata for {package} v{version}...")
    data = fetch_pypi(package, version)
    sdist = next((u for u in data["urls"] if u["packagetype"] == "sdist"), None)
    if not sdist:
        print("Error: no sdist found in PyPI release", file=sys.stderr)
        sys.exit(1)

    url    = sdist["url"]
    sha256 = sdist["digests"]["sha256"]
    print(f"  url:    {url}")
    print(f"  sha256: {sha256}")

    update_formula(formula_path, url, sha256)
    print(f"Updated {formula_path}")


if __name__ == "__main__":
    main()
