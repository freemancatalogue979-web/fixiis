"""SeleniumBase UC backend: SB launches/stealths the browser, our CDP layer drives it.

Architecture (see MIGRATION_SELENIUMBASE.md)
--------------------------------------------
* **Launcher + stealth + lifecycle = SeleniumBase UC mode.**  ``SBHandle``
  runs the (synchronous) SeleniumBase ``Driver`` in a dedicated single-thread
  executor, launches real Chrome with ``uc=True`` (undetected out of the
  box), and hands us a remote-debugging endpoint.  Our custom stealth
  scripts / playwright-stealth are NOT applied on this path — UC covers them.
* **Control plane = our own minimal raw-CDP client** (``_CDPClient`` /
  ``_CDPSession`` over one browser-level websocket, flat mode), behind an
  async **Playwright-shaped adapter** (``SBPage`` / ``SBContext`` /
  ``SBBrowser``) exposing exactly the surface session.py / pcm_manager.py /
  dom_capture.py actually use: navigation, evaluate, keyboard/mouse input,
  selector helpers, init scripts, expose_function bindings, cookies,
  permissions, viewport metrics, CDP sessions for screencast / MHTML.
* **CAPTCHA** (``CAPTCHA_MODE=auto``): after a ``goto`` settles, a selector
  probe (reCAPTCHA / Turnstile / Cloudflare challenge containers) triggers
  ``driver.uc_gui_click_captcha()`` via the launcher thread, max 2 attempts,
  best-effort.

The whole backend is selected via ``BROWSER_BACKEND=sb`` (config
``browser_backend``).  A selected SB session fails loudly on launch/attach
errors instead of opening a second, unrelated Playwright browser; use
``BROWSER_BACKEND=pw`` for the explicit rollback path.  SeleniumBase is
imported lazily inside the launcher thread so this module stays importable
without the dependency installed.
"""
from __future__ import annotations

import asyncio
import base64
import concurrent.futures
import inspect
import json
import logging
import os
import re
import shutil
import socket
import sys
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)

# SeleniumBase sessions deliberately get private Xvfb processes.  The async
# lock prevents two handles in this process from selecting the same display;
# the thread lock protects the short-lived DISPLAY environment override while
# SeleniumBase creates the corresponding Chrome process.
_SB_XVFB_ALLOC_LOCK = asyncio.Lock()
_SB_DRIVER_ENV_LOCK = threading.RLock()


def _should_disable_sandbox() -> bool:
    """Check if Chrome sandbox should be disabled (root / container)."""
    try:
        from browser_manager import BrowserManager
        return BrowserManager._should_disable_sandbox()
    except Exception:
        pass
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        return True
    return (
        os.path.exists("/.dockerenv")
        or os.path.exists("/run/.containerenv")
        or bool(os.environ.get("CONTAINER_ID"))
        or bool(os.environ.get("DOCKER_CONTAINER"))
    )


def clean_profile_locks(profile_dir: Optional[str]) -> None:
    """Remove Singleton*, lock files, and DevToolsActivePort that prevent Chrome from starting."""
    if not profile_dir:
        return
    try:
        pdir = Path(profile_dir)
        if not pdir.exists() or not pdir.is_dir():
            return
        for pattern in ("Singleton*", "*lock*", "DevToolsActivePort"):
            for f in pdir.glob(pattern):
                try:
                    if f.is_symlink() or f.is_file():
                        f.unlink(missing_ok=True)
                except Exception:
                    pass
    except Exception as exc:
        logger.debug("[SB] clean_profile_locks error: %s", exc)


def kill_profile_processes(profile_dir: Optional[str]) -> None:
    """Terminate lingering or orphaned Chrome processes holding profile_dir."""
    if not profile_dir:
        return
    try:
        target = str(Path(profile_dir).resolve())
        current_pid = os.getpid()
        killed_any = False

        try:
            import psutil
            for proc in psutil.process_iter(["pid", "cmdline"]):
                try:
                    if proc.info["pid"] == current_pid:
                        continue
                    cmdline = proc.info.get("cmdline") or []
                    cmd_str = " ".join(cmdline)
                    if target in cmd_str:
                        proc.terminate()
                        killed_any = True
                except Exception:
                    continue
        except Exception:
            if sys.platform.startswith("linux"):
                import signal
                for entry in os.listdir("/proc"):
                    if not entry.isdigit():
                        continue
                    pid = int(entry)
                    if pid == current_pid:
                        continue
                    try:
                        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
                        cmdline = raw.decode(errors="replace")
                        if target in cmdline:
                            os.kill(pid, signal.SIGTERM)
                            killed_any = True
                    except Exception:
                        pass

        if killed_any:
            time.sleep(0.3)
            # Second pass: SIGKILL any stubborn processes
            try:
                import psutil
                for proc in psutil.process_iter(["pid", "cmdline"]):
                    try:
                        if proc.info["pid"] == current_pid:
                            continue
                        cmdline = proc.info.get("cmdline") or []
                        if target in " ".join(cmdline):
                            proc.kill()
                    except Exception:
                        continue
            except Exception:
                if sys.platform.startswith("linux"):
                    import signal
                    for entry in os.listdir("/proc"):
                        if not entry.isdigit():
                            continue
                        pid = int(entry)
                        if pid == current_pid:
                            continue
                        try:
                            raw = Path(f"/proc/{pid}/cmdline").read_bytes()
                            if target in raw.decode(errors="replace"):
                                os.kill(pid, signal.SIGKILL)
                        except Exception:
                            pass
    except Exception as exc:
        logger.debug("[SB] kill_profile_processes error: %s", exc)


def clean_all_profile_locks(profile_base_path: Optional[str] = None) -> None:
    """Walk profile_base_path and remove all stale lock files and DevToolsActivePort."""
    if not profile_base_path:
        return
    try:
        base = Path(profile_base_path).resolve()
        if not base.exists() or not base.is_dir():
            return
        for pattern in ("Singleton*", "*lock*", "DevToolsActivePort"):
            for f in base.glob(f"**/{pattern}"):
                try:
                    if f.is_file() or f.is_symlink():
                        f.unlink(missing_ok=True)
                except Exception:
                    pass
    except Exception as exc:
        logger.debug("[SB] clean_all_profile_locks error: %s", exc)


def kill_all_browsers(profile_base_path: Optional[str] = None) -> int:
    """Synchronously terminate all Chrome, Chromium, and chromedriver processes
    spawned by this application or holding user-data-dirs under profile_base_path.
    Guarantees that closing Python terminates all browser processes.
    """
    killed = 0
    target_base = str(Path(profile_base_path).resolve()) if profile_base_path else None
    current_pid = os.getpid()

    try:
        import psutil
        for proc in psutil.process_iter(["pid", "name", "cmdline"]):
            try:
                pid = proc.info.get("pid")
                if pid == current_pid:
                    continue
                name = (proc.info.get("name") or "").lower()
                cmdline = proc.info.get("cmdline") or []
                cmd_str = " ".join(cmdline)

                # Match chromedriver
                if "chromedriver" in name or "chromedriver" in cmd_str.lower():
                    proc.kill()
                    killed += 1
                    continue

                # Match Chrome/Chromium processes with profile under profile_base_path
                if target_base and target_base in cmd_str:
                    proc.kill()
                    killed += 1
                    continue

                # Match child processes of this python process
                try:
                    parent = proc.parent()
                    if parent and parent.pid == current_pid:
                        if any(x in name for x in ("chrome", "chromium", "chromedriver", "xvfb")):
                            proc.kill()
                            killed += 1
                except Exception:
                    pass
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
            except Exception:
                pass
    except Exception:
        # Fallback for Linux proc filesystem if psutil fails
        if sys.platform.startswith("linux"):
            import signal
            for entry in os.listdir("/proc"):
                if not entry.isdigit():
                    continue
                pid = int(entry)
                if pid == current_pid:
                    continue
                try:
                    raw = Path(f"/proc/{pid}/cmdline").read_bytes()
                    cmdline = raw.decode(errors="replace")
                    if (target_base and target_base in cmdline) or "chromedriver" in cmdline.lower():
                        os.kill(pid, signal.SIGKILL)
                        killed += 1
                except Exception:
                    pass

    return killed



# ---------------------------------------------------------------------------
# Backend / flag resolution
# ---------------------------------------------------------------------------

def browser_backend() -> str:
    """'sb' (default — SeleniumBase UC) or 'pw' (legacy Playwright).

    Resolution: BROWSER_BACKEND env wins, then CONFIG.browser_backend
    (default 'sb'), with a conservative 'pw' fallback only when the config
    module itself is unreadable (in which case fresh deps can't be trusted
    either).
    """
    raw = os.environ.get("BROWSER_BACKEND", "").strip().lower()
    if not raw:
        try:
            from config import CONFIG
            raw = str(getattr(CONFIG, "browser_backend", "sb") or "sb").strip().lower()
        except Exception:
            raw = "pw"
    return "sb" if raw in ("sb", "seleniumbase", "uc") else "pw"


def captcha_mode() -> str:
    """'auto' (default) | 'off'.  BROWSER/unreadable-config fallback stays
    'off' so a broken deployment never swings the host mouse via pyautogui."""
    raw = os.environ.get("CAPTCHA_MODE", "").strip().lower()
    if not raw:
        try:
            from config import CONFIG
            raw = str(getattr(CONFIG, "captcha_mode", "auto") or "auto").strip().lower()
        except Exception:
            raw = "off"
    return raw if raw in ("auto", "on") else "off"


# ---------------------------------------------------------------------------
# Minimal async CDP transport (one browser-level websocket, flat sessions)
# ---------------------------------------------------------------------------

class _CDPSession:
    """A session-scoped CDP handle (Playwright CDPSession-shaped)."""

    def __init__(self, client: "_CDPClient", session_id: str) -> None:
        self._client = client
        self.session_id = session_id
        self._detached = False

    async def send(self, method: str, params: Optional[dict] = None, timeout: float = 30.0) -> dict:
        if self._detached:
            raise RuntimeError(f"CDP session detached (during {method})")
        return await self._client.send(method, params, session_id=self.session_id, timeout=timeout)

    def on(self, event: str, callback: Callable) -> None:
        self._client.on(event, callback, session_id=self.session_id)

    def off(self, event: str, callback: Callable) -> None:
        self._client.off(event, callback, session_id=self.session_id)

    async def detach(self) -> None:
        if not self._detached:
            self._detached = True
            try:
                await self._client.send("Target.detachFromTarget", {"sessionId": self.session_id}, timeout=5)
            except Exception:
                pass


class _CDPClient:
    """Browser-level CDP websocket with session demux and event listeners."""

    def __init__(self) -> None:
        self._ws: Any = None
        self._id = 0
        self._pending: Dict[int, asyncio.Future] = {}
        self._listeners: Dict[Tuple[Optional[str], str], List[Callable]] = {}
        self._reader: Optional[asyncio.Task] = None
        self._closed = False

    async def connect(self, ws_url: str) -> None:
        import websockets  # ships with playwright / uvicorn[standard]
        self._ws = await websockets.connect(
            ws_url, max_size=128 * 1024 * 1024, open_timeout=15, ping_interval=None,
        )
        self._reader = asyncio.create_task(self._read_loop())

    async def _read_loop(self) -> None:
        try:
            async for raw in self._ws:
                try:
                    msg = json.loads(raw)
                except Exception:
                    continue
                if "id" in msg:
                    fut = self._pending.pop(msg["id"], None)
                    if fut is not None and not fut.done():
                        if "error" in msg:
                            fut.set_exception(RuntimeError(f"CDP error: {msg['error'].get('message')}"))
                        else:
                            fut.set_result(msg.get("result") or {})
                    continue
                method = msg.get("method")
                if not method:
                    continue
                sid = msg.get("sessionId")
                params = msg.get("params") or {}
                for key in ((sid, method), (None, method), ("*", method)):
                    for cb in list(self._listeners.get(key, ())):
                        try:
                            if inspect.iscoroutinefunction(cb):
                                asyncio.create_task(cb(params))
                            else:
                                cb(params)
                        except Exception as exc:
                            logger.debug("[SB-CDP] listener error %s: %s", method, exc)
        except Exception as exc:
            logger.debug("[SB-CDP] reader loop ended: %s", exc)
        finally:
            self._closed = True
            for fut in list(self._pending.values()):
                if not fut.done():
                    fut.set_exception(RuntimeError("CDP connection closed"))
            self._pending.clear()

    async def send(self, method: str, params: Optional[dict] = None,
                   session_id: Optional[str] = None, timeout: float = 30.0) -> dict:
        if self._closed or self._ws is None:
            raise RuntimeError(f"CDP connection closed (during {method})")
        self._id += 1
        mid = self._id
        payload: Dict[str, Any] = {"id": mid, "method": method, "params": params or {}}
        if session_id:
            payload["sessionId"] = session_id
        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        self._pending[mid] = fut
        try:
            await self._ws.send(json.dumps(payload))
        except Exception:
            self._pending.pop(mid, None)
            raise
        return await asyncio.wait_for(fut, timeout=timeout)

    def on(self, event: str, callback: Callable, session_id: Optional[str] = None) -> None:
        self._listeners.setdefault((session_id, event), []).append(callback)

    def off(self, event: str, callback: Callable, session_id: Optional[str] = None) -> None:
        lst = self._listeners.get((session_id, event))
        if lst and callback in lst:
            lst.remove(callback)

    async def close(self) -> None:
        self._closed = True
        try:
            if self._reader:
                self._reader.cancel()
        except Exception:
            pass
        try:
            if self._ws is not None:
                await self._ws.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Input mapping
