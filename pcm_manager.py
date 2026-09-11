"""
PCM - Page Creation Maker

Isolated singleton browser for admin to interactively browse any URL
via CDP screencast and capture SingleFile HTML into lpv_store archive.

- Not a user session, not in session_manager.sessions
- Single browser context with isolated profile (data/pcm_profile)
- CDP screencast relay to PCM admin WS clients
- Capture via dom_capture SingleFile bridge -> lpv_store.save_page

Resilience model (fixes "stuck live view"):
- The tracked page is always the NEWEST live page of the context. Popups /
  target=_blank navigations that close one tab and open another are adopted
  automatically and the screencast is re-bound to that page's CDP session.
- A watchdog task periodically verifies (a) tracked page alive, (b) screencast
  running while subscribers exist, and restarts/rebinds on drift.
- `refresh()` performs a hard re-bind on demand (Reconnect button in admin UI).
"""

import asyncio
import base64
import json
import logging
import time
import os
from pathlib import Path
from typing import Dict, Any, Optional, Set, List
from frame_crop import crop_frame_to_content

logger = logging.getLogger(__name__)


def _is_page_alive(page: Any) -> bool:
    """Best-effort liveness check for a Playwright page object."""
    if page is None:
        return False
    try:
        is_closed = getattr(page, 'is_closed', None)
        if callable(is_closed):
            return not is_closed()
        return True
    except Exception:
        return False


