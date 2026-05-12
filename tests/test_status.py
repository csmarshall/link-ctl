"""Tests for the `link-ctl status` command and per-option `<cmd> status` form.

Covers:
- STATUS_OPTIONS metadata sanity
- Parser accepts `status` in every documented form
- Backend readers (USB / WS / Linux) decode correctly with mocked I/O
- _emit_status exit codes per option kind
- Round-trip via the public CLI when a real camera is present (skipped in CI)
"""
from __future__ import annotations
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import link_ctl  # noqa: E402


# ──────────────────────────────────────────────────────────────────────────────
# 1. STATUS_OPTIONS table integrity
# ──────────────────────────────────────────────────────────────────────────────
class TestStatusOptionsTable(unittest.TestCase):

    def test_every_kind_is_valid(self):
        valid_kinds = {'bool', 'enum', 'scalar', 'ai-mode'}
        for opt, meta in link_ctl.STATUS_OPTIONS.items():
            self.assertIn(meta['kind'], valid_kinds, f"{opt} has bad kind")

    def test_every_backend_flag_is_bool(self):
        for opt, meta in link_ctl.STATUS_OPTIONS.items():
            for backend in ('usb', 'ws', 'linux'):
                self.assertIsInstance(meta[backend], bool,
                                      f"{opt}.{backend} must be bool")

    def test_at_least_one_backend_per_option(self):
        # An option in the table that no backend can read is dead weight.
        for opt, meta in link_ctl.STATUS_OPTIONS.items():
            self.assertTrue(meta['usb'] or meta['ws'] or meta['linux'],
                            f"{opt} is not readable on any backend")

    def test_ai_modes_present(self):
        for opt in ('track', 'deskview', 'whiteboard', 'overhead'):
            self.assertEqual(link_ctl.STATUS_OPTIONS[opt]['kind'], 'ai-mode')

    def test_known_bool_options(self):
        # If any of these stops being a bool the README + exit-code contract
        # silently breaks.
        for opt in ('hdr', 'mirror', 'awb', 'autoexposure', 'autofocus',
                    'gesture-zoom', 'smartcomposition'):
            self.assertEqual(link_ctl.STATUS_OPTIONS[opt]['kind'], 'bool',
                             f"{opt} kind changed unexpectedly")


# ──────────────────────────────────────────────────────────────────────────────
# 2. Parser accepts all status forms
# ──────────────────────────────────────────────────────────────────────────────
class TestParser(unittest.TestCase):

    def _parse(self, *argv):
        return link_ctl.build_parser().parse_args(list(argv))

    def test_per_option_status_on_togglables(self):
        for cmd in ('track', 'deskview', 'whiteboard', 'overhead',
                    'hdr', 'mirror', 'gesture-zoom',
                    'autoexposure', 'awb', 'smartcomposition'):
            with self.subTest(cmd=cmd):
                args = self._parse(cmd, 'status')
                self.assertEqual(args.command, cmd)
                self.assertEqual(args.state, 'status')

    def test_per_option_status_on_autofocus(self):
        args = self._parse('autofocus', 'status')
        self.assertEqual(args.state, 'status')

    def test_per_option_status_on_anti_flicker(self):
        args = self._parse('anti-flicker', 'status')
        self.assertEqual(args.mode, 'status')

    def test_per_option_status_on_smartcomp_frame(self):
        args = self._parse('smartcomp-frame', 'status')
        self.assertEqual(args.frame, 'status')

    def test_bare_track_still_toggles(self):
        # Charles' explicit requirement: bare `link-ctl track` preserves
        # no-arg toggle semantics. Parser should hand back state=None,
        # which the dispatcher treats as toggle.
        args = self._parse('track')
        self.assertIsNone(args.state)

    def test_autofocus_still_requires_arg(self):
        with self.assertRaises(SystemExit):
            self._parse('autofocus')

    def test_top_level_status_no_option(self):
        args = self._parse('status')
        self.assertEqual(args.command, 'status')
        self.assertIsNone(args.option)

    def test_top_level_status_with_option(self):
        args = self._parse('status', 'hdr')
        self.assertEqual(args.option, 'hdr')

    def test_top_level_status_flags(self):
        args = self._parse('status', 'hdr', '-q', '--json')
        self.assertTrue(args.status_quiet)
        self.assertTrue(args.status_json)

    def test_top_level_status_invalid_option(self):
        with self.assertRaises(SystemExit):
            self._parse('status', 'definitely-not-an-option')

    def test_top_level_status_choices_match_table(self):
        # The parser's choices list must be the set of STATUS_OPTIONS keys —
        # otherwise the per-option short-circuit in main() and the top-level
        # form diverge in what they accept.
        parser = link_ctl.build_parser()
        for action in parser._subparsers._actions:                # noqa: SLF001
            if hasattr(action, 'choices') and 'status' in (action.choices or {}):
                status_sub = action.choices['status']
                for sub_action in status_sub._actions:             # noqa: SLF001
                    if sub_action.dest == 'option':
                        self.assertEqual(
                            set(sub_action.choices),
                            set(link_ctl.STATUS_OPTIONS.keys()),
                        )
                        return
        self.fail("could not find status subparser's option choices")