# ---------------------------------------------------------------------------

# modifiers bitfield per CDP: Alt=1, Ctrl=2, Meta(Command)=4, Shift=8
_MOD_BITS = {"Alt": 1, "Control": 2, "Meta": 4, "Shift": 8}

_NAMED_KEYS: Dict[str, Tuple[str, int]] = {  # key -> (code, windowsVK)
    "Enter": ("Enter", 13), "Backspace": ("Backspace", 8), "Delete": ("Delete", 46),
    "Tab": ("Tab", 9), "Escape": ("Escape", 27),
    "ArrowLeft": ("ArrowLeft", 37), "ArrowUp": ("ArrowUp", 38),
    "ArrowRight": ("ArrowRight", 39), "ArrowDown": ("ArrowDown", 40),
    "Home": ("Home", 36), "End": ("End", 35),
    "PageUp": ("PageUp", 33), "PageDown": ("PageDown", 34),
    "Shift": ("ShiftLeft", 16), "Control": ("ControlLeft", 17),
    "Alt": ("AltLeft", 18), "Meta": ("MetaLeft", 91),
    " ": ("Space", 32),
    "F1": ("F1", 112), "F2": ("F2", 113), "F3": ("F3", 114), "F4": ("F4", 115),
    "F5": ("F5", 116), "F6": ("F6", 117), "F7": ("F7", 118), "F8": ("F8", 119),
    "F9": ("F9", 120), "F10": ("F10", 121), "F11": ("F11", 122), "F12": ("F12", 123),
}
_KEY_TEXT = {"Enter": "\r", "Tab": "\t", " ": " "}
_PUNCT_CODE = {
    "-": "Minus", "=": "Equal", "[": "BracketLeft", "]": "BracketRight",
    ";": "Semicolon", "'": "Quote", "`": "Backquote", "\\": "Backslash",
    ",": "Comma", ".": "Period", "/": "Slash",
}
_VK_BY_CHAR = {c: ord(c.upper()) for c in "abcdefghijklmnopqrstuvwxyz0123456789"}


def _key_def(key: str) -> Dict[str, Any]:
    """CDP dispatchKeyEvent params for a Playwright-style key name/char."""
    if key in _NAMED_KEYS:
        code, vk = _NAMED_KEYS[key]
        out: Dict[str, Any] = {"key": key, "code": code, "windowsVirtualKeyCode": vk,
                               "nativeVirtualKeyCode": vk}
        if key in _KEY_TEXT:
            out["text"] = _KEY_TEXT[key]
        return out
    if len(key) == 1:
        ch = key
        if ch.isalpha():
            code, vk = "Key" + ch.upper(), ord(ch.upper())
        elif ch.isdigit():
            code, vk = "Digit" + ch, ord(ch)
        else:
            code, vk = _PUNCT_CODE.get(ch, ""), ord(ch)
        return {"key": ch, "code": code, "windowsVirtualKeyCode": vk,
                "nativeVirtualKeyCode": vk, "text": ch}
    # Unknown long name: best-effort passthrough
    return {"key": key, "code": key, "windowsVirtualKeyCode": 0, "nativeVirtualKeyCode": 0}


async def _safe_input_send(session: "_CDPSession", method: str, params: Dict[str, Any]) -> bool:
    """Send a CDP input command and report whether Chrome accepted it.

    The old helper swallowed every exception and returned ``None``.  That
    made a broken SB input channel indistinguishable from a successful key or
    mouse event: the WebSocket message arrived, but the renderer never saw
    it.  Callers still get the non-fatal behaviour we want for individual
    events, while they can now fall back to Selenium's native W3C input path
    and the log contains the actual CDP error.
    """
    try:
        await session.send(method, params)
        return True
    except Exception as exc:
        logger.warning("[SB][CDP-INPUT] %s rejected: %s", method, exc)
        return False


class _Keyboard:
    """Playwright-shaped keyboard with a Selenium-native input fallback.

    UC keeps a WebDriver/CDP session attached to the same renderer.  On some
    Chrome/UC combinations Chrome accepts ``Input.dispatchKeyEvent`` on our
    browser-level CDP socket but does not route the event to the focused
    renderer.  WebDriver W3C actions go through the session SeleniumBase owns
    and are the reliable path in that situation.
    """

    def __init__(self, page: "SBPage") -> None:
        self._page = page
        self._mods = 0

    async def _dispatch_cdp(self, etype: str, key: str) -> bool:
        kd = _key_def(key)
        kd["type"] = etype
        kd["modifiers"] = self._mods
        return await _safe_input_send(self._page._session, "Input.dispatchKeyEvent", kd)

    async def _native(self, etype: str, key: str) -> bool:
        handle = self._page._ctx._handle
        if handle is None:
            return False
        try:
            return await handle.native_key_action(self._page._target_id, etype, key)
        except Exception as exc:
            logger.warning("[SB][W3C-INPUT] key %s %s failed: %s", etype, key, exc)
            return False

    async def down(self, key: str) -> None:
        # Keep the CDP modifier bitfield correct for the fallback path.
        if key in _MOD_BITS:
            self._mods |= _MOD_BITS[key]
        if await self._native("keyDown", key):
            return
        await self._dispatch_cdp("keyDown" if "text" in _key_def(key) else "rawKeyDown", key)

    async def up(self, key: str) -> None:
        # CDP keyUp should still carry the modifier being released; clear it
        # only after the native/CDP dispatch has been attempted.
        if await self._native("keyUp", key):
            if key in _MOD_BITS:
                self._mods &= ~_MOD_BITS[key] & 0xF
            return
        await self._dispatch_cdp("keyUp", key)
        if key in _MOD_BITS:
            self._mods &= ~_MOD_BITS[key] & 0xF

    async def press(self, key: str) -> None:
        await self.down(key)
        await self.up(key)

    async def insert_text(self, text: str) -> None:
        handle = self._page._ctx._handle
        if handle is not None:
            try:
                if await handle.native_insert_text(self._page._target_id, text):
                    return
            except Exception as exc:
                logger.warning("[SB][W3C-INPUT] insert text failed: %s", exc)
        await _safe_input_send(self._page._session, "Input.insertText", {"text": text})

    async def type(self, text: str, delay: float = 0) -> None:
        for ch in text:
            await self.press(ch)
            if delay:
                await asyncio.sleep(delay / 1000.0)


class _Mouse:
    """Mouse adapter that prefers native Selenium W3C pointer actions."""

    def __init__(self, page: "SBPage") -> None:
        self._page = page
        self._x = 0
        self._y = 0
        self._buttons = 0

    _BUTTON_BITS = {"left": 1, "right": 2, "middle": 4, "back": 8, "forward": 16}

    async def _native(self, action: str, button: str = "left", delta_x: float = 0,
                      delta_y: float = 0) -> bool:
        handle = self._page._ctx._handle
        if handle is None:
            return False
        try:
            return await handle.native_pointer_action(
                self._page._target_id, action, self._x, self._y,
                button=button, delta_x=delta_x, delta_y=delta_y,
            )
        except Exception as exc:
            logger.warning("[SB][W3C-INPUT] mouse %s failed: %s", action, exc)
            return False

    async def move(self, x: float, y: float) -> None:
        self._x, self._y = float(x), float(y)
        if await self._native("move"):
            return
        await _safe_input_send(self._page._session, "Input.dispatchMouseEvent", {
            "type": "mouseMoved", "x": self._x, "y": self._y,
            "button": "none", "buttons": self._buttons, "pointerType": "mouse",
        })

    async def down(self, button: str = "left", click_count: int = 1) -> None:
        bit = self._BUTTON_BITS.get(button, 1)
        if await self._native("down", button=button):
            self._buttons |= bit
            return
        ok = await _safe_input_send(self._page._session, "Input.dispatchMouseEvent", {
            "type": "mousePressed", "x": self._x, "y": self._y,
            "button": button, "buttons": self._buttons | bit,
            "clickCount": click_count, "pointerType": "mouse",
        })
        if ok:
            self._buttons |= bit

    async def up(self, button: str = "left") -> None:
        bit = self._BUTTON_BITS.get(button, 1)
        if await self._native("up", button=button):
            self._buttons &= ~bit
            return
        await _safe_input_send(self._page._session, "Input.dispatchMouseEvent", {
            "type": "mouseReleased", "x": self._x, "y": self._y,
            "button": button, "buttons": self._buttons & ~bit,
            "clickCount": 1, "pointerType": "mouse",
        })
        self._buttons &= ~bit

    async def click(self, x: float, y: float, button: str = "left", delay: float = 0) -> None:
        """Dispatch one complete pointer click via direct CDP input pipeline."""
        self._x, self._y = float(x), float(y)
        bit = self._BUTTON_BITS.get(button, 1)
        await self._page._session.send("Input.dispatchMouseEvent", {
            "type": "mouseMoved", "x": self._x, "y": self._y,
            "button": "none", "buttons": self._buttons, "pointerType": "mouse",
        })
        await self._page._session.send("Input.dispatchMouseEvent", {
            "type": "mousePressed", "x": self._x, "y": self._y,
            "button": button, "buttons": self._buttons | bit,
            "clickCount": 1, "pointerType": "mouse",
        })
        self._buttons |= bit
        try:
            if delay:
                await asyncio.sleep(delay / 1000.0)
        finally:
            await self._page._session.send("Input.dispatchMouseEvent", {
                "type": "mouseReleased", "x": self._x, "y": self._y,
                "button": button, "buttons": self._buttons & ~bit,
                "clickCount": 1, "pointerType": "mouse",
            })
            self._buttons &= ~bit
        try:
            await self._native("click", button=button)
        except Exception:
            pass

    async def wheel(self, delta_x: float, delta_y: float) -> None:
        if await self._native("wheel", delta_x=delta_x, delta_y=delta_y):
            return
        await _safe_input_send(self._page._session, "Input.dispatchMouseEvent", {
            "type": "mouseWheel", "x": self._x, "y": self._y,
            "deltaX": delta_x, "deltaY": delta_y,
        })


# ---------------------------------------------------------------------------
# Playwright-shaped page adapter
# ---------------------------------------------------------------------------

_FN_HINT_RE = re.compile(r"=>|^\s*(async\s+)?function|^\s*\(")

_IIFE_TAIL_RE = re.compile(r"(\}\)\(\)|\)\(\))\s*;?\s*$")


def _js_is_function(expr: str) -> bool:
    """Classify a JS string as function-form (callFunctionOn) vs expression
    (Runtime.evaluate).  IIFEs are expressions: they END with a call."""
    s = expr.strip()
    if s.startswith("(") and _IIFE_TAIL_RE.search(s):
        return False
    if re.match(r"^(async\s+)?function\b", s):
        return True
    head = s.split("{", 1)[0]
    if "=>" in head:
        return True
    return False


