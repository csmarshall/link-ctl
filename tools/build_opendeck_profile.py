#!/usr/bin/env python3
"""Generate an OpenDeck profile JSON for Insta360 Link 2 (Starter Pack / Run Command).

OpenDeck stores profiles as JSON under ~/.config/opendeck/profiles/<device-id>/.
Each key uses the Starter Pack ``com.amansprojects.starterpack.runcommand`` action.

OpenDeck only rescans the profile list on startup — use ``--install`` to copy the
profile, select it, and restart OpenDeck.

Usage:
    python3 tools/build_opendeck_profile.py
    python3 tools/build_opendeck_profile.py --install
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

PROFILE_NAME = 'Link2'
LEGACY_NAMES = ('Insta360 Link 2',)

RUN_COMMAND_ICON = 'plugins/com.amansprojects.starterpack.sdPlugin/icons/runCommand.png'

# OpenDeck treats empty image as missing and falls back to /alert.png (yellow !).
# A 1×1 transparent PNG loads cleanly and stays invisible on coloured buttons.
TRANSPARENT_IMAGE = (
    'data:image/png;base64,'
    'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVQIW2NgYGD4DwABBAEAwS2OUAAAAABJRU5ErkJggg=='
)

RUN_COMMAND = {
    'controllers': ['Keypad', 'Encoder'],
    'disable_automatic_states': False,
    'icon': RUN_COMMAND_ICON,
    'name': 'Run Command',
    'plugin': 'com.amansprojects.starterpack.sdPlugin',
    'property_inspector': 'plugins/com.amansprojects.starterpack.sdPlugin/propertyInspector/runCommand.html',
    'supported_in_multi_actions': True,
    'tooltip': 'Run a command',
    'uuid': 'com.amansprojects.starterpack.runcommand',
    'visible_in_action_list': True,
}

# 5×3 MK.2 layout — key index 0..14 (see ~/.config/opendeck/EXTENSIONS.md)
BUTTONS: list[tuple[int, str, str, str]] = [
    (0,  'Track',    '#2563eb', 'track_on.sh'),
    (1,  'Track\nOff', '#1e40af', 'track_off.sh'),
    (2,  'Desk',     '#7c3aed', 'deskview_on.sh'),
    (3,  'Desk\nOff', '#5b21b6', 'deskview_off.sh'),
    (4,  'Center',   '#0d9488', 'center.sh'),
    (5,  'Overhead', '#0369a1', 'overhead_on.sh'),
    (6,  'Over\nOff', '#075985', 'overhead_off.sh'),
    (7,  'Board',    '#c026d3', 'whiteboard_on.sh'),
    (8,  'Board\nOff', '#a21caf', 'whiteboard_off.sh'),
    (9,  'Mirror',   '#9333ea', 'mirror_on.sh'),
    (10, 'Zoom +',   '#15803d', 'zoom_in.sh'),
    (11, 'Zoom −',   '#166534', 'zoom_out.sh'),
    (12, 'Normal',   '#475569', 'normal.sh'),
    (13, 'Privacy',  '#b91c1c', 'privacy_on.sh'),
    (14, 'Priv\nOff', '#991b1b', 'privacy_off.sh'),
]


def opendeck_config_dir() -> Path:
    return Path.home() / '.config' / 'opendeck'


def _state(label: str, bg: str) -> dict:
    return {
        'alignment': 'middle',
        'background_colour': bg,
        'colour': '#FFFFFF',
        'family': 'Liberation Sans',
        'image': TRANSPARENT_IMAGE,
        'image_scale': 10,
        'name': '',
        'show': True,
        'size': 13,
        'stroke_colour': '#000000',
        'stroke_size': 2,
        'style': 'Regular',
        'text': label,
        'underline': False,
    }


def _make_key(index: int, label: str, bg: str, script: str, repo: Path) -> dict:
    cmd = f"bash -lc '{repo / 'streamdeck' / script}'"
    state = _state(label, bg)
    action = dict(RUN_COMMAND)
    action['states'] = [dict(state)]
    return {
        'action': action,
        'children': None,
        'context': f'Keypad.{index}.0',
        'current_state': 0,
        'settings': {
            'down': cmd,
            'file': '',
            'rotate': '',
            # False = static state.text labels; True = overlay shell command stdout.
            'show': False,
            'up': '',
        },
        'states': [dict(state)],
    }


def build_profile(repo: Path) -> dict:
    repo = repo.resolve()
    keys = [_make_key(idx, label, bg, script, repo) for idx, label, bg, script in BUTTONS]
    return {'keys': keys, 'sliders': []}


def device_ids(config_dir: Path) -> list[str]:
    profiles_root = config_dir / 'profiles'
    ids: list[str] = []
    for path in sorted(profiles_root.glob('sd-*')):
        if path.is_dir():
            ids.append(path.name)
    return ids


def install_profile(profile_path: Path, config_dir: Path) -> list[Path]:
    installed: list[Path] = []
    profiles_root = config_dir / 'profiles'
    if not profiles_root.is_dir():
        raise SystemExit(f'OpenDeck profiles dir not found: {profiles_root}')
    for device_dir in sorted(profiles_root.glob('sd-*')):
        if not device_dir.is_dir():
            continue
        for legacy in LEGACY_NAMES:
            legacy_path = device_dir / f'{legacy}.json'
            if legacy_path.exists():
                legacy_path.unlink()
        dest = device_dir / profile_path.name
        shutil.copy2(profile_path, dest)
        installed.append(dest)
        set_selected_profile(config_dir, device_dir.name, PROFILE_NAME)
    return installed


def set_selected_profile(config_dir: Path, device_id: str, profile_name: str) -> None:
    cfg_path = config_dir / 'profiles' / f'{device_id}.json'
    cfg: dict = {'selected_profile': profile_name}
    if cfg_path.exists():
        try:
            cfg = json.loads(cfg_path.read_text())
        except json.JSONDecodeError:
            pass
    cfg['selected_profile'] = profile_name
    cfg_path.write_text(json.dumps(cfg, indent=2) + '\n')
    print(f'Selected profile "{profile_name}" for {device_id}')


def restart_opendeck() -> None:
    subprocess.run(['killall', 'opendeck'], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(1.5)
    subprocess.Popen(
        ['opendeck', '--hide'],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    print('Restarted OpenDeck (tray icon). Open the window to confirm the Link2 profile.')


def main() -> int:
    ap = argparse.ArgumentParser(description='Build OpenDeck Link 2 profile JSON')
    ap.add_argument('--repo', type=Path, default=Path(__file__).resolve().parent.parent)
    ap.add_argument('--output', type=Path, default=None,
                    help=f'Output path (default: streamdeck/opendeck/{PROFILE_NAME}.json)')
    ap.add_argument('--install', action='store_true',
                    help='Install profile, select it, and restart OpenDeck')
    ap.add_argument('--no-restart', action='store_true',
                    help='With --install, skip restarting OpenDeck')
    args = ap.parse_args()

    repo = args.repo.resolve()
    output = args.output or (repo / 'streamdeck' / 'opendeck' / f'{PROFILE_NAME}.json')
    config_dir = opendeck_config_dir()

    profile = build_profile(repo)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(profile, indent=2) + '\n')
    print(f'Wrote {output}')

    if not args.install:
        return 0

    paths = install_profile(output, config_dir)
    if not paths:
        print('No sd-* device folders found — plug in Stream Deck, open OpenDeck once, then re-run --install',
              file=sys.stderr)
        return 1
    for path in paths:
        print(f'Installed → {path}')

    if not args.no_restart:
        restart_opendeck()
    else:
        print('\nRestart OpenDeck (tray → Restart) so the Link2 profile appears in the dropdown.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