# ──────────────────────────────────────────────────────────────────────────────
# 3. USB-direct reader with mocked _uvc_get / read_ai_mode / etc.
# ──────────────────────────────────────────────────────────────────────────────
class TestReadStatusUsb(unittest.TestCase):

    def test_ai_mode_active_is_on(self):
        with mock.patch.object(link_ctl, 'read_ai_mode', return_value='track'):
            r = link_ctl.read_status_usb('track')
        self.assertTrue(r['value'])
        self.assertTrue(r['is_on'])
        self.assertEqual(r['display'], 'on')

    def test_ai_mode_inactive_is_off(self):
        with mock.patch.object(link_ctl, 'read_ai_mode', return_value='normal'):
            r = link_ctl.read_status_usb('track')
        self.assertFalse(r['value'])
        self.assertFalse(r['is_on'])
        self.assertEqual(r['display'], 'off')

    def test_mode_returns_current_name(self):
        with mock.patch.object(link_ctl, 'read_ai_mode', return_value='deskview'):
            r = link_ctl.read_status_usb('mode')
        self.assertEqual(r['value'], 'deskview')
        self.assertEqual(r['display'], 'deskview')
        # mode is enum, not bool — is_on must be None so exit code is 0.
        self.assertIsNone(r['is_on'])

    def test_bitmask_bit_options(self):
        for opt, bit_const in (('hdr', link_ctl.BIT_HDR),
                               ('mirror', link_ctl.BIT_MIRROR),
                               ('gesture-zoom', link_ctl.BIT_GESTURE_ZOOM)):
            with self.subTest(opt=opt):
                with mock.patch.object(link_ctl, '_bitmask_get_bit',
                                       return_value=True) as m:
                    r = link_ctl.read_status_usb(opt)
                m.assert_called_once_with(bit_const)
                self.assertTrue(r['is_on'])
                self.assertEqual(r['display'], 'on')

    def test_autoexposure_decodes_byte(self):
        with mock.patch.object(link_ctl, '_uvc_get', return_value=bytes([2])):
            r = link_ctl.read_status_usb('autoexposure')
        self.assertTrue(r['is_on'])
        with mock.patch.object(link_ctl, '_uvc_get', return_value=bytes([1])):
            r = link_ctl.read_status_usb('autoexposure')
        self.assertFalse(r['is_on'])

    def test_awb_decodes_byte(self):
        with mock.patch.object(link_ctl, '_uvc_get', return_value=bytes([1])):
            r = link_ctl.read_status_usb('awb')
        self.assertTrue(r['is_on'])

    def test_anti_flicker_maps_known_values(self):
        for raw, expected in ((3, 'auto'), (1, '50hz'), (2, '60hz')):
            with self.subTest(raw=raw):
                with mock.patch.object(link_ctl, '_uvc_get',
                                       return_value=bytes([raw])):
                    r = link_ctl.read_status_usb('anti-flicker')
                self.assertEqual(r['value'], expected)
                self.assertEqual(r['display'], expected)

    def test_smartcomp_frame_maps_known_values(self):
        for raw, expected in ((1, 'head'), (2, 'halfbody'), (3, 'wholebody')):
            with self.subTest(raw=raw):
                with mock.patch.object(link_ctl, '_uvc_get',
                                       return_value=bytes([raw])):
                    r = link_ctl.read_status_usb('smartcomp-frame')
                self.assertEqual(r['display'], expected)

    def test_brightness_returns_int(self):
        with mock.patch.object(link_ctl, '_uvc_get', return_value=bytes([42])):
            r = link_ctl.read_status_usb('brightness')
        self.assertEqual(r['value'], 42)
        self.assertEqual(r['display'], '42')
        self.assertIsNone(r['is_on'])

    def test_zoom_uses_read_zoom_helper(self):
        with mock.patch.object(link_ctl, 'read_zoom', return_value=275):
            r = link_ctl.read_status_usb('zoom')
        self.assertEqual(r['value'], 275)

    def test_pan_tilt_uses_read_pantilt_helper(self):
        with mock.patch.object(link_ctl, 'read_pantilt',
                               return_value=(1234, -5678)):
            self.assertEqual(link_ctl.read_status_usb('pan')['value'], 1234)
            self.assertEqual(link_ctl.read_status_usb('tilt')['value'], -5678)

    def test_unknown_option_raises(self):
        with self.assertRaises(KeyError):
            link_ctl.read_status_usb('definitely-not-readable')

    def test_smartcomposition_not_usb_readable(self):
        # smartcomposition's XU bit is unconfirmed — the table says usb=False.
        # If that changes the test should be updated deliberately.
        self.assertFalse(link_ctl.STATUS_OPTIONS['smartcomposition']['usb'])
        with self.assertRaises(KeyError):
            link_ctl.read_status_usb('smartcomposition')