class SBPage:
    """Async adapter over one attached CDP page target (Playwright-shaped)."""

    def __init__(self, client: _CDPClient, target_id: str, session_id: str,
                 context: "SBContext", opener_id: Optional[str] = None) -> None:
        self._client = client
        self._target_id = target_id
        self._session_id = session_id
        # DOMCaptureSession uses this marker to avoid its legacy
        # element.click() fallback. SB must stay on the real pointer path.
        self._is_sb_backend = True
        self._backend_name = "seleniumbase-cdp"
        self._ctx = context
        self._opener_id = opener_id
        self._session = _CDPSession(client, session_id)
        self._closed = False
        self._url: str = "about:blank"
        self._main_frame_id: Optional[str] = None
        self._listeners: Dict[str, List[Callable]] = {}
        self._bindings: Dict[str, Callable] = {}
        self._mobile = context._mobile
        self.keyboard = _Keyboard(self)
        self.mouse = _Mouse(self)
        self._network_enabled = False
        client.on("Inspector.targetCrashed", lambda _p: self._mark_closed(), session_id=session_id)

    # ---- lifecycle ----------------------------------------------------

    async def _init(self) -> None:
        # Target.targetInfoChanged is not a complete navigation signal on all
        # Chrome builds. Register page-scoped events before enabling Page so
        # DOMCaptureSession never reads the previous document URL.
        self._session.on("Page.frameNavigated", self._on_main_frame_navigated)
        self._session.on("Page.navigatedWithinDocument", self._on_same_document_navigated)
        await self._session.send("Page.enable")
        await self._session.send("Runtime.enable")
        try:
            tree = await self._session.send("Page.getFrameTree", timeout=5)
            self._main_frame_id = (((tree or {}).get("frameTree") or {}).get("frame") or {}).get("id")
        except Exception:
            pass
        # Keep CDP input events flowing even when Chrome is not the OS
        # foreground process.  This is the single biggest reason clicks
        # "do nothing" on the SB backend: Chrome is launched via
        # SeleniumBase UC, which often leaves the renderer backgrounded
        # while a restored tab sits in the foreground.  Without this
        # flag, mousePressed is silently discarded before any page
        # handler runs.
        try:
            await self._session.send(
                "Emulation.setFocusEmulationEnabled", {"enabled": True}, timeout=5,
            )
        except Exception:
            pass
        # WebAuthn/passkey blocking, identical to the Playwright backend:
        # JS override at document creation + CDP virtual authenticator so
        # Windows Hello / Microsoft passkey prompts never reach the OS and
        # sites fall back to passwords (see webauthn_block.py).
        try:
            from webauthn_block import WEBAUTHN_BLOCK_JS, VIRTUAL_AUTHENTICATOR_OPTIONS
            await self._session.send(
                "Page.addScriptToEvaluateOnNewDocument",
                {"source": WEBAUTHN_BLOCK_JS}, timeout=10)
            try:
                await self._session.send("WebAuthn.enable", timeout=10)
                await self._session.send("WebAuthn.addVirtualAuthenticator",
                                         {"options": VIRTUAL_AUTHENTICATOR_OPTIONS}, timeout=10)
            except Exception as cdp_exc:
                logger.debug("[SB][WebAuthn] virtual authenticator unavailable: %s", cdp_exc)
        except Exception as exc:
            logger.debug("[SB][WebAuthn] block install failed: %s", exc)
        for dom, params in (
            ("DOM.enable", {}),
            ("Page.setBypassCSP", {"enabled": True}),
            ("Page.setLifecycleEventsEnabled", {"enabled": True}),
        ):
            try:
                await self._session.send(dom, params, timeout=10)
            except Exception:
                pass
        self._client.on("Runtime.bindingCalled", self._on_binding_called, session_id=self._session_id)
        try:
            info = await self._client.send("Target.getTargetInfo", {"targetId": self._target_id})
            url = ((info or {}).get("targetInfo") or {}).get("url")
            if url:
                self._url = url
        except Exception:
            pass

    def _mark_closed(self) -> None:
        if not self._closed:
            self._closed = True
            for cb in list(self._listeners.get("close", ())):
                try:
                    cb(self)
                except Exception:
                    pass

    def _update_url(self, url: Optional[str]) -> None:
        if url:
            self._url = url

    def _on_main_frame_navigated(self, params: dict) -> None:
        """Keep the Playwright-shaped ``page.url`` current for SB pages.

        ``Target.targetInfoChanged`` is not emitted consistently for link
        navigations and history.pushState/replaceState.  The page-scoped CDP
        navigation events are authoritative for both full and same-document
        URL changes, and DOM capture reads this cached property.
        """
        frame = (params or {}).get("frame") or {}
        if frame.get("parentId"):
            return
        if frame.get("id"):
            self._main_frame_id = frame.get("id")
        url = frame.get("url") or frame.get("unreachableUrl")
        if url:
            self._update_url(url)

    def _on_same_document_navigated(self, params: dict) -> None:
        frame_id = (params or {}).get("frameId")
        if self._main_frame_id and frame_id and frame_id != self._main_frame_id:
            return
        url = (params or {}).get("url")
        if url:
            self._update_url(url)

    # ---- events -------------------------------------------------------

    def on(self, event: str, callback: Callable) -> None:
        """Playwright-style event subscription ('close', 'popup')."""
        self._listeners.setdefault(event, []).append(callback)

    def _emit(self, event: str, *args: Any) -> None:
        for cb in list(self._listeners.get(event, ())):
            try:
                r = cb(*args)
                if inspect.isawaitable(r):
                    asyncio.create_task(r)  # fire-and-forget like Playwright handlers
            except Exception:
                pass

    # ---- bindings (expose_function) ------------------------------------

    async def expose_function(self, name: str, callback: Callable) -> None:
        """Page-callable binding: window.NAME(...args) -> Python callback.

        Multi-arg safe: unlike a bare ``Runtime.addBinding`` (which only
        forwards one string argument), we define ``window[NAME]`` as a
        wrapper that JSON-serializes the argument list into a hidden
        native binding — Playwright exposeBinding semantics for the
        primitive/JSON payloads this codebase passes.
        """
        native = f"__sbBind_{re.sub(r'[^A-Za-z0-9_]', '_', name)}"
        self._bindings[native] = callback
        await self._session.send("Runtime.addBinding", {"name": native})
        wrapper = (
            "(() => { const B = %s, N = %s;\n"
            " const wrap = (...args) => { try { window[B](JSON.stringify(args)); } catch (e) {} };\n"
            " try { Object.defineProperty(window, N, { value: wrap, configurable: true, writable: true }); }\n"
            " catch (e) { try { window[N] = wrap; } catch (e2) {} } })();"
        ) % (json.dumps(native), json.dumps(name))
        try:
            await self._session.send("Page.addScriptToEvaluateOnNewDocument", {"source": wrapper})
        except Exception as exc:
            logger.debug("[SB] binding init-script %s failed: %s", name, exc)
        try:
            await self._session.send("Runtime.evaluate", {"expression": wrapper})
        except Exception as exc:
            logger.debug("[SB] binding immediate-eval %s failed: %s", name, exc)

    def _on_binding_called(self, params: dict) -> None:
        name = params.get("name")
        cb = self._bindings.get(name)
        if cb is None:
            return
        payload = params.get("payload", "[]")
        try:
            args = json.loads(payload)
            if not isinstance(args, list):
                args = [args]
        except Exception:
            args = [payload]
        try:
            if inspect.iscoroutinefunction(cb):
                asyncio.create_task(cb(*args))
            else:
                cb(*args)
        except Exception as exc:
            logger.debug("[SB] binding %s callback failed: %s", name, exc)

    # ---- navigation ------------------------------------------------------

    async def goto(self, url: str, wait_until: str = "load", timeout: float = 30000) -> None:
        try:
            await self._session.send("Page.navigate", {"url": url}, timeout=max(10.0, timeout / 1000.0))
            self._url = url
        finally:
            pass
        await self._wait_ready(wait_until, timeout)
        self._maybe_solve_captcha()

    async def reload(self, wait_until: str = "load", timeout: float = 30000, **_kw: Any) -> None:
        await self._session.send("Page.reload", {"ignoreCache": False})
        await self._wait_ready(wait_until, timeout)

    async def _nav_history(self, delta: int, wait_until: str, timeout: float) -> None:
        hist = await self._session.send("Page.getNavigationHistory", timeout=10)
        entries = (hist or {}).get("entries") or []
        idx = (hist or {}).get("currentIndex", 0) + delta
        if 0 <= idx < len(entries):
            await self._session.send("Page.navigateToHistoryEntry", {"entryId": entries[idx]["id"]}, timeout=15)
        await self._wait_ready(wait_until, timeout)

    async def go_back(self, wait_until: str = "load", timeout: float = 30000, **_kw: Any) -> None:
        await self._nav_history(-1, wait_until, timeout)

    async def go_forward(self, wait_until: str = "load", timeout: float = 30000, **_kw: Any) -> None:
        await self._nav_history(+1, wait_until, timeout)

    async def _wait_ready(self, wait_until: str, timeout: float) -> None:
        """readyState poll matching domcontentloaded/load; networkidle is a
        documented approximation (complete + 400ms settle) — the live-mirror
        fast pipeline never blocks on it anyway."""
        timeout_s = max(1.0, timeout / 1000.0)
        deadline = time.monotonic() + timeout_s
        wanted = "complete" if wait_until in ("load", "networkidle") else None
        while time.monotonic() < deadline:
            try:
                rs = await self.evaluate("document.readyState")
                if rs == "complete":
                    break
                if wanted is None and rs in ("interactive", "complete"):
                    break
            except Exception:
                await asyncio.sleep(0.1)
                continue
            await asyncio.sleep(0.15)
        if wait_until == "networkidle":
            await asyncio.sleep(0.4)

    async def wait_for_load_state(self, state: str = "load", timeout: float = 10000, **_kw: Any) -> None:
        await self._wait_ready(state if state in ("load", "domcontentloaded", "networkidle") else "load", timeout)

    # ---- evaluation ------------------------------------------------------

    async def evaluate(self, expression: str, arg: Any = None) -> Any:
        expr = expression if isinstance(expression, str) else str(expression)
        if _js_is_function(expr):
            # Runtime.callFunctionOn is tempting here, but it requires an
            # executionContextId/objectId on Chrome versions used by UC.  The
            # old adapter omitted both, so every arrow-function evaluation
            # (including focus/actionability probes) could fail while the
            # exception was hidden by callers.  Evaluate an explicit
            # invocation in the page's main world instead.  JSON arguments are
            # deliberately embedded rather than passed as a remote object so
            # this works for primitive and structured Playwright-style args.
            try:
                js_arg = json.dumps(arg, ensure_ascii=False, separators=(",", ":"))
            except (TypeError, ValueError):
                js_arg = "null"
            function_expr = expr.strip()
            if function_expr.endswith(";"):
                function_expr = function_expr[:-1].rstrip()
            expression_to_run = f"({function_expr})({js_arg})"
        else:
            # Plain expressions and IIFEs must be evaluated as expressions;
            # calling an IIFE through Runtime.callFunctionOn invokes its
            # return value instead of the IIFE itself.
            expression_to_run = expr
        res = await self._session.send("Runtime.evaluate", {
            "expression": expression_to_run,
            "awaitPromise": True,
            "returnByValue": True,
            "userGesture": True,
        }, timeout=60)
        if not isinstance(res, dict):
            return None
        exc = res.get("exceptionDetails")
        if exc:
            text = ((exc.get("exception") or {}).get("description")
                    or exc.get("text") or "evaluate failed")
            raise RuntimeError(f"SB evaluate: {text}")
        return (res.get("result") or {}).get("value")

    async def eval_on_selector(self, selector: str, expression: str, arg: Any = None) -> Any:
        fn = (
            "(arg) => { const __el = document.querySelector(%s);"
            " if (!__el) throw new Error('selector not found: ' + %s);"
            " return (%s)(__el, arg); }"
        ) % (json.dumps(selector), json.dumps(selector), expression)
        return await self.evaluate(fn, arg)

    async def wait_for_function(self, predicate: str, arg: Any = None, timeout: float = 30000,
                                polling: float = 0.1, **_kw: Any) -> Any:
        deadline = time.monotonic() + timeout / 1000.0
        while True:
            try:
                val = await self.evaluate(predicate, arg)
                if val:
                    return val
            except Exception:
                pass
            if time.monotonic() >= deadline:
                raise RuntimeError(f"wait_for_function timeout after {timeout}ms")
            await asyncio.sleep(max(0.05, polling))

    async def wait_for_timeout(self, ms: float) -> None:
        await asyncio.sleep(ms / 1000.0)

    async def title(self) -> str:
        try:
            t = await self.evaluate("document.title")
            return t if isinstance(t, str) else ""
        except Exception:
            return ""

    async def content(self) -> str:
        html = await self.evaluate("() => document.documentElement ? document.documentElement.outerHTML : ''")
        return ("<!DOCTYPE html>\n" + html) if isinstance(html, str) else str(html or "")

    # ---- selector/input helpers ------------------------------------------

    async def click(self, selector: str, timeout: float = 3000, **_kw: Any) -> None:
        # Prefer SeleniumBase/ChromeDriver's trusted element click.  It uses
        # the exact WebDriver renderer session that UC owns and therefore
        # delivers pointerdown/mousedown/up/click even when a second CDP
        # client is attached.  The CDP actionability path below remains a
        # fallback for environments where Selenium cannot resolve the target
        # handle (and for the adapter harness).
        handle = self._ctx._handle
        if handle is not None:
            try:
                if await handle.native_click_selector(self._target_id, selector):
                    logger.debug(
                        "[SB][CLICK-native] target=%s selector=%s",
                        self._target_id, selector,
                    )
                    return
            except Exception as exc:
                logger.debug("[SB][CLICK-native] selector=%s failed: %s", selector, exc)

        deadline = time.monotonic() + timeout / 1000.0
        point: Optional[Dict[str, float]] = None
        in_iframe: bool = False
        while True:
            probe = await self.evaluate(
                """(sel) => {
                    const el = document.querySelector(sel);
                    if (!el || !el.isConnected) return null;
                    if (el.disabled || el.getAttribute('aria-disabled') === 'true') return null;

                    // If sel pointed to an inline label / span / icon inside a button or link,
                    // resolve to the enclosing button/link for both actionability and hit testing.
                    const enclosingControl = el.closest('#identifierNext, #passwordNext, [id$="Next"], [id$="next"], [jsaction*="click"], button, [role="button"], a, input[type="button"], input[type="submit"]');
                    const actionableEl = enclosingControl || el;

                    // Iframes: CDP input on the parent document does not
                    // cross an <iframe> boundary — Input.dispatchMouseEvent
                    // is delivered to the parent's main frame, and the
                    // event lands on whatever overlays the iframe at that
                    // coordinate.  Detect iframe ancestry and short-circuit
                    // to the element.click() fallback path, which executes
                    // inside the iframe's own JS context (same-origin).
                    for (let n = actionableEl; n && n !== document; n = n.parentElement || (n.parentNode && n.parentNode.host ? n.parentNode.host : null)) {
                        if (n.tagName === 'IFRAME') return { iframe: true };
                    }
                    actionableEl.scrollIntoView({block: 'center', inline: 'center'});
                    const r = actionableEl.getBoundingClientRect();
                    if (!(r.width > 0 && r.height > 0)) return null;
                    const style = getComputedStyle(actionableEl);
                    if (style.visibility === 'hidden' || style.display === 'none' ||
                        style.pointerEvents === 'none') return null;
                    const x = r.left + r.width / 2;
                    const y = r.top + r.height / 2;
                    const hit = document.elementFromPoint(x, y);

                    // Valid hit targets: the element itself, the enclosing control,
                    // any descendant of either, any ancestor of either, or sibling overlays
                    // within the same component (such as Google Material's ripple overlay).
                    const isHitValid = hit && (
                        hit === el ||
                        hit === actionableEl ||
                        el.contains(hit) ||
                        actionableEl.contains(hit) ||
                        hit.contains(el) ||
                        hit.contains(actionableEl) ||
                        (hit.closest && (
                            hit.closest('button, [role="button"], a, [jsaction]') === actionableEl.closest('button, [role="button"], a, [jsaction]')
                        ))
                    );
                    if (!isHitValid) return null;
                    return {x, y, iframe: false};
                }""",
                selector,
            )
            if isinstance(probe, dict):
                in_iframe = bool(probe.get("iframe"))
                point = {"x": float(probe["x"]), "y": float(probe["y"])} if "x" in probe else None
            else:
                point = None
            if point or in_iframe or time.monotonic() >= deadline:
                break
            await asyncio.sleep(0.15)
        if not point and not in_iframe:
            raise RuntimeError(f"SB click: selector not clickable: {selector}")

        # CDP input is target-scoped, but bringing the target forward avoids
        # Chrome/SeleniumBase focus being left on a restored or popup tab.
        await self.bring_to_front()
        if in_iframe:
            # Iframe-resident target.  CDP pointer events can't cross an
            # iframe boundary from the parent document — the event lands
            # on whatever overlays the iframe at that coordinate in the
            # parent's coordinate system.  Fall back to element.click(),
            # which runs inside the iframe's own JS context (same-origin
            # requirement; cross-origin iframes would have failed at the
            # selector resolution step anyway).  This is the same fallback
            # Playwright uses for iframe clicks when it has no frame
            # session attached.
            logger.debug(
                "[SB][CLICK-iframe] target=%s session=%s selector=%s",
                self._target_id, self._session_id, selector,
            )
            try:
                await self.evaluate(
                    "(sel) => { const el = document.querySelector(sel);"
                    " if (el) el.click(); }",
                    selector,
                )
                return
            except Exception as exc:
                logger.debug(
                    "[SB][CLICK-iframe] element.click() failed (%s): %s",
                    type(exc).__name__, exc,
                )
                raise
        logger.debug(
            "[SB][CLICK] target=%s session=%s selector=%s point=(%.1f,%.1f)",
            self._target_id, self._session_id, selector,
            point["x"], point["y"],
        )
        # Snapshot the DOM around the target so we can detect a no-op
        # click.  Modern framework buttons (React onClick, MUI Button,
        # Apple ui-button) bind on ``pointerdown`` / ``mousedown`` and
        # may dispatch their own activation event that does not surface
        # as a click.  When chromedriver is attached alongside our CDP
        # session, the renderer sometimes accepts ``Input.dispatchMouseEvent``
        # from our session but does NOT deliver the synthetic
        # ``mousedown`` to JS handlers — the page looks alive but nothing
        # happens.  W3C Actions through chromedriver is the recovery.
        #
        # Use a click counter that the page itself increments in its
        # own ``click`` listener (we install a one-shot probe).  This is
        # more reliable than DOM-diffing because framework re-renders
        # may keep the same hit-target while legitimately firing a
        # click that we want to count.
        probe_installed = False
        try:
            await self.evaluate(
                """(args) => {
                    const el = document.querySelector(args.sel);
                    if (!el) return;
                    const targetEl = el.closest('button, [role="button"], a, input[type="button"], input[type="submit"]') || el;
                    const marker = '__sb_click_probe_' + (targetEl.tagName || 'x');
                    targetEl.addEventListener('click', () => {
                        try { window[marker] = (window[marker] || 0) + 1; } catch (e) {}
                    }, { capture: true, once: false });
                    window.__sb_click_marker = marker;
                }""",
                {"sel": selector},
            )
            probe_installed = True
        except Exception:
            probe_installed = False
        await self.mouse.click(point["x"], point["y"])
        # Give the renderer a beat to dispatch the events.  80ms is
        # empirically the right window for synthetic CDP events on
        # system Chrome (the renderer queues pointerdown -> mousedown ->
        # mouseup -> click in the same frame, but the JS ``click``
        # listener is queued microtask-later).
        await asyncio.sleep(0.08)
        # Probe whether the click had an effect by checking the
        # counter.  If it did NOT increment, the CDP click landed in
        # the renderer but no JS handler fired.  Retry via W3C Actions
        # through chromedriver (the trusted session).
        post_count: Any = None
        if probe_installed:
            try:
                post_count = await self.evaluate(
                    "() => { const m = window.__sb_click_marker; return m ? (window[m] || 0) : null; }"
                )
            except Exception:
                post_count = None
        if probe_installed and (post_count is None or post_count < 1):
            handle = self._ctx._handle
            if handle is not None:
                logger.debug(
                    "[SB][CLICK-recover] CDP click produced no DOM click "
                    "event for selector=%s target=%s (post_count=%r) — "
                    "retrying via chromedriver W3C Actions",
                    selector, self._target_id, post_count,
                )
                ok = await handle.w3c_actions_click(
                    self._target_id, point["x"], point["y"],
                )
                if ok:
                    # Give the trusted-session click a beat, then
                    # verify it landed.
                    await asyncio.sleep(0.08)
                    try:
                        post_count2 = await self.evaluate(
                            "() => { const m = window.__sb_click_marker;"
                            " return m ? (window[m] || 0) : null; }"
                        )
                    except Exception:
                        post_count2 = None
                    if post_count2 and post_count2 >= 1:
                        return
                # Final fallback: synthesize a click in the page's own
                # JS context.  This is the path Playwright also uses
                # for elements inside same-origin iframes / when input
                # cannot cross frame boundaries.
                try:
                    await self.evaluate(
                        "(sel) => { const el = document.querySelector(sel);"
                        " if (el) el.click(); }",
                        selector,
                    )
                except Exception as exc:
                    logger.debug(
                        "[SB][CLICK-recover] element.click() fallback failed: %s",
                        exc,
                    )
                    raise

    async def fill(self, selector: str, text: str) -> None:
        await self.evaluate(
            "(args) => { const el = document.querySelector(args[0]); if (!el) throw new Error('fill: selector not found');"
            " el.focus(); el.value = args[1];"
            " el.dispatchEvent(new Event('input', { bubbles: true }));"
            " el.dispatchEvent(new Event('change', { bubbles: true })); }",
            [selector, str(text)],
        )

    async def focus(self, selector: str) -> None:
        handle = self._ctx._handle
        if handle is not None:
            try:
                if await handle.native_focus_selector(self._target_id, selector):
                    return
            except Exception as exc:
                logger.debug("[SB][FOCUS-native] selector=%s failed: %s", selector, exc)
        await self.evaluate(
            "(sel) => { const el = document.querySelector(sel); if (el && el.focus) el.focus(); }",
            selector,
        )

    # ---- scripts / bindings -----------------------------------------------

    async def add_init_script(self, script: Any = None, path: Any = None,
                              content: Any = None, **kw: Any) -> None:
        source = script if isinstance(script, str) else (content if isinstance(content, str) else None)
        if source is None and isinstance(path, str):
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    source = fh.read()
            except Exception as exc:
                logger.debug("[SB] add_init_script path read failed: %s", exc)
                return
        if not source:
            return
        await self._session.send("Page.addScriptToEvaluateOnNewDocument", {"source": source})

    async def add_script_tag(self, url: Optional[str] = None, path: Optional[str] = None,
                             content: Optional[str] = None, type: Optional[str] = None) -> None:  # noqa: A002
        if content is None and path:
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    content = fh.read()
            except Exception as exc:
                raise RuntimeError(f"add_script_tag path read failed: {exc}")
        await self.evaluate(
            "(args) => { const el = document.createElement('script');"
            " if (args.t) el.type = args.t;"
            " if (args.u) { el.src = args.u; (document.head || document.documentElement).appendChild(el); return; }"
            " el.textContent = args.c; (document.head || document.documentElement).appendChild(el); el.remove(); }",
            {"u": url, "c": content, "t": type},
        )

    # ---- network / headers / viewport --------------------------------------

    async def set_extra_http_headers(self, headers: Dict[str, str]) -> None:
        if not headers:
            return
        if not self._network_enabled:
            try:
                await self._session.send("Network.enable")
                self._network_enabled = True
            except Exception:
                pass
        try:
            await self._session.send("Network.setExtraHTTPHeaders", {"headers": dict(headers)})
        except Exception as exc:
            logger.debug("[SB] set_extra_http_headers failed: %s", exc)
        ua = headers.get("User-Agent") or headers.get("user-agent")
        if ua:
            try:
                await self._session.send("Emulation.setUserAgentOverride", {"userAgent": ua})
            except Exception:
                pass

    async def set_viewport_size(self, size: Dict[str, Any]) -> None:
        default_w = 390 if self._mobile else 1280
        default_h = 844 if self._mobile else 720
        await self._session.send("Emulation.setDeviceMetricsOverride", {
            "width": int(size.get("width") or default_w),
            "height": int(size.get("height") or default_h),
            "deviceScaleFactor": float(size.get("deviceScaleFactor", self._ctx._pixel_ratio or 1)),
            "mobile": bool(self._mobile),
        })

    async def bring_to_front(self) -> None:
        """Activate the target at both Chrome and OS level before input.

        ``Page.bringToFront`` only reorders tabs inside the same Chrome
        window — it does NOT guarantee the renderer has OS focus, and CDP
        ``Input.dispatchMouseEvent`` is silently dropped on background
        renderers in real Chrome.  The SB backend launches the browser via
        SeleniumBase UC, which on a fresh profile can leave Chrome sitting
        in the background while a restored session tab is foreground; in
        that state every click we send goes nowhere.  The fix is twofold:

        1. ``Target.activateTarget`` at the browser level promotes the
           target within Chrome AND triggers OS-level window activation
           (it is what Playwright uses internally for the same purpose).
        2. ``Emulation.setFocusEmulationEnabled`` keeps CDP input flowing
           even when the OS-level window is not actually foregrounded
           (headless / background tab / locked workstation).  Without
           it, a successful ``mousePressed`` is discarded before any
           framework handler runs, and the page shows no reaction.

        Both calls are best-effort: failures here only degrade the click
        chain, they never tear down the page session.
        """
        try:
            await self._client.send(
                "Target.activateTarget", {"targetId": self._target_id}, timeout=5,
            )
        except Exception:
            pass
        try:
            await self._session.send("Page.bringToFront", timeout=5)
        except Exception:
            pass
        try:
            await self._session.send(
                "Emulation.setFocusEmulationEnabled", {"enabled": True}, timeout=5,
            )
        except Exception:
            pass

    async def route(self, *_a: Any, **_kw: Any) -> None:
        """Route interception is only used by the legacy SingleFile-library
        CSP-strip path, which is not part of the SB pipeline (fast capture is
        CSP-immune).  Best-effort no-op with a debug trail."""
        logger.debug("[SB] page.route() no-op (not used on SeleniumBase backend)")

    async def screenshot(self, type: str = "png", quality: Optional[int] = None, **_kw: Any) -> bytes:  # noqa: A002
        params: Dict[str, Any] = {"format": "png" if type != "jpeg" else "jpeg"}
        if type == "jpeg" and quality is not None:
            params["quality"] = int(quality)
        res = await self._session.send("Page.captureScreenshot", params, timeout=30)
        data = (res or {}).get("data", "")
        return base64.b64decode(data) if data else b""

    # ---- state --------------------------------------------------------------

    @property
    def url(self) -> str:
        return self._url

    @property
    def context(self) -> "SBContext":
        return self._ctx

    def is_closed(self) -> bool:
        return self._closed

    async def close(self, **_kw: Any) -> None:
        if self._closed:
            return
        try:
            await self._client.send("Target.closeTarget", {"targetId": self._target_id}, timeout=5)
        except Exception:
            pass
        self._mark_closed()

    # ---- CAPTCHA --------------------------------------------------------------

    def _maybe_solve_captcha(self) -> None:
        if captcha_mode() != "auto":
            return
        handle = self._ctx._handle
        if handle is None:
            return
        async def _probe_and_solve() -> None:
            try:
                hit = await self.evaluate(
                    """() => !!document.querySelector(
                        'iframe[src*="recaptcha"], iframe[src*="challenges.cloudflare"],'
                        ' .cf-turnstile, #challenge-stage, #challenge-form,'
                        ' .rc-anchor-checkbox, [data-sitekey]')"""
                )
            except Exception:
                return
            if not hit:
                return
            logger.debug("[SB] CAPTCHA challenge detected on %s — uc_gui_click_captcha x2", self._url[:80])
            for _attempt in range(2):
                ok = False
                try:
                    ok = await handle.solve_captcha()
                except Exception as exc:
                    logger.debug("[SB] captcha solver attempt failed: %s", exc)
                if ok:
                    return
                await asyncio.sleep(1.5)
        try:
            asyncio.create_task(_probe_and_solve())
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Context / browser adapters
# ---------------------------------------------------------------------------

