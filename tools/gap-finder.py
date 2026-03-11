#!/usr/bin/env python3
"""gap-finder.py — Targeted discovery script for unknown Insta360 Link paramTypes.

Drives the mobile web UI with Playwright while tshark captures WebSocket packets.
Timestamps in the log let you correlate UI actions to wire bytes.

Architecture
────────────
Phase 1 — Direct WS tests (no browser):
  Opens a WS connection, sends paramTypes directly, checks status diffs.
  Confirms paramTypes 25, 39, 2 and their value ranges.

Phase 2 — Browser sessions (no WS calls while browser is open):
  Each session: set prerequisite state via WS → open browser → probe via
  clicks + screenshots only → close browser → restore state via WS.
  The controller only allows one control connection; the browser holds it.
  We NEVER call get_status() or ws_set() while the browser window is open.

Phase 3 — Final status check via WS.

Prerequisites
─────────────
1. Camera + Link Controller running (v2.2.1+)
2. pip install playwright && playwright install chromium
3. pip install websockets
4. link_ctl.py in the same directory

QR code URL
───────────
Open Link Controller → Remote Control → scan QR code, or decode the PNG:
    zbarimg --raw -q qr_code_screenshot.png

Session setup
─────────────
  Terminal 1 — capture:
    sudo tshark -i lo0 -Y "websocket" -T fields \\
        -e frame.time_relative -e data.data \\
        -E header=y > ~/gap-capture-$(date +%Y%m%d-%H%M%S).txt

  Terminal 2 — this script:
    python3 gap-finder.py "http://link-controller.insta360.com/v3/link/?port=PORT&token=TOKEN"

Decoding a hex payload from the capture:
    python3 -c "
    import sys, binascii; sys.path.insert(0,'.')
    from link_ctl import _decode_fields
    raw = binascii.unhexlify('PASTE_HEX')
    outer = _decode_fields(raw)
    if 16 in outer: print('ValueChange:', _decode_fields(outer[16][0]))
    elif 15 in outer: print('PresetUpdate:', _decode_fields(outer[15][0]))
    "
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path
from urllib.parse import urlparse, parse_qs

sys.path.insert(0, str(Path(__file__).parent))
from link_ctl import LinkClient, _read_token_from_ini, _lsof_port, build_value_change


# ── Logging ───────────────────────────────────────────────────────────────────

def ts() -> str:
    t = time.time()
    return time.strftime('%H:%M:%S') + f'.{int((t % 1) * 1000):03d}'

def log(msg: str):
    print(f'[{ts()}] {msg}', flush=True)

def sep(label: str):
    print(f'\n[{ts()}] ── {label} ──', flush=True)


# ── Status readback (WS only — never call while browser is open) ──────────────

WATCHED = ['hdr', 'mirror', 'smartComposition', 'autoExposure', 'autoWhiteBalance',
           'brightness', 'contrast', 'saturation', 'sharpness', 'exposureComp',
           'wbTemp', 'mode']

async def get_status(port: int, token: str) -> dict | None:
    """Connect via WS and read device state. ONLY call when browser is closed."""
    client = LinkClient(port, token=token)
    try:
        await client.connect()
        ok, _ = await client.handshake()
        await client.close()
        if ok and client.device_info and client.device_info.get('devices'):
            return client.device_info['devices'][0]
    except Exception as e:
        log(f'  status error: {e}')
    return None

async def ws_set(port: int, token: str, param_type: int, value: str):
    """Send a paramType value via WS. ONLY call when browser is closed."""
    c = LinkClient(port, token=token)
    await c.connect()
    _, _ = await c.handshake()
    await c.send_command(build_value_change(c.serial, param_type, value), wait_ms=300)
    await c.close()

def show(label: str, s: dict | None):
    if s is None:
        log(f'  {label}: <unavailable>')
        return
    log(f'  {label}: {json.dumps({k: s[k] for k in WATCHED if k in s})}')

def diff(a: dict | None, b: dict | None) -> dict:
    if not a or not b:
        return {}
    return {k: {'was': a.get(k), 'now': b.get(k)}
            for k in set(a) | set(b) if a.get(k) != b.get(k)}


# ── Phase 1: Direct WS paramType probes ──────────────────────────────────────

async def probe_ws_direct(port: int, token: str,
                           param_type: int, test_value: str,
                           restore_value: str | None, label: str) -> dict:
    """Send paramType directly over WS and check status diff. No browser."""
    sep(f'DIRECT WS: {label} (paramType={param_type}, value={test_value!r})')
    before = await get_status(port, token)
    show('before', before)

    log(f'  SEND paramType={param_type} value={test_value!r}')
    await ws_set(port, token, param_type, test_value)
    await asyncio.sleep(1.2)
    after = await get_status(port, token)
    show('after', after)
    d = diff(before, after)
    if d:
        log(f'  DIFF: {json.dumps(d)}')
    else:
        log(f'  no change — paramType={param_type} may not match this feature, or value unchanged')

    if restore_value is not None:
        log(f'  RESTORE paramType={param_type} value={restore_value!r}')
        await ws_set(port, token, param_type, restore_value)
        await asyncio.sleep(0.8)

    return d or {}


# ── Visual state check ────────────────────────────────────────────────────────

async def element_visual_state(page, loc) -> dict:
    """Return computed visual/interactive state — detects grayed-out controls."""
    try:
        box = await loc.bounding_box()
        if not box:
            return {'visible': False, 'reason': 'no bounding box'}
        state = await loc.evaluate("""
            el => {
                const cs = window.getComputedStyle(el);
                const opacity = parseFloat(cs.opacity || '1');
                const pe = cs.pointerEvents;
                const cls = (el.className || '').toString();
                return {
                    opacity: opacity,
                    pointerEvents: pe,
                    cls: cls.substring(0, 120),
                    disabled: el.disabled || el.getAttribute('aria-disabled') === 'true'
                              || /disabled|inactive/i.test(cls)
                              || opacity < 0.5 || pe === 'none',
                    text: el.textContent.trim().substring(0, 40),
                };
            }
        """)
        state['visible'] = True
        state['box'] = box
        return state
    except Exception as e:
        return {'visible': False, 'reason': str(e)}


async def assert_enabled(page, loc, label: str) -> bool:
    """Log visual state and return True if element appears interactive."""
    vs = await element_visual_state(page, loc)
    if not vs.get('visible'):
        log(f'  [{label}] not visible ({vs.get("reason", "?")})')
        return False
    if vs.get('disabled'):
        log(f'  [{label}] GRAYED OUT — opacity={vs.get("opacity")}, '
            f'pointer-events={vs.get("pointerEvents")}, cls: {vs.get("cls","")[:60]}')
        return False
    log(f'  [{label}] interactive (opacity={vs.get("opacity")}, '
        f'pointer-events={vs.get("pointerEvents")})')
    return True


# ── Touch-event canvas drag (Konva sliders) ───────────────────────────────────

_TOUCH_DRAG_JS = """
(args) => {
    const {x0, y0, x1, steps} = args;
    const el = document.elementFromPoint(x0, y0);
    if (!el) return 'no element at point';
    function mkTouch(x, y) {
        return new Touch({identifier: 1, target: el,
            clientX: x, clientY: y, screenX: x, screenY: y,
            pageX: x, pageY: y, radiusX: 4, radiusY: 4, rotationAngle: 0, force: 1});
    }
    el.dispatchEvent(new TouchEvent('touchstart', {bubbles:true, cancelable:true, view:window,
        touches:[mkTouch(x0,y0)], changedTouches:[mkTouch(x0,y0)], targetTouches:[mkTouch(x0,y0)]}));
    for (let i = 1; i <= steps; i++) {
        const x = x0 + (x1 - x0) * i / steps;
        el.dispatchEvent(new TouchEvent('touchmove', {bubbles:true, cancelable:true, view:window,
            touches:[mkTouch(x,y0)], changedTouches:[mkTouch(x,y0)], targetTouches:[mkTouch(x,y0)]}));
    }
    el.dispatchEvent(new TouchEvent('touchend', {bubbles:true, cancelable:true, view:window,
        touches:[], changedTouches:[mkTouch(x1,y0)], targetTouches:[]}));
    return el.tagName + '.' + (el.className||'').toString().substring(0,40);
}
"""

async def canvas_touch_drag(page, loc, from_frac: float, to_frac: float,
                             label: str = '') -> bool:
    enabled = await assert_enabled(page, loc, label or 'canvas')
    if not enabled:
        return False
    try:
        box = await loc.bounding_box()
        if not box:
            log('  canvas drag: no bounding box')
            return False
        x0 = box['x'] + box['width'] * from_frac
        x1 = box['x'] + box['width'] * to_frac
        y  = box['y'] + box['height'] / 2
        result = await page.evaluate(_TOUCH_DRAG_JS,
                                     {'x0': x0, 'y0': y, 'x1': x1, 'steps': 12})
        log(f'  touch drag hit: {result}')
        return True
    except Exception as e:
        log(f'  canvas drag error: {e}')
        return False


# ── Browser-session probe helpers (NO WS calls inside these) ─────────────────

async def probe_slider_browser(page, label: str, loc, shot_fn):
    """Probe a slider using touch events. No WS status calls — rely on tshark."""
    sep(f'PROBE SLIDER: {label}')
    enabled = await assert_enabled(page, loc, label)
    if not enabled:
        log('  SKIP — grayed out (wrong prerequisite state?)')
        return

    await shot_fn(f'slider_{label}_before')
    log('  DRAG → MAX (touch events)')
    await canvas_touch_drag(page, loc, 0.05, 0.95, label)
    await asyncio.sleep(1.0)
    await shot_fn(f'slider_{label}_max')

    log('  DRAG → MIN (touch events)')
    await canvas_touch_drag(page, loc, 0.95, 0.05, label)
    await asyncio.sleep(1.0)
    await shot_fn(f'slider_{label}_min')

    log('  DRAG → MID (restore)')
    await canvas_touch_drag(page, loc, 0.05, 0.5, label)
    await asyncio.sleep(0.5)
    await shot_fn(f'slider_{label}_restored')


async def probe_toggle_browser(page, label: str, loc, shot_fn,
                                restore: bool = True):
    """Click a toggle ON/OFF. No WS status calls — rely on tshark + screenshots."""
    sep(f'PROBE TOGGLE: {label}')
    enabled = await assert_enabled(page, loc, label)
    if not enabled:
        log('  SKIP — grayed out')
        return

    await shot_fn(f'toggle_{label}_before')
    log('  CLICK → ON')
    await loc.click()
    await asyncio.sleep(1.2)
    await shot_fn(f'toggle_{label}_on')

    if restore:
        log('  CLICK → OFF (restore)')
        await loc.click()
        await asyncio.sleep(1.2)
        await shot_fn(f'toggle_{label}_off')


async def dump_leaf_text(page) -> list:
    return await page.evaluate("""
        () => Array.from(document.querySelectorAll('*'))
            .filter(e => e.children.length === 0 && e.textContent.trim() && e.offsetParent)
            .map(e => e.textContent.trim())
            .filter((t, i, a) => a.indexOf(t) === i)
    """)

async def dump_html_around(page, text: str) -> str | None:
    result = await page.evaluate("""
        (searchText) => {
            const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
            let node;
            while ((node = walker.nextNode())) {
                if (node.textContent.trim() === searchText) {
                    let el = node.parentElement;
                    for (let i = 0; i < 5; i++) el = el.parentElement || el;
                    return el.outerHTML.substring(0, 3000);
                }
            }
            return null;
        }
    """, text)
    if result:
        log(f'  HTML around "{text}": {result}')
    else:
        log(f'  "{text}" not found in DOM')
    return result


# ── Browser session factory ───────────────────────────────────────────────────

async def wait_for_mask_clear(page, timeout_ms: int = 15_000):
    """Wait for any loading overlay (div.mask___*) to disappear."""
    try:
        mask = page.locator('div[class*="mask"]').first
        if await mask.is_visible(timeout=1000):
            log('  Waiting for loading mask to clear...')
            await mask.wait_for(state='hidden', timeout=timeout_ms)
            log('  Mask cleared')
    except Exception:
        pass  # mask not found or already gone — that's fine


async def open_browser_session(pw, url: str):
    """Launch a mobile-viewport Chromium window and navigate to the URL."""
    browser = await pw.chromium.launch(headless=False)
    ctx = await browser.new_context(
        viewport={'width': 390, 'height': 844},
        user_agent=(
            'Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) '
            'AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 '
            'Mobile/15E148 Safari/604.1'
        ),
    )
    page = await ctx.new_page()
    log(f'Opening: {url}')
    await page.goto(url, wait_until='domcontentloaded', timeout=15_000)
    await asyncio.sleep(3)
    await wait_for_mask_clear(page)
    return browser, page


def shot_counter():
    n = [0]
    shot_dir = Path('gap-finder-screenshots')
    shot_dir.mkdir(exist_ok=True)

    async def shot(label: str, page=None, _page=[None]):
        if page is not None:
            _page[0] = page
        path = shot_dir / f'{n[0]:03d}_{label}.png'
        await _page[0].screenshot(path=str(path))
        n[0] += 1
        log(f'  screenshot: {path.name}')
        return path

    return shot


# ── Main ──────────────────────────────────────────────────────────────────────

async def run(url: str, port: int, token: str):
    from playwright.async_api import async_playwright

    shot = shot_counter()

    # ═══════════════════════════════════════════════════════════════════════════
    # PHASE 1 — Direct WS tests (no browser open)
    # ═══════════════════════════════════════════════════════════════════════════
    sep('PHASE 1: DIRECT WS TESTS (no browser)')

    baseline = await get_status(port, token)
    if baseline is None:
        print('✗ Cannot read device status — is the camera connected?', file=sys.stderr)
        sys.exit(2)
    show('BASELINE', baseline)

    # Verify autofocus: paramType=9 — unverified in v2.2.1, no readback field.
    # Send '1' and '0', check if any status field changes (may be read-only).
    await probe_ws_direct(port, token, 9, '1', '1',
                          'Autofocus ON (paramType=9, value=1)')
    await probe_ws_direct(port, token, 9, '0', '1',
                          'Autofocus OFF (paramType=9, value=0)')

    # Check mirror readback — does DeviceInfo.mirror update when we send paramType=2?
    mirror_base = '1' if baseline.get('mirror') else '0'
    flip_to = '0' if mirror_base == '1' else '1'
    await probe_ws_direct(port, token, 2, flip_to, mirror_base,
                          'HorizontalFlip-readback(paramType=2)')

    # Re-read baseline after all Phase 1 tests (state should be fully restored)
    baseline = await get_status(port, token)
    show('BASELINE (post Phase1)', baseline)

    # ═══════════════════════════════════════════════════════════════════════════
    # PHASE 2 — Browser sessions
    # Rule: ZERO WS connections while browser is open.
    # Prerequisite state is set via WS before opening, restored after closing.
    # ═══════════════════════════════════════════════════════════════════════════

    async with async_playwright() as pw:

        # ── Session A: Normal/baseline state ─────────────────────────────────
        # Goals: anti-flicker dropdown, mirror tab, gesture tab, image tab DOM
        sep('SESSION A: Normal state — anti-flicker / mirror / gesture')
        browser, page = await open_browser_session(pw, url)

        async def shotA(label, p=None): return await shot(label, p or page)

        await shotA('A00_initial', page)

        # Open More Settings
        more = page.get_by_text('More', exact=False).first
        if await more.is_visible(timeout=3000):
            await wait_for_mask_clear(page)
            await more.click()
            await asyncio.sleep(2)
            await wait_for_mask_clear(page)
        await shotA('A01_more_settings')

        # Dump Image tab DOM to understand all elements
        sep('DUMP IMAGE TAB DOM')
        leaf = await dump_leaf_text(page)
        log(f'  Leaf text: {json.dumps(leaf)}')

        all_els = await page.evaluate("""
            () => Array.from(document.querySelectorAll(
                'input, select, canvas, [role="slider"], [role="option"], ' +
                '[class*="select"], [class*="dropdown"], [class*="picker"], ' +
                '[class*="option"], [class*="list"]'
            )).filter(e => e.offsetParent).map(e => ({
                tag: e.tagName, type: e.type||null,
                cls: (e.className||'').toString().substring(0,80),
                role: e.getAttribute('role'),
                text: e.textContent.trim().substring(0,30),
                box: (() => { const r = e.getBoundingClientRect();
                              return {x:r.x,y:r.y,w:r.width,h:r.height}; })(),
            }))
        """)
        log(f'  Interactive/canvas elements: {json.dumps(all_els)}')

        # Dump HTML around key sections
        for label in ['Anti-Flicker', 'Exposure', 'Exposure Compensation',
                      'White Balance']:
            await dump_html_around(page, label)

        # Log visual state of every canvas (grayed vs enabled)
        sep('CANVAS VISUAL STATES (baseline)')
        canvases = page.locator('canvas')
        count = await canvases.count()
        log(f'  {count} canvas elements found')
        for i in range(count):
            c = canvases.nth(i)
            try:
                if not await c.is_visible(timeout=200):
                    continue
                vs = await element_visual_state(page, c)
                log(f'  canvas#{i}: {json.dumps(vs)}')
            except Exception as e:
                log(f'  canvas#{i}: {e}')
        await shotA('A02_canvas_states')

        # Anti-flicker: dump DOM, find dropdown, click each option
        sep('PROBE ANTI-FLICKER')
        # Find and report visual state of Anti-Flicker row
        af_text = page.get_by_text('Anti-Flicker', exact=True).first
        if await af_text.is_visible(timeout=1000):
            af_html = await af_text.evaluate("""
                el => {
                    let c = el;
                    for (let i=0; i<5; i++) c = c.parentElement||c;
                    return c.outerHTML.substring(0,2000);
                }
            """)
            log(f'  Anti-Flicker row HTML: {af_html}')

            # Find all elements in the AF row and report their state
            af_row_els = await page.evaluate("""
                () => {
                    const w = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
                    let n; while ((n=w.nextNode())) {
                        if (n.textContent.trim()==='Anti-Flicker') {
                            let c=n.parentElement;
                            for(let i=0;i<5;i++) c=c.parentElement||c;
                            return Array.from(c.querySelectorAll('*')).filter(e=>e.offsetParent).map(e=>({
                                tag:e.tagName, cls:(e.className||'').toString().substring(0,80),
                                text:e.textContent.trim().substring(0,30),
                                role:e.getAttribute('role'),
                            }));
                        }
                    }
                    return [];
                }
            """)
            log(f'  Anti-Flicker row elements: {json.dumps(af_row_els)}')
            await shotA('A03_antiflicker_before')

            # Click the select button (div.selectButton___NauaV) to open the dropdown,
            # not the text label — the label click doesn't open the picker.
            af_btn = page.locator('div[class*="selectButton"]').first
            if await af_btn.is_visible(timeout=1000):
                log('  Clicking selectButton (Anti-Flicker dropdown trigger)...')
                await af_btn.click()
            else:
                log('  selectButton not found — falling back to row click')
                await af_text.click()
            await asyncio.sleep(1.5)
            await shotA('A04_antiflicker_clicked')

            # Check what appeared
            new_leaf = await dump_leaf_text(page)
            log(f'  Text after click: {json.dumps(new_leaf)}')

            # Try clicking 50Hz and 60Hz options if visible
            for opt in ['50Hz', '60Hz', '50 Hz', '60 Hz']:
                loc = page.get_by_text(opt, exact=False).first
                try:
                    if await loc.is_visible(timeout=500):
                        log(f'  Found option {opt!r} — clicking')
                        await shotA(f'A05_af_before_{opt}')
                        await loc.click()
                        await asyncio.sleep(1.5)
                        await shotA(f'A05_af_after_{opt}')
                except Exception as e:
                    log(f'  option {opt!r}: {e}')

            # Restore Auto
            for opt in ['Auto', 'auto']:
                loc = page.get_by_text(opt, exact=True).first
                try:
                    if await loc.is_visible(timeout=400):
                        log(f'  Restoring to {opt!r}')
                        await loc.click()
                        await asyncio.sleep(1.0)
                        break
                except Exception:
                    pass
            await shotA('A06_antiflicker_restored')
        else:
            log('  "Anti-Flicker" text not found on Image tab')

        # Mirror tab
        sep('MIRROR TAB')
        mirror_tab = page.get_by_text('Mirror', exact=True).first
        if await mirror_tab.is_visible(timeout=1000):
            await mirror_tab.click()
            await asyncio.sleep(2)
            await shotA('A07_mirror_tab')
            leaf_m = await dump_leaf_text(page)
            log(f'  Mirror tab text: {json.dumps(leaf_m)}')
            await dump_html_around(page, 'Horizontal Flip')

            # Probe Horizontal Flip toggle
            hf_btn = page.locator('div[class*="switch"] div[class*="btn"]').first
            await probe_toggle_browser(page, 'HorizontalFlip', hf_btn, shotA)

        # Gesture Control tab
        sep('GESTURE CONTROL TAB')
        gesture_tab = page.get_by_text('Gesture Control', exact=False).first
        if await gesture_tab.is_visible(timeout=1000):
            await gesture_tab.click()
            await asyncio.sleep(2)
            await shotA('A08_gesture_tab')
            leaf_g = await dump_leaf_text(page)
            log(f'  Gesture tab text: {json.dumps(leaf_g)}')

            # Dump full HTML of this tab to understand structure
            g_html = await page.evaluate("""
                () => document.body.innerHTML.substring(0, 5000)
            """)
            log(f'  Gesture tab HTML snippet: {g_html}')

            # Probe all switch/btn elements
            switches = page.locator('div[class*="switch"]')
            sw_count = await switches.count()
            log(f'  Found {sw_count} switch elements')
            for i in range(sw_count):
                sw = switches.nth(i)
                try:
                    if not await sw.is_visible(timeout=200):
                        continue
                    btn = sw.locator('div[class*="btn"]').first
                    if not await btn.is_visible(timeout=200):
                        continue
                    txt = (await sw.inner_text()).strip()[:40]
                    log(f'  switch[{i}]: {txt!r}')
                    await probe_toggle_browser(page, f'gesture-switch[{i}]({txt})',
                                               btn, shotA)
                except Exception as e:
                    log(f'  switch[{i}]: {e}')

        await browser.close()
        log('Session A closed — WS now free')

        # ── Session B: AE=OFF — unlock Exposure Compensation slider ───────────
        sep('SESSION B: Auto Exposure OFF → probe EC slider')
        ae_was_on = baseline.get('autoExposure', True)
        log(f'  AE baseline: {ae_was_on}')

        if ae_was_on:
            log('  Setting autoExposure=OFF via WS (paramType=17, "0")...')
            await ws_set(port, token, 17, '0')
            await asyncio.sleep(1.0)
            state_b = await get_status(port, token)
            show('state for Session B', state_b)

        browser, page = await open_browser_session(pw, url)

        async def shotB(label, p=None): return await shot(label, p or page)

        # Open More Settings
        more2 = page.get_by_text('More', exact=False).first
        if await more2.is_visible(timeout=3000):
            await wait_for_mask_clear(page)
            await more2.click()
            await asyncio.sleep(2)
            await wait_for_mask_clear(page)
        await shotB('B00_ae_off_image_tab', page)

        # Report canvas states — EC slider should now be enabled
        sep('CANVAS STATES: AE=off (EC slider should be enabled)')
        canvases_b = page.locator('canvas')
        count_b = await canvases_b.count()
        log(f'  {count_b} canvas elements')
        for i in range(count_b):
            c = canvases_b.nth(i)
            try:
                if not await c.is_visible(timeout=200):
                    continue
                vs = await element_visual_state(page, c)
                box = vs.get('box') or {}
                log(f'  canvas#{i}: w={box.get("width",0):.0f} h={box.get("height",0):.0f} '
                    f'opacity={vs.get("opacity")} disabled={vs.get("disabled")}')
                # Probe any slider-shaped canvas that is now enabled
                if (box.get('height', 99) < 50 and box.get('width', 0) > 100
                        and not vs.get('disabled')):
                    log(f'  canvas#{i}: ENABLED slider → probing')
                    await probe_slider_browser(page, f'B-canvas{i}', c, shotB)
            except Exception as e:
                log(f'  canvas#{i}: {e}')

        await browser.close()
        log('Session B closed')

        if ae_was_on:
            log('  Restoring autoExposure=ON (paramType=17, "1")...')
            await ws_set(port, token, 17, '1')
            await asyncio.sleep(0.8)

        # ── Session C: AE=OFF + AWB=OFF — unlock WB temperature slider ────────
        sep('SESSION C: Auto WB OFF → probe WB temperature slider')
        awb_was_on = baseline.get('autoWhiteBalance', True)
        log(f'  AWB baseline: {awb_was_on}')

        if awb_was_on or ae_was_on:
            if ae_was_on:
                log('  Setting autoExposure=OFF (paramType=17, "0")...')
                await ws_set(port, token, 17, '0')
                await asyncio.sleep(0.5)
            if awb_was_on:
                log('  Setting autoWhiteBalance=OFF (paramType=20, "0")...')
                await ws_set(port, token, 20, '0')
                await asyncio.sleep(0.5)
            state_c = await get_status(port, token)
            show('state for Session C', state_c)

        browser, page = await open_browser_session(pw, url)

        async def shotC(label, p=None): return await shot(label, p or page)

        more3 = page.get_by_text('More', exact=False).first
        if await more3.is_visible(timeout=3000):
            await wait_for_mask_clear(page)
            await more3.click()
            await asyncio.sleep(2)
            await wait_for_mask_clear(page)
        await shotC('C00_ae_awb_off_image_tab', page)

        sep('CANVAS STATES: AE=off + AWB=off (WB temp slider should also be enabled)')
        canvases_c = page.locator('canvas')
        count_c = await canvases_c.count()
        log(f'  {count_c} canvas elements')
        for i in range(count_c):
            c = canvases_c.nth(i)
            try:
                if not await c.is_visible(timeout=200):
                    continue
                vs = await element_visual_state(page, c)
                box = vs.get('box') or {}
                log(f'  canvas#{i}: w={box.get("width",0):.0f} h={box.get("height",0):.0f} '
                    f'opacity={vs.get("opacity")} disabled={vs.get("disabled")}')
                if (box.get('height', 99) < 50 and box.get('width', 0) > 100
                        and not vs.get('disabled')):
                    log(f'  canvas#{i}: ENABLED → probing')
                    await probe_slider_browser(page, f'C-canvas{i}', c, shotC)
            except Exception as e:
                log(f'  canvas#{i}: {e}')

        await browser.close()
        log('Session C closed')

        # Restore all
        if ae_was_on:
            log('  Restoring autoExposure=ON (paramType=17, "1")...')
            await ws_set(port, token, 17, '1')
            await asyncio.sleep(0.5)
        if awb_was_on:
            log('  Restoring autoWhiteBalance=ON (paramType=20, "1")...')
            await ws_set(port, token, 20, '1')
            await asyncio.sleep(0.5)

    # ═══════════════════════════════════════════════════════════════════════════
    # PHASE 3 — Final status check (browser closed, WS free)
    # ═══════════════════════════════════════════════════════════════════════════
    sep('PHASE 3: FINAL STATUS')
    final = await get_status(port, token)
    show('FINAL', final)
    overall = diff(baseline, final)
    if overall:
        log(f'Changes from baseline: {json.dumps(overall)}')
    else:
        log('Final state matches baseline — all changes restored')

    log(f'\nScreenshots in: gap-finder-screenshots/')
    log('Correlate log timestamps with your tshark capture to identify paramTypes.')


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    arg = sys.argv[1]
    if not (arg.startswith('http') or arg.startswith('ws')):
        print(f'Expected QR URL, got: {arg}', file=sys.stderr)
        sys.exit(1)

    qs = parse_qs(urlparse(arg).query)
    port_str = qs.get('port', [None])[0]
    token = qs.get('token', [None])[0]
    port = int(port_str) if port_str and port_str.isdigit() else None

    if not port:
        port = _lsof_port()
        if not port:
            print('✗ Could not find port.', file=sys.stderr)
            sys.exit(2)

    if not token:
        token = _read_token_from_ini() or ''

    print(f'[gap-finder] port={port}')
    print('[gap-finder] Ready. Start tshark, then press Enter.')
    input()

    asyncio.run(run(arg, port, token))


if __name__ == '__main__':
    main()