# ──────────────────────────────────────────────────────────────────────────────
# 4. WebSocket reader with sample DeviceInfoNotify payload
# ──────────────────────────────────────────────────────────────────────────────
class TestReadStatusWs(unittest.TestCase):
    SAMPLE_DEV = {
        'mode': link_ctl.VideoMode.TRACKING,
        'hdr': True,
        'mirror': False,
        'autoExposure': True,
        'autoWhiteBalance': False,
        'smartComposition': True,
        'brightness': 55,
        'contrast': 50,
        'saturation': 48,
        'sharpness': 50,
        'exposureComp': 50,
        'wbTemp': 5500,
        'zoom': {'curValue': 200},
    }

    def test_ai_mode_matches(self):
        r = link_ctl.read_status_ws('track', self.SAMPLE_DEV)
        self.assertTrue(r['is_on'])

    def test_ai_mode_mismatch(self):
        r = link_ctl.read_status_ws('deskview', self.SAMPLE_DEV)
        self.assertFalse(r['is_on'])

    def test_mode_returns_name(self):
        r = link_ctl.read_status_ws('mode', self.SAMPLE_DEV)
        self.assertEqual(r['value'], 'track')

    def test_bool_fields(self):
        self.assertTrue(link_ctl.read_status_ws('hdr', self.SAMPLE_DEV)['is_on'])
        self.assertFalse(link_ctl.read_status_ws('mirror', self.SAMPLE_DEV)['is_on'])
        self.assertTrue(link_ctl.read_status_ws('autoexposure', self.SAMPLE_DEV)['is_on'])
        self.assertTrue(link_ctl.read_status_ws('smartcomposition', self.SAMPLE_DEV)['is_on'])

    def test_scalars(self):
        self.assertEqual(link_ctl.read_status_ws('brightness', self.SAMPLE_DEV)['value'], 55)
        self.assertEqual(link_ctl.read_status_ws('zoom', self.SAMPLE_DEV)['value'], 200)
        self.assertEqual(link_ctl.read_status_ws('wb-temp', self.SAMPLE_DEV)['value'], 5500)

    def test_options_not_in_ws(self):
        # gesture-zoom, autofocus, anti-flicker, smartcomp-frame are set-only
        # on the WS protocol — DeviceInfoNotify does not surface them.
        for opt in ('gesture-zoom', 'autofocus', 'anti-flicker', 'smartcomp-frame'):
            with self.subTest(opt=opt):
                self.assertFalse(link_ctl.STATUS_OPTIONS[opt]['ws'])
                with self.assertRaises(KeyError):
                    link_ctl.read_status_ws(opt, self.SAMPLE_DEV)