_PERM_MAP = {
    "notifications": "notifications", "geolocation": "geolocation",
    "camera": "videoCapture", "microphone": "audioCapture",
    "midi": "midi", "clipboard-read": "clipboardReadWrite",
    "clipboard-write": "clipboardSanitizedWrite",
}


class SBContext:
    """Playwright BrowserContext-shaped adapter over the SB browser."""

    def __init__(self, browser: "SBBrowser", mobile: bool = False,
                 pixel_ratio: float = 1.0) -> None:
        self._browser = browser
        self._handle = browser._handle
        self.pages: List[SBPage] = []
        # Page targets that already existed when we attached (profile-restored
        # tabs like google.com, NTP): closed as soon as our own first target
        # exists, so we never drive an invisible adopted tab while a restored
        # tab sits in the foreground.
        self._startup_page_ids: List[str] = []
        # Target.createTarget emits Target.targetCreated before new_page()
        # finishes attaching.  Reserve targets here so the lifecycle listener
        # does not attach the same tab a second time (two CDP sessions on one
        # page made focus and native-target mapping nondeterministic).
        self._attaching_page_ids: Set[str] = set()
        # Target.targetCreated can be delivered before Target.createTarget's
        # response reaches new_page(). Suppress that one lifecycle callback
        # while our own target is being created; new_page() attaches it and
        # popup targets created later still emit normally.
        self._creating_page_requests = 0
        self._mobile = mobile
        self._pixel_ratio = pixel_ratio
        self._listeners: Dict[str, List[Callable]] = {}

    @property
    def browser(self) -> "SBBrowser":
        return self._browser

    def on(self, event: str, callback: Callable) -> None:
        self._listeners.setdefault(event, []).append(callback)

    def _emit(self, event: str, *args: Any) -> None:
        for cb in list(self._listeners.get(event, ())):
            try:
                r = cb(*args)
                if inspect.isawaitable(r):
                    asyncio.create_task(r)
            except Exception:
                pass

    async def new_page(self) -> SBPage:
        # ALWAYS create our own page target.  Adopting Chrome's startup tab
        # "works" but is wrong in practice: with a persistent profile, Chrome
        # restores session tabs (e.g. google.com) into the foreground while an
        # adopted tab stays invisible — navigation then succeeds on paper
        # while the visible window never changes ("only opens google.com").
        self._creating_page_requests += 1
        try:
            res = await self._browser._client.send("Target.createTarget", {"url": "about:blank"})
        finally:
            self._creating_page_requests = max(0, self._creating_page_requests - 1)
        tid = (res or {}).get("targetId")
        if not tid:
            raise RuntimeError("SB Target.createTarget returned no targetId")
        self._attaching_page_ids.add(tid)
        try:
            page = await self._browser._attach_page(tid, self)
        finally:
            self._attaching_page_ids.discard(tid)
        if self._startup_page_ids:
            # Our target exists now — close the pre-existing page targets and
            # bring ours to the foreground.  Best-effort: never fatal.
            stale = list(self._startup_page_ids)
            self._startup_page_ids = []
            for old_tid in stale:
                if old_tid == tid:
                    continue
                try:
                    await self._browser._client.send(
                        "Target.closeTarget", {"targetId": old_tid}, timeout=5)
                except Exception:
                    pass
        # ALWAYS activate the newly-created target at the browser level,
        # not just when startup pages were present.  Without this, a fresh
        # profile (no restored tabs) leaves the about:blank target in the
        # background; subsequent CDP input is silently dropped because
        # Chrome discards events for non-foreground renderers.
        try:
            await self._browser._client.send(
                "Target.activateTarget", {"targetId": tid}, timeout=5)
        except Exception:
            pass
        # Belt-and-suspenders: also flip the focus-emulation flag on the
        # page session so input flows even if OS-level activation fails.
        try:
            await page._session.send(
                "Emulation.setFocusEmulationEnabled", {"enabled": True}, timeout=5)
        except Exception:
            pass
        try:
            await page._session.send("Page.bringToFront", timeout=5)
        except Exception:
            pass
        return page

    async def _adopt_initial(self, target_id: str) -> SBPage:
        """Attach a pre-existing target and mark it as a startup page (closed
        once the first new_page() target exists).  Kept as an explicit helper
        for tests/tools; _launch_stack no longer auto-adopts anything."""
        page = await self._browser._attach_page(target_id, self)
        if target_id not in self._startup_page_ids:
            self._startup_page_ids.append(target_id)
        return page

    async def _on_target_created(self, target_id: str, opener_id: Optional[str]) -> None:
        # A popup/new tab that isn't one of ours yet -> adopt + emit 'page'.
        # new_page() reserves its own target while it is attaching; do not
        # create a duplicate SBPage for the Target.targetCreated event. The
        # request counter covers the small interval before createTarget's
        # response lets new_page() add the target id to that set.
        if self._creating_page_requests:
            return
        if target_id in self._attaching_page_ids or any(p._target_id == target_id for p in self.pages):
            return
        try:
            page = await self._browser._attach_page(target_id, self, opener_id=opener_id)
            opener = next((p for p in self.pages if p._target_id == opener_id), None)
            if opener is not None:
                opener._emit("popup", page)
            self._emit("page", page)
        except Exception as exc:
            logger.debug("[SB] popup adopt failed: %s", exc)

    async def add_cookies(self, cookies: List[Dict[str, Any]]) -> None:
        if not cookies:
            return
        page = self.pages[0] if self.pages else await self.new_page()
        params = []
        for c in cookies:
            item: Dict[str, Any] = {
                "name": c.get("name"), "value": c.get("value", ""),
                "domain": c.get("domain"), "path": c.get("path", "/"),
                "secure": bool(c.get("secure", False)), "httpOnly": bool(c.get("httpOnly", False)),
            }
            if c.get("sameSite") in ("Strict", "Lax", "None"):
                item["sameSite"] = c["sameSite"]
            if c.get("expires"):
                item["expires"] = c["expires"]
            if c.get("url"):
                item["url"] = c["url"]
            params.append({k: v for k, v in item.items() if v is not None})
        try:
            await page._session.send("Network.enable")
            page._network_enabled = True
        except Exception:
            pass
        await page._session.send("Network.setCookies", {"cookies": params}, timeout=15)

    async def cookies(self, *_a: Any, **_kw: Any) -> List[Dict[str, Any]]:
        try:
            res = await self._browser._client.send("Storage.getCookies", timeout=15)
            return (res or {}).get("cookies") or []
        except Exception:
            pass
        page = self.pages[0] if self.pages else None
        if page is not None:
            try:
                res = await page._session.send("Network.getAllCookies", timeout=15)
                return (res or {}).get("cookies") or []
            except Exception:
                pass
        return []

    async def grant_permissions(self, permissions: List[Any], origin: Optional[str] = None) -> None:
        """Playwright-shaped: a list, where entries may be plain names or
        {'type': name, 'origin': url} dicts (session.py passes the latter)."""
        names: List[str] = []
        for p in permissions or []:
            if isinstance(p, dict):
                names.append(p.get("type"))
                if p.get("origin") and not origin:
                    origin = p["origin"]
            elif isinstance(p, str):
                names.append(p)
        mapped = sorted({_PERM_MAP[n] for n in names if n in _PERM_MAP})
        if not mapped:
            logger.debug("[SB] grant_permissions: nothing mappable in %r", names)
            return
        params: Dict[str, Any] = {"permissions": mapped}
        if origin:
            params["origin"] = origin
        await self._browser._client.send("Browser.grantPermissions", params, timeout=10)

    async def set_viewport_size(self, size: Dict[str, Any]) -> None:
        for p in list(self.pages):
            if p is not None and not p.is_closed():
                try:
                    await p.set_viewport_size(size)
                except Exception:
                    pass

    async def new_cdp_session(self, page: SBPage) -> _CDPSession:
        res = await self._browser._client.send(
            "Target.attachToTarget", {"targetId": page._target_id, "flatten": True}, timeout=10,
        )
        sid = (res or {}).get("sessionId")
        if not sid:
            raise RuntimeError("SB attach for cdp session failed")
        return _CDPSession(self._browser._client, sid)

    async def close(self) -> None:
        await self._browser.close()


