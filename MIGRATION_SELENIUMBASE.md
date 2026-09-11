# Migration Plan: SeleniumBase for all browsers

> **STATUS: IMPLEMENTED (P0–P6), DEFAULT-ON.** `browser_backend` defaults to `'sb'` (escape hatch `BROWSER_BACKEND=pw`); `captcha_mode` defaults to `'auto'` (hatch `CAPTCHA_MODE=off`).
> `sb_backend.py`: SeleniumBase UC `Driver(uc=True)` launches/stealths/lifecycles real Chrome in a
> dedicated single-thread executor (sync API isolated from asyncio); our own async raw-CDP client +
> Playwright-shaped adapters (`SBPage`/`SBContext`/`SBBrowser`) drive it — custom stealth scripts and
> playwright-stealth are bypassed on this path by design (P4). Cutovers: PCM pilot (P1) and sessions (P3),
> both strict when SB is explicitly selected (no hidden Playwright browser after an SB attach failure).
> P2: `ARCHIVE_FORMAT=mhtml` via CDP
> `Page.captureSnapshot`. P5: `CAPTCHA_MODE=auto` probes for checkbox-class challenges after navigations
> and runs `driver.uc_gui_click_captcha()` (≤2 attempts). **Linux display guarantee:** every
> Each SeleniumBase browser now creates and owns a private Xvfb process/display, uses that display
> only while its `Driver` is constructed, and stops Xvfb with the browser. It never adopts the shared
> application display or inherited `DISPLAY`; if Xvfb is unavailable, the SB launch fails loudly instead
> of silently using a real operator display or plain headless mode. This guard adds no Chrome flags and
> the SB path does not load/import a browser extension. Mobile iPhone/iPad inputs are normalized to
> the same Android 15 Pixel 7 Chrome UA in both SB and Playwright paths. Keywords verified byte-for-byte against
> installed seleniumbase **4.53.7** (chromium_arg comma-join, window_size kwarg, DevToolsActivePort
> discovery — UC/chromedriver owns the debug-port flag, so we never set it). Verified: py_compile all,
> 31-case fake-CDP adapter harness (navigation/evaluate/input/bindings/cookies/permissions/popups/
> cdp-session/MHTML), JS syntax checks. Remaining verification is on the live box: stealth scores
> (creepjs/nowsecure), captcha auto-solve on real challenges, PCM live view pixels, session profiles.
> P6: Playwright stays installed as the default/backstop backend; no dependencies were removed.
>
> **LIVE-VERIFY FIX ROUND (report: "SB browser opens but nothing connected — no streaming, no captures"):**
> root cause = the adapters were Playwright-*internal*-shaped while all consumers use Playwright's
> **public** shapes. Fixed in `sb_backend.py`: (1) `page.context`/`page.url` are now real Python
> **properties** (they were methods — every `page.context.new_cdp_session(...)` call in
> webrtc_stream/dom_capture crashed or chained onto `None`, killing screencast + captures)
> — `_setup_permission_handler`'s `context.browser.contexts.append(...)` now also lands correctly;
> (2) every attached target registers into `SBContext.pages` (tabs/popups/listeners were invisible
> before); (3) attach enables `DOM.enable` / `Page.setBypassCSP` / lifecycle events (best-effort);
> (4) **no-leak guarantee**: if attach fails after UC Chrome opens, the driver+Chrome are stopped
> before the selected backend reports failure (previously an orphaned SB window stayed open while the app
> drove a different browser — the exact reported symptom); (5) the driver executor remains single-threaded and the
> "CDP endpoint didn't answer" error now names the probed ports + selected-backend fate. Harness grew to
> **39 asserts** (property surface, registration, attach domains, no-leak path) — **ATTACH ROBUSTNESS (DevToolsActivePort unreadable on live box):** the debug
endpoint now comes from chromedriver's own `debuggerAddress` capability (deterministic,
no file guessing), with DevToolsActivePort polled ~8s as fallback (UC's re-attach dance
rewrites it late).  Both missing => loud error and a refused SB session when SB is selected
(`BROWSER_BACKEND=pw` is the explicit rollback); port guessing stays banned
(stopping PCM from latching onto the wrong browser).
**LIVE-VERIFY ROUND 2 (report: PCM attached to the wrong browser; windows only show
google.com; stream OK but client gets no DOM):** three independent defects, all fixed:
(1) **port guessing removed** — with a known profile we now trust DevToolsActivePort
exclusively and *raise* if unreadable; guessing 9222/9223 is exactly how a second SB
browser (PCM) latched onto the session's Chrome; (2) **tab ownership** — `new_page()`
always creates its own page target and closes profile-restored startup tabs (the
"only ever google.com" window: we were navigating an invisible adopted tab while a
restored tab sat in the foreground), then activates ours; (3) **`evaluate` semantics**
— IIFE/plain-expression strings now go through `Runtime.evaluate` (arrow/function
strings stay on `callFunctionOn`). Previously IIFEs were sent to callFunctionOn where
Chrome chokes — this silently killed BOTH the delta observer and the interaction
trigger on SB (they're IIFEs), which broke interaction-driven DOM updates and the
typing recapture skip on this backend. Plus: consecutive capture failures now log an
ERROR (a client receiving no DOM is never silent). Harness updated: 43 asserts.
earlier: input layer hardened (never raises — a bad key event can't tear the browser down), green ×2 alongside
> dom_capture (24) + jsdom mirror (28) + py_compile(6 files).

**Goal:** every browser (customer sessions + PCM) is launched and kept
undetected by **SeleniumBase UC mode** (undetected-chromedriver) — our
custom stealth scripts retire, and SB's built-in CAPTCHA clicker replaces
manual captcha work. Control (streaming, input, capture) stays on raw CDP.

**Why this is feasible (key insight):** `browser_manager.py` already has a
full **direct-CDP control plane** (`DirectChromeLauncher` + raw CDP
websocket — screencast, input, navigation) that talks to Chrome with **no
Playwright involvement**. SeleniumBase UC mode launches a real Chrome with
a known `--remote-debugging-port`. So:

> **SB = the launcher/stealth/lifecycle. Our existing CDP layer = the
> control plane.** We attach our CDP code to the SB-launched browser.

---

## 1. Current inventory (what exists today)

| Area | File / size | Role |
|---|---|---|
| Launch paths ×3 | `browser_manager.py` (~6.5k lines) | direct-CDP launcher, Playwright persistent-context, CDP screencast builder |
| Stealth | `stealth_advanced.py` (656 lines), `playwright-stealth` pip pkg, ~8 `add_init_script` injects, fingerprint manager + `mobile_devices.json` | UA/sec-ch-ua/webdriver/canvas/WebGL spoofing, per-user profile persistence |
| Customer session | `session.py` (~3k lines) | drives the browser via Playwright `page`: goto/reload/back/forward/evaluate/keyboard/mouse/click/title/url, `expose_function`, `add_init_script`, `context.new_cdp_session` |
| PCM live browser | `pcm_manager.py` (~950 lines) | CDP screencast + input via Playwright CDP session |
| DOM capture | `dom_capture.py` (~1.8k lines) | SingleFile **extension** discovered over CDP Target API, plus `evaluate` bridge |
| Viewport policy | `MIN_VIEWPORT_WIDTH=500`, `WINDOW_CHROME_HEIGHT=157` | stays — it’s about Chrome windows, not the driver |

## 2. Target architecture

```
                 ┌─────────────────────────┐
   api.py        │  BrowserBackend (new)   │  factory picks impl by flag
   session.py ──▶│  sb_backend.py          │
   pcm_manager ─▶│  - SBLauncher           │  SeleniumBase Driver(uc=True,
                 │    (SeleniumBase UC)    │   user_data_dir, proxy, agent…)
                 │  - BrowserHandle        │  thin Playwright-like adapter
                 │    over raw CDP         │  (existing code, moved)
                 └──────────┬──────────────┘
                            │ CDP websocket (debug port)
                 ┌──────────▼──────────────┐
                 │  Chrome (UC-hardened)   │  stealth handled by SB
                 └─────────────────────────┘
```

### Component notes

- **SBLauncher (sync, thread-confined).** Selenium/SB is synchronous; our
  stack is asyncio. All SB calls run through `asyncio.to_thread` on a
  dedicated single-thread executor per driver (same rule as UC: one driver
  = one thread). Lifecycle: `Driver(...)` → capture the debugger address →
  hand the **debug port** to our CDP layer → SB object kept only for:
  UC-mode reconnect cycles, captcha solving, and clean `quit()`.
- **BrowserHandle (async, Playwright-shaped).** Implements the exact
  methods session/pcm/dom_capture use (measured usage: `goto, reload,
  go_back, go_forward, evaluate, keyboard.*, mouse.*, click, title, url,
  is_closed, close, bring_to_front, expose_function, add_init_script,
  new_cdp_session`). Internals = raw CDP over the debug-port websocket —
  mostly **reuse of the existing direct-CDP code**, just pointed at an
  SB-launched Chrome instead of our own subprocess.
- **CDP attach.** Connect to `/json/version` → `webSocketDebuggerUrl`,
  `Target.attachToTarget` per page. Screencast/input/metrics override all
  go through this socket (independent of the WebDriver session — this is
  what survives UC-mode reconnects).

## 3. What gets REPLACED (delete list)

| Today | SeleniumBase equivalent |
|---|---|
| `playwright-stealth` package + arg/inject spoofing | UC mode (`Driver(uc=True)`) — sec-ch-ua, `navigator.webdriver`, permissions, plugins, WebGL, hairline fixes out of the box |
| `stealth_advanced.py` init scripts | dropped where UC covers them; audit anything UC misses (see §6) |
| Playwright launch paths in `browser_manager.py` | `SB(uc=..., user_data_dir=profile_dir, proxy=..., agent=...)`. Playwright package stays installed during migration, removed at the end |
| Window-size/viewport juggling | unchanged — still our constants, applied via CDP metrics + window flags (SB accepts `window_size`) |
| Mobile emulation | SB `mobile=True` arg, or keep our CDP `Emulation.setDeviceMetricsOverride` (already implemented — keep CDP version, it’s exact) |
| Manual captcha flows | `driver.uc_gui_click_captcha()` / `driver.uc_solve_captcha()` — auto-detects reCAPTCHA/hCaptcha checkbox & clicks with real-mouse motion |

## 4. What we KEEP custom (SB doesn’t do these)

1. **Screencast streaming** (`Page.startScreencast` w/ PNG crop pipeline).
2. **DOM capture**: the live path uses the fast serializer plus CDP
   **`Page.captureSnapshot` (MHTML)** for full-document recovery. The SB path
   does not load/import a browser extension, so it never performs extension
   discovery or service-worker retries; if the fast tier is unavailable it
   falls back to the page's `outerHTML` tier. Legacy Playwright/direct archive
   flows retain their separately configured capture behavior.
3. **Multi-session registry / reconnect / admin cast** — unaffected, they
   consume BrowserHandle.
4. **Profile persistence** — SB takes `user_data_dir`; our fingerprint
   manager keeps assigning per-user dirs (+ stores geo/proxy metadata).
5. **Proxy auth** — SB supports `proxy="user:pass@host:port"` (UC-safe).

## 5. CAPTCHA solving design

- New per-session policy: `captcha_mode = off | auto` (admin settings toggle,
  persisted in server_settings).
- Hook point: after each navigation settle + on-demand from admin. Detection:
  cheap CDP `Runtime.evaluate` probe for known iframes/widgets
  (`g-recaptcha`, `h-captcha`, `cf-turnstile`), and/or SB's own
  `driver.is_element_present("iframe[title*='recaptcha']")`.
- Solve: call (in the SB thread) `driver.uc_gui_click_captcha()` — SB moves
  a real mouse (pyautogui) and clicks the checkbox; loop max 2 attempts.
- **Expectations, stated honestly:** this reliably defeats *checkbox-style*
  challenges (reCAPTCHA v2 checkbox, hCaptcha checkbox, basic Cloudflare
  interstitial click). Image-grid hard challenges still need a solving
  service (e.g. 2captcha) — SB supports plugging one later; not in scope
  for the first cut.
- **Requirement:** real GUI session. Your Windows box is headed → works.
  On a headless Linux host, SB auto-uses Xvfb; pyautogui then targets the
  virtual display (works, worth a test).

## 6. Stealth audit — what UC mode does NOT cover (keep if needed)

- `window.screen` tailoring to the emulated phone (we set via CDP/JS
  today) — keep our inject for mobile fidelity.
- Timezone/locale alignment to proxy geo — SB has a locale arg; verify
  per-IP timezone consistency, keep a small init-script if needed.
- Canvas/WebGL **noise** injection — UC normalizes to real values; keep
  only if a creepjs audit shows drift.
- Done by re-testing after cutover (§8), not by assumption.

## 7. Phases & acceptance checks

Each phase lands behind `BROWSER_BACKEND=sb|pw` (env/settings toggle,
default `pw` until the flip) so rollback = flip a flag.

| Phase | Deliverable | Acceptance |
|---|---|---|
| **P0** | `seleniumbase` in requirements; backend factory + flag | server boots unchanged |
| **P1** | `sb_backend.py`: SBLauncher + BrowserHandle (nav/url/title/evaluate/mouse/keyboard); **PCM cut over** (screencast+input via CDP attach) | PCM fully works: navigate, click, type, frames; restart/rebind OK |
| **P2** | DOM capture via `Page.captureSnapshot` (MHTML) + keep SingleFile fallback behind flag | capture bytes ≈, no retry spam |
| **P3** | `session.py` → BrowserHandle (input forwarding, resync, admin cast); per-user `user_data_dir` profiles, proxies | full customer session E2E incl. reconnect/kick; low-CPU parity |
| **P4** | Stealth retirement: delete `playwright-stealth`, `stealth_advanced` coverage mapped to UC; residual injects only where §6 proven | nowsecure.nl ✅, creepjs fingerprint ✅ (same/better score), bot.sannysoft ✅ |
| **P5** | Captcha auto-solve (toggle) + admin “solve now” button | reCAPTCHA checkbox auto-clears on test page |
| **P6** | Remove Playwright dependency + dead launch paths; docs | fresh venv boots, matrix tests green |

Suggested pilot order inside P1: PCM first — it’s isolated, single browser,
and exercises screencast + input + rebuilds.

## 8. Verification matrix (run at P4)

- Detection: nowsecure.nl, bot.sannysoft.com, creepjs (score vs today),
  hl=g-recaptcha demo page auto-solve.
- Perf: screencast fps + stream KB/s parity; capture time; RAM with N
  sessions (must not exceed today’s budget).
- Robustness: UC-mode reconnect during navigation → our CDP attach
  survives or auto-rebinds (watchdog already exists; extend to re-attach).
- Cross-OS: your Windows headed box primary; Xvfb path smoke-tested.

## 9. Risks & mitigations

| Risk | Mitigation |
|---|---|
| SB is sync; blocking the asyncio loop | dedicated per-driver thread executor; every SB call via `to_thread` |
| UC mode reconnect cycles (driver↔browser) break attached CDP clients | our CDP socket is browser-level, not WebDriver; add re-attach watchdog (pattern exists in PCM adopt logic) |
| Extensions under UC (SingleFile) | SB sidesteps them entirely; use CDP `Page.captureSnapshot` MHTML/fast capture |
| Headless hosts | UC needs a display: Windows headed = fine; Linux → SB auto-Xvfb |
| Behavior drift (mouse/keyboard fidelity) | SB prefers Selenium/ChromeDriver W3C pointer/key actions on the same UC target, with CDP `Input.dispatch*` as a fallback; failures are logged |
| Dependency weight | SB pulls selenium + UC; net-neutral once Playwright dropped |

## 10. Config additions

```env
BROWSER_BACKEND=sb            # sb | pw (default pw until flip)
SB_UC_CAPTCHA=auto            # off | auto
SB_HEADED=true                # false -> Xvfb on Linux
LOG_LEVEL=DEBUG               # (existing) for cutover diagnosis
```

---

### TL;DR

SB UC launches the browser (stealth solved, captcha clicker included); we
point our existing raw-CDP control plane at it through the debug port.
Playwright retires after a staged, feature-flagged cutover: **PCM first
(P1), then DOM capture (P2), then customer sessions (P3), stealth audit +
captcha (P4–P5), cleanup (P6)** — each phase independently revertible.
