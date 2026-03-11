#!/usr/bin/env python3
"""mobileui-dump — Drive the Insta360 Link Controller mobile web UI and label
every action for correlating with a simultaneous TCP dump.

Usage:
    python3 mobileui-dump.py <qr-code-image>   # decode QR, then drive UI
    python3 mobileui-dump.py <url>             # use URL directly

The script opens the mobile remote URL in a headless Chromium (playwright),
walks every discoverable control in a fixed sequence, and prints a timestamped
action log to stdout. Run tshark alongside and correlate timestamps.

Requirements:
    pip install playwright pillow
    playwright install chromium
    # For QR decode (alternative to passing URL directly):
    brew install zbar      # provides zbarimg
    # OR: pip install pyzbar
"""

import asyncio
import subprocess
import sys
import time
from pathlib import Path


# ── QR decode ────────────────────────────────────────────────────────────────

def decode_qr(image_path: str) -> str:
    # Try zbarimg first (brew install zbar)
    try:
        r = subprocess.run(['zbarimg', '--raw', '-q', image_path],
                           capture_output=True, text=True)
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip()
    except FileNotFoundError:
        pass

    # Try pyzbar (pip install pyzbar pillow)
    try:
        from PIL import Image
        from pyzbar.pyzbar import decode
        decoded = decode(Image.open(image_path))
        if decoded:
            return decoded[0].data.decode('utf-8')
    except ImportError:
        pass

    raise RuntimeError(
        "Cannot decode QR code.\n"
        "  Install zbarimg:  brew install zbar\n"
        "  Or pyzbar:        pip install pyzbar pillow\n"
        "  Or pass the URL directly as the argument."
    )


# ── Logging ───────────────────────────────────────────────────────────────────

def log(action: str):
    ts = time.strftime('%H:%M:%S')
    print(f"[{ts}] ACTION: {action}", flush=True)


async def pause(seconds: float = 1.5):
    """Pause between actions so tshark captures are clearly separated."""
    await asyncio.sleep(seconds)


# ── UI walker ────────────────────────────────────────────────────────────────