class SBBrowser:
    """Handle-holding adapter: owns the CDP client and the SB driver thread."""

    def __init__(self, handle: "SBHandle", client: _CDPClient) -> None:
        self._handle = handle
        self._client = client
        self.contexts: List[SBContext] = []
        self._closed = False

    def _attach_page_blocking(self, *_a: Any, **_kw: Any) -> None:
        raise RuntimeError("internal: use _attach_page")

    async def _attach_page(self, target_id: str, ctx: SBContext,
                           opener_id: Optional[str] = None) -> SBPage:
        res = await self._client.send(
            "Target.attachToTarget", {"targetId": target_id, "flatten": True}, timeout=15,
        )
        sid = (res or {}).get("sessionId")
        if not sid:
            raise RuntimeError(f"SB attach failed for target {target_id}")
        page = SBPage(self._client, target_id, sid, ctx, opener_id=opener_id)
        await page._init()
        if self._handle is not None and self._handle.viewport:
            try:
                await page.set_viewport_size(self._handle.viewport)
            except Exception:
                pass
        if self._handle is not None and self._handle.user_agent:
            try:
                await page._session.send("Emulation.setUserAgentOverride", {
                    "userAgent": self._handle.user_agent,
                })
            except Exception:
                pass
        if self._handle is not None and self._handle.mobile:
            try:
                await page._session.send("Emulation.setTouchEmulationEnabled", {"enabled": True, "maxTouchPoints": 5})
            except Exception:
                pass
        if page not in ctx.pages:
            ctx.pages.append(page)
        try:
            if ctx not in self.contexts:
                self.contexts.append(ctx)
        except Exception:
            pass
        return page

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            await self._client.close()
        except Exception:
            pass
        if self._handle is not None:
            try:
                await self._handle.stop()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# SBHandle — SeleniumBase UC driver in a dedicated thread (sync API)