# ──────────────────────────────────────────────────────────────────────────────
# 5. _emit_status exit codes
# ──────────────────────────────────────────────────────────────────────────────
class TestEmitStatus(unittest.TestCase):

    def _emit(self, result, **kw):
        kw.setdefault('quiet', True)        # suppress stdout in test runs
        kw.setdefault('json_out', False)
        return link_ctl._emit_status(result, **kw)

    def test_bool_on_exits_0(self):
        r = {'option': 'hdr', 'value': True, 'display': 'on', 'is_on': True}
        self.assertEqual(self._emit(r), 0)

    def test_bool_off_exits_1(self):
        r = {'option': 'hdr', 'value': False, 'display': 'off', 'is_on': False}
        self.assertEqual(self._emit(r), 1)

    def test_scalar_always_exits_0(self):
        r = {'option': 'brightness', 'value': 42, 'display': '42', 'is_on': None}
        self.assertEqual(self._emit(r), 0)

    def test_enum_always_exits_0(self):
        # Even off-ish enum values (e.g. anti-flicker='auto') exit 0 — there
        # is no "is active" semantic for enums.
        r = {'option': 'anti-flicker', 'value': 'auto', 'display': 'auto', 'is_on': None}
        self.assertEqual(self._emit(r), 0)


# ──────────────────────────────────────────────────────────────────────────────
# 6. End-to-end CLI smoke tests (skipped if no camera / not on macOS USB-direct)
# ──────────────────────────────────────────────────────────────────────────────
def _camera_available() -> bool:
    return link_ctl._uvc_probe_available() and link_ctl._camera_usb_present()


@unittest.skipUnless(_camera_available(),
                     "needs USB-direct camera on macOS")
class TestCliEndToEnd(unittest.TestCase):
    """Shell out to the CLI and verify the contract end-to-end. Skipped in CI
    (no hardware) but invaluable when hacking on the status code locally."""

    def _run(self, *argv) -> tuple[int, str]:
        r = subprocess.run([sys.executable, str(REPO / 'link_ctl.py'), *argv],
                           capture_output=True, text=True)
        return r.returncode, r.stdout.strip()

    def test_top_level_status_zoom(self):
        rc, out = self._run('status', 'zoom')
        self.assertEqual(rc, 0)
        self.assertTrue(out.isdigit())

    def test_per_option_status_matches_top_level(self):
        rc1, out1 = self._run('hdr', 'status')
        rc2, out2 = self._run('status', 'hdr')
        self.assertEqual(rc1, rc2)
        self.assertEqual(out1, out2)
        self.assertIn(out1, ('on', 'off'))

    def test_quiet_flag_produces_no_stdout(self):
        rc, out = self._run('status', 'hdr', '-q')
        self.assertEqual(out, '')
        self.assertIn(rc, (0, 1))

    def test_json_flag_is_parseable(self):
        rc, out = self._run('status', 'hdr', '--json')
        self.assertEqual(rc if rc != 1 else 0, 0)  # rc may be 0 or 1
        payload = json.loads(out)
        self.assertEqual(payload['option'], 'hdr')
        self.assertIn(payload['value'], (True, False))

    def test_full_dump_json(self):
        rc, out = self._run('status', '--json')
        self.assertEqual(rc, 0)
        payload = json.loads(out)
        # Every option we expect to be USB-readable should be present.
        for opt, meta in link_ctl.STATUS_OPTIONS.items():
            if meta['usb']:
                self.assertIn(opt, payload, f"{opt} missing from dump")


if __name__ == '__main__':
    unittest.main()
