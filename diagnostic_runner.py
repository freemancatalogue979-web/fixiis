"""
diagnostic_runner.py
====================

Spins up a browser using the same stealth stack as browser_manager.py,
navigates to the four major JS fingerprinting test sites, and prints a
scorecard showing which detection signals passed and which failed.

This is the ONLY way to verify that stealth patches actually work — running
it locally shows you which gaps remain before you hit Google.

Usage:
    python diagnostic_runner.py                      # desktop Chrome
    python diagnostic_runner.py --mobile pixel_7     # mobile emulation
    python diagnostic_runner.py --mobile iphone_14_pro
    python diagnostic_runner.py --url https://bot.sannysoft.com  # custom

Tested sites (open in headed mode for best results):
  - https://bot.sannysoft.com                    (FpScanner, basic)
  - https://browserleaks.com/javascript          (full navigator dump)
  - https://pixelscan.net                        (modern, Cloudflare-style)
  - https://abrahamjuliot.github.io/creepjs/     (deepest open-source test)

Each site tests different layers; passing bot.sannysoft is easy, passing
creepjs is hard.  Use the scorecard to know where you stand.

Requirements:
  pip install playwright
  playwright install chromium
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path

# Reuse the v2 patch builder so the diagnostic is in sync with what the
# production browser sees.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from stealth_advanced import build_advanced_stealth_script, DIAGNOSTIC_JS  # noqa: E402


DEFAULT_TEST_URLS = [
    ("bot.sannysoft", "https://bot.sannysoft.com"),
    ("browserleaks/javascript", "https://browserleaks.com/javascript"),
    ("pixelscan", "https://pixelscan.net"),
    ("creepjs", "https://abrahamjuliot.github.io/creepjs/"),
]

# Expected "good" values for each known test. If the page's reported value
# matches the expected one, the test passes. Each entry is a (key, expected,
# description) tuple — the runner compares out[key] == expected and prints
# PASS / FAIL.
SCORECARD = [
    ("webdriver",         None,            "Should be undefined / false"),
    ("hardwareConcurrency", 8,             "Should match fingerprint"),
    ("deviceMemory",      16,              "Should match fingerprint"),
    ("maxTouchPoints",    0,               "0 desktop, 5 mobile"),
    ("uaData",            None,            "Should be present and consistent"),
    ("dpr",               1.0,             "Must be 1.0 - logical-pixel policy"),
    ("outerW",            None,            "Should be > innerW + 0"),
    ("outerH",            None,            "Should be > innerH + 0"),
    ("availH",            None,            "Should be < screenH (taskbar)"),
    ("colorDepth",        24,              "Should be 24 (or 30/48 HDR)"),
    ("isExtended",        False,           "Should be false on single monitor"),
    ("notificationPermission", "default",  "Should be 'default' on fresh profile"),
    ("dateToString",      None,            "Should contain 'GMT' and TZ name"),
    ("intlResolvedTz",    None,            "Should match fingerprint timezone"),
    ("intlFormattedSample", None,          "Should reflect spoofed TZ"),
    ("connectionType",    None,            "Should be 'ethernet' / 'cellular' / etc"),
    ("chromeRuntimeId",   None,            "Should be 32-char hex"),
    ("rtcPeerConn",       "function",      "Should still exist (don't remove)"),
    ("webglVendor",       None,            "Should match fingerprint"),
    ("webglRenderer",     None,            "Should match fingerprint"),
    ("canvasHash",        None,            "Should be deterministic per user"),
]


def color(text: str, code: str) -> str:
    if not sys.stdout.isatty():
        return text
    return f"\x1b[{code}m{text}\x1b[0m"


def green(s):  return color(s, "32")
def red(s):    return color(s, "31")
def yellow(s): return color(s, "33")
def bold(s):   return color(s, "1")


def scorecard_line(key, expected, value, description):
    if expected is None:
        # Just informational
        return f"  {yellow('?')} {key:30s} = {value!r:60s} ({description})"
    ok = value == expected
    mark = green("PASS") if ok else red("FAIL")
    return f"  {mark} {key:30s} = {value!r:60s}  expected={expected!r}  ({description})"


async def run_diagnostic(mobile_device: str = None, urls=None, headless: bool = False,
                         locale: str = "en-US", proxy: str = None) -> None:
    from playwright.async_api import async_playwright

    if urls is None:
        urls = DEFAULT_TEST_URLS

    # The fingerprint here is illustrative.  In production, you'd pass
    # the user's saved fingerprint from FingerprintManager.
    is_mobile = bool(mobile_device)
    fingerprint = {
        "user_agent": (
            "Mozilla/5.0 (Linux; Android 15; Pixel 7) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/147.0.0.0 Mobile Safari/537.36"
            if mobile_device == "pixel_7" else
            "Mozilla/5.0 (iPhone; CPU iPhone OS 18_3 like Mac OS X) "
            "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.3 "
            "Mobile/22D72 Safari/604.1"
            if mobile_device == "iphone_14_pro" else
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36"
        ),
        "viewport": {"width": 412, "height": 915} if mobile_device == "pixel_7" else
                    {"width": 393, "height": 852} if mobile_device == "iphone_14_pro" else
                    {"width": 1920, "height": 1080},
        "pixel_ratio": 2.625 if mobile_device == "pixel_7" else
                       3.0 if mobile_device == "iphone_14_pro" else 1.0,
        "is_mobile": is_mobile,
        "platform": "Linux; Android 15" if mobile_device == "pixel_7" else
                    "iPhone" if mobile_device == "iphone_14_pro" else
                    "Win32",
        "oscpu": "Linux; Android 15" if mobile_device == "pixel_7" else
                 "CPU OS 18_3 like Mac OS X" if mobile_device == "iphone_14_pro" else
                 "Windows NT 10.0; Win64; x64",
        "cpu_cores": 8,
        "memory": 8 if is_mobile else 16,
        "max_touch_points": 5 if is_mobile else 0,
        "timezone": "America/New_York",
        "timezone_offset": 240,  # EDT = UTC-4
        "language": locale,
        "country": "US",
        "webgl_vendor": "ARM" if mobile_device == "pixel_7" else
                        "Apple Inc." if mobile_device == "iphone_14_pro" else
                        "Google Inc. (NVIDIA)",
        "webgl_renderer": "Mali G78" if mobile_device == "pixel_7" else
                          "Apple GPU" if mobile_device == "iphone_14_pro" else
                          "ANGLE (NVIDIA, NVIDIA GeForce GTX 1050 Ti Direct3D11 vs_5_0 ps_5_0)",
        "fonts": ["Roboto", "Noto Sans", "Helvetica Neue", "Arial", "Times New Roman"] if is_mobile
                 else ["Segoe UI", "Arial", "Times New Roman", "Calibri", "Consolas"],
        "canvas_seed": 12345,
        "audio_seed": 12345,
        "screen_width": 412 if mobile_device == "pixel_7" else
                        393 if mobile_device == "iphone_14_pro" else 1920,
        "screen_height": 915 if mobile_device == "pixel_7" else
                         852 if mobile_device == "iphone_14_pro" else 1080,
    }

    print(bold(f"\n=== Stealth diagnostic — mode={'mobile ' + mobile_device if mobile_device else 'desktop'} ==="))
    print(f"  UA: {fingerprint['user_agent'][:80]}")
    print(f"  viewport: {fingerprint['viewport']}")
    print(f"  locale: {locale}, proxy: {proxy or 'none'}\n")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=headless)
        context_args = {
            "viewport": fingerprint["viewport"],
            "user_agent": fingerprint["user_agent"],
            "locale": locale,
            "is_mobile": is_mobile,
            "has_touch": is_mobile,
        }
        if proxy:
            context_args["proxy"] = {"server": proxy}
        context = await browser.new_context(**context_args)

        # Apply the v2 stealth patches. In production you'd also call
        # _apply_stealth, _apply_stealth_hardening, _apply_mobile_stealth
        # from browser_manager.py before this.
        await context.add_init_script(build_advanced_stealth_script(fingerprint, is_mobile))

        all_results = {}
        for label, url in urls:
            print(bold(f"\n--- {url} ---"))
            try:
                page = await context.new_page()
                # Set a sane timeout so a hanging site doesn't block the run
                page.set_default_timeout(20000)
                try:
                    await page.goto(url, wait_until="domcontentloaded")
                    # Give the page's fingerprint scripts a moment to run
                    await page.wait_for_timeout(2500)
                except Exception as e:
                    print(yellow(f"  navigation warning: {e}"))
                try:
                    diag = await page.evaluate(DIAGNOSTIC_JS)
                except Exception as e:
                    diag = {"_error": str(e)}
                try:
                    title = await page.title()
                except Exception:
                    title = "?"
                all_results[label] = {"url": url, "title": title, "diag": diag}

                # Print the scorecard for this page
                if isinstance(diag, dict) and "_error" not in diag:
                    for key, expected, desc in SCORECARD:
                        if key in diag:
                            print(scorecard_line(key, expected, diag[key], desc))
                    # Special: chromeRuntimeId hex check
                    crid = diag.get("chromeRuntimeId")
                    if isinstance(crid, str) and len(crid) == 32 and all(c in "0123456789abcdef" for c in crid):
                        print(green("  PASS") + " chromeRuntimeId             = 32-char hex (deterministic)")
                    else:
                        print(red("  FAIL") + f" chromeRuntimeId             = {crid!r} (not 32-char hex)")
                else:
                    print(red("  could not read diagnostic from page"))
                await page.close()
            except Exception as e:
                print(red(f"  page error: {e}"))

        await context.close()
        await browser.close()

    # Summary
    print(bold("\n=== SUMMARY ==="))
    for label, res in all_results.items():
        diag = res.get("diag") or {}
        if "_error" in diag:
            print(f"  {label:30s} {red('ERROR')}  {diag.get('_error', '?')[:60]}")
            continue
        # Count PASS / FAIL on the scorecard
        pass_count = 0
        fail_count = 0
        for key, expected, _ in SCORECARD:
            if key in diag:
                if expected is None or diag[key] == expected:
                    pass_count += 1
                else:
                    fail_count += 1
        total = pass_count + fail_count
        pct = (100 * pass_count / total) if total else 0
        status = green(f"{pct:.0f}% pass") if pct >= 80 else yellow(f"{pct:.0f}% pass") if pct >= 50 else red(f"{pct:.0f}% pass")
        print(f"  {label:30s} {status}  ({pass_count}/{total} scorecard checks)")


def main():
    ap = argparse.ArgumentParser(description="Stealth diagnostic runner")
    ap.add_argument("--mobile", choices=["pixel_7", "iphone_14_pro"],
                    help="Use mobile emulation")
    ap.add_argument("--headless", action="store_true",
                    help="Run headless (default: headed so you can see)")
    ap.add_argument("--locale", default="en-US",
                    help="Locale (e.g. en-US, de-DE, ja-JP)")
    ap.add_argument("--proxy", default=None,
                    help="Proxy server URL, e.g. http://user:pass@host:port")
    ap.add_argument("--url", action="append",
                    help="Add a custom test URL (can be used multiple times)")
    args = ap.parse_args()

    urls = list(DEFAULT_TEST_URLS)
    if args.url:
        for u in args.url:
            label = u.split("//", 1)[-1].split("/", 1)[0]
            urls.append((label, u))

    asyncio.run(run_diagnostic(
        mobile_device=args.mobile,
        urls=urls,
        headless=args.headless,
        locale=args.locale,
        proxy=args.proxy,
    ))


if __name__ == "__main__":
    main()