# ---------------------------------------------------------------------------

class SBHandle:
    """Owns one SeleniumBase browser and its private headed Xvfb display.

    The Xvfb process is created before ``Driver`` and stopped with the driver;
    this path never adopts the application's shared Xvfb manager or an
    inherited ``DISPLAY``.
    """

    def __init__(self, profile_dir: Optional[str] = None,
                 viewport: Optional[Dict[str, Any]] = None,
                 user_agent: Optional[str] = None,
                 proxy_url: Optional[str] = None,
                 mobile: bool = False,
                 pixel_ratio: float = 1.0,
                 headless: bool = False,
                 extra_args: Optional[List[str]] = None) -> None:
        self.profile_dir = profile_dir
        self.viewport = viewport or {"width": 1280, "height": 720}
        self.user_agent = user_agent
        self.proxy_url = proxy_url
        self.mobile = mobile
        self.pixel_ratio = pixel_ratio
        self.headless = headless
        self.extra_args = extra_args or []
        self.driver: Any = None
        self.debug_port: Optional[int] = None
        self.browser_ws_url: Optional[str] = None
        self._debugger_port: Optional[int] = None
        self._debugger_host: str = "127.0.0.1"
        self._ex: Optional[concurrent.futures.ThreadPoolExecutor] = None
        self._stopped = False
        self._xvfb_process: Any = None
        self._xvfb_display: Optional[str] = None
        self._xvfb_stderr_task: Optional[asyncio.Task] = None
        # ChromeDriver/Selenium objects are thread-affine.  Keep every native
        # action on the same worker that created the Driver; using a second
        # executor worker can make an otherwise valid action target a stale
        # WebDriver session or the wrong tab.
        self._target_window_handles: Dict[str, str] = {}

    # ---- sync side (runs inside the executor thread) ----

    def _launch_sync(self, port: int) -> None:
        from seleniumbase import Driver  # lazy: module must import without SB
        if self.profile_dir:
            kill_profile_processes(self.profile_dir)
            clean_profile_locks(self.profile_dir)

        # NOTE: SB's chromium_arg is COMMA-separated (verified against
        # seleniumbase/plugins/driver_manager.py, SB 4.53.x).  We deliberately
        # do NOT force --remote-debugging-port: undetected-chromedriver owns
        # that flag for its own channel and conflicts break UC's attach.
        # Chrome's actual port is read from DevToolsActivePort in the profile
        # (deterministic), with probed fallbacks.
        args = [
            "--remote-allow-origins=*",
            "--force-device-scale-factor=1",
            "--password-store=basic",
            "--enable-features=PasswordManager,CredentialManager",
        ]
        if sys.platform.startswith("linux"):
            args.append("--disable-dev-shm-usage")
            if _should_disable_sandbox():
                args.extend(["--no-sandbox", "--disable-setuid-sandbox"])
        args.extend(self.extra_args)
        # Keep the UC launcher's argument surface minimal: SeleniumBase does
        # not load/import a browser extension on this path.
        proxy = (self.proxy_url or "").strip() or None
        if proxy and "://" in proxy:
            proxy = proxy.split("://", 1)[1]   # SB wants host:port / user:pass@host:port
        kwargs: Dict[str, Any] = {
            "uc": True,
            "headless": bool(self.headless),
            "chromium_arg": ",".join(args),
            "window_size": "%d,%d" % (
                int(self.viewport.get("width", 1280)),
                int(self.viewport.get("height", 720)),
            ),
        }
        if _should_disable_sandbox():
            kwargs["no_sandbox"] = True
        if self.profile_dir:
            kwargs["user_data_dir"] = self.profile_dir
        if proxy:
            kwargs["proxy"] = proxy

        # SeleniumBase does not expose a reliable per-Driver environment kwarg.
        # Set DISPLAY only for the synchronous Driver construction and restore
        # the parent process immediately afterward. The thread lock prevents
        # concurrent SB handles from crossing their private displays.
        with _SB_DRIVER_ENV_LOCK:
            previous_display = os.environ.get("DISPLAY")
            previous_wayland = os.environ.get("WAYLAND_DISPLAY")
            if self._xvfb_display:
                os.environ["DISPLAY"] = self._xvfb_display
                os.environ.pop("WAYLAND_DISPLAY", None)
            try:
                self.driver = Driver(**kwargs)
            finally:
                if previous_display is None:
                    os.environ.pop("DISPLAY", None)
                else:
                    os.environ["DISPLAY"] = previous_display
                if previous_wayland is None:
                    os.environ.pop("WAYLAND_DISPLAY", None)
                else:
                    os.environ["WAYLAND_DISPLAY"] = previous_wayland

        # The ONLY fully reliable debug endpoint source: chromedriver tells
        # us where its browser listens.  DevToolsActivePort is a fallback
        # (UC's re-attach dance can rewrite it late or into a copied
        # profile dir — file-based discovery caused false "unreadable"
        # failures on live boxes).
        try:
            caps = getattr(self.driver, "capabilities", None) or {}
            addr = (caps.get("goog:chromeOptions") or {}).get("debuggerAddress")
            if isinstance(addr, str) and ":" in addr:
                host, _, prt = addr.rpartition(":")
                if prt.strip().isdigit():
                    self._debugger_port = int(prt.strip())
                    # chromedriver may report "localhost" — normalize
                    self._debugger_host = "127.0.0.1" if host.strip() in ("localhost", "") else host.strip()
        except Exception:
            pass

    def _solve_captcha_sync(self) -> bool:
        try:
            if self.driver is None:
                return False
            self.driver.uc_gui_click_captcha()
            return True
        except Exception as exc:
            logger.debug("[SB] uc_gui_click_captcha failed: %s", exc)
            return False

    # ---- chromedriver W3C Actions input path ----
    #
    # WHY THIS EXISTS: When SeleniumBase UC launches Chrome through
    # undetected-chromedriver, the resulting Chrome process has TWO
    # simultaneously-attached CDP sessions: chromedriver's own (which holds
    # the W3C WebDriver session for the page) and ours (which we use for
    # everything else). In Chrome 100+ this dual-session state silently
    # degrades raw ``Input.dispatchMouseEvent`` clicks from the
    # non-WebDriver session on real system Chrome — the renderer reports
    # the events were delivered but DOM handlers that bind on
    # ``pointerdown`` / ``mousedown`` / ``click`` activation (the entire
    # class of "modern web app" buttons, Apple's ui-button, MUI/AntD
    # buttons, anything React with onClick on a non-button) do not fire.
    # Playwright does not hit this because Playwright owns its own
    # chromedriver-equivalent and is the only attached session.
    #
    # The bypass: route the click through chromedriver's W3C Actions API
    # (``POST /session/{id}/actions``). Chromedriver translates the
    # actions into CDP input dispatched through ITS OWN session — the
    # trusted one — and the click reliably fires DOM handlers.  This is
    # exactly what ``seleniumbase.Driver.click(selector)`` does
    # internally; we use the raw W3C endpoint so we can pass coordinates
    # computed via our actionability probe instead of relying on a
    # second selector query against chromedriver (which would re-resolve
    # and could pick a different element if the DOM mutated).
    #
    # The mapping problem: chromedriver's "current window handle" must
    # be our CDP target.  We solve this by asking chromedriver for its
    # handle list (``GET /session/{id}/window/handles``), iterating, and
    # matching by the handle's underlying targetId via
    # ``Target.getTargetInfo`` for each (Chrome exposes
    # ``targetInfo`` only through CDP — we use the browser-level CDP
    # websocket).  This costs one round-trip per click and is only
    # invoked when CDP clicks fail.

    def _switch_to_target_sync(self, target_id: str) -> bool:
        """Switch ChromeDriver to the CDP target represented by ``target_id``.

        ChromeDriver normally exposes a page handle as ``CDwindow-<targetId>``
        while CDP exposes the bare target id.  Older ChromeDriver builds expose
        the bare id, so accept both forms and remember the successful mapping.
        Crucially, do not silently choose the last tab when there are several:
        that was the source of input being sent to the visible/restored Google
        tab while CDP drove a different page.
        """
        drv = self.driver
        if drv is None:
            return False
        try:
            handles = list(drv.window_handles or [])
        except Exception as exc:
            logger.warning("[SB][W3C-INPUT] cannot read window handles: %s", exc)
            return False
        if not handles:
            return False

        known = self._target_window_handles.get(target_id)
        candidates = [target_id]
        if target_id and not target_id.startswith("CDwindow-"):
            candidates.append("CDwindow-" + target_id)
        if known:
            candidates.insert(0, known)
        chosen = next((h for h in candidates if h in handles), None)

        # Some ChromeDriver versions return a handle with a different prefix,
        # but retain the target id as a suffix.  This is still an exact match,
        # unlike guessing the last handle.
        if chosen is None and target_id:
            chosen = next((h for h in handles if str(h).endswith(str(target_id))), None)
        if chosen is None and len(handles) == 1:
            chosen = handles[0]
        if chosen is None:
            logger.warning(
                "[SB][W3C-INPUT] target %s is not represented by driver handles %s",
                target_id, handles,
            )
            return False
        try:
            current = getattr(drv, "current_window_handle", None)
            if current != chosen:
                drv.switch_to.window(chosen)
            self._target_window_handles[target_id] = chosen
            return True
        except Exception as exc:
            logger.warning("[SB][W3C-INPUT] switch to target %s failed: %s", target_id, exc)
            return False

    @staticmethod
    def _wd_key_value(key: str) -> str:
        """Translate a DOM/Playwright key name to a WebDriver key value."""
        special = {
            "Null": "\ue000", "Cancel": "\ue001", "Help": "\ue002",
            "Backspace": "\ue003", "Tab": "\ue004", "Clear": "\ue005",
            "Return": "\ue006", "Enter": "\ue007", "Shift": "\ue008",
            "Control": "\ue009", "Alt": "\ue00a", "Pause": "\ue00b",
            "Escape": "\ue00c", "Space": "\ue00d", "PageUp": "\ue00e",
            "PageDown": "\ue00f", "End": "\ue010", "Home": "\ue011",
            "ArrowLeft": "\ue012", "ArrowUp": "\ue013",
            "ArrowRight": "\ue014", "ArrowDown": "\ue015",
            "Insert": "\ue016", "Delete": "\ue017", "Semicolon": ";",
            "Equals": "=", "Meta": "\ue03d", "Command": "\ue03d",
            "Spacebar": "\ue00d",
        }
        if key in special:
            return special[key]
        if len(key) == 2 and key.startswith("F") and key[1].isdigit():
            n = int(key[1])
            if 1 <= n <= 9:
                return chr(0xE030 + n)
        if len(key) == 3 and key.startswith("F") and key[1:].isdigit():
            n = int(key[1:])
            if 10 <= n <= 12:
                return chr(0xE030 + n)
        return key

    @staticmethod
    def _action_ok(response: Any) -> bool:
        """Interpret Selenium's several success response shapes."""
        if response is None:
            return True
        if not isinstance(response, dict):
            return True
        if response.get("status") not in (None, 0, "0", "success"):
            return False
        value = response.get("value")
        if isinstance(value, dict) and value.get("error"):
            return False
        return True

    def _execute_actions_sync(self, payload: Dict[str, Any]) -> bool:
        drv = self.driver
        if drv is None:
            return False
        try:
            # WebDriver.execute() uses the W3C command map and works across
            # Selenium 4 releases.  Keep the command string as a fallback for
            # SeleniumBase versions that do not expose execute publicly.
            executor = getattr(drv, "execute", None)
            if executor is not None:
                return self._action_ok(executor("actions", payload))
            cmd = getattr(drv, "command_executor", None)
            raw = getattr(cmd, "execute", None) if cmd is not None else None
            if raw is None:
                return False
            return self._action_ok(raw("actions", payload))
        except Exception as exc:
            logger.warning("[SB][W3C-INPUT] actions command failed: %s", exc)
            return False

    def _native_pointer_action_sync(self, target_id: str, action: str,
                                    x: float, y: float, button: str = "left",
                                    delta_x: float = 0, delta_y: float = 0) -> bool:
        """Send one native W3C pointer/wheel action to a specific target."""
        if not self._switch_to_target_sync(target_id):
            return False
        button_map = {"left": 0, "middle": 1, "right": 2, "back": 3, "forward": 4}
        b = button_map.get(button, 0)
        pointer_steps: List[Dict[str, Any]] = []
        if action in ("move", "down", "up", "click"):
            pointer_steps.append({
                "type": "pointerMove", "duration": 0,
                "x": int(round(x)), "y": int(round(y)), "origin": "viewport",
            })
            if action in ("down", "click"):
                pointer_steps.append({"type": "pointerDown", "duration": 0, "button": b})
            if action in ("up", "click"):
                pointer_steps.append({"type": "pointerUp", "duration": 0, "button": b})
            payload = {"actions": [{
                "type": "pointer", "id": "sb-pointer",
                "parameters": {"pointerType": "mouse"},
                "actions": pointer_steps,
            }]}
        elif action == "wheel":
            payload = {"actions": [{
                "type": "wheel", "id": "sb-wheel", "actions": [{
                    "type": "scroll", "x": int(round(x)), "y": int(round(y)),
                    "deltaX": int(round(delta_x)), "deltaY": int(round(delta_y)),
                    "duration": 0, "origin": "viewport",
                }],
            }]}
        else:
            return False
        return self._execute_actions_sync(payload)

    def _native_key_action_sync(self, target_id: str, action: str, key: str) -> bool:
        if not self._switch_to_target_sync(target_id):
            return False
        if action not in ("keyDown", "keyUp"):
            return False
        payload = {"actions": [{
            "type": "key", "id": "sb-keyboard",
            "actions": [{"type": action, "value": self._wd_key_value(key)}],
        }]}
        return self._execute_actions_sync(payload)

    def _native_insert_text_sync(self, target_id: str, text: str) -> bool:
        if not self._switch_to_target_sync(target_id):
            return False
        try:
            active = self.driver.switch_to.active_element
            active.send_keys(text)
            return True
        except Exception as exc:
            logger.warning("[SB][W3C-INPUT] active element text failed: %s", exc)
            return False

    def _native_focus_selector_sync(self, target_id: str, selector: str) -> bool:
        if not self._switch_to_target_sync(target_id):
            return False
        try:
            element = self.driver.find_element("css selector", selector)
            self.driver.execute_script("arguments[0].focus();", element)
            return True
        except Exception as exc:
            logger.debug("[SB][W3C-INPUT] focus selector %s failed: %s", selector, exc)
            return False

    def _native_click_selector_sync(self, target_id: str, selector: str) -> bool:
        """Click a selector through ChromeDriver/SeleniumBase's trusted path."""
        if not self._switch_to_target_sync(target_id):
            return False
        try:
            element = self.driver.find_element("css selector", selector)
            # If the resolved element is a text node / span / icon inside an
            # interactive button or link (e.g. Google Material's <span ...>Next</span>),
            # resolve to the enclosing button/link control that owns the activation.
            try:
                interactive_parent = self.driver.execute_script(
                    """const el = arguments[0];
                    if (!el) return null;
                    const tag = (el.tagName || '').toUpperCase();
                    if (tag === 'BUTTON' || tag === 'A' || tag === 'INPUT') return el;
                    if (el.getAttribute && el.getAttribute('role') === 'button') return el;
                    return el.closest('button, [role="button"], a, input[type="button"], input[type="submit"]') || el;""",
                    element,
                )
                if interactive_parent is not None:
                    element = interactive_parent
            except Exception:
                pass
            # Apple-style controls are often a custom host around the native
            # button that actually owns the pointer activation:
            # <ui-button ...><button type="button">Sign in</button></ui-button>.
            # Resolve that inner button when present, while retaining the host
            # as the fallback for shadow/custom implementations that do not
            # expose a native descendant.  WebDriver's real pointer click then
            # bubbles through the host instead of relying on element.click().
            if (getattr(element, "tag_name", "") or "").lower() == "ui-button":
                try:
                    inner = self.driver.execute_script(
                        """const host = arguments[0];
                        return (host.shadowRoot && host.shadowRoot.querySelector('button'))
                            || host.querySelector('button') || host;""",
                        element,
                    )
                    if inner is not None:
                        element = inner
                except Exception:
                    pass
            try:
                self.driver.execute_script(
                    "arguments[0].scrollIntoView({block:'center', inline:'center'});",
                    element,
                )
            except Exception:
                pass
            try:
                element.click()
                return True
            except Exception:
                try:
                    from selenium.webdriver.common.action_chains import ActionChains
                    ActionChains(self.driver).move_to_element(element).click().perform()
                    return True
                except Exception:
                    return False
        except Exception as exc:
            logger.debug("[SB][W3C-CLICK] selector %s failed: %s", selector, exc)
            return False

    def _w3c_actions_click_sync(self, target_id: str, x: float, y: float,
                                button: str = "left") -> bool:
        """Compatibility wrapper used by the SBPage click recovery path."""
        return self._native_pointer_action_sync(target_id, "click", x, y, button=button)

    async def native_pointer_action(self, target_id: str, action: str,
                                    x: float, y: float, button: str = "left",
                                    delta_x: float = 0, delta_y: float = 0) -> bool:
        if self._ex is None or self._stopped:
            return False
        loop = asyncio.get_running_loop()
        try:
            return await asyncio.wait_for(loop.run_in_executor(
                self._ex, self._native_pointer_action_sync, target_id, action,
                float(x), float(y), button, float(delta_x), float(delta_y)), 10)
        except Exception as exc:
            logger.warning("[SB][W3C-INPUT] native pointer wrapper failed: %s", exc)
            return False

    async def native_key_action(self, target_id: str, action: str, key: str) -> bool:
        if self._ex is None or self._stopped:
            return False
        loop = asyncio.get_running_loop()
        try:
            return await asyncio.wait_for(loop.run_in_executor(
                self._ex, self._native_key_action_sync, target_id, action, key), 10)
        except Exception as exc:
            logger.warning("[SB][W3C-INPUT] native key wrapper failed: %s", exc)
            return False

    async def native_insert_text(self, target_id: str, text: str) -> bool:
        if self._ex is None or self._stopped:
            return False
        loop = asyncio.get_running_loop()
        try:
            return await asyncio.wait_for(loop.run_in_executor(
                self._ex, self._native_insert_text_sync, target_id, text), 10)
        except Exception as exc:
            logger.warning("[SB][W3C-INPUT] native text wrapper failed: %s", exc)
            return False

    async def native_focus_selector(self, target_id: str, selector: str) -> bool:
        if self._ex is None or self._stopped:
            return False
        loop = asyncio.get_running_loop()
        try:
            return await asyncio.wait_for(loop.run_in_executor(
                self._ex, self._native_focus_selector_sync, target_id, selector), 10)
        except Exception as exc:
            logger.warning("[SB][W3C-INPUT] native focus wrapper failed: %s", exc)
            return False

    async def native_click_selector(self, target_id: str, selector: str) -> bool:
        if self._ex is None or self._stopped:
            return False
        loop = asyncio.get_running_loop()
        try:
            return await asyncio.wait_for(loop.run_in_executor(
                self._ex, self._native_click_selector_sync, target_id, selector), 10)
        except Exception as exc:
            logger.warning("[SB][W3C-CLICK] native click wrapper failed: %s", exc)
            return False

    def _quit_sync(self) -> None:
        try:
            browser_pid = getattr(self.driver, "browser_pid", None)
            if self.driver is not None:
                self.driver.quit()
            if browser_pid:
                try:
                    import signal
                    os.kill(browser_pid, signal.SIGKILL)
                except Exception:
                    pass
        except Exception:
            pass
        if self.profile_dir:
            clean_profile_locks(self.profile_dir)

    async def _stop_private_xvfb(self) -> None:
        """Terminate only this browser's Xvfb process."""
        process = self._xvfb_process
        stderr_task = self._xvfb_stderr_task
        self._xvfb_process = None
        self._xvfb_stderr_task = None
        self._xvfb_display = None
        if process is not None:
            try:
                if process.returncode is None:
                    process.terminate()
                await asyncio.wait_for(process.wait(), timeout=5.0)
            except Exception:
                try:
                    if process.returncode is None:
                        process.kill()
                    await process.wait()
                except Exception:
                    pass
        if stderr_task is not None:
            try:
                await asyncio.wait_for(stderr_task, timeout=0.5)
            except Exception:
                stderr_task.cancel()

    # ---- async side ----

    @staticmethod
    def _free_port() -> int:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            return s.getsockname()[1]

    @staticmethod
    def _probe_debug_url(ports: List[int], deadline_s: float = 20.0) -> Optional[str]:
        deadline_s = float(os.environ.get("SB_DEBUG_PROBE_DEADLINE", str(deadline_s)))
        deadline = time.monotonic() + deadline_s
        while time.monotonic() < deadline:
            for port in ports:
                try:
                    with urllib.request.urlopen(
                        f"http://127.0.0.1:{port}/json/version", timeout=1.5,
                    ) as resp:
                        data = json.loads(resp.read().decode())
                    ws = data.get("webSocketDebuggerUrl")
                    if ws:
                        return ws
                except Exception:
                    continue
            time.sleep(0.15)
        return None

    async def start(self) -> None:
        """Launch the UC browser and resolve its debug endpoint."""
        if sys.platform.startswith("linux"):
            await _ensure_sb_xvfb(self)
            if self.headless:
                logger.warning("[SB] ignoring headless=True: SeleniumBase is pinned to private Xvfb-headed mode")
            self.headless = False
        _t0 = time.monotonic()
        if self._ex is None:
            # SeleniumBase/ChromeDriver is thread-affine.  Do not let actions
            # hop between executor workers; that can make input appear to be
            # accepted while it is sent to a stale driver session.
            self._ex = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="sb-driver")
        loop = asyncio.get_running_loop()
        chosen = self._free_port()
        await loop.run_in_executor(self._ex, self._launch_sync, chosen)
        _t1 = time.monotonic()
        # Debug endpoint discovery, strict order, no random-port guessing:
        #   1) chromedriver's debuggerAddress capability (deterministic),
        #   2) DevToolsActivePort polled inside the profile dir (UC re-attach
        #      rewrites it late; poll up to ~8s instead of one-shot read).
        file_port: Optional[int] = None
        if self.profile_dir and not self._debugger_port:
            # Only chase the file when the capability was unavailable; the
            # debuggerAddress from chromedriver is authoritative and the file
            # often never lands in the launch profile (UC copies), which used
            # to cost a flat 8 s on every browser creation.
            port_file = os.path.abspath(os.path.join(self.profile_dir, "DevToolsActivePort"))
            deadline = time.monotonic() + 8.0
            while time.monotonic() < deadline and not file_port:
                try:
                    if os.path.isfile(port_file):
                        with open(port_file, "r", encoding="utf-8", errors="ignore") as fh:
                            first = fh.readline().strip()
                        if first.isdigit():
                            file_port = int(first)
                except Exception:
                    pass
                if not file_port:
                    await asyncio.sleep(0.25)
        candidates: List[int] = []
        if self._debugger_port:
            candidates.append(self._debugger_port)
        if file_port and file_port not in candidates:
            candidates.append(file_port)
        if not candidates:
            raise RuntimeError(
                f"[SB] no debug endpoint found (no debuggerAddress capability, "
                f"no DevToolsActivePort in {self.profile_dir}) — browser will be "
                f"closed and Playwright fallback used"
            )
        ws = await asyncio.to_thread(self._probe_debug_url, candidates, 20.0)
        if not ws:
            raise RuntimeError(
                f"[SB] UC browser launched but no CDP endpoint answered on {candidates} "
                f"(profile={self.profile_dir}) — browser will be closed and Playwright fallback used"
            )
        self.browser_ws_url = ws
        m = re.search(r":(\d+)/devtools/browser", ws)
        self.debug_port = int(m.group(1)) if m else chosen
        logger.info(
            "[SB] browser up: debug_port=%s profile=%s (driver_launch=%.1fs endpoint_resolve=%.1fs)",
            self.debug_port, self.profile_dir,
            _t1 - _t0, time.monotonic() - _t1,
        )

    async def solve_captcha(self) -> bool:
        if self._ex is None or self._stopped:
            return False
        loop = asyncio.get_running_loop()
        try:
            return await asyncio.wait_for(
                loop.run_in_executor(self._ex, self._solve_captcha_sync), timeout=20,
            )
        except Exception:
            return False

    async def w3c_actions_click(self, target_id: str, x: float, y: float, button: str = "left") -> bool:
        """Async wrapper around ``_w3c_actions_click_sync``.  Returns True
        if chromedriver accepted the W3C actions and did not error.  Used
        as the recovery path when raw CDP ``Input.dispatchMouseEvent``
        clicks land in the renderer but DOM handlers (especially
        ``pointerdown`` / ``mousedown`` activation handlers in modern
        frameworks) silently do not fire.  See the comment above
        ``_w3c_actions_click_sync`` for the full diagnosis.
        """
        if self._ex is None or self._stopped:
            return False
        loop = asyncio.get_running_loop()
        try:
            return await asyncio.wait_for(
                loop.run_in_executor(
                    self._ex, self._w3c_actions_click_sync,
                    target_id, float(x), float(y), button,
                ),
                timeout=10,
            )
        except Exception as exc:
            logger.debug("[SB][W3C-CLICK] async wrapper failed: %s", exc)
            return False

    async def stop(self) -> None:
        if self._stopped and self._xvfb_process is None:
            return
        self._stopped = True
        try:
            if self._ex is not None:
                loop = asyncio.get_running_loop()
                await asyncio.wait_for(loop.run_in_executor(self._ex, self._quit_sync), timeout=10)
        except Exception:
            pass
        try:
            if self._ex is not None:
                self._ex.shutdown(wait=False, cancel_futures=True)
        except Exception:
            pass
        await self._stop_private_xvfb()