async def drive_ui(url: str, screenshot_dir: Path):
    from playwright.async_api import async_playwright

    screenshot_dir.mkdir(exist_ok=True)
    shot_idx = 0

    async def screenshot(label: str):
        nonlocal shot_idx
        path = screenshot_dir / f"{shot_idx:03d}_{label}.png"
        await page.screenshot(path=str(path))
        shot_idx += 1

    async def click_labeled(locator, label: str):
        log(label)
        await locator.click()
        await pause()
        await screenshot(label.lower().replace(' ', '_'))

    async def drag_slider(locator, label: str, steps=5):
        """Drag a slider through its range end-to-end."""
        log(f"{label} — slide full range")
        box = await locator.bounding_box()
        if not box:
            print(f"  [skip] {label} — no bounding box", flush=True)
            return
        x_start = box['x'] + 4
        x_end   = box['x'] + box['width'] - 4
        y_mid   = box['y'] + box['height'] / 2
        await page.mouse.move(x_start, y_mid)
        await page.mouse.down()
        for i in range(1, steps + 1):
            await page.mouse.move(x_start + (x_end - x_start) * i / steps, y_mid)
            await asyncio.sleep(0.1)
        await page.mouse.up()
        await pause()
        await screenshot(label.lower().replace(' ', '_') + '_max')
        # Slide back
        await page.mouse.move(x_end, y_mid)
        await page.mouse.down()
        for i in range(1, steps + 1):
            await page.mouse.move(x_end - (x_end - x_start) * i / steps, y_mid)
            await asyncio.sleep(0.1)
        await page.mouse.up()
        await pause()
        await screenshot(label.lower().replace(' ', '_') + '_min')

    async with async_playwright() as pw:
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

        print(f"\n[mobileui-dump] Opening: {url}", flush=True)
        await page.goto(url, wait_until='domcontentloaded', timeout=15_000)
        await pause(3)
        await screenshot('00_initial')
        print("[mobileui-dump] Page loaded. Starting control walk...\n", flush=True)

        # ── Step 1: discover and click every button/toggle by visible text ──────
        # We'll try known labels first, then fall back to generic discovery.

        known_buttons = [
            # AI modes
            'Tracking',
            'DeskView',
            'Desk View',
            'Whiteboard',
            'Overhead',
            'Normal',
            # Privacy
            'Privacy',
            # Zoom
            'Zoom In',
            'Zoom Out',
            # Pan/tilt directional
            'Pan Left',
            'Pan Right',
            'Tilt Up',
            'Tilt Down',
            'Center',
            # Advanced settings
            'HDR',
            'Brightness',
            'Contrast',
            'Saturation',
            'Sharpness',
            'Auto Exposure',
            'Auto White Balance',
            'White Balance',
            'Noise Reduction',
            'Flip',
            'Mirror',
        ]

        for label in known_buttons:
            loc = page.get_by_text(label, exact=False).first
            try:
                if await loc.is_visible(timeout=500):
                    await click_labeled(loc, f"Button: {label}")
            except Exception:
                pass

        # ── Step 2: generic button sweep — anything not already clicked ──────────
        log("GENERIC BUTTON SWEEP — all remaining visible buttons")
        buttons = page.locator('button, [role="button"], [role="switch"]')
        count = await buttons.count()
        print(f"  Found {count} button/switch elements", flush=True)
        for i in range(count):
            btn = buttons.nth(i)
            try:
                if not await btn.is_visible(timeout=300):
                    continue
                text = (await btn.inner_text()).strip()[:60] or f"button#{i}"
                await click_labeled(btn, f"Generic button: {text}")
            except Exception as e:
                print(f"  [skip] button#{i}: {e}", flush=True)

        # ── Step 3: sliders ─────────────────────────────────────────────────────
        log("SLIDER SWEEP — all range inputs")
        sliders = page.locator('input[type="range"]')
        count = await sliders.count()
        print(f"  Found {count} range sliders", flush=True)
        for i in range(count):
            sl = sliders.nth(i)
            try:
                if not await sl.is_visible(timeout=300):
                    continue
                aria = await sl.get_attribute('aria-label') or f"slider#{i}"
                await drag_slider(sl, f"Slider: {aria}")
            except Exception as e:
                print(f"  [skip] slider#{i}: {e}", flush=True)

        # ── Step 4: checkboxes / toggles ─────────────────────────────────────────
        log("CHECKBOX / TOGGLE SWEEP")
        checks = page.locator('input[type="checkbox"], input[type="radio"]')
        count = await checks.count()
        print(f"  Found {count} checkbox/radio elements", flush=True)
        for i in range(count):
            cb = checks.nth(i)
            try:
                if not await cb.is_visible(timeout=300):
                    continue
                label_el = page.locator(f'label[for="{await cb.get_attribute("id")}"]')
                label_text = ''
                try:
                    label_text = (await label_el.inner_text()).strip()[:60]
                except Exception:
                    pass
                name = label_text or await cb.get_attribute('aria-label') or f"check#{i}"
                log(f"Toggle: {name} ON")
                await cb.check()
                await pause()
                log(f"Toggle: {name} OFF")
                await cb.uncheck()
                await pause()
            except Exception as e:
                print(f"  [skip] check#{i}: {e}", flush=True)

        # ── Step 5: select dropdowns ─────────────────────────────────────────────
        log("SELECT SWEEP — all dropdowns")
        selects = page.locator('select')
        count = await selects.count()
        print(f"  Found {count} select elements", flush=True)
        for i in range(count):
            sel = selects.nth(i)
            try:
                if not await sel.is_visible(timeout=300):
                    continue
                options = await sel.locator('option').all_text_contents()
                for opt in options:
                    log(f"Select: option '{opt.strip()}'")
                    await sel.select_option(label=opt.strip())
                    await pause()
            except Exception as e:
                print(f"  [skip] select#{i}: {e}", flush=True)

        await screenshot('99_done')
        print(f"\n[mobileui-dump] Done. Screenshots in: {screenshot_dir}/", flush=True)
        await pause(2)
        await browser.close()


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    arg = sys.argv[1]

    if arg.startswith('http') or arg.startswith('ws'):
        url = arg
    else:
        print(f"[mobileui-dump] Decoding QR: {arg}", flush=True)
        url = decode_qr(arg)
        print(f"[mobileui-dump] URL: {url}", flush=True)

    screenshot_dir = Path('mobileui-screenshots')
    print("[mobileui-dump] Ready. Start your tshark capture, then press Enter.")
    input()

    asyncio.run(drive_ui(url, screenshot_dir))


if __name__ == '__main__':
    main()
