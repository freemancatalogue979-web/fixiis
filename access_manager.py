"""Operator-controlled Access browser sessions.

Access is deliberately separate from the singleton Page Creation Maker (PCM)
browser and from public client sessions.  Every selected profile gets one
SeleniumBase/UC browser, one Playwright-shaped context adapter, one page/CDP
session, one screencast subscriber set, and (on a display-less Linux host) a
private Xvfb owned by SeleniumBase.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import sys
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit
from typing import Any, Dict, Optional, Set

logger = logging.getLogger(__name__)


def _normalize_mouse_button(value: Any) -> str:
    """Normalize browser/DOM mouse-button values for Playwright input APIs."""
    if isinstance(value, str) and value.lower() in ("left", "middle", "right"):
        return value.lower()
    try:
        return {0: "left", 1: "middle", 2: "right"}.get(int(value), "left")
    except Exception:
        return "left"


class AccessSession:
    def __init__(self, access_id: str, user_id: str, profile_dir: str,
                 url: str = "https://www.google.com") -> None:
        self.access_id = access_id
        self.user_id = user_id
        self.profile_dir = profile_dir
        self.current_url = url
        self.browser = None
        self.context = None
        self.page = None
        self.cdp = None
        self.subscribers: Set[Any] = set()
        # Playwright keeps mouse button state on the page. Track it per
        # operator socket so a dropped/switching Access tab can always release
        # the buttons it pressed instead of leaving a remote drag stuck.
        self._pressed_mouse_buttons: Dict[Any, Set[str]] = {}
        self.lock = asyncio.Lock()
        self.cast_running = False
        self.created_at = time.time()
        self.last_activity = self.created_at
        self._cast_width = 1280
        self._cast_height = 720
        # Keep CDP acknowledgements independent from browser/operator network
        # latency. A one-item latest-frame queue prevents slow admin sockets
        # from building an unbounded task backlog while preserving every
        # delivered frame's original lossless PNG quality.
        self._frame_queue: Optional[asyncio.Queue] = None
        self._frame_worker: Optional[asyncio.Task] = None
        self._frame_ack_tasks: Set[asyncio.Task] = set()

    @property
    def alive(self) -> bool:
        try:
            return bool(self.page and not self.page.is_closed())
        except Exception:
            return bool(self.page)

    async def _load_supplemental_cookies(self, target_url: str) -> None:
        """Merge the profile's optional cookies.json into the live context.

        The Chromium user-data directory remains the source of persistent
        state.  cookies.json is only the server's compatibility snapshot (for
        example, a public session may have saved cookies before Access takes
        over), so it is never used instead of the profile directory.
        """
        try:
            cookie_file = Path(self.profile_dir) / "cookies.json"
            if not cookie_file.is_file() or self.context is None:
                return
            raw = json.loads(cookie_file.read_text(encoding="utf-8"))
            if isinstance(raw, dict) and isinstance(raw.get("cookies"), list):
                records = raw["cookies"]
            elif isinstance(raw, list):
                records = raw
            elif isinstance(raw, dict):
                records = []
                for domain, group in raw.items():
                    if not isinstance(group, dict) or not isinstance(group.get("cookies"), list):
                        continue
                    for item in group["cookies"]:
                        if isinstance(item, dict):
                            item = dict(item)
                            item.setdefault("domain", domain)
                            records.append(item)
            else:
                records = []
            cookies = []
            for source in records:
                if not isinstance(source, dict) or not source.get("name") or "value" not in source:
                    continue
                cookie = {
                    key: source[key]
                    for key in (
                        "name", "value", "domain", "path", "secure", "httpOnly",
                        "expires", "expiry", "sameSite", "url",
                    )
                    if key in source and source[key] is not None
                }
                if "expires" not in cookie and "expiry" in cookie:
                    cookie["expires"] = cookie.pop("expiry")
                if isinstance(cookie.get("sameSite"), str):
                    same_site = cookie["sameSite"].capitalize()
                    if same_site in {"Strict", "Lax", "None"}:
                        cookie["sameSite"] = same_site
                    else:
                        cookie.pop("sameSite", None)
                if cookie.get("domain"):
                    cookie.pop("url", None)
                elif not cookie.get("url"):
                    cookie["url"] = target_url
                cookies.append(cookie)
            if cookies:
                await self.context.add_cookies(cookies)
        except Exception:
            logger.debug("[Access] supplemental cookies could not be loaded", exc_info=True)

    async def ensure_browser(self, url: Optional[str] = None) -> Any:
        async with self.lock:
            if self.alive:
                if url and url != self.current_url:
                    await self.page.goto(url, wait_until="domcontentloaded", timeout=30000)
                    self.current_url = getattr(self.page, "url", url) or url
                return self.page
            if self.browser is not None:
                try:
                    await self.browser.close()
                except Exception:
                    pass
                self.browser = None

            from sb_backend import launch_for_access, clean_profile_locks, kill_profile_processes

            if self.profile_dir:
                kill_profile_processes(self.profile_dir)
                clean_profile_locks(self.profile_dir)

            # Access is intentionally SeleniumBase-only.  SBHandle creates a
            # private Xvfb on Linux and restores the parent DISPLAY immediately
            # after Driver construction; Windows/macOS retain native headed
            # behavior without an Xvfb requirement.
            self.browser = await launch_for_access(
                self.profile_dir,
                {"width": 1280, "height": 720},
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 "
                "Safari/537.36",
                headless=False,
            )
            self.context = self.browser.contexts[0]
            try:
                self.context.on("page", self._on_new_page)
            except Exception:
                pass
            self.page = await self.context.new_page()
            self._wire_page_events(self.page)
            target = url or self.current_url or "https://www.google.com"
            if not target.startswith(("http://", "https://")):
                target = "https://" + target.lstrip("/")
            await self._load_supplemental_cookies(target)
            try:
                await self.page.goto(target, wait_until="domcontentloaded", timeout=30000)
            except Exception as exc:
                logger.warning("[Access] initial navigation failed for %s: %s", self.user_id, exc)
            self.current_url = getattr(self.page, "url", target) or target
            try:
                parts = urlsplit(self.current_url)
                origin = f"{parts.scheme}://{parts.netloc}" if parts.scheme and parts.netloc else None
                if origin:
                    await self.context.grant_permissions(["clipboard-read", "clipboard-write"], origin=origin)
            except Exception:
                # Clipboard permissions are origin/enterprise-policy dependent;
                # the input path still works and reports denial to the UI.
                pass
            return self.page

    def _wire_page_events(self, page: Any) -> None:
        try:
            page.on("close", self._on_page_closed)
        except Exception:
            pass

    def _on_page_closed(self, page: Any) -> None:
        async def recover() -> None:
            await asyncio.sleep(0.1)
            async with self.lock:
                if page is not self.page:
                    return
                await self._release_mouse_buttons_locked()
                candidates = []
                try:
                    candidates = [
                        item for item in (getattr(self.context, "pages", None) or [])
                        if item is not page and not item.is_closed()
                    ]
                except Exception:
                    candidates = []
                was_cast = self.cast_running
                if was_cast:
                    await self._stop_cast_locked()
                if candidates:
                    self.page = candidates[-1]
                    self._wire_page_events(self.page)
                    self.current_url = getattr(self.page, "url", self.current_url) or self.current_url
                    if was_cast and self.subscribers:
                        await self._start_cast_locked()
                else:
                    self.page = None
        try:
            asyncio.create_task(recover())
        except Exception:
            pass

    def _on_new_page(self, page: Any) -> None:
        """Follow a popup/new tab so the Access live view stays on the visible tab."""
        async def adopt() -> None:
            await asyncio.sleep(0.2)
            async with self.lock:
                if page is self.page:
                    return
                try:
                    if page.is_closed():
                        return
                except Exception:
                    pass
                await self._release_mouse_buttons_locked()
                self._wire_page_events(page)
                was_cast = self.cast_running
                if was_cast:
                    await self._stop_cast_locked()
                self.page = page
                self.current_url = getattr(page, "url", self.current_url) or self.current_url
                if was_cast and self.subscribers:
                    await self._start_cast_locked()
        try:
            asyncio.create_task(adopt())
        except Exception:
            pass

    async def _release_mouse_buttons_locked(self, websocket: Any = None) -> None:
        """Release buttons owned by a socket before it disappears.

        This runs while ``self.lock`` is held, so a disconnect, tab switch, or
        explicit session close cannot race a final mouseup with a new input
        event. The bookkeeping is cleared even when Playwright reports a
        closed page; a later browser/page recovery starts with no stale state.
        """
        if websocket is None:
            owned = set().union(*self._pressed_mouse_buttons.values()) if self._pressed_mouse_buttons else set()
            self._pressed_mouse_buttons.clear()
        else:
            owned = self._pressed_mouse_buttons.pop(websocket, set())
        if not owned or not self.alive:
            return
        for button in tuple(owned):
            try:
                await self.page.mouse.up(button=button)
            except Exception:
                logger.debug("[Access] could not release %s mouse button", button, exc_info=True)

    async def close(self) -> None:
        async with self.lock:
            await self._release_mouse_buttons_locked()
            for ws in list(self.subscribers):
                try:
                    await ws.send_json({"type": "access_replaced", "reason": "profile_taken_over"})
                except Exception:
                    pass
                try:
                    await ws.close(code=4004, reason="profile_taken_over")
                except Exception:
                    pass
            await self._stop_cast_locked()
            if self.page is not None:
                try:
                    await self.page.close()
                except Exception:
                    pass
            self.page = None
            self.context = None
            if self.browser is not None:
                try:
                    await self.browser.close()
                except Exception:
                    pass
            self.browser = None
            if self.profile_dir:
                try:
                    from sb_backend import clean_profile_locks
                    clean_profile_locks(self.profile_dir)
                except Exception:
                    pass
            self.subscribers.clear()
            self._pressed_mouse_buttons.clear()

    async def subscribe(self, websocket: Any) -> None:
        async with self.lock:
            self.subscribers.add(websocket)
            self._pressed_mouse_buttons.setdefault(websocket, set())
            await self._start_cast_locked()

    async def unsubscribe(self, websocket: Any) -> None:
        async with self.lock:
            await self._release_mouse_buttons_locked(websocket)
            self.subscribers.discard(websocket)
            # Keep the persistent browser alive, but stop the image stream
            # when no admin tab is watching. Re-selecting the Access tab
            # rebinds a fresh cast without relaunching Chrome.
            if not self.subscribers and self.cast_running:
                await self._stop_cast_locked()

    async def _stop_cast_if_unwatched(self, cast_cdp: Any) -> None:
        """Stop a cast after relay-level send failures remove its last viewer."""
        async with self.lock:
            if self.cdp is not cast_cdp or self.subscribers:
                return
            # This helper can be called by the relay worker itself. Detach its
            # worker slot before the common stop path so it does not cancel and
            # await itself.
            if self._frame_worker is asyncio.current_task():
                self._frame_worker = None
            await self._stop_cast_locked()

    async def _relay_frames(self, cast_cdp: Any, queue: asyncio.Queue) -> None:
        """Relay the newest lossless frame without serializing CDP acks.

        CDP screencast flow is ack-driven. A slow WebSocket must not hold the
        ack, otherwise Chrome stops producing frames and the operator sees a
        frozen view. The queue deliberately keeps only the newest PNG; it can
        skip intermediate frames under load, but it never recompresses or
        resizes the frame that reaches the operator.
        """
        try:
            last_send_time = 0.0
            min_interval = 1.0 / 30.0  # Rate-limit to ~30 FPS to prevent TCP buffer bloat & lag
            while True:
                raw = await queue.get()
                if not self.cast_running or self.cdp is not cast_cdp:
                    continue

                # Drain queue so stale frames are skipped and only freshest frame is broadcast
                while not queue.empty():
                    try:
                        raw = queue.get_nowait()
                    except asyncio.QueueEmpty:
                        break

                now = time.monotonic()
                to_sleep = min_interval - (now - last_send_time)
                if to_sleep > 0:
                    await asyncio.sleep(to_sleep)
                    while not queue.empty():
                        try:
                            raw = queue.get_nowait()
                        except asyncio.QueueEmpty:
                            break

                last_send_time = time.monotonic()
                subscribers = list(self.subscribers)
                if not subscribers:
                    await self._stop_cast_if_unwatched(cast_cdp)
                    return

                async def _send_to_ws(ws: WebSocket) -> bool:
                    try:
                        await asyncio.wait_for(ws.send_bytes(raw), timeout=0.25)
                        return True
                    except Exception:
                        return False

                results = await asyncio.gather(
                    *(_send_to_ws(ws) for ws in subscribers),
                    return_exceptions=True,
                )
                failed = [
                    ws for ws, result in zip(subscribers, results)
                    if result is not True
                ]
                if failed:
                    async with self.lock:
                        for ws in failed:
                            await self._release_mouse_buttons_locked(ws)
                            self.subscribers.discard(ws)
                if not self.subscribers:
                    await self._stop_cast_if_unwatched(cast_cdp)
                    return
        except asyncio.CancelledError:
            raise

    async def _start_cast_locked(self) -> None:
        if self.cast_running or not self.alive or not self.subscribers:
            return
        ctx = getattr(self.page, "context", None)
        if ctx is None or not hasattr(ctx, "new_cdp_session"):
            raise RuntimeError("Access page has no CDP session adapter")
        cdp = await ctx.new_cdp_session(self.page)
        self.cdp = cdp
        cast_cdp = cdp
        frame_queue: asyncio.Queue = asyncio.Queue(maxsize=1)
        self._frame_queue = frame_queue

        def schedule_ack(sid: Any) -> None:
            async def acknowledge() -> None:
                try:
                    await cast_cdp.send("Page.screencastFrameAck", {"sessionId": sid})
                except Exception:
                    pass
            try:
                task = asyncio.create_task(acknowledge())
                self._frame_ack_tasks.add(task)
                task.add_done_callback(self._frame_ack_tasks.discard)
            except Exception:
                pass

        def on_frame(frame: Dict[str, Any]) -> None:
            data = frame.get("data") or ""
            sid = frame.get("sessionId")
            if not data or not sid:
                return
            if not self.cast_running or self.cdp is not cast_cdp or self._frame_queue is not frame_queue:
                return
            # Schedule the CDP acknowledgement before doing any frame decode
            # or queue work. The browser can continue producing frames even if
            # decoding or a subscriber send is momentarily slow.
            schedule_ack(sid)
            try:
                raw = base64.b64decode(data) if isinstance(data, str) else data
            except Exception:
                return
            # Latest-wins queue: acknowledge every valid frame immediately,
            # while the relay sends at most one frame at a time per socket.
            try:
                while True:
                    frame_queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
            try:
                frame_queue.put_nowait(raw)
            except asyncio.QueueFull:
                pass

        try:
            cdp.on("Page.screencastFrame", on_frame)
            await cdp.send("Page.enable")
            try:
                await cdp.send("Emulation.setDeviceMetricsOverride", {
                    "width": 1280,
                    "height": 720,
                    "deviceScaleFactor": 1,
                    "mobile": False,
                })
            except Exception:
                pass
            try:
                metrics = await cdp.send("Page.getLayoutMetrics")
                viewport = (metrics or {}).get("cssLayoutViewport") or {}
                self._cast_width = int(viewport.get("width") or 1280)
                self._cast_height = int(viewport.get("height") or 720)
            except Exception:
                self._cast_width, self._cast_height = 1280, 720
            # Pass the measured CSS viewport through unchanged. The operator
            # surface displays the native viewport; no downscaling is used.
            self._cast_width = max(1, self._cast_width)
            self._cast_height = max(1, self._cast_height)
            # Mark the stream live before startScreencast so a frame emitted
            # during that command is queued and acknowledged rather than
            # being dropped and stalling Chrome.
            self.cast_running = True
            self._frame_worker = asyncio.create_task(
                self._relay_frames(cast_cdp, frame_queue)
            )
            await cdp.send("Page.startScreencast", {
                "format": "png",
                "maxWidth": self._cast_width,
                "maxHeight": self._cast_height,
                "everyNthFrame": 1,
            })
        except Exception:
            if self.cdp is cdp:
                self.cdp = None
            self.cast_running = False
            self._frame_queue = None
            worker = self._frame_worker
            self._frame_worker = None
            if worker is not None:
                worker.cancel()
                await asyncio.gather(worker, return_exceptions=True)
            try:
                await cdp.detach()
            except Exception:
                pass
            raise

    async def _stop_cast_locked(self) -> None:
        cdp = self.cdp
        self.cdp = None
        self.cast_running = False
        self._frame_queue = None
        worker = self._frame_worker
        self._frame_worker = None
        if worker is not None:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
        ack_tasks = list(self._frame_ack_tasks)
        self._frame_ack_tasks.clear()
        for task in ack_tasks:
            task.cancel()
        if ack_tasks:
            await asyncio.gather(*ack_tasks, return_exceptions=True)
        if cdp is None:
            return
        try:
            await cdp.send("Page.stopScreencast")
        except Exception:
            pass
        try:
            await cdp.detach()
        except Exception:
            pass

    async def navigate(self, url: str) -> bool:
        if not url:
            return False
        if not url.startswith(("http://", "https://")):
            url = "https://" + url.lstrip("/")
        page = await self.ensure_browser(url=url)
        self.current_url = getattr(page, "url", url) or url
        try:
            parts = urlsplit(self.current_url)
            origin = f"{parts.scheme}://{parts.netloc}" if parts.scheme and parts.netloc else None
            if origin and self.context:
                await self.context.grant_permissions(["clipboard-read", "clipboard-write"], origin=origin)
        except Exception:
            pass
        self.last_activity = time.time()
        return True

    async def _clipboard_text(self) -> Optional[str]:
        if not self.alive:
            return None
        # Clipboard APIs are permission-gated by the remote origin.  Do not
        # silently manufacture a value: None lets the UI explain that the
        # origin denied access.
        try:
            value = await self.page.evaluate(
                "navigator.clipboard && navigator.clipboard.readText ? navigator.clipboard.readText() : null"
            )
            return value if isinstance(value, str) else None
        except Exception:
            return None

    async def handle_input(self, data: Dict[str, Any], websocket: Any = None) -> None:
        if not isinstance(data, dict):
            return
        async with self.lock:
            if not self.alive:
                return
            page = self.page
            self.last_activity = time.time()
            kind = str(data.get("subtype") or data.get("event") or data.get("type") or "")
            if kind == "input":
                kind = str(data.get("subtype") or data.get("event") or "")

            def number(name: str, default: float = 0.0) -> float:
                try:
                    value = float(data.get(name, default))
                    return value if value == value else default
                except Exception:
                    return default

            # Every websocket gets its own ownership set. Calls made by a
            # direct test/integration hook use a shared ``None`` owner.
            owner_buttons = self._pressed_mouse_buttons.setdefault(websocket, set())
            if kind in ("mousemove", "mouse_move"):
                await page.mouse.move(number("x"), number("y"))
            elif kind in ("mousedown", "mouseDown"):
                button = _normalize_mouse_button(data.get("button", 0))
                await page.mouse.move(number("x"), number("y"))
                await page.mouse.down(button=button)
                owner_buttons.add(button)
            elif kind in ("mouseup", "mouseUp"):
                button = _normalize_mouse_button(data.get("button", 0))
                await page.mouse.move(number("x"), number("y"))
                try:
                    await page.mouse.up(button=button)
                finally:
                    # Treat the button as released even if the page vanished
                    # while the event was in flight.
                    owner_buttons.discard(button)
            elif kind in ("click", "tap"):
                await page.mouse.click(number("x"), number("y"), button=_normalize_mouse_button(data.get("button", 0)))
            elif kind in ("wheel", "scroll"):
                await page.mouse.move(number("x"), number("y"))
                await page.mouse.wheel(number("deltaX"), number("deltaY"))
            elif kind in ("keydown", "keyDown"):
                key = str(data.get("key") or "")
                if key:
                    await page.keyboard.down(key)
            elif kind in ("keyup", "keyUp"):
                key = str(data.get("key") or "")
                if key:
                    await page.keyboard.up(key)
            elif kind in ("keypress", "press"):
                key = str(data.get("key") or "")
                if key:
                    await page.keyboard.press(key)
            elif kind in ("type", "insert_text", "text"):
                await page.keyboard.insert_text(str(data.get("text") or ""))
            elif kind in ("paste", "clipboard_paste"):
                await page.keyboard.insert_text(str(data.get("text") or ""))
            elif kind in ("copy", "clipboard_copy"):
                text = await self._clipboard_text()
                if websocket is not None:
                    try:
                        await websocket.send_json({
                            "type": "clipboard_text",
                            "text": text or "",
                            "available": text is not None,
                        })
                    except Exception:
                        pass
            elif kind == "highlight":
                x, y = number("x"), number("y")
                # Highlight the element under the operator's pointer. This is
                # an operator aid only; it does not alter the page's DOM
                # permanently and disappears after two seconds.
                await page.evaluate("""({x,y}) => {
                    const e = document.elementFromPoint(x, y); if (!e) return false;
                    const previous = window.__fixiisAccessHighlight;
                    if (previous && previous.timer) clearTimeout(previous.timer);
                    if (previous && previous.el && previous.el !== e) {
                        try { previous.el.style.outline = previous.outline; } catch (_) {}
                    }
                    const state = previous && previous.el === e
                        ? previous
                        : {el: e, outline: e.style.outline};
                    e.style.outline = '3px solid #ffcc00';
                    state.timer = setTimeout(() => {
                        try { state.el.style.outline = state.outline; } catch (_) {}
                        if (window.__fixiisAccessHighlight === state) delete window.__fixiisAccessHighlight;
                    }, 2000);
                    window.__fixiisAccessHighlight = state;
                    return true;
                }""", {"x": x, "y": y})

            if kind in ("keydown", "keypress", "press"):
                key = str(data.get("key") or "").lower()
                mods = {str(m).lower() for m in (data.get("modifiers") or [])}
                if key in ("c", "insert") and ("control" in mods or "ctrl" in mods or "meta" in mods):
                    # Chrome commits the copy event after key dispatch; allow
                    # the renderer's clipboard task to settle before reading.
                    await asyncio.sleep(0.08)
                    text = await self._clipboard_text()
                    if websocket is not None:
                        try:
                            await websocket.send_json({"type": "clipboard_text", "text": text or "", "available": text is not None})
                        except Exception:
                            pass


class AccessManager:
    def __init__(self) -> None:
        self.sessions: Dict[str, AccessSession] = {}
        self._access_locks: Dict[str, str] = {}
        self._lock = asyncio.Lock()
        self._profile_operation_locks: Dict[str, asyncio.Lock] = {}

    async def _profile_operation_lock(self, user_id: str) -> asyncio.Lock:
        async with self._lock:
            return self._profile_operation_locks.setdefault(user_id, asyncio.Lock())

    async def _lock_profile(self, access_id: str, user_id: str) -> bool:
        try:
            import api
            manager = getattr(api, "session_manager", None)
            registry = getattr(manager, "registry", None)
            if registry is None:
                return True
            ok = await registry.lock_session("access:" + access_id, user_id)
            if ok:
                self._access_locks[access_id] = user_id
            return ok
        except Exception:
            logger.warning("[Access] profile lock failed for %s", user_id, exc_info=True)
            return False

    async def _unlock_profile(self, access_id: str) -> None:
        user_id = self._access_locks.pop(access_id, None)
        if not user_id:
            return
        try:
            import api
            manager = getattr(api, "session_manager", None)
            registry = getattr(manager, "registry", None)
            if registry is not None and await registry.get_locked_session_id(user_id) == "access:" + access_id:
                await registry.unlock_session(user_id)
        except Exception:
            logger.debug("[Access] profile unlock failed for %s", user_id, exc_info=True)

    async def _release_public_user(self, user_id: str) -> int:
        """Release every public/hidden runtime for a profile before Access."""
        try:
            import api
            manager = getattr(api, "session_manager", None)
        except Exception:
            manager = None
        if manager is None:
            return 0
        sessions = await manager.get_session_by_user(user_id)
        released = 0
        for session in sessions:
            ws = getattr(session, "websocket", None)
            if ws is not None:
                try:
                    await ws.send_json({
                        "type": "session_released",
                        "reason": "access_takeover",
                        "permanent_until_refresh": True,
                        "user_id": user_id,
                    })
                except Exception:
                    pass
            try:
                if await manager.remove_session(session.session_id, force=True):
                    released += 1
            except Exception:
                logger.warning("[Access] failed to release runtime %s", session.session_id, exc_info=True)
            if ws is not None:
                try:
                    await ws.close(code=4003, reason="access_takeover")
                except Exception:
                    pass
        return released

    async def open(self, user_id: str, profile_dir: str, url: str,
                   access_id: Optional[str] = None) -> Dict[str, Any]:
        if not user_id or not profile_dir:
            return {"ok": False, "error": "user_id and profile_dir are required"}
        if not url.startswith(("http://", "https://")):
            url = "https://" + url.lstrip("/")
        access_id = access_id or ("access_" + uuid.uuid4().hex)

        # Serialize only operations for the same profile.  Browser creation is
        # intentionally outside the manager-wide map lock so independent
        # Access users can launch concurrently without sharing state or making
        # one slow SeleniumBase startup block all other profiles.
        profile_operation = await self._profile_operation_lock(user_id)
        async with profile_operation:
            async with self._lock:
                previous = [
                    s for s in self.sessions.values()
                    if s.user_id == user_id and s.access_id != access_id
                ]
                for old in previous:
                    self.sessions.pop(old.access_id, None)
                session = self.sessions.get(access_id)
                if session is not None and session.user_id != user_id:
                    self.sessions.pop(access_id, None)
                    conflicting = session
                    session = None
                else:
                    conflicting = None

            # Close and unlock replaced sessions only after removing them from
            # the map. This prevents a concurrent status/open call from
            # treating a browser that is already being replaced as current.
            for old in previous:
                await old.close()
                await self._unlock_profile(old.access_id)
            if conflicting is not None:
                await conflicting.close()
                await self._unlock_profile(access_id)

            if not await self._lock_profile(access_id, user_id):
                return {"ok": False, "error": "profile is locked by another admin operation"}
            try:
                released_public = await self._release_public_user(user_id)
            except Exception as exc:
                await self._unlock_profile(access_id)
                logger.exception("[Access] public profile release failed for %s", user_id)
                return {"ok": False, "error": str(exc)}
            if session is None:
                session = AccessSession(access_id, user_id, profile_dir, url)
                async with self._lock:
                    self.sessions[access_id] = session
            try:
                await session.ensure_browser(url=url)
            except Exception as exc:
                async with self._lock:
                    if self.sessions.get(access_id) is session:
                        self.sessions.pop(access_id, None)
                await session.close()
                await self._unlock_profile(access_id)
                logger.exception("[Access] browser launch failed for %s", user_id)
                return {"ok": False, "error": str(exc)}
            return {
                "ok": True,
                "access_id": access_id,
                "user_id": user_id,
                "url": session.current_url,
                "released_sessions": len(previous) + released_public,
            }

    async def get(self, access_id: str) -> Optional[AccessSession]:
        async with self._lock:
            return self.sessions.get(access_id)

    async def close(self, access_id: str) -> bool:
        async with self._lock:
            current = self.sessions.get(access_id)
        if current is None:
            return False
        profile_operation = await self._profile_operation_lock(current.user_id)
        async with profile_operation:
            async with self._lock:
                session = self.sessions.pop(access_id, None)
            if session is None:
                return False
            await session.close()
            await self._unlock_profile(access_id)
            return True

    async def shutdown(self) -> None:
        async with self._lock:
            values = list(self.sessions.values())
            access_ids = list(self.sessions.keys())
            self.sessions.clear()
        await asyncio.gather(*(session.close() for session in values), return_exceptions=True)
        await asyncio.gather(*(self._unlock_profile(access_id) for access_id in access_ids), return_exceptions=True)

    async def status(self, access_id: Optional[str] = None) -> Dict[str, Any]:
        async with self._lock:
            values = list(self.sessions.values())
        if access_id:
            values = [s for s in values if s.access_id == access_id]
        return {
            "sessions": [{
                "access_id": s.access_id,
                "user_id": s.user_id,
                "url": s.current_url,
                "connected": s.alive,
                "subscribers": len(s.subscribers),
                "cast": s.cast_running,
            } for s in values],
        }


access_manager = AccessManager()

__all__ = ["AccessSession", "AccessManager", "access_manager"]