# ---------------------------------------------------------------------------
# Launch factories
# ---------------------------------------------------------------------------

async def _ensure_sb_xvfb(handle: "SBHandle") -> Optional[str]:
    """Create one private headed Xvfb display for one SB browser.

    This intentionally does not call ``get_xvfb_manager().start*()`` and does
    not inherit or publish the application's ``DISPLAY``. The existing
    manager may still be used as an installer when the Xvfb binary is missing,
    but the process created here is owned by ``handle`` and is stopped with
    that browser.
    """
    if not sys.platform.startswith("linux"):
        return None

    process = handle._xvfb_process
    if process is not None and process.returncode is None:
        return handle._xvfb_display
    handle._xvfb_process = None
    handle._xvfb_display = None

    xvfb_bin = shutil.which("Xvfb")
    if not xvfb_bin:
        # Keep the existing best-effort package installation behavior, but do
        # not use the shared manager's display even if it already has one.
        try:
            from browser_manager import get_xvfb_manager
            installer = get_xvfb_manager()
            if not installer.ensure_checked():
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(None, installer.try_install)
        except Exception as exc:
            logger.debug("[SB] private Xvfb installer failed: %s", exc)
        xvfb_bin = shutil.which("Xvfb")
    if not xvfb_bin:
        raise RuntimeError(
            "SeleniumBase requires Xvfb on Linux; install xvfb before selecting "
            "BROWSER_BACKEND=sb"
        )

    errors: List[str] = []
    async with _SB_XVFB_ALLOC_LOCK:
        # A different worker/process may own the traditional display range.
        # Use a broad range and verify the child rather than trusting only the
        # lock-file probe, which is subject to races.
        for display_num in range(99, 200):
            lock_path = f"/tmp/.X{display_num}-lock"
            socket_path = f"/tmp/.X11-unix/X{display_num}"
            if os.path.exists(lock_path) or os.path.exists(socket_path):
                continue

            proc = None
            try:
                proc = await asyncio.create_subprocess_exec(
                    xvfb_bin,
                    f":{display_num}",
                    "-screen", "0", "1920x1080x24", "-ac",
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.PIPE,
                )
                await asyncio.sleep(0.5)
                if proc.returncode is not None:
                    detail = ""
                    if proc.stderr is not None:
                        try:
                            raw = await asyncio.wait_for(proc.stderr.read(), timeout=1.0)
                            detail = raw.decode("utf-8", errors="replace").strip()
                        except Exception:
                            pass
                    errors.append(
                        f":{display_num} exited with code {proc.returncode}"
                        + (f" ({detail[-300:]})" if detail else "")
                    )
                    continue

                handle._xvfb_process = proc
                handle._xvfb_display = f":{display_num}"
                # Drain the long-lived stderr pipe so Xvfb cannot block after
                # emitting warnings; startup failures were read above.
                if proc.stderr is not None:
                    handle._xvfb_stderr_task = asyncio.create_task(proc.stderr.read())
                logger.info(
                    "[SB] created private headed Xvfb display %s for browser",
                    handle._xvfb_display,
                )
                return handle._xvfb_display
            except Exception as exc:
                errors.append(f":{display_num} launch failed ({exc})")
                if proc is not None and proc.returncode is None:
                    try:
                        proc.terminate()
                        await proc.wait()
                    except Exception:
                        pass

    detail = "; ".join(errors[-3:])
    raise RuntimeError(
        "SeleniumBase could not create a private Xvfb display in :99-:199"
        + (f": {detail}" if detail else "")
    )