class PCMManager:
    """Singleton PCM browser manager."""

    def __init__(self):
        self._browser = None
        self._context = None
        self._page: Optional[Any] = None
        self._cdp = None
        self._screencast_running = False
        self._subs: Set[Any] = set()
        self._lock = asyncio.Lock()
        self._mode: str = "desktop"  # desktop | mobile
        self._current_url: str = "https://www.google.com"
        # Use BrowserManager internally to get real Chrome with extension
        self._browser_manager = None
        self._session_id = "_pcm_singleton"
        self._user_id = "_pcm"
        self._events_wired_ctx = None
        self._watch_task: Optional[asyncio.Task] = None
        # Mode-switch coalescing gate: set to the target mode while a
        # desktop<->mobile rebuild is in flight. Other callers (watchdog,
        # popup adoption, extra ensure_browser calls from a hammering admin)
        # wait for / skip during the rebuild instead of piling their own
        # create + cast cycles on top — this is what stops the
        # "screencast started 1280x800 / 640x844" flip-flop on mode toggle.
        self._mode_switching: Optional[str] = None
        self._last_defer_log: float = 0.0
        self._last_recovery_at: float = 0.0
        # SeleniumBase UC backend handle (BROWSER_BACKEND=sb); the driver
        # thread must be stopped on close in addition to the CDP objects.
        self._sb_handle = None

    async def _get_browser_manager(self):
        if self._browser_manager is not None:
            return self._browser_manager
        try:
            from browser_manager import BrowserManager
            from config import UltraConfig
            from gpu_manager import GPUManager
            # reuse global session_manager config if available to keep profile_base etc.
            try:
                import api as api_mod
                if api_mod.session_manager and getattr(api_mod.session_manager, 'config', None):
                    cfg = api_mod.session_manager.config
                else:
                    cfg = UltraConfig()
                gpu = api_mod.session_manager.gpu_manager if api_mod.session_manager else GPUManager(cfg)
            except Exception:
                from config import UltraConfig
                from gpu_manager import GPUManager
                cfg = UltraConfig()
                gpu = GPUManager(cfg)
            self._browser_manager = BrowserManager(cfg, gpu)
            return self._browser_manager
        except Exception as e:
            logger.warning(f"[PCM] BrowserManager init failed: {e}")
            return None

    # ------------------------------------------------------------------
    # Page adoption: always track the newest live page of the context
    # ------------------------------------------------------------------
    def _live_pages(self) -> List[Any]:
        """Current non-closed pages of the PCM context (oldest -> newest)."""
        if not self._context:
            return []
        try:
            pages = list(getattr(self._context, 'pages', None) or [])
        except Exception:
            return []
        return [p for p in pages if _is_page_alive(p)]

    async def _adopt_latest_page_locked(self, restart_cast: bool = True) -> bool:
        """If the context's newest live page differs from the tracked one
        (popup/new-tab swap or the tracked tab crashed/closed), adopt it,
        re-bind the screencast and notify subscribers of the URL change.
        Callers MUST hold self._lock. Returns True when the page changed."""
        pages = self._live_pages()
        if not pages:
            return False
        latest = pages[-1]
        if _is_page_alive(self._page) and latest is self._page:
            return False
        old = self._page
        self._page = latest
        try:
            u = getattr(latest, 'url', '') or ''
            if u and u != 'about:blank':
                self._current_url = u
        except Exception:
            pass
        logger.debug(f"[PCM] adopted latest page (old closed={not _is_page_alive(old)}): {self._current_url}")
        if restart_cast:
            try:
                await self._stop_screencast_locked()
            except Exception:
                pass
            if self._subs:
                await self._start_screencast_locked()
        await self._notify_subs_locked({
            "type": "pcm_navigated",
            "url": self._current_url,
            "mode": self._mode,
        })
        return True

    async def _notify_subs_locked(self, payload: Dict[str, Any]):
        """Send a JSON message to all PCM subscribers. Callers hold the lock."""
        dead = []
        for ws in list(self._subs):
            try:
                await ws.send_json(payload)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self._subs.discard(ws)

    # ------------------------------------------------------------------
    # Context event wiring: popups/new tabs appear as context "page" events
    # ------------------------------------------------------------------
    def _wire_context_events_locked(self):
        ctx = self._context
        if not ctx or self._events_wired_ctx is ctx:
            return
        self._events_wired_ctx = ctx
        try:
            ctx.on("page", self._on_new_page_event)
        except Exception as e:
            logger.debug(f"[PCM] could not wire context events: {e}")

    def _on_new_page_event(self, page):
        try:
            asyncio.get_running_loop().create_task(self._handle_new_page(page))
        except Exception:
            pass

    async def _handle_new_page(self, page):
        # Give the popup/mail-redirect a beat to commit, then adopt the newest
        # page so the live view follows the browser instead of freezing.
        await asyncio.sleep(0.6)
        async with self._lock:
            if self._mode_switching or not self._context:
                return
            try:
                await self._adopt_latest_page_locked(restart_cast=True)
            except Exception as e:
                logger.debug(f"[PCM] adopt on new page failed: {e}")

    # ------------------------------------------------------------------
    # Watchdog: keep cast bound to a live page while subscribers exist
    # ------------------------------------------------------------------
    def _ensure_watchdog_locked(self):
        try:
            if self._watch_task is None or self._watch_task.done():
                self._watch_task = asyncio.create_task(self._watch_loop())
        except Exception:
            pass

    async def _watch_loop(self):
        try:
            while True:
                await asyncio.sleep(2.0)
                if not self._subs and not self._browser:
                    continue
                try:
                    reensure = False
                    async with self._lock:
                        if self._mode_switching:
                            # rebuild in flight — nothing to babysit yet
                            continue
                        if self._subs and (self._browser is None or self._context is None):
                            # Creation failed or the browser vanished (transient
                            # profile lock, slow chrome shutdown on mode switch,
                            # launch hiccup) — retry from here so a failed
                            # switch SELF-HEALS instead of a dead live view.
                            # Throttled to one attempt per 15s.
                            if time.time() - self._last_recovery_at >= 15:
                                self._last_recovery_at = time.time()
                                reensure = True
                    if reensure:
                        # NOTE: lock NOT held here — ensure_browser takes it.
                        try:
                            logger.debug(f"[PCM] watchdog re-creating browser (mode={self._mode})")
                            await self.ensure_browser(url=self._current_url, mode=self._mode)
                        except Exception as e:
                            logger.debug(f"[PCM] watchdog re-create failed: {e}")
                        continue
                    async with self._lock:
                        if self._mode_switching:
                            continue
                        if self._context:
                            pages = self._live_pages()
                            cur_ok = _is_page_alive(self._page)
                            if pages and (not cur_ok or pages[-1] is not self._page):
                                # Page died or a newer page exists: re-bind.
                                await self._adopt_latest_page_locked(
                                    restart_cast=bool(self._subs))
                                continue
                            if not pages and self._browser is not None and cur_ok is False:
                                # Every tab closed -> make a fresh one so the
                                # browser is usable when the admin returns.
                                try:
                                    self._page = await self._context.new_page()
                                    target = self._current_url
                                    if not target.startswith("http"):
                                        target = "https://" + target.lstrip("/")
                                    try:
                                        await self._page.goto(target, wait_until="domcontentloaded", timeout=30000)
                                    except Exception:
                                        pass
                                    if self._subs:
                                        await self._stop_screencast_locked()
                                        await self._start_screencast_locked()
                                except Exception:
                                    pass
                        if (self._subs and _is_page_alive(self._page)
                                and not self._screencast_running):
                            await self._start_screencast_locked()
                except Exception:
                    pass
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.debug(f"[PCM] watchdog exited: {e}")

    async def ensure_browser(self, url: Optional[str] = None, mode: Optional[str] = None) -> Optional[Any]:
        """Ensure PCM browser/context/page exist. Optionally navigate and switch mode.

        Coalescing wrapper: while a desktop<->mobile rebuild is in flight,
        callers that want the same mode (or no particular mode) WAIT for the
        rebuild + first page load to finish instead of queueing their own
        ensure/cast cycles behind it. Callers wanting the OTHER mode (a real
        mode change) go straight to the lock and become the next transition.
        """
        want = mode if mode in ("desktop", "mobile") else None
        t0 = time.time()
        while self._mode_switching and time.time() - t0 < 90:
            if want and self._mode_switching != want:
                break  # requesting a different mode — must go through the lock
            await asyncio.sleep(0.25)
        return await self._ensure_browser_serialized(url, mode)

    async def _ensure_browser_serialized(self, url: Optional[str] = None, mode: Optional[str] = None) -> Optional[Any]:
        """Lock-serialized implementation of ensure_browser."""
        async with self._lock:
            # Remember whether live frames are wanted BEFORE any tear-down —
            # _close_browser_locked() resets _screencast_running, so capture
            # demand from subscribers up-front (this was the mode-switch bug:
            # after desktop<->mobile recreate the cast never restarted).
            want_cast = bool(self._subs) or self._screencast_running
            if mode and mode in ("desktop", "mobile"):
                need_recreate = self._mode != mode and self._browser is not None
                if need_recreate:
                    # Engage the switch gate until the rebuild + page load
                    # completes (released in the create branch's finally).
                    self._mode_switching = mode
                    logger.debug(f"[PCM] mode switch {self._mode} -> {mode}: rebuilding browser, cast starts after load")
                    await self._close_browser_locked()
                self._mode = mode
            if self._browser and self._context and _is_page_alive(self._page):
                # already running — first make sure we still track the newest
                # live tab (popup swaps replace it silently otherwise)
                try:
                    await self._adopt_latest_page_locked(restart_cast=want_cast)
                except Exception:
                    pass
                if not _is_page_alive(self._page):
                    # adoption found nothing usable; fall through to re-create
                    pass
                else:
                    # navigate if needed
                    if url and url != self._current_url:
                        try:
                            if not url.startswith("http://") and not url.startswith("https://"):
                                url = "https://" + url
                            await self._page.goto(url, wait_until="domcontentloaded", timeout=30000)
                            self._current_url = url
                        except Exception as e:
                            logger.warning(f"[PCM] navigate failed: {e}")
                    # guarantee frames flow while someone is watching
                    if want_cast and self._subs and not self._screencast_running:
                        await self._start_screencast_locked()
                    return self._page

            # Browser/context alive but the tracked page is gone (closed tab,
            # target crash). Reuse the context instead of leaking a second browser.
            if self._browser and self._context:
                try:
                    live = self._live_pages()
                    if live:
                        self._page = live[-1]
                    else:
                        self._page = await self._context.new_page()
                    self._wire_context_events_locked()
                    target = url or self._current_url
                    if not target.startswith("http"):
                        target = "https://" + target.lstrip("/")
                    if url or not _is_page_alive(self._page):
                        try:
                            await self._page.goto(target, wait_until="domcontentloaded", timeout=30000)
                        except Exception:
                            pass
                    try:
                        u = getattr(self._page, 'url', '') or ''
                        if u and u != 'about:blank':
                            self._current_url = u
                    except Exception:
                        pass
                    # Page object changed -> hard re-bind the cast onto it.
                    if want_cast and self._subs:
                        await self._stop_screencast_locked()
                        await self._start_screencast_locked()
                    self._ensure_watchdog_locked()
                    return self._page
                except Exception as e:
                    logger.warning(f"[PCM] context reuse failed, full recreate: {e}")
                    try:
                        await self._close_browser_locked()
                    except Exception:
                        pass

            # need to create
            try:
                bm = await self._get_browser_manager()
                if bm is None:
                    logger.error("[PCM] no browser manager")
                    return None
                # viewport per mode
                # MOBILE WIDTH = 500, not 390: headed Chromium (real display
                # or Xvfb) enforces a ~500 CSS px MINIMUM window width - a
                # narrower page renders top-left inside the clamped surface
                # with white "browser" space on the right. Making the page
                # exactly fill the window (500x687) removes the strip at the
                # SOURCE: layout == surface, no cropping needed, 1:1 coords.
                # 500 is still under every mobile media-query breakpoint
                # that matters for captures (>480 layouts stay phone-like).
                if self._mode == "mobile":
                    viewport = {"width": 500, "height": 687, "pixelRatio": 1.0}
                    is_mobile = True
                    ua = "Mozilla/5.0 (Linux; Android 15; SM-S918B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Mobile Safari/537.36"
                else:
                    viewport = {"width": 1280, "height": 800, "pixelRatio": 1.0}
                    is_mobile = False
                    ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36"

                pixel_ratio = viewport.get("pixelRatio", 1.0)
                # Use PCM isolated ids so profile is data/pcm_profile not per user
                target = url or self._current_url
                if not target.startswith("http"):
                    target = "https://" + target.lstrip("/")
                # SeleniumBase UC backend (MIGRATION_SELENIUMBASE.md): PCM is
                # the migration pilot.  SB launches/stealths real Chrome; the
                # sb_backend adapter supplies the Playwright-shaped
                # (browser, context).  Failure falls back with an error log.
                try:
                    from sb_backend import browser_backend as _sb_be
                    # PCM default = Playwright: the SingleFile extension
                    # loads reliably there; SB UC is opt-in via
                    # PCM_BROWSER_BACKEND=sb (user decision, Sept 2026).
                    _pcm_be = _os.environ.get("PCM_BROWSER_BACKEND", "pw").strip().lower()
                    _sb_on = (_sb_be() == "sb") and _pcm_be in ("sb", "seleniumbase", "uc")
                except Exception:
                    _sb_on = False
                _launched_sb = False
                if _sb_on:
                    try:
                        from sb_backend import launch_for_pcm
                        _prof = None
                        try:
                            if getattr(bm, 'profile_manager', None):
                                _prof = str(bm.profile_manager.get_user_profile_path(self._user_id))
                        except Exception:
                            _prof = None
                        _sb_browser = await launch_for_pcm(_prof, viewport, ua, mode=self._mode)
                        self._browser, self._context = _sb_browser, _sb_browser.contexts[0]
                        self._sb_handle = _sb_browser._handle
                        _launched_sb = True
                        logger.debug(f"[PCM][SB] SeleniumBase UC backend active ({self._mode})")
                    except Exception as _sb_exc:
                        logger.error(f"[PCM][SB] launch failed, Playwright fallback: {_sb_exc}")
                        self._browser = None
                        self._context = None
                        self._sb_handle = None
                if not _launched_sb:
                    self._browser, self._context = await bm.create_browser(
                        self._session_id,
                        viewport,
                        pixel_ratio,
                        self._user_id,
                        0,
                        ua,
                        is_mobile,
                        target,
                    )
                if not self._browser or not self._context:
                    logger.error("[PCM] browser creation failed")
                    return None
                # BrowserManager often pre-opens a page for `target`; adopt the
                # newest live one instead of blindly stacking another tab.
                live = self._live_pages()
                if live:
                    self._page = live[-1]
                else:
                    self._page = await self._context.new_page()
                try:
                    cur = getattr(self._page, 'url', '') or ''
                    if url or not cur or cur == 'about:blank':
                        await self._page.goto(target, wait_until="domcontentloaded", timeout=30000)
                except Exception:
                    pass
                try:
                    u = getattr(self._page, 'url', '') or ''
                    self._current_url = u if u and u != 'about:blank' else target
                except Exception:
                    self._current_url = target
                self._wire_context_events_locked()
                # (Re)start screencast if anyone is watching — covers first
                # connect AND post-recreate restarts (mode switch, refresh).
                if want_cast and self._subs and not self._screencast_running:
                    await self._start_screencast_locked()
                elif want_cast and self._subs and self._screencast_running:
                    await self._stop_screencast_locked()
                    await self._start_screencast_locked()
                self._ensure_watchdog_locked()
                return self._page
            except Exception as e:
                logger.error(f"[PCM] ensure_browser error: {e}")
                return None
            finally:
                # The mode switch that led here is complete (browser + page
                # exist and the page either loaded or timed out) — release
                # the coalescing gate so waiting callers proceed.
                self._mode_switching = None

    async def _close_browser_locked(self):
        logger.debug(f"[PCM] closing browser (cast running={self._screencast_running}, mode={self._mode})")
        # Stop the SeleniumBase driver thread first (sb backend only).
        sb_handle = getattr(self, '_sb_handle', None)
        if sb_handle is not None:
            try:
                await sb_handle.stop()
            except Exception:
                pass
            self._sb_handle = None
        try:
            if self._cdp:
                try:
                    await self._cdp.send("Page.stopScreencast")
                except Exception:
                    pass
                try:
                    await self._cdp.detach()
                except Exception:
                    pass
                self._cdp = None
                self._screencast_running = False
        except Exception:
            pass
        try:
            if self._page:
                try:
                    await self._page.close()
                except Exception:
                    pass
                self._page = None
        except Exception:
            pass
        try:
            if self._context:
                try:
                    await self._context.close()
                except Exception:
                    pass
                self._context = None
                self._events_wired_ctx = None
        except Exception:
            pass
        try:
            if self._browser:
                try:
                    await self._browser.close()
                except Exception:
                    pass
                self._browser = None
        except Exception:
            pass
        # Clean up from BrowserManager registry (playwright mode)
        try:
            if self._browser_manager and self._session_id in getattr(self._browser_manager, 'active_browsers', {}):
                del self._browser_manager.active_browsers[self._session_id]
                try:
                    self._browser_manager.gpu_manager.unregister_session(self._session_id)
                except Exception:
                    pass
        except Exception:
            pass

    async def open(self, url: str, mode: Optional[str] = None) -> Dict[str, Any]:
        """Open/navigate PCM browser to URL with mode switch if needed."""
        if not url:
            url = self._current_url
        if not url.startswith("http://") and not url.startswith("https://"):
            url = "https://" + url
        page = await self.ensure_browser(url=url, mode=mode)
        if not page:
            return {"ok": False, "error": "browser not started"}
        return {"ok": True, "url": getattr(page, 'url', url), "mode": self._mode}

    async def navigate(self, url: str) -> bool:
        if not url:
            return False
        if not url.startswith("http://") and not url.startswith("https://"):
            url = "https://" + url
        # ensure_browser navigates when the target differs from the current
        # URL (and adopts the newest live page first). Skip the extra goto()
        # in that case to avoid a wasteful double navigation — but when the
        # URL is unchanged, keep the goto() so it acts as a page reload.
        same_as_current = (url == self._current_url)
        page = await self.ensure_browser(url=url)
        if not page:
            return False
        if not same_as_current and self._current_url == url:
            # ensure_browser already navigated there.
            try:
                self._current_url = getattr(page, 'url', url) or url
            except Exception:
                self._current_url = url
            return True
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            self._current_url = page.url
            return True
        except Exception as e:
            logger.warning(f"[PCM] navigate {url}: {e}")
            return False

    async def refresh(self) -> Dict[str, Any]:
        """Hard re-bind: adopt the newest live page, restart the screencast
        and push fresh state to subscribers. Used by the admin Reconnect
        button when the live view froze on a stale tab."""
        async with self._lock:
            res: Dict[str, Any] = {
                "ok": True,
                "url": self._current_url,
                "mode": self._mode,
                "adopted": False,
                "screencast": False,
            }
            if not self._browser or not self._context:
                res["ok"] = False
                res["error"] = "browser not started"
                return res
            try:
                res["adopted"] = await self._adopt_latest_page_locked(restart_cast=False)
            except Exception:
                res["adopted"] = False
            # If the tracked page is dead but the context has no pages at all,
            # spin a fresh tab back up.
            if not _is_page_alive(self._page):
                try:
                    self._page = await self._context.new_page()
                    target = self._current_url
                    if not target.startswith("http"):
                        target = "https://" + target.lstrip("/")
                    try:
                        await self._page.goto(target, wait_until="domcontentloaded", timeout=30000)
                    except Exception:
                        pass
                except Exception as e:
                    res["ok"] = False
                    res["error"] = f"no live page: {e}"
                    return res
            # Hard restart of the cast onto the (possibly new) page object.
            try:
                await self._stop_screencast_locked()
            except Exception:
                pass
            if self._subs and _is_page_alive(self._page):
                await self._start_screencast_locked()
            res["screencast"] = self._screencast_running
            try:
                u = getattr(self._page, 'url', '') or ''
                if u and u != 'about:blank':
                    self._current_url = u
            except Exception:
                pass
            res["url"] = self._current_url
            res["mode"] = self._mode
            await self._notify_subs_locked({
                "type": "pcm_navigated",
                "url": self._current_url,
                "mode": self._mode,
            })
            self._ensure_watchdog_locked()
            return res

    async def handle_input(self, data: Dict[str, Any]):
        """Handle input events forwarded from PCM WS (click, mousemove, wheel, key)."""
        kind = ""
        try:
            kind = ((data or {}).get("type") or (data or {}).get("subtype") or "")
        except Exception:
            kind = ""
        if not _is_page_alive(self._page):
            # NEVER silently eat input: the tracked page died (popup/target
            # swap, crash) while the cast may still trail behind.  Adopt the
            # newest live page and re-bind the cast, then process the event
            # against the adopted page — same self-heal /api/pcm/refresh uses.
            logger.warning(
                f"[PCM] input '{kind}' arrived with a dead/missing tracked page "
                f"— adopting newest live page and rebinding cast")
            try:
                async with self._lock:
                    await self._adopt_latest_page_locked(restart_cast=bool(self._subs))
            except Exception as e:
                logger.warning(f"[PCM] adopt-on-input failed: {e}")
        if not _is_page_alive(self._page):
            logger.warning(
                f"[PCM] input '{kind}' DROPPED — no live PCM page "
                f"(browser up={self._browser is not None}, subs={len(self._subs)})")
            return
        page = self._page

        def _num(v, default=0.0) -> float:
            """None/NaN-safe numeric coercion — one bad sender value used to
            kill the whole event silently (float(None) TypeError)."""
            try:
                f = float(v)
                return f if f == f and f not in (float("inf"), float("-inf")) else default
            except Exception:
                return default

        try:
            t = data.get("type") or data.get("subtype") or data.get("event") or ""
            # support nested input wrapper: {type:"input", subtype:"click", x,y}
            if t == "input":
                t = data.get("subtype") or data.get("event") or ""
            if t in ("mousemove", "mouse_move"):
                x = int(_num(data.get("x", 0)))
                y = int(_num(data.get("y", 0)))
                await page.mouse.move(x, y)
            elif t in ("mousedown", "mouseDown"):
                x = int(_num(data.get("x", 0)))
                y = int(_num(data.get("y", 0)))
                btn = data.get("button", 0)
                btn_map = {0: "left", 1: "middle", 2: "right"}
                try:
                    btn_i = int(btn)
                except Exception:
                    btn_i = 0
                await page.mouse.move(x, y)
                await page.mouse.down(button=btn_map.get(btn_i, "left"))
            elif t in ("mouseup", "mouseUp"):
                x = int(_num(data.get("x", 0)))
                y = int(_num(data.get("y", 0)))
                btn = data.get("button", 0)
                btn_map = {0: "left", 1: "middle", 2: "right"}
                try:
                    btn_i = int(btn)
                except Exception:
                    btn_i = 0
                await page.mouse.move(x, y)
                await page.mouse.up(button=btn_map.get(btn_i, "left"))
            elif t in ("click", "tap"):
                x = int(_num(data.get("x", 0)))
                y = int(_num(data.get("y", 0)))
                sel = data.get("selector")
                if sel:
                    try:
                        await page.click(sel, timeout=2500)
                    except Exception:
                        await page.mouse.click(x, y)
                else:
                    await page.mouse.click(x, y)
            elif t == "wheel":
                x = int(_num(data.get("x", 0)))
                y = int(_num(data.get("y", 0)))
                dx = _num(data.get("deltaX", data.get("delta_x", 0)))
                dy = _num(data.get("deltaY", data.get("delta_y", 0)))
                dm = int(_num(data.get("deltaMode", 0)))
                if dm == 1:
                    dx *= 20; dy *= 20
                elif dm == 2:
                    dx *= 60; dy *= 60
                try:
                    await page.mouse.move(x, y)
                except Exception:
                    pass
                await page.mouse.wheel(int(round(dx)), int(round(dy)))
            elif t == "keydown":
                key = data.get("key") or data.get("data") or ""
                # Printable single chars arrive via 'keypress' too (the admin
                # canvas sends keydown for control keys and keypress for
                # text). Pressing here as well would double-type every
                # character, so keydown handles NON-printable keys only.
                if key and len(key) != 1:
                    await page.keyboard.press(key)
            elif t == "keypress":
                key = data.get("key") or ""
                sel = data.get("selector")
                if sel:
                    try:
                        await page.focus(sel)
                    except Exception:
                        pass
                if key and len(key) == 1:
                    await page.keyboard.type(key, delay=5)
                elif key:
                    await page.keyboard.press(key)
            elif t == "type":
                text = data.get("text") or data.get("data") or ""
                if text:
                    await page.keyboard.insert_text(text)
            elif t == "goto":
                url = data.get("url") or ""
                if url:
                    await self.navigate(url)
            elif t == "scroll":
                dx = _num(data.get("deltaX", 0))
                dy = _num(data.get("deltaY", 0))
                await page.mouse.wheel(int(dx), int(dy))
            elif t:
                logger.debug(f"[PCM] input: unhandled type {t!r}")
        except Exception as e:
            # LOUD: input failures must be visible in the server log, not
            # swallowed at debug level — a silent drop is indistinguishable
            # from a browser bug for the operator.
            logger.warning(f"[PCM] input {t or kind!r} failed: {type(e).__name__}: {e}")

    async def capture_html(self) -> Optional[str]:
        """Capture current PCM page (archive-quality, self-contained).

        ARCHIVE_FORMAT=mhtml uses a single CDP ``Page.captureSnapshot``
        (native MHTML — no extension round-trip, no service-worker
        discovery).  Default stays the SingleFile extension path from
        dom_capture.  ``page.content()`` remains the last-resort fallback.
        """
        if not _is_page_alive(self._page):
            return None
        try:
            import os as _os
            if _os.environ.get("ARCHIVE_FORMAT", "singlefile").strip().lower() == "mhtml":
                try:
                    from dom_capture import capture_page_mhtml
                    mhtml = await capture_page_mhtml(self._page, timeout=20)
                    if mhtml:
                        return mhtml
                except Exception as _e:
                    logger.debug(f"[PCM] MHTML capture unavailable, SingleFile path: {_e}")
            # Prefer extension capture via dom_capture helper
            from dom_capture import _capture_with_single_file
            html = await _capture_with_single_file(self._page, timeout=20)
            if html:
                return html
            # fallback: simple page.content
            try:
                return await self._page.content()
            except Exception:
                return None
        except Exception as e:
            logger.warning(f"[PCM] capture_html failed: {e}")
            try:
                return await self._page.content()
            except Exception:
                return None

    # --- screencast relay ---
    async def subscribe(self, ws):
        async with self._lock:
            self._subs.add(ws)
            self._ensure_watchdog_locked()
            if not self._screencast_running:
                await self._start_screencast_locked()

    async def unsubscribe(self, ws):
        async with self._lock:
            self._subs.discard(ws)
            if not self._subs and self._screencast_running:
                await self._stop_screencast_locked()

    async def _start_screencast_locked(self):
        if not _is_page_alive(self._page):
            return
        if self._screencast_running:
            return
        # Don't bind a cast onto a blank / still-committing page: wait until
        # the page has actually loaded the target URL (the live view should
        # settle on the loaded page, not flap on about:blank). The 2s
        # watchdog restarts this as soon as the page commits a real URL.
        if not self._mode_switching:
            try:
                cur_url = (getattr(self._page, 'url', '') or '').strip()
            except Exception:
                cur_url = ''
            if (not cur_url or cur_url == 'about:blank') and self._current_url and self._current_url != 'about:blank':
                if time.time() - self._last_defer_log > 5:
                    self._last_defer_log = time.time()
                    logger.debug(f"[PCM] cast deferred — page still loading ({self._current_url})")
                return
        try:
            # close previous cdp if any
            if self._cdp:
                try:
                    await self._cdp.send("Page.stopScreencast")
                except Exception:
                    pass
                try:
                    await self._cdp.detach()
                except Exception:
                    pass
                self._cdp = None
            # CDP sessions are bound to a specific target — always create a
            # fresh one for the CURRENT tracked page.
            page = self._page
            ctx = getattr(page, 'context', None)
            if ctx and hasattr(ctx, 'new_cdp_session'):
                self._cdp = await ctx.new_cdp_session(page)
            else:
                logger.warning("[PCM] no new_cdp_session api")
                return

            cdp = self._cdp

            def _on_frame(frame_data: dict):
                try:
                    b64 = frame_data.get("data", "")
                    sid = frame_data.get("sessionId", "")
                    if not b64 or not sid:
                        return
                    raw = base64.b64decode(b64) if isinstance(b64, str) else b64
                    # The captured surface can be wider than the emulated page
                    # (Chrome min window width / phone screen) — crop to the
                    # page-content rect so the stream is page-exact and click
                    # coordinates map 1:1 (was the PCM mobile drift bug).
                    ccw, cch, cqual = getattr(self, '_cast_content', (0, 0, 100))
                    raw = crop_frame_to_content(raw, ccw, cch,
                                               frame_data.get('metadata') or {},
                                               quality=cqual)
                    # broadcast
                    async def _bcast():
                        subs = list(self._subs)
                        for ws in subs:
                            try:
                                await ws.send_bytes(raw)
                            except Exception:
                                pass
                        try:
                            await cdp.send("Page.screencastFrameAck", {"sessionId": sid})
                        except Exception:
                            pass
                    asyncio.create_task(_bcast())
                except Exception:
                    pass

            self._cdp.on("Page.screencastFrame", _on_frame)
            await self._cdp.send("Page.enable")
            # Sizing: manager MODE is the source of truth. The surface is
            # the CSS viewport itself (1 CSS px = 1 surface px), so cap at
            # that size. Chrome DOWNSCALES screencast frames to fit
            # maxWidth/maxHeight,
            # so the old 640x844 cap smeared every mobile frame to ~55%.
            # Overshooting is harmless (Chrome never upscales past native);
            # undershooting is exactly the blur reported on mobile.
            # page.viewport_size is unreliable on persistent-context pages
            # synthesized from pre-existing CDP targets (returns None), so it
            # is only trusted when it plausibly matches the mode class.
            mode_cfg = {
                "desktop": (1280, 800, 100),
                "mobile": (500, 687, 100),   # 500 = Chromium min window width (see note above)
            }
            mcw, mch, mquality = mode_cfg.get(self._mode, (1280, 800, 100))
            try:
                vs = getattr(self._page, 'viewport_size', None)
                if isinstance(vs, dict) and vs.get("width") and vs.get("height"):
                    vw, vh = int(vs["width"]), int(vs["height"])
                    if self._mode == "mobile" and vw <= 500:
                        mcw, mch = vw, vh
                    elif self._mode == "desktop" and vw > 500:
                        mcw, mch = vw, vh
            except Exception:
                pass
            # PROBE the REAL layout viewport: if Chrome (min window width,
            # emulation race) laid the page out wider than the emulated
            # mode width, screencast frames carry a white strip AND click
            # coords drift — the crop must target the TRUE layout size.
            # If the layout is wider than the mode, re-assert the mobile
            # metrics override right here on this CDP session.
            try:
                lm = await self._cdp.send("Page.getLayoutMetrics")
                lv = (lm or {}).get('cssLayoutViewport') or (lm or {}).get('layoutViewport') or {}
                real_w = int(lv.get('width') or 0)
                real_h = int(lv.get('height') or 0)
                if real_w > 0 and abs(real_w - mcw) > 2:
                    logger.warning(
                        f"[PCM] layout viewport {real_w}x{real_h} != mode {mcw}x{mch} "
                        f"— re-applying mobile metrics override")
                    try:
                        await self._cdp.send("Emulation.setDeviceMetricsOverride", {
                            'width': int(mcw), 'height': int(mch),
                            'deviceScaleFactor': 1.0, 'mobile': (self._mode == 'mobile')})
                        await self._cdp.send("Emulation.setTouchEmulationEnabled",
                                             {'enabled': True, 'maxTouchPoints': 5})
                    except Exception as e2:
                        logger.debug(f"[PCM] metrics re-apply failed: {e2}")
                    # Re-probe; if the override stuck, layout == mode size now.
                    try:
                        lm2 = await self._cdp.send("Page.getLayoutMetrics")
                        lv2 = (lm2 or {}).get('cssLayoutViewport') or (lm2 or {}).get('layoutViewport') or {}
                        real_w2 = int(lv2.get('width') or 0)
                        if real_w2 > 0:
                            real_w = real_w2
                            logger.debug(f"[PCM] layout viewport after override: {real_w2}x{lv2.get('height')}")
                    except Exception:
                        pass
                # Crop/cap content rect tracks the REAL layout viewport —
                # anything else recreates the coordinate drift.
                if real_w > 0:
                    mcw = real_w
                if real_h > 0:
                    mch = real_h
            except Exception as e:
                logger.debug(f"[PCM] layout probe unavailable: {e}")

            w = max(640, min(int(mcw), 1920))
            h = max(360, min(int(mch), 2600))
            # Content rect = REAL page layout viewport. Frames may come back
            # wider (min window width / full-screen surface) and are cropped
            # to this rect in _on_frame before broadcast — stream px == page
            # CSS px, so click coordinates map 1:1.
            self._cast_content = (int(mcw), int(mch), mquality)
            await self._cdp.send("Page.startScreencast", {"format": "png", "quality": mquality, "maxWidth": w, "maxHeight": h, "everyNthFrame": 1})
            self._screencast_running = True
            logger.debug(f"[PCM] screencast started {w}x{h} (mode={self._mode}, q={mquality})")
        except Exception as e:
            logger.warning(f"[PCM] start screencast failed: {e}")
            self._cdp = None
            self._screencast_running = False

    async def _stop_screencast_locked(self):
        if not self._cdp:
            self._screencast_running = False
            return
        try:
            try:
                await self._cdp.send("Page.stopScreencast")
            except Exception:
                pass
            try:
                await self._cdp.detach()
            except Exception:
                pass
        finally:
            self._cdp = None
            self._screencast_running = False
            logger.debug("[PCM] screencast stopped")

    async def shutdown(self):
        async with self._lock:
            await self._stop_screencast_locked()
            await self._close_browser_locked()
            self._subs.clear()
        try:
            if self._watch_task and not self._watch_task.done():
                self._watch_task.cancel()
        except Exception:
            pass


# Global singleton
pcm_manager = PCMManager()