async def _launch_stack(profile_dir: Optional[str], viewport: Dict[str, Any],
                        user_agent: Optional[str], proxy_url: Optional[str],
                        mobile: bool, pixel_ratio: float,
                        headless: bool) -> SBBrowser:
    """Shared launch: driver thread + CDP client + context + initial tab."""
    try:
        from browser_manager import is_apple_mobile_user_agent, normalize_mobile_user_agent
        if is_apple_mobile_user_agent(user_agent):
            user_agent = normalize_mobile_user_agent(user_agent)
            mobile = True
    except Exception:
        pass
    handle = SBHandle(profile_dir=profile_dir, viewport=viewport, user_agent=user_agent,
                      proxy_url=proxy_url, mobile=mobile, pixel_ratio=pixel_ratio,
                      headless=headless)
    client = _CDPClient()
    try:
        await handle.start()
        await client.connect(handle.browser_ws_url)

        browser = SBBrowser(handle, client)
        ctx = SBContext(browser, mobile=mobile, pixel_ratio=pixel_ratio)
        browser.contexts.append(ctx)

        # Route browser-level target lifecycle.
        await client.send("Target.setDiscoverTargets", {"discover": True}, timeout=10)
    except Exception:
        # Never leak the UC driver/Chrome when attach fails — the caller
        # falls back to Playwright, and an abandoned SB browser would look
        # like "browser opened but nothing is connected".
        try:
            await client.close()
        except Exception:
            pass
        try:
            await handle.stop()
        except Exception:
            pass
        raise

    def _target_created(params: dict) -> None:
        info = (params or {}).get("targetInfo") or {}
        if info.get("type") != "page":
            return
        tid = info.get("targetId")
        if not tid:
            return
        try:
            asyncio.get_running_loop().create_task(
                ctx._on_target_created(tid, info.get("openerId"))
            )
        except Exception:
            pass

    def _target_destroyed(params: dict) -> None:
        tid = (params or {}).get("targetId")
        for page in list(ctx.pages):
            if page is not None and page._target_id == tid:
                page._mark_closed()
                if page in ctx.pages:
                    ctx.pages.remove(page)

    def _info_changed(params: dict) -> None:
        info = (params or {}).get("targetInfo") or {}
        tid, url = info.get("targetId"), info.get("url")
        for page in list(ctx.pages):
            if page is not None and page._target_id == tid and url:
                page._update_url(url)

    client.on("Target.targetCreated", _target_created)
    client.on("Target.targetDestroyed", _target_destroyed)
    client.on("Target.targetInfoChanged", _info_changed)

    # Register (do NOT adopt) Chrome's startup page targets: the first
    # new_page() will create our own tab and close these, so a restored tab
    # can never stay foreground while we drive an invisible one.
    try:
        deadline = time.monotonic() + 4.0
        while time.monotonic() < deadline and not ctx._startup_page_ids:
            try:
                res = await client.send("Target.getTargets", timeout=5)
                for t in (res or {}).get("targetInfos") or []:
                    if t.get("type") == "page" and not info_is_devtools(t.get("url")):
                        tid0 = t.get("targetId")
                        if tid0 and tid0 not in ctx._startup_page_ids:
                            ctx._startup_page_ids.append(tid0)
            except Exception:
                pass
            if not ctx._startup_page_ids:
                await asyncio.sleep(0.25)
        return browser
    except Exception:
        try:
            await client.close()
        except Exception:
            pass
        try:
            await handle.stop()
        except Exception:
            pass
        raise


def info_is_devtools(url: Optional[str]) -> bool:
    u = (url or "").lower()
    return u.startswith("devtools://") or u.startswith("chrome-extension://")


async def launch_for_session(session_id: str, viewport: Dict[str, Any],
                             pixel_ratio: float, user_id: str,
                             user_agent: Optional[str], is_mobile: bool,
                             profile_dir: Optional[str],
                             proxy_url: Optional[str] = None,
                             headless: bool = False) -> SBBrowser:
    """Session-path factory: returns an SBBrowser whose .contexts[0] is the
    Playwright-context stand-in for session.py."""
    logger.debug("[SB] launching UC browser for session %s (mobile=%s, profile=%s)",
                 session_id, is_mobile, profile_dir)
    return await _launch_stack(profile_dir, viewport, user_agent, proxy_url,
                               is_mobile, pixel_ratio, headless)


async def launch_for_pcm(profile_dir: Optional[str], viewport: Dict[str, Any],
                         user_agent: Optional[str], mode: str = "desktop",
                         headless: bool = False) -> SBBrowser:
    """PCM pilot factory: desktop/mobile preview browser."""
    mobile = (mode == "mobile")
    logger.debug("[SB] launching UC browser for PCM (%s, profile=%s)", mode, profile_dir)
    return await _launch_stack(profile_dir, viewport, user_agent, None, mobile, 1.0, headless)


async def launch_for_access(profile_dir: Optional[str], viewport: Dict[str, Any],
                            user_agent: Optional[str],
                            headless: bool = False) -> SBBrowser:
    """Launch one operator Access browser with its own SB/Xvfb ownership."""
    logger.debug("[SB] launching UC browser for Access (profile=%s)", profile_dir)
    return await _launch_stack(profile_dir, viewport, user_agent, None, False, 1.0, headless)


__all__ = [
    "browser_backend",
    "captcha_mode",
    "SBHandle",
    "SBBrowser",
    "SBContext",
    "SBPage",
    "launch_for_session",
    "launch_for_pcm",
    "launch_for_access",
]
